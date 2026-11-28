import os
import shutil
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import git
from typer.testing import CliRunner

from keepfold import cli as cli_mod
from keepfold import session as sm_mod
from keepfold.app import targets
from keepfold.cli import app
from keepfold.events import EventLog
from keepfold.session import SessionManager, sweep
from keepfold.vault import SECRET_RES, Vault, VaultError

real_extra_path = sm_mod.extra_path  # conftest replaces it with "" in every test

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def root(root_committed):
    return root_committed


FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def mgr(root, clock):
    return SessionManager(Vault(root), clock=clock)


def kinds(root, sid):
    return [e["kind"] for e in EventLog(Vault(root)).iter_events(sid)]


def code(fn, *args):
    with pytest.raises(VaultError) as e:
        fn(*args)
    return e.value.code


# ---- start -----------------------------------------------------------------------

def test_start(mgr, clock):
    s = mgr.start("learn", topic="two pointers", model="m")
    assert s.id.startswith("s_") and s.mode == "learn" and s.started == T0
    assert mgr.require("grade") is s
    assert code(mgr.start, "bogus") == "invalid_argument"


# ---- require ------------------------------------------------------------------------

def test_require_without_session(mgr, root):
    for tool in ("find", "read", "context", "lint"):
        assert mgr.require(tool) is None  # reads never open a session
    assert mgr.current is None
    assert code(mgr.require, "end") == "no_session"
    s = mgr.require("patch")  # the first write opens an ambient session
    assert s is mgr.current and s.mode == "ambient"
    assert mgr.require("grade") is s
    mgr.close("done")
    assert kinds(root, s.id) == ["open", "close"]


@pytest.mark.parametrize("mode", ["about", "week"])
def test_read_only_modes(mgr, mode):
    mgr.start(mode)
    for tool in ("grade", "patch", "add", "ask", "log"):
        assert code(mgr.require, tool) == "read_only_mode"
    for tool in ("find", "read", "end", "resurface"):
        mgr.require(tool)


def test_log_mode(mgr):
    mgr.start("log")
    assert code(mgr.require, "grade") == "read_only_mode"
    for tool in ("log", "patch", "add", "find"):
        mgr.require(tool)


def test_idle_one_hour_resumes_once(mgr, clock, root):
    s = mgr.start()
    clock.tick(hours=1)
    mgr.require("find")
    mgr.require("find")
    clock.tick(minutes=5)
    mgr.require("find")
    assert kinds(root, s.id) == ["open", "resume"]
    assert s.last_activity == clock.t


def test_idle_three_hours_auto_closes(mgr, clock, root):
    s = mgr.start()
    clock.tick(hours=3)
    assert mgr.require("find") is None
    assert mgr.current is None
    evs = list(EventLog(Vault(root)).iter_events(s.id))
    assert [e["kind"] for e in evs] == ["open", "close"]
    assert evs[-1]["summary"] == "auto-closed after 2 h idle"
    assert mgr.require("log").mode == "ambient"  # a later write opens a fresh session


def test_start_while_open_auto_closes(mgr, root):
    first = mgr.start()
    second = mgr.start("review")
    assert first.id != second.id and mgr.current is second
    evs = list(EventLog(Vault(root)).iter_events(first.id))
    assert [(e["kind"], e.get("summary")) for e in evs] == [
        ("open", None), ("close", "auto-closed: new session started")]


# ---- exposure that sticks (v6 T2 + T3) ------------------------------------------------

def _change(mgr, id):
    mgr.require("patch")
    mgr.note("patched", change={"tool": "patch", "id": id, "kind": "note", "op": "set_field",
                                "target": "status"})


def test_exposure_lasts_two_hours_after_the_last_outside_read(mgr, clock, root):
    mgr.expose()  # a Keepfold read with no session open
    _change(mgr, "a")
    assert mgr.current.exposed
    mgr.close()
    clock.tick(hours=1, minutes=59)
    assert mgr.start("learn").exposed  # any mode, not only ambient
    _change(mgr, "b")
    mgr.close()
    clock.tick(minutes=2)  # 2 h 1 min after the read
    _change(mgr, "a")  # a clean write clears page a; b stays exposed
    assert not mgr.current.exposed
    mgr.close()
    evs = list(EventLog(Vault(root)).iter_events())
    stamps = [e["payload"].get("exposed") for e in evs if e["kind"] == "change"]
    assert stamps == [True, True, None]
    assert sm_mod.exposed_pages(evs) == {"b"}


