"""The setup window's pure parts (KeepfoldSetup.exe, installer/setup_app.py): the step lists,
progress from the log lines install() says and uv prints, and the in-use list in plain words.
Stdlib only, no Tk, so tests run them without a window."""

from __future__ import annotations

import re
from pathlib import PureWindowsPath
from typing import NamedTuple

# where each step sits on the one progress bar: (start, end) as fractions of the whole
SPANS = {
    "update": {"wait": (0, .05), "close": (.05, .08), "install": (.08, .92),
               "finish": (.92, .98), "open": (.98, 1)},
    "install": {"folder": (0, 0), "install": (0, .92), "finish": (.92, 1), "done": (1, 1)},
}


class State(NamedTuple):
    step: str  # the running step's key (SPANS)
    fraction: float  # the whole bar, 0..1
    status: str  # the line under the bar
    mode: str = "install"  # or "update"
    version: str = ""
    packages: int = 0  # uv's "Resolved N packages"
    started: int = 0  # uv's "Downloading X" lines, after Resolved
    downloaded: int = 0  # and its " Downloaded X" lines


def keepfold(version: str) -> str:
    return f"Keepfold {version}" if version else "Keepfold"


def steps(mode: str, version: str = "") -> list[tuple[str, str]]:
    """(key, label) of each row; the update's "close" row shows only when programs must
    close."""
    v = keepfold(version)
    if mode == "update":
        return [("wait", "Waiting for Keepfold to close"),
                ("close", "Closing programs that use Keepfold"),
                ("install", f"Installing {v}"), ("finish", "Finishing up"),
                ("open", "Opening Keepfold")]
    return [("folder", "Choose a folder"),
            ("install", f"Installing {v} (downloads Python and packages the first time)"),
            ("finish", "Finishing up"), ("done", "Done")]


def start(mode: str, version: str = "") -> State:
    if mode == "update":
        return State("wait", 0.0, "Waiting for Keepfold to close…", mode, version)
    return State("folder", 0.0, "Choose where to install Keepfold, then click Install.", mode,
                 version)


def _n(text: str) -> int:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else 0


def progress(s: State, line: str) -> State:
    """The state after one log line: the step and the bar never move backwards."""
    t, v, to = line.strip(), keepfold(s.version), s.version or "the new version"
    new: dict = {}
    sub = None  # how far into the (new) step, 0..1
    if t.startswith("waiting for process"):
        new, sub = {"step": "wait", "status": "Waiting for Keepfold to close…"}, 0
    elif t.startswith("closing "):
        new, sub = {"step": "close", "status": "Closing programs that use Keepfold…"}, .5
    elif t.startswith("Installing Keepfold "):
        new, sub = {"step": "install", "status": (
            f"Installing {v}…" if s.mode == "update" else
            "Downloading Python 3.14 and packages (first install only)…")}, 0
    elif t.startswith("running: "):
        sub = .02
    elif t.startswith("Downloading cpython"):
        sub = .05
    elif t.startswith("Downloaded cpython"):
        sub = .2
    elif t.startswith("Installed Python"):
        sub = .25
    elif t.startswith("Resolved "):
        n = _n(t)
        new, sub = {"packages": n, "status": f"Installing {n} packages…"}, .3
    elif t.startswith("Downloading ") and s.packages:
        new = {"started": s.started + 1}
        sub = .3 + .4 * s.downloaded / (s.started + 1)
    elif t.startswith("Downloaded ") and s.packages:
        done = s.downloaded + 1
        new, sub = {"downloaded": done}, .3 + .4 * done / max(s.started, done)
    elif t.startswith("Prepared "):
        sub = .7
    elif t.startswith("Uninstalled "):
        sub = .72
    elif re.match(r"Installed \d+ packages? ", t):
        new, sub = {"status": "Getting Keepfold ready for a fast first start…"}, .8
    elif t.startswith("Bytecode compiled"):
        sub = .95
    elif re.match(r"Installed \d+ executables?", t):
        sub = 1
    elif t.startswith("app: "):
        new, sub = {"step": "finish", "status": "Making shortcuts…"}, .3
    elif t.startswith("added to your PATH"):
        sub = .5
    elif t.startswith("shortcut: "):
        sub = .6
    elif t.startswith("added to Apps & Features"):
        sub = .9
    elif t.startswith("Keepfold is updated"):
        new, sub = {"step": "open", "status": f"Keepfold is updated to {to}. Opening it…"}, 0
    elif t == "opened the Keepfold app":
        new, sub = {"step": "open", "status": (
            f"Keepfold is updated to {to}. Restart Claude and your other AI apps so they "
            "reconnect.")}, 1
    elif t == "Keepfold is installed.":
        new, sub = {"step": "done", "status": "Keepfold is installed."}, 1
    order = list(SPANS[s.mode])
    if new.get("step", s.step) not in order:  # install has no "close" row: the words only
        new.pop("step")
        sub = None
    elif order.index(new.get("step", s.step)) < order.index(s.step):
        return s  # a late line from an earlier step changes nothing
    s = s._replace(**new)
    if sub is not None:
        lo, hi = SPANS[s.mode][s.step]
        s = s._replace(fraction=max(s.fraction, lo + (hi - lo) * sub))
    return s


def elapsed(seconds: float) -> str:
    m, sec = divmod(int(seconds), 60)
    return f"{m} min {sec} s" if m else f"{sec} s"


# ---- the programs that keep Keepfold in use -------------------------------------------------

CONNECTION, WINDOW, RECEIVER, OTHER = ("Claude's Keepfold connection", "The Keepfold window",
                                       "The browsing receiver", "Other Keepfold programs")


def kind(path: str) -> str:
    p = PureWindowsPath(path)
    name, parent = p.name.lower(), p.parent.name.lower()
    if name.startswith("cortex"):  # a Cortex (0.6.1 and older) install's programs
        name = "keepfold" + name[len("cortex"):]
    if name == "keepfold-receiver.exe":
        return RECEIVER
    if name == "keepfold-app.exe" or (name == "keepfold.exe" and parent == "app"):
        return WINDOW  # app\Keepfold.exe, or the older bin\keepfold-app.exe launcher
    if name == "keepfold.exe":
        return CONNECTION  # bin\keepfold.exe: `keepfold mcp`, which AI apps start
    return OTHER


def group_in_use(busy: list[tuple[int, str]], parents: dict[int, int]) -> list[str]:
    """`in_use`'s programs in plain words, e.g. "Claude's Keepfold connection (2)": a busy
    process started by another busy one (keepfold.exe's python.exe) counts with its parent."""
    pids = {pid for pid, _ in busy}
    counts: dict[str, int] = {}
    for pid, path in busy:
        if parents.get(pid) not in pids or parents.get(pid) == pid:
            counts[kind(path)] = counts.get(kind(path), 0) + 1
    return ([f"{k} ({counts[k]})" for k in (CONNECTION, WINDOW, RECEIVER, OTHER) if k in counts]
            or ([f"{OTHER} ({len(busy)})"] if busy else []))  # a pid-reuse loop: still listed
