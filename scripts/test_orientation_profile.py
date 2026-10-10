#!/usr/bin/env python3
"""Deterministic synthetic tests for orientation_profile.py.

Creates fake JSONL history in a temporary directory; never reads your own transcripts,
never invokes Claude. Run after any change:

    python3 scripts/test_orientation_profile.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orientation_profile as op  # noqa: E402

T0 = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
FAILURES = []


PAGE_ORDER = ["index.html", "orientation.html", "definitions.html",
              "ledger.html", "appendix.html"]


def read_pages(out):
    """Every HTML page of the report set, by filename.

    The report is a set of pages, not one document, so a
    content check that reads only orientation.html now tests a fifth of the
    report. Read them all and say which one a failure is in.
    """
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(out.glob("*.html"))}


def check(name, condition, detail=""):
    if condition:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAILURES.append(name)


# --------------------------------------------------------------------------
# fixture builders
# --------------------------------------------------------------------------
def assistant(minute, session, tools=(), model="claude-opus-5", read=20_000,
              write=1_000, out=500, thinking=0, mid=None, cwd="/w/proj-a",
              effort="high"):
    return {
        "type": "assistant", "sessionId": session, "cwd": cwd, "version": "9.9.9",
        "effort": effort,
        "timestamp": (T0 + dt.timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
        "message": {
            "id": mid or "msg-%s-%d" % (session, minute),
            "model": model,
            "usage": {"input_tokens": 10, "output_tokens": out,
                      "cache_read_input_tokens": read,
                      "cache_creation": {"ephemeral_5m_input_tokens": write,
                                         "ephemeral_1h_input_tokens": 0},
                      "output_tokens_details": {"thinking_tokens": thinking}},
            "content": ([{"type": "thinking", "thinking": "x" * (thinking * 4)}] if thinking else [])
                       + [{"type": "tool_use", "id": "t%d%d" % (minute, i), "name": n, "input": a}
                          for i, (n, a) in enumerate(tools)],
        },
    }


def human(minute, session, text, cwd="/w/proj-a"):
    return {"type": "user", "sessionId": session, "cwd": cwd,
            "timestamp": (T0 + dt.timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
            "message": {"role": "user", "content": text}}


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def build_history(root):
    projects = root / "projects" / "-w-proj-a"

    # 1. SCRIPTED: Bash-led, few hand edits, mutates at turn 3.
    write_jsonl(projects / "scripted.jsonl", [
        human(0, "s-scripted", "rename the config keys"),
        assistant(1, "s-scripted", [("Glob", {"pattern": "**/*.py"})], thinking=200),
        assistant(2, "s-scripted", [("Read", {"file_path": "/w/proj-a/main.py"})], thinking=200),
        assistant(3, "s-scripted", [("Bash", {"command": "python3 -c 'print(1)' > /w/proj-a/out.txt"})]),
        assistant(4, "s-scripted", [("Bash", {"command": "sed -i '' s/a/b/ /w/proj-a/main.py"})]),
        assistant(5, "s-scripted", [("Edit", {"file_path": "/w/proj-a/main.py"})]),
        assistant(6, "s-scripted", [("Bash", {"command": "pytest -q"})]),
    ])

    # 2. BY-HAND: 60 Edit calls, no scripting.
    rows = [human(0, "s-byhand", "rename the tag on each file")]
    rows.append(assistant(1, "s-byhand", [("Read", {"file_path": "/w/proj-a/a.py"})]))
    for i in range(60):
        rows.append(assistant(2 + i, "s-byhand",
                              [("Edit", {"file_path": "/w/proj-a/f%d.py" % i})]))
    write_jsonl(projects / "byhand.jsonl", rows)

    # 3. READ-ONLY: never mutates -> no_mutation bucket, share must be None.
    write_jsonl(projects / "readonly.jsonl", [
        human(0, "s-readonly", "explain the module layout"),
        assistant(1, "s-readonly", [("Read", {"file_path": "/w/proj-a/main.py"})]),
        assistant(2, "s-readonly", [("Grep", {"pattern": "resource", "path": "/w/proj-a"})]),
        assistant(3, "s-readonly", [("Bash", {"command": "grep -rn tags /w/proj-a | head -20"})]),
    ])

    # 4. THREAD: two sessions on /w/proj-b sharing files; the second opens
    #    HANDOFF.md at turn 2 and starts editing immediately.
    threads = root / "projects" / "-w-proj-b"
    write_jsonl(threads / "t1.jsonl", [
        human(0, "s-t1", "start the pipeline work", cwd="/w/proj-b"),
        assistant(1, "s-t1", [("Read", {"file_path": "/w/proj-b/pipeline.yml"})], cwd="/w/proj-b"),
        assistant(2, "s-t1", [("Read", {"file_path": "/w/proj-b/src/app.py"})], cwd="/w/proj-b"),
        assistant(3, "s-t1", [("Grep", {"pattern": "def handler", "path": "/w/proj-b"})], cwd="/w/proj-b"),
        assistant(4, "s-t1", [("Edit", {"file_path": "/w/proj-b/pipeline.yml"})], cwd="/w/proj-b"),
        assistant(5, "s-t1", [("Write", {"file_path": "/w/proj-b/HANDOFF.md"})], cwd="/w/proj-b"),
    ])
    write_jsonl(threads / "t2.jsonl", [
        human(2880, "s-t2", "pick up the pipeline work", cwd="/w/proj-b"),
        assistant(2881, "s-t2", [("Read", {"file_path": "/w/proj-b/HANDOFF.md"})], cwd="/w/proj-b"),
        assistant(2882, "s-t2", [("Edit", {"file_path": "/w/proj-b/pipeline.yml"}),
                                 ("Edit", {"file_path": "/w/proj-b/src/app.py"})],
                  cwd="/w/proj-b"),
    ])

    # 5. CONTINUATION: a resumed file that re-serialises session s-cont's first
    #    two responses. Without dedupe on (sessionId, message.id) the token
    #    ledger doubles.
    cont = root / "projects" / "-w-proj-c"
    first = [human(0, "s-cont", "long job", cwd="/w/proj-c"),
             assistant(1, "s-cont", [("Read", {"file_path": "/w/proj-c/x.py"})],
                       mid="msg-cont-1", cwd="/w/proj-c"),
             assistant(2, "s-cont", [("Read", {"file_path": "/w/proj-c/y.py"})],
                       mid="msg-cont-2", cwd="/w/proj-c")]
    write_jsonl(cont / "cont-a.jsonl", first)
    write_jsonl(cont / "cont-b.jsonl", first + [
        assistant(3, "s-cont", [("Write", {"file_path": "/w/proj-c/x.py"})],
                  mid="msg-cont-3", cwd="/w/proj-c")])

    # 6. A session with real-looking secrets, so --redact has something to hide.
    secrets = root / "projects" / "-w-proj-d"
    write_jsonl(secrets / "sec.jsonl", [
        human(0, "s-sec", "check https://portal.example.com/x and mail ops@example.com",
              cwd="/w/proj-d/6f1e0c4a-6a71-4a5f-9a1c-2b7d8e9f0a1b"),
        assistant(1, "s-sec",
                  [("Bash", {"command": "curl -H 'Authorization: sk-abcdefghijklmnopqrstuvwx' "
                                        "https://portal.example.com/api > /w/proj-d/out.json"})],
                  cwd="/w/proj-d/6f1e0c4a-6a71-4a5f-9a1c-2b7d8e9f0a1b"),
        assistant(2, "s-sec", [("Edit", {"file_path": "/w/proj-d/main.py"})],
                  cwd="/w/proj-d/6f1e0c4a-6a71-4a5f-9a1c-2b7d8e9f0a1b"),
    ])

    # 7. Two rate-limit refusals, for the meter card.
    write_jsonl(projects / "limit.jsonl", [
        human(600, "s-limit", "keep going"),
        {"type": "assistant", "sessionId": "s-limit", "cwd": "/w/proj-a",
         "timestamp": (T0 + dt.timedelta(minutes=601)).isoformat().replace("+00:00", "Z"),
         "error": "rate_limit", "isApiErrorMessage": True, "apiErrorStatus": 429,
         "message": {"id": "msg-limit-1", "model": "<synthetic>",
                     "usage": {"input_tokens": 0, "output_tokens": 0,
                               "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0},
                     "content": [{"type": "text",
                                  "text": "You've hit your session limit · resets 10:20pm (UTC)"}]}},
        assistant(602, "s-limit", [("Write", {"file_path": "/w/proj-a/z.txt"})]),
    ])


def build_handoff_history(root):
    """Two synthetic threads the handoff fixture CSVs join against.

    Both threads run: a first session, then a successor after a NOTE end, then
    a successor after a NONE end. The NOTE successors open HANDOFF.md at turn 1
    and mutate immediately (cheap orientation); the NONE successors read around
    first (expensive orientation). SYNTHETIC — never a real row (a real field
    CSV can carry project names).
    """
    for name, note_first in (("alpha", True), ("beta", False)):
        cwd = "/w/handoff-%s" % name
        folder = root / "projects" / ("-w-handoff-" + name)
        shared = [("Read", {"file_path": "%s/pipeline.yml" % cwd}),
                  ("Read", {"file_path": "%s/main.py" % cwd})]
        # the predecessor: establishes the thread's shared resource tokens
        write_jsonl(folder / "s1.jsonl", [
            human(0, "s-%s-1" % name, "start the %s work" % name, cwd=cwd),
            assistant(1, "s-%s-1" % name, shared[:1], cwd=cwd),
            assistant(2, "s-%s-1" % name, shared[1:], cwd=cwd),
            assistant(3, "s-%s-1" % name, [("Edit", {"file_path": "%s/main.py" % cwd})], cwd=cwd),
            assistant(4, "s-%s-1" % name, [("Write", {"file_path": "%s/HANDOFF.md" % cwd})], cwd=cwd),
        ])
        cheap = [                                   # a NOTE-arm successor
            human(1440, "s-%s-note" % name, "carry on", cwd=cwd),
            assistant(1441, "s-%s-note" % name,
                      [("Read", {"file_path": "%s/HANDOFF.md" % cwd})], cwd=cwd),
            assistant(1442, "s-%s-note" % name,
                      [("Edit", {"file_path": "%s/pipeline.yml" % cwd}),
                       ("Edit", {"file_path": "%s/main.py" % cwd})], cwd=cwd),
        ]
        # a NONE-arm successor: no note to find, so it re-reads the thread's
        # own files and casts about before it edits anything.
        dear = [
            human(1440, "s-%s-none" % name, "carry on", cwd=cwd),
            assistant(1441, "s-%s-none" % name,
                      [("Read", {"file_path": "%s/pipeline.yml" % cwd})], cwd=cwd),
            assistant(1442, "s-%s-none" % name,
                      [("Read", {"file_path": "%s/main.py" % cwd})], cwd=cwd),
        ] + [assistant(1443 + i, "s-%s-none" % name,
                       [("Read", {"file_path": "%s/f%d.py" % (cwd, i)})], cwd=cwd)
             for i in range(3)] + [
            assistant(1447, "s-%s-none" % name,
                      [("Edit", {"file_path": "%s/main.py" % cwd})], cwd=cwd)]
        # arm order differs per thread so the join is doing real work
        first, second = (cheap, dear) if note_first else (dear, cheap)
        for offset, rows in ((1440, first), (4320, second)):
            shifted = []
            for row in rows:
                copy = json.loads(json.dumps(row))
                stamp = dt.datetime.fromisoformat(copy["timestamp"].replace("Z", "+00:00"))
                copy["timestamp"] = (stamp + dt.timedelta(minutes=offset - 1440)
                                     ).isoformat().replace("+00:00", "Z")
                copy["sessionId"] = "%s-%d" % (copy["sessionId"], offset)
                shifted.append(copy)
            write_jsonl(folder / ("s-%d.jsonl" % offset), shifted)


def ns(**kwargs):
    base = dict(root=None, store=None, vscode_root=[], copilot_root=[], since=None,
                until=None, include_cwd_pattern=[], exclude_cwd_pattern=[],
                pricing_file=None, gap_hours=72.0, min_shared=2, max_sessions=12,
                bucket_hours=5.0, max_files=None, progress=False, explain_caps=False,
                redact=False, top=25, json_out=None, label=None,
                handoff_log=None, minute_rate_usd=100.0,
                resume_gap_hours=op.RESUME_GAP_HOURS)
    base.update(kwargs)
    import argparse
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------
def main():
    tmp = Path(tempfile.mkdtemp(prefix="orient-test-"))
    # Rendered output shows the home folder as `~`. Pin it, so a developer's
    # own home never changes what a check sees.
    for var in ("HOME", "USERPROFILE"):
        os.environ[var] = str(tmp / "home" / "homeowner7")
    try:
        root = tmp / "claude"
        build_history(root)
        prices = op.load_prices(None)
        args = ns(root=str(root), store=["claude-code"])
        sessions, stats = op.collect_sessions(args, prices)
        index = {s.session_id: s for s in sessions}

        print("\n[boundary and strategy]")
        scripted = index["s-scripted"]
        check("scripted: boundary at turn 2 (0-indexed)",
              scripted.metrics["first_mutation_turn"] == 2,
              scripted.metrics["first_mutation_turn"])
        check("scripted: boundary reason is the redirect, named",
              scripted.metrics["first_mutation_reason"] == "redirect",
              scripted.metrics["first_mutation_reason"])
        check("scripted: strategy == scripted",
              scripted.metrics["strategy"] == "scripted", scripted.metrics["strategy"])
        check("scripted: orientation share is 2 turns of 6",
              abs(scripted.metrics["orientation_share"] - 2 / 6.0) < 0.02,
              scripted.metrics["orientation_share"])

        byhand = index["s-byhand"]
        check("by-hand: strategy == by-hand",
              byhand.metrics["strategy"] == "by-hand", byhand.metrics["strategy"])
        check("by-hand: 60 edit calls counted", byhand.metrics["edit_calls"] == 60,
              byhand.metrics["edit_calls"])
        check("by-hand: orientation share is small (1 turn of 61)",
              byhand.metrics["orientation_share"] < 0.05, byhand.metrics["orientation_share"])

        readonly = index["s-readonly"]
        check("read-only: no_mutation bucket", readonly.metrics["no_mutation"] is True)
        check("read-only: share is None, not a fake boundary",
              readonly.metrics["orientation_share"] is None)
        check("read-only: strategy == read-only",
              readonly.metrics["strategy"] == "read-only", readonly.metrics["strategy"])
        check("read-only: a `grep | head` is NOT a mutation",
              readonly.metrics["first_mutation_turn"] is None)

        print("\n[handoff vs README]")
        # A README read is orientation WORK; a HANDOFF read is the substitute
        # for it. Pooled, the lever can look like the cost, so the mediator
        # excludes README.
        readme = root / "projects" / "-w-proj-f"
        write_jsonl(readme / "r.jsonl", [
            human(0, "s-readme", "what is this repo", cwd="/w/proj-f"),
            assistant(1, "s-readme", [("Read", {"file_path": "/w/proj-f/README.md"})],
                      cwd="/w/proj-f"),
            assistant(2, "s-readme", [("Edit", {"file_path": "/w/proj-f/a.py"})], cwd="/w/proj-f"),
        ])
        got, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                        include_cwd_pattern=["proj-f"]), op.load_prices(None))
        check("a README read is NOT the handoff mediator",
              got[0].metrics["handoff_opened_by_turn_2"] is False)
        check("but it is kept as its own column",
              got[0].metrics["readme_opened_by_turn_2"] is True)
        check("and the pooled field still says yes, so the change is auditable",
              got[0].metrics["notes_opened_by_turn_2"] is True)

        print("\n[notes opened by turn 2]")
        t2 = index["s-t2"]
        check("successor opened HANDOFF.md by turn 2",
              t2.metrics["handoff_opened_by_turn_2"] is True)
        check("the handoff file is named in full-fidelity output",
              str(t2.metrics["handoff_file"]).endswith("HANDOFF.md"), t2.metrics["handoff_file"])
        check("predecessor did not open a note by turn 2",
              index["s-t1"].metrics["handoff_opened_by_turn_2"] is False)
        check("a Write of HANDOFF.md at turn 5 is not an 'opened' note",
              index["s-t1"].metrics["handoff_file"] is None)

        print("\n[continuation dedupe]")
        cont = index["s-cont"]
        check("continuation joined into one session, 3 turns not 5",
              cont.metrics["turns"] == 3, cont.metrics["turns"])
        check("duplicate rows were counted and removed",
              cont.duplicate_rows == 2, cont.duplicate_rows)
        naive = 5 * (10 + 500 + 20_000 + 1_000)
        check("token ledger is the deduped one, not the naive sum",
              cont.metrics["total_tokens"] < naive, (cont.metrics["total_tokens"], naive))

        print("\n[threads]")
        threads = op.build_threads(sessions, 72.0, 2, 12)
        multi = [t for t in threads if len(t.sessions) > 1]
        check("exactly one multi-session thread", len(multi) == 1,
              [(t.id, [m.session_id for m in t.sessions]) for t in threads])
        thread = multi[0]
        check("the thread is the two proj-b sessions",
              sorted(m.session_id for m in thread.sessions) == ["s-t1", "s-t2"])
        record = op.thread_record(thread, False)
        check("re-orientation tax counts only sessions 2..n",
              record["reorientation_tax_tokens"] == index["s-t2"].metrics["orientation_tokens"],
              (record["reorientation_tax_tokens"], index["s-t2"].metrics["orientation_tokens"]))
        check("thread carries the shared resource tokens",
              "pipeline.yml" in record["shared_resource_tokens"], record["shared_resource_tokens"])
        far = op.build_threads(sessions, 1.0, 2, 12)
        check("a 48-hour gap does not join under --gap-hours 1",
              all(len(t.sessions) == 1 for t in far))
        strict = op.build_threads(sessions, 72.0, 4, 12)
        check("--min-shared 4 breaks the 3-file join",
              all(len(t.sessions) == 1 for t in strict))
        capped = op.build_threads(sessions, 72.0, 2, 1)
        check("--max-sessions 1 caps the thread or prints a justification",
              all(len(t.sessions) == 1 or t.justifications for t in capped))

        print("\n[calibration]")
        result = op.calibrate(threads, {thread.id: "yes"}, sessions, 72.0, 2, 12)
        check("pair precision 1.0 on a coherent label", result["pair_precision"] == 1.0,
              result["pair_precision"])
        check("recall is null and says why", result["recall"] is None and result["recall_note"])
        result2 = op.calibrate(threads, {thread.id: "2"}, sessions, 72.0, 2, 12)
        check("pair precision 0.0 when the label says two pieces of work",
              result2["pair_precision"] == 0.0, result2["pair_precision"])
        check("the sweep suggests thresholds", bool(result["sweep"]) and "suggested" in result)

        print("\n[meter]")
        meter = op.meter_report(sessions, 5.0)
        check("a 429 refusal is picked up as a reading", meter["reading_count"] >= 1,
              meter["reading_count"])
        check("the reading carries the reset text",
              any("resets" in (r.get("reset_text") or "") for r in meter["readings"]))
        check("every reading is dated by session start",
              all(r["dated_by"] == "session-start" for r in meter["readings"]))
        check("5-hour and weekly buckets both exist",
              meter["five_hour_buckets"] and meter["weekly_buckets"])

        print("\n[effort]")
        check("effort is read from the top-level record field",
              scripted.metrics["effort"] == "high", scripted.metrics["effort"])
        check("effort mix counts every turn",
              scripted.metrics["effort_mix"] == {"high": 6}, scripted.metrics["effort_mix"])
        check("per-(model,effort) rollup exists",
              "claude-opus-5|high" in scripted.metrics["per_model_effort"],
              list(scripted.metrics["per_model_effort"]))
        noeffort = op.Session("claude-code", "s-noeffort", "x")
        blank = op.Turn(0, T0, "claude-opus-5", None)
        blank.out, blank.thinking_tokens = 100, 30
        noeffort.turns.append(blank)
        op.finalise(noeffort, prices)
        pte_rows = [human(0, "s-pte", "go", cwd="/w/proj-pte")]
        for minute, tools in ((1, [("Read", {"file_path": "/w/proj-pte/a.py"})]),
                              (2, [("Edit", {"file_path": "/w/proj-pte/a.py"})])):
            row = assistant(minute, "s-pte", tools, cwd="/w/proj-pte")
            row["perTurnEffort"] = row.pop("effort")       # the other field name
            pte_rows.append(row)
        write_jsonl(root / "projects" / "-w-proj-pte" / "p.jsonl", pte_rows)
        pte, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                        include_cwd_pattern=["proj-pte"]), prices)
        check("effort written as `perTurnEffort` is read too",
              pte and pte[0].metrics["effort"] == "high"
              and pte[0].metrics["effort_mix"] == {"high": 2},
              pte and pte[0].metrics["effort_mix"])
        check("an older CLI without `effort` gives None, never a guess",
              noeffort.metrics["effort"] is None and noeffort.metrics["effort_mix"] == {})
        check("the thinking cross-check still works without effort",
              noeffort.metrics["thinking_share"] == 0.3)
        agg_e = op.aggregate(sessions, threads)
        check("aggregate reports the recorded-effort share",
              agg_e["effort_recorded_share"] > 0.95, agg_e["effort_recorded_share"])
        check("aggregate splits thinking share by model and effort",
              any("|high" in k for k in agg_e["median_thinking_share_by_model_effort"]),
              list(agg_e["median_thinking_share_by_model_effort"])[:3])

        print("\n[subagent prompts and search scopes]")
        # A subagent transcript shares its parent's sessionId; its task prompt
        # is written by the parent agent and must never become "the prompt".
        main_rows = [human(0, "s-sc", "fix the module", cwd="/w/proj-sc"),
                     assistant(1, "s-sc", [("Task", {"prompt": "find callers"})], cwd="/w/proj-sc"),
                     assistant(3, "s-sc", [("Edit", {"file_path": "/w/proj-sc/m.py"})],
                               cwd="/w/proj-sc")]
        side_prompt = dict(human(2, "s-sc", "find callers", cwd="/w/proj-sc"),
                           isSidechain=True, agentId="a1")
        side_reply = dict(assistant(2, "s-sc", [("Read", {"file_path": "/w/proj-sc/x.py"})],
                                    cwd="/w/proj-sc", mid="msg-side"),
                          isSidechain=True, agentId="a1")
        write_jsonl(root / "projects" / "-w-proj-sc" / "s-sc.jsonl", main_rows)
        write_jsonl(root / "projects" / "-w-proj-sc" / "s-sc" / "subagents" / "agent-a1.jsonl",
                    [side_prompt, side_reply])
        # An unflagged subagent row in a path that sorts before the main file:
        # only reading main transcripts first keeps the human prompt first.
        write_jsonl(root / "projects" / "-w-proj-sc2" / "zz-main.jsonl",
                    [human(0, "s-sc2", "rename the bucket", cwd="/w/proj-sc2"),
                     assistant(1, "s-sc2", [("Edit", {"file_path": "/w/proj-sc2/b.py"})],
                               cwd="/w/proj-sc2")])
        write_jsonl(root / "projects" / "-w-proj-sc2" / "s-sc2" / "subagents" / "agent-b.jsonl",
                    [human(2, "s-sc2", "list the buckets", cwd="/w/proj-sc2")])
        sub, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                        include_cwd_pattern=["proj-sc"]), prices)
        by_sid = {s.session_id: s for s in sub}
        sc, sc2 = by_sid.get("s-sc"), by_sid.get("s-sc2")
        check("a subagent's task prompt is never the session's first prompt",
              sc is not None and sc.first_prompt == "fix the module",
              sc and sc.first_prompt)
        check("sidechain rows are not counted as human messages",
              sc is not None and [m["text"] for m in sc.user_messages] == ["fix the module"],
              sc and sc.user_messages)
        check("main transcripts are read before subagent transcripts",
              sc2 is not None and sc2.first_prompt == "rename the bucket",
              sc2 and sc2.first_prompt)
        check("a Grep or Glob search folder is not a file",
              op.paths_from_tool("Grep", {"pattern": "x", "path": "/w/proj"}) == []
              and op.paths_from_tool("Glob", {"pattern": "*.py", "path": "/w/proj"}) == [])
        check("a Read path is still a file",
              op.paths_from_tool("Read", {"file_path": "/w/proj/a.py"}) == ["/w/proj/a.py"])

        print("\n[thinking share]")
        thinky = op.Session("claude-code", "s-think", "x")
        turn = op.Turn(0, T0, "claude-opus-5")
        turn.out, turn.thinking_tokens = 1000, 400
        thinky.turns.append(turn)
        op.finalise(thinky, prices)
        check("thinking share = reported/output", thinky.metrics["thinking_share"] == 0.4)
        check("source labelled reported", thinky.metrics["thinking_source"] == "reported")
        turn.thinking_tokens = 0
        turn.thinking_chars = 800
        op.finalise(thinky, prices)
        check("falls back to chars/4 and says so",
              thinky.metrics["thinking_share"] == 0.2
              and thinky.metrics["thinking_source"] == "estimated-from-chars")
        turn.thinking_chars = 0
        op.finalise(thinky, prices)
        check("absent thinking records are None, not a 0 that reads as low effort",
              thinky.metrics["thinking_share"] is None
              and thinky.metrics["thinking_source"] == "absent")

        print("\n[cwd filters]")
        filtered, _ = op.collect_sessions(
            ns(root=str(root), store=["claude-code"], exclude_cwd_pattern=["proj-b"]), prices)
        check("--exclude-cwd-pattern drops the proj-b sessions",
              not any(s.session_id.startswith("s-t") for s in filtered))
        included, _ = op.collect_sessions(
            ns(root=str(root), store=["claude-code"], include_cwd_pattern=["proj-b"]), prices)
        check("--include-cwd-pattern keeps only proj-b",
              sorted(s.session_id for s in included) == ["s-t1", "s-t2"],
              sorted(s.session_id for s in included))

        print("\n[report artefacts]")
        out = tmp / "out"
        rc = op.main(["report", "--root", str(root), "--store", "claude-code",
                      "--output-dir", str(out)])
        check("report rc 0", rc == 0, rc)
        for name in ("orientation.json", "orientation.md",
                     "index.html", "orientation.html", "definitions.html",
                     "ledger.html", "appendix.html"):
            check("wrote %s" % name, (out / name).exists())
        pages = read_pages(out)
        page = "".join(pages[n] for n in PAGE_ORDER if n in pages)
        for name, body in sorted(pages.items()):
            check("%s is self-contained (no external src/href)" % name,
                  not re.search(r'(src|href)\s*=\s*["\']https?://', body))
            check("%s has no external font import" % name,
                  "@import" not in body and "fonts.g" not in body)
            check("%s links back to the home page" % name,
                  name == "index.html" or 'href="index.html"' in body)
            check("%s has a jump link to the definitions" % name,
                  name == "definitions.html" or 'href="definitions.html"' in body)
        check("HTML carries inline SVG", "<svg" in page)
        check("every panel states its decision", page.count("Decides:") >= 7,
              page.count("Decides:"))
        check("the appendix says effort is a RECORDED field, not an inference",
              "Effort is a recorded field" in pages["appendix.html"])
        check("the home page's analysis index lists the unrun analyses",
              "not run — ask your agent" in pages["index.html"])
        check("the index has no button that looks like it runs something",
              "run here" not in pages["index.html"].lower())
        check("light/dark aware", "prefers-color-scheme" in page)
        summary_lines = [l for l in json.loads((out / "orientation.json").read_text(encoding="utf-8"))["summary"].splitlines() if l.strip()]
        check("summary is five lines or fewer", len(summary_lines) <= 5, len(summary_lines))

        check("the report writes no model-tier page",
              not (out / "tiers.html").exists() and "tiers.html" not in page)
        check("the orientation page carries no tier/shift panel",
              "shift" not in pages["orientation.html"].lower(),
              [l for l in pages["orientation.html"].splitlines()
               if "shift" in l.lower()][:2])

        print("\n[the report label names no host]")
        label = json.loads((out / "orientation.json").read_text(encoding="utf-8"))["scope"]["label"]
        check("the default label is generic", label == "this machine", label)
        check("...with no hostname appended to it on any page",
              "this machine (" not in page,
              [l for l in page.splitlines() if "this machine (" in l][:1])
        lout = tmp / "out-label"
        rc = op.main(["report", "--root", str(root), "--store", "claude-code",
                      "--output-dir", str(lout), "--label", "box-a"])
        check("--label still overrides the default",
              rc == 0 and json.loads((lout / "orientation.json").read_text(
                  encoding="utf-8"))["scope"]["label"] == "box-a")

        print("\n[written files are owner-only]")
        if os.name == "posix":
            written = [p for p in out.rglob("*") if p.is_file()]
            loose = sorted(str(p.relative_to(out)) for p in written
                           if p.stat().st_mode & 0o777 != 0o600)
            check("every file `report` writes is 0600", written and not loose, loose[:5])
            dirs = [out] + [p for p in out.rglob("*") if p.is_dir()]
            open_dirs = sorted(str(p) for p in dirs if p.stat().st_mode & 0o777 != 0o700)
            check("every directory `report` creates is 0700", not open_dirs, open_dirs[:5])
            jout = tmp / "json-out.json"
            op.main(["sessions", "--root", str(root), "--store", "claude-code",
                     "--json-out", str(jout)])
            check("--json-out is 0600 too", jout.stat().st_mode & 0o777 == 0o600,
                  oct(jout.stat().st_mode & 0o777))
        else:
            check("file modes are POSIX-only; skipped on %s" % os.name, True)

        print("\n[full fidelity is the DEFAULT]")
        document = json.loads((out / "orientation.json").read_text(encoding="utf-8"))
        blob = json.dumps(document)
        check("default output keeps real cwd paths", "/w/proj-a" in blob)
        check("default output keeps per-session records", bool(document.get("sessions")))
        check("default output keeps the first prompt line",
              any(r.get("first_prompt") for r in document["sessions"]))
        check("default output keeps a Bash command line",
              "sed -i" in blob or "python3 -c" in blob)

        print("\n[--redact is opt-in, and must be clean]")
        rout = tmp / "rout"
        rc = op.main(["report", "--root", str(root), "--store", "claude-code",
                      "--output-dir", str(rout), "--redact"])
        check("redacted report rc 0", rc == 0, rc)
        redacted = ((rout / "orientation.json").read_text(encoding="utf-8")
                    + (rout / "orientation.md").read_text(encoding="utf-8")
                    + "".join(read_pages(rout).values()))
        hits = sorted({m.group(0) for m in op.SECRETISH_RE.finditer(redacted)})
        check("no URL / email / GUID / secret-shaped string in --redact output",
              not hits, hits[:6])
        check("--redact drops per-session records",
              not json.loads((rout / "orientation.json").read_text(encoding="utf-8")).get("sessions"))
        check("--redact hashes the cwd", "/w/proj-a" not in redacted)
        default_blob = ((out / "orientation.json").read_text(encoding="utf-8")
                        + "".join(read_pages(out).values()))
        check("the privacy grep would have FIRED on the default output "
              "(so the test is meaningful, and deliberately not run against it)",
              bool(op.SECRETISH_RE.search(default_blob)))

        print("\n[probe-store]")
        probe = op.probe_store(root / "projects")
        check("probe reports files", probe["files_seen"] > 0)
        check("probe reports key NAMES", bool(probe["top_level_keys"]))
        probe_blob = json.dumps(probe)
        check("probe prints no values", "sed -i" not in probe_blob
              and "rename the config keys" not in probe_blob)
        missing = op.probe_store(tmp / "nope")
        check("probe on a missing store is not fatal", missing["exists"] is False)

        print("\n[other stores are optional and best-effort]")
        empty, vstats = op.ingest_vscode_copilot([tmp / "no-vscode"], None, None, prices)
        check("absent VS Code store yields no sessions and no crash", empty == [])
        empty2, cstats = op.ingest_copilot_cli([tmp / "no-copilot"], None, None, prices)
        check("absent Copilot CLI store yields no sessions and no crash", empty2 == [])
        vs = tmp / "vscode" / "workspaceStorage" / "ws1" / "chatSessions"
        vs.mkdir(parents=True)
        (vs / "a.json").write_text(json.dumps({
            "sessionId": "vs-1", "creationDate": 1788326497785,
            "requests": [{"requestId": "r1", "modelId": "example-model",
                          "message": {"text": "fix the pipeline"},
                          "timestamp": 1788326497785,
                          "response": [{"kind": "toolInvocationSerialized",
                                        "toolId": "editFile",
                                        "toolSpecificData": {"path": "/w/proj-b/pipeline.yml"}}]}],
        }), encoding="utf-8")
        (vs / "b.jsonl").write_text(json.dumps({"kind": 0, "v": {
            "sessionId": "vs-2", "creationDate": 1788412897785,
            "requests": [{"requestId": "r1", "message": {"text": "again"},
                          "timestamp": 1788412897785, "response": []}]}}), encoding="utf-8")
        rows, vstats = op.ingest_vscode_copilot([tmp / "vscode"], None, None, prices)
        check("reads BOTH the old .json and the new .jsonl VS Code stores",
              sorted(s.session_id for s in rows) == ["vs-1", "vs-2"],
              [s.session_id for s in rows])
        check("VS Code tokens are null-ish, not invented",
              all(s.metrics["total_tokens"] == 0 for s in rows))
        check("a session with no token counts has an UNKNOWN est. cost, not $0",
              all(s.metrics["est_cost_usd"] is None for s in rows),
              [s.metrics["est_cost_usd"] for s in rows])
        unpriced_agg = op.aggregate(rows, [])
        check("...it is left out of every median and adds nothing to the total",
              unpriced_agg["median_cost_by_strategy"] == {}
              and unpriced_agg["est_cost_usd_total"] == 0
              and unpriced_agg["median_cost_by_effort"] == {},
              unpriced_agg["median_cost_by_strategy"])
        unpriced_scope = op.build_payload(ns(store=["vscode-copilot"]), rows, [], [],
                                          "test")["scope"]
        check("...and the coverage line names it as unpriced",
              unpriced_scope["sessions_without_token_counts"] == 2
              and "2 sessions record no token counts, so they are unpriced"
              in op.unpriced_note(unpriced_scope)
              and any("unpriced" in line for line in op.scope_block(unpriced_scope)),
              op.unpriced_note(unpriced_scope))
        check("...and it renders as a dash in the strategy matrix, not a $0.00 entry",
              "$0.00" not in op.panel_e_matrix(rows))
        vs1 = [s for s in rows if s.session_id == "vs-1"][0]
        check("VS Code tool invocation became a tool call",
              vs1.metrics["tool_histogram"], vs1.metrics["tool_histogram"])
        cli = tmp / "copilot"
        cli.mkdir()
        (cli / "session-1.json").write_text(json.dumps({
            "sessionId": "cli-1", "cwd": "/w/proj-b", "model": "example-model",
            "createdAt": "2026-09-02T10:00:00Z",
            "messages": [{"role": "user", "content": "resume", "timestamp": "2026-09-02T10:00:00Z"},
                         {"role": "assistant", "timestamp": "2026-09-02T10:01:00Z",
                          "usage": {"input_tokens": 100, "output_tokens": 20},
                          "toolCalls": [{"name": "str_replace",
                                         "arguments": {"file_path": "/w/proj-b/pipeline.yml"}}]}]}),
            encoding="utf-8")
        rows2, _ = op.ingest_copilot_cli([cli], None, None, prices)
        check("Copilot CLI session read", [s.session_id for s in rows2] == ["cli-1"],
              [s.session_id for s in rows2])
        check("Copilot CLI first prompt captured",
              rows2 and rows2[0].first_prompt == "resume", rows2 and rows2[0].first_prompt)

        print("\n[CLI surface]")
        for argv in (["sessions", "--root", str(root)],
                     ["threads", "--root", str(root)],
                     ["threads", "--root", str(root), "--label-candidates", "2"],
                     ["meter", "--root", str(root)],
                     ["ingest", "--root", str(root)]):
            rc = op.main(argv)
            check("`%s` rc 0" % " ".join(argv[:2]), rc == 0, rc)

        # ---- the handoff experiment, regressions, carry or restart -------
        build_handoff_history(root)
        handoff_checks(root, regenerate="--regenerate-expected" in sys.argv)
        prices = op.load_prices(None)
        every, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"]), prices)
        borrow_checks(every, op.build_threads(every, 72.0, 2, 12), root)
        sqlite_checks(root)
        structure_checks(root, tmp)
        accounting_checks(root, tmp)
        accounting_checks_more(root, tmp)
        pickup_checks(root, tmp)
        report_text_checks(tmp)
        home_path_checks(tmp)
        ledger_index_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print("FAILED %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


def structure_checks(root, tmp):
    """The page set, the streams, the ledger, and the two things that must
    never appear again (`proxy`, `<synthetic>`)."""
    print("\n[report structure]")
    out = Path(tmp) / "structure"
    ledger = out / "sessions.jsonl"
    rc = op.main(["report", "--root", str(root), "--store", "claude-code",
                  "--output-dir", str(out), "--ledger", str(ledger)])
    check("report with --ledger rc 0", rc == 0, rc)
    pages = read_pages(out)
    page = "".join(pages[n] for n in PAGE_ORDER if n in pages)
    md = (out / "orientation.md").read_text(encoding="utf-8")

    # -- the two prohibitions, as literal greps ---------------------------
    check("no `proxy` anywhere in the HTML (a subscription word that reads as "
          "wrong on API billing)",
          "proxy" not in page.lower(),
          [line for line in page.splitlines() if "proxy" in line.lower()][:2])
    check("no `proxy` in the Markdown either", "proxy" not in md.lower())
    for name in sorted(p.name for p in (out / "screen-pack").iterdir()):
        body = (out / "screen-pack" / name).read_text(encoding="utf-8")
        if "proxy" in body.lower():
            check("no `proxy` in screen-pack page %s" % name, False)
            break
    else:
        check("no `proxy` on any screen-pack page", True)
    check("`est. cost` is the wording that replaced it",
          "est. cost" in page.lower() or "estimated cost at list api rates" in page.lower())

    # `<synthetic>` may be NAMED (as an exclusion note) but must never be a
    # value in a table or a model list.
    document = json.loads((out / "orientation.json").read_text(encoding="utf-8"))
    every_model = {m for r in document["sessions"] for m in (r.get("models") or [])}
    check("`<synthetic>` is not in any session's model list", "<synthetic>" not in every_model,
          sorted(every_model))
    check("...and it is filtered from the aggregates too",
          not any("synthetic" in k for k in document["aggregates"]["median_thinking_share_by_model"]),
          document["aggregates"]["median_thinking_share_by_model"])
    check("but the report SAYS it is excluded, rather than silently dropping it",
          "synthetic" in page)

    # -- the page SET, and the order within each page ---------------------
    # One document became five. The order inside
    # orientation.html is still the deliverable -- the metric is taught before
    # the first real number, and the terms are defined before Decision 2's
    # table -- and the home page leads with the analysis index.
    home = pages["index.html"]
    check("the home page leads with the answer; the analysis menu comes BELOW it",
          0 <= home.find('class="pointer"') < home.find("Everything this skill can measure"),
          (home.find('class="pointer"'), home.find("Everything this skill can measure")))
    check("...and the answer is drawn: stat tiles and a headline chart above the text",
          0 <= home.find('class="metric"') < home.find("<svg") < home.find('class="pointer"'))
    check("the sceptic questions and the definition check moved to the appendix, collapsed",
          "sceptic" not in pages["index.html"].lower()
          and "Five questions a sceptic asks first" in pages["appendix.html"]
          and "Does the ranking survive a change of definition" in pages["appendix.html"])
    check("the home page points at the biggest finding in one line, not the five lines",
          home.count('class="pointer"') == 1 and "In five lines" not in home
          and 'href="orientation.html#repeat-startup"' in home
          and 'id="repeat-startup"' in pages["orientation.html"],
          re.findall(r'<p class="pointer">.*?</p>', home))
    check("the pointer names the work item and where its repeat startup came",
          re.search(r'Most repeat startup: <strong>[^<]+</strong> — [\d.]+[KMB]? tokens, '
                    r'(mostly|all) (after long breaks|inside sittings)', home) is not None,
          re.findall(r'<p class="pointer">.*?</p>', home))
    check("the pale legend swatch has a visible edge",
          "box-shadow:inset 0 0 0 1px var(--rest-edge)" in home
          and home.count("--rest-edge:") == 3)
    orient = pages["orientation.html"]
    positions = {name: orient.find(marker) for name, marker in (
        ("explainer", "How to read startup share"),
        ("decision1", "Where the startup money went"),
        ("terms", "Work items, sittings and repeat startup"),
        ("timeline", "Work items over time"),
        ("decision2", "Which work items pay the most repeat startup"))}
    check("every section of the orientation page is present",
          all(v >= 0 for v in positions.values()),
          {k: v for k, v in positions.items() if v < 0})
    order = ["explainer", "decision1", "terms", "timeline", "decision2"]
    check("stream/episode/thread and the two taxes are DEFINED, and the "
          "timeline drawn, before Decision 2's first number",
          [positions[k] for k in order] == sorted(positions[k] for k in order),
          {k: positions[k] for k in order})
    check("the appendix page holds what has not been shown, not the decision page",
          "What has not been shown" in pages["appendix.html"]
          and "A0 · What has not been shown" not in orient)
    check("the definitions page exists and defines both repeat-startup figures",
          "repeat startup" in pages["definitions.html"]
          and "incl. long breaks" in pages["definitions.html"])
    check("the meter panel is GONE from the report",
          "Usage meter — 5-hour" not in page and "5-hour bucket" not in page)
    check("...and the report says why, rather than just omitting it",
          "API billing" in page and "meter" in page)
    check("the meter SUBCOMMAND still works for a subscription plan",
          op.main(["meter", "--root", str(root), "--store", "claude-code"]) == 0)

    # -- the explainer, and the corrected numbers -------------------------
    check("the explainer is drawn on TOY data, before any real number",
          "toy" in orient.lower() and positions["explainer"] < positions["decision1"])
    # The toy is a toy: it must not present its 30 % / ~0 as a measurement,
    # and the report quotes no reference value taken from anybody's runs --
    # a number like that reads as a threshold, and none has been validated.
    explainer = op.panel_explainer()
    check("the explainer's toy values are not presented as measured",
          "measured" not in explainer.lower() and "median" not in explainer.lower(),
          [line for line in explainer.split("><") if "measured" in line.lower()][:2])
    for name, body in sorted(list(pages.items()) + [("orientation.md", md)]):
        lowered = body.lower()
        check("no outside reference value on %s" % name,
              "reference value" not in lowered and "reference median" not in lowered,
              [l for l in body.splitlines() if "reference " in l.lower()][:1])
    check("...and the explainer links to what has NOT been shown",
          'appendix.html#not-shown' in orient and 'id="not-shown"' in pages["appendix.html"])

    # -- bands: REMOVED ---------------------------------------------------
    # FAILS IF REVERTED. No outcome- or cost-validated threshold exists, so
    # painting healthy/watch/expensive bands under the distribution would tell
    # the reader a session was "healthy" when nothing here establishes that.
    for name, body in sorted(pages.items()):
        if name == "appendix.html":
            continue                    # the no-threshold sentence lives here
        for word in ("healthy", "expensive"):
            check("no normative '%s' band on %s" % (word, name),
                  word not in body.lower(),
                  [l for l in body.splitlines() if word in l.lower()][:1])
    check("the appendix keeps the no-threshold sentence",
          "no outcome-validated threshold exists yet" in pages["appendix.html"])
    check("the distribution panel draws no band, and says why",
          "no outcome-validated threshold exists yet" in op.panel_a_histogram([])
          and 'data-tip="healthy' not in op.panel_a_histogram([]))
    # The report must not tell anyone to write a five-line handoff: that
    # length is a habit, not a published result, and nothing here shows that
    # any handoff advice helps.
    for name, body in sorted(pages.items()):
        check("no 'five-line'/'5-line' prescription on %s" % name,
              "five-line" not in body.lower() and "5-line" not in body.lower(),
              [l for l in body.splitlines()
               if "five-line" in l.lower() or "5-line" in l.lower()][:1])
    check("no 'five-line'/'5-line' in the Markdown either",
          "five-line" not in md.lower() and "5-line" not in md.lower())
    check("what replaced it states the observation, the condition and how to "
          "measure it",
          all(bit in orient for bit in ("What this shows",
                                        "What it doesn't show",
                                        "--handoff-log")),
          [bit for bit in ("What this shows", "What it doesn't show", "--handoff-log")
           if bit not in orient])
    for share, band in ((0.05, "0-10 %"), (0.30, "30-40 %"), (0.90, "90-100 %"),
                        (None, "no edit")):
        check("share %s reads as the neutral position %s" % (share, band),
              op._band_of(share) == band, op._band_of(share))

    # -- definitions ------------------------------------------------------
    check("every metric has a sentence, a direction and a band",
          all(len(row) == 4 and all(str(value).strip() for value in row)
              for row in op.DEFINITIONS), op.DEFINITIONS)
    check("the share definition is in plain words, not its own name",
          any("100 tokens" in row[1] for row in op.DEFINITIONS))
    check("repeat startup is a per-sitting amount, share a per-session fraction",
          any("per-sitting amount" in row[1] for row in op.DEFINITIONS)
          and any("Of every 100 tokens" in row[1] for row in op.DEFINITIONS))

    # -- the five-line rule still holds -----------------------------------
    lines = [l for l in document["summary"].splitlines() if l.strip()]
    check("the answer is five lines or fewer", len(lines) <= 5, len(lines))
    check("the answer does not end in an unproven instruction",
          not lines[-1].lower().startswith(("do this", "write a")), lines[-1])
    check("...it ends by naming what the number would depend on",
          "depends on" in lines[-1], lines[-1])

    print("\n[streams]")
    every, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"]),
                                   op.load_prices(None))
    threads = op.build_threads(every, 72.0, 2, 12)
    streams, stats = op.build_streams(threads, "auto", [])
    check("every thread lands in exactly one stream",
          sorted(t.id for s in streams for t in s.threads) == sorted(t.id for t in threads),
          (len(streams), len(threads)))
    check("no session is lost or duplicated",
          sum(len(s.sessions) for s in streams) == sum(len(t.sessions) for t in threads))
    check("every stream says WHAT named it",
          all(s.named_by in ("folder", "branch", "ticket") for s in streams),
          sorted({s.named_by for s in streams}))
    check("the rule note says cwd alone is not a link",
          "The folder alone is not enough" in stats["rule_note"], stats["rule_note"])
    check("join counts are reported per rule, so an over-joining rule is visible",
          isinstance(stats["merges_by_rule"], dict))

    # cwd alone must NOT join: many people run every stream out of one
    # directory.
    shared = root / "projects" / "-w-onefolder"
    write_jsonl(shared / "a.jsonl", [
        human(0, "s-one-a", "APP-4001 fix the ingest job", cwd="/w/onefolder"),
        assistant(1, "s-one-a", [("Read", {"file_path": "/w/onefolder/ingest.py"})],
                  cwd="/w/onefolder"),
        assistant(2, "s-one-a", [("Edit", {"file_path": "/w/onefolder/ingest.py"})],
                  cwd="/w/onefolder")])
    write_jsonl(shared / "b.jsonl", [
        human(20_000, "s-one-b", "APP-9002 unrelated billing export", cwd="/w/onefolder"),
        assistant(20_001, "s-one-b", [("Read", {"file_path": "/w/onefolder/billing.py"})],
                  cwd="/w/onefolder"),
        assistant(20_002, "s-one-b", [("Edit", {"file_path": "/w/onefolder/billing.py"})],
                  cwd="/w/onefolder")])
    write_jsonl(shared / "c.jsonl", [
        human(40_000, "s-one-c", "back on APP-4001, the ingest job again", cwd="/w/onefolder"),
        assistant(40_001, "s-one-c", [("Read", {"file_path": "/w/onefolder/other.py"})],
                  cwd="/w/onefolder"),
        assistant(40_002, "s-one-c", [("Edit", {"file_path": "/w/onefolder/other.py"})],
                  cwd="/w/onefolder")])
    one, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                    include_cwd_pattern=["onefolder"]), op.load_prices(None))
    one_threads = op.build_threads(one, 72.0, 2, 12)
    auto, auto_stats = op.build_streams(one_threads, "auto", [])
    by_cwd, _ = op.build_streams(one_threads, "cwd", [])
    ticket_of = {s.id: s for s in auto}
    check("three sittings in ONE folder are three threads", len(one_threads) == 3,
          len(one_threads))
    check("--stream-by cwd fuses all three (the naive key, and why it is not "
          "the default)", len(by_cwd) == 1, len(by_cwd))
    check("auto does NOT fuse them on cwd alone: two streams, not one",
          len(auto) == 2, [(s.id, s.named_by, s.name, len(s.threads)) for s in auto])
    joined = [s for s in auto if len(s.threads) == 2]
    check("the two APP-4001 sittings ARE joined, across a 13-day gap, by their "
          "ticket token", len(joined) == 1 and joined[0].named_by == "ticket",
          [(s.named_by, s.name) for s in auto])
    check("and the stream is NAMED by the ticket that joined it",
          joined and joined[0].name == "jira:APP-4001", joined[0].name if joined else None)
    check("the unrelated billing work stays its own stream",
          any(len(s.threads) == 1 for s in auto))

    check("--stream-ignore-token switches an over-joining token off",
          len(op.build_streams(one_threads, "auto", ["APP-4001"])[0]) == 3,
          len(op.build_streams(one_threads, "auto", ["APP-4001"])[0]))

    # ONE case for every supported tracker and infrastructure CLI.
    tix = op.Session("claude-code", "s-tix", "x")
    tix.title = "#1234 PR 77 APP-12 feature/login-form AB#4001 work item 55"
    check("every ticket pattern is recognised: GitHub, PR, Jira/Linear, branch, Azure Boards",
          op.ticket_tokens(tix) == {
              "hash": "hash:1234", "pr": "pr:77", "jira": "jira:APP-12",
              "branch-token": "branch-token:feature/login-form",
              "azure-boards": "azure-boards:4001", "work-item": "work-item:55"},
          op.ticket_tokens(tix))
    check("every infrastructure CLI write is an edit, its reads are not",
          op.bash_mutation_reason("kubectl apply -f app.yaml") == "kubectl apply"
          and op.bash_mutation_reason("az deployment group create -f main.json") == "az deploy"
          and op.bash_mutation_reason("terraform state rm aws_s3_bucket.b") == "terraform state"
          and op.bash_mutation_reason("kubectl get pods") is None
          and op.bash_mutation_reason("az vm show -n web") is None
          and op.bash_mutation_reason("terraform state list") is None)

    # the stream tax counts what the thread tax forgives
    record = op.stream_record(joined[0], False)
    thread_tax = sum(op.thread_record(t, False)["reorientation_tax_tokens"]
                     for t in joined[0].threads)
    check("the stream tax is >= the sum of its threads' taxes",
          record["reorientation_tax_tokens"] >= thread_tax,
          (record["reorientation_tax_tokens"], thread_tax))
    check("...strictly greater here, because the returning session's "
          "orientation is what the gap costs",
          record["reorientation_tax_tokens"] > thread_tax,
          (record["reorientation_tax_tokens"], thread_tax))
    check("the gap between episodes is reported in days",
          record["gaps_days"] and record["max_gap_days"] > 1, record["gaps_days"])
    check("a generic branch carries no stream information",
          "main" in op.GENERIC_BRANCHES and "master" in op.GENERIC_BRANCHES)

    print("\n[the session ledger]")
    import session_ledger
    rows, provenance = session_ledger.load(ledger)
    check("the ledger has one row per session",
          len(rows) == len(document["sessions"]), (len(rows), len(document["sessions"])))
    check("orientation columns are populated",
          any(r.get("orientation_position") for r in rows.values()))
    check("thread and stream ids travel with the row",
          all("thread_id" in r and "stream_id" in r for r in rows.values()))
    check("every column names its producer",
          all(meta.get("producer") for meta in provenance["columns"].values()))
    check("analyses that have NOT run here are registered, not invented",
          any(meta.get("updated") is None for meta in provenance["columns"].values()))
    stats = session_ledger.summary(ledger)
    check("a registered-but-unrun column has zero values",
          all(stats["coverage"][c] == 0 for c in stats["registered_not_run"]),
          stats["registered_not_run"])
    check("the report's browser reads the LEDGER, not the in-memory sessions",
          "written by" in pages["ledger.html"]
          or "orientation_profile.py" in pages["ledger.html"])
    check("the analysis index names the ledger before the analyses not run here",
          pages["index.html"].index("Session ledger")
          < pages["index.html"].index("Cache lifetime"))
    check("every analysis row says which columns it adds",
          "adds ledger columns:" in pages["index.html"])
    # The ledger is its own page, sortable, and narrow by default.
    ledger_page = pages["ledger.html"]
    check("the ledger has its own page", "One row per session" in ledger_page)
    check("...with a show-all-columns toggle",
          'data-colstoggle="ledgertable"' in ledger_page)
    check("...and at most 10 columns visible by default",
          len(op.LEDGER_DEFAULT_COLUMNS) <= 10, len(op.LEDGER_DEFAULT_COLUMNS))
    head = re.search(r"<thead><tr>(.*?)</tr></thead>", ledger_page, re.S)
    check("the extra columns are hidden, not dropped",
          head and 'class="x' in head.group(1))
    # The sort JS took its column index from a document-wide NodeList, so on
    # any table but the first it sorted by the wrong column. Guard the fix.
    check("the sort handler reads the column index inside its own row "
          "(so a second table on a page is sortable)",
          "indexOf.call(th.parentNode.children,th)" in op.JS,
          [l for l in op.JS.splitlines() if "indexOf" in l or "forEach(function(th" in l])

    # a second producer must not be able to destroy the first's columns
    session_ledger.upsert(ledger, [{"session_id": next(iter(rows)), "top_tool": "Bash"}],
                          "tool_bloat.py", ["top_tool"])
    after, _ = session_ledger.load(ledger)
    check("a second producer's upsert keeps the orientation columns",
          after[next(iter(rows))].get("orientation_position") is not None)
    check("and adds its own", after[next(iter(rows))].get("top_tool") == "Bash")

    # SCOPE. The ledger is a union across producers that do NOT share filters.
    # Found on the first real three-producer run: the ledger came back with
    # several times the sessions the orientation pass saw, because tool_bloat
    # and the cache audit ran with a --since instead. The browser must show THIS
    # report's sessions and say what else is in the file.
    session_ledger.upsert(ledger, [{"session_id": "not-in-this-report",
                                    "tool_result_bytes": 1}],
                          "tool_bloat.py", ["tool_result_bytes"],
                          scope="--since 1999-01-01")
    mine = [r["session_id"] for r in document["sessions"]]
    browser = op.ledger_browser(str(ledger), mine)
    check("the browser does NOT silently widen the report's scope",
          "not-in-this-report" not in browser)
    check("...and says how many out-of-scope rows the file holds",
          "session(s) written by another analysis" in browser, browser[:200])
    check("...naming the filter each producer ran under",
          "--since 1999-01-01" in browser)
    check("with no filter argument it shows everything, unchanged",
          "not-in-this-report" in op.ledger_browser(str(ledger)))
    check("the scope is recorded per producer in the provenance sidecar",
          session_ledger.summary(ledger)["scopes"].get("orientation_profile.py"),
          session_ledger.summary(ledger)["scopes"])

    # ==================================================================
    # The chart checks.
    #
    # The bar colours. A reader read the explainer bar backwards and was right to: the legend
    # said "grey = tokens spent BEFORE the first change", and the segment the
    # 30 % label pointed at was drawn BLUE. The rule this check enforces is
    # the one that would have caught it -- in every toy bar in the document,
    # the ORIENTATION segment is the LEFT one and it is grey.
    #
    # The wrapping. Every clipped line ("opened a handoff note first -- measus") was an
    # SVG <text> running past its viewBox, because SVG does not wrap. The
    # check here is geometric but arithmetic, not rendered: no wrapped line,
    # at the wrap width it was given, can be wider than the box it sits in.
    # The RENDERED confirmation is check_report_overflow.py, which needs a
    # browser and so cannot live in a unit test -- see its docstring.
    # ==================================================================
    print("\n[charts]")
    explainer_svg = op.panel_explainer()
    rects = re.findall(r'<rect data-seg="(whole|startup)"[^>]*x="([\d.]+)"'
                       r'[^>]*fill="([^"]+)"', explainer_svg)
    pairs = [(rects[i], rects[i + 1]) for i in range(0, len(rects) - 1, 2)]
    check("every toy bar draws a work segment and an orientation segment",
          pairs and all(a[0] == "whole" and b[0] == "startup" for a, b in pairs),
          rects[:4])
    check("the orientation segment starts at the bar's LEFT edge — time "
          "runs left to right",
          all(float(a[1]) == float(b[1]) for a, b in pairs),
          [(a[1], b[1]) for a, b in pairs][:4])
    check("the orientation segment is GREY, matching the word the legend "
          "uses for it",
          all(b[2] == op.TOY_GREY for a, b in pairs), sorted({b[2] for a, b in pairs}))
    check("the work segment is BLUE, matching the word the legend uses "
          "for it",
          all(a[2] == op.TOY_BLUE for a, b in pairs), sorted({a[2] for a, b in pairs}))
    check("the legend names grey for the tokens before the first change",
          "grey — startup: tokens before the first edit" in op.TOY_LEGEND)
    check("...and blue for the work", "blue — the work" in op.TOY_LEGEND)
    check("no sentence in the explainer calls the orientation segment blue",
          "blue is the price" not in explainer_svg.lower()
          and "first blue is the price" not in explainer_svg.lower())
    # Each wrapped line is its own <tspan>, so tags become a SPACE here or
    # the last word of one line fuses with the first word of the next.
    explainer_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", explainer_svg))
    check("the 'price of new work' sentence points at a GREY segment",
          "first grey segment is the price of new work" in explainer_text,
          explainer_text[-260:])

    # The wrapper itself, then every panel that uses it.
    check("wrap_lines breaks a long string into lines that fit",
          all(len(line) * 12.0 * op.CHAR_EM <= 200 + 1
              for line in op.wrap_lines("a " * 200, 200, 12.0)),
          op.wrap_lines("a " * 40, 200, 12.0)[:3])
    check("a single unbreakable word is emitted rather than dropped",
          op.wrap_lines("x" * 90, 100, 12.0) == ["x" * 90])
    check("text_block emits one tspan per line, all at the same x",
          op.text_block(20, 30, 120, "one two three four five six seven eight nine"
                        ).count('<tspan x="20.0"') >= 2,
          op.text_block(20, 30, 120, "one two three four five six seven eight nine"))

    def overflowing(markup, label):
        """Any <text>/<tspan> whose widest line runs past its own viewBox."""
        bad = []
        for view, body in re.findall(r'<svg viewBox="0 0 (\d+) \d+"[^>]*>(.*?)</svg>',
                                     markup, re.S):
            box = float(view)
            for node in re.finditer(r'<text ([^>]*)>(.*?)</text>', body, re.S):
                attrs, inner = node.group(1), node.group(2)
                cls = (re.search(r'class="([^"]+)"', attrs) or [None, "axis"])[1]
                size = op.SVG_FONT.get(cls, 12.0)
                anchor = (re.search(r'text-anchor="([^"]+)"', attrs) or [None, "start"])[1]
                lines = (re.findall(r"<tspan[^>]*>(.*?)</tspan>", inner, re.S)
                         or [re.sub(r"<[^>]+>", "", inner)])
                xs = [float(m) for m in re.findall(r'x="([\d.]+)"', attrs)] or [0.0]
                for line in lines:
                    text = re.sub(r"&[a-z]+;", "x", line)
                    wide = len(text) * size * op.CHAR_EM
                    left = xs[0] - (wide if anchor == "end" else
                                    wide / 2 if anchor == "middle" else 0.0)
                    if left < -1 or left + wide > box + 1:
                        bad.append((label, round(left), round(left + wide), box,
                                    text[:52]))
        return bad

    panels = {
        "explainer": explainer_svg,
        "tax definitions": op.panel_tax_definitions(),
        "money went": op.panel_money_went(every, False,
                                          first_ids=op.first_in_thread_ids(threads)),
        "stream timeline": op.panel_stream_timeline(streams, False),
        "histogram": op.panel_a_histogram(every),
        "breadth": op.panel_i_breadth(every),
    }
    for label, markup in sorted(panels.items()):
        bad = overflowing(markup, label)
        check("no text overflows its viewBox in the %s panel" % label,
              not bad, bad[:3])

    print("\n[screen pack order]")
    # Read the TITLES out of INDEX.html, not the filenames: the slug is
    # truncated to 40 characters, so a filename check silently tests the
    # truncation rather than the order.
    index_html = (out / "screen-pack" / "INDEX.html").read_text(encoding="utf-8")
    titles = re.findall(r'<a href="(\d\d-[^"]+)"[^>]*>(.*?)</a>', index_html, re.S)
    titles = [re.sub(r"<[^>]+>", "", label).strip() for _, label in titles]
    if not titles:                       # index markup changed: fall back to the pages
        titles = []
        for page_path in sorted((out / "screen-pack").glob("[0-9][0-9]-*.html")):
            match = re.search(r"<title>\d+ (.*?)</title>",
                              page_path.read_text(encoding="utf-8"))
            titles.append(match.group(1) if match else page_path.name)
    check("the pack has titles to check", len(titles) >= 10, titles)
    lower = [x.lower() for x in titles]
    check("the pack leads with inventory, then the ANSWER, then the explainer",
          "inventory" in lower[0] and "answer" in lower[1] and "toy" in lower[2],
          titles[:3])
    decisions = [i for i, x in enumerate(lower) if x.startswith("decision")]
    appendix = [i for i, x in enumerate(lower) if x.startswith("appendix")]
    check("there are three decision pages", len(decisions) >= 3, titles)
    check("every decision page comes before every appendix page",
          appendix and max(decisions) < min(appendix), titles)
    check("the meter page is gone from the pack",
          not any("meter" in x for x in lower), titles)


def report_text_checks(tmp):
    """Rendered text: paths cut at a separator, tokens compact, no build history."""
    print("\n[paths are cut at a separator, never mid-name]")
    deep = "/home/dev/src/example-org/example-app/services/analysis/report_builder"
    check("a long path keeps whole trailing segments behind …/",
          op.short_path(deep, 30) == "…/analysis/report_builder", op.short_path(deep, 30))
    check("a path that fits is left alone", op.short_path("/w/proj-a", 30) == "/w/proj-a")
    check("a Windows path keeps its backslashes",
          op.short_path("C:\\Users\\dev\\Documents\\proj\\sub", 20)
          == "…\\Documents\\proj\\sub",
          op.short_path("C:\\Users\\dev\\Documents\\proj\\sub", 20))
    long_name = "/x/" + "y" * 50
    check("a single over-long name is cut from the left and still marked",
          op.short_path(long_name, 20) == "…" + "y" * 19, op.short_path(long_name, 20))
    check("the result never exceeds the limit",
          all(len(op.short_path(p, n)) <= n for p in (deep, long_name, "/a/b/c")
              for n in (5, 12, 22, 40)))

    root = Path(tmp) / "long-paths" / "claude"
    write_jsonl(root / "projects" / "-home-dev-deep" / "d.jsonl", [
        human(0, "s-deep", "build the report", cwd=deep),
        assistant(1, "s-deep", [("Read", {"file_path": deep + "/main.py"})], cwd=deep,
                  read=33_860_469),
        assistant(2, "s-deep", [("Edit", {"file_path": deep + "/main.py"})], cwd=deep)])
    out = Path(tmp) / "long-paths" / "out"
    rc, stdout = capture(["report", "--root", str(root), "--store", "claude-code",
                          "--output-dir", str(out), "--ledger", str(out / "sessions.jsonl")])
    check("report on a deep working directory rc 0", rc == 0, rc)
    md = (out / "orientation.md").read_text(encoding="utf-8")
    texts = {"orientation.md": md,
             "ledger.html": (out / "ledger.html").read_text(encoding="utf-8"),
             "screen pack": "".join(p.read_text(encoding="utf-8")
                                    for p in sorted((out / "screen-pack").glob("*.html")))}

    def mid_segment_cuts(text):
        """Every displayed form of `deep` that is neither whole nor cut at a '/'."""
        bad = []
        for token in re.findall(r"[^\s|<>\"'(),;=]*report_builder", text):
            if token in (deep, "report_builder"):
                continue
            if token.startswith("…/") and deep.endswith(token[1:]):
                continue
            bad.append(token)
        return sorted(set(bad))

    for name, text in sorted(texts.items()):
        check("%s shows the deep path only whole or as …/<whole segments>" % name,
              "report_builder" in text and not mid_segment_cuts(text),
              mid_segment_cuts(text)[:3])
    check("the Markdown session table uses the …/ form",
          "…/services/analysis/report_builder" in md)
    check("the ledger's cwd column uses the …/ form (the full path stays in data-v)",
          ">%s<" % op.short_path(deep, 60) in texts["ledger.html"]
          and op.short_path(deep, 60).startswith("…/"), op.short_path(deep, 60))

    print("\n[Markdown token counts are compact]")
    # (a dated model id such as claude-haiku-4-5-20251001 is a name, not a count)
    raw = re.findall(r"(?<![\d.-])\d{7,}(?![\d.])", md)
    check("no raw 7+-digit count in orientation.md", not raw, raw[:3])
    check("...the measure table prints 33.9M-style figures",
          re.search(r"\| tokens \| [\d.]+[KMB] \|", md) is not None,
          [l for l in md.splitlines() if l.startswith("| tokens")])

    print("\n[no build history in what a reader sees]")
    pages = read_pages(out)
    rendered = dict(texts, stdout=stdout, **pages)
    for name, text in sorted(rendered.items()):
        hits = [phrase for phrase in ("used to be", "was still allocating", "corrected 20",
                                      "withdrawn", "the old formula", "an earlier version")
                if phrase in text.lower()]
        check("no changelog wording on %s" % name, not hits, hits)


def ledger_index_checks(tmp):
    """An analysis with its own report shows a tick once its columns are in the ledger."""
    print("\n[the index reads the ledger: an analysis that wrote into it ran]")
    import claude_cache_audit
    root = Path(tmp) / "ledger-index" / "claude"
    write_jsonl(root / "projects" / "-w-li" / "l.jsonl", [
        human(0, "s-li", "tidy the module", cwd="/w/li"),
        assistant(1, "s-li", [("Read", {"file_path": "/w/li/a.py"})], cwd="/w/li"),
        assistant(12, "s-li", [("Read", {"file_path": "/w/li/b.py"})], cwd="/w/li"),
        assistant(13, "s-li", [("Edit", {"file_path": "/w/li/a.py"})], cwd="/w/li")])
    out = Path(tmp) / "ledger-index" / "report"
    ledger = Path(tmp) / "ledger-index" / "sessions.jsonl"
    argv = ["report", "--root", str(root), "--store", "claude-code",
            "--output-dir", str(out), "--ledger", str(ledger)]

    def status(title):
        page = (out / "index.html").read_text(encoding="utf-8")
        match = re.search(r"<strong>%s</strong></td><td class=\"l\">[^<]*</td>"
                          r"<td class=\"l\">(.*?)</td></tr>" % re.escape(title), page)
        return re.sub(r"<[^>]+>", "", match.group(1)) if match else None

    capture(argv)
    check("before the cache audit runs, the index says it has not run",
          (status("Cache lifetime") or "").startswith("not run"), status("Cache lifetime"))
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        rc = claude_cache_audit.main(["--root", str(root), "--output-dir",
                                      str(Path(tmp) / "ledger-index" / "cache"),
                                      "--ledger", str(ledger)])
    check("the cache audit writes into the same ledger", rc == 0, rc)
    rc, _ = capture(argv)
    cache = status("Cache lifetime") or ""
    check("after it, the index ticks it: in the ledger, with the date it ran",
          rc == 0 and re.fullmatch(r"✓ in the ledger · ran \d{4}-\d\d-\d\d", cache) is not None,
          cache)
    check("...and links to the ledger page",
          'href="ledger.html">in the ledger</a>' in (out / "index.html").read_text(encoding="utf-8"))
    check("an analysis that wrote nothing into the ledger still says not run",
          (status("Tool-result bloat") or "").startswith("not run"), status("Tool-result bloat"))
    check("one with no --ledger option says it prints its own report",
          (status("Compaction") or "").startswith("prints its own report"), status("Compaction"))
    md = (out / "orientation.md").read_text(encoding="utf-8")
    check("the Markdown index agrees",
          any(line.startswith("| Cache lifetime |") and "in the ledger, ran 20" in line
              for line in md.splitlines()))


def home_path_checks(tmp):
    """The home folder is `~` in every output; --redact drops the store roots."""
    print("\n[the home folder is shown as ~]")
    home = Path(os.path.expanduser("~"))               # pinned by main()
    work = home / "src" / "app"
    root = home / ".claude"
    write_jsonl(root / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(work)) / "h.jsonl", [
        human(0, "s-home", "update the module, see %s" % (work / "notes.md"), cwd=str(work)),
        assistant(1, "s-home", [("Read", {"file_path": str(work / "main.py")})], cwd=str(work)),
        assistant(2, "s-home", [("Edit", {"file_path": str(work / "main.py")})], cwd=str(work))])
    write_jsonl(root / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(home)) / "g.jsonl", [
        human(0, "s-homedir", "tidy the shell config", cwd=str(home)),
        assistant(1, "s-homedir", [("Edit", {"file_path": str(home / ".zshrc")})],
                  cwd=str(home))])

    def everything(out, stdout):
        files = sorted(p for p in out.rglob("*") if p.is_file())
        return stdout + "".join(p.read_text(encoding="utf-8") for p in files), len(files)

    out = Path(tmp) / "home-out"
    rc, stdout = capture(["report", "--root", str(root), "--store", "claude-code",
                          "--output-dir", str(out), "--ledger", str(out / "sessions.jsonl")])
    check("report on a history under the home folder rc 0", rc == 0, rc)
    blob, n_files = everything(out, stdout)
    check("no file (of %d) and no stdout line shows the account name" % n_files,
          home.name not in blob, [line for line in blob.splitlines() if home.name in line][:2])
    work_shown = "~%ssrc%sapp" % (os.sep, os.sep)
    document = json.loads((out / "orientation.json").read_text(encoding="utf-8"))
    check("paths under the home folder read ~/...",
          any(r.get("cwd") == work_shown for r in document["sessions"])
          and work_shown in (out / "ledger.html").read_text(encoding="utf-8"),
          sorted({r.get("cwd") for r in document["sessions"]})[:4])
    check("...in free text too (the first prompt)",
          any(work_shown in (r.get("first_prompt") or "") for r in document["sessions"]))
    check("...and in the store roots",
          any(r.startswith("~") for r in document["scope"]["roots"]), document["scope"]["roots"])
    check("a session run in the home folder is named ~, not the account",
          any(r.get("name") == "~" for r in document["streams"]),
          [r.get("name") for r in document["streams"]][:6])
    def win(text):
        return op.audit_common.replace_home(text, "C:\\Users\\homeowner7")

    check("Windows spellings of the home folder are all caught",
          [win(text) for text in (
              "C:\\Users\\homeowner7\\src", json.dumps("C:\\Users\\homeowner7\\src"),
              "file:///c%3A/Users/homeowner7/src", "projects\\C--Users-homeowner7-src")]
          == ["~\\src", json.dumps("~\\src"), "file:///~/src", "projects\\~-src"])
    elsewhere = "/tmp/C--Users-homeowner7-src/out"
    check("an encoded name outside a projects folder is a real path, left alone",
          win(elsewhere) == elsewhere)
    check("a longer name that starts the same is left alone",
          op.home_relative(str(home) + "-backup/x") == str(home) + "-backup/x")

    red = Path(tmp) / "home-redact"
    rc, stdout = capture(["report", "--root", str(root), "--store", "claude-code",
                          "--output-dir", str(red), "--redact"])
    check("--redact report rc 0", rc == 0, rc)
    document = json.loads((red / "orientation.json").read_text(encoding="utf-8"))
    check("--redact drops the store roots, not just hashes them",
          document["scope"]["roots"] == []
          and not any({"root", "roots"} & set(st) for st in document["store_stats"]),
          document["scope"]["roots"])
    blob, _ = everything(red, stdout)
    check("--redact output carries no Roots line",
          "Roots:" not in blob, [line[:160] for line in blob.splitlines() if "Roots:" in line][:2])


FIXTURES = HERE.parent / "tests" / "fixtures"


def capture(argv):
    """Run the CLI and return (rc, stdout). The expected-output files are
    byte-compared against this, so nothing non-deterministic may leak in."""
    import contextlib
    import io
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = op.main(argv)
    return rc, buffer.getvalue()


def handoff_checks(root, regenerate=False):
    """The handoff-log read-back — the machine-checkable acceptance probe.

    Three fixtures, three states a handoff log really passes through:
    a clean log that pairs, a hand-edited log with exactly three bad lines,
    and a log whose ends have no successor yet.
    """
    print("\n[--handoff-log]")
    base = ["sessions", "--root", str(root), "--store", "claude-code", "--handoff-log"]

    rc, clean = capture(base + [str(FIXTURES / "handoff-clean.csv")])
    check("clean fixture exits 0", rc == 0, rc)
    check("clean fixture finds 4 successors", "successor sessions joined          4" in clean)
    check("clean fixture finds 2 usable pairs",
          re.search(r"usable pairs \(sittings, both arms\)\s+2\b", clean) is not None)
    check("clean fixture prints n, pending and dropped ABOVE the result",
          clean.index("PENDING ends") < clean.index("Wilcoxon"))
    check("clean fixture reports both tests",
          "Wilcoxon signed-rank" in clean and "sign test" in clean)
    check("clean fixture states the conversion rate on the output, not in the code",
          "$100.00/hour" in clean and "--minute-rate-usd" in clean)
    check("clean fixture reports the mediator with a Wilson 90 % CI",
          "note read   in the first two turns" in clean and "Wilson 90 % CI" in clean)

    rc, malformed = capture(base + [str(FIXTURES / "handoff-malformed.csv")])
    check("malformed fixture exits 0", rc == 0, rc)
    check("malformed fixture reports EXACTLY 3 skips",
          "log lines skipped as malformed     3" in malformed, malformed[:400])
    check("each skip names its line number and reason",
          malformed.count("! line ") == 3, malformed.count("! line "))
    check("a blank note_minutes is UNKNOWN, not coerced to 0",
          "never 0" in malformed or "UNKNOWN, never 0" in malformed)

    rc, pending = capture(base + [str(FIXTURES / "handoff-pending.csv")])
    check("pending fixture exits 0", rc == 0, rc)
    check("pending fixture reports 0 pairs and 3 pending",
          re.search(r"usable pairs \(sittings, both arms\)\s+0\b", pending) is not None
          and "PENDING ends                       3" in pending)
    check("pending fixture prints NO comparison",
          "Wilcoxon" not in pending and "sign test" not in pending, pending[-300:])
    check("pending fixture prints the empty state in words",
          "0 successors yet" in pending or "No pair yet" in pending)

    check("underpowered banner is the stated text, verbatim",
          op.UNDERPOWERED_BANNER.splitlines()[0] in clean
          and "underpowered\", not \"no effect\"" in clean)

    for name, text in (("handoff-clean", clean), ("handoff-pending", pending)):
        target = FIXTURES / (name + ".expected.txt")
        if regenerate:
            target.write_text(text, encoding="utf-8")
            print("  regenerated %s" % target)
            continue
        check("%s reproduces its committed expected output" % name,
              target.exists() and target.read_text(encoding="utf-8") == text,
              "run with --regenerate-expected after an intentional change")

    print("\n[statistics]")
    check("sign test matches the stated values: 15 of 20 gives p = 0.041",
          abs(op.sign_test([-1] * 15 + [1] * 5)["p"] - 0.0414) < 5e-4)
    check("sign test matches the stated values: 14 of 20 gives p = 0.115 (not enough)",
          abs(op.sign_test([-1] * 14 + [1] * 6)["p"] - 0.1153) < 5e-4)
    check("sign test matches the stated values: 13 of 16 gives p = 0.021",
          abs(op.sign_test([-1] * 13 + [1] * 3)["p"] - 0.0213) < 5e-4)
    check("Wilcoxon is EXACT at this n, not a normal approximation",
          "exact" in op.wilcoxon_signed_rank([-5, -4, -3, -2, -1])["method"])
    check("Wilcoxon exact p for n=5 all-negative is 1/16",
          abs(op.wilcoxon_signed_rank([-5, -4, -3, -2, -1])["p"] - 0.0625) < 1e-6)
    check("Wilcoxon drops zero differences and says how many",
          op.wilcoxon_signed_rank([0, 0, -3, -2, -1])["zeros_dropped"] == 2)
    check("Wilson 90 % CI on 2/2 does not claim certainty",
          op.wilson(2, 2)[0] < 0.9)

    print("\n[the Experiment panel renders in every state]")
    entries, skipped = op.parse_handoff_log(str(FIXTURES / "handoff-clean.csv"))
    prices = op.load_prices(None)
    sessions, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"]), prices)
    threads = op.build_threads(sessions, 72.0, 2, 12)
    full = op.handoff_report(op.join_handoff_log(entries, threads), skipped, entries, 100.0)
    one = dict(full, pairs=full["pairs"][:1], usable_pairs=1)
    zero = dict(full, pairs=[], usable_pairs=0, successor_rows=0)
    for label, report in (("two pairs", full), ("ONE pair", one), ("ZERO pairs", zero)):
        html = op.panel_h_experiment(report)
        check("panel renders with %s" % label, bool(html) and len(html) > 200)
    check("zero-pair panel carries the empty state in words",
          "0 successors yet" in op.panel_h_experiment(zero))
    check("panel renders with no handoff log at all",
          "nothing to read back" in op.panel_h_experiment(None))
    check("a negative net-value bar is not clipped (symmetric zero-centred scale)",
          "0 — the note exactly paid for itself" in op.panel_h_experiment(full))
    check("arm colours are the categorical pair, not red/green",
          "var(--arm-note)" in op.panel_h_experiment(full)
          and "var(--arm-none)" in op.panel_h_experiment(full))
    below = dict(full)
    below["mediator"] = dict(full["mediator"], rate=0.5, below_floor=True)
    check("a mediator below 80 % carries the caveat INLINE on the tile",
          "Below the 80" in op.panel_h_experiment(below))


def borrow_checks(sessions, threads, root):
    """Ideas borrowed from related tools, the machine-checkable ones."""
    print("\n[borrow list]")
    index = {s.session_id: s for s in sessions}
    scripted = index["s-scripted"]
    # Two pre-boundary tool calls, but only ONE names a path: a Glob
    # `pattern` is a query, not a file. Breadth counts things looked AT.
    check("orientation_breadth counts distinct pre-boundary PATHS, not calls",
          scripted.metrics["orientation_breadth"] == 1,
          scripted.metrics["orientation_breadth"])
    check("a session that re-reads one file has breadth 1 and high tokens/path",
          scripted.metrics["orientation_tokens_per_path"]
          == scripted.metrics["orientation_tokens"])
    byhand = index["s-byhand"]
    check("breadth is measured over the PRE-boundary window only",
          byhand.metrics["orientation_breadth"] == 1
          and len(byhand.files_touched) > 30,
          (byhand.metrics["orientation_breadth"], len(byhand.files_touched)))
    check("tokens-per-path is reported beside it",
          scripted.metrics["orientation_tokens_per_path"] is not None)
    check("every session carries an outcome class",
          all(s.metrics.get("outcome") in ("success", "failure", "unknown") for s in sessions))
    check("`unknown` is a real class, never folded into success",
          "unknown" in {s.metrics["outcome"] for s in sessions})
    check("OTel gen_ai.* names are mirrored exactly",
          set(scripted.metrics["otel"]) >= {
              "gen_ai.agent.name", "gen_ai.request.model", "gen_ai.usage.input_tokens",
              "gen_ai.usage.output_tokens", "gen_ai.usage.cache_read.input_tokens",
              "gen_ai.usage.cache_write.input_tokens", "gen_ai.usage.reasoning_tokens"})
    check("the OTel alias agrees with our native token count",
          scripted.metrics["otel"]["gen_ai.usage.output_tokens"]
          == scripted.metrics["output_tokens"])
    check("a session carries a Helicone-style stage path",
          bool(scripted.metrics["stage_path"])
          and all(stage in op.STAGE_ORDER for stage, _ in scripted.metrics["stage_path"]))
    check("the Weave-style thread -> session table renders",
          "<th>startup</th>" in op.panel_j_turn_table(threads))
    check("the mutating set is the vendor's accepted-edit list plus MultiEdit",
          op.MUTATING_TOOLS == {"Edit", "Write", "MultiEdit", "NotebookEdit"})

    print("\n[the block-merge fix]")
    # One API response, three JSONL rows (thinking / text / tool_use), the
    # same usage on each. The first build dropped rows 2 and 3 and lost their
    # tool calls with them; the boundary then landed a turn late.
    merged = root / "projects" / "-w-proj-e"
    rows = []
    for kind in ("thinking", "text", "tool"):
        row = assistant(1, "s-merge", [("Edit", {"file_path": "/w/proj-e/a.py"})]
                        if kind == "tool" else [], cwd="/w/proj-e", mid="msg-merge-1")
        if kind == "thinking":
            row["message"]["content"] = [{"type": "thinking", "thinking": "x" * 40}]
        elif kind == "text":
            row["message"]["content"] = [{"type": "text", "text": "ok"}]
        rows.append(row)
    write_jsonl(merged / "m.jsonl", [human(0, "s-merge", "go", cwd="/w/proj-e")] + rows)
    got, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                    include_cwd_pattern=["proj-e"]), op.load_prices(None))
    check("three rows of one message become ONE turn", got[0].metrics["turns"] == 1,
          got[0].metrics["turns"])
    check("the usage is counted ONCE, not three times",
          got[0].metrics["total_tokens"] == 10 + 500 + 20_000 + 1_000,
          got[0].metrics["total_tokens"])
    check("the tool call on the THIRD row survives the dedupe",
          got[0].metrics["edit_calls"] == 1, got[0].metrics["tool_histogram"])
    check("so the boundary is turn 0, not a turn late",
          got[0].metrics["first_mutation_turn"] == 0, got[0].metrics["first_mutation_turn"])
    check("the thinking text on the FIRST row survives too",
          got[0].turns[0].thinking_chars == 40, got[0].turns[0].thinking_chars)


def privacy_check(directory):
    """Run the --redact privacy grep against REAL output in `directory`.

    Same rule as the synthetic test: no URL, email, GUID or secret-shaped
    string may survive redaction. This mode is for a redacted report you are
    about to send somewhere; it is NEVER run against the default output,
    which is full fidelity on purpose.
    """
    directory = Path(directory)
    required = ["orientation.json", "orientation.md", "index.html"]
    for name in required:
        if not (directory / name).exists():
            print("  FAIL missing %s" % (directory / name))
            return 1
    # EVERY generated file, not a hard-coded three. The report is a set of
    # pages, and a check that read only orientation.html would have cleared
    # index.html, ledger.html and appendix.html unexamined -- exactly the
    # files that carry the per-session table. The screen pack is included
    # because it is the artefact that gets shown and shared.
    targets = sorted(set(
        [directory / n for n in required]
        + list(directory.glob("*.html"))
        + list(directory.glob("*.md"))
        + list(directory.glob("*.json"))
        + list((directory / "screen-pack").glob("*.html"))))
    blob = "".join(t.read_text(encoding="utf-8") for t in targets)
    hits = sorted({m.group(0) for m in op.SECRETISH_RE.finditer(blob)})
    print("privacy check on %s (%s bytes over %d files: %s)"
          % (directory, len(blob), len(targets),
             ", ".join(sorted({t.parent.name + "/" + t.name if t.parent != directory else t.name
                               for t in targets}))[:300]))
    if hits:
        print("  FAIL %d secret-shaped string(s), e.g. %s" % (len(hits), hits[:5]))
        return 1
    document = json.loads((directory / "orientation.json").read_text(encoding="utf-8"))
    if document.get("sessions"):
        print("  FAIL per-session records survived --redact")
        return 1
    if not document.get("scope", {}).get("redact"):
        print("  FAIL this report was not generated with --redact")
        return 1
    print("  ok   no URL / email / GUID / secret-shaped string")
    print("  ok   aggregates only: no per-session records")
    return 0


def sqlite_checks(root):
    """The screen pack, scope filters, and Copilot CLI's files AND SQLite."""
    print("\n[the screen pack]")
    # A screen-pack page is shown on a projector, in a screenshot or read
    # aloud, so every page must fit ONE screen. That is asserted by
    # construction — the row caps — not by rendering and measuring pixels,
    # because a pixel measurement needs a browser and we have none.
    import re as _re
    prices = op.load_prices(None)
    sessions, store_stats = op.collect_sessions(ns(root=str(root), store=["claude-code"]), prices)
    threads = op.build_threads(sessions, 72.0, 2, 12)
    op.apply_thread_handoffs(threads, prices)
    for redact in (False, True):
        args = ns(root=str(root), store=["claude-code"], redact=redact)
        payload = op.build_payload(args, sessions, threads, store_stats, "test")
        pack = root.parent / ("pack-redact" if redact else "pack-full")
        titles = op.screen_pack(payload, sessions, threads, store_stats, pack)
        label = "redacted" if redact else "full-fidelity"
        check("%s pack writes numbered pages and an INDEX" % label,
              len(titles) >= 10 and (pack / "INDEX.html").exists(), len(titles))
        worst = 0
        for name, _ in titles:
            text = (pack / name).read_text(encoding="utf-8")
            rows = text.count("<tr>") - text.count('<tr><th')
            worst = max(worst, rows)
            if "<table>" in text:
                check_silent = None
        check("%s pack: no page exceeds the row cap by construction" % label,
              worst <= op.SCREEN_ROWS + 2, worst)
        first = (pack / titles[0][0]).read_text(encoding="utf-8")
        check("%s pack: pages are one screen, not a scrolling document" % label,
              "overflow:hidden" in first and "%dpx" % op.SCREEN_W in first)
        check("%s pack: body type is >= 18px monospace" % label,
              "monospace" in first and _re.search(r"font:\s*19px", first))
        check("%s pack: numbers carry thousands separators" % label,
              op._thousands(1234567) == "1,234,567", op._thousands(1234567))
        check("%s pack: arrow paging and full-screen are on every page, not just INDEX"
              % label, "ArrowRight" in first and "requestFullscreen" in first)
        check("%s pack says which fidelity it is, on page 1" % label,
              ("safe to share" if redact else "do not share") in first)
    blob = "".join((root.parent / "pack-redact" / name).read_text(encoding="utf-8")
                   for name, _ in titles)
    hits = sorted({m.group(0) for m in op.SECRETISH_RE.finditer(blob)})
    check("redacted pack carries no URL / email / GUID / secret-shaped string",
          not hits, hits[:3])
    check("an over-long table is TRUNCATED with a printed count, never silently cut",
          "+ 5 more row(s) not shown" in op._screen_table(["a"], [[i] for i in range(45)]))

    print("\n[several --exclude-cwd-pattern values]")
    # Scripted runs launched from a repo can execute out of a scratch
    # directory, so excluding the repo alone does not exclude them.
    scratch = root / "projects" / "-home-dev-scratch-runs-x"
    write_jsonl(scratch / "b.jsonl", [
        human(0, "s-scratch", "run the job", cwd="/home/dev/scratch-runs/x"),
        assistant(1, "s-scratch", [("Edit", {"file_path": "/home/dev/scratch-runs/x/a.py"})],
                  cwd="/home/dev/scratch-runs/x")])
    repo_only, _ = op.collect_sessions(
        ns(root=str(root), store=["claude-code"],
           exclude_cwd_pattern=["myrepo"]), op.load_prices(None))
    both, _ = op.collect_sessions(
        ns(root=str(root), store=["claude-code"],
           exclude_cwd_pattern=["myrepo", "scratch-runs"]), op.load_prices(None))
    check("excluding the repo alone does NOT drop a scratch-run session",
          any(s.session_id == "s-scratch" for s in repo_only))
    check("a second --exclude-cwd-pattern drops it",
          not any(s.session_id == "s-scratch" for s in both))
    check("...and keeps everything else",
          len(both) == len(repo_only) - 1, (len(both), len(repo_only)))

    print("\n[the Copilot CLI SQLite store]")
    import sqlite3
    store = root.parent / "copilot" / "session-state"
    store.mkdir(parents=True, exist_ok=True)
    database = store / "sessions.db"
    connection = sqlite3.connect(str(database))
    connection.execute("CREATE TABLE session_store (id TEXT PRIMARY KEY, blob TEXT)")
    document = {
        "sessionId": "cli-1", "cwd": "/w/proj-x", "model": "example-copilot-model",
        "createdAt": "2026-09-01T09:00:00Z",
        "messages": [
            {"role": "user", "content": "fix the pipeline",
             "timestamp": "2026-09-01T09:00:00Z"},
            {"role": "assistant", "timestamp": "2026-09-01T09:01:00Z",
             "usage": {"input_tokens": 900, "output_tokens": 120},
             "toolCalls": [{"name": "Read", "arguments": {"path": "/w/proj-x/main.py"}}]},
            {"role": "assistant", "timestamp": "2026-09-01T09:02:00Z",
             "usage": {"input_tokens": 900, "output_tokens": 300},
             "toolCalls": [{"name": "Write", "arguments": {"path": "/w/proj-x/main.py"}}]}]}
    connection.execute("INSERT INTO session_store VALUES (?,?)",
                       ("cli-1", json.dumps(document)))
    connection.commit()
    connection.close()
    before = sorted(p.name for p in store.iterdir())

    sessions, stats = op.ingest_copilot_cli([root.parent / "copilot"], None, None,
                                            op.load_prices(None))
    check("the SQLite session store is ingested, not only the JSON files",
          stats["sqlite_sessions"] == 1, stats)
    # This root also holds the JSON-file fixture the cross-tool checks use, so
    # pick the SQLite one by its path rather than by position.
    from_sqlite = [s for s in sessions if s.path.endswith("sessions.db")]
    check("exactly one session came out of the SQLite store",
          len(from_sqlite) == 1, [s.path for s in sessions])
    check("both halves of the store are read, not one or the other",
          len(sessions) == 2, [s.path for s in sessions])
    check("a session read out of SQLite gets a real boundary",
          from_sqlite and from_sqlite[0].metrics["first_mutation_turn"] == 1,
          from_sqlite[0].metrics if from_sqlite else None)
    check("and its tool calls survive the SQLite round trip",
          from_sqlite and from_sqlite[0].metrics["tool_histogram"] == {"Read": 1, "Write": 1},
          from_sqlite[0].metrics["tool_histogram"] if from_sqlite else None)
    check("opening the store took no lock and wrote no -wal file",
          sorted(p.name for p in store.iterdir()) == before,
          sorted(p.name for p in store.iterdir()))
    schema = op.probe_store(root.parent / "copilot")["sqlite"]
    check("probe-store prints SQLite table and column NAMES",
          any("session_store" in report.get("tables", {}) for report in schema.values()), schema)
    check("probe-store reads no VALUE out of the store",
          all("no stored value was read" in report.get("privacy", "")
              for report in schema.values()))
    blob = json.dumps(schema)
    check("no stored value leaked into the probe output",
          "fix the pipeline" not in blob and "example-copilot-model" not in blob)




