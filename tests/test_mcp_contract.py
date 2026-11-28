"""A7 (D7 §4.1, §5): the MCP contract frozen at 1.0, guarded by tests/fixtures/contract/.

`mcp-1.json` holds each tool's inputSchema and annotations for an owner, a reader and a
`--private` server, the resource URIs, the prompt's arguments and the error codes docs/tools.md
lists; `results-1.json` holds each tool's top-level result keys on canonical calls. Both are
rewritten only under KEEPFOLD_WRITE_CONTRACT=1, and docs/tools.md's `contract-sha:` line must
name mcp-1.json's sha256, so a regenerated snapshot also touches the docs. The checker lets the
contract grow (a new optional property, a new enum value, a relaxed `required`) and fails a
removal, a rename, a type change, optional→required, a dropped enum value or a changed default.
Descriptions, instructions and `display` text are soft and not snapshotted."""

import hashlib
import json
import os
import re
from pathlib import Path

import anyio
import pytest
from conftest import events
from mcp import Client

from keepfold.mcp_server import DESCRIPTIONS, build_server
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

SNAP = Path(__file__).parent / "fixtures" / "contract"
MCP_SNAP, RESULTS_SNAP = SNAP / "mcp-1.json", SNAP / "results-1.json"
DOCS = Path(__file__).parents[1] / "docs" / "tools.md"
WRITE = os.environ.get("KEEPFOLD_WRITE_CONTRACT") == "1"
SOFT = {"description", "title", "examples"}  # wording: never frozen


def dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def error_codes() -> list[str]:
    """The codes docs/tools.md's "Every error" convention lists."""
    line = next(ln for ln in DOCS.read_text(encoding="utf-8").splitlines()
                if ln.startswith("- **Every error**"))
    return sorted({c for c in re.findall(r"`([a-z_]+)`", line) if not c.startswith("keepfold_")})


def snapshot(root) -> dict:
    """The contract as the servers serve it now (public MCPServer API, no client needed)."""
    async def tools(server):
        out = {}
        for t in await server.list_tools():
            wire = t.model_dump(mode="json", by_alias=True, exclude_none=True)
            out[t.name] = {"inputSchema": wire["inputSchema"],
                           "annotations": wire.get("annotations", {})}
        return out

    async def main():
        owner = build_server(Vault(root))
        prompts = {}
        for p in await owner.list_prompts():
            args = p.arguments or []
            prompts[p.name] = {"properties": {a.name: {} for a in args},
                               "required": [a.name for a in args if a.required]}
        return {"servers": {"owner": await tools(owner),
                            "reader": await tools(build_server(Vault(root), "reader")),
                            "private": await tools(build_server(Vault(root), private=True))},
                "resources": sorted(str(r.uri) for r in await owner.list_resources()),
                "prompts": prompts,
                "errors": error_codes()}

    return anyio.run(main)


def schema_breaks(old, new, at: str) -> list[str]:
    """What in JSON schema `new` breaks a caller written against `old`; [] when compatible."""
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return [] if old == new else [f"{at}: {old!r} became {new!r}"]
    out = []
    for k, was in old.items():
        now = new.get(k)
        if k in SOFT or k == "required":
            continue
        if k in ("properties", "$defs"):
            for p, s in was.items():
                out += (schema_breaks(s, now[p], f"{at}.{p}") if p in (now or {})
                        else [f"{at}.{p}: removed"])
        elif k == "enum":
            if lost := [e for e in was if e not in (now or [])]:
                out.append(f"{at}: enum values dropped: {lost}")
        elif k in ("anyOf", "oneOf"):
            if lost := [o for o in was if o not in (now or [])]:
                out.append(f"{at}: {k} options dropped: {lost}")
        elif k not in new:
            if k == "default":
                out.append(f"{at}: default {was!r} dropped")
            # any other keyword gone (type, a bound) accepts more: relaxed
        elif k in ("items", "additionalProperties"):
            out += schema_breaks(was, now, f"{at}.{k}")
        elif now != was:
            out.append(f"{at}: {k} {was!r} became {now!r}")
    if newly := sorted(set(new.get("required", [])) - set(old.get("required", []))):
        out.append(f"{at}: now required: {newly}")
    grown = {"default", "properties", "$defs", "required"}  # checked above, or harmless
    for k in sorted(new.keys() - old.keys() - SOFT - grown):
        out.append(f"{at}: new constraint {k}")
    return out


