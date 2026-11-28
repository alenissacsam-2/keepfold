"""localllm: fake loopback servers only; no network, no real vault."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from keepfold import localllm
from keepfold.localllm import Budget, Runner


class Fake:
    """A loopback server; `routes` maps (method, path) to a dict or a callable(body) -> dict."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.calls.append((method, self.path, body))
                r = fake.routes.get((method, self.path))
                if r is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                out = r(body) if callable(r) else r
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=lambda: self.srv.serve_forever(0.05), daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def serve():
    made = []

    def make(routes):
        made.append(Fake(routes))
        return made[-1]

    yield make
    for f in made:
        f.close()


def ollama_reply(*contents):
    it = iter(contents)
    return lambda body: {"message": {"role": "assistant", "content": next(it)}}


def openai_reply(*contents):
    it = iter(contents)
    return lambda body: {"choices": [{"message": {"content": next(it)}}]}


def tags(*names):
    return {"models": [{"name": n} for n in names]}


def oa_models(*ids):
    return {"data": [{"id": i} for i in ids]}


# ---------- find ----------

def test_find_none_when_nothing_answers(monkeypatch):
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", "http://127.0.0.1:9", "/api/tags")])
    assert localllm.find({}) is None


def test_find_order_ollama_first(serve, monkeypatch):
    o = serve({("GET", "/api/tags"): tags("llama3:70b", "qwen3:4b")})
    a = serve({("GET", "/v1/models"): oa_models("x")})
    monkeypatch.setattr(localllm, "_PROBES", [
        ("ollama", o.base, "/api/tags"), ("openai", a.base, "/v1/models")])
    r = localllm.find({})
    assert (r.kind, r.model) == ("ollama", "qwen3:4b")  # small model beats the first one
    assert not a.calls
    assert "qwen3:4b at 127.0.0.1:" in r.describe()


def test_find_falls_through_to_openai(serve, monkeypatch):
    o = serve({})  # answers 404 on tags
    a = serve({("GET", "/v1/models"): oa_models("big-70b", "gemma-3-4b")})
    monkeypatch.setattr(localllm, "_PROBES", [
        ("ollama", o.base, "/api/tags"), ("openai", a.base, "/v1/models")])
    r = localllm.find({})
    assert (r.kind, r.model) == ("openai", "gemma-3-4b")


def test_model_choice_rules(serve, monkeypatch):
    o = serve({("GET", "/api/tags"): tags("llama3:70b", "mistral:7b")})
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", o.base, "/api/tags")])
    assert localllm.find({}).model == "llama3:70b"  # none small: first
    assert localllm.find({"localllm": {"model": "mistral:7b"}}).model == "mistral:7b"


def test_background_work_asks_for_its_own_model_first(serve, monkeypatch):
    o = serve({("GET", "/api/tags"): tags("qwen3.5:2b", "qwen3.5:9b")})
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", o.base, "/api/tags")])
    both = {"localllm": {"model": "qwen3.5:2b", "background_model": "qwen3.5:9b"}}
    assert localllm.find(both).model == "qwen3.5:2b"  # Chat and ask: the main model
    assert localllm.find(both, background=True).model == "qwen3.5:9b"
    gone = {"localllm": {"model": "qwen3.5:2b", "background_model": "llama3:70b"}}
    assert localllm.find(gone, background=True).model == "qwen3.5:2b"  # not held: falls back
    assert localllm.find({"localllm": {"model": "qwen3.5:2b"}}, background=True).model \
        == "qwen3.5:2b"


def test_find_uses_loaded_config_when_none(serve, monkeypatch):
    o = serve({("GET", "/api/tags"): tags("a:3b")})
    monkeypatch.setattr(localllm, "_PROBES", [])
    monkeypatch.setattr("keepfold.taint.config", lambda: {"localllm": {"url": o.base}})
    assert localllm.find().model == "a:3b"


