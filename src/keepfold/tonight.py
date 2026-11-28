"""The Keepfold app's Tonight tab (Gemini round 3 "Nightly Closeout"), read-only: the current
Keepfold day's pulse (24 hours from 04:00), what AI apps saved today grouped by their session,
and the concepts due tomorrow. Every text is shown text (`render.plain`); private pages,
hidden subjects and private browse pages are never named."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta, tzinfo

from keepfold import planner
from keepfold.access import Audience, audience_of_note
from keepfold.browse.forget import PRIVATE_IGNORE
from keepfold.catalog import Catalog
from keepfold.events import DAY_ROLLOVER, KINDS, local_day, parse_ts
from keepfold.render import plain
from keepfold.review import _payload, _private

START = int(DAY_ROLLOVER.total_seconds() // 3600)  # the Keepfold day starts at 04:00
LEARNED_MAX = 20
TOMORROW_MAX = 3
SOURCES_MAX = 5
IMPORTED = {"imported", "safety_net", "night"}  # event sources the app's own imports stamp
OWN_CLIENTS = {"unknown", "keepfold-app"}  # not an AI app: the app's own 90-second check
HARVEST = "Keepfold, from your chats"  # a fact_harvest pass's row (its session's mode)


def hour_of(when: datetime, tz: tzinfo | None = None) -> int:
    """0-23: the hour of the Keepfold day `when` falls in (0 is 04:00-05:00 local)."""
    return (when.astimezone(tz).hour - START) % 24


def levels(counts: list[int]) -> list[int]:
    """0 for an empty hour; else 1-3 (low, mid, high) by the quartiles of the non-zero hours:
    above the lower quartile is mid, above the upper one high."""
    nz = sorted(n for n in counts if n)
    if not nz:
        return [0] * len(counts)
    q1, q3 = nz[(len(nz) - 1) // 4], nz[3 * (len(nz) - 1) // 4]
    return [0 if not n else 1 + (n > q1) + (n > q3) for n in counts]


def clients(events) -> dict[str, str]:
    """session -> its AI app's name: the `client` its open event carries (server-stamped
    from the MCP handshake), else a grade's `grader.client`. App clicks and the CLI have
    none and the app's own check is `keepfold-app`, so they are never an AI app's session. A
    fact_harvest pass (mode `harvest`) is HARVEST, so Tonight lists it with its Undo."""
    out: dict[str, str] = {}
    for e in events:
        s = e.get("session")
        if e.get("kind") == "open":
            c = HARVEST if e.get("mode") == "harvest" else _payload(e).get("client")
        else:
            c = e["grader"].get("client") if isinstance(e.get("grader"), dict) else None
        if (isinstance(s, str) and s.startswith("s_") and s not in out and isinstance(c, str)
                and c.strip() and c not in OWN_CLIENTS):
            out[s] = plain(c, 60)
    return out


def chat_turns(day: str, tz: tzinfo | None = None) -> list[datetime]:
    """The times of today's user turns in the live chat logs (`chats.log_live`, outside the
    vault). Counts only: no text leaves here."""
    from keepfold import chats
    folder = chats.export_root() / chats.LIVE
    since = datetime.fromisoformat(day).replace(tzinfo=UTC) - timedelta(days=1)  # any zone
    out = []
    for p in folder.glob("*.jsonl") if folder.is_dir() else ():
        try:
            if datetime.fromtimestamp(p.stat().st_mtime, UTC) < since:
                continue
            lines = p.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                x = json.loads(line)
                when = datetime.fromisoformat(x["timestamp"])
            except (ValueError, KeyError, TypeError):
                continue
            if x.get("type") == "user" and when.tzinfo and local_day(when, tz) == day:
                out.append(when)
    return out


def _what(e: dict) -> str | None:
    """What a pulse event is: added, changed, import, touch; None for anything else."""
    p = _payload(e)
    if e.get("kind") == "change":
        return {"added": "added", "patched": "changed"}.get(p.get("note"))
    if e.get("session") == "import" or e.get("source") in IMPORTED:
        return "import"
    if e.get("kind") == "log" and p.get("kind") == "touch":
        return "touch"
    return None


def _source(e: dict, apps: dict) -> str:
    s = e.get("session")
    if s in apps:
        return apps[s]
    if s == "import" or e.get("source") in IMPORTED:
        return plain(str(_payload(e).get("source") or e.get("source")), 40)
    return "Keepfold CLI" if s == "cli" else "Keepfold app"


def _shown(vault, c: dict, hide: set[str], me_private: set[str], lines: dict) -> tuple | None:
    """(text, page id) for an AI app's change, or None when its page is private, hidden,
    a private browse page or gone."""
    kind, nid = c.get("kind"), c.get("id")
    if not isinstance(kind, str) or not isinstance(nid, str):
        return None
    if kind == "me":
        if nid in me_private:
            return None
        cid = c.get("claim")
        cid = cid[0] if isinstance(cid, list) and cid else cid
        return plain(lines.get((nid, cid)) or str(c.get("target") or nid)), f"me/{nid}"
    try:
        note = vault.read(kind, nid)
    except Exception:  # noqa: BLE001  (an unknown kind, a deleted or unparsable page)
        return None
    rel = note.path.relative_to(vault.root).as_posix()
    if rel.startswith(PRIVATE_IGNORE) or audience_of_note(note, hide) is not Audience.CLOUD:
        return None
    return plain(str(note.fields.get("title") or nid)), nid


def view(vault, events: list[dict], now: datetime, tz: tzinfo | None = None) -> dict:
    """Everything the Tonight tab shows for the Keepfold day of `now` (`tz`: the local zone,
    the system's by default)."""
    from keepfold import claims, dashboard
    day = local_day(now, tz)
    todays = [e for e in events if e.get("day") == day]
    apps = clients(events)
    turns = chat_turns(day, tz)
    counts, hours, sources = Counter(), [0] * 24, Counter()
    for e in todays:
        if e.get("session") in apps and e.get("kind") in KINDS - {"open", "close", "resume"}:
            counts["recorded"] += 1
        if (w := _what(e)) is None:
            continue
        counts[w] += 1
        p = _payload(e)
        if w == "import" and p.get("kind") == "chat_saved":  # each saved chat is a page added
            counts["added"] += sum(isinstance(i, str) for i in p.get("pages") or [])
        hours[hour_of(parse_ts(e["ts"]), tz)] += 1
        sources[_source(e, apps)] += 1
    for t in turns:
        hours[hour_of(t, tz)] += 1
    if turns:
        from keepfold import chats
        sources[chats.LIVE] += len(turns)
    lv = levels(hours)
    pulse = [{"from": f"{(START + i) % 24:02d}:00", "to": f"{(START + i + 1) % 24:02d}:00",
              "n": n, "level": lv[i]} for i, n in enumerate(hours)]

    cat = Catalog.load(vault)
    hide = planner.hidden(vault, cat)
    me_private = _private(vault)
    lines = {(r["page"], r["id"]): r["text"] for r in claims.rows(vault)}
    voided = {e.get("ref") for e in events if e.get("kind") == "void"}
    groups: dict[str, dict] = {}
    rows = 0
    for e in reversed(todays):  # newest first
        c = _payload(e).get("change") if e.get("kind") == "change" else None
        if (rows >= LEARNED_MAX or not isinstance(c, dict) or e.get("session") not in apps
                or e.get("id") in voided or _payload(e).get("note") not in ("added", "patched")):
            continue
        if (shown := _shown(vault, c, hide, me_private, lines)) is None:
            continue
        g = groups.setdefault(e["session"], {"session": e["session"],
                                             "client": apps[e["session"]], "rows": []})
        g["rows"].append({"text": shown[0], "id": shown[1],
                          "time": parse_ts(e["ts"]).astimezone(tz).strftime("%H:%M")})
        rows += 1

    tomorrow = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    states, _ = planner.current_states(vault, day, cat, events)
    due = [c for c, st in states.items() if c in cat.entries and c not in hide
           and st.next_due and str(st.next_due)[:10] == tomorrow]

    def title(i: str | None) -> str:
        e = cat.entries.get(i) if i else None
        return plain(e.title if e else (i or ""))

    soon = sorted(({"id": c, "title": title(c), "subject": title(planner._subject(cat, c))}
                   for c in due), key=lambda x: (x["subject"], x["title"]))
    return {"day": day, "hour": hour_of(now, tz), "pulse": pulse,
            "counts": {"added": counts["added"], "changed": counts["changed"],
                       "chat_turns": len(turns), "recorded": counts["recorded"]},
            "sources": [{"source": s, "n": n} for s, n in sources.most_common(SOURCES_MAX)],
            "learned": list(groups.values()), "tomorrow": soon[:TOMORROW_MAX],
            "tomorrow_day": tomorrow, "studies": dashboard.studies(vault, events)}
