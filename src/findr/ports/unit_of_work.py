from typing import Protocol


class UnitOfWork(Protocol):
    """Transaction boundary for use cases that must commit part-way through
    (the upload worker commits its claim before a minutes-long OCR run, so
    the status is visible and no transaction stays open meanwhile)."""

    def commit(self) -> None: ...
    def rollback(self) -> None: ...
