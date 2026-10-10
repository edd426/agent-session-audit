# Handoff-log acceptance fixtures

Everything here is synthetic. A real handoff field log (the `--handoff-log`
CSV; format in
[references/orientation-and-threads.md](../../references/orientation-and-threads.md)
§2b) names projects in its `cwd` column. So no real row is ever
committed. These sittings (`/w/handoff-alpha`, `/w/handoff-beta`, …)
exist only in this directory and in the temporary session store the test
builds.

| file | what it is for |
|---|---|
| `handoff-clean.csv` | the happy path: four ends, both arms, in two sittings |
| `handoff-malformed.csv` | the same log with **exactly three** unreadable lines |
| `handoff-pending.csv` | ends logged, **no successor yet** — the real day-one state |
| `handoff-clean.expected.txt` | committed expected output for `handoff-clean.csv` |
| `handoff-pending.expected.txt` | committed expected output for `handoff-pending.csv` |

The session store the CSVs join against is built in a temp directory by
`scripts/test_orientation_profile.py::build_handoff_history()`. So the fixtures
reproduce without a JSONL corpus in the repo. Regenerate the expected files
with:

    python3 scripts/test_orientation_profile.py --regenerate-expected
