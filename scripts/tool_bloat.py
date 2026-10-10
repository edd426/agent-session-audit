#!/usr/bin/env python3
"""Which tool results cost the most, because every later turn reads them again.

A tool result entering a lane's context is re-read (at the 0.1x cache-read
rate) on every remaining turn of that lane. Downstream est. cost at list API rates of one
result = result tokens x remaining assistant turns x 0.1 x base input rate.
Ranked by tool, the remedy is tool-specific: fat Bash output -> filter or
redirect+tail; fat Read -> limit/offset or a disposable explore lane; fat
web/MCP results -> summarize in a sidechain first. Offline and read-only.
"""
import argparse, bisect, collections, glob, json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_common
from audit_common import CwdFilter, Coverage, add_filter_args

# Shared price table and shared scope filters (audit_common.py). No built-in
# fallback rate: an unpriced model lowers coverage, it is not charged at Opus.
_PRICES = None

def rate(model):
    """$/M input for a model, or None when the shared table does not price it."""
    r = audit_common.price_for(model, _PRICES) if _PRICES else None
    return r[0] if r else None

def ts_parse(t):
    try: return datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except Exception: return None

def result_tokens(c):
    """Estimate context tokens of a tool_result. Text ~ chars/4. Image blocks
    bill as vision tokens (~(w*h)/750, capped ~1.6k), NOT as their base64
    serialization — counting chars/4 there overstates by ~100x."""
    if isinstance(c, str):
        return len(c) // 4
    if isinstance(c, list):
        tot = 0
        for b in c:
            if isinstance(b, dict) and b.get("type") == "image":
                tot += 1_600
            else:
                tot += len(json.dumps(b)) // 4
        return tot
    return len(json.dumps(c or "")) // 4

def target_of(name, inp):
    # `~` before cutting: a cut path must not keep part of the account name.
    if inp.get("file_path"):
        return audit_common.home_relative(str(inp["file_path"]))[-60:]   # the end informs
    for k in ("pattern", "url", "query", "command", "prompt"):
        if inp.get(k):
            return audit_common.home_relative(str(inp[k])).replace("\n", " ")[:60]
    return ""

def collect(root, since, cwd_filter=None):
    turn_ts = collections.defaultdict(list)   # lane -> sorted assistant ts
    compact_ts = collections.defaultdict(list)  # lane -> sorted compact ts
    lane_model = {}
    uses = {}                                 # tool_use_id -> (lane, tool, target, ts)
    res_by_id = {}                            # tool_use_id -> (lane, tool, target, ts, tokens)
    seen = set()
    reader = audit_common.EventReader()      # the one canonical reader
    cwd_filter = cwd_filter or CwdFilter()
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        project_hint = os.path.dirname(path)
        for line in open(path, encoding="utf-8", errors="replace"):
            if '"assistant"' not in line and '"tool_result"' not in line \
                    and 'isCompactSummary' not in line: continue
            try: d = json.loads(line)
            except Exception: continue
            if not cwd_filter.keep(d.get("cwd"), project_hint): continue
            ts = ts_parse(d.get("timestamp"))
            if not ts or ts < since: continue
            lane = (d.get("sessionId") or "?",
                    d.get("agentId") or ("side" if d.get("isSidechain") else "main"))
            if d.get("isCompactSummary"):
                compact_ts[lane].append(ts)
                continue
            m = d.get("message") or {}
            content = m.get("content")
            if d.get("type") == "assistant":
                # A RESPONSE, not a ROW. Claude Code writes one row per
                # content block and repeats the identical usage object on
                # each, so counting rows here inflates every replay horizon
                # about 2x. audit_common owns the rule.
                response, is_new = reader.add(d, ts)
                if response is None: continue
                if is_new:
                    turn_ts[lane].append(ts)
                    lane_model[lane] = m.get("model") or "?"
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            uses[b.get("id")] = (lane, b.get("name") or "?",
                                                 target_of(b.get("name"), b.get("input") or {}), ts)
            elif d.get("type") == "user" and isinstance(content, list):
                key = (d.get("sessionId"), d.get("uuid"))
                if key in seen: continue
                seen.add(key)
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result" \
                            and b.get("tool_use_id") in uses:
                        tid = b["tool_use_id"]
                        lane_u, tool, tgt, uts = uses[tid]
                        tok = result_tokens(b.get("content"))
                        # one physical call can be re-serialized on resume/compact
                        # continuations: keep one row per tool_use_id (earliest ts,
                        # largest size)
                        prev = res_by_id.get(tid)
                        if prev is None or tok > prev[4]:
                            res_by_id[tid] = (lane_u, tool, tgt,
                                              min(ts, prev[3]) if prev else ts, tok)
    for v in turn_ts.values(): v.sort()
    for v in compact_ts.values(): v.sort()
    return turn_ts, compact_ts, lane_model, list(res_by_id.values())

