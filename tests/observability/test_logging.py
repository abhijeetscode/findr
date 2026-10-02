"""specs/logging-telemetry.md §12: the logging setup itself."""

import asyncio
import json
import logging
import socket
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.concurrency import run_in_threadpool

from findr.config import Settings
from findr.observability import add_fields, bind, current_fields, log_event, timed
from findr.observability.setup import configure_logging
from findr.observability.formatting import REDACTED

logger = logging.getLogger("findr.tests.observability")


def _settings(log_dir: Path, **overrides) -> Settings:
    return Settings(FINDR_LOG_DIR=str(log_dir), **overrides)


def read_log(path: Path) -> list[dict]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text().splitlines()]


def _events(path: Path, name: str) -> list[dict]:
    return [r for r in read_log(path) if r.get("event") == name]


def test_writes_json_lines_to_one_file_per_service(tmp_path):
    path = configure_logging(_settings(tmp_path), "api")
    assert path == tmp_path / "api.log"

    with bind(request_id="req-1", user_id=1):
        log_event(logger, "thing.happened", count=3)

    [record] = _events(path, "thing.happened")
    assert record["level"] == "INFO"
    assert record["logger"] == "findr.tests.observability"
    assert record["service"] == "api"
    assert record["msg"] == "thing.happened"
    assert record["ts"].endswith("Z")
    assert (record["request_id"], record["user_id"], record["count"]) == ("req-1", 1, 3)


def test_worker_and_script_file_names(tmp_path, monkeypatch):
    host = socket.gethostname()
    assert configure_logging(_settings(tmp_path), "worker").name == f"worker-{host}.log"
    # taskiq's --workers N runs processes named worker-0, worker-1, ...:
    # each gets its own file.
    monkeypatch.setattr(
        "findr.observability.setup.multiprocessing.current_process",
        lambda: SimpleNamespace(name="worker-1"),
    )
    assert configure_logging(_settings(tmp_path), "worker").name == f"worker-{host}-1.log"
    assert configure_logging(_settings(tmp_path), "script", log_name="reindex").name == (
        "reindex.log"
    )


def test_tracebacks_stay_on_one_line(tmp_path):
    path = configure_logging(_settings(tmp_path), "api")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("it failed")

    lines = path.read_text().splitlines()
    record = json.loads(lines[-1])
    assert record["exc_type"] == "ValueError"
    assert record["exc_message"] == "boom"
    assert "Traceback" in record["stack"]


def test_default_level_is_debug_and_libraries_are_capped(tmp_path):
    path = configure_logging(_settings(tmp_path), "api")
    logger.debug("ours")
    logging.getLogger("elasticsearch").info("library chatter")
    logging.getLogger("sqlalchemy.engine.Engine").info("INSERT ... ('Client name',)")
    logging.getLogger("elasticsearch").warning("library warning")

    messages = [r["msg"] for r in read_log(path)]
    assert "ours" in messages
    assert "library chatter" not in messages
    assert not any("Client name" in m for m in messages)
    assert "library warning" in messages


def test_level_settings_are_applied(tmp_path):
    path = configure_logging(
        _settings(tmp_path, FINDR_LOG_LEVEL="info", FINDR_LOG_LEVEL_LIBS="DEBUG"), "api"
    )
    logger.debug("hidden")
    logging.getLogger("elasticsearch").debug("library detail")

    messages = [r["msg"] for r in read_log(path)]
    assert "hidden" not in messages
    assert "library detail" in messages


def test_unknown_level_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown log level"):
        configure_logging(_settings(tmp_path, FINDR_LOG_LEVEL="LOUD"), "api")


