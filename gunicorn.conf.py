import os

bind = f"""[::]:{os.getenv("GUNICORN_PORT", "8001")}"""
workers = os.getenv("GUNICORN_NUM_WORKERS", "1")
threads = os.getenv("GUNICORN_NUM_THREADS", "1")
timeout = os.getenv("GUNICORN_TIMEOUT", "60")
loglevel = os.getenv("GUNICORN_LOGLEVEL", "INFO").lower()
