#!/usr/bin/env python3
"""What each compaction in Claude Code history cost, and what it saved.

For every compaction (isCompactSummary rows): what it cost, what it saves per
turn, payback horizon, whether it fired on a warm or cold cache, and what
compacting earlier would have saved. See references/lane-economics.md.

MODELED vs OBSERVED: the summarization API call is NOT recorded with usage in
the local ledger. Its cost is modeled here — pre-compact context at the
cache-read rate if the cache was warm (last activity within the TTL), at the
full input rate if cold — and every such figure is labeled modeled. The
post-compact prefix write IS observed. Offline and read-only.
"""
import argparse, collections, glob, json, os, sys, tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_common
from audit_common import CwdFilter, Coverage, add_filter_args

# The price table and the scope filters are shared with every other analysis in
# this skill (audit_common.py). There is deliberately NO built-in fallback
# table here: a second table is how two scripts came to report 15 % and 100 %
# coverage for the same --pricing-file.
_PRICES = None
_COVERAGE = None

def rate(model):
    """($/M in, $/M out) for a model, or None. Unpriced turns are counted in
    coverage and left out of the dollar totals -- never silently defaulted."""
    return audit_common.price_for(model, _PRICES) if _PRICES else None

def ts_parse(t):
    try: return datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except Exception: return None

