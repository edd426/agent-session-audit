# Claude Code log fields the audit reads

## Where it looks

`claude_cache_audit.py` reads `<CLAUDE_CONFIG_DIR>/projects/**/*.jsonl`,
defaulting to `~/.claude`. Files are streamed line by line. It copies no prompt
text, code, paths, repository names, hostnames or user identifiers into its
reports.

`stats-cache.json` is ignored. The same response can be written to the JSONL
more than once, and client totals can inherit that duplication.

## Assistant records

| field | use |
|---|---|
| `type` | must be `assistant` |
| `timestamp` | response time; ISO-8601 or Unix seconds/milliseconds |
| `sessionId` | groups a conversation |
| `isSidechain`, `agentId`, `attributionAgent` | separate the main conversation from subagents |
| `version` | a CLI version change can invalidate the cache |
| `cwd`, `gitBranch` | hashed at once; used only to detect changes |
| `requestId`, `uuid` | fallback de-duplication keys |
| `message.id` | preferred response id |
| `message.model` | pricing and model-change detection |
| `message.usage` | the token counts |

| usage field | meaning |
|---|---|
| `input_tokens` | uncached input |
| `output_tokens` | generated output |
| `cache_read_input_tokens` | read from the prompt cache |
| `cache_creation_input_tokens` | total cache writes, when not split by lifetime |
| `cache_creation.ephemeral_5m_input_tokens` | 5-minute cache writes |
| `cache_creation.ephemeral_1h_input_tokens` | 1-hour cache writes |

Reusable context is `cacheable = cache_read + cache_creation`. The context two
responses could share is estimated as the smaller of the two. The share of
context still reusable on return is then applied as a what-if setting, because
similar size does not prove an exact byte match.

## De-duplication

The key is `(sessionId, message.id)`, falling back to `requestId`, then `uuid`,
then a timestamp-and-usage fingerprint. When duplicates disagree, the record
with the largest usage wins and the earliest timestamp is kept. The report
says how many rows were removed. Never count raw rows or trust client totals.

## Human prompts

A user record counts as a likely human prompt when its content is a non-empty
string or has blocks other than `tool_result`. Metadata, compaction summaries,
tool results, `sdk`/`system` prompts and subagent rows (`isSidechain` or
`agentId`) are excluded. Only the timestamp and an
anonymous session and project grouping are kept.

Pauses between human prompts describe working rhythm. Cache costs use the
pause between responses instead: the agent can make several tool calls between
two human messages, and each refreshes the cache.

## Conversations, subagents and context changes

The main conversation and each identifiable subagent are analysed separately,
in timestamp order. A new context epoch starts when the model, CLI version,
project or branch changes. It also starts when cacheable context drops below
70 % of its previous size after exceeding 2,000 tokens.

These are visible proxies. Tool schemas and MCP definitions are part of the
prefix but are not reliably recorded. Compaction detection is heuristic.
Server-side cache keys, routing and isolation are not visible locally.

## Known limitations

- The local record is not an invoice and may not match enterprise billing.
- `--resume` restores the conversation but does not show whether the server
  still holds a matching prefix.
- Context shared across sessions or accounts is not modelled.
- Parallel branches without a stable `agentId` can be grouped imperfectly.
- Older cache writes without a lifetime split are priced by
  `--unknown-write-ttl` and reported separately.
- A date-only `--since` or `--until` means midnight UTC; `--until` is
  exclusive.

If a new Claude Code version changes fields, inspect field names and types
without printing message content. Update the parser and its synthetic tests
together.
