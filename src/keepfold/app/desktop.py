"""The desktop app (`keepfold-app`): the app page in its own window, with a tray icon.

Threads. pywebview's GUI (WinForms with Edge WebView2, Cocoa, GTK) must own the main thread;
pystray's Windows icon runs its own message loop on any thread:
- main: `webview.start()`, the window's GUI loop, until the window is destroyed; on a Mac (the
  menu-bar icon) and Linux (AppIndicator) it also drives the tray icon (`run_detached`);
- keepfold-app-http: the app server (`server.start` adds its auto-import and watch threads; the
  desktop app has no idle stop);
- keepfold-tray: the tray icon (Open Keepfold, Start with Windows, Quit) and its notifications,
  Windows only;
- keepfold-notify: the once-a-day notifications (reviews due, an exam in 3 days or tomorrow);
- keepfold-hotkey: quick capture's global hotkey (`Hotkey`, Windows), a GetMessageW loop that
  shows the capture box (a second, hidden window) on each press; Quit posts it WM_QUIT. A Mac's
  `MacHotkey` (Carbon) runs on the main thread's loop instead; GNOME's (`GnomeHotkey`) is a
  system shortcut running `keepfold capture`;
- keepfold-end: waits for `app.stop` (Quit in the tray or on the page, an update) and destroys
  the window, which ends the main thread's GUI loop; `run` then stops the rest;
- keepfold-smoke: only with `--smoke` (the release build's check, `Desktop.smoke`).
Closing the window only hides it, and its page sleeps a minute later (`Desktop.hide`). With no
tray (Linux without a StatusNotifierWatcher) the tray's items are in the window's menu and
closing it quits. Logging out or shutting down quits (`_shutting_down` on Windows,
`_watch_logout` on Mac and Linux). Without pywebview or the WebView2 runtime the app opens in
the browser, as `keepfold app` does.
"""

from __future__ import annotations

import ast
import ctypes
import json
import os
import shlex
import sys
import threading
import traceback
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from keepfold import atomic
from keepfold.app import server, status

AUTOSTART = "Keepfold"  # the HKCU Run value: `"<Keepfold.exe>" -m keepfold.app.launch --hidden`
AUTOSTART_ID = "io.github.alenissacsam-2.keepfold"  # the app's LaunchAgent label (bundle id)
DESKTOP_ID = "io.github.alenissacsam_2.keepfold"  # its .desktop name (no "-" after a dot)
SM_ENABLED, SM_APPROVAL = 1, 2  # SMAppServiceStatusEnabled, ...RequiresApproval
APPROVE = "Allow Keepfold in System Settings > General > Login Items to start it at login."
# the Mac launch Apple Event's keyAEPropData is keyAELaunchedAsLogInItem at a login start
AE_PROP, AE_LOGIN = int.from_bytes(b"prop", "big"), int.from_bytes(b"lgit", "big")
# the AppUserModelID, also on the shortcuts: pins and groups as Keepfold
APP_ID = "Keepfold.Learner"
ICON = Path(__file__).resolve().parent.parent / "assets" / "keepfold.ico"  # scripts/make_icon.py
# Linux's window icon: GTK's gdk-pixbuf cannot read the .ico's PNG-compressed sizes, and a
# window whose icon fails to load is never made (trial build 2's blank, hung GUI smoke test)
ICON_PNG = ICON.with_suffix(".png")
SIZE, MIN_SIZE = (1200, 800), (640, 480)
AMBER, GROUND = "#ffb000", "#0b0b0a"  # app.css's night --lit and --ground
NOTIFY = ("reviews", "exams", "jobs", "updates")  # app.notify in config.yaml, all on by default
EXAM_DAYS = (3, 1)
FIRST_S, EVERY_S = 60, 30 * 60  # the first daily check after start, then every 30 minutes
KEEP_DAYS = 40  # a sent notification's key is remembered this long
SM_SHUTTINGDOWN = 0x2000
GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x80
SLEEP_S = 60  # this long in the tray, the page sleeps (WebView2's TrySuspendAsync)
# quick capture's global hotkey (app.hotkey in config.yaml): choice -> (modifiers, key, label)
MOD_ALT, MOD_CONTROL, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x8, 0x4000
VK_SPACE, VK_K = 0x20, 0x4B
WM_QUIT, WM_HOTKEY, WM_APP = 0x12, 0x312, 0x8000  # WM_APP: app.hotkey changed, register again
HOTKEYS = {"ctrl+alt+space": (MOD_CONTROL | MOD_ALT, VK_SPACE, "Ctrl+Alt+Space"),
           "win+alt+space": (MOD_WIN | MOD_ALT, VK_SPACE, "Win+Alt+Space"),
           "ctrl+alt+k": (MOD_CONTROL | MOD_ALT, VK_K, "Ctrl+Alt+K"),
           "off": None}
HOTKEY_DEFAULT = "ctrl+alt+space"
# the same choices on a Mac (Carbon: modifiers, virtual key code, label) and on GNOME (an
# accelerator for its custom keybinding, label)
MAC_CMD, MAC_SHIFT, MAC_OPTION, MAC_CONTROL = 0x100, 0x200, 0x800, 0x1000
KVK_SPACE, KVK_K = 0x31, 0x28
KEYB, HOTKEY_PRESSED = int.from_bytes(b"keyb", "big"), 5  # kEventClassKeyboard, ...HotKeyPressed
MAC_HOTKEYS = {"ctrl+alt+space": (MAC_CONTROL | MAC_OPTION, KVK_SPACE, "Ctrl+Option+Space"),
               # not Cmd+Option+Space: a stock Mac opens a Finder search window with it
               "win+alt+space": (MAC_CMD | MAC_SHIFT, KVK_SPACE, "Cmd+Shift+Space"),
               "ctrl+alt+k": (MAC_CONTROL | MAC_OPTION, KVK_K, "Ctrl+Option+K"),
               "off": None}
GNOME_HOTKEYS = {"ctrl+alt+space": ("<Control><Alt>space", "Ctrl+Alt+Space"),
                 "win+alt+space": ("<Super><Alt>space", "Super+Alt+Space"),
                 "ctrl+alt+k": ("<Control><Alt>k", "Ctrl+Alt+K"),
                 "off": None}
GNOME_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GNOME_KB = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/keepfold/"
HOTKEY_NOTE = ("The quick capture key works on GNOME. On this desktop, add a keyboard shortcut "
               "in its settings that runs: keepfold capture")
BOX = (560, 120)  # the quick-capture box, CSS px
# `--smoke` (release-apps.yml): wait this long for the page to load, then keep the window up this
# long for CI's screenshot
SMOKE_LOAD_S, SMOKE_SHOW_S = 60, 15
FIRST_LOAD_S = 60  # Mac/Linux: a shown start whose page has not loaded by then uses the browser
SMOKE_DEADLINE_S = 180  # a check stuck past this dumps every thread's stack and exits 1

