#!/usr/bin/env python3
"""Where token cost sits, per conversation or subagent, and when its cache went cold.

Complements claude_cache_audit.py (5-minute or 1-hour cache) with the
question: WHERE does token volume live? See references/lane-economics.md for
the cost model and how to read it. Offline and read-only; never invokes
Claude.

A lane is one conversation or subagent: (sessionId, agentId | side | main).
Each keeps its own cache. A cold event is a turn reading back <30% of the
previous turn's cached context while writing >50% of it again; TTL-EXPIRY
when the pause outlived the cache lifetime (5 or 60 minutes), INVALIDATION
when something else reset it.

Dollar figures are estimated cost at list API rates from the shared price
table (references/pricing-public-example.json): an estimate, never an invoice.

Scope filters (--include-cwd-pattern / --exclude-cwd-pattern) and the price
table both come from audit_common.py, so they behave identically here and in
every other analysis in this skill.
"""
import argparse, collections, glob, json, os, sys, tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_common
from audit_common import CwdFilter, Coverage, add_filter_args, price_for

CACHE_READ_MULT = 0.1
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0


def ts_parse(t):
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t / 1000 if t > 2e10 else t, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except ValueError:
        return None


def iter_rows(root, since, until, cwd_filter=None):
    cwd_filter = cwd_filter or CwdFilter()
    seen = set()
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        proj = audit_common.project_folder_label(os.path.basename(os.path.dirname(path)))
        project_hint = os.path.dirname(path)
        for line in open(path, encoding="utf-8", errors="replace"):
            if '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not cwd_filter.keep(d.get("cwd"), project_hint):
                continue
            if d.get("type") != "assistant":
                continue
            ts = ts_parse(d.get("timestamp"))
            if not ts or ts < since or (until and ts >= until):
                continue
            m = d.get("message") or {}
            u = m.get("usage") or {}
            if not u:
                continue
            key = audit_common.response_key(d)   # the one response-identity rule
            if key in seen:
                continue
            seen.add(key)
            cc = u.get("cache_creation") or {}
            w5 = cc.get("ephemeral_5m_input_tokens") or 0
            w1 = cc.get("ephemeral_1h_input_tokens") or 0
            if not (w5 or w1):
                w5 = u.get("cache_creation_input_tokens") or 0
            lane = (d.get("sessionId") or "?",
                    d.get("agentId") or ("side" if d.get("isSidechain") else "main"))
            yield lane, {
                "ts": ts, "read": u.get("cache_read_input_tokens") or 0,
                "w5": w5, "w1": w1, "uin": u.get("input_tokens") or 0,
                "out": u.get("output_tokens") or 0,
                "model": m.get("model") or "?", "proj": proj,
            }


