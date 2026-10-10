#!/usr/bin/env python3
"""Tests for the shared scope filter and price table (audit_common.py).

Two defects motivated the module and are pinned here as regressions:

  * the cwd filters existed on two of five analyses, with different names and
    different semantics -- so the documented lane step could not be run under
    an exclusion requirement;
  * two scripts read the same --pricing-file and reported 15 % and 100 %
    coverage for it, because one silently merged a built-in table over it.

The last test runs claude_cache_audit.py and orientation_profile.py over the
SAME synthetic history and asserts they print the same coverage number. That
is the check that fails if anyone re-introduces a private price table.

    python3 test_audit_common.py
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import audit_common as ac


def _parser():
    p = argparse.ArgumentParser()
    ac.add_filter_args(p)
    return p


class FilterTest(unittest.TestCase):
    def test_no_filter_keeps_everything(self):
        f = ac.CwdFilter()
        self.assertFalse(f.active)
        self.assertTrue(f.keep("/anywhere"))
        self.assertTrue(f.keep(None, "/fallback"))
        self.assertEqual(f.filtered_rows, 0)
        self.assertEqual(f.describe(), "no cwd filter (everything)")

    def test_include_is_a_whitelist(self):
        f = ac.CwdFilter(include=["myproj"])
        self.assertTrue(f.keep("/home/dev/myproj/sub"))
        self.assertFalse(f.keep("/home/dev/other"))
        self.assertEqual(f.filtered_rows, 1)

    def test_exclude_beats_include(self):
        f = ac.CwdFilter(include=["proj"], exclude=["proj/vendor"])
        self.assertTrue(f.keep("/a/proj/src"))
        self.assertFalse(f.keep("/a/proj/vendor/x"))

    def test_patterns_are_regexes_matched_with_search(self):
        f = ac.CwdFilter(exclude=[r"scratch-runs/r\d+$"])
        self.assertFalse(f.keep("/home/dev/scratch-runs/r12"))
        self.assertTrue(f.keep("/home/dev/scratch-runs/rXX"))

    def test_cwd_falls_back_to_the_project_directory(self):
        f = ac.CwdFilter(exclude=["proj-b"])
        self.assertFalse(f.keep(None, "/root/projects/proj-b"))
        self.assertTrue(f.keep(None, "/root/projects/proj-a"))
        # a present cwd wins over the fallback
        self.assertTrue(f.keep("/root/projects/proj-a", "/root/projects/proj-b"))

    def test_several_exclude_patterns_all_apply(self):
        # runs launched from one repo but executed out of a scratch directory
        # land under a different path, so excluding the repo alone is not enough
        f = ac.CwdFilter(exclude=["myrepo", "scratch-runs"])
        self.assertFalse(f.keep("/home/dev/Projects/myrepo"))
        self.assertFalse(f.keep("/home/dev/scratch-runs/run-3/ws"))
        self.assertTrue(f.keep("/home/dev/Projects/otherproj"))

    def test_from_args_reads_both_flags(self):
        args = _parser().parse_args(
            ["--include-cwd-pattern", "a", "--exclude-cwd-pattern", "b",
             "--exclude-cwd-pattern", "c"])
        f = ac.CwdFilter.from_args(args)
        self.assertEqual(f.include_patterns, ["a"])
        self.assertEqual(f.exclude_patterns, ["b", "c"])

    def test_from_args_tolerates_a_parser_without_the_flags(self):
        f = ac.CwdFilter.from_args(argparse.Namespace())
        self.assertFalse(f.active)

    def test_every_analysis_advertises_both_flags(self):
        # --help is the contract an agent reads first.
        # orientation_profile.py registers them on its subcommands, so its
        # scope help lives one level down; the other four are flat.
        for argv in (["claude_cache_audit.py", "--help"],
                     ["orientation_profile.py", "sessions", "--help"],
                     ["orientation_profile.py", "report", "--help"],
                     ["lane_profile.py", "--help"],
                     ["compact_economics.py", "--help"],
                     ["tool_bloat.py", "--help"]):
            out = subprocess.run([sys.executable, os.path.join(HERE, argv[0])] + argv[1:],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=120).stdout
            for flag in ("--include-cwd-pattern", "--exclude-cwd-pattern"):
                self.assertIn(flag, out,
                              "%s does not advertise %s" % (" ".join(argv), flag))


class PriceTableTest(unittest.TestCase):
    def setUp(self):
        self.prices = ac.load_prices()

    def test_the_default_table_is_the_skills_own_file(self):
        self.assertTrue(ac.DEFAULT_PRICING_FILE.endswith(
            os.path.join("references", "pricing-public-example.json")))
        self.assertTrue(os.path.exists(ac.DEFAULT_PRICING_FILE))

    def test_every_id_in_the_bundled_table_is_priced_with_its_markers(self):
        # Derived from the table itself: each id, its context-window markers
        # and a dated snapshot of it resolve to that id's own rate, and each
        # short alias (`opus`, `sonnet`, ...) resolves to its rule's rate.
        models = self.prices["models"]
        self.assertTrue(models)
        for name, rate in models.items():
            for variant in (name, name + "[1m]", name + "-1m", name + "-20260101"):
                self.assertEqual(ac.price_for(variant, self.prices), rate, variant)
        self.assertTrue(self.prices["rules"])
        for pattern, rate in self.prices["rules"]:
            alias = pattern.pattern.strip("^$")
            for variant in (alias, alias + "[1m]"):
                self.assertEqual(ac.price_for(variant, self.prices), rate, variant)

    def test_fable_is_not_priced_at_opus_rates(self):
        # Fable has its own list price ($10 / $50); pricing it at Opus
        # rates would be wrong by 2x.
        self.assertEqual(ac.price_for("claude-fable-5", self.prices), (10.0, 50.0))
        self.assertEqual(ac.price_for("claude-fable-5-1", self.prices), (10.0, 50.0))
        self.assertEqual(ac.price_for("claude-opus-5", self.prices), (5.0, 25.0))

    def test_longer_id_wins_over_its_prefix(self):
        self.assertNotEqual(ac.price_for("claude-sonnet-4-6", self.prices),
                            ac.price_for("claude-sonnet-5", self.prices))
        self.assertEqual(ac.price_for("claude-sonnet-4-6", self.prices), (3.0, 15.0))

    def test_context_markers_are_stripped_not_treated_as_a_price_tier(self):
        for marked, bare in (("claude-opus-5[1m]", "claude-opus-5"),
                             ("claude-opus-4-8-1m", "claude-opus-4-8")):
            self.assertEqual(ac.price_for(marked, self.prices),
                             ac.price_for(bare, self.prices))

    def test_synthetic_is_not_a_model(self):
        self.assertFalse(ac.is_priceable("<synthetic>"))
        self.assertIsNone(ac.price_for("<synthetic>", self.prices))

    def test_an_unknown_model_is_unpriced_not_defaulted(self):
        self.assertIsNone(ac.price_for("some-other-vendor-model", self.prices))
        self.assertIsNone(ac.price_for("example-model", self.prices))

    def test_a_missing_table_id_is_an_error_not_a_silent_fallback(self):
        with self.assertRaises(ac.PriceTableError):
            ac.load_prices(ac.DEFAULT_PRICING_FILE, "no-such-table")


class EffortTest(unittest.TestCase):
    def test_effort_is_read_from_either_field(self):
        self.assertEqual(ac.record_effort({"effort": "high"}), "high")
        # some CLI versions write the same thing as `perTurnEffort`
        self.assertEqual(ac.record_effort({"perTurnEffort": "low"}), "low")
        self.assertIsNone(ac.record_effort({}))

    def test_the_event_reader_carries_a_perTurnEffort_level(self):
        record = {"type": "assistant", "sessionId": "s", "perTurnEffort": "xhigh",
                  "message": {"id": "m1", "model": "claude-opus-5",
                              "usage": {"input_tokens": 1, "output_tokens": 1}}}
        response, _ = ac.EventReader().add(record)
        self.assertEqual(response.effort, "xhigh")


class HomeRelativeTest(unittest.TestCase):
    def test_every_spelling_of_the_home_folder_becomes_tilde(self):
        home = "/home/dev"
        self.assertEqual(ac.replace_home("cd /home/dev/src", home), "cd ~/src")
        self.assertEqual(ac.replace_home(json.dumps("\\home\\dev\\src"), home), '"~\\\\src"')
        self.assertEqual(ac.replace_home("file:///home/dev/a", home), "file://~/a")
        self.assertEqual(ac.replace_home("projects/-home-dev-src/x", home), "projects/~-src/x")

    def test_a_lookalike_is_left_alone(self):
        home = "/home/dev"
        for text in ("/home/developer/x", "/mnt/home/dev/x", "/tmp/-home-dev-src",
                     "/home/dev.bak/x"):
            self.assertEqual(ac.replace_home(text, home), text)
        # A JSON-escaped path glued to a name: only the second backslash can
        # start the home folder, exactly as for an unescaped one.
        self.assertEqual(ac.replace_home("a\\\\home\\\\dev", home), "a\\~")


class CoverageTest(unittest.TestCase):
    def test_synthetic_stays_out_of_the_denominator(self):
        c = ac.Coverage()
        c.add("claude-opus-5", 1000, True)
        c.add("<synthetic>", 9000, False)
        self.assertEqual(c.total_tokens, 1000)
        self.assertEqual(c.fraction, 1.0)

    def test_an_unpriced_model_lowers_coverage_and_is_named(self):
        c = ac.Coverage()
        c.add("claude-opus-5", 1000, True)
        c.add("example-model", 3000, False)
        self.assertEqual(c.fraction, 0.25)
        self.assertFalse(c.ok)
        self.assertIn("example-model", c.caveat())

    def test_a_dollar_figure_cannot_be_formatted_without_its_coverage(self):
        c = ac.Coverage()
        c.add("claude-opus-5", 1000, True)
        self.assertEqual(c.money(1234.5), "$1,234.50 (coverage 100 %)")

    def test_below_the_floor_the_caveat_is_loud_and_non_empty(self):
        c = ac.Coverage()
        c.add("claude-opus-5", 1, True)
        c.add("mystery", 99, False)
        self.assertTrue(c.caveat().startswith("Note: dollar totals cover only 1 %"),
                        c.caveat())
        self.assertNotIn("!!", c.caveat())
        self.assertIn("1 %", c.money(10.0))

    def test_the_floor_is_one_number_for_the_whole_skill(self):
        self.assertEqual(ac.COVERAGE_FLOOR, 0.80)


def _write_history(root):
    """A tiny Claude Code history: one priced model, one unpriced, one
    <synthetic> record, in a project directory both scripts will read."""
    proj = os.path.join(root, "projects", "-home-dev-demo")
    os.makedirs(proj)
    t0 = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    rows = []
    for i, model in enumerate(["claude-opus-5"] * 6 + ["some-other-vendor-model"] * 2
                              + ["<synthetic>"]):
        rows.append({
            "type": "assistant", "sessionId": "s1", "uuid": "u%d" % i,
            "cwd": "/home/dev/demo",
            "timestamp": (t0 + dt.timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
            "message": {"id": "m%d" % i, "model": model, "role": "assistant",
                        "content": [{"type": "text", "text": "hi"}],
                        "usage": {"input_tokens": 100, "output_tokens": 100,
                                  "cache_read_input_tokens": 800,
                                  "cache_creation_input_tokens": 0}},
        })
    with open(os.path.join(proj, "s1.jsonl"), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class SameCoverageTest(unittest.TestCase):
    """claude_cache_audit.py and orientation_profile.py, same input, same number."""

    def test_both_scripts_report_the_same_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            root = os.path.join(td, "claude")
            _write_history(root)

            out_a = os.path.join(td, "cache-audit")
            rc = subprocess.run(
                [sys.executable, os.path.join(HERE, "claude_cache_audit.py"),
                 "--root", root, "--output-dir", out_a],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600)
            self.assertEqual(rc.returncode, 0, rc.stderr)
            with open(os.path.join(out_a, "metrics.json"), encoding="utf-8") as fh:
                metrics = json.load(fh)
            cov_a = metrics["actual"]["price_coverage"]

            out_b = os.path.join(td, "orientation")
            os.makedirs(out_b)
            rc = subprocess.run(
                [sys.executable, os.path.join(HERE, "orientation_profile.py"), "sessions",
                 "--root", root, "--json-out", os.path.join(out_b, "o.json")],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600)
            self.assertEqual(rc.returncode, 0, rc.stderr)
            with open(os.path.join(out_b, "o.json"), encoding="utf-8") as fh:
                payload = json.load(fh)
            cov_b = payload["scope"]["price_coverage_fraction"]

            # 6 priced turns of 1000 tokens, 2 unpriced of 1000; the
            # <synthetic> record is not a model and is out of the denominator.
            self.assertAlmostEqual(cov_a, 0.75, places=3)
            self.assertAlmostEqual(cov_b, cov_a, places=3,
                                   msg="the two scripts disagree on coverage for the "
                                       "same input -- a private price table is back")
            self.assertIn("coverage 75 %", payload["scope"]["price_coverage"])


if __name__ == "__main__":
    ac.utf8_stdio()
    unittest.main(verbosity=2)
