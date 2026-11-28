import json
import os
import shutil
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from keepfold import events
from keepfold.events import EventLog, local_day
from keepfold.vault import Vault, VaultError

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"
NOON = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
GOOD = {"object": "concept:edit-distance", "item_type": "recall", "question": "What is dp[i][j]?"}


@pytest.fixture
def ev(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    return EventLog(Vault(root))


def files(ev):
    return sorted((ev.vault.root / "events").rglob("*.jsonl"))


def grade(ev, now=NOON, session="cli", key=None, **fields):
    return ev.append(session=session, kind="grade", source="external", now=now, key=key,
                     fields={"object": "concept:edit-distance", "verdict": "correct", **fields})


def test_append_stamps_server_fields(ev):
    e = ev.append(session="cli", kind="ask", source="tutor", mode="learn", fields=GOOD,
                  predicted_p=0.5, grader={"client": "claude-code", "model": "x"}, now=NOON)
    assert e["ts"] == "2026-11-13T12:00:00Z" and e["schema"] == 1 and e["key"] == e["id"]
    assert e["source"] == "tutor" and e["predicted_p"] == 0.5
    [path] = files(ev)
    assert path.relative_to(ev.vault.root).as_posix() == "events/2026/11/cli.jsonl"
    assert json.loads(path.read_text("utf-8")) == e


def test_ids_increase_across_eventlogs_in_one_process(ev):
    # the app server builds one EventLog per request; ids must still order within a millisecond
    other = EventLog(ev.vault)
    # `now` steps back a millisecond now and then (two requests that read the clock in one
    # order and append in the other); all within one second, so ts ties and id decides
    ids = [(ev if i % 2 else other).append(
        session="cli", kind="log", source="self", fields={"payload": {"i": i}},
        now=NOON + timedelta(milliseconds=500 - i % 3))["id"] for i in range(200)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


@pytest.mark.parametrize("field", ["id", "ts", "day", "source", "predicted_p", "grader",
                                   "schema", "key", "session"])
def test_server_fields_rejected(ev, field):
    with pytest.raises(VaultError) as e:
        ev.append(session="cli", kind="ask", source="tutor", fields={**GOOD, field: "x"})
    assert e.value.code == "invalid_argument"
    assert not files(ev)


@pytest.mark.parametrize("fields", [
    {"bogus": 1},
    {"item_type": "essay"},
    {"verdict": "right"},
    {"model_verdict": "maybe"},
    {"confidence": 0}, {"confidence": 5}, {"confidence": True}, {"confidence": "3"},
    {"hints": -1}, {"hints": 4},
    {"question": "q" * 2001},
    {"learner_answer": "a" * 2001},
    {"answer_key": 7},
    {"summary": "s" * 601},
    {"mistakes": ["Bad Id"]}, {"mistakes": ["ok\n"]},
    {"also": ["dp-2d"]},
    {"payload": {"note": "n" * 2001}},
    {"payload": "text"},
])
def test_field_validation(ev, fields):
    with pytest.raises(VaultError) as e:
        ev.append(session="cli", kind="log", source="self", fields=fields)
    assert e.value.code == "invalid_argument"


def test_limits_inclusive(ev):
    e = ev.append(session="cli", kind="close", source="tutor", now=NOON,
                  fields={"summary": "s" * 600, "question": "q" * 300, "hints": 3,
                          "confidence": 4, "mistakes": ["dp.min-vs-max-choice"],
                          "also": ["concept:dp-2d"], "problem": "problem:lc-72"})
    assert len(e["summary"]) == 600


@pytest.mark.parametrize("args", [
    {"session": "s_bad"}, {"session": "../x"}, {"session": "cli\n"},
    {"kind": "accept"},
    {"source": "claimed_external"}, {"mode": "party"}, {"predicted_p": 1.5},
    {"grader": {"client": "c", "evil": "x"}},
])
def test_call_validation(ev, args):
    base = {"session": "cli", "kind": "ask", "source": "tutor", "fields": GOOD}
    with pytest.raises(VaultError) as e:
        ev.append(**{**base, **args})
    assert e.value.code == "invalid_argument"


@pytest.mark.parametrize("obj,ok", [
    ("concept:edit-distance", True), ("inbox:dijkstra", True), ("domain:dsa", True),
    ("area:graphs", True), ("mistake:dp.off-by-one", True), ("problem:lc-72", True),
    ("assessment:oa-1", True), ("application:acme", True),
    ("edit-distance", False), ("concept:", False), ("concept:Upper", False),
    ("track:x", False), ("concept:a/b", False), ("concept:x\n", False), (3, False),
])
def test_object_regex(ev, obj, ok):
    if ok:
        assert ev.append(session="cli", kind="log", source="self",
                         fields={"object": obj})["object"] == obj
    else:
        with pytest.raises(VaultError):
            ev.append(session="cli", kind="log", source="self", fields={"object": obj})


def test_secret_rejected(ev):
    with pytest.raises(VaultError) as e:
        grade(ev, learner_answer="my key is sk-" + "a" * 30)
    assert e.value.code == "secret_detected"
    assert not files(ev)


def test_duplicate_key_returns_first(ev):
    first = grade(ev, key="k1")
    again = grade(ev, key="k1", now=NOON + timedelta(hours=1), verdict="wrong")
    assert again == first
    [path] = files(ev)
    assert path.read_bytes().count(b"\n") == 1
    assert ev.has_key("k1") and not ev.has_key("k2")
    fresh = EventLog(ev.vault)  # rebuilds the key set from the files
    assert fresh.has_key("k1") and grade(fresh, key="k1") == first
    assert path.read_bytes().count(b"\n") == 1


def test_torn_tail_skipped_and_never_truncated(ev):
    first = grade(ev)
    [path] = files(ev)
    with open(path, "ab") as f:
        f.write(b'{"id":"01TORN","ts":"2026-11-13T12:00:01Z","ki')
    assert list(ev.iter_events()) == [first]
    second = grade(ev, now=NOON + timedelta(minutes=1))
    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b"" and len(lines) == 4  # the torn line stays, closed by a newline
    assert lines[1].startswith(b'{"id":"01TORN"')
    assert list(ev.iter_events()) == [first, second]


def test_whole_last_line_without_newline_survives_an_append(ev):
    first = grade(ev)
    [path] = files(ev)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))  # a complete event, no trailing newline
    second = grade(ev, now=NOON + timedelta(minutes=1))
    assert list(ev.iter_events()) == [first, second]


