"""RQ worker entrypoint.

No jobs are registered yet, that lands with the async ingestion pipeline
(see docs/ROADMAP.md, Milestone 2). This process just needs to boot and
stay connected to Redis so the worker container in docker-compose comes up
cleanly ahead of that work.
"""

from redis import Redis
from rq import Queue, Worker

from app.core.config import settings
from app.core.logging import configure_logging


def main() -> None:
    # Configured before the worker starts pulling jobs so every log line this
    # process emits, RQ's own included, is JSON (see app/core/logging.py and
    # docs/TDD.md section 8). Each job then sets its own job-<hex>
    # correlation id (see app/workers/jobs.py, process_document).
    configure_logging()

    connection = Redis.from_url(settings.redis_url)
    queue = Queue("default", connection=connection)
    worker = Worker([queue], connection=connection)
    worker.work()


if __name__ == "__main__":
    main()
