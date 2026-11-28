"""`keepfold import`: notes folders and zips (Obsidian, Notion, plain), calendars (.ics), and the
kind detection that sends every export to its importer."""

import json
import re
import shutil
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keepfold import importers
from keepfold.catalog import Catalog
from keepfold.cli import app
from keepfold.index import Index
from keepfold.vault import Vault, VaultError

EXPORTS = Path(__file__).parent / "fixtures" / "exports"
runner = CliRunner()


def _zip(dest: Path, members: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(dest, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return dest


def _folder_zip(src: Path, dest: Path) -> Path:
    return _zip(dest, {p.relative_to(src).as_posix(): p.read_bytes()
                       for p in sorted(src.rglob("*")) if p.is_file()})


REPORT = importers.REPORT


def _counts(r: dict) -> dict:
    """The importer's own counts, without the shared import-report keys."""
    return {k: v for k, v in r.items()
            if k not in (*REPORT, "updated", "gone", "loops", "loops_over")}


def _notion_zip(tmp_path) -> Path:
    """Notion's export: an outer zip holding one zip per part."""
    part = _folder_zip(EXPORTS / "notion", tmp_path / "Export-abc-Part-1.zip")
    return _zip(tmp_path / "notion.zip", {"Export-abc-Part-1.zip": part.read_bytes()})


def _obsidian(tmp_path) -> Path:
    vault = tmp_path / "MyObsidian"
    shutil.copytree(EXPORTS / "obsidian", vault)
    (vault / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0binary")
    (vault / "huge.md").write_text("x" * (importers.MAX_BYTES + 1), "utf-8")
    return vault


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts}


def test_notion_zip_strips_id_suffixes_and_skips_csv(root_plain, tmp_path):
    v = Vault(root_plain)
    r = importers.import_notes(v, _notion_zip(tmp_path))
    assert _counts(r) == {"source": "notion", "new": 2, "same": 0, "skipped": 1}
    work = v.read("resource", "notion-work")
    trip = v.read("resource", "notion-trip-plan")
    assert work.fields["title"] == "Work" and trip.fields["status"] == "reference"
    assert "From: Work/Trip plan.md" in trip.body
    assert "Book the Goa train" in trip.body
    for p in (root_plain / "resources").glob("*.md"):
        assert not re.search(r"[0-9a-f]{32}", p.name + p.read_text("utf-8").split("\n\n", 2)[1])


def test_obsidian_folder_skips_hidden_binary_and_big_and_keeps_private(root_plain, tmp_path):
    v = Vault(root_plain)
    r = importers.import_notes(v, _obsidian(tmp_path))
    # .obsidian/app.json, .trash/old.md, pic.png and huge.md are skipped
    assert _counts(r) == {"source": "obsidian", "new": 4, "same": 0, "skipped": 4}
    ids = sorted(p.stem for p in (root_plain / "resources").glob("obsidian-*.md"))
    assert ids == ["obsidian-daily", "obsidian-diary", "obsidian-graph-theory", "obsidian-reading"]
    assert v.read("resource", "obsidian-diary").fields["private"] is True
    graph = v.read("resource", "obsidian-graph-theory").body
    # a link to another imported note keeps working; one to a private note becomes plain text
    assert "see [[obsidian-daily|Daily]] and my diary." in graph
    assert "pic.png" not in graph and "tags:" not in graph
    idx = Index(v, Catalog.load(v))
    assert [h["id"] for h in idx.find("Kruskal")] == ["obsidian-daily"]  # recall can reach it
    assert idx.find("private line about my week") == []


def test_reimport_dedupes_by_content(root_plain, tmp_path):
    v = Vault(root_plain)
    src = _obsidian(tmp_path)
    importers.import_notes(v, src)
    before = _tree(root_plain)
    assert _counts(importers.import_notes(v, src)) == {"source": "obsidian", "new": 0,
                                                       "same": 4, "skipped": 4}
    assert importers.import_calendar(v, EXPORTS / "calendar.ics")["new"] == 3
    after_cal = _tree(root_plain)
    assert _counts(importers.import_calendar(v, EXPORTS / "calendar.ics")) == \
        {"events": 3, "months": 2, "new": 0}
    assert _tree(root_plain) == after_cal and set(before) < set(after_cal)
    (src / "Daily.md").write_text("Revised Prim too.\n", "utf-8")  # RR-18: updated in place
    r = importers.import_notes(v, src)
    assert (r["new"], r["updated"]) == (0, 1)
    assert "Revised Prim too." in (root_plain / "resources" / "obsidian-daily.md").read_text(
        "utf-8")


def test_calendar_month_pages_without_attendees(root_plain):
    v = Vault(root_plain)
    assert _counts(importers.import_calendar(v, EXPORTS / "calendar.ics")) == \
        {"events": 3, "months": 2, "new": 3}
    nov = v.read("resource", "calendar-2026-11")
    assert "private" not in nov.fields  # visible unless the user chooses private
    assert "- 2026-11-05 all day · Diwali holiday" in nov.body
    assert ("- 2026-11-03 09:30 (Asia/Kolkata) · Algorithms lab with a long title that the "
            "export folds onto a second line · Room 4, CS block · repeats FREQ=WEEKLY;BYDAY=TU"
            ) in nov.body
    assert "Mock interview" in v.read("resource", "calendar-2026-12").body
    everything = b"".join(_tree(root_plain).values())
    for gone in (b"friend@", b"other.person", b"prof@", b"meet.example", b"Reminder"):
        assert gone not in everything


def test_calendar_private_when_chosen(root_plain):
    v = Vault(root_plain)
    importers.import_calendar(v, EXPORTS / "calendar.ics", private=True)
    assert v.read("resource", "calendar-private-2026-12").fields["private"] is True


def _ics(tmp_path, name: str, summary: str, location: str = "") -> Path:
    f = tmp_path / name
    f.write_text("BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20261110\n"
                 f"SUMMARY:{summary}\n" + (f"LOCATION:{location}\n" if location else "")
                 + "END:VEVENT\nEND:VCALENDAR\n", "utf-8")
    return f


def test_private_calendar_never_lands_on_a_public_page(root_plain, tmp_path):
    v = Vault(root_plain)
    importers.import_calendar(v, EXPORTS / "calendar.ics")
    importers.import_calendar(v, _ics(tmp_path, "therapy.ics", "Therapy session"), private=True)
    assert "Therapy" not in v.read("resource", "calendar-2026-11").body
    page = v.read("resource", "calendar-private-2026-11")
    assert page.fields["private"] is True and "Therapy session" in page.body


def test_calendar_drops_remote_images_and_links(root_plain, tmp_path):
    v = Vault(root_plain)
    importers.import_calendar(v, _ics(
        tmp_path, "invite.ics",
        "Sync ![x](https://attacker.example/p.png?u=1) [click](https://evil.example)",
        "<https://evil.example/room> Room 5"))
    body = v.read("resource", "calendar-2026-11").body
    assert "Sync" in body and "click" in body and "Room 5" in body
    for gone in ("attacker.example", "evil.example", "![", "]("):
        assert gone not in body


def test_detect_reads_only_the_head_and_agrees_with_chat_import(tmp_path, monkeypatch):
    gem = (EXPORTS / "gemini_MyActivity.json").read_text("utf-8")
    z = _zip(tmp_path / "top.zip", {"MyActivity.json": gem})
    assert importers.detect(z) == ["chats"]
    monkeypatch.setenv("KEEPFOLD_CHAT_EXPORT_DIR", str(tmp_path / "exported"))
    assert importers.chats.import_export(z)[0] == "gemini"
    big = _zip(tmp_path / "big.zip", {"MyActivity.json": gem + " " * 9000})
    monkeypatch.setattr(importers, "MAX_ENTRY", 8000)  # detect still works: it reads 4 KB
    assert importers.detect(big) == ["chats"]


def test_zip_bomb_entries_are_refused_before_they_are_read(root_plain, tmp_path, monkeypatch):
    from keepfold.browse import takeout
    monkeypatch.setenv("KEEPFOLD_CHAT_EXPORT_DIR", str(tmp_path / "exported"))
    monkeypatch.setattr(importers, "MAX_ENTRY", 1000)
    monkeypatch.setattr(importers, "MAX_STREAM", 1000)  # the JSON lists stream (C11)
    pad = " " * 5000
    bomb = _zip(tmp_path / "bomb.zip", {
        "conversations.json": f"[{pad}]",
        "Takeout/My Activity/Search/MyActivity.json": f"[{pad}]",
        "big.ics": f"BEGIN:VCALENDAR\n{pad}"})
    for call in (lambda: importers.chats.import_export(bomb), lambda: takeout.records(bomb),
                 lambda: importers.import_calendar(Vault(root_plain), bomb)):
        with pytest.raises(VaultError) as e:
            call()
        assert "too big" in str(e.value)
    monkeypatch.setattr(importers, "MAX_ENTRY", 10_000)
    monkeypatch.setattr(importers, "MAX_STREAM", 10_000)
    monkeypatch.setattr(importers, "MAX_TOTAL", 8000)  # two 5 KB files are over the total
    two = _zip(tmp_path / "two.zip", {f"Takeout/My Activity/{p}/MyActivity.json": f"[{pad}]"
                                      for p in ("Search", "YouTube")})
    with pytest.raises(VaultError) as e:
        takeout.records(two)
    assert "too big" in str(e.value)


@pytest.mark.gate("G8")
def test_member_names_never_become_paths(root_plain, tmp_path, monkeypatch):
    """T20: a zip is read in memory; whatever a member is called, no file appears outside the
    vault (and the chat export folder), and no page id carries a path."""
    exported = tmp_path / "exported"
    monkeypatch.setenv("KEEPFOLD_CHAT_EXPORT_DIR", str(exported))
    names = ["../../escape.md", "..\\..\\escape2.md", "/abs/escape3.md", "C:/win/escape4.md",
             "C:\\win\\escape5.md", "//server/share/escape6.md", "\\\\server\\share\\escape7.md",
             "ok/../../escape8.md", "con.md", "a/../b.md"]
    note = "Kruskal builds a minimum spanning tree.\n"
    convs = [{"uuid": "../../escape-chat", "chat_messages": [{"sender": "human", "text": "x"}]},
             {"uuid": "C:\\escape-chat", "chat_messages": [{"sender": "human", "text": "x"}]},
             {"uuid": "11111111-aaaa", "chat_messages": [{"sender": "human", "text": "hi"}]}]
    (tmp_path / "in").mkdir()
    notes = _zip(tmp_path / "in" / "notes.zip", {n: note + n for n in names})
    chats = [_zip(tmp_path / "in" / f"chats{i}.zip", {n: json.dumps(convs)})
             for i, n in enumerate(("conversations.json", "../../conversations.json"))]
    before = _tree(tmp_path)
    importers.import_notes(Vault(root_plain), notes)
    for chat in chats:
        importers.chats.import_export(chat)
    allowed = (root_plain.resolve(), exported.resolve())
    for p in _tree(tmp_path).keys() - before.keys():
        where = (tmp_path / p).resolve()
        assert any(where.is_relative_to(a) for a in allowed), p
    for p in (root_plain / "resources").glob("*.md"):
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*\.md", p.name), p.name  # a slug, never a path
    assert sorted(p.name for p in exported.rglob("*.jsonl")) == ["11111111-aaaa.jsonl"]


def test_detect_keeps_takeout_kinds_beside_mail_and_location(tmp_path):
    """Sources B decide only when nothing else matches: a full Takeout with Mail and Location
    History is still activity and calendar; each of those files alone is its own kind."""
    search = (EXPORTS / "takeout_MyActivity.json").read_text("utf-8")
    ics = (EXPORTS / "calendar.ics").read_text("utf-8")
    loc = '{"timelineObjects": []}'
    mbox = "From x Mon Jan 01 00:00:00 2024\nFrom: me@example.test\n\nhi\n"
    files = {"Takeout/My Activity/Search/MyActivity.json": search,
             "Takeout/Calendar/me.ics": ics,
             "Takeout/Location History/Semantic Location History/2024/2024_JANUARY.json": loc,
             "Takeout/Mail/All mail.mbox": mbox}
    assert importers.detect(_zip(tmp_path / "full.zip", files)) == ["takeout", "calendar"]
    alone = {"Takeout/My Activity/Search/MyActivity.json": ["takeout"],
             "Takeout/Calendar/me.ics": ["calendar"],
             "Takeout/Location History/Semantic Location History/2024/2024_JANUARY.json":
                 ["location"], "Takeout/Mail/All mail.mbox": ["mail"]}
    for i, (name, kinds) in enumerate(alone.items()):
        assert importers.detect(_zip(tmp_path / f"{i}.zip", {name: files[name]})) == kinds
        one = tmp_path / str(i) / name.rsplit("/", 1)[-1]
        one.parent.mkdir()
        one.write_text(files[name], encoding="utf-8")
        assert importers.detect(one) == kinds, name
    timeline = tmp_path / "Timeline.json"
    timeline.write_text('{"semanticSegments": []}', encoding="utf-8")
    assert importers.detect(timeline) == ["location"]  # not a one-file chat export


def test_detect_every_kind(tmp_path):
    gem = (EXPORTS / "gemini_MyActivity.json").read_text("utf-8")
    search = (EXPORTS / "takeout_MyActivity.json").read_text("utf-8")
    cases = {
        _zip(tmp_path / "claude.zip", {"conversations.json": '[{"chat_messages": []}]'}):
            ["chats"],
        EXPORTS / "chatgpt_conversations.json": ["chats"],
        _zip(tmp_path / "gemini.zip", {"Takeout/My Activity/Gemini Apps/MyActivity.json": gem}):
            ["chats"],
        EXPORTS / "perplexity_thread.md": ["chats"],
        _zip(tmp_path / "pplx.zip", {"conversations-20260901_100000-ab.json": "{}",
                                     "user-data-20260901_100000-ab.xlsx": "x"}): ["chats"],
        _zip(tmp_path / "takeout.zip", {"Takeout/My Activity/Search/MyActivity.json": search}):
            ["takeout"],
        _zip(tmp_path / "both.zip", {"Takeout/My Activity/Gemini Apps/MyActivity.json": gem,
                                     "Takeout/My Activity/Search/MyActivity.json": search}):
            ["chats", "takeout"],
        _notion_zip(tmp_path): ["notes"],
        _obsidian(tmp_path): ["notes"],
        EXPORTS / "obsidian" / "Daily.md": ["notes"],
        EXPORTS / "calendar.ics": ["calendar"],
        _zip(tmp_path / "calendar-export.zip",
             {"me@gmail.com.ics": (EXPORTS / "calendar.ics").read_bytes()}): ["calendar"],
    }
    for path, kinds in cases.items():
        assert importers.detect(path) == kinds, path
    with pytest.raises(VaultError):
        importers.detect(tmp_path / "missing.zip")
    html = _zip(tmp_path / "takeout-html.zip", {  # activity left as HTML; a stray .txt
        "Takeout/My Activity/Gemini Apps/MyActivity.html": "<html></html>",
        "Takeout/My Activity/Gemini Apps/notes.txt": "attachment"})
    with pytest.raises(VaultError, match="JSON"):  # HTML Keepfold cannot parse
        importers.detect(html)


def test_detect_youtube_history_as_takeout(tmp_path):
    rows = (EXPORTS / "takeout_MyActivity.json").read_text("utf-8")
    one = tmp_path / "watch-history.json"
    one.write_text(rows, "utf-8")
    assert importers.detect(one) == ["takeout"]  # not a lone chat .json
    yt = "Takeout/YouTube and YouTube Music/history/"
    z = _zip(tmp_path / "yt.zip", {yt + "watch-history.json": rows,
                                   yt + "search-history.json": rows})
    assert importers.detect(z) == ["takeout"]
    from keepfold.app import exports
    assert exports.kind_of([yt + "watch-history.html"]) == "google-activity"


def test_detect_routes_html_takeout(tmp_path):
    act = EXPORTS / "takeout_html" / "Takeout" / "My Activity"
    search, gem = (act / p / "MyActivity.html" for p in ("Search", "Gemini Apps"))
    assert importers.detect(EXPORTS / "takeout_html") == ["chats", "takeout"]
    assert importers.detect(_zip(tmp_path / "s.zip", {
        "Takeout/My Activity/Search/MyActivity.html": search.read_bytes(),
        "Takeout/My Activity/Ads/MyActivity.html": search.read_bytes()})) == ["takeout"]
    assert importers.detect(gem) == ["chats"]  # one file alone: sniffed for Gemini Apps
    assert importers.detect(search) == ["takeout"]
    with pytest.raises(VaultError, match="no Search, YouTube, Chrome or Gemini"):
        importers.detect(_zip(tmp_path / "ads.zip", {
            "Takeout/My Activity/Ads/MyActivity.html": search.read_bytes()}))


def test_cli_dry_run_writes_nothing(root_plain, tmp_path):
    before = _tree(root_plain)
    for src in (_obsidian(tmp_path), EXPORTS / "calendar.ics", EXPORTS / "perplexity_thread.md"):
        r = runner.invoke(app, ["import", str(src), "--dry-run", "--vault", str(root_plain)])
        assert r.exit_code == 0, r.output
        assert "dry run" in r.output
    assert _tree(root_plain) == before
    assert not any(importers.chats.export_root().rglob("*.jsonl"))


def test_cli_imports_and_source_overrides(root_plain):
    r = runner.invoke(app, ["import", str(EXPORTS / "calendar.ics"), "--vault", str(root_plain)])
    assert r.exit_code == 0 and "3 new events" in r.output, r.output
    r = runner.invoke(app, ["import", str(EXPORTS / "obsidian" / "Daily.md"), "--source",
                            "notion", "--vault", str(root_plain)])
    assert r.exit_code == 0 and (root_plain / "resources" / "notion-daily.md").is_file()
    r = runner.invoke(app, ["import", str(EXPORTS / "calendar.ics"), "--source", "nope",
                            "--vault", str(root_plain)])
    assert r.exit_code == 1 and "--source must be one of" in r.output
    r = runner.invoke(app, ["import-chats", str(EXPORTS / "perplexity_thread.md")])
    assert r.exit_code == 0 and "Perplexity" in r.output


def test_cli_private_is_refused_for_chats_and_google_activity(root_plain):
    """G5: chats and Google activity have no private mode; --private refuses, writing nothing."""
    for f in ("perplexity_thread.md", "takeout_MyActivity.json"):
        r = runner.invoke(app, ["import", str(EXPORTS / f), "--private", "--vault",
                                str(root_plain)])
        assert r.exit_code == 1 and "--private applies to notes, calendars" in r.output
    assert not any(importers.chats.export_root().rglob("*.jsonl"))


def test_guides_for_notes_and_calendar():
    assert set(importers.GUIDES) == {"notes", "calendar", "docs", "repos", "highlights", "mail",
                                    "selfchat", "location", "photos", "spotify",
                                    "voice-memos", "anki"}
    for k, g in importers.GUIDES.items():
        assert g["steps"] and g["wait"] and g["pick"]
        # docs, repos and photos are folders on this computer: nothing to open
        assert g["link"].startswith("https://") or (
            k in ("docs", "repos", "photos", "voice-memos") and not g["link"])


def test_detect_takeout_in_another_language(tmp_path):
    """A German Takeout's localized folder and file names route like English ones; an
    unknown file name (Russian here) is not taken as Google activity."""
    search = (EXPORTS / "takeout_MyActivity.json").read_text("utf-8").replace(
        '"Search"', '"Suche"')
    de = _zip(tmp_path / "de.zip", {
        "Takeout/Meine Aktivitäten/Suche/MeineAktivitäten.json": search,
        "Takeout/Meine Aktivitäten/Gemini-Apps/MeineAktivitäten.json":
            (EXPORTS / "gemini_MyActivity.json").read_text("utf-8").replace(
                "Gemini Apps", "Gemini-Apps")})
    assert importers.detect(de) == ["chats", "takeout"]
    ru = _zip(tmp_path / "ru.zip", {"Takeout/Мои действия/Поиск/МоиДействия.json": search})
    assert "takeout" not in importers.detect(ru)


@pytest.mark.parametrize("front", [
    "private: true", "private: True", 'private: "true"', "private: yes", "private: on",
    "private: 1", "tags: [study, private]", "tags:\n  - private", "tags: '#private'",
    "tags: study, private", "tag: private", "title: [unclosed", "- just\n- a list",
    "private: true # trailing comment", "a: 1\na: 2"])
def test_frontmatter_private_in_any_truthy_form_or_tag_or_broken_is_private(front):
    """C4: the old regex only knew `private: true|yes` alone on a line."""
    assert importers._is_private(front)


@pytest.mark.parametrize("front", [
    "", "private: false", "private: no", "private: 0", "title: Graphs",
    "tags: [study, privateer]", "summary: kept private for now"])
def test_frontmatter_without_a_private_mark_is_public(front):
    assert not importers._is_private(front)


def test_notes_import_ends_with_the_import_report(root_plain, tmp_path):
    """Every import ends with read / imported / private / duplicate / skipped (+ reason)."""
    src = _obsidian(tmp_path)
    (src / "Quoted.md").write_text('---\nprivate: "true"\n---\nquoted private\n', "utf-8")
    (src / "Tagged.md").write_text("---\ntags: [private]\n---\ntagged private\n", "utf-8")
    v = Vault(root_plain)
    r = importers.import_notes(v, src)
    assert v.read("resource", "obsidian-quoted").fields["private"] is True
    assert v.read("resource", "obsidian-tagged").fields["private"] is True
    assert importers.report_line("Obsidian", r) == (
        "Obsidian: read 10, imported 6, private 3, duplicate 0, skipped 4 "
        "(3 not a note, 1 over 5 MB)")
    out = runner.invoke(app, ["import", str(src), "--vault", str(root_plain)]).output
    assert "Obsidian: read 10, imported 0, private 0, duplicate 6, skipped 4" in out
    out = runner.invoke(app, ["import", str(EXPORTS / "calendar.ics"), "--private",
                              "--vault", str(root_plain)]).output
    assert "calendar: read 3, imported 3, private 3, duplicate 0, skipped 0" in out


# ---- C11: a big chat or activity JSON list streams --------------------------------------


def _reader(tmp_path, text: str):
    p = tmp_path / "conversations.json"
    p.write_bytes(text.encode("utf-8"))
    ((_, _, _, read),) = importers._files(p)
    return read


def test_a_json_list_streams_element_by_element_and_matches_json_loads(tmp_path, monkeypatch):
    rows = [{"id": i, "n": 12345 + i, "f": -1.5e3, "s": "ä€😀 \"q\" \\ \n", "ok": i % 2 == 0,
             "none": None, "deep": [[], {}, [1, {"x": "]"}]]} for i in range(60)]
    rows += [7, "plain", None, [1, 2], 3.25]
    text = "﻿ \n[ " + " ,\r\n ".join(json.dumps(r, ensure_ascii=False) for r in rows) + " ]\n"
    monkeypatch.setattr(importers, "STREAM_CHUNK", 7)  # chunk edges split numbers and UTF-8
    monkeypatch.setattr(importers, "MAX_ENTRY", 100)  # too big to be read whole
    got = importers.json_value(_reader(tmp_path, text))
    assert not isinstance(got, list)  # the streamed path, lazy
    assert list(got) == json.loads(text.lstrip("﻿")) == rows
    assert list(importers.json_value(_reader(tmp_path, " [ ] "))) == []
    with pytest.raises(VaultError, match="too big"):  # a non-list is still read whole, capped
        importers.json_value(_reader(tmp_path, json.dumps({"conversations": rows})))


@pytest.mark.parametrize("bad", ['[{"a": 1}, {"a": ', "[1, 2", "[1 2]", "[1,]", "[", "  ",
                                 '["unterminated', '{"a": 1}'])
def test_a_truncated_or_malformed_json_list_fails_cleanly(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(importers, "STREAM_CHUNK", 3)
    with pytest.raises(ValueError):
        list(_reader(tmp_path, bad)(items=True))


def test_a_big_chat_export_streams_through_import_and_matches_a_whole_read(tmp_path,
                                                                           monkeypatch):
    src = EXPORTS / "chatgpt_conversations.json"
    monkeypatch.setenv("KEEPFOLD_CHAT_EXPORT_DIR", str(tmp_path / "whole"))
    whole = importers.chats.import_export(src)
    monkeypatch.setenv("KEEPFOLD_CHAT_EXPORT_DIR", str(tmp_path / "streamed"))
    # over the whole-file cap (which also caps one streamed item), under MAX_STREAM
    monkeypatch.setattr(importers, "MAX_ENTRY", src.stat().st_size - 1)
    monkeypatch.setattr(importers, "STREAM_CHUNK", 64)
    assert importers.chats.import_export(src) == whole
    a, b = (sorted((p.name, p.read_bytes()) for p in (tmp_path / d).rglob("*.jsonl"))
            for d in ("whole", "streamed"))
    assert a == b and a
    cut = tmp_path / "conversations.json"
    cut.write_bytes(src.read_bytes().rstrip()[:-40])  # cut short: refused, nothing written
    monkeypatch.setenv("KEEPFOLD_CHAT_EXPORT_DIR", str(tmp_path / "cut"))
    with pytest.raises(VaultError, match="Cannot read conversations.json"):
        importers.chats.import_export(cut)
    assert not list((tmp_path / "cut").rglob("*.jsonl"))


def test_a_big_activity_file_streams_and_a_cut_one_is_refused_by_name(tmp_path, monkeypatch):
    from keepfold.browse import takeout
    src = EXPORTS / "takeout_MyActivity.json"
    whole = [(v.url, v.start, v.title) for v in takeout.records(src)]
    monkeypatch.setattr(importers, "MAX_ENTRY", src.stat().st_size - 1)
    monkeypatch.setattr(importers, "STREAM_CHUNK", 50)
    assert [(v.url, v.start, v.title) for v in takeout.records(src)] == whole and whole
    cut = tmp_path / "MyActivity.json"
    cut.write_bytes(src.read_bytes().rstrip()[:-30])
    with pytest.raises(VaultError, match="Cannot read MyActivity.json"):
        takeout.records(cut)


def test_an_import_dates_a_file_by_the_keepfold_day():
    """Audit 21 Nov I12: a note or document's date is its mtime's Keepfold day (local, 04:00
    rollover, events.local_day), never its UTC date (04:45 IST is still that day)."""
    from datetime import UTC, datetime

    from keepfold import more_sources
    from keepfold.events import local_day
    base = datetime(2026, 11, 20, tzinfo=UTC).timestamp()
    t = next((base + h * 3600 for h in range(24) if local_day(datetime.fromtimestamp(
        base + h * 3600, UTC)) != datetime.fromtimestamp(base + h * 3600, UTC).date()
        .isoformat()), None)
    if t is None:
        pytest.skip("this time zone's Keepfold day always matches the UTC date")
    want = local_day(datetime.fromtimestamp(t, UTC))
    assert importers.note_item("a.md", 2, t, lambda: b"hi").date == want
    assert more_sources._day(t) == want


def test_imported_notes_keep_their_aliases(root_plain, tmp_path, home, monkeypatch):
    """R13d-2: a note's `aliases:` (a list or one string) reach its page and name it in find;
    a bad value is ignored; a private note's alias stays in its private page; a changed alias
    updates the page."""
    from conftest import FakeEmbed

    from keepfold import semantic
    from keepfold.mcp_server import Keepfold
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    src = tmp_path / "MyNotes"
    src.mkdir()
    (src / "Campus.md").write_text("---\naliases: [NWTU, ' Northwest campus ', '']\n---\n"
                                   "Hostel and mess notes.\n", "utf-8")
    (src / "Lab.md").write_text("---\naliases: Kandi lab\n---\nLab bench notes.\n", "utf-8")
    (src / "Bad.md").write_text("---\naliases: {a: 1}\n---\nOdd front matter.\n", "utf-8")
    (src / "Mixed.md").write_text("---\naliases: [ok, 3]\n---\nMixed list.\n", "utf-8")
    (src / "Long.md").write_text("---\naliases: ['" + "x" * 81 + "', fine]\n---\nLong.\n",
                                 "utf-8")
    (src / "Secret.md").write_text("---\naliases: [Zorblat]\nprivate: true\n---\nHidden.\n",
                                   "utf-8")
    v = Vault(root_plain)
    importers.import_notes(v, src, "obsidian")
    assert v.read("resource", "obsidian-campus").fields["aliases"] == ["NWTU",
                                                                       "Northwest campus"]
    assert v.read("resource", "obsidian-lab").fields["aliases"] == ["Kandi lab"]
    assert "aliases" not in v.read("resource", "obsidian-bad").fields
    assert "aliases" not in v.read("resource", "obsidian-mixed").fields
    assert v.read("resource", "obsidian-long").fields["aliases"] == ["fine"]
    secret = v.read("resource", "obsidian-secret")
    assert secret.fields["private"] is True and secret.fields["aliases"] == ["Zorblat"]
    assert "aliases" not in secret.body
    c = Keepfold(v)
    assert [h["id"] for h in c.keepfold_find("NWTU")["hits"]][:1] == ["obsidian-campus"]
    assert [h["id"] for h in c.keepfold_find("Kandi")["hits"]][:1] == ["obsidian-lab"]
    assert "obsidian-secret" not in json.dumps(c.keepfold_find("Zorblat"))
    (src / "Campus.md").write_text("---\naliases: [Northwest Tech]\n---\n"
                                   "Hostel and mess notes.\n", "utf-8")
    assert importers.import_notes(v, src, "obsidian")["updated"] == 1
    assert v.read("resource", "obsidian-campus").fields["aliases"] == ["Northwest Tech"]
