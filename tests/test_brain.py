"""D12 B1: the brain listener. Loopback is shared with every user on the PC, so nothing is
answered before the mutual HMAC challenge; bad first lines get a silent close; caps and the 2 s
challenge limit keep a local program from tying it up."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time

import pytest

from keepfold import pids
from keepfold.app import brain, server

KEY = "k" * 43


@pytest.fixture
def b():
    lis = brain.Brain(KEY)
    yield lis
    lis.close()


def connect(lis) -> tuple[socket.socket, dict]:
    s = socket.create_connection(("127.0.0.1", lis.port), timeout=5)
    return s, json.loads(readline(s))


def readline(s: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(b"\n"):
        got = s.recv(4096)
        if not got:
            break
        buf += got
    return buf


def closed(s: socket.socket) -> bool:
    """The server closed with nothing more to say (Windows may reset instead of a FIN)."""
    try:
        return s.recv(4096) == b""
    except (ConnectionResetError, ConnectionAbortedError):
        return True


def answer(s: socket.socket, hello: dict, key: str = KEY) -> tuple[str, dict]:
    n_c = secrets.token_hex(32)
    s.sendall(json.dumps({"v": 1, "n": n_c,
                          "mac": brain.mac(key.encode(), "c", hello["n"], n_c)}).encode() + b"\n")
    return n_c, json.loads(readline(s))


def test_the_mutual_challenge_then_prime_is_not_ready_yet(b):
    s, hello = connect(b)
    with s:
        assert hello["keepfold"] == "brain" and hello["v"] == [1] and len(hello["n"]) == 64
        n_c, ok = answer(s, hello)
        assert ok["ok"] is True and ok["app"]
        assert ok["mac"] == brain.mac(KEY.encode(), "s", n_c, hello["n"])  # the server knows it
        s.sendall(b'{"op":"prime","prompt":"hi"}\n')
        assert json.loads(readline(s)) == {"ok": False, "why": "not_ready"}
        assert closed(s)


def test_an_unknown_op_is_refused(b):
    s, hello = connect(b)
    with s:
        answer(s, hello)
        s.sendall(b'{"op":"routes"}\n')
        assert json.loads(readline(s)) == {"ok": False, "why": "op"}


def test_a_wrong_key_is_refused_and_closed(b):
    s, hello = connect(b)
    with s:
        _, got = answer(s, hello, key="x" * 43)
        assert got == {"ok": False, "why": "auth"} and closed(s)


def test_a_reflected_server_mac_is_refused(b):
    """The c and s labels: the client's mac is not the server's mac for the same nonces."""
    s, hello = connect(b)
    with s:
        n_c = secrets.token_hex(32)
        s.sendall(json.dumps({"v": 1, "n": n_c, "mac": brain.mac(
            KEY.encode(), "s", n_c, hello["n"])}).encode() + b"\n")
        assert json.loads(readline(s)) == {"ok": False, "why": "auth"}


@pytest.mark.parametrize("first", [b"GET / HTTP/1.1\r\n", b"POST /api HTTP/1.1\r\n",
                                   b"[1,2]\n", b"\xff\xfe\n"])
def test_a_bad_first_line_gets_a_silent_close(b, first):
    s, _ = connect(b)
    with s:
        s.sendall(first)
        assert closed(s)


def test_an_oversize_frame_is_cut(b):
    s, hello = connect(b)
    with s:
        try:
            s.sendall(b"x" * (brain.HELLO_MAX + 10))
        except OSError:
            pass
        assert closed(s)
    s, hello = connect(b)  # after the challenge, an op line over 1 MB
    with s:
        answer(s, hello)
        try:
            s.sendall(b"x" * (brain.OP_MAX + 10))
        except OSError:
            pass
        assert closed(s)


def test_slowloris_is_cut_at_the_challenge_deadline(b):
    """A byte every 0.2 s never finishes the line: the limit is from accept, not per read."""
    t0 = time.monotonic()
    s, _ = connect(b)
    with s:
        s.settimeout(0.2)
        while time.monotonic() - t0 < 5:
            try:
                s.sendall(b"{")
                if s.recv(1) == b"":
                    break
            except TimeoutError:
                continue
            except OSError:  # reset
                break
        took = time.monotonic() - t0
    assert brain.CHALLENGE_S - 0.1 <= took < brain.CHALLENGE_S + 1.5


def test_eight_sockets_before_auth_then_the_ninth_is_closed(b):
    held = [connect(b) for _ in range(brain.PRE_AUTH_MAX)]  # each read its nonce: counted
    try:
        extra = socket.create_connection(("127.0.0.1", b.port), timeout=5)
        with extra:
            assert closed(extra)  # no nonce, just a close
        s, hello = held.pop()
        s.close()
        for _ in range(50):  # the server sees the close on its own loop
            t = socket.create_connection(("127.0.0.1", b.port), timeout=5)
            with t:
                if readline(t):
                    break
            time.sleep(0.02)
        else:
            pytest.fail("a freed slot was never reused")
    finally:
        for s, _ in held:
            s.close()


def test_it_binds_loopback_only_on_a_free_port(b):
    assert b.address == ("127.0.0.1", b.port) and b.port != server.PORT
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        assert b._sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1


def test_brain_json_is_discovery_only(b):
    got = json.loads(brain.brain_file().read_text(encoding="utf-8"))
    assert got == {"port": b.port, "v": [1], "pid": os.getpid(), "hook": False}
    assert KEY not in brain.brain_file().read_text(encoding="utf-8")
    if os.name != "nt":
        assert brain.brain_file().stat().st_mode & 0o077 == 0


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not pids.alive(p.pid)
    return p.pid


def test_a_stale_brain_json_is_replaced_and_only_ours_is_removed():
    f = brain.brain_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"port": 1, "v": [1], "pid": dead_pid(), "hook": False}))
    lis = brain.Brain(KEY)
    try:
        assert json.loads(f.read_text())["pid"] == os.getpid()
        other = {"port": lis.port + 1, "v": [1], "pid": os.getpid() + 1, "hook": False}
        f.write_text(json.dumps(other))  # a newer app took over the file
    finally:
        lis.close()
    assert json.loads(f.read_text()) == other  # never another app's file
    f.unlink()
    lis = brain.Brain(KEY)
    lis.close()
    assert not f.exists()
    with pytest.raises(OSError):  # the port is closed too
        socket.create_connection(("127.0.0.1", lis.port), timeout=1).close()


def test_the_app_runs_the_brain_while_it_serves(monkeypatch):
    monkeypatch.setattr(server, "PORT", 0)  # never the real app's port
    srv, app, _ = server.start(None, echo=lambda *_: None)
    try:
        assert json.loads(brain.brain_file().read_text())["port"] == app.brain.port
        assert "brain" not in json.loads(server.state_file().read_text())
        s, hello = connect(app.brain)
        with s:
            _, ok = answer(s, hello, key=app.token)
            assert ok["ok"] is True
    finally:
        server.finish(srv, app)
    assert not brain.brain_file().exists() and not server.state_file().exists()


def test_running_url_checks_the_pid():
    """D6: app.json naming a dead pid never sends the key to whoever holds that port now."""
    squatter = socket.create_server(("127.0.0.1", 0))
    seen: list[bytes] = []

    def serve():
        squatter.settimeout(3)
        try:
            c, _ = squatter.accept()
            with c:
                seen.append(c.recv(4096))
        except OSError:
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    f = server.state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    port = squatter.getsockname()[1]
    try:
        for pid in (dead_pid(), 0, -1, "1", None):
            f.write_text(json.dumps({"port": port, "token": KEY, "pid": pid}))
            assert server.running_url() is None
        f.write_text(json.dumps({"port": port, "token": KEY}))  # no pid at all
        assert server.running_url() is None
    finally:
        squatter.close()
        t.join(5)
    assert seen == []
