from __future__ import annotations

from taskiq import AsyncBroker, InMemoryBroker
from taskiq_redis import RedisStreamBroker

from findr.config import Settings

# Consumer settings — see specs/upload-chunking.md §5.2. Uploads are
# CPU-bound, so a worker takes one message at a time (the library default of
# 100 would let a busy worker hoard jobs), and a message is only redelivered
# after 30 minutes unacknowledged, matching ProcessUpload's stale bound
# (the default 10 minutes is shorter than a long OCR run).
XREAD_COUNT = 1
IDLE_TIMEOUT_MS = 30 * 60 * 1000
# Acknowledged messages are trimmed beyond roughly this many.
STREAM_MAXLEN = 10_000


def create_broker(settings: Settings) -> AsyncBroker:
    if settings.redis_url.startswith("memory://"):
        return InMemoryBroker()
    return RedisStreamBroker(
        url=settings.redis_url,
        queue_name="findr_uploads",
        consumer_group_name="findr_workers",
        xread_count=XREAD_COUNT,
        idle_timeout=IDLE_TIMEOUT_MS,
        maxlen=STREAM_MAXLEN,
        approximate=True,
    )


# Module-level so `taskiq worker findr.adapters.taskiq.tasks:broker` and the
# API share one broker definition.
broker = create_broker(Settings())
