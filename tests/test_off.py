"""A24: `//off` at the start of a line turns memory off for one chat. The prime hook prints one
line and marks the session; keepfold_prime serves nothing and refuses every later call; the chat
import, the safety net and the fact harvest skip that chat. Synthetic vault_mini, temp home."""

import json
from contextlib import closing

import pytest
from conftest import events, note
from test_chat_import import a, dirs, saved, u, write  # noqa: F401 (dirs: a fixture)
from test_safety_net import NOW, chat

from keepfold import chat_import, prime_hook, tags
from keepfold import fact_harvest as fh
from keepfold import safety_net as sn
from keepfold.events import EventLog
from keepfold.mcp_server import Keepfold
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

SID = "0b9e7a52-6c1d-4f3e-9a2b-7d4c1e8f5a60"


def hook(prompt, cwd, event="UserPromptSubmit", sid=SID, **kw):
    return prime_hook.run(json.dumps({"session_id": sid, "cwd": str(cwd),
                                      "hook_event_name": event, "prompt": prompt, **kw}))


def context(out):
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_only_a_line_start_off_outside_code_counts():
    assert tags.find("//off") == [("off", "")]
    assert tags.find("hello\n  //OFF please\n//pref: tables") == [("off", ""),
                                                                    ("pref", "tables")]
    for text in ("please //off now", "type `//off`", "```\n//off\n```", "//offline docs",
                 "~//off", "<private>\n//off\n</private>"):
        assert not tags.off(text), text
    assert tags.apply_in(None, [("off", "", "2026-11-28")]) == []  # never a write


def test_hook_off_serves_nothing_and_marks_the_session(root, tmp_path, monkeypatch):
    monkeypatch.setenv("KEEPFOLD_VAULT", str(root))
    before = len(events(root))
    assert context(hook("//off\nhelp me plan my week //pref: tables", tmp_path)) \
        == prime_hook.OFF_LINE
    assert (root / prime_hook.OFF / SID).read_text(encoding="utf-8") == ""
    assert not (root / prime_hook.STATE).exists()  # never primed
    for later in ("What do I know about heaps?", "//fact: I live in Pune"):
        assert context(hook(later, tmp_path)) == prime_hook.OFF_LINE
    assert context(hook("", tmp_path, "SessionStart", source="compact")) == prime_hook.OFF_LINE
    assert hook("", tmp_path, "PreCompact") == prime_hook.OFF_LINE
    assert len(events(root)) == before  # no event, no tag saved
    assert not (root / prime_hook.SERVED).exists()
    # another chat, and //off in the middle of a line, prime as before
    other = context(hook("help me plan my week, not //off", tmp_path, sid="chat-2"))
    assert other.startswith(prime_hook.HEAD)
    assert not (root / prime_hook.OFF / "chat-2").exists()


def test_off_chat_is_never_imported_or_harvested(root, dirs, tmp_path):  # noqa: F811
    code, _ = dirs
    write(code / "p" / "typed.jsonl", [u("How do heaps work?"), a("answer"),
                                        u("wait\n//off\nI moved to Pune last month")])
    write(code / "p" / "marked.jsonl", [u("I moved to Pune last month")])
    write(code / "p" / "in-code.jsonl", [u("what does this do?\n```\n//off\n```")])
    (root / prime_hook.OFF).mkdir(parents=True)
    (root / prime_hook.OFF / "marked").write_text("", encoding="utf-8")
    v = Vault(root)
    out = chat_import.run(v)
    assert out["off_chat"] == 2 and out["saved"] == 1
    assert [p["chat_id"] for p in saved(root)] == ["p/in-code"]
    with closing(chat_import.State(v)) as st:  # STATE keeps no text of an off chat
        rec = st.get(chat_import._key("claude-code", code / "p" / "typed.jsonl"))
    assert rec["chat"]["off"] and "Pune" not in json.dumps(rec) and "heaps" not in json.dumps(rec)
    # a page from before the chat went off: by its line, or by its session marked since
    chat(root, "c-typed", "I moved to Pune last month", "fine\n//off")
    chat(root, "c-marked", "I moved to Pune last month")
    chat(root, "c-on", "I moved to Pune last month")
    EventLog(v).append(session="import", kind="log", source="imported", fields={"payload": {
        "kind": "chat_saved", "source": "claude-code", "chat_id": "p/marked", "title": "t",
        "pages": ["c-marked"], "auto": True}})
    got = sn.run(Vault(root), now=NOW)
    assert got["skipped_off"] == 2 and got["chats"] == 1
    harvested = fh.run(Vault(root), None, now=NOW)
    assert harvested["skipped_off"] == 2


def test_mcp_off_serves_nothing(root):
    core = Keepfold(Vault(root))
    before = len(events(root))
    out = core.keepfold_prime("//off\nwhat should I revise today?")
    assert out == {"context": "", "next": prime_hook.OFF_LINE}
    assert core.excluded
    for name, kw in (("keepfold_prime", {"prompt": "what should I revise?"}),
                     ("keepfold_log", {"kind": "touch", "payload": {"ids": ["lc-1"]}})):
        assert core._call(name, getattr(core, name), kw)["error"] == "role_refused"
    assert len(events(root)) == before
    assert not (root / ".keepfold/xray.jsonl").exists()
    # a model's //off in the middle of a line is no //off
    fresh = Keepfold(Vault(root))
    assert fresh.keepfold_prime("I said //off yesterday")["context"]
    assert not fresh.excluded


def test_a_page_with_a_quoted_off_is_still_harvested(root):
    note(root, "resources/c-code.md", 'title: "c"\nkind: resource\n',
         "# c\n\nTimeline: · Source: Claude · Date: 2026-11-28 · My turns only\n\n"
         "**Me:** see `//off` and\n```\n//off\n```\n")
    n = Vault(root).read("resource", "c-code")
    assert sn.skip_reason(n, sn.chat_of(n), chat_import.Ctx(Vault(root)), None, NOW) != "off"