def test_a_close_keeps_hook_exposure(mgr, clock, root):
    marker = root / sm_mod.TAINT_MARKER
    marker.parent.mkdir(exist_ok=True)
    marker.write_text("{}")
    at = (clock.t + timedelta(minutes=10)).timestamp()
    os.utime(marker, (at, at))  # an outside read 10 min after the server started
    clock.tick(minutes=30)
    _change(mgr, "a")
    assert mgr.current.exposed
    mgr.close()
    clock.tick(hours=1)
    assert mgr.require("patch").exposed  # the marker is 1 h 20 min old
    mgr.close()
    clock.tick(hours=1)
    assert not mgr.require("patch").exposed  # 2 h 20 min: clean again


def test_exposed_pages_ignore_file_mtime(mgr, clock, root):
    """`_end` rewrites keepfold_status on touched concepts; that is no change event, so the
    page stays exposed."""
    mgr.expose()
    mgr.require("patch")
    mgr.note("added", "two-pointers", change={"tool": "add", "id": "two-pointers",
                                              "kind": "concept"})
    mgr.close()
    assert "keepfold_status" in (root / "concepts" / "two-pointers.md").read_text("utf-8")
    assert sm_mod.exposed_pages(EventLog(Vault(root)).iter_events()) == {"two-pointers"}


# ---- close --------------------------------------------------------------------------

def test_close_pipeline(mgr, clock, root):
    s = mgr.start()
    clock.tick(minutes=20)
    mgr.log.append(session=s.id, kind="grade", source="tutor", mode="learn", now=clock.t,
                   fields={"object": "concept:two-pointers", "verdict": "correct", "hints": 0,
                           "confidence": 3})
    mgr.note("graded", "two-pointers")
    mgr.log.append(session=s.id, kind="taught", source="tutor", mode="learn", now=clock.t,
                   fields={"object": "concept:arrays-basics"})
    mgr.note("logged", "arrays-basics")
    out = mgr.close("Worked on [two pointers](http://x) <b>")
    assert mgr.current is None
    assert out["session"] == s.id and out["counts"]["graded"] == 1
    assert out["moved"] == [
        {"id": "arrays-basics", "title": "Array basics", "before": "not_started",
         "after": "unverified"},
        {"id": "two-pointers", "title": "Two pointers", "before": "not_started",
         "after": "learning"}]
    assert out["unchecked"] == [{"id": "arrays-basics", "title": "Array basics"}]
    assert kinds(root, s.id)[-1] == "close"
    notes = list((root / "sessions").glob(f"2026-11-13-learn-{s.id[-8:].lower()}.md"))
    assert len(notes) == 1
    text = notes[0].read_text(encoding="utf-8")
    assert "20 min" in text and "Two pointers (two-pointers)" in text
    assert "Worked on two pointers" in text and "http" not in text and "<b>" not in text
    assert "# Today" in (root / "Today.md").read_text(encoding="utf-8")
    assert 'keepfold_status: "learning"' in (root / "concepts/two-pointers.md").read_text(
        encoding="utf-8")
    assert out["commit"] and git(root, "log", "-1", "--format=%s").strip() == \
        f"keepfold: session {s.id} learn"
    assert git(root, "status", "--porcelain") == ""
    assert "session learn: 0 changes, 1 graded" in (root / "journal/2026-11-13.md").read_text(
        encoding="utf-8")
    assert "] session | learn, 0 changes" in (root / "log.md").read_text(encoding="utf-8")


def test_session_page_escapes_hostile_text():
    """Session pages follow render.esc like cards: no links, images, URLs, controls; capped."""
    bad = "[click](http://evil.example) ![img](http://x/p.png) https://raw.example/a \x1b[31m\x00"
    rows = [{"id": "c1", "title": bad, "before": None, "after": "learning"}]
    text = sm_mod._session_md("S1", "learn", T0, T0 + timedelta(minutes=5), {}, rows,
                              [{"id": "c2", "title": bad}], bad + " x" * 2500)
    assert "http" not in text and "evil" not in text and "](" not in text and "img" not in text
    assert "\x1b" not in text and "\x00" not in text
    assert "click" in text  # the link text stays
    summary = text.split("## Summary\n")[1].strip()
    assert len(summary) <= sm_mod.SUMMARY_MAX and summary.endswith("…")


def test_close_unchecked_follows_the_7_day_rule(mgr, clock, root):
    mgr.log.append(session="cli", kind="taught", source="tutor", mode="learn",
                   now=clock.t - timedelta(days=10), fields={"object": "concept:arrays-basics"})
    s = mgr.start()
    clock.tick(minutes=5)
    mgr.log.append(session=s.id, kind="taught", source="tutor", mode="learn", now=clock.t,
                   fields={"object": "concept:two-pointers"})
    mgr.note("logged", "two-pointers")
    mgr.note("logged", "arrays-basics")  # touched again, but taught 10 days ago
    assert mgr.close("")["unchecked"] == [{"id": "two-pointers", "title": "Two pointers"}]


