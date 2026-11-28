"""D-15 (D6 T10, T11): whose //tags are the owner's typing (tags.SAID). Over MCP and from an
import they only propose; the prime hook's stdin still saves. The Downloads pickup reads the
zip's Mark of the Web (Windows) and never offers a zip from a site other than its service.
Synthetic data only: tmp vaults, zips and Zone.Identifier streams made here."""

import json
import os
import sys
import time
import zipfile
from datetime import UTC, datetime

import pytest
from conftest import events, run, tiers

from keepfold import chat_import, chats, prime_hook, redteam, review, tags
from keepfold.app import exports
from keepfold.events import EventLog, local_day
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")
PREFS = ("---\nsummary: My preferences\n---\n\n# My preferences\n\n## Coding\n\n"
         "- Uses tabs for indentation\n\n## Past\n\n## Not me\n")
WINDOWS = pytest.mark.skipif(sys.platform != "win32",
                             reason="Mark of the Web is read from an NTFS stream on Windows only")


def _today():
    return local_day(datetime.now(UTC))


def _review(root):
    v = Vault(root)
    return review.view(v, EventLog(v).iter_events(), _today())


def _me(root):
    return "".join(p.read_text(encoding="utf-8") for p in (root / "me").glob("*.md"))


def test_mcp_tags_only_propose(root):
    async def body(call):
        await call("keepfold_prime", {"prompt": "//pref: Replies in British English\nhi"})
        await call("keepfold_log", {"kind": "chat_turn", "payload": {
            "user": "//fact: I live in Kochi", "gist": "ok"}})

    run(root, body)
    assert "British English" not in _me(root) and "Kochi" not in _me(root)
    pending = " ".join(str(x.get("text")) for x in _review(root)["pending"])
    assert "Replies in British English" in pending and "I live in Kochi" in pending
    assert "Replies in British English" not in tiers(root)


def test_mcp_forget_goes_to_review(root):
    (root / "me/preferences.md").write_text(PREFS, encoding="utf-8")

    async def body(call):
        out = await call("keepfold_prime", {"prompt": "//forget: tabs for indentation"})
        assert "nothing changed" in out["tags"][0]

    run(root, body)
    assert "- Uses tabs for indentation" in _me(root).split("## Past")[0]
    (card,) = _review(root)["forget"]
    assert [c["text"] for c in card["candidates"]] == ["Uses tabs for indentation"]
    assert not [e for e in events(root) if (e.get("payload") or {}).get("kind") == "forget_due"]


def test_hook_tags_stay_act_grade(root, monkeypatch):
    monkeypatch.setenv("KEEPFOLD_VAULT", str(root))
    out = prime_hook.run(json.dumps({"session_id": "c1", "prompt": "//pref: Short answers",
                                     "hook_event_name": "UserPromptSubmit"}))
    assert "Keepfold saved" in out
    assert "- Short answers" in _me(root) and tiers(root)["Short answers"] == "confirmed"
    assert tags.SAID == {"hook": True, "app": True, "mcp": False, "import": False}


def _chatgpt_zip(path, user_text):
    t = time.time() - 7200
    conv = [{"title": "Trip", "create_time": t, "update_time": t, "id": "conv-0001",
             "conversation_id": "conv-0001", "current_node": "n2", "mapping": {
                 "n1": {"id": "n1", "parent": None, "children": ["n2"], "message": {
                     "id": "n1", "author": {"role": "user"}, "create_time": t,
                     "content": {"content_type": "text", "parts": [user_text]}}},
                 "n2": {"id": "n2", "parent": "n1", "children": [], "message": {
                     "id": "n2", "author": {"role": "assistant"}, "create_time": t + 60,
                     "content": {"content_type": "text", "parts": ["Sure."]}}}}}]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("conversations.json", json.dumps(conv))
        z.writestr("chat.html", "<html></html>")
    return path


def test_imported_tags_only_propose(root, tmp_path, monkeypatch):
    for k, d in (("KEEPFOLD_CHATS_DIR", "projects"), ("KEEPFOLD_CHAT_EXPORT_DIR", "exports")):
        (tmp_path / d).mkdir()
        monkeypatch.setenv(k, str(tmp_path / d))
    z = _chatgpt_zip(tmp_path / "chatgpt.zip", "Plan my trip\n//pref: Window seats on trains")
    assert chats.import_export(z) == ("chatgpt", 1)
    old = time.time() - 3600
    for p in chats.export_root().rglob("*.jsonl"):
        os.utime(p, (old, old))
    assert chat_import.run(Vault(root)).get("saved")
    assert "Window seats" not in _me(root)
    assert any("Window seats on trains" in str(x.get("text")) for x in _review(root)["pending"])