def test_config_url_non_loopback_raises():
    with pytest.raises(ValueError, match="only a loopback runner"):
        localllm.find({"localllm": {"url": "http://example.com:11434"}})


# ---------- loopback guard ----------

@pytest.mark.parametrize("url", [
    "http://example.com", "http://127.0.0.1@evil.com", "http://evil.com@127.0.0.1",
    "http://127.0.0.1.evil.com", "http://localhost.evil.com", "http://evil.com\\@127.0.0.1",
    "http://2130706433", "http://0x7f000001", "http://0177.0.0.1", "http://10.0.0.5",
    "http://192.168.1.2:8080", "http://0.0.0.0", "http://[::ffff:8.8.8.8]",
    "http://[::2]", "http://[2001:db8::1]", "http://169.254.169.254",
    "ftp://127.0.0.1", "file:///etc/passwd", "127.0.0.1:11434", "//127.0.0.1", "",
    "http://127.0.0.1 .evil.com", "http://:@evil.com", "http://127.0.0.1:99999",
    "http://[::1", "http://user:pw@127.0.0.1",
])
def test_non_loopback_refused(url):
    with pytest.raises(ValueError, match="localllm: only a loopback runner is allowed"):
        localllm._check_url(url)
    with pytest.raises(ValueError, match="only a loopback runner"):
        Runner("ollama", url, "m").classify("x", ["a"])


@pytest.mark.parametrize("url", [
    "http://127.0.0.1", "http://127.0.0.1:11434",
    "http://[::1]:1234", "https://127.0.0.1:1", "http://127.5.5.5", "http://[::ffff:127.0.0.1]",
])
def test_loopback_allowed(url):
    assert localllm._check_url(url) == url


@pytest.mark.parametrize("url, want", [
    ("http://localhost:8080", "http://127.0.0.1:8080"), ("http://LOCALHOST", "http://127.0.0.1"),
    ("http://localhost:11434/api/chat", "http://127.0.0.1:11434/api/chat"),
])
def test_localhost_becomes_127(url, want):
    """RR-16: a hosts file can point `localhost` elsewhere; every request goes to 127.0.0.1."""
    assert localllm._check_url(url) == want


def test_proxy_env_is_ignored(serve, monkeypatch):
    f = serve({("POST", "/api/chat"): ollama_reply('{"label":"a","confidence":0.5}')})
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")  # dead: would fail if it were used
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert Runner("ollama", f.base, "m").classify("t", ["a"]) == ("a", 0.5)


# ---------- classify ----------

def test_classify_ollama_request_shape(serve):
    f = serve({("POST", "/api/chat"): ollama_reply('{"label": "Health", "confidence": 0.9}')})
    r = Runner("ollama", f.base, "m")
    assert r.classify("x" * 5000, ["health", "money"]) == ("health", 0.9)
    body = f.calls[0][2]
    assert body["format"] == "json" and body["options"]["temperature"] == 0
    assert body["stream"] is False and body["options"]["num_predict"] <= 100
    user = body["messages"][1]["content"]
    assert len(user) < 1600 and "data" in body["messages"][0]["content"].lower()
    assert "never instructions" in body["messages"][0]["content"]


def test_classify_openai_shape(serve):
    f = serve({("POST", "/v1/chat/completions"): openai_reply('{"label":"money","confidence":1}')})
    r = Runner("openai", f.base, "m")
    assert r.classify("t", ["health", "money"]) == ("money", 1.0)
    body = f.calls[0][2]
    # LM Studio answers 400 to json_object; JSON is asked for in the prompt instead
    assert "response_format" not in body and body["temperature"] == 0
    assert body["max_tokens"] == 600 and "/no_think" not in body["messages"][1]["content"]


