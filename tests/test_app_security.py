"""The local app's edges (D6 §6, T1, T2, T5): the Host header must match exactly, a page on
another site cannot POST to any route, and no page script writes HTML from a string. Real sockets
on 127.0.0.1 and a temp vault; nothing leaves the machine."""

import re
import socket
import threading
from pathlib import Path

import pytest

from keepfold.app import actions, server, targets

pytestmark = [pytest.mark.usefixtures("home"), pytest.mark.gate("G8")]
ASSETS = Path(server.__file__).resolve().parents[1] / "assets"


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    """Nothing real starts, opens or listens on the owner's port."""
    monkeypatch.setattr(targets, "_which", lambda c: None)
    monkeypatch.setattr(targets, "_run", lambda args, timeout=300: (False, "faked"))
    monkeypatch.setattr(actions, "_popen", lambda *a, **k: pytest.fail("a process started"))
    monkeypatch.setattr(server, "_open", lambda url: None)
    monkeypatch.setattr(server, "_obsidian", lambda: True)
    monkeypatch.setattr(server, "PORT", 0)


@pytest.fixture
def app(root_committed):
    (root_committed / ".keepfold").mkdir(exist_ok=True)
    srv, a = server.make_server(root_committed)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    a.vault_root = root_committed
    yield a
    srv.shutdown()
    srv.server_close()


def raw(app, method, path, headers, body=b"") -> int:
    """The status of one request written by hand, so the Host line is exactly what we say (or
    absent): http.client would add its own."""
    lines = [f"{method} {path} HTTP/1.0", *(f"{k}: {v}" for k, v in headers.items())]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    with socket.create_connection(("127.0.0.1", app.port), timeout=10) as s:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1])


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.parts}


def test_host_must_match_exactly(app):
    p, key = app.port, {"X-Keepfold-Token": app.token}
    for host in (f"127.0.0.1:{p}.evil", f"127.0.0.1:{p}.", f"LOCALHOST:{p}", f"[::1]:{p}",
                 f"127.0.0.1.evil:{p}", f"127.0.0.1:{p},x", "127.0.0.1", f"localhost:{p + 1}", ""):
        assert raw(app, "GET", "/api/status", {**key, "Host": host}) == 403, repr(host)
    assert raw(app, "GET", "/api/status", key) == 403  # no Host line at all
    assert raw(app, "GET", "/api/status", {**key, "Host": f"127.0.0.1:{p}"}) == 200
    assert raw(app, "GET", "/api/status", {**key, "Host": f"localhost:{p}"}) == 200


def test_no_cors_post_is_refused_on_every_route(app, home):
    """A page on another site can send a "simple" POST (text/plain, no preflight), or a JSON one
    after a preflight the server never answers. Every route says no, and nothing changes."""
    assert len(app.post) > 50  # the table is the one the handler reads
    before, home_before = tree(app.vault_root), tree(home)
    evil = {"Origin": "http://evil.example", "Content-Type": "text/plain"}
    for path in app.post:
        body = b'{"x": 1}'
        for key in ({}, {"X-Keepfold-Token": app.token}):  # without the key, and with it
            got = raw(app, "POST", path, {"Host": f"127.0.0.1:{app.port}", **evil, **key}, body)
            assert got == 403, (path, bool(key), got)  # the foreign Origin is checked first
        plain = {"Host": f"127.0.0.1:{app.port}", "Content-Type": "text/plain"}
        assert raw(app, "POST", path, plain, body) == 401, path  # no key
        assert raw(app, "POST", path, {**plain, "X-Keepfold-Token": app.token}, body) == 415, path
    assert not app.stop.is_set()
    assert tree(app.vault_root) == before and tree(home) == home_before


def test_post_without_origin_and_cross_site_fetch_metadata_is_refused(app):
    host = {"Host": f"127.0.0.1:{app.port}", "Content-Type": "application/json"}
    key = {"X-Keepfold-Token": app.token}
    body = b"{}"
    path = "/api/job/cancel"
    # no Origin and no key: a script outside a browser, or a no-cors form
    assert raw(app, "POST", path, host, body) == 401
    # a browser that says "cross-site" is refused even with JSON and the key
    for site in ("cross-site", "Cross-Site"):
        assert raw(app, "POST", path, {**host, **key, "Sec-Fetch-Site": site}, body) == 403
    assert raw(app, "POST", "/api/quit", {**host, **key, "Sec-Fetch-Site": "cross-site",
                                          "Origin": f"http://127.0.0.1:{app.port}"}, body) == 403
    assert not app.stop.is_set()
    # the desktop window and older browsers keep working
    for extra in ({}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"},
                  {"Origin": f"http://127.0.0.1:{app.port}", "Sec-Fetch-Site": "same-origin"}):
        assert raw(app, "POST", path, {**host, **key, **extra}, body) == 200, extra


# Every `innerHTML`-style sink a page script may not call. An allow-list entry names a file that
# holds one, and why that is safe.
SINKS = re.compile(r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|document\s*\.\s*write(?:ln)?"
                   r"|new\s+Function|setTimeout\s*\(\s*['\"`]|createContextualFragment"
                   r"|srcdoc)\b|(?<![\w./-])eval\s*\(|\.html\s*\(")
ALLOWED_SINKS = {
    # d3's `selection.html()` sets innerHTML. No page script of ours calls `.html(` (the scan
    # below covers every other file), so the vendored library never gets a string to write.
    "d3.v7.min.js": "vendored d3 v7; selection.html is never called by Keepfold's scripts",
}


def test_no_html_sinks_in_any_page_script():
    scripts = sorted(ASSETS.rglob("*.js"))
    assert {p.name for p in scripts} >= {"app.js", "capture.js", "dashboard.js", "brain3d.js"}
    hits = {}
    for p in scripts:
        found = sorted({m.group(0) for m in SINKS.finditer(p.read_text("utf-8"))})
        if found and p.name not in ALLOWED_SINKS:
            hits[p.relative_to(ASSETS).as_posix()] = found
    assert not hits, f"a page script writes HTML from a string: {hits}"
    assert set(ALLOWED_SINKS) <= {p.name for p in scripts}  # an allow-list entry for a gone file
    for page in (*ASSETS.rglob("*.html"), *ASSETS.rglob("*.tmpl")):  # inline scripts too
        assert not SINKS.search(page.read_text("utf-8")), page.name
