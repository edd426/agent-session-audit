---
name: agent-session-audit
description: Audits local coding-agent history and reports where a session's tokens went and what to change. Answers where weekly usage went and which conversations or subagents drove it; how much of each session is spent getting started before its first edit, and whether writing a handoff note pays for itself; which multi-session or cross-tool pieces of work keep re-learning context they already paid for; what compaction cost; which tool results bloat context; which reasoning-effort level a session actually ran at; and whether a 5-minute or 1-hour prompt-cache lifetime fits the history. Produces a self-contained visual report and a per-session ledger. Use when the user asks where their tokens or usage went, why compaction cost so much, which tool is bloating their context, whether handoff or context notes are worth writing, whether a session is re-learning what it already knew, how to choose between the 1-hour and 5-minute cache TTL, or wants a usage audit of their own history - on macOS, Linux or Windows.
---

# Agent Session Audit

Run local scripts over real agent history, then explain the evidence to the
user. This is a decision aid, not an invoice: local logs show token flows and
timing, not every server-side cache change or enterprise price. The scripts are
offline and read-only. They read local history (Claude Code JSONL; VS Code
Copilot and Copilot CLI for the startup views) and write reports. They never
call a model.

## Quickstart — if you run one thing, run this

Works from any directory. It writes into the current directory, not a temporary
one, so the ledger it starts can be added to later.

```bash
SKILL="${CLAUDE_SKILL_DIR:-$HOME/.claude/skills/agent-session-audit}"  # the folder holding this SKILL.md
OUT="./agent-audit-$(date +%F)"
python3 "$SKILL/scripts/orientation_profile.py" report \
  --root "$HOME/.claude" --output-dir "$OUT" --ledger "$OUT/sessions.jsonl"
```

On Windows (PowerShell) use `python`, `$env:USERPROFILE\.claude` as the root and
an explicit output path; see
[references/orientation-and-threads.md](references/orientation-and-threads.md) §5.

Open `$OUT/index.html`. The report is a set of linked, self-contained pages:

| page (nav label) | shows |
|---|---|
| `index.html` (Home) | the answer as tiles and a chart, then a checklist of every analysis this skill can run |
| `orientation.html` (Startup) | startup cost and repeat startup, by session and work item |
| `carry-vs-restart.html` (Pick-up cost) | resuming vs starting fresh (only when the history has both) |
| `ledger.html` (Session ledger) | the sortable per-session table |
| `definitions.html`, `appendix.html` | every term; methods, checks, and "What has not been shown" |
| `screen-pack/` | numbered one-screen pages for a projector, a screenshot or a meeting |

The home page marks the analyses this run did not do. Offer the user those.

Scope: no folder filter by default. `--include-cwd-pattern` and
`--exclude-cwd-pattern` take regexes (repeatable) and work on every script. Use
them to keep or drop sessions by working directory, such as automated runs
that would swamp interactive work. On `orientation_profile.py`, `--label NAME`
names the history in the report title (default "this machine").

## Words to use

Use these words with the user, in summaries and in any report you write. The
scripts' JSON keys and ledger columns keep older names; the mapping is in
[references/orientation-and-threads.md](references/orientation-and-threads.md).

| say | meaning |
|---|---|
| startup, startup share | tokens a session spends before its first edit, and their share of the session |
| first edit | the first successful change to a file: an edit, a write, or a file-changing command |
| no-edit session | never changed a file (questions, reviews, research); it has no startup share |
| sitting | sessions on the same work with no gap longer than 72 hours |
| work item | the same work across any gap, linked by ticket, branch or shared files |
| repeat startup | startup spent by every session after the first in a sitting (or work item, "incl. long breaks") |
| resumed after a pause / after compaction / new session | the three ways work continues |
| pick-up cost | what it cost to get going again, up to the next edit |
| read a handoff note | opened a HANDOFF/STATUS-style note in its first two turns |
| est. cost | estimated cost at list API rates, for comparing sessions |
| cache lifetime | how long a prompt cache entry lives: 5 minutes or 1 hour |
| reusable context, pause between responses | the cached context the next request could read back; the time between two responses |