def _touch(mgr, s):
    mgr.log.append(session=s.id, kind="log", source="tutor", mode=s.mode, now=mgr._now(),
                   fields={"payload": {"kind": "touch", "ids": ["two-pointers"]}})
    mgr.note("logged", log_kind="touch")


@pytest.mark.parametrize("swept", [False, True])
def test_a_touch_only_session_closes_quietly(mgr, clock, root, swept, monkeypatch):
    s = mgr.start("ambient")
    _touch(mgr, s)
    if swept:  # its server died: the SessionEnd sweep closes it from the events alone
        mgr.current = None
        monkeypatch.setattr(sm_mod, "_pid_alive", lambda pid: False)
        clock.tick(hours=3)
        assert sweep(Vault(root), clock) == [s.id]
        commit = git(root, "rev-parse", "HEAD").strip()
    else:
        commit = mgr.close()["commit"]
    assert kinds(root, s.id) == ["open", "log", "close"]
    (events,) = files_in(root, commit)  # its commit holds only its own event file
    assert events.endswith(f"/{s.id}.jsonl")
    assert subjects(root)[0] == f"keepfold: session {s.id} ambient"
    for quiet in ("journal", "sessions", "log.md", "Today.md", "index.md",
                  ".keepfold/dashboard.html"):
        assert not (root / quiet).exists(), quiet
    assert git(root, "status", "--porcelain") == ""


def test_a_non_ledger_log_is_not_quiet(mgr, root):
    s = mgr.start("ambient")
    _touch(mgr, s)
    mgr.note("logged", log_kind="taught")
    mgr.close()
    assert list((root / "sessions").glob(f"*-{s.id[-8:].lower()}.md"))
    assert "session ambient:" in (root / "journal/2026-11-13.md").read_text(encoding="utf-8")


def test_commit_skipped_when_clean_or_not_a_repo(root, tmp_path, clock):
    assert sm_mod._commit(Vault(root), "nothing") is None
    plain = tmp_path / "plain"
    shutil.copytree(FIXTURES / "vault_mini", plain)
    mgr = SessionManager(Vault(plain), clock=clock)
    mgr.start()
    mgr.note("logged")
    assert mgr.close()["commit"] is None and (plain / "Today.md").is_file()


def test_close_leaves_a_file_with_a_key_uncommitted(mgr, root, caplog):
    key = "ghp_" + "x" * 30  # built here so the repo never holds a key-shaped literal
    (root / "inbox.md").write_text(f"pasted by hand: {key}\n", encoding="utf-8")
    (root / "clean.md").write_text("a plain hand edit\n", encoding="utf-8")
    mgr.start()
    mgr.note("logged")
    out = mgr.close()
    assert out["commit"]
    assert out["not_committed"] == [f"inbox.md: {SECRET_RES[1].pattern}"]  # the ghp_ rule
    assert git(root, "status", "--porcelain").strip() == "M inbox.md"
    assert "clean.md" in git(root, "log", "--name-only", "--format=")
    assert "inbox.md" in caplog.text and key not in caplog.text + str(out)


def test_a_hung_git_fails_the_commit_and_frees_the_lock(mgr, root, monkeypatch, caplog):
    calls = []

    def hung(cmd, **kw):
        calls.append(kw)
        raise sm_mod.subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    mgr.start()
    mgr.note("logged")
    monkeypatch.setattr(sm_mod.subprocess, "run", hung)
    assert mgr.close()["commit"] is None
    assert not (root / ".keepfold" / "lock").exists()
    assert calls and all(kw["timeout"] == 60 and kw["encoding"] == "utf-8" for kw in calls)
    assert "timed out" in caplog.text
    calls.clear()
    assert sm_mod._git(root, "status").returncode and calls[0]["encoding"] == "utf-8"


def test_commit_refused_mid_revert(root):
    (root / "inbox.md").write_text("half-done revert\n", encoding="utf-8")
    head = git(root, "rev-parse", "HEAD")
    (root / ".git/REVERT_HEAD").write_text(head, encoding="utf-8")
    assert sm_mod._commit(Vault(root), "nope") is None
    assert git(root, "rev-parse", "HEAD") == head


def test_commit_runs_git_add_and_commit_outside_the_vault_lock(root, monkeypatch):
    """B12: other writers wait only for the status snapshot, never for git add/commit."""
    real, held = sm_mod._commit_paths, []

    def spy(top, paths, message):
        held.append((root / ".keepfold" / "lock").exists())
        Vault(root).write_text(root / "inbox.md", "written while git runs\n")  # no wait
        return real(top, paths, message)

    monkeypatch.setattr(sm_mod, "_commit_paths", spy)
    (root / "clean.md").write_text("a hand edit\n", encoding="utf-8")
    sha = sm_mod._commit(Vault(root), "keepfold: test")
    assert held == [False]
    assert sha == git(root, "rev-parse", "HEAD").strip()
    assert git(root, "show", "--name-only", "--format=", sha).split() == ["clean.md"]
    assert git(root, "status", "--porcelain").strip() == "M inbox.md"  # not in the snapshot


