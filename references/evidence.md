# Evidence behind this skill's rules

Each rule below comes from inspecting real Claude Code transcripts. Magnitudes
are rounded and only given where a rule needs one. Re-measure on your own
history before leaning on any of them. Other files cite these by number.

| # | what was checked | what it showed |
|---|---|---|
| E1 | How Claude Code writes an assistant response to its JSONL, and what two analyzer bugs did to the numbers. | One API response is one turn, not one JSONL row. Claude Code writes a row per content block and repeats the same `usage` on each. Counting rows instead of responses inflates totals about 2×, unevenly, so it does not cancel in a ratio. Dropping the repeats without merging them loses their tool calls. Matching shell write verbs as substrings misread a flag such as `-prune`, and `2>/dev/null`, as file changes. |
| E2 | Startup share split by what a session opened in its first two turns. | A README is not a handoff note. Sessions that opened a handoff note had the lowest startup share; sessions that opened a README had the highest, above those that opened nothing. Pooling README with notes made notes look like a cost. So `readme_opened_by_turn_2` sits beside "read a handoff note", never inside it. |
| E3 | Compaction records. | The compaction's own summary call writes no assistant record with usage, so its cost is modelled from the `compact_boundary` row (`trigger`, `preTokens`, `postTokens`, `durationMs`) and kept in its own column, never added to an observed total. The write after compaction is observed. A tool result stops costing at the next compaction. |
| E4 | How a resume is recorded. | It is not. One file is one `sessionId`; a resume opens no new file. `entrypoint` never changes within a session. `away_summary` is written a few minutes after the last response: it marks stepping away, not coming back. A mid-session CLI `version` change proves a restart but is rare. So "resumed after a pause" is a clock gap and nothing more. |
| E5 | The next response's own cache usage after a pause between two main-conversation responses. | The one-hour cache, measured. After a pause of up to an hour the next response nearly always read from the cache; after more than an hour it nearly never did. Hence `--resume-gap-hours` defaults to 1.0. Writing a context at the 1-hour rate costs 20× reading it. The clock is not the only cause: a CLI upgrade between two responses forces a full rewrite even after a short pause. |
| E6 | Cost per conversation and subagent, priced at list rates. | A near-perfect cache hit ratio can sit beside cache reads being most of est. cost. Cost concentrates in a few long conversations carrying a large context. Details in [lane-economics.md](lane-economics.md). |

## Not evidence: how sessions are grouped

Sittings and work items come from a rule with settings, not a measurement.
Metadata-only grouping over-merges: one "sitting" can turn out to be one piece
of work plus unrelated tickets sharing a busy folder. Until you label your own
history, its precision is not measured. Quote `--gap-hours`, `--min-shared`
and `--max-sessions` with any sitting figure. Check the rule with
`--label-candidates` / `--labels` before believing one. Read the sitting and
work-item tables as a ranked shortlist, not a result.
