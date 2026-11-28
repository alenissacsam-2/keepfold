"""2.0 c (D2 L4): chat pages keep what tier 0 needs. A long turn keeps its end, a turn past the
page cap keeps its first-person sentences, a coding page has an "In my words" block the safety
net reads, never code, assistant text or tool results; a tag the hook saved is not proposed
again; an undone import stays undone."""

import json
import os
import time
from datetime import UTC, datetime

import pytest
from conftest import events
from test_chat_import import CWD, KEY, a, page, saved, u, write
from test_chat_import import dirs as dirs  # noqa: F401 (fixture)

from keepfold import chat_import, chats, prime_hook, review, safety_net
from keepfold.events import EventLog, local_day, void_event
from keepfold.vault import CHAT_ARCHIVE, Vault

pytestmark = pytest.mark.usefixtures("home")
FILL = "Here is the log of the failing run, it stops on the third case. "


def test_own_keeps_first_person_sentences_and_never_code():
    own = chat_import._own
    assert own("Fix the test.\nAlso I'm on Windows 11, not bash. Thanks") == \
        "Also I'm on Windows 11, not bash."
    assert own("i'm vegetarian, my bad") == "i'm vegetarian, my bad"
    assert own("I said it. I said it.") == "I said it."  # once
    for code in ("for i in range(n): x[i] = 1", "```\nI = my_list[0]\n```",
                 "    if I == my: return", "if (I) { my(); }", "`I am code`"):
        assert own(code) == "", code
    assert len(own("I " + "x" * 2000 + ".")) == chat_import.OWN_CHARS


def test_a_long_turn_keeps_its_start_and_its_end(root_committed, dirs):
    code, _ = dirs
    write(code / "p" / "long.jsonl", [u("Summarise: " + FILL * 40 + "Also, I'm vegetarian.")])
    chat_import.run(Vault(root_committed))
    body = page(root_committed, "claude-code", "p/long")
    (turn,) = chat_import._ME.findall(body)
    assert turn.startswith(" Summarise:") and turn.endswith("Also, I'm vegetarian.")
    assert " … " in turn and len(turn) <= chats.LINE_CHARS + 1


def test_past_the_page_cap_first_person_turns_are_kept(root_committed, dirs):
    code, _ = dirs
    lines = [u(f"Round {i}: " + "x" * 1400) for i in range(9)]  # 9 x ~1,410 > PART_CHARS
    lines += [a("ASSISTANT-SAYS I'm sure"), u("Round 9: " + "y" * 1400),
              u("Unrelated: I'm allergic to peanuts.\n```\nI = 'CODE-IN-TURN'\n```"),
              u(f"my key is {KEY} and I keep it")]
    write(code / "p" / "cap.jsonl", lines)
    chat_import.run(Vault(root_committed))
    body = page(root_committed, "claude-code", "p/cap")
    assert "**Me:** Unrelated: I'm allergic to peanuts." in body
    assert "The first 8 of my 12 turns, then what I said about myself in 2 later ones." in body
    for gone in ("y" * 50, "ASSISTANT", "CODE-IN-TURN", KEY):
        assert gone not in body, gone
    assert "[redacted]" in body


def test_late_turns_stop_at_the_extra_budget():
    c = chat_import.Chat()
    for i in range(9):
        c.turn("", f"Round {i}: " + "x" * 1400)
    for i in range(40):
        c.turn("", f"I said thing number {i} about my week, " + "and more words " * 20)
    assert c.full and 0 < c.late < 40
    assert sum(len(x) for _, x in c.kept) <= chats.PART_CHARS + chat_import.LATE