def analyze(turn_ts, compact_ts, lane_model, results, coverage=None):
    by_tool = collections.defaultdict(lambda: [0, 0, 0.0, 0])  # n, tok, cost, maxtok
    calls = []
    for lane, tool, tgt, ts, tok in results:
        turns = turn_ts.get(lane) or []
        # a result is re-read only until the lane's next compaction evicts it
        horizon = next((c for c in compact_ts.get(lane, []) if c > ts), None)
        end = bisect.bisect_right(turns, horizon) if horizon else len(turns)
        remaining = max(0, end - bisect.bisect_right(turns, ts))
        model = lane_model.get(lane, "?")
        in_rate = rate(model)
        if coverage is not None:
            coverage.add(model, tok, in_rate is not None)
        if in_rate is None:
            continue                # unpriced: visible in coverage, not in dollars
        cost = tok * remaining * 0.1 * in_rate / 1e6
        t = by_tool[tool]
        t[0] += 1; t[1] += tok; t[2] += cost; t[3] = max(t[3], tok)
        calls.append((cost, tok, remaining, tool, tgt,
                      f"{lane[0][:8]}/{lane[1] if lane[1] in ('main','side') else 'agent'}"))
    league = sorted(((c, n, tk, mx, tool) for tool, (n, tk, c, mx) in by_tool.items()),
                    reverse=True)
    calls.sort(reverse=True)
    return league, calls

