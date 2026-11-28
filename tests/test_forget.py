"""R10: a forget takes the text out of every store Keepfold owns, and its receipt names the rest."""

import json
import time
from datetime import UTC, datetime

import pytest
from conftest import FakeEmbed, git, note
from test_browse import chromium, hubbed, m
from test_browse_sites import OWNER
from test_chat_log import serve
from typer.testing import CliRunner

from keepfold import chat_import, claims, clients, forget, levels, prime_hook, semantic, tags
from keepfold.app import server, targets
from keepfold.browse import build, sources
from keepfold.catalog import Catalog
from keepfold.cli import app
from keepfold.events import EventLog, void_event
from keepfold.index import Index
from keepfold.vault import Vault, VaultError

pytestmark = pytest.mark.usefixtures("home")
CANARY = "zqcanary9x"


def test_forget_yes_skips_prompt_and_prints_receipt(root):
    page = root / "problems" / "lc-1.md"
    assert page.exists()

    result = CliRunner().invoke(app, ["forget", "lc-1", "--vault", str(root), "--yes"], input="")

    assert result.exit_code == 0, result.output
    assert "Forget Two Sum everywhere" not in result.output
    assert "removed the page" in result.output
    assert not page.exists()


def hits(root, home):
    """Every file under the vault (but .git) and the temp home (.keepfold, the AI apps' rules
    files and their backups) holding the canary."""
    out = []
    for base in (root, home):
        out += [p.relative_to(base.parent).as_posix() for p in base.rglob("*") if p.is_file()
                and ".git" not in p.relative_to(base).parts and CANARY.encode() in p.read_bytes()]
    return sorted(out)


def cli(*args, ok=True):
    r = CliRunner().invoke(app, [str(a) for a in args], input="y\n")
    assert (r.exit_code == 0) == ok, r.output
    return r.output