# ==========================================================================
# Regression checks — every check here FAILS IF ITS FIX IS REVERTED.
# ==========================================================================
def accounting_checks(root, tmp):
    prices = op.load_prices(None)

    # ---- the shell mutation detector -----------------------------
    print("\n[observation is not mutation]")
    # A review reproduced all four of these as MUTATIONS on the old detector.
    for command in ('python3 -c "print(1)"',
                    "cat <<EOF\nhello\nEOF",
                    'printf "%s" "please touch nothing"',
                    'git log --grep="rm"'):
        check("%r is not a mutation" % command.replace("\n", "\\n"),
              op.bash_mutation_reason(command) is None,
              op.bash_mutation_reason(command))
    for command, expected in (("rm -rf build", "rm"),
                              ("sed -i s/a/b/ x", "sed -i"),
                              ("cat x > y", "redirect"),
                              ("tee out.txt", "tee"),
                              ('git commit -m "rm stuff"', "git commit"),
                              ("for f in *; do rm $f; done", "rm"),
                              ("sudo rm -rf /x", "rm"),
                              ("echo $(rm x)", "rm")):
        check("%r is still %s" % (command, expected),
              op.bash_mutation_reason(command) == expected,
              op.bash_mutation_reason(command))
    for command in ("ls > /dev/null", "make test 2>&1 | tail",
                    "sed -n 1,5p f", "docker ps", "grep -rn 'mv ' src/"):
        check("%r is still a read" % command,
              op.bash_mutation_reason(command) is None,
              op.bash_mutation_reason(command))
    check("heredoc BODIES are dropped before matching",
          op.strip_heredoc_bodies("cat <<EOF\nrm -rf /\nEOF\nls").splitlines()
          == ["cat <<EOF", "ls"],
          op.strip_heredoc_bodies("cat <<EOF\nrm -rf /\nEOF\nls"))

    # ---- attempt vs success --------------------------------------
    print("\n[an attempt is not a mutation]")
    denied = root / "projects" / "-w-accta"
    write_jsonl(denied / "s.jsonl", [
        human(0, "s-denied", "add the tag", cwd="/w/accta"),
        assistant(1, "s-denied", [("Read", {"file_path": "/w/accta/main.py"})], cwd="/w/accta"),
        assistant(2, "s-denied", [("Write", {"file_path": "/w/accta/main.py"})], cwd="/w/accta"),
        {"type": "user", "sessionId": "s-denied", "cwd": "/w/accta",
         "timestamp": (T0 + dt.timedelta(minutes=2, seconds=30)).isoformat().replace("+00:00", "Z"),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t20", "is_error": True,
              "content": "Error: " + op.DENIAL_MARK}]}},
        assistant(3, "s-denied", [("Read", {"file_path": "/w/accta/b.py"})], cwd="/w/accta"),
        assistant(4, "s-denied", [("Edit", {"file_path": "/w/accta/main.py"})], cwd="/w/accta"),
    ])
    rows, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                     include_cwd_pattern=["accta"]), prices)
    session = {s.session_id: s for s in rows}["s-denied"]
    check("a REFUSED write does not end orientation",
          session.metrics["first_mutation_turn"] == 3,
          session.metrics["first_mutation_turn"])
    check("the refused write is counted as an attempt, in its own column",
          session.metrics["first_mutation_attempt_turn"] == 1
          and session.metrics["failed_mutation_attempts"] == 1,
          (session.metrics["first_mutation_attempt_turn"],
           session.metrics["failed_mutation_attempts"]))
    check("the boundary response's own size is published",
          session.metrics["boundary_turn_tokens"] == session.turns[3].tokens,
          session.metrics["boundary_turn_tokens"])

    # ---- child agents --------------------------------------------
    print("\n[a child agent's edit is not the parent's boundary]")
    parent = root / "projects" / "-w-acctb"
    write_jsonl(parent / "p.jsonl", [
        human(0, "s-child", "investigate and fix", cwd="/w/acctb"),
        assistant(1, "s-child", [("Read", {"file_path": "/w/acctb/a.py"})], cwd="/w/acctb"),
        assistant(3, "s-child", [("Read", {"file_path": "/w/acctb/b.py"})], cwd="/w/acctb"),
        assistant(5, "s-child", [("Edit", {"file_path": "/w/acctb/a.py"})], cwd="/w/acctb"),
    ])
    kid = dict(assistant(2, "s-child", [("Write", {"file_path": "/w/acctb/scratch.txt"})],
                         cwd="/w/acctb", mid="msg-child-2"))
    kid["agentId"] = "a-kid"
    kid["isSidechain"] = True
    write_jsonl(parent / "s-child" / "subagents" / "agent-a-kid.jsonl", [kid])
    rows, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                     include_cwd_pattern=["acctb"]), prices)
    session = {s.session_id: s for s in rows}["s-child"]
    check("the child's records are ingested into the workflow total",
          session.metrics["turns"] == 4, session.metrics["turns"])
    check("but the boundary is the MAIN conversation's own first edit",
          session.metrics["first_mutation_turn"] == 2,
          session.metrics["first_mutation_turn"])
    check("child spend is rolled up in its own columns",
          session.metrics["child_agents"] == 1 and session.metrics["child_turns"] == 1
          and session.metrics["child_tokens"] == session.turns[1].tokens,
          {k: session.metrics[k] for k in ("child_agents", "child_turns", "child_tokens")})
    check("orientation counts only main responses before the boundary",
          session.metrics["orientation_tokens"]
          == sum(t.tokens for t in session.turns if t.agent is None) - session.turns[3].tokens,
          session.metrics["orientation_tokens"])

    # ---- "handoff opened" ----------------------------------------
    print("\n[opened, not merely named]")
    check("PLAN does not match a longer word in a note-shaped name (planner.md)",
          not op.note_basename_kind("planner.md", op.HANDOFF_WORDS))
    check("PLAN no longer matches planet.py",
          not op.note_basename_kind("planet.py", op.HANDOFF_WORDS))
    check("STATUS no longer matches post-status.js",
          not op.note_basename_kind("post-status.js", op.HANDOFF_WORDS))
    check("a real handoff still matches",
          op.note_basename_kind("HANDOFF_2026-07-05.md", op.HANDOFF_WORDS)
          and op.note_basename_kind("STATUS.md", op.HANDOFF_WORDS)
          and op.note_basename_kind("CLAUDE.md", op.HANDOFF_WORDS))
    searched = root / "projects" / "-w-acctc"
    write_jsonl(searched / "s.jsonl", [
        human(0, "s-glob", "carry on", cwd="/w/acctc"),
        assistant(1, "s-glob", [("Glob", {"pattern": "**/HANDOFF.md"})], cwd="/w/acctc"),
        assistant(2, "s-glob", [("Edit", {"file_path": "/w/acctc/a.py"})], cwd="/w/acctc"),
    ])
    rows, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                     include_cwd_pattern=["acctc"]), prices)
    session = {s.session_id: s for s in rows}["s-glob"]
    check("a Glob for a handoff is a SEARCH, not an open",
          session.metrics["handoff_opened_by_turn_2"] is False
          and session.metrics["handoff_searched_by_turn_2"] is True,
          (session.metrics["handoff_opened_by_turn_2"],
           session.metrics["handoff_searched_by_turn_2"]))
    failed = root / "projects" / "-w-acctd"
    write_jsonl(failed / "s.jsonl", [
        human(0, "s-badread", "carry on", cwd="/w/acctd"),
        assistant(1, "s-badread", [("Read", {"file_path": "/w/acctd/HANDOFF.md"})], cwd="/w/acctd"),
        {"type": "user", "sessionId": "s-badread", "cwd": "/w/acctd",
         "timestamp": (T0 + dt.timedelta(minutes=1, seconds=30)).isoformat().replace("+00:00", "Z"),
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t10", "is_error": True,
              "content": "Error: file does not exist"}]}},
        assistant(2, "s-badread", [("Edit", {"file_path": "/w/acctd/a.py"})], cwd="/w/acctd"),
    ])
    rows, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                     include_cwd_pattern=["acctd"]), prices)
    session = {s.session_id: s for s in rows}["s-badread"]
    check("a read that came back an ERROR did not open anything",
          session.metrics["handoff_opened_by_turn_2"] is False,
          session.metrics["handoff_file"])

    # ---- orientation dollars -------------------------------------
    print("\n[orientation dollars are priced, not allocated]")
    priced = root / "projects" / "-w-accte"
    # response 1 is all CHEAP cache reads; response 2 is all EXPENSIVE output.
    # Allocation by token share and direct pricing must therefore disagree.
    write_jsonl(priced / "s.jsonl", [
        human(0, "s-priced", "go", cwd="/w/accte"),
        assistant(1, "s-priced", [("Read", {"file_path": "/w/accte/a.py"})],
                  read=100_000, write=0, out=0, cwd="/w/accte"),
        assistant(2, "s-priced", [("Edit", {"file_path": "/w/accte/a.py"})],
                  read=0, write=0, out=100_000, cwd="/w/accte"),
    ])
    rows, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                     include_cwd_pattern=["accte"]), prices)
    session = {s.session_id: s for s in rows}["s-priced"]
    direct = session.metrics["orientation_cost_usd"]
    allocated = (session.metrics["est_cost_usd"] or 0) * (session.metrics["orientation_share"] or 0)
    check("the pre-boundary responses are priced from their own buckets",
          direct is not None and abs(direct - op.turn_cost_usd(session.turns[0], prices)) < 1e-9,
          (direct, op.turn_cost_usd(session.turns[0], prices)))
    check("and that is NOT what the old share-allocation gives",
          allocated > direct * 2, (allocated, direct))
    check("the panel's dollar figure is the direct one",
          abs(op._orientation_dollars(session) - direct) < 1e-12,
          (op._orientation_dollars(session), direct))

    # ---- no-boundary sessions ------------------------------------
    print("\n[an unknown boundary is not 100 % tax and $0]")
    twin = root / "projects" / "-w-acctf"
    write_jsonl(twin / "a.jsonl", [
        human(0, "s-nb1", "start the job", cwd="/w/acctf"),
        assistant(1, "s-nb1", [("Read", {"file_path": "/w/acctf/main.py"})], cwd="/w/acctf"),
        assistant(2, "s-nb1", [("Edit", {"file_path": "/w/acctf/main.py"})], cwd="/w/acctf"),
        assistant(3, "s-nb1", [("Read", {"file_path": "/w/acctf/vars.py"})], cwd="/w/acctf"),
    ])
    write_jsonl(twin / "b.jsonl", [
        human(60, "s-nb2", "keep looking", cwd="/w/acctf"),
        assistant(61, "s-nb2", [("Read", {"file_path": "/w/acctf/main.py"})], cwd="/w/acctf"),
        assistant(62, "s-nb2", [("Read", {"file_path": "/w/acctf/vars.py"})], cwd="/w/acctf"),
    ])
    rows, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                     include_cwd_pattern=["acctf"]), prices)
    index = {s.session_id: s for s in rows}
    check("a session with no boundary has UNKNOWN orientation tokens, not all of them",
          index["s-nb2"].metrics["orientation_tokens"] is None,
          index["s-nb2"].metrics["orientation_tokens"])
    check("and undefined breadth",
          index["s-nb2"].metrics["orientation_breadth"] is None,
          index["s-nb2"].metrics["orientation_breadth"])
    threads = op.build_threads(rows, 72.0, 2, 12)
    record = op.thread_record(threads[0], redact=False)
    check("the thread tax EXCLUDES it",
          record["reorientation_tax_tokens"] == 0, record["reorientation_tax_tokens"])
    check("and reports it as its own category beside the tax",
          record["successors_without_boundary"]["sessions"] == 1
          and record["successors_without_boundary"]["tokens"]
          == index["s-nb2"].metrics["total_tokens"],
          record["successors_without_boundary"])

    # ---- one canonical reader -----------------------------------
    print("\n[one reader, and replaying a row changes nothing]")
    import audit_common as ac
    row = assistant(1, "s-idem", [("Read", {"file_path": "/w/x/a.py"})], thinking=100)
    reader = ac.EventReader()
    first, is_new = reader.add(row)
    again, is_new2 = reader.add(json.loads(json.dumps(row)))
    check("an identical replayed row is not a second response",
          is_new is True and is_new2 is False and len(reader.responses) == 1,
          (is_new, is_new2, len(reader.responses)))
    check("and its content blocks are merged idempotently",
          len(first.content()) == len(row["message"]["content"]),
          (len(first.content()), len(row["message"]["content"])))
    idem = root / "projects" / "-w-acctg"
    base = [human(0, "s-idem", "go", cwd="/w/acctg"),
            assistant(1, "s-idem", [("Read", {"file_path": "/w/acctg/a.py"}),
                                    ("Read", {"file_path": "/w/acctg/b.py"})],
                      thinking=100, cwd="/w/acctg"),
            assistant(2, "s-idem", [("Edit", {"file_path": "/w/acctg/a.py"})], cwd="/w/acctg")]
    write_jsonl(idem / "one.jsonl", base)
    clean, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                      include_cwd_pattern=["acctg"]), prices)
    write_jsonl(idem / "two.jsonl", base)      # the SAME rows, replayed
    replayed, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                         include_cwd_pattern=["acctg"]), prices)
    a = {s.session_id: s for s in clean}["s-idem"].metrics
    b = {s.session_id: s for s in replayed}["s-idem"].metrics
    for field in ("total_tokens", "turns", "tool_histogram", "first_mutation_turn",
                  "orientation_tokens", "thinking_tokens", "strategy"):
        check("replaying an identical row leaves %s unchanged" % field,
              a[field] == b[field], (a[field], b[field]))
    check("and thinking share never exceeds 1 because of a replay",
          (b["thinking_share"] or 0) <= 1.0, b["thinking_share"])

    # ---- thread calibration --------------------------------------
    print("\n[labels bind to sessions, not to Txxx ids]")
    cal = root / "projects" / "-w-accth"
    for n, (start, files) in enumerate([(0, ["p.py", "q.py"]), (2, ["p.py", "q.py"]),
                                        (400, ["r.py", "s.py"]), (402, ["r.py", "s.py"])]):
        write_jsonl(cal / ("c%d.jsonl" % n), [
            human(start, "s-cal%d" % n, "go on", cwd="/w/accth"),
            assistant(start + 1, "s-cal%d" % n,
                      [("Read", {"file_path": "/w/accth/" + f}) for f in files], cwd="/w/accth"),
            assistant(start + 2, "s-cal%d" % n,
                      [("Edit", {"file_path": "/w/accth/" + files[0]})], cwd="/w/accth"),
        ])
    rows, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                     include_cwd_pattern=["accth"]), prices)
    threads = op.build_threads(rows, 72.0, 2, 12)
    labels = {t.id: "yes" for t in threads if len(t.sessions) > 1}
    report = op.calibrate(threads, labels, rows, 72.0, 2, 12)
    check("with labels, the binding is named on the output",
          "session ids" in (report.get("label_binding") or "").lower(), report.get("label_binding"))
    empty = op.calibrate(threads, {}, rows, 72.0, 2, 12)
    check("with NO labels the sweep says uncalibrated and prints no precision",
          empty["status"] == "uncalibrated" and empty["pair_precision"] is None
          and empty["suggested"] is None,
          (empty["status"], empty["pair_precision"], empty["suggested"]))
    check("a swept partition scores a label only when it keeps those exact "
          "sessions together",
          all(("labels_matched" in row) for row in report["sweep"]),
          report["sweep"][:1])
    check("recall compares session-id SETS, so a declared continuation can hit",
          op.calibrate(threads, {t.id: "same_as:%s" % t.id for t in threads[:1]},
                       rows, 72.0, 2, 12)["recall"] == 1.0,
          op.calibrate(threads, {t.id: "same_as:%s" % t.id for t in threads[:1]},
                       rows, 72.0, 2, 12)["recall"])

