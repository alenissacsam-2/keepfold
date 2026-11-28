"""A5: the vault format stamp (.keepfold/format.json) and the write gate in Vault._take (D7 §1.3,
§1.6, §2.1, §2.6). Synthetic vaults only (tests/fixtures/vault_mini copies)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keepfold import levels, prime_hook, vault_format
from keepfold.cli import FORMAT_KEEP, app
from keepfold.events import EventLog
from keepfold.vault import Vault, VaultError

pytestmark = [pytest.mark.usefixtures("home"), pytest.mark.gate("G1")]

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"


@pytest.fixture(autouse=True)
def _stamping(monkeypatch):
    monkeypatch.setattr(vault_format, "AUTO_STAMP", True)
    monkeypatch.setattr(vault_format, "READ_GAP_S", 0)  # the unreadable retries, without waiting


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    return Vault(root)


def stamp(v, data) -> None:
    """A stamp written as another program would: straight to the file."""
    p = v.root / vault_format.STAMP
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def format_events(v):
    return [e for e in EventLog(v).iter_events() if e["kind"] == "change"
            and e.get("payload", {}).get("note") == "format"]


def test_future_vault_refused(v):
    """format 99: every write path refuses with vault_too_new, reads still work; back to 1, the
    same Vault writes again (the cache follows the stamp's mtime and size)."""
    stamp(v, {"format": 99, "vault_id": "x"})
    assert vault_format.state(v.root).status == "too_new"
    for write in (lambda: v.create_note("note", "fresh", {"title": "Fresh"}, "body"),
                  lambda: EventLog(v).append(session="cli", kind="log", source="self",
                                             fields={"payload": {"kind": "touch"}}),
                  lambda: v.write_text(v.root / "notes" / "x.md", "x"),
                  lambda: v.job_lock(".keepfold/job.lock", 0.1, "busy").__enter__()):
        with pytest.raises(VaultError) as e:
            write()
        assert e.value.code == "vault_too_new" and "vault format 99" in str(e.value)
    assert not (v.root / "notes" / "fresh.md").exists()
    assert not (v.root / ".keepfold" / "lock").exists()
    from keepfold.mcp_server import ERROR_NEXT  # an agent is told not to retry
    assert "do not retry" in ERROR_NEXT["vault_too_new"]
    assert "do not retry" in ERROR_NEXT["format_unreadable"]
    assert v.read("me", "profile")  # reads work
    stamp(v, {"format": 1, "vault_id": "x"})
    v.create_note("note", "fresh", {"title": "Fresh"}, "body")
    assert (v.root / "notes" / "fresh.md").is_file()


def test_unreadable_and_missing_stamp(v):
    assert vault_format.state(v.root).status == "needs_upgrade"
    v.write_text(v.root / "notes" / "a.md", "a\n")  # no stamp yet: still writable
    for bad in ("{not json", '{"format": "1"}', '{"format": true}', '[1]', '{"format": 0}'):
        stamp(v, bad)
        assert vault_format.state(v.root).status == "unreadable", bad
        with pytest.raises(VaultError) as e:
            v.write_text(v.root / "notes" / "b.md", "b\n")
        assert e.value.code == "format_unreadable"
        with pytest.raises(VaultError):  # nor is a broken stamp replaced
            vault_format.write_stamp(v)
        assert (v.root / vault_format.STAMP).read_text(encoding="utf-8") == bad
    assert not (v.root / "notes" / "b.md").exists()


def test_stamp_written_once(v):
    for verdict in ("correct", "wrong", "correct"):  # evidence the fold must keep as it is
        EventLog(v).append(session="cli", kind="grade", source="tutor",
                           fields={"object": "concept:two-pointers", "verdict": verdict})
    before = list(EventLog(v).iter_events())
    assert vault_format.write_stamp(v) is True
    assert vault_format.write_stamp(v) is False
    data = json.loads((v.root / vault_format.STAMP).read_text(encoding="utf-8"))
    assert data["format"] == vault_format.CODE_FORMAT == 1
    assert len(data["vault_id"]) == 26 and data["applied"] == []
    assert data["dirs"] == {"resource": ["archive/chats"]}
    assert data["created_by"] == "pre-1.0" and data["created_at"] is None
    assert vault_format.state(v.root).status == "current"
    (ev,) = format_events(v)
    assert ev["session"] == "cli" and ev["payload"]["vault_id"] == data["vault_id"]
    # a lost file is rebuilt from the event: the same vault_id, no second event
    (v.root / vault_format.STAMP).unlink()
    assert vault_format.write_stamp(v) is True
    again = json.loads((v.root / vault_format.STAMP).read_text(encoding="utf-8"))
    assert again["vault_id"] == data["vault_id"] and len(format_events(v)) == 1
    # A4: the format event is never evidence; the fold is unchanged by it
    assert "change" not in levels.FOLD_KINDS
    after = list(EventLog(v).iter_events())
    assert len(after) == len(before) + 1 and before
    assert levels.fold(after, "2026-11-28") == levels.fold(before, "2026-11-28")


def test_hook_never_stamps(v, monkeypatch):
    monkeypatch.setenv("KEEPFOLD_VAULT", str(v.root))
    prime_hook.run(json.dumps({"session_id": "chat-1", "hook_event_name": "UserPromptSubmit",
                               "prompt": "How does a car motor work?"}))
    list(Vault(v.root).iter_notes())  # opening and reading the vault stamps nothing either
    assert not (v.root / vault_format.STAMP).exists()


def test_declared_dir_id_clash(v):
    """A newer Keepfold's page folder, declared in the stamp: its ids stay unique here too."""
    stamp(v, {"format": 1, "dirs": {"widget": ["widgets"], "bad": ["../outside", "/abs", 3]}})
    (v.root / "widgets").mkdir()
    (v.root / "widgets" / "gizmo.md").write_text("---\nid: gizmo\n---\n", encoding="utf-8")
    assert vault_format.declared_dirs(v.root) == ["widgets"]
    with pytest.raises(VaultError) as e:
        v.create_note("note", "gizmo", {"title": "Gizmo"}, "body")
    assert e.value.code == "invalid_id"
    v.create_note("note", "gadget", {"title": "Gadget"}, "body")


def test_init_stamps_and_keeps_the_stamp_in_git(tmp_path):
    root = tmp_path / "fresh"
    r = CliRunner().invoke(app, ["init", str(root)])
    assert r.exit_code == 0, r.output
    assert f"created  {vault_format.STAMP}" in r.output
    data = json.loads((root / vault_format.STAMP).read_text(encoding="utf-8"))
    assert data["created_by"] != "pre-1.0" and data["created_at"]
    ignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert FORMAT_KEEP in ignore and ".*.tmp" in ignore
    assert ignore.index(FORMAT_KEEP) > ignore.index(".keepfold/*")
    if (root / ".git").is_dir():
        git = shutil.which("git") or "git"
        tracked = subprocess.run([git, "-C", str(root), "ls-files"], capture_output=True,
                                 text=True).stdout.splitlines()
        assert vault_format.STAMP in tracked
        fsync = subprocess.run([git, "-C", str(root), "config", "core.fsync"],
                               capture_output=True, text=True).stdout.strip()
        assert fsync == "committed"
    again = CliRunner().invoke(app, ["init", str(root)])
    assert f"created  {vault_format.STAMP}" not in again.output
    assert len(format_events(Vault(root))) == 1


def test_init_adds_the_stamp_line_to_an_old_gitignore(v):
    (v.root / ".gitignore").write_text(".keepfold/*\n!.keepfold/config.yaml\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["init", str(v.root)])
    assert r.exit_code == 0, r.output
    lines = (v.root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[:3] == [".keepfold/*", "!.keepfold/config.yaml", FORMAT_KEEP]
    assert json.loads((v.root / vault_format.STAMP).read_text())["created_by"] == "pre-1.0"


def test_app_start_stamps(v, monkeypatch):
    from keepfold.app import server

    (v.root / ".keepfold").mkdir(exist_ok=True)
    monkeypatch.setattr(server, "PORT", 0)
    srv, a, _ = server.start(v.root, echo=lambda *x: None)
    server.finish(srv, a)
    assert json.loads((v.root / vault_format.STAMP).read_text())["created_by"] == "pre-1.0"
