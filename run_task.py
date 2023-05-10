import json
import hmac
import hashlib
import queue
import requests
import os
import time
from typing import Dict, Any
from threading import Thread
from flask import Flask, request, Response as Flask_Response, send_from_directory

app = Flask(__name__)  # Flask app
run_ids_queue = queue.SimpleQueue()  # Queue to store the run ids that need to be applied
processing_thread = None
debug_statements = False


# This code will handle the case when a secrets file was mounted at /etc/secrets/.env as well as the case when 
# environment variables were used.
def get_secret(secret_name):
    secrets_file = '/etc/secrets/.env'
    if os.path.exists(secrets_file):
        print("Found secrets file.")
        with open(secrets_file) as f:
            secrets = dict(line.strip().split('=', 1) for line in f if line.strip() and not line.startswith('#'))
        return secrets.get(secret_name)
    return os.environ.get(secret_name)


# Pull the HMAC secret and TFC API Token from the environment. These could also be
# retrieved from Vault.
secret_key = bytes(get_secret("HMAC_SECRET"), "utf-8")
api_token = get_secret("TFC_API_TOKEN")

# Headers required to call TFC
tfc_headers = {
    "Authorization": f"Bearer {api_token}",
    "Content-Type": "application/vnd.api+json"
}


def process_queue():
    prev_run_id = None
    apply_payload = {
        "data": {
            "type": "runs",
            "attributes": {
                "comment": "Automatically applying run triggered run"
            }
        }
    }

    while True:
        # Get the next run-id from the queue. If there's nothing in the queue,
        # block until there is a run-id
        run_id = run_ids_queue.get()
        print(f"Processing {run_id} from queue.")

        # If we just tried the previous run-id, go ahead and wait 5 seconds.
        # This accounts for time when the plan could be performing cost estimation
        # or some other longer task.
        if prev_run_id == run_id:
            print(f"Retrying the previous {run_id}. Sleeping for 5 seconds")
            time.sleep(5)

        # Since we can continue before reaching the end of this block, update the run id now.
        prev_run_id = run_id

        # Get the run details
        run_details_url = f"https://app.terraform.io/api/v2/runs/{run_id}"
        run_response = requests.get(run_details_url, headers=tfc_headers)
        if run_response.status_code != 200:
            # We errored here for some reason, remove this run id
            print(f"Failed to get run details for {run_id} - {run_response.text}")
            print(f"Removing {run_id} from the processing queue.")
            continue

        # Check the status of the run id to ensure it hasn't already planned and finished.
        # This could happen if the plan produced no chances and an apply was not required.
        run_response_json = run_response.json()
        if run_response_json["data"]["attributes"]["status"] == "planned_and_finished":
            print(f"{run_id} was planned and finished successfully. Removing from processing queue.")
            continue

        # Before trying to apply the run, check to see if it's confirmable.
        if run_response_json["data"]["attributes"]["actions"]["is-confirmable"] == False:
            print(f"Still waiting for {run_id} to ask for plan confirmation.")
            run_ids_queue.put(run_id)
            continue

        # We're in a good state and the run should be able to be applied.
        apply_url = f"https://app.terraform.io/api/v2/runs/{run_id}/actions/apply"
        apply_response = requests.post(apply_url, headers=tfc_headers, json=apply_payload)
        if apply_response.status_code == 202:
            print(f"Successfully applied run {run_id}")
        elif apply_response.status_code == 409:
            print(f"Still waiting for {run_id} to ask for plan confirmation.")
            run_ids_queue.put(run_id)
        else:
            print(f"Unexpected status code {apply_response.status_code} for run {run_id}")


