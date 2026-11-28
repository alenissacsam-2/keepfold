"""cloudllm and the browse runner router (docs/design/data-access.md §2-3). One test per §3
guardrail. No network: a fake opener stands in for the HTTPS endpoint."""

import io
import json
import urllib.error
from dataclasses import replace

import pytest
from test_browse_sites import OWNER, hist, imp, vault  # noqa: F401  (fixtures)

from keepfold import browse_serve as bs
from keepfold import cloudllm, localllm
from keepfold.vault import Vault

CONFIG = {"cloudllm": {"provider": "openai", "model": "gpt-x", "api_key_env": "TEST_CLOUD_KEY"}}
HIDDEN = ("pornhub", "adulttitle", "adultterm", "adultvid", "healthmarker", "webmd")


class Opener:
    """Records every request; answers like an OpenAI-compatible chat endpoint."""

    def __init__(self, content='{"category": "other", "description": "A site.", '
                               '"related": [], "topics": []}'):
        self.reqs, self.content = [], content

    def open(self, req, timeout=None):
        self.reqs.append(req)
        return io.BytesIO(json.dumps(
            {"choices": [{"message": {"content": self.content}}]}).encode())

    def sent(self) -> str:
        return "\n".join(r.data.decode("utf-8") for r in self.reqs).lower()


class Local:
    """The local runner: records the site names it was asked about."""

    def __init__(self):
        self.asked = []

    def site(self, name, titles, labels, candidates, topics, *, budget=None):
        self.asked.append(name)
        return {"category": "other", "description": "Local.", "related": [], "topics": []}


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TEST_CLOUD_KEY", "test-key-123")


@pytest.fixture
def opener(monkeypatch):
    o = Opener()
    monkeypatch.setattr(cloudllm, "_OPENER", o)
    return o


def deepen(monkeypatch, vault, settings, *, cloud=True, flip=None):  # noqa: F811
    """run_deepen with a fake local runner, the real cloud runner over the fake opener, and
    `settings` read on every call (flip(n) may change them after the nth cloud request)."""
    local = Local()
    cur = [settings]
    monkeypatch.setattr(bs, "load_settings", lambda config=None: cur[0])
    monkeypatch.setattr(bs, "local_runner",
                        lambda budget_s=None: (local, localllm.Budget(100)))
    monkeypatch.setattr("keepfold.taint.config", lambda: CONFIG if cloud else {})
    monkeypatch.setattr("keepfold.browse.sites.update_vectors", lambda v, say=None, **kw: None)
    if flip:
        real = cloudllm.CloudRunner._post

        def post(self, url, body, timeout):
            out = real(self, url, body, timeout)
            cur[0] = flip(self.items, cur[0])
            return out
        monkeypatch.setattr(cloudllm.CloudRunner, "_post", post)
    r = bs.run_deepen(Vault(vault), say=lambda s: None)
    return r, local


# ---- 1, 2: a local-audience item never reaches the cloud ------------------------------------

def test_hidden_maybe_and_skipped_sites_never_reach_the_cloud(vault, hist, key, opener,  # noqa: F811
                                                               monkeypatch):
    imp(vault, hist)  # adult hidden, health visible at import
    # now health is skipped: webmd is not shown at all; adult (incl. the "nsfw" maybe) hidden
    s = replace(OWNER, skip_categories=("health",), cloud_processing=True)
    r, local = deepen(monkeypatch, vault, s)
    assert r["described"] and opener.reqs and local.asked
    sent = opener.sent()
    for w in HIDDEN:
        assert w not in sent, w
    assert "lichess" in sent  # a visible site did go
    assert "pornhub.com" in local.asked and "webmd.com" not in local.asked
    for req in opener.reqs:  # only HTTPS to the configured host, with the key from the env
        assert req.full_url == "https://api.openai.com/v1/chat/completions"
        assert req.get_header("Authorization") == "Bearer test-key-123"


def test_with_cloud_processing_off_nothing_goes_to_the_cloud(vault, hist, key, opener,  # noqa: F811
                                                              monkeypatch):
    imp(vault, hist)
    r, local = deepen(monkeypatch, vault, OWNER)  # cloud_processing defaults to False
    assert r["described"] and local.asked and not opener.reqs


