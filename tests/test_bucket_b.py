"""Bucket B: void, changed-list gaps, open event, text caps, answer_key normalisation, etc."""

import webbrowser
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import events, git, run
from typer.testing import CliRunner

from keepfold import session as session_mod
from keepfold.cli import app
from keepfold.events import EventLog, _check_fields
from keepfold.mcp_server import _norm_answer
from keepfold.planner import is_ready
from keepfold.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def root(root_committed):
    return root_committed


T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
runner = CliRunner()


def void(call, event_id, reason):
    return call("keepfold_log", {"kind": "void",
                               "payload": {"event_id": event_id, "reason": reason}})


ASK = {"concept": "two-pointers", "item_type": "recall", "question": "Q?"}


def test_void_tool_and_end_card(root):
    out = {}

    async def body(call):
        await call("keepfold_start", {"mode": "learn"})
        ask = await call("keepfold_ask", ASK)
        await call("keepfold_grade", {"item_id": ask["item_id"], "learner_answer": "a",
                                    "verdict": "wrong"})
        gid = next(e["id"] for e in events(root) if e["kind"] == "grade")
        v = await void(call, gid, "typo")
        assert v["voided"] == gid
        vid = next(e["id"] for e in events(root) if e["kind"] == "void")
        assert (await void(call, gid, "again"))[
            "error"] == "invalid_argument"
        assert (await void(call, vid, "x"))["error"]
        assert (await void(call, "nope", "x"))[
            "error"] == "unknown_id"
        out["card"] = (await call("keepfold_end"))["display"]
        out["gid"] = gid

    run(root, body)
    assert f"- voided grade {out['gid']} (typo)" in out["card"]
    (v,) = [e for e in events(root) if e["kind"] == "void"]
    assert v["ref"] == out["gid"] and v["payload"] == {"reason": "typo"}


def test_void_tool_opens_ambient_session(root):
    async def body(call):
        gid = EventLog(Vault(root)).append(
            session="cli", kind="grade", source="external",
            fields={"object": "concept:two-pointers", "verdict": "correct", "hints": 0})["id"]
        await void(call, gid, "mistaken")

    run(root, body)
    assert any(e["kind"] == "open" and e["mode"] == "ambient" for e in events(root))


