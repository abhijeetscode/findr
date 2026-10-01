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


class RecordingTask:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def kiq(self, upload_id: int) -> None:
        self.sent.append(upload_id)


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


def test_worker_task_reenqueues_on_retry_and_not_otherwise(monkeypatch):
    sent: list[int] = []
    outcomes = iter([Outcome.RETRY, Outcome.READY, Outcome.FAILED, Outcome.SKIPPED])

    async def fake_kiq(upload_id):
        sent.append(upload_id)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(tasks, "run_process_upload", lambda resources, upload_id: next(outcomes))
    monkeypatch.setattr(tasks.process_upload, "kiq", fake_kiq)
    monkeypatch.setattr(tasks.asyncio, "sleep", no_sleep)
    context = SimpleNamespace(state=SimpleNamespace(resources=object()))

    for _ in range(4):
        asyncio.run(tasks.process_upload.original_func(7, context=context))

    assert sent == [7]


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