def test_the_router():
    on = replace(OWNER, cloud_processing=True)
    loc, cl = object(), object()
    assert bs.runner_for("cloud", loc, cl, on) is cl
    assert bs.runner_for("cloud", loc, cl, OWNER) is loc
    assert bs.runner_for("cloud", loc, None, on) is loc
    assert bs.runner_for("local", loc, cl, on) is loc
    assert bs.runner_for("local", None, cl, on) is None  # no cloud fallback, ever
    assert bs.runner_for("skip", loc, cl, on) is None


# ---- 3: secrets are scrubbed ----------------------------------------------------------------

def test_a_secret_skips_the_call(key, opener):
    r = cloudllm.find(CONFIG)
    assert r.site("x.org", ["my api_key=abcdefghijklmnopq"], ["other"], [], []) is None
    assert not opener.reqs and r.skipped == 1 and r.items == 0
    assert r.site("x.org", ["A plain title"], ["other"], [], []) is not None
    assert len(opener.reqs) == 1


# ---- 4: the kill switch ---------------------------------------------------------------------

def test_turning_cloud_processing_off_stops_the_next_call(vault, hist, key, opener,  # noqa: F811
                                                           monkeypatch):
    imp(vault, hist)
    s = replace(OWNER, cloud_processing=True)
    r, local = deepen(monkeypatch, vault, s,
                      flip=lambda n, cur: replace(cur, cloud_processing=False))
    assert len(opener.reqs) == 1  # the first visible site, then the switch was off
    assert r["described"] > 1 and len(local.asked) == r["asked"] - 1


# ---- 5: the audit line holds counts only ----------------------------------------------------

