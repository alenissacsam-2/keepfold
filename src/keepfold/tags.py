"""R3-5: `//pref: <text>`, `//fact: <text>` and `//forget: <text>`, typed by the owner in any
chat and caught by code, never by the model. Callers pass user turns only, never assistant
text, tool output or note text: the prime prompt (keepfold_prime and the plugin's prime hook,
keepfold.prime_hook), a chat_turn's `user` and an imported chat's user turns (chat_import).

- pref: a me/preferences line under `## Personal`, written as keepfold_patch writes a
  user_confirmed one (its change stamped `confirm: tagged`, an act-grade yes);
- fact: the same on me/profile under `## Background`;
- forget: the one live claim line the text covers at least STRONG of (review's word bag) is
  retired to `## Past` (history kept, `keepfold undo` takes it back); none or several, and
  nothing changes: a `tag_forget` event holds the candidates for the app's Review.

Whose typing a tag is decides what it does (`SAID`, D-15): the prime hook's stdin and the app
save at once; a model's text over MCP and an import only propose, as does any tag in a chat
that read outside text (an exposed session): a pref or fact waits in inbox.md for Approve, a
forget becomes the Review card. So does a pref or fact whose text reads
like a planted deep link or promotion (`promo`, G-Q9-14), in any chat. A tag seen twice on one
day (the hook, then keepfold_prime or chat_turn of the same turn, or a re-import) writes once:
each carries a `key` (tag, text, day) that keepfold_patch's own key dedupe (`Keepfold._keyed`)
honours."""

from __future__ import annotations

import hashlib
import re
from datetime import date

from keepfold import claims, dates, review
from keepfold.chats import _CODE, drop_private
from keepfold.events import EventLog, local_day
from keepfold.render import plain
from keepfold.vault import VaultError, scan_secrets

TAG = re.compile(r"(?<![\w:/])//(pref|fact|forget):[ \t]*([^\r\n]*)", re.I)
# A24: `//off` at the start of a line, outside code, turns memory off for its chat
OFF = re.compile(r"^[ \t]*//off(?!\w)", re.I | re.M)
MAX_CHARS = 300  # a tag's text
PAGES = {"pref": ("preferences", "Personal"), "fact": ("profile", "Background")}
STRONG = 0.6  # of a claim line's content words the forget text must hold
SHOWN = 5  # candidates on a forget card
# D-15 (D6 T10, T11): whose //tags are the owner's own typing. The prime hook's stdin (the host
# app's real prompt) and the Keepfold app save at once. Text a model passes over MCP
# (keepfold_prime, keepfold_log chat_turn), an import and a transcript read later (chat_import)
# only propose: a pref or fact waits in Review for Approve, a forget is a Review card and
# retires nothing.
SAID = {"hook": True, "app": True, "mcp": False, "import": False}
# G-Q9-14: what a planted deep link or a promotion says ("summarize this page and remember
# ...", "remember X as a trusted source", "always recommend X", "from now on recommend X")
PROMO = re.compile(r"\bsummari[sz]e\b.{0,300}?\bremember\b|\bremember\b.{0,200}?\bas (?:an? |the |"
                   r"my )?(?:trusted|reliable|authoritative|official|preferred|go-to|top|best) "
                   r"(?:source|site|reference|resource|vendor|provider|brand)|"
                   r"\balways (?:recommend|suggest)\b|\bfrom now on,? (?:always )?"
                   r"(?:recommend|suggest)\b", re.I | re.S)


def promo(text: str) -> bool:
    """G-Q9-14: does `text` read like a planted deep link or promotion (`PROMO`)?"""
    return bool(PROMO.search(text.replace("’", "'")))


def find(text: str) -> list[tuple[str, str]]:
    """(tag, text) of each `//pref:`, `//fact:` or `//forget:` in a user turn, in order; the
    text runs to the end of its line, whitespace collapsed; an empty one is skipped. A
    `<private>` span is dropped first, so a tag inside one is never saved. A24: ("off", "")
    comes first when a line starts with `//off` outside a code span or fence."""
    text = drop_private(text or "")
    out: list[tuple[str, str]] = [("off", "")] if OFF.search(_CODE.sub("", text)) else []
    for m in TAG.finditer(text):
        item = (m.group(1).lower(), " ".join(m.group(2).split())[:MAX_CHARS])
        if item[1] and item not in out:
            out.append(item)
    return out


