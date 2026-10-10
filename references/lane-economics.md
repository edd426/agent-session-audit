# Where usage goes: conversations and subagents

How to find where cost lives in Claude Code history (`lane_profile.py`,
`compact_economics.py`, `tool_bloat.py`). The cost model and the checklist hold
generally ([evidence.md](evidence.md) E6); measure the magnitudes on your own
history.

## Terms

- **Lane**: one continuous conversation — a session's main conversation, or
  one subagent. The scripts key it on `(sessionId, agentId | side | main)`.
  Cost is analysed per lane because each lane has its own context.
- **Est. cost**: tokens priced at published list API rates. It is for
  comparing lanes, never an invoice, and on a subscription it is not savings.
  Never call it "proxy cost".
- **Cold event**: a turn that reads back almost none of the previous turn's
  cached context and writes a similar amount again (thresholds in
  `lane_profile.py`). Past the cache lifetime it is expected expiry; inside it,
  something changed the prefix.

## The cost model

Every API call re-sends the lane's whole context. With a warm cache that
re-read costs 0.1× the input rate: discounted, not free. So:

> A context token's lifetime cost ≈ remaining turns in the lane × 0.1 × input rate (plus one write).

For example, a 50k-token tool result with 750 turns still to go in its lane is
re-read 750 times: 37.5M tokens at 0.1 × $5/M, about $19 at Opus input rates.
Caching works perfectly the whole time.

What this means in practice:

- A cache hit ratio above 0.9 can sit beside cache reads being most of est. cost.
- Most read volume accrues while a lane's context is large.
- Cost concentrates: a handful of lanes can be half of est. cost.
- Prefix breaks within the cache lifetime are usually second-order next to
  steady re-reading.

The expensive shape is a long lane (hundreds of turns) carrying a large context
(hundreds of thousands of tokens re-read per turn). A high hit ratio does not
mean low cost; volume is the bill.

## What to change (each with its caveat)

1. **Retire stale context, and compact while the cache is warm.** Compact or
   end a lane when its history goes stale. Dead tool results are paid for on
   every later turn; this is the largest lever. Compaction re-reads the whole
   context, about 10× cheaper while the cache is warm. So compact before
   stepping away, not on return after the cache has expired.
2. **Splitting a lane saves only when the work splits.** If each part needs only
   a slice of the context, read volume drops roughly by the split. If every
   turn needs everything, splitting only adds overhead. Parallel versus
   sequential is a wall-clock choice, not a token choice.
3. **Separate discovery from execution.** Pay for the search once, in a cheap
   short-lived lane. Hand the file list to the long lane, so it does not carry
   the search history for hundreds of turns.
4. **Load deferred tools early and together.** Loading tool schemas rewrites the
   lane's prefix: noise at 8k context, a full rewrite at 400k. Subagents whose
   definitions declare their tools up front avoid this. Untested as a strategy.
5. **Say how sub-spawns are routed in delegation briefs** (model and rough
   budget). Unrouted spawns default to the strong model and are invisible to
   the parent's budget. Sub-spawns can pay off; unbudgeted fan-out is the
   problem.
6. **Stop rules and effort settings work together.** Effort sets how hard each
   decision searches. A stop rule ("if inconclusive by X, deliver what you
   have") caps total spend. Without one, an agent can grind for hours at any
   effort.

## Audit checklist

- Per lane: turns, largest context, reads, writes, output, reads per turn, hit
  ratio, share of reads above a context threshold, est. cost.
- How concentrated cost is: the top-N lanes' share. Name the top lanes' shape
  (main conversation or subagent, model, project).
- Est. cost split into cache reads, cache writes, uncached input and output.
- Cold events: count and rewritten volume, expiry versus prefix change. For
  prefix changes, test causes (model switch, compaction, tool load, version
  change) before blaming one.
- Do not headline the hit ratio. Do not call estimated dollars subscription
  savings. Do not name a cause without a coincidence test; the obvious suspect
  is often not the cause.

## Interpretation rules

- **Never headline the cache hit ratio.** It can be near-perfect while
  discounted reads dominate cost. The lane question is context × turns.
- Read per-lane findings through the cost model above. Long lanes carrying stale
  history, not cache failures, are the usual driver.
- **Name a cause for a prefix break only after testing it**: model switch,
  compaction marker, tool-schema load, version change. Assumed causes have been
  wrong.

### Compaction and tool-result cautions (`compact_economics.py`, `tool_bloat.py`)

- The compaction summary call has no usage record locally. Its cost is
  modelled and labelled: the pre-compact context at the cache-read rate if
  warm, or the full input rate if cold, plus the summary's output tokens. The
  write after compaction is observed. Never present the modelled figure as observed.
- Tool results are de-duplicated by `tool_use_id`, because resumes and
  compactions re-send the same call.
- A tool result stops costing at the lane's next compaction.
- Images count as vision tokens (~1.6k), never as base64 characters ÷ 4, which
  overstates them about 100×.
- Fixes depend on the tool: large shell output → filter, redirect, `tail`;
  large file reads → `limit`/`offset` or a short-lived explore lane; large web
  or MCP results → summarize in a side conversation first.
