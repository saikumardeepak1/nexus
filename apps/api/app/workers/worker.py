"""RQ worker entrypoint.

No jobs are registered yet, that lands with the async ingestion pipeline
(see docs/ROADMAP.md, Milestone 2). This process just needs to boot and
stay connected to Redis so the worker container in docker-compose comes up
cleanly ahead of that work.
"""

from redis import Redis
from rq import Queue, Worker

from app.core.config import settings


def main() -> None:
    connection = Redis.from_url(settings.redis_url)
    queue = Queue("default", connection=connection)
    worker = Worker([queue], connection=connection)
    worker.work()


if __name__ == "__main__":
    main()