def test_invalid_lines_skipped(ev):
    first = grade(ev)
    [path] = files(ev)
    with open(path, "ab") as f:
        f.write(b"not json\n[1,2]\n\xff\xfe\n{\"no\":\"ts\"}\n")
    second = grade(ev, now=NOON + timedelta(minutes=1))
    assert list(ev.iter_events()) == [first, second]


def test_cached_reads_match_a_fresh_parse(ev):
    first = grade(ev)
    again = list(ev.iter_events())  # parsed once, then served from the cache
    again[0]["kind"] = "edited by a caller"  # a copy: the cache is not touched
    assert list(ev.iter_events()) == [first]
    second = grade(ev, now=NOON + timedelta(minutes=1))  # an append re-parses that file
    cached = list(ev.iter_events())
    events._PARSED.clear()
    assert cached == list(ev.iter_events()) == [first, second]


IST = timezone(timedelta(hours=5, minutes=30))


@pytest.mark.parametrize("utc,tz,day", [
    (datetime(2026, 11, 13, 22, 29, tzinfo=UTC), IST, "2026-11-13"),  # 03:59 IST
    (datetime(2026, 11, 13, 22, 30, tzinfo=UTC), IST, "2026-11-14"),  # 04:00 IST
    (datetime(2026, 11, 13, 3, 59, tzinfo=UTC), UTC, "2026-11-12"),
    (datetime(2026, 11, 13, 4, 0, tzinfo=UTC), UTC, "2026-11-13"),
    (datetime(2026, 11, 13, 0, 30), UTC, "2026-11-12"),  # naive means UTC
    (datetime(2027, 1, 1, 3, 0, tzinfo=UTC), UTC, "2026-12-31"),
])
def test_local_day_rollover(utc, tz, day):
    assert local_day(utc, tz) == day


def test_month_crossing_two_files(ev):
    grade(ev, now=datetime(2026, 11, 30, 12, tzinfo=UTC))
    grade(ev, now=datetime(2026, 12, 1, 12, tzinfo=UTC))
    rel = [p.relative_to(ev.vault.root).as_posix() for p in files(ev)]
    assert rel == ["events/2026/11/cli.jsonl", "events/2026/12/cli.jsonl"]


