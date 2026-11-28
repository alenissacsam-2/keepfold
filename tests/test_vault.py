import os
import shutil
import time
from pathlib import Path

import pytest

from keepfold import vault as vault_mod
from keepfold.vault import Vault, VaultError, is_private, scan_secrets

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"


@pytest.fixture
def v(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    return Vault(root)


def code(excinfo):
    return excinfo.value.code


@pytest.mark.parametrize("bad", ["", "Upper", "-lead", "a/b", "a\\b", "..", "../me/profile",
                                 "x" * 82, "a b", "a_b"])
def test_invalid_ids_rejected(v, bad):
    with pytest.raises(VaultError) as e:
        v.path_for("concept", bad)
    assert code(e) == "invalid_id"


def test_traversal_rejected(v):
    with pytest.raises(VaultError) as e:
        v.read_path(Path("..") / "outside.md")
    assert code(e) == "invalid_id"
    with pytest.raises(VaultError) as e:
        v.write_text(v.root / "concepts" / ".." / ".." / "x.md", "hi")
    assert code(e) == "invalid_id"


def test_unknown_kind_and_id(v):
    with pytest.raises(VaultError) as e:
        v.path_for("planet", "x")
    assert code(e) == "invalid_argument"
    with pytest.raises(VaultError) as e:
        v.read("concept", "nope")
    assert code(e) == "unknown_id"


def test_read_and_private(v):
    note = v.read("concept", "two-pointers")
    assert note.fields["title"] == "Two pointers"
    assert note.body.startswith("# Two pointers\n")
    assert not is_private(note)
    assert is_private(v.read("concept", "secret-diary"))
    brief = v.read_path(Path("BRIEF.md"))
    assert brief.body.startswith("## Brief") and not brief.fields


def test_iter_notes_skips_unparsable(v, caplog):
    (v.root / "concepts" / "broken.md").write_text("---\ntitle: [unclosed\n---\nx\n")
    (v.root / "concepts" / "BadName.md").write_text("---\nid: x\n---\n")
    ids = [n.id for n in v.iter_notes("concept")]
    assert ids == ["arrays-basics", "secret-diary", "two-pointers"]
    assert "broken.md" in caplog.text and "BadName.md" in caplog.text
    assert {n.kind for n in v.iter_notes()} >= {"concept", "problem", "track", "me"}


def test_parse_errors_never_quote_the_note(v, caplog):
    """D1 §4.2: ruamel quotes the bad line; logs and MCP errors get the class and line only."""
    (v.root / "concepts" / "leaky.md").write_text("---\ntitle: ok\nbad: [unclosed SECRETWORD\n"
                                                  "---\nx\n")
    with pytest.raises(VaultError) as e:
        v.read("concept", "leaky")
    msg = str(e.value)
    assert "leaky.md" in msg and "Error at line " in msg and "SECRETWORD" not in msg
    with pytest.raises(VaultError) as e:
        v.read_path(Path("concepts/leaky.md"))
    assert "SECRETWORD" not in str(e.value)
    list(v.iter_notes("concept"))
    assert "leaky.md" in caplog.text and "SECRETWORD" not in caplog.text


def test_set_field_keeps_other_bytes(v):
    path = v.path_for("assessment", "acme-oa-2026")
    before = path.read_bytes()
    v.set_field("assessment", "acme-oa-2026", "outcome", "pass")
    after = path.read_bytes()
    assert after == before.replace(b'outcome: "pending"', b'outcome: "pass"')


@pytest.mark.parametrize("bad", ["next friday", "20261130", "2026-11-31", "2026-11-30x"])
def test_set_field_refuses_a_bad_assessment_date(v, bad):
    """B10: an unreadable date silently dropped the test off the exam clock."""
    before = v.path_for("assessment", "acme-oa-2026").read_bytes()
    with pytest.raises(VaultError, match="date"):
        v.set_field("assessment", "acme-oa-2026", "date", bad)
    assert v.path_for("assessment", "acme-oa-2026").read_bytes() == before
    v.set_field("assessment", "acme-oa-2026", "date", "2026-11-30")


def racer(monkeypatch, name: str, line: bytes) -> None:
    """The next write to `name` finds `line` appended by another editor (Obsidian) first."""
    real, fired = Vault._atomic_write, []

    def edit_first(self, path, text, expect=None):
        if Path(path).name == name and not fired:
            fired.append(1)
            with open(path, "ab") as f:
                f.write(line)
        return real(self, path, text, expect)

    monkeypatch.setattr(Vault, "_atomic_write", edit_first)


def test_update_text_keeps_an_edit_made_meanwhile(v, monkeypatch):
    """B13: read-modify-write of a page Obsidian may edit rereads instead of overwriting."""
    page = v.root / "inbox.md"
    page.write_bytes(b"# Inbox\n")
    racer(monkeypatch, "inbox.md", b"- typed in Obsidian\n")
    assert v.update_text(page, lambda t: t + "- mine\n") is True
    assert page.read_bytes() == b"# Inbox\n- typed in Obsidian\n- mine\n"
    new = v.root / "me" / "fresh.md"
    assert v.update_text(new, lambda t: t + "## A\n", default="# Fresh\n") is True
    assert new.read_bytes() == b"# Fresh\n## A\n"
    assert v.update_text(new, lambda t: t) is False
    with pytest.raises(VaultError, match="key or token"):
        v.update_text(page, lambda t: t + "- sk-ant-api03-" + "x" * 90 + "\n")
    with pytest.raises(VaultError):
        v.update_text(v.root / ".." / "out.md", lambda t: "x")


def test_set_field_crlf_body_identical(v):
    path = v.path_for("problem", "lc-1")
    crlf = path.read_bytes().replace(b"\n", b"\r\n")
    path.write_bytes(crlf)
    v.set_field("problem", "lc-1", "status", "solved")
    assert path.read_bytes() == crlf.replace(b'status: "attempted"', b'status: "solved"')


def test_set_field_new_field_and_int(v):
    v.set_field("track", "sprint-test", "current_day", 3)
    v.set_field("problem", "lc-167", "solved_on", "2026-11-13")
    assert b"current_day: 3\n" in v.path_for("track", "sprint-test").read_bytes()
    text = v.path_for("problem", "lc-167").read_text()
    assert 'status: "todo"\nsolved_on: "2026-11-13"\n---\n' in text


@pytest.mark.parametrize("kind,id,field,value", [
    ("concept", "two-pointers", "title", "x"),
    ("problem", "lc-1", "url", "https://x"),
    ("me", "profile", "title", "x"),
    ("problem", "lc-1", "status", "done"),  # not in the enum
    ("problem", "lc-1", "status", ["solved"]),
    ("concept", "two-pointers", "keepfold_status", "solid"),  # computed by Keepfold
    ("concept", "two-pointers", "keepfold_next", "2026-11-15"),
    ("concept", "two-pointers", "merged_into", "[[arrays-basics]]"),
])
def test_set_field_rejects(v, kind, id, field, value):
    with pytest.raises(VaultError) as e:
        v.set_field(kind, id, field, value)
    assert code(e) == "invalid_argument"


def test_set_field_refuses_tool_props_with_reason(v):
    with pytest.raises(VaultError, match="computed by Keepfold"):
        v.set_field("concept", "two-pointers", "keepfold_status", "solid")


def test_set_tool_props(v):
    path = v.path_for("concept", "two-pointers")
    v.set_tool_props("concept", "two-pointers", "learning", "2026-11-15")
    assert 'keepfold_status: "learning"\nkeepfold_next: "2026-11-15"\n---' in path.read_text()
    v.set_tool_props("concept", "two-pointers", "solid", None)
    text = path.read_text()
    assert 'keepfold_status: "solid"' in text and "keepfold_next" not in text


def test_append_to_section(v):
    path = v.path_for("mistake", "dsa.off-by-one")
    v.append_to_section("mistake", "dsa.off-by-one", "Pattern", "Also at the start.")
    assert "one short when the end index is inclusive.\nAlso at the start.\n\n## Fix" in (
        path.read_text())
    v.append_to_section("concept", "arrays-basics", "## Notes", "- first")
    assert v.read("concept", "arrays-basics").body.endswith("## Notes\n- first\n")


def test_append_to_me_note(v):
    v.append_to_section("me", "profile", "Brief", "- Prefers C++.")
    assert v.read("me", "profile").body.endswith("- Test learner, synthetic.\n- Prefers C++.\n")


def test_append_missing_heading(v):
    before = v.path_for("problem", "lc-1").read_bytes()
    with pytest.raises(VaultError) as e:
        v.append_to_section("problem", "lc-1", "Solutions", "x")
    assert code(e) == "invalid_argument"
    assert v.path_for("problem", "lc-1").read_bytes() == before


def test_check_item_ticks_exactly_one(v):
    path = v.path_for("track", "sprint-test")
    before = path.read_text().splitlines()
    assert v.check_item("track", "sprint-test", "Day 2", "[[lc-167]]") is True
    after = path.read_text().splitlines()
    diff = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert diff == [("- [ ] [[lc-167]] Two Sum II", "- [x] [[lc-167]] Two Sum II")]
    assert "- [ ] [[lc-167]] Two Sum II (owed copy)" in after  # other section untouched
    assert v.check_item("track", "sprint-test", "Day 2", "[[lc-167]]") is False  # idempotent


def test_check_item_ambiguous_or_missing(v):
    for text in ("[[two-pointers]]", "no such item"):
        with pytest.raises(VaultError) as e:
            v.check_item("track", "sprint-test", "Day 2", text)
        assert code(e) == "invalid_argument"


def test_create_note(v):
    path = v.create_note("problem", "lc-15", {"title": "3Sum", "concepts": ["[[two-pointers]]"],
                                               "status": "todo"}, "# 3Sum\n\n## Notes\n")
    assert path.read_bytes() == (b'---\nid: "lc-15"\ntitle: "3Sum"\n'
                                 b'concepts: ["[[two-pointers]]"]\nstatus: "todo"\n---\n'
                                 b"# 3Sum\n\n## Notes\n")
    assert v.read("problem", "lc-15").fields["concepts"] == ["[[two-pointers]]"]


def test_create_note_refuses_overwrite(v):
    before = v.path_for("problem", "lc-1").read_bytes()
    with pytest.raises(VaultError) as e:
        v.create_note("problem", "lc-1", {"title": "x"}, "# x\n")
    assert code(e) == "invalid_argument"
    assert v.path_for("problem", "lc-1").read_bytes() == before


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-" + "a" * 30,
    "ghp_" + "A" * 36,
    "github_pat_" + "B" * 30,
    "AKIAIOSFODNN7EXAMPLE",
    "-----BEGIN RSA PRIVATE KEY-----",
    "api_key = abcdef123456789",
])
def test_secret_rejected(v, secret):
    before = v.path_for("problem", "lc-1").read_bytes()
    with pytest.raises(VaultError) as e:
        v.append_to_section("problem", "lc-1", "Notes", f"note {secret}")
    assert code(e) == "secret_detected"
    with pytest.raises(VaultError) as e:
        v.create_note("problem", "lc-99", {"title": secret}, "")
    assert code(e) == "secret_detected"
    assert v.path_for("problem", "lc-1").read_bytes() == before
    assert not v.path_for("problem", "lc-99").exists()
    with pytest.raises(VaultError) as e:
        v.write_text(v.root / "Today.md", secret)
    assert code(e) == "secret_detected"
    v.write_text(v.root / ".keepfold" / "dashboard.html", secret)  # git-ignored view: not scanned


