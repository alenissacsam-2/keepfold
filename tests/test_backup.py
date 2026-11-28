"""`keepfold backup` (verdict item 30, D7 §6): a round trip restores the vault; rebuilt and temp
files stay out; zips go outside the vault; the newest of each reason are kept."""

import os
import shutil
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import note
from typer.testing import CliRunner

from keepfold import backup, chat_import, semantic
from keepfold.browse import store, vectors
from keepfold.cli import app
from keepfold.events import EventLog
from keepfold.vault import Vault

FIXTURE = Path(__file__).parent / "fixtures" / "vault_mini"


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and (p.suffix == ".md" or "events" in p.parts)}


def test_round_trip_skips_rebuilt_and_temp_files(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    EventLog(Vault(root)).append(session="cli", kind="grade", source="external",
                                 now=datetime(2026, 11, 13, 12, tzinfo=UTC),
                                 fields={"object": "concept:two-pointers", "verdict": "correct"})
    cache = root / ".keepfold"
    cache.mkdir(exist_ok=True)
    (cache / "dashboard.html").write_text("<html>" + "x" * 1000, encoding="utf-8")
    (cache / "vectors.npy").write_bytes(b"keep me")
    (root / "notes").mkdir()
    (root / "notes" / ".x.md.123.tmp").write_text("stray", encoding="utf-8")
    zp = backup.backup(root)
    out = tmp_path / "restored"
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        z.extractall(out)
    assert _files(out) == _files(root) and any(n.startswith("events/") for n in names)
    assert ".keepfold/vectors.npy" in names  # re-embedding takes minutes
    assert ".keepfold/dashboard.html" not in names
    assert not [n for n in names if n.endswith(".tmp")]


def test_keeps_the_last_ten_zips(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    dest = tmp_path / "bk"
    dest.mkdir()
    old = [dest / f"keepfold-20000101-0000{n:02}.zip" for n in range(12)]
    for p in old:
        p.write_bytes(b"old")
    (dest / "notes.txt").write_text("not a backup", encoding="utf-8")
    new = backup.backup(root, dest)
    left = sorted(p.name for p in dest.glob("keepfold-*.zip"))
    assert len(left) == 10 and new.name == left[-1]
    assert left[:9] == [p.name for p in old[3:]]
    assert (dest / "notes.txt").exists()


def test_keeps_durable_browse_stores_and_leaves_out_secrets(tmp_path):
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    b = root / ".keepfold" / "browse"
    (b / "inbox-archive").mkdir(parents=True)
    (b / "chat").mkdir()
    keep = ["site-days.json", "sites.json", "verdicts.json", "state.json",
            "vectors-private.npz", "store.db", "vectors-private.db",
            "inbox-archive/inbox-2026-11-01.jsonl",
            "inbox-2026-11-19.jsonl"]  # filtered on arrival, like the archive
    drop = ["token", "lock", "chat/01JABCDEFGHJKMNPQRSTVWXYZ0.jsonl"]
    for name in keep + drop:
        if not name.endswith(".db"):
            (b / name).write_text("x", encoding="utf-8")
    assert set(backup.DBS) == {store.PATH, vectors.CACHE, semantic.CACHE, chat_import.STATE}
    live = []
    for rel in backup.DBS:  # open, with rows in the WAL: the copy still holds them
        con = sqlite3.connect(root / rel)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES ('in the wal')")
        con.commit()
        live.append(con)
    try:
        with zipfile.ZipFile(backup.backup(root)) as z:
            names = set(z.namelist())
            z.extractall(tmp_path / "out")
    finally:
        for con in live:
            con.close()
    assert {f".keepfold/browse/{n}" for n in keep} | {".keepfold/vectors.db",
                                                      chat_import.STATE} <= names
    assert not [n for n in names if n.startswith(".keepfold/browse/")
                and n.removeprefix(".keepfold/browse/") not in keep]
    assert not [n for n in names if n.endswith(("-wal", "-shm"))]
    for rel in backup.DBS:
        with closing(sqlite3.connect(tmp_path / "out" / rel)) as con:
            assert con.execute("SELECT x FROM t").fetchall() == [("in the wal",)]


def test_no_private_leaves_out_private_pages_and_stores(tmp_path, home):
    """`keepfold backup --no-private`: private pages, a mistake about a private concept (privacy
    by inheritance), a private resource's raw source, browse/private, the browse store and
    .git stay out; public pages, the events and the public vectors stay in."""
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    note(root, "notes/diary.md", 'title: "Diary"\nkind: note\nprivate: true\n', "# Diary\n")
    note(root, "notes/pasta.md", 'title: "Pasta"\nkind: note\n', "# Pasta\n")
    note(root, "mistakes/dsa.diary-slip.md", 'id: "dsa.diary-slip"\ntitle: "Slip"\n'
         'concepts: ["[[secret-diary]]"]\n', "# Slip\n")
    note(root, "resources/letter.md", 'title: "Letter"\nkind: resource\nprivate: true\n'
         'source: "raw/letter.txt"\n', "# Letter\n")
    (root / "raw").mkdir()
    (root / "raw" / "letter.txt").write_text("private letter", encoding="utf-8")
    (root / "raw" / "public.txt").write_text("public text", encoding="utf-8")
    note(root, "browse/private/browse-private-2026-11-01.md", "kind: browse\nprivate: true\n",
         "# Browsing (private)\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    b = root / ".keepfold" / "browse"
    b.mkdir(parents=True)
    (b / "sites.json").write_text("{}", encoding="utf-8")
    for rel in (store.PATH, semantic.CACHE, chat_import.STATE):  # Audit 21 Nov I3: chat-import
        # holds raw chat turns, private ones too
        with closing(sqlite3.connect(root / rel)) as con:
            con.execute("CREATE TABLE t(x)")
            con.commit()
    EventLog(Vault(root)).append(session="cli", kind="grade", source="external",
                                 now=datetime(2026, 11, 13, 12, tzinfo=UTC),
                                 fields={"object": "concept:two-pointers", "verdict": "correct"})
    with zipfile.ZipFile(backup.backup(root, tmp_path / "full")) as z:  # the default: all
        assert {"notes/diary.md", ".git/HEAD", ".keepfold/browse/sites.json",
                "browse/private/browse-private-2026-11-01.md"} <= set(z.namelist())
    r = CliRunner().invoke(app, ["backup", "--no-private", "--to", str(tmp_path / "np"),
                                 "--vault", str(root)])
    assert r.exit_code == 0, r.output
    (zp,) = (tmp_path / "np").glob("keepfold-*.zip")
    with zipfile.ZipFile(zp) as z:
        names = set(z.namelist())
    assert {"notes/pasta.md", "concepts/two-pointers.md", "mistakes/dsa.off-by-one.md",
            "raw/public.txt", ".keepfold/vectors.db"} <= names, names
    assert any(n.startswith("events/") for n in names)
    for gone in ("notes/diary.md", "concepts/secret-diary.md", "mistakes/dsa.diary-slip.md",
                 "resources/letter.md", "raw/letter.txt", ".keepfold/chat-import.sqlite",
                 ".keepfold/chat-import.sqlite-wal", ".keepfold/chat-import.sqlite-shm"):
        assert gone not in names, gone
    assert not [n for n in names if n.startswith((".git/", ".keepfold/browse/", "browse/private/"))]


# ---- D7 §6: outside the vault, kept per reason (A8) -------------------------------------------


def _vault(tmp_path, home) -> Path:
    """A vault_mini copy; asserts first that backups land in the temp HOME, never the real one."""
    assert Path.home() == home and home.is_relative_to(tmp_path)
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE, root)
    (root / ".keepfold").mkdir(exist_ok=True)
    return root


def test_backup_goes_outside_the_vault(tmp_path, home, monkeypatch):
    """The default folder is ~/.keepfold/backups/<vault_id>; an unstamped vault is stamped first;
    backup.folder moves it, never into the vault (nor --to); too little free space is refused."""
    from keepfold import taint, vault_format
    from keepfold.vault import VaultError

    root = _vault(tmp_path, home)
    zp = backup.backup(root)
    vid = vault_format.state(root).stamp["vault_id"]
    assert zp.parent == (home / ".keepfold" / "backups" / vid).resolve()
    assert backup.ZIP_RE.fullmatch(zp.name) and zp.name.endswith("-manual.zip")
    assert backup.last(root) == zp and not (root / ".keepfold" / "backups").exists()
    with zipfile.ZipFile(zp) as z:
        assert ".keepfold/format.json" in z.namelist()

    ext = tmp_path / "drive"
    taint.write_config(lambda cfg: {"backup": {"folder": str(ext)}})
    assert backup.backup(root).parent == (ext / vid).resolve()
    for inside in (root, root / "notes" / "bk"):
        taint.write_config(lambda cfg, inside=inside: {"backup": {"folder": str(inside)}})
        with pytest.raises(VaultError, match="inside the vault"):
            backup.backup(root)
    taint.write_config(lambda cfg: {"backup": {"folder": str(ext)}})
    with pytest.raises(VaultError, match="inside the vault"):
        backup.backup(root, root / "zips")  # --to too

    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10, 9, 1))
    with pytest.raises(VaultError, match="Not enough free space"):
        backup.backup(root)
    assert len(backup.zips(ext / vid)) == 1  # the refusal left nothing