def test_canary_leaves_every_store(root, home, tmp_path, monkeypatch):
    monkeypatch.setenv("KEEPFOLD_CHAT_EXPORT_DIR", str(home / ".keepfold" / "claude-chat"))
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    hubbed(root)
    note(root, "me/core.md", "id: core\n", "# Core\n\n## Core\n- Call me Sam.\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "core")
    got = {}

    async def body(call):  # a chat, a page, claim lines, all through the normal paths
        p = await call("keepfold_prime", {"prompt": f"remember my word {CANARY} please"})
        got["add"] = await call("keepfold_add", {"kind": "note", "title": f"Note {CANARY}",
                                               "fields": {"summary": f"Note {CANARY}"}})
        await call("keepfold_patch", {"id": got["add"]["id"], "op": "append_to_section",
                                    "target": "Notes", "text": f"- Note {CANARY} car"})
        got["claim"] = await call("keepfold_patch", {
            "id": "me/profile", "op": "append_to_section", "target": "Brief",
            "user_confirmed": True, "text": f"- My lucky word is {CANARY}"})
        await call("keepfold_patch", {  # the pinned core, served into AI apps' rules files once
            "id": "me/core", "op": "append_to_section", "target": "Core",  # the owner approves
            "user_confirmed": True, "text": f"- Greet me with {CANARY}"})
        await call("keepfold_log", {"kind": "chat_turn", "payload": {
            "chat": p["chat"], "user": f"My lucky word is {CANARY}", "gist": "noted"}})
        await call("keepfold_end", {"summary": f"added Note {CANARY}"})

    serve(root, body)
    (home / ".codex").mkdir()  # Codex, Cursor and VS Code rules files, all under the temp home
    (home / ".cursor").mkdir()
    targets._vscode_user().mkdir(parents=True)
    rules = [targets.by_id()[t] for t in ("codex", "cursor", "vscode")]
    for t in rules:
        t.apply(clients.server_entry("keepfold", root))
    rules = [t.rule_path() for t in rules]
    a = server.App(root)
    monkeypatch.setattr(a, "entry", lambda: clients.server_entry("keepfold", root))
    a.review_approve({"key": next(x for x in a.review()["pending"] if x["page"] == "core")["key"]})
    assert all(str(p).startswith(str(home)) and CANARY in p.read_text("utf-8") for p in rules)
    local = tmp_path / "local"  # a browse page
    chromium(local / "Google" / "Chrome" / "User Data" / "Default" / "History", [
        (m(0), f"https://lichess.org/study/{CANARY}", f"Chess {CANARY} openings", 300)])
    build.run(Vault(root), sources.discover(local, tmp_path / "roaming"), OWNER, tz=UTC,
              now=m(0, 2), embed=FakeEmbed())
    assert chat_import.run(Vault(root), now=time.time() + 10**5)["saved"] == 1
    v = Vault(root)
    idx = Index(v, Catalog.load(v))
    idx.save_snapshot()
    assert semantic.vectors(idx, FakeEmbed(), 4) is not None
    before = hits(root, home)
    assert {"vault/.keepfold/vectors.db", "vault/.keepfold/index-snapshot.json",
            "vault/.keepfold/chat-import.sqlite", "home/.keepfold/claude-chat"} & {
        h if h.startswith("vault/") else h.rsplit("/", 2)[0] for h in before}
    chat = next(e["payload"]["pages"][0] for e in EventLog(v).iter_events()
                if (e.get("payload") or {}).get("kind") == "chat_saved")
    cid = {r["page"]: r["id"] for r in claims.rows(v) if CANARY in r["text"]}
    git(root, "add", "-A")  # what the imports above left uncommitted
    git(root, "commit", "-qm", "imports")

    out = cli("forget", f"me/profile^{cid['profile']}", "--vault", root)
    assert out.startswith("Forget the line ") and "removed 1 claim lines" in out
    assert "1 chat transcripts swept" in out and "filter-repo" in out, out
    assert "Backups" in out and "raw/" in out and "AI apps keep" in out
    out = cli("forget", f"me/core^{cid['core']}", "--vault", root)
    assert "3 AI apps' rules files rewritten" in out, out
    assert all(f"rewritten: {p}" in out for p in rules) and "rules files, or the" not in out
    assert not any(CANARY in p.read_text("utf-8") for p in rules)
    out = cli("forget", chat, "--vault", root)
    assert "chat transcripts swept or deleted" in out, out
    out = cli("forget", got["add"]["id"], "--vault", root)
    assert "removed the page; 3 events redacted" in out, out  # 2 changes, the close summary
    out = cli("browse", "forget", "--site", "lichess.org", "--vault", root)
    assert "forgot lichess.org:" in out and "deny_domains" in out
    assert hits(root, home) == [], [(h, [ln for ln in (root.parent / h).read_text(
        "utf-8", "replace").splitlines() if CANARY in ln][:3]) for h in hits(root, home)]
    assert git(root, "status", "--porcelain") == ""  # each forget is committed
    v = Vault(root)
    assert all(e.get("id") and e.get("ts") and e.get("kind") for e in EventLog(v).iter_events())
    assert Index(v, Catalog.load(v)).find(CANARY) == []
    assert chat_import.run(Vault(root), now=time.time() + 10**5).get("saved", 0) == 0
    assert hits(root, home) == []  # the chat is not imported again


def _session(root, when, text, verdict="correct"):
    from keepfold.session import SessionManager

    mgr = SessionManager(Vault(root), clock=lambda: when)
    s = mgr.start()
    ev = mgr.log.append(session=s.id, kind="grade", source="tutor", mode="learn", now=when,
                        fields={"object": "concept:two-pointers", "verdict": verdict,
                                "hints": 0, "confidence": 3,
                                "payload": {"answer": f"{text}, said twice: {text}"}})
    mgr.note("graded", "two-pointers")
    assert mgr.close()["commit"]
    return s.id, ev["id"]


def _levels(root):
    return levels.replay(Vault(root), today="2026-11-20")[0].get("two-pointers")


def test_redacted_events_still_fold_void_and_undo(root):
    git(root, "config", "core.autocrlf", "false")
    p = root / "me" / "profile.md"
    p.write_text(p.read_text(encoding="utf-8") + f"- My lucky word is {CANARY} ^c-aaaaa\n",
                 encoding="utf-8")
    git(root, "commit", "-qam", "a claim")
    first, ev1 = _session(root, datetime(2026, 11, 10, 12, tzinfo=UTC),
                          f"My lucky word is {CANARY}")
    second, ev2 = _session(root, datetime(2026, 11, 11, 12, tzinfo=UTC), "two pointers meet")
    v = Vault(root)
    lv, before = _levels(root), list(EventLog(v).iter_events())
    out = cli("forget", "me/profile^c-aaaaa", "--vault", root)
    assert "1 events redacted" in out
    after = list(EventLog(Vault(root)).iter_events())
    assert CANARY not in json.dumps(after) and _levels(root) == lv
    assert [{k: e[k] for k in sorted(e) if k != "payload"} for e in after] == [
        {k: e[k] for k in sorted(e) if k != "payload"} for e in before]
    graded = next(e for e in after if e["id"] == ev1)
    assert graded["payload"] == {"answer": "[forgotten], said twice: [forgotten]"}
    assert git(root, "status", "--porcelain") == ""

    out = cli("undo", first, "--vault", root)  # its redacted event file goes with it
    assert "reverted" in out and "events" not in out
    assert not list(root.glob(f"events/**/{first}.jsonl"))
    v = Vault(root)
    assert {e["session"] for e in EventLog(v).iter_events() if e["kind"] == "grade"} == {second}
    assert _levels(root).attempts == lv.attempts - 1
    void_event(EventLog(v), session="cli", event_id=ev2, reason="test", source="external")
    assert _levels(root) is None  # no live grade left


def test_app_forget_card_forgets_and_returns_the_receipt(root):
    from conftest import run

    from keepfold.app import server

    (root / ".keepfold").mkdir(exist_ok=True)
    (root / "me" / "preferences.md").write_text(
        "# Preferences\n\n## Coding\n- Writes Python with type hints\n"
        "- Writes Rust for small CLI tools\n", encoding="utf-8")

    async def body(call):  # matches both lines: a Review card
        await call("keepfold_prime", {"prompt": "hello"})
        out = await call("keepfold_log", {"kind": "chat_turn", "payload": {
            "user": "//forget: writes python rust", "gist": "x"}})
        assert "nothing changed" in out["tags"][0]

    run(root, body)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "the card")
    app = server.App(root)
    (card,) = app.review()["forget"]
    pick = next(c for c in card["candidates"] if c["text"].startswith("Writes Python"))
    view = app.review_retire({"id": pick["id"]})
    assert view["forget"] == [] and "events redacted" in " ".join(forget.lines(view["receipt"]))
    assert view["receipt_lines"] == forget.lines(view["receipt"])  # what the Review board shows
    assert not [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts
                and b"writes python" in p.read_bytes().lower()]
    assert "Writes Rust" in (root / "me" / "preferences.md").read_text(encoding="utf-8")
    assert git(root, "status", "--porcelain") == ""


