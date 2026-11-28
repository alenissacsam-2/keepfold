"""L1: a `<private>…</private>` span is never saved (chats.drop_private, wherever Keepfold keeps
chat text a person typed), and the relative dates of a durable statement are pinned to the
day they meant when it was written (dates.pin)."""

import datetime as dt
import re
from datetime import date

import anyio
import pytest
from conftest import run
from mcp import Client
from mcp.types import Implementation

from keepfold import chat, chats, dates, fact_harvest, recall_log, session, tags, voice
from keepfold.app import server
from keepfold.mcp_server import build_server
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

SECRET = "Ravi 98450 11223"  # what the person marked private
NOW = dt.datetime(2026, 11, 23, 12, 0).astimezone()  # a Monday, local noon


# ---- drop_private -----------------------------------------------------------------------------

@pytest.mark.parametrize("text, want", [
    ("my landlord <private>(name, number)</private> said", "my landlord [private] said"),
    ("a <PRIVATE>x</Private> b <private>y</private> c", "a [private] b [private] c"),  # case, twice
    ("a <private>line one\nline two</private> b", "a [private] b"),  # across lines
    ("a <private>x <private>y</private> z</private> b", "a [private] b"),  # nested
    ("a <private>x and all the rest\nof it", "a [private]"),  # unclosed: the safe reading
    ("a </private> b", "a </private> b"),  # a stray close tag opens nothing
    ("a < private >x</ private > b", "a [private] b"),
    ("nothing to hide", "nothing to hide"),
])
def test_drop_private(text, want):
    assert chats.drop_private(text) == want


def test_redact_drops_private_spans_before_secrets():
    assert chats.redact("key <private>sk-" + "a" * 24 + "</private> ok") == "key [private] ok"


# ---- every write point ------------------------------------------------------------------------

def _serve(root, body, client="claude-ai"):
    """`body(call)` against a fresh owner server whose client is a Claude app (a live log)."""
    server = build_server(Vault(root), "owner")

    async def main():
        async with Client(server, client_info=Implementation(name=client, version="1")) as c:
            async def call(name, args=None):
                r = await c.call_tool(name, args or {})
                assert not r.is_error, r.content
                return r.structured_content

            await body(call)

    anyio.run(main)


def test_the_chat_log_and_tags_never_keep_a_private_span(root):
    got = {}

    async def body(call):
        p = await call("keepfold_prime", {"prompt": (
            f"My landlord <private>{SECRET}</private> called.\n//fact: I rent a flat in Kochi\n"
            f"<private>//fact: I owe {SECRET}</private>")})
        got["chat"] = p["chat"]
        await call("keepfold_log", {"kind": "chat_turn", "payload": {
            "chat": p["chat"], "user": f"//fact: I moved to Pune yesterday\nand <private>{SECRET}",
            "gist": f"About <private>{SECRET}</private>."}})

    _serve(root, body)
    log = (chats.export_root() / chats.LIVE / f"{got['chat']}.jsonl").read_text("utf-8")
    assert SECRET not in log and "[private]" in log and "Kochi" in log
    # D-15: over MCP the tags only propose; the proposal keeps no private span either
    inbox = (root / "inbox.md").read_text(encoding="utf-8")
    assert "I rent a flat in Kochi" in inbox and "I owe" not in inbox
    assert SECRET not in inbox
    assert re.search(r"I moved to Pune yesterday \(\d{4}-\d\d-\d\d\)", inbox)  # day pinned
    profile = (root / "me/profile.md").read_text(encoding="utf-8")
    assert SECRET not in profile and "Kochi" not in profile and "Pune" not in profile


@pytest.fixture
def monday(monkeypatch):
    monkeypatch.setattr(session.SessionManager, "_now", lambda self: NOW.astimezone(dt.UTC))


