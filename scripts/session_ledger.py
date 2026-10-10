#!/usr/bin/env python3
"""The session LEDGER — one canonical row per session that every analysis in
this skill adds columns to.

A session browser with sortable columns is one of the most useful views, and
more useful still with every analysis's metrics beside it — compaction stats,
cache TTL, lane economics, tool results. So the table is its own analysis
index, linked from the top of the report, and each analysis adds its columns
as it runs.

So this file owns ONE artefact, `sessions.jsonl`:

  * one JSON object per line, one line per session, keyed by `session_id`;
  * full fidelity by DEFAULT (it is your own machine's history);
  * every analysis script takes `--ledger PATH` and UPSERTS its own columns.

The contract that makes it safe for many producers to write one file:

  UPSERT NEVER DROPS A COLUMN IT DID NOT WRITE.

`upsert()` reads the existing row, updates only the keys the caller declares,
and writes the merged row back. A producer that crashes half-way leaves the
other analyses' columns intact. Column provenance — which script wrote a
column and when — is kept in a sidecar (`<ledger>.columns.json`) so the report
can show a tooltip saying where each column came from, and so a stale column
from a producer that has not run since a schema change is visible rather than
silently believed.

Namespacing: a producer that is work-in-progress or self-declared
uncalibrated writes under an explicit prefix (`wip_`), so nobody reads an
uncalibrated number as a settled one.

Schema, column by column: `references/session-ledger.md`.

    python3 session_ledger.py show    --ledger OUT/sessions.jsonl
    python3 session_ledger.py columns --ledger OUT/sessions.jsonl
    python3 session_ledger.py selftest
"""
import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_common import make_private_dir, utf8_stdio         # noqa: E402

SCHEMA_VERSION = 1
KEY = "session_id"