def _texts_in(root, *texts):
    """The files under the vault (but .git) and the chat exports holding one of `texts`."""
    from keepfold import chats

    return sorted(p.name for base in (root, chats.export_root()) for p in base.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(base).parts
                  and any(t.lower().encode() in p.read_bytes().lower() for t in texts))


def _said(root):
    """The events and the live chat logs (a chat_turn writes no event), as one text."""
    from keepfold import chats

    return json.dumps(list(EventLog(Vault(root)).iter_events())) + "".join(
        p.read_text("utf-8") for p in chats.export_root().rglob("*.jsonl"))


def test_a_common_title_sweeps_only_the_page_and_its_id(root, monkeypatch):
    run = serve  # logs chats, as an owner app does

    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    got = {}

    async def body(call):
        got["add"] = await call("keepfold_add", {"kind": "note", "title": "Python",
                                               "fields": {"summary": "Snakes and scripts"}})
        await call("keepfold_log", {"kind": "chat_turn", "payload": {
            "user": "I write Python every day", "gist": "noted"}})
        await call("keepfold_log", {"kind": "taught", "payload": {
            "concept": "two-pointers", "summary": "Python slices meet in the middle"}})

    run(root, body)
    pid, v = got["add"]["id"], Vault(root)
    idx = Index(v, Catalog.load(v))
    idx.save_snapshot()
    semantic.vectors(idx, FakeEmbed(), 4)
    assert pid in semantic._read(root)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "vectors")

    out = cli("forget", pid, "--vault", root)
    assert 'Too short or common to sweep safely: "Python"' in out, out
    assert not list(root.glob(f"**/{pid}.md")) and pid not in semantic._read(root)
    named = [e["payload"]["change"] for e in EventLog(Vault(root)).iter_events()
             if e["kind"] == "change"]
    assert named and all(c["id"] == forget.MARK for c in named)  # the events naming its id
    said = _said(root)
    assert "I write Python every day" in said and "Python slices meet in the middle" in said


