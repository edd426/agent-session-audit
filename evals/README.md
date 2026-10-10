# Evaluations

`evals.json` holds four cases in `skill-creator`'s schema format. The top level
has `skill_name` and `evals`; each case has `id`, `prompt`, `expected_output`
and `expectations`.

| id | kind | what it tests |
|---|---|---|
| 1 | trigger | "Where did my tokens go last week?" fires the skill from its description alone, with no tool, path or flag named |
| 2 | non-trigger | "What does prompt caching cost on the API?" must not fire it: that asks about published prices, not this machine's history |
| 3 | end-to-end | "Run the full report and tell me what to change" produces the files and a reading that follows the skill's rules and plain terms |
| 4 | regression | The per-conversation cost analysis (`lane_profile.py`) runs under a working-directory exclusion; an agent once skipped it because that script lacked the folder filters |

These test discovery and driving: does the skill fire when it should, stay
quiet when it should not, and reach a correct result? The unit tests in
`scripts/test_*.py` test the arithmetic instead. Neither replaces the other.

## The model matrix has not been run

Anthropic's authoring checklist asks for tests with Haiku, Sonnet and Opus.
None has been run, and no results are recorded. Do not describe the skill as
tested across models.

When it is run, record the date, exact model ids, cases given, and the outcome
per case. Watch the non-trigger case: a smaller model is most likely to fire on
any mention of cost.

| model | date run | case 1 | case 2 | case 3 | case 4 |
|---|---|---|---|---|---|
| Haiku | not run | — | — | — | — |
| Sonnet | not run | — | — | — | — |
| Opus | not run | — | — | — | — |

## Schema source

The shape follows `skill-creator`'s `references/schemas.md`, read 2026-09-06
through a summarizing fetch, so the field list is reliable but not verbatim.
Anthropic's best-practices page shows a second shape under "Build evaluations
first" (`skills` / `query` / `files` / `expected_behavior`). If a runner rejects
this file, try that one.

## How to run these

There is no bundled runner; Anthropic's guidance is to build your own. By hand,
start a fresh agent session with only this skill installed. Paste the `prompt`
exactly, and check each `expectations` line against what the agent did. Record
the result in the table above.