def test_a_file_rewritten_after_the_snapshot_stays_out_of_the_commit(root, monkeypatch):
    """B12 race: session B rewrites a file between A's snapshot and A's add. Its text must not
    land in A's commit, or `undo A` would revert B's text."""
    real, calls = sm_mod._withhold_secrets, []

    def scan(top, paths, withheld):
        if not calls:  # runs outside the lock, after the snapshot
            Vault(root).write_text(root / "inbox.md", "session B's text\n")
        calls.append(paths)
        return real(top, paths, withheld)

    monkeypatch.setattr(sm_mod, "_withhold_secrets", scan)
    (root / "inbox.md").write_text("session A's text\n", encoding="utf-8")
    (root / "clean.md").write_text("an unchanged hand edit\n", encoding="utf-8")
    (root / "BRIEF.md").unlink()  # a tracked file's deletion
    sha = sm_mod._commit(Vault(root), "keepfold: test")
    assert "inbox.md" in calls[0]  # it was in the snapshot
    assert files_in(root, sha) == {"clean.md", "BRIEF.md"}
    assert git(root, "ls-tree", "--name-only", sha, "BRIEF.md").strip() == ""  # deleted
    assert git(root, "status", "--porcelain").strip() == "M inbox.md"
    assert (root / "inbox.md").read_text(encoding="utf-8") == "session B's text\n"


def test_a_same_size_replace_within_one_mtime_tick_is_still_caught(root, monkeypatch):
    real = sm_mod._withhold_secrets
    p = root / "inbox.md"

    def scan(top, paths, withheld):
        # once: the scan also runs for the empty `other` list, and a second replace could get
        # back the first file's freed inode number (ext4 reuses them), undoing the change
        if paths:
            st = p.stat()
            Vault(root).write_text(p, "BBBB\n")  # an atomic replace, same size as AAAA
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # and the same mtime
        return real(top, paths, withheld)

    monkeypatch.setattr(sm_mod, "_withhold_secrets", scan)
    p.write_text("AAAA\n", encoding="utf-8")
    assert sm_mod._commit(Vault(root), "keepfold: test") is None  # nothing of ours left
    assert git(root, "status", "--porcelain").strip() == "M inbox.md"


def test_commit_fails_cleanly_when_another_git_holds_the_index(root, caplog):
    (root / "clean.md").write_text("a hand edit\n", encoding="utf-8")
    (root / ".git/index.lock").write_text("", encoding="utf-8")  # a commit running elsewhere
    head = git(root, "rev-parse", "HEAD")
    assert sm_mod._commit(Vault(root), "keepfold: test") is None
    assert "index.lock" in caplog.text and git(root, "rev-parse", "HEAD") == head
    assert not (root / ".keepfold" / "lock").exists()
    (root / ".git/index.lock").unlink()
    assert sm_mod._commit(Vault(root), "keepfold: test")  # the next commit takes it


def test_budget_flags_only(mgr):
    s = mgr.start()
    for _ in range(41):
        mgr.note("asked")
    for _ in range(11):
        mgr.note("patched")
    assert s.over_events and s.over_patches
    assert mgr.require("grade") is s  # never blocks


def test_budget_hint_once_on_crossing(mgr):
    mgr.start()
    hints = [mgr.note("patched") for _ in range(13)]
    assert hints[:10] == [""] * 10 and hints[11:] == ["", ""]
    assert hints[10] == (" This session has made 11 changes; consider wrapping up with "
                         "keepfold_end.")


def test_close_lists_changes_in_session_note(mgr, root):
    s = mgr.start()
    mgr.note("patched", change={"tool": "patch", "id": "profile", "kind": "me",
                                "op": "append_to_section", "target": "Brief"})
    out = mgr.close()
    assert out["changes"][0]["id"] == "profile"
    note = next((root / "sessions").glob(f"*-{s.id[-8:].lower()}.md"))
    text = note.read_text(encoding="utf-8")
    assert "## Changed\n- patched me/profile § Brief (append)\n" in text
    s2 = mgr.start()
    mgr.note("graded", "two-pointers")
    mgr.close()
    assert "## Changed" not in next(
        (root / "sessions").glob(f"*-{s2.id[-8:].lower()}.md")).read_text(encoding="utf-8")


# ---- sweep, CLI ---------------------------------------------------------------------

