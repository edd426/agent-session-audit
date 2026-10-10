#!/usr/bin/env python3
"""Write a synthetic Claude Code history: one invented developer's six weeks.

    python3 demo_history.py --out /tmp/demo
    python3 orientation_profile.py report --store claude-code --root /tmp/demo \\
        --output-dir /tmp/demo-report

The output is a fake Claude Code config root -- `DIR/projects/<encoded-cwd>/
<session-id>.jsonl`, with child-agent transcripts in `<session-id>/subagents/`
-- that every analysis in this skill accepts as `--root DIR` (lane_profile.py,
compact_economics.py and tool_bloat.py read `DIR/projects` the same way).

EVERYTHING IN IT IS INVENTED: a developer under /home/dev/src working on six
neutral projects between 2026-08-24 and 2026-10-03. It exists so the report can
be demonstrated, screenshotted and tested without anyone's real history.
Deterministic: the same seed writes byte-identical files.

Shapes it reproduces, because the parsers depend on them (see
references/log-schema.md and audit_common.py):
  * one JSONL row per content block; the rows of one API response share
    `message.id` and repeat the identical `usage` object;
  * usage carries input/output tokens, cache reads and cache writes with the
    `cache_creation.ephemeral_5m/1h` split, accounted as a growing prompt:
    a warm response reads the previous prompt and writes only what is new; a
    response after an idle gap longer than the TTL writes the whole prompt;
  * `effort` is a top-level field on every main-conversation assistant record;
  * a compaction is a `system`/`compact_boundary` row with `compactMetadata`
    followed by an `isCompactSummary` user row;
  * child agents (Task) run in `<session>/subagents/agent-<id>.jsonl` under
    the parent's `sessionId`, with `isSidechain: true` and an `agentId`.

What the history is built to show, so every chart has something on it:
  work items that return after hours, days and weeks (sittings > 72 h apart);
  sessions that open a HANDOFF/STATUS note in their first response and
  sessions that do not; question-only and review-only sessions with no edit;
  sessions that ask a question first and edit later; edits by Edit/Write and
  by `sed -i`, `mv`, `git mv`, `npm install`; long
  sessions with auto and manual compactions; resumes after several hours;
  subagent runs; three models at several effort levels; a switch from
  5-minute to 1-hour cache writes half-way through; and a few very large tool
  results.

Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import shlex
import shutil
import string
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SEED = 20260824
HOME = "/home/dev/src"
START = dt.datetime(2026, 8, 24, tzinfo=dt.timezone.utc)
MARKER = ".demo-history"            # written at the root; --force only clears a root that has it

OPUS = "claude-opus-5"
OPUS_1M = "claude-opus-5[1m]"       # same model and rate; the 1M-context selection
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"

# Day the user upgraded Claude Code, and the day main-conversation cache
# writes switched from 5-minute to 1-hour (both are visible to the cache audit).
VERSION_BUMP_DAY = 17
ONE_HOUR_TTL_DAY = 21
VERSIONS = ("2.1.255", "2.1.268")

BASE_MAIN = 19_500                  # system prompt + tool schemas + memory
BASE_AGENT = 8_000
AUTO_COMPACT_AT = {OPUS_1M: 900_000}
AUTO_COMPACT_DEFAULT = 165_000
THINKING_SHARE = {"low": 0.08, "medium": 0.22, "high": 0.40, "xhigh": 0.55}

PRICING_FILE = (Path(__file__).resolve().parent.parent
                / "references" / "pricing-public-example.json")

# --------------------------------------------------------------------------
# Text banks. Neutral filler, chosen so a tool result looks like what it is.
# --------------------------------------------------------------------------
BANK = {
    "py": [
        "from __future__ import annotations",
        "import logging",
        "from dataclasses import dataclass, field",
        "from typing import Iterable, Optional",
        "",
        "log = logging.getLogger(__name__)",
        "",
        "@dataclass(frozen=True)",
        "class Policy:",
        "    attempts: int = 3",
        "    backoff_seconds: float = 0.5",
        "def handle(request, *, timeout: float = 5.0):",
        "    if not request.items:",
        "        raise ValueError(\"empty request\")",
        "    for attempt in range(policy.attempts):",
        "        try:",
        "            return client.submit(payload, idempotency_key=key)",
        "        except TimeoutError:",
        "            log.debug(\"attempt %d timed out\", attempt)",
        "    return None",
        "    total = sum(line.price * line.quantity for line in order.lines)",
        "    rows = repository.fetch(since=cursor, limit=page_size)",
        "    if row.key in seen:",
        "        continue",
        "    seen.add(row.key)",
        "def test_returns_none_when_every_attempt_fails(fake_client):",
        "    assert result.status == \"ok\"",
        "    assert len(calls) == 3",
        "class Repository:",
        "    def __init__(self, session):",
        "        self._session = session",
        "    yield from batch",
        "    window = Window(start=event.ts - lateness, end=event.ts)",
        "    return {\"id\": item.id, \"status\": item.status}",
        "    settings = load_settings()",
        "    parser.add_argument(\"--json\", action=\"store_true\")",
    ],
    "ts": [
        "import { useEffect, useState } from 'react';",
        "import type { CartItem, PendingChange } from './types';",
        "",
        "export class CartStore {",
        "  private items = new Map<string, CartItem>();",
        "  constructor(private readonly api: CartApi) {}",
        "  async apply(change: PendingChange): Promise<void> {",
        "    if (!this.online) {",
        "      this.queue.push(change);",
        "      return;",
        "    }",
        "    await this.api.patch(change.itemId, change.patch);",
        "  }",
        "}",
        "export function useConnectivity(): boolean {",
        "  const [online, setOnline] = useState(true);",
        "  useEffect(() => subscribe(setOnline), []);",
        "  return online;",
        "describe('syncQueue', () => {",
        "  it('replays changes in order', async () => {",
        "    expect(api.patch).toHaveBeenCalledTimes(2);",
        "  });",
        "const MAX_ATTEMPTS = 5;",
        "export const linking = { prefixes: ['shop://'], config };",
    ],
    "md": [
        "# Reference",
        "",
        "## Overview",
        "This page describes the endpoints and their parameters.",
        "| Parameter | Type | Description |",
        "|---|---|---|",
        "| `status` | string | Filter by order status. |",
        "See the [install guide](../guides/install.md) for setup.",
        "- [ ] move the remaining pages",
        "- [x] switch the generator",
        "Next step: run the link checker and fix the redirects.",
        "```bash",
        "make check",
        "```",
    ],
    "css": [
        ":root {",
        "  --surface: #ffffff;",
        "  --text-primary: #1f2933;",
        "}",
        "[data-theme=\"dark\"] {",
        "  --surface: #111827;",
        "  --text-primary: #e5e7eb;",
        ".card { background: var(--surface); color: var(--text-primary); }",
        ".chart-tooltip { border: 1px solid var(--border-muted); }",
        "@media (prefers-color-scheme: dark) {",
    ],
    "yaml": [
        "pipeline:",
        "  lateness_hours: 48",
        "  window_minutes: 15",
        "  sinks:",
        "    - warehouse",
        "nav:",
        "  - Reference: reference/index.md",
        "plugins:",
        "  - search",
    ],
    "json": [
        "{",
        "  \"id\": \"ord_000184\",",
        "  \"status\": \"shipped\",",
        "  \"currency\": \"USD\",",
        "  \"total\": 129.90,",
        "  \"shipping_address\": null,",
        "  \"lines\": [{\"sku\": \"SKU-1042\", \"quantity\": 2}]",
        "},",
        "  \"version\": \"7.2.1\",",
        "  \"resolved\": \"https://registry.example.org/pkg/-/pkg-7.2.1.tgz\",",
    ],
    "log": [
        "2026-08-25T02:14:07Z INFO  backfill partition=2026-08-24 rows=184220",
        "2026-08-25T02:14:09Z WARN  late event key=evt_7731 lag_hours=51",
        "2026-08-25T02:14:12Z INFO  sink=warehouse batch=412 written=5000",
        "2026-08-25T02:14:13Z WARN  sink=warehouse batch=413 timeout, retrying",
        "2026-08-25T02:14:15Z INFO  sink=warehouse batch=413 written=5000",
        "2026-08-25T02:14:16Z INFO  dedupe dropped=12 kept=4988",
    ],
    "coverage": [
        "File                 | % Stmts | % Branch | % Funcs | % Lines |",
        "---------------------|---------|----------|---------|---------|",
        "src/theme/tokens.ts  |   96.15 |    87.50 |     100 |   96.15 |",
        "src/components       |   81.42 |    70.00 |   85.71 |   82.06 |",
        "  Chart.tsx          |   78.95 |    66.67 |   83.33 |   79.41 |",
        "src/pages            |   74.30 |    61.54 |   80.00 |   75.12 |",
        "PASS src/theme/__tests__/ThemeProvider.test.tsx",
    ],
}
THINK = [
    "Let me look at how this is wired before changing anything.",
    "The retry has to be idempotent, so the key must be created once per order.",
    "The test fixture probably covers the happy path only.",
    "I should check whether anything else calls this function.",
    "The handoff note says the next step is the config change.",
    "This is a read-only question, so no edits.",
    "Two places build the same key; I will change both.",
    "Running the narrow test first is cheaper than the whole suite.",
    "Every colour needs a light and a dark value.",
]
PROSE = [
    "Here is what I found.",
    "The change is in place and the tests pass.",
    "The function is called from two places, and only one of them handles the timeout.",
    "I would flag the missing test for the empty case and the unbounded retry.",
    "Nothing in the production path still uses it; the admin tooling does.",
    "I updated the note with what is done and what is left.",
    "The cost is in the reader opening every partition instead of the ones in range.",
    "Next step is the reports pages, which reuse the same components.",
    "One open question for you: should the limit be per merchant or global?",
]
COMMITS = ["retry capture on timeout", "add export endpoint", "fix dedupe key",
           "move reference pages", "bump version", "add json output",
           "cap sync retries", "add theme tokens", "pin navigation"]
ALNUM = string.ascii_letters + string.digits


def lang_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".py": "py", ".ts": "ts", ".tsx": "ts", ".md": "md", ".css": "css",
            ".yaml": "yaml", ".yml": "yaml", ".json": "json",
            ".jsonl": "json", ".log": "log", ".toml": "yaml", ".html": "md",
            ".js": "ts"}.get(ext, "md")


def filler(lang: str, tokens: int, salt: int, numbered: bool = True) -> str:
    """About `tokens` tokens (4 characters each) of plausible lines."""
    bank = BANK[lang]
    target = max(16, tokens * 4)
    out, size, i = [], 0, 0
    while size < target:
        line = bank[(i * 7 + salt) % len(bank)]
        text = ("%6d\t%s" % (i + 1, line)) if numbered else line
        out.append(text)
        size += len(text) + 1
        i += 1
    return "\n".join(out)


def sentences(bank: List[str], tokens: int, salt: int) -> str:
    target = max(8, tokens * 4)
    out, size, i = [], 0, 0
    while size < target:
        sentence = bank[(i * 5 + salt) % len(bank)]
        out.append(sentence)
        size += len(sentence) + 1
        i += 1
    return " ".join(out)


def stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def encode_cwd(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


# --------------------------------------------------------------------------
# The fictional month. Projects, work items, then the session schedule.
# --------------------------------------------------------------------------
PROJECTS = {
    "webshop-api": {"test": "pytest -q {tests}", "runner": "pytest"},
    "data-pipeline": {"test": "pytest -q {tests}", "runner": "pytest"},
    "mobile-app": {"test": "npx jest {tests}", "runner": "jest"},
    "docs-site": {"test": "make check", "runner": "links"},
    "cli-tool": {"test": "pytest -q {tests}", "runner": "pytest"},
    "admin-dashboard": {"test": "npx jest {tests}", "runner": "jest"},
}

ITEMS: Dict[str, Dict[str, Any]] = {
    "A": {"project": "webshop-api", "branch": "feature/checkout-retry", "note": "HANDOFF.md",
          "files": ["src/checkout/service.py", "src/checkout/retry.py", "src/payments/client.py",
                    "src/payments/errors.py", "tests/test_checkout.py", "tests/test_retry.py"],
          "tests": "tests/test_checkout.py tests/test_retry.py",
          "open": ["SHOP-142: payment capture sometimes times out at checkout and the order is "
                   "left pending. Add a bounded retry around the capture call, and make sure a "
                   "retry can never charge the customer twice.",
                   "Picking SHOP-142 back up. Read HANDOFF.md and carry on with the "
                   "idempotency-key part.",
                   "SHOP-142: last bits from the handoff note, then push the branch.",
                   "Back on SHOP-142 after the review comments. Read HANDOFF.md first, then "
                   "address the comment about the backoff jitter.",
                   "SHOP-142 came back from QA: when every retry fails, the order still says "
                   "processing. Find out why and fix it.",
                   "Review the SHOP-142 branch before I merge it. Don't change anything, just "
                   "tell me what you would flag."],
          "more": ["Run the checkout tests again and fix whatever fails.",
                   "Also log each attempt with the attempt number, but only at debug level.",
                   "Make the max attempts configurable from settings.",
                   "Add a test for the case where the second attempt succeeds.",
                   "The backoff should have jitter. Keep it simple.",
                   "Check the error classification: a declined card must not be retried."],
          "ask_more": ["Would the retry ever run after the order was cancelled?",
                       "Anything you'd still want tested before merge?"]},
    "B": {"project": "webshop-api", "branch": "feature/order-export", "note": None,
          "files": ["src/orders/export.py", "src/orders/repository.py", "src/api/routes/orders.py",
                    "src/orders/serializers.py", "tests/test_export.py"],
          "tests": "tests/test_export.py",
          "open": ["SHOP-155: add a CSV export endpoint for orders, filterable by date range "
                   "and status. Stream it; some merchants have hundreds of thousands of orders.",
                   "SHOP-155: finish the export endpoint from yesterday. Streaming works; the "
                   "repository query still pages with offsets."],
          "more": ["Some sample orders have no shipping address. Export those as empty columns.",
                   "Add the currency column next to the total.",
                   "Use keyset pagination in the repository instead of offsets.",
                   "The test is slow; use a smaller fixture for the unit test.",
                   "Now add a test for the status filter.",
                   "Write short endpoint docs in the docstring.",
                   "Add a header row option, default on.",
                   "Make the date range inclusive of the end day."]},
    "C-auth": {"project": "webshop-api", "branch": "main", "note": None,
               "files": ["src/auth/middleware.py", "src/auth/tokens.py", "src/auth/clock.py"],
               "open": ["How does the auth middleware decide that a token has expired? I think we "
                        "handle clock skew somewhere but I can't find it."],
               "ask_more": ["Is the skew allowance configurable?",
                            "What happens to a request that arrives exactly at expiry?"]},
    "C-v1": {"project": "webshop-api", "branch": "main", "note": None,
             "files": ["src/payments/legacy_v1.py", "src/jobs/refunds.py", "src/admin/views.py"],
             "open": ["Is anything in the codebase still calling the v1 payments endpoint? I want "
                      "to know before we turn it off."],
             "ask_more": ["Which of those are production paths and which are admin tooling?"]},
    "D": {"project": "data-pipeline", "branch": "fix/late-events", "note": "STATUS.md",
          "files": ["pipeline/dedupe.py", "pipeline/windowing.py", "pipeline/sinks/warehouse.py",
                    "pipeline/events.py", "tests/test_dedupe.py", "config/pipeline.yaml",
                    "tests/test_windowing.py"],
          "tests": "tests/test_dedupe.py tests/test_windowing.py",
          "open": ["Why are we seeing duplicate events in the warehouse after the nightly "
                   "backfill? This is DP-31.",
                   "Continue DP-31 from STATUS.md. The dedupe key change is in; next is the "
                   "watermark for late events.",
                   "DP-31 again. Read STATUS.md; the staging backfill still shows a handful of "
                   "duplicates.",
                   "DP-31: the warehouse sink retries a batch after a timeout and writes it "
                   "twice. Make the write idempotent.",
                   "DP-31 resurfaced in the monthly reconciliation: duplicates for events that "
                   "arrive more than two days late. Investigate and fix."],
          "more": ["OK, fix it the way you described.",
                   "Run the dedupe tests and the windowing tests together.",
                   "Make the lateness allowance a config value, default 48 hours.",
                   "Add a test with an event that arrives after the window has closed.",
                   "Check the sink batch ids are stable across retries.",
                   "Look at the backfill log again and confirm the duplicates are gone.",
                   "Tidy up: the old dedupe helper is unused now."]},
    "E": {"project": "data-pipeline", "branch": "main", "note": None,
          "files": ["jobs/nightly.py", "pipeline/io/parquet_reader.py",
                    "pipeline/io/partitions.py", "tests/test_parquet_reader.py"],
          "tests": "tests/test_parquet_reader.py",
          "open": ["Why did the nightly job go from 20 minutes to almost an hour this week? "
                   "Nothing obvious changed in the job itself.",
                   "Follow-up on the nightly runtime: apply the partition pruning fix to the "
                   "other two readers as well."],
          "more": ["Do it: only read the partitions the job needs.",
                   "Add a test that the reader only opens partitions in range."]},
    "F": {"project": "mobile-app", "branch": "feature/offline-cart", "note": "docs/STATUS.md",
          "files": ["src/cart/CartStore.ts", "src/cart/syncQueue.ts", "src/api/cartApi.ts",
                    "src/cart/__tests__/syncQueue.test.ts", "src/cart/types.ts",
                    "src/net/connectivity.ts"],
          "tests": "src/cart/__tests__/syncQueue.test.ts",
          "open": ["MOB-88: cart changes made while offline are lost when the app comes back "
                   "online. Add a sync queue that replays them in order when connectivity "
                   "returns.",
                   "MOB-88, continue from docs/STATUS.md.",
                   "Back to MOB-88. Read docs/STATUS.md. QA found that two quick edits to the "
                   "same line item replay in the wrong order.",
                   "MOB-88: last item on the status list, then open the PR.",
                   "MOB-88 follow-up from the beta: the queue retries forever if the server "
                   "rejects an item. Cap it and show the error."],
          "more": ["Coalesce consecutive changes to the same item before replaying.",
                   "Persist the queue so it survives an app restart.",
                   "Add a test for two edits to the same line item.",
                   "Show a small banner while changes are pending.",
                   "Run the cart tests and fix what fails.",
                   "Use the connectivity hook instead of polling."]},
    "G-review": {"project": "mobile-app", "branch": "feature/push-permissions", "note": None,
                 "files": ["src/notifications/permissions.ts", "src/notifications/register.ts",
                           "src/screens/Onboarding.tsx"],
                 "open": ["Review the diff on feature/push-permissions before I open the PR. "
                          "Don't edit anything."],
                 "ask_more": ["Is the newer Android runtime permission handled, or only iOS?"]},
    "G-q": {"project": "mobile-app", "branch": "main", "note": None,
            "files": ["app.json", "src/updates/checkForUpdate.ts", "eas.json"],
            "open": ["What does the updates block in app.json actually control? Does it affect "
                     "store builds?"]},
    "H": {"project": "docs-site", "branch": "chore/api-ref-migration", "note": "MIGRATION-PLAN.md",
          "files": ["scripts/gen_api_ref.py", "docs/api/orders.md", "docs/api/payments.md",
                    "docs/api/customers.md", "docs/reference/index.md", "docs/guides/install.md"],
          "tests": "",
          "open": ["Move the API reference pages from docs/api to docs/reference and switch them "
                   "to the new generator. Keep a plan in MIGRATION-PLAN.md as you go; this will "
                   "take a few sittings.",
                   "Continue the API reference migration from MIGRATION-PLAN.md.",
                   "Finish the API reference migration: the last section of MIGRATION-PLAN.md, "
                   "then fix the redirects."],
          "more": ["Do the payments and customers pages the same way.",
                   "Fix the links in the guides that still point at docs/api.",
                   "Regenerate and run the link checker."],
          "muts": ["git mv docs/api/orders.md docs/reference/orders.md",
                   "git mv docs/api/payments.md docs/reference/payments.md",
                   "sed -i 's#(../api/#(../reference/#g' docs/guides/install.md docs/guides/upgrade.md",
                   "mv docs/api/_snippets docs/reference/_snippets",
                   "python3 scripts/gen_api_ref.py --out docs/reference > build/gen.log",
                   "git mv docs/api/customers.md docs/reference/customers.md"]},
    "I-search": {"project": "docs-site", "branch": "main", "note": None,
                 "files": ["overrides/partials/search.html", "docs/javascripts/search-config.js",
                           "site-config.yml"],
                 "open": ["Where do we configure the search index for the docs site? Results "
                          "seem to ignore the reference pages."],
                 "ask_more": ["Would excluding the changelog from the index break anything?"]},
    "I-links": {"project": "docs-site", "branch": "main", "note": None,
                "files": ["docs/guides/faq.md", "docs/guides/upgrade.md", "docs/guides/proxy.md"],
                "tests": "",
                "open": ["The link checker reports eleven broken links in docs/guides. Fix them."],
                "more": ["Two are still broken: the anchors changed case."],
                "muts": ["sed -i 's#/v1/quickstart#/getting-started#g' docs/guides/faq.md",
                         "sed -i 's#(proxy.md#(../proxy.md#g' docs/guides/upgrade.md",
                         "sed -i 's/#Configuring-A-Proxy/#configuring-a-proxy/' docs/guides/proxy.md"]},
    "J": {"project": "cli-tool", "branch": "feature/json-output", "note": None,
          "files": ["cli/commands/status.py", "cli/output.py", "cli/formatting.py",
                    "tests/test_status.py"],
          "tests": "tests/test_status.py",
          "open": ["How is the output of the status command rendered today? CLI-27 asks for a "
                   "--json flag and I want to know how invasive that is.",
                   "CLI-27: finish the --json flag. Yesterday we added the formatter; wire it "
                   "into status and add tests."],
          "more": ["Sounds fine. Add the --json flag the way you suggested.",
                   "Make sure the JSON keys are stable and sorted.",
                   "Add a test that the human output did not change."]},
    "K": {"project": "cli-tool", "branch": "main", "note": None,
          "files": ["pyproject.toml", "CHANGELOG.md", "cli/__init__.py"],
          "tests": "",
          "open": ["Cut the 0.9.0 release: bump the version everywhere, move the Unreleased "
                   "section of CHANGELOG.md under 0.9.0, commit and tag.",
                   "Cut 0.9.1 with the two fixes on main. Same steps as last time."],
          "muts": ["sed -i 's/^version = .*/version = \"{ver}\"/' pyproject.toml",
                   "sed -i 's/^__version__ = .*/__version__ = \"{ver}\"/' cli/__init__.py"]},
    "L": {"project": "admin-dashboard", "branch": "feature/dark-mode", "note": "HANDOFF.md",
          "files": ["src/theme/tokens.ts", "src/theme/ThemeProvider.tsx", "src/styles/global.css",
                    "src/components/Chart.tsx", "src/pages/Reports.tsx", "src/pages/Settings.tsx"],
          "tests": "src/theme",
          "open": ["Add a dark mode to the admin dashboard. Start with colour tokens and a theme "
                   "toggle in settings; the page-by-page sweep comes later. Keep notes in "
                   "HANDOFF.md, this will take a few sittings.",
                   "Continue dark mode from HANDOFF.md: swap the hard-coded colours in the "
                   "components for tokens.",
                   "Dark mode, last sitting. Read HANDOFF.md first; the reports pages and the "
                   "charts are what is left."],
          "more": ["The chart tooltips are unreadable on the dark background. Fix them.",
                   "Follow the system preference until the user picks a theme.",
                   "Run the theme tests.",
                   "Check the contrast of the secondary text."],
          "muts": ["sed -i 's/#1f2933/var(--text-primary)/g' src/components/Chart.tsx "
                   "src/pages/Reports.tsx",
                   "sed -i 's/#ffffff/var(--surface)/g' src/styles/global.css",
                   "git mv src/styles/colors.css src/theme/legacy-colors.css"]},
    "L-q": {"project": "admin-dashboard", "branch": "main", "note": None,
            "files": ["src/api/reportsClient.ts", "src/hooks/useReports.ts",
                      "src/components/Spinner.tsx"],
            "open": ["What does the dashboard do when the reports API is slow? Is there a loading "
                     "state, or does the page just hang?"],
            "ask_more": ["Would a client-side timeout break the CSV download?"]},
    "M": {"project": "webshop-api", "branch": "feature/rate-limit-headers", "note": None,
          "files": ["src/api/middleware/ratelimit.py", "src/api/middleware/headers.py",
                    "tests/test_ratelimit.py", "src/api/limits.py"],
          "tests": "tests/test_ratelimit.py",
          "open": ["SHOP-160: return the standard rate-limit headers on every API response.",
                   "SHOP-160: the headers are wrong for burst limits. Fix and add a test."],
          "more": ["Include the retry-after header on 429 responses only.",
                   "Run the rate limit tests."]},
    "N-schema": {"project": "data-pipeline", "branch": "feature/schema-check", "note": None,
                 "files": ["pipeline/sinks/schema_check.py", "pipeline/sinks/table_schema.yaml",
                           "tests/test_schema_check.py"],
                 "tests": "tests/test_schema_check.py",
                 "open": ["Add a schema check before the warehouse sink writes a batch: reject "
                          "batches whose columns don't match the table."],
                 "more": ["Log the mismatched columns, not the whole batch.",
                          "Run the sink tests."]},
    "N-q": {"project": "data-pipeline", "branch": "main", "note": None,
            "files": ["pipeline/sinks/legacy_csv.py", "jobs/export_csv.py",
                      "config/sinks.yaml"],
            "open": ["Can we drop the legacy CSV sink? Who still uses it?"],
            "ask_more": ["What would we have to change in the jobs config?"]},
    "O": {"project": "mobile-app", "branch": "chore/nav-upgrade", "note": None,
          "files": ["package.json", "src/navigation/RootNavigator.tsx", "src/navigation/linking.ts",
                    "src/screens/Settings.tsx"],
          "tests": "src/navigation",
          "open": ["Upgrade the navigation library to the next major version and fix whatever "
                   "breaks.",
                   "Nav upgrade: deep links still break on Android. Continue from where we "
                   "stopped."],
          "more": ["The settings screen header is gone after the upgrade. Fix it.",
                   "Run the navigation tests.",
                   "Check the linking config against the new API."],
          "muts": ["npm install @react-navigation/native@7 @react-navigation/native-stack@7"]},
    "P": {"project": "cli-tool", "branch": "main", "note": None,
          "files": ["cli/main.py", "cli/plugins.py", "cli/config.py", "tests/test_plugins.py"],
          "tests": "tests/test_plugins.py",
          "open": ["Why does the status command take two seconds on a cold start?"],
          "more": ["Lazy-load the plugins then."]},
    "Q": {"project": "docs-site", "branch": "main", "note": None,
          "files": ["docs/cli/setup-cli.md", "docs/cli/commands.md", "docs/home.md"],
          "tests": "",
          "open": ["Write a getting-started page for the CLI and link it from the home page."],
          "more": ["Shorter, please. Two screens at most."],
          "new_file": "docs/getting-started.md"},
    "R": {"project": "webshop-api", "branch": "feature/refund-webhook", "note": None,
          "files": ["src/webhooks/refunds.py", "src/webhooks/signature.py",
                    "tests/test_webhook_signature.py", "src/webhooks/router.py"],
          "tests": "tests/test_webhook_signature.py",
          "open": ["SHOP-171: verify the signature on incoming refund webhooks and reject "
                   "anything older than five minutes.",
                   "SHOP-171: the signature check from yesterday rejects valid retries from the "
                   "provider. Fix it without loosening the replay window."],
          "more": ["Compare signatures in constant time.",
                   "Add tests for an expired timestamp and a tampered body.",
                   "Log rejected webhooks with the reason, not the payload.",
                   "Accept both the current and the previous signing secret during rotation.",
                   "Run the webhook tests."]},
    "S": {"project": "mobile-app", "branch": "fix/camera-crash", "note": None,
          "files": ["src/screens/ScanReceipt.tsx", "src/camera/useCamera.ts",
                    "android/app/src/main/AndroidManifest.xml",
                    "src/camera/__tests__/useCamera.test.ts"],
          "tests": "src/camera",
          "open": ["Why does the app crash on some Android phones when the receipt scanner "
                   "opens the camera? MOB-97 has the crash log.",
                   "MOB-97: ship the camera permission fix and add a test."],
          "more": ["Go ahead with the fix you suggested.",
                   "Handle the user denying the permission twice."]},
}

BIG = {   # very large tool results, so tool_bloat has a leader
    "orders-fixture": ("Read", "fixtures/orders_sample.json", 38_000),
    "backfill-log": ("Bash", "cat logs/backfill-2026-08-25.log", 31_000),
    "late-events": ("Read", "tests/fixtures/late_events.jsonl", 26_000),
    "lockfile": ("Read", "package-lock.json", 26_000),
    "coverage": ("Bash", "npx jest --coverage", 22_000),
}


def S(item: str, day: int, at: str, model: str, effort: str, kind: str, **extra) -> Dict[str, Any]:
    spec = {"item": item, "day": day, "at": at, "model": model, "effort": effort, "kind": kind,
            "reads": 6, "edits": 3, "prompts": 4, "agents": 0, "note_read": False,
            "note_write": False, "commit": False, "fail": False, "long": False,
            "pause": None, "idle": None, "manual_compact": None, "big": [], "ask": 0,
            "ver": None, "per_prompt": (5, 9), "switch": None, "edit_miss": False}
    spec.update(extra)
    return spec


# kind: edit | question (no edit) | review (no edit) | qte (question, then edit) | scripted
SCHEDULE = [
    S("A", 0, "09:10", OPUS, "high", "edit", reads=7, edits=4, prompts=4, agents=1, note_write=True),
    S("A", 0, "14:30", OPUS, "high", "edit", ask=1, note_read=True, note_write=True, reads=3, fail=True,
      edit_miss=True),
    S("A", 1, "10:05", SONNET, "medium", "edit", ask=2, note_read=True, reads=2, edits=2, prompts=2, commit=True),
    S("D", 1, "13:30", OPUS, "high", "qte", reads=9, agents=1, note_write=True),
    S("D", 2, "09:15", OPUS_1M, "high", "edit", ask=1, note_read=True, note_write=True, long=True,
      prompts=10, reads=6, big=[(0, "backfill-log"), (2, "late-events")], pause=(5, 4.1),
      manual_compact=5, per_prompt=(12, 16)),
    S("F", 2, "15:00", OPUS, "high", "edit", reads=8, edits=4, prompts=4, agents=2, note_write=True),
    S("B", 3, "13:00", OPUS, "high", "edit", long=True, prompts=12, reads=8, edits=4, agents=2,
      big=[(0, "orders-fixture")], idle=(7, 72)),
    S("B", 4, "09:30", OPUS, "medium", "edit", ask=1, reads=6, commit=True),
    S("H", 4, "14:00", OPUS, "medium", "scripted", reads=4, note_write=True),
    S("F", 5, "10:30", OPUS, "high", "edit", ask=1, note_read=True, note_write=True, reads=3),
    S("H", 6, "10:00", OPUS, "medium", "scripted", ask=1, note_read=True, note_write=True, commit=True),
    S("C-auth", 7, "16:20", SONNET, "medium", "question", reads=6),
    S("G-review", 8, "11:15", OPUS, "medium", "review", reads=5, prompts=2),
    S("A", 9, "09:40", OPUS, "high", "edit", ask=3, note_read=True, note_write=True, long=True,
      prompts=9, reads=4, pause=(4, 2.6)),
    S("E", 10, "08:50", OPUS, "high", "qte", reads=8, edits=2),
    S("L-q", 10, "15:30", OPUS, "medium", "question", reads=4, prompts=2),
    S("E", 11, "09:30", OPUS, "medium", "edit", ask=1, reads=4, prompts=2, commit=True),
    S("I-search", 12, "09:00", SONNET, "low", "question", reads=5, prompts=2),
    S("J", 13, "10:20", OPUS, "high", "qte", reads=7, edits=2),
    S("J", 14, "09:10", OPUS, "high", "edit", ask=1, reads=5, edits=4, commit=True, fail=True,
      switch=(3, SONNET, "medium")),
    S("F", 15, "13:00", OPUS, "high", "edit", ask=2, note_read=True, note_write=True, long=True,
      prompts=9, reads=5, edits=4, agents=1, big=[(1, "lockfile")], pause=(3, 3.2)),
    S("F", 16, "09:00", SONNET, "medium", "edit", ask=3, note_read=True, reads=2, edits=2,
      prompts=2, commit=True),
    S("L", 16, "13:00", OPUS, "xhigh", "edit", reads=8, edits=4, prompts=4, note_write=True,
      pause=(2, 1.6)),
    S("L", 17, "09:30", OPUS, "high", "scripted", ask=1, note_read=True, note_write=True),
    S("H", 18, "15:30", SONNET, "medium", "scripted", ask=2, note_read=True, prompts=2, commit=True),
    S("O", 19, "10:00", OPUS, "high", "edit", reads=6, edits=5, prompts=8, long=True,
      edit_miss=True),
    S("O", 20, "09:45", OPUS, "medium", "edit", ask=1, reads=5, commit=True),
    S("K", 20, "16:00", SONNET, "medium", "scripted", prompts=1, reads=2, commit=True, ver="0.9.0"),
    S("D", 21, "10:00", OPUS, "high", "edit", ask=2, note_read=True, note_write=True, reads=3,
      commit=True),
    S("D", 22, "14:00", SONNET, "high", "edit", ask=3, reads=6, edits=2, prompts=2, commit=True),
    S("A", 23, "15:00", OPUS, "xhigh", "edit", ask=4, reads=14, commit=True),
    S("A", 24, "09:20", OPUS, "high", "review", ask=5, reads=6, prompts=2),
    S("N-schema", 25, "11:00", OPUS, "medium", "edit", reads=5, commit=True),
    S("G-q", 26, "10:40", SONNET, "low", "question", reads=3, prompts=1),
    S("I-links", 27, "13:30", OPUS, "medium", "scripted", reads=3, prompts=2, commit=True),
    S("L", 28, "10:00", OPUS, "high", "scripted", ask=2, note_read=True, note_write=True, long=True,
      prompts=8, reads=5, big=[(0, "coverage")], commit=True, per_prompt=(1, 3)),
    S("C-v1", 29, "11:00", OPUS, "low", "question", reads=6, agents=1, prompts=2),
    S("S", 29, "14:00", OPUS, "high", "qte", reads=7, edits=2, agents=1),
    S("C-auth", 30, "09:40", SONNET, "medium", "question", reads=4, prompts=2),
    S("S", 30, "10:15", OPUS, "high", "edit", ask=1, reads=5, commit=True),
    S("M", 31, "14:10", OPUS, "high", "edit", reads=6),
    S("F", 32, "16:00", OPUS, "high", "edit", ask=4, reads=9, commit=True),
    S("M", 33, "09:50", SONNET, "medium", "edit", ask=1, reads=4, edits=2, prompts=2, commit=True,
      fail=True),
    S("K", 34, "11:00", SONNET, "medium", "scripted", ask=1, prompts=1, reads=2, commit=True,
      ver="0.9.1"),
    S("R", 35, "10:00", OPUS, "high", "edit", reads=7, prompts=7, long=True, idle=(3, 38)),
    S("R", 36, "14:30", OPUS, "high", "edit", ask=1, reads=5, commit=True, fail=True),
    S("D", 36, "09:00", OPUS, "high", "edit", ask=4, reads=12, agents=1, commit=True),
    S("P", 37, "10:30", OPUS, "medium", "qte", reads=5, edits=2, prompts=2),
    S("N-q", 38, "15:00", SONNET, "medium", "question", reads=4, prompts=2),
    S("Q", 39, "13:00", OPUS, "medium", "edit", reads=4, edits=1, prompts=2, commit=True),
]


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------
class Generator:
    def __init__(self, seed: int = SEED):
        self.rng = random.Random(seed)
        self.file_sizes: Dict[Tuple[str, str], int] = {}
        self.totals: Dict[str, Dict[str, int]] = {}
        self.counts = {"sessions": 0, "responses": 0, "rows": 0, "compactions": 0,
                       "subagents": 0, "note_reads": 0, "no_edit": 0}

    def rid(self, n: int) -> str:
        return "".join(self.rng.choice(ALNUM) for _ in range(n))

    def uuid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def size_of(self, project: str, path: str) -> int:
        key = (project, path)
        if key not in self.file_sizes:
            low, high = {"md": (500, 2200), "css": (400, 2000), "yaml": (200, 900),
                         "json": (300, 1500)}.get(lang_of(path), (1200, 6500))
            self.file_sizes[key] = self.rng.randint(low, high)
        return self.file_sizes[key]

    def tally(self, model: str, usage: Dict[str, Any]) -> None:
        row = self.totals.setdefault(model, {"in": 0, "out": 0, "read": 0, "w5": 0, "w1": 0})
        creation = usage.get("cache_creation") or {}
        row["in"] += usage["input_tokens"]
        row["out"] += usage["output_tokens"]
        row["read"] += usage["cache_read_input_tokens"]
        row["w5"] += creation.get("ephemeral_5m_input_tokens", 0)
        row["w1"] += creation.get("ephemeral_1h_input_tokens", 0)


class Lane:
    """One agent conversation: the main thread, or one child agent."""

    def __init__(self, sb: "SessionBuilder", agent: Optional[str], model: str,
                 effort: Optional[str], ttl: str, base: int):
        self.sb = sb
        self.agent = agent
        self.model = model
        self.effort = effort
        self.ttl = ttl
        self.rows: List[Dict[str, Any]] = []
        self.ctx = 0                 # prompt tokens of the last request
        self.pending = base          # tokens added since then
        self.cached = 0              # prefix the cache holds right now
        self.last_call: Optional[dt.datetime] = None
        self.parent: Optional[str] = None

    def head(self) -> Dict[str, Any]:
        sb = self.sb
        row = {"parentUuid": self.parent, "isSidechain": self.agent is not None,
               "userType": "external", "cwd": sb.cwd, "sessionId": sb.sid,
               "version": sb.version, "gitBranch": sb.branch}
        if self.agent:
            row["agentId"] = self.agent
        return row

    def emit(self, row: Dict[str, Any]) -> None:
        self.rows.append(row)
        self.parent = row.get("uuid") or self.parent
        self.sb.gen.counts["rows"] += 1

    def user(self, text: str) -> None:
        g = self.sb.gen
        row = self.head()
        row.update({"type": "user", "message": {"role": "user", "content": text},
                    "uuid": g.uuid(), "timestamp": stamp(self.sb.now)})
        self.emit(row)
        self.pending += len(text) // 4 + 12

    def respond(self, tools: List[Dict[str, Any]], text_tokens: int = 0,
                extra_out: int = 0) -> None:
        """One API response: thinking, optional text, tool calls -- one row per block.

        Each tool is {name, input, result, tokens, is_error, runtime}.
        """
        sb, g, rng = self.sb, self.sb.gen, self.sb.gen.rng
        if self.agent is None:
            sb.maybe_auto_compact()
        request_at = sb.now
        prompt = self.ctx + self.pending
        ttl_s = 300 if self.ttl == "5m" else 3600
        warm = self.last_call is not None and (request_at - self.last_call).total_seconds() <= ttl_s
        inp = rng.randint(1, 9)
        read = min(self.cached, prompt - inp) if warm else 0
        write = max(0, prompt - inp - read)
        tool_out = sum(rng.randint(40, 160) for _ in tools)
        visible = text_tokens + tool_out + extra_out
        share = THINKING_SHARE.get(self.effort or "", 0.0) if self.model != HAIKU else 0.0
        think = int(visible * share / (1 - share)) if share else 0
        out = visible + think + rng.randint(5, 30)
        usage = {"input_tokens": inp, "cache_creation_input_tokens": write,
                 "cache_read_input_tokens": read, "output_tokens": out,
                 "cache_creation": {"ephemeral_5m_input_tokens": write if self.ttl == "5m" else 0,
                                    "ephemeral_1h_input_tokens": write if self.ttl == "1h" else 0},
                 "service_tier": "standard"}
        g.tally(self.model, usage)
        g.counts["responses"] += 1
        self.cached = prompt - inp
        self.last_call = request_at
        self.ctx = prompt
        self.pending = out
        sb.now = request_at + dt.timedelta(seconds=1.5 + out / rng.uniform(45, 80))

        blocks: List[Dict[str, Any]] = []
        salt = rng.randint(0, 999)
        if think:
            blocks.append({"type": "thinking", "thinking": sentences(THINK, think, salt),
                           "signature": ""})
        if text_tokens:
            blocks.append({"type": "text", "text": sentences(PROSE, text_tokens, salt)})
        ids = []
        for tool in tools:
            use_id = "toolu_01" + g.rid(22)
            ids.append(use_id)
            blocks.append({"type": "tool_use", "id": use_id, "name": tool["name"],
                           "input": tool["input"]})
        message_id = "msg_01" + g.rid(22)
        request_id = "req_011C" + g.rid(18)
        moment = sb.now
        for index, block in enumerate(blocks):
            row = self.head()
            last = index == len(blocks) - 1
            row.update({"message": {"id": message_id, "type": "message", "role": "assistant",
                                    "model": self.model, "content": [block],
                                    "stop_reason": ("tool_use" if tools else "end_turn") if last else None,
                                    "stop_sequence": None, "usage": usage},
                        "requestId": request_id, "type": "assistant", "uuid": g.uuid(),
                        "timestamp": stamp(moment + dt.timedelta(milliseconds=40 * index))})
            if self.effort and self.agent is None:
                row["effort"] = self.effort
            self.emit(row)
        sb.now = moment + dt.timedelta(milliseconds=40 * len(blocks))
        for use_id, tool in zip(ids, tools):
            if tool["name"] == "Task":
                result_text = sb.run_agent(tool)
                content: Any = [{"type": "text", "text": result_text}]
                tokens = len(result_text) // 4
            else:
                sb.now += dt.timedelta(seconds=tool.get("runtime", rng.uniform(0.1, 1.5)))
                content = tool["result"]
                tokens = tool["tokens"]
            row = self.head()
            block = {"tool_use_id": use_id, "type": "tool_result", "content": content}
            if tool.get("is_error"):
                block["is_error"] = True
            row.update({"type": "user", "message": {"role": "user", "content": [block]},
                        "uuid": g.uuid(), "timestamp": stamp(sb.now),
                        "toolUseResult": tool.get("meta") or {"type": "text"}})
            self.emit(row)
            self.pending += tokens + 8


class SessionBuilder:
    def __init__(self, gen: Generator, spec: Dict[str, Any]):
        self.gen, self.spec = gen, spec
        rng = gen.rng
        self.item = ITEMS[spec["item"]]
        self.project = self.item["project"]
        self.cwd = "%s/%s" % (HOME, self.project)
        self.branch = self.item["branch"]
        self.sid = gen.uuid()
        self.version = VERSIONS[1] if spec["day"] >= VERSION_BUMP_DAY else VERSIONS[0]
        self.ttl = "1h" if spec["day"] >= ONE_HOUR_TTL_DAY else "5m"
        hour, minute = (int(x) for x in spec["at"].split(":"))
        self.now = START + dt.timedelta(days=spec["day"], hours=hour, minutes=minute,
                                        seconds=rng.randint(0, 59))
        self.main = Lane(self, None, spec["model"], spec["effort"], self.ttl, BASE_MAIN)
        self.agents: Dict[str, Lane] = {}
        self.read_files: List[str] = []
        self.compactions = 0
        self.edited = False
        self.mut_index = 0

    # -- paths and tool payloads ---------------------------------------------
    def abs(self, rel: str) -> str:
        return "%s/%s" % (self.cwd, rel)

    def tool_read(self, rel: str, tokens: Optional[int] = None) -> Dict[str, Any]:
        tokens = tokens or self.gen.size_of(self.project, rel)
        body = filler(lang_of(rel), tokens, len(rel))
        lines = body.count("\n") + 1
        if rel not in self.read_files:
            self.read_files.append(rel)
        return {"name": "Read", "input": {"file_path": self.abs(rel)}, "result": body,
                "tokens": tokens, "meta": {"type": "text", "file": {
                    "filePath": self.abs(rel), "numLines": lines, "startLine": 1,
                    "totalLines": lines}}}

    def tool_grep(self, word: str) -> Dict[str, Any]:
        rng = self.gen.rng
        files = self.item["files"]
        hits = ["%s:%d:%s" % (self.abs(files[i % len(files)]), rng.randint(5, 400),
                              BANK[lang_of(files[i % len(files)])][i % 9])
                for i in range(rng.randint(4, 30))]
        body = "\n".join(hits)
        # A subdirectory, not the repo root: the thread join reads a Grep `path`
        # as a touched file, and the root's basename would be shared by every
        # session in the project.
        payload = {"pattern": word, "output_mode": "content"}
        if os.path.dirname(files[0]):
            payload["path"] = self.abs(os.path.dirname(files[0]))
        return {"name": "Grep", "input": payload, "result": body, "tokens": len(body) // 4}

    def tool_glob(self, pattern: str) -> Dict[str, Any]:
        body = "\n".join(self.abs(f) for f in self.item["files"])
        return {"name": "Glob", "input": {"pattern": pattern},
                "result": body, "tokens": len(body) // 4}

    def tool_bash(self, command: str, body: str, is_error: bool = False,
                  runtime: Optional[float] = None) -> Dict[str, Any]:
        tool = {"name": "Bash", "input": {"command": command,
                                          "description": "Run " + command.split()[0]},
                "result": body, "tokens": len(body) // 4, "is_error": is_error,
                "meta": {"stdout": "", "stderr": "", "interrupted": False, "isImage": False}}
        if runtime is not None:
            tool["runtime"] = runtime
        return tool

    def tool_edit(self, rel: str) -> Dict[str, Any]:
        rng = self.gen.rng
        lang = lang_of(rel)
        old = filler(lang, rng.randint(20, 120), rng.randint(0, 99), numbered=False)
        new = filler(lang, rng.randint(40, 260), rng.randint(0, 99), numbered=False)
        body = ("The file %s has been updated. Here's the result of running `cat -n` on a "
                "snippet of the edited file:\n%s" % (self.abs(rel),
                                                     filler(lang, rng.randint(120, 450), len(rel))))
        self.edited = True
        return {"name": "Edit", "input": {"file_path": self.abs(rel), "old_string": old,
                                          "new_string": new},
                "result": body, "tokens": len(body) // 4,
                "meta": {"filePath": self.abs(rel)}, "out": (len(old) + len(new)) // 4}

    def tool_write(self, rel: str, tokens: int) -> Dict[str, Any]:
        content = filler(lang_of(rel), tokens, len(rel), numbered=False)
        self.edited = True
        body = "File created successfully at: %s" % self.abs(rel)
        return {"name": "Write", "input": {"file_path": self.abs(rel), "content": content},
                "result": body, "tokens": len(body) // 4, "meta": {"type": "create"},
                "out": tokens}

    def tool_test(self, fail: bool) -> Dict[str, Any]:
        rng = self.gen.rng
        runner = PROJECTS[self.project]["runner"]
        command = PROJECTS[self.project]["test"].format(tests=self.item.get("tests", "")).strip()
        n = rng.randint(8, 40)
        if runner == "pytest":
            body = ("FAILED %s::test_case_%d - AssertionError: assert 2 == 3\n1 failed, %d passed "
                    "in %.2fs" % (self.item.get("tests", "tests").split()[0], rng.randint(1, 9),
                                  n - 1, rng.uniform(0.8, 6))
                    if fail else "%s\n%d passed in %.2fs" % ("." * n, n, rng.uniform(0.8, 6)))
        elif runner == "jest":
            body = ("FAIL %s\n  Expected: 3\n  Received: 2\nTests: 1 failed, %d passed" %
                    (self.item.get("tests", "src"), n - 1)
                    if fail else "PASS %s\nTests: %d passed, %d total" %
                    (self.item.get("tests", "src"), n, n))
        else:
            body = "Checked %d pages, %d links: 0 broken" % (n * 6, n * 40)
        if fail:
            body += "\n" + filler(lang_of(self.item["files"][0]), rng.randint(300, 900), 3)
        return self.tool_bash(command, body, is_error=fail, runtime=rng.uniform(3, 40))

    def tool_task(self, kind: str) -> Dict[str, Any]:
        description = {"Explore": "Map the code paths involved",
                       "general-purpose": "Draft a plan for the change"}[kind]
        prompt = ("%s for this task in %s. Report file paths and the functions involved; "
                  "do not edit anything." % (description, self.project))
        return {"name": "Task", "input": {"description": description, "prompt": prompt,
                                          "subagent_type": kind}}

    # -- conversation steps --------------------------------------------------
    def maybe_auto_compact(self) -> None:
        limit = AUTO_COMPACT_AT.get(self.main.model, AUTO_COMPACT_DEFAULT)
        if self.main.ctx + self.main.pending > limit:
            self.compact("auto")

    def compact(self, trigger: str) -> None:
        g, rng, lane = self.gen, self.gen.rng, self.main
        pre = lane.ctx + lane.pending
        summary_tokens = rng.randint(3_500, 7_500)
        duration = rng.uniform(25, 70)
        boundary = lane.head()
        boundary.update({"parentUuid": None, "logicalParentUuid": lane.parent, "type": "system",
                         "subtype": "compact_boundary", "content": "Conversation compacted",
                         "isMeta": False, "timestamp": stamp(self.now), "uuid": g.uuid(),
                         "level": "info",
                         "compactMetadata": {"trigger": trigger, "preTokens": pre,
                                             "postTokens": BASE_MAIN + summary_tokens,
                                             "durationMs": int(duration * 1000)}})
        lane.emit(boundary)
        self.now += dt.timedelta(seconds=duration)
        summary = lane.head()
        summary.update({"type": "user", "isCompactSummary": True,
                        "isVisibleInTranscriptOnly": True,
                        "message": {"role": "user", "content":
                                    "This session is being continued from a previous "
                                    "conversation that ran out of context. The conversation is "
                                    "summarized below:\n" + sentences(PROSE, summary_tokens, 7)},
                        "uuid": g.uuid(), "timestamp": stamp(self.now)})
        lane.emit(summary)
        # The summarising call refreshed the system prefix; the rest is new.
        lane.ctx = BASE_MAIN
        lane.cached = BASE_MAIN
        lane.last_call = self.now
        lane.pending = summary_tokens
        self.compactions += 1
        g.counts["compactions"] += 1

    def think_gap(self, prompt_index: int) -> None:
        rng, spec = self.gen.rng, self.spec
        if spec["pause"] and spec["pause"][0] == prompt_index:
            seconds = spec["pause"][1] * 3600 + rng.uniform(0, 600)
        elif spec["idle"] and spec["idle"][0] == prompt_index:
            seconds = spec["idle"][1] * 60 + rng.uniform(0, 60)
        else:
            roll = rng.random()
            if roll < 0.70:
                seconds = rng.uniform(25, 240)
            elif roll < 0.82:
                seconds = rng.uniform(240, 299)
            else:
                seconds = rng.uniform(6 * 60, 48 * 60)
        self.now += dt.timedelta(seconds=seconds)
        if spec["switch"] and spec["switch"][0] == prompt_index:
            self.main.model, self.main.effort = spec["switch"][1], spec["switch"][2]
            self.main.cached = 0
        if spec["manual_compact"] == prompt_index:
            self.compact("manual")
            self.now += dt.timedelta(seconds=rng.uniform(20, 90))

    def first_response(self) -> None:
        """The first two main responses -- the window the handoff detector reads.

        A session that opens its note does so in the first response. One that
        does not starts with two searches: reading a source file this early
        would count as opening a handoff whenever the previous session in the
        thread happened to write that file last.
        """
        rng, note = self.gen.rng, self.item.get("note")
        if self.spec["note_read"] and note:
            tools = [self.tool_read(note, rng.randint(900, 2200))]
            if rng.random() < 0.5:
                tools.append(self.tool_bash("git status --short", " M " + self.item["files"][0]))
            self.gen.counts["note_reads"] += 1
            self.main.respond(tools, text_tokens=rng.randint(0, 60))
            return
        stem = os.path.splitext(os.path.basename(self.item["files"][0]))
        if rng.random() < 0.5:
            body = "\n".join("%07x %s" % (rng.getrandbits(28), rng.choice(COMMITS))
                             for _ in range(15))
            self.main.respond([self.tool_bash("git log --oneline -15", body)],
                              text_tokens=rng.randint(0, 60))
        else:
            self.main.respond([self.tool_glob("**/*%s" % stem[1])], text_tokens=rng.randint(0, 60))
        self.main.respond([self.tool_grep(stem[0])], text_tokens=rng.randint(0, 40))

    def read_phase(self, count: int, prompt_index: int, agents: int = 0) -> None:
        rng = self.gen.rng
        files = self.item["files"]
        big = [name for (at, name) in self.spec["big"] if at == prompt_index]
        steps: List[Dict[str, Any]] = []
        for i in range(count):
            unread = [f for f in files if f not in self.read_files]
            roll = rng.random()
            if roll < 0.62 or not steps:
                target = unread[0] if unread else rng.choice(files)
                if target not in self.read_files:
                    self.read_files.append(target)
                steps.append({"kind": "read", "path": target})
            elif roll < 0.80:
                steps.append({"kind": "grep"})
            elif roll < 0.88:
                steps.append({"kind": "glob"})
            else:
                steps.append({"kind": "sed", "path": rng.choice(files)})
        position = 0
        while position < len(steps) or big or agents:
            batch: List[Dict[str, Any]] = []
            if big:
                tool, target, tokens = BIG[big.pop(0)]
                batch.append(self.tool_read(target, tokens) if tool == "Read" else
                             self.tool_bash(target, filler("coverage" if "coverage" in target
                                                           else "log", tokens, 1, numbered=False),
                                            runtime=rng.uniform(0.5, 4)))
            elif agents and (position >= len(steps) // 2 or position >= len(steps)):
                agents -= 1
                kind = "Explore" if rng.random() < 0.7 else "general-purpose"
                batch.append(self.tool_task(kind))
            else:
                for step in steps[position:position + rng.choice((1, 1, 2, 3))]:
                    position += 1
                    if step["kind"] == "read":
                        batch.append(self.tool_read(step["path"]))
                    elif step["kind"] == "grep":
                        batch.append(self.tool_grep(rng.choice(("retry", "timeout", "status",
                                                                "window", "export", "queue"))))
                    elif step["kind"] == "glob":
                        batch.append(self.tool_glob("**/*%s" % os.path.splitext(files[0])[1]))
                    else:
                        size = self.gen.size_of(self.project, step["path"]) // 3
                        batch.append(self.tool_bash(
                            "sed -n '1,160p' %s" % step["path"],
                            filler(lang_of(step["path"]), size, 2, numbered=False)))
                if not batch:
                    continue
            self.main.respond(batch, text_tokens=rng.choice((0, 0, 40, 120)))

    def work_phase(self, edits: int, mut_count: int, fail: bool) -> None:
        rng = self.gen.rng
        files = [f for f in self.item["files"]
                 if not f.endswith(".md") or self.spec["kind"] == "scripted"] or self.item["files"]
        muts = list(self.item.get("muts") or [])
        done = 0
        if self.spec["edit_miss"] and not self.edited:
            miss = self.tool_edit(rng.choice(files))
            miss.update(result="<tool_use_error>String to replace not found in file.</tool_use_error>",
                        tokens=20, is_error=True)
            self.main.respond([miss], text_tokens=20, extra_out=miss["out"])
            self.edited = False
            self.main.respond([self.tool_read(rng.choice(files))], text_tokens=30)
        while done < edits:
            take = min(edits - done, rng.choice((1, 1, 2)))
            batch = []
            for _ in range(take):
                target = rng.choice(files)
                batch.append(self.tool_edit(target))
            self.main.respond(batch, text_tokens=rng.choice((0, 30, 80)),
                              extra_out=sum(t.get("out", 0) for t in batch))
            done += take
        for _ in range(mut_count):
            if not muts:
                break
            command = muts[self.mut_index % len(muts)].format(ver=self.spec["ver"] or "0.9.0")
            self.mut_index += 1
            body = "" if not command.startswith("npm") else filler(
                "log", rng.randint(80, 600), 4, numbered=False)
            self.main.respond([self.tool_bash(command, body, runtime=rng.uniform(0.2, 25))],
                              text_tokens=rng.choice((0, 40)))
            self.edited = True
        if rng.random() < 0.4:
            stat = "\n".join(" %s | %d %s" % (f, rng.randint(2, 60), "+" * rng.randint(2, 30))
                             for f in files[:rng.randint(1, len(files))])
            self.main.respond([self.tool_bash("git diff --stat", stat)], text_tokens=0)
        if self.item.get("tests") is not None:
            self.main.respond([self.tool_test(fail)], text_tokens=rng.choice((0, 40)))
            if fail:
                self.main.respond([self.tool_read(rng.choice(self.item["files"]))],
                                  text_tokens=rng.randint(60, 200))
                fix = self.tool_edit(rng.choice(files))
                self.main.respond([fix], text_tokens=40, extra_out=fix["out"])
                self.main.respond([self.tool_test(False)], text_tokens=0)

    def answer(self, low: int, high: int) -> None:
        self.main.respond([], text_tokens=self.gen.rng.randint(low, high))

    def finish(self) -> None:
        rng, spec, note = self.gen.rng, self.spec, self.item.get("note")
        if spec["note_write"] and note:
            write = self.tool_write(note, rng.randint(500, 1300))
            self.main.respond([write], text_tokens=30, extra_out=write["out"])
        if self.item.get("new_file"):
            write = self.tool_write(self.item["new_file"], rng.randint(900, 1800))
            self.main.respond([write], text_tokens=30, extra_out=write["out"])
        if spec["commit"]:
            message = "%s: %s" % (spec["item"], rng.choice(COMMITS))
            self.main.respond([self.tool_bash(
                "git add -A && git commit -m '%s'" % message,
                "[%s %07x] %s\n %d files changed, %d insertions(+), %d deletions(-)"
                % (self.branch, rng.getrandbits(28), message, rng.randint(2, 7),
                   rng.randint(20, 240), rng.randint(2, 90)))], text_tokens=20)
            if spec["item"] == "K":
                self.main.respond([self.tool_bash("git tag v%s" % spec["ver"], "")], text_tokens=10)

    # -- child agents ----------------------------------------------------------
    def run_agent(self, tool: Dict[str, Any]) -> str:
        g, rng = self.gen, self.gen.rng
        kind = tool["input"]["subagent_type"]
        agent_id = "a%015x" % rng.getrandbits(60)
        model, effort, base = ((HAIKU, None, BASE_AGENT) if kind == "Explore"
                               else (SONNET, "medium", BASE_AGENT + 4_000))
        lane = Lane(self, agent_id, model, effort, "5m", base)
        self.agents[agent_id] = lane
        g.counts["subagents"] += 1
        self.now += dt.timedelta(seconds=rng.uniform(0.5, 2))
        lane.user(tool["input"]["prompt"])
        others = self.item["files"]
        for _ in range(rng.randint(5, 12)):
            batch = []
            for _ in range(rng.choice((1, 2, 3))):
                roll = rng.random()
                if roll < 0.6:
                    batch.append(self.tool_read(rng.choice(others)))
                elif roll < 0.85:
                    batch.append(self.tool_grep(rng.choice(("client", "error", "config", "test"))))
                else:
                    batch.append(self.tool_glob("**/*"))
            lane.respond(batch, text_tokens=rng.choice((0, 30)))
            self.now += dt.timedelta(seconds=rng.uniform(1, 6))
        final = rng.randint(700, 1500)
        lane.respond([], text_tokens=final)
        return sentences(PROSE, final, 11)

    # -- one session -----------------------------------------------------------
    def run(self) -> None:
        g, rng, spec, item = self.gen, self.gen.rng, self.spec, self.item
        kind, prompts = spec["kind"], spec["prompts"]
        opening = item["open"][spec["ask"] % len(item["open"])]
        more = list(item.get("more") or [])
        ask_more = list(item.get("ask_more") or more or ["Anything else worth knowing?"])
        long_session = spec["long"]
        agents_left = spec["agents"]
        for p in range(prompts):
            if p:
                self.think_gap(p)
                if kind in ("question", "review"):
                    text = ask_more[(p - 1) % len(ask_more)]
                else:
                    text = more[(p - 1 + spec["ask"]) % len(more)] if more else "Continue."
                self.main.user(text)
            else:
                self.main.user(opening)
            if p == 0:
                self.first_response()
                if kind == "review":
                    diff = filler(lang_of(item["files"][0]), rng.randint(5_000, 11_000), 5,
                                  numbered=False)
                    self.main.respond([self.tool_bash("git diff main...%s" % self.branch, diff)],
                                      text_tokens=60)
                self.read_phase(max(0, spec["reads"] - 1), p, agents=min(agents_left, 1))
                agents_left -= min(agents_left, 1)
            else:
                count = rng.randint(*spec["per_prompt"]) if long_session else rng.randint(1, 5)
                if kind in ("question", "review"):
                    count = max(1, count)
                self.read_phase(count, p, agents=1 if agents_left and p == 2 else 0)
                if agents_left and p == 2:
                    agents_left -= 1
            asking = kind in ("question", "review") or (kind == "qte" and p == 0)
            if asking:
                self.answer(450, 1400)
                continue
            if p == 0:
                edits = spec["edits"] if kind != "scripted" else rng.randint(0, 1)
                muts = (min(len(item.get("muts") or []), rng.randint(3, 5)) if kind == "scripted"
                        else (1 if item.get("muts") else 0))
                self.work_phase(edits, muts, spec["fail"])
            else:
                edits = rng.randint(2, 5) if long_session else rng.randint(1, 4)
                if kind == "scripted":
                    self.work_phase(rng.randint(0, 1), rng.randint(1, 3), False)
                else:
                    self.work_phase(edits, 1 if item.get("muts") and p == 1 else 0, False)
            if p == prompts - 1:
                self.finish()
            self.answer(120, 500)
        if not self.edited:
            g.counts["no_edit"] += 1
        g.counts["sessions"] += 1

    def write(self, root: Path) -> List[Path]:
        folder = root / "projects" / encode_cwd(self.cwd)
        written = []
        jobs = [(folder / ("%s.jsonl" % self.sid), self.main.rows)]
        for agent_id, lane in self.agents.items():
            jobs.append((folder / self.sid / "subagents" / ("agent-%s.jsonl" % agent_id), lane.rows))
        for path, rows in jobs:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                for row in rows:
                    handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            last = max(dt.datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")
                       .replace(tzinfo=dt.timezone.utc) for r in rows)
            os.utime(path, (last.timestamp(), last.timestamp()))
            written.append(path)
        return written


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def estimate_cost(totals: Dict[str, Dict[str, int]]) -> Optional[float]:
    """List-rate estimate from the skill's own price table, if it is there."""
    try:
        doc = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
        table = doc["tables"][doc["default_table_id"]]
    except (OSError, KeyError, ValueError):
        return None
    models = table["models"]
    total = 0.0
    for model, row in totals.items():
        name = model[:-4] if model.endswith("[1m]") else model
        rate = models.get(name)
        if not rate:
            return None
        total += (row["in"] * rate["input"] + row["read"] * 0.1 * rate["input"]
                  + row["w5"] * 1.25 * rate["input"] + row["w1"] * 2.0 * rate["input"]
                  + row["out"] * rate["output"]) / 1e6
    return total


