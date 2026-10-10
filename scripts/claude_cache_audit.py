#!/usr/bin/env python3
"""Should Claude Code's prompt cache last 5 minutes or 1 hour?

Reads token counts and timestamps from Claude Code history, removes duplicate
rows, and estimates what each cache lifetime would have cost on that history.
It never calls Claude and never writes inside the history folder.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import dataclasses
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_common
from audit_common import CwdFilter, Coverage, add_filter_args


GAP_LABELS = ["<1 min", "1–5 min", "5–15 min", "15–30 min", "30–60 min", ">60 min"]
WRITE_MULTIPLIERS = {"5m": 1.25, "1h": 2.0}
READ_MULTIPLIER = 0.10
POLICY_BREAK_EVEN = (2.0 - 1.25) / (1.25 - 0.10)  # 65.217%
TRANSITION_BREAK_EVEN = (2.0 - 1.25) / ((2.0 - 1.25) + (1.25 - 0.10))  # 39.474%


@dataclasses.dataclass
class Event:
    key: str
    timestamp: float
    timestamp_iso: str
    session: str
    lane: str
    project: str
    model: str
    version: str
    branch: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    cache_write_unknown: int
    api_error: bool
    duplicate_rows: int = 1
    inferred_ttl: Optional[str] = None

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h + self.cache_write_unknown

    @property
    def cacheable(self) -> int:
        return self.cache_read + self.cache_write

    @property
    def usage_score(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write


@dataclasses.dataclass
class HumanPrompt:
    key: str
    timestamp: float
    session: str
    project: str


@dataclasses.dataclass
class Gap:
    seconds: float
    previous: Event
    current: Event
    structural_tokens: int
    causes: Tuple[str, ...]

    @property
    def same_epoch(self) -> bool:
        return not self.causes

    @property
    def ttl_eligible(self) -> bool:
        return 300 < self.seconds <= 3600 and self.same_epoch and self.structural_tokens > 0


def numeric(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def stable_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "unknown").encode("utf-8", "replace")).hexdigest()[:12]


def parse_timestamp(value: Any) -> Optional[Tuple[float, str]]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            moment = dt.datetime.fromtimestamp(number, dt.timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
        return number, moment.isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        moment = moment.astimezone(dt.timezone.utc)
        return moment.timestamp(), moment.isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def parse_bound(value: Optional[str], is_until: bool = False) -> Optional[float]:
    if not value:
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        moment = dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)
        return moment.timestamp()
    parsed = parse_timestamp(text)
    if not parsed:
        raise ValueError("invalid date/time: %s" % value)
    return parsed[0]


def within(timestamp: float, since: Optional[float], until: Optional[float]) -> bool:
    return (since is None or timestamp >= since) and (until is None or timestamp < until)


def is_human_prompt(record: Dict[str, Any]) -> bool:
    if record.get("type") != "user":
        return False
    if record.get("isMeta") or record.get("isCompactSummary") or record.get("toolUseResult"):
        return False
    if str(record.get("promptSource") or "").lower() in {"sdk", "system"}:
        return False
    message = record.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list) or not content:
        return False
    block_types = {str(block.get("type", "")) for block in content if isinstance(block, dict)}
    return bool(block_types - {"tool_result"})


def event_from_record(record: Dict[str, Any], project_hint: str) -> Optional[Event]:
    if record.get("type") != "assistant":
        return None
    message = record.get("message") or {}
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    parsed = parse_timestamp(record.get("timestamp"))
    if not parsed:
        return None
    session = str(record.get("sessionId") or "missing-session")
    # The one response-identity rule (audit_common.response_key).
    message_id = audit_common.response_key(record)[1]
    if not message_id:
        message_id = "%s:%s:%s" % (session, parsed[1], stable_hash(usage))
    key = "%s:%s" % (session, message_id)
    sidechain = bool(record.get("isSidechain"))
    agent = str(record.get("agentId") or record.get("attributionAgent") or "sidechain")
    lane = "%s:%s" % (session, agent if sidechain else "main")
    cache_creation = usage.get("cache_creation") or {}
    split_5m = numeric(cache_creation.get("ephemeral_5m_input_tokens"))
    split_1h = numeric(cache_creation.get("ephemeral_1h_input_tokens"))
    aggregate = numeric(usage.get("cache_creation_input_tokens"))
    unknown = max(0, aggregate - split_5m - split_1h)
    cwd = record.get("cwd") or project_hint
    return Event(
        key=key,
        timestamp=parsed[0],
        timestamp_iso=parsed[1],
        session=session,
        lane=lane,
        project=stable_hash(cwd),
        model=str(message.get("model") or "unknown"),
        version=str(record.get("version") or "unknown"),
        branch=stable_hash(record.get("gitBranch") or "unknown"),
        input_tokens=numeric(usage.get("input_tokens")),
        output_tokens=numeric(usage.get("output_tokens")),
        cache_read=numeric(usage.get("cache_read_input_tokens")),
        cache_write_5m=split_5m,
        cache_write_1h=split_1h,
        cache_write_unknown=unknown,
        api_error=bool(record.get("isApiErrorMessage") or record.get("apiErrorStatus") or record.get("error")),
    )


def read_history(
    root: Path, since: Optional[float], until: Optional[float],
    cwd_filter: Optional["CwdFilter"] = None,
) -> Tuple[List[Event], List[HumanPrompt], Dict[str, Any]]:
    cwd_filter = cwd_filter if cwd_filter is not None else CwdFilter()
    projects = root / "projects"
    paths = sorted(projects.rglob("*.jsonl")) if projects.exists() else []
    events: Dict[str, Event] = {}
    prompts: Dict[str, HumanPrompt] = {}
    stats: Dict[str, Any] = {
        "files": len(paths), "rows": 0, "json_errors": 0, "timestamp_errors": 0,
        "assistant_rows_with_usage": 0, "duplicate_assistant_rows": 0,
        "human_prompt_rows": 0, "files_unreadable": 0, "filtered_rows": 0,
        "stats_cache_present": (root / "stats-cache.json").exists(),
    }
    for path in paths:
        project_hint = str(path.parent)
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            stats["files_unreadable"] += 1
            continue
        with handle:
            for line_no, line in enumerate(handle, 1):
                stats["rows"] += 1
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    stats["json_errors"] += 1
                    continue
                if not cwd_filter.keep(record.get("cwd"), project_hint):
                    stats["filtered_rows"] += 1
                    continue
                if record.get("type") == "assistant" and isinstance((record.get("message") or {}).get("usage"), dict):
                    stats["assistant_rows_with_usage"] += 1
                    event = event_from_record(record, project_hint)
                    if not event:
                        stats["timestamp_errors"] += 1
                        continue
                    if not within(event.timestamp, since, until):
                        continue
                    prior = events.get(event.key)
                    if prior:
                        stats["duplicate_assistant_rows"] += 1
                        earliest_ts = min(prior.timestamp, event.timestamp)
                        earliest_iso = prior.timestamp_iso if prior.timestamp <= event.timestamp else event.timestamp_iso
                        chosen = event if event.usage_score > prior.usage_score else prior
                        chosen.timestamp = earliest_ts
                        chosen.timestamp_iso = earliest_iso
                        chosen.duplicate_rows = prior.duplicate_rows + 1
                        events[event.key] = chosen
                    else:
                        events[event.key] = event
                elif is_human_prompt(record):
                    parsed = parse_timestamp(record.get("timestamp"))
                    if not parsed:
                        stats["timestamp_errors"] += 1
                        continue
                    if not within(parsed[0], since, until):
                        continue
                    session = str(record.get("sessionId") or "missing-session")
                    prompt_id = record.get("promptId") or record.get("uuid") or "%s:%s:%s" % (path.name, line_no, parsed[1])
                    key = "%s:%s" % (session, prompt_id)
                    prompts[key] = HumanPrompt(key, parsed[0], session, stable_hash(record.get("cwd") or project_hint))
                    stats["human_prompt_rows"] += 1
    return sorted(events.values(), key=lambda e: (e.timestamp, e.key)), sorted(prompts.values(), key=lambda p: (p.timestamp, p.key)), stats


def invalidation_causes(previous: Event, current: Event) -> Tuple[str, ...]:
    causes: List[str] = []
    if previous.model != current.model:
        causes.append("model changed")
    if previous.version != current.version:
        causes.append("Claude Code version changed")
    if previous.project != current.project:
        causes.append("project/directory changed")
    if previous.branch != current.branch:
        causes.append("branch changed")
    if previous.cacheable >= 2_000 and current.cacheable < previous.cacheable * 0.70:
        causes.append("context shrank (possible compact/reset)")
    return tuple(causes)


def infer_ttls(lane_events: List[Event]) -> None:
    explicit: List[Optional[str]] = []
    for event in lane_events:
        if event.cache_write_1h > event.cache_write_5m:
            explicit.append("1h")
        elif event.cache_write_5m > 0:
            explicit.append("5m")
        else:
            explicit.append(None)
    last: Optional[str] = None
    for index, value in enumerate(explicit):
        if value:
            last = value
        lane_events[index].inferred_ttl = last
    following: Optional[str] = None
    for index in range(len(lane_events) - 1, -1, -1):
        if explicit[index]:
            following = explicit[index]
        if lane_events[index].inferred_ttl is None:
            lane_events[index].inferred_ttl = following


def make_gaps(events: Sequence[Event]) -> Tuple[List[Gap], Dict[str, List[Event]]]:
    lanes: Dict[str, List[Event]] = collections.defaultdict(list)
    for event in events:
        lanes[event.lane].append(event)
    gaps: List[Gap] = []
    for lane_events in lanes.values():
        lane_events.sort(key=lambda event: (event.timestamp, event.key))
        infer_ttls(lane_events)
        for previous, current in zip(lane_events, lane_events[1:]):
            seconds = current.timestamp - previous.timestamp
            if seconds < 0:
                continue
            causes = invalidation_causes(previous, current)
            gaps.append(Gap(seconds, previous, current, min(previous.cacheable, current.cacheable), causes))
    gaps.sort(key=lambda gap: gap.current.timestamp)
    return gaps, dict(lanes)


def gap_bin(seconds: float) -> int:
    if seconds < 60:
        return 0
    if seconds <= 300:
        return 1
    if seconds <= 900:
        return 2
    if seconds <= 1800:
        return 3
    if seconds <= 3600:
        return 4
    return 5


def gap_histogram(gaps: Sequence[Gap]) -> Dict[str, List[int]]:
    counts = [0] * len(GAP_LABELS)
    tokens = [0] * len(GAP_LABELS)
    for gap in gaps:
        index = gap_bin(gap.seconds)
        counts[index] += 1
        tokens[index] += gap.structural_tokens
    return {"labels": list(GAP_LABELS), "counts": counts, "structural_tokens": tokens}


def human_gap_histogram(prompts: Sequence[HumanPrompt]) -> Dict[str, Any]:
    sessions: Dict[str, List[HumanPrompt]] = collections.defaultdict(list)
    for prompt in prompts:
        sessions[prompt.session].append(prompt)
    values: List[float] = []
    counts = [0] * len(GAP_LABELS)
    for session_prompts in sessions.values():
        session_prompts.sort(key=lambda prompt: prompt.timestamp)
        for previous, current in zip(session_prompts, session_prompts[1:]):
            seconds = current.timestamp - previous.timestamp
            if seconds >= 0:
                values.append(seconds)
                counts[gap_bin(seconds)] += 1
    return {
        "labels": list(GAP_LABELS), "counts": counts, "total": len(values),
        "median_seconds": statistics.median(values) if values else None,
    }


def nearest_quantile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


def agent_activity_metrics(
    gaps: Sequence[Gap], lanes: Dict[str, List[Event]], prompts: Sequence[HumanPrompt]
) -> Dict[str, Any]:
    """Separate direct-human returns from uninterrupted API/tool-loop activity."""
    prompt_times: Dict[str, List[float]] = collections.defaultdict(list)
    for prompt in prompts:
        prompt_times[prompt.session].append(prompt.timestamp)
    for times in prompt_times.values():
        times.sort()

    def direct_prompt_between(previous: Event, current: Event) -> bool:
        times = prompt_times.get(current.session, [])
        index = bisect.bisect_right(times, previous.timestamp)
        return index < len(times) and times[index] <= current.timestamp

    actor_counts = {"no_direct_human": [0] * len(GAP_LABELS), "direct_human": [0] * len(GAP_LABELS)}
    actor_tokens = {"no_direct_human": [0] * len(GAP_LABELS), "direct_human": [0] * len(GAP_LABELS)}
    short_labels = ["<1 sec", "1–5 sec", "5–15 sec", "15–30 sec", "30–60 sec"]
    short_counts = {"no_direct_human": [0] * len(short_labels), "direct_human": [0] * len(short_labels)}
    short_tokens = {"no_direct_human": [0] * len(short_labels), "direct_human": [0] * len(short_labels)}
    eligible = collections.Counter()

    for gap in gaps:
        actor = "direct_human" if direct_prompt_between(gap.previous, gap.current) else "no_direct_human"
        index = gap_bin(gap.seconds)
        actor_counts[actor][index] += 1
        actor_tokens[actor][index] += gap.structural_tokens
        if gap.seconds < 60:
            short_index = 0 if gap.seconds < 1 else 1 if gap.seconds < 5 else 2 if gap.seconds < 15 else 3 if gap.seconds < 30 else 4
            short_counts[actor][short_index] += 1
            short_tokens[actor][short_index] += gap.structural_tokens
        if gap.ttl_eligible:
            eligible.update({actor + "_gaps": 1, actor + "_tokens": gap.structural_tokens})

    bursts: List[List[Event]] = []
    for lane_events in lanes.values():
        if not lane_events:
            continue
        lane_events = sorted(lane_events, key=lambda event: (event.timestamp, event.key))
        chain = [lane_events[0]]
        for previous, current in zip(lane_events, lane_events[1:]):
            uninterrupted = current.timestamp - previous.timestamp < 60 and not direct_prompt_between(previous, current)
            if uninterrupted:
                chain.append(current)
            else:
                if len(chain) >= 2:
                    bursts.append(chain)
                chain = [current]
        if len(chain) >= 2:
            bursts.append(chain)

    durations = [chain[-1].timestamp - chain[0].timestamp for chain in bursts]
    calls = [len(chain) for chain in bursts]
    subminute_total = sum(short_counts["no_direct_human"]) + sum(short_counts["direct_human"])
    subminute_tokens = sum(short_tokens["no_direct_human"]) + sum(short_tokens["direct_human"])
    eligible_total_tokens = eligible["no_direct_human_tokens"] + eligible["direct_human_tokens"]
    return {
        "gap_actor_histogram": {
            "labels": list(GAP_LABELS), "counts": actor_counts, "structural_tokens": actor_tokens,
        },
        "subminute": {
            "labels": short_labels, "counts": short_counts, "structural_tokens": short_tokens,
            "total_gaps": subminute_total, "total_structural_tokens": subminute_tokens,
            "no_direct_human_fraction": sum(short_counts["no_direct_human"]) / subminute_total if subminute_total else None,
            "no_direct_human_token_fraction": sum(short_tokens["no_direct_human"]) / subminute_tokens if subminute_tokens else None,
        },
        "ttl_eligible_by_actor": {
            "direct_human_gaps": eligible["direct_human_gaps"],
            "direct_human_tokens": eligible["direct_human_tokens"],
            "no_direct_human_gaps": eligible["no_direct_human_gaps"],
            "no_direct_human_tokens": eligible["no_direct_human_tokens"],
            "direct_human_token_fraction": eligible["direct_human_tokens"] / eligible_total_tokens if eligible_total_tokens else None,
        },
        "autonomous_bursts": {
            "bursts": len(bursts), "api_responses": sum(calls),
            "median_api_responses": statistics.median(calls) if calls else None,
            "p90_api_responses": nearest_quantile(calls, 0.90),
            "median_duration_seconds": statistics.median(durations) if durations else None,
            "p90_duration_seconds": nearest_quantile(durations, 0.90),
            "p95_duration_seconds": nearest_quantile(durations, 0.95),
            "maximum_duration_seconds": max(durations) if durations else None,
            "at_least_5_minutes": sum(duration >= 300 for duration in durations),
            "at_least_15_minutes": sum(duration >= 900 for duration in durations),
            "at_least_30_minutes": sum(duration >= 1800 for duration in durations),
        },
    }


def load_pricing(path: Path, table_id: Optional[str]) -> Dict[str, Any]:
    """THE price table, loaded by audit_common so every analysis agrees.

    This used to be a second, independent loader; orientation_profile.py had a
    third that merged a built-in table over the file. That is how two scripts
    came to report 15 % and 100 % coverage for the same --pricing-file.
    One loader now, one table, one matching rule.
    """
    try:
        return audit_common.load_prices(str(path), table_id)
    except audit_common.PriceTableError as error:
        raise ValueError(str(error)) from error


def rate_for(table: Dict[str, Any], model: str) -> Optional[Dict[str, float]]:
    rates = audit_common.price_for(model, table)
    return None if rates is None else {"input": rates[0], "output": rates[1]}


def priced_cost(event: Event, table: Dict[str, Any], read_tokens: float, write_tokens: float, policy: str) -> Optional[float]:
    rates = rate_for(table, event.model)
    if not rates:
        return None
    input_equivalent = event.input_tokens + READ_MULTIPLIER * read_tokens + WRITE_MULTIPLIERS[policy] * write_tokens
    return (input_equivalent * rates["input"] + event.output_tokens * rates["output"]) / 1_000_000.0


def actual_cost(event: Event, table: Dict[str, Any], unknown_write_ttl: str) -> Optional[float]:
    rates = rate_for(table, event.model)
    if not rates:
        return None
    equivalent = (
        event.input_tokens + READ_MULTIPLIER * event.cache_read
        + WRITE_MULTIPLIERS["5m"] * event.cache_write_5m
        + WRITE_MULTIPLIERS["1h"] * event.cache_write_1h
        + WRITE_MULTIPLIERS[unknown_write_ttl] * event.cache_write_unknown
    )
    return (equivalent * rates["input"] + event.output_tokens * rates["output"]) / 1_000_000.0


def simulate(events: Sequence[Event], lanes: Dict[str, List[Event]], table: Dict[str, Any], reuse: float) -> Dict[str, Any]:
    totals: Dict[str, Dict[str, float]] = {
        "5m": {"read": 0.0, "write": 0.0, "input_equivalent": 0.0, "usd": 0.0},
        "1h": {"read": 0.0, "write": 0.0, "input_equivalent": 0.0, "usd": 0.0},
    }
    covered_events = 0
    covered_cacheable = 0
    total_cacheable = sum(event.cacheable for event in events)
    for lane_events in lanes.values():
        previous: Optional[Event] = None
        for event in lane_events:
            priced = rate_for(table, event.model) is not None
            if priced:
                covered_events += 1
                covered_cacheable += event.cacheable
            for policy, ttl in (("5m", 300), ("1h", 3600)):
                # Preserve observed behavior where TTL choice cannot make a
                # difference (first request, <=5m, >60m, or a detectable
                # invalidation). Apply the sensitivity assumption only to the
                # 5–60 minute same-lane opportunity that distinguishes 1h.
                read = min(float(event.cacheable), float(event.cache_read))
                if previous is not None:
                    seconds = event.timestamp - previous.timestamp
                    causes = invalidation_causes(previous, event)
                    if 300 < seconds <= 3600 and not causes:
                        structural = min(previous.cacheable, event.cacheable)
                        read = reuse * structural if policy == "1h" else 0.0
                read = min(float(event.cacheable), max(0.0, read))
                write = max(0.0, event.cacheable - read)
                equivalent = event.input_tokens + READ_MULTIPLIER * read + WRITE_MULTIPLIERS[policy] * write
                totals[policy]["read"] += read
                totals[policy]["write"] += write
                totals[policy]["input_equivalent"] += equivalent
                cost = priced_cost(event, table, read, write, policy)
                if cost is not None:
                    totals[policy]["usd"] += cost
            previous = event
    usd_delta = totals["5m"]["usd"] - totals["1h"]["usd"]
    eq_delta = totals["5m"]["input_equivalent"] - totals["1h"]["input_equivalent"]
    return {
        "reuse_fraction": reuse,
        "5m": totals["5m"], "1h": totals["1h"],
        "usd_savings_1h": usd_delta,
        "usd_savings_percent_of_5m": (100 * usd_delta / totals["5m"]["usd"]) if totals["5m"]["usd"] else None,
        "input_equivalent_savings_1h": eq_delta,
        "input_equivalent_savings_percent_of_5m": (100 * eq_delta / totals["5m"]["input_equivalent"]) if totals["5m"]["input_equivalent"] else None,
        "price_coverage_events": covered_events / len(events) if events else 0.0,
        "price_coverage_cacheable_tokens": covered_cacheable / total_cacheable if total_cacheable else 0.0,
    }


def actual_metrics(events: Sequence[Event], gaps: Sequence[Gap], table: Dict[str, Any], unknown_write_ttl: str) -> Dict[str, Any]:
    totals = collections.Counter()
    costs = 0.0
    priced_events = 0
    model_counts = collections.Counter()
    coverage = Coverage()
    for event in events:
        coverage.add(event.model,
                     event.input_tokens + event.output_tokens + event.cache_read
                     + event.cache_write,
                     rate_for(table, event.model) is not None)
        totals.update({
            "input": event.input_tokens, "output": event.output_tokens, "cache_read": event.cache_read,
            "cache_write_5m": event.cache_write_5m, "cache_write_1h": event.cache_write_1h,
            "cache_write_unknown": event.cache_write_unknown,
        })
        model_counts[event.model] += 1
        cost = actual_cost(event, table, unknown_write_ttl)
        if cost is not None:
            costs += cost
            priced_events += 1
    eligible_1h = [gap for gap in gaps if gap.ttl_eligible and gap.current.inferred_ttl == "1h"]
    eligible_reads = sum(min(gap.current.cache_read, gap.structural_tokens) for gap in eligible_1h)
    eligible_creation = sum(gap.current.cache_write for gap in eligible_1h)
    creation_aligned_with_growth = sum(
        min(gap.current.cache_write, max(0, gap.current.cacheable - gap.previous.cacheable))
        for gap in eligible_1h
    )
    one_hour_writes = sum(event.cache_write_1h for event in events if event.inferred_ttl == "1h")
    transition_denominator = eligible_reads + eligible_creation
    return {
        "tokens": dict(totals), "observed_usd": costs, "priced_events": priced_events,
        "price_coverage_events": priced_events / len(events) if events else 0.0,
        # THE shared coverage number: priced tokens / priceable tokens, using
        # audit_common's denominator so this equals what every other analysis
        # in the skill prints for the same input. `<synthetic>` is not a model
        # and is out of the denominator.
        "price_coverage": round(coverage.fraction, 3),
        "price_coverage_label": coverage.label(),
        "price_coverage_caveat": coverage.caveat() or None,
        "model_event_counts": dict(model_counts),
        "one_hour_eligible_gaps": len(eligible_1h),
        "one_hour_eligible_reads": eligible_reads,
        "one_hour_eligible_structural_tokens": sum(gap.structural_tokens for gap in eligible_1h),
        "eligible_return_cache_creation": eligible_creation,
        "eligible_return_creation_aligned_with_context_growth": creation_aligned_with_growth,
        "eligible_return_creation_not_explained_by_net_growth": max(0, eligible_creation - creation_aligned_with_growth),
        "eligible_return_creation_growth_fraction": creation_aligned_with_growth / eligible_creation if eligible_creation else None,
        "one_hour_writes": one_hour_writes,
        "policy_reuse_ratio": eligible_reads / one_hour_writes if one_hour_writes else None,
        "observed_prefix_survival": eligible_reads / sum(gap.structural_tokens for gap in eligible_1h) if eligible_1h and sum(gap.structural_tokens for gap in eligible_1h) else None,
        "transition_reuse_fraction": eligible_reads / transition_denominator if transition_denominator else None,
    }


def funnel(gaps: Sequence[Gap]) -> List[Dict[str, Any]]:
    stages: List[Tuple[str, Iterable[Gap]]] = []
    all_gaps = list(gaps)
    over_five = [gap for gap in all_gaps if gap.seconds > 300]
    within_hour = [gap for gap in over_five if gap.seconds <= 3600]
    same_epoch = [gap for gap in within_hour if gap.same_epoch]
    reusable = [gap for gap in same_epoch if gap.structural_tokens > 0]
    stages.extend([
        ("All pauses", all_gaps),
        ("Longer than 5 minutes", over_five),
        ("…and up to 1 hour", within_hour),
        ("…and nothing else reset the cache", same_epoch),
        ("…and had context to reuse", reusable),
    ])
    return [{"stage": name, "gaps": len(values), "structural_tokens": sum(g.structural_tokens for g in values)} for name, values in stages]


def workload_segments(lanes: Dict[str, List[Event]]) -> Dict[str, Dict[str, int]]:
    result: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for lane_events in lanes.values():
        lane_tokens = sum(event.cacheable for event in lane_events)
        if len(lane_events) == 1:
            label = "one-shot"
        else:
            local_gaps: List[Gap] = []
            for previous, current in zip(lane_events, lane_events[1:]):
                seconds = current.timestamp - previous.timestamp
                if seconds >= 0:
                    local_gaps.append(Gap(seconds, previous, current, min(previous.cacheable, current.cacheable), invalidation_causes(previous, current)))
            weighted_total = sum(gap.structural_tokens for gap in local_gaps) or 1
            eligible_share = sum(gap.structural_tokens for gap in local_gaps if gap.ttl_eligible) / weighted_total
            short_share = sum(gap.structural_tokens for gap in local_gaps if gap.seconds <= 300) / weighted_total
            long_share = sum(gap.structural_tokens for gap in local_gaps if gap.seconds > 3600) / weighted_total
            if eligible_share >= 0.15:
                label = "1-hour candidate"
            elif short_share >= 0.80:
                label = "continuous/5-minute fit"
            elif long_share >= 0.50:
                label = "mostly beyond 1 hour"
            else:
                label = "mixed"
        result[label].update({"lanes": 1, "cacheable_tokens": lane_tokens})
    return {label: dict(values) for label, values in sorted(result.items())}


def recommendation_confidence(
    scenarios: Sequence[Dict[str, Any]], quality: Dict[str, Any], funnel_data: Sequence[Dict[str, Any]], actual: Dict[str, Any]
) -> Dict[str, Any]:
    reasons: List[str] = []
    level = "high"
    if quality.get("unique_api_events", 0) < 100 or funnel_data[-1]["gaps"] < 10:
        level = "low"
        reasons.append("few responses, or few returns after a 5–60 minute pause")
    signs = {scenario["input_equivalent_savings_1h"] >= 0 for scenario in scenarios}
    if len(signs) > 1:
        if level == "high":
            level = "medium"
        reasons.append("the answer flips with the share of context still reusable on return")
    coverage = min((scenario["price_coverage_cacheable_tokens"] for scenario in scenarios), default=0.0)
    if coverage < 0.80:
        if level == "high":
            level = "medium"
        reasons.append("under 80% of cached tokens have a list price")
    sign_conflict = any(
        (scenario["usd_savings_1h"] >= 0) != (scenario["input_equivalent_savings_1h"] >= 0)
        for scenario in scenarios
    )
    if sign_conflict:
        if level == "high":
            level = "medium"
        reasons.append("missing prices make the dollar and token comparisons disagree")
    if actual.get("one_hour_eligible_gaps", 0) < 20:
        if level == "high":
            level = "medium"
        reasons.append("few measured returns in conversations already using the 1-hour cache")
    if not reasons:
        reasons.append("plenty of data, the same answer at every tested share, and prices for most tokens")
    return {"level": level, "reasons": reasons}


def fmt_int(value: float) -> str:
    return f"{int(round(value)):,}"


def fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def fmt_pct(value: Optional[float], already_percent: bool = False) -> str:
    if value is None:
        return "n/a"
    number = value if already_percent else value * 100
    return f"{number:.1f}%"


def duration_text(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{seconds:.0f} seconds"
    return f"{seconds / 60:.1f} minutes"


def savings_outcome(value: float) -> str:
    if value >= 0:
        return f"would save {fmt_money(value)}"
    return f"would cost {fmt_money(abs(value))} more"


REC_INCONCLUSIVE = "Inconclusive"
REC_EITHER = "Either lifetime is fine"
REC_ONE_HOUR = "Use the 1-hour cache"
REC_FIVE_MINUTES = "Keep the 5-minute cache"
REC_SELECTIVE = "Use the 1-hour cache only for some conversations"
RESET_ADVICE = "Changing model or tools resets either cache."


def choose_recommendation(scenarios: Sequence[Dict[str, Any]], gap_data: Dict[str, List[int]], funnel_data: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    if not scenarios:
        return REC_INCONCLUSIVE, "There were no what-if settings to compare."
    deltas = [scenario["input_equivalent_savings_1h"] for scenario in scenarios]
    total_weight = sum(gap_data["structural_tokens"])
    eligible_weight = funnel_data[-1]["structural_tokens"] if funnel_data else 0
    eligible_share = eligible_weight / total_weight if total_weight else 0.0
    if max(abs(delta) for delta in deltas) < max(10_000, 0.002 * max(scenarios[0]["5m"]["input_equivalent"], 1)):
        return REC_EITHER, "The two cost about the same on this history."
    if all(delta > 0 for delta in deltas) and eligible_share >= 0.05:
        return REC_ONE_HOUR, "It is cheaper at every tested share of reusable context, and enough context waits 5–60 minutes."
    if all(delta < 0 for delta in deltas):
        return REC_FIVE_MINUTES, "The 1-hour cache costs more to write, and that is never earned back here."
    return REC_SELECTIVE, "It pays off for large conversations you resume within the hour, when most context is still reusable."


def advice(metrics: Dict[str, Any]) -> List[str]:
    """What to do, following the recommendation: never advice the result does not carry."""
    label = metrics["recommendation"]["label"]
    segments = metrics["workload_segments"]
    total = sum(data["lanes"] for data in segments.values())
    candidates = segments.get("1-hour candidate", {}).get("lanes", 0)
    which = "%s of %s" % (fmt_int(candidates), fmt_int(total))
    if label == REC_FIVE_MINUTES:
        lines = ["Keep 5 minutes: here the 1-hour cache costs more.",
                 "Revisit if you start leaving large conversations idle for 5–60 minutes."]
    elif label == REC_ONE_HOUR:
        lines = [f"Use 1 hour: {which} conversations often resume after 5–60 minutes."]
    elif label == REC_SELECTIVE and candidates:
        lines = [f"Use 1 hour where conversations often resume after 5–60 minutes: {which} here.",
                 "Keep 5 minutes for the rest."]
    elif label == REC_SELECTIVE:
        lines = ["Use 1 hour only for large conversations resumed after 5–60 minutes.",
                 "None does that often here, so keep 5 minutes for now."]
    elif label == REC_EITHER:
        lines = ["Either setting is fine: the cost difference here is small."]
    else:
        lines = ["Not enough history to choose; run again on a longer period."]
    return lines + [RESET_ADVICE]


AGENT_LABEL = "Agent working on its own"
YOU_LABEL = "Waiting for you"
# Display names for workload_segments() keys (the keys stay as metrics.json writes them).
SEGMENT_LABELS = {
    "1-hour candidate": "Often resumes after 5–60 min",
    "continuous/5-minute fit": "Steady: pauses under 5 min",
    "mostly beyond 1 hour": "Mostly resumes after 1 hour+",
    "mixed": "Mixed",
    "one-shot": "Single response",
}
WINDOW_BINS = (2, 3, 4)          # the 5–15, 15–30 and 30–60 minute bins of GAP_LABELS


def central_scenario(metrics: Dict[str, Any]) -> Dict[str, Any]:
    return min(metrics["scenarios"], key=lambda item: abs(item["reuse_fraction"] - 0.8))


def savings_line(scenarios: Sequence[Dict[str, Any]]) -> Optional[Tuple[float, float]]:
    """(intercept, slope) of the 1-hour dollar saving against the reusable share.

    simulate() scales only the 5–60 minute reads by that share, so the saving
    is exactly linear in it: two scenarios fix the whole line. None with fewer
    than two distinct shares.
    """
    points = sorted({item["reuse_fraction"]: item["usd_savings_1h"] for item in scenarios}.items())
    if len(points) < 2:
        return None
    (x0, y0), (x1, y1) = points[0], points[-1]
    slope = (y1 - y0) / (x1 - x0)
    return y0 - slope * x0, slope


def break_even_share(scenarios: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Reusable share at which the 1-hour cache costs the same as 5 minutes.

    May fall outside 0–1: below 0 means it pays off at any share, above 1 that
    it never does. None when the line is unknown or flat.
    """
    line = savings_line(scenarios)
    if line is None or line[1] == 0:
        return None
    return -line[0] / line[1]