def _orphan(root, when, concept="two-pointers"):
    log = EventLog(Vault(root))
    sid = f"s_{log._new_id(when)}"
    log.append(session=sid, kind="grade", source="tutor", mode="review", now=when,
               fields={"object": f"concept:{concept}", "verdict": "correct", "hints": 0})
    return sid


def test_sweep(root, clock):
    old = _orphan(root, T0 - timedelta(hours=3))
    recent = _orphan(root, T0 - timedelta(minutes=30), "arrays-basics")
    assert sweep(Vault(root), clock) == [old]
    assert kinds(root, old) == ["grade", "close"]
    assert kinds(root, recent) == ["grade"]
    assert list((root / "sessions").glob(f"*-review-{old[-8:].lower()}.md"))
    assert 'keepfold_status: "learning"' in (root / "concepts/two-pointers.md").read_text(
        encoding="utf-8")
    assert sweep(Vault(root), clock) == []  # already closed


def test_cli_session_sweep_close_and_hook(root, home):
    runner = CliRunner()
    now = datetime.now(UTC)
    old = _orphan(root, now - timedelta(hours=3))
    recent = _orphan(root, now - timedelta(minutes=5), "arrays-basics")
    r = runner.invoke(app, ["session", "sweep", "--vault", str(root)])
    assert r.exit_code == 0 and f"closed {old}" in r.output and recent not in r.output
    r = runner.invoke(app, ["session", "close", "--vault", str(root)])
    assert r.exit_code == 0 and f"closed {recent}" in r.output
    r = runner.invoke(app, ["session", "sweep", "--vault", str(root)])
    assert "No orphan sessions" in r.output
    assert not (home / ".keepfold").exists()


