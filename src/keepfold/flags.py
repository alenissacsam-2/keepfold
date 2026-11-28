"""Config switches for Wave B work. Each reads `taint.config().get("<top>")` with a literal key
(so doctor's unknown-key scan finds it); a missing, odd-typed or unreadable config gives the
default."""

from keepfold import taint


def _get(section, key: str):
    return section.get(key) if isinstance(section, dict) else None


def night() -> bool:
    """`app.night`: the night pass, off unless set to true."""
    return _get(taint.config().get("app"), "night") is True


def warm_hook() -> bool:
    """`app.warm_hook`: the warm hook, off unless set to true."""
    return _get(taint.config().get("app"), "warm_hook") is True


def prime_rank() -> str:
    """`prime.rank`: how prime ranks its hits ("words" by default)."""
    v = _get(taint.config().get("prime"), "rank")
    return v if isinstance(v, str) and v else "words"


def mcp_tools() -> str:
    """`mcp.tools`: which MCP tools are offered ("all" by default)."""
    v = _get(taint.config().get("mcp"), "tools")
    return v if isinstance(v, str) and v else "all"