def accounting_checks_more(root, tmp):
    """The gap annotation, the field estimand and the minor economics. Same
    rule: every check fails if its fix reverts."""
    prices = op.load_prices(None)

    # ---- the gap annotation ----------------------------------------
    print("\n[a two-day gap is not >72 h]")
    gaps = root / "projects" / "-w-accti"
    # two sittings whose LAST STARTS are ~2 days apart, but whose end-to-start
    # wall clock is longer. The old label called this ">72 h, so the rule
    # split them"; the rule tests start-to-start, and 2 days is under 72 h.
    for n, minute in ((0, 0), (1, 2 * 24 * 60)):
        write_jsonl(gaps / ("g%d.jsonl" % n), [
            human(minute, "s-gap%d" % n, "go on", cwd="/w/accti"),
            assistant(minute + 1, "s-gap%d" % n,
                      [("Read", {"file_path": "/w/accti/a.py"}),
                       ("Read", {"file_path": "/w/accti/b.py"})], cwd="/w/accti"),
            assistant(minute + 2, "s-gap%d" % n,
                      [("Edit", {"file_path": "/w/accti/a.py"})], cwd="/w/accti"),
        ])
    rows, _ = op.collect_sessions(ns(root=str(root), store=["claude-code"],
                                     include_cwd_pattern=["accti"]), prices)
    threads = op.build_threads(rows, 24.0, 2, 12)     # 24 h gap -> two episodes
    streams = op.build_streams(threads)[0]
    stream = [s for s in streams if len(s.threads) > 1]
    check("a stream with two episodes was built", bool(stream), len(streams))
    if stream:
        svg = op.panel_stream_timeline(stream, False, gap_hours=72.0)
        check("a 2-day gap is NOT labelled as having passed the 72-hour rule",
              "more than the 72 hours" not in svg, svg[svg.find("gap") - 40:][:200] if "gap" in svg else "")
        check("it says which test actually separated the episodes",
              "less than 72 hours; the shared-file test" in svg)
        days = stream[0].gaps_days()
        wall = stream[0].wall_gaps_days()
        check("the displayed gap uses the SAME reference as the rule (start to start)",
              days and abs(days[0] - 2.0) < 0.01, (days, wall))

    # ---- one ranking metric ---------------------------------------
    check("the summary names the metric the stream table ranks by",
          op.STREAM_RANK_LABEL in op.five_line_summary(
              {"sessions": 2, "tokens_total": 10, "est_cost_usd_total": 1.0,
               "median_orientation_share": 0.3, "sessions_no_mutation": 0,
               "orientation_cost_usd_total": 0.5, "orientation_cost_share": 0.5,
               "multi_episode_streams": 1, "reorientation_tax_tokens": 10,
               "sessions_with_boundary": 2},
              [{"thread_id": "T001", "reorientation_tax_tokens": 5}], {},
              [{"stream_id": "S001", "name": "x", "sessions": 2,
                "reorientation_tax_tokens": 9, "reorientation_tax_usd": 0.1}]))

    # ---- the field estimand -----------------------------------
    print("\n[reallocation is not saving]")
    # A review's probe, verbatim in shape: identical $1.00 total cost in both
    # arms; the NOTE arm merely has 490 fewer tokens BEFORE the boundary.
    # The old formula reported +$0.49 of net value for moving them.
    def row(arm, orient, total_cost):
        return {"thread_id": "T1", "cwd": "/w", "arm": arm,
                "orientation_tokens": orient, "total_tokens": 1000,
                "est_cost_usd": total_cost, "note_minutes": 0.0,
                "handoff_opened_by_turn_2": arm == "NOTE"}
    joined = {"rows": [row("NOTE", 10, 1.00), row("NONE", 500, 1.00)],
              "pending": [], "unmatched": []}
    report = op.handoff_report(joined, [], [], 100.0)
    pair = report["pairs"][0]
    check("moving 490 tokens past the boundary at identical total cost nets $0",
          abs(pair["net_usd"]) < 1e-9, pair["net_usd"])
    check("and the token movement is still reported, as the MECHANISM",
          pair["orientation_tokens_saved"] == 490, pair["orientation_tokens_saved"])
    joined2 = {"rows": [row("NOTE", 10, 0.60), row("NONE", 500, 1.00)],
               "pending": [], "unmatched": []}
    pair2 = op.handoff_report(joined2, [], [], 100.0)["pairs"][0]
    check("a real reduction in TOTAL successor cost still nets positive",
          abs(pair2["net_usd"] - 0.40) < 1e-9, pair2["net_usd"])
    check("the stopping rule can reach its own floor and states the arithmetic",
          str(op.MIN_SUCCESSORS_FOR_FLOOR) in op.STOPPING_RULE
          and op.MIN_SUCCESSORS_FOR_FLOOR >= 2 * op.MIN_USABLE_PAIRS,
          op.STOPPING_RULE)
    check("the log template carries predecessor and successor ids",
          "predecessor_session_id" in op.HANDOFF_COLUMNS
          and "successor_session_id" in op.HANDOFF_COLUMNS, op.HANDOFF_COLUMNS)

    # ---- the minor economics ----------------------------------
    print("\n[the smaller accounting corrections]")
    import compact_economics as ce, lane_profile as lp
    ce._PRICES = prices
    t0 = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    lane = ("s1", "main")
    pre = [(t0.replace(minute=m), 400_000, 1_000, "claude-opus-5", True) for m in range(0, 30)]
    post = [(t0.replace(hour=14), 400_000, 50_000, "claude-opus-5", True)]
    result = ce.analyze({lane: pre + post},
                        [(lane, t0.replace(hour=13, minute=45), 5_000)])[0]
    check("the post-compaction prefix is cache-READ plus cache-WRITE",
          result["post_prefix"] == 400_000
          and result["post_read"] == 350_000 and result["post_write"] == 50_000,
          result["post_prefix"])
    lp_prices = prices
    mixed = {("s", "main"): [
        {"ts": t0, "read": 1_000_000, "w5": 0, "w1": 0, "uin": 0, "out": 0,
         "model": "claude-opus-5", "proj": "p"},
        {"ts": t0 + dt.timedelta(minutes=1), "read": 0, "w5": 0, "w1": 0, "uin": 0,
         "out": 10, "model": "claude-haiku-4-5-20251001", "proj": "p"}]}
    out = lp.analyze(mixed, lp_prices, 200_000)
    lane_rows = out[0] if isinstance(out, tuple) else out
    row0 = lane_rows[0] if isinstance(lane_rows, list) else None
    opus_rate = op.price_for("claude-opus-5", prices)[0]
    check("a lane is priced per response, not all at its LAST model",
          row0 is not None and abs(row0["est_cost_usd"]
                                   - (1_000_000 * 0.1 * opus_rate / 1e6
                                      + 10 * op.price_for("claude-haiku-4-5-20251001",
                                                          prices)[1] / 1e6)) < 1e-6,
          row0 and row0["est_cost_usd"])
    check("and it says the lane mixed models",
          row0 is not None and row0["mixed_models"] is True, row0 and row0.get("models"))
    meter = op.meter_report([], bucket_hours=5.0)
    check("the meter says its buckets are clock-aligned, not provider windows",
          "not the provider" in meter["bucket_scheme"].lower(), meter["bucket_scheme"])