def contract_breaks(old: dict, new: dict) -> list[str]:
    out = []
    for srv, tools in old["servers"].items():
        cur = new["servers"].get(srv, {})
        for name, t in tools.items():
            if name not in cur:
                out.append(f"{srv}: tool {name} removed")
                continue
            out += schema_breaks(t["inputSchema"], cur[name]["inputSchema"], f"{srv}:{name}")
            if t["annotations"] != cur[name]["annotations"]:
                out.append(f"{srv}:{name}: annotations changed")
    out += [f"resource {u} removed" for u in old["resources"] if u not in new["resources"]]
    for name, args in old["prompts"].items():
        out += (schema_breaks(args, new["prompts"][name], f"prompt {name}")
                if name in new["prompts"] else [f"prompt {name} removed"])
    out += [f"error code {c} removed" for c in old["errors"] if c not in new["errors"]]
    return out


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()  # LF on any OS


@pytest.mark.gate("G4")
def test_mcp_contract(root):
    cur = snapshot(root)
    if WRITE:
        SNAP.mkdir(parents=True, exist_ok=True)
        MCP_SNAP.write_text(dump(cur), encoding="utf-8", newline="\n")
    old = json.loads(MCP_SNAP.read_text(encoding="utf-8"))
    assert contract_breaks(old, cur) == []
    assert f"contract-sha: {sha(MCP_SNAP)}" in DOCS.read_text(encoding="utf-8"), (
        "tests/fixtures/contract/mcp-1.json changed: update docs/tools.md's contract-sha line")


BASE = {"type": "object", "required": ["q"], "properties": {
    "q": {"type": "string"},
    "n": {"type": "integer", "default": 5},
    "m": {"type": "string", "enum": ["a", "b"]}}}


def changed(**edits) -> dict:
    """BASE with property edits: name -> new schema (None removes it); `required=` replaces."""
    out = json.loads(json.dumps(BASE))
    if "required" in edits:
        out["required"] = edits.pop("required")
    for name, s in edits.items():
        if s is None:
            out["properties"].pop(name)
        else:
            out["properties"][name] = s
    return out


@pytest.mark.parametrize("new, why", [
    (changed(n=None), "n: removed"),
    (changed(q=None, query={"type": "string"}, required=["query"]), "q: removed"),  # a rename
    (changed(n={"type": "string", "default": 5}), "type 'integer' became 'string'"),
    (changed(required=["q", "n"]), "now required: ['n']"),
    (changed(m={"type": "string", "enum": ["a"]}), "enum values dropped: ['b']"),
    (changed(n={"type": "integer", "default": 6}), "default 5 became 6"),
    (changed(n={"type": "integer"}), "default 5 dropped"),
])
def test_checker_fails_a_break(new, why):
    assert any(why in b for b in schema_breaks(BASE, new, "t")), schema_breaks(BASE, new, "t")


@pytest.mark.parametrize("new", [
    changed(extra={"type": "boolean"}),  # a new optional property
    changed(m={"type": "string", "enum": ["a", "b", "c"]}),  # a new enum value
    changed(required=[]),  # relaxed
    {**BASE, "description": "Reworded."},  # soft
])
def test_checker_allows_growth(new):
    assert schema_breaks(BASE, new, "t") == []


def test_checker_fails_a_removed_tool_resource_prompt_or_code():
    old = {"servers": {"owner": {"keepfold_x": {"inputSchema": BASE, "annotations": {}}}},
           "resources": ["keepfold://core"], "prompts": {"keepfold": {"properties": {}}},
           "errors": ["invalid_argument"]}
    gone = {"servers": {"owner": {}}, "resources": [], "prompts": {}, "errors": []}
    assert len(contract_breaks(old, gone)) == 4
    assert contract_breaks(old, old) == []


def test_snapshot_covers_the_servers():
    snap = json.loads(MCP_SNAP.read_text(encoding="utf-8"))
    assert set(snap["servers"]["owner"]) == set(DESCRIPTIONS)
    assert set(snap["servers"]["private"]) == {*DESCRIPTIONS, "keepfold_browse_search"}
    log = snap["servers"]["owner"]["keepfold_log"]["inputSchema"]["properties"]["kind"]["enum"]
    assert {"need", "need_skip"} <= set(log)  # refused at run time, kept in the schema (A7)


