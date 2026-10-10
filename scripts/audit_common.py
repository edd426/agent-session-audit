#!/usr/bin/env python3
"""Shared scope filter and price table for every analysis in this skill.

WHY THIS MODULE EXISTS. Two defects were both "the same thing implemented
twice":

1. `--exclude-cwd-pattern` existed on `orientation_profile.py` and
   `claude_cache_audit.py`, and on NOTHING else -- so the documented lane step
   could not be run under an exclusion requirement.
2. Two scripts read the same `--pricing-file` and reported different price
   coverage for it (15 % vs 100 %), because `orientation_profile.py` silently
   merged a built-in table over the file. A dollar total resting on 15 % of
   tokens was printed with no caveat.

So: ONE filter with ONE set of flag names and ONE meaning, and ONE price table
(`references/pricing-public-example.json`) with no silent built-in fallback.
Every analysis imports this module. If you add a sixth analysis, import it too.

Tests: `test_audit_common.py` (run it after changing anything here).
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PRICING_FILE = os.path.join(
    SKILL_ROOT, "references", "pricing-public-example.json")

# --------------------------------------------------------------------------
# Scope filter
# --------------------------------------------------------------------------
# Scripted or synthetic runs often execute out of a scratch directory whose
# transcripts land under a different project directory from the repo that
# launched them, so excluding the repo path alone may not exclude them. Pass
# one --exclude-cwd-pattern per location; the report prints the
# pattern-by-pattern inclusion/exclusion breakdown.
_INCLUDE_HELP = ("keep only records whose working directory matches this regex "
                 "(repeatable; matched with re.search against the record's `cwd`, "
                 "falling back to the transcript's project directory)")
_EXCLUDE_HELP = ("drop records whose working directory matches this regex "
                 "(repeatable; same matching rule as --include-cwd-pattern). "
                 "Exclude wins over include.")


def add_filter_args(parser) -> None:
    """Add the two scope flags. Identical names and help text everywhere."""
    group = parser.add_argument_group("scope filters (shared by every analysis)")
    group.add_argument("--include-cwd-pattern", action="append", default=[],
                       metavar="REGEX", help=_INCLUDE_HELP)
    group.add_argument("--exclude-cwd-pattern", action="append", default=[],
                       metavar="REGEX", help=_EXCLUDE_HELP)


class CwdFilter:
    """Include/exclude decision for one record, by working directory.

    Semantics, fixed here so all five analyses agree:
      * the tested string is `record["cwd"]`, or the transcript's project
        directory when the record carries no cwd;
      * patterns are regexes, matched with `re.search` (not fullmatch);
      * `--include-cwd-pattern` is a whitelist: given any, a record must match
        at least one;
      * `--exclude-cwd-pattern` wins over include.
    """

    def __init__(self, include: Sequence[str] = (), exclude: Sequence[str] = ()) -> None:
        self.include_patterns: List[str] = list(include or [])
        self.exclude_patterns: List[str] = list(exclude or [])
        self._include = [re.compile(p) for p in self.include_patterns]
        self._exclude = [re.compile(p) for p in self.exclude_patterns]
        self.filtered_rows = 0

    @classmethod
    def from_args(cls, args) -> "CwdFilter":
        return cls(getattr(args, "include_cwd_pattern", None) or [],
                   getattr(args, "exclude_cwd_pattern", None) or [])

    @property
    def active(self) -> bool:
        return bool(self._include or self._exclude)

    def keep(self, cwd: Optional[str], fallback: Optional[str] = None) -> bool:
        """True if a record with this cwd is in scope. Counts what it drops."""
        target = str(cwd or fallback or "")
        if (self._include and not any(p.search(target) for p in self._include)) \
                or any(p.search(target) for p in self._exclude):
            self.filtered_rows += 1
            return False
        return True

    def describe(self) -> str:
        """One line naming the scope, for a report header or a ledger row."""
        parts = []
        if self.include_patterns:
            parts.append("include " + ",".join(self.include_patterns))
        if self.exclude_patterns:
            parts.append("exclude " + ",".join(self.exclude_patterns))
        return "; ".join(parts) if parts else "no cwd filter (everything)"


# --------------------------------------------------------------------------
# Private output files
# --------------------------------------------------------------------------
# Every report, JSON, Markdown page and ledger an analysis writes describes
# the user's own sessions: working directories, prompts, project names. They
# are created owner-only (files 0600, directories this code creates 0700), the
# same as `sessions.jsonl`. Directories that already exist are left alone. On
# Windows chmod only toggles the read-only bit, so this is best effort there
# and never raises.
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def _chmod_quietly(path, mode: int) -> None:
    try:
        os.chmod(os.fspath(path), mode)
    except OSError:
        pass


def make_private_dir(path) -> str:
    """mkdir -p, with every directory it CREATES set to 0700. Returns the path."""
    path = os.path.abspath(os.fspath(path))
    missing = []
    probe = path
    while not os.path.isdir(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, PRIVATE_DIR_MODE)
        except FileExistsError:
            continue
        _chmod_quietly(directory, PRIVATE_DIR_MODE)   # umask may have narrowed it
    return path


def write_private(path, text: str, encoding: str = "utf-8") -> None:
    """Write a text file that only its owner can read (0600 on POSIX).

    The mode is set at creation, so the content is never briefly readable by
    others, and re-applied to a file that already existed with wider bits.
    """
    path = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        make_private_dir(parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    handle = os.open(path, flags, PRIVATE_FILE_MODE)
    _chmod_quietly(path, PRIVATE_FILE_MODE)
    with os.fdopen(handle, "wb") as stream:
        stream.write(text.encode(encoding))


# --------------------------------------------------------------------------
# The home folder is `~` in everything an analysis writes or prints
# --------------------------------------------------------------------------
_NAME_CHAR = r"A-Za-z0-9._-"


@functools.lru_cache(maxsize=8)
def home_patterns(home: str) -> Tuple["re.Pattern[str]", ...]:
    """Every spelling of the home folder that turns up in transcripts.

    The plain path with either separator, its JSON-escaped form, a file URI
    (`/c:/Users/...`, `c%3A/Users/...`) and, right after a `projects` folder,
    Claude Code's encoded project-folder name (`projects/-Users-name-...`).
    Case is ignored, as it is on Windows and macOS.

    Each pattern STARTS with a character the regex engine can scan for (a
    separator, a drive letter, the encoded name) and checks what precedes it
    with a lookbehind afterwards. A leading lookbehind made the engine try
    every position of a multi-megabyte report, several times slower.
    """
    parts = [part for part in re.split(r"[\\/]+", home) if part]
    drive = re.match(r"^([A-Za-z]):$", parts[0]) if parts else None
    if not parts or (drive and len(parts) < 2):
        return ()                                 # "/" or "C:\\": nothing to hide
    sep = r"(?:\\\\|[\\/])"                       # \, / or a JSON-escaped \\
    if drive:
        letter = "[%s%s]" % (drive.group(1).lower(), drive.group(1).upper())
        path = (letter + r"(?<![%s]%s)(?::|%%3A)" % (_NAME_CHAR, letter)
                + "".join(sep + re.escape(part) for part in parts[1:]))
    else:
        # the first separator, then: no name character right before it
        path = (r"(?:\\\\(?<![%s]\\\\)|[\\/](?<![%s][\\/]))" % (_NAME_CHAR, _NAME_CHAR)
                + re.escape(parts[0]) + "".join(sep + re.escape(part) for part in parts[1:]))
    encoded = re.escape(re.sub(r"[^A-Za-z0-9]", "-", home.rstrip("\\/")))
    in_projects = "|".join(r"(?<=%s%s)" % (folder, encoded)
                           for folder in (r"projects/", r"projects\\", r"projects\\\\"))
    return (re.compile(path + r"(?![%s])" % _NAME_CHAR, re.IGNORECASE),
            re.compile(encoded + r"(?:%s)(?![A-Za-z0-9])" % in_projects, re.IGNORECASE))


def replace_home(text: str, home: str) -> str:
    """`text` with every spelling of `home` replaced by `~`."""
    for pattern in home_patterns(home):
        text = pattern.sub("~", text)
    return text


def home_relative(text: Any) -> Any:
    """Show the home folder as `~` in a path, or anywhere in a block of text.

    Applied to everything an analysis writes or prints, so a shared report or
    screenshot does not carry the account name. Ingestion keeps full paths.
    """
    if not isinstance(text, str) or not text:
        return text
    for home in _home_spellings(os.path.expanduser("~")):
        text = replace_home(text, home)
    return text


@functools.lru_cache(maxsize=8)
def _home_spellings(home: str) -> Tuple[str, ...]:
    """The home folder as set, and as resolved (`Path.resolve()` follows links)."""
    real = os.path.realpath(home)
    return (home,) if real == home else (home, real)


def project_folder_label(name: str) -> str:
    """A Claude Code project-folder name (`-Users-name-src-app`) with home as `~`."""
    return home_relative("projects/" + name)[len("projects/"):]


def write_output(path, text: str) -> None:
    """An analysis output file: owner-only, with the home folder shown as `~`."""
    write_private(path, home_relative(text))


class HomeRelativeStream:
    """A text stream that shows the home folder as `~`, like every output file."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        return self._stream.write(home_relative(text))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def utf8_stdio() -> None:
    """Write UTF-8 to stdout and stderr, whatever the locale says.

    On Windows a redirected or piped stream uses the locale code page
    (cp1252), which cannot encode the → ≥ × ✓ this skill prints: a report
    redirected to a file would crash, and a reader decoding UTF-8 would choke.
    Called first in every script's main().
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):          # a real text stream, not a StringIO
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):             # detached or already closed
                pass


def home_relative_stdout():
    """`with home_relative_stdout(): ...` -- what an analysis prints shows `~`."""
    return contextlib.redirect_stdout(HomeRelativeStream(sys.stdout))


# --------------------------------------------------------------------------
# Price table -- ONE table, no silent fallback
# --------------------------------------------------------------------------
# Below this share of priceable tokens, a dollar total is not reported without
# the coverage caveat printed next to it. 0.80 is the threshold
# claude_cache_audit.py already used for its confidence downgrade; reused here
# so the whole skill has one number.
COVERAGE_FLOOR = 0.80

# `<synthetic>` is not a model. Claude Code stamps it on records it injects
# itself (interrupt notices, hook output, meta messages): no published rate, no
# effort level, no reasoning volume. It is excluded from the coverage
# denominator -- counting it as "unpriced" would make full coverage impossible.
NON_MODELS = frozenset({"<synthetic>", "", "?", "none", "null"})


class PriceTableError(RuntimeError):
    pass


def _normalise(model: str) -> str:
    """Strip Claude Code's context-window markers from a model id.

    Claude Code history carries `claude-opus-5[1m]`, `claude-opus-4-8[1m]`
    and `claude-opus-4-8-1m` alongside the bare ids. These are the same model
    at (as of 2026-09-06) the same published rate; the marker records the
    1M-context selection, not a price tier.
    """
    m = (model or "").strip()
    if m.endswith("[1m]"):
        m = m[:-4]
    if m.endswith("-1m"):
        m = m[:-3]
    return m


def is_priceable(model: str) -> bool:
    """False for records that are not a model call at all."""
    return _normalise(model).lower() not in NON_MODELS


def load_prices(path: Optional[str] = None, table_id: Optional[str] = None) -> Dict:
    """Load THE price table. No built-in table is merged in.

    Returns a dict with `models` (exact ids), `rules` (regex -> rate) and
    `table_id`. A caller that wants a different table passes `--pricing-file`.
    """
    path = path or DEFAULT_PRICING_FILE
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    if "tables" in doc:
        selected = table_id or doc.get("default_table_id")
        if selected not in doc.get("tables", {}):
            raise PriceTableError("pricing table %r not found in %s" % (selected, path))
        table = dict(doc["tables"][selected])
        table["table_id"] = selected
    else:
        table = dict(doc)
        table.setdefault("table_id", os.path.basename(path))
    if table.get("currency", "USD") != "USD":
        raise PriceTableError("only USD pricing tables are supported")
    models = table.get("models")
    if not isinstance(models, dict):
        raise PriceTableError("pricing table must contain a models object")
    return {
        "table_id": table["table_id"],
        "description": table.get("description", ""),
        "models": {k: (float(v["input"]), float(v["output"]))
                   for k, v in models.items()
                   if v.get("input") is not None and v.get("output") is not None},
        "rules": [(re.compile(r["pattern"]), (float(r["input"]), float(r["output"])))
                  for r in table.get("model_rules", [])
                  if r.get("input") is not None and r.get("output") is not None],
        "path": path,
    }


def price_for(model: str, prices: Dict) -> Optional[Tuple[float, float]]:
    """($/M input, $/M output) for a model id, or None if the table prices it not.

    Exact id first, then the table's regex rules in order. Longest exact key
    wins on a prefix tie so `claude-fable-5-1` never resolves as
    `claude-fable-5`.
    """
    if not is_priceable(model):
        return None
    name = _normalise(model)
    models = prices.get("models", {})
    if name in models:
        return models[name]
    for key in sorted(models, key=len, reverse=True):
        if name.startswith(key):
            return models[key]
    for pattern, rate in prices.get("rules", []):
        if pattern.search(name):
            return rate
    return None


class Coverage:
    """Token-weighted price coverage, computed the same way in every script."""

    def __init__(self) -> None:
        self.priced_tokens = 0
        self.total_tokens = 0
        self.unpriced_models: Dict[str, int] = {}

    def add(self, model: str, tokens: int, priced: bool) -> None:
        if not is_priceable(model):
            return                      # not a model call: out of the denominator
        self.total_tokens += tokens
        if priced:
            self.priced_tokens += tokens
        else:
            self.unpriced_models[model] = self.unpriced_models.get(model, 0) + tokens

    @property
    def fraction(self) -> float:
        return self.priced_tokens / self.total_tokens if self.total_tokens else 0.0

    @property
    def ok(self) -> bool:
        return self.fraction >= COVERAGE_FLOOR

    def label(self) -> str:
        """The string every script prints. Same words, same rounding."""
        return "coverage %.0f %%" % (100 * self.fraction)

    def caveat(self) -> str:
        """Empty when coverage is fine; a plain warning line when it is not."""
        if self.ok:
            return ""
        worst = sorted(self.unpriced_models.items(), key=lambda kv: -kv[1])[:3]
        names = ", ".join("%s (%.1fM tok)" % (m, t / 1e6) for m, t in worst)
        return coverage_caveat(self.fraction, names or "n/a")

    def money(self, usd: float) -> str:
        """A dollar figure that CANNOT be printed without its coverage."""
        return "$%s (%s)" % (format(usd, ",.2f"), self.label())


def coverage_caveat(fraction: float, unpriced: Optional[str] = None) -> str:
    """The one wording, in every script, for a dollar total below COVERAGE_FLOOR."""
    text = ("Note: dollar totals cover only %.0f %% of tokens (the floor is %.0f %%), so "
            "read the token counts, not the dollars." % (100 * fraction, 100 * COVERAGE_FLOOR))
    return text + (" Unpriced: %s" % unpriced if unpriced else "")


def record_effort(record: Dict) -> Optional[str]:
    """The effort level on an assistant record, or None.

    Newer CLI versions write it as `effort`, some versions as `perTurnEffort`;
    both are read. Absent on older versions -- never guessed.
    """
    value = record.get("effort") or record.get("perTurnEffort")
    return str(value) if value else None


def format_money(usd: float, coverage: "Coverage") -> str:
    """Module-level twin of Coverage.money, for call sites that read better."""
    return coverage.money(usd)


# --------------------------------------------------------------------------
# THE ONE JSONL EVENT READER
#
# Every analysis in this skill had to rediscover the same fact about Claude
# Code's transcript format: a multi-block assistant message is written as one
# JSONL ROW PER CONTENT BLOCK, with the IDENTICAL `usage` object repeated on
# every one of them. Four of the five got it right; `tool_bloat.collect`
# deduplicated on the row `uuid` instead of the message id and therefore
# counted about twice as many "turns" as there were responses -- a ~2x
# inflation of every replay horizon it computes. Its selftest passed because
# it tested already-constructed analysis inputs rather than this path.
#
# Two rules, in one place:
#   1. A RESPONSE is identified by (sessionId, message.id) -- falling back to
#      requestId, then uuid. Usage is taken ONCE per response.
#   2. Content blocks are MERGED, not dropped, and the merge is IDEMPOTENT:
#      replaying an identical row changes nothing. Dropping repeat rows loses
#      their tool calls; a merge that is not idempotent doubles tool counts
#      on a replayed row and can push thinking share above 1.
# --------------------------------------------------------------------------
def response_key(record: Dict) -> Tuple[str, str]:
    """The identity of an assistant RESPONSE. Never the row's identity."""
    message = record.get("message") or {}
    return (str(record.get("sessionId") or ""),
            str(message.get("id") or record.get("requestId") or record.get("uuid") or ""))


