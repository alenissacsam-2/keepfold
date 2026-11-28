"""Agent X-ray: what prime and context served each AI app, and its dry runs.

Pure moves out of `App` (server.py): the methods keep their bodies; `App` inherits this
mixin and its get/post tables still bind `self.<name>`."""

from __future__ import annotations

import time
from contextlib import contextmanager

from keepfold.app.routes._common import DRY_IDLE_S


class XrayRoutes:
    # ---- Agent X-ray (v9 item 6): what prime and context served each AI app ----
    def xray(self) -> dict:
        from keepfold import xray
        return xray.view(self._vault())

    def _xray_set(self, which: str, body: dict) -> dict:
        """Pin or Mute (`on`, default true) one id, or a private one by its view `token`. The
        app's click is the only way in: no MCP tool sets these (keepfold.xray)."""
        from keepfold import xray
        v, on = self._vault(), body.get("on", True)
        id = xray.resolve_token(v, body["token"]) if isinstance(body.get("token"), str) \
            else body.get("id")
        if not isinstance(on, bool) or id is None:
            raise ValueError("Pin or mute takes one page or claim id.")
        with self.reviewing:
            xray.set_flag(v, which, id, on)
        return self.xray()

    def xray_pin(self, body: dict) -> dict:
        return self._xray_set("pin", body)

    def xray_mute(self, body: dict) -> dict:
        return self._xray_set("mute", body)

    def xray_dryrun(self, body: dict) -> dict:
        """What keepfold_prime would serve one AI app for a prompt (mcp_server.dry_prime), shown
        as an X-ray call: titles only. Nothing is written or logged, the prompt included."""
        from keepfold import xray
        from keepfold.mcp_server import dry_prime
        client, prompt = body.get("client"), body.get("prompt")
        if not isinstance(client, str) or len(client) > 100:
            raise ValueError("client must be an AI app's name, at most 100 characters.")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
            raise ValueError("Type a prompt of at most 2,000 characters.")
        with self._dry_index() as (v, index):
            got = dry_prime(v, client, prompt, index=index)
        return xray.present(v, got["served"], got["trimmed"], got["chars"])

    @contextmanager
    def _dry_index(self):
        """(the vault, X-ray's one read-only Index for it), held by this request alone: built
        by the first dry run or compare, kept for the next (dry_prime sets each run's withhold
        and refreshes it by stat), replaced for another vault and let go by `drop_dry`. A run
        that fails drops it, so a half-done refresh is never reused."""
        from keepfold.catalog import Catalog
        from keepfold.index import Index
        v = self._vault()
        with self.dry:
            if self.dry_index is None or self.dry_index[0] != v.root:
                self._close_dry()
                self.dry_index = (v.root, Index(v, Catalog.load(v), save=False))
            try:
                yield v, self.dry_index[1]
            except BaseException:
                self._close_dry()
                raise
            finally:
                self.dry_used = time.monotonic()

    def _close_dry(self) -> None:  # under self.dry
        if self.dry_index is not None:
            self.dry_index[1].db.close()
            self.dry_index = None

    def drop_dry(self, now: float | None = None) -> bool:
        """Let X-ray's shared index go once unused for DRY_IDLE_S or once another vault is open
        (`start`'s watch checks); never while a request holds it. True when it went."""
        if not self.dry.acquire(blocking=False):  # in use: not idle
            return False
        try:
            now = time.monotonic() if now is None else now
            if self.dry_index is None or (now - self.dry_used < DRY_IDLE_S
                                          and self.dry_index[0] == self.root):
                return False
            self._close_dry()
            return True
        finally:
            self.dry.release()

    @staticmethod
    def _xray_client(body: dict, key: str = "client") -> str:
        c = body.get(key)
        if not isinstance(c, str) or len(c) > 100:
            raise ValueError("client must be an AI app's name, at most 100 characters.")
        return c

    def xray_trace(self, body: dict) -> dict:
        """R27b: which of what Keepfold gave one AI app lately a pasted reply echoes
        (xray.trace, local words only). The reply is never written or logged: not here, not in
        an X-ray row or event, and an error answers with its type or our own message only."""
        from keepfold import xray
        client, text = self._xray_client(body), body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > xray.TRACE_MAX:
            raise ValueError(f"Paste a reply of at most {xray.TRACE_MAX:,} characters.")
        return xray.trace(self._vault(), client, text)

    def xray_compare(self, body: dict) -> dict:
        """R27b: two AI apps' dry runs for one prompt and what each one's profile withholds
        (xray.compare). Nothing is written or logged, the prompt included."""
        from keepfold import xray
        a, b, prompt = self._xray_client(body, "a"), self._xray_client(body, "b"), \
            body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
            raise ValueError("Type a prompt of at most 2,000 characters.")
        if (a or "unknown") == (b or "unknown"):
            raise ValueError("Pick two different apps.")
        with self._dry_index() as (v, index):
            return xray.compare(v, prompt, a, b, index=index)

    def xray_ack(self, body: dict) -> dict:
        """R27b: Got it on an app's broad-read warning; like Pin and Mute, only this click sets
        it (xray.ack)."""
        from keepfold import xray
        client, end = self._xray_client(body), body.get("end")
        if not isinstance(end, str) or len(end) > 40:
            raise ValueError("end must be the warning's time.")
        v = self._vault()
        with self.reviewing:
            xray.ack(v, client, end)
        return self.xray()