def analyze(rows_by_lane, prices, ctx_threshold, coverage=None):
    lanes, events = [], []
    tot = collections.Counter()
    coverage = coverage if coverage is not None else Coverage()
    for lane, rows in rows_by_lane.items():
        rows.sort(key=lambda r: r["ts"])
        reads = sum(r["read"] for r in rows)
        w5 = sum(r["w5"] for r in rows)
        w1 = sum(r["w1"] for r in rows)
        uin = sum(r["uin"] for r in rows)
        out = sum(r["out"] for r in rows)
        maxctx = max(r["read"] + r["w5"] + r["w1"] for r in rows)
        fat = sum(r["read"] for r in rows if r["read"] + r["w5"] + r["w1"] > ctx_threshold)
        # PER-RESPONSE pricing. The lane used to be
        # priced entirely at its LAST observed model, so one Haiku response at
        # the end of an Opus lane repriced the whole lane -- and the fallback
        # classifier can swap a model in mid-session without anyone asking.
        model = rows[-1]["model"]
        models = sorted({r["model"] for r in rows})
        cost = 0.0
        priced_any = False
        for r in rows:
            rp = price_for(r["model"], prices)
            row_tok = r["read"] + r["w5"] + r["w1"] + r["uin"] + r["out"]
            coverage.add(r["model"], row_tok, rp is not None)
            if not rp:
                continue
            priced_any = True
            cost += (r["read"] * CACHE_READ_MULT * rp[0] + r["w5"] * CACHE_WRITE_5M_MULT * rp[0]
                     + r["w1"] * CACHE_WRITE_1H_MULT * rp[0] + r["uin"] * rp[0]
                     + r["out"] * rp[1]) / 1e6
            tot["read$"] += r["read"] * CACHE_READ_MULT * rp[0] / 1e6
            tot["write$"] += (r["w5"] * CACHE_WRITE_5M_MULT
                              + r["w1"] * CACHE_WRITE_1H_MULT) * rp[0] / 1e6
            tot["uin$"] += r["uin"] * rp[0] / 1e6
            tot["out$"] += r["out"] * rp[1] / 1e6
            tot["priced_tok"] += row_tok
        if not priced_any:
            cost = None
        tot["all_tok"] += reads + w5 + w1 + uin + out
        tot["reads"] += reads
        tot["fat_reads"] += fat
        lanes.append({
            "session": lane[0], "lane": lane[1],
            "kind": lane[1] if lane[1] in ("main", "side") else "agent",
            "turns": len(rows), "max_ctx": maxctx, "reads": reads,
            "writes": w5 + w1, "output": out,
            "reads_per_turn": reads // max(1, len(rows)),
            "hit_ratio": round(reads / max(1, reads + w5 + w1 + uin), 3),
            "fat_read_share": round(fat / reads, 3) if reads else 0.0,
            "model": model, "models": models, "mixed_models": len(models) > 1,
            "project": rows[-1]["proj"], "est_cost_usd": cost,
        })
        for prev, cur in zip(rows, rows[1:]):
            pctx = prev["read"] + prev["w5"] + prev["w1"]
            if pctx < 50_000:
                continue
            cctx_w = cur["w5"] + cur["w1"]
            if cur["read"] < 0.3 * pctx and cctx_w > 0.5 * pctx:
                gap_min = (cur["ts"] - prev["ts"]).total_seconds() / 60
                ttl_min = 60 if prev["w1"] > prev["w5"] else 5
                events.append({
                    "kind": "TTL-EXPIRY" if gap_min > ttl_min * 1.1 else "INVALIDATION",
                    "gap_min": round(gap_min, 1), "rewritten": cctx_w,
                    "ts": cur["ts"].isoformat(), "session": lane[0][:8],
                    "lane": lane[1] if lane[1] in ("main", "side") else "agent",
                    "model": cur["model"], "project": cur["proj"],
                })
    lanes.sort(key=lambda l: (l["est_cost_usd"] is None, -(l["est_cost_usd"] or 0)))
    return lanes, events, tot, coverage