@pytest.mark.parametrize("secret, near_miss", [  # SEC-3; built by concatenation, no real keys
    ("sk_live_" + "a1" * 12, "sk_live_ is the Stripe prefix"),
    ("rk_live_" + "Z9" * 12, "rk_live_short"),
    ("xoxb-" + "1234567890-abcdef", "xoxb-short"),
    ("AIza" + "B" * 35, "AIza is a name"),
    ("glpat-" + "c" * 20, "glpat-abc"),
    ("hf_" + "d" * 30, "hf_model and hf_hub_download"),
    ("npm_" + "e1" * 18, "npm_config_cache"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlLXg", "eyJ.eyJ.x"),
    ("https://alice:s3cr3t@db.example.invalid", "https://example.invalid:8080/a@b"),
    ("Password = hunter22", "the password is long"),
    ("const privateKeyHex = '" + "ab" * 32 + "'",
     "tx hash 0x" + "ab" * 32 + " and, much later in the line, where a private key goes"),
])
def test_secret_patterns(secret, near_miss):
    with pytest.raises(VaultError):
        scan_secrets(f"note {secret}")
    scan_secrets(f"note {near_miss}")


def test_secret_scan_allows_plain_text(v):
    v.append_to_section("problem", "lc-1", "Notes", "- used sk-learn and an api key concept")


def test_lock_exclusive(v):
    other = Vault(v.root)
    with v.lock():
        t0 = time.monotonic()
        with pytest.raises(VaultError) as e:
            with other.lock(timeout=0.3):
                pass
        assert code(e) == "vault_locked"
        assert time.monotonic() - t0 >= 0.3
        with v.lock():  # reentrant for the same Vault
            pass
        assert (v.root / ".keepfold" / "lock").exists()
    assert not (v.root / ".keepfold" / "lock").exists()
    with other.lock(timeout=0.3):
        pass


def test_stale_lock_stolen(v):
    lock = v.root / ".keepfold" / "lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text(f"999999 {time.time() - 1000:.3f}")  # old and the pid is gone
    with v.lock(timeout=0.3):
        assert lock.read_text().startswith(f"{os.getpid()} ")
    lock.write_text(f"999999 {time.time():.3f}")  # fresh but the pid is gone
    with v.lock(timeout=0.3):
        pass
    assert not lock.exists()
    lock.write_text("garbage")  # no readable pid: the file's age decides
    old = time.time() - vault_mod.LOCK_STALE_S - 10
    os.utime(lock, (old, old))
    with v.lock(timeout=0.3):
        pass


def test_live_holders_lock_is_never_stolen(v, monkeypatch):
    """R2-3: a lock older than LOCK_STALE_S whose holder is alive (a long sites.write) stays."""
    lock = v.root / ".keepfold" / "lock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text(f"{os.getppid()} {time.time() - vault_mod.LOCK_STALE_S - 1:.3f}")
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: 0.0)  # started long before
    with pytest.raises(VaultError) as e:
        with v.lock(timeout=0.3):
            pass
    assert code(e) == "vault_locked"
    assert lock.exists()