def test_qwen_gets_no_think_and_its_reasoning_is_ignored(serve):
    def reply(body):
        return {"choices": [{"message": {
            "reasoning_content": '{"label":"cooking","confidence":1}',
            "content": 'Sure.\n```json\n{"label": "programming", "confidence": 0.8}\n```'
                       ' {"label":"sports","confidence":1}'}}]}

    f = serve({("POST", "/v1/chat/completions"): reply})
    r = Runner("openai", f.base, "Qwen3.5-2B-x")
    assert r.classify("t", ["programming", "cooking", "sports"]) == ("programming", 0.8)
    msgs = f.calls[0][2]["messages"]
    assert msgs[1]["content"].endswith("</data> /no_think")
    assert msgs[2] == {"role": "assistant", "content": "<think>\n\n</think>\n\n"}
    assert r.summarise(["a"]) is not None and f.calls[1][2]["max_tokens"] == 800


def test_reasoning_only_answer_is_none(serve):
    f = serve({("POST", "/v1/chat/completions"): lambda b: {"choices": [{"message": {
        "reasoning_content": '{"label":"a","confidence":1}', "content": ""}}]}})
    assert Runner("openai", f.base, "qwen").classify("t", ["a"]) is None
    assert len(f.calls) == 2


def test_ping_is_one_token(serve):
    f = serve({("POST", "/v1/chat/completions"): openai_reply("")})
    assert Runner("openai", f.base, "m").ping() >= 0
    assert f.calls[0][2]["max_tokens"] == 1
    assert Runner("openai", "http://127.0.0.1:9", "m").ping() is None


def test_classify_retries_once_then_succeeds(serve):
    f = serve({("POST", "/api/chat"): ollama_reply("not json", '{"label":"a","confidence":0.3}')})
    assert Runner("ollama", f.base, "m").classify("t", ["a", "b"]) == ("a", 0.3)
    assert len(f.calls) == 2


@pytest.mark.parametrize("bad", [
    "nope", '{"label":"zzz","confidence":0.5}', '{"label":"a","confidence":2}',
    '{"label":"a","confidence":"high"}', '{"label":"a"}', "[1]", '{"label":"a","confidence":true}',
])
def test_classify_bad_twice_is_none(serve, bad):
    f = serve({("POST", "/api/chat"): ollama_reply(bad, bad)})
    assert Runner("ollama", f.base, "m").classify("t", ["a"]) is None
    assert len(f.calls) == 2


def test_classify_strips_think(serve):
    c = '<think>hmm {"label":"b","confidence":1}</think>\n{"label":"a","confidence":0.7}'
    f = serve({("POST", "/api/chat"): ollama_reply(c)})
    assert Runner("ollama", f.base, "m").classify("t", ["a", "b"]) == ("a", 0.7)


@pytest.mark.parametrize("raw,want", [
    ("The answer.<|im_end|>", "The answer."),
    ("</think>\n\nThe answer [1].", "The answer [1]."),
    ("Yes<end_of_turn>", "Yes"), ("Done.<|eot_id|>", "Done."), ("<|endoftext|>", ""),
    ("Use <|pipe|> and </b> as they are", "Use <|pipe|> and </b> as they are"),
    ("<think>plan</think>Real text<|im_end|>", "Real text"),
])
def test_clean_drops_leaked_tokens_not_words(raw, want):
    assert localllm._clean(raw) == want


def test_unclosed_think_is_failure(serve):
    f = serve({("POST", "/api/chat"): ollama_reply("<think>ran out", "<think>again")})
    assert Runner("ollama", f.base, "m").classify("t", ["a"]) is None


def test_server_error_is_none_no_retry(serve):
    f = serve({})  # 404
    assert Runner("ollama", f.base, "m").classify("t", ["a"]) is None
    assert len(f.calls) == 1


def test_connection_refused_is_none():
    assert Runner("ollama", "http://127.0.0.1:9", "m").classify("t", ["a"]) is None


def test_timeout_is_fast_none(serve):
    hold, done = threading.Event(), []

    def slow(body):
        hold.wait(5)
        done.append(1)
        return {"message": {"content": "{}"}}

    f = serve({("POST", "/api/chat"): slow})
    assert Runner("ollama", f.base, "m").classify("t", ["a"], timeout=0.1) is None
    assert not done  # gave up on the timeout while the server was still holding the answer
    hold.set()