_lock = threading.Lock()


# ---- ~/.keepfold/desktop.json: the window's place and the notifications already sent ----------


def state_file() -> Path:
    return Path.home() / ".keepfold" / "desktop.json"


def load_state() -> dict:
    try:
        d = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_state(key: str, value) -> None:
    """Set one key of desktop.json (atomic.replace; no fsync: saved on every window move)."""
    with _lock:
        d = {**load_state(), key: value}
        p = state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic.replace(p, json.dumps(d), fsync=False, newline=None)


def _app_cfg() -> dict:
    a = status.config().get("app")
    return a if isinstance(a, dict) else {}


def _merge_app(fn) -> None:
    """`app:` in config.yaml becomes fn(app) (its other keys kept), under the config lock."""
    from keepfold import taint
    taint.write_config(lambda cfg: {"app": fn(
        cfg["app"] if isinstance(cfg.get("app"), dict) else {})})


# ---- Start with Windows -----------------------------------------------------------------------


def autostart_command() -> str:
    """`Keepfold.exe -m keepfold.app.launch --hidden` (installed), else `keepfold-app --hidden`
    (beside `keepfold`): Keepfold starts in the tray, window hidden."""
    from keepfold.app.launch import installed_app
    if app := installed_app():
        return f'"{app}" -m keepfold.app.launch --hidden'
    from keepfold import clients
    keepfold = Path(clients.keepfold_command())
    exe = keepfold.with_name("keepfold-app" + keepfold.suffix)
    if not exe.is_file():
        raise ValueError(f"{exe.name} is not beside {keepfold.name}; reinstall Keepfold to start "
                         "it with Windows.")
    return f'"{exe}" --hidden'


def autostart_label() -> str:
    """The setting's name, in the tray and on the page."""
    return "Start with Windows" if sys.platform == "win32" else "Start at login"


def autostart_file() -> Path | None:
    """Start at login on Linux (an XDG autostart entry) and the Mac's old LaunchAgent (removed:
    the Mac app is its own login item now, `_smapp`); None on Windows
    (the Run value) and elsewhere."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{AUTOSTART_ID}.plist"
    if sys.platform.startswith("linux"):
        xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return xdg / "autostart" / f"{DESKTOP_ID}.desktop"
    return None


def autostart_args() -> list[str]:
    """What the Linux login entry runs: the .deb's stable launcher, starting in the tray, never
    this version's own path (an update replaces it)."""
    from keepfold import update
    if update.mode() != "app":
        raise ValueError("Start at login comes with the Keepfold app.")
    from keepfold.app import bundle
    return [bundle.LINUX_APP.as_posix(), "--hidden"]


def _autostart_supported() -> bool:
    from keepfold import update
    return sys.platform == "win32" or (autostart_file() is not None and update.mode() == "app")


def _smapp():  # tests replace this: never the real login items in a test
    """The frozen Mac app as its own login item (SMAppService, macOS 13+). Spike S4: a
    LaunchAgent shows as "Unknown Developer" and notifies again after every update."""
    from ServiceManagement import SMAppService
    return SMAppService.mainAppService()


def autostart_state() -> dict:
    from keepfold.app import bundle
    if bundle._mac_app():
        st = _smapp().status()
        out = {"supported": True, "enabled": st == SM_ENABLED, "label": autostart_label()}
        return {**out, "note": APPROVE} if st == SM_APPROVAL else out
    on = _autostart_supported()
    if sys.platform == "win32":
        enabled = status.run_get(AUTOSTART) is not None
    else:
        enabled = on and autostart_file().is_file()
    return {"supported": on, "enabled": enabled, "label": autostart_label()}


def set_autostart(enabled: bool) -> dict:
    """Start with Windows (Start at login on Mac and Linux) on or off. Either choice counts as
    made, so the first-run default (`first_run`) never overrides it."""
    from keepfold.app import bundle
    if bundle._mac_app():
        svc = _smapp()
        ok, err = (svc.registerAndReturnError_ if enabled
                   else svc.unregisterAndReturnError_)(None)
        # registered but waiting for approval counts as on; autostart_state says where
        if not ok and (svc.status() in (SM_ENABLED, SM_APPROVAL)) != enabled:
            why = err.localizedDescription() if err is not None else "no reason given"
            raise ValueError(f"Start at login could not be turned {'on' if enabled else 'off'}:"
                             f" {why}")
        autostart_file().unlink(missing_ok=True)  # the LaunchAgent this replaces
    elif sys.platform == "win32":
        if enabled:
            status.run_set(autostart_command(), AUTOSTART)
        else:
            status.run_delete(AUTOSTART)
    elif (f := autostart_file()) is None:
        raise ValueError("Start at login is not offered on this system.")
    elif enabled:
        kind = f.suffix
        text = status.render_login(kind, AUTOSTART_ID if kind == ".plist" else "Keepfold",
                                   autostart_args())
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8", newline="\n")
    else:
        f.unlink(missing_ok=True)
    _merge_app(lambda a: {**a, "autostart_asked": True})
    return autostart_state()


def first_run(echo=print) -> None:
    """The desktop app's first start turns Start with Windows (Start at login) on, once: the
    owner's choice. Off Windows only the Keepfold app has it."""
    if not _autostart_supported() or _app_cfg().get("autostart_asked") is True:
        return
    try:
        set_autostart(True)
    except (OSError, ValueError) as e:  # tried again at the next start
        echo(f"Keepfold app: {autostart_label()} stays off ({e})")


# ---- notifications ----------------------------------------------------------------------------


def update_check() -> bool:
    """`app.update_check`: the quiet update check, on unless set to false."""
    return _app_cfg().get("update_check") is not False


def notify_settings() -> dict:
    n = _app_cfg().get("notify")
    n = n if isinstance(n, dict) else {}
    return {k: n.get(k) is not False for k in NOTIFY}


def save_notify(body: dict) -> dict:
    if not body or set(body) - set(NOTIFY) or not all(isinstance(v, bool) for v in body.values()):
        raise ValueError(f"send true or false for any of {', '.join(NOTIFY)}")

    def merge(a: dict) -> dict:
        old = a.get("notify") if isinstance(a.get("notify"), dict) else {}
        return {**a, "notify": {**old, **body}}
    _merge_app(merge)
    return notify_settings()


def hotkey_setting() -> str:
    """app.hotkey: one of HOTKEYS; a missing or unknown value is the default."""
    k = _app_cfg().get("hotkey")
    return k if isinstance(k, str) and k in HOTKEYS else HOTKEY_DEFAULT


def save_hotkey(body: dict) -> str:
    k = body.get("hotkey")
    if not isinstance(k, str) or k not in HOTKEYS:
        raise ValueError(f"hotkey must be one of {', '.join(HOTKEYS)}")
    _merge_app(lambda a: {**a, "hotkey": k})
    return k