def test_exam_retention_agrees_across_start_close_and_replay(mgr, root, monkeypatch):
    # The fixture's pending Acme OA (2026-11-20) tests two-pointers, so it replays at 0.90
    # retention: due before today, where 0.85 would still call it Solid. Two Good
    # application passes 7 days apart reach stability ~21 and the gap pass Solid needs.
    for day in (15, 22):
        mgr.log.append(session="cli", kind="grade", source="tutor",
                       now=datetime(2026, 10, day, 10, tzinfo=UTC),
                       fields={"object": "concept:two-pointers", "verdict": "correct",
                               "hints": 0, "item_type": "application"})
    mgr.start()
    mgr.note("asked", "two-pointers")  # touched, but no new evidence
    out = mgr.close()
    assert out["moved"] == []
    note = root / "concepts/two-pointers.md"
    stamped = note.read_text(encoding="utf-8")
    assert 'keepfold_status: "needs_refresh"' in stamped

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return T0

    monkeypatch.setattr(cli_mod, "datetime", Frozen)
    r = CliRunner().invoke(app, ["replay", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert note.read_text(encoding="utf-8") == stamped  # same keepfold_status / keepfold_next


# ---- sweep keeps Changed: and closes sessions of exited servers (§15.2 N2, N3) ------------

def test_sweep_close_rebuilds_changed_counts_and_journal(mgr, clock, root, monkeypatch):
    s = mgr.start("ambient")
    mgr.note("patched", change={"tool": "patch", "id": "profile", "kind": "me",
                                "op": "append_to_section", "target": "Brief"})
    mgr.note("added", "two-pointers", change={"tool": "add", "id": "two-pointers",
                                              "kind": "concept", "op": "create", "target": None})
    mgr.current = None  # the process dies; no close
    monkeypatch.setattr(sm_mod, "_pid_alive", lambda pid: False)
    clock.tick(hours=3)
    assert sweep(Vault(root), clock) == [s.id]
    text = next((root / "sessions").glob(f"*-{s.id[-8:].lower()}.md")).read_text(
        encoding="utf-8")
    assert "patched 1, added 1" in text
    assert "## Changed\n- patched me/profile § Brief (append)\n" in text
    assert "two-pointers" in text.split("## Changed")[1]
    assert "session ambient: 2 changes" in (root / "journal/2026-11-13.md").read_text(
        encoding="utf-8")
    assert [e["kind"] for e in EventLog(Vault(root)).iter_events(s.id)] == [
        "open", "change", "change", "close"]


def test_a_session_closes_exactly_once(mgr, clock, root):
    s = mgr.start("ambient")
    mgr.note("patched", change={"tool": "patch", "id": "profile", "kind": "me",
                                "op": "append_to_section", "target": "Brief"})
    assert sweep(Vault(root), clock, force=True) == [s.id]  # e.g. a sweep in another process
    assert mgr.close("done")["already_closed"] is True
    assert mgr.current is None
    assert kinds(root, s.id).count("close") == 1
    journal = (root / "journal/2026-11-13.md").read_text(encoding="utf-8")
    assert journal.count("session ambient:") == 1


def test_sweep_closes_a_session_at_once_when_its_server_has_exited(mgr, clock, root,
                                                                   monkeypatch):
    gone = mgr.start()
    live = SessionManager(Vault(root), clock=clock).start()
    log = EventLog(Vault(root))
    (path,) = (root / "events").glob(f"*/*/{gone.id}.jsonl")
    path.write_text(path.read_text(encoding="utf-8").replace('"pid":', '"pid":1'),
                    encoding="utf-8")  # e.g. pid 12345 -> 112345, not this process
    dead = next(e["payload"]["pid"] for e in log.iter_events(gone.id))
    monkeypatch.setattr(sm_mod, "_pid_alive", lambda pid: pid != dead)
    clock.tick(minutes=5)  # far from the 2 h idle close
    assert sweep(Vault(root), clock) == [gone.id]  # the live server's session stays open
    assert kinds(root, live.id) == ["open"]


def test_a_live_servers_reading_session_survives_another_servers_sweep(root, clock):
    """B2: Desktop reads for hours (reads write no events); a new server's start-up sweep
    used to close its session, and Desktop's next write landed in a closed session."""
    desk = SessionManager(Vault(root), clock=clock)
    s = desk.require("log")  # a write opens ambient; its `open` holds this live pid
    for _ in range(15):
        clock.tick(minutes=10)  # under RESUME_AFTER: no resume event either
        assert desk.require("find") is s  # reads only
    assert kinds(root, s.id) == ["open"]
    assert sweep(Vault(root), clock) == []  # 2.5 h after its last event, but its pid lives
    assert desk.require("log") is s
    clock.tick(hours=25)
    desk.current = None  # forget it in memory; a day-old session is closed even if alive
    assert sweep(Vault(root), clock) == [s.id]


def test_a_write_after_another_process_closed_the_session_opens_a_fresh_one(root, clock):
    """B2: `require` on a write sees a close written by another process and starts anew,
    so the write is not appended to (and lost from the commit of) a closed session."""
    desk = SessionManager(Vault(root), clock=clock)
    s = desk.start("learn", topic="graphs")
    desk.expose()
    assert sweep(Vault(root), clock, force=True) == [s.id]  # e.g. `keepfold session close`
    assert desk.require("find") is s  # a read writes nothing: no need to reopen
    new = desk.require("grade")
    assert new is not s and desk.current is new
    assert (new.mode, new.topic, new.exposed) == ("learn", "graphs", True)
    assert kinds(root, s.id).count("close") == 1 and kinds(root, new.id) == ["open"]


def test_a_sweep_dates_an_idle_close_at_the_last_event(root, clock):
    """B15: an orphan swept days later closes at its last event: its journal line lands on
    its own day and its duration is real, while Today.md is as of now."""
    sid = _orphan(root, T0 - timedelta(days=3))
    assert sweep(Vault(root), clock) == [sid]
    close = list(EventLog(Vault(root)).iter_events(sid))[-1]
    assert close["kind"] == "close" and close["ts"] == "2026-11-10T12:00:01Z"
    assert "session review" in (root / "journal/2026-11-10.md").read_text(encoding="utf-8")
    assert not (root / "journal/2026-11-13.md").exists()
    note = next((root / "sessions").glob(f"*-{sid[-8:].lower()}.md")).read_text("utf-8")
    assert "- Duration: 0 min" in note
    assert "# Today · 2026-11-13" in (root / "Today.md").read_text(encoding="utf-8")


# ---- per-session commits (outside review: undo isolation) --------------------------------

def files_in(root, rev):
    return set(git(root, "show", "--name-only", "--format=", rev).split())


def subjects(root):
    return git(root, "log", "--format=%s").splitlines()


def test_session_commit_holds_only_its_files_and_undo_spares_the_rest(root, clock):
    mgr = SessionManager(Vault(root), clock=clock)
    s = mgr.start()
    mgr.log.append(session=s.id, kind="grade", source="tutor", mode="learn", now=clock.t,
                   fields={"object": "concept:two-pointers", "verdict": "correct", "hints": 0,
                           "confidence": 3})
    mgr.note("graded", "two-pointers")
    (root / "notes").mkdir()
    (root / "notes/hand.md").write_text("# Written in Obsidian\n", encoding="utf-8")
    (root / "inbox.md").write_text("- hand edit\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["log", "taught", "arrays-basics", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert subjects(root)[0] == "keepfold: cli log taught"  # the CLI write committed itself
    (cli_events,) = files_in(root, "HEAD")
    assert cli_events.startswith("events/") and cli_events.endswith("/cli.jsonl")
    clock.tick(minutes=20)
    commit = mgr.close()["commit"]
    assert subjects(root)[:2] == [f"keepfold: session {s.id} learn",
                                  f"keepfold: other changes before session {s.id}"]
    assert files_in(root, "HEAD~1") == {"notes/hand.md", "inbox.md"}
    mine = files_in(root, commit)
    assert not mine & {"notes/hand.md", "inbox.md", cli_events}
    assert {"concepts/two-pointers.md", "Today.md", "log.md"} <= mine
    assert "index.md" not in mine  # whole-vault views are built on demand, not at close
    assert any(p.endswith(f"/{s.id}.jsonl") for p in mine)
    assert any(p.startswith("sessions/") for p in mine)
    assert git(root, "status", "--porcelain") == ""

    r = CliRunner().invoke(app, ["undo", s.id, "--vault", str(root)])
    assert r.exit_code == 0 and "reverted" in r.output, r.output
    assert (root / "notes/hand.md").read_text(encoding="utf-8") == "# Written in Obsidian\n"
    assert (root / "inbox.md").read_text(encoding="utf-8") == "- hand edit\n"
    assert (root / cli_events).is_file() and not list(root.glob(f"events/*/*/{s.id}.jsonl"))
    assert subjects(root)[0] == f"keepfold: undo {s.id}"


def test_two_open_sessions_each_commit_only_their_own_notes(tmp_path, clock):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURES / "vault_mini", root)
    (root / ".gitignore").write_text(".keepfold/*\n")
    git(root, "init", "-q")  # no commit yet: the first close starts the history
    a, b = (SessionManager(Vault(root), clock=clock) for _ in range(2))  # two servers
    for m, nid in ((a, "note-a"), (b, "note-b")):
        m.start()
        m.vault.create_note("note", nid, {"title": nid}, "# x\n")
        m.note("added", change={"tool": "add", "id": nid, "kind": "note", "op": "create",
                                "target": None})
    sa, sb = a.current.id, b.current.id
    clock.tick(minutes=5)
    a.close()
    assert subjects(root) == [f"keepfold: session {sa} learn",
                              f"keepfold: other changes before session {sa}"]
    first, other = files_in(root, "HEAD"), files_in(root, "HEAD~1")
    assert "notes/note-a.md" in first and "concepts/two-pointers.md" in other
    assert "notes/note-b.md" not in first | other
    assert not any(sb in p for p in first | other)  # b is still open: its files wait for it
    clock.tick(minutes=5)
    b.close()
    assert subjects(root)[0] == f"keepfold: session {sb} learn"
    second = files_in(root, "HEAD")
    assert "notes/note-b.md" in second and "notes/note-a.md" not in second
    assert any(p.endswith(f"/{sb}.jsonl") for p in second)
    assert git(root, "status", "--porcelain") == ""


def test_a_new_file_deleted_before_the_commit_does_not_fail_it(tmp_path):
    import subprocess

    from keepfold import session as s
    top = tmp_path / "r"
    top.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=top, check=True)
    (top / "keep.md").write_text("a", encoding="utf-8")
    (top / "old.md").write_text("b", encoding="utf-8")
    assert s._commit_paths(str(top), ["old.md"], "first")
    (top / "old.md").unlink()  # a tracked file deleted: its deletion is committed
    assert s._commit_paths(str(top), ["keep.md", "gone.md", "old.md"], "second")
    files = subprocess.run(["git", "ls-files"], cwd=top, capture_output=True, text=True).stdout
    assert files.split() == ["keep.md"]
    assert not s._commit_paths(str(top), ["gone.md"], "nothing")


def test_extra_path_mac(home, monkeypatch):
    """A Dock-launched app gets a bare PATH: Homebrew, ~/.local/bin, npm-global and the
    Command Line Tools are added."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert real_extra_path().split(os.pathsep) == [
        "/opt/homebrew/bin", "/usr/local/bin", str(home / ".local/bin"),
        str(home / ".npm-global/bin"), "/Library/Developer/CommandLineTools/usr/bin"]


def test_extra_path_linux(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert real_extra_path().split(os.pathsep) == [str(home / ".local/bin")]


def test_extra_path_windows_is_registry(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sm_mod, "_registry_path", lambda: "X")
    assert real_extra_path() == "X"


def test_git_exe_found_in_extra_path_off_windows(home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    # only the real list's home folders: a Mac runner's own /opt/homebrew/bin/git comes first
    monkeypatch.setattr(sm_mod, "extra_path", lambda: os.pathsep.join(
        d for d in real_extra_path().split(os.pathsep) if d.startswith(str(home))))
    monkeypatch.setenv("PATH", "")
    exe = home / ".local" / "bin" / "git"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    exe.chmod(0o755)
    assert sm_mod.git_exe() == str(exe)


def test_git_stub_is_not_git(monkeypatch):
    """macOS ships /usr/bin/git as a stub that only offers the Command Line Tools."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sm_mod.shutil, "which", lambda cmd, path=None, **k: (
        "/usr/bin/git" if path is None else None))  # nothing in extra_path
    monkeypatch.setattr(sm_mod, "_clt_ok", lambda: False)
    assert sm_mod.git_exe() is None
    monkeypatch.setattr(sm_mod, "_clt_ok", lambda: True)
    assert sm_mod.git_exe() == "/usr/bin/git"


