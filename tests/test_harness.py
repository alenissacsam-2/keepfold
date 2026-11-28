"""GT2: the crash points in tests/e2e/_launch.py (KEEPFOLD_E2E_CRASH) and the vault invariant
checker in tests/harness/invariants.py, on copies of vault_mini under a temp HOME only.

Not gate-marked: these prove the harness, and a G18 gate counting them would go green without a
single recovery test."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from harness.invariants import check, sha256

pytestmark = pytest.mark.usefixtures("home")
LAUNCH = Path(__file__).parent / "e2e" / "_launch.py"


def launch(root: Path, *args, crash: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("KEEPFOLD_")}
    if crash:
        env["KEEPFOLD_E2E_CRASH"] = crash
    return subprocess.run([sys.executable, "-I", str(LAUNCH), *args, "--vault", str(root)],
                          env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def events(root: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in (root / "events").rglob("*.jsonl"))


# (spec, command, whether the write before the point landed); each kind of target once:
# a method, a staticmethod, a module function, and the n-th call
SPECS = [
    ("keepfold.events:EventLog._write", ["log", "taught", "two-pointers"], False),
    ("keepfold.vault:Vault._drop", ["log", "taught", "two-pointers"], True),
    ("keepfold.cli:_commit_cli", ["log", "taught", "two-pointers"], True),
    ("keepfold.vault:Vault._atomic_write#2", ["log", "solved", "lc-1"], True),
]


@pytest.mark.slow  # a keepfold process and two replays per case
@pytest.mark.parametrize(("spec", "command", "landed"), SPECS, ids=[s[0] for s in SPECS])
def test_a_crash_point_fires_and_leaves_a_sound_vault(root_committed, spec, command, landed):
    start = time.time()
    p = launch(root_committed, *command, crash=spec)
    assert p.returncode == 86, p.stderr
    assert f"CRASHPOINT {spec}" in p.stderr
    if command[1] == "taught":
        assert ('"kind":"taught"' in events(root_committed)) is landed
    else:  # the 1st _atomic_write (status) landed, the 2nd (solved_on) never ran
        text = (root_committed / "problems" / "lc-1.md").read_text(encoding="utf-8")
        assert 'status: "solved"' in text and "solved_on" not in text
    assert check(root_committed, since=start) == []


@pytest.mark.parametrize("spec", [
    "keepfold.vault:Vault._gone",  # a method renamed away
    "keepfold.nowhere:main",  # a module moved
    "keepfold.vault:Vault._drop#0",
    "keepfold.vault:VaultError.code",  # set in __init__, not a function on the class
])
def test_a_moved_point_fails_at_start(root_committed, spec):
    p = launch(root_committed, "log", "taught", "two-pointers", crash=spec)
    assert p.returncode == 1 and f"KEEPFOLD_E2E_CRASH={spec}" in p.stderr
    assert "CRASHPOINT" not in p.stderr and not (root_committed / "events").exists()


def test_a_point_never_reached_runs_to_the_end(root_committed):
    """What a harness sees when a refactor moved the call: exit 0, not 86."""
    p = launch(root_committed, "log", "taught", "two-pointers",
               crash="keepfold.events:EventLog._write#2")
    assert p.returncode == 0 and "CRASHPOINT" not in p.stderr
    assert launch(root_committed, "log", "taught", "two-pointers").returncode == 0


# ---- the checker ----------------------------------------------------------------------

LINE = '{"id":"01J0000000000000000000000A","kind":"taught","object":"concept:two-pointers"}\n'


def _events(root: Path, text: str) -> Path:
    p = root / "events" / "2026" / "11" / "s1.jsonl"
    p.parent.mkdir(parents=True)
    p.write_bytes(text.encode())
    return p


def test_the_checker_passes_a_clean_vault(root_committed):
    _events(root_committed, LINE * 2)
    note = root_committed / "concepts" / "two-pointers.md"
    receipts = [("concepts/two-pointers.md", sha256(note)),
                (str(root_committed / "events/2026/11/s1.jsonl"),
                 sha256(root_committed / "events/2026/11/s1.jsonl"))]
    start = time.time()
    (root_committed / "concepts" / ".arrays-basics.md.live.tmp").write_text("a writer's")
    assert check(root_committed, receipts, since=start - 1) == []  # a temp file from the run


def _torn(root):
    _events(root, LINE + LINE[:30])
    return [], "events/2026/11/s1.jsonl: torn last line"


def _garbled(root):
    _events(root, LINE + "{not json\n" + LINE)
    return [], "events/2026/11/s1.jsonl:2: not a whole JSON object"


def _stray_tmp(root):
    tmp = root / "concepts" / ".two-pointers.md.x1y2.tmp"
    tmp.write_text("half")
    os.utime(tmp, (time.time() - 3600,) * 2)
    return [], "concepts/.two-pointers.md.x1y2.tmp: temp file left behind"


def _half_note(root):
    note = root / "concepts" / "two-pointers.md"
    receipt = ("concepts/two-pointers.md", sha256(note))
    note.write_bytes(note.read_bytes()[:40])  # cut inside the front matter
    return [receipt], "concepts/two-pointers.md: does not parse"


def _missing(root):
    receipt = ("concepts/two-pointers.md", sha256(root / "concepts" / "two-pointers.md"))
    (root / "concepts" / "two-pointers.md").unlink()
    return [receipt], "concepts/two-pointers.md: receipt's file is missing"


def _replay_fails(root):
    (root / "catalog.yaml").write_text("domains: [\n", encoding="utf-8")
    return [], "keepfold replay failed"


@pytest.mark.slow  # two replays each
@pytest.mark.parametrize("broken", [_torn, _garbled, _stray_tmp, _half_note, _missing,
                                    _replay_fails])
def test_the_checker_fails_a_broken_vault(root_committed, broken):
    receipts, want = broken(root_committed)
    problems = check(root_committed, receipts, since=time.time())
    assert any(want in p for p in problems), problems


@pytest.mark.slow  # a held lock skips the replays; a free one runs two
def test_the_checker_fails_a_lock_a_live_process_holds(root_committed):
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.5)  # its start time is before the lock's
        (root_committed / ".keepfold").mkdir(exist_ok=True)
        (root_committed / ".keepfold" / "lock").write_text(
            f"{holder.pid} {time.time():.3f}000 {socket.gethostname()} 0", encoding="utf-8")
        assert check(root_committed) == [".keepfold/lock: held by a live process"]
        holder.kill()
        holder.wait()
        assert check(root_committed) == []  # a dead holder's lock is free
    finally:
        holder.kill()