def test_lock_of_a_reused_pid_is_stolen(v, monkeypatch):
    """G4: after a reboot the lock's pid can belong to a live, newer process; a holder that
    started after the lock was written is not the holder, so the lock is stale."""
    lock = v.root / ".keepfold" / "lock"
    lock.parent.mkdir(exist_ok=True)
    ts = time.time() - 3600
    monkeypatch.setattr(vault_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: ts + 600)  # a newer process
    lock.write_text(f"4242 {ts:.3f}")
    with v.lock(timeout=0.3):
        pass
    assert not lock.exists()
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: ts - 60)  # the real holder
    lock.write_text(f"4242 {ts:.3f}")
    with pytest.raises(VaultError):
        with v.lock(timeout=0.3):
            pass
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: None)  # unknown: age decides
    lock.write_text(f"4242 {time.time() - 600:.3f}")
    with pytest.raises(VaultError):  # ten minutes is within the bound
        with v.lock(timeout=0.3):
            pass
    lock.write_text(f"4242 {time.time() - 11 * vault_mod.LOCK_STALE_S:.3f}")
    with v.lock(timeout=0.3):
        pass
    assert not lock.exists()


def test_a_lock_this_process_failed_to_delete_is_taken_back(v, monkeypatch):
    """Windows refuses to delete a file another process is reading: a release that keeps
    failing leaves this pid's lock behind, and the long-running server must not wait on its own
    leftover forever. A lock another Vault of this process holds still waits."""
    from pathlib import Path
    lock = v.root / ".keepfold" / "lock"
    real = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(
        PermissionError("in use")) if self == lock else real(self, *a, **k))
    with v.lock(timeout=0.3):
        pass
    assert lock.read_text().startswith(f"{os.getpid()} ")  # left behind
    monkeypatch.setattr(Path, "unlink", real)
    with v.lock(timeout=0.3):  # taken back, not "busy"
        pass
    assert not lock.exists()
    other = Vault(v.root)
    with other.lock(timeout=0.3):
        with pytest.raises(VaultError):  # a live holder in this very process
            with v.lock(timeout=0.3):
                pass
    assert not lock.exists()