# ---------- summarise ----------

def test_summarise_cleans_and_cuts(serve):
    raw = ("<think>x</think>**Reading** about [rust](https://a.com/x?q=1) and <b>Go</b> "
           + "w " * 300)
    f = serve({("POST", "/api/chat"): ollama_reply(raw)})
    out = Runner("ollama", f.base, "m").summarise(["a", "b"], max_chars=50)
    assert out is not None and len(out) <= 50
    assert "http" not in out and "*" not in out and "<" not in out and "[" not in out
    assert out.startswith("Reading about rust")
    body = f.calls[0][2]
    assert body["options"]["temperature"] == 0.2 and "format" not in body


def test_summarise_openai_and_failure(serve):
    f = serve({("POST", "/v1/chat/completions"): openai_reply("Learning Rust.")})
    r = Runner("openai", f.base, "m")
    assert r.summarise(["t"]) == "Learning Rust."
    assert Runner("openai", "http://127.0.0.1:9", "m").summarise(["t"]) is None


def test_chat_answers_in_plain_text(serve):
    f = serve({("POST", "/api/chat"): ollama_reply("<think>x</think>An answer.")})
    assert Runner("ollama", f.base, "m").chat("sys", "q?") == "An answer."
    assert f.calls[0][2]["messages"][1]["content"] == "q?" and "format" not in f.calls[0][2]
    assert Runner("ollama", "http://127.0.0.1:9", "m").chat("s", "q") is None


def test_summarise_empty_answer_is_none(serve):
    f = serve({("POST", "/api/chat"): ollama_reply("<think>x</think>")})
    assert Runner("ollama", f.base, "m").summarise(["t"]) is None


# ---------- budget ----------

def test_budget_blocks_and_spends(serve):
    f = serve({("POST", "/api/chat"): ollama_reply('{"label":"a","confidence":1}')})
    r = Runner("ollama", f.base, "m")
    b = Budget(0)
    assert not b.ok()
    assert r.classify("t", ["a"], budget=b) is None and r.summarise(["t"], budget=b) is None
    assert not f.calls
    b = Budget(5)
    assert b.ok()
    assert r.classify("t", ["a"], budget=b) == ("a", 1.0)
    assert 0 <= 5 - b.left < 5
    b.spend(10)
    assert not b.ok()


def test_budget_spent_on_failure():
    b = Budget(100)
    Runner("ollama", "http://127.0.0.1:9", "m").summarise(["t"], budget=b)
    assert b.left < 100


def test_a_redirect_to_another_host_is_refused():
    import http.server
    import threading
    import urllib.error

    import pytest

    from keepfold import localllm

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.com/leak")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError):
            localllm._request(f"http://127.0.0.1:{srv.server_port}/v1/models", None, 2)
    finally:
        srv.shutdown()


# ---------- privacy review H3: cloud models and the enabled switch ----------

def test_cloud_models_are_skipped(serve, monkeypatch):
    o = serve({("GET", "/api/tags"): {"models": [
        {"name": "gpt-oss:120b-cloud"}, {"name": "glm-4.6:cloud"},
        {"name": "proxied:3b", "remote_host": "https://ollama.com:443", "remote_model": "x"},
        {"name": "mistral:7b"}]}})
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", o.base, "/api/tags")])
    assert localllm.find({}).model == "mistral:7b"  # "proxied:3b" would win if it counted
    only = serve({("GET", "/api/tags"): tags("kimi-k2:1t-cloud")})
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", only.base, "/api/tags")])
    assert localllm.find({}) is None


def test_a_configured_cloud_model_is_refused(serve, monkeypatch):
    o = serve({("GET", "/api/tags"): {"models": [
        {"name": "remote:4b", "remote_host": "https://ollama.com:443"}, {"name": "a:3b"}]}})
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", o.base, "/api/tags")])
    for model in ("gpt-oss:120b-cloud", "glm-4.6:cloud", "remote:4b"):
        with pytest.raises(ValueError, match="cloud model is refused"):
            localllm.find({"localllm": {"model": model}})