def _user32():  # tests replace this: never a real global hotkey in a test
    import ctypes
    return ctypes.windll.user32


class Hotkey:
    """Quick capture's global hotkey, on its own thread `keepfold-hotkey`: RegisterHotKey delivers
    WM_HOTKEY to the thread that registered it, so that thread owns a GetMessageW loop. `change`
    posts WM_APP (register app.hotkey again), `stop` posts WM_QUIT; the loop then ends with
    UnregisterHotKey. A combination another app holds leaves `taken` set and nothing else."""

    ID = 1

    def __init__(self, on_press):
        self.on_press = on_press
        self.key: str | None = None  # the choice registered (or tried)
        self.taken = False
        self.on = False  # registered now
        self.tid: int | None = None
        self.done = threading.Event()  # set after each (re-)registration
        self.thread: threading.Thread | None = None

    def label(self) -> str | None:
        """"Ctrl+Alt+Space" while registered, else None (off, taken, not started)."""
        return HOTKEYS[self.key][2] if self.on and self.key else None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop, daemon=True, name="keepfold-hotkey")
        self.thread.start()
        self.done.wait(5)

    def _register(self, u) -> None:
        if self.on:
            u.UnregisterHotKey(None, self.ID)
            self.on = False
        self.key = hotkey_setting()
        spec = HOTKEYS[self.key]
        self.on = bool(spec) and bool(u.RegisterHotKey(None, self.ID, spec[0] | MOD_NOREPEAT,
                                                       spec[1]))
        self.taken = bool(spec) and not self.on
        self.done.set()

    def loop(self) -> None:
        import ctypes
        from ctypes import wintypes
        u = _user32()
        msg = wintypes.MSG()
        self.tid = threading.get_native_id()  # GetCurrentThreadId
        u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # makes the queue PostThreadMessage needs
        try:
            self._register(u)
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # 0: WM_QUIT, -1: error
                if msg.message == WM_HOTKEY:
                    try:
                        self.on_press()
                    except Exception:  # noqa: BLE001  (a failed show never ends the hotkey)
                        traceback.print_exc(limit=1)
                elif msg.message == WM_APP:
                    self._register(u)
        finally:
            if self.on:
                u.UnregisterHotKey(None, self.ID)
                self.on = False
            self.done.set()

    def _post(self, message: int) -> bool:
        if self.tid is None or self.thread is None or not self.thread.is_alive():
            return False
        return bool(_user32().PostThreadMessageW(self.tid, message, 0, 0))

    def change(self) -> None:
        """Register app.hotkey again at once; returns once it has (or after 2 s)."""
        self.done.clear()
        if self._post(WM_APP):
            self.done.wait(2)

    def stop(self) -> None:
        self._post(WM_QUIT)
        if self.thread is not None:
            self.thread.join(2)


def hotkey_label(k: str) -> str:
    """A choice's name on this OS ("Off" for off)."""
    table = (MAC_HOTKEYS if sys.platform == "darwin" else
             GNOME_HOTKEYS if sys.platform.startswith("linux") else HOTKEYS)
    return table[k][-1] if table[k] else "Off"


def _gnome() -> bool:
    return sys.platform.startswith("linux") and "GNOME" in os.environ.get(
        "XDG_CURRENT_DESKTOP", "").upper().split(":")


def _hotkey(on_press):
    """This OS's quick capture key: None on Linux desktops other than GNOME (HOTKEY_NOTE)."""
    if sys.platform == "win32":
        return Hotkey(on_press)
    if sys.platform == "darwin":
        return MacHotkey(on_press)
    return GnomeHotkey() if _gnome() else None


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


# OSStatus (*)(EventHandlerCallRef, EventRef, void *userData)
_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


def _carbon():  # tests replace this: never a real global hotkey in a test
    """Carbon's hotkey calls (HIToolbox), typed."""
    c = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    p = ctypes.POINTER(ctypes.c_void_p)
    c.GetApplicationEventTarget.restype = ctypes.c_void_p
    c.GetApplicationEventTarget.argtypes = []
    c.InstallEventHandler.argtypes = [ctypes.c_void_p, _HANDLER, ctypes.c_ulong,
                                      ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p, p]
    c.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
                                      ctypes.c_void_p, ctypes.c_uint32, p]
    c.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
    for f in (c.InstallEventHandler, c.RegisterEventHotKey, c.UnregisterEventHotKey):
        f.restype = ctypes.c_int32
    return c


def _on_main(fn) -> None:  # tests replace this
    """fn() on the Mac's main thread, in the NSApplication loop `webview.start` runs."""
    from PyObjCTools import AppHelper
    AppHelper.callAfter(fn)


class MacHotkey:
    """Quick capture's global hotkey on a Mac: Carbon's RegisterEventHotKey, which needs no
    Accessibility permission. Its handler sits on the application event target, so the main
    run loop (pywebview's NSApplication) delivers each press. `start` runs on the main thread
    before `webview.start`; `change` and `stop` hop to it. A taken combination: `taken`."""

    SIGNATURE = int.from_bytes(b"kpfd", "big")

    def __init__(self, on_press):
        self.on_press = on_press
        self.key: str | None = None
        self.taken = self.on = False
        self.ref, self.handler = ctypes.c_void_p(), ctypes.c_void_p()
        self.proc = _HANDLER(self._pressed)  # kept here: Carbon holds only the pointer
        self.done = threading.Event()
        self.c = None

    def label(self) -> str | None:
        return hotkey_label(self.key) if self.on and self.key else None

    def _pressed(self, _call, _event, _data) -> int:
        try:
            self.on_press()
        except Exception:  # noqa: BLE001  (a failed show never ends the hotkey)
            traceback.print_exc(limit=1)
        return 0  # noErr

    def start(self) -> None:
        self.c = _carbon()
        spec = _EventTypeSpec(KEYB, HOTKEY_PRESSED)
        self.c.InstallEventHandler(self.c.GetApplicationEventTarget(), self.proc, 1,
                                   ctypes.byref(spec), None, ctypes.byref(self.handler))
        self._register()

    def _release(self) -> None:
        if self.on:
            self.c.UnregisterEventHotKey(self.ref)
            self.on = False

    def _register(self) -> None:
        self._release()
        self.key = hotkey_setting()
        spec = MAC_HOTKEYS[self.key]
        self.on = bool(spec) and self.c.RegisterEventHotKey(
            spec[1], spec[0], _EventHotKeyID(self.SIGNATURE, 1),
            self.c.GetApplicationEventTarget(), 0, ctypes.byref(self.ref)) == 0
        self.taken = bool(spec) and not self.on
        self.done.set()

    def change(self) -> None:
        """Register app.hotkey again (on the main thread); returns once it has (or after 2 s)."""
        self.done.clear()
        _on_main(self._register)
        self.done.wait(2)

    def stop(self) -> None:
        if threading.current_thread() is threading.main_thread():
            self._release()
        else:
            _on_main(self._release)