def test_a_coarse_clock_never_lets_a_second_taker_steal_a_live_lock(v, monkeypatch):
    """CI 27 Nov: time.time() ticks every 15.6 ms on Windows before Python 3.13, so a second
    Vault of this process tried in the holder's tick, built the same token, and its failed try
    unmarked the holder's: the live lock was then stolen as a leftover."""
    monkeypatch.setattr(vault_mod.time, "time", lambda: 1795770412.091)
    with Vault(v.root).lock(timeout=0.3):
        with pytest.raises(VaultError):
            with v.lock(timeout=0.3):
                pass
    assert not (v.root / ".keepfold" / "lock").exists()


def test_release_retries_a_briefly_busy_lock_file(v, monkeypatch):
    from pathlib import Path
    lock = v.root / ".keepfold" / "lock"
    real, fails = Path.unlink, []

    def busy_twice(self, *a, **k):
        if self == lock and len(fails) < 2:
            fails.append(1)
            raise PermissionError("a reader has it open")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", busy_twice)
    with v.lock(timeout=0.3):
        pass
    assert len(fails) == 2 and not lock.exists()


def test_pid_started_reads_this_process():
    started = vault_mod._pid_started(os.getpid())
    if started is not None or os.name == "nt":
        assert time.time() - 86400 * 30 < started <= time.time()