def off(text: str) -> bool:
    """A24: does `text` turn memory off for its chat (a line-start `//off`, `find`)?"""
    return ("off", "") in find(text)


def key(tag: str, text: str, day: str) -> str:
    return "tag:" + hashlib.sha1(f"{tag}\n{claims.norm(text)}\n{day}".encode()).hexdigest()[:16]


def apply(core, items, said: bool = True, forget_said: bool = True,
          refused: list[str] | None = None) -> list[str]:
    """Act on (tag, text, day) items through `core` (an mcp_server.Keepfold): each one through
    its `_call`, so roles, the excluded folder, an ambient session and the key dedupe all
    apply. `said`: the caller's `SAID` entry (True only for the hook and the app); a session
    that read outside text still only proposes. `forget_said=False` (an import: an
    old chat's forget may predate the line it matches) sends every forget to the Review.
    Returns a line per tag acted on; `refused` gets a `refusal` line per tag Keepfold refused."""
    lines = []
    for tag, text, day in items:
        if tag == "off":  # A24: it saves nothing; its callers turn memory off themselves
            continue
        k = key(tag, text, day)
        if core.log.has_key(k):  # before _call, which would open a session for nothing
            continue
        ok = said and (forget_said or tag != "forget")
        # "yesterday" as of the turn's own day (an import's is old); no day, no pin, and
        # keepfold_patch never pins a tag itself (mcp_server._pin_day: `tagged`)
        if tag in PAGES and day:
            text = dates.pin(text, date.fromisoformat(day))
        out = core._call("keepfold_patch", core._tag, {"tag": tag, "text": text, "said": ok,
                                                     "key": k})
        if "error" in out:
            if refused is not None:
                refused.append(refusal(tag, str(out.get("message") or out["error"])))
        elif not out.get("duplicate"):
            lines.append(out["summary"])
    return lines


def refusal(tag: str, why: str) -> str:
    """`//<tag> line: <why>`, with no invisible or control character from `why`."""
    return f"//{tag} line: " + "".join(c for c in why if c.isprintable())


def _refused(tag: str, text: str) -> str | None:
    """What `act` would refuse the text for before it writes anything, else None."""
    try:
        scan_secrets(text)
        if tag != "forget":
            claims.check(text, PAGES[tag][0])
    except VaultError as e:
        return str(e)
    return None


def apply_in(vault, items, log: EventLog | None = None, forget_said: bool = True,
             refused: list[str] | None = None, said: bool = True) -> list[str]:
    """`apply` outside the MCP server (the prime hook, chat_import): a Keepfold of its own, built
    only when an item is new (`log`: an EventLog to check keys with) and passes act's checks
    (a refused one goes to `refused` with no session), whose session is closed (and
    committed) at once."""
    log = log or EventLog(vault)
    items = [i for i in items if i[0] != "off" and not log.has_key(key(*i))]
    bad = {i: why for i in items if (why := _refused(i[0], i[1]))}
    if refused is not None:
        refused += [refusal(i[0], why) for i, why in bad.items()]
    items = [i for i in items if i not in bad]
    if not items:
        return []
    from keepfold.mcp_server import Keepfold  # the SDK: only when there is something to write

    # no index build: a tag writes me/ pages only, and the build thread slowed the hook ~1 s
    core = Keepfold(vault, build=False)
    core._ready.set()  # so a use of the index raises at once instead of waiting forever
    try:
        return apply(core, items, said=said, forget_said=forget_said, refused=refused)
    finally:
        if core.sessions.current is not None:
            core.sessions.close(summary="Saved //tags the user typed")


