from __future__ import annotations

import asyncio

from taskiq import AsyncTaskiqDecoratedTask

from findr.observability import current_fields

# How long a caller waits for Redis to accept a message.
_ENQUEUE_TIMEOUT_SECONDS = 10


class TaskiqUploadQueue:
    """Implements ports.upload_queue.UploadQueue by sending the
    process_upload task through Taskiq.

    The port is synchronous (the use cases are plain sync code, run in
    threads), but Taskiq's kiq() is async and its connection pool belongs to
    the API's event loop — so the coroutine is handed to that loop. Works
    from any thread: FastAPI's threadpool and APScheduler's thread alike.
    """

    def __init__(self, task: AsyncTaskiqDecoratedTask, loop: asyncio.AbstractEventLoop) -> None:
        self._task = task
        self._loop = loop

    def enqueue(self, upload_id: int) -> None:
        # The caller's request id travels as a message label, so the worker's
        # log lines for this upload share it (specs/logging-telemetry.md §5).
        kicker = self._task.kicker()
        request_id = current_fields().get("request_id")
        if request_id is not None:
            kicker = kicker.with_labels(request_id=request_id)
        future = asyncio.run_coroutine_threadsafe(kicker.kiq(upload_id), self._loop)
        future.result(timeout=_ENQUEUE_TIMEOUT_SECONDS)