def generate(out: Path, seed: int = SEED) -> Dict[str, Any]:
    gen = Generator(seed)
    projects = out / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("synthetic history written by demo_history.py; safe to delete\n",
                              encoding="utf-8")
    files = 0
    first = last = None
    for spec in SCHEDULE:
        builder = SessionBuilder(gen, spec)
        start = builder.now
        builder.run()
        files += len(builder.write(out))
        first = start if first is None else min(first, start)
        last = builder.now if last is None else max(last, builder.now)
    sittings: Dict[str, List[int]] = {}
    for spec in SCHEDULE:
        days = sittings.setdefault(spec["item"], [])
        if not days or spec["day"] - days[-1] > 3:
            days.append(spec["day"])
    summary = dict(gen.counts)
    summary.update({
        "root": str(out), "files": files, "seed": seed,
        "projects": len({ITEMS[s["item"]]["project"] for s in SCHEDULE}),
        "work_items": len({s["item"] for s in SCHEDULE}),
        "multi_sitting_items": sum(1 for days in sittings.values() if len(days) > 1),
        "first": stamp(first)[:10], "last": stamp(last)[:10],
        "est_cost_usd": estimate_cost(gen.totals),
        "tokens_by_model": gen.totals,
    })
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="directory to write the fake config root into")
    parser.add_argument("--seed", type=int, default=SEED,
                        help="random seed (default %(default)s; the same seed writes the same bytes)")
    parser.add_argument("--force", action="store_true",
                        help="replace an earlier demo root (only one this script wrote)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        if not (args.force and (out / MARKER).exists()):
            print("refusing to write into non-empty %s (pass --force to replace an earlier demo "
                  "root; anything else is left alone)" % out, file=sys.stderr)
            return 2
        shutil.rmtree(out / "projects", ignore_errors=True)
    summary = generate(out, args.seed)
    if args.json:
        print(json.dumps(summary, indent=1))
        return 0
    cost = summary["est_cost_usd"]
    print(out)
    print("demo history: %d sessions across %d projects (%d work items, %d with more than one "
          "sitting), %s to %s; %d responses, %d compactions, %d subagent runs, %d sessions "
          "opened a handoff note, %d made no edit; est. %s at list API rates"
          % (summary["sessions"], summary["projects"], summary["work_items"],
             summary["multi_sitting_items"], summary["first"], summary["last"],
             summary["responses"], summary["compactions"], summary["subagents"],
             summary["note_reads"], summary["no_edit"],
             ("$%.0f" % cost) if cost is not None else "(price table not found)"))
    # --store claude-code: without it the report also reads the VS Code and
    # Copilot CLI history of whoever runs the demo, and mixes it in.
    report = Path(__file__).resolve().parent / "orientation_profile.py"
    print("next: python3 %s report --store claude-code --root %s --output-dir ./demo-report"
          % (shlex.quote(str(report)), shlex.quote(str(out))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