def test_unwritable_directory_fails_setup(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    with pytest.raises(RuntimeError, match="log directory"):
        configure_logging(_settings(blocker / "logs"), "api")


def test_rotates_at_max_bytes_keeping_backups(tmp_path):
    path = configure_logging(
        _settings(tmp_path, FINDR_LOG_MAX_BYTES="2000", FINDR_LOG_BACKUP_COUNT="2"), "api"
    )
    for i in range(100):
        logger.info("line %d %s", i, "x" * 50)

    assert path.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["api.log", "api.log.1", "api.log.2"]


def test_calling_setup_twice_does_not_duplicate_lines(tmp_path):
    configure_logging(_settings(tmp_path), "api")
    path = configure_logging(_settings(tmp_path), "api")
    log_event(logger, "once")
    assert len(_events(path, "once")) == 1


def test_nothing_reaches_the_console(tmp_path, capsys):
    # Libraries' own console handlers, an earlier basicConfig, and Python
    # warnings all end up in the file instead (spec §4.7).
    logging.getLogger("uvicorn.error").addHandler(logging.StreamHandler())
    logging.getLogger("taskiq").addHandler(logging.StreamHandler())
    logging.basicConfig()
    path = configure_logging(_settings(tmp_path), "api")

    logger.error("our error")
    logging.getLogger("uvicorn.error").error("uvicorn error")
    logging.getLogger("taskiq.receiver").error("taskiq error")
    logging.getLogger("uvicorn.access").info("GET /search?q=secret 200")
    # Called directly: pytest's own warning capture intercepts warnings.warn.
    warnings.showwarning("a deprecation", UserWarning, "lib.py", 1)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    messages = " ".join(r["msg"] for r in read_log(path))
    for expected in ("our error", "uvicorn error", "taskiq error", "a deprecation"):
        assert expected in messages
    # uvicorn's access log is replaced by http.request, never written.
    assert "secret" not in messages


def test_sensitive_extra_keys_are_redacted(tmp_path):
    path = configure_logging(_settings(tmp_path), "api")
    log_event(
        logger,
        "oops",
        access_token="abc",
        password="hunter2",
        code="oauth-code",
        state="oauth-state",
        status=200,
        upload_id=4,
    )
    [record] = _events(path, "oops")
    assert record["access_token"] == REDACTED
    assert record["password"] == REDACTED
    assert record["code"] == REDACTED
    assert record["state"] == REDACTED
    assert (record["status"], record["upload_id"]) == (200, 4)


def test_explicit_fields_win_over_bound_ones(tmp_path):
    path = configure_logging(_settings(tmp_path), "api")
    with bind(workspace_id=1):
        log_event(logger, "explicit", workspace_id=2)
    [record] = _events(path, "explicit")
    assert record["workspace_id"] == 2


def test_bind_restores_and_add_fields_updates_the_enclosing_scope():
    assert current_fields() == {}
    add_fields(user_id=1)  # no scope: ignored, nothing leaks
    assert current_fields() == {}

    with bind(request_id="r"):
        with bind(upload_id=5):
            assert current_fields() == {"request_id": "r", "upload_id": 5}
        assert current_fields() == {"request_id": "r"}
        add_fields(user_id=1)
        assert current_fields() == {"request_id": "r", "user_id": 1}
    assert current_fields() == {}


def test_context_survives_threadpools():
    # FastAPI's sync dependencies run in a threadpool on a copy of the
    # context: add_fields there must still reach the request's scope.
    async def main() -> tuple[dict, dict]:
        with bind(request_id="r"):
            await run_in_threadpool(add_fields, user_id=7)
            seen = await asyncio.to_thread(current_fields)
            return current_fields(), seen

    after, in_thread = asyncio.run(main())
    assert after == {"request_id": "r", "user_id": 7}
    assert in_thread == {"request_id": "r", "user_id": 7}


def test_timed_logs_duration_and_collected_fields(tmp_path):
    path = configure_logging(_settings(tmp_path), "api")
    with timed(logger, "work.done", kind="a") as event:
        event["items"] = 3
    with pytest.raises(KeyError):
        with timed(logger, "work.failed"):
            raise KeyError("x")

    [done] = _events(path, "work.done")
    assert (done["kind"], done["items"], done["level"]) == ("a", 3, "INFO")
    assert isinstance(done["duration_ms"], int)
    [failed] = _events(path, "work.failed")
    assert (failed["level"], failed["outcome"], failed["error_type"]) == (
        "WARNING",
        "error",
        "KeyError",
    )


def test_email_addresses_are_masked_in_messages_and_tracebacks(tmp_path):
    # Safety net for Q2: an upstream error body quoting an address.
    path = configure_logging(_settings(tmp_path), "api")
    try:
        raise RuntimeError("Delegation denied for boss@client.example")
    except RuntimeError:
        logger.exception("Gmail said no to boss@client.example")

    record = read_log(path)[-1]
    assert "client.example" not in json.dumps(record)
    assert record["msg"] == "Gmail said no to [email]"
    assert record["exc_message"] == "Delegation denied for [email]"
