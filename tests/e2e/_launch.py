"""`keepfold` for the E2E tests, run as `sys.executable -I _launch.py <args>`: FakeEmbed as the
embedder (no model download), no browser opened, browser history read from %LOCALAPPDATA% on
every OS, and Ollama's fixed port 11434 redirected to the one the test picked
(KEEPFOLD_E2E_OLLAMA, else refused). No src/ change is needed.

KEEPFOLD_E2E_CRASH=<module:qualname>[#n] arms a crash point (D21 §2.2): the n-th call (default 1)
of that function prints `CRASHPOINT <spec>` to stderr and ends the process with os._exit(86)
before the function runs. A spec naming no function exits 1 at start, so a moved point fails
loudly instead of never firing. Only lookups through the attribute see the wrap: a caller that
did `from module import name` before this ran keeps the real function."""

import importlib
import inspect
import os
import sys
import webbrowser
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/, for conftest.FakeEmbed

from conftest import FakeEmbed  # noqa: E402

from keepfold import localllm, semantic  # noqa: E402
from keepfold.app import server  # noqa: E402
from keepfold.browse import sources  # noqa: E402
from keepfold.cli import app  # noqa: E402

OLLAMA = "http://127.0.0.1:11434"

webbrowser.open = lambda *a, **k: True
# history under the fake %LOCALAPPDATA% (conftest.windows_browsers), on every OS
sources.discover = partial(sources.discover, platform="win32")
server._open = lambda *a, **k: True  # a distinct object: _open_path would os.startfile otherwise
semantic._embedder = FakeEmbed()
_real_request = localllm._request


def _request(url, body, timeout):
    if url.startswith(OLLAMA):
        if not (fake := os.environ.get("KEEPFOLD_E2E_OLLAMA")):
            raise OSError("no Ollama in the E2E tests")
        url = fake + url[len(OLLAMA):]
    return _real_request(url, body, timeout)


localllm._request = _request


def _arm(spec: str) -> None:
    target, _, n = spec.partition("#")
    module, _, qualname = target.partition(":")
    try:
        nth = int(n or 1)
        owner = importlib.import_module(module)
        *path, name = qualname.split(".")
        for part in path:
            owner = getattr(owner, part)
        raw = inspect.getattr_static(owner, name)  # a staticmethod stays one, not its function
    except (ValueError, ImportError, AttributeError) as e:
        sys.exit(f"KEEPFOLD_E2E_CRASH={spec}: no such function ({e!r})")
    wrapped = isinstance(raw, staticmethod | classmethod)
    real = raw.__func__ if wrapped else raw
    if nth < 1 or not callable(real):
        sys.exit(f"KEEPFOLD_E2E_CRASH={spec}: not a function, or #n below 1")
    calls = 0

    def crash(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == nth:
            sys.stderr.write(f"CRASHPOINT {spec}\n")
            sys.stderr.flush()
            os._exit(86)
        return real(*args, **kwargs)

    setattr(owner, name, type(raw)(crash) if wrapped else crash)


if spec := os.environ.get("KEEPFOLD_E2E_CRASH"):
    _arm(spec)

if __name__ == "__main__":
    app()