# ==========================================================================
# Carry or restart. Every check here fails if its behaviour is reverted. The
# measurement it rests on (how a resume appears in this store) is recorded in
# the CARRY OR RESTART comment block in orientation_profile.py.
# ==========================================================================
def pickup_row(minute, session, tools=(), model="claude-opus-5", read=0, w5=0, w1=0,
       inp=0, out=0, cwd="/w/pickup", mid=None, effort="high"):
    """An assistant response with EXACT usage buckets, so the carry-cost
    arithmetic can be asserted against a number computed by hand."""
    return {
        "type": "assistant", "sessionId": session, "cwd": cwd, "version": "9.9.9",
        "effort": effort,
        "timestamp": (T0 + dt.timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
        "message": {
            "id": mid or "m-%s-%d" % (session, minute),
            "model": model,
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_read_input_tokens": read,
                      "cache_creation": {"ephemeral_5m_input_tokens": w5,
                                         "ephemeral_1h_input_tokens": w1}},
            "content": [{"type": "tool_use", "id": "u%d-%d" % (minute, i), "name": n, "input": a}
                        for i, (n, a) in enumerate(tools)],
        },
    }


def pickup_compact(minute, session, cwd="/w/pickup", pre=110_000, post=12_000):
    """The `compact_boundary` system row Claude Code writes on a compaction.

    Shape verified against real transcripts: `compactMetadata` carries
    preTokens / postTokens / trigger.
    """
    return {"type": "system", "subtype": "compact_boundary", "sessionId": session,
            "cwd": cwd, "version": "9.9.9", "content": "Conversation compacted",
            "timestamp": (T0 + dt.timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
            "compactMetadata": {"trigger": "manual", "preTokens": pre, "postTokens": post,
                                "cumulativeDroppedTokens": pre - post, "durationMs": 81_000}}


def pickup_checks(root, tmp):
    prices = op.load_prices(None)
    rate_in, rate_out = op.price_for("claude-opus-5", prices)
    READ = [("Read", {"file_path": "/w/pickup/a.py"}),
            ("Read", {"file_path": "/w/pickup/b.py"})]
    EDIT = [("Edit", {"file_path": "/w/pickup/a.py"})]

    # ---- the kind classifier, on a THREE-SESSION stream -------------------
    print("\n[carry or restart: R0 / R1 / R2 on a three-session stream]")
    project = root / "projects" / "-w-pickup"
    day = 24 * 60
    for index, (sid, start) in enumerate((("s-pu-1", 0), ("s-pu-2", 5 * day),
                                          ("s-pu-3", 10 * day))):
        rows = [human(start, sid, "carry on with the tf work", cwd="/w/pickup"),
                pickup_row(start + 1, sid, READ, read=100_000, out=100),
                pickup_row(start + 2, sid, EDIT, read=100_000, out=100)]
        if sid == "s-pu-3":
            # ... and this one is ALSO picked back up inside itself, 4 h later.
            rows += [pickup_row(start + 2 + 240, sid, READ, w1=300_000, out=100),
                     pickup_row(start + 3 + 240, sid, EDIT, read=300_000, out=100)]
        write_jsonl(project / ("%s.jsonl" % sid), rows)
    # a session that is NOT a stream successor but resumes inside itself
    solo = root / "projects" / "-w-pickupsolo"
    write_jsonl(solo / "solo.jsonl", [
        human(0, "s-pu-solo", "one sitting", cwd="/w/pickupsolo"),
        pickup_row(1, "s-pu-solo", READ, cwd="/w/pickupsolo", read=50_000, out=100),
        pickup_row(1 + 180, "s-pu-solo", READ, cwd="/w/pickupsolo", w1=400_000, out=100),
        pickup_row(2 + 180, "s-pu-solo", EDIT, cwd="/w/pickupsolo", read=400_000, out=100),
    ])
    # a session continued past a COMPACTION, with no clock gap at all
    comp = root / "projects" / "-w-pickupcomp"
    write_jsonl(comp / "comp.jsonl", [
        human(0, "s-pu-comp", "long sitting", cwd="/w/pickupcomp"),
        pickup_row(1, "s-pu-comp", READ, cwd="/w/pickupcomp", read=50_000, out=100),
        pickup_compact(2, "s-pu-comp", cwd="/w/pickupcomp"),
        pickup_row(3, "s-pu-comp", READ, cwd="/w/pickupcomp", w5=12_000, out=100),
        pickup_row(4, "s-pu-comp", EDIT, cwd="/w/pickupcomp", read=12_000, out=100),
    ])
    sessions, _ = op.collect_sessions(
        ns(root=str(root), store=["claude-code"], include_cwd_pattern=["pickup"]), prices)
    index = {s.session_id: s for s in sessions}
    threads = op.build_threads(sessions, 72.0, 2, 12)
    streams = op.build_streams(threads)[0]
    result = op.classify_continuations(sessions, streams, prices)
    kinds = {sid: row["continuation_kind"] for sid, row in result["per_session"].items()}
    check("the stream's first session continues nothing",
          kinds.get("s-pu-1") == "first", kinds.get("s-pu-1"))
    check("a later session of the same stream is R2",
          kinds.get("s-pu-2") == "R2", kinds.get("s-pu-2"))
    check("a session that is BOTH a successor and a resume is R2 (stated precedence)",
          kinds.get("s-pu-3") == "R2", kinds.get("s-pu-3"))
    check("... and its in-session resume is still counted as its own R0 event",
          any(e["kind"] == "R0" and e["session_id"] == "s-pu-3" for e in result["events"]))
    check("a lone session resumed after a gap is R0",
          kinds.get("s-pu-solo") == "R0", kinds.get("s-pu-solo"))
    check("a session continued past a compaction is R1, with NO clock gap",
          kinds.get("s-pu-comp") == "R1", kinds.get("s-pu-comp"))
    check("the compaction was read from the compact_boundary row, not guessed",
          [c["trigger"] for c in index["s-pu-comp"].compactions] == ["manual"],
          index["s-pu-comp"].compactions)
    check("R2's cost is the successor's DIRECTLY PRICED orientation",
          abs((next(e for e in result["events"]
                    if e["kind"] == "R2" and e["session_id"] == "s-pu-2")["carry_cost_usd"])
              - index["s-pu-2"].metrics["orientation_cost_usd"]) < 1e-9)
    check("a stream with both kinds is reported as paired",
          result["paired_streams"] and len(result["paired_streams"]) == 1,
          result["paired_streams"])

    # ---- the carry-cost arithmetic, on known buckets ----------------------
    print("\n[carry or restart: the carry cost is summed from the buckets, not modelled]")
    solo_events = op.carry_events(index["s-pu-solo"], prices)
    check("exactly one carry event in the resumed session", len(solo_events) == 1,
          len(solo_events))
    event = solo_events[0]
    # window = the ONE response after the gap; the response that performs the
    # first change after it is excluded, exactly as the orientation boundary is.
    expected = (400_000 * op.CACHE_WRITE_1H_MULT * rate_in + 100 * rate_out) / 1e6
    check("carry cost = that response's own buckets at its own model's rates",
          abs(event["carry_cost_usd"] - expected) < 1e-9,
          (event["carry_cost_usd"], expected))
    check("carry tokens = the same responses' tokens",
          event["carry_tokens"] == 400_000 + 100, event["carry_tokens"])
    check("and the excluded first-change response's size is published",
          event["boundary_turn_tokens"] == 400_000 + 100, event["boundary_turn_tokens"])
    check("the gap that produced it is recorded in hours",
          abs(event["gap_hours"] - 3.0) < 0.01, event["gap_hours"])
    comp_events = op.carry_events(index["s-pu-comp"], prices)
    check("the compaction produces one R1 event",
          [e["kind"] for e in comp_events] == ["R1"], [e["kind"] for e in comp_events])
    check("the summarising call is MODELLED and kept OUT of the observed cost",
          comp_events[0]["modeled_summary_usd"] is not None
          and abs(comp_events[0]["carry_cost_usd"]
                  - (12_000 * op.CACHE_WRITE_5M_MULT * rate_in + 100 * rate_out) / 1e6) < 1e-9,
          comp_events[0])
    check("... and it is modelled from the row's own preTokens/postTokens",
          abs(comp_events[0]["modeled_summary_usd"]
              - (110_000 * op.CACHE_READ_MULT * rate_in + 12_000 * rate_out) / 1e6) < 1e-9,
          comp_events[0]["modeled_summary_usd"])

    # ---- under an hour is a cache READ, over an hour is a WRITE -----------
    print("\n[carry or restart: the one-hour cache TTL, priced from the record]")
    ttl = root / "projects" / "-w-pickupttl"
    write_jsonl(ttl / "warm.jsonl", [
        human(0, "s-pu-warm", "pause and carry on", cwd="/w/pickupttl"),
        pickup_row(1, "s-pu-warm", READ, cwd="/w/pickupttl", read=10_000, out=100),
        # 30 minutes later: still inside the 1 h cache, so the prefix is READ back
        pickup_row(31, "s-pu-warm", READ, cwd="/w/pickupttl", read=500_000, out=100),
        pickup_row(32, "s-pu-warm", EDIT, cwd="/w/pickupttl", read=500_000, out=100),
    ])
    write_jsonl(ttl / "cold.jsonl", [
        human(0, "s-pu-cold", "pause and carry on", cwd="/w/pickupttl"),
        pickup_row(1, "s-pu-cold", READ, cwd="/w/pickupttl", read=10_000, out=100),
        # 3 hours later: past the 1 h cache, so the whole prefix is WRITTEN again
        pickup_row(181, "s-pu-cold", READ, cwd="/w/pickupttl", w1=500_000, out=100),
        pickup_row(182, "s-pu-cold", EDIT, cwd="/w/pickupttl", read=500_000, out=100),
    ])
    ttl_sessions, _ = op.collect_sessions(
        ns(root=str(root), store=["claude-code"], include_cwd_pattern=["pickupttl"]), prices)
    ttl_index = {s.session_id: s for s in ttl_sessions}
    warm = op.carry_events(ttl_index["s-pu-warm"], prices, gap_hours=0.25)
    cold = op.carry_events(ttl_index["s-pu-cold"], prices, gap_hours=0.25)
    check("a resume UNDER one hour is priced as a cache READ",
          len(warm) == 1 and warm[0]["prefix_mode"] == "cache-read"
          and abs(warm[0]["carry_cost_usd"]
                  - (500_000 * op.CACHE_READ_MULT * rate_in + 100 * rate_out) / 1e6) < 1e-9,
          warm and (warm[0]["prefix_mode"], warm[0]["carry_cost_usd"]))
    check("a resume OVER one hour is priced as a cache WRITE",
          len(cold) == 1 and cold[0]["prefix_mode"] == "cache-write"
          and abs(cold[0]["carry_cost_usd"]
                  - (500_000 * op.CACHE_WRITE_1H_MULT * rate_in + 100 * rate_out) / 1e6) < 1e-9,
          cold and (cold[0]["prefix_mode"], cold[0]["carry_cost_usd"]))
    check("the same prefix therefore costs 20x more after the hour than before it",
          abs((cold[0]["carry_cost_usd"] - 100 * rate_out / 1e6)
              / (warm[0]["carry_cost_usd"] - 100 * rate_out / 1e6) - 20.0) < 1e-6,
          (cold[0]["carry_cost_usd"], warm[0]["carry_cost_usd"]))
    check("at the DEFAULT one-hour threshold the sub-hour pause is not a continuation",
          op.carry_events(ttl_index["s-pu-warm"], prices) == []
          and len(op.carry_events(ttl_index["s-pu-cold"], prices)) == 1)

    # ---- a session that ran BESIDE an earlier one is not a pick-up ---------
    print("\n[carry or restart: a parallel session is not a new-session pick-up]")
    par = root / "projects" / "-w-parallel-pu"
    for sid, start, end in (("s-par-1", 0, 300), ("s-par-2", 100, 140),
                            ("s-par-3", 3 * 24 * 60, 3 * 24 * 60 + 2)):
        write_jsonl(par / ("%s.jsonl" % sid), [
            human(start, sid, "the parallel work", cwd="/w/parallel-pu"),
            pickup_row(start + 1, sid, [("Read", {"file_path": "/w/parallel-pu/a.py"}),
                                ("Read", {"file_path": "/w/parallel-pu/b.py"})],
               cwd="/w/parallel-pu", read=10_000, out=100),
            pickup_row(end, sid, [("Edit", {"file_path": "/w/parallel-pu/a.py"})],
               cwd="/w/parallel-pu", read=10_000, out=100)])
    par_sessions, _ = op.collect_sessions(
        ns(root=str(root), store=["claude-code"], include_cwd_pattern=["parallel-pu"]), prices)
    par_streams = op.build_streams(op.build_threads(par_sessions, 72.0, 2, 12))[0]
    par_result = op.classify_continuations(par_sessions, par_streams, prices)
    par_kinds = {sid: row["continuation_kind"]
                 for sid, row in par_result["per_session"].items()}
    check("the three sessions are one work item", len(par_streams) == 1, len(par_streams))
    check("a session that started before the earlier one ended is `parallel`",
          par_kinds.get("s-par-2") == "parallel", par_kinds)
    check("... it is NOT a new-session pick-up event, and it is counted apart",
          not any(e["session_id"] == "s-par-2" for e in par_result["events"])
          and par_result["parallel_sessions"] == 1,
          [(e["kind"], e["session_id"]) for e in par_result["events"]])
    check("no new-session pick-up has a negative gap",
          all((e.get("gap_hours") or 0) >= 0 for e in par_result["events"]),
          [e.get("gap_hours") for e in par_result["events"]])
    third = next(e for e in par_result["events"] if e["session_id"] == "s-par-3")
    check("the next real restart is timed from the LATEST earlier end",
          abs(third["gap_hours"] - (3 * 24 * 60 + 1 - 300) / 60.0) < 0.01,  # starts at its 1st response
          third["gap_hours"])

    # ---- the page, and the state where there is nothing to compare --------
    print("\n[carry or restart: the page, and the honest empty state]")
    out = tmp / "pu-report"
    args = ns(root=str(root), store=["claude-code"], include_cwd_pattern=["pickup"],
              output_dir=str(out), screen_pack=None,
              ledger=str(out / "sessions.jsonl"))
    op.cmd_report(args)
    pages = read_pages(out)
    check("the carry-vs-restart page is written when a stream did both",
          "carry-vs-restart.html" in pages, sorted(pages))
    page_html = pages.get("carry-vs-restart.html", "")
    check("every panel on it states its decision",
          page_html.count("Decides:") >= 5, page_html.count("Decides:"))
    check("it prints how many pick-ups it is out of, in words",
          "pick-ups across" in page_html)
    check("it names the rule that defines a pause, in whole hours",
          "a pause = a gap of at least 1 hour" in page_html and "1.00 h" not in page_html)
    check("it says there is no resume marker in the transcripts",
          "no resume marker" in page_html)
    check("it does not present itself as advice",
          "What it doesn't show" in page_html and "A fair test" in page_html)
    check("it shows no machine kind code to a reader",
          not re.search(r"\bR[012]\b", page_html), re.findall(r".{20}\bR[012]\b.{20}", page_html)[:2])
    for banned in ("five-line", "5-line", "proxy cost", "before work starts"):
        check("the page never says %r" % banned, banned not in page_html.lower())
    check("home links to it and does not offer a command to re-run it",
          "carry-vs-restart.html" in pages["index.html"])
    ledger = [json.loads(line) for line in
              (out / "sessions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {row["session_id"]: row for row in ledger}
    check("the ledger carries continuation_kind",
          by_id["s-pu-solo"]["continuation_kind"] == "R0",
          by_id["s-pu-solo"].get("continuation_kind"))
    check("... and carry_tokens, carry_cost_usd, gap_hours_before, compactions",
          by_id["s-pu-solo"]["carry_tokens"] == 400_100
          and by_id["s-pu-comp"]["compactions"] == 1
          and by_id["s-pu-solo"]["gap_hours_before"] is not None
          and by_id["s-pu-solo"]["carry_cost_usd"] > 0,
          {k: by_id["s-pu-solo"].get(k) for k in
           ("carry_tokens", "carry_cost_usd", "gap_hours_before")})
    check("... and orientation_cost_usd, which the schema documents",
          by_id["s-pu-2"]["orientation_cost_usd"] is not None,
          by_id["s-pu-2"].get("orientation_cost_usd"))
    check("a session that continues nothing has a null carry cost",
          by_id["s-pu-1"]["carry_tokens"] is None and
          by_id["s-pu-1"]["continuation_kind"] == "first")

    # the empty state: one lone session, no stream that did both
    lonely = root / "projects" / "-w-punone"
    write_jsonl(lonely / "one.jsonl", [
        human(0, "s-pu-none", "just one sitting", cwd="/w/punone"),
        pickup_row(1, "s-pu-none", READ, cwd="/w/punone", read=1_000, out=10),
        pickup_row(2, "s-pu-none", EDIT, cwd="/w/punone", read=1_000, out=10),
    ])
    out2 = tmp / "pu-empty"
    op.cmd_report(ns(root=str(root), store=["claude-code"], include_cwd_pattern=["punone"],
                     output_dir=str(out2), screen_pack=None, ledger=None))
    empty_pages = read_pages(out2)
    check("with nothing to compare, the page is NOT written",
          "carry-vs-restart.html" not in empty_pages, sorted(empty_pages))
    check("... and the analysis index says so instead of offering a re-run",
          "not enough" in empty_pages["index.html"]
          or "nothing to compare" in empty_pages["index.html"])


if __name__ == "__main__":
    op.audit_common.utf8_stdio()          # check names carry → ≥ … on every platform
    if len(sys.argv) > 2 and sys.argv[1] == "--privacy-check":
        sys.exit(privacy_check(sys.argv[2]))
    sys.exit(main())