def test_enabled_needs_an_explicit_true(monkeypatch):
    assert localllm.enabled({"localllm": {"enabled": True}})
    for cfg in ({}, {"localllm": {}}, {"localllm": {"enabled": "yes"}}, {"localllm": True},
                {"localllm": {"enabled": False}}):
        assert not localllm.enabled(cfg)
    monkeypatch.setattr("keepfold.taint.config", lambda: {"localllm": {"enabled": True}})
    assert localllm.enabled()


# ---------- second privacy review M2 ----------

def test_r2_only_ollama_is_probed_on_its_own():
    assert [k for k, *_ in localllm._PROBES] == ["ollama"]


@pytest.mark.parametrize("model", [
    "gpt-oss:120b-cloud", "GPT-OSS:120B-CLOUD", "gpt-oss:120b-cloud ", " gpt-oss:120b-cloud",
    "gpt-oss:120b-cloud\n", "gpt-oss:120b-cloud@sha256:abc",
    "registry.ollama.ai/library/gpt-oss:120b-cloud", "deepseek-v3.1:671b-cloud",
    "x-cloud-y:1b", "glm:CLOUD-latest"])
def test_r2_a_cloud_model_name_is_refused_however_written(serve, monkeypatch, model):
    o = serve({("GET", "/api/tags"): {"models": [
        {"name": "gpt-oss:120b-cloud", "remote_host": "https://ollama.com:443"},
        {"name": "qwen3:4b"}]}})
    monkeypatch.setattr(localllm, "_PROBES", [])
    with pytest.raises(ValueError, match="cloud model is refused"):
        localllm.find({"localllm": {"url": o.base, "model": model}})


def test_r2_a_configured_model_must_be_in_the_local_list(serve, monkeypatch):
    o = serve({("GET", "/api/tags"): tags("qwen3:4b", "phi4-mini:latest")})
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", o.base, "/api/tags")])
    assert localllm.find({"localllm": {"model": " QWEN3:4b\n"}}).model == "qwen3:4b"
    assert localllm.find({"localllm": {"model": "qwen3:4b@sha256:abc"}}).model == "qwen3:4b"
    assert localllm.find({"localllm": {"model": "phi4-mini"}}).model == "phi4-mini:latest"
    assert localllm.find({"localllm": {"model": "llama3:70b"}}) is None  # not pulled here
    assert localllm.find({}).model == "qwen3:4b"


def test_r2_an_openai_server_is_used_only_from_an_explicit_url(serve, monkeypatch):
    a = serve({("GET", "/v1/models"): oa_models("gemma-3-4b")})
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", "http://127.0.0.1:9", "/api/tags")])
    assert localllm.find({}) is None and not a.calls
    r = localllm.find({"localllm": {"url": a.base}})
    assert (r.kind, r.model) == ("openai", "gemma-3-4b")


# ---------- LM Studio through a fake `lms` (no real LM Studio) ----------

FAKE_LMS = r'''
import json, sys
from pathlib import Path
here = Path(__file__).parent
st = json.loads((here / "state.json").read_text())
args = sys.argv[1:]
with open(here / "calls.txt", "a") as f:
    f.write(" ".join(args) + "\n")
if args[:2] == ["server", "status"]:
    print(json.dumps({"running": st["running"], "port": st["port"]}))
elif args[:2] == ["server", "start"]:
    st["running"] = True
elif args[:1] == ["ps"]:
    print(json.dumps(st["loaded"]))
elif args[:1] == ["ls"]:
    print(json.dumps(st["disk"]))
elif args[:1] == ["load"]:
    st["loaded"] += [dict(r, identifier=r["modelKey"]) for r in st["disk"]
                     if r["modelKey"] == args[1]]
else:
    sys.exit(3)
(here / "state.json").write_text(json.dumps(st))
'''


