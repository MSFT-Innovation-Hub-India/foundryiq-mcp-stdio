# FoundryIQ stdio MCP Server

Exposes the FoundryIQ (Azure AI Search) RFP knowledge base as a native MCP tool
(`knowledge_base_retrieve`) over stdio, so an MCP client (VS Code, GitHub Copilot
CLI, etc.) can call it directly instead of shelling out to a script per question.

A fresh Azure AD access token (scoped to `search.azure.com`) is minted on every
tool call via the Azure CLI (`az account get-access-token`) — nothing is
hardcoded and the token never goes stale.

## Prerequisites

- Python 3.10+ (this project was verified with Python 3.13)
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) installed and logged in:
  ```powershell
  az login
  ```
  You must have access to the tenant/subscription that hosts the FoundryIQ
  search service referenced in `config.json` (the server requests a token for
  that specific tenant, so it works even if it differs from your CLI's active
  tenant/subscription).

## Setup

1. Create and activate a virtual environment (already present as `.venv/` in
   this repo — recreate it if missing):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
2. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
3. Copy `config.example.json` to `config.json` and fill in your knowledge base
   settings:
   ```powershell
   Copy-Item config.example.json config.json
   ```
   ```json
   {
     "endpoint": "https://<your-search-service>.search.windows.net",
     "kb_name": "<your-knowledge-base-name>",
     "api_version": "2026-05-01-preview",
     "token_resource": "https://search.azure.com",
     "tenant_id": "<tenant-guid>",
     "subscription_id": "<subscription-guid>"
   }
   ```
   `tenant_id` / `subscription_id` are optional but recommended when the
   knowledge base lives in a different tenant/subscription than your CLI's
   default login context. `config.json` is gitignored — it holds your real
   values and should never be committed.

## Running standalone (sanity check)

The server speaks MCP JSON-RPC over stdio, so running it directly will just
block waiting for input on stdin — that's expected:

```powershell
.\.venv\Scripts\python.exe foundryiq_mcp_server.py --config config.json
```

Press `Ctrl+C` to stop. This is only useful to confirm the process starts
without import/config errors; normally an MCP client launches and drives it.

## Registering with an MCP client

### VS Code (`mcp.json`)

Add an entry to your workspace or user `mcp.json` (Command Palette →
"MCP: Open User Configuration", or a `.vscode/mcp.json` in this repo):

```json
{
  "servers": {
    "foundryiq": {
      "type": "stdio",
      "command": "c:\\Users\\sansri\\stdio-mcpservers\\foundryiq-rfp-kb\\.venv\\Scripts\\python.exe",
      "args": [
        "c:\\Users\\sansri\\stdio-mcpservers\\foundryiq-rfp-kb\\foundryiq_mcp_server.py",
        "--config",
        "c:\\Users\\sansri\\stdio-mcpservers\\foundryiq-rfp-kb\\config.json"
      ]
    }
  }
}
```

### GitHub Copilot CLI (and Microsoft Scout, which rides on it)

The Copilot CLI reads its MCP server registrations from
`~/.copilot/mcp-config.json` (i.e. `C:\Users\<you>\.copilot\mcp-config.json`
on Windows). Add/update the `foundryiq` entry there:

```json
{
  "mcpServers": {
    "foundryiq": {
      "type": "local",
      "command": "C:\\Users\\sansri\\stdio-mcpservers\\foundryiq-rfp-kb\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\sansri\\stdio-mcpservers\\foundryiq-rfp-kb\\foundryiq_mcp_server.py",
        "--config",
        "C:\\Users\\sansri\\stdio-mcpservers\\foundryiq-rfp-kb\\config.json"
      ],
      "tools": ["knowledge_base_retrieve"]
    }
  }
}
```

Note the schema differs slightly from VS Code's `mcp.json`: Copilot CLI uses
`"mcpServers"` (not `"servers"`) and `"type": "local"` (not `"stdio"`), but
the underlying mechanism is identical — a local subprocess over stdin/stdout,
no URL/port involved. Microsoft Scout calls into the same Copilot CLI MCP
config, so registering it here also makes `knowledge_base_retrieve` callable
from Scout. Restart Scout / start a new Copilot CLI session after editing
this file for the change to take effect.

### Other MCP clients

Point the client's server registration at the same venv Python + script +
`--config` path shown above. Use absolute paths so the server resolves
correctly regardless of the client's working directory.

## Microsoft Scout skill (`SKILL.md`)

This repo also includes [SKILL.md](SKILL.md) — a Scout skill that teaches the
agent *how* to use the registered `foundryiq-rfp-kb-knowledge_base_retrieve`
MCP tool correctly (call it directly instead of shelling out, treat the
returned chunks as the only source of truth, always append a citation-naming
instruction to the query, never decompose the user's request into
sub-questions, etc.).

**Registering the MCP server (steps above) is not enough on its own** — Scout
needs this skill imported separately so the agent knows these usage rules
exist and when to invoke the tool. Two things have to both be true for Scout
to use this correctly:

1. The `foundryiq` MCP server is registered in `~/.copilot/mcp-config.json`
   (see the GitHub Copilot CLI section above) — this is what makes the
   `foundryiq-rfp-kb-knowledge_base_retrieve` tool exist at all.
2. **[SKILL.md](SKILL.md) is imported into Scout's Skills/Extensions.** In
   Scout, add this skill via its extensions/skills UI (import from this repo
   path) so the skill's `name`/`description` frontmatter is indexed and Scout
   knows to route FoundryIQ/knowledge-base questions through this tool with
   the correct calling convention.

After importing, restart Scout (or start a new session) so it re-discovers
both the MCP tool and the skill.

## Config resolution order

`--config` flag → `FOUNDRYIQ_CONFIG` env var → `config.json` in the process's
current working directory. Individual `FOUNDRYIQ_*` env vars
(`FOUNDRYIQ_ENDPOINT`, `FOUNDRYIQ_KB_NAME`, `FOUNDRYIQ_API_VERSION`,
`FOUNDRYIQ_TOKEN_RESOURCE`, `FOUNDRYIQ_TENANT_ID`, `FOUNDRYIQ_SUBSCRIPTION_ID`)
override individual fields on top of whatever config file was loaded.

## Exposed tool

- **`knowledge_base_retrieve(queries: list[str]) -> str`** — runs the
  knowledge base's agentic retrieval pipeline and returns the retrieved
  grounding chunks as Markdown (`### Reference N` blocks with `ref_id` +
  content). It returns source chunks only; the caller/model synthesizes the
  final answer from them.

## Troubleshooting

- **`Failed to acquire Azure Search token. Run 'az login' first.`** — your CLI
  session expired or doesn't have access to the tenant/subscription in
  `config.json`. Re-run `az login` (and `az login --tenant <tenant_id>` if
  needed).
- **No config found** — ensure `config.json` exists next to the script, or
  pass `--config <path>` / set `FOUNDRYIQ_CONFIG`.
- **Stray output breaking the client** — all logging must go to stderr (this
  is already handled in `foundryiq_mcp_server.py`); don't add `print()` calls
  that write to stdout.
