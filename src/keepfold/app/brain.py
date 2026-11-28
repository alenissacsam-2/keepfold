"""The brain listener (D12 B1): a TCP port on 127.0.0.1 where hooks (and later the MCP proxy)
reach the running app. Loopback is shared by every user on the PC, so nothing is served before
a mutual HMAC challenge keyed by the app's per-launch token (kept in app.json, owner-only).

Frames are one UTF-8 JSON object per line:
1. server, on accept: {"keepfold": "brain", "v": [1], "n": <64 hex>}
2. client, within CHALLENGE_S, at most HELLO_MAX bytes:
   {"v": 1, "n": <64 hex>, "mac": hex HMAC-SHA256(key, "c\\n" + n_server + "\\n" + n_client)}
3. server: {"ok": true, "mac": hex HMAC-SHA256(key, "s\\n" + n_client + "\\n" + n_server),
   "app": <version>}, or {"ok": false, "why": "auth" | "version"} and close. A line that is not
   a JSON object (an HTTP request, say) is closed with no answer.
4. client checks the mac, then sends one op line (at most OP_MAX bytes); `prime` answers
   {"ok": false, "why": "not_ready"} until it is wired, any other op {"ok": false, "why": "op"}.

~/.keepfold/brain.json {port, v, pid, hook} is discovery only and never holds the key. Frames,
payloads and the key are never logged: errors log their type name only."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
from importlib.metadata import version
from pathlib import Path

import anyio
import anyio.from_thread
import anyio.lowlevel
from anyio.abc import SocketListener, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

HOST = "127.0.0.1"
V = 1
PRE_AUTH_MAX = 8  # sockets that have not passed the challenge yet
CONN_MAX = 64  # sockets in all
CHALLENGE_S = 2.0  # from accept to the client's answer (anyio's clock is monotonic)
HELLO_MAX = 1024  # the client's challenge line
OP_MAX = 1 << 20  # an op line
_HEX64 = re.compile(r"[0-9a-f]{64}")
_QUIET = (anyio.EndOfStream, anyio.IncompleteRead, anyio.DelimiterNotFound,
          anyio.BrokenResourceError, anyio.ClosedResourceError, OSError, TimeoutError)


def brain_file() -> Path:
    return Path.home() / ".keepfold" / "brain.json"


def mac(key: bytes, label: str, first: str, second: str) -> str:
    return hmac.new(key, f"{label}\n{first}\n{second}".encode(), hashlib.sha256).hexdigest()


def _line(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


def _parse(raw: bytes) -> dict | None:
    try:
        obj = json.loads(raw)
    except ValueError:  # an HTTP request line, binary noise, bad UTF-8
        return None
    return obj if isinstance(obj, dict) else None


class Brain:
    """The listener, serving on its own thread from construction until `close`."""

    def __init__(self, token: str, *, challenge_s: float = CHALLENGE_S):
        self.key = token.encode()
        self.challenge_s = challenge_s
        self.app_version = version("keepfold")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no second binder on our port
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((HOST, 0))
            sock.listen(PRE_AUTH_MAX * 2)
        except BaseException:
            sock.close()
            raise
        self._sock = sock
        self.address = sock.getsockname()
        self.port = self.address[1]
        self._pre = self._all = 0  # touched on the loop thread only
        self._scope: anyio.CancelScope | None = None
        self._token: anyio.lowlevel.EventLoopToken | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="keepfold-brain")
        self._thread.start()
        self._ready.wait(10)
        try:
            if self._thread.is_alive():  # never point hooks at a listener that died at start
                self._save()
        except OSError:  # no discovery file: hooks take today's cold path
            print("keepfold brain: brain.json not written", file=sys.stderr)

    # -- discovery -------------------------------------------------------------------------

    def _save(self) -> None:
        from keepfold import atomic
        p = brain_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        # owner-only: atomic.replace's temp file is created 0600 (POSIX); on Windows the
        # profile's inherited ACL guards it, as it does app.json
        atomic.replace(p, json.dumps({"port": self.port, "v": [V], "pid": os.getpid(),
                                      "hook": False}), newline=None)

    def _drop(self) -> None:
        try:
            s = json.loads(brain_file().read_text(encoding="utf-8"))
            if s.get("pid") == os.getpid() and s.get("port") == self.port:  # never another app's
                brain_file().unlink()
        except (OSError, ValueError, AttributeError):
            pass

    # -- lifecycle -------------------------------------------------------------------------

    def _run(self) -> None:
        try:
            anyio.run(self._main)
        except Exception as e:  # noqa: BLE001  (the app runs on without a brain)
            print(f"keepfold brain: stopped ({type(e).__name__})", file=sys.stderr)
            self._sock.close()  # nobody accepts: refuse at once rather than hang clients
        finally:
            self._ready.set()

    async def _main(self) -> None:
        self._token = anyio.lowlevel.current_token()
        listener = await SocketListener.from_socket(self._sock)
        with anyio.CancelScope() as self._scope:
            self._ready.set()
            async with listener:
                await listener.serve(self._handle)

    def close(self) -> None:
        """Stop listening, cut every connection and remove brain.json."""
        self._drop()
        if self._scope is not None and self._thread.is_alive():
            try:
                anyio.from_thread.run_sync(self._scope.cancel, token=self._token)
            except RuntimeError:  # the loop ended between the check and the call
                pass
        self._thread.join(5)
        self._sock.close()

    # -- one connection --------------------------------------------------------------------

    async def _handle(self, stream: SocketStream) -> None:
        if self._pre >= PRE_AUTH_MAX or self._all >= CONN_MAX:
            await stream.aclose()
            return
        self._pre += 1
        self._all += 1
        pre = True
        try:
            async with stream:
                buf = BufferedByteReceiveStream(stream)
                n_s = secrets.token_hex(32)
                with anyio.move_on_after(self.challenge_s) as cut:
                    await stream.send(_line({"keepfold": "brain", "v": [V], "n": n_s}))
                    raw = await buf.receive_until(b"\n", HELLO_MAX)
                if cut.cancelled_caught:
                    return
                hello = _parse(raw)
                if hello is None:
                    return  # a silent close: no answer to an HTTP request or noise
                n_c, got = hello.get("n"), hello.get("mac")
                if not (isinstance(n_c, str) and _HEX64.fullmatch(n_c) and isinstance(got, str)
                        and hmac.compare_digest(got.encode(),
                                                mac(self.key, "c", n_s, n_c).encode())):
                    await stream.send(_line({"ok": False, "why": "auth"}))
                    return
                if hello.get("v") != V:
                    await stream.send(_line({"ok": False, "why": "version"}))
                    return
                self._pre -= 1
                pre = False
                await stream.send(_line({"ok": True, "mac": mac(self.key, "s", n_c, n_s),
                                         "app": self.app_version}))
                req = _parse(await buf.receive_until(b"\n", OP_MAX))
                if req is None:
                    return
                why = "not_ready" if req.get("op") == "prime" else "op"  # WB2 wires prime
                await stream.send(_line({"ok": False, "why": why}))
        except _QUIET:
            pass
        except Exception as e:  # noqa: BLE001  (one connection's bug never stops the listener)
            print(f"keepfold brain: a connection failed ({type(e).__name__})", file=sys.stderr)
        finally:
            if pre:
                self._pre -= 1
            self._all -= 1
