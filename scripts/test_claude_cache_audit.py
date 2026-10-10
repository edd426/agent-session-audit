#!/usr/bin/env python3
"""Synthetic acceptance tests for claude_cache_audit.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().with_name("claude_cache_audit.py")
SPEC = importlib.util.spec_from_file_location("claude_cache_audit", MODULE_PATH)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)


def assistant(timestamp: str, message_id: str, *, read: int = 0, write_5m: int = 0, write_1h: int = 0,
              model: str = "test-model", version: str = "1.0", output: int = 10) -> dict:
    total_write = write_5m + write_1h
    return {
        "type": "assistant", "timestamp": timestamp, "sessionId": "session-a", "uuid": "row-" + message_id,
        "cwd": "/private/example", "gitBranch": "main", "version": version, "isSidechain": False,
        "message": {
            "id": message_id, "model": model, "role": "assistant", "content": [],
            "usage": {
                "input_tokens": 3, "output_tokens": output, "cache_read_input_tokens": read,
                "cache_creation_input_tokens": total_write,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": write_5m,
                    "ephemeral_1h_input_tokens": write_1h,
                },
            },
        },
    }


def user(timestamp: str, prompt_id: str, content) -> dict:
    return {
        "type": "user", "timestamp": timestamp, "sessionId": "session-a", "uuid": "user-" + prompt_id,
        "promptId": prompt_id, "cwd": "/private/example", "message": {"role": "user", "content": content},
    }


class AuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "claude"
        project = self.root / "projects" / "anonymous-project"
        project.mkdir(parents=True)
        self.output = base / "output"
        self.pricing = base / "pricing.json"
        self.pricing.write_text(json.dumps({
            "currency": "USD", "description": "synthetic test prices",
            "models": {"test-model": {"input": 2.0, "output": 10.0}},
        }), encoding="utf-8")
        records = [
            user("2026-01-01T00:00:00Z", "p1", "first prompt SECRET_TEXT"),
            {**user("2026-01-01T00:00:00.500Z", "sdk", "automated prompt"), "promptSource": "sdk"},
            assistant("2026-01-01T00:00:01Z", "m1", write_5m=100_000),
            user("2026-01-01T00:02:00Z", "tool", [{"type": "tool_result", "content": "SECRET_TOOL"}]),
            assistant("2026-01-01T00:02:01Z", "m2", read=100_000, write_5m=10_000),
            assistant("2026-01-01T00:02:01.100Z", "m2", read=100_000, write_5m=10_000),
            assistant("2026-01-01T00:02:05Z", "m2b", read=110_000, write_5m=1_000),
            user("2026-01-01T00:12:00Z", "p2", "second prompt"),
            assistant("2026-01-01T00:12:01Z", "m3", write_1h=120_000),
        ]
        (project / "history.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def args(self):
        return AUDIT.parser().parse_args([
            "--root", str(self.root), "--output-dir", str(self.output),
            "--pricing-file", str(self.pricing), "--reuse-scenarios", "0.5,1.0",
        ])

    def test_parse_deduplicate_and_classify_human_prompts(self) -> None:
        metrics = AUDIT.analyze(self.args())
        self.assertEqual(metrics["quality"]["unique_api_events"], 4)
        self.assertEqual(metrics["quality"]["duplicate_assistant_rows"], 1)
        self.assertEqual(metrics["quality"]["human_prompts"], 2)
        self.assertEqual(metrics["human_prompt_gaps"]["total"], 1)
        self.assertEqual(metrics["eligibility_funnel"][-1]["gaps"], 1)
        self.assertEqual(metrics["agent_activity"]["subminute"]["total_gaps"], 1)
        self.assertEqual(metrics["agent_activity"]["subminute"]["no_direct_human_fraction"], 1.0)
        self.assertEqual(metrics["agent_activity"]["autonomous_bursts"]["bursts"], 1)
        self.assertEqual(metrics["actual"]["eligible_return_cache_creation"], 120_000)
        self.assertEqual(metrics["actual"]["eligible_return_creation_aligned_with_context_growth"], 9_000)

    def test_counterfactual_changes_sign_with_prefix_survival(self) -> None:
        metrics = AUDIT.analyze(self.args())
        low, high = metrics["scenarios"]
        self.assertLess(low["input_equivalent_savings_1h"], 0)
        self.assertGreater(high["input_equivalent_savings_1h"], 0)
        self.assertEqual(metrics["recommendation"]["label"], "Use the 1-hour cache only for some conversations")

    def test_outputs_are_complete_and_do_not_copy_prompt_text(self) -> None:
        AUDIT.analyze(self.args())
        summary = (self.output / "summary.md").read_text(encoding="utf-8")
        report = (self.output / "report.html").read_text(encoding="utf-8")
        raw_metrics = (self.output / "metrics.json").read_text(encoding="utf-8")
        self.assertIn("Claude Code cache: 5 minutes or 1 hour?", report)
        self.assertIn("65.2%", summary)
        self.assertIn("5 min", report)
        self.assertIn("1 hour", report)
        combined = summary + report + raw_metrics
        self.assertNotIn("SECRET_TEXT", combined)
        self.assertNotIn("SECRET_TOOL", combined)
        self.assertNotIn("/private/example", combined)

    def test_report_uses_plain_words(self) -> None:
        # The words a reader new to caching does not know; each has a plain
        # replacement in the report.
        AUDIT.analyze(self.args())
        for name in ("report.html", "summary.md"):
            text = (self.output / name).read_text(encoding="utf-8")
            found = re.findall(r"structural|prefix survival|TTL-eligible|eligibility funnel|autonomy profile|"
                               r"lane style|input-equivalent|policy-level|counterfactual", text, re.I)
            self.assertEqual(found, [], name)

    def test_report_is_picture_first(self) -> None:
        AUDIT.analyze(self.args())
        page = (self.output / "report.html").read_text(encoding="utf-8")
        self.assertGreaterEqual(page.count("<svg"), 4)
        self.assertIn('aria-label="Which pauses could the 1-hour cache have helped"', page)
        self.assertIn('class="zero"', page)                       # the savings chart's zero line
        self.assertIn("<details><summary>Exact numbers</summary>", page)
        self.assertIn("pays off if more than", page)               # the one break-even sentence
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIn(':root[data-theme="dark"]', page)
        self.assertNotRegex(page, r"<script[^>]+src=|<link[^>]+href=|https?://")
        # Prose creeps back one caveat at a time. The old page carried 490
        # visible words on this history; the picture-first page stays near 70 % of that.
        text = re.sub(r"<(script|style)[\s\S]*?</\1>|<[^>]+>", " ", page)
        self.assertLess(len(text.split()), 345)

    def test_advice_agrees_with_the_headline(self) -> None:
        # "What to do" follows the recommendation: a page that says "keep 5
        # minutes" must not open its advice with "use 1 hour".
        metrics = AUDIT.analyze(self.args())

        def segments(candidates: int) -> dict:
            result = {"continuous/5-minute fit": {"lanes": 3, "cacheable_tokens": 900}}
            if candidates:
                result["1-hour candidate"] = {"lanes": candidates, "cacheable_tokens": 100}
            return result

        cases = [
            (AUDIT.REC_FIVE_MINUTES, 2, ["Keep 5 minutes: here the 1-hour cache costs more", "Revisit if"], ["Use 1 hour"]),
            (AUDIT.REC_ONE_HOUR, 2, ["Use 1 hour: 2 of 5 conversations"], ["Keep 5 minutes"]),
            (AUDIT.REC_SELECTIVE, 2, ["Use 1 hour where", "2 of 5 here", "Keep 5 minutes for the rest"], []),
            (AUDIT.REC_SELECTIVE, 0, ["Use 1 hour only for", "None does that often here"], [" of 3 here"]),
            (AUDIT.REC_EITHER, 2, ["Either setting is fine"], ["Use 1 hour", "Keep 5 minutes"]),
            (AUDIT.REC_INCONCLUSIVE, 2, ["Not enough history"], ["Use 1 hour", "Keep 5 minutes"]),
        ]
        self.assertEqual({case[0] for case in cases},
                         {AUDIT.REC_FIVE_MINUTES, AUDIT.REC_ONE_HOUR, AUDIT.REC_SELECTIVE,
                          AUDIT.REC_EITHER, AUDIT.REC_INCONCLUSIVE})
        for label, candidates, present, absent in cases:
            with self.subTest(label=label, candidates=candidates):
                metrics["recommendation"] = {"label": label, "reason": "reason"}
                metrics["workload_segments"] = segments(candidates)
                page = AUDIT.html_report(metrics)
                self.assertIn("<h2>%s</h2>" % label, page)
                box = re.search(r"<h2>What to do</h2><ul>([\s\S]*?)</ul>", page).group(1)
                summary = AUDIT.markdown_report(metrics)
                self.assertIn("**%s.**" % label, summary)
                md_box = summary.split("## What to do", 1)[1].split("## Data and limits", 1)[0]
                for text in present:
                    self.assertIn(text, box)
                    self.assertIn(text, md_box)
                for text in absent:
                    self.assertNotIn(text, box)
                    self.assertNotIn(text, md_box)
                self.assertIn(AUDIT.RESET_ADVICE, box)

    def test_break_even_share_is_where_the_saving_crosses_zero(self) -> None:
        metrics = AUDIT.analyze(self.args())
        share = AUDIT.break_even_share(metrics["scenarios"])
        self.assertIsNotNone(share)
        self.assertTrue(0 < share < 1, share)
        events, _prompts, _quality = AUDIT.read_history(self.root, None, None)
        _gaps, lanes = AUDIT.make_gaps(events)
        table = AUDIT.load_pricing(self.pricing, None)
        # The saving is linear in the share, so the crossing found from two
        # scenarios is exact.
        self.assertAlmostEqual(AUDIT.simulate(events, lanes, table, share)["usd_savings_1h"], 0.0, places=9)
        self.assertIn("more than %.0f%%" % (100 * share), AUDIT.break_even_sentence(metrics["scenarios"]))

    def test_break_even_sentence_edge_cases(self) -> None:
        def scenario(share: float, saving: float) -> dict:
            return {"reuse_fraction": share, "usd_savings_1h": saving}
        self.assertIn("any share", AUDIT.break_even_sentence([scenario(0.5, 3.0), scenario(1.0, 4.0)]))
        self.assertIn("even if all", AUDIT.break_even_sentence([scenario(0.5, -3.0), scenario(1.0, -1.0)]))
        self.assertIn("two or more", AUDIT.break_even_sentence([scenario(0.8, 1.0)]))

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX-only")
    def test_written_files_are_owner_only(self) -> None:
        AUDIT.analyze(self.args())
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for name in ("metrics.json", "summary.md", "report.html"):
            self.assertEqual((self.output / name).stat().st_mode & 0o777, 0o600, name)

    def test_output_cannot_be_nested_under_source(self) -> None:
        args = self.args()
        args.output_dir = str(self.root / "audit-output")
        with self.assertRaises(ValueError):
            AUDIT.analyze(args)

    def test_break_even_constants(self) -> None:
        self.assertAlmostEqual(AUDIT.POLICY_BREAK_EVEN, 0.652173913, places=8)
        self.assertAlmostEqual(AUDIT.TRANSITION_BREAK_EVEN, 0.394736842, places=8)


class HomeFolderTest(unittest.TestCase):
    """The cache audit and the three flat analyses show the home folder as `~`."""

    ACCOUNT = "acct7q"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve() / "home" / self.ACCOUNT
        self.saved = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE")}
        for name in self.saved:
            os.environ[name] = str(self.home)
        self.work = self.home / "src" / "app"
        project = (self.home / ".claude" / "projects"
                   / re.sub(r"[^A-Za-z0-9]", "-", str(self.work)))
        project.mkdir(parents=True)
        read = {"type": "tool_use", "id": "t1", "name": "Read",
                "input": {"file_path": str(self.work / "app.py")}}
        model = "claude-opus-5"                  # priced in the default table
        first = assistant("2026-01-01T00:00:01Z", "m1", write_5m=50_000, model=model)
        first["message"]["content"] = [read]
        records = [
            user("2026-01-01T00:00:00Z", "p1", "fix %s" % (self.work / "app.py")),
            first,
            user("2026-01-01T00:00:02Z", "tool",
                 [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 8000}]),
            assistant("2026-01-01T00:00:03Z", "m2", read=50_000, write_5m=2_000, model=model),
            assistant("2026-01-01T00:10:00Z", "m3", write_1h=52_000, model=model),
            assistant("2026-01-01T00:10:05Z", "m4", read=52_000, model=model),
        ]
        (project / "history.jsonl").write_text(
            "".join(json.dumps({**record, "cwd": str(self.work)}) + "\n" for record in records),
            encoding="utf-8")
        self.out = self.home / "audit"

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    def test_every_output_shows_the_home_folder_as_tilde(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = AUDIT.main(["--root", str(self.home / ".claude"),
                             "--output-dir", str(self.out / "cache"),
                             "--ledger", str(self.out / "sessions.jsonl")])
        self.assertEqual(rc, 0)
        printed = {"claude_cache_audit.py": stdout.getvalue()}
        projects = str(self.home / ".claude" / "projects")
        for script, extra in (("lane_profile.py", []), ("compact_economics.py", []),
                              ("tool_bloat.py", ["--ledger", str(self.out / "sessions.jsonl")])):
            result = subprocess.run(
                [sys.executable, str(MODULE_PATH.with_name(script)), "--root", projects,
                 "--json-out", str(self.out / (script + ".json"))] + extra,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=dict(os.environ))
            self.assertEqual(result.returncode, 0, result.stderr)
            printed[script] = result.stdout
        written = {path.name: path.read_text(encoding="utf-8")
                   for path in sorted(self.out.rglob("*")) if path.is_file()}
        self.assertGreaterEqual(len(written), 7, sorted(written))
        for name, text in list(printed.items()) + list(written.items()):
            self.assertNotIn(self.ACCOUNT, text, name)
        self.assertIn("~%saudit%scache" % (os.sep, os.sep), printed["claude_cache_audit.py"])
        self.assertIn("~%ssrc%sapp%sapp.py" % (os.sep, os.sep, os.sep),
                      printed["tool_bloat.py"])
        self.assertIn("~-src-app", printed["lane_profile.py"])

    def test_a_cut_path_keeps_no_part_of_the_account_name(self) -> None:
        sys.path.insert(0, str(MODULE_PATH.parent))
        import tool_bloat
        # 56 characters after the account name: a 60-character cut of the raw
        # path would start inside it ("ct7q/src/app/...").
        target = tool_bloat.target_of("Read", {"file_path": str(self.work / ("a" * 42) / "b.py")})
        self.assertTrue(target.startswith("~" + os.sep) and target.endswith("b.py"), target)


if __name__ == "__main__":
    AUDIT.audit_common.utf8_stdio()
    unittest.main(verbosity=2)