def collect(root, since, cwd_filter=None):
    cwd_filter = cwd_filter or CwdFilter()
    lanes = collections.defaultdict(list)   # lane -> [(ts, ctx, writes, model)]
    compacts = []                           # (lane, ts, summary_tok)
    seen = set()
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        project_hint = os.path.dirname(path)
        for line in open(path, encoding="utf-8", errors="replace"):
            if '"assistant"' not in line and 'isCompactSummary' not in line: continue
            try: d = json.loads(line)
            except Exception: continue
            if not cwd_filter.keep(d.get("cwd"), project_hint): continue
            ts = ts_parse(d.get("timestamp"))
            if not ts or ts < since: continue
            lane = (d.get("sessionId") or "?",
                    d.get("agentId") or ("side" if d.get("isSidechain") else "main"))
            if d.get("isCompactSummary"):
                compacts.append((lane, ts,
                                 len(json.dumps(d.get("message") or {})) // 4))
                continue
            if d.get("type") != "assistant": continue
            m = d.get("message") or {}; u = m.get("usage") or {}
            if not u: continue
            key = audit_common.response_key(d)   # the one response-identity rule
            if key in seen: continue
            seen.add(key)
            cc = u.get("cache_creation") or {}
            w5 = cc.get("ephemeral_5m_input_tokens") or 0
            w1 = cc.get("ephemeral_1h_input_tokens") or 0
            if not (w5 or w1): w5 = u.get("cache_creation_input_tokens") or 0
            lanes[lane].append((ts, (u.get("cache_read_input_tokens") or 0) + w5 + w1,
                                w5 + w1, m.get("model") or "?", w1 > w5))
    for rows in lanes.values(): rows.sort()
    return lanes, sorted(compacts, key=lambda c: c[1])

def analyze(lanes, compacts, sweep=(5, 10, 20, 40), coverage=None):
    out, unpriced = [], []
    for lane, cts, summary_tok in compacts:
        rows = lanes.get(lane) or []
        pre = [r for r in rows if r[0] < cts]
        post = [r for r in rows if r[0] > cts]
        if not pre or not post: continue
        pre_ctx = pre[-1][1]
        model = pre[-1][3]
        rates = rate(model)
        if coverage is not None:
            coverage.add(model, pre_ctx, rates is not None)
        if rates is None:
            unpriced.append(model)
            continue                # no invented rate: it shows up in coverage
        in_r, out_r = rates
        ttl_min = 60 if pre[-1][4] else 5
        gap_min = (cts - pre[-1][0]).total_seconds() / 60
        warm = gap_min <= ttl_min
        # The post-compaction PREFIX is cache-read PLUS
        # cache-write. It used to be the cache-WRITE alone whenever any was
        # present, so a first post-compaction request that also read a
        # surviving cache prefix understated the post-compaction context and
        # therefore overstated the saving. Priced in two parts, because the
        # read half is charged at 0.1x and the written half at 2.0x, not both
        # at the write rate.
        post_write = post[0][2]
        post_read = max(0, post[0][1] - post[0][2])
        post_prefix = post_read + post_write
        summarize_cost = pre_ctx * (0.1 if warm else 1.0) * in_r / 1e6
        warm_cost = pre_ctx * 0.1 * in_r / 1e6
        total_cost = summarize_cost + summary_tok * out_r / 1e6 \
            + (post_write * 2.0 + post_read * 0.1) * in_r / 1e6
        per_turn_saving = max(0, pre_ctx - post_prefix) * 0.1 * in_r / 1e6
        payback = total_cost / per_turn_saving if per_turn_saving else None
        early = {k: sum(max(0, r[1] - post_prefix) for r in pre[-k:]) * 0.1 * in_r / 1e6
                 for k in sweep}
        out.append({"lane": f"{lane[0][:8]}/{lane[1] if lane[1] in ('main','side') else 'agent'}",
                    "ts": cts.isoformat()[:16], "pre_ctx": pre_ctx,
                    "post_prefix": post_prefix, "post_write": post_write,
                    "post_read": post_read, "gap_min": round(gap_min, 1),
                    "cache": "warm" if warm else "COLD",
                    "cold_penalty": round(summarize_cost - warm_cost, 2),
                    "modeled_cost": round(total_cost, 2),
                    "per_turn_saving": round(per_turn_saving, 3),
                    "payback_turns": round(payback, 1) if payback else None,
                    "post_turns_observed": len(post),
                    "earlier_savings": {k: round(v, 2) for k, v in early.items()}})
    return out

def selftest():
    global _PRICES
    _PRICES = audit_common.load_prices()
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    lane = ("s1", "main")
    rows = [(t0.replace(minute=m), 400_000, 1_000, "claude-opus-5", True)
            for m in range(0, 30)]
    rows.append((t0.replace(hour=14), 400_000, 50_000, "claude-opus-5", True))
    lanes = {lane: rows}
    compacts = [(lane, t0.replace(hour=13, minute=45), 5_000)]
    res = analyze(lanes, compacts)
    assert len(res) == 1, res
    r = res[0]
    assert r["cache"] == "COLD" and r["gap_min"] > 60, r      # 1h15m gap vs 1h TTL
    # FAILS IF REVERTED: this fixture's first post-compaction
    # request WRITES 50k and READS a surviving 350k prefix. The old code
    # called the post-compaction context 50k -- the cache-write alone --
    # which understated post-context and overstated the saving.
    assert r["pre_ctx"] == 400_000 and r["post_prefix"] == 400_000, r
    assert r["post_write"] == 50_000 and r["post_read"] == 350_000, r
    # modeled: 400k*1.0*5 + 5k*25 + (50k*2.0 + 350k*0.1)*5 (per M)
    #        = 2.0 + 0.125 + 0.675 = 2.80
    assert abs(r["modeled_cost"] - 2.80) < 0.02, r
    # ... and the saving follows from it: this compaction did NOT shrink the
    # context (400k before, 400k of read+write after), so the per-turn saving
    # is 0. The old arithmetic reported $0.175 a turn and a 10-turn saving of
    # $1.75 from a compaction that saved nothing.
    assert r["per_turn_saving"] == 0.0, r
    assert r["earlier_savings"][10] == 0.0, r
    # a compaction that DOES shrink the context still shows a saving
    shrunk = list(rows[:-1]) + [(t0.replace(hour=14), 60_000, 50_000, "claude-opus-5", True)]
    r3 = analyze({lane: shrunk}, compacts)[0]
    assert r3["post_prefix"] == 60_000 and r3["per_turn_saving"] > 0.1, r3
    compacts_hot = [(lane, t0.replace(minute=29, second=30), 5_000)]
    r2 = analyze({lane: rows[:29] + rows[-1:]}, compacts_hot)[0]
    assert r2["cache"] == "warm" and r2["cold_penalty"] == 0.0

    # an unpriced model is dropped from dollars and shows in coverage,
    # never quietly charged at Opus rates
    cov = Coverage()
    rows_u = [(t, c, w, "some-other-vendor-model", h) for (t, c, w, m, h) in rows]
    assert analyze({lane: rows_u}, compacts, coverage=cov) == []
    assert cov.fraction == 0.0 and not cov.ok and "cover only" in cov.caveat()

    # the shared cwd filter reaches this script too
    f = CwdFilter(exclude=["scratch-runs", "myrepo"])
    assert not f.keep("/home/dev/scratch-runs/r1")
    assert not f.keep("/home/dev/Projects/myrepo")
    assert f.keep("/home/dev/Projects/something-else")
    print("selftest OK")

def main():
    # What this prints shows the home folder as `~`, like the JSON it writes.
    with audit_common.home_relative_stdout():
        return _main()


def _main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"),
                    help="folder of Claude Code transcripts (default: ~/.claude/projects)")
    ap.add_argument("--since", required=False, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--json-out", help="write full machine-readable results here")
    ap.add_argument("--selftest", action="store_true", help="run the built-in checks and exit")
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
    lanes, compacts = collect(a.root, since, cwd_filter)
    res = analyze(lanes, compacts, coverage=coverage)
    if a.json_out:
        audit_common.write_output(a.json_out, json.dumps(
                  {"scope": cwd_filter.describe(),
                   "price_table": _PRICES["table_id"],
                   "price_coverage": round(coverage.fraction, 3),
                   "price_coverage_label": coverage.label(),
                   "price_coverage_caveat": coverage.caveat() or None,
                   "compacts": res}, indent=1))
    cold = [r for r in res if r["cache"] == "COLD"]
    print(f"scope: {cwd_filter.describe()}  |  prices: {_PRICES['table_id']}"
          f"  |  rows left out by filter: {cwd_filter.filtered_rows:,d}")
    print(f"{len(res)} compactions; {len(cold)} ran after the cache had expired, which cost "
          f"{coverage.money(sum(r['cold_penalty'] for r in res))} extra (modeled)")
    if coverage.caveat(): print(coverage.caveat())
    if res:
        print(f"\n  {'when':16s} {'conversation':14s} {'context':>11s} {'cache':5s} {'idle':>8s} "
              f"{'cost*':>6s} {'saves/turn':>10s} {'pays back':>13s} {'turns after':>11s} {'20 turns earlier':>16s}")
    for r in res:
        payback = f"{r['payback_turns']} turns" if r['payback_turns'] is not None else "never"
        context = f"{r['pre_ctx']//1000}k→{r['post_prefix']//1000}k"
        print(f"  {r['ts']:16s} {r['lane']:14s} {context:>11s} {r['cache'].lower():5s} "
              f"{'%.1f m' % r['gap_min']:>8s} {'$%.2f' % r['modeled_cost']:>6s} "
              f"{'$%.2f' % r['per_turn_saving']:>10s} {payback:>13s} {r['post_turns_observed']:11d} "
              f"{'+$%.2f' % r['earlier_savings'][20]:>16s}")
    if res:
        print("\n* modeled: the call that writes the summary is not in the logs, so its cost is "
              "estimated from the context size.\ncache: cold = more than 5 or 60 minutes "
              "(the cache lifetime) since the last response; idle = that pause.\n"
              "20 turns earlier = what compacting 20 turns sooner would have saved.")

if __name__ == "__main__":
    main()
