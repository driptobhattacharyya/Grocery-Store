"""Celery settings. The broker is REDIS_URL (Render Key Value in production).

Job status is tracked in each sandbox's ``jobs`` table, so task results are not stored.
There is deliberately no beat schedule: nothing in the demo runs periodically.
"""
import os

_redis = (os.environ.get("REDIS_URL") or "").strip().rstrip("/")
broker_url = os.environ.get("CELERY_BROKER_URL") or _redis or "memory://"
result_backend = None
task_ignore_result = True
timezone = "UTC"
enable_utc = True
broker_connection_retry_on_startup = True
# Fail fast (the web request gets a 503) rather than hang when Redis is down.
# visibility_timeout: how long an unacknowledged task waits before Redis hands it to a worker
# again (default one hour). Jobs take seconds, so five minutes; execute_job() also refuses to
# run a job twice, so a redelivery can never send a second e-mail.
broker_transport_options = {"socket_connect_timeout": 3, "socket_timeout": 5, "retry_on_timeout": False,
                            "visibility_timeout": 300}
broker_connection_timeout = 3
# Keep the free 512 MB instance small and make a lost worker re-run, not drop, a job.
worker_prefetch_multiplier = 1
task_acks_late = True
worker_concurrency = 1
worker_max_tasks_per_child = 200
task_time_limit = 60
broker_pool_limit = 2
redis_max_connections = 8
