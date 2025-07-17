import os

bind = f"""[::]:{os.getenv("GUNICORN_PORT")}"""
workers = os.getenv("GUNICORN_NUM_WORKERS")
threads = os.getenv("GUNICORN_NUM_THREADS")
timeout = os.getenv("GUNICORN_TIMEOUT", "60")
worker_tmp_dir = os.getenv("GUNICORN_WORKER_DIR")
loglevel = os.getenv("GUNICORN_LOGLEVEL", "INFO").lower()