@pytest.fixture
def lms(tmp_path):
    """A fake lms: `make(**state)` returns (config, calls-reader)."""
    import os
    import sys

    def make(running=True, port=1235, loaded=(), disk=(), **cfg):
        (tmp_path / "lms.py").write_text(FAKE_LMS)
        (tmp_path / "calls.txt").unlink(missing_ok=True)
        (tmp_path / "state.json").write_text(json.dumps(
            {"running": running, "port": port, "loaded": list(loaded), "disk": list(disk)}))
        if os.name == "nt":
            exe = tmp_path / "lms.cmd"
            exe.write_text(f'@"{sys.executable}" "{tmp_path / "lms.py"}" %*\n')
        else:
            exe = tmp_path / "lms"
            exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "lms.py"}" "$@"\n')
            exe.chmod(0o755)
        config = {"localllm": {"enabled": True, "runner": "lmstudio", "lms": str(exe), **cfg}}

        def calls():
            p = tmp_path / "calls.txt"
            return p.read_text().splitlines() if p.exists() else []
        return config, calls
    return make


QWEN = "qwen3.5-2b-uncensored-hauhaucs-aggressive"
DISK = [{"modelKey": QWEN, "deviceIdentifier": None}]


def test_lmstudio_starts_the_server_and_loads_the_model(lms):
    cfg, calls = lms(running=False, port=1235, disk=DISK)
    assert localllm.ensure_lmstudio(QWEN, config=cfg) == "http://127.0.0.1:1235"
    got = calls()
    assert "server start" in got and f"load {QWEN} --ttl 600 -y" in got
    assert not any(c.startswith("get") for c in got)
    cfg, calls = lms(loaded=[{"identifier": QWEN, "deviceIdentifier": None}], disk=DISK)
    assert localllm.ensure_lmstudio(QWEN, ttl=60, config=cfg) == "http://127.0.0.1:1235"
    assert not any(c.startswith(("load", "server start")) for c in calls())


def test_lmstudio_never_downloads_or_uses_a_remote_model(lms):
    cfg, calls = lms(disk=DISK)
    assert localllm.ensure_lmstudio("not-on-disk-7b", config=cfg) is None
    assert localllm.ensure_lmstudio("--evil", config=cfg) is None
    assert not any(c.startswith(("load", "get")) for c in calls())
    cfg, _ = lms(disk=[{"modelKey": QWEN, "deviceIdentifier": "laptop-2"}])
    with pytest.raises(ValueError, match="cloud model is refused"):
        localllm.ensure_lmstudio(QWEN, config=cfg)
    cfg, _ = lms(loaded=[{"identifier": QWEN, "deviceIdentifier": "laptop-2"}], disk=DISK)
    with pytest.raises(ValueError, match="cloud model is refused"):
        localllm.ensure_lmstudio(QWEN, config=cfg)


def test_lmstudio_needs_enabled_and_lmstudio_or_its_port(lms):
    cfg, calls = lms(disk=DISK)
    cfg["localllm"]["enabled"] = False
    assert localllm.ensure_lmstudio(QWEN, config=cfg) is None and calls() == []
    cfg, _ = lms(disk=DISK, runner="ollama", url="http://127.0.0.1:1234")
    assert localllm.ensure_lmstudio(QWEN, config=cfg) is None
    cfg, _ = lms(disk=DISK, runner="", url="http://localhost:1235")
    assert localllm.ensure_lmstudio(QWEN, config=cfg) == "http://127.0.0.1:1235"
    cfg, _ = lms(disk=DISK, lms="C:/nowhere/lms.exe")
    assert localllm.ensure_lmstudio(QWEN, config=cfg) is None


