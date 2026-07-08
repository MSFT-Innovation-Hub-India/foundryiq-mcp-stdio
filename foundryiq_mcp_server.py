#!/usr/bin/env python3
"""
FoundryIQ stdio MCP server.

Exposes the FoundryIQ (Azure AI Search) knowledge base as a NATIVE MCP tool over
stdio, so an MCP client (e.g. the GitHub Copilot CLI) can call it directly instead
of shelling out to the one-shot `foundryiq_query.py` script per question.

Why a stdio server instead of registering the remote HTTP endpoint directly:
the FoundryIQ endpoint needs a Bearer token scoped to `search.azure.com`, minted
for a specific tenant, that expires in ~1 hour. A static HTTP registration would
rot. This server mints a FRESH token on every tool call via the user's existing
Azure CLI login (`az account get-access-token`), so nothing is hardcoded and the
token never goes stale.

Transport: stdio (local child process). stdout is reserved for the MCP JSON-RPC
protocol; all logging goes to stderr.

Config: resolved exactly like foundryiq_query.py — priority order:
    1. --config <path>            (CLI flag passed in the server registration args)
    2. FOUNDRYIQ_CONFIG env var   (explicit path to a config file)
    3. config.json in the current working directory (the cwd the server is spawned in)
Individual FOUNDRYIQ_* env vars override individual fields.

Exposed tool:
    knowledge_base_retrieve(queries: list[str]) -> str
        Runs the FoundryIQ retrieval pipeline and returns the retrieved grounding
        chunks rendered as Markdown ("### Reference N" blocks with ref_id + content).
        The caller synthesizes the final answer from these chunks.

Usage (standalone sanity check — normally launched by the MCP client):
    python foundryiq_mcp_server.py --config <path-to-workspace-config.json>
"""
import logging
import os
import sys

# CRITICAL for stdio transport: stdout is the MCP JSON-RPC channel. Any stray text
# on stdout corrupts the protocol and hangs the client. httpx / mcp / httpcore emit
# INFO logs ("HTTP Request:", "Negotiated protocol version:") that must NOT land on
# stdout — pin all logging to stderr and quiet those chatty loggers.
logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)
for _noisy in ("httpx", "httpcore", "mcp", "mcp.client", "mcp.client.streamable_http"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Make the sibling query module importable regardless of the spawn cwd, and reuse
# its proven config-loading / token-minting / result-flattening / formatting logic.
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from foundryiq_query import load_config, get_token, _collect_items, format_items  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402


def _log(*args):
    """Log to stderr only — stdout is sacred (MCP JSON-RPC channel)."""
    print("[foundryiq-mcp]", *args, file=sys.stderr, flush=True)


def _parse_config_flag(argv):
    """Pull an optional --config/-c <path> out of argv; returns the path or None."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--config", "-c") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--config="):
            return a.split("=", 1)[1]
        i += 1
    return None


_CONFIG_PATH_FLAG = _parse_config_flag(sys.argv[1:])
_CONFIG_CACHE = None


def _get_config():
    """Lazily resolve + cache the workspace config so the server can still start
    (and return a clean tool error) even if config is momentarily missing."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        _CONFIG_CACHE = load_config(_CONFIG_PATH_FLAG)
        _log(f"config loaded from {_CONFIG_CACHE.get('config_path')} -> "
             f"kb={_CONFIG_CACHE.get('kb_name')} endpoint={_CONFIG_CACHE.get('endpoint')}")
    return _CONFIG_CACHE


mcp = FastMCP("foundryiq")


@mcp.tool()
async def knowledge_base_retrieve(queries: list[str]) -> str:
    """Retrieve grounding chunks from the FoundryIQ (Azure AI Search) knowledge base.

    Runs the knowledge base's agentic retrieval pipeline (query planning ->
    parallel keyword/vector/hybrid search -> semantic rerank) and returns the
    retrieved SOURCE CHUNKS as Markdown "### Reference N" blocks (ref_id + content).

    This returns chunks ONLY — it does NOT synthesize an answer. Read the returned
    chunks and compose the answer yourself, using only their content as source
    material, and keep the [ref_id:N] markers and any named source documents next to
    the facts they support.

    Args:
        queries: One or more natural-language questions to retrieve grounding for.
                 Pass the user's request as-is; the pipeline does its own query
                 planning. To get source document names back, include an instruction
                 like "Also cite the source document name for each point." in the
                 query text (the retrieval tool has no separate citation parameter).
    """
    if isinstance(queries, str):
        queries = [queries]
    if not queries:
        raise ValueError("Provide at least one query string.")

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    config = _get_config()

    # Mint a FRESH token on every call — this is what defeats the ~1h token rot.
    try:
        token = get_token(
            config["resource"], config.get("tenant_id"), config.get("subscription_id")
        )
    except SystemExit as e:  # get_token raises SystemExit on az failure; don't kill the server
        raise RuntimeError(str(e))

    headers = {"Authorization": f"Bearer {token}"}
    _log(f"retrieving {len(queries)} query(ies) against {config['mcp_url']}")
    async with streamablehttp_client(config["mcp_url"], headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "knowledge_base_retrieve", {"queries": queries}
            )
            if getattr(result, "isError", False):
                _log("upstream knowledge_base_retrieve reported isError=True")
            items = _collect_items(result)

    _log(f"returned {len(items)} chunk(s)")
    return format_items(items, "markdown")


if __name__ == "__main__":
    _log("starting stdio MCP server (config flag: "
         f"{_CONFIG_PATH_FLAG or 'none -> env/cwd'})")
    mcp.run()  # stdio transport by default