## Router — question → command → what to read first

Every command takes `--root`, `--since`, `--until`, `--pricing-file`, the two
scope flags, and `--ledger` where shown. Run `--help` for the rest.

| The user asks | Run (from `$SKILL/scripts/`) | Read first |
|---|---|---|
| "where did my tokens / usage go", "which sessions or agents cost the most" | `lane_profile.py --json-out $OUT/lane.json` | [references/lane-economics.md](references/lane-economics.md) |
| "how much do my sessions spend before the first edit", "what keeps re-learning the same context", "what effort did this run at" | `orientation_profile.py report --output-dir $OUT --ledger $OUT/sessions.jsonl` (also `sessions`, `threads`, `ingest`, `probe-store`) | [references/orientation-and-threads.md](references/orientation-and-threads.md) |
| "is it cheaper to carry a session on than to start fresh", "what did compaction cost me to keep going" | the same `report`; it writes `carry-vs-restart.html` when a work item was both resumed and restarted | [references/orientation-and-threads.md](references/orientation-and-threads.md) §2e |
| "does writing a handoff note pay for itself" | `orientation_profile.py sessions --handoff-log handoff-log.csv` (CSV format in §2b) | [references/orientation-and-threads.md](references/orientation-and-threads.md) §2b |
| "why did compaction cost so much" | `compact_economics.py --json-out $OUT/compact.json` | [references/lane-economics.md](references/lane-economics.md) |
| "which tool results are bloating my context" | `tool_bloat.py --ledger $OUT/sessions.jsonl` | [references/lane-economics.md](references/lane-economics.md) |
| "1-hour or 5-minute cache?", "what would a different cache lifetime have saved" | `claude_cache_audit.py --output-dir $OUT --ledger $OUT/sessions.jsonl` | [references/cache-economics.md](references/cache-economics.md) |
| "show me the sessions, sortable" | any of the above with the same `--ledger`; the table is `ledger.html` | [references/session-ledger.md](references/session-ledger.md) |
| "make it a report I can show someone" | read the spec first; share only a `--redact` run | [references/visual-report-spec.md](references/visual-report-spec.md) |
| "an unfamiliar or changed log layout" | `orientation_profile.py probe-store PATH` | [references/log-schema.md](references/log-schema.md) |

`--ledger PATH` builds `sessions.jsonl`: one row per session, and every
analysis adds columns to it. Pass the same path to every command. Read each
analysis's `summary.md` or stdout first; open the HTML when a chart helps. Use
the JSON only for follow-up calculations, and never paste it into the
conversation.

## Output directory

Use `./agent-audit-$(date +%F)` under the current directory. Not `/tmp`: it is
world-readable on macOS, absent on Windows, and may be cleared, taking the
ledger with it. On Windows use a path such as `$env:USERPROFILE\agent-audit`.
`mktemp -d` is fine for a one-off. Never write inside the agent's own
configuration folder. Files are created readable by the current user only
(0600; folders 0700) where the filesystem allows.

## Data boundary

The local report is complete by default: real paths, titles, first prompt
lines, one-line tool arguments. A report you cannot interpret is a worse tool.
Every analysis shows paths under the home folder as `~/...`, in its files and
its printed output. Analyze your own usage data, not other people's.

Before you share a report, run with `--redact` and check it with
`python3 scripts/test_orientation_profile.py --privacy-check <output-dir>`.
Before you share results with anyone, share aggregates only: counts,
histograms, totals. Never raw prompts, file names or content.

## Rules for every analysis

Each analysis also has its own rules in its reference file. Read that file
before explaining its numbers.

- **One API response is one turn, not one JSONL row.** Claude Code writes a row
  per content block and repeats the same `usage` on each. De-duplicate on
  `(sessionId, message.id)` and merge the blocks. Counting rows inflates totals
  about 2×, unevenly. Dropping repeats without merging loses their tool calls
  ([references/evidence.md](references/evidence.md) E1).
