"""`keepfold eval loop`: how much of what a student says in chats the core loop keeps, serves and
keeps right, with no model. Ported from the D2 study (1.0 research). It builds a made-up corpus
(tests/fixtures/loop/: 21 chats holding 26 known durable items, a vault with 20 prompts) in a temp
home and a temp vault, runs the real no-model code over it (chat import, safety net, fact harvest
tier 0, prime, tidy, keyword search) and counts what survived. It never reads ~/.keepfold or any
vault of yours, and it refuses to run if the temp home would be your real one.

The numbers are today's baseline; Wave A's fixes (A21, A22) move them. No model is loaded or
downloaded: the temp home has none, so search is keyword-only, as D2 measured. `run(embed=
FakeEmbed())` swaps in the deterministic hashing embedder defined here (it changes the
numbers, so it is not the baseline).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
import zlib
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "loop"
ENV_CLEARED = ("KEEPFOLD_VAULT", "KEEPFOLD_REAL_VAULT", "CLAUDE_PROJECT_DIR", "CODEX_HOME",
               "GEMINI_CLI_HOME", "COPILOT_HOME", "XDG_CONFIG_HOME")
CLIENT = "claude-code"
PRIME_PROMPTS = ["Give me a DSA problem", "How should you format answers for me?",
                 "Set up my editor", "Teach me Rust lifetimes", "Where do I live?",
                 "Plan my week", "Write my bio", "Explain graphs", "Recommend a phone",
                 "Explain Dijkstra"] * 2


class LoopRefused(Exception):
    """The temp home would be the real one, or the fixtures are missing."""


class FakeEmbed:
    """A deterministic embedder: each word hashes into one of DIM slots; unit length. Equal
    texts give equal vectors, shared words give a positive cosine. (src may not import
    tests/conftest.FakeEmbed, so this is its own.)"""
    DIM = 64

    def __call__(self, texts) -> np.ndarray:
        out = np.zeros((len(texts), self.DIM), dtype=np.float32)
        for k, t in enumerate(texts):
            for w in re.findall(r"\w+", t.lower()):
                out[k, zlib.crc32(w.encode()) % self.DIM] += 1
            n = float(np.linalg.norm(out[k]))
            if n:
                out[k] /= n
        return out


def _norm(p: Path | str) -> str:
    return os.path.normcase(str(Path(p).resolve()))


def real_homes() -> set[str]:
    """Where the user's real home is, read before anything switches: Path.home() and the
    USERPROFILE / HOME variables."""
    got = {_norm(Path.home())}
    got |= {_norm(v) for k in ("USERPROFILE", "HOME") if (v := os.environ.get(k))}
    return got


@contextmanager
def sandbox(home: Path | None = None, embed=None):
    """A temp HOME (and APPDATA, chat and download folders) for the block; yields its path and
    puts everything back. Refuses when `home` is a real home folder."""
    real = real_homes()
    tmp = Path(tempfile.mkdtemp(prefix="keepfold-loop-"))
    home = Path(home) if home is not None else tmp / "home"
    if _norm(home) in real:
        shutil.rmtree(tmp, ignore_errors=True)
        raise LoopRefused(f"{home} is your real home folder; the loop eval only runs in a temp "
                          "home and will not touch it.")
    home.mkdir(parents=True, exist_ok=True)
    from keepfold import semantic

    keys = {*ENV_CLEARED, "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "KEEPFOLD_CHATS_DIR",
            "KEEPFOLD_CHAT_EXPORT_DIR", "KEEPFOLD_DOWNLOADS_DIR", "GIT_AUTHOR_NAME",
            "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"}
    saved_env = {k: os.environ.get(k) for k in keys}
    saved_home, saved_embed = Path.__dict__.get("home"), semantic._embedder
    try:
        for k in ENV_CLEARED:
            os.environ.pop(k, None)
        for k in ("HOME", "USERPROFILE"):
            os.environ[k] = str(home)
        for k in ("APPDATA", "LOCALAPPDATA"):
            (home / k).mkdir(exist_ok=True)
            os.environ[k] = str(home / k)
        for k, d in (("KEEPFOLD_CHATS_DIR", "projects"), ("KEEPFOLD_CHAT_EXPORT_DIR", "exports"),
                     ("KEEPFOLD_DOWNLOADS_DIR", "downloads")):
            (tmp / d).mkdir(exist_ok=True)
            os.environ[k] = str(tmp / d)
        os.environ.update(GIT_AUTHOR_NAME="Test", GIT_COMMITTER_NAME="Test",
                          GIT_AUTHOR_EMAIL="test@example.invalid",
                          GIT_COMMITTER_EMAIL="test@example.invalid")
        Path.home = classmethod(lambda cls: home)  # a test run may have pinned it already
        if _norm(Path.home()) in real:  # the switch did not take: stop before anything runs
            raise LoopRefused("could not move the home folder; refusing to run.")
        semantic._embedder = embed  # None: keyword-only, as D2 measured; never a real model
        yield tmp
    finally:
        semantic._embedder = saved_embed
        if saved_home is None:
            del Path.home
        else:
            Path.home = saved_home
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def load(name: str, fixtures: Path | None = None) -> dict | list:
    p = (fixtures or FIXTURES) / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LoopRefused(f"cannot read the loop fixtures ({p}): they live in a repo checkout, "
                          f"tests/fixtures/loop/. {e}") from None


def _config(vault: Path) -> None:
    d = Path.home() / ".keepfold"
    d.mkdir(exist_ok=True)
    (d / "config.yaml").write_text(f"vault: '{vault.as_posix()}'\n", encoding="utf-8")


def _expand(text, filler: str):
    if isinstance(text, list):
        return [(k, _expand(v, filler)) for k, v in text]
    return re.sub(r"\{FILLER\*(\d+)\}", lambda m: filler * int(m.group(1)), text)


def _line(role: str, text, ts: str, cwd: str) -> dict:
    """One Claude Code transcript line."""
    base = {"timestamp": ts, "cwd": cwd, "isSidechain": False, "sessionId": "s"}
    if role == "user":
        return {"type": "user", "message": {"role": "user", "content": text}, **base}
    blocks = []
    for kind, val in (text if isinstance(text, list) else [("text", text)]):
        if kind == "text":
            blocks.append({"type": "text", "text": val})
        elif kind == "Bash":
            blocks.append({"type": "tool_use", "name": "Bash", "input": {"command": val}})
        else:
            blocks.append({"type": "tool_use", "name": kind, "input": {"file_path": val}})
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}, **base}


def _has(text: str, kws: list[str]) -> bool:
    t = text.casefold()
    return all(k.casefold() in t for k in kws)


def capture(root: Path, corpus: dict) -> dict:
    """D2 (a): the chats through the automatic no-model paths, then where each of the 26
    durable items ended up. `root` is the vault (a copy of vault_mini); returns the counts."""
    from ulid import ULID

    from keepfold import chat_import, chats, claims, fact_harvest, safety_net
    from keepfold.vault import Vault

    now = datetime.now(UTC).replace(microsecond=0)
    old = now.timestamp() - 3600

    def ts(age, i):
        return (now - timedelta(days=age, hours=3) + timedelta(minutes=2 * i)).isoformat()

    for c in corpus["chats"]:
        cwd = r"C:\proj\messmate-api" if c["coding"] else r"C:\Users\student"
        turns = [(r, _expand(t, corpus["filler"])) for r, t in c["turns"]]
        if c["source"] == "claude-live-first-only":  # the Chat tab: only the first message logged
            d = chats.export_root() / chats.LIVE
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{ULID()}.jsonl"
            p.write_text(json.dumps({"type": "user", "message": {"content": turns[0][1]},
                                     "timestamp": ts(c["age"], 0)}) + "\n", encoding="utf-8")
        else:
            d = chats.root() / ("C--proj-messmate-api" if c["coding"] else "C--Users-student")
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{uuid.uuid4()}.jsonl"
            p.write_text("".join(json.dumps(_line(r, t, ts(c["age"], i), cwd)) + "\n"
                                 for i, (r, t) in enumerate(turns)), encoding="utf-8")
        os.utime(p, (old, old))
    convs = []
    for e in corpus["export"]:
        t0 = now - timedelta(days=e["age"])
        convs.append({"uuid": str(uuid.uuid4()), "name": "katakana", "created_at": t0.isoformat(),
                      "updated_at": t0.isoformat(), "chat_messages": [
                          {"sender": "human" if r == "user" else "assistant", "text": t,
                           "created_at": (t0 + timedelta(minutes=i)).isoformat(), "content": []}
                          for i, (r, t) in enumerate(e["turns"])]})
    exp = root.parent / "export"
    exp.mkdir()
    (exp / "conversations.json").write_text(json.dumps(convs), encoding="utf-8")
    chats.import_export(exp / "conversations.json")

    v = Vault(root)
    chat_import.run(v)
    safety_net.run(v, None, None)
    for _ in range(4):  # the app runs one pass every 5 minutes, 10 chats a pass
        if not fact_harvest.run(v, None):
            break

    rows = claims.rows(v)
    pages = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
             for p in root.rglob("*.md") if ".keepfold" not in p.parts}
    chat_pages = {k: t for k, t in pages.items()
                  if k.startswith(("archive/chats/", "resources/chat-"))}
    layers = {}
    for g in corpus["gold"]:
        kws = g["keywords"]
        layers[g["id"]] = ("me-line" if any(_has(r["text"], kws) for r in rows)
                           else "decision/project page" if any(
                               k.startswith(("decisions/", "projects/")) and _has(t, kws)
                               for k, t in pages.items())
                           else "chat text only" if any(_has(t, kws) for t in chat_pages.values())
                           else "LOST")
    dp = [g["id"] for g in corpus["gold"] if g["kind"] in ("decision", "project")]
    n = Counter(layers.values())
    return {"items": len(layers), "lost": n["LOST"], "me_line": n["me-line"],
            "text_only": n["chat text only"],
            "decisions_projects": len(dp),
            "decisions_projects_proposed": sum(layers[i] == "decision/project page" for i in dp),
            "negatives_saved": sum(any(_has(r["text"], n_["keywords"]) for r in rows)
                                   for n_ in corpus["negative"]),
            "layers": layers}


def recall_text(root: Path, corpus: dict) -> dict:
    """D2 (a6): can keyword search find the chat that holds each question's answer?"""
    from keepfold import semantic
    from keepfold.catalog import Catalog
    from keepfold.index import Index
    from keepfold.vault import Vault

    v = Vault(root)
    idx = Index(v, Catalog.load(v))
    first = five = 0
    for q in corpus["questions"]:
        ids = [h["id"] for h in semantic.search(idx, q["ask"], 5, 1)[0]]
        r = next((i + 1 for i, x in enumerate(ids) if q["page_has"] in x), None)
        first += r == 1
        five += r is not None
    return {"found_1": first, "found_5": five, "asked": len(corpus["questions"])}


