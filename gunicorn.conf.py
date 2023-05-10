def post_worker_init(worker):
    from run_task import start_processing_thread
    start_processing_thread()