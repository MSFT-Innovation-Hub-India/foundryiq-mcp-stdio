#!/usr/bin/env python3
"""
FoundryIQ (Azure AI Search knowledge base) query runner.

Calls the knowledge base's remote streamable-HTTP MCP endpoint and invokes the
`knowledge_base_retrieve` tool. Auth uses the user's existing Azure CLI login
(`az account get-access-token`), so a fresh token is minted on every call and
never hardcoded.

Connection settings are NOT hardcoded here and are NOT read from the skill folder.
They are per-workspace: the config file is resolved (in priority order) from:
    1. --config <path>            (explicit CLI flag)
    2. FOUNDRYIQ_CONFIG env var   (explicit path to a config file)
    3. config.json in the current working directory (the workspace folder)
Individual FOUNDRYIQ_* env vars (FOUNDRYIQ_ENDPOINT / FOUNDRYIQ_KB_NAME /
FOUNDRYIQ_API_VERSION / FOUNDRYIQ_TOKEN_RESOURCE / FOUNDRYIQ_TENANT_ID /
FOUNDRYIQ_SUBSCRIPTION_ID) still override individual fields.
If no config file is found in the workspace AND the required fields are not supplied
via env vars, the script errors out (it does NOT fall back to any skill-folder default).

Usage:
    python foundryiq_query.py "your natural-language question"
    python foundryiq_query.py "question one" "question two"
    python foundryiq_query.py --config /path/to/config.json "your question"
    python foundryiq_query.py --format json "your question"        # machine JSON
    python foundryiq_query.py --out results.md "your question"     # safe file write

Output:
    Results are rendered as readable Markdown by default (real newlines — safe to
    capture through a shell). Use --format json for the raw structured array, or
    --format pretty for plain-text separators. Use --out <path> to write the output
    directly to a UTF-8 file from Python, which avoids terminal line-wrapping that
    can corrupt escaped JSON when the payload is large. Prefer `--format json --out
    results.json` (then read the file) whenever you need to parse results
    programmatically — never scrape raw JSON from stdout.
"""
import asyncio
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_config_path(explicit=None):
    """Locate the per-workspace config.json. Never falls back to the skill folder."""
    searched = []

    if explicit:
        p = os.path.abspath(explicit)
        if not os.path.exists(p):
            raise SystemExit(f"--config path not found: {p}")
        return p, searched

    env_path = os.environ.get("FOUNDRYIQ_CONFIG")
    if env_path:
        p = os.path.abspath(env_path)
        if not os.path.exists(p):
            raise SystemExit(f"FOUNDRYIQ_CONFIG path not found: {p}")
        return p, searched

    cwd_cfg = os.path.join(os.getcwd(), "config.json")
    searched.append(cwd_cfg)
    if os.path.exists(cwd_cfg):
        return cwd_cfg, searched

    return None, searched


def load_config(explicit=None):
    """Read the resolved per-workspace config.json + apply env-var field overrides."""
    config_path, searched = resolve_config_path(explicit)

    cfg = {}
    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise SystemExit(f"Failed to read config file {config_path}: {e}")

    endpoint = os.environ.get("FOUNDRYIQ_ENDPOINT") or cfg.get("endpoint")
    kb_name = os.environ.get("FOUNDRYIQ_KB_NAME") or cfg.get("kb_name")
    api_version = os.environ.get("FOUNDRYIQ_API_VERSION") or cfg.get("api_version") or "2026-05-01-preview"
    resource = os.environ.get("FOUNDRYIQ_TOKEN_RESOURCE") or cfg.get("token_resource") or "https://search.azure.com"
    tenant_id = os.environ.get("FOUNDRYIQ_TENANT_ID") or cfg.get("tenant_id")
    subscription_id = os.environ.get("FOUNDRYIQ_SUBSCRIPTION_ID") or cfg.get("subscription_id")

    missing = [name for name, val in (("endpoint", endpoint), ("kb_name", kb_name)) if not val]
    if missing:
        if config_path is None:
            where = "\n".join(f"    - {p}" for p in searched) or "    - (none)"
            raise SystemExit(
                "No FoundryIQ config.json found for this workspace.\n"
                "Looked for a config file at:\n" + where + "\n"
                "Add a config.json to the workspace folder (copy config.example.json "
                "from the skill folder and fill in your endpoint + kb_name), "
                "pass --config <path>, set FOUNDRYIQ_CONFIG, or set the FOUNDRYIQ_* env vars."
            )
        raise SystemExit(
            "Missing required FoundryIQ setting(s): " + ", ".join(missing) + ".\n"
            f"Fill them in {config_path} or set the matching FOUNDRYIQ_* env vars."
        )

    endpoint = endpoint.rstrip("/")
    return {
        "endpoint": endpoint,
        "kb_name": kb_name,
        "api_version": api_version,
        "resource": resource.rstrip("/"),
        "tenant_id": tenant_id,
        "subscription_id": subscription_id,
        "mcp_url": f"{endpoint}/knowledgebases/{kb_name}/mcp?api-version={api_version}",
        "config_path": config_path,
    }