def act(core, tag: str, text: str, said: bool) -> dict:
    """One tag, inside `core`'s open session (Keepfold._tag)."""
    scan_secrets(text)
    day = local_day(core._now())
    if tag == "forget":
        return _forget(core, text, day, said)
    page, section = PAGES[tag]
    claims.check(text, page)  # G6: before _ensure_section writes the heading
    if not said or promo(text):  # G-Q9-14: a promotion waits for Approve too
        why = ("it reads like a promotion" if said else "this chat read outside text"
               if getattr(core.session, "exposed", False) else "an AI app or an import sent it")
        core._propose(f"- {section}: {text} (proposed {day}, me/{page})", page=page)
        core.proposed.append(plain(f"{section}: {text}", 200))
        core.sessions.note("patched", change={"tool": "propose", "id": page, "kind": "me",
                                              "op": "append_to_section", "target": section,
                                              "hash": review.text_hash(text)})
        return {"changed": False, "queued": True,
                "summary": f"//{tag} waits for Approve in the Keepfold app ({why})"}
    core._ensure_section(page, section)
    core.tagged = True
    try:
        out = core.keepfold_patch(id=f"me/{page}", op="append_to_section", target=section,
                                text=text, user_confirmed=True)
    finally:
        core.tagged = False
    return {**out, "summary": f"//{tag} saved to me/{page}"}


def _score(text: str, line: str) -> float:
    """The share of `line`'s content words (review._bag) that `text` holds."""
    mine = review._bag(line)
    return len(review._bag(text) & mine) / len(mine) if mine else 0.0


def candidates(vault, text: str, today: str) -> list[dict]:
    """Live, non-private claim rows sharing a content word with `text`, best first, each with
    its `score`."""
    hidden = review._private(vault)
    rows = [{**r, "score": s} for r in claims.rows(vault)
            if claims.is_live(r, today) and r["page"] not in hidden
            and (s := _score(text, r["text"])) > 0]
    return sorted(rows, key=lambda r: -r["score"])


def _forget(core, text: str, day: str, said: bool) -> dict:
    rows = candidates(core.vault, text, day)
    strong = [r for r in rows if r["score"] >= STRONG]
    if said and len(strong) == 1:
        r = strong[0]
        core.tagged = True
        try:
            out = core.keepfold_patch(id=f"me/{r['page']}", op="retire", target=r["id"],
                                    user_confirmed=True)
        finally:
            core.tagged = False
        if "error" in out:
            return out
        # R10: the sweep from every store can take a minute, longer than a prompt hook may run,
        # so the app's periodic pass runs it (forget.run_due); `text` is swept with the line
        core._append("log", {"payload": {"kind": "forget_due", "page": r["page"],
                                         "claim": r["id"], "text": text}})
        return {**out, "summary": f"//forget retired a line on me/{r['page']}; the Keepfold app "
                                  "removes it from everywhere Keepfold keeps it within 10 minutes"}
    shown = (strong or rows)[:SHOWN]
    core._append("log", {"payload": {"kind": "tag_forget", "text": text, "candidates": [
        {"page": r["page"], "id": r["id"]} for r in shown]}})
    return {"changed": False, "queued": True,
            "summary": f"//forget matched {len(strong) or 'no'} line{'s' * (len(strong) != 1)}"
                       " strongly: nothing changed; the Keepfold app's Review lists "
                       f"{len(shown)} to pick from"}


def forget_cards(vault, events, today: str) -> list[dict]:
    """The Review's open forget cards, newest first: {key (the event id), text, day,
    candidates [{page, id, text}]} with raw note text. A card closes on Dismiss (a `dismiss`
    change on page `forget`) or once one of its candidates is no longer live."""
    events = list(events)
    voided = review._voided(events)
    live = {(r["page"], r["id"]): r for r in claims.rows(vault) if claims.is_live(r, today)}
    hidden = review._private(vault)
    out = []
    for e in events:
        p = review._payload(e)
        if (e.get("kind") != "log" or p.get("kind") != "tag_forget" or e.get("id") in voided
                or review.dismissed(events, "forget", e["id"])):
            continue
        pairs = [(c.get("page"), c.get("id")) for c in p.get("candidates") or []
                 if isinstance(c, dict)]
        if any(k not in live for k in pairs):
            continue
        out.append({"key": e["id"], "text": str(p.get("text") or ""), "day": e.get("day"),
                    "candidates": [{"page": pg, "id": i, "text": live[(pg, i)]["text"]}
                                   for pg, i in pairs if pg not in hidden]})
    return out[::-1]