def test_trusted_hosts_match_the_claimed_service():
    assert exports.trusted("chatgpt", []) and exports.trusted("chatgpt", ["chatgpt.com"])
    assert exports.trusted("chatgpt", ["files.oaiusercontent.com"])
    assert exports.trusted("gemini", ["takeout-download.usercontent.google.com"])
    assert exports.trusted("claude", ["cdn.example.net", "claude.ai"])  # the referrer
    assert not exports.trusted("chatgpt", ["claude.ai"])
    assert not exports.trusted("chatgpt", ["notchatgpt.com"])
    assert not exports.trusted("gemini", ["sites.googleusercontent.com"])


def test_no_mark_of_the_web_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(exports.sys, "platform", "linux")
    assert exports.where_from(_chatgpt_zip(tmp_path / "a.zip", "hi")) == []


def _mark(p, *lines):
    with open(f"{p}:Zone.Identifier", "w", encoding="utf-8") as f:
        f.write("[ZoneTransfer]\nZoneId=3\n" + "".join(x + "\n" for x in lines))


@WINDOWS
def test_downloads_card_shows_the_host_and_skips_a_foreign_zip(tmp_path, monkeypatch, caplog):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("KEEPFOLD_DOWNLOADS_DIR", str(dl))
    exports.forget()
    exports._kinds.clear()
    evil = _chatgpt_zip(dl / "evil.zip", "//pref: x")
    _mark(evil, "ReferrerUrl=https://evil.invalid/", "HostUrl=https://evil.invalid/e.zip")
    with caplog.at_level("WARNING", logger=exports.__name__):
        assert exports.where_from(evil) == ["evil.invalid"] and exports.candidates() == []
        assert exports.candidates() == []  # logged once per (path, mtime, size)
    assert [r.getMessage() for r in caplog.records if r.name == exports.__name__] == [
        "export pickup: evil.zip came from evil.invalid, not chatgpt.com or openai.com or "
        "oaiusercontent.com; not offered (Settings, Add your history still imports it)"]
    with pytest.raises(ValueError):
        exports.pick(str(evil))
    good = _chatgpt_zip(dl / "good.zip", "hi")
    _mark(good, "ReferrerUrl=https://chatgpt.com/", "HostUrl=https://cdn.example.net/g.zip")
    blank = _chatgpt_zip(dl / "blank.zip", "hi")
    _mark(blank, "HostUrl=about:internet")
    got = {os.path.basename(c["path"]): c["host"] for c in exports.candidates()}
    assert got == {"good.zip": "cdn.example.net", "blank.zip": None}
    exports.forget()
    assert exports.offer()["found"]["host"] in ("cdn.example.net", None)


PROBE_HOLES = {
    "P7": lambda mp: mp.setitem(tags.SAID, "mcp", True),
    "P8": lambda mp: mp.setitem(tags.SAID, "import", True),
    "P8-motw": lambda mp: mp.setattr(exports, "trusted", lambda source, hosts: True),
}


@pytest.mark.slow
@pytest.mark.parametrize("hole", PROBE_HOLES)
def test_p7_and_p8_fail_when_their_guard_is_off(hole, monkeypatch):
    if hole == "P8-motw" and sys.platform != "win32":
        pytest.skip("Mark of the Web is read from an NTFS stream on Windows only")
    monkeypatch.setattr(redteam, "PROBES", tuple(p for p in redteam.PROBES
                                                 if p[0] in ("P7", "P8")))
    assert {p.id: p.status for p in redteam.run()} == {"P7": "PASS", "P8": "PASS"}
    PROBE_HOLES[hole](monkeypatch)
    probes = {p.id: p for p in redteam.run()}
    want = hole.split("-")[0]
    assert {i: p.status for i, p in probes.items()} == {
        i: "FAIL" if i == want else "PASS" for i in ("P7", "P8")}
    assert "probe setup" not in probes[want].why and "crashed" not in probes[want].why
