"""G23: the app, the MCP server and Chat's own answers run with every non-loopback connection
refused. In this process `offline` (test_egress_runtime) records any attempt; the MCP server is
a child, so it starts through a wrapper that installs the same guard and logs to a file. Temp
HOME and a copy of vault_mini only; no model, no search model, no network."""

import os
import subprocess
import sys
import threading
from pathlib import Path

import anyio
import pytest
from conftest import FakeEmbed
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from test_app import _serve, call, fakes  # noqa: F401  (autouse: nothing real runs or opens)
from test_chat import TODAY
from test_egress_runtime import offline  # noqa: F401

from keepfold import chat, semantic
from keepfold.vault import Vault

pytestmark = [pytest.mark.usefixtures("home"), pytest.mark.gate("G23")]
LAUNCH = Path(__file__).parent / "e2e" / "_launch.py"

GUARD = '''
import os, runpy, socket, sys

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def guard(addr):
    host = str(addr[0] if isinstance(addr, tuple) else addr)
    if host not in LOOPBACK:
        with open(os.environ["KEEPFOLD_G23_BLOCKED"], "a", encoding="utf-8") as f:
            f.write(host + "\\n")
        raise OSError("G23: outbound connection to " + host + " blocked")


real = (socket.socket.connect, socket.socket.connect_ex, socket.create_connection,
        socket.getaddrinfo)


def connect(self, addr):
    if self.family != getattr(socket, "AF_UNIX", None):
        guard(addr)
    return real[0](self, addr)


def connect_ex(self, addr):
    if self.family != getattr(socket, "AF_UNIX", None):
        guard(addr)
    return real[1](self, addr)


def create_connection(addr, *a, **k):
    guard(addr)
    return real[2](addr, *a, **k)


def getaddrinfo(host, *a, **k):
    if host is not None:
        guard((host.decode() if isinstance(host, bytes) else host,))
    return real[3](host, *a, **k)


socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
socket.create_connection, socket.getaddrinfo = create_connection, getaddrinfo
launch = sys.argv[1]
sys.argv = [launch, *sys.argv[2:]]
runpy.run_path(launch, run_name="__main__")
'''


@pytest.fixture
def vault(root_committed, monkeypatch):
    monkeypatch.setattr(semantic, "_embedder", FakeEmbed())
    (root_committed / ".keepfold").mkdir(exist_ok=True)
    return Vault(root_committed)


def _blocked_msg(blocked) -> str:
    return "outbound connections:\n" + "\n".join(f"{h}\n{st}" for h, st in blocked)


def test_the_child_guard_refuses_an_outside_address(tmp_path):
    """Without this a broken wrapper would let the MCP check below pass vacuously."""
    log = tmp_path / "blocked.txt"
    (tmp_path / "guard.py").write_text(GUARD)
    (tmp_path / "probe.py").write_text("import socket\n"
                                       "socket.create_connection(('example.invalid', 80))\n")
    r = subprocess.run([sys.executable, "-I", str(tmp_path / "guard.py"),
                        str(tmp_path / "probe.py")], capture_output=True, text=True,
                       env={**os.environ, "KEEPFOLD_G23_BLOCKED": str(log)})
    assert r.returncode != 0 and "G23: outbound connection" in r.stderr
    assert log.read_text().split() == ["example.invalid"]


def test_the_app_answers_status_over_loopback(vault, offline):  # noqa: F811
    srv, app = _serve(vault.root)
    try:
        status, js, _, _ = call(app, "GET", "/api/status")
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 200 and js and js["connects"]
    assert not offline, _blocked_msg(offline)


def test_keepfold_mcp_over_stdio_starts_and_lists_tools(vault, tmp_path):
    (tmp_path / "guard.py").write_text(GUARD)
    log = tmp_path / "blocked.txt"
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("CLAUDE") and k.upper() != "PYTHONPATH"}
    env["KEEPFOLD_G23_BLOCKED"] = str(log)
    params = StdioServerParameters(
        command=sys.executable, env=env, cwd=str(tmp_path),
        args=["-I", str(tmp_path / "guard.py"), str(LAUNCH), "mcp", "--vault", str(vault.root)])

    async def main():
        with anyio.fail_after(90):
            async with Client(params) as client:  # initialize
                return {t.name for t in (await client.list_tools()).tools}

    names = anyio.run(main)
    assert {"keepfold_prime", "keepfold_find"} <= names, names
    assert not log.exists(), f"the MCP server tried to reach: {log.read_text()}"


def test_a_chat_answer_needs_no_model_and_no_network(vault, offline, monkeypatch):  # noqa: F811
    monkeypatch.setattr(semantic, "load_embedder", lambda *a, **k: None)  # no search model
    events: list = []
    chat.reply(vault, None, "What did I do yesterday?", None, events.append, threading.Event(),
               chat.get_settings(), today=TODAY)  # runner None: no local model either
    assert not [e for e in events if e["t"] == "err"], events
    assert [e["model"] for e in events if e["t"] in ("start", "done")] == ["Keepfold", "Keepfold"]
    assert "".join(e["s"] for e in events if e["t"] == "tok")
    assert not offline, _blocked_msg(offline)
