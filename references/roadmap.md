# Roadmap

None of this is needed to run anything. Every open idea below is computable
from the same local history: offline, free, no model call. When one lands, it
gets a row in the report's analysis index and in the SKILL.md router.

## Open ideas — analyses not built yet

1. **Standing-config overhead.** Baseline first-turn cache-write size across
   projects and configs — i.e. what the MCP/tool configuration costs at every
   session start.
2. **Subagents re-reading their parent's files.** How much of a subagent's
   reading repeats files its parent conversation already read — discovery a
   richer delegation brief could have carried. Descriptive only until a
   controlled test validates it. A lot of reading is not waste on its own:
   for research agents, discovery is the deliverable.
3. **Output tokens per conversation and subagent.** Output per model and per
   recorded effort level is in `orientation_profile.py`; `lane_profile.py` does
   not yet break it down per conversation.
4. **Configurable tool-output truncation.** Check whether harness-level
   truncation of tool output can be configured; a setting beats a habit.
5. **Run the evaluation matrix.** The trigger, non-trigger and end-to-end
   cases in `evals/` have not been run against Haiku, Sonnet and Opus.

## Known limitations

- **No resume marker.** Claude Code records no explicit resume, so "resumed
  after a pause" is detected by a clock gap only ([evidence.md](evidence.md) E4).
- **Sittings and work items are uncalibrated** until you label your own
  history; metadata-only grouping over-merges.
- **The first-edit detector is a heuristic.** A write made through an MCP tool,
  a custom tool or an unknown wrapper (`make deploy`) is missed.
- **Copilot stores are best-effort.** VS Code Copilot Chat and Copilot CLI
  records often lack token counts and never carry an effort level. A session
  without token counts gets est. cost null, shown as "—": left out of totals
  and medians, and counted as unpriced in price coverage. The Copilot CLI
  SQLite schema is undocumented.
- **The compaction summary call is modelled, not observed** — it has no usage
  record in the transcript ([evidence.md](evidence.md) E3).
- **Cache-read pricing is one multiplier for every model.** Models that publish
  a flat cache-read rate (Claude Fable 5.1) are overstated; see the note in
  `pricing-public-example.json`.
- **Hidden cache-key changes are invisible.** Tool-schema and MCP changes alter
  the prefix but are not reliably recorded locally.

## Studied and rejected

- **Redundant-read audit** — pure re-reads of the same file were a negligible
  share of read volume. Not worth a module.
- **Near-miss cache returns** — the only useful version (which sessions should
  switch cache lifetime) is already the cache-lifetime analysis.
- **Meter calibration** — the report uses estimated dollars at list rates
  instead of a calibrated subscription meter.
- **Cross-session merge counterfactuals** — cannot be established by ledger
  arithmetic: independent sessions share no conversation prefix, so
  `min(cacheable)` over-credits massively.
- **Model-tier / effort shift detector** ("should this session have moved up
  or down a tier?") — its precision was far too low to act on and there was no
  calibration data, so it is not shipped: a ranked list nobody should act on is
  worse than none.