def test_keep_per_reason(tmp_path, home):
    """The newest 10 manual zips (a 0.x zip with no reason counts as manual) and the newest 3
    update zips are kept; pruning one reason never touches the other."""
    root = _vault(tmp_path, home)
    dest = tmp_path / "bk"
    dest.mkdir()
    manual = [dest / f"keepfold-20000101-0000{n:02}-manual.zip" for n in range(10)]
    update = [dest / f"keepfold-20000102-0000{n:02}-update.zip" for n in range(4)]
    legacy = dest / "keepfold-19991231-000000.zip"
    for p in [*manual, *update, legacy]:
        p.write_bytes(b"old")
    new = backup.backup(root, dest, reason="update")
    assert backup.zips(dest, "update") == [*update[2:], new]
    assert backup.zips(dest, "manual") == [legacy, *manual]  # untouched by an update backup
    new = backup.backup(root, dest)
    assert backup.zips(dest, "manual") == [*manual[1:], new] and not legacy.exists()
    assert len(backup.zips(dest)) == 13


def test_chat_import_store_is_snapshotted(tmp_path, home):
    """.keepfold/chat-import.sqlite goes through SQLite's backup, rows in its WAL included, and
    never as the live file or its -wal/-shm; --no-private leaves it out."""
    root = _vault(tmp_path, home)
    con = sqlite3.connect(root / chat_import.STATE)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES ('in the wal')")
        con.commit()
        assert (root / f"{chat_import.STATE}-wal").is_file()
        full = backup.backup(root, tmp_path / "full")
        bare = backup.backup(root, tmp_path / "bare", private=False)
    finally:
        con.close()
    with zipfile.ZipFile(full) as z:
        names = set(z.namelist())
        z.extractall(tmp_path / "out")
    assert chat_import.STATE in names and not [n for n in names if n.endswith(("-wal", "-shm"))]
    with closing(sqlite3.connect(tmp_path / "out" / chat_import.STATE)) as c:
        assert c.execute("SELECT x FROM t").fetchall() == [("in the wal",)]
    with zipfile.ZipFile(bare) as z:
        assert not [n for n in z.namelist() if n.startswith(chat_import.STATE)]


def test_update_backs_up_first_unless_a_day_old(tmp_path, home):
    """Update now makes an `update` zip first, unless the last backup is under a day old."""
    from keepfold.app.routes.system import SystemRoutes

    root = _vault(tmp_path, home)
    said = []
    me, job = SimpleNamespace(root=root), SimpleNamespace(say=said.append)
    SystemRoutes._backup_first(me, job)
    (first,) = backup.zips(backup.folder(root), "update")
    SystemRoutes._backup_first(me, job)  # under a day old: no second zip
    assert backup.zips(backup.folder(root)) == [first] and said == ["backing up the vault first"]
    os.utime(first, (first.stat().st_atime, first.stat().st_mtime - 2 * 86400))
    first.rename(first.with_name("keepfold-20000101-000000-update.zip"))  # a new name this second
    SystemRoutes._backup_first(me, job)
    assert len(backup.zips(backup.folder(root), "update")) == 2