def test_cli_void(root):
    gid = EventLog(Vault(root)).append(
        session="cli", kind="grade", source="external",
        fields={"object": "concept:two-pointers", "verdict": "correct", "hints": 0})["id"]
    r = runner.invoke(app, ["void", gid, "--reason", "oops", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["void", gid, "--reason", "oops", "--vault", str(root)])
    assert r.exit_code != 0 and "already voided" in r.output
    (v,) = [e for e in events(root) if e["kind"] == "void"]
    assert v["ref"] == gid and v["session"] == "cli"


def test_minted_mistake_in_changed_list(root):
    out = {}

    async def body(call):
        await call("keepfold_start", {"mode": "learn"})
        g = await call("keepfold_grade", {
            "concept": "two-pointers", "question": "Q?", "learner_answer": "a",
            "verdict": "wrong", "new_mistake": {"title": "Skips the middle", "why": "bounds"}})
        out["mid"] = g["mistake"]
        out["card"] = (await call("keepfold_end"))["display"]

    run(root, body)
    assert f"- added mistake {out['mid']}" in out["card"]


def test_event_budget_hint_once(root, monkeypatch):
    monkeypatch.setattr(session_mod, "EVENT_BUDGET", 2)

    async def body(call):
        await call("keepfold_start", {"mode": "learn"})
        nexts = [(await call("keepfold_ask", ASK))["next"] for _ in range(4)]
        assert ["events" in n for n in nexts] == [False, False, True, False]

    run(root, body)


def test_open_event_lets_sweep_close_and_commit_a_patch_only_session(root, monkeypatch):
    monkeypatch.setattr(session_mod, "_pid_alive", lambda pid: False)  # B2: its server is gone
    clock = SimpleNamespace(t=T0)
    mgr = session_mod.SessionManager(Vault(root), clock=lambda: clock.t)
    s = mgr.start("learn")
    (root / "problems/p.md").write_text("x\n", encoding="utf-8")  # a write outside events
    del mgr  # the process dies; no close
    clock.t = T0 + timedelta(hours=3)
    assert session_mod.sweep(Vault(root), lambda: clock.t) == [s.id]
    kinds = [e["kind"] for e in EventLog(Vault(root)).iter_events(s.id)]
    assert kinds == ["open", "close"]
    assert s.id in git(root, "log", "-1", "--format=%s")


def test_text_caps_2000():
    _check_fields({"question": "q" * 2000, "answer_key": "k" * 2000,
                   "learner_answer": "a" * 2000, "payload": {"text": "p" * 2000}})
    for f in ({"question": "q" * 2001}, {"payload": {"text": "p" * 2001}}):
        with pytest.raises(VaultError):
            _check_fields(f)


def test_dashboard_opens_the_app(root, monkeypatch):
    """`keepfold dashboard` opens the running app's home (its window, else the browser), or
    starts the app as `keepfold app` does."""
    from keepfold.app import server
    opened, shown, ran = [], [], []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(server, "running_url", lambda: "http://127.0.0.1:1/?t=k")
    monkeypatch.setattr(server, "show_window", lambda url: shown.append(url) or False)
    monkeypatch.setattr(server, "run", lambda root, echo: ran.append(root))
    r = runner.invoke(app, ["dashboard", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert shown == opened == ["http://127.0.0.1:1/?t=k"] and ran == []
    monkeypatch.setattr(server, "running_url", lambda: None)
    assert runner.invoke(app, ["dashboard", "--vault", str(root)]).exit_code == 0
    assert ran == [Vault(root).root] and len(opened) == 1
    assert not (root / ".keepfold" / "dashboard.html").exists()


def test_init_appends_workspace_ignore_once(root):
    for _ in range(2):
        r = runner.invoke(app, ["init", str(root)])
        assert r.exit_code == 0, r.output
    lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines == [".keepfold/*", "!.keepfold/format.json",  # A5: after .keepfold/*
                     ".obsidian/workspace*.json", "browse/private/",  # C3
                     "archive/chats/", ".*.tmp"]  # v9 C5, D1


def test_norm_answer():
    assert _norm_answer("`O(n log n)`") == _norm_answer("O(nlogn)") == _norm_answer(
        "o( N  LOG N )")
    assert _norm_answer(" $x^2$  y ") == "x^2 y"


def test_answer_key_normalised_match_only_upgrades(root):
    async def body(call):
        await call("keepfold_start", {"mode": "learn"})
        a = await call("keepfold_ask", {**ASK, "answer_key": "O(n log n)"})
        await call("keepfold_grade", {"item_id": a["item_id"], "learner_answer": "`O(N log N)`"})
        b = await call("keepfold_ask", {**ASK, "answer_key": "O(n)"})
        err = await call("keepfold_grade", {"item_id": b["item_id"], "learner_answer": "O(1)"})
        assert err["error"] == "invalid_argument"  # a mismatch cannot supply a verdict
        await call("keepfold_grade", {"item_id": b["item_id"], "learner_answer": "O(1)",
                                    "verdict": "partial"})

    run(root, body)
    g = [e for e in events(root) if e["kind"] == "grade"]
    assert [e["verdict"] for e in g] == ["correct", "partial"]
    assert "model_verdict" not in g[1]


def test_short_mode_offers_stop(root):
    async def body(call):
        out = await call("keepfold_start", {"mode": "short"})
        assert "Offer to stop after 5 items." in out["next"]

    run(root, body)


def test_is_ready_predicate():
    st = {"a": SimpleNamespace(state="solid"), "b": SimpleNamespace(state="learning")}
    c = lambda *r: SimpleNamespace(requires=list(r))  # noqa: E731
    assert is_ready("not_started", c("a"), st)
    assert is_ready("learning", c(), st)
    assert not is_ready("solid", c(), st)
    assert not is_ready("needs_refresh", c(), st)
    assert not is_ready("not_started", c("b"), st)
    assert not is_ready("not_started", c("missing"), st)
    # Solid waits for the 7-day gap check; two pass days already unlock dependents
    st |= {"two": SimpleNamespace(state="learning", pass_days=2),
           "one": SimpleNamespace(state="learning", pass_days=1)}
    assert is_ready("not_started", c("two", "a"), st)
    assert not is_ready("not_started", c("two", "one"), st)


@pytest.mark.parametrize("mode", ["capture", "reflect", "project"])
def test_new_modes_start_with_hint_not_plan_and_allow_writes(root, mode):
    async def body(call):
        out = await call("keepfold_start", {"mode": mode})
        assert out["mode"] == mode and out["today"] == [] and out["brief"] is not None
        assert "Today's plan" not in out["display"] and len(out["display"].splitlines()) == 2
        assert "error" not in await call("keepfold_log", {"kind": "journal",
                                                         "payload": {"text": "hi"}})

    run(root, body)


def test_journal_only_session_is_a_change(root):
    end = {}

    async def body(call):
        await call("keepfold_start", {"mode": "log"})
        for t in ("one", "two"):
            await call("keepfold_log", {"kind": "journal", "payload": {"text": t}})
        end.update(await call("keepfold_end"))

    run(root, body)
    assert "- journal line added ×2" in end["display"]
    (sess,) = (root / "sessions").glob("*.md")
    assert "- journal line added ×2" in sess.read_text(encoding="utf-8")
    assert "session log: 2 changes" in next((root / "journal").glob("*.md")).read_text(
        encoding="utf-8")


def test_log_next_gets_budget_hint_once(root, monkeypatch):
    monkeypatch.setattr(session_mod, "EVENT_BUDGET", 1)

    async def body(call):
        await call("keepfold_start", {"mode": "log"})
        pl = {"kind": "self_report", "payload": {"concept": "two-pointers", "verdict": "correct"}}
        nexts = [(await call("keepfold_log", pl))["next"] for _ in range(3)]
        assert ["events" in n for n in nexts] == [False, True, False]

    run(root, body)