- **Use the pause between responses for cache costs**, not the pause between
  the user's prompts. The agent's own tool calls refresh the cache in between.
- **Weight by reusable context, not by count.** Ten tiny requests should not
  outweigh one large return. Never headline a count.
- **Do not give advice the evidence does not carry.** Say what was measured,
  what would have to be true for a fix to work, and which test would show it.
  Never tell the reader to write a handoff note of any length. "Five-line
  note" is a writing habit, not a result
  ([references/orientation-and-threads.md](references/orientation-and-threads.md) §6).
  A grep for `five-line` / `5-line` over the generated pages must find nothing.
- **Say "estimated cost at list API rates" (short: "est. cost").** Never "proxy
  cost". Dollar figures use list API rates so sessions can be compared
  whatever plan you are on. Never show a subscription meter in a report; on a
  Max/Pro plan, `orientation_profile.py meter` prints the 5-hour and weekly
  buckets separately. Footnote the price table and quote its coverage. A dollar total
  below the coverage floor prints with a caveat; carry it into your summary.
- **`<synthetic>` is not a model.** Leave it out of every table; keep its tokens
  in the totals.

## Reporting

How to answer:

- **Lead with a table or chart, not prose.**
- **Keep the summary short — a few lines.** Put detail below it.
- **Use the words in "Words to use".** Define any other term the first time you
  use it.

Close every audit with:

- the recommendation and its sensitivity range, with price coverage;
- which pauses and how much reusable context drive it;
- likely invalidations and data-quality limits;
- two or three concrete working-style changes (never an artificial paid
  keep-alive prompt unless separately evaluated);
- **which router rows were not run**, so the user knows what else to ask for.

## Validation

```bash
cd "$SKILL/scripts"
for t in test_audit_common test_claude_cache_audit test_orientation_profile; do
  python3 $t.py || echo "FAILED $t"; done
for a in lane_profile compact_economics tool_bloat; do
  python3 $a.py --selftest || echo "FAILED $a"; done
python3 orientation_profile.py selftest
```

The suites use synthetic logs only. An audit of your own history is also
offline and read-only: it must never launch an agent.

## Resources

**Scripts** in `scripts/` (run them; do not read them into context)

- `audit_common.py` — the shared scope filter and the one price table; import it from any analysis you add.
- `claude_cache_audit.py` — cache-lifetime simulator and report.
- `lane_profile.py` — cost per conversation and subagent, concentration, cold-cache events.
- `orientation_profile.py` — startup, sittings, work items, pick-up cost, the report and screen pack, the usage meter (subscription plans), the Copilot ingest.
- `compact_economics.py` — cost and payback per compaction, warm vs cold cache.
- `tool_bloat.py` — later-turn cost by tool, and the largest single calls.
- `session_ledger.py` — updates `sessions.jsonl`, with per-column provenance.
- `test_*.py` — the suites above; `test_orientation_profile.py` also has `--privacy-check` and `--regenerate-expected`.
- `check_report_overflow.py` — fails on clipped text in headless Chrome; needs a browser, so run it by hand after layout changes.

**References** in `references/` (read on demand)

- `cache-economics.md` — cache prices, when the 1-hour cache pays off, cache-lifetime rules.
- `lane-economics.md` — the cost model for conversations and subagents, and what to change.
- `orientation-and-threads.md` — startup, sittings, work items, pick-up cost, the handoff-log CSV, running on Windows.
- `session-ledger.md` — every ledger column, and adding a producer.
- `visual-report-spec.md` — the report's required visuals and panel rules.
- `log-schema.md` — the log fields read, de-duplication, limitations.
- `evidence.md` — the checks behind the rules.
- `roadmap.md` — open ideas, known limitations, rejected ideas.
- `pricing-public-example.json` — the price table; replace or override it when your rates differ.

**Other**: `agents/openai.yaml` · `evals/` (trigger, non-trigger and end-to-end cases; the model matrix has not been run) · `tests/fixtures/` (synthetic handoff logs, never a real row).