def get_token(token_resource, tenant_id=None, subscription_id=None) -> str:
    """Mint an Azure AD access token via the Azure CLI.

    The knowledge base may live in a DIFFERENT tenant/subscription than the CLI's
    active context. When `tenant_id` is set we acquire the token for that tenant so
    it validates against the target search service (this fixes cross-tenant
    401 "invalid_token" errors). `tenant_id` and `subscription_id` are mutually
    exclusive for `az account get-access-token`, so tenant takes precedence.
    """
    az = "az"
    if os.name == "nt":
        az = "az.cmd"

    scope_args = ["--resource", token_resource]
    if tenant_id:
        scope_args += ["--tenant", tenant_id]
    elif subscription_id:
        scope_args += ["--subscription", subscription_id]

    try:
        out = subprocess.run(
            [az, "account", "get-access-token", *scope_args,
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        # fall back to plain 'az' resolved via shell
        shell_scope = " ".join(scope_args)
        out = subprocess.run(
            f"az account get-access-token {shell_scope} --query accessToken -o tsv",
            capture_output=True, text=True, check=True, shell=True, stdin=subprocess.DEVNULL,
        )
    token = out.stdout.strip()
    if not token:
        raise SystemExit("Failed to acquire Azure Search token. Run `az login` first. "
                         f"stderr: {out.stderr.strip()}")
    return token


async def run(queries, config):
    """Call knowledge_base_retrieve and return a normalized list of result items.

    Each item is a dict. Structured retrieval results (a JSON array of
    {ref_id, content, ...}) are flattened into individual items; non-JSON payloads
    become {"content": <text>}. Formatting/printing is handled by the caller so the
    output can be written safely to a file instead of streamed through the terminal.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {get_token(config['resource'], config.get('tenant_id'), config.get('subscription_id'))}"}
    async with streamablehttp_client(config["mcp_url"], headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("knowledge_base_retrieve", {"queries": queries})
            if getattr(result, "isError", False):
                print("ERROR from knowledge_base_retrieve:", file=sys.stderr)
            return _collect_items(result)


def _collect_items(result):
    """Flatten the tool result into a list of dict items."""
    items = []
    for c in result.content:
        text = getattr(c, "text", None)
        if text is None:
            items.append({"content": json.dumps(c, default=str)})
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            items.append({"content": text})
            continue
        if isinstance(parsed, list):
            for el in parsed:
                items.append(el if isinstance(el, dict) else {"content": el})
        elif isinstance(parsed, dict):
            items.append(parsed)
        else:
            items.append({"content": parsed})
    return items


def _normalize(text):
    """Tidy passage whitespace: drop trailing spaces and collapse blank-line runs."""
    if not isinstance(text, str):
        return str(text)
    out, blank = [], 0
    for ln in text.replace("\r\n", "\n").split("\n"):
        ln = ln.rstrip()
        if ln == "":
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip()


def format_items(items, fmt):
    """Render items as machine JSON, readable markdown, or plain text."""
    if fmt == "json":
        return json.dumps(items, indent=2, ensure_ascii=False)

    blocks = []
    for i, it in enumerate(items):
        it = it if isinstance(it, dict) else {"content": it}
        ref = it.get("ref_id", i)
        content = _normalize(it.get("content", ""))
        meta = {k: v for k, v in it.items() if k not in ("ref_id", "content")}
        if fmt == "markdown":
            metaline = ("".join(f"- **{k}**: {v}\n" for k, v in meta.items()) + "\n") if meta else ""
            blocks.append(f"### Reference {ref}\n\n{metaline}{content}\n")
        else:  # pretty / text
            sep = "=" * 60
            metaline = "".join(f"{k}: {v}\n" for k, v in meta.items())
            blocks.append(f"{sep}\nReference {ref}\n{sep}\n{metaline}{content}\n")
    return "\n".join(blocks)


def parse_args(argv):
    """Extract --config/--out/--format flags; everything else is a query string.

    -o/--out <path>  write results to a UTF-8 file (avoids terminal mangling of JSON)
    -f/--format      json | markdown | pretty   (default: markdown)
    """
    config_path = None
    out_path = None
    fmt = "markdown"
    queries = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--config", "-c"):
            if i + 1 >= len(argv):
                raise SystemExit("--config requires a path argument.")
            config_path = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
            i += 1
            continue
        if arg in ("--out", "-o"):
            if i + 1 >= len(argv):
                raise SystemExit("--out requires a path argument.")
            out_path = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--out="):
            out_path = arg.split("=", 1)[1]
            i += 1
            continue
        if arg in ("--format", "-f"):
            if i + 1 >= len(argv):
                raise SystemExit("--format requires a value (json|markdown|pretty).")
            fmt = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--format="):
            fmt = arg.split("=", 1)[1]
            i += 1
            continue
        if arg.strip():
            queries.append(arg)
        i += 1
    if fmt not in ("json", "markdown", "pretty"):
        raise SystemExit(f"Unknown --format '{fmt}'. Use json, markdown, or pretty.")
    return config_path, out_path, fmt, queries


def main():
    config_path, out_path, fmt, queries = parse_args(sys.argv[1:])
    if not queries:
        print(__doc__)
        raise SystemExit("Provide at least one query string.")
    if len(queries) == 1 and len(queries[0]) > 400:
        print("WARNING: query exceeds 400 chars; the tool may truncate it.", file=sys.stderr)
    config = load_config(config_path)
    items = asyncio.run(run(queries, config))
    output = format_items(items, fmt)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Wrote {len(items)} result item(s) to {out_path} (format={fmt}).")
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(output)


if __name__ == "__main__":
    main()