def tier0(sentences: list[str]) -> dict:
    from keepfold import fact_harvest as fh

    return {"hits": sum(bool(fh.tier0([s])) for s in sentences), "of": len(sentences)}


def retire(root: Path) -> dict:
    """D2 (a4): 20 primes on the capture vault, then the tidy as of 31 days on. A harvested line
    no app used retires; `silent` is how many went with the X-ray's ring older than the guess,
    the case an owner who used Keepfold before the harvest is in."""
    from keepfold import claims
    from keepfold.events import EventLog, local_day
    from keepfold.mcp_server import Keepfold
    from keepfold.vault import Vault

    v = Vault(root)
    c = Keepfold(v)
    c.over_mcp, c.sessions.client = True, CLIENT
    for p in PRIME_PROMPTS:
        c.keepfold_prime(p)
    now = datetime.now(UTC).replace(microsecond=0)
    future = now + timedelta(days=31)
    ring = root / ".keepfold" / "xray.jsonl"
    lines = ring.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["ts"] = (now - timedelta(days=3)).isoformat()
    ring.write_text("\n".join([json.dumps(first), *lines[1:]]) + "\n", encoding="utf-8")
    c2 = Keepfold(v)
    c2.sessions._now = lambda: future
    before = [r["text"] for r in claims.tiers(v, list(EventLog(v).iter_events()))
              if r["status"] == "live"]
    due = [r["text"] for r in c2._old_guesses(list(EventLog(v).iter_events()), local_day(future))]
    gone = c2.tidy().get("guesses", 0)
    return {"silent": gone, "due": due, "live_before": len(before)}