def test_add_patch_and_journal_drop_private_spans_and_pin_dates(root, monday):
    got = {}

    async def body(call):
        got["d"] = (await call("keepfold_add", {
            "kind": "decision", "title": "Take the offer next Monday",
            "fields": {"summary": f"Agreed yesterday with <private>{SECRET}</private>"}}))["id"]
        got["l"] = (await call("keepfold_add", {
            "kind": "loop", "title": "Send the form tomorrow",
            "fields": {"summary": "Asked for it last week"}}))["id"]
        await call("keepfold_patch", {
            "id": "me/profile", "op": "append_to_section", "target": "Brief",
            "user_confirmed": True,
            "text": f"- Moved to Pune last week <private>{SECRET}</private>"})
        await call("keepfold_log", {"kind": "journal", "payload": {
            "text": f"Met the recruiter yesterday; <private>{SECRET}</private> exam this Friday"}})
        # last: raw_text exposes the session, which then may not change me/
        got["r"] = (await call("keepfold_add", {
            "kind": "resource", "title": "Lecture tomorrow",  # not a durable statement kind
            "raw_text": f"Slides <private>{SECRET}</private> here"}))["id"]

    run(root, body)
    decision = (root / "decisions" / f"{got['d']}.md").read_text(encoding="utf-8")
    assert "Take the offer next Monday (2026-11-30)" in decision
    assert "Agreed yesterday (2026-11-22) with [private]" in decision
    loop = (root / "loops" / f"{got['l']}.md").read_text(encoding="utf-8")
    assert "Send the form tomorrow (2026-11-24)" in loop
    assert "Asked for it last week (2026-11-16 to 2026-11-22)" in loop
    resource = (root / "resources" / f"{got['r']}.md").read_text(encoding="utf-8")
    assert "Lecture tomorrow" in resource and "2026-11-24" not in resource
    assert (root / "raw" / f"{got['r']}.md").read_text(encoding="utf-8") == \
        "Slides [private] here"
    profile = (root / "me/profile.md").read_text(encoding="utf-8")
    assert "- Moved to Pune last week (2026-11-16 to 2026-11-22) [private]" in profile
    journal = next((root / "journal").glob("2026-11-23*.md")).read_text(encoding="utf-8")
    assert ("Met the recruiter yesterday (2026-11-22); [private] exam this Friday (2026-11-27)"
            in journal)
    every = "".join(p.read_text(encoding="utf-8", errors="replace")
                    for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
    assert SECRET not in every


def test_an_imported_tag_is_pinned_to_its_own_day(root):
    tags.apply_in(Vault(root), [("fact", "Started at Acme yesterday", "2026-06-10")])
    assert "Started at Acme yesterday (2026-06-09)" in \
        (root / "me/profile.md").read_text(encoding="utf-8")


def test_an_imported_tag_with_no_day_is_never_pinned(root):
    """No timestamp, no day: keepfold_patch must not pin it with today either."""
    tags.apply_in(Vault(root), [("fact", "Started at Beta Corp yesterday", "")])
    profile = (root / "me/profile.md").read_text(encoding="utf-8")
    assert "Started at Beta Corp yesterday" in profile and "yesterday (20" not in profile


def test_a_backdated_journal_line_is_pinned_as_of_its_own_day(root, monday):
    async def body(call):
        await call("keepfold_log", {"kind": "journal", "payload": {
            "text": "Ran 5k yesterday", "date": "2026-11-20"}})

    run(root, body)
    journal = next((root / "journal").glob("2026-11-20*.md")).read_text(encoding="utf-8")
    assert "Ran 5k yesterday (2026-11-19) (added 2026-11-23)" in journal


def test_approve_in_the_app_never_pins(root):
    """A line proposed days ago and approved now: today is not the day it was written."""
    (root / ".keepfold").mkdir(exist_ok=True)
    (root / "me" / "preferences.md").write_bytes(b"# Preferences\n\n## Coding\n")
    then = date.today() - dt.timedelta(days=3)  # inside the Review's 14 days
    (root / "inbox.md").write_text("# Inbox\n\n## About me (unconfirmed)\n- Coding: Reviews "
                                   f"code the day after tomorrow (proposed {then}, "
                                   "me/preferences)\n", encoding="utf-8")
    app = server.App(root)
    (p,) = [p for p in app.review()["pending"] if p["page"] == "preferences"]
    app.review_approve({"key": p["key"]})
    prefs = (root / "me" / "preferences.md").read_text(encoding="utf-8")
    assert "- Reviews code the day after tomorrow (confirmed " in prefs


def test_a_fact_harvest_from_old_chats_pins_nothing(root):
    fact_harvest._save(Vault(root), [("c1", "profile", "Moved to Pune yesterday", False)])
    profile = (root / "me/profile.md").read_text(encoding="utf-8")
    assert "Moved to Pune yesterday" in profile and "yesterday (20" not in profile


def test_the_chat_app_history_keeps_no_private_span(root):
    v, cid = Vault(root), "01KA0000000000000000000000"
    chat._save(v, cid, [{"role": "user", "text": f"Who is <private>{SECRET}</private>?",
                         "ts": "2026-11-23T10:00:00Z"},
                        {"role": "assistant", "text": "I don't know.", "ts": "x"}], True)
    saved = (root / chat.DIR / f"{cid}.jsonl").read_text(encoding="utf-8")
    assert SECRET not in saved and "Who is [private]?" in saved


def test_an_imported_export_keeps_no_private_span():
    chats._save("claude", [("conv-0001", 1_700_000_000, [
        ("user", "2026-11-01T09:00:00Z", f"Plan <private>{SECRET}</private> trip"),
        ("assistant", "2026-11-01T09:00:05Z", "Sure.")])])
    text = (chats.export_root() / chats.EXPORT_DIRS["claude"][0] / "conv-0001.jsonl"
            ).read_text(encoding="utf-8")
    assert SECRET not in text and "Plan [private] trip" in text


def test_the_recall_log_and_voice_samples_keep_no_private_span(root):
    v = Vault(root)
    recall_log.log_recall(v, "keepfold_recall", f"rent <private>{SECRET}</private>", [])
    assert [r["query"] for r in recall_log.rows(v)] == ["rent [private]"]
    sample = voice.fields({"audience": "friend", "act": "update", "channel": "chat",
                           "text": f"Paid <private>{SECRET}</private> today"})
    assert sample["text"] == "Paid [private] today"


# ---- dates.pin ---------------------------------------------------------------------------------

@pytest.mark.parametrize("today, text, want", [
    # a Monday
    (date(2026, 11, 23), "met the recruiter yesterday", "yesterday (2026-11-22)"),
    (date(2026, 11, 23), "last Monday", "last Monday (2026-11-16)"),
    (date(2026, 11, 23), "this Monday", "this Monday (2026-11-23)"),
    (date(2026, 11, 23), "exam next Monday", "next Monday (2026-11-30)"),
    (date(2026, 11, 23), "trip this weekend", "this weekend (2026-11-28 to 2026-11-29)"),
    (date(2026, 11, 23), "away last week", "last week (2026-11-16 to 2026-11-22)"),
    # a Sunday
    (date(2026, 11, 29), "trip this weekend", "this weekend (2026-11-28 to 2026-11-29)"),
    (date(2026, 11, 29), "away last week", "last week (2026-11-16 to 2026-11-22)"),
    (date(2026, 11, 29), "due this Friday", "this Friday (2026-11-27)"),
    (date(2026, 11, 29), "last Sunday", "last Sunday (2026-11-22)"),
    (date(2026, 11, 29), "next Monday", "next Monday (2026-11-30)"),
    (date(2026, 11, 29), "next Friday", "next Friday (2026-12-04)"),
    (date(2026, 11, 24), "next Wednesday", "next Wednesday (2026-12-02)"),  # not tomorrow
    (date(2026, 11, 29), "tomorrow", "tomorrow (2026-11-30)"),
    # a month boundary (a Tuesday)
    (date(2026, 12, 1), "yesterday", "yesterday (2026-11-30)"),
    (date(2026, 12, 1), "the day before yesterday", "the day before yesterday (2026-11-29)"),
    (date(2026, 12, 1), "3 days ago", "3 days ago (2026-11-28)"),
    (date(2026, 12, 1), "due in 30 days", "in 30 days (2026-12-31)"),
    # a year boundary (a Friday)
    (date(2027, 1, 1), "yesterday", "yesterday (2026-12-31)"),
    (date(2027, 1, 1), "tonight", "tonight (2027-01-01)"),
    (date(2027, 1, 1), "tomorrow", "tomorrow (2027-01-02)"),
    (date(2027, 1, 1), "two weeks ago", "two weeks ago (2026-12-18)"),
    (date(2027, 1, 1), "last week", "last week (2026-12-21 to 2026-12-27)"),
    (date(2027, 1, 1), "next Monday", "next Monday (2027-01-04)"),
])
def test_pin_dates_the_phrase(today, text, want):
    out = dates.pin(text, today)
    assert want in out and dates.unpin(out) == text
    assert dates.pin(out, today) == out  # a second save adds no second date


@pytest.mark.parametrize("text", [
    "a few days ago", "3 or 4 days ago", "2-3 days ago", "in a day or two",  # vague
    "in October", "on 30 Nov", "on Monday, 30 Nov", "on 2026-11-30", "since 12 Nov",  # absolute
    "run `make today` first", "```\nexam tomorrow\n```", "past 3 days", "this week",
    "yesterday (2026-11-22) again", "Yesterdays",
    "exam on Friday", "on Monday", "met her Friday", "Friday works",  # no tense: never pinned
])
def test_pin_leaves_these_alone(text):
    assert dates.pin(text, date(2026, 11, 23)) == text


def test_pin_keeps_the_words_and_steps_over_a_possessive():
    assert dates.pin("Yesterday's talk, back today.", date(2026, 11, 23)) == \
        "Yesterday's (2026-11-22) talk, back today (2026-11-23)."
