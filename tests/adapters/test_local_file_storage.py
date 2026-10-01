import pytest

from findr.adapters.outbound.files.local_file_storage import LocalFileStorage


def test_save_read_delete_round_trip(tmp_path):
    storage = LocalFileStorage(tmp_path)

    storage_path = storage.save(7, "Q3 Report.PDF", b"%PDF-bytes")

    assert storage_path.startswith("7/")
    assert storage_path.endswith(".pdf")
    assert (tmp_path / storage_path).read_bytes() == b"%PDF-bytes"
    assert storage.read(storage_path) == b"%PDF-bytes"

    storage.delete(storage_path)
    assert not (tmp_path / storage_path).exists()
    storage.delete(storage_path)  # idempotent


def test_same_filename_twice_gets_distinct_paths(tmp_path):
    storage = LocalFileStorage(tmp_path)
    assert storage.save(1, "notes.txt", b"a") != storage.save(1, "notes.txt", b"b")


def test_hostile_filename_cannot_escape_root(tmp_path):
    storage = LocalFileStorage(tmp_path / "root")

    storage_path = storage.save(1, "../../etc/passwd", b"x")
    assert storage_path.startswith("1/")
    assert "/" not in storage_path.removeprefix("1/")
    assert (tmp_path / "root" / storage_path).exists()

    # Weird extensions are dropped rather than kept verbatim.
    assert "." not in storage.save(1, "evil.p/../hp", b"x").removeprefix("1/")


def test_read_and_delete_reject_paths_outside_root(tmp_path):
    storage = LocalFileStorage(tmp_path / "root")
    (tmp_path / "secret.txt").write_text("secret")

    with pytest.raises(ValueError):
        storage.read("../secret.txt")
    with pytest.raises(ValueError):
        storage.delete("../secret.txt")
    assert (tmp_path / "secret.txt").exists()