def lane_key(record: Dict) -> Tuple[str, str]:
    """(session, agent) -- the MAIN conversation and each child agent apart.

    Child-agent records live in `<session>/subagents/**.jsonl` under the SAME
    sessionId and carry `agentId` / `isSidechain`.
    """
    return (str(record.get("sessionId") or "?"),
            str(record.get("agentId") or ("side" if record.get("isSidechain") else "main")))


def block_key(block: Dict) -> str:
    """Identity of one content block, for the idempotent merge."""
    kind = str(block.get("type") or "?")
    if block.get("id"):
        return "%s:%s" % (kind, block["id"])
    body = block.get("thinking") if kind == "thinking" else block.get("text")
    if body is None:
        body = json.dumps(block, sort_keys=True, default=str)
    return "%s:%s" % (kind, hash_text(str(body)))


def hash_text(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


class Response:
    """One assistant API response, however many JSONL rows carried it."""

    __slots__ = ("key", "session_id", "lane", "agent", "ts", "model", "effort",
                 "usage", "blocks", "cwd", "rows")

    def __init__(self, record: Dict, ts=None):
        message = record.get("message") or {}
        self.key = response_key(record)
        self.session_id = str(record.get("sessionId") or "")
        self.lane = lane_key(record)
        self.agent = self.lane[1] if self.lane[1] != "main" else None
        self.ts = ts
        self.model = str(message.get("model") or "unknown")
        self.effort = record_effort(record)
        self.usage = dict(message.get("usage") or {})
        self.blocks: Dict[str, Dict] = {}
        self.cwd = record.get("cwd")
        self.rows = 0
        self.merge(record)

    def merge(self, record: Dict) -> "Response":
        """Fold another row of the SAME response in. Idempotent."""
        self.rows += 1
        message = record.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict):
                self.blocks.setdefault(block_key(block), block)
        return self

    def content(self) -> List[Dict]:
        return list(self.blocks.values())


class EventReader:
    """Feed it assistant records; it yields one merged Response per response.

    `add()` returns (response, is_new). A row for a response already seen
    merges its blocks and returns is_new=False, so a caller can count
    responses and rows separately without either being wrong.
    """

    def __init__(self) -> None:
        self.responses: Dict[Tuple[str, str], Response] = {}
        self.rows = 0
        self.duplicate_rows = 0

    def add(self, record: Dict, ts=None) -> Tuple[Optional["Response"], bool]:
        message = record.get("message") or {}
        if not isinstance(message.get("usage"), dict):
            return None, False
        self.rows += 1
        key = response_key(record)
        existing = self.responses.get(key)
        if existing is not None:
            self.duplicate_rows += 1
            existing.merge(record)
            return existing, False
        response = Response(record, ts)
        self.responses[key] = response
        return response, True

    def by_lane(self) -> Dict[Tuple[str, str], List["Response"]]:
        lanes: Dict[Tuple[str, str], List[Response]] = {}
        for response in self.responses.values():
            lanes.setdefault(response.lane, []).append(response)
        for rows in lanes.values():
            rows.sort(key=lambda r: (r.ts is None, r.ts))
        return lanes