def test_a_forget_tag_of_one_word_takes_only_its_line(root):
    run = serve  # logs chats, as an owner app does

    (root / "me" / "preferences.md").write_text(  # "python" holds all its content words
        "# Preferences\n\n## Coding\n- Python is it for me\n"
        "- Uses tabs for indentation\n", encoding="utf-8")
    # D-15: a said path (the prime hook) retires at once; over MCP a forget only proposes
    from keepfold import review
    from keepfold.events import local_day
    day = local_day(datetime.now(UTC))
    assert tags.apply_in(Vault(root), [("forget", "python", day)], said=tags.SAID["hook"]) == [
        "//forget retired a line on me/preferences; the Keepfold app removes it from "
        "everywhere Keepfold keeps it within 10 minutes"]

    async def body(call):
        await call("keepfold_log", {"kind": "chat_turn", "payload": {
            "user": "I write Python every day", "gist": "x"}})
        out = await call("keepfold_prime", {"prompt": "//forget: python"})  # the same turn
        assert "tags" not in out  # the hook handled it
        out = await call("keepfold_prime", {"prompt": "//forget: tabs for indentation"})
        assert "nothing changed" in out["tags"][0]

    run(root, body)
    assert forget.run_due(Vault(root))  # the app's periodic pass
    assert _texts_in(root, "python is it for me") == []
    said = _said(root)
    assert "I write Python every day" in said and "//forget: python" in said  # nothing else
    assert "- Uses tabs for indentation" in (root / "me" / "preferences.md").read_text("utf-8")
    v = Vault(root)
    (card,) = review.view(v, EventLog(v).iter_events(), day)["forget"]
    assert card["text"] == "tabs for indentation"


TABS = ("# Preferences\n\n## Coding\n- Uses tabs for indentation\n"
        "- Writes Python with type hints\n")


def _payloads(root, kind):
    return [e for e in EventLog(Vault(root)).iter_events() if e.get("kind") == "log"
            and isinstance(e.get("payload"), dict) and e["payload"].get("kind") == kind]


def test_a_forget_tag_in_a_prompt_hook_only_retires_and_queues(root, monkeypatch):
    """A hook times out at 30 s and a cold sweep can take a minute: the hook retires the line
    and queues the sweep; the app's periodic pass (run_due) sweeps it, once."""
    monkeypatch.setenv("KEEPFOLD_VAULT", str(root))
    (root / "me" / "preferences.md").write_text(TABS, encoding="utf-8")
    (root / "log.md").write_text("# Log\n\n- 2026-11-20 uses tabs for indentation\n",
                                 encoding="utf-8")

    def hook(prompt):
        return prime_hook.run(json.dumps({"session_id": "chat-1", "prompt": prompt,
                                          "hook_event_name": "UserPromptSubmit"}))

    hook("hello there")  # primes the chat
    t = time.monotonic()
    out = hook("//forget: tabs for indentation")
    assert time.monotonic() - t < 30  # Claude Code, Codex and Gemini kill a hook at 30 s
    assert "//forget retired a line on me/preferences; the Keepfold app removes it" in out
    assert "tabs for indentation" in (root / "me/preferences.md").read_text(
        "utf-8").split("## Past")[1]  # retired at once: prime no longer serves it
    assert "tabs for indentation" in (root / "log.md").read_text("utf-8")  # swept nothing
    v = Vault(root)
    (due,) = forget.due(EventLog(v).iter_events())
    assert due["payload"]["text"] == "tabs for indentation"

    (root / ".keepfold").mkdir(exist_ok=True)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "the fixture", "--allow-empty")
    (receipt,) = server.App(root).forget_due()  # the app's periodic pass
    assert receipt["removed"]["lines"] == 1 and git(root, "status", "--porcelain") == ""
    assert _texts_in(root, "tabs for indentation") == []
    (swept,) = _payloads(root, "forget_due")
    assert swept["payload"]["text"] == forget.MARK
    (done,) = _payloads(root, "forget_done")
    assert done["payload"] == {"kind": "forget_done", "ref": due["id"],
                               "removed": receipt["removed"]}
    n = len(list(EventLog(v).iter_events()))
    assert forget.run_due(v) == [] and len(list(EventLog(v).iter_events())) == n  # once