def test_lock_records_pid_host_and_born(v):
    import socket
    vault_mod._born.cache_clear()  # another test may have filled it under a fake _pid_started
    with v.lock():
        pid, _ts, host, born = (v.root / ".keepfold" / "lock").read_text().split()
    assert int(pid) == os.getpid() and host == socket.gethostname()
    assert float(born) == round(vault_mod._pid_started(os.getpid()) or 0)


def test_a_live_holder_with_its_born_survives_a_clock_that_went_back(v, monkeypatch):
    """Audit #2: after the clock jumps back past the holder's uptime its ts is older than its
    start time, which looked like a reused pid; the born field says it is the same process."""
    import socket
    lock = v.root / ".keepfold" / "lock"
    lock.parent.mkdir(exist_ok=True)
    born = time.time() - 600
    ts = born - 3 * 3600  # written after the clock went back three hours
    monkeypatch.setattr(vault_mod, "_FIXED_START", True)  # Windows: creation times never move
    monkeypatch.setattr(vault_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: born + 1.4)  # etime's rounding
    lock.write_text(f"4242 {ts:.3f}000 {socket.gethostname()} {born:.0f}")
    with pytest.raises(VaultError):
        with v.lock(timeout=0.3):
            pass
    assert lock.exists()
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: born + 600)  # a reused pid
    with v.lock(timeout=0.3):
        pass
    assert not lock.exists()
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: born - 600)  # the clock maths
    lock.write_text(f"4242 {time.time():.3f}000 {socket.gethostname()} {born:.0f}")  # keeps it,
    with v.lock(timeout=0.3):  # but the start time is not the holder's: stolen
        pass
    assert not lock.exists()


def test_a_born_mismatch_on_a_moving_clock_falls_back_to_the_ts_rule(v, monkeypatch):
    """/proc's and ps's start times shift with a clock step, so there a mismatch is no proof."""
    import socket
    lock = v.root / ".keepfold" / "lock"
    lock.parent.mkdir(exist_ok=True)
    ts = time.time() - 3600
    monkeypatch.setattr(vault_mod, "_FIXED_START", False)
    monkeypatch.setattr(vault_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: ts - 60)
    lock.write_text(f"4242 {ts:.3f}000 {socket.gethostname()} {ts - 900:.0f}")
    with pytest.raises(VaultError):
        with v.lock(timeout=0.3):
            pass
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: ts + 600)
    with v.lock(timeout=0.3):
        pass
    assert not lock.exists()


