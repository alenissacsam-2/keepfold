"""The one list of secret-looking text: `vault.scan_secrets` (refuses a write), `chats.redact`
(masks kept chat text), the recall log and `log_safe` (log lines) all use it.

A hit refuses a write, so every rule needs a value that looks like a secret (length, letters and
digits mixed), never just a keyword: "the token = the smallest unit" must pass. No pattern may
hold ": " (render.py splits "path: pattern" on it).

The service-specific rules (Azure, Discord, Telegram, SendGrid, bearer headers, assignments,
cookies, .env lines) are adapted by hand from gitleaks, https://github.com/gitleaks/gitleaks
(MIT licence, (c) Zachary Rice).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# a value with a letter and a digit in it, `n` or more characters: a secret, not a word or a number
# (the look-aheads read at most 80 characters, so a long run of keywords stays linear)
_VAL = r"(?=[\w+/=.%~-]{0,80}[A-Za-z])(?=[\w+/=.%~-]{0,80}\d)[\w+/=.%~-]{N,}"


def _qv(n: int) -> str:
    return r"""\s*[:=]\s*["']?""" + _VAL.replace("{N,}", f"{{{n},}}")


SECRET_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"api[_-]?key\s*[:=]\s*\S{12,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),  # Stripe
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),  # Google API key
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),  # GitLab
    re.compile(r"\bhf_[A-Za-z]{30,}"),  # Hugging Face
    re.compile(r"\bnpm_[A-Za-z0-9]{36}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),  # credentials in a URL
    re.compile(r"\bpassword\s*[:=]\s*\S{6,}", re.IGNORECASE),
    # a 64-hex value soon after a private-key word (pasted wallet keys); bare tx hashes pass
    re.compile(r"priv(?:ate)?[_ -]?key[^\n]{0,40}?(?<![0-9a-f])(?:0x)?[0-9a-f]{64}(?![0-9a-f])",
               re.IGNORECASE),
    # --- adapted from gitleaks (see the top) ---
    re.compile(r"\bAccountKey\s*=\s*[A-Za-z0-9+/]{86}==", re.IGNORECASE),  # Azure storage
    # Discord bot token: id.timestamp.hmac
    re.compile(r"\b[MNO][A-Za-z\d_-]{23,25}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,38}\b"),
    re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b"),  # Telegram bot token
    re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),  # SendGrid
    re.compile(r"\bAuthorization\s*[:=]\s*[\"']?Bearer\s+(?=[\w.~+/=-]*\d)[\w.~+/=-]{20,}",
               re.IGNORECASE),
    # secret= / token= (also client_secret, access_token, ...)
    re.compile(r"(?:secret|token)[A-Za-z_]{0,20}" + _qv(16), re.IGNORECASE),
    # session cookies and ids
    re.compile(r"\b(?:session(?:_?id)?|sessid|phpsessid|jsessionid|connect\.sid|sid)"
               + _qv(20), re.IGNORECASE),
    # .env lines: UPPER_CASE names ending in KEY / TOKEN / SECRET / PASSWORD, or PASSWORD alone
    re.compile(r"^[ \t]*(?:export[ \t]+)?[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)"
               + _qv(12), re.MULTILINE),
]


def mask(text: str) -> str:
    """`text` with every secret-looking span replaced by [redacted]."""
    for rx in SECRET_RES:
        text = rx.sub("[redacted]", text)
    return text


def _tilde(text: str) -> str:
    """Home-folder paths (either slash, or JSON-doubled backslashes) shown as ~."""
    try:
        home = str(Path.home())
    except (RuntimeError, KeyError, OSError):
        return text
    for h in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}:
        if len(h) > 3:
            text = re.sub(re.escape(h), "~", text, flags=re.IGNORECASE if os.name == "nt" else 0)
    return text


def log_safe(e: BaseException) -> str:
    """One log line for an exception: its type, its message (secrets masked, home as ~) and the
    file:line of the frame that raised it. Never the stack text, which quotes source lines and
    local paths."""
    tb = e.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    where = f" ({tb.tb_frame.f_code.co_filename}:{tb.tb_lineno})" if tb else ""
    return _tilde(mask(f"{type(e).__name__}: {str(e)[:500]}{where}"))
