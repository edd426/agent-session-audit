# The session ledger — `sessions.jsonl`

One row per session. Every analysis in this skill adds its own columns to the
same file. An empty column means that analysis has not been run, which is
different from a column of zeros.

```bash
# build it, with the startup columns
orientation_profile.py report --output-dir OUT --ledger OUT/sessions.jsonl

# each further analysis adds its columns to the same file
claude_cache_audit.py --root ~/.claude --output-dir OUT --ledger OUT/sessions.jsonl
tool_bloat.py --since 2026-09-01 --ledger OUT/sessions.jsonl

# what is in it, and which script wrote each column
session_ledger.py show    --ledger OUT/sessions.jsonl
session_ledger.py columns --ledger OUT/sessions.jsonl   # JSON, with provenance
session_ledger.py schema                                # every registered column
```

## The page it renders on

`ledger.html`: every header sorts; ten columns show by default
(`LEDGER_DEFAULT_COLUMNS`) with a *show all columns* toggle; each header's
tooltip names the script that wrote it. The complete row is always in
`sessions.jsonl`. Layout rules are in `visual-report-spec.md`.

## The contract

- **An update never drops a column it did not write.** `session_ledger.upsert()`
  reads the row, changes only the caller's declared keys, and writes it back.
  Writes go through a temp file and `os.replace`. Several scripts may share one
  ledger in a pipeline; a half-written line would corrupt every column.
- **Provenance is kept per column** in `sessions.jsonl.columns.json`: which
  script wrote it, when, and a one-line definition. A column whose script has
  not run since a schema change is visible, not silently trusted.
- **Uncalibrated columns get a prefix.** A script that calls its own numbers
  uncalibrated writes them as `wip_…`.
- **Registering is not writing.** `session_ledger.register(path, producer)`
  claims column names without values. A real write always wins over a
  registration.

## Columns

Key: `session_id`. Sessions from every store share one namespace. Under
`--redact`, the id, folder and work-item name are hashed and the title dropped.
The report shows plain names for the `orientation_*`, `thread`, `stream` and
`carry_*` columns; the mapping is in
[orientation-and-threads.md](orientation-and-threads.md).

### `orientation_profile.py` — the session and its startup

| column | type | meaning |
|---|---|---|
| `source` | str | the store: `claude-code`, `vscode-copilot`, `copilot-cli` |
| `start` / `end` | ISO-8601 | first and last turn, UTC |
| `cwd`, `cwd_basename` | str | working directory, and the folder name alone |
| `title` | str | session title, else the first prompt line |
| `models` | list | models used. `<synthetic>` is left out: Claude Code stamps it on records it injects, and nobody chose it. |
| `turns` | int | assistant responses; one API response is one turn however many rows carry it |
| `main_turns` | int | responses on the main conversation only |
| `total_tokens` | int | input + output + cache read + cache write |
| `orientation_tokens` | int / null | startup: tokens before the first edit. Lower is better. |
| `orientation_share` | float / null | startup share: `orientation_tokens / total_tokens`. `null` for a no-edit session. Lower is better once the session edits. |
| `orientation_cost_usd` | float / null | startup cost: the pre-edit responses, each priced at its own model's rates. Never session cost × share. |
| `orientation_position` | str | which tenth of the distribution the share sits in (`0-10 %` … `90-100 %`) or `no edit`. A position, not a verdict: no threshold has been validated against an outcome. |
| `orientation_breadth` | int / null | files read first: distinct files touched before the first edit. Low means re-reading; high means discovery. |
| `boundary_turn_tokens` | int / null | size of the response that made the first edit; not counted as startup |
| `first_mutation_attempt_turn` | int / null | first file-changing call of any kind, including failed ones |
| `failed_mutation_attempts` | int | file-changing calls that came back an error or refusal; they do not end startup |
| `handoff_opened_by_turn_2` | bool | read a handoff note in the first two turns, or the file the previous session in the sitting wrote last. README does not count. |
| `readme_opened_by_turn_2` | bool | read a README in the first two turns; kept apart because reading a README is startup work |
| `handoff_searched_by_turn_2` | bool | searched (Glob/Grep/LS) for a note-like name in the first two responses. Searching is not reading. |
| `handoff_search_target` | str / null | what it searched for |
| `question_first` | bool / null | started with a question, and the first edit followed a later human turn. Repeat-startup figures are shown with and without these. |
| `strategy` | str | `scripted`, `mixed`, `by-hand` or `read-only`, from tool counts |
| `effort` | str / null | reasoning-effort level recorded on the assistant records, read from `effort` (newer CLI) or `perTurnEffort` (some older versions); `null` when neither is present |
| `thinking_share` | float / null | reasoning ÷ output tokens; a cross-check on effort, not its source |
| `outcome` | str | `success`, `failure` or `unknown`, inferred locally |
| `est_cost_usd` | float / null | estimated cost at list API rates; for comparing sessions, never an invoice. `null`, shown as "—", when the store has no token counts; left out of totals and medians |
| `child_agents`, `child_turns`, `child_tokens`, `child_cost_usd` | int / float | subagents under this session id: count, responses, tokens and list-rate cost. Real spend, kept out of the main conversation's timeline. |
| `thread_id` | str | the sitting |
| `stream_id`, `stream_name`, `stream_named_by` | str | the work item, its name, and the rule that grouped it (`folder`, `branch` or `ticket`) |

