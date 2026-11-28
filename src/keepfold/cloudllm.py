"""Opt-in cloud processing of cloud-audience browsing (docs/design/data-access.md §2-3).

`find` builds a localllm.Runner (kind openai) for an HTTPS OpenAI-compatible endpoint from
`cloudllm: {provider, model, base_url?, api_key_env, max_items}` in ~/.keepfold/config.yaml. The
key comes only from the environment variable named by `api_key_env`; it is never stored, logged
or shown. Requests go only over https to the configured base, with no proxy and no redirect, and
not to a host that resolves to a private, loopback or link-local address (`local_server: true`
in the block allows one on purpose).
Before each call the prompt is checked with vault.secret_pattern and the call is skipped on a
match; at most `max_items` calls (default 500) per run. `audit` appends one counts-only line per
run to `<vault>/.keepfold/cloud-log.jsonl`. Who may reach this runner is decided only by
`browse_serve.runner_for`; localllm keeps its loopback-only guarantee.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from keepfold import localllm
from keepfold.vault import Vault, secret_pattern

log = logging.getLogger(__name__)

# OpenAI-compatible bases; `chat_path` is appended (checked 18 Nov 2026 in each provider's docs)
PRESETS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "anthropic": "https://api.anthropic.com/v1",
}
MAX_ITEMS = 500
LOG = ".keepfold/cloud-log.jsonl"
_REFUSED = "cloudllm: only https to the configured host is allowed"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), localllm._NoRedirect())
# r1 S10: the name of a secret, so PATH, HOME or USERPROFILE are never sent as a bearer token
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,63}")


def check_env_name(name) -> str:
    """Return `name` when it can name an API key variable (capitals, ending in _KEY, _TOKEN or
    _API_KEY); raise ValueError otherwise."""
    if not (isinstance(name, str) and _ENV_NAME.fullmatch(name)
            and name.endswith(("_KEY", "_TOKEN"))):
        raise ValueError("api_key_env is the NAME of the variable that holds your key: capitals, "
                         "ending in _KEY, _TOKEN or _API_KEY, such as OPENAI_API_KEY. Never "
                         "paste the key itself here.")
    return name


def _check(url) -> str:
    """Return url when it is a plain https URL with a host; raise ValueError otherwise."""
    if not isinstance(url, str) or any(c in url for c in "\\ \t\r\n\x00@"):
        raise ValueError(_REFUSED)
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018  (raises on a junk port)
    except ValueError:
        raise ValueError(_REFUSED) from None
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(_REFUSED)
    return url


def _resolve(host: str, port: int) -> list[str]:
    """The addresses `host` resolves to (tests replace this: no network in them)."""
    return [i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _public(url: str) -> None:
    """Raise ValueError when url's host is, or resolves to, a non-global address (private,
    loopback, link-local, multicast, reserved, unspecified). A name that does not resolve passes:
    the request then fails by itself. shortcut: the connection resolves again, so a name that
    changes between the two lookups gets through; pin the address if that ever matters."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        addrs = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addrs = _resolve(host, parts.port or 443)
        except OSError:
            return
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])  # a link-local IPv6 carries a %zone
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global or ip.is_multicast:
            raise ValueError(_REFUSED + "; this host is on a private or local address (set "
                             "cloudllm: {local_server: true} if that is on purpose)")


@dataclass
class CloudRunner(localllm.Runner):
    provider: str = "custom"
    key: str = field(default="", repr=False)
    max_items: int = MAX_ITEMS
    items: int = 0  # requests sent this run (a retry counts again)
    chars: int = 0
    skipped: int = 0  # calls refused for a secret
    local_server: bool = False  # the owner's own server on a private address
    chat_path = "/chat/completions"

    def _chat(self, system: str, user: str, **kw) -> str | None:
        if self.items >= self.max_items:
            return None
        if secret_pattern(system) or secret_pattern(user):
            self.skipped += 1
            return None
        self.items += 1
        self.chars += len(system) + len(user)
        return super()._chat(system, user, **kw)

    def _post(self, url: str, body: dict, timeout: float) -> dict:
        if not url.startswith(self.base + "/"):
            raise ValueError(_REFUSED)
        if not self.local_server:
            _public(_check(url))
        req = urllib.request.Request(_check(url), data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))


def find(config: dict | None = None) -> CloudRunner | None:
    """The configured cloud runner, or None when `cloudllm` is missing or incomplete, its key
    variable is unset, or the base is not https."""
    if config is None:
        from keepfold.taint import config as load_config
        config = load_config()
    c = config.get("cloudllm") if isinstance(config, dict) else None
    if not isinstance(c, dict):
        return None
    provider = str(c.get("provider") or "custom").strip().lower()
    base, model, env = c.get("base_url") or PRESETS.get(provider), c.get("model"), \
        c.get("api_key_env")
    try:
        key = os.environ.get(check_env_name(env), "").strip()
    except ValueError:
        key = ""
    if not (key and isinstance(model, str) and model.strip() and base):
        return None
    try:
        _check(base)
    except ValueError:
        log.warning("cloudllm: base_url refused; it must be https")
        return None
    n = c.get("max_items")
    n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else MAX_ITEMS
    return CloudRunner("openai", base.rstrip("/"), model.strip(), provider=provider, key=key,
                       max_items=n, local_server=c.get("local_server") is True)


def audit(vault: Vault, runner: CloudRunner) -> None:
    """One counts-only line for this run: time, provider, model, requests, characters sent."""
    line = json.dumps({"ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "provider": runner.provider, "model": runner.model,
                       "items": runner.items, "chars": runner.chars,
                       "skipped": runner.skipped})
    path = vault.root / LOG
    with vault.lock():
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        vault.write_text(path, old + line + "\n")