def test_the_audit_line_holds_counts_only(vault, hist, key, opener, monkeypatch):  # noqa: F811
    imp(vault, hist)
    deepen(monkeypatch, vault, replace(OWNER, cloud_processing=True))
    lines = (vault / ".keepfold" / "cloud-log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert set(row) == {"ts", "provider", "model", "items", "chars", "skipped"}
    assert row["provider"] == "openai" and row["model"] == "gpt-x"
    assert row["items"] == len(opener.reqs) and row["chars"] > 0
    assert "lichess" not in lines[0] and "test-key" not in lines[0]


# ---- 6: the spending cap --------------------------------------------------------------------

def test_max_items_is_respected(key, opener):
    assert cloudllm.find(CONFIG).max_items == 500
    r = cloudllm.find({"cloudllm": {**CONFIG["cloudllm"], "max_items": 2}})
    for n in range(4):
        r.site(f"s{n}.org", ["A title"], ["other"], [], [])
    assert len(opener.reqs) == 2 and r.items == 2


# ---- 7: only HTTPS to the configured host, no redirects -------------------------------------

@pytest.mark.parametrize("base", ["http://api.example.com/v1", "ftp://x.org", "https://u:p@x.org",
                                  "https://x.org /v1", "not a url"])
def test_a_non_https_base_url_gives_no_runner(key, base):
    assert cloudllm.find({"cloudllm": {**CONFIG["cloudllm"], "base_url": base}}) is None


def test_requests_off_the_configured_https_host_are_refused(key, opener):
    r = cloudllm.find(CONFIG)
    for url in ("http://api.openai.com/v1/chat/completions",
                "https://evil.example/v1/chat/completions"):
        with pytest.raises(ValueError):
            r._post(url, {}, 1)
    assert not opener.reqs


def test_redirects_are_refused():
    assert any(isinstance(h, localllm._NoRedirect) for h in cloudllm._OPENER.handlers)
    with pytest.raises(urllib.error.HTTPError):
        localllm._NoRedirect().redirect_request(
            type("R", (), {"full_url": "https://x.org"})(), None, 302, "", {}, "https://y.org")


def test_localllm_still_refuses_a_cloud_url():
    with pytest.raises(ValueError, match="loopback"):
        localllm.Runner("openai", "https://api.openai.com/v1", "m")._post(
            "https://api.openai.com/v1/chat/completions", {}, 1)


# ---- 8: no key, no runner -------------------------------------------------------------------

def test_no_key_env_var_gives_no_runner(monkeypatch):
    monkeypatch.delenv("TEST_CLOUD_KEY", raising=False)
    assert cloudllm.find(CONFIG) is None
    assert cloudllm.find({}) is None


def test_presets_and_the_key_never_shows(key):
    for p, host in (("openrouter", "openrouter.ai"), ("gemini", "generativelanguage.googleapis"),
                    ("anthropic", "api.anthropic.com")):
        r = cloudllm.find({"cloudllm": {**CONFIG["cloudllm"], "provider": p}})
        assert host in r.base and r.provider == p
    r = cloudllm.find(CONFIG)
    assert "test-key" not in repr(r) and "test-key" not in r.describe()


def test_api_key_env_names_a_secret_variable_only(monkeypatch):
    """r1 S10: only a NAME such as OPENAI_API_KEY; PATH, HOME or a lower-case or unrelated
    variable is refused at save time, and `find` never sends one as the bearer token."""
    for ok in ("OPENAI_API_KEY", "TEST_CLOUD_KEY", "HF_TOKEN", "MY_LLM_API_KEY"):
        cloudllm.check_env_name(ok)
    for bad in ("PATH", "HOME", "USERPROFILE", "openai_api_key", "_KEY", "K", "A B_KEY",
                "OPENAI_API_KEY_", "SECRET", "X" * 61 + "_KEY", "1PASS_KEY", "sk-abc_KEY"):
        with pytest.raises(ValueError):
            cloudllm.check_env_name(bad)
    monkeypatch.setenv("HOME", "test-key-123")
    cfg = {"cloudllm": {**CONFIG["cloudllm"], "api_key_env": "HOME"}}
    assert cloudllm.find(cfg) is None


# ---- T28: a public-looking https name that points inside the network ------------------------

@pytest.fixture(autouse=True)
def resolver(monkeypatch):
    """Every name resolves through this table (default: a public address); no DNS in a test."""
    table = {}
    monkeypatch.setattr(cloudllm, "_resolve",
                        lambda host, port: table.get(host, ["93.184.216.34"]))
    return table


@pytest.mark.gate("G8")
@pytest.mark.parametrize("host", [
    "169.254.169.254", "10.0.0.5", "172.16.0.1", "192.168.1.10", "127.0.0.1", "localhost",
    "[::1]", "[fe80::1]", "[fd00::1]", "0.0.0.0", "224.0.0.1", "100.64.0.1", "[::ffff:10.0.0.1]",
    "metadata.example", "rebind.example", "v6.example"])
def test_private_and_link_local_hosts_are_refused(key, opener, resolver, host):
    resolver.update({"localhost": ["127.0.0.1", "::1"], "metadata.example": ["169.254.169.254"],
                     "rebind.example": ["93.184.216.34", "10.1.2.3"],  # one bad answer is enough
                     "v6.example": ["fe80::1%eth0"]})
    base = f"https://{host}/v1"
    runner = cloudllm.find({"cloudllm": {**CONFIG["cloudllm"], "base_url": base}})
    with pytest.raises(ValueError, match="private or local"):
        runner._post(base + "/chat/completions", {}, 1)
    assert not opener.reqs
    # the owner's own server on purpose: the same host goes through
    own = cloudllm.find({"cloudllm": {**CONFIG["cloudllm"], "base_url": base,
                                      "local_server": True}})
    own._post(base + "/chat/completions", {}, 1)
    assert len(opener.reqs) == 1
    # only a literal true counts
    assert not cloudllm.find({"cloudllm": {**CONFIG["cloudllm"], "base_url": base,
                                           "local_server": "yes"}}).local_server


def test_a_public_host_and_an_unresolvable_one_still_send(key, opener, monkeypatch):
    runner = cloudllm.find(CONFIG)
    runner._post("https://api.openai.com/v1/chat/completions", {}, 1)
    assert len(opener.reqs) == 1

    def gone(host, port):
        raise OSError("no DNS")  # the request then fails on its own; no private address passed
    monkeypatch.setattr(cloudllm, "_resolve", gone)
    runner._post("https://api.openai.com/v1/chat/completions", {}, 1)
    assert len(opener.reqs) == 2
