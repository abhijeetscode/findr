import asyncio
import threading
from types import SimpleNamespace

from taskiq import InMemoryBroker
from taskiq_redis import RedisStreamBroker

from findr.adapters.taskiq import tasks
from findr.adapters.taskiq.broker import IDLE_TIMEOUT_MS, XREAD_COUNT, create_broker
from findr.adapters.taskiq.upload_queue_taskiq import TaskiqUploadQueue
from findr.application.uploads.process_upload import Outcome
from findr.config import Settings
from findr.observability import bind, current_fields


class RecordingTask:
    """Stands in for a Taskiq task: records what was sent, with its labels."""

    def __init__(self) -> None:
        self.sent: list[int] = []
        self.labels: list[dict] = []

    def kicker(self) -> "RecordingKicker":
        return RecordingKicker(self)


class RecordingKicker:
    def __init__(self, task: RecordingTask) -> None:
        self._task = task
        self._labels: dict = {}

    def with_labels(self, **labels) -> "RecordingKicker":
        self._labels.update(labels)
        return self

    async def kiq(self, upload_id: int) -> None:
        self._task.sent.append(upload_id)
        self._task.labels.append(dict(self._labels))


def test_broker_consumer_settings(monkeypatch):
    monkeypatch.setenv("FINDR_REDIS_URL", "redis://example:6379/0")
    broker = create_broker(Settings())

    assert isinstance(broker, RedisStreamBroker)
    # One message at a time; redelivery only after 30 minutes (spec §5.2).
    assert XREAD_COUNT == 1
    assert IDLE_TIMEOUT_MS == 30 * 60 * 1000

    monkeypatch.setenv("FINDR_REDIS_URL", "memory://")
    assert isinstance(create_broker(Settings()), InMemoryBroker)


def test_upload_queue_enqueues_from_a_non_async_thread():
    # The API calls enqueue from threadpool/scheduler threads; the coroutine
    # must run on the event loop that owns the broker connection.
    task = RecordingTask()

    async def main() -> None:
        queue = TaskiqUploadQueue(task, asyncio.get_running_loop())
        await asyncio.to_thread(queue.enqueue, 42)

    asyncio.run(main())
    assert task.sent == [42]
    assert task.labels == [{}]


def test_upload_queue_sends_the_callers_request_id_as_a_label():
    # specs/logging-telemetry.md §5: the worker's log lines for an upload
    # share the id of the request that enqueued it.
    task = RecordingTask()

    async def main() -> None:
        queue = TaskiqUploadQueue(task, asyncio.get_running_loop())
        with bind(request_id="req-123"):
            await asyncio.to_thread(queue.enqueue, 42)

    asyncio.run(main())
    assert task.labels == [{"request_id": "req-123"}]


def test_worker_task_reenqueues_on_retry_and_not_otherwise(monkeypatch):
    sent: list[tuple[int, str]] = []
    outcomes = iter([Outcome.RETRY, Outcome.READY, Outcome.FAILED, Outcome.SKIPPED])

    async def fake_requeue(upload_id, request_id):
        sent.append((upload_id, request_id))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(tasks, "run_process_upload", lambda resources, upload_id: next(outcomes))
    monkeypatch.setattr(tasks, "_requeue", fake_requeue)
    monkeypatch.setattr(tasks.asyncio, "sleep", no_sleep)
    context = SimpleNamespace(
        state=SimpleNamespace(resources=object()),
        message=SimpleNamespace(labels={"request_id": "req-abc"}),
    )

    for _ in range(4):
        asyncio.run(tasks.process_upload.original_func(7, context=context))

    # The retry keeps the original request id.
    assert sent == [(7, "req-abc")]


def test_worker_binds_the_request_id_label_for_the_job(monkeypatch):
    seen: list[dict] = []

    def fake_run(resources, upload_id):
        seen.append(current_fields())
        return Outcome.READY

    monkeypatch.setattr(tasks, "run_process_upload", fake_run)
    labelled = SimpleNamespace(
        state=SimpleNamespace(resources=object()),
        message=SimpleNamespace(labels={"request_id": "req-abc"}),
    )
    unlabelled = SimpleNamespace(state=SimpleNamespace(resources=object()))

    asyncio.run(tasks.process_upload.original_func(7, context=labelled))
    asyncio.run(tasks.process_upload.original_func(8, context=unlabelled))

    assert seen[0] == {"request_id": "req-abc", "upload_id": 7}
    # No label (e.g. enqueued before this change): a fresh id, never none.
    assert seen[1]["request_id"].startswith("upload-")
    assert seen[1]["upload_id"] == 8


def test_worker_runs_the_blocking_job_off_the_event_loop(monkeypatch):
    loop_thread: list[threading.Thread] = []
    job_thread: list[threading.Thread] = []

    def fake_run(resources, upload_id):
        job_thread.append(threading.current_thread())
        return Outcome.READY

    monkeypatch.setattr(tasks, "run_process_upload", fake_run)
    context = SimpleNamespace(state=SimpleNamespace(resources=object()))

    async def main():
        loop_thread.append(threading.current_thread())
        await tasks.process_upload.original_func(1, context=context)

    asyncio.run(main())
    assert job_thread[0] is not loop_thread[0]