def _build_prime_vault(root: Path, spec: dict, grown: bool):
    from keepfold import agent_eval, taint

    def body(title, sections):
        out = [f"# {title}", ""]
        for head, lines in sections.items():
            out += [f"## {head}", *[f"- {x}" for x in lines], ""]
        return "\n".join(out)

    prefs = {h: list(x) for h, x in spec["prefs"].items()}
    if grown:  # months later: more lines of each kind
        for h, x in spec["grown_extra"].items():
            prefs[h] = prefs.get(h, []) + x
    block = {"pages": [
        {"kind": "me", "id": "core", "fields": {"title": "Core"},
         "body": body("Core", {"Core": spec["core"]})},
        {"kind": "me", "id": "profile", "fields": {"title": "Profile", "summary": "Who I am"},
         "body": body("Profile", {"Background": spec["profile"]})},
        {"kind": "me", "id": "preferences", "fields": {"title": "Preferences"},
         "body": body("Preferences", prefs)},
        *spec["pages"]], "guesses": spec["guesses"]}
    with taint.pinned({}):
        return agent_eval.build(root, block)


def prime_scores(base: Path, spec: dict, name: str, grown: bool) -> dict:
    """D2 (b): 20 prompts through the hook's light prime (`prime.core`: what Claude Code, Codex
    and Gemini CLI get with no model's help) and the full keepfold_prime an AI model must call."""
    from keepfold import prime
    from keepfold.events import EventLog
    from keepfold.mcp_server import Keepfold

    v = _build_prime_vault(base / f"vault-{name}", spec, grown)
    _config(base / f"vault-{name}")
    guesses = {g[2]: set(spec["guess_fits"].get(g[2], [])) for g in spec["guesses"]}
    out = {}
    for chan in ("hook", "mcp"):
        c = Keepfold(v) if chan == "mcp" else None
        if c is not None:
            c.over_mcp, c.sessions.client = True, CLIENT
        must = got = rel = relhit = fit = fit_served = 0
        for case in spec["cases"]:
            if chan == "hook":
                parts, _ = prime.core(v, case["prompt"], list(EventLog(v).iter_events()),
                                      datetime.now(UTC), set(), {}, None, CLIENT)
                ctx = "\n\n".join(parts)
            else:
                ctx = c.keepfold_prime(case["prompt"])["context"]
            folded = ctx.casefold()
            must += len(case["must"])
            got += sum(f.casefold() in folded for f in case["must"])
            rel += len(case["related"])
            relhit += sum(f"[[{r}]]" in ctx for r in case["related"])
            for g, prompts in guesses.items():
                if case["prompt"] in prompts:
                    fit += 1
                    fit_served += g.casefold() in folded
        out[chan] = {"must": must, "got": got, "related": rel, "related_hit": relhit,
                     "guess_fit": fit, "guess_served": fit_served}
    return out


