"""Names the route mixins share with `server.py` (which imports them back, so `server.Busy`,
`server.NotFound` and the others stay where callers and tests look for them)."""

from __future__ import annotations

import re
from pathlib import Path

from keepfold.app import status, wizard

DRY_IDLE_S = 600  # X-ray's shared dry-run index goes after 10 unused minutes, as a model does

CLOUD_FIELDS = ("cloud_processing", "provider", "model", "base_url", "api_key_env")

IMPORTS = ("claude", "chatgpt", "gemini", "perplexity", "google-activity", "notes", "calendar",
           "docs", "repos", "highlights", "mail", "selfchat", "location",
           "photos", "spotify", "voice-memos", "anki")

CAPTURE_MAX = 1000  # quick capture: a line's characters

URL_ONLY = re.compile(r"https?://\S+", re.I)  # a quick-capture line that is one link

PAGE_ROWS = 20  # the private chats list, a page at a time


class Busy(Exception):
    pass


class NotFound(Exception):  # a 404: the item does not exist, or may not be shown now
    pass


def _connect_skipped() -> bool:
    app = status.config().get("app")
    return isinstance(app, dict) and app.get("connect_skipped") is True


def _bool(body: dict, key: str) -> bool:
    v = body.get(key)
    if not isinstance(v, bool):
        raise ValueError(f"{key} must be true or false")
    return v


def _disk(body: dict, where: Path, need: int) -> dict | None:
    """{"disk": message} when a download of `need` bytes would leave less than 1 GB free in
    `where` and the owner has not chosen to continue anyway (`override`), else None."""
    ok, msg = wizard.free_space_ok(where, need)
    return None if ok or body.get("override") is True else {"disk": msg}


def _domains(v, key: str) -> list[str]:
    if isinstance(v, str):
        v = v.replace(",", "\n").split()
    if not isinstance(v, list) or len(v) > 1000:
        raise ValueError(f"{key} must be a list of domains")
    out = []
    for d in v:
        d = str(d).strip().lower().lstrip(".")
        if d and not (len(d) <= 253 and all(c.isalnum() or c in ".-" for c in d)):
            raise ValueError(f"{d!r} is not a domain such as example.com")
        if d:
            out.append(d)
    return sorted(set(out))


def _checkout() -> Path:
    from keepfold import cli
    return cli.CHECKOUT


def skill_url() -> str:
    """The Chat skill attached to the latest release (installer/build.py makes it)."""
    from keepfold import cli
    return f"{cli.RELEASES}/latest/download/keepfold-skill.zip"
