"""The shared secret rules (A13, D6 T30 and T31): every rule catches its own kind of key, prose
does not hit, and a log line holds neither a secret nor a home path. Every key-shaped string is
assembled from parts at run time, so none sits in the repo for GitHub push protection or our
own privacy scan to flag."""

import time
from pathlib import Path

import pytest

from keepfold import chats, secret_rules, vault
from keepfold.app import server
from keepfold.secret_rules import SECRET_RES, log_safe, mask

FIXTURES = Path(__file__).parent / "fixtures"
# fixture files that hold a secret on purpose: the redaction tests need one in each export format
SECRET_FIXTURES = {
    "chats/D--proj-alpha/aaaa-1111.jsonl",
    "exports/chatgpt_conversations.json",
    "exports/gemini_MyActivity.json",
    "exports/perplexity_conversations.json",
    "exports/takeout_html/Takeout/My Activity/Gemini Apps/MyActivity.html",
}


def corpus() -> dict[str, str]:
    a1b2 = "A1b2"
    return {
        "openai": "sk" + "-" + a1b2 * 6,
        "github": "gh" + "p_" + "A1" * 15,
        "github_pat": "github" + "_pat_" + a1b2 * 6,
        "aws": "AK" + "IA" + "ABCD1234EFGH5678",
        "pem": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
        "api_key": "api_" + "key = " + "abcdef123456ghij",
        "stripe": "sk_" + "live_" + "a1B2c3D4e5F6g7H8",
        "slack": "xox" + "b-" + "1234567890-abcdef",
        "google": "AI" + "za" + "A1" * 17 + "A",
        "gitlab": "glp" + "at-" + a1b2 * 5,
        "huggingface": "h" + "f_" + "abcd" * 8,
        "npm": "np" + "m_" + a1b2 * 9,
        "jwt": "ey" + "JhbGciOiJIUzI1" + "." + "ey" + "JzdWIiOiIxMjM0" + "." + "abcDEF123456",
        "url_credentials": "post" + "gres://user:hunt" + "er2@db.local",
        "password": "pass" + "word: " + "hunter22x",
        "wallet_key": "private " + "key: 0x" + "ab12" * 16,
        "azure_storage": "Account" + "Key=" + a1b2 * 21 + "A1" + "==",
        "discord": "M" + a1b2 * 6 + "." + "A1b2c3" + "." + a1b2 * 7,
        "telegram": "1234567890" + ":" + "AA" + a1b2 * 8 + "c",
        "sendgrid": "S" + "G." + a1b2 * 4 + "." + "C3d4" * 4,
        "bearer": "Authorization: " + "Bear" + "er " + "abc123" * 4,
        "token_assignment": "access_" + "token=" + "a1b2c3d4" * 3,
        "secret_assignment": "client_" + "secret: " + "Zq9xLm2PvT7wKd4R",
        "session_cookie": "Cookie: theme=dark; sess" + "ion_id=" + a1b2 * 6,
        "env_key": "export STRIPE_" + "SECRET=" + "a1b2c3d4e5f6g7h8",
        "env_token": "SERVICE_API_" + "TOKEN='" + "q9w8e7r6t5y4u3i2" + "'",
    }


def test_rule_set_catches_the_corpus():
    c = corpus()
    for name, text in c.items():
        assert vault.secret_pattern(text), name
        with pytest.raises(vault.VaultError):
            vault.scan_secrets(f"a note line\n{text}\nmore prose")
        assert chats.redact(text) != text, name
    # every rule has an example of its own that it catches
    for rx in SECRET_RES:
        assert any(rx.search(t) for t in c.values()), rx.pattern
    # no pattern holds ": " (render.py splits "path: pattern" on it)
    assert not any(": " in rx.pattern for rx in SECRET_RES)


def test_one_list_shared():
    assert vault.SECRET_RES is SECRET_RES is secret_rules.SECRET_RES
    t = corpus()["sendgrid"]
    assert "[redacted]" in chats.redact(f"key {t} end") == "key [redacted] end"
    assert mask(t) == "[redacted]"


PROSE = [
    "the token = the smallest unit of text",
    "A token: the smallest unit a model reads, about four characters.",
    "secret= is how some config files spell it, but a note should never hold one",
    "tokens: 1000000 per minute is the rate limit",
    "token=2024-01-15 and secret: 1234567890123456789",
    "Authorization: Bearer tokens are sent in a header",
    "Set the session = the study block you planned; sid = the student id",
    "SECRET_KEY= in a .env file holds the signing key",
    "API_TOKEN=your-api-token-here",
    "export TOKEN=<paste-here>",
    "The password manager stores them; use a passphrase, never reuse one.",
    "SendGrid and Discord and Telegram all issue bot tokens.",
    "AccountKey is the name Azure gives the storage key.",
    "Cookie: theme=dark; lang=en",
]


@pytest.mark.parametrize("text", PROSE)
def test_no_hit_on_ordinary_prose(text):
    assert vault.secret_pattern(text) is None


def test_no_hit_on_fixture_prose():
    hits = {}
    for p in sorted(FIXTURES.rglob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary fixture
        if (rx := vault.secret_pattern(text)):
            hits[p.relative_to(FIXTURES).as_posix()] = rx
    assert set(hits) <= SECRET_FIXTURES, hits
    # the rules added in A13 hit nothing under fixtures, the deliberate ones included
    assert not [p for p in FIXTURES.rglob("*.*") if p.suffix in {".md", ".json", ".jsonl", ".html"}
                and any(rx.search(p.read_text(encoding="utf-8", errors="ignore"))
                        for rx in SECRET_RES[16:])]


def test_logs_hold_no_canary(home, capsys):
    canary = "sk" + "-" + "Canary9" * 4
    path = home / "notes" / "private.md"

    def boom():
        fwd = str(path).replace("\\", "/")
        raise RuntimeError(f"cannot write {path} with key {canary} as {fwd}")

    try:
        boom()
    except RuntimeError as e:
        line = log_safe(e)
        _ = e
    assert line.startswith("RuntimeError: cannot write ~")
    assert "test_secrets.py" in line  # the raising frame, enough to debug
    assert canary not in line and str(home) not in line
    assert str(home).replace("\\", "/") not in line

    # the server's error path: a failing job logs through log_safe
    jobs = server.Jobs()

    def fail(job):
        raise RuntimeError(f"{canary} in {path}")

    jobs.start("t", "home", fail)
    for _ in range(200):
        if not jobs.cur.running:
            break
        time.sleep(0.02)
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert canary not in err and str(home) not in err and "Traceback" not in err


def test_rules_stay_linear_on_keyword_runs():
    """A long run of keywords with no space must not make a scan quadratic."""
    runs = ["token==" * 50000, "secret" * 40000, "a1" * 100000, "SERVICE_KEY" * 20000,
            "sid" + " " * 100000 + "x", "token=" + "a" * 100000]
    t0 = time.perf_counter()
    for r in runs:
        mask(r)
    assert time.perf_counter() - t0 < 5
