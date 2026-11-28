"""Build a small vault with an old release's own code, zipped into tests/fixtures/vaults/<tag>.zip.

    uv run python scripts/make_fixture_vault.py v0.6.1
    uv run python scripts/make_fixture_vault.py v0.7.0

Steps: `git worktree add` of the tag in a temp folder, `uv sync --frozen` there (the network:
that tag's locked packages and its Python), then, with HOME, USERPROFILE, APPDATA, LOCALAPPDATA,
XDG_CONFIG_HOME and XDG_DATA_HOME all inside that temp folder, that version's CLI (`cortex` up
to v0.6.1, `keepfold` from v0.6.2): `init`, two `concept add`s, `replay`; then its MCP tools
in-process over the SDK's memory connection (no stdio server, no AI-app config): a //pref line
through prime, a note, a learn session with an ask and a grade, end. Synthetic text only.
The vault (never the temp HOME) is zipped; the temp worktree is removed after.

The old CLI runs from the tag's own .venv (`.venv/Scripts/cortex.exe`), which is what
`uv run cortex` runs, without uv itself reading the temp HOME for its cache and Pythons."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "tests" / "fixtures" / "vaults"
HOME_VARS = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME")


# The old version's MCP tools, in-process; {pkg} is cortex or keepfold (also the tool prefix).
DRIVER = '''
import logging
import sys
from pathlib import Path

import anyio
from mcp import Client
from mcp.client import session
from mcp.types import Implementation

from {pkg} import mcp_server, semantic  # noqa: F401  (semantic whole before any thread)
from {pkg}.mcp_server import build_server
from {pkg}.vault import Vault

mcp_server.WARM_EMBEDDER = False  # no model here; its thread races the import of semantic

# a local model app's name: an unknown client name is restricted in newer versions
session.DEFAULT_CLIENT_INFO = Implementation(name="lmstudio-fixture", version="0")
root, concept = Path(sys.argv[1]), sys.argv[2]
logging.basicConfig(level=logging.WARNING)  # a failing tool's traceback


async def main():
    async with Client(build_server(Vault(root), "owner")) as c:
        async def call(name, args=None):
            r = await c.call_tool("{pkg}_" + name, args or {{}})
            out = r.structured_content
            assert not r.is_error and "error" not in (out or {{}}), (name, r.content)
            print(name, "ok")
            return out

        await call("prime", {{"prompt": "//pref: Synthetic fixture: show a short worked "
                                        "example before the rule."}})
        await call("add", {{"kind": "note", "title": "Fixture note on stacks",
                           "fields": {{"summary": "A stack is last in, first out."}}}})
        await call("start", {{"mode": "learn"}})
        ask = await call("ask", {{"concept": concept, "item_type": "recall",
                                 "question": "What order does a stack remove items in?",
                                 "answer_key": "last in, first out"}})
        await call("grade", {{"item_id": ask["item_id"], "learner_answer": "last in first out",
                             "verdict": "correct", "confidence": 3}})
        await call("end", {{"summary": "Synthetic fixture session on stacks."}})


anyio.run(main)
'''


def sh(cmd: list, cwd: Path | None = None, env: dict | None = None) -> str:
    print("$", " ".join(map(str, cmd)), flush=True)
    r = subprocess.run(list(map(str, cmd)), cwd=cwd, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = r.stdout + r.stderr
    print(out.rstrip())
    if r.returncode:
        raise SystemExit(f"failed ({r.returncode}): {cmd[0]}")
    return out


def inside(p: str | Path, tmp: Path) -> bool:
    return Path(p).resolve().is_relative_to(tmp.resolve())


def old_env(tmp: Path) -> dict:
    """The environment for every old-version command: every home-like folder inside `tmp`."""
    h = tmp / "home"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CORTEX_", "KEEPFOLD_", "CLAUDE_", "VIRTUAL_ENV"))}
    env |= {"HOME": str(h), "USERPROFILE": str(h), "APPDATA": str(h / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(h / "AppData" / "Local"), "XDG_CONFIG_HOME": str(h / ".config"),
            "XDG_DATA_HOME": str(h / ".local" / "share"), "PYTHONUTF8": "1",
            "GIT_CONFIG_NOSYSTEM": "1"}
    for k in HOME_VARS:
        Path(env[k]).mkdir(parents=True, exist_ok=True)
        assert inside(env[k], tmp), k
    return env


def build(tag: str, tmp: Path) -> Path:
    major_minor = tuple(int(x) for x in tag.lstrip("v").split(".")[:3])
    pkg = "cortex" if major_minor <= (0, 6, 1) else "keepfold"
    wt = tmp / "src"
    sh(["git", "-C", REPO, "worktree", "add", "--detach", wt, tag])
    try:
        plain = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        sh(["uv", "sync", "--frozen", "--no-dev"], cwd=wt, env=plain)  # real uv cache, no vault
        bindir = wt / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        py, cli = bindir / "python", bindir / pkg
        env = old_env(tmp)
        # refuse unless the old code itself resolves its home inside the temp folder
        got = sh([py, "-I", "-X", "utf8", "-c", "from pathlib import Path; import os; "
                  "print(Path.home()); print(os.path.expanduser('~'))"], env=env).splitlines()
        if not got or not all(inside(g, tmp) for g in got):
            raise SystemExit(f"refused: the old code's home {got} is outside {tmp}")
        vault = tmp / "vault"
        sh([cli, "init", vault], cwd=tmp, env=env)
        parent: list = []
        for title, kind in (("Fixture data structures", "domain"), ("Linear", "area"),
                            ("Fixture stacks", "concept")):  # "added <kind> <id>"
            parent = ["--parent", sh([cli, "concept", "add", title, "--kind", kind, *parent,
                                      "--vault", vault], cwd=tmp, env=env).split()[-1]]
        concept = parent[1]
        driver = tmp / "driver.py"
        driver.write_text(DRIVER.format(pkg=pkg), encoding="utf-8")
        sh([py, "-I", "-X", "utf8", driver, vault, concept], cwd=tmp, env=env)
        sh([cli, "replay", "--vault", vault], cwd=tmp, env=env)
        return pack(vault, tag)
    finally:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])


def pack(vault: Path, tag: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{tag}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(vault.rglob("*")):
            rel = p.relative_to(vault).as_posix()
            if p.is_file() and not rel.startswith(".git/hooks/") and p.name != "lock":
                z.write(p, rel)
    print(f"wrote {dest} ({dest.stat().st_size // 1024} KB)")
    return dest


def main() -> None:
    sys.stdout.reconfigure(errors="replace")  # old CLIs print glyphs a cp1252 console lacks
    ap = argparse.ArgumentParser()
    ap.add_argument("tag", choices=("v0.6.1", "v0.7.0"))
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix=f"keepfold-fixture-{args.tag}-"))
    try:
        build(args.tag, tmp)
    finally:
        subprocess.run(["git", "-C", str(REPO), "worktree", "prune"])
        # git's objects are read-only, which Windows' rmtree will not delete
        shutil.rmtree(tmp, onexc=lambda fn, p, e: (os.chmod(p, 0o700), fn(p)))


if __name__ == "__main__":
    sys.exit(main())