def summarize(lanes, events, tot, top_n, coverage=None):
    total = sum(l["est_cost_usd"] or 0 for l in lanes)
    agent_hits = sorted(l["hit_ratio"] for l in lanes
                        if l["kind"] == "agent" and l["turns"] >= 3)
    ev = collections.Counter(e["kind"] for e in events)
    ev_tok = collections.Counter()
    for e in events:
        ev_tok[e["kind"]] += e["rewritten"]
    out = {
        "est_cost_usd_total": round(total, 2),
        "components": {k: round(tot[k], 2) for k in ("read$", "write$", "uin$", "out$")},
        "price_coverage": round(coverage.fraction, 3) if coverage is not None
                          else round(tot["priced_tok"] / max(1, tot["all_tok"]), 3),
        "price_coverage_label": coverage.label() if coverage is not None else None,
        "price_coverage_caveat": (coverage.caveat() or None) if coverage is not None else None,
        "read_volume_tok": tot["reads"],
        "fat_read_share": round(tot["fat_reads"] / max(1, tot["reads"]), 3),
        "concentration": {f"top{n}": round(sum(l["est_cost_usd"] or 0 for l in lanes[:n]) / total, 3)
                          for n in (5, 10, 20) if total},
        "lanes": len(lanes),
        "agent_lane_hit_ratio_median":
            agent_hits[len(agent_hits) // 2] if agent_hits else None,
        "cold_events": {k: {"n": ev[k], "rewritten_tok": ev_tok[k]} for k in ev},
        "top_lanes": lanes[:top_n],
        "events": sorted(events, key=lambda e: -e["rewritten"]),
    }
    return out


def selftest():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    def row(minute, read, w5, w1, sid="s1", agent=None, model="claude-opus-5",
            mid=None):
        return {"type": "assistant", "sessionId": sid, "agentId": agent,
                "timestamp": t0.replace(minute=minute).isoformat(),
                "message": {"id": mid or f"m{minute}{agent}", "model": model,
                            "usage": {"cache_read_input_tokens": read,
                                      "cache_creation": {"ephemeral_5m_input_tokens": w5,
                                                         "ephemeral_1h_input_tokens": w1},
                                      "input_tokens": 10, "output_tokens": 100}}}
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "p"))
        with open(os.path.join(td, "p", "a.jsonl"), "w", encoding="utf-8") as f:
            for r in [row(1, 0, 100_000, 0), row(2, 100_000, 5_000, 0),
                      row(2, 100_000, 5_000, 0, mid="m2None"),        # duplicate
                      row(10, 1_000, 90_000, 0),                      # invalidation (5m ttl, gap 8m>5.5 -> expiry!)
                      row(20, 0, 0, 200_000, agent="ag1"),
                      row(22, 1_000, 0, 190_000, agent="ag1")]:       # 1h ttl, gap 2m -> INVALIDATION
                f.write(json.dumps(r) + "\n")
        prices = audit_common.load_prices()
        rows_by_lane = collections.defaultdict(list)
        for lane, r in iter_rows(td, t0.replace(minute=0), None):
            rows_by_lane[lane].append(r)
        lanes, events, tot, cov = analyze(rows_by_lane, prices, 150_000)
        assert len(lanes) == 2, lanes                       # dedup + lane split
        assert sum(l["turns"] for l in lanes) == 5
        kinds = sorted(e["kind"] for e in events)
        assert kinds == ["INVALIDATION", "TTL-EXPIRY"], events
        inv = next(e for e in events if e["kind"] == "INVALIDATION")
        assert inv["lane"] == "agent" and inv["rewritten"] == 190_000
        s = summarize(lanes, events, tot, 5, cov)
        assert s["price_coverage"] == 1.0
        assert s["price_coverage_label"] == "coverage 100 %", s["price_coverage_label"]
        assert s["price_coverage_caveat"] is None
        assert abs(s["est_cost_usd_total"]
                   - sum(l["est_cost_usd"] for l in lanes)) < 0.01

        # the shared cwd filter is wired in and actually drops rows here
        rows_by_lane = collections.defaultdict(list)
        filt = CwdFilter(exclude=["a-nonexistent-fragment"])
        for lane, r in iter_rows(td, t0.replace(minute=0), None, filt):
            rows_by_lane[lane].append(r)
        assert len(rows_by_lane) == 2 and filt.filtered_rows == 0
        rows_by_lane = collections.defaultdict(list)
        filt = CwdFilter(exclude=[os.path.basename(td)])
        for lane, r in iter_rows(td, t0.replace(minute=0), None, filt):
            rows_by_lane[lane].append(r)
        assert not rows_by_lane and filt.filtered_rows == 6, filt.filtered_rows
    print("selftest OK")


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
    ap.add_argument("--until", required=False, help="YYYY-MM-DD (UTC, exclusive)")
    ap.add_argument("--ctx-threshold", type=int, default=150_000,
                    help="a context this many tokens or larger counts as big (default 150000)")
    ap.add_argument("--top", type=int, default=15,
                    help="how many of the costliest conversations and subagents to list")
    ap.add_argument("--pricing-file", default=audit_common.DEFAULT_PRICING_FILE,
                    help="the shared price table (default: the skill's "
                         "references/pricing-public-example.json). Every analysis "
                         "in this skill reads the same file and reports the same "
                         "coverage number.")
    ap.add_argument("--json-out", help="write full machine-readable results here")
    ap.add_argument("--selftest", action="store_true", help="run the built-in checks and exit")
    add_filter_args(ap)
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc) \
        if a.since else datetime(1970, 1, 1, tzinfo=timezone.utc)
    until = datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc) \
        if a.until else None
    prices = audit_common.load_prices(a.pricing_file)
    cwd_filter = CwdFilter.from_args(a)
    rows_by_lane = collections.defaultdict(list)
    for lane, r in iter_rows(a.root, since, until, cwd_filter):
        rows_by_lane[lane].append(r)
    lanes, events, tot, cov = analyze(rows_by_lane, prices, a.ctx_threshold)
    s = summarize(lanes, events, tot, a.top, cov)
    s["scope"] = cwd_filter.describe()
    s["price_table"] = prices["table_id"]
    if a.json_out:
        audit_common.write_output(a.json_out, json.dumps(s, indent=1, default=str))
    c = s["components"]
    cold_names = {"TTL-EXPIRY": "the pause outlived the cache lifetime",
                  "INVALIDATION": "something else reset the cache"}
    print(f"scope: {s['scope']}  |  prices: {s['price_table']}"
          f"  |  rows left out by filter: {cwd_filter.filtered_rows:,d}")
    print(f"{s['lanes']} lanes (a lane is one conversation or subagent; each keeps its own cache); "
          f"est. cost at list API rates {cov.money(s['est_cost_usd_total'])}")
    if cov.caveat():
        print(cov.caveat())
    print(f"cost split: cache reads ${c['read$']:,.2f}  cache writes ${c['write$']:,.2f}  "
          f"uncached input ${c['uin$']:,.2f}  output ${c['out$']:,.2f}")
    print(f"cache reads: {s['read_volume_tok']/1e9:.2f}B tokens, "
          f"{100*s['fat_read_share']:.0f}% of them with a context over {a.ctx_threshold//1000}k; "
          f"median subagent cache hit ratio {s['agent_lane_hit_ratio_median']} (share of input read from cache)")
    for k, v in s["concentration"].items():
        print(f"  the {k[3:]} costliest lanes = {100*v:.0f}% of est. cost")
    for k, v in s["cold_events"].items():
        print(f"  cache went cold, {cold_names.get(k, k)} ({k}): "
              f"{v['n']} times, {v['rewritten_tok']:,d} tokens written again")
    print(f"\n{'est.$':>8s} {'turns':>5s} {'maxctx':>7s} {'rd/turn':>7s} "
          f"{'hit':>5s} {'big%':>4s}  lane")
    for l in s["top_lanes"]:
        cost = f"${l['est_cost_usd']:,.2f}" if l["est_cost_usd"] is not None else "unpriced"
        print(f"{cost:>8s} {l['turns']:5d} {l['max_ctx']//1000:6d}k "
              f"{l['reads_per_turn']//1000:6d}k {l['hit_ratio']:.2f} "
              f"{100*l['fat_read_share']:3.0f}%  {l['kind']:5s} "
              f"{l['model'].replace('claude-','')[:12]} {l['session'][:8]} "
              f"{l['project'][:32]}")
    print(f"\nmaxctx = largest context; rd/turn = cache reads per turn; hit = share of input "
          f"read from cache; big% = share of reads with a context over {a.ctx_threshold//1000}k")


if __name__ == "__main__":
    main()