def test_iter_events_order_and_session_filter(ev):
    s = "s_01JD0000000000000000000000"
    c = grade(ev, now=NOON + timedelta(hours=2))
    a = grade(ev, session=s, now=NOON)
    b1 = grade(ev, session="import", now=NOON + timedelta(hours=1))
    b2 = grade(ev, session=s, now=NOON + timedelta(hours=1))  # same ts: id breaks the tie
    assert [e["id"] for e in ev.iter_events()] == [a["id"], b1["id"], b2["id"], c["id"]]
    assert [e["id"] for e in ev.iter_events(s)] == [a["id"], b2["id"]]
    with pytest.raises(VaultError):
        list(ev.iter_events("../x"))


def test_keys_are_shared_across_logs_and_follow_a_revert(ev):
    a, b = EventLog(ev.vault), EventLog(ev.vault)
    assert not b.has_key("k1")  # b reads the log before a writes
    first = grade(a, key="k1")
    assert grade(b, key="k1", now=NOON + timedelta(hours=1)) == first
    [path] = files(ev)
    assert path.read_bytes().count(b"\n") == 1
    path.write_bytes(b"")  # keepfold undo reverts the file
    again = grade(a, key="k1", now=NOON + timedelta(hours=2))
    assert again["id"] != first["id"] and path.read_bytes().count(b"\n") == 1


def problem_status(ev, pid, status, now):
    """What mcp_server._log_problem does: log the event, then set the note's fields."""
    e = ev.append(session="cli", kind="log", source="tutor", now=now, fields={
        "object": f"problem:{pid}",
        "payload": {"kind": "problem_status", "problem": pid, "status": status}})
    ev.vault.set_field("problem", pid, "status", status)
    if status == "solved":
        ev.vault.set_field("problem", pid, "solved_on", e["day"])
    return e


def void(ev, e, now=NOON + timedelta(days=1)):
    return events.void_event(ev, session="cli", event_id=e["id"], reason="wrong", source="self",
                             now=now)


def test_void_checks_already_voided_under_the_lock(ev, monkeypatch):
    """B11: the 'already voided' read ran outside the lock, so two voids could both pass it."""
    e = problem_status(ev, "lc-167", "solved", NOON)
    real, depths = EventLog.iter_events, []

    def seen(self, *a, **k):
        depths.append(self.vault._depth)
        return real(self, *a, **k)

    monkeypatch.setattr(EventLog, "iter_events", seen)
    void(ev, e)
    assert depths and depths[0] > 0
    with pytest.raises(VaultError, match="already voided"):
        void(ev, e)


def test_void_solve_resets_problem_to_todo(ev):
    e = problem_status(ev, "lc-167", "solved", NOON)
    void(ev, e)
    f = ev.vault.read("problem", "lc-167").fields
    assert f["status"] == "todo" and "solved_on" not in f


def test_void_solve_restores_the_latest_live_status(ev):
    problem_status(ev, "lc-1", "attempted", NOON)
    e = problem_status(ev, "lc-1", "solved", NOON + timedelta(hours=1))
    void(ev, e)
    f = ev.vault.read("problem", "lc-1").fields
    assert f["status"] == "attempted" and "solved_on" not in f


def test_void_solve_keeps_a_later_hand_edit(ev):
    e = problem_status(ev, "lc-167", "solved", NOON)
    ev.vault.set_field("problem", "lc-167", "status", "attempted")  # the owner's hand edit
    void(ev, e)
    assert ev.vault.read("problem", "lc-167").fields["status"] == "attempted"


@pytest.mark.parametrize("bad", [{"score": [90]}, {"score": True}, {"date": "soon"}])
def test_a_bad_assessment_result_writes_nothing(ev, bad):
    """B10: a bad score failed after the note was created, leaving a partial write."""
    with pytest.raises(VaultError):
        events.log_assessment(ev, session="cli", source="self", assessment="new-oa",
                              outcome="pass", now=NOON, **bad)
    assert not ev.vault.path_for("assessment", "new-oa").exists()
    assert not list(ev.iter_events())


