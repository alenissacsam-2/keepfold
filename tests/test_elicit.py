"""Idea 11 (docs/design/v6/ideas-r2.md): a `user_confirmed` claim line asks the user in an MCP
form-elicitation dialog when the client can show one; the change event stamps how the yes came.
Both protocol eras: `legacy` asks mid-call (elicitation/create), `auto` (in-process, 2026-07-28)
asks through input_required and a retried call."""

import anyio
import pytest
from conftest import events
from mcp import Client
from mcp.types import ElicitResult, ErrorData
from typer.testing import CliRunner

from keepfold import claims, know, mcp_server
from keepfold.cli import app
from keepfold.events import EventLog
from keepfold.mcp_server import build_server
from keepfold.vault import Vault

pytestmark = pytest.mark.usefixtures("home")

PREF = {"id": "me/preferences", "op": "append_to_section", "target": "Coding",
        "text": "- Tabs over [spaces](x.test/tabs)", "user_confirmed": True}
YES = ElicitResult(action="accept", content={"yes": True})
NO_CAPABILITY = object()
MODES = ("legacy", "auto")


def drive(root, reply, *calls, mode="legacy"):
    """Send each keepfold_patch of `calls` (default: PREF) from a client whose dialog answers
    `reply` (an ElicitResult, an ErrorData, an exception to raise, or a coroutine function);
    NO_CAPABILITY: a client without elicitation. Returns (results, dialog params)."""
    (root / "me" / "preferences.md").write_bytes(b"# Preferences\n\n## Coding\n")
    asked, out = [], []

    async def dialog(ctx, params):
        asked.append(params)
        if isinstance(reply, Exception):
            raise reply
        return await reply() if callable(reply) else reply

    async def main():
        cb = None if reply is NO_CAPABILITY else dialog
        async with Client(build_server(Vault(root)), mode=mode, elicitation_callback=cb) as c:
            for args in calls or (PREF,):
                r = await c.call_tool("keepfold_patch", args)
                assert not r.is_error, r.content
                out.append(r.structured_content)
                assert out[-1]["next"]

    anyio.run(main)
    return out, asked


def stamps(root):
    return [e["payload"]["change"] for e in events(root) if e["kind"] == "change"
            and e["payload"].get("change", {}).get("id") == "preferences"]


def live(root):
    v = Vault(root)
    return [(r["tier"], r["confirm"]) for r in claims.tiers(v, EventLog(v).iter_events())
            if r["page"] == "preferences"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("reply", [YES, ElicitResult(action="accept", content={})])
def test_accept_with_yes_stamps_clicked(root, reply, mode):
    """The box is ticked by default, so a bare accept (no content) is a yes too."""
    (out,), (asked,) = drive(root, reply, mode=mode)
    assert out["changed"] is True and out["claim"].startswith("c-")
    assert "me/preferences.md" in asked.message and "Tabs over spaces" in asked.message
    assert "x.test" not in asked.message  # note text is untrusted: plain, links stripped
    assert asked.requested_schema["properties"]["yes"]["type"] == "boolean"
    assert asked.requested_schema["properties"]["yes"]["default"] is True
    assert "Accept to save it; untick or decline to skip." in asked.message
    (c,) = stamps(root)
    assert (c["tier"], c["confirm"]) == ("confirmed", "clicked")
    assert live(root) == [("confirmed", "clicked")]
    rep = know.report(Vault(root), events(root), "2026-11-16")
    prefs = next(p for p in rep["pages"] if p["page"] == "preferences")
    assert prefs["clicked"] == rep["total"]["clicked"] == 1
    r = CliRunner().invoke(app, ["know", "--vault", str(root)])
    assert r.exit_code == 0, r.output
    assert "(confirmed: 1 clicked, 0 reflected, 0 relayed)" in r.output, r.output


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("reply", [ElicitResult(action="decline"), ElicitResult(action="cancel"),
                                   ElicitResult(action="accept", content={"yes": False})])
def test_no_writes_nothing(root, reply, mode):
    (out,), asked = drive(root, reply, mode=mode)
    assert len(asked) == 1
    assert out["changed"] is False and out["declined"] is True
    assert "do not ask about it again" in out["next"]
    assert (root / "me" / "preferences.md").read_bytes() == b"# Preferences\n\n## Coding\n"
    assert stamps(root) == []


async def _slow():
    await anyio.sleep(5)


@pytest.mark.parametrize("reply", [ErrorData(code=-32600, message="no dialog here"),
                                   RuntimeError("client crashed"), _slow])
def test_dialog_failure_falls_back_to_relayed(root, monkeypatch, reply):
    """Handshake era only: a 2026-07-28 server waits on nothing, the client drives the retry."""
    monkeypatch.setattr(mcp_server, "ELICIT_WAIT", 0.2)  # _slow times out
    (out,), asked = drive(root, reply)
    assert len(asked) == 1 and out["changed"] is True
    (c,) = stamps(root)
    assert (c["tier"], c["confirm"]) == ("confirmed", "relayed")


@pytest.mark.parametrize("mode", MODES)
def test_no_capability_as_before_and_stamp_is_server_side(root, mode):
    """No dialog without the capability. `confirm` is server-stamped and `click` is never tool
    input: a call sending them is refused with invalid_argument (A7; the SDK used to drop them)
    and records nothing, while the valid calls get the server's stamp (`relayed`, or none for
    a guess)."""
    forged = {**PREF, "text": "- Forged", "confirm": "clicked", "click": {"yes": True}}
    unasked = {**PREF, "text": "- Unasked", "user_confirmed": False}
    out, asked = drive(root, NO_CAPABILITY, PREF, forged, unasked, mode=mode)
    assert asked == [] and out[0]["changed"] is True and out[2]["changed"] is True
    assert out[1]["error"] == "invalid_argument"
    assert "'click', 'confirm'" in out[1]["message"] and "user_confirmed" in out[1]["message"]
    assert "Forged" not in (root / "me" / "preferences.md").read_text(encoding="utf-8")
    assert not [e for e in events(root) if "Forged" in str(e)]
    confirms = [c.get("confirm") for c in stamps(root) if c["tool"] == "patch"]
    assert confirms == ["relayed", None]  # the guess: no confirm
    assert len(live(root)) == 2 and live(root)[0] == ("confirmed", "relayed")
    assert live(root)[1][0] == "agent"
    assert know.report(Vault(root), events(root), "2026-11-16")["total"]["clicked"] == 0


def test_click_is_not_tool_input(root):
    tools = build_server(Vault(root))._tool_manager.list_tools()
    patch = next(t for t in tools if t.name == "keepfold_patch")
    assert "user_confirmed" in patch.parameters["properties"]
    assert not {"click", "ctx"} & set(patch.parameters["properties"])


def test_unconfirmed_patch_never_asks(root):
    (out,), asked = drive(root, YES, {**PREF, "user_confirmed": False})
    assert asked == [] and out["changed"] is True and "queued" not in out


def test_confirm_missing_on_old_events_reads_relayed(root):
    (root / "me" / "preferences.md").write_bytes(b"# Preferences\n\n## Coding\n- Old ^c-aaaaa\n")
    old = {"kind": "change", "day": "2026-11-01", "payload": {"change": {
        "tool": "patch", "id": "preferences", "kind": "me", "claim": "c-aaaaa",
        "tier": "confirmed"}}}
    rows = claims.tiers(Vault(root), [old])
    assert sorted((r["page"], r["tier"], r["confirm"]) for r in rows) == [
        ("preferences", "confirmed", "relayed"), ("profile", "legacy", None)]
