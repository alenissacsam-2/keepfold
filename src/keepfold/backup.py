"""Vault backup copies (D7 §6): one zip of the whole vault, .git included, and the durable
browse stores in .keepfold/browse (sites.json, state.json, browse.legacy's *.migrated backups,
inbox-archive/, any not-yet-imported inbox-*.jsonl the capture extension left before 0.3.2,
filtered like the archive), which hold browsing the browsers have already forgotten. The SQLite
stores (DBS: the browsing store, the two vector stores, which take minutes to embed again, and
the chat import's state) are copied by SQLite's own backup, a consistent snapshot, never the
live file and its WAL. .git and .gitignore are copied under the vault lock, which commits take
too, so a ref never precedes its objects. Left out: old backups, the old extension's pairing
token (a leftover file), the local chat history (.keepfold/browse/chat), lock files, the rebuilt
dashboard, stray *.tmp files and damaged stores moved aside (`quarantine.found`: no use
restored, and the chat import's holds raw chat turns, private ones too). A zip holds private
browsing: keep it as private as the vault.

Zips go to `folder(root)`: ~/.keepfold/backups/<vault_id>, or `backup.folder` in config.yaml
(never inside the vault, which may be synced and which forget sweeps), named
keepfold-<time>-<reason>.zip; the newest KEEP[reason] zips of each reason are kept.

`private=False` (`keepfold backup --no-private`) also leaves out what Claude and cloud apps may
never see: private pages and pages under a private subject (`ask.private_pages`, the access
gate's LOCAL audience), the raw sources of private resources, browse/private/, the browse
store in .keepfold/browse (its visits mix private browsing in), the chat import's state
(.keepfold/chat-import.sqlite: the raw turns of private chats too), and .git, whose history
holds every private page.

`restore` unpacks a zip into a new or empty folder only, after checking every member, and gives
the restored vault a new vault_id (`restored_from` in its stamp names the old one)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing, nullcontext
from datetime import datetime
from pathlib import Path

from keepfold import quarantine, vault_format
from keepfold.vault import PRIVATE_BROWSE_DIR, Vault, VaultError, sqlite_ro_uri

# the newest zips kept per reason (D7 §6); manual's 10 is the owner's choice (verdict Q11 a)
KEEP = {"manual": 10, "update": 3}
CHAT_STATE = ".keepfold/chat-import.sqlite"  # chat_import.STATE
# store.PATH, vectors.CACHE and semantic.CACHE (not imported: numpy is slow to load)
DBS = (".keepfold/browse/store.db", ".keepfold/browse/vectors-private.db", ".keepfold/vectors.db",
       CHAT_STATE)
ZIP_RE = re.compile(r"keepfold-(\d{8}-\d{6})(?:-([a-z]+))?\.zip")  # no reason: a 0.x manual zip
ID_RE = re.compile(r"[0-9A-Za-z]{10,40}")  # a vault_id fit to name a folder
# restore's limits against zip bombs: one member, all of them, and the compression ratio of a
# member over RATIO_FROM bytes
MAX_MEMBER, MAX_TOTAL, RATIO, RATIO_FROM = 8 << 30, 64 << 30, 100, 1 << 20


def private_files(root: Path) -> set[Path]:
    """The private pages of the vault at `root` (`ask.private_pages`: `vault.is_private`, or
    under a private subject) and the raw files private resources name as their source."""
    from keepfold import ask
    from keepfold.catalog import Catalog
    from keepfold.index import Index

    v = Vault(root)
    pages = ask.private_pages(v, Index(v, Catalog.load(v))).values()
    return {(root / path).resolve() for path, _ in pages} | {
        (root / str(n["fields"]["source"])).resolve() for _, n in pages
        if n["fields"].get("source")}


def _refuse(msg: str) -> VaultError:
    return VaultError("invalid_argument", msg)


def folder(root: Path) -> Path:
    """Where `backup` puts `root`'s zips: <backup.folder or ~/.keepfold/backups>/<vault_id>, or
    that folder alone while the vault has no stamp yet. VaultError when backup.folder is inside
    the vault."""
    from keepfold import taint
    root = Path(root).resolve()
    cfg = taint.config().get("backup")
    f = cfg.get("folder") if isinstance(cfg, dict) else None
    base = (Path(f).expanduser() if isinstance(f, str) and f.strip()
            else Path.home() / ".keepfold" / "backups").resolve()
    if base.is_relative_to(root):
        raise _refuse(f"The backup folder {base} (backup.folder in config.yaml) is inside the "
                      "vault; pick a folder outside it, ideally on another drive.")
    vid = vault_format.state(root).stamp.get("vault_id")
    return base / vid if isinstance(vid, str) and ID_RE.fullmatch(vid) else base


def zips(d: Path, reason: str | None = None) -> list[Path]:
    """The backup zips in `d` (of `reason`, if given), oldest first."""
    out = [(m[1], p) for p in Path(d).glob("keepfold-*.zip")
           if (m := ZIP_RE.fullmatch(p.name)) and reason in (None, m[2] or "manual")]
    return [p for _, p in sorted(out)]


def last(root: Path) -> Path | None:
    """The newest backup zip of `root` in `folder(root)`, any reason."""
    return (zips(folder(root)) or [None])[-1]


def _walk(top: Path, skip_dirs, skip_files):
    for dirpath, dirnames, filenames in os.walk(top):
        d = Path(dirpath)
        dirnames[:] = [n for n in dirnames if d / n not in skip_dirs]
        for name in filenames:
            p = d / name
            if p not in skip_files and p.suffix != ".tmp":
                yield p


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:  # gone since the walk
        return 0


def backup(root: Path, dest_dir: Path | None = None, private: bool = True,
           reason: str = "manual") -> Path:
    """Zip `root` into dest_dir (default `folder(root)`); returns the zip path. An unstamped
    vault is stamped first (its vault_id names the folder). Refused when dest_dir is inside the
    vault or has less free space than twice the vault's size. `private` False leaves out the
    private pages and stores (module docstring)."""
    root = Path(root).resolve()
    keep = KEEP[reason]
    v = Vault(root)
    if not (root / vault_format.STAMP).exists():
        vault_format.write_stamp(v)
    dest_dir = Path(dest_dir).resolve() if dest_dir else folder(root)
    if dest_dir.is_relative_to(root):
        raise _refuse(f"{dest_dir} is inside the vault; back up to a folder outside it.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"keepfold-{datetime.now():%Y%m%d-%H%M%S}-{reason}.zip"
    tmp = dest.with_suffix(".zip.tmp")
    browse = root / ".keepfold" / "browse"
    git, gitignore = root / ".git", root / ".gitignore"
    skip_dirs = {root / ".keepfold" / "backups", browse / "chat",
                 *(() if private else (git, browse, root / PRIVATE_BROWSE_DIR))}
    dbs = [root / d for d in DBS
           if private or not ((root / d).is_relative_to(browse) or d == CHAT_STATE)]
    skip_files = {tmp, dest, root / ".keepfold" / "lock", root / ".keepfold" / "dashboard.html",
                  browse / "lock", browse / "token",
                  *(Path(f"{root / db}{s}") for db in DBS for s in ("", "-wal", "-shm")),
                  *(() if private else private_files(root)), *quarantine.found(root)}
    files = list(_walk(root, skip_dirs | {git}, skip_files | {gitignore}))
    need = 2 * sum(map(_size, [*files, gitignore, *dbs,
                               *(_walk(git, skip_dirs, skip_files) if private else ())]))
    if (free := shutil.disk_usage(dest_dir).free) < need:
        raise _refuse(f"Not enough free space to back up: {dest_dir} has {free >> 20:,} MB free "
                      f"and a backup needs twice the vault's size ({need >> 20:,} MB). Free "
                      "some space, or set backup.folder to a folder on another drive.")
    # a too-new or unreadable vault takes no lock (Vault._take refuses it) yet still backs up
    lock = v.lock(timeout=30) if vault_format.writable(root) else nullcontext()
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, p.relative_to(root).as_posix())
            for db in [db for db in dbs if db.is_file()]:
                snap = tmp.with_suffix(".db.tmp")
                try:
                    with closing(sqlite3.connect(sqlite_ro_uri(db), uri=True)) as src, \
                            closing(sqlite3.connect(snap)) as dst:
                        src.backup(dst)
                    z.write(snap, db.relative_to(root).as_posix())
                finally:
                    snap.unlink(missing_ok=True)
            with lock:
                later = [*_walk(git, skip_dirs, skip_files)] if private else []
                for p in ([gitignore] if gitignore.is_file() else []) + later:
                    z.write(p, p.relative_to(root).as_posix())
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    for old in zips(dest_dir, reason)[:-keep]:
        old.unlink(missing_ok=True)
    return dest


def same_drive(a: Path, b: Path) -> bool:
    return Path(a).stat().st_dev == Path(b).stat().st_dev


# ---- restore ---------------------------------------------------------------------------------


def _check(i: zipfile.ZipInfo) -> None:
    n = i.filename
    if (n.startswith("/") or "\\" in n or ":" in n or "\0" in n or ".." in n.split("/")
            or stat.S_ISLNK(i.external_attr >> 16)):
        raise _refuse(f"This zip holds {n[:200]!r}: an absolute path, a drive letter, a '..' or "
                      "a symlink, which restore never unpacks.")
    if i.file_size > MAX_MEMBER:
        raise _refuse(f"{n[:200]!r} in this zip is {i.file_size >> 20:,} MB, over the "
                      f"{MAX_MEMBER >> 20:,} MB restore unpacks for one file.")
    if i.file_size > RATIO_FROM and i.file_size > RATIO * max(i.compress_size, 1):
        raise _refuse(f"{n[:200]!r} in this zip unpacks to over {RATIO} times its packed size, "
                      "as a zip bomb does; restore never unpacks it.")


def _zip_stamp(z: zipfile.ZipFile) -> dict | None:
    """The zip's format stamp; None when it has none (a 0.x backup). Refused when it cannot be
    read or is newer than this Keepfold's format."""
    if vault_format.STAMP not in z.namelist():
        return None
    try:
        stamp = json.loads(z.read(vault_format.STAMP).decode("utf-8"))
    except ValueError:
        stamp = None
    f = stamp.get("format") if isinstance(stamp, dict) else None
    if not isinstance(f, int) or isinstance(f, bool) or f < 1:
        raise _refuse(f"This zip's format stamp ({vault_format.STAMP}) cannot be read, so "
                      "restore will not unpack it.")
    if f > vault_format.CODE_FORMAT:
        raise _refuse(f"This backup was made by a newer Keepfold (vault format {f}). Update "
                      "Keepfold, then restore it.")
    return stamp