def test_void_assessment_result_resets_outcome(ev):
    e = events.log_assessment(ev, session="cli", source="self", assessment="acme-oa-2026",
                              outcome="pass", now=NOON)
    assert ev.vault.read("assessment", "acme-oa-2026").fields["outcome"] == "pass"
    void(ev, e)
    assert ev.vault.read("assessment", "acme-oa-2026").fields["outcome"] == "pending"


def assess(ev, outcome, score=None, hours=0):
    return events.log_assessment(ev, session="cli", source="self", assessment="acme-oa",
                                 outcome=outcome, score=score, now=NOON + timedelta(hours=hours))


def test_void_assessment_result_drops_its_score(ev):
    e = assess(ev, "pass", 87)
    void(ev, e)
    f = ev.vault.read("assessment", "acme-oa").fields
    assert f["outcome"] == "pending" and "score" not in f


def test_void_assessment_result_restores_the_last_live_score(ev):
    assess(ev, "pass", "72%")
    assess(ev, "pass", hours=1)  # no score: the note keeps 72%
    e = assess(ev, "fail", 40, hours=2)
    void(ev, e)
    f = ev.vault.read("assessment", "acme-oa").fields
    assert f["outcome"] == "pass" and f["score"] == "72%"


def test_void_assessment_result_keeps_a_hand_edited_score(ev):
    e = assess(ev, "pass", 87)
    ev.vault.set_field("assessment", "acme-oa", "score", 90)
    void(ev, e)
    assert ev.vault.read("assessment", "acme-oa").fields["score"] == 90


# ---- the incremental parse cache and key map ----------------------------------------------

def fresh(ev):
    """The log as a cold process parses it."""
    events._PARSED.clear()
    return list(EventLog(ev.vault).iter_events())


def raw(i, key=None):
    """One event line as another process would write it."""
    eid = f"01JD{i:022d}"
    return (json.dumps({"id": eid, "key": key or eid, "ts": "2026-11-13T13:00:00Z",
                        "day": "2026-11-13", "session": "cli", "kind": "log",
                        "source": "external", "schema": 1}) + "\n").encode()


def test_incremental_parse_matches_a_full_parse(ev):
    for i in range(60):
        out = grade(ev, now=NOON + timedelta(seconds=i), key=f"k{i}")
        out["verdict"] = "edited by a caller"  # the returned event is not the cached one
        if i % 7 == 0:
            list(ev.iter_events())
        if i % 13 == 0:
            [path] = files(ev)
            with open(path, "ab") as f:  # another process
                f.write(raw(i) + b"not json\n")
    cached = list(ev.iter_events())
    assert len(cached) == 60 + 5 and all(e.get("verdict") != "edited by a caller" for e in cached)
    assert cached == fresh(ev)


def test_another_process_append_and_key_are_seen(ev):
    grade(ev)
    assert not ev.has_key("theirs")
    [path] = files(ev)
    with open(path, "ab") as f:
        f.write(raw(1, key="theirs"))
    assert ev.has_key("theirs") and len(list(ev.iter_events())) == 2
    other = EventLog(Vault(ev.vault.root))  # another instance with its own Vault
    theirs = grade(other, key="k2", now=NOON + timedelta(minutes=1))
    assert ev.has_key("k2")
    assert grade(ev, key="k2", now=NOON + timedelta(hours=1)) == theirs  # a retry: the old event
    assert grade(ev, key="k2", now=NOON + timedelta(hours=2)) == theirs
    assert len(list(ev.iter_events())) == 3


