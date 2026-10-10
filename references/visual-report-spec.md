# Standard visual report

## Contents

- Analysis index — the reader always leaves knowing what else they can ask for
- Cache-lifetime views — the charts the cache audit renders, with what each is out of
- Conversation-and-subagent views — when `lane_profile.py` ran
- Required scope labels — filters, price table, coverage, dates
- Integrity rules
- Startup report — the page set, each page's panels, and the writing rules
- How to build or extend the graphics
- Panel rules — out of how many, no good/bad colours, priced money, the main conversation's first edit

Use this specification whenever an audit is presented visually. The medium is
flexible — HTML/SVG, a plotting library, a notebook, a slide or an image — but
keep the metrics, what each is out of, and the order below.

## Analysis index

The skill has several analyses (cache lifetime, conversations and subagents,
startup cost, compaction, tool-result bloat — the rows of the SKILL.md router).
Every visual report links to an analysis index: one row per analysis, with the
question it answers and whether it ran on this history. An analysis that did
not run shows the command to hand to an agent, so the reader learns it exists.
Nothing in the index looks clickable-to-run. A text-only report ends with the
same menu, one line per analysis.

## Cache-lifetime views

### 1. Pauses between responses

One bar chart, one bar per pause length: `<1 min`, `1–5 min`, `5–15 min`,
`15–30 min`, `30–60 min` and `>60 min`. Bar length is the reusable context at
stake, not the pause count. Split each bar in two: "agent working on its own"
(no human prompt directly before the response) and "waiting for you". Shade
the band from 5 minutes to 1 hour. The title states the takeaway, for example
"Only 12 % of reusable context waited the 5–60 minutes where the 1-hour cache
helps". "Agent working on its own" is a sign, not proof. Pause counts go in
the tooltip and the numbers table.

Answers: is the work continuous, pause-and-resume, or mostly expired, and who
causes each pause?

### 2. How much ran without you

Uninterrupted sub-minute runs of responses: a duration distribution or
cumulative curve, the median and 90th percentile, 5- and 15-minute reference
lines, and responses per run (median and 90th percentile).

Answers: do many short pauses come from brief tool loops or long runs?

### 3. Where the cache lifetime matters

Reusable-context shares in exactly three bands: `≤5 minutes` (both lifetimes
help), `>5 and ≤60 minutes` (only the 1-hour cache helps), `>60 minutes`
(neither helps by duration alone). Headline token volume, not pause counts.

Answers: how much context sits in the only band where the choice matters?

### 4. Which pauses could the 1-hour cache have helped?

Draw it as a funnel of decreasing bars, one per step: all pauses; longer than
5 minutes; up to 1 hour; nothing else reset the cache; had context to reuse.
Each bar is a pause count with its number beside it. The last bar is the
answer and gets the strong colour; the others stay muted. The tooltip adds the
reusable-context tokens and, on the reset step, what reset the cache.

Answers: where does the apparent opportunity drop out after 5 minutes?

### 5. Economics and break-even

Paired bars or a bridge for the extra 1-hour write premium (`0.75 × all 1-hour
write tokens`), the recoverable read benefit (`1.15 × reused tokens on returns
after a 5–60 minute pause`), and the net. Beside it, one plain sentence: "the
1-hour cache pays off if more than 65.2 % of …". Never relabel all cache
creation as rewritten old context.

Answers: why did this history land above or below break-even?

### 6. What-if: share of context still reusable on return

A line chart: the 1-hour saving (y, dollars, with a zero line) against the
share of context still reusable on return (x, 0 to 100 %). Mark each modelled
setting with a labelled dot and, where the line crosses zero, the break-even
share. Beside it, the break-even sentence and "the share is a what-if setting,
not a measurement". Above zero means the 1-hour cache is cheaper. The 5-minute
and 1-hour costs per setting go in the tooltip and the numbers table.

Answers: does the recommendation survive reasonable doubt about how much
context survives?

### Conditional views

When the history is large enough: **conversation types** (one row per
anonymous project or session group, with the share returning after a 5–60
minute pause, reuse and the suggested lifetime), and a **weekly trend** with
known configuration changes marked.

## Conversation-and-subagent views

When `lane_profile.py` ran. A *lane* is one conversation or subagent. Every
dollar is an estimate at list API rates from the stated price table, never a
bill or a saving.