def _gnome_list(text: str) -> list[str]:
    """gsettings' text of an `as` value (`@as []`, `['/a/', '/b/']`) as a list; ValueError for
    anything else, so a list never read is never rewritten."""
    t = text.strip().removeprefix("@as").strip()
    try:
        v = ast.literal_eval(t)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"not a gsettings string list: {text[:80]!r}") from e
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"not a gsettings string list: {text[:80]!r}")
    return v


def _gsettings(*args: str) -> tuple[bool, str]:
    from keepfold.app import targets
    return targets._run(["gsettings", *args], timeout=10)


def _gnome_binding(accel: str | None) -> bool:
    """GNOME's custom keyboard shortcut running `keepfold capture` set to `accel`, or removed
    (None). Keepfold's path is added to or removed from the custom-keybindings list; every
    other entry stays. False when gsettings fails or the list cannot be read."""
    ok, out = _gsettings("get", GNOME_SCHEMA, "custom-keybindings")
    try:
        paths = _gnome_list(out) if ok else None
    except ValueError:
        paths = None
    if paths is None:
        return False
    entry = f"{GNOME_SCHEMA}.custom-keybinding:{GNOME_KB}"
    if accel is None:
        if GNOME_KB in paths:
            ok = _gsettings("set", GNOME_SCHEMA, "custom-keybindings",
                            repr([p for p in paths if p != GNOME_KB]))[0]
        return _gsettings("reset-recursively", entry)[0] and ok
    from keepfold import clients
    from keepfold.app import bundle
    cli = bundle.cli_path()
    command = f"{shlex.quote(cli.as_posix() if cli else clients.keepfold_command())} capture"
    for key, value in (("name", "Keepfold quick capture"), ("command", command),
                       ("binding", accel)):
        if not _gsettings("set", entry, key, repr(value))[0]:
            return False
    if GNOME_KB in paths:
        return True
    return _gsettings("set", GNOME_SCHEMA, "custom-keybindings", repr([*paths, GNOME_KB]))[0]


class GnomeHotkey:
    """Quick capture's key on GNOME: a custom keyboard shortcut that runs `keepfold capture`
    (works on Wayland too). Written when the key is on, removed when off; Quit keeps it, as
    `keepfold capture` starts the app."""

    taken = False  # GNOME does not say when another shortcut uses the same keys

    def __init__(self):
        self.key: str | None = None
        self.on = False

    def label(self) -> str | None:
        return hotkey_label(self.key) if self.on and self.key else None

    def start(self) -> None:
        self.change()

    def change(self) -> None:
        self.key = hotkey_setting()
        spec = GNOME_HOTKEYS[self.key]
        self.on = _gnome_binding(spec[0] if spec else None) and bool(spec)

    def stop(self) -> None:
        pass


def daily_notes(root: Path, now: datetime) -> list[tuple[str, str]]:
    """Today's (key, text) notes: reviews due and exams in 3 days or tomorrow, counted and named
    from non-private concepts and assessments only. Each key is sent once (`Notifier.daily`)."""
    from keepfold import planner
    from keepfold.catalog import Catalog
    from keepfold.events import local_day
    from keepfold.vault import Vault
    on, vault, day = notify_settings(), Vault(root), local_day(now)
    out = []
    if on["reviews"]:
        cat = Catalog.load(vault)
        states, _ = planner.current_states(vault, day, cat)
        hide = planner.hidden(vault, cat)
        n = sum(1 for c, st in states.items() if c not in hide and planner.is_due(st, day))
        if n:
            out.append((f"reviews:{day}", f"{n} review{'s' * (n != 1)} due today"))
    if on["exams"]:
        for a in planner._upcoming(vault, date.fromisoformat(day)):
            if not a["private"] and a["days"] in EXAM_DAYS:
                when = "tomorrow" if a["days"] == 1 else f"in {a['days']} days"
                name = (a["company"] or a["id"])[:60]
                out.append((f"exam:{a['id']}:{a['date']}:{a['days']}", f"{name} test {when}"))
    return out


class Notifier:
    """Notifications through `send(text)` (the tray): the daily notes and each job's end."""

    def __init__(self, app, send):
        self.app, self.send = app, send

    def daily(self, now: datetime | None = None) -> None:
        from keepfold.events import local_day
        if self.app.entry() is None:  # no vault yet
            return
        now = now or datetime.now(UTC)
        day = local_day(now)
        sent = load_state().get("notified")
        sent = sent if isinstance(sent, dict) else {}
        new = [(k, text) for k, text in daily_notes(self.app.root, now) if k not in sent]
        for _, text in new:
            self.send(text)
        if new:
            keep = (date.fromisoformat(day) - timedelta(KEEP_DAYS)).isoformat()
            save_state("notified", {**{k: v for k, v in sent.items()
                                       if isinstance(v, str) and v >= keep},
                                    **{k: day for k, _ in new}})

    def loop(self, echo=print) -> None:
        wait = FIRST_S
        while not self.app.stop.wait(wait):
            wait = EVERY_S
            try:
                self.daily()
            except Exception as e:  # noqa: BLE001  (the type only: a message could quote a page)
                echo(f"Keepfold app: the notification check failed ({type(e).__name__})")

    def job_ended(self, job) -> None:
        """"<job> finished" or "<job> failed"; a cancelled job, or a background one whose result
        is `quiet` (it found nothing to do), says nothing. Job names are the app's own words,
        never a page's title."""
        quiet = isinstance(job.result, dict) and job.result.get("quiet") is True
        if job.cancelled.is_set() or quiet or not notify_settings()["jobs"]:
            return
        failed = job.error is not None or (
            isinstance(job.result, dict) and job.result.get("ok") is False)
        self.send(f"{job.name} {'failed' if failed else 'finished'}")


# ---- the window and the tray -----------------------------------------------------------------


def _shutting_down() -> bool:  # tests replace this
    """Windows is signing out or shutting down: the window must close, not hide."""
    if sys.platform != "win32":
        return False
    import ctypes
    return bool(ctypes.windll.user32.GetSystemMetrics(SM_SHUTTINGDOWN))


def _appkit():  # tests replace this
    """pyobjc's AppKit, on a Mac only (pywebview's Cocoa window brings it)."""
    import AppKit
    return AppKit


def _gio():  # tests replace this
    """PyGObject's Gio, on Linux only (pywebview's GTK window brings it)."""
    from gi.repository import Gio
    return Gio


def _unc():  # tests replace this
    """pyobjc's UserNotifications, on a Mac only."""
    import UserNotifications
    return UserNotifications


_NOTE_LOCK = threading.Lock()
_MAC = {"granted": None, "pending": None}  # this run's one authorization ask, and what waits on it
_GAPP: list = []  # Linux: the one registered Gio.Application