def test_a_due_forget_whose_line_went_meanwhile_is_marked_done(root):
    (root / "me" / "preferences.md").write_text(TABS, encoding="utf-8")
    v = Vault(root)
    (line,) = tags.apply_in(v, [("forget", "tabs for indentation", "2026-11-23")])
    assert line.startswith("//forget retired a line")
    (due,) = forget.due(EventLog(v).iter_events())
    forget.forget(v, f"me/preferences^{due['payload']['claim']}")  # the CLI, meanwhile
    assert forget.run_due(v) == []
    assert forget.due(EventLog(v).iter_events()) == []
    assert _payloads(root, "forget_due")[0]["payload"]["text"] == forget.MARK


def test_a_person_forget_redacts_every_mention_of_the_name(root):
    run = serve  # logs chats, as an owner app does

    from keepfold import people

    people.add(Vault(root), "Asha", {"relation": "friend"})

    async def body(call):
        p = await call("keepfold_prime", {"prompt": "Asha and I went hiking"})
        await call("keepfold_log", {"kind": "chat_turn", "payload": {
            "chat": p["chat"], "user": "Asha said hi to [[asha]]", "gist": "noted"}})

    run(root, body)
    assert _texts_in(root, "asha")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "people")
    out = cli("forget", "people/asha", "--vault", root)
    assert ('Every mention of "Asha" was redacted, including other people with that name.'
            in out), out
    assert _texts_in(root, "asha") == []


def test_a_forgotten_page_from_a_watched_folder_stays_forgotten(root, tmp_path):
    from keepfold import watch

    d = tmp_path / "Docs"
    d.mkdir()
    (d / "Big data pipelines.md").write_bytes(b"# Big data pipelines\n\nKafka and Spark.\n")
    v = Vault(root)
    watch.add(v, d, "docs", "normal")
    assert watch.run(v)[0]["read"] == 1
    (page,) = [p for p in (root / "resources").glob("docs-*.md")]
    r = forget.forget(v, page.stem)
    assert any(x.startswith("Watched folders") for x in r["left"]) and not page.exists()
    assert watch.run(v)[0]["read"] == 0 and not page.exists()  # its file is unchanged


def test_drop_takes_the_line_and_its_past(tmp_path):
    text = ("# Profile\n\n## Brief\n- Lives in Pune ^c-bbbbb\n- Keeps this ^c-ccccc\n\n## Past\n"
            "- Lives in Delhi (until 2026-01-02 → ^c-bbbbb) ^c-ddddd\n"
            "- Lives in Agra (until 2025-01-02 → ^c-ddddd) ^c-eeeee\n")
    new, gone = claims.drop(text, "profile", "c-bbbbb")
    assert new == "# Profile\n\n## Brief\n- Keeps this ^c-ccccc\n\n## Past\n"
    assert sorted(r["id"] for r in gone) == ["c-bbbbb", "c-ddddd", "c-eeeee"]
    assert gone[0]["line"] == "- Lives in Pune ^c-bbbbb"


def test_refuses_what_it_cannot_forget(root):
    v = Vault(root)
    for bad, why in (("concepts/two-pointers", "catalog.yaml"), ("me/profile", "^c-"),
                     ("nothing-here", "No page"), ("me/profile^c-zzzzz", "No claim line")):
        with pytest.raises(VaultError, match=why.replace("^", r"\^")):
            forget.forget(v, bad)