def test_a_coding_page_has_my_words_and_the_safety_net_reads_them(root_committed, dirs):
    code, _ = dirs
    tools = [("Write", {"file_path": f"{CWD}\\main.py", "content": "SECRET-CODE = 1"})]
    write(code / "D--Dev-proj" / "code.jsonl", [
        u("Set up the repo.\nbtw I always use uv, never pip.\n```\nI = 'FENCED-CODE'\n```"),
        a("ASSISTANT-PLAN I will use uv", tools=tools),
        {"type": "user", "timestamp": "2026-11-10T09:01:00Z", "cwd": CWD,
         "toolUseResult": {"stdout": "x"},
         "message": {"role": "user", "content": "TOOL-RESULT I ran it"}},
        u(f"Fix the login test.\nI'm on Windows 11 with PowerShell. My key is {KEY}")])
    assert chat_import.run(Vault(root_committed)) == {"saved": 1}
    body = page(root_committed, "claude-code", "D--Dev-proj/code")
    assert "## In my words\n\n**Me:** btw I always use uv, never pip.\n\n**Me:** I'm on Windows " \
           "11 with PowerShell. My key is [redacted]\n" in body
    for gone in ("SECRET-CODE", "FENCED-CODE", "ASSISTANT", "TOOL-RESULT", KEY):
        assert gone not in body, gone
    assert "2 requests" in body
    note = Vault(root_committed).read("resource", body.split('id: "', 1)[1].split('"', 1)[0])
    got = safety_net.chat_of(note)
    assert got is not None and got.turns == [
        "btw I always use uv, never pip.", "I'm on Windows 11 with PowerShell. My key is "
        "[redacted]"]


def test_a_coding_session_without_first_person_has_no_block(root_committed, dirs):
    code, _ = dirs
    write(code / "p" / "c.jsonl", [
        u("Rename the module."), a(tools=[("Edit", {"file_path": f"{CWD}\\m.py"})])])
    chat_import.run(Vault(root_committed))
    body = page(root_committed, "claude-code", "p/c")
    assert "In my words" not in body and "**Me:**" not in body


def test_my_words_count_for_the_category_check(root_committed, dirs):
    code, _ = dirs
    write(code / "p" / "h.jsonl", [
        u("Fix the build.\nI wonder what do these symptoms mean."),
        a(tools=[("Edit", {"file_path": f"{CWD}\\m.py"})])])
    chat_import.run(Vault(root_committed))
    assert "private: true" in page(root_committed, "claude-code", "p/h")


def test_a_tag_the_hook_saved_is_not_proposed_again(root, dirs, monkeypatch):
    code, _ = dirs
    monkeypatch.setenv("KEEPFOLD_VAULT", str(root))
    prompt = "//pref: Short answers\nhi"
    assert "Keepfold saved" in prime_hook.run(json.dumps({
        "session_id": "c1", "prompt": prompt, "hook_event_name": "UserPromptSubmit"}))
    write(code / "p" / "c1.jsonl", [u(prompt, ts=datetime.now(UTC).isoformat())])
    assert chat_import.run(Vault(root)).get("saved") == 1
    me = (root / "me" / "preferences.md").read_text(encoding="utf-8")
    assert me.count("- Short answers") == 1
    v = Vault(root)
    pending = review.view(v, EventLog(v).iter_events(), local_day(datetime.now(UTC)))["pending"]
    assert not any("Short answers" in str(x.get("text")) for x in pending)


def test_an_undone_import_is_not_imported_again(root_committed, dirs):
    code, _ = dirs
    p = write(code / "p" / "u.jsonl", [u("I live in Kochi.")])
    v = Vault(root_committed)
    assert chat_import.run(v) == {"saved": 1}
    (ev,) = [e for e in events(root_committed)
             if (e.get("payload") or {}).get("kind") == "chat_saved"]
    void_event(EventLog(v), session="cli", event_id=ev["id"], reason="undo", source="self")
    pid = ev["payload"]["pages"][0]
    (root_committed / CHAT_ARCHIVE / f"{pid}.md").unlink()
    with p.open("a", encoding="utf-8") as f:  # the chat goes on, so it is read again
        f.write(json.dumps(u("And I moved to Bangalore.")) + "\n")
    t = time.time() - 600
    os.utime(p, (t, t))
    assert chat_import.run(v) == {"undone": 1}
    assert not (root_committed / CHAT_ARCHIVE / f"{pid}.md").exists()
    assert len(saved(root_committed)) == 1