# canonical calls on vault_mini, in an order that works (start before ask, ask before grade)
CALLS = [
    ("keepfold_prime", {"prompt": "help me with two pointers"}),
    ("keepfold_context", {}),
    ("keepfold_find", {"query": "pointers"}),
    ("keepfold_recall", {"query": "two pointers"}),
    ("keepfold_read", {"id": "two-pointers"}),
    ("keepfold_chats", {}),
    ("keepfold_as_me", {"task": "Cover letter", "audience": "recruiter"}),
    ("keepfold_reflect", {"action": "lint"}),
    ("keepfold_start", {"mode": "learn", "model": "test-model"}),
    ("keepfold_ask", {"concept": "two-pointers", "item_type": "recall",
                      "question": "Which option?", "answer_key": "B"}),
    ("keepfold_grade", {"item_id": None, "learner_answer": "B", "verdict": "correct"}),
    ("keepfold_log", {"kind": "taught", "payload": {"concept": "two-pointers"}}),
    ("keepfold_patch", {"id": "me/profile", "op": "append_to_section", "target": "Brief",
                        "text": "- Likes Python."}),
    ("keepfold_add", {"kind": "problem", "title": "Container With Most Water"}),
    ("keepfold_end", {"summary": "Done."}),
]


def call_all(root, calls, private=False):
    """Each (tool, args) over the SDK's in-memory client; the structured results in order."""
    async def main():
        out = []
        async with Client(build_server(Vault(root), private=private)) as c:
            for name, args in calls:
                if args.get("item_id", 0) is None:  # the ask just before
                    args = {**args, "item_id": out[-1]["item_id"]}
                r = await c.call_tool(name, args)
                assert not r.is_error, r.content
                out.append(r.structured_content)
        return out

    return anyio.run(main)


def test_mcp_result_keys(root):
    got = {name: sorted(r) for (name, _), r in zip(CALLS, call_all(root, CALLS), strict=True)}
    assert set(got) == set(DESCRIPTIONS)  # a new tool needs a canonical call here
    for name, keys in got.items():
        assert "error" not in keys, (name, keys)
    if WRITE:
        RESULTS_SNAP.write_text(dump(got), encoding="utf-8", newline="\n")
    snap = json.loads(RESULTS_SNAP.read_text(encoding="utf-8"))
    for name, keys in snap.items():
        assert not set(keys) - set(got[name]), (name, set(keys) - set(got[name]))
    err = call_all(root, [("keepfold_read", {"id": "no-such-page"})])[0]
    assert {"error", "message", "next"} <= set(err) and err["error"] in error_codes()


def test_unknown_argument_refused(root):
    before = len(events(root))
    typo, extra, write, private = call_all(root, [
        ("keepfold_recall", {"qurey": "two pointers"}),  # SDK alone: a pydantic "Field required"
        ("keepfold_read", {"id": "two-pointers", "bogus": 1}),  # SDK alone: silently dropped
        ("keepfold_log", {"kind": "taught", "payload": {"concept": "two-pointers"}, "concpet": 1}),
        ("keepfold_browse_search", {"query": "x", "kk": 2}),  # not wrapped by build_server's tool()
    ], private=True)
    # the caller's names are echoed bounded: 5 at most, 40 printable characters each
    flood = {"query": "x", "k" * 5000: 1, "a\x00\n\x1bb": 2, **{f"u{i}": i for i in range(6)}}
    (big,) = call_all(root, [("keepfold_recall", flood)])
    assert big["error"] == "invalid_argument" and len(big["message"]) < 600, big["message"]
    assert "'" + "k" * 40 + "'" in big["message"] and "and 3 more" in big["message"]
    assert "'ab'" in big["message"] and "\x00" not in big["message"]
    assert "its arguments are: query, " in big["message"]
    assert typo["error"] == "invalid_argument" and typo["next"]
    assert "keepfold_recall has no argument 'qurey'" in typo["message"]
    assert "its arguments are: query, " in typo["message"]
    assert "'bogus'" in extra["message"] and "'concpet'" in write["message"]
    assert private["error"] == "invalid_argument"
    assert len(events(root)) == before  # the refused log recorded nothing