def test_git_missing_tool(monkeypatch):
    """No xcode-select at all: the Command Line Tools count as absent, nothing raises."""
    monkeypatch.setattr(sys, "platform", "darwin")
    ran = []

    def missing(args, **k):
        ran.append(args)
        raise FileNotFoundError(args[0])
    monkeypatch.setattr(targets.subprocess, "run", missing)
    assert sm_mod._clt_ok() is False
    assert ran == [["xcode-select", "-p"]]


def test_homebrew_git_wins_over_the_stub(tmp_path, monkeypatch):
    """A Dock-launched app's PATH finds the /usr/bin/git stub first; without the Command Line
    Tools, Homebrew's git in extra_path is used instead."""
    monkeypatch.setattr(sys, "platform", "darwin")
    brew = tmp_path / "homebrew-bin"
    brew.mkdir()
    exe = brew / "git"
    exe.write_bytes(b"")
    exe.chmod(0o755)
    real_which = shutil.which
    monkeypatch.setattr(sm_mod.shutil, "which", lambda cmd, *a, path=None, **k: (
        "/usr/bin/git" if path is None else real_which(cmd, *a, path=path, **k)))
    monkeypatch.setattr(sm_mod, "_clt_ok", lambda: False)
    monkeypatch.setattr(sm_mod, "extra_path", lambda: str(brew))
    assert sm_mod.git_exe() == str(exe)


