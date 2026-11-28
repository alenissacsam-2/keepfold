"""`keepfold doctor --security` (keepfold.redteam): the six probes PASS on this code, and each one
really detects its hole: with the one guard it depends on turned off, that probe and only that
probe FAILs. The run builds its own made-up vaults in a temp folder it deletes."""

import os
import re
import sqlite3
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keepfold import claims, cli, dashboard, forget, prime, redteam, review, tags, vecstore
from keepfold.browse import store

IDS = [p[0] for p in redteam.PROBES]


def _statuses(probes) -> dict:
    return {p.id: p.status for p in probes}


def test_every_probe_passes_and_the_temp_folder_and_env_are_left_as_found(monkeypatch):
    made = []
    real = redteam.tempfile.mkdtemp
    monkeypatch.setattr(redteam.tempfile, "mkdtemp",
                        lambda **k: made.append(real(**k)) or made[-1])
    before = {k: os.environ.get(k) for k in redteam._ENV}
    probes = redteam.run()
    assert [p.line() for p in probes] == [f"PASS  {i} {n}" for i, n, _, _ in redteam.PROBES]
    assert len(made) == 1 and not Path(made[0]).exists()
    assert {k: os.environ.get(k) for k in redteam._ENV} == before


def _carving_open(monkeypatch):
    """No secure_delete on any store connection and no compaction (VACUUM) after a forget: each
    layer alone covers for the other, so both go."""
    def off(real):
        def connect(*a, **k):
            con = real(*a, **k)
            con.execute("PRAGMA secure_delete=OFF")
            return con
        return connect
    monkeypatch.setattr(vecstore, "_open", off(vecstore._open))
    monkeypatch.setattr(store, "connect", off(store.connect))
    monkeypatch.setattr(forget, "_db", off(forget._db))
    monkeypatch.setattr(forget, "_compact", lambda path: False)


HOLES = {  # probe -> the one guard turned off
    "P1": lambda mp: mp.setattr(claims, "_IMAGE", re.compile(r"(?!x)x")),
    "P1-card": lambda mp: mp.setattr(dashboard, "plain", lambda text, limit=120: str(text)),
    "P2": lambda mp: mp.setattr(tags, "promo", lambda text: False),
    "P3": lambda mp: mp.setattr(prime, "withheld_playbooks", lambda vault, ids, evs: set()),
    "P4": lambda mp: mp.setattr(review, "outside_lines", lambda vault, events: []),
    "P5": _carving_open,
    "P5-sweep": lambda mp: mp.setattr(forget, "_sweep", lambda *a, **k: {}),  # events, caches
    "P6": lambda mp: mp.setattr(claims, "command", lambda text: None),
}


@pytest.mark.slow
@pytest.mark.parametrize("hole", HOLES)
def test_each_probe_fails_when_its_guard_is_off(hole, monkeypatch):
    HOLES[hole](monkeypatch)
    probes = redteam.run()
    probe = hole.split("-")[0]
    assert _statuses(probes) == {i: "FAIL" if i == probe else "PASS" for i in IDS}
    bad = next(p for p in probes if p.id == probe)
    assert bad.why and "probe setup" not in bad.why and "crashed" not in bad.why, bad.why


def test_secure_delete_is_what_p5_checks(tmp_path):
    """The P5 negative's premise: without secure_delete (or a VACUUM) a deleted row's bytes
    stay on disk."""
    p = tmp_path / "x.db"
    con = sqlite3.connect(p)
    con.execute("PRAGMA secure_delete=OFF")
    con.execute("CREATE TABLE t(v TEXT)")
    con.execute("INSERT INTO t VALUES ('CANARY-SELF-TEST')")
    con.commit()
    con.execute("DELETE FROM t")
    con.commit()
    con.close()
    assert b"CANARY-SELF-TEST" in p.read_bytes()


def test_doctor_security_prints_a_line_per_probe_and_exits_1_on_a_fail(monkeypatch):
    probes = [redteam.Probe(i, n, a, ok=i != "P3", why=None if i != "P3" else "a guard")
              for i, n, a, _ in redteam.PROBES]
    monkeypatch.setattr(redteam, "run", lambda now=None: probes)
    r = CliRunner().invoke(cli.app, ["doctor", "--security"])
    assert r.exit_code == 1, r.output
    assert "PASS  P1 image exfiltration\n" in r.output
    assert "FAIL  P3 MemoryGraft playbook: a guard\n" in r.output
    assert "PASS  P6 command line\n" in r.output
    assert r.output.rstrip().endswith("7 of 8 attacks stopped; 1 got through.")
    for p in probes:
        p.ok = True
    r = CliRunner().invoke(cli.app, ["doctor", "--security"])
    assert r.exit_code == 0 and r.output.rstrip().endswith("8 of 8 attacks stopped.")
    assert "Python" not in r.output  # not the plain doctor's checks


@pytest.mark.perf
def test_the_red_team_takes_under_eleven_seconds():
    # measured 24 Nov, warm, at ~100% CPU from other suites: 8.0-9.6 s (P5 ~30%, P2 and P3
    # ~20% each, prime/context over three apps ~35% in all). Idle was ~8.3 s before _vault took
    # one lock for its ten writes (-0.2 s a vault), so ~7.5 s idle expected; the bound is ~1.5x
    redteam.run()  # imports warm
    t = time.monotonic()
    assert all(p.ok for p in redteam.run())
    assert time.monotonic() - t < 11
