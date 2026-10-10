#!/usr/bin/env python3
"""The synthetic demo history must give every analysis something to show.

Generates the demo into a temporary directory, runs each analysis on it the
way a user would (as a subprocess, with the documented flags), and checks
that each one produces non-trivial output. Never reads real history.

    python3 scripts/test_demo_history.py
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(*args) -> str:
    proc = subprocess.run([sys.executable] + [str(a) for a in args], cwd=str(HERE),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=900)
    if proc.returncode:
        raise AssertionError("%s exited %d:\n%s" % (Path(str(args[0])).name, proc.returncode,
                                                    (proc.stderr or proc.stdout)[-2000:]))
    return proc.stdout


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class DemoHistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="demo-history-test-")
        cls.base = Path(cls.tmp.name)
        cls.root = cls.base / "root"
        cls.stdout = run(HERE / "demo_history.py", "--out", cls.root)
        cls.files = sorted(cls.root.rglob("*.jsonl"))
        cls.rows = [json.loads(line) for path in cls.files
                    for line in path.read_text(encoding="utf-8").splitlines()]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    # -- the generator itself -------------------------------------------------
    def test_prints_root_summary_and_next_command(self) -> None:
        lines = self.stdout.strip().splitlines()
        self.assertEqual(lines[0], str(self.root.resolve()))
        self.assertTrue(lines[1].startswith("demo history: "), lines[1])
        # the suggested next command must not mix in the user's own Copilot stores
        self.assertTrue(lines[2].startswith("next: python3 "), lines[2])
        self.assertIn("--store claude-code", lines[2])
        self.assertEqual(len(lines), 3)

    def test_layout_is_a_claude_config_root(self) -> None:
        projects = [p for p in (self.root / "projects").iterdir() if p.is_dir()]
        self.assertGreaterEqual(len(projects), 5)
        sessions = [p for p in self.files if p.parent.parent.name == "projects"]
        children = [p for p in self.files if p.parent.name == "subagents"]
        self.assertTrue(40 <= len(sessions) <= 60, len(sessions))
        self.assertGreater(len(children), 0)
        for child in children:                    # <session>/subagents/agent-<id>.jsonl
            session_dir = child.parent.parent
            self.assertTrue((session_dir.parent / (session_dir.name + ".jsonl")).exists())

    def test_same_seed_writes_the_same_bytes(self) -> None:
        again = self.base / "again"
        run(HERE / "demo_history.py", "--out", again)
        self.assertEqual(tree_digest(self.root / "projects"), tree_digest(again / "projects"))

    def test_refuses_a_directory_it_did_not_write(self) -> None:
        stranger = self.base / "stranger"
        stranger.mkdir()
        (stranger / "keep.txt").write_text("not ours", encoding="utf-8")
        proc = subprocess.run([sys.executable, str(HERE / "demo_history.py"), "--out",
                               str(stranger), "--force"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 2)
        self.assertTrue((stranger / "keep.txt").exists())

    def test_rows_have_the_claude_code_shape(self) -> None:
        assistant = [r for r in self.rows if r["type"] == "assistant"]
        by_message = collections.defaultdict(list)
        for row in assistant:
            by_message[(row["sessionId"], row["message"]["id"])].append(row)
        multi = [rows for rows in by_message.values() if len(rows) > 1]
        self.assertGreater(len(multi), 100, "one row per content block")
        for rows in multi:
            self.assertEqual(len({json.dumps(r["message"]["usage"], sort_keys=True)
                                  for r in rows}), 1, "usage repeats on every block row")
        main = [r for r in assistant if not r.get("isSidechain")]
        self.assertTrue(all(r.get("effort") for r in main))
        self.assertGreaterEqual(len({r["effort"] for r in main}), 3)
        self.assertGreaterEqual(len({r["message"]["model"] for r in assistant}), 3)
        side = [r for r in self.rows if r.get("isSidechain")]
        self.assertTrue(side and all(r.get("agentId") for r in side))
        boundaries = [r for r in self.rows if r.get("subtype") == "compact_boundary"]
        self.assertGreater(len(boundaries), 1)
        self.assertTrue(all(r["compactMetadata"].get("preTokens") for r in boundaries))
        self.assertEqual({r["compactMetadata"]["trigger"] for r in boundaries}, {"auto", "manual"})
        self.assertEqual(len(boundaries), sum(1 for r in self.rows if r.get("isCompactSummary")))
        writes = collections.Counter()
        for row in assistant:
            for key, value in row["message"]["usage"]["cache_creation"].items():
                writes[key] += value
        self.assertGreater(writes["ephemeral_5m_input_tokens"], 0)
        self.assertGreater(writes["ephemeral_1h_input_tokens"], 0)

    def test_no_trace_of_the_machine_that_generated_it(self) -> None:
        blob = "\n".join(p.read_text(encoding="utf-8") for p in self.files)
        self.assertNotIn("/Users/", blob)
        home = os.path.expanduser("~")
        if home not in ("", "/", "/home/dev"):
            self.assertNotIn(home, blob)
        host = socket.gethostname().split(".")[0]
        if len(host) >= 4:
            self.assertNotIn(host.lower(), blob.lower())
        cwds = {r["cwd"] for r in self.rows if r.get("cwd")}
        self.assertTrue(all(c.startswith("/home/dev/src/") for c in cwds), cwds)

    # -- every analysis has something to show ---------------------------------
    def test_orientation_report(self) -> None:
        out = self.base / "orientation"
        # --store claude-code: without it the report also ingests the VS Code
        # and Copilot CLI stores of whoever runs the test, and names their
        # home-directory paths in its scope block.
        run(HERE / "orientation_profile.py", "report", "--store", "claude-code",
            "--root", self.root, "--output-dir", out, "--ledger", out / "sessions.jsonl")
        doc = json.loads((out / "orientation.json").read_text(encoding="utf-8"))
        sessions = doc["sessions"]
        self.assertTrue(40 <= len(sessions) <= 60, len(sessions))
        self.assertGreaterEqual(doc["aggregates"]["multi_episode_streams"], 2)
        compactions = sum(r["compactions"] for r in doc["continuations"]["per_session"].values())
        self.assertGreaterEqual(compactions, 1)
        opened = sum(1 for s in sessions if s.get("handoff_opened_by_turn_2"))
        self.assertTrue(0 < opened < len(sessions), opened)
        self.assertGreaterEqual(sum(1 for s in sessions if s.get("no_mutation")), 1)
        self.assertGreaterEqual(sum(1 for s in sessions if s.get("question_first")), 1)
        counts = doc["continuations"]["counts"]
        self.assertTrue(counts["R0"] and counts["R1"] and counts["R2"], counts)
        self.assertTrue((out / "carry-vs-restart.html").exists())
        self.assertEqual(doc["scope"]["price_coverage_fraction"], 1.0)
        total = sum(s["est_cost_usd"] for s in sessions)
        self.assertTrue(50 < total < 500, total)
        ledger = (out / "sessions.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(ledger), len(sessions))

    def test_cache_audit(self) -> None:
        out = self.base / "cache"
        run(HERE / "claude_cache_audit.py", "--root", self.root, "--output-dir", out)
        metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        self.assertGreater(metrics["quality"]["unique_api_events"], 1000)
        self.assertGreater(metrics["quality"]["duplicate_assistant_rows"], 0)
        gaps = dict(zip(metrics["api_gap_histogram"]["labels"],
                        metrics["api_gap_histogram"]["counts"]))
        self.assertGreater(sum(v for k, v in gaps.items()
                               if k in ("5–15 min", "15–30 min", "30–60 min")), 0, gaps)
        self.assertGreater(gaps[">60 min"], 0, gaps)
        tokens = metrics["actual"]["tokens"]
        self.assertGreater(tokens["cache_write_5m"], 0)
        self.assertGreater(tokens["cache_write_1h"], 0)
        self.assertTrue((out / "report.html").exists())

    def test_lane_profile(self) -> None:
        out = self.base / "lanes.json"
        run(HERE / "lane_profile.py", "--root", self.root / "projects", "--json-out", out)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertGreater(doc["lanes"], 40)
        self.assertEqual(doc["price_coverage"], 1.0)
        self.assertGreater(doc["cold_events"].get("TTL-EXPIRY", {}).get("n", 0), 0)
        self.assertIsNotNone(doc["agent_lane_hit_ratio_median"], "child-agent lanes")

    def test_compact_economics(self) -> None:
        out = self.base / "compact.json"
        run(HERE / "compact_economics.py", "--root", self.root, "--json-out", out)
        compacts = json.loads(out.read_text(encoding="utf-8"))["compacts"]
        self.assertGreaterEqual(len(compacts), 2)
        self.assertEqual({c["cache"] for c in compacts}, {"warm", "COLD"})

    def test_tool_bloat(self) -> None:
        out = self.base / "bloat.json"
        run(HERE / "tool_bloat.py", "--root", self.root, "--json-out", out)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(doc["league"]), 4)
        self.assertGreaterEqual(doc["top_calls"][0]["tok"], 20_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
