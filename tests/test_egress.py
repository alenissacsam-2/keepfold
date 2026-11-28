"""E1: every way data leaves Keepfold is in `keepfold.egress.REGISTRY`. The scan walks src/keepfold
for network imports (urllib.request, http.client, socket, ssl, requests, httpx, aiohttp,
websocket(s), ftplib, smtplib), network calls (`urlopen`, `HTTPConnection`, `HTTPSConnection`,
`create_connection`, `download_model`, fastembed's `TextEmbedding`/`TextCrossEncoder` without
`local_files_only=True`) and commands that download (`winget`, `curl`, `wget`,
`brew`, `Invoke-WebRequest`, a `push`/`fetch`/`clone`/`pull`, `pip install`, `claude plugin
marketplace add`), and fails for a module that
is neither registered nor ALLOWED. `urllib.parse` alone is not egress."""

import ast
import os
import sys
from pathlib import Path

from keepfold import egress

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "keepfold"
NET_MODULES = {"urllib.request", "http.client", "socket", "ssl", "requests", "httpx", "aiohttp",
               "websocket", "websockets", "ftplib", "smtplib"}
NET_CALLS = {"urlopen", "HTTPConnection", "HTTPSConnection", "create_connection",
             "download_model"}
FASTEMBED = {"TextEmbedding", "TextCrossEncoder"}
NET_COMMANDS = {"winget", "brew", "curl", "wget", "Invoke-WebRequest"}
# git's, `ollama pull`, and `claude plugin marketplace add` (Claude Code fetches the plugin)
NET_VERBS = {"push", "fetch", "clone", "pull", "marketplace"}
# module: why it is flagged though nothing leaves the PC (so it is not in REGISTRY)
ALLOWED = {
    "vault.py": "socket.gethostname() names this PC in the vault lock; no connection",
    "migrate.py": "asks 127.0.0.1 only whether an old Cortex app still runs, and names this "
                  "PC in the old lock (socket.gethostname()); nothing leaves the PC",
    "app/brain.py": "the brain port (D12 B1): listens on 127.0.0.1 only, for this user's "
                    "hooks after a mutual HMAC challenge; it connects nowhere",
}


def _net_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in NET_MODULES)


def _strs(node) -> list[str]:
    return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]


def _egress(path: Path):
    """(kind, line) of each way `path` can reach the network."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for a in node.names:
                if _net_module(a.name):
                    yield f"import {a.name}", node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                full = f"{node.module}.{a.name}"
                if _net_module(node.module) or _net_module(full):
                    yield f"from {node.module} import {a.name}", node.lineno
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in NET_CALLS:
                yield name, node.lineno
            elif "git" in name.lower() and (hit := NET_VERBS.intersection(_strs(
                    ast.Tuple(elts=node.args)))):  # _git(root, "push")
                yield f"git {sorted(hit)[0]}", node.lineno
            elif name in FASTEMBED and not any(
                    k.arg == "local_files_only" and isinstance(k.value, ast.Constant)
                    and k.value.value is True for k in node.keywords):
                yield f"{name} may download", node.lineno
        elif isinstance(node, (ast.List, ast.Tuple)):
            words = _strs(node)
            if hit := NET_COMMANDS.intersection(words) or NET_VERBS.intersection(words) or (
                    {"pip", "install"} <= set(words) and {"pip install"}):
                yield f"command {sorted(hit)[0]}", node.lineno


def _flagged(src: Path) -> dict[str, list]:
    out = {}
    for p in sorted(src.rglob("*.py")):
        if hits := list(_egress(p)):
            out[p.relative_to(src).as_posix()] = hits
    return out


def test_every_module_that_reaches_the_network_is_registered():
    known = {e.module for e in egress.REGISTRY} | set(ALLOWED)
    bad = {m: h for m, h in _flagged(SRC).items() if m not in known}
    assert not bad, ("this code can send data off the PC; add an entry for it to "
                     f"keepfold.egress.REGISTRY (where, what, when): {bad}")


def test_every_registry_entry_is_still_reached():
    """A stale entry would let a later connection in the same module through unseen."""
    flagged = _flagged(SRC)
    idle = [e.module for e in egress.REGISTRY if e.module not in flagged]
    assert not idle, f"drop these from REGISTRY, they no longer reach the network: {idle}"
    assert not [m for m in ALLOWED if m not in flagged]


def test_the_scan_flags_an_unregistered_urlopen(tmp_path):
    """T-R11: a new module that sends data is caught (a temp folder; src/ is not touched)."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "leak.py").write_text(
        "from urllib.request import urlopen\n"
        "urlopen('https://example.invalid/?q=notes')\n", encoding="utf-8")
    (tmp_path / "pkg" / "fine.py").write_text(
        "from urllib.parse import quote\nquote('x')\n", encoding="utf-8")
    assert _flagged(tmp_path) == {"pkg/leak.py": [("from urllib.request import urlopen", 1),
                                                   ("urlopen", 2)]}


def test_the_scan_finds_each_kind(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("import socket, json\n"
                 "import urllib.parse\n"
                 "from http import client\n"
                 "TextEmbedding(M, local_files_only=True)\n"
                 "TextEmbedding(M, local_files_only=not d)\n"
                 "subprocess.run(['winget', 'install', 'x'])\n"
                 "subprocess.run(['git', '-C', r, 'push'])\n"
                 "subprocess.run(['git', '-C', r, 'log'])\n"
                 "_git(root, 'fetch', 'origin')\n"
                 "[uv, 'pip', 'install', 'x']\n"
                 "c = http.client.HTTPSConnection(h)\n", encoding="utf-8")
    assert sorted(_egress(p), key=lambda x: x[1]) == [
        ("import socket", 1), ("from http import client", 3), ("TextEmbedding may download", 5),
        ("command winget", 6), ("command push", 7), ("git fetch", 9),
        ("command pip install", 10), ("HTTPSConnection", 11)]


def test_the_model_download_turns_off_hugging_face_telemetry(monkeypatch):
    """Without it huggingface_hub asks huggingface.co for its list of AI agents and puts the
    agent it detects and the torch version in its User-Agent; an explicit setting is kept."""
    from keepfold import _proc, cli
    monkeypatch.setattr(_proc.urllib.request, "getproxies", lambda: {})
    monkeypatch.setitem(sys.modules, "truststore", None)  # never injected into this process
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "x")  # restored after the test
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY")
    cli._download_setup()
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "0")
    cli._download_setup()
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "0"


def test_doctor_prints_where_keepfold_connects():
    from typer.testing import CliRunner

    from keepfold.cli import app
    r = CliRunner().invoke(app, ["doctor", "--privacy"])
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == ["Where Keepfold connects:",
                                     *(f"  {s}" for s in egress.summary())]


def test_summary_is_one_line_per_outside_destination():
    lines = egress.summary()
    outside = [e for e in egress.REGISTRY if not e.loopback]
    assert len(lines) == len({(e.dest, e.what) for e in outside}) < len(outside)
    assert all(isinstance(s, str) and s for s in lines) and len(set(lines)) == len(lines)
    plugin = [s for s in lines if "Keepfold plugin" in s]  # cli.py and app/targets.py: one line
    assert len(plugin) == 1 and "you say yes, or when you connect Claude Code" in plugin[0]
    assert all(not e.dest.startswith("localhost") for e in outside)
    assert any(s.startswith("github.com") for s in lines)