def _notify_mac(text: str) -> None:
    """UNUserNotificationCenter, under the bundle's own name. The first notification asks for
    authorization (the OS prompts once; later runs just get the answer); while it is asked,
    notifications wait; denied drops them and is noted (`system_off` on the app API).
    osascript, whose notification belongs to Script Editor, only when the framework is missing
    or there is no bundle (a source run)."""
    from keepfold.app import launch
    try:
        un = _unc()
        center = un.UNUserNotificationCenter.currentNotificationCenter()
    except Exception:  # noqa: BLE001  (no pyobjc framework, or no bundle: no center)
        try:
            launch._run_alert(["osascript", "-e", "display notification "
                               f'{launch._quote_applescript(text)} with title "Keepfold"'])
        except OSError:
            pass
        return

    def post():
        c = un.UNMutableNotificationContent.alloc().init()
        c.setTitle_("Keepfold")
        c.setBody_(text)
        center.addNotificationRequest_withCompletionHandler_(
            un.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                os.urandom(8).hex(), c, None), None)

    def answered(granted, _error):
        with _NOTE_LOCK:
            _MAC["granted"], waiting, _MAC["pending"] = bool(granted), _MAC["pending"], None
        save_state("notify_denied", not granted)
        for p in waiting if granted else ():
            p()

    with _NOTE_LOCK:
        if _MAC["granted"] is not None:
            if _MAC["granted"]:
                post()
            return
        ask = _MAC["pending"] is None
        if ask:
            _MAC["pending"] = []
        _MAC["pending"].append(post)
    if ask:
        center.requestAuthorizationWithOptions_completionHandler_(
            un.UNAuthorizationOptionAlert | un.UNAuthorizationOptionSound, answered)


def _notify_linux(text: str) -> None:
    """Gio.Notification through a Gio.Application whose id is the .desktop id, so the shell
    shows Keepfold's name and icon; notify-send without gi or a session bus."""
    from keepfold.app import launch
    try:
        gio = _gio()
        with _NOTE_LOCK:
            if not _GAPP:
                a = gio.Application.new(DESKTOP_ID, gio.ApplicationFlags.NON_UNIQUE)
                a.register(None)
                _GAPP.append(a)
        n = gio.Notification.new("Keepfold")
        n.set_body(text)
        _GAPP[0].send_notification(None, n)
    except Exception:  # noqa: BLE001  (no gi, no session bus)
        try:
            launch._run_alert(["notify-send", "--app-name=Keepfold", "Keepfold", text])
        except OSError:  # no notify-send either: dropped
            pass


def _tray_host() -> bool:  # tests replace this
    """Linux: something shows tray icons, a StatusNotifierWatcher on the session bus (Ubuntu's
    AppIndicator extension, KDE); False without one or without D-Bus."""
    try:
        gio = _gio()
        f = gio.DBusProxyFlags
        return gio.DBusProxy.new_for_bus_sync(
            gio.BusType.SESSION,
            f.DO_NOT_LOAD_PROPERTIES | f.DO_NOT_CONNECT_SIGNALS | f.DO_NOT_AUTO_START, None,
            "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher", None).get_name_owner() is not None
    except Exception:  # noqa: BLE001  (no gi, no session bus: no tray)
        return False


def _watch_logout(quit):  # tests replace this
    """Mac and Linux: logging out or shutting down calls `quit` (the tray's Quit), so the app
    ends cleanly. It only listens, never inhibits, so it never holds either up. Returns what
    keeps the watch alive; None on Windows, where `_shutting_down` does this."""
    if sys.platform == "darwin":  # posted on the main thread before the OS asks apps to quit
        ak = _appkit()
        return ak.NSWorkspace.sharedWorkspace().notificationCenter() \
            .addObserverForName_object_queue_usingBlock_(
                ak.NSWorkspaceWillPowerOffNotification, None, None, lambda _note: quit())
    if not sys.platform.startswith("linux"):
        return None
    gio, me = _gio(), os.environ.get("XDG_SESSION_ID")

    def on_signal(_bus, _sender, _path, _iface, name, params):
        args = params.unpack()
        if (name == "PrepareForShutdown" and args[0]) or (
                name == "SessionRemoved" and me and args[0] == me):
            quit()
    bus = gio.bus_get_sync(gio.BusType.SYSTEM, None)  # logind; signals reach the GTK loop
    bus.signal_subscribe("org.freedesktop.login1", "org.freedesktop.login1.Manager", None,
                         "/org/freedesktop/login1", None, gio.DBusSignalFlags.NONE, on_signal)
    return bus


def _when_launched(fn):  # tests replace this
    """Mac: fn() when NSApp finishes launching (inside webview.start), the only time the launch
    Apple Event is current. Returns what keeps the observer alive."""
    ak = _appkit()
    return ak.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
        ak.NSApplicationDidFinishLaunchingNotification, None, None, lambda _note: fn())


def _place(w) -> dict:
    """The window's size and place, from its closing handler. GTK runs that handler on its own
    thread, where pywebview's w.width and w.x queue work for that same thread and wait for it
    forever (trial build 4: Quit, or the X, froze the app), so there read the Gtk.Window."""
    native = getattr(w, "native", None)
    if sys.platform.startswith("linux") and hasattr(native, "get_size"):
        (width, height), (x, y) = native.get_size(), native.get_position()
    else:
        width, height, x, y = w.width, w.height, w.x, w.y
    return {"width": width, "height": height, "x": x, "y": y}


def _app_terminating() -> bool:
    """True when called from inside pywebview's applicationShouldTerminate_ (Cocoa runs the
    closing handlers synchronously, on its own stack)."""
    f = sys._getframe(1)
    while f is not None:
        if f.f_code.co_name == "applicationShouldTerminate_":
            return True
        f = f.f_back
    return False


def _login_launch() -> bool:  # tests replace this
    """Mac: this start is a login item's, its launch Apple Event marked 'lgit'. False when that
    cannot be told, so the window shows. Not yet seen across a real login (port P3)."""
    try:
        ev = _appkit().NSAppleEventManager.sharedAppleEventManager().currentAppleEvent()
        prop = ev.paramDescriptorForKeyword_(AE_PROP) if ev is not None else None
        return prop is not None and prop.enumCodeValue() == AE_LOGIN
    except Exception:  # noqa: BLE001  (no pyobjc, no event: a normal start)
        return False


def _native(win, fn) -> bool:
    """fn(form, control) on the GUI thread, given pywebview's WinForms form and its Edge WebView2
    control; False, with nothing run, for another backend or a window already gone."""
    form = getattr(win, "native", None)
    control = getattr(getattr(form, "browser", None), "webview", None)
    if not hasattr(type(control), "CoreWebView2"):  # its value: only on the GUI thread
        return False
    try:
        from System import Func, Type  # pythonnet: pywebview's WinForms backend loaded it
        form.Invoke(Func[Type](lambda: fn(form, control)))
        return True
    except Exception:  # noqa: BLE001  (closing at Quit; pythonnet's errors)
        traceback.print_exc(limit=1)
        return False