def test_an_unknown_born_keeps_the_ts_rule(v, monkeypatch):
    import socket
    lock = v.root / ".keepfold" / "lock"
    lock.parent.mkdir(exist_ok=True)
    ts = time.time() - 3600
    monkeypatch.setattr(vault_mod, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: ts - 60)
    for token in (f"4242 {ts:.3f} {socket.gethostname()}", f"4242 {ts:.3f} "
                  f"{socket.gethostname()} 0"):
        lock.write_text(token)
        with pytest.raises(VaultError):
            with v.lock(timeout=0.3):
                pass
    monkeypatch.setattr(vault_mod, "_pid_started", lambda pid: ts + 600)
    with v.lock(timeout=0.3):
        pass
    assert not lock.exists()


@pytest.mark.parametrize("out, secs", [
    ("05:07", 307), ("1:02:03", 3723), ("3-04:05:06", 273906), ("  12:34\n", 754),
    ("", None), ("garbage", None), ("1-05:07", None), ("12", None), ("ps: no such pid", None),
])
def test_etime_parser(out, secs):
    assert vault_mod._etime_s(out) == secs


def test_pid_started_on_a_mac_reads_ps_and_survives_its_absence(monkeypatch):
    import subprocess
    monkeypatch.setattr(vault_mod.sys, "platform", "darwin")
    monkeypatch.setattr(vault_mod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout="  01:40\n"))
    assert abs(vault_mod._pid_started(4242) - (time.time() - 100)) < 2

    def missing(*a, **k):
        raise FileNotFoundError("ps")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 2)

    for run in (missing, slow, lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="")):
        monkeypatch.setattr(vault_mod.subprocess, "run", run)
        assert vault_mod._pid_started(4242) is None


def test_replace_retries_permission_error(v, monkeypatch):
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError("locked by another program")
        return real(src, dst)

    monkeypatch.setattr(vault_mod.os, "replace", flaky)
    monkeypatch.setattr(vault_mod.time, "sleep", lambda s: None)
    v.set_field("problem", "lc-1", "status", "solved")
    assert len(calls) == 3
    assert v.read("problem", "lc-1").fields["status"] == "solved"
    assert not list(v.path_for("problem", "lc-1").parent.glob("*.tmp"))


def test_concurrent_edit_rereads(v, monkeypatch):
    path = v.path_for("problem", "lc-1")
    real_write = Vault._atomic_write
    seen = []

    def edit_then_write(self, p, text, expect=None):
        if not seen:  # Obsidian saves the note while Keepfold is mid-edit
            raw = path.read_bytes()
            path.write_bytes(raw.replace(b"- Hash map version first.",
                                         b"- Hash map version first.\n- Obsidian edit."))
        seen.append(text)
        return real_write(self, p, text, expect)

    monkeypatch.setattr(Vault, "_atomic_write", edit_then_write)
    v.append_to_section("problem", "lc-1", "Notes", "- Keepfold edit.")
    assert len(seen) == 2
    assert path.read_text().endswith("- Obsidian edit.\n- Keepfold edit.\n")


def test_id_with_trailing_newline_is_rejected(tmp_path):
    import shutil

    from keepfold.vault import Vault, VaultError

    root = tmp_path / "v"
    shutil.copytree(Path(__file__).parent / "fixtures" / "vault_mini", root)
    with pytest.raises(VaultError):
        Vault(root).path_for("concept", "two-pointers\n")


def test_bom_crlf_private_note_keeps_frontmatter_and_bytes(v):
    # T2: a UTF-8 BOM must not hide `private: true`
    path = v.path_for("problem", "lc-1")
    raw = path.read_bytes().replace(b"---\n", b"---\nprivate: true\n", 1)
    raw = b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n")
    path.write_bytes(raw)
    assert is_private(v.read("problem", "lc-1"))
    v.set_field("problem", "lc-1", "status", "solved")
    assert path.read_bytes() == raw.replace(b'status: "attempted"', b'status: "solved"')
    assert is_private(v.read("problem", "lc-1"))


def _flag(v, flag: str):
    path = v.path_for("problem", "lc-1")
    path.write_bytes(path.read_bytes().replace(b"---\n", f"---\nprivate: {flag}\n".encode(), 1))
    return v.read("problem", "lc-1")


@pytest.mark.parametrize("flag", ["true", "True", "yes", "Yes", "YES", "y", "on", "ON", "1",
                                  '"true"', "'yes'", '"1"', '" on "'])