def break_even_sentence(scenarios: Sequence[Dict[str, Any]]) -> str:
    share = break_even_share(scenarios)
    line = savings_line(scenarios)
    if line is None:
        return "Run two or more --reuse-scenarios values to see where the 1-hour cache breaks even."
    if share is None:
        return ("The 1-hour cache saves money at any share of reusable context." if line[0] > 0
                else "The 1-hour cache never saves money here." if line[0] < 0
                else "The two lifetimes cost the same at every share.")
    if share <= 0:
        return "The 1-hour cache saves money here at any share of reusable context."
    if share >= 1:
        return "The 1-hour cache costs more here, even if all the context is still reusable."
    return ("The 1-hour cache pays off if more than %s of the context is still reusable "
            "when a conversation resumes." % pct0(share))


def counted(number: int, one: str, many: str) -> str:
    return "%s %s" % (fmt_int(number), one if number == 1 else many)


def pct0(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    number = 100 * value
    return "%.0f%%" % number if number >= 1 or number == 0 else "<1%"


def window_share(hist: Dict[str, List[int]]) -> float:
    total = sum(hist["structural_tokens"])
    return sum(hist["structural_tokens"][i] for i in WINDOW_BINS) / total if total else 0.0


def markdown_report(metrics: Dict[str, Any]) -> str:
    rec = metrics["recommendation"]
    quality = metrics["quality"]
    actual = metrics["actual"]
    hist = metrics["api_gap_histogram"]
    human = metrics["human_prompt_gaps"]
    activity = metrics["agent_activity"]
    scenarios = metrics["scenarios"]
    central = central_scenario(metrics)
    tokens = actual["tokens"]
    who = activity["gap_actor_histogram"]
    subminute = activity["subminute"]
    bursts = activity["autonomous_bursts"]
    eligible_actor = activity["ttl_eligible_by_actor"]
    lines = [
        "# Claude Code cache: 5 minutes or 1 hour?", "",
        f"**{rec['label']}.** {rec['reason']}", "",
        f"Confidence: {metrics['confidence']['level']} ({'; '.join(metrics['confidence']['reasons'])}).", "",
        f"{break_even_sentence(scenarios)} If {central['reuse_fraction']:.0%} of it is, the 1-hour cache "
        f"{savings_outcome(central['usd_savings_1h'])} ({fmt_pct(central['usd_savings_percent_of_5m'], True)} of the 5-minute cost).",
        f"Estimates at list API rates, not a bill; {fmt_pct(central['price_coverage_cacheable_tokens'])} of cached tokens have a price.", "",
        "## The history", "",
        f"- {fmt_int(quality['unique_api_events'])} responses in {fmt_int(quality['sessions'])} sessions, "
        f"spread over {fmt_int(quality['lanes'])} conversations and subagents. Each one keeps its own cache.",
        f"- {fmt_int(quality['duplicate_assistant_rows'])} duplicate rows removed (of {fmt_int(quality['assistant_rows_with_usage'])} rows with token counts).",
        f"- {fmt_int(quality['human_prompts'])} prompts typed by you; median time between them: {duration_text(human['median_seconds'])}.",
        f"- Cache tokens: {compact_number(tokens.get('cache_read', 0))} read, {compact_number(tokens.get('cache_write_5m', 0))} written for 5 minutes, "
        f"{compact_number(tokens.get('cache_write_1h', 0))} written for 1 hour.",
        "", "## Pauses between responses", "",
        "A pause is the time between two responses in one conversation or subagent. "
        "Tool calls count too, so a pause is not the same as you being away. "
        "The cache lifetime only matters for pauses of 5–60 minutes.", "",
        f"| Pause | Pauses | {AGENT_LABEL} | {YOU_LABEL} | Reusable context (tokens) |", "|---|---:|---:|---:|---:|",
    ]
    for index, label in enumerate(hist["labels"]):
        lines.append(f"| {label} | {fmt_int(hist['counts'][index])} | {fmt_int(who['counts']['no_direct_human'][index])} | "
                     f"{fmt_int(who['counts']['direct_human'][index])} | {compact_number(hist['structural_tokens'][index])} |")
    lines.extend([
        "", "## How much ran without you", "",
        f"- {fmt_pct(subminute['no_direct_human_fraction'])} of pauses under a minute had no prompt from you in between: "
        "the agent was working on its own.",
        f"- The agent ran unattended {fmt_int(bursts['bursts'])} times. Half of these runs took under {duration_text(bursts['median_duration_seconds'])}, "
        f"9 in 10 under {duration_text(bursts['p90_duration_seconds'])}; the longest took {duration_text(bursts['maximum_duration_seconds'])}.",
        f"- Of the reusable context at returns after a 5–60 minute pause, {fmt_pct(eligible_actor['direct_human_token_fraction'])} "
        "came back because you sent a prompt"
        + ("; the rest resumed without you, for example after a long-running tool." if (eligible_actor['direct_human_token_fraction'] or 0) < 1 else "."),
        "- “No prompt from you” is a strong sign of the agent working alone, not proof: automated follow-ups look the same.",
        "", "## Which pauses could the 1-hour cache have helped?", "",
        "| Step | Pauses | Reusable context (tokens) |", "|---|---:|---:|",
    ])
    for row in metrics["eligibility_funnel"]:
        lines.append(f"| {row['stage']} | {fmt_int(row['gaps'])} | {compact_number(row['structural_tokens'])} |")
    lines.extend(["", "## Conversation types", "",
                  "Each conversation or subagent is sorted by when its reusable context came back.", "",
                  "| Type | Conversations and subagents | Cached tokens |", "|---|---:|---:|"])
    for name, data in metrics["workload_segments"].items():
        lines.append(f"| {SEGMENT_LABELS.get(name, name)} | {fmt_int(data['lanes'])} | {compact_number(data['cacheable_tokens'])} |")
    lines.extend(["", "## Savings by share of context still reusable on return", "",
                  "A positive saving means the 1-hour cache is cheaper. The share is a what-if setting, not a measurement.", "",
                  "| Still reusable | 5-minute cost | 1-hour cost | 1-hour saving | Saving as % of input cost |",
                  "|---:|---:|---:|---:|---:|"])
    for scenario in scenarios:
        lines.append(
            f"| {scenario['reuse_fraction']:.0%} | {fmt_money(scenario['5m']['usd'])} | {fmt_money(scenario['1h']['usd'])} | "
            f"{fmt_money(scenario['usd_savings_1h'])} ({fmt_pct(scenario['usd_savings_percent_of_5m'], True)}) | "
            f"{fmt_pct(scenario['input_equivalent_savings_percent_of_5m'], True)} |"
        )
    lines.extend(["", break_even_sentence(scenarios)])
    lines.extend([
        "", "## Measured in conversations already using the 1-hour cache", "",
        f"- Returns after a 5–60 minute pause: {fmt_int(actual['one_hour_eligible_gaps'])}.",
        f"- Share of the reusable context read back from the cache on those returns: {fmt_pct(actual.get('observed_prefix_survival'))}.",
        f"- Cache reads on those returns, divided by all 1-hour cache writes: {fmt_pct(actual.get('policy_reuse_ratio'))}. "
        f"On this measure the 1-hour cache pays for itself above {fmt_pct(POLICY_BREAK_EVEN)}.",
        f"- Per return, reads divided by reads plus new writes: {fmt_pct(actual.get('transition_reuse_fraction'))} "
        f"(break-even {fmt_pct(TRANSITION_BREAK_EVEN)}). This answers a narrower question; do not use it in place of the line above.",
        f"- New cache writes on those returns: {compact_number(actual.get('eligible_return_cache_creation', 0))} tokens. "
        f"{fmt_pct(actual.get('eligible_return_creation_growth_fraction'))} of them match the conversation growing "
        "(new messages and tool results), not old context written again.",
        "", "## What else reset the cache", "",
    ])
    if metrics["invalidation_signals"]:
        for name, data in metrics["invalidation_signals"].items():
            lines.append(f"- {name}: {counted(data['gaps'], 'pause', 'pauses')}, {compact_number(data['structural_tokens'])} tokens of reusable context.")
    else:
        lines.append("- Nothing visible in the selected history.")
    lines.extend([
        "", "Changes to MCP tools and server-side changes do not show in local logs, so this list can miss resets.",
        "", "## What to do", "",
    ])
    lines.extend("- " + line for line in advice(metrics))
    lines.extend([
        "- Load MCP servers before the context grows: a changed tool set can reset the cache.",
        "- `--resume` restores the conversation, not an expired cache. After an hour away, neither setting helps.",
        "- Your own idle time overstates the pauses: tool calls keep the cache warm. Test paid keep-alive prompts separately.",
        "", "## Data and limits", "",
        f"- Unreadable input: {fmt_int(quality['json_errors'])} malformed rows, {fmt_int(quality['timestamp_errors'])} bad timestamps, "
        f"{fmt_int(quality['files_unreadable'])} unreadable files. Responses that were API errors (kept): {fmt_int(quality.get('api_error_events', 0))}.",
        f"- Rows left out by working-directory filters: {fmt_int(quality.get('filtered_rows', 0))}.",
        f"- Prices: `{metrics['pricing']['table_id']}` ({metrics['pricing'].get('description', 'no description')}). "
        f"Responses with a price: {fmt_pct(actual['price_coverage_events'])}.",
        "- Dollar figures come from that price table, not an invoice. Token counts and timestamps are more reliable than the dollar amounts.",
        "- Two responses in a row are assumed to share the smaller of their two cached contexts, unless something visibly reset the cache. "
        "The real cache keys are not in the logs.",
        "- `stats-cache.json` is only checked for presence; its totals can count the same response twice.",
        "",
    ])
    return "\n".join(lines)


def compact_number(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def tick_money(value: float, step: float) -> str:
    """Axis label: as few decimals as the tick step needs, -$40 / $0 / $1.2K."""
    sign = "-" if value < -1e-12 else ""
    absolute = abs(value)
    if absolute >= 1_000:
        return sign + "$" + compact_number(absolute)
    if absolute < 1e-12:
        return "$0"
    return sign + "$%.*f" % (0 if step >= 1 else 2, absolute)


def _attr(text: str) -> str:
    return html.escape(text, quote=True)


def _hbar(x: float, y: float, width: float, height: float, css: str, round_end: bool = True) -> str:
    """Horizontal bar: square at the baseline, 4px rounded data-end."""
    if width <= 0:
        return ""
    radius = min(4.0, width, height / 2) if round_end else 0.0
    if radius <= 0:
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" class="{css}"/>'
    right = x + width
    return (f'<path class="{css}" d="M{x:.1f},{y:.1f}H{right - radius:.1f}'
            f'Q{right:.1f},{y:.1f} {right:.1f},{y + radius:.1f}V{y + height - radius:.1f}'
            f'Q{right:.1f},{y + height:.1f} {right - radius:.1f},{y + height:.1f}H{x:.1f}Z"/>')


def _hit(x: float, y: float, width: float, height: float, tip: str) -> str:
    """Transparent, focusable hover/focus target bigger than the mark."""
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" class="hit" '
            f'tabindex="0" data-tip="{_attr(tip)}" aria-label="{_attr(tip)}"/>')


def _svg(width: float, height: float, label: str, body: str) -> str:
    return (f'<svg viewBox="0 0 {width:.0f} {height:.0f}" style="max-width:{width * 1.25:.0f}px" '
            f'role="img" aria-label="{_attr(label)}">{body}</svg>')


def pause_chart(metrics: Dict[str, Any]) -> str:
    """Reusable context by pause length, stacked by who the pause was waiting on."""
    hist = metrics["api_gap_histogram"]
    who = metrics["agent_activity"]["gap_actor_histogram"]
    width, row, bar = 480, 34, 18
    left, right, top = 96, 108, 10
    plot = width - left - right
    height = top + row * len(hist["labels"]) + 8
    totals = hist["structural_tokens"]
    peak = max(totals) or 1
    body: List[str] = []
    band_top, band_bottom = top + row * WINDOW_BINS[0], top + row * (WINDOW_BINS[-1] + 1)
    body.append(f'<rect x="0" y="{band_top}" width="{width}" height="{band_bottom - band_top}" class="band"/>')
    for y_line, text in ((band_top, "5 min"), (band_bottom, "1 hour")):
        body.append(f'<line x1="0" x2="{width}" y1="{y_line}" y2="{y_line}" class="rule"/>'
                    f'<text x="{width - 4}" y="{y_line - 4}" text-anchor="end" class="tick">{text}</text>')
    for index, label in enumerate(hist["labels"]):
        y = top + row * index + (row - bar) / 2
        agent = who["structural_tokens"]["no_direct_human"][index]
        you = who["structural_tokens"]["direct_human"][index]
        agent_w = plot * agent / peak
        you_w = plot * you / peak
        gap = 2 if agent_w > 0 and you_w > 0 else 0
        body.append(f'<text x="{left - 8}" y="{y + bar / 2 + 4:.1f}" text-anchor="end" class="cat">{html.escape(label)}</text>')
        body.append(_hbar(left, y, agent_w, bar, "m1", round_end=you_w <= 0))
        body.append(_hbar(left + agent_w + gap, y, max(0.0, you_w - gap), bar, "m2"))
        unit = " tokens" if totals[index] == peak else ""          # name the unit once, on the longest bar
        body.append(f'<text x="{left + agent_w + you_w + 6:.1f}" y="{y + bar / 2 + 4:.1f}" class="val">'
                    f'{compact_number(totals[index])}{unit}</text>')
        tip = (f"{label}: {counted(hist['counts'][index], 'pause', 'pauses')}, {compact_number(totals[index])} tokens of reusable context "
               f"({compact_number(agent)} agent on its own, {compact_number(you)} waiting for you)")
        body.append(_hit(0, top + row * index, width, row, tip))
    return _svg(width, height, "Reusable context by pause length", "".join(body))


def funnel_chart(metrics: Dict[str, Any]) -> str:
    """The eligibility steps as decreasing bars; the last step is the answer."""
    rows = metrics["eligibility_funnel"]
    causes = metrics["invalidation_signals"]
    width, row, bar, top = 440, 44, 16, 4
    plot = width - 70
    height = top + row * len(rows)
    peak = max(item["gaps"] for item in rows) or 1
    body: List[str] = []
    for index, item in enumerate(rows):
        y = top + row * index
        last = index == len(rows) - 1
        bar_w = max(2.0, plot * item["gaps"] / peak) if item["gaps"] else 0.0
        body.append(f'<text x="0" y="{y + 14}" class="cat">{html.escape(item["stage"])}</text>')
        body.append(_hbar(0, y + 20, bar_w, bar, "m1" if last else "md"))
        body.append(f'<text x="{bar_w + 6:.1f}" y="{y + 20 + bar / 2 + 4:.1f}" class="val">{fmt_int(item["gaps"])}</text>')
        tip = f"{counted(item['gaps'], 'pause', 'pauses')}, {compact_number(item['structural_tokens'])} tokens of reusable context"
        if index == 3 and causes:
            tip += ". Reset by: " + ", ".join(f"{name} ({fmt_int(data['gaps'])})" for name, data in causes.items())
        body.append(_hit(0, y, width, row, tip))
    return _svg(width, height, "Which pauses could the 1-hour cache have helped", "".join(body))


def _nice_step(span: float) -> float:
    raw = span / 4 or 1.0
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= multiple * magnitude:
            return multiple * magnitude
    return 10 * magnitude


def savings_chart(metrics: Dict[str, Any]) -> str:
    """1-hour saving against the share of context still reusable, with the zero line."""
    scenarios = metrics["scenarios"]
    line = savings_line(scenarios)
    width, height = 480, 310
    left, right, top, bottom = 58, 20, 30, 54
    plot_w, plot_h = width - left - right, height - top - bottom
    if line is not None:
        ends = [line[0], line[0] + line[1]]
    else:
        ends = [item["usd_savings_1h"] for item in scenarios]
    low, high = min(ends + [0.0]), max(ends + [0.0])
    step = _nice_step(high - low)
    low, high = math.floor(low / step) * step, math.ceil(high / step) * step
    if high == low:
        high = low + step

    def px(share: float) -> float:
        return left + plot_w * share

    def py(usd: float) -> float:
        return top + plot_h * (high - usd) / (high - low)

    body: List[str] = []
    tick = low
    while tick <= high + step / 2:
        css = "zero" if abs(tick) < step / 1000 else "grid"
        body.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{py(tick):.1f}" y2="{py(tick):.1f}" class="{css}"/>'
                    f'<text x="{left - 8}" y="{py(tick) + 4:.1f}" text-anchor="end" class="tick">{tick_money(tick, step)}</text>')
        tick += step
    for share in (0, 0.5, 1.0):
        body.append(f'<text x="{px(share):.1f}" y="{top + plot_h + 18}" text-anchor="middle" class="tick">{share:.0%}</text>')
    body.append(f'<text x="{left + plot_w / 2:.1f}" y="{height - 10}" text-anchor="middle" class="cat">'
                'Share of context still reusable on return</text>')
    body.append('<text x="0" y="16" class="cat">1-hour saving</text>')
    if line is not None:
        body.append(f'<line x1="{px(0):.1f}" y1="{py(line[0]):.1f}" x2="{px(1):.1f}" y2="{py(line[0] + line[1]):.1f}" class="series"/>')
        share = break_even_share(scenarios)
        if share is not None and 0 < share < 1:
            body.append(f'<line x1="{px(share):.1f}" x2="{px(share):.1f}" y1="{py(0):.1f}" y2="{top + plot_h}" class="rule"/>'
                        f'<circle cx="{px(share):.1f}" cy="{py(0):.1f}" r="4" class="ring-ink"/>'
                        f'<text x="{px(share) + 8:.1f}" y="{py(0) + 16:.1f}" class="val">break-even {pct0(share)}</text>')
    for item in scenarios:
        x, y = px(item["reuse_fraction"]), py(item["usd_savings_1h"])
        above = item["usd_savings_1h"] >= 0
        body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" class="dot"/>'
                    f'<text x="{x:.1f}" y="{y - 10 if above else y + 20:.1f}" text-anchor="{"end" if x > left + plot_w - 30 else "middle"}" '
                    f'class="val">{fmt_money(item["usd_savings_1h"])}</text>')
        tip = (f"{item['reuse_fraction']:.0%} still reusable: 5-minute {fmt_money(item['5m']['usd'])}, "
               f"1-hour {fmt_money(item['1h']['usd'])}, saving {fmt_money(item['usd_savings_1h'])}")
        body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="14" class="hit" tabindex="0" '
                    f'data-tip="{_attr(tip)}" aria-label="{_attr(tip)}"/>')
    return _svg(width, height, "1-hour cache saving by share of context still reusable", "".join(body))