# Producers that are expected to contribute, with the columns each one owns.
# A producer that has not run yet is REGISTERED but empty — the report says
# "not run" rather than leaving a reader to wonder whether the column is a
# zero or an absence. Registering costs nothing and makes the index honest.
REGISTRY: Dict[str, Dict[str, str]] = {
    "orientation_profile.py": {
        "source": "which store the session came from (claude-code, vscode-copilot, copilot-cli)",
        "start": "first turn timestamp, UTC ISO-8601",
        "end": "last turn timestamp, UTC ISO-8601",
        "cwd": "working directory (hashed under --redact)",
        "cwd_basename": "the folder name alone",
        "title": "session title or the first prompt line",
        "models": "distinct models used; `<synthetic>` is excluded (it is not a model)",
        "turns": "assistant responses — one API response is one turn, not one JSONL row",
        "total_tokens": "input + output + cache read + cache write",
        "orientation_tokens": "startup: tokens spent before the session's first edit",
        "orientation_share": "startup share: startup tokens / all tokens; empty for a no-edit session",
        "orientation_position": ("which tenth of the distribution the startup share sits in "
                                 "(`0-10 %` … `90-100 %`), or `no edit`. A position, never a "
                                 "verdict: no threshold has been validated"),
        "orientation_breadth": "files read first: distinct files touched before the first edit",
        "orientation_cost_usd": ("startup cost: the responses before the first edit, each priced "
                                 "at its own model's list rate"),
        "continuation_kind": ("how work was picked up in this session: resumed after a pause, "
                              "resumed after compaction, new session, ran in parallel with an "
                              "earlier session, or first. A new session wins over a resume"),
        "carry_tokens": ("pick-up cost in tokens: the responses from the restart to the next "
                         "edit; for a new session, its startup. Empty when unknown"),
        "carry_cost_usd": "pick-up cost in dollars, at list API rates",
        "gap_hours_before": ("hours between the previous response (a resume) or the previous "
                             "session's end (a new session) and this pick-up"),
        "compactions": "how many times the conversation was compacted, from the CLI's own records",
        "handoff_opened_by_turn_2": "read a handoff note (not a README) in the first 2 turns",
        "readme_opened_by_turn_2": "read a README in the first 2 turns; kept apart from handoff notes",
        "question_first": "started with a question, and was asked to change something only "
                          "later. Empty when the store records no user text",
        "strategy": "scripted | mixed | by-hand | read-only, from the tool calls",
        "effort": "the reasoning-effort level recorded on the assistant records",
        "thinking_share": "reasoning tokens / output tokens: the cross-check on effort",
        "outcome": "success | failure | unknown, inferred from the transcript",
        "est_cost_usd": "estimated cost at list API rates, for comparing sessions, never a bill",
        "thread_id": "the sitting this session belongs to (sessions less than 72 h apart)",
        "stream_id": "the work item this session belongs to (no gap limit)",
        "stream_name": "the work item's name (folder, branch or ticket)",
        "stream_named_by": "which rule grouped it: folder | branch | ticket",
    },
    "claude_cache_audit.py": {
        "cache_hit_ratio": "cache_read / (cache_read + fresh input). Misleading on its own: "
                           "a session that re-read a huge context scores well; read it beside cost",
        "cache_cold_events": ("pauses between responses longer than 5 minutes with nothing else "
                             "changed; the cache lifetime that applied is not verified, and no "
                             "cache miss was observed"),
        "cache_ttl_band": "which cache lifetime the session's pauses suit: 5m | 1h | mixed",
    },
    "tool_bloat.py": {
        "tool_result_bytes": "total bytes of tool results carried in this session's context",
        "top_tool": "the tool whose results cost the most downstream re-reads",
        "top_tool_bytes": "that tool's share of the bytes",
    },
    "lane_profile.py": {
        "lane": "the conversation or subagent this session's work was attributed to",
        "fat_context_read_share": "share of tokens that were cache reads of a large carried context",
    },
    "compact_economics.py": {
        # `compactions` belongs to orientation_profile.py: that reader takes it
        # from the CLI's own `compact_boundary` system rows, which publish the
        # count directly, and compact_economics.py has never written a ledger
        # row. The two size columns stay here.
        "tokens_before_compact": "context size at the last compaction",
        "tokens_after_compact": "context size immediately after it",
    },
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sidecar_path(ledger: os.PathLike) -> Path:
    return Path(str(ledger) + ".columns.json")


def load(ledger: os.PathLike) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Return (rows keyed by session id, provenance). Missing file = empty."""
    path = Path(ledger)
    rows: Dict[str, Dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue                       # a truncated write is skipped, never fatal
            key = row.get(KEY)
            if key:
                rows[str(key)] = row
    side = sidecar_path(path)
    provenance: Dict[str, Any] = {"schema_version": SCHEMA_VERSION, "columns": {}}
    if side.exists():
        try:
            provenance = json.loads(side.read_text(encoding="utf-8"))
        except ValueError:
            pass
    provenance.setdefault("columns", {})
    return rows, provenance


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then replace.

    Several analyses may be pointed at one ledger in a single shell pipeline;
    a half-written sessions.jsonl that another producer then reads and merges
    would corrupt every column, not just the interrupted one.
    """
    make_private_dir(path.parent)
    # mkstemp creates the file 0600, and os.replace keeps that mode.
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger-", suffix=".tmp")
    try:
        # newline="\n": LF on every platform, like every other file the skill writes.
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def upsert(ledger: os.PathLike, rows: Iterable[Dict[str, Any]], producer: str,
           columns: Optional[Sequence[str]] = None,
           prefix: str = "", definitions: Optional[Dict[str, str]] = None,
           scope: Optional[str] = None) -> Dict[str, Any]:
    """Merge `rows` into the ledger. Only the declared columns are touched.

    Each row must carry `session_id`. Columns not named in `columns` (or, if
    `columns` is None, not present on the incoming row) are left exactly as
    whatever other producer wrote them.

    `scope` records the FILTER this producer ran under (`--since`, a cwd
    pattern). Producers do not share filters — `tool_bloat.py --since
    2026-08-25` and `orientation_profile.py --exclude-cwd-pattern scratch` see
    different session sets — so the ledger is a UNION and any consumer that
    shows it must say whose rows it is showing. Found the first time three
    producers were pointed at one file: the ledger came back with several times
    as many sessions as the orientation run had seen.
    """
    path = Path(ledger)
    existing, provenance = load(path)
    incoming = list(rows)
    declared = list(columns) if columns is not None else sorted(
        {k for row in incoming for k in row if k != KEY})
    declared = [prefix + c if prefix and not c.startswith(prefix) else c for c in declared]
    added = updated = 0
    for row in incoming:
        key = str(row.get(KEY) or "")
        if not key:
            continue
        target = existing.get(key)
        if target is None:
            target = {KEY: key}
            existing[key] = target
            added += 1
        else:
            updated += 1
        for column in declared:
            raw = column[len(prefix):] if prefix and column.startswith(prefix) else column
            if raw in row:
                target[column] = row[raw]
            elif column in row:
                target[column] = row[column]
    known = definitions or REGISTRY.get(producer, {})
    for column in declared:
        raw = column[len(prefix):] if prefix and column.startswith(prefix) else column
        provenance["columns"][column] = {
            "producer": producer,
            "updated": _now(),
            "definition": known.get(raw) or known.get(column) or "",
        }
    provenance["schema_version"] = SCHEMA_VERSION
    provenance["sessions"] = len(existing)
    provenance.setdefault("scopes", {})[producer] = {
        "scope": scope or "unrecorded",
        "rows_written": len(incoming),
        "at": _now(),
    }
    ordered = sorted(existing.values(), key=lambda r: (r.get("start") or "", r.get(KEY)))
    _atomic_write(path, "".join(json.dumps(r, default=str, sort_keys=False) + "\n"
                                for r in ordered))
    _atomic_write(sidecar_path(path), json.dumps(provenance, indent=1) + "\n")
    return {"ledger": str(path), "producer": producer, "sessions": len(existing),
            "rows_added": added, "rows_updated": updated,
            "columns_written": declared,
            "columns_total": len(provenance["columns"])}


def register(ledger: os.PathLike, producer: str,
             columns: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Record that a producer OWNS these columns without writing any values.

    This is what makes the analysis index honest: a column that appears with
    `producer` set and no values is an analysis that has not been run here,
    which is a different statement from a column of zeroes.
    """
    path = Path(ledger)
    _, provenance = load(path)
    names = list(columns) if columns is not None else list(REGISTRY.get(producer, {}))
    known = REGISTRY.get(producer, {})
    for column in names:
        if column in provenance["columns"] and provenance["columns"][column].get("updated"):
            continue                            # a real write always wins over a registration
        provenance["columns"][column] = {"producer": producer, "updated": None,
                                         "definition": known.get(column, "")}
    provenance["schema_version"] = SCHEMA_VERSION
    _atomic_write(sidecar_path(path), json.dumps(provenance, indent=1) + "\n")
    return {"ledger": str(path), "producer": producer, "registered": names}


def summary(ledger: os.PathLike) -> Dict[str, Any]:
    rows, provenance = load(ledger)
    columns = provenance.get("columns", {})
    written = {c: meta for c, meta in columns.items() if meta.get("updated")}
    producers = sorted({meta.get("producer") for meta in written.values() if meta.get("producer")})
    present: Dict[str, int] = {}
    for column in columns:
        present[column] = sum(1 for r in rows.values() if r.get(column) is not None)
    return {
        "sessions": len(rows),
        "columns": len(columns),
        "columns_with_values": sum(1 for c, n in present.items() if n),
        "analyses": len(producers),
        "producers": producers,
        "registered_not_run": sorted(c for c, meta in columns.items() if not meta.get("updated")),
        # Producers do not share filters, so the ledger is a union of scopes.
        # A consumer that shows the table must say whose rows it is showing.
        "scopes": provenance.get("scopes", {}),
        "coverage": present,
        "provenance": columns,
    }


def headline(ledger: os.PathLike) -> str:
    """The one line the report's analysis index shows on the ledger card."""
    stats = summary(ledger)
    return ("Session ledger — %d sessions, %d columns from %d %s"
            % (stats["sessions"], stats["columns"], stats["analyses"],
               "analysis" if stats["analyses"] == 1 else "analyses"))


# --------------------------------------------------------------------------
def _selftest() -> int:
    failures: List[str] = []

    def check(name: str, condition: bool, detail: Any = "") -> None:
        if condition:
            print("  ok   %s" % name)
        else:
            print("  FAIL %s %s" % (name, detail))
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="ledger-test-") as tmp:
        ledger = Path(tmp) / "sessions.jsonl"
        first = upsert(ledger, [{"session_id": "a", "start": "2026-09-01T00:00:00Z",
                                 "orientation_share": 0.3},
                                {"session_id": "b", "start": "2026-09-02T00:00:00Z",
                                 "orientation_share": 0.1}],
                       "orientation_profile.py", ["start", "orientation_share"])
        check("1: two rows written", first["sessions"] == 2, first)
        check("1: rows_added counted", first["rows_added"] == 2, first)

        second = upsert(ledger, [{"session_id": "a", "top_tool": "Bash",
                                  "tool_result_bytes": 4096}],
                        "tool_bloat.py", ["top_tool", "tool_result_bytes"])
        rows, provenance = load(ledger)
        check("2: a second producer does NOT drop the first's columns",
              rows["a"].get("orientation_share") == 0.3, rows["a"])
        check("2: and its own columns are there",
              rows["a"].get("top_tool") == "Bash", rows["a"])
        check("2: a row the second producer never saw is untouched",
              rows["b"].get("orientation_share") == 0.1 and "top_tool" not in rows["b"],
              rows["b"])
        check("2: rows_updated, not rows_added", second["rows_updated"] == 1
              and second["rows_added"] == 0, second)
        check("2: provenance names the producer per column",
              provenance["columns"]["top_tool"]["producer"] == "tool_bloat.py"
              and provenance["columns"]["orientation_share"]["producer"]
              == "orientation_profile.py", provenance["columns"])

        third = upsert(ledger, [{"session_id": "a", "shift": "opus->haiku"}],
                       "example_wip.py", ["shift"], prefix="wip_")
        rows, provenance = load(ledger)
        check("3: a WIP producer writes under its prefix",
              rows["a"].get("wip_shift") == "opus->haiku" and "shift" not in rows["a"],
              rows["a"])
        check("3: and the prefix is what provenance records",
              "wip_shift" in provenance["columns"], sorted(provenance["columns"]))

        register(ledger, "compact_economics.py")
        stats = summary(ledger)
        check("4: registering adds column names without values",
              "tokens_before_compact" in stats["registered_not_run"],
              stats["registered_not_run"])
        check("4: a registered-only column is not counted as having values",
              stats["coverage"]["tokens_before_compact"] == 0, stats["coverage"])
        check("4: a registration does NOT overwrite a real write",
              provenance["columns"]["orientation_share"]["updated"] is not None)

        register(ledger, "orientation_profile.py")
        _, after = load(ledger)
        check("4: ... even when the same producer re-registers",
              after["columns"]["orientation_share"]["updated"] is not None,
              after["columns"]["orientation_share"])

        check("5: headline reads as a sentence",
              headline(ledger).startswith("Session ledger — 2 sessions,"), headline(ledger))
        check("5: every registry producer has at least one documented column",
              all(any(v for v in cols.values()) for cols in REGISTRY.values()))
        check("5: no registry column is documented twice under two producers",
              len({c for cols in REGISTRY.values() for c in cols})
              == sum(len(cols) for cols in REGISTRY.values()),
              sorted(c for cols in REGISTRY.values() for c in cols))

        # A truncated final line must not take the whole ledger down.
        with ledger.open("a", encoding="utf-8") as stream:
            stream.write('{"session_id": "c", "start"')
        rows, _ = load(ledger)
        check("6: a truncated line is skipped, the good rows survive",
              set(rows) == {"a", "b"}, sorted(rows))

        if os.name == "posix":
            nested = Path(tmp) / "new-dir" / "sessions.jsonl"
            upsert(nested, [{"session_id": "a", "start": "x"}], "orientation_profile.py",
                   ["start"])
            modes = {p.name: oct(p.stat().st_mode & 0o777)
                     for p in (nested, sidecar_path(nested), nested.parent)}
            check("7: the ledger, its sidecar and a directory it creates are owner-only",
                  modes == {"sessions.jsonl": "0o600", "sessions.jsonl.columns.json": "0o600",
                            "new-dir": "0o700"}, modes)

    print()
    if failures:
        print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("all checks passed")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="session_ledger.py",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name, helptext in (("show", "one line per column: producer, when, how many rows have it"),
                           ("columns", "the column list as JSON"),
                           ("schema", "the documented registry: every producer and its columns")):
        p = sub.add_parser(name, help=helptext, description=helptext)
        if name != "schema":
            p.add_argument("--ledger", required=True)
    sub.add_parser("selftest", help="run the bundled unit tests")
    args = ap.parse_args(argv)

    if args.command == "selftest":
        return _selftest()
    if args.command == "schema":
        for producer, columns in REGISTRY.items():
            print("\n%s" % producer)
            for column, definition in columns.items():
                print("  %-28s %s" % (column, definition))
        return 0
    if not Path(args.ledger).exists() and not sidecar_path(args.ledger).exists():
        print("no ledger at %s — run `orientation_profile.py report --ledger %s` first"
              % (args.ledger, args.ledger), file=sys.stderr)
        return 2
    stats = summary(args.ledger)
    if args.command == "columns":
        print(json.dumps(stats["provenance"], indent=1))
        return 0
    print(headline(args.ledger))
    print("%-30s %-26s %-22s %s" % ("column", "producer", "written", "rows with a value"))
    for column, meta in sorted(stats["provenance"].items(),
                               key=lambda kv: (kv[1].get("producer") or "", kv[0])):
        print("%-30s %-26s %-22s %d"
              % (column[:30], (meta.get("producer") or "?")[:26],
                 (meta.get("updated") or "NOT RUN HERE")[:22], stats["coverage"].get(column, 0)))
    if stats["registered_not_run"]:
        print("\n%d column(s) are registered but their analysis has not been run here: %s"
              % (len(stats["registered_not_run"]), ", ".join(stats["registered_not_run"])))
        print("A registered column with no values is an analysis you have not run — "
              "not a column of zeroes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