`orientation_tokens`, `orientation_share` and `orientation_breadth` are `null`,
not 0 and not the whole session, when there is no first edit. Anything that
sums them must skip nulls and print the no-edit group beside the sum.

### `orientation_profile.py` — picking work back up

| column | type | meaning |
|---|---|---|
| `continuation_kind` | str | how work continued into or within this session: `R0` resumed after a pause · `R1` resumed after compaction · `R2` new session on the same work item · `parallel` ran in parallel (started before an earlier session of the work item had ended; not a pick-up) · `first` first session. The report shows only the words. A description, not a score. |
| `carry_tokens` | int / null | pick-up cost in tokens: from the restart up to, not including, the next first edit. For a new session, its `orientation_tokens`. Compare only within a kind. |
| `carry_cost_usd` | float / null | the same responses priced at their own model's rates |
| `gap_hours_before` | float / null | hours since the previous response (same session), or since the latest end among the work item's earlier sessions (new session); never negative |
| `compactions` | int | times this session was compacted, from the CLI's `compact_boundary` rows |

A session can be more than one kind; the column names one. Precedence is fixed:
`R2` > `R1` > `R0` > `parallel` > `first`. A new session that also resumed inside itself
reads `R2`, because that is how the work arrived. `carry_*` cost the named
event only. Every event, with its own gap, cost and model, is in
`orientation.json` under `continuations.events`; the report counts those.
Parallel sessions are counted in `continuations.parallel_sessions` and get no
new-session event. Do
not sum `carry_tokens` down the ledger and call it the total pick-up bill.

### `claude_cache_audit.py`

| column | type | meaning |
|---|---|---|
| `cache_hit_ratio` | float | cache reads ÷ (cache reads + fresh input). Misleading alone: re-reading a huge context forty times scores better than a cheap session. Read it beside cost. |
| `cache_cold_events` | int | pauses between responses over 5 minutes, inside one conversation or subagent, with no context change. A cache miss is not observed directly; a 1-hour cache would have covered many. Lower is better. |
| `cache_ttl_band` | str | `5m` (never went cold), `1h` (a 1-hour cache would have covered its pauses), `beyond-1h` or `mixed` |

### `tool_bloat.py`

| column | type | meaning |
|---|---|---|
| `tool_result_bytes` | int | tokens of tool results carried in this session's context. Lower is better. |
| `top_tool` | str | the tool whose results carry the most |
| `top_tool_bytes` | int | that tool's share |

### Registered, not yet written

`lane_profile.py` (`lane`, `fat_context_read_share`) and `compact_economics.py`
(`tokens_before_compact`, `tokens_after_compact`) are registered as stubs. They
have no `--ledger` option and write no columns; the home page lists them as
"prints its own report", with the command to run. `compactions` is written by
`orientation_profile.py`.

### How the home page knows what ran

`orientation.json` has a top-level `ledger_runs` key: `{script: "YYYY-MM-DD"}`
for each analysis whose columns hold values in the `--ledger` file. The home
page marks those analyses "✓ in the ledger · ran DATE".

## Adding a producer

1. Add your columns to `REGISTRY` in `scripts/session_ledger.py`, each with a
   one-line definition. Readers see it as the header tooltip.
2. Add a `--ledger PATH` flag to your script.
3. Build `[{"session_id": ..., "your_column": ...}, ...]` and call
   `session_ledger.upsert(path, rows, "your_script.py", ["your_column", ...])`.
   Pass `prefix="wip_"` if the numbers are not calibrated yet.
4. Add a row to the tables above and a line to the report's analysis index.
5. Run `session_ledger.py selftest`.

Join on the full session id. A display label truncated to 8 characters joins to
nothing.