def segments_chart(metrics: Dict[str, Any]) -> str:
    segments = metrics["workload_segments"]
    total = sum(data["cacheable_tokens"] for data in segments.values()) or 1
    order = [name for name in SEGMENT_LABELS if name in segments] + [n for n in segments if n not in SEGMENT_LABELS]
    width, row, bar, top = 440, 44, 16, 4
    plot = width - 70
    height = top + row * len(order)
    body: List[str] = []
    for index, name in enumerate(order):
        data = segments[name]
        y = top + row * index
        share = data["cacheable_tokens"] / total
        bar_w = max(2.0, plot * share) if data["cacheable_tokens"] else 0.0
        body.append(f'<text x="0" y="{y + 14}" class="cat">{html.escape(SEGMENT_LABELS.get(name, name))}</text>')
        body.append(_hbar(0, y + 20, bar_w, bar, "m1" if name == "1-hour candidate" else "md"))
        body.append(f'<text x="{bar_w + 6:.1f}" y="{y + 20 + bar / 2 + 4:.1f}" class="val">{pct0(share)}</text>')
        tip = (f"{counted(data['lanes'], 'conversation or subagent', 'conversations and subagents')}, "
               f"{compact_number(data['cacheable_tokens'])} cached tokens")
        body.append(_hit(0, y, width, row, tip))
    return _svg(width, height, "Cached tokens by conversation type", "".join(body))


