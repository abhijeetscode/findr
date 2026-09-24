from datetime import UTC, datetime


class SystemClock:
    """Implements ports.clock.Clock. Returns naive UTC datetimes (not
    timezone-aware) so they compare cleanly with values SQLite reads back —
    SQLite's DateTime storage doesn't round-trip tzinfo."""

    def now(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