def test_find_uses_lmstudio_from_lms(serve, lms, monkeypatch):
    a = serve({("GET", "/v1/models"): oa_models("phonon-40m", QWEN)})
    monkeypatch.setattr(localllm, "_PROBES", [])
    port = int(a.base.rsplit(":", 1)[1])
    cfg, _ = lms(port=port, loaded=[{"identifier": QWEN, "deviceIdentifier": None}],
                 model=QWEN)
    r = localllm.find(cfg)
    assert (r.kind, r.model, r.base) == ("openai", QWEN, a.base)
    cfg["localllm"]["enabled"] = False  # off: lms is not even asked
    assert localllm.find(cfg) is None


def test_openai_rows_marked_remote_are_skipped(serve, monkeypatch):
    a = serve({("GET", "/v1/models"): {"data": [
        {"id": "far-3b", "deviceIdentifier": "laptop-2"}, {"id": "near-3b"}]}})
    monkeypatch.setattr(localllm, "_PROBES", [])
    assert localllm.find({"localllm": {"url": a.base}}).model == "near-3b"
    with pytest.raises(ValueError, match="cloud model is refused"):
        localllm.find({"localllm": {"url": a.base, "model": "far-3b"}})


def test_cli_llm_test_sends_only_the_synthetic_prompt(serve, monkeypatch):
    from typer.testing import CliRunner

    from keepfold.cli import app

    f = serve({("POST", "/v1/chat/completions"): openai_reply(
        '{"label":"programming","confidence":0.9}')})
    monkeypatch.setattr(localllm, "find",
                        lambda config=None, background=False: Runner("openai", f.base, "qwen-x"))
    out = CliRunner().invoke(app, ["llm", "test"])
    assert out.exit_code == 0, out.output
    assert "answer: programming (confidence 0.90)" in out.output and "time: " in out.output
    assert "Dijkstra shortest path tutorial" in f.calls[0][2]["messages"][1]["content"]
    monkeypatch.setattr(localllm, "find", lambda config=None, **_: None)
    assert CliRunner().invoke(app, ["llm", "test"]).exit_code == 1


def test_cli_llm_status_never_starts_or_loads_lmstudio(serve, lms, monkeypatch):
    from typer.testing import CliRunner

    from keepfold.cli import app

    a = serve({("GET", "/v1/models"): oa_models(QWEN)})
    port = int(a.base.rsplit(":", 1)[1])
    monkeypatch.setattr(localllm, "_PROBES", [])
    cfg, calls = lms(running=False, port=port, disk=DISK, model=QWEN)
    monkeypatch.setattr("keepfold.taint.config", lambda: cfg)
    out = CliRunner().invoke(app, ["llm", "status"])
    assert out.exit_code == 0, out.output
    assert "server off" in out.output and "keepfold llm start" in out.output
    assert not any(c.startswith(("server start", "load")) for c in calls())
    cfg, calls = lms(running=True, port=port, disk=DISK, model=QWEN)  # on, model not loaded
    monkeypatch.setattr("keepfold.taint.config", lambda: cfg)
    out = CliRunner().invoke(app, ["llm", "status"])
    assert out.exit_code == 0, out.output
    assert f"found: openai-compatible {QWEN}" in out.output
    assert "loaded in LM Studio: no" in out.output and "keepfold llm start" in out.output
    assert not any(c.startswith(("server start", "load")) for c in calls())
    assert not any(m == "POST" for m, _, _ in a.calls)  # no ping: LM Studio would load it


def test_doctor_never_starts_or_loads_lmstudio(serve, lms, monkeypatch):
    """G3: doctor uses the read-only path of `llm status`: a stopped LM Studio is a WARN that
    names `keepfold llm start`, and no `server start` or `load` runs."""
    from keepfold import doctor

    a = serve({("GET", "/v1/models"): oa_models(QWEN)})
    port = int(a.base.rsplit(":", 1)[1])
    monkeypatch.setattr(localllm, "_PROBES", [])
    cfg, calls = lms(running=False, port=port, disk=DISK, model=QWEN)
    monkeypatch.setattr("keepfold.taint.config", lambda: cfg)
    status, _, detail, fix = doctor.check_local_model()
    assert status == "WARN" and "not running" in detail and "keepfold llm start" in fix
    assert not any(c.startswith(("server start", "load")) for c in calls())
    cfg, calls = lms(running=True, port=port, disk=DISK, model=QWEN)
    monkeypatch.setattr("keepfold.taint.config", lambda: cfg)
    assert doctor.check_local_model()[0] == "PASS"
    assert not any(c.startswith(("server start", "load")) for c in calls())