def test_private_in_any_truthy_form_is_private(v, flag):
    # privacy: `private: yes` (and friends) used to read as public
    assert is_private(_flag(v, flag))


@pytest.mark.parametrize("flag", ["false", "no", "off", "0", '"false"', "maybe", "null", "2"])
def test_private_falsy_or_unknown_is_public(v, flag):
    assert not is_private(_flag(v, flag))


def test_replace_always_failing_is_vault_locked(v, monkeypatch):
    def locked(src, dst):
        raise PermissionError("locked by another program")

    monkeypatch.setattr(vault_mod.os, "replace", locked)
    monkeypatch.setattr(vault_mod.time, "sleep", lambda s: None)
    with pytest.raises(VaultError) as e:
        v.set_field("problem", "lc-1", "status", "solved")
    assert code(e) == "vault_locked" and "lc-1.md" in str(e.value)
    assert not list(v.path_for("problem", "lc-1").parent.glob("*.tmp"))


SITE_FM = ('id: "browse-a.com"\ntitle: "a \\"b\\" \\u00e9 \\\\ c \u00e9"\nkind: "browse"\n'
           'visits: 12\nneg: -3\nseen: true\nhidden: false\n')


@pytest.mark.parametrize("fm", [SITE_FM, SITE_FM.replace("\n", "\r\n"), "x: 1\n", ""])
def test_flat_frontmatter_reads_like_ruamel_without_it(fm, monkeypatch):
    """R2-10: ruamel took ~5 ms a page, minutes for 20k site pages; flat key: value lines
    (sites._fm) are read directly, to the same values and types."""
    want = vault_mod._yaml().load(fm) if fm.strip() else {}
    monkeypatch.setattr(vault_mod, "_yaml", lambda: pytest.fail("ruamel used"))
    got = vault_mod._load_fm(fm)
    assert got == want and [type(x) for x in got.values()] == [type(x) for x in want.values()]


@pytest.mark.parametrize("fm", ["tags: [a, b]\n", "a: 1\na: 2\n", "a: 1 # c\n", "a: 007\n",
                                "a: plain\n", 'a: "\\x41"\n', "a: |\n  b\n"])
def test_other_frontmatter_still_goes_through_ruamel(fm):
    real = vault_mod._yaml
    try:
        want = real().load(fm)
    except Exception as e:
        with pytest.raises(type(e)):
            vault_mod._load_fm(fm)
        return
    assert vault_mod._load_fm(fm) == want