def restore(zp: Path, dest: Path) -> Path:
    """Unpack backup zip `zp` into `dest`, a new or empty folder. Every member is checked first
    (`_check`; the total against MAX_TOTAL and the free space), a zip stamped by a newer
    Keepfold is refused, the zip unpacks into a .partial folder beside `dest` that is then
    renamed, and the restored vault gets a new vault_id, `restored_from` naming the old one.
    Nothing outside `dest` and its .partial folder is written. Returns `dest`."""
    zp, dest = Path(zp).resolve(), Path(dest).resolve()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise _refuse(f"{dest} is not an empty folder. Restore only into a new or empty folder, "
                      "so nothing is ever restored over a vault.")
    try:
        z = zipfile.ZipFile(zp)
    except (OSError, zipfile.BadZipFile) as e:
        raise _refuse(f"{zp} cannot be read as a zip: {e}") from None
    with z:
        infos = z.infolist()
        for i in infos:
            _check(i)
        names = [i.filename.rstrip("/").casefold() for i in infos]
        if len(set(names)) != len(names):
            raise _refuse("This zip names one file twice, which restore never unpacks.")
        old = _zip_stamp(z)
        total = sum(i.file_size for i in infos)
        parent = next(p for p in dest.parents if p.exists())
        if total > MAX_TOTAL or total >= shutil.disk_usage(parent).free:
            raise _refuse(f"This zip unpacks to {total >> 20:,} MB: more than {parent} has free "
                          f"or than restore unpacks ({MAX_TOTAL >> 30} GB).")
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(tempfile.mkdtemp(prefix=f"{dest.name}.partial-", dir=dest.parent))
        try:
            for i in infos:
                out = partial / i.filename
                if not out.resolve().is_relative_to(partial.resolve()):  # CON, AUX on Windows
                    raise _refuse(f"{i.filename[:200]!r} in this zip is not a plain file name.")
                if i.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as src, open(out, "xb") as dst:
                    shutil.copyfileobj(src, dst)
            _restamp(partial, zp, old)
            if dest.exists():
                dest.rmdir()
            os.replace(partial, dest)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
    return dest


def _restamp(root: Path, zp: Path, old: dict | None) -> None:
    """Give the vault unpacked at `root` a new vault_id: its stamp names the old id in
    `restored_from` ({vault_id, zip, at}), and a format event copies the stamp into the log
    (vault_format.write_stamp rebuilds a lost stamp from the newest one)."""
    import time

    from ulid import ULID

    from keepfold import atomic
    from keepfold.events import EventLog

    v = Vault(root)
    vault_format.write_stamp(v)  # a 0.x zip has no stamp: it gets an upgraded vault's first
    path = root / vault_format.STAMP
    stamp = json.loads(path.read_text(encoding="utf-8"))
    stamp |= {"vault_id": str(ULID()), "restored_from": {
        "vault_id": (old or {}).get("vault_id"), "zip": zp.name,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}
    with v.lock():
        EventLog(v).append(session="cli", kind="change", source="tutor", fields={"payload": {
            "note": "format", "format": stamp["format"], "vault_id": stamp["vault_id"],
            "applied": stamp.get("applied") or [], "by": vault_format._version(),
            "restored_from": stamp["restored_from"]["vault_id"]}})
        atomic.replace(path, json.dumps(stamp, indent=2) + "\n")
        v._track(path)