def test_status_is_the_one_read_only_lookup(serve, lms, monkeypatch):
    """`keepfold llm status` and doctor share `localllm.status`: it finds a model already served
    by LM Studio, reports the server, never starts it or loads, and returns a bad config as
    an error instead of raising."""
    a = serve({("GET", "/v1/models"): oa_models(QWEN)})
    port = int(a.base.rsplit(":", 1)[1])
    monkeypatch.setattr(localllm, "_PROBES", [])
    cfg, calls = lms(running=True, port=port, disk=DISK, model=QWEN)
    got = localllm.status({**cfg, "localllm": {**cfg["localllm"], "enabled": True}})
    assert got["lmstudio"]["port"] == port and got["error"] is None
    assert got["runner"].model == QWEN
    assert not any(c.startswith(("server start", "load")) for c in calls())
    cfg, calls = lms(running=False, port=port, disk=DISK, model=QWEN)
    got = localllm.status(cfg)
    assert got["lmstudio"]["port"] is None and got["runner"] is None
    assert not any(c.startswith(("server start", "load")) for c in calls())
    bad = localllm.status({"localllm": {"url": "http://example.com:1234"}})
    assert bad["runner"] is None and bad["error"]


def test_status_asks_lms_only_for_lmstudio(serve, lms, monkeypatch):
    """R5 F2: an Ollama or auto user is never made to wait on `lms`; LM Studio is asked when it
    is the runner, or when the runner found is an OpenAI-compatible server (maybe LM Studio)."""
    o = serve({("GET", "/api/tags"): tags("qwen3.5:4b")})
    monkeypatch.setattr(localllm, "_PROBES", [("ollama", o.base, "/api/tags")])
    for runner in ("ollama", None):
        cfg, calls = lms(runner=runner)
        got = localllm.status(cfg)
        assert got["runner"].kind == "ollama" and got["lmstudio"] is None and calls() == []
    a = serve({("GET", "/v1/models"): oa_models(QWEN)})
    monkeypatch.setattr(localllm, "_PROBES", [])
    cfg, calls = lms(runner=None, port=int(a.base.rsplit(":", 1)[1]), url=a.base)
    got = localllm.status(cfg)
    assert got["runner"].kind == "openai" and got["lmstudio"]["port"] and calls()
    cfg, calls = lms(runner="ollama")
    assert localllm.status(cfg, lms=True)["lmstudio"] is not None and calls()


def test_lms_is_found_without_exe_and_on_path(tmp_path, monkeypatch):
    """Deps A10: macOS and Linux have `~/.lmstudio/bin/lms` (no .exe); else `lms` on PATH;
    a configured `localllm.lms` that is gone is not replaced by a guess."""
    import shutil

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda c, *a, **k: None)
    assert localllm._lms_exe({}) is None
    on_path = tmp_path / "elsewhere" / "lms"
    monkeypatch.setattr(shutil, "which", lambda c, *a, **k: str(on_path) if c == "lms" else None)
    assert localllm._lms_exe({}) == on_path
    plain = tmp_path / ".lmstudio" / "bin" / "lms"
    plain.parent.mkdir(parents=True)
    plain.write_bytes(b"")
    assert localllm._lms_exe({}) == plain
    (plain.parent / "lms.exe").write_bytes(b"")
    assert localllm._lms_exe({}) == plain.parent / "lms.exe"
    assert localllm._lms_exe({"lms": str(tmp_path / "gone" / "lms.exe")}) is None
