# Cache lifetime: 5 minutes or 1 hour?

How `claude_cache_audit.py` decides whether a 5-minute or 1-hour prompt-cache
lifetime (TTL) fits a history, and how to explain its numbers.

## Words

| say | meaning |
|---|---|
| reusable context | the cached part of a request that the next request could read back |
| pause between responses | the time between two assistant responses in one conversation or subagent |
| return after a 5–60 minute pause | the only case where the two cache lifetimes differ |
| share of context still reusable on return | a what-if setting (`--reuse-scenarios`), not a measurement |
| how much ran without you | responses with no human prompt before them: the agent working on its own |

## Prices

For a model whose input price is `P`, Anthropic's published cache multipliers:

| token kind | price |
|---|---|
| uncached input | `1.00 × P` |
| 5-minute cache write | `1.25 × P` |
| 1-hour cache write | `2.00 × P` |
| cache read | `0.10 × P` |

Output keeps the model's output price. Observed cost per response:

`input × P + output × P_output + read × 0.10P + write_5m × 1.25P + write_1h × 2.00P`

Older records whose cache writes are not split by lifetime use the
`--unknown-write-ttl` assumption and are reported separately. The bundled price
table is dated example data. Use current list prices or your own rates before
treating dollars as financial evidence.

## The what-if comparison

For each pair of consecutive responses in one conversation or subagent:

- `C` is the current response's reusable context (`cache_read + cache_creation`);
- `S = min(previous C, current C)` is the most context the two could share;
- `f` is the share of context still reusable on return.

| pause between the two responses | 5-minute cache | 1-hour cache |
|---|---|---|
| first response, ≤ 5 min, > 60 min, or a detected context change | observed cache read | observed cache read |
| 5–60 min, same conversation, no context change | read = 0 | read = `f × S` |

Either way, `write = C − read`. Only returns after a 5–60 minute pause can
differ. The 1-hour cache is also charged its higher write price on new context,
so long-pause reads are not free savings. This models one conversation at a
time; it does not replay Anthropic's cache service. Context shared across
sessions is not credited, so the result leans conservative.

`f` defaults to 50 %, 80 % and 100 % (`--reuse-scenarios`). These are
scenarios, not confidence intervals. If the answer flips across them,
recommend the 1-hour cache only selectively, or measure more.

## When the 1-hour cache pays off

**The 1-hour cache pays off if reads on returns after a 5–60 minute pause
exceed 65.2 % of all 1-hour cache writes.** Lead with this figure. The
arithmetic: a 1-hour write costs `0.75P` more than a 5-minute one. Reading a
token instead of rewriting it at the 5-minute price saves `1.25P − 0.10P =
1.15P`. And `0.75 / 1.15 = 65.2 %`.

For a single return there is a narrower version. The 1-hour cache paid off on
that return if more than 39.5 % of the context it sent was read from cache,
`read / (read + creation)`. That is `0.75 / (0.75 + 1.15)`. It ignores writes
outside that return, so never call it "39.5 % of all cache tokens" or swap it
for the 65.2 % figure.

Cache creation on a return is not necessarily old context written again. In a
conversation that only grows, it usually includes the new prompt, the last
answer and new tool results. Positive growth in context bounds that new part
from below. Leave the rest of the creation unexplained unless stronger evidence
exists.

## Pauses

| pause between responses | what the cache lifetime can do |
|---|---|
| ≤ 5 min | both lifetimes can reuse the context; 1 hour only costs more |
| 5–60 min | only the 1-hour lifetime can reuse it |
| > 60 min | neither keeps a session's context just because of its lifetime |

Every cache hit resets the timer. When the agent works on its own, its tool
calls refresh the cache even when you type nothing. So nobody should send a
prompt every five minutes to keep it warm.

`--resume` can reuse the cache only if the entry has not expired and the
context still matches exactly. A model switch, an MCP or tool-definition
change, an upgrade, compaction or expiry can lose the hit, even when the
conversation looks restored.

## Reporting a result

Lead with:

1. the recommendation, and whether it holds in all three scenarios;
2. how much reusable context sits in returns after a 5–60 minute pause, weighted
   by tokens, not counted by pauses;
3. the modelled saving with price coverage, or the saving as a % of input cost
   when coverage is weak;
4. observed 1-hour reads, when there are enough returns to judge;
5. context-change and data-quality caveats;
6. the fit: deep, resumable contexts versus one-shot or continuous work.

On a fixed or quota-based plan, cache savings are not money saved. Describe
them as lower usage or less quota pressure unless billing shows otherwise.

## Interpretation rules

- **Keep observed and simulated apart.** Token fields say what happened; the
  comparison estimates what a different lifetime might have done.
- Call `cache_creation_input_tokens` "cache creation", not "rewritten context".
  Only creation bounded by context growth is likely new.
- A return after a 5–60 minute pause is not a guaranteed hit. The model, tool
  definitions, version or a compaction can still change the context.
- **Lead with the 65.2 % sentence.** Label 39.5 % as the single-return version;
  they are not interchangeable.
- If a model has no price, report tokens and price coverage, never an invented
  dollar figure. Let the user supply their own price file.
- `--resume` alone neither keeps nor destroys the cache. It restores the
  conversation; reuse still needs an unexpired, exactly matching context.
- Recommend 1 hour selectively: when long, expensive contexts often come back
  within the hour. Recommend 5 minutes for short, one-shot or continuously
  active work, where the higher write price is unlikely to pay back.