def _table(head: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    return ("<table><thead><tr>" + "".join(f"<th>{html.escape(text)}</th>" for text in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{html.escape(text)}</td>" for text in cells) + "</tr>" for cells in rows)
            + "</tbody></table>")


REPORT_CSS = """
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--border:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--de:#aeaca4;--wash:rgba(42,120,214,.07);color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--muted:#898781;--grid:#2c2c2a;--axis:#4a4a46;--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--de:#5a5954;
--wash:rgba(57,135,229,.10);color-scheme:dark}}
:root[data-theme="dark"]{--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#4a4a46;
--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--de:#5a5954;--wash:rgba(57,135,229,.10);color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px}
h2{font-size:19px;margin:0 0 6px;line-height:1.3}
.sub,.cap{color:var(--ink2);margin:0 0 14px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin:16px 0}
.verdict .kick{color:var(--ink2);font-size:13px;margin:0 0 2px} .why{border-bottom:1px dotted var(--muted);cursor:help}
.verdict h2{font-size:24px}
.verdict p{margin:0;max-width:720px}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:16px}
.tile{border-top:1px solid var(--border);padding-top:10px}
.tile b{display:block;font-size:26px;font-weight:600}
.tile span{color:var(--ink2);font-size:13px}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--ink2);margin:0 0 6px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.split{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:24px;align-items:center}
.big{font-size:18px;margin:0 0 8px}
.note{color:var(--ink2);font-size:13px;margin:0}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{font-size:12px;fill:var(--ink2)}
svg .cat{fill:var(--ink)} svg .val{fill:var(--ink);font-weight:600} svg .tick{fill:var(--muted);font-size:11px}
svg .note-t{fill:var(--muted);font-size:11px}
.m1{fill:var(--s1)} .m2{fill:var(--s2)} .md{fill:var(--de)} .band{fill:var(--wash)}
.rule{stroke:var(--axis);stroke-width:1} .grid{stroke:var(--grid);stroke-width:1} .zero{stroke:var(--ink2);stroke-width:1.5}
.series{stroke:var(--s1);stroke-width:2;stroke-linecap:round}
.dot{fill:var(--s1);stroke:var(--surface);stroke-width:2} .ring-ink{fill:var(--ink);stroke:var(--surface);stroke-width:2}
.hit{fill:transparent;outline:none} .hit:hover,.hit:focus{fill:var(--ink);fill-opacity:.05}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--border);
border-radius:8px;padding:6px 10px;font-size:13px;max-width:320px;box-shadow:0 4px 14px rgba(0,0,0,.12);z-index:9}
ul{padding-left:20px;margin:0} li{margin:4px 0}
details{margin:16px 0} summary{cursor:pointer;color:var(--ink2)}
table{width:100%;border-collapse:collapse;margin:10px 0 4px;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--grid);text-align:right} th:first-child,td:first-child{text-align:left}
th{color:var(--ink2);font-weight:600}
code{background:var(--grid);padding:1px 4px;border-radius:4px}
@media (max-width:720px){.tiles,.split{grid-template-columns:1fr} h1{font-size:24px} svg text{font-size:16px} svg .tick,svg .note-t{font-size:14px}}
"""

TOOLTIP_JS = """
(function(){var tip=document.getElementById('tip');
function place(el,x,y){tip.textContent=el.getAttribute('data-tip');tip.hidden=false;
var w=tip.offsetWidth,h=tip.offsetHeight;tip.style.left=Math.max(8,Math.min(x+14,innerWidth-w-8))+'px';
tip.style.top=Math.max(8,(y-h-12<8?y+18:y-h-12))+'px';}
document.addEventListener('pointermove',function(e){var el=e.target.closest&&e.target.closest('[data-tip]');
if(el){place(el,e.clientX,e.clientY);}else{tip.hidden=true;}});
document.addEventListener('focusin',function(e){var el=e.target.closest&&e.target.closest('[data-tip]');
if(el){var r=el.getBoundingClientRect();place(el,r.left+r.width/2,r.top);}});
document.addEventListener('focusout',function(){tip.hidden=true;});})();
"""


def html_report(metrics: Dict[str, Any]) -> str:
    rec = metrics["recommendation"]
    quality = metrics["quality"]
    hist = metrics["api_gap_histogram"]
    scenarios = metrics["scenarios"]
    central = central_scenario(metrics)
    who = metrics["agent_activity"]["gap_actor_histogram"]
    final = metrics["eligibility_funnel"][-1]
    window = window_share(hist)
    saving = central["usd_savings_1h"]
    segments = metrics["workload_segments"]
    seg_total = sum(data["cacheable_tokens"] for data in segments.values()) or 1
    candidate_share = segments.get("1-hour candidate", {}).get("cacheable_tokens", 0) / seg_total
    if candidate_share:
        segments_title = f"Conversations that often resume after 5–60 minutes hold {pct0(candidate_share)} of cached tokens"
    else:
        segments_title = "No conversation or subagent often resumes after a 5–60 minute pause"
    share = break_even_share(scenarios)
    line = savings_line(scenarios)
    if line is None:
        savings_title = "1-hour saving at the tested share"
    elif share is not None and 0 < share < 1:
        savings_title = f"Break-even: {pct0(share)} of context still reusable"
    elif (share is not None and share <= 0) or (share is None and line[0] > 0):
        savings_title = "The 1-hour cache saves money at every share"
    else:
        savings_title = "The 1-hour cache never saves money here"
    first, last = quality["first_event"][:10], quality["last_event"][:10]
    advice_items = "".join("<li>%s</li>" % html.escape(line) for line in advice(metrics))
    pause_rows = [
        (label, fmt_int(hist["counts"][i]), fmt_int(who["counts"]["no_direct_human"][i]), compact_number(hist["structural_tokens"][i]))
        for i, label in enumerate(hist["labels"])
    ]
    saving_rows = [
        (f"{item['reuse_fraction']:.0%}", fmt_money(item["5m"]["usd"]), fmt_money(item["1h"]["usd"]), fmt_money(item["usd_savings_1h"]))
        for item in scenarios
    ]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cache lifetime audit</title><style>{REPORT_CSS}</style></head><body><main>
<h1>Claude Code cache: 5 minutes or 1 hour?</h1>
<p class="sub">{fmt_int(quality['unique_api_events'])} responses, {first} to {last}. Estimated at list API rates, not a bill.</p>
<section class="panel verdict"><p class="kick"><span class="why" tabindex="0" data-tip="{_attr('; '.join(metrics['confidence']['reasons']))}">Confidence: {html.escape(metrics['confidence']['level'])}</span></p>
<h2>{html.escape(rec['label'])}</h2><p>{html.escape(rec['reason'])}</p>
<div class="tiles">
<div class="tile"><b>{fmt_money(abs(saving))}</b><span>{'saved' if saving >= 0 else 'extra cost'} with 1 hour, if {central['reuse_fraction']:.0%} of context is still reusable</span></div>
<div class="tile"><b>{fmt_int(final['gaps'])}</b><span>{'return' if final['gaps'] == 1 else 'returns'} after a 5–60 minute pause</span></div>
<div class="tile"><b>{pct0(central['price_coverage_cacheable_tokens'])}</b><span>of tokens priced</span></div>
</div></section>
<section class="panel"><h2>{'Only ' if window < 0.25 else ''}{pct0(window)} of reusable context waited the 5–60 minutes where the 1-hour cache helps</h2>
<p class="cap">A pause is the time between two responses in one conversation or subagent.</p>
<div class="legend"><span><i style="background:var(--s1)"></i>{AGENT_LABEL}</span><span><i style="background:var(--s2)"></i>{YOU_LABEL}</span></div>
{pause_chart(metrics)}</section>
<section class="panel"><h2>Which pauses could the 1-hour cache have helped?</h2>
{funnel_chart(metrics)}</section>
<section class="panel"><h2>{html.escape(segments_title)}</h2>
{segments_chart(metrics)}</section>
<section class="panel"><h2>{html.escape(savings_title)}</h2>
<div class="split">{savings_chart(metrics)}
<div><p class="big">{html.escape(break_even_sentence(scenarios))}</p>
<p class="note">The share is a what-if setting, not a measurement.</p></div></div></section>
<section class="panel"><h2>What to do</h2><ul>
{advice_items}
</ul></section>
<details><summary>Exact numbers</summary>
{_table(("Pause", "Pauses", "Agent alone", "Reusable context"), pause_rows)}
{_table(("Still reusable", "5-minute cost", "1-hour cost", "Saving"), saving_rows)}
</details>
<p class="note">Prices: {html.escape(str(metrics['pricing']['table_id']))}. Some cache resets, like MCP tool changes, are not in local logs.</p>
</main><div id="tip" role="tooltip" hidden></div><script>{TOOLTIP_JS}</script></body></html>"""


def write_session_ledger(path: str, events: Sequence[Event], gaps: Sequence[Gap],
                         scope: Optional[str] = None) -> None:
    """Upsert this analysis's per-SESSION columns into the shared ledger.

    Three columns, and the first one needs its warning carried with it:

    `cache_hit_ratio` is LEGIBLE AND MISLEADING ON ITS OWN. It is monotone in
    the wrong thing — a session that re-read a huge context forty times
    through the cache scores BETTER than a cheap session that read a small
    context twice. The official Claude Code Grafana board headlines it; this
    skill deliberately does not. It is in the ledger because it is a useful
    covariate beside cost, and its definition string says so.

    NAMING: a "cold event" here is a GAP OVER 5 MINUTES
    with no structural change. It is not an observed cache MISS, and the TTL
    that actually applied to that request is not verified -- a 1-hour cache
    would have covered many of them, and the provider may or may not have
    evicted. The ledger column says so, and so does the reference.

    `cache_cold_events` is the actionable one: gaps inside a lane that
    outlived the 5-minute TTL with NO structural change (same model, version,
    project, branch, no context shrink). Those are turns that paid a fresh
    cache write purely because of the clock, which is exactly what a 1-hour
    TTL buys back.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import session_ledger
    except Exception as error:                       # never take the audit down for this
        print("ledger skipped: %s: %s" % (error.__class__.__name__, error), file=sys.stderr)
        return
    reads: Dict[str, int] = collections.defaultdict(int)
    fresh: Dict[str, int] = collections.defaultdict(int)
    for event in events:
        reads[event.session] += event.cache_read
        fresh[event.session] += event.input_tokens
    cold: Dict[str, int] = collections.defaultdict(int)
    within_hour: Dict[str, int] = collections.defaultdict(int)
    beyond_hour: Dict[str, int] = collections.defaultdict(int)
    for gap in gaps:
        if not gap.same_epoch or gap.seconds <= 300:
            continue                                 # inside the 5-minute TTL, or a real change
        cold[gap.current.session] += 1
        if gap.seconds <= 3600:
            within_hour[gap.current.session] += 1
        else:
            beyond_hour[gap.current.session] += 1
    rows = []
    for session in sorted(set(reads) | set(fresh)):
        denominator = reads[session] + fresh[session]
        if within_hour[session] and beyond_hour[session]:
            band = "mixed"
        elif within_hour[session]:
            band = "1h"                              # a 1-hour TTL would have covered these
        elif beyond_hour[session]:
            band = "beyond-1h"                       # even 1h would not have held it
        else:
            band = "5m"                              # never went cold on the clock
        rows.append({
            "session_id": session,
            "cache_hit_ratio": (round(reads[session] / denominator, 4) if denominator else None),
            "cache_cold_events": cold[session],
            "cache_ttl_band": band,
        })
    stats = session_ledger.upsert(path, rows, "claude_cache_audit.py",
                                  ["cache_hit_ratio", "cache_cold_events", "cache_ttl_band"],
                                  scope=audit_common.home_relative(scope))
    print("ledger: %s — %d session(s) updated, %d added; %s"
          % (stats["ledger"], stats["rows_updated"], stats["rows_added"],
             session_ledger.headline(path)))


def analyze(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.root).expanduser().resolve()
    output = Path(args.output_dir).expanduser().resolve()
    if root == output or root in output.parents:
        raise ValueError("output directory must be outside the Claude configuration directory")
    if not root.exists():
        raise FileNotFoundError("Claude configuration directory not found: %s" % root)
    since = parse_bound(args.since)
    until = parse_bound(args.until, is_until=True)
    if since is not None and until is not None and since >= until:
        raise ValueError("--since must be earlier than --until")
    pricing = load_pricing(Path(args.pricing_file).expanduser().resolve(), args.pricing_table)
    try:
        cwd_filter = CwdFilter.from_args(args)
    except re.error as error:
        raise ValueError("invalid working-directory regular expression: %s" % error) from error
    events, prompts, quality = read_history(root, since, until, cwd_filter)
    if not events:
        raise ValueError("no assistant events with usage were found in the selected period")
    gaps, lanes = make_gaps(events)
    if getattr(args, "ledger", None):
        write_session_ledger(str(Path(args.ledger).expanduser()), events, gaps,
                             scope="since=%s until=%s [%s]"
                             % (args.since, args.until, cwd_filter.describe()))
    hist = gap_histogram(gaps)
    human_hist = human_gap_histogram(prompts)
    activity = agent_activity_metrics(gaps, lanes, prompts)
    funnel_data = funnel(gaps)
    scenarios = [simulate(events, lanes, pricing, reuse) for reuse in args.reuse_scenarios]
    actual = actual_metrics(events, gaps, pricing, args.unknown_write_ttl)
    invalidations: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for gap in gaps:
        for cause in gap.causes:
            invalidations[cause].update({"gaps": 1, "structural_tokens": gap.structural_tokens})
    one_shot = sum(1 for lane in lanes.values() if len(lane) == 1)
    quality.update({
        "unique_api_events": len(events), "human_prompts": len(prompts), "sessions": len({e.session for e in events}),
        "lanes": len(lanes), "one_shot_lanes": one_shot,
        "api_error_events": sum(1 for event in events if event.api_error),
        "first_event": events[0].timestamp_iso, "last_event": events[-1].timestamp_iso,
    })
    label, reason = choose_recommendation(scenarios, hist, funnel_data)
    segments = workload_segments(lanes)
    confidence = recommendation_confidence(scenarios, quality, funnel_data, actual)
    metrics: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "selection": {"since": args.since, "until": args.until, "timezone": "UTC"},
        "quality": quality,
        "pricing": {key: pricing.get(key) for key in ("table_id", "description", "effective_from", "effective_through", "currency")},
        "break_even": {"policy_reuse_ratio": POLICY_BREAK_EVEN, "narrow_transition_fraction": TRANSITION_BREAK_EVEN},
        "api_gap_histogram": hist, "human_prompt_gaps": human_hist,
        "agent_activity": activity,
        "eligibility_funnel": funnel_data,
        "workload_segments": segments,
        "invalidation_signals": {key: dict(value) for key, value in sorted(invalidations.items())},
        "actual": actual, "scenarios": scenarios,
        "recommendation": {"label": label, "reason": reason},
        "confidence": confidence,
    }
    # Owner-only: these files describe the user's own sessions.
    audit_common.make_private_dir(output)
    # The home folder is written as `~`, so a shared report does not name the account.
    audit_common.write_output(output / "metrics.json",
                              json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    audit_common.write_output(output / "summary.md", markdown_report(metrics))
    audit_common.write_output(output / "report.html", html_report(metrics))
    return metrics


def reuse_values(text: str) -> List[float]:
    values: List[float] = []
    for piece in text.split(","):
        try:
            value = float(piece.strip())
        except ValueError as error:
            raise argparse.ArgumentTypeError("reuse scenarios must be comma-separated decimals") from error
        if not 0 <= value <= 1:
            raise argparse.ArgumentTypeError("reuse scenarios must lie between 0 and 1")
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("at least one reuse scenario is required")
    return sorted(set(values))


def parser() -> argparse.ArgumentParser:
    skill_root = Path(__file__).resolve().parent.parent
    default_root = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", default=default_root, help="Claude configuration directory (default: CLAUDE_CONFIG_DIR or ~/.claude)")
    result.add_argument("--output-dir", required=True, help="directory for summary.md, report.html, and metrics.json")
    result.add_argument("--since", help="UTC start, inclusive (YYYY-MM-DD or ISO-8601)")
    result.add_argument("--until", help="UTC end, exclusive (YYYY-MM-DD or ISO-8601)")
    result.add_argument("--pricing-file", default=str(skill_root / "references" / "pricing-public-example.json"), help="JSON price catalog")
    result.add_argument("--pricing-table", help="table ID when the catalog contains multiple tables")
    result.add_argument("--reuse-scenarios", type=reuse_values, default=[0.5, 0.8, 1.0], help="what-if values, 0 to 1, for the share of context still reusable "
                             "when a conversation resumes after a 5-60 minute pause (default 0.5,0.8,1.0)")
    result.add_argument("--unknown-write-ttl", choices=("5m", "1h"), default="5m", help="price cache writes that older logs do not label 5m or 1h at this lifetime")
    result.add_argument("--ledger", metavar="PATH",
                        help="add this analysis's columns (cache_hit_ratio, "
                             "cache_cold_events, cache_ttl_band) to the session ledger "
                             "(sessions.jsonl). Columns written by other analyses are "
                             "kept; see references/session-ledger.md")
    add_filter_args(result)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    with audit_common.home_relative_stdout():
        return _main(argv)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        metrics = analyze(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 2
    central = central_scenario(metrics)
    print("%s. %s" % (metrics["recommendation"]["label"], metrics["recommendation"]["reason"]))
    actual = metrics["actual"]
    print("If %d%% of the context is still reusable on return, the 1-hour cache %s. %s" % (
        round(100 * central["reuse_fraction"]), savings_outcome(central["usd_savings_1h"]),
        break_even_sentence(metrics["scenarios"])))
    print("Estimated at list API rates, not a bill; %s of tokens have a price." % pct0(actual["price_coverage"]))
    if actual.get("price_coverage_caveat"):
        print(actual["price_coverage_caveat"])
    print("Reports written to %s" % Path(args.output_dir).expanduser().resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
