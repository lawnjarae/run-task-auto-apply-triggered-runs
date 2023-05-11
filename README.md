# Run Task to auto apply run triggered runs

By default, when a Terraform workspace triggers a run in another workspace, that [run will not auto-apply](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/settings/run-triggers#creating-a-run-trigger) regardless of the auto-apply setting on the workspace.

To get around this behavior, we can use a [Run Task](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/settings/run-tasks) that triggers during the post plan phase. The logic in this run task is as follows:
1. TFC calls the associated run task during the post plan phase with [information](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/settings/run-tasks) about the current run.
2. Since it is recommend to use HMAC authentication to ensure we are in fact talking to our TFC instance, we will verify the provided HMAC signature TFC sends in the `X-Tfc-Task-Signature` header.  [Securing your Run Task docs](https://developer.hashicorp.com/terraform/cloud-docs/integrations/run-tasks#securing-your-run-task)
3. Extract the `run_id` from the payload and call TFC to retreive additional details about the run.
4. Check the source of this run. i.e. what is the reason this run was triggered. Make sure it was started via a run trigger (`tfe-run-trigger`) and not VCS or the UI.
5. While this step is optional, it's a good idea to make sure that the workspace we're looking at has `auto-apply` the setting set to true. We don't want to auto-apply a workspace that's set to manual apply only.
6. At this point, we're ready to auto-apply the run. However, Terraform is not ready for the run to by applied as we're still in the middle of a Run Task. Terraform will respond with a `409 - Run was not paused for confirmation; apply not allowed.` [Apply a Run docs.](https://developer.hashicorp.com/terraform/cloud-docs/api-docs/run#apply-a-run) To get around this, we add the `run_id` to a queue that is being monitored in a separate thread.
7. The thread monitoring the queue will pop the `run_id` and make a call to TFC to retrieve the run details.
8. The run details are examined to make sure the run hasn't already been `planned_and_finished` which is the result of a plan producing no action items.
9. The run state is then checked to see if it is in the `is-confirmable` state. If so, we call TFC to apply the run. If for some reason a `409` is returned, we re-add the `run_id` back into the queue.

## Environment variables
| Name | Description | Required |
|----|-----------|--------|
| HMAC_SECRET | A secret key that may be required by the external service to verify request authenticity. | yes |
| TFC_API_TOKEN | The generated token used to communicate with Terraform Cloud | yes |

## Running this run task
Python 3.11.3 is the version that this Run Task was developed on.

**IMPORTANT** - For this Run Task to commuicate with Terraform Cloud, the endpoint **must** be publicly available.

### From source
1. Install the requirements with `pip -r requirements.txt`
2. Run the main file with `gunicorn --threads 12 -b 0.0.0.0:8000 run_task:app`

### Docker
1. Build the docker image `docker build -t NAME:TAG .`
2. Pass in the required environment variables:
    - With `--env-file`: 
      - Create a a `.env` file that contains the [required environment variables](#environment-variables). Start the docker container `docker run -p 0.0.0.0:EXTERNAL_PORT:8000 --env-file PATH_TO_ENV_FILE --rm NAME:TAG`
    - With `--env`:
      - `docker run -p 0.0.0.0:EXTERNAL_PORT:8000 --env HMAC_SECRET=YOUR_HMAC_SECRET --env TFC_API_TOKEN=YOUR_TFC_API_TOKEN --rm NAME:TAG`

### Google Cloud Run
Follow the instructions for [deploying to Cloud Run](https://cloud.google.com/run/docs/deploying) and configuring [environment variables](https://cloud.google.com/run/docs/configuring/environment-variables).

#### Possible limitations
When running in a non-always-on environment on GCP, it's unclear when GCP would determine the container idle and shut it down. As such, it's recommended to configure the instance to be always-on. [GCP's lifecycle of a container on Cloud Run](https://cloud.google.com/blog/topics/developers-practitioners/lifecycle-container-cloud-run)