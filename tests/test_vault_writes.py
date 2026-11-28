"""RR-17a: vault files are written only through `keepfold.vault` (lock, temp file, os.replace
with retry). The scan finds direct writes in src/keepfold — `open(..., 'w'/'a'/'x'/'+')`,
`Path.open` with such a mode, `Path.write_text`/`write_bytes`, `os.replace`, `shutil.move` /
`copy*` — and fails for any module not in ALLOWED. `vault.write_text(path, text)` (two
positional arguments) is the Vault method and passes. A new module that writes outside the vault
gets one ALLOWED line saying where it writes and why that is not a vault file."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "keepfold"
MODES = set("wax+")
SHUTIL = {"move", "copy", "copy2", "copyfile", "copytree"}
# module (relative to src/keepfold): why it writes without keepfold.vault
ALLOWED = {
    "vault.py": "it is the vault writer itself",
    "events.py": "the append-only event log: one O_APPEND line per event under the vault lock",
    "app/actions.py": "the Start-at-login file in the user's Startup folder, outside the vault",
    "app/desktop.py": "~/.keepfold/desktop.json, the desktop app's window state",
    "app/wizard.py": "the Ollama download and its unpacked app under ~/.keepfold (staging + "
                     "os.replace), not the vault",
    "app/launch.py": "~/.keepfold/logs, the windowless app's stdout log",
    "app/server.py": "the owner's chosen snapshot file and ~/.keepfold app state, outside "
                     "the vault",
    "app/targets.py": "agent rule files in other tools' folders (backed up, temp + os.replace)",
    "backup.py": "the backup zip at a path the owner names",
    "loop_eval.py": "made-up chat transcripts and a config file in `keepfold eval loop`'s own "
                    "temp home, and a vault_mini copy there; never the owner's vault or home",
    "migrate.py": "the one-time move from Cortex: os.replace of the ~/.cortex and vault "
                  ".cortex/ folders, its own marker files and renamed backup zips; note "
                  "pages go through Vault._mutate",
    "browse/sources.py": "temp copies of browser history databases, deleted after reading",
    "browse/store.py": "browse page zips: temp + os.replace under the browse job lock",
    "chats.py": "imported chat logs in the chats export folder (~/.keepfold), not vault pages",
    "cli.py": "--out files the owner names and the model-ready mark in ~/.keepfold/models",
    "redteam.py": "P2's made-up chat export in the red team's own temp folder, not a vault file",
    "taint.py": "~/.keepfold/config.yaml under its own lock (temp + os.replace)",
    "update.py": "the downloaded installer or update file and its checksum in the update "
                 "folder, the Mac or Linux app swapped in by Update now, and its per-user "
                 "copies removed by Uninstall Keepfold…",
    "zotero.py": "copies Zotero's database into a temp folder to read it; never the vault",
    "anki.py": "copies Anki's collection into a temp folder to read it; never the vault",
}


def _mode(node: ast.Call, at: int) -> str:
    """The mode argument; "?" when it is computed (counted as a write for the builtin open)."""
    arg = node.args[at] if len(node.args) > at else next(
        (k.value for k in node.keywords if k.arg == "mode"), None)
    if arg is None:
        return ""
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else "?"


def _writes(path: Path):
    """(kind, line) of each direct file write in `path`."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        via = getattr(f.value, "id", "") if isinstance(f, ast.Attribute) else ""
        if name in ("write_text", "write_bytes") and len(node.args) < 2:  # 2 args: Vault's
            yield name, node.lineno
        elif (via, name) == ("os", "replace") or (via == "shutil" and name in SHUTIL):
            yield f"{via}.{name}", node.lineno
        elif isinstance(f, ast.Name) and name == "open" and (MODES | {"?"}) & set(_mode(node, 1)):
            yield "open", node.lineno
        elif (isinstance(f, ast.Attribute) and name == "open" and via not in ("os", "webbrowser")
              and MODES & set(_mode(node, 0))):  # Path.open("a"); zip/url openers take no mode
            yield "Path.open", node.lineno


def test_only_allowed_modules_write_files_directly():
    bad = []
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC).as_posix()
        if rel not in ALLOWED:
            bad += [f"{rel}:{line} {kind}" for kind, line in _writes(p)]
    assert not bad, ("write vault files through keepfold.vault (Vault.write_text/update_text), "
                     "or add the module to ALLOWED with its reason: " + ", ".join(bad))


def test_every_allowed_module_still_writes():
    """A stale ALLOWED line would let a later vault write through unseen."""
    idle = [m for m in ALLOWED if not list(_writes(SRC / m))]
    assert not idle, f"drop these from ALLOWED, they no longer write directly: {idle}"


def test_the_scan_finds_each_write(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("import os, shutil\n"
                 "p.write_text('x')\n"
                 "vault.write_text(p, 'x')\n"
                 "open(p, 'a')\n"
                 "open(p)\n"
                 "open(p, m)\n"
                 "p.open('wb')\n"
                 "z.open(i)\n"
                 "os.replace(a, b)\n"
                 "shutil.copy2(a, b)\n"
                 "p.write_bytes(b'')\n")
    assert [k for k, _ in sorted(_writes(p), key=lambda x: x[1])] == [
        "write_text", "open", "open", "Path.open", "os.replace", "shutil.copy2", "write_bytes"]
