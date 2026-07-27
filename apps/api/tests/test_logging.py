"""Tests for app.core.logging / app.core.middleware: JSON log formatting and
correlation-id propagation via contextvars, for both the API process and the
RQ worker (see docs/TDD.md section 8, issue #18).

Two of these tests (the nested-call-chain one and the job-scoped one) run
against the real app and the real worker job function rather than synthetic
loggers, so they prove the contextvar actually threads through this repo's
own route -> service call chain and job pipeline, not just a toy example.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis import Redis
from rq import Queue

from app.core.config import settings
from app.core.db import engine
from app.core.logging import JSONFormatter, configure_logging, correlation_id_var
from app.workers.jobs import process_document

_REQUEST_ID_PATTERN = re.compile(r"^req-[0-9a-f]{32}$")
_JOB_ID_PATTERN = re.compile(r"^job-[0-9a-f]{32}$")


class _CapturingHandler(logging.Handler):
    """Collects every LogRecord verbatim (no formatting), so a test can
    assert on attributes like ``correlation_id`` directly instead of parsing
    them back out of a formatted string.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured_records() -> Iterator[list[logging.LogRecord]]:
    """Configures logging (idempotent, the same call app startup makes) and
    attaches a capturing handler to the root logger for the test, removed
    again afterward so it never leaks into any other test.
    """
    configure_logging()
    handler = _CapturingHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        root_logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def _reset_correlation_id_after_test() -> Iterator[None]:
    """Safety net: guarantees the contextvar is unset by the time the next
    test runs even if a test fails before its own cleanup executes.
    """
    yield
    correlation_id_var.set(None)


# --- configure_logging / JSONFormatter ---------------------------------


def test_configure_logging_emits_one_json_line_per_record_with_expected_fields(
    captured_records: list[logging.LogRecord],
) -> None:
    logging.getLogger("tests.logging.plain").info("hello world")

    assert len(captured_records) == 1
    line = JSONFormatter().format(captured_records[0])
    payload = json.loads(line)  # raises ValueError if this isn't valid JSON

    assert payload["level"] == "INFO"
    assert payload["logger"] == "tests.logging.plain"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload
    # No correlation id was set for this record, so the field is omitted
    # entirely rather than present-but-null.
    assert "correlation_id" not in payload


def test_configure_logging_includes_correlation_id_on_the_record_when_set(
    captured_records: list[logging.LogRecord],
) -> None:
    token = correlation_id_var.set("req-test-manual-id")
    try:
        logging.getLogger("tests.logging.plain").warning("with id")
    finally:
        correlation_id_var.reset(token)

    payload = json.loads(JSONFormatter().format(captured_records[0]))
    assert payload["level"] == "WARNING"
    assert payload["correlation_id"] == "req-test-manual-id"


def test_configure_logging_is_idempotent_and_does_not_duplicate_handlers() -> None:
    configure_logging()
    configure_logging()
    root_logger = logging.getLogger()
    assert len(root_logger.handlers) == 1


# --- correlation id propagation through a real nested call chain -------


async def _register_and_get_access_token(client: AsyncClient) -> str:
    email = f"user-{uuid.uuid4().hex[:12]}@example.com"
    response = await client.post(
        "/v1/auth/register",
        json={
            "organization_name": "Acme Corp",
            "email": email,
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 201, response.text
    access_token: str = response.json()["access_token"]
    return access_token


@pytest_asyncio.fixture
async def rq_queue() -> AsyncGenerator[Queue, None]:
    """A Queue bound to the same Redis the app enqueues onto, emptied before
    and after the test (same pattern as tests/test_documents.py).
    """
    connection = Redis.from_url(settings.redis_url)
    queue = Queue("default", connection=connection)
    queue.empty()
    try:
        yield queue
    finally:
        queue.empty()
        connection.close()


async def test_correlation_id_set_at_request_start_appears_on_nested_service_call_logs(
    client: AsyncClient, rq_queue: Queue, captured_records: list[logging.LogRecord]
) -> None:
    """POST /v1/documents logs once in the route handler
    (app.api.documents.upload_document) and once in the service it calls
    into (app.services.ingestion_service.ingest_document). Neither passes a
    correlation id as an explicit parameter, so both log lines carrying the
    same id set by CorrelationIdMiddleware at request start only holds if
    the contextvar itself threads through the call.
    """
    access_token = await _register_and_get_access_token(client)
    known_id = f"req-{uuid.uuid4().hex}"

    response = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {access_token}", "X-Request-ID": known_id},
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert response.status_code == 201, response.text
    assert response.headers["x-request-id"] == known_id

    records_by_logger = {record.name: record for record in captured_records}
    assert "app.api.documents" in records_by_logger
    assert "app.services.ingestion_service" in records_by_logger
    assert records_by_logger["app.api.documents"].correlation_id == known_id
    assert records_by_logger["app.services.ingestion_service"].correlation_id == known_id


# --- CorrelationIdMiddleware ---------------------------------------------


async def test_middleware_generates_a_correlation_id_when_none_is_sent(
    client: AsyncClient,
) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert _REQUEST_ID_PATTERN.match(response.headers["x-request-id"])


async def test_middleware_reuses_and_echoes_an_inbound_correlation_id(
    client: AsyncClient,
) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "caller-supplied-id-123"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "caller-supplied-id-123"


# --- worker job-scoped correlation id -------------------------------------


def test_process_document_job_scoped_correlation_id_is_set_during_and_cleared_after(
    captured_records: list[logging.LogRecord],
) -> None:
    """process_document sets its own job-<hex> id for the duration of the
    job (the worker-side equivalent of CorrelationIdMiddleware) and clears
    it afterward, so it can never leak into whatever this same worker
    process handles next.

    Uses a document id that does not exist, the same fast, DB-only path
    tests/test_process_document_job.py's
    test_process_document_missing_document_returns_cleanly exercises, so
    this stays a fast unit-style test with no need to run the full
    parse/chunk/embed pipeline.
    """
    assert correlation_id_var.get() is None

    missing_document_id = str(uuid.uuid4())
    # See tests/test_process_document_job.py's module docstring ("Bridging
    # event loops in-process"): process_document runs its own asyncio.run
    # internally, so the shared async engine's connection pool is disposed
    # immediately before and after so a connection pooled on one now-closed
    # event loop is never handed to a different one.
    asyncio.run(engine.dispose())
    try:
        process_document(missing_document_id)
    finally:
        asyncio.run(engine.dispose())

    assert correlation_id_var.get() is None

    job_records = [record for record in captured_records if record.name == "app.workers.jobs"]
    assert job_records, "expected at least one log line from app.workers.jobs during the job"
    for record in job_records:
        assert record.correlation_id is not None
        assert _JOB_ID_PATTERN.match(record.correlation_id)
    # Every log line from this one job run shares the same id.
    assert len({record.correlation_id for record in job_records}) == 1
