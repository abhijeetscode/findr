"""One logging setup for the API, the worker and scripts: JSON lines to a
rotating file per process, nothing on the console
(specs/logging-telemetry.md §4.1, §4.6, §4.7)."""

from __future__ import annotations

import logging
import multiprocessing
import os
import socket
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from findr.config import Settings
from findr.observability.formatting import ContextFilter, JsonFormatter, RedactionFilter

# Third-party loggers held at FINDR_LOG_LEVEL_LIBS, so the default DEBUG
# level shows our code, not every Elasticsearch request. sqlalchemy matters
# for privacy too: at INFO it logs every statement with its parameters,
# which include filenames and workspace names (spec §8).
LIBRARY_LOGGERS = (
    "sqlalchemy",
    "psycopg",
    "redis",
    "starlette",
    "fastapi",
    "anyio",
    "watchfiles",
    "filelock",
    "fsspec",
    "torch",
    "spacy",
    "pdfplumber",
    "pypdf",
    "pytesseract",
    "onnxruntime",
    "timm",
    "elasticsearch",
    "elastic_transport",
    "urllib3",
    "httpx",
    "httpcore",
    "apscheduler",
    "taskiq",
    "sentence_transformers",
    "transformers",
    "huggingface_hub",
    "unstructured",
    "unstructured_inference",
    "pdfminer",
    "PIL",
    "multipart",
    "python_multipart",
    "asyncio",
    "numba",
    "matplotlib",
    "tzlocal",
    "MARKDOWN",
)
# Progress bars (model downloads/loading) write straight to stderr, not
# through logging. Read when these libraries are imported, so they're set
# before the embedding model and parser load.
_PROGRESS_BAR_SWITCHES = {"TQDM_DISABLE": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
# Replaced by the access-log middleware's `http.request` event (§4.4).
SILENCED_LOGGERS = ("uvicorn.access",)



class _FindrFileHandler(RotatingFileHandler):
    """Marks our handler so a repeat configure_logging replaces it."""


def log_file_name(service: str, log_name: str | None = None) -> str:
    """One file per process: RotatingFileHandler isn't safe with several
    processes rotating one file (§4.6). Workers scale two ways: more
    containers (each its own hostname) and `taskiq worker --workers N`
    (processes named worker-0, worker-1, ... in one container)."""
    if log_name is not None:
        return f"{log_name}.log"
    if service == "worker":
        process = multiprocessing.current_process().name
        index = process.removeprefix("worker-") if process.startswith("worker-") else None
        suffix = f"-{index}" if index is not None else ""
        return f"worker-{socket.gethostname()}{suffix}.log"
    return f"{service}.log"


def _level(name: str) -> int:
    level = logging.getLevelName(name.strip().upper())
    if not isinstance(level, int):
        raise ValueError(f"Unknown log level {name!r}")
    return level


def _prepare_dir(log_dir: str) -> Path:
    path = Path(log_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Can't create log directory {path}: {exc}") from exc
    if not os.access(path, os.W_OK):
        raise RuntimeError(f"Log directory {path} isn't writable")
    return path


def _is_console_handler(handler: logging.Handler) -> bool:
    # FileHandler subclasses StreamHandler, so check the stream itself.
    return (
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and getattr(handler, "stream", None)
        in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    )


def reclaim_console_handlers() -> None:
    """Removes console handlers that libraries attached to their own
    loggers (uvicorn, taskiq, huggingface_hub, transformers, ...) and lets
    those loggers propagate to the root's file handler instead (§4.7).

    Some libraries only attach them when first imported, so entry points
    call this again after loading heavy dependencies."""
    for name, candidate in list(logging.root.manager.loggerDict.items()):
        if not isinstance(candidate, logging.Logger) or name in SILENCED_LOGGERS:
            continue
        console = [h for h in candidate.handlers if _is_console_handler(h)]
        for handler in console:
            candidate.removeHandler(handler)
        if console:
            candidate.propagate = True


def configure_logging(settings: Settings, service: str, log_name: str | None = None) -> Path:
    """Sends all logging to `<FINDR_LOG_DIR>/<file>` as JSON lines and
    takes it off the console. Idempotent. Returns the log file's path.
    Raises if the directory can't be written: there is nowhere else to log."""
    level = _level(settings.log_level)
    libs_level = _level(settings.log_level_libs)
    path = _prepare_dir(settings.log_dir) / log_file_name(service, log_name)

    handler = _FindrFileHandler(
        path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(JsonFormatter(service))
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        # Ours from an earlier call, or a console handler (e.g. basicConfig).
        # Anything else, like pytest's caplog handler, is left alone.
        if isinstance(existing, _FindrFileHandler) or _is_console_handler(existing):
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)
    root.setLevel(level)

    for name in LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(libs_level)
    for key, value in _PROGRESS_BAR_SWITCHES.items():
        os.environ.setdefault(key, value)
    reclaim_console_handlers()
    for name in SILENCED_LOGGERS:
        silenced = logging.getLogger(name)
        silenced.handlers = [logging.NullHandler()]
        silenced.propagate = False

    # Off then on: a second captureWarnings(True) is a no-op, even if
    # something has since replaced warnings.showwarning again.
    logging.captureWarnings(False)
    logging.captureWarnings(True)
    return path