@pytest.mark.parametrize("how,keys", [
    ("replace", ["ka", "kc"]),  # keepfold undo: temp file + os.replace, here also longer
    ("in_place_same_size", ["ka", "kc"]),
    ("shorter", ["ka"]),
    ("grew_tail_changed", ["ka", "kb"]),
])
def test_a_rewrite_parses_the_file_again(ev, how, keys):
    a = grade(ev, key="ka")
    b = grade(ev, key="kb", now=NOON + timedelta(minutes=1))
    assert ev.has_key("kb")
    [path] = files(ev)
    data, stat = path.read_bytes(), path.stat()
    rekeyed = data.replace(b'"kb"', b'"kc"')  # same size; outside the last 64 bytes
    if how == "replace":
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(rekeyed + raw(1))
        tmp.replace(path)
    elif how == "in_place_same_size":
        path.write_bytes(rekeyed)
    elif how == "shorter":
        path.write_bytes(data[:data.index(b"\n") + 1])
    else:
        i = data.rindex(b'"correct"')  # in the last line's last 64 bytes
        path.write_bytes(data[:i] + b'"partial"' + data[i + 9:] + raw(1))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))  # a coarse clock differs too
    got = list(ev.iter_events())
    assert [e["key"] for e in got if e["key"] in ("ka", "kb", "kc")] == keys
    assert got == fresh(ev) and got[0]["id"] == a["id"]
    if how == "grew_tail_changed":
        assert [e.get("verdict") for e in got[:2]] == ["correct", "partial"]
    assert ev.has_key("kb") == (how == "grew_tail_changed")
    again = grade(ev, key="kb", now=NOON + timedelta(hours=1))
    assert (again["id"] == b["id"]) == (how == "grew_tail_changed")


def test_a_torn_line_is_parsed_once_when_completed(ev, caplog):
    grade(ev)
    [path] = files(ev)
    line = raw(1, key="late")
    with open(path, "ab") as f:
        f.write(line[:30])
    assert len(list(ev.iter_events())) == 1 and not ev.has_key("late")
    with open(path, "ab") as f:
        f.write(line[30:])
    assert len(list(ev.iter_events())) == 2 and ev.has_key("late")
    with open(path, "ab") as f:
        f.write(raw(2)[:-1])  # a whole event, not yet terminated, is seen
    assert len(list(ev.iter_events())) == 3
    with open(path, "ab") as f:
        f.write(b'\n{"torn')
    assert len(list(ev.iter_events())) == 3
    assert "skipping" not in caplog.text  # a torn line may be mid-write: not logged yet
    with open(path, "ab") as f:
        f.write(b'"]\n')
    for _ in range(3):
        list(ev.iter_events())
    grade(ev, now=NOON + timedelta(hours=1))
    got = list(ev.iter_events())
    assert caplog.text.count("skipping") == 1  # logged once, when the line was completed
    assert len(got) == 4 and got == fresh(ev)


def test_own_append_reads_nothing_back(ev, monkeypatch):
    grade(ev)
    ev.has_key("x")
    calls = []
    monkeypatch.setattr(events, "_parse_file", lambda *a: calls.append(a))
    for i in range(5):
        grade(ev, key=f"k{i}", now=NOON + timedelta(minutes=i))
    assert calls == [] and ev.has_key("k4") and len(list(ev.iter_events())) == 6


@pytest.mark.slow
def test_append_time_is_flat_in_a_big_month_file(ev):
    import time

    path = ev.vault.root / "events" / "2026" / "11" / "cli.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(raw(i) for i in range(75_000)))
    ev.has_key("warm")
    t = time.perf_counter()
    for i in range(200):
        grade(ev, key=f"k{i}", now=NOON + timedelta(seconds=i))
    per = (time.perf_counter() - t) / 200
    print(f"{per * 1000:.1f} ms per append at 75k events")
    assert per < 0.1  # was ~0.5 s here when each append re-parsed the month file


def test_parallel_readers_of_a_grown_file_list_each_event_once(ev):
    """Audit #1: threads that both see the file grew must not extend one cached parse twice."""
    for i in range(50):
        grade(ev, key=f"k{i}", now=NOON + timedelta(seconds=i))
    path = ev.vault.root / "events/2026/11/cli.jsonl"
    base = len(list(EventLog(ev.vault).iter_events()))
    for t in range(30):
        list(EventLog(ev.vault).iter_events())  # warm the shared parse cache
        last = json.loads(path.read_bytes().splitlines()[-1])
        with open(path, "ab") as f:  # another process's append: past this process's cache
            f.write((json.dumps(last | {"id": f"01ZZZZZZZZZZZZZZZZZZZZ{t:04d}"}) + "\n").encode())
        barrier, seen = threading.Barrier(6), []

        def read(barrier=barrier, seen=seen):
            barrier.wait(30)
            seen.append([e["id"] for e in EventLog(ev.vault).iter_events()])

        threads = [threading.Thread(target=read, daemon=True) for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60)
        assert not any(th.is_alive() for th in threads), f"trial {t}: a reader hung (deadlock?)"
        seen.append([e["id"] for e in EventLog(ev.vault).iter_events()])
        for ids in seen:
            assert len(ids) == len(set(ids)) == base + t + 1, f"trial {t}: {len(ids)} events"