def test_iter_notes_leaves_out_browse_site_pages(tmp_path):
    """R2-10: whole-vault walks (dashboard, lint) skip the generated site pages; the index
    still reads them through note_paths."""
    v = Vault(tmp_path)
    for rel in ("browse/2026-11-09.md", "browse/sites/browse-sites.md",
                "browse/sites/news/browse-a.com.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'---\nid: "{p.stem}"\nkind: "browse"\n---\nx\n', encoding="utf-8")
    assert {n.id for n in v.iter_notes("browse")} == {"2026-11-09", "browse-sites"}
    assert len(v.note_paths("browse")) == 3


def test_a_non_ascii_hostname_takes_and_releases_the_lock(v, monkeypatch):
    """Audit #6: a Cyrillic PC name on a cp1252 locale broke the token write and left the lock."""
    monkeypatch.setattr(vault_mod.socket, "gethostname", lambda: "Иван-ПК")
    lock = v.root / ".keepfold" / "lock"
    with v.lock(timeout=0.3):
        assert lock.read_bytes().decode("utf-8").split()[2] == "Иван-ПК"
        with pytest.raises(VaultError):  # a live holder in this very process: wait, not steal
            with Vault(v.root).lock(timeout=0.3):
                pass
    assert not lock.exists()


def test_own_live_lock_is_kept_after_the_hostname_changes(v, monkeypatch):
    """Audit #3: a Mac that wakes on another network gets a new gethostname(); its own held
    lock then looked like another host's and was stolen once LOCK_STALE_S passed."""
    with v.lock(timeout=0.3):
        later = time.time() + vault_mod.LOCK_STALE_S + 60
        monkeypatch.setattr(vault_mod.socket, "gethostname", lambda: "dhcp-10-4-2-7.campus.edu")
        monkeypatch.setattr(vault_mod.time, "time", lambda: later)
        with pytest.raises(VaultError) as e:
            with Vault(v.root).lock(timeout=0.3):
                pass
        assert code(e) == "vault_locked"
    assert not (v.root / ".keepfold" / "lock").exists()


def test_a_same_size_rewrite_in_one_mtime_tick_is_read_back(v):
    """Audit #7: FAT32/HFS+ keep mtime and size across an atomic replace; the inode tells."""
    path = v.create_note("problem", "tick-test", {"title": "Tick"}, "## Todo\n- [ ] one\n")
    assert "- [ ] one" in v.read("problem", "tick-test").body  # cached now
    st = path.stat()
    assert v.check_item("problem", "tick-test", "Todo", "one")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # a coarse clock: the same tick
    assert path.stat().st_size == st.st_size
    assert "- [x] one" in v.read("problem", "tick-test").body


def test_sqlite_ro_uri_gives_a_unc_path_an_empty_authority(tmp_path):
    """Audit #5: file://nas/share/... is "invalid uri authority" to SQLite; a vault on a share or
    a mapped drive (resolve() turns Z: into a UNC path) could not back up its stores."""
    import sqlite3
    from pathlib import PureWindowsPath

    from keepfold.vault import sqlite_ro_uri
    unc = sqlite_ro_uri(PureWindowsPath(r"\\nas\share\v\x.sqlite"))
    assert unc == "file:////nas/share/v/x.sqlite?mode=ro"
    assert sqlite_ro_uri(PureWindowsPath(r"C:\v\x.sqlite")) == "file:///C:/v/x.sqlite?mode=ro"
    db = tmp_path / "Zoë #1.sqlite"
    sqlite3.connect(db).close()
    with pytest.raises(sqlite3.OperationalError, match="authority"):  # the old URI's failure
        sqlite3.connect(PureWindowsPath(r"\\nas\share\v\x.sqlite").as_uri() + "?mode=ro",
                        uri=True)
    con = sqlite3.connect(sqlite_ro_uri(db), uri=True)
    try:
        assert con.execute("select 1").fetchone() == (1,)
    finally:
        con.close()


# ---- sweep_tmp (A15r) ------------------------------------------------------------------

def _tmp(path: Path, age_s: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("half", encoding="utf-8")
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def test_sweep_tmp_leaves_young_files(v):
    old = [_tmp(v.root / "concepts" / ".two-pointers.md.ab12cd34.tmp", 7200),
           _tmp(v.root / ".keepfold" / ".config.yaml.zz99yy88.tmp", 7200),
           _tmp(v.root / ".Today.md.q1w2e3r4.tmp", 7200)]
    young = _tmp(v.root / "concepts" / ".fresh.md.ab12cd34.tmp", 60)  # a write running now
    other = [_tmp(v.root / "concepts" / "plain.tmp", 7200),  # not atomic's name
             _tmp(v.root / "concepts" / ".hidden.md", 7200)]
    deep = _tmp(v.root / "concepts" / "sub" / ".x.md.ab12cd34.tmp", 7200)  # one folder deep: left
    assert v.sweep_tmp() == 3
    assert not any(p.exists() for p in old)
    assert young.exists() and deep.exists() and all(p.exists() for p in other)
    assert not (v.root / ".keepfold" / "lock").exists()  # the lock is let go
    assert v.sweep_tmp() == 0
    assert v.sweep_tmp(older_than=0) == 1 and not young.exists()


def test_sweep_tmp_skips_a_read_only_vault(v, monkeypatch):
    old = _tmp(v.root / "concepts" / ".two-pointers.md.ab12cd34.tmp", 7200)
    stamp = v.root / ".keepfold" / "format.json"
    stamp.parent.mkdir(exist_ok=True)
    stamp.write_text('{"format": 99, "vault_id": "x"}', encoding="utf-8")  # a newer Keepfold's
    assert v.sweep_tmp() == 0 and old.exists()
    monkeypatch.setattr(vault_mod.vault_format, "READ_GAP_S", 0)
    stamp.write_text("{not json", encoding="utf-8")  # unreadable
    assert v.sweep_tmp() == 0 and old.exists()