- **L1. Where the cost sits.** Lanes sorted by est. cost with a cumulative
  curve; label the top-5 and top-20 shares and the top lanes by kind, model and
  project group. Answers: would changing a few lanes change the bill?
- **L2. What the cost is made of.** Cache reads, cache writes, uncached input
  and output as shares of est. cost; label reads "at the 0.1× rate". Answers:
  re-reading, write churn, or output?
- **L3. Expensive-lane shape.** Turns against reads per turn, point size = est.
  cost, marked by kind, with the large-context line. Answers: long-and-fat,
  short-and-fat, or many-and-small?
- **L4. Cache breaks over time.** Size = rewritten tokens, split expiry vs
  invalidation. Name a cause only where a coincidence test ran.

Never headline the cache hit ratio: it is often near-perfect while cost is
high. State the cost model once: a context token's lifetime cost ≈ remaining
lane turns × 0.1 × base input rate.

## Required scope labels

Every visual report states: the dates and that they are UTC; the folder
filters; unique responses and duplicate rows removed; the price table and the
share of tokens it prices; that human-message timing is not API timing; and
that tool-schema and other hidden cache-key changes cannot be fully seen.

## Integrity rules

- Print every funnel step's number beside its bar, so a small last step
  stays readable when all pauses set the scale.
- Never headline raw counts when token weighting tells a different story.
- Never treat every sub-minute pause as a human interaction.
- Never call list-price differences subscription savings without billing
  evidence.
- Every value in a chart is also in a label or a table.

## Startup report

`orientation_profile.py report`. It answers two questions: which sessions
spend the most before their first edit, and which work pays that again each
time it comes back.

### Words

Use these words in everything a reader sees: page text, legends, tooltips,
help, stdout. Machine names (JSON keys, ledger columns, flags) do not change.

| reader sees | means |
|---|---|
| startup, startup share | tokens before a session's first edit; their share of the session |
| first edit | the first successful change to a file: an edit, a write, or a file-changing command |
| startup cost | those responses in dollars, each at its own model's list rate |
| no-edit session | a session that never changed a file; it has no startup share |
| sitting | sessions on the same work with no gap longer than 72 hours |
| work item | the same folder, branch or ticket, however long between sessions |
| repeat startup | startup of every session after the first in its sitting |
| repeat startup, incl. long breaks | the same, across the whole work item |
| resumed after a pause / after compaction / new session | the three ways work is picked up (`continuation_kind` R0 / R1 / R2) |
| pick-up cost | what it cost to get going again, up to the next edit |
| files read first | distinct files touched before the first edit |
| read a handoff note | opened a HANDOFF/STATUS-style note in the first two turns |
| started with a question | the first request was a question; the edit came later |

Machine values never reach a reader: the ledger page shows a kind as words, a
work item by its name, and a sitting by its folder. Ids stay in the ledger file.

### Writing rules

- Sentences of 25 words or fewer, one idea each. No all-caps emphasis; bold one
  phrase at most.