def selftest():
    global _PRICES
    _PRICES = audit_common.load_prices()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lane = ("s1", "main")
    turn_ts = {lane: [t0.replace(minute=m) for m in range(11)]}
    lane_model = {lane: "claude-opus-5"}
    results = [(lane, "Bash", "npm test", t0.replace(minute=0, second=30), 100_000),
               (lane, "Read", "a.py", t0.replace(minute=9, second=30), 100_000)]
    league, calls = analyze(turn_ts, {}, lane_model, results)
    # Bash at turn 1: 10 remaining turns; Read at turn 10: 1 remaining
    assert calls[0][3] == "Bash" and calls[0][2] == 10
    assert abs(calls[0][0] - 100_000 * 10 * 0.5 / 1e6) < 1e-6   # $0.50
    assert abs(calls[1][0] - 0.05) < 1e-6
    assert league[0][4] == "Bash"
    # a compaction at minute 5 evicts the Bash result: only 5 turns remain
    league2, calls2 = analyze(turn_ts, {lane: [t0.replace(minute=4, second=30)]},
                              lane_model, results)
    bash2 = next(c for c in calls2 if c[3] == "Bash")
    assert bash2[2] == 4 and abs(bash2[0] - 0.20) < 1e-6, bash2

    # unpriced model -> no dollars, visible coverage; never an assumed rate
    cov = Coverage()
    l2 = ("s2", "main")
    _, calls3 = analyze({l2: turn_ts[lane]}, {}, {l2: "some-other-vendor-model"},
                        [(l2, "Bash", "x", t0.replace(minute=0, second=30), 100_000)],
                        coverage=cov)
    assert calls3 == [] and cov.fraction == 0.0 and not cov.ok

    # a <synthetic> record is not a model: out of the coverage denominator
    cov2 = Coverage()
    cov2.add("<synthetic>", 10_000, False)
    cov2.add("claude-opus-5", 10_000, True)
    assert cov2.total_tokens == 10_000 and cov2.fraction == 1.0

    # the shared cwd filter reaches this script too
    f = CwdFilter(include=["myproj"])
    assert f.keep("/home/dev/myproj") and not f.keep("/home/dev/other")

    # ---- the INGESTION path, on a real-shaped fixture ---------------------
    # This selftest used to exercise `analyze()` on hand-built inputs only,
    # which is why it passed while `collect()` counted ~2x too many turns.
    # Claude Code writes ONE ROW PER CONTENT BLOCK and repeats the identical
    # usage object on each; the fixture below is that shape.
    import tempfile as _tf, shutil as _sh, json as _json
    tmp = _tf.mkdtemp(prefix="tool-bloat-selftest-")
    try:
        proj = os.path.join(tmp, "-w-proj")
        os.makedirs(proj)
        usage = {"input_tokens": 10, "output_tokens": 100,
                 "cache_read_input_tokens": 1000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                    "ephemeral_1h_input_tokens": 0}}
        rows = []
        for n in range(3):                       # three RESPONSES
            mid = "msg-%d" % n
            for block_index, block in enumerate(
                    [{"type": "thinking", "thinking": "x" * 40},
                     {"type": "text", "text": "doing it"},
                     {"type": "tool_use", "id": "t%d" % n, "name": "Bash",
                      "input": {"command": "npm test"}}]):
                rows.append({"type": "assistant", "sessionId": "s1", "cwd": "/w/proj",
                             "uuid": "u-%d-%d" % (n, block_index),
                             "timestamp": t0.replace(minute=n).isoformat().replace("+00:00", "Z"),
                             "message": {"id": mid, "model": "claude-opus-5",
                                         "usage": usage, "content": [block]}})
            rows.append({"type": "user", "sessionId": "s1", "cwd": "/w/proj",
                         "uuid": "ur-%d" % n,
                         "timestamp": t0.replace(minute=n, second=30)
                         .isoformat().replace("+00:00", "Z"),
                         "message": {"role": "user", "content": [
                             {"type": "tool_result", "tool_use_id": "t%d" % n,
                              "content": "y" * 4000}]}})
        with open(os.path.join(proj, "s1.jsonl"), "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(_json.dumps(row) + "\n")
        tts, cts, lm, res = collect(tmp, datetime(2020, 1, 1, tzinfo=timezone.utc))
        got = tts[("s1", "main")]
        assert len(got) == 3, ("rows counted as turns: %d (should be 3 responses)" % len(got))
        assert len(res) == 3, res
        # replaying the same file must change nothing
        _sh.copyfile(os.path.join(proj, "s1.jsonl"), os.path.join(proj, "s1-resumed.jsonl"))
        tts2, _c2, _m2, res2 = collect(tmp, datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert len(tts2[("s1", "main")]) == 3, len(tts2[("s1", "main")])
        assert len(res2) == 3, len(res2)
    finally:
        _sh.rmtree(tmp, ignore_errors=True)
    print("selftest OK")

def write_ledger(path, results, scope=None):
    """Upsert this analysis's per-SESSION columns into the shared ledger.

    Built from `results` (the raw per-tool-call rows), NOT from the printed
    `calls` league: the league truncates its lane label to 8 characters for
    display, which will not join to a session id. A lane is
    (sessionId, agentId); a sidechain's tool results are carried in the parent
    session's context and paid for by it, so they are folded into that
    session rather than dropped.
    """
    import collections as _c
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import session_ledger
    except Exception as exc:
        print(f"ledger skipped: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return
    per_tool = _c.defaultdict(lambda: _c.defaultdict(int))
    for lane, tool, tgt, ts, tok in results:
        per_tool[lane[0]][tool] += tok
    rows = []
    for sid, tools in per_tool.items():
        if not sid or sid == "?":
            continue
        top = max(tools.items(), key=lambda kv: kv[1])
        rows.append({"session_id": sid, "tool_result_bytes": sum(tools.values()),
                     "top_tool": top[0], "top_tool_bytes": top[1]})
    stats = session_ledger.upsert(path, rows, "tool_bloat.py",
                                  ["tool_result_bytes", "top_tool", "top_tool_bytes"],
                                  scope=audit_common.home_relative(scope))
    print(f"\nledger: {stats['ledger']} — {stats['rows_updated']} session(s) updated, "
          f"{stats['rows_added']} added; {session_ledger.headline(path)}")

def main():
    audit_common.utf8_stdio()
    # What this prints shows the home folder as `~`, like the JSON it writes.
    with audit_common.home_relative_stdout():
        return _main()


def _main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"),
                    help="folder of Claude Code transcripts (default: ~/.claude/projects)")
    ap.add_argument("--since", required=False, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--top", type=int, default=12, help="how many tools and single results to list")
    ap.add_argument("--json-out", help="write full machine-readable results here")
    ap.add_argument("--selftest", action="store_true", help="run the built-in checks and exit")
    ap.add_argument("--ledger", metavar="PATH",
                    help="add this analysis's columns to the session ledger "
                         "(sessions.jsonl). Columns written by other analyses are kept; "
                         "see references/session-ledger.md")
    ap.add_argument("--pricing-file", default=audit_common.DEFAULT_PRICING_FILE,
                    help="the shared price table (default: the skill's "
                         "references/pricing-public-example.json). Every analysis "
                         "in this skill reads the same file and reports the same "
                         "coverage number.")
    add_filter_args(ap)
    a = ap.parse_args()
    if a.selftest: selftest(); return
    global _PRICES
    _PRICES = audit_common.load_prices(a.pricing_file)
    coverage = Coverage()
    cwd_filter = CwdFilter.from_args(a)
    since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc) \
        if a.since else datetime(1970, 1, 1, tzinfo=timezone.utc)
    turn_ts, compact_ts, lane_model, results = collect(a.root, since, cwd_filter)
    league, calls = analyze(turn_ts, compact_ts, lane_model, results, coverage)
    total = sum(l[0] for l in league)
    print(f"scope: {cwd_filter.describe()}  |  prices: {_PRICES['table_id']}"
          f"  |  rows left out by filter: {cwd_filter.filtered_rows:,d}")
    print(f"{sum(l[1] for l in league):,} tool results. Every later turn in the same conversation "
          f"reads them again from the cache: est. {coverage.money(total)} at list API rates "
          f"(result tokens x later turns x the cache-read price)")
    if coverage.caveat(): print(coverage.caveat())
    print(f"\n{'re-read $':>11s} {'share':>6s} {'calls':>6s} {'result tok':>11s} {'largest':>7s}  tool")
    for cost, n, tok, mx, tool in league[:a.top]:
        print(f"${cost:10,.2f} {100*cost/max(total,1e-9):5.1f}% {n:6,d} {tok:11,d} {mx//1000:6d}k  {tool}")
    print("\nlargest single results (re-read cost, size x later turns that read it again):")
    for cost, tok, remaining, tool, tgt, lane in calls[:a.top]:
        print(f"  ${cost:7.2f} {tok//1000:4d}k tok x{remaining:4d} turns  {tool:22s} {lane:15s} {tgt}")
    if a.ledger:
        write_ledger(a.ledger, results,
                     scope=f"--root {a.root} --since {a.since} "
                           f"[{cwd_filter.describe()}]")
    if a.json_out:
        audit_common.write_output(a.json_out, json.dumps({"scope": cwd_filter.describe(),
                   "price_table": _PRICES["table_id"],
                   "price_coverage": round(coverage.fraction, 3),
                   "price_coverage_label": coverage.label(),
                   "price_coverage_caveat": coverage.caveat() or None,
                   "league": [{"tool": t, "downstream_cost": round(c, 2), "calls": n,
                               "result_tok": tk, "max_tok": mx}
                              for c, n, tk, mx, t in league],
                   "top_calls": [{"cost": round(c, 2), "tok": tk, "remaining": r,
                                  "tool": t, "target": tg, "lane": ln}
                                 for c, tk, r, t, tg, ln in calls[:50]]},
                  indent=1))

if __name__ == "__main__":
    main()