def process_request(body: Dict[str, Any], body_raw: str, request_hmac: str) -> Dict[str, Any]:
    run_task_headers = {
        "Authorization": f"Bearer {body['access_token']}",
        "Content-Type": "application/vnd.api+json"
    }

    # Create a default body. Fail by default.
    tfc_body = {
        "data": {
            "type": "task-results",
            "attributes": {
                "status": "failed"
            }
        }
    }
    tfc_body_attributes = tfc_body["data"]["attributes"]

    # Optional but recommended HMAC check
    generated_hmac = hmac.new(secret_key, body_raw, hashlib.sha512)
    if request_hmac != generated_hmac.hexdigest():
        print("HMACs do not match.")
        print(f"Request: {request_hmac}")
        print(f"genearted: {generated_hmac.hexdigest()}")
        tfc_body_attributes["message"] = "Invalid HMAC."
        tfc_body_attributes["status"] = "failed"
        patch_response = requests.patch(body.get("task_result_callback_url"),
                                        json.dumps(tfc_body),
                                        headers=run_task_headers)
        return patch_response

    # Ensure that we're actually getting called for a run task
    if "task_result_callback_url" not in body:
        # There's nothing to call back to, so we'll just return.
        print("task_result_callback_url not found in the request body.")
        return

    # We only want this run task to handle the post_plan phase
    if body.get("stage") != "post_plan":
        tfc_body_attributes["message"] = "Nothing to do. This is not a post_plan phase."
        tfc_body_attributes["status"] = "passed"
        patch_response = requests.patch(body.get("task_result_callback_url"),
                                        json.dumps(tfc_body),
                                        headers=run_task_headers)
        return patch_response

    run_id = body.get("run_id")
    if not run_id:
        print("Run ID not found in the request body.")
        tfc_body_attributes["message"] = "There was no run_id specified in the payload."
        tfc_body_attributes["status"] = "failed"
        patch_response = requests.patch(body.get("task_result_callback_url"),
                                        json.dumps(tfc_body),
                                        headers=run_task_headers)
        return patch_response

    print(f"Processing message received from {run_id}")

    # Get the run details from TFC
    run_details_url = f"https://app.terraform.io/api/v2/runs/{run_id}"
    run_response = requests.get(run_details_url, headers=tfc_headers)
    if run_response.status_code != 200:
        # We can't get the run details. We can't really continue.
        tfc_body_attributes["message"] = f"Failed to get run details: {run_response.text}"
        tfc_body_attributes["status"] = "failed"
        patch_response = requests.patch(body.get("task_result_callback_url"),
                                        json.dumps(tfc_body),
                                        headers=run_task_headers)
        return patch_response

    run_response_json = run_response.json()
    # See if the source of this run was a run trigger rather than from VCS or from the UI.
    if run_response_json["data"]["attributes"]["source"] != "tfe-run-trigger":
        print(f"Nothing to do. {run_id} is not type 'tfe-run-trigger'.")
        tfc_body_attributes["message"] = "Nothing to do. This is not a run triggered run."
        tfc_body_attributes["status"] = "passed"
        patch_response = requests.patch(body.get("task_result_callback_url"),
                                        json.dumps(tfc_body),
                                        headers=run_task_headers)
        return patch_response

    # Optional, but check the workspace to see if the setting allows for auto-apply.
    workspace_id = run_response_json["data"]["relationships"]["workspace"]["data"]["id"]
    workspace_url = f"https://app.terraform.io/api/v2/workspaces/{workspace_id}"
    workspace_response = requests.get(workspace_url, headers=tfc_headers)
    if workspace_response.status_code != 200:
        # We can't get the run details. We can't really continue.
        tfc_body_attributes["message"] = f"Failed to get workspace details: {workspace_response.text}"
        tfc_body_attributes["status"] = "failed"
        patch_response = requests.patch(body.get("task_result_callback_url"),
                                        json.dumps(tfc_body),
                                        headers=run_task_headers)
        return patch_response

    workspace_response_json = workspace_response.json()
    # Only apply this run if auto-apply is set on the workspace.
    if workspace_response_json["data"]["attributes"]["auto-apply"] == False:
        print("The auto-apply attribute is not enabled for this workspace.")
        tfc_body_attributes["message"] = "Nothing to do. Workspace is not configured for auto-apply"
        tfc_body_attributes["status"] = "passed"
        patch_response = requests.patch(body.get("task_result_callback_url"),
                                        json.dumps(tfc_body),
                                        headers=run_task_headers)
        return patch_response

    # https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run#apply-a-run
    # We can't call apply right away as the run isn't pasued for confirmation and the
    # apply endpoint will respond with a 409. Instead, add the run id to a threaded queue
    # that will attempt to apply the run with retry logic.
    run_ids_queue.put(run_id)
    tfc_body_attributes["message"] = f"Added {run_id} to the queue to be auto-applied when ready."
    tfc_body_attributes["status"] = "passed"
    patch_response = requests.patch(body.get("task_result_callback_url"),
                                    json.dumps(tfc_body),
                                    headers=run_task_headers)
    return patch_response


@app.route('/', methods=['POST'])
def run_function():
    req_data_json = request.get_json()
    # Debug output
    if debug_statements:
        print(json.dumps(req_data_json, indent=2))
        print(str(request.headers))

    # When adding your run task to TFC, TFC makes a call and expects a 200 response.
    if req_data_json.get("task_result_enforcement_level") != "test":
        # Not the initial test call. Start a thread and process the request
        thread = Thread(target=process_request,
                        args=(req_data_json,
                              request.data,
                              request.headers.get("X-Tfc-Task-Signature")))
        thread.start()

    # TFC expects a 200 that we received the message
    # print(f"processing_thread: {processing_thread.is_alive()}")
    return Flask_Response(status=200)


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'),
                               'favicon.ico', mimetype='image/vnd.microsoft.icon')


def start_processing_thread():
    # Create the thread that will monitor the queue
    global processing_thread
    processing_thread = Thread(target=process_queue)
    processing_thread.daemon = True
    processing_thread.start()


if __name__ == '__main__':
    start_processing_thread()

    # Cloud Run start
    app.run(debug=True, threaded=True, ssl_context='adhoc', host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

    # app.run(threaded=True, ssl_context='adhoc')