- A chart title states the takeaway ("10 sessions hold 95 % of the startup
  cost"), not the metric's name.
- At most one short callout per panel, two sentences at most. Longer method
  notes go to the appendix, inside a collapsed `<details>`.
- A page that interprets ends with one "What this shows / What it doesn't
  show" box, three bullets a side at most.
- Never print build history, ticket ids, dates of fixes or "was previously".
- No subscription words: "estimated cost at list API rates", short label
  **est. cost**. The usage meter is not in the report; `meter` prints it for
  subscription plans.
- `<synthetic>` is not a model. Filter it from every table; its tokens stay in
  the totals.
- Never tell the reader to write a handoff note of a set length. No test of
  note length exists; `grep -i 'five-line\|5-line'` over the pages is empty.

### The page set

| file | what is on it |
|---|---|
| `index.html` | the answer first: four stat tiles, one headline chart (est. cost split startup / the rest; sessions split edit / no-edit), a short summary, then the analysis checklist |
| `orientation.html` | "Startup": the toy explainer, where the startup money went, the drawn definition of work item / sitting / repeat startup, work items over time, which work items pay the most, the shows / doesn't-show box |
| `carry-vs-restart.html` | "Pick-up cost", only when a work item was both resumed and restarted: the three kinds drawn, cost per kind, a dot plot per work item, the work items that did both, model × effort, the shows / doesn't-show box |
| `definitions.html` | every term in one table (what it is, which way is good, how to read it), the two drawings, and what the numbers cannot tell you |
| `ledger.html` | the session ledger: sortable, ten columns by default, every column in the DOM |
| `appendix.html` | what has not been shown; the sceptic's questions and the definition check (collapsed); distributions; scripted vs by hand; effort; the shape of each sitting; rules, dedupe and prices (collapsed); the handoff experiment; all sittings; which sessions are included |

Every page is one self-contained file: inline CSS, JS and SVG, no network.
Links between pages are relative. Every page carries the same header: the page
list and a link to the definitions. A new analysis gets its own page and a row
in the index. The screen pack follows the same split.

### Home page

- **Tiles:** est. cost of all sessions; startup cost; median startup share, out
  of the sessions that made an edit; repeat startup incl. long breaks, in tokens.
- **Headline chart:** two 100 % bars on one scale. The part that matters is in
  the accent hue, the rest in neutral grey (emphasis, not two categories).
  Segments are labelled inside only when the label fits.
- **A short summary** (a few lines), then the analysis checklist: name · question · ✓ in this
  report, or the command. The ledger columns each analysis adds are in the row
  tooltip.

### Startup page

1. **The explainer, on toy data, before any real number.** One session as a
   token bar: time runs left to right, grey (startup) is the left segment and
   blue (the work) the right, in every bar, and the share label sits on the
   grey. Then one work item, three sessions: the first grey is the price of new
   work, the later ones are repeat startup. The suite asserts the segment order
   and fills.
2. **Where the startup money went.** Top 10 sessions by startup dollars, each
   named by folder and first prompt line, a two-segment bar (startup, then the
   rest of the session), "first" or "later" in its sitting, and ✎ for a note read.
3. **Work item, sitting and repeat startup, drawn** before any number: five
   sessions, a 31-day break, two brackets.
4. **Work items over time:** one row per work item, one band per sitting, the
   break labelled in days, and a hover that says whether the 72-hour rule or the
   shared-file test split them. The legend draws its encodings (dot, band,
   dotted line) rather than naming them.
5. **Which work items pay the most repeat startup:** one bar per work item,
   split inside sittings / after a long break, with the full table (sittings,
   breaks, dollars, handoff pattern) collapsed under it. One callout: later
   no-edit sessions left out, and the effect of setting aside sessions that
   started with a question.
6. **What this shows / what it doesn't.**

A work item is never keyed on the folder alone: many people run all their
work from one folder. A link is a shared ticket token (any folder), or the same
folder plus the same non-default branch, or the same folder plus ≥ 2 shared
file names. Each work item shows which rule grouped it.

### Pick-up cost page

Written only when at least one work item was both resumed and started again;
otherwise the index says *ran — not enough data* and why.

1. **The three kinds, drawn** on one work item, with "transcripts carry no
   resume marker, so a pause is a clock gap" on the panel.
2. **Cost per kind:** median pick-up tokens per kind with $ and n at the tip;
   one callout with the counts and what was left out (new sessions that never
   edited, sessions that ran in parallel); the table and the rule collapsed.
3. **Each work item, each kind:** a dot plot, log scale, one colour per kind;
   the table collapsed.
4. **Work items that did both:** one row each, a dot for the new session and
   one for the resume, joined; the right-hand figure is resume ÷ new session.
5. **By model and effort:** both change cost, so each model × effort group is
   one comparison.
6. **What this shows / what it doesn't.**

One colour per kind on every chart of the page — categorical slots 1–3:
new session blue, resumed after a pause orange, resumed after compaction aqua.
The three validate all-pairs in both modes; aqua is below 3:1 on the light
surface, so every chart has a legend and a table.

Rules: never present the kinds as a fair test — nothing was assigned. A
session that started before an earlier session of the same work item ended
ran **in parallel**: it is not a pick-up, and its gap would be negative. A new
session's gap is measured from the latest earlier end. Never add the modelled
compaction-summary price into an observed total. A new session with no edit
has an unknown pick-up cost and is left out of the medians, not counted as zero.

### Session ledger page

`sessions.jsonl` holds one row per session; each analysis adds its own columns,
and each column's tooltip names the script that wrote it. The page is sortable
on every header (the sort reads the column index from the header's own row),
shows at most ten columns by default with a toggle for the rest, and keeps
numbers right-aligned. A registered column with no values is an analysis not
run here, never zeroes. Schema: `references/session-ledger.md`.

### Appendix

Evidence that the pages measure what they say; none of it is a decision.

- **A0 · What has not been shown**: no validated good/bad line, no proof of
  re-learning, no test of handoff notes. The sceptic's five questions and the
  "does the ranking survive a change of definition?" table, both collapsed.
- **A1 · Startup share across sessions**: the histogram (whole-number count
  ticks, no bands), each group as a share of itself with both n printed, split
  by how the session ended (inferred; unknown kept apart), files read first,
  and est. cost against startup share.
- **A2 · Scripted or by hand**: median est. cost per strategy and model.
- **A3 · Effort**: thinking share per model, colour = recorded effort level,
  an ordered ramp, no shape encoding. This checks the record; it is not a finding.
- **A4 · The shape of the work**: what each sitting did in order (startup,
  explore, edit, check), repeat startup per sitting, and each sitting session
  by session (collapsed).
- **A5–A7**, collapsed: how sittings and work items were built and how far they
  are calibrated; what was deduplicated (one response is one turn); the price
  table, with a note that Copilot stores record AI credits, not token prices.
- **A8 · The handoff experiment**: paired dots per sitting (the line's slope is
  the result), the categorical blue/orange pair for NOTE/NONE (never
  red/green), a net-value bar on a symmetric zero-centred scale (the only
  place the blue ↔ red diverging pair appears), and the note-read tile with
  its Wilson 90 % interval, carrying the below-80 % caveat inline. It renders
  with zero pairs and with one.
- **A9 · All sittings** (collapsed) and **A10 · Which sessions are included**.

## How to build or extend the graphics

`scripts/orientation_profile.py` is the reference implementation.

1. **Theme tokens.** One `CSS` string holds every colour as a custom property
   on `:root`, with `html[data-theme="dark"]` and a
   `@media (prefers-color-scheme: dark)` block. Marks use classes or `var(--…)`
   (`--accent`, `--accent2`, `--c3`, `--div-mid`, the stage ramp); text uses
   ink tokens, never a series colour.
2. **`panel(title, decision, body, note="")`** renders the card, the
   **Decides:** line and at most one callout. `shows_box(shows, not_shown)`
   renders the closing box.
3. **Scales and text.** `linscale` / `logscale` map data to pixels;
   `text_block()` wraps any label longer than a few words into `<tspan>` lines
   at a stated width (SVG does not wrap); `compact_number()` prints 33.9M and
   812.0K; `short_path()` cuts a path at a separator behind "…/".
4. **Interaction.** A theme toggle, one delegated tooltip that reads
   `data-tip`, and click-to-sort on `table.sortable` (`data-v` holds the
   sortable value).

### Adding a panel

1. Write a function that returns `svg(...)`, plus a collapsed table of the same
   numbers when the chart is dense.
2. Put every mark's exact values in `data-tip`.
3. Wrap it in `panel(title, decision, body)`. If the decision line does not fit
   in one sentence, the panel is not ready.
4. Add a check to `scripts/test_orientation_profile.py`, run
   `scripts/check_report_overflow.py` (it renders every page in headless Chrome
   and reports clipped labels), and screenshot the page.

A plotting library may be used only if it is inlined: the pages must work
with no network. The suite fails if an `http(s)://` source or an `@import`
appears.

## Panel rules

### 1. Say what a number is out of

Every share, median and distribution names its population in words: "20 of
21 sessions made an edit", not "half your sessions". Two groups of unequal
size are drawn each as a share of itself, with both n printed.

### 2. No good/bad colours without an outcome

No green/amber/red and no "healthy" or "expensive" unless a threshold has been
validated against an outcome on held-out work. None has, so the report quotes
no reference value for the startup share.

### 3. Money is priced, never allocated

A dollar figure for part of a session is the sum of `turn_cost_usd()` over the
responses it is about, each at its own model's rates. Never the session total
times a token share: input, cache and output rates differ before and after the
first edit.

### 4. The first edit is the main conversation's

The first edit, the first-two-turns window and the files read first are
computed over responses with no `agentId` / `isSidechain`. Subagent spend is
rolled up in its own columns and never merged into the parent's timeline.
