"""atomic.replace: temp + fsync + os.replace with retry, and no temp left behind."""

import os

import pytest

from keepfold import atomic


def test_replace_fsyncs_and_retries(tmp_path, monkeypatch):
    path = tmp_path / "f.txt"
    path.write_text("old", encoding="utf-8")
    real, calls, synced, sleeps = os.replace, [], [], []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError(13, "in use")
        real(src, dst)

    monkeypatch.setattr(atomic.os, "replace", flaky)
    monkeypatch.setattr(atomic.os, "fsync", lambda fd: synced.append(fd))
    monkeypatch.setattr(atomic.time, "sleep", sleeps.append)
    assert atomic.replace(path, "new\n") is True
    assert path.read_bytes() == b"new\n" and len(calls) == 3 and len(synced) == 1
    assert sleeps == [0.05, 0.1] and not list(tmp_path.glob(".*.tmp"))
    atomic.replace(path, b"raw", fsync=False)
    assert path.read_bytes() == b"raw" and len(synced) == 1


def test_failed_write_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "f.txt"
    path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(atomic.os, "replace", lambda s, d: (_ for _ in ()).throw(
        PermissionError(13, "in use")))
    monkeypatch.setattr(atomic.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        atomic.replace(path, "new")
    assert path.read_text(encoding="utf-8") == "old" and not list(tmp_path.glob(".*.tmp"))


def test_guard_false_leaves_the_file_and_no_temp(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("old", encoding="utf-8")
    assert atomic.replace(path, "new", guard=lambda: False) is False
    assert path.read_text(encoding="utf-8") == "old" and not list(tmp_path.glob(".*.tmp"))


def test_newline_follows_the_open_meaning(tmp_path):
    path = tmp_path / "f.txt"
    atomic.replace(path, "a\nb\n")
    assert path.read_bytes() == b"a\nb\n"
    atomic.replace(path, "a\nb\n", newline=None)
    assert path.read_bytes() == b"a" + os.linesep.encode() + b"b" + os.linesep.encode()