def run(fixtures: Path | None = None, home: Path | None = None, embed=None) -> dict:
    """Run every part in a temp home and vault; returns the numbers (see `summary`)."""
    corpus = load("corpus.json", fixtures)
    spec = load("prime.json", fixtures)
    sentences = load("tier0.json", fixtures)["sentences"]
    with sandbox(home, embed) as tmp:
        base = tmp / "work"
        base.mkdir()
        root = base / "vault"
        shutil.copytree(Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "vault_mini",
                        root)
        (root / ".keepfold").mkdir(exist_ok=True)
        _config(root)
        cap = capture(root, corpus)
        found = recall_text(root, corpus)
        retired = retire(root)
        t0 = tier0(sentences)
        primes = {n: prime_scores(base, spec, n, g) for n, g in (("baseline", False),
                                                                  ("grown", True))}
    b, g = primes["baseline"], primes["grown"]
    return {
        "items": cap["items"], "lost": cap["lost"], "me_line": cap["me_line"],
        "text_only": cap["text_only"], "decisions_projects": cap["decisions_projects"],
        "decisions_projects_proposed": cap["decisions_projects_proposed"],
        "negatives_saved": cap["negatives_saved"],
        "guess_fit": sum(p[c]["guess_fit"] for p in (b, g) for c in p),
        "guess_served": sum(p[c]["guess_served"] for p in (b, g) for c in p),
        "related_expected": b["hook"]["related"], "related_hook": b["hook"]["related_hit"],
        "related_mcp": b["mcp"]["related_hit"],
        "silent_retirements": retired["silent"],
        "must_total": b["hook"]["must"], "must_hook": b["hook"]["got"],
        "must_mcp": b["mcp"]["got"], "must_hook_grown": g["hook"]["got"],
        "tier0_hits": t0["hits"], "tier0_of": t0["of"],
        "found_5": found["found_5"], "found_asked": found["asked"],
    }


def summary(r: dict) -> str:
    pc = r["must_hook"] / r["must_total"]
    return "\n".join([
        f"Core loop with no model: {r['items']} durable items said in made-up chats, then the "
        "next chat's prime.",
        f"Items lost of {r['items']}: {r['lost']}  (a line about you: {r['me_line']}, found "
        f"only as chat text: {r['text_only']})",
        f"Decisions and projects proposed, of {r['decisions_projects']}: "
        f"{r['decisions_projects_proposed']}",
        f"Guesses served when they fit, of {r['guess_fit']} prompts: {r['guess_served']}",
        f"The hook's Related pages, of {r['related_expected']}: {r['related_hook']}  "
        f"(a model's prime: {r['related_mcp']})",
        f"Silent retirements of harvested lines after 31 days: {r['silent_retirements']}",
        f"Keyword must-serve recall (hook): {r['must_hook']}/{r['must_total']} = {pc:.0%}  "
        f"(model's prime {r['must_mcp']}, hook on a 3.5x vault {r['must_hook_grown']})",
        f"Also: tier 0 gave a line for {r['tier0_hits']} of {r['tier0_of']} sentences; keyword "
        f"search found the right chat in the top 5 for {r['found_5']} of {r['found_asked']} "
        "questions.",
    ])