# ---- the listing signature and the merged-view cache (A17) ---------------------------------

def test_an_unchanged_log_reuses_its_merged_view(ev, monkeypatch):
    first = grade(ev)
    got = list(ev.iter_events())
    view = events._VIEWS[str(ev._dir())]
    got[0]["kind"] = "edited by a caller"  # callers get copies
    monkeypatch.setattr(events, "_merged", lambda *a, **k: pytest.fail("merged again"))
    assert list(EventLog(ev.vault).iter_events()) == [first]  # one view per process
    assert events._VIEWS[str(ev._dir())] is view
    monkeypatch.undo()
    second = grade(ev, now=NOON + timedelta(minutes=1))
    assert list(ev.iter_events()) == [first, second] == fresh(ev)


_APPENDER = r"""
import os, sys
path, held = sys.argv[1], sys.argv[2] == "held"
f = open(path, "ab") if held else None
for line in sys.stdin.buffer:
    if held:
        f.write(line); f.flush()  # no fsync: Windows' folder listing keeps the old size
    else:
        with open(path, "ab") as g:  # as another Keepfold process appends
            g.write(line); g.flush(); os.fsync(g.fileno())
    sys.stdout.write("ok\n"); sys.stdout.flush()
"""


@pytest.mark.parametrize("held", [False, True])
def test_a_subprocess_append_is_seen(ev, held):
    """Another process's appends show on the next read: at once when it closes the file (as
    Keepfold does), and once RESTAT_S has passed while it holds the file open."""
    import subprocess
    import sys

    grade(ev)
    [path] = files(ev)
    child = subprocess.Popen([sys.executable, "-c", _APPENDER, str(path), "held" * held],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for i in range(1, 6):
            assert len(list(ev.iter_events())) == i and not ev.has_key(f"p{i}")
            child.stdin.write(raw(i, key=f"p{i}"))
            child.stdin.flush()
            assert child.stdout.readline().strip() == b"ok"
            if held:
                events._CHECKED.clear()  # as if RESTAT_S passed
            assert ev.has_key(f"p{i}") and len(list(ev.iter_events())) == i + 1
    finally:
        child.stdin.close()
        child.wait(30)
    assert list(ev.iter_events()) == fresh(ev)


# ---- perf ----------------------------------------------------------------------------------

def _sessions(ev, n_files, per_file):
    """`n_files` session files over 10 months, `per_file` events each, as other processes left
    them."""
    root = ev.vault.root / "events" / "2026"
    for f in range(n_files):
        d = root / f"{f % 10 + 1:02d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"s_{f:026d}.jsonl").write_bytes(
            b"".join(raw(f * per_file + i) for i in range(per_file)))


def _median_ms(fn, n):
    import statistics
    import time

    times = []
    for i in range(n):
        t = time.perf_counter()
        fn(i)
        times.append(time.perf_counter() - t)
    return statistics.median(times) * 1000


@pytest.mark.slow
@pytest.mark.perf
def test_append_with_5000_session_files(ev, capsys):
    _sessions(ev, 5000, 1)
    ev.has_key("warm")  # the first call parses every file
    ms = _median_ms(lambda i: grade(ev, key=f"k{i}", now=NOON + timedelta(seconds=i)), 21)
    with capsys.disabled():
        print(f"\nappend with 5000 session files: {ms:.1f} ms (median of 21)")
    assert len(list(ev.iter_events())) == 5000 + 21
    # brief: 20 ms; 120 here on a loaded PC (the vault lock ~25 ms of it), 719 before A17
    assert ms < 300


@pytest.mark.slow
@pytest.mark.perf
def test_iter_events_second_call_at_50k(ev, capsys):
    _sessions(ev, 500, 100)
    assert len(list(ev.iter_events())) == 50_000  # the first call parses
    ms = _median_ms(lambda i: list(ev.iter_events()), 11)
    with capsys.disabled():
        print(f"\niter_events second call at 50k events: {ms:.1f} ms (median of 11)")
    assert ms < 120  # brief: 50 ms; 51 here on a loaded PC, 189 before A17
