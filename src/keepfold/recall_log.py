"""Recall usage log and the offline ranking eval (NEXT.md item 1).

`.keepfold/recall-log.jsonl` (tool-private, gitignored, never an event): one row per
keepfold_recall / keepfold_context(topic) call `{ts, rid, tool, query, ids}` and one per page read
soon after it `{ts, opened: rid, id}`. The eval set is (query, opened ids) for each recall whose
returned ids were later opened; `evaluate` replays the queries through each ranking variant.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ulid import ULID

from keepfold import semantic
from keepfold.vault import Vault, VaultError

log = logging.getLogger(__name__)

FILE = ".keepfold/recall-log.jsonl"
CAP = 5000  # rows kept
WINDOW_S = 600  # a read this soon after a recall counts as opening its result
MIN_TRUST = 10  # fewer queries than this: the numbers are noise
VARIANTS = ("current", "rrf", "rerank", "rrf+rerank", "no-downrank")  # last: current, chats=True


def _append(vault: Vault, row: dict) -> None:
    path = vault.root / FILE
    line = json.dumps({"ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), **row},
                      ensure_ascii=False, separators=(",", ":"))
    try:
        with vault.lock():  # the lock creates .keepfold/
            old = path.read_text("utf-8").splitlines() if path.is_file() else []
            vault.write_text(path,"\n".join([*old, line][-CAP:]) + "\n")
    except (VaultError, OSError) as e:  # usage stats: never fail the tool over them
        log.warning("recall log not written: %s", e)


def log_recall(vault: Vault, tool: str, query: str, ids: list[str]) -> str:
    """Append a recall row; returns its id for `log_open`. Secrets in the query are redacted
    (the shared secret_rules, via chats.redact; vault.write_text skips its scan in .keepfold/)."""
    from keepfold.chats import redact

    rid = str(ULID())
    _append(vault, {"rid": rid, "tool": tool, "query": redact(query), "ids": ids})
    return rid


def log_open(vault: Vault, rid: str, id: str) -> None:
    _append(vault, {"opened": rid, "id": id})


def rows(vault: Vault) -> list[dict]:
    path = vault.root / FILE
    out = []
    for line in path.read_text("utf-8").splitlines() if path.is_file() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def eval_set(rows: list[dict]) -> list[tuple[str, set[str]]]:
    """(query, relevant ids): every id opened after a recall that returned at least one of
    them. Opened ids it did not return still count, so recall@k can miss."""
    opened: dict[str, set[str]] = {}
    for r in rows:
        if r.get("opened"):
            opened.setdefault(r["opened"], set()).add(str(r.get("id")))
    return [(r["query"], rel) for r in rows
            if r.get("rid") and (rel := opened.get(r["rid"])) and rel & set(r.get("ids") or [])]


def evaluate(index, pairs, embed, ce, k: int = 8) -> dict[str, tuple[float, float] | None]:
    """Variant -> (mean recall@k, mean MRR@k); None for a rerank variant without `ce`.
    `no-downrank` is `current` without the CHAT_WEIGHT down-rank of imported chat pages."""
    out: dict[str, tuple[float, float] | None] = {}
    for v in VARIANTS:
        if "rerank" in v and ce is None:
            out[v] = None
            continue
        rec = mrr = 0.0
        for query, rel in pairs:
            got = [h["id"] for h in semantic.recall(
                index, query, embed, k, 1, ce if "rerank" in v else None,
                "rrf" if v.startswith("rrf") else "weighted", chats=v == "no-downrank")][:k]
            rec += len(rel & set(got)) / len(rel)
            mrr += next((1 / (n + 1) for n, i in enumerate(got) if i in rel), 0.0)
        out[v] = (rec / len(pairs), mrr / len(pairs)) if pairs else (0.0, 0.0)
    return out