def _hide(form, control) -> None:
    # WinForms' Hide alone leaves WebView2's page visible (document.hidden false: its 3D brain
    # and polls ran on, docs/benchmarks/memory.md); hiding the control while the form is still
    # shown sets the WebView2 IsVisible false, so the page knows and can sleep
    control.Visible = False
    form.Hide()


def _show(form, control) -> None:
    form.Show()
    control.Visible = True  # IsVisible again: a sleeping page wakes
    form.Activate()


def _show_box(form, control) -> None:
    """The quick-capture box: kept out of the taskbar (a tool window, set while hidden, so
    the form keeps its handle and its WebView2), centred on the monitor with the mouse, a
    third of the way down, then shown and focused."""
    import ctypes

    from System.Drawing import Point  # pythonnet, loaded by pywebview's WinForms backend
    from System.Windows.Forms import Cursor, Screen
    if not form.Visible:
        u, hwnd = ctypes.windll.user32, ctypes.c_void_p(form.Handle.ToInt64())
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, u.GetWindowLongW(hwnd, GWL_EXSTYLE)
                         | WS_EX_TOOLWINDOW)
    area = Screen.FromPoint(Cursor.Position).WorkingArea
    form.Location = Point(area.X + (area.Width - form.Width) // 2,
                          area.Y + area.Height // 3 - form.Height // 2)
    _show(form, control)
    control.Focus()


def _box_origin(mouse, screens, size) -> tuple[float, float]:
    """Mac: the box's bottom-left corner (Cocoa's y runs up), centred on the screen holding
    `mouse`, a third of the way down its visible area, as on Windows. `screens`: (frame,
    visibleFrame) pairs of (x, y, width, height), the main screen first (the fallback)."""
    mx, my = mouse
    _, (x, y, w, h) = next((s for s in screens if s[0][0] <= mx <= s[0][0] + s[0][2]
                            and s[0][1] <= my <= s[0][1] + s[0][3]), screens[0])
    return x + (w - size[0]) / 2, y + h * 2 / 3 - size[1] / 2


def _place_box_mac(win) -> None:
    """Move the box to the screen with the mouse (on the main thread, before its show)."""
    def place():
        ak = _appkit()
        rect = lambda r: (r.origin.x, r.origin.y, r.size.width, r.size.height)  # noqa: E731
        m, f = ak.NSEvent.mouseLocation(), win.native.frame().size
        x, y = _box_origin((m.x, m.y), [(rect(s.frame()), rect(s.visibleFrame()))
                                        for s in ak.NSScreen.screens()], (f.width, f.height))
        win.native.setFrameOrigin_(ak.NSPoint(x, y))
    _on_main(place)


def _sleep(form, control) -> None:
    if not form.Visible and control.CoreWebView2 is not None:  # still hidden, WebView2 ready
        control.CoreWebView2.TrySuspendAsync()


class Desktop:
    """One running app's window and tray icon (threads: see the module doc)."""

    def __init__(self, app, url: str):
        self.app, self.url = app, url
        self.win = self.tray = None
        self.box = None  # the quick-capture box: a second window, made once, shown and hidden
        self.hotkey: Hotkey | None = None
        self.quitting, self.state = False, "normal"  # normal | minimized | maximized
        self.fresh = False  # started hidden and never shown: the first show loads start_url
        self.smoke_ok = False

    def show(self) -> bool:
        """Bring the window up (Open Keepfold, a second launch); with no window, the browser.
        The page is the one for now (server.start_url: Tonight in the evening) in a new tab or
        on the first show after a hidden start; a window already seen keeps its page."""
        if self.win is None:
            server._open(server.start_url(self.app))
            return True
        if self.fresh:
            self.fresh = False
            self.win.load_url(server.start_url(self.app))
        if not _native(self.win, _show):
            self.win.show()
        if self.state == "minimized":
            self.win.restore()
        return True

    def watch_first_load(self, echo=print, wait: float = FIRST_LOAD_S) -> None:
        """Mac/Linux, a shown start: a window whose page never loads (GTK or Cocoa failing
        quietly, as trial build 2's icon did) leaves the page in the browser, so the app is never
        a process with no window. The tray, or Quit on the page, still quits."""
        w = self.win
        if w is None or w.events.loaded.wait(wait) or self.app.stop.is_set():
            return
        echo("Keepfold app: the window did not open; using the browser")
        server._open(server.start_url(self.app))

    def closing(self) -> bool:
        """The window's X (pywebview's closing event; False cancels the close): remember its
        place and hide it, Keepfold stays in the tray. Quit, a shutdown or no tray closes it."""
        if self.state == "normal":
            try:
                save_state("window", _place(self.win))
            except Exception:  # noqa: BLE001  (a place not remembered never blocks closing)
                traceback.print_exc(limit=1)
        if self.quitting or self.tray is None or _shutting_down():
            return True
        if self._mac_quit():
            return False
        self.app.lock()  # the Private switch relocks when the window goes to the tray
        # hidden from another thread: this handler runs on the GUI thread inside FormClosing
        threading.Thread(target=self.hide, daemon=True, name="keepfold-hide").start()
        return False

    def hide(self) -> None:
        """To the tray; SLEEP_S later, still there, its page sleeps: WebView2 pauses its
        timers and animations and lets Windows take the renderer's memory back, and wakes it
        when the window shows."""
        if _native(self.win, _hide):
            self.sleep_later()
        else:
            self.win.hide()

    def capture(self) -> bool:
        """The hotkey or the tray's Quick capture: show the box, its input focused."""
        if self.box is None:
            return False
        if sys.platform == "darwin":
            try:
                _place_box_mac(self.box)
            except Exception:  # noqa: BLE001  (shown where it was rather than not at all)
                traceback.print_exc(limit=1)
        if not _native(self.box, _show_box):
            self.box.show()
        return True

    def hide_box(self) -> bool:
        """Esc, Enter's saved line or the box losing focus: hide it (its text stays); SLEEP_S
        later, still hidden, its page sleeps as the main window's does."""
        if self.box is None:
            return False
        if _native(self.box, _hide):
            self.sleep_later(self.box)
        else:
            self.box.hide()
        return True

    def box_closing(self) -> bool:
        """Alt+F4 on the box hides it; only Quit closes it."""
        if self.quitting:
            return True
        if not self._mac_quit():
            threading.Thread(target=self.hide_box, daemon=True, name="keepfold-hide").start()
        return False

    def _mac_quit(self) -> bool:
        """Mac (owner, 28 Nov): Cmd+Q, or Quit in the app or Dock menu, quits as Mac users
        expect; only the red button hides. pywebview asks every window's closing handler inside
        its AppDelegate's applicationShouldTerminate_, the red button only that window's. The
        OS's terminate is cancelled and the menu-bar Quit's clean stop runs instead."""
        if sys.platform != "darwin" or not _app_terminating():
            return False
        self.quit()
        return True

    def capture_label(self) -> str:
        key = self.hotkey.label() if self.hotkey is not None else None
        return f"Quick capture ({key})" if key else "Quick capture"

    def change_hotkey(self) -> None:
        """app.hotkey changed: register it again now, and the tray's text follows."""
        if self.hotkey is not None:
            self.hotkey.change()
        self.refresh_tray()

    def sleep_later(self, win=None) -> None:
        """SLEEP_S from now, `win` (the main window by default, or the capture box) sleeps if
        it is still hidden (`_sleep`); a show in between keeps it awake."""
        w = win or self.win
        t = threading.Timer(SLEEP_S, lambda: self.quitting or _native(w, _sleep))
        t.daemon = True
        t.start()

    def minimized(self) -> None:
        self.state = "minimized"
        self.app.lock()  # the Private switch relocks when the window is minimised

    def end(self) -> None:
        """keepfold-end: once the app stops, close the window (that ends `webview.start`)."""
        self.app.stop.wait()
        self.quitting = True
        if self.hotkey is not None:
            self.hotkey.stop()  # WM_QUIT: its thread unregisters the key
        for w in (self.box, self.win):  # the main window last: that ends webview.start
            if w is None:
                continue
            try:
                w.events.shown.wait(30)  # a window not yet started cannot be destroyed
                w.destroy()
            except Exception:  # noqa: BLE001  (already gone)
                traceback.print_exc(limit=1)

    def quit(self) -> None:
        """Quit in the tray or the window's menu, and logging out on Mac and Linux."""
        self.quitting = True  # from now on a close closes: the OS's quit at logout never waits
        self.app.quit({})  # cancels a running job and sets app.stop

    def notify(self, text: str) -> None:
        if sys.platform == "darwin":
            _notify_mac(text)
        elif sys.platform.startswith("linux"):
            _notify_linux(text)
        elif self.tray is not None:
            self.tray.notify(text, "Keepfold")

    def toggle_autostart(self) -> None:
        try:
            if note := set_autostart(not autostart_state()["enabled"]).get("note"):
                self.notify(note)  # a Mac login item the user must allow
        except (OSError, ValueError) as e:
            self.notify(str(e)[:200])
        self.refresh_tray()

    def launched(self) -> None:
        """The Mac app finished launching: a login start (no --hidden there) goes to the menu
        bar, as `--hidden` does. With no menu-bar icon the window stays: no way back to it."""
        if self.tray is not None and _login_launch():
            self.fresh = True
            self.hide()

    def refresh_tray(self) -> None:
        if self.tray is not None:
            self.tray.update_menu()

    def smoke(self, echo=print) -> None:
        """keepfold-smoke (`--smoke`, the release build's check): the window's page loaded and
        the app answers on its own address with its key (running_url, as a second launch
        checks); then the window stays up SMOKE_SHOW_S for a screenshot, and the app quits."""
        w = self.win  # None once the window failed
        self.smoke_ok = (w is not None and bool(w.events.loaded.wait(SMOKE_LOAD_S))
                         and server.running_url() is not None)
        echo(f"Keepfold app: smoke {'ok' if self.smoke_ok else 'FAILED'}")
        if self.smoke_ok:
            self.app.stop.wait(SMOKE_SHOW_S)
        self.quit()


LETTER_K = ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#")
SMALL = 32  # up to this size the dots are whole-pixel squares, so the K stays crisp


def mark(size: int):
    """The Keepfold logo at `size` px (Pillow, already a dependency): a dark rounded square with
    an amber dot-matrix K, as the KEEPFOLD wordmark draws it. The tray, the window, the
    shortcuts and Keepfold.exe all use it (scripts/make_icon.py writes assets/keepfold.ico)."""
    from PIL import Image, ImageDraw
    k = 1 if size <= SMALL else 4  # round dots are drawn at 4x and scaled down
    n = size * k
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    m = round(size * 0.06) * k
    side = n - 2 * m
    draw.rounded_rectangle((m, m, n - m - 1, n - m - 1), radius=round(side * 0.22), fill=GROUND)
    rows, cols = len(LETTER_K), len(LETTER_K[0])
    if k == 1:  # whole pixels
        pitch = max(1, round(side * 0.6 / rows))
        x0, y0 = (n - cols * pitch) // 2, (n - rows * pitch) // 2
    else:
        pitch = side * 0.6 / rows
        x0, y0 = (n - cols * pitch) / 2, (n - rows * pitch) / 2
    gap = pitch * 0.1  # round dots are 80% of the pitch
    for r, line in enumerate(LETTER_K):
        for c, cell in enumerate(line):
            if cell != "#":
                continue
            x, y = x0 + c * pitch, y0 + r * pitch
            if k == 1:
                draw.rectangle((x, y, x + pitch - 1, y + pitch - 1), fill=AMBER)
            else:
                draw.ellipse((x + gap, y + gap, x + pitch - gap, y + pitch - gap), fill=AMBER)
    return img if k == 1 else img.resize((size, size), Image.LANCZOS)


def _icon_image():
    """The tray icon: the logo at 64 px."""
    return mark(64)


def _window_icon() -> Path:
    return ICON_PNG if sys.platform.startswith("linux") else ICON


def _tray(d: Desktop):
    """The tray icon (a left click is Open Keepfold): on Windows on its own thread; on a Mac
    and Linux made on the main thread before `webview.start()`, whose loop then runs it."""
    linux = sys.platform.startswith("linux")
    if linux:
        if not _tray_host():
            raise RuntimeError("nothing on this desktop shows tray icons")
        os.environ.setdefault("PYSTRAY_BACKEND", "appindicator")  # read when pystray loads
    import pystray
    item = pystray.MenuItem
    icon = pystray.Icon("keepfold", _icon_image(), "Keepfold", pystray.Menu(
        item("Open Keepfold", lambda: d.show(), default=True),
        item(lambda _: d.capture_label(), lambda: d.capture()),
        item(autostart_label(), lambda: d.toggle_autostart(),
             checked=lambda _: autostart_state()["enabled"]),
        pystray.Menu.SEPARATOR,
        item("Quit", lambda: d.quit())))
    if linux or sys.platform == "darwin":
        icon.run_detached()
    else:
        threading.Thread(target=icon.run, daemon=True, name="keepfold-tray").start()
    return icon


def _window_menu(d: Desktop, menu) -> list:
    """With no tray, its items in the window's own menu (pywebview's `webview.menu`)."""
    return [menu.Menu("Keepfold", [
        menu.MenuAction("Open Keepfold", d.show),
        menu.MenuAction(d.capture_label(), d.capture),
        menu.MenuAction(autostart_label(), d.toggle_autostart),
        menu.MenuSeparator(),
        menu.MenuAction("Quit", d.quit)])]


def _webview():
    """pywebview drawing with Edge WebView2, or an exception that says why not."""
    import webview
    if sys.platform == "win32":  # webview.guilib is None until start(); initialize() is reused
        if webview.initialize().renderer != "edgechromium":  # MSHTML cannot run the app page
            raise RuntimeError("the Microsoft Edge WebView2 Runtime is not installed")
    return webview


def _geometry(webview) -> dict:
    """The remembered size, and place while it is still on a screen (a monitor may be gone)."""
    w = load_state().get("window")
    out = {"width": SIZE[0], "height": SIZE[1]}
    try:
        width, height, x, y = (int(w[k]) for k in ("width", "height", "x", "y"))
    except (KeyError, TypeError, ValueError):
        return out
    if width >= MIN_SIZE[0] and height >= MIN_SIZE[1]:
        out.update(width=width, height=height)
    if any(s.x <= x < s.x + s.width - 100 and s.y <= y < s.y + s.height - 100
           for s in webview.screens):
        out.update(x=x, y=y)
    return out


def run(root: Path | None, *, hidden: bool = False, port: int = 0, echo=print,
        smoke: bool = False) -> None:
    """The desktop app until Quit; `hidden` starts it in the tray (Start with Windows). With no
    window possible, the browser app as `keepfold app` runs it (nothing when hidden). `smoke`:
    Desktop.smoke, then exit 1 unless it passed; no first-run Start at login."""
    try:
        webview = _webview()
    except Exception as e:  # noqa: BLE001  (ImportError, pythonnet's errors, no WebView2)
        echo(f"Keepfold app: no app window ({type(e).__name__}: {e}); using the browser")
        if smoke:
            raise SystemExit(1) from None
        if not hidden:  # at login: no browser tab nobody asked for
            server.run(root, port=port, echo=echo)
        return
    if smoke:
        _smoke_deadline(True)
    # before any window: the taskbar shows and pins Keepfold, not Python
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    srv, app, url = server.start(root, port=port, echo=echo, idle=False)
    d = app.desktop = Desktop(app, url)
    d.fresh = hidden
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True,
                     name="keepfold-app-http").start()
    try:
        d.tray = _tray(d)
    except Exception as e:  # noqa: BLE001  (no tray: closing the window quits)
        echo(f"Keepfold app: no tray icon ({type(e).__name__}: {e})")
    try:
        d.logout_watch = _watch_logout(d.quit)
    except Exception as e:  # noqa: BLE001  (no pyobjc or gi: the OS ends the app as before)
        echo(f"Keepfold app: logout is not watched ({type(e).__name__}: {e})")
    notes = Notifier(app, d.notify)
    app.jobs.on_end = notes.job_ended
    threading.Thread(target=notes.loop, args=(echo,), daemon=True, name="keepfold-notify").start()
    if not smoke:  # a CI check never turns Start at login on
        first_run(echo)
    echo(f"Keepfold app: {url.split('?')[0]}")  # never the key (?t=): stdout may be a shared log
    echo("Closing the window keeps Keepfold in the tray; Quit there stops it.")
    try:
        d.win = webview.create_window("Keepfold", url, hidden=hidden, min_size=MIN_SIZE,
                                      background_color=GROUND, **_geometry(webview))
        ev = d.win.events
        ev.closing += d.closing
        ev.minimized += d.minimized
        ev.maximized += lambda: setattr(d, "state", "maximized")
        ev.restored += lambda: setattr(d, "state", "normal")
        parts = urlsplit(url)  # quick capture's box: made now, hidden, shown by the hotkey
        d.box = webview.create_window(
            "Quick capture", f"{parts.scheme}://{parts.netloc}/capture?t={app.token}",
            hidden=True, frameless=True, on_top=True, resizable=False, width=BOX[0],
            height=BOX[1], background_color=GROUND)
        d.box.events.closing += d.box_closing
        d.sleep_later(d.box)  # hidden until the first press: no awake renderer meanwhile
        try:
            d.hotkey = _hotkey(d.capture)
            if d.hotkey is not None:  # on a Mac: here, on the main thread
                d.hotkey.start()
                d.refresh_tray()  # its Quick capture text names the key
        except Exception as e:  # noqa: BLE001  (no Carbon: the tray's Quick capture still works)
            d.hotkey = None
            echo(f"Keepfold app: no quick capture key ({type(e).__name__}: {e})")
        if hidden:  # Start with Windows: the page sleeps until the window is first opened
            d.sleep_later()
        else:
            from keepfold.app import bundle
            if bundle._mac_app():  # its login item passes no --hidden: tell from the launch
                try:
                    d.launch_watch = _when_launched(d.launched)
                except Exception as e:  # noqa: BLE001  (no pyobjc: the window shows)
                    echo(f"Keepfold app: a login start is not told apart ({e})")
        menu = {}
        if d.tray is None and sys.platform != "win32":
            menu = {"menu": _window_menu(d, webview.menu)}
            ev.closed += d.quit  # the hidden box would keep webview.start running
        threading.Thread(target=d.end, daemon=True, name="keepfold-end").start()
        if smoke:
            threading.Thread(target=d.smoke, args=(echo,), daemon=True,
                             name="keepfold-smoke").start()
        elif not hidden and sys.platform != "win32":  # WebView2 failing raises below instead
            threading.Thread(target=d.watch_first_load, args=(echo,), daemon=True,
                             name="keepfold-first-load").start()
        # blocks this (main) thread until the window is destroyed
        webview.start(icon=str(_window_icon()), **menu)
    except Exception as e:  # noqa: BLE001  (WebView2 failed to start: the page in the browser)
        echo(f"Keepfold app: the window failed ({type(e).__name__}: {e}); using the browser")
        d.win = None
        if not hidden and not smoke:
            d.show()
        app.stop.wait()  # the tray's Open Keepfold now opens the browser
    finally:
        app.stop.set()
        if d.hotkey is not None:
            d.hotkey.stop()
        srv.shutdown()
        if d.tray is not None:
            d.tray.stop()
        server.finish(srv, app)
        if smoke:
            _smoke_deadline(False)
    if smoke and not d.smoke_ok:
        raise SystemExit(1)


def _smoke_deadline(on: bool) -> None:
    """`--smoke` only: a check stuck anywhere, even before the window, writes every thread's
    stack to stderr (the app log) and exits 1 after SMOKE_DEADLINE_S, so CI fails with the
    cause instead of hanging until its job timeout. faulthandler's watchdog needs no GIL."""
    import faulthandler
    try:
        if on:
            faulthandler.dump_traceback_later(SMOKE_DEADLINE_S, exit=True, file=sys.stderr)
        else:
            faulthandler.cancel_dump_traceback_later()
    except (AttributeError, ValueError, OSError):  # a stderr with no fileno: no deadline
        pass