# ---- housekeeping: stale index.lock, hourly temp sweep (A15r) -----------------------------

def _aged_lock(root, age_s):
    lock = root / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    t = time.time() - age_s
    os.utime(lock, (t, t))
    return lock


def test_stale_index_lock_removed_only_when_no_git_runs(root, monkeypatch):
    running = {"git": True}
    monkeypatch.setattr(sm_mod, "_git_running", lambda: running["git"])
    young = _aged_lock(root, 60)
    running["git"] = False
    assert sm_mod.clear_index_lock(root) is False and young.exists()  # a commit may be running
    old = _aged_lock(root, 3600)
    running["git"] = True
    assert sm_mod.clear_index_lock(root) is False and old.exists()  # git still runs
    running["git"] = False
    assert sm_mod.clear_index_lock(root) is True and not old.exists()
    assert sm_mod.clear_index_lock(root) is False  # none left


def test_process_list_decides_whether_git_runs(root, monkeypatch):
    def broken():
        raise OSError("no list")

    monkeypatch.setattr(sm_mod, "_process_names", broken)
    old = _aged_lock(root, 3600)
    assert sm_mod.clear_index_lock(root) is False and old.exists()  # unreadable: it stays
    monkeypatch.setattr(sm_mod, "_process_names", lambda: ["git-remote-https.exe"])
    assert sm_mod.clear_index_lock(root) is False and old.exists()
    monkeypatch.setattr(sm_mod, "_process_names", lambda: ["python.exe", "GitHubDesktop.exe"])
    assert sm_mod.clear_index_lock(root) is True
    assert isinstance(sm_mod._git_running(), bool)  # the real process list answers


def test_sweep_cleans_temp_files_at_most_once_an_hour(root, clock, monkeypatch):
    monkeypatch.setattr(sm_mod, "_swept_tmp", {})
    now = [1000.0]
    monkeypatch.setattr(sm_mod.time, "monotonic", lambda: now[0])
    tmp = root / "concepts" / ".a.md.ab12cd34.tmp"

    def leave():
        tmp.write_text("half", encoding="utf-8")
        os.utime(tmp, (time.time() - 7200,) * 2)

    leave()
    sweep(Vault(root), clock)
    assert not tmp.exists()
    leave()
    now[0] += 3599
    sweep(Vault(root), clock)
    assert tmp.exists()  # inside the hour
    now[0] += 2
    sweep(Vault(root), clock)
    assert not tmp.exists()


def test_doctor_shows_the_last_commit_and_a_left_lock(root):
    from keepfold import doctor
    last, lock = doctor.check_git_state(root)
    assert last[:2] == ("PASS", "Last vault commit") and last[2] != "none yet"
    assert lock == ("PASS", "index.lock present", "no", "")
    _aged_lock(root, 120)
    last, lock = doctor.check_git_state(root)
    assert lock[:3] == ("WARN", "index.lock present", "yes, 2 minutes ago")
