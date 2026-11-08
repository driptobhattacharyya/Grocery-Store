"""Tiny process supervisor for the single-container deployment.

Starts gunicorn and, only when REDIS_URL is set, a Celery worker. If either process
exits, the other is stopped and the container exits non-zero so the platform
restarts it. SIGTERM/SIGINT are forwarded to both children.
"""
import os
import signal
import subprocess
import sys
import time

GUNICORN = [sys.executable, "-m", "gunicorn", "--config", "gunicorn.conf.py", "app:app"]
CELERY = [
    sys.executable, "-m", "celery", "-A", "app:celery_app", "worker",
    "--pool=solo", "--concurrency=1", "--loglevel=" + (os.environ.get("LOG_LEVEL") or "INFO").upper(),
    "--without-gossip", "--without-mingle", "--without-heartbeat",
]


def main():
    children = {"gunicorn": subprocess.Popen(GUNICORN)}
    if (os.environ.get("REDIS_URL") or "").strip():
        env = dict(os.environ, PROCESS_ROLE="worker")
        children["celery"] = subprocess.Popen(CELERY, env=env)
    else:
        print("supervisor: REDIS_URL not set, running without a Celery worker "
              "(jobs run on a thread inside gunicorn)", flush=True)

    stopping = []

    def forward(signum, _frame):
        stopping.append(signum)
        for child in children.values():
            if child.poll() is None:
                child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    exit_code = 0
    while True:
        dead = [(name, c) for name, c in children.items() if c.poll() is not None]
        if dead:
            name, child = dead[0]
            exit_code = 0 if stopping else (child.returncode or 1)
            if not stopping:
                print(f"supervisor: {name} exited with {child.returncode}; stopping the container", flush=True)
            break
        time.sleep(0.5)

    for child in children.values():
        if child.poll() is None:
            child.terminate()
    deadline = time.time() + 30
    for child in children.values():
        try:
            child.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            child.kill()
    return exit_code if exit_code else (1 if not stopping else 0)


if __name__ == "__main__":
    sys.exit(main())
