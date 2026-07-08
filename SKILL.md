---
name: "foundryiq-search"
description: "Query an Azure AI Search FoundryIQ knowledge base by calling the registered stdio MCP tool `foundryiq-rfp-kb-knowledge_base_retrieve` directly (do NOT run any python/shell command). Use when the user asks to search FoundryIQ, query the Contoso/RFP knowledge base, or do agentic retrieval over Azure AI Search."
---

# FoundryIQ Search (Azure AI Search knowledge base via MCP)

Query a FoundryIQ knowledge base hosted on Azure AI Search by calling the registered
stdio MCP server tool **`foundryiq-rfp-kb-knowledge_base_retrieve`** directly. This
runs the retrieval pipeline (query planning -> parallel keyword/vector/hybrid search
-> semantic rerank) and returns the retrieved **source chunks** as `{ ref_id, content }`
items.

## When to use
Use whenever the user wants to search / ask questions against the FoundryIQ
knowledge base, e.g. "search FoundryIQ for...", "ask the knowledge base...",
"what does the KB say about...", "do agentic retrieval on...".

## Call the registered MCP tool
Invoke `foundryiq-rfp-kb-knowledge_base_retrieve` directly with a `queries` argument.
The registered stdio MCP server handles everything server-side — token minting, config
resolution, and endpoint calls. You never run the python scripts, `az`, or any shell
command yourself. The tool takes a single input: `queries` (an array of one or more
natural-language question strings) and returns the chunks as Markdown `### Reference N`
blocks (`ref_id` + `content`).

## CRITICAL: FoundryIQ returns CHUNKS ONLY — YOU synthesize the answer
FoundryIQ does **NOT** return a synthesized, ready-to-present answer. It returns only
the retrieved grounding chunks (`ref_id` + `content`). Your job is to read those chunks
and synthesize the answer yourself, using **only** the returned chunks as source
material. Rules:

- The chunks are the ONLY source of truth. Synthesize the answer strictly from their
  `content` — do NOT answer from your own knowledge, training data, or other tools, and
  do NOT add facts, figures, or claims that are not present in the returned chunks.
- Do NOT decompose the request. Pass the user's request **verbatim and in its entirety**
  as a single query string — do not split it into sub-questions, rewrite it into
  keywords, or issue multiple separate calls for one request. (The retrieval pipeline
  does its own query planning across the chunks.) Any synthesis, comparison,
  table-building, or formatting the user asked for is done by YOU afterward, from the
  returned chunks.
- Synthesize IN MEMORY. Read the returned chunks directly and compose the answer in your
  reasoning — do NOT write throwaway parser/inspection scripts and do NOT persist the
  chunks to intermediate files just to re-read them.
- Preserve provenance: keep the `[ref_id:N]` markers and the named source documents
  next to the facts they support.
- ONLY when the returned chunks are empty, or clearly do not contain the requested
  information: state this plainly to the user, and OFFER to answer from your own
  knowledge instead. Do NOT actually answer from your own knowledge until the user
  accepts the offer.

## REQUIRED: append a citation-source-name instruction to every query
The `knowledge_base_retrieve` tool exposes only ONE input parameter (`queries`) with
`additionalProperties: false` — there is NO parameter to make it return citation names.
By default it emits bare `[ref_id:N]` markers with no source names, which are not useful
on their own.

Workaround (verified to work): the source document names CAN be pulled into the answer
text by asking for them in the query. This is the ONE permitted augmentation of the
user's request — it does NOT count as decomposing or changing the question:

- When building the query argument, take the user's request VERBATIM and append a
  citation-naming instruction to the end, e.g.:
  `"<user's request verbatim> Also cite the source document name for each point/fact."`
- Keep the combined string under 400 characters. If appending would exceed 400 chars,
  shorten the appended instruction (e.g. " Cite source document names.") rather than
  altering the user's original wording.
- This augmentation is always applied unless the user explicitly says they don't want
  source names.
- When presenting the answer, keep the named sources alongside their `[ref_id:N]`
  markers so the user sees which document each fact came from.

## How to run
Build the query string = the user's request VERBATIM + the appended citation-naming
instruction. Keep it as ONE complete natural-language question (max 400 chars) and pass
it as the single element of the `queries` array:

```
foundryiq-rfp-kb-knowledge_base_retrieve(
  queries: ["the user's full request verbatim. Also cite the source document name for each point."]
)
```

Pass multiple strings in the `queries` array ONLY when the USER genuinely asked several
distinct questions — never to decompose a single request yourself:

```
foundryiq-rfp-kb-knowledge_base_retrieve(
  queries: [
    "first question. Cite source document names.",
    "second question. Cite source document names."
  ]
)
```

Read the returned `### Reference N` chunks straight from the tool result and synthesize
the answer in memory — one tool call, read it, answer. Do NOT write scripts to slice the
chunks, do NOT persist them to files, and do NOT re-issue the same request just to
reshape the output.

## Error handling
- **401 / 403** — token/permission issue. Tell the user to re-run `az login` or confirm
  their account has data-plane access (Search Index Data Reader role) on the search
  service.
- **Tool not available at all** — the MCP server isn't registered/started. Tell the user
  to restart the client so it picks up the `foundryiq-rfp-kb` registration.
- **Empty results** — reword the question, or tell the user the KB has no matching
  content.
