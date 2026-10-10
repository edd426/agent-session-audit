#!/usr/bin/env python3
"""Startup cost and repeat startup, from local agent history.

Third analyzer in the agent-session-audit skill, beside claude_cache_audit.py
(*which cache lifetime*) and lane_profile.py (*where did the usage go*). This
one answers two more:

  * how many tokens a session spends before its first edit -- its startup; and
  * which work, picked up again across sessions and tools, pays that startup
    over and over.

Offline, read-only, stdlib-only (Python 3.9+), macOS / Windows / Linux. It
never invokes Claude and never runs a paid experiment.

FULL FIDELITY BY DEFAULT. The local report shows real working directories,
file paths, session titles, first prompt lines and one-line tool arguments,
because it is for the owner of the data. `--redact` is OPT-IN and exists only
for a report you will share; the skill's Data-boundary note governs what goes
to an agent or into a repo (aggregates), not what the local report is allowed
to show you.

See references/orientation-and-threads.md for the metric definitions, the
boundary heuristic's known failure modes, and how to run it on Windows.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import glob
import hashlib
import html
import json
import math
import os
import re
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_common
from audit_common import CwdFilter, Coverage, add_filter_args, home_relative, write_output

# --------------------------------------------------------------------------
# Pricing (same schema as claude_cache_audit.py / lane_profile.py).
# ESTIMATED COST AT LIST API RATES: an ATTRIBUTION WEIGHT, never an invoice.
# --------------------------------------------------------------------------
# There is deliberately NO built-in price table here any more. This module used
# to merge one over whatever --pricing-file said, which is how it reported 100 %
# price coverage for a file that claude_cache_audit.py scored at 15 % on the
# same history. The table lives in
# references/pricing-public-example.json and is loaded by audit_common.
# Set by _load_all() so the report's price appendix renders the table that
# was actually loaded, not a copy that can drift away from it.
_ACTIVE_PRICES: Dict[str, Any] = {"models": {}, "rules": [], "table_id": "(not loaded)"}
CACHE_READ_MULT = 0.1
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0

# --------------------------------------------------------------------------
# The first-edit boundary.
#
# PUBLISHED PRECEDENT, so the metric is a comparator and not an invented one:
# RepoMirage (arXiv 2605.26177) scores agents on the "share of steps before the
# first edit operation"; Parnin & Rugaber (2011) measured the human version and
# found only 7% of programming sessions involve NO navigation before editing.
# So ~93% of human sessions have a non-zero orientation share too: this number
# is a contrast BETWEEN arms or between sessions, never an absolute waste
# figure. See references/orientation-and-threads.md.
#
# The mutating set is Anthropic's own accepted-edit list -- the first-party
# "suggestion accept rate" counts Edit, Write and NotebookEdit
# (https://code.claude.com/docs/en/analytics) -- extended with MultiEdit and
# with Bash commands matching a named write-ish pattern. Stated here so the
# "why those tools?" review question is answered before it is asked.
# --------------------------------------------------------------------------
MUTATING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead", "LS"}

# The harness's own refusal string, verbatim.
DENIAL_MARK = "has been denied because Claude Code is running in don't ask mode"
FAILUREISH_RE = re.compile(
    r"(\bTraceback \(most recent call last\)|\bFAILED\b|\bFAIL\b|\bERROR\b|"
    r"\bError:|\bexit(?: code|status) [1-9]|\bcommand not found\b|"
    r"\bAssertionError\b|\bnon-zero exit\b)")

# Helicone-style session PATH stages (docs.helicone.ai/features/sessions), with
# the stage vocabulary from Coherence Collapse (arXiv 2603.24631). A session is
# a sequence of these, derived from the tool order -- it is what panel (c)
# stacks instead of a bare orientation/work split.
# `unclassified` is a real fifth value: a text-only
# response after the boundary was being scored `verify`, which it is not.
STAGE_ORDER = ("orient", "explore", "mutate", "verify", "unclassified")
VERIFY_RE = re.compile(
    r"\b(test|pytest|jest|npm\s+(test|run\s+lint)|make\s+(test|check)|"
    r"terraform\s+(plan|validate|fmt)|tflint|kubectl\s+(get|describe|diff)|"
    r"git\s+(diff|status|log)|lint|ruff|mypy|shellcheck|az\s+\S+\s+show)\b")

# --------------------------------------------------------------------------
# THE SHELL MUTATION VERB TABLE -- one place, explicit.
#
# The old table was a list of regexes matched against the WHOLE command
# string. It therefore counted `git log --grep="rm"`, `printf "please touch
# nothing"`, `python3 -c "print(1)"` and every heredoc as mutations, because a
# quoted argument, a heredoc body and a flag value all look like shell to a
# regex. A review reproduced all four. The replacement TOKENISES the command
# (see `shell_segments`), drops heredoc bodies and the CONTENTS of quoted
# strings, and matches a verb only at COMMAND POSITION -- the first word of a
# segment, where a segment starts at the line start or after ; && || | & $( `.
#
# Two patterns were REMOVED rather than repaired:
#   * `python -c` -- running Python is not a mutation. `python3 -c "..." > f`
#     is caught by the redirect rule; `python3 -c "print(1)"` is a read.
#   * `heredoc`   -- a heredoc is a way of passing DATA, not a write.
#     `cat <<EOF > f` is caught by the redirect rule; `python3 - <<EOF` that
#     only prints is not a mutation.
# Both were boundary reasons on real sessions.
#
# Columns: reason, verb, subcommands (None = the verb alone is enough),
# flag test (None = no flag needed).
# --------------------------------------------------------------------------
GIT_WRITE_SUBS = ("commit", "add", "apply", "checkout", "switch", "merge", "rebase",
                  "reset", "push", "stash", "rm", "mv", "tag", "restore", "revert",
                  "cherry-pick", "clean", "init", "clone", "am")
TF_WRITE_SUBS = ("apply", "import", "taint", "untaint", "destroy")
TF_STATE_WRITE_SUBS = ("mv", "rm", "push", "replace-provider")
K8S_WRITE_SUBS = ("apply", "create", "delete", "patch", "edit", "scale", "replace",
                  "annotate", "label", "rollout", "cordon", "drain")
AZ_WRITE_SUBS = ("create", "update", "delete", "deploy", "set", "add", "remove",
                 "start", "stop", "restart", "purge")
PKG_INSTALL_SUBS = ("install", "add", "i", "uninstall", "remove", "update", "upgrade")
# Wrappers that run another command; the real verb is the next word.
COMMAND_WRAPPERS = ("sudo", "env", "nohup", "time", "command", "xargs", "nice",
                    "stdbuf", "doas", "exec", "builtin")
# Shell keywords that can stand where a command word does (`for f in *; do rm
# $f; done`). Skipped so the verb behind them is still seen.
SHELL_KEYWORDS = ("do", "then", "else", "elif", "{", "(", "!", "while", "until",
                  "if", "for", "case", "esac", "fi", "done", "in", "function")
# A redirection to one of these writes nothing anyone can read back.
NULL_SINKS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/1",
              "/dev/fd/2")


def _has_flag_i(argv: Sequence[str]) -> bool:
    """`sed -i` / `sed -i.bak` / `sed --in-place`, but not `sed -n`."""
    for token in argv[1:]:
        if token == "--in-place" or token.startswith("--in-place="):
            return True
        if token.startswith("-") and not token.startswith("--") and "i" in token.split(".")[0]:
            return True
    return False


BASH_MUTATE_VERBS: List[Tuple[str, str, Optional[Sequence[str]], Optional[Any]]] = [
    ("rm", "rm", None, None),
    ("mv", "mv", None, None),
    ("cp", "cp", None, None),
    ("mkdir", "mkdir", None, None),
    ("rmdir", "rmdir", None, None),
    ("touch", "touch", None, None),
    ("chmod", "chmod", None, None),
    ("chown", "chown", None, None),
    ("ln", "ln", None, None),
    ("tee", "tee", None, None),
    ("dd", "dd", None, None),
    ("truncate", "truncate", None, None),
    ("install", "install", None, None),
    ("patch", "patch", None, None),
    ("sed -i", "sed", None, _has_flag_i),
    ("perl -i", "perl", None, _has_flag_i),
    ("git commit", "git", GIT_WRITE_SUBS, None),
    ("terraform apply", "terraform", TF_WRITE_SUBS, None),
    # `terraform state list/show/pull` are reads; only these subcommands write.
    ("terraform state", "terraform", TF_STATE_WRITE_SUBS,
     lambda words: "state" in words),
    ("kubectl apply", "kubectl", K8S_WRITE_SUBS, None),
    ("az deploy", "az", AZ_WRITE_SUBS, None),
    ("package install", "npm", PKG_INSTALL_SUBS, None),
    ("package install", "yarn", PKG_INSTALL_SUBS, None),
    ("package install", "pnpm", PKG_INSTALL_SUBS, None),
    ("package install", "pip", PKG_INSTALL_SUBS, None),
    ("package install", "pip3", PKG_INSTALL_SUBS, None),
    ("package install", "uv", PKG_INSTALL_SUBS, None),
    ("package install", "cargo", PKG_INSTALL_SUBS, None),
    ("package install", "go", PKG_INSTALL_SUBS, None),
    ("package install", "brew", PKG_INSTALL_SUBS, None),
]

_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_REDIRECT_OPS = (">", ">>", ">|", "&>", "&>>")

# A note/handoff file: opening one of these early is the cheap re-orientation.
#
# SPLIT, because a pooled "opened notes" column can point the WRONG WAY.
# Reading a project's README is orientation WORK -- it is what you do when you
# do not know where you are -- whereas reading a HANDOFF is the cheap
# substitute for that work. Pooled, sessions that read a note can show a
# HIGHER orientation share, and the lever looks like the cost. So:
#   handoff_opened_by_turn_2  -- the mediator. Panels split on THIS.
#   readme_opened_by_turn_2   -- kept as a column, never as the mediator.
# ANCHORED, WHOLE-WORD. An unanchored substring search lets `PLAN` match
# `planet.py` or `planner.md`.
# A basename is split into words on non-alphanumerics and camel-case
# boundaries, and one of those words must BE a keyword.
HANDOFF_WORDS = frozenset((
    "handoff", "handover", "note", "notes", "status", "claude", "todo",
    "plan", "state", "context",
))
README_WORDS = frozenset(("readme",))
NOTES_WORDS = HANDOFF_WORDS | README_WORDS
# A handover note is prose. A `.json` fixture or a `.js` module whose name
# happens to contain "status" or "plan" is not one: `post-status.js` and
# `plan.json` do not count.
NOTE_EXTENSIONS = frozenset((
    "", ".md", ".markdown", ".txt", ".rst", ".org", ".adoc", ".text", ".mdx",
))
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def basename_words(name: str) -> List[str]:
    stem = os.path.basename(str(name).rstrip("/\\"))
    stem = stem[:-3] if stem.lower().endswith(".md") else os.path.splitext(stem)[0]
    return [w.lower() for w in _WORD_SPLIT_RE.split(stem) if w]


def note_basename_kind(name: str, words: "frozenset[str]") -> bool:
    """Is this basename a note-shaped file of the given kind?"""
    base = os.path.basename(str(name).rstrip("/\\"))
    if not base:
        return False
    extension = os.path.splitext(base)[1].lower()
    if extension not in NOTE_EXTENSIONS:
        return False
    return bool(set(basename_words(base)) & words)


class _WordMatcher:
    """Kept API-compatible with the old `re.Pattern` call sites (`.search`)."""

    def __init__(self, words: "frozenset[str]", label: str):
        self.words = words
        self.label = label

    def search(self, text: str):
        return self if note_basename_kind(text, self.words) else None


HANDOFF_BASENAME_RE = _WordMatcher(HANDOFF_WORDS, "handoff")
README_BASENAME_RE = _WordMatcher(README_WORDS, "readme")
# Kept for anything that still wants the pooled question, and so the change is
# auditable rather than silent.
NOTES_BASENAME_RE = _WordMatcher(NOTES_WORDS, "notes")
NOTES_TURN_WINDOW = 2  # "within the first 2 assistant responses"
# Searching for a note is not opening one. Its own column.
SEARCH_TOOLS = {"Glob", "Grep", "LS"}
# At most this many predecessor-written basenames count as "the handoff this
# thread actually uses".
PREDECESSOR_HANDOFF_CAP = 3

# Bash commands that are really a read of a file.
BASH_READ_RE = re.compile(
    r"\b(cat|head|tail|less|more|bat|sed\s+-n|grep|rg|awk|wc|jq|open)\b")

# Strategy classification, read from the tool histogram (Edit+Write <= 50
# with Bash-led work = scripted).
SCRIPTED_EDIT_MAX = 50

PATH_TOKEN_RE = re.compile(r"[\w./\\~+-]*[\w-]+\.[A-Za-z0-9]{1,8}\b")
SECRETISH_RE = re.compile(
    r"(https?://\S+)"
    r"|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
    r"|(\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b)"
    r"|(\b(sk|pk|ghp|gho|ghs|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b)"
    r"|(\bAKIA[0-9A-Z]{16}\b)"
    r"|(\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b)"
)


# --------------------------------------------------------------------------
# Small shared helpers (kept byte-compatible in behaviour with the siblings).
# --------------------------------------------------------------------------
def stable_hash(value: Any) -> str:
    """Same 12-hex digest claude_cache_audit.py uses for cwd/branch."""
    return hashlib.sha256(str(value or "unknown").encode("utf-8", "replace")).hexdigest()[:12]


def numeric(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e11:          # milliseconds
            number /= 1000.0
        try:
            return dt.datetime.fromtimestamp(number, dt.timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        return parse_timestamp(int(text))
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def parse_bound(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)
    parsed = parse_timestamp(text)
    if parsed is None:
        raise ValueError("invalid date/time: %s" % value)
    return parsed


def iso(moment: Optional[dt.datetime]) -> Optional[str]:
    return moment.isoformat().replace("+00:00", "Z") if moment else None


def load_prices(path: Optional[str] = None) -> Dict[str, Any]:
    """THE price table (audit_common). No built-in table is merged in."""
    return audit_common.load_prices(path)


def price_for(model: str, prices: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """($/M in, $/M out) for a model, or None. Shared matcher: exact id first
    (longest key wins, so claude-fable-5-1 never resolves as claude-fable-5),
    then the table's regex rules, with [1m]/-1m context markers stripped."""
    return audit_common.price_for(model, prices)


def price_rows(prices: Dict[str, Any]) -> List[Tuple[str, Tuple[float, float]]]:
    """The rows a report's price appendix renders -- from the LOADED table,
    never from a hardcoded copy."""
    return sorted(prices.get("models", {}).items())


# `<synthetic>` is NOT a model. Claude Code stamps it on records it injects
# itself -- interrupt notices, hook output, meta messages -- so it has no
# published rate, no effort level and no reasoning volume. It appeared as a
# column in an early report and a reader took it for something to interpret.
# It is filtered out of every user-facing table. The
# tokens stay in the TOTALS, because they were really spent; what is dropped
# is the pretence that they belong to a model you could have chosen.
SYNTHETIC_MODELS = {"<synthetic>", "synthetic", "<none>"}


def real_models(models: Sequence[str]) -> List[str]:
    return [m for m in models if str(m).strip().lower() not in SYNTHETIC_MODELS]


def primary_model(session: "Session") -> str:
    return (real_models(session.models) or ["unknown"])[0]


def compact_number(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return "%.1fB" % (value / 1_000_000_000)
    if absolute >= 1_000_000:
        return "%.1fM" % (value / 1_000_000)
    if absolute >= 1_000:
        return "%.1fK" % (value / 1_000)
    return "%.0f" % value


def median_or_none(values: Sequence[float]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return statistics.median(clean) if clean else None


def one_line(text: Optional[str], limit: int = 160) -> Optional[str]:
    if text is None:
        return None
    flat = " ".join(str(text).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _n(count: int, singular: str, plural: Optional[str] = None) -> str:
    """'1 session', '3 sessions': a count with its noun, never "session(s)"."""
    return "%d %s" % (count, singular if count == 1 else (plural or singular + "s"))


def hours_text(hours: Optional[float]) -> str:
    """'1 hour', '72 hours', '1.5 hours', '30 minutes': never '1.00 h'."""
    if hours is None:
        return "—"
    if 0 < hours < 1:
        return _n(int(round(hours * 60)), "minute")
    value = round(hours, 1)
    return _n(int(value), "hour") if value == int(value) else "%.1f hours" % value


def days_text(days: float) -> str:
    """'3 days', '2.5 days', '1 day'."""
    value = round(days, 1)
    return _n(int(value), "day") if value == int(value) else "%.1f days" % value


def short_path(path: Any, limit: int) -> str:
    """A path cut to fit `limit` characters at a SEPARATOR, never mid-name.

    Keeps the trailing segments that fit behind a leading "…/", so a long
    working directory reads `…/Analysis/report_builder` rather than
    `sis/Analysis/report_builder`. A last segment too long to fit on its own
    is cut from the left, still behind "…". Windows paths keep their backslash.
    """
    text = "" if path is None else home_relative(str(path))
    if len(text) <= limit:
        return text
    sep = "\\" if ("\\" in text and "/" not in text) else "/"
    parts = [part for part in re.split(r"[\\/]+", text) if part]
    kept: List[str] = []
    for part in reversed(parts):
        if len("…" + sep + sep.join([part] + kept)) > limit:
            break
        kept.insert(0, part)
    if kept:
        return "…" + sep + sep.join(kept)
    last = parts[-1] if parts else text
    return "…" + last[-(limit - 1):] if limit > 1 else "…"


def folder_name(cwd: Any, default: str = "unknown") -> str:
    """The last folder of a working directory; the home folder itself is `~`."""
    text = str(cwd or default).rstrip("/\\")
    if home_relative(text) == "~":
        return "~"
    return os.path.basename(text) or str(cwd or default)


# --------------------------------------------------------------------------
# The one interface every ingest adapter produces.
# --------------------------------------------------------------------------
class ToolCall:
    __slots__ = ("turn", "name", "argline", "paths", "mutating", "mutate_reason",
                 "use_id")

    def __init__(self, turn: int, name: str, argline: Optional[str],
                 paths: Sequence[str], mutating: bool, mutate_reason: Optional[str],
                 use_id: Optional[str] = None):
        self.turn = turn
        self.name = name
        self.argline = argline
        self.paths = list(paths)
        self.mutating = mutating
        self.mutate_reason = mutate_reason
        # The `tool_use` block id, so a tool_result can say whether this call
        # SUCCEEDED (attempt vs success). None on stores that do not
        # carry one -- there every call reads as successful, and the reference
        # says so.
        self.use_id = use_id


class Turn:
    """One assistant response: the unit of 'turn' everywhere in this module."""
    __slots__ = ("index", "ts", "model", "effort", "inp", "out", "cread", "cw5", "cw1",
                 "thinking_tokens", "thinking_chars", "tools", "agent", "main_index",
                 "seen_blocks")

    def __init__(self, index: int, ts: Optional[dt.datetime], model: str,
                 effort: Optional[str] = None, agent: Optional[str] = None):
        self.index = index
        self.ts = ts
        self.model = model
        self.effort = effort
        # WHICH AGENT produced this response. None = the
        # MAIN conversation; anything else is a child agent (a Task/Agent
        # subagent or a workflow agent), whose records Claude Code writes to
        # <session>/subagents/**.jsonl under the SAME sessionId. Merging them
        # into one timeline made a child's early edit end the parent's
        # orientation. Boundary and first-two-responses are computed on the
        # main conversation only; child spend is rolled up in its own columns.
        self.agent = agent
        # position in the MAIN-only sequence; None on a child response.
        self.main_index: Optional[int] = None
        self.inp = self.out = self.cread = self.cw5 = self.cw1 = 0
        self.thinking_tokens = 0
        self.thinking_chars = 0
        self.tools: List[ToolCall] = []
        # content blocks already folded in, for the idempotent merge
        self.seen_blocks: set = set()

    @property
    def tokens(self) -> int:
        """All context cost for the turn, not just billed fresh input.

        Re-reading a carried context through the cache is still re-orientation
        work, so cache_read and cache_creation count.
        """
        return self.inp + self.out + self.cread + self.cw5 + self.cw1


class Session:
    """A normalised session record from ANY store."""

    def __init__(self, source: str, session_id: str, path: str):
        self.source = source
        self.session_id = session_id
        self.path = path
        self.cwd: Optional[str] = None
        self.title: Optional[str] = None
        self.first_prompt: Optional[str] = None
        self.turns: List[Turn] = []
        self.models: List[str] = []
        self.version: Optional[str] = None
        self.git_branch: Optional[str] = None
        self.meter_readings: List[Dict[str, Any]] = []
        self.duplicate_rows = 0
        self.unparsed_rows = 0
        self.file_mtime: Optional[dt.datetime] = None
        # Human turns, in order, including the first. The missed-shift
        # detector reads these for correction / re-send signals, and the
        # local (never redacted-export) report prints the last one before a
        # model switch so the human reason is readable. A store that does not
        # carry user text leaves this empty -- never a fabricated placeholder.
        self.user_messages: List[Dict[str, Any]] = []
        # Permission-layer refusals and failed tool results. A denied call
        # still cost the turn it was made in, so it lands inside
        # orientation_tokens; it is kept as a COVARIATE, not removed.
        self.denials = 0
        self.tool_failures = 0
        # tool_use ids whose RESULT came back an error or a refusal. Used only
        # for the attempt/success split at the boundary. Only the
        # UNAMBIGUOUS signals go in here -- `is_error` and the harness's own
        # refusal string -- never the loose FAILUREISH_RE, which matches the
        # word "ERROR" anywhere in a result body and would mark successful
        # edits as failures.
        self.failed_tool_ids: set = set()
        # COMPACTION EVENTS. Claude Code writes a
        # `{"type":"system","subtype":"compact_boundary"}` row carrying
        # `compactMetadata` -- trigger, preTokens, postTokens, durationMs --
        # at the moment the conversation is compacted. That row is a better
        # source than the `isCompactSummary` user row `compact_economics.py`
        # keys on, because it publishes the before/after sizes directly
        # (both `manual` and `auto` triggers occur in real transcripts).
        # The summarising API call itself
        # is NOT in the transcript with a usage object, so its price is
        # modelled and labelled as such, never mixed into an observed total.
        self.compactions: List[Dict[str, Any]] = []
        # filled by finalise()
        self.metrics: Dict[str, Any] = {}

    # -- derived ---------------------------------------------------------
    @property
    def start(self) -> Optional[dt.datetime]:
        stamps = [t.ts for t in self.turns if t.ts]
        return min(stamps) if stamps else None

    @property
    def end(self) -> Optional[dt.datetime]:
        stamps = [t.ts for t in self.turns if t.ts]
        return max(stamps) if stamps else None

    @property
    def duration_s(self) -> Optional[float]:
        if self.start and self.end:
            return (self.end - self.start).total_seconds()
        return None

    @property
    def cwd_hash(self) -> str:
        return stable_hash(self.cwd)

    @property
    def cwd_base(self) -> str:
        if not self.cwd:
            return "unknown"
        return folder_name(self.cwd)

    @property
    def files_touched(self) -> List[str]:
        seen: Dict[str, None] = {}
        for turn in self.turns:
            for call in turn.tools:
                for p in call.paths:
                    seen.setdefault(p, None)
        return list(seen)


# --------------------------------------------------------------------------
# Boundary / strategy / share computation -- shared by every adapter.
# --------------------------------------------------------------------------
def strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc BODIES. They are data the command is fed, not shell.

    `cat <<EOF\nrm -rf /\nEOF` mutates nothing; the old whole-string regexes
    read the body as commands.
    """
    lines = command.splitlines()
    kept: List[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in _HEREDOC_RE.finditer(line):
            delimiter = match.group(2)
            while index < len(lines) and lines[index].strip() != delimiter:
                index += 1
            index += 1              # drop the terminator too
    return "\n".join(kept)


def shell_segments(command: str) -> List[List[Tuple[str, bool]]]:
    """Tokenise a shell command into SEGMENTS of (token, quoted) pairs.

    A segment is one command position: the text after a line start, `;`,
    `&&`, `||`, `|`, `&`, or the opening of a `$( )` / backtick substitution.
    `quoted` is True for a token whose text came (wholly or partly) from
    inside quotes, so `git log --grep="rm"` cannot look like `rm`.
    Redirection operators are emitted as their own unquoted tokens.

    A hand-rolled lexer rather than `shlex`: shlex neither reports which
    tokens were quoted nor splits on shell operators, and both facts are the
    point here.
    """
    segments: List[List[Tuple[str, bool]]] = []
    current: List[Tuple[str, bool]] = []
    token = ""
    quoted = False
    started = False

    def flush() -> None:
        nonlocal token, quoted, started
        if started:
            current.append((token, quoted))
        token, quoted, started = "", False, False

    def close() -> None:
        nonlocal current
        flush()
        if current:
            segments.append(current)
        current = []

    text = strip_heredoc_bodies(command or "")
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            token += text[i + 1]
            quoted = started = True
            i += 2
            continue
        if ch == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            token += text[i + 1:j]
            quoted = started = True
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            body = ""
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    body += text[j + 1]
                    j += 2
                    continue
                body += text[j]
                j += 1
            # A `$( )` inside double quotes is still a command position.
            for inner in re.findall(r"\$\(([^()]*)\)", body):
                segments.extend(shell_segments(inner))
            token += re.sub(r"\$\([^()]*\)", "", body)
            quoted = started = True
            i = j + 1
            continue
        if text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            close()
            segments.extend(shell_segments(text[i + 2:j - 1]))
            i = j
            continue
        if ch == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            close()
            segments.extend(shell_segments(text[i + 1:j]))
            i = j + 1
            continue
        if ch in ">":
            # an fd prefix (`2>`) is part of the operator, not a word
            if started and token.isdigit():
                token, quoted, started = "", False, False
            flush()
            op = ">"
            i += 1
            if i < n and text[i] in ">|":
                op += text[i]
                i += 1
            current.append((op, False))
            if i < n and text[i] == "&":       # 2>&1 -- a dup, not a file
                j = i + 1
                while j < n and (text[j].isdigit() or text[j] == "-"):
                    j += 1
                current.append(("&" + text[i + 1:j], False))
                i = j
            continue
        if ch == "<":
            flush()
            j = i
            while j < n and text[j] == "<":
                j += 1
            current.append((text[i:j], False))
            i = j
            continue
        if ch in "&|;\n":
            close()
            j = i
            while j < n and text[j] in "&|;\n \t":
                j += 1
            i = j
            continue
        if ch in " \t":
            flush()
            i += 1
            continue
        token += ch
        started = True
        i += 1
    close()
    return segments


def _command_word(argv: Sequence[str]) -> List[str]:
    """Strip `VAR=x` prefixes and command wrappers; return the real argv."""
    words = list(argv)
    while words:
        head = words[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", head):
            words = words[1:]
            continue
        if head in COMMAND_WRAPPERS:
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
            continue
        if head in SHELL_KEYWORDS:
            words = words[1:]
            continue
        break
    return words


def _segment_mutation_reason(tokens: Sequence[Tuple[str, bool]]) -> Optional[str]:
    argv: List[str] = []
    index = 0
    redirect = False
    while index < len(tokens):
        text, is_quoted = tokens[index]
        if not is_quoted and text in _REDIRECT_OPS:
            target = tokens[index + 1][0] if index + 1 < len(tokens) else ""
            if target and not target.startswith("&") and target not in NULL_SINKS:
                redirect = True
            index += 2
            continue
        if not is_quoted and set(text) == {"<"}:
            index += 2                      # an input redirection and its source
            continue
        argv.append(text)
        index += 1
    words = _command_word(argv)
    if words:
        verb = os.path.basename(words[0])
        subs = {w for w in words[1:] if not w.startswith("-")}
        for reason, wanted, subcommands, flag_test in BASH_MUTATE_VERBS:
            if verb != wanted:
                continue
            if subcommands is not None and not (subs & set(subcommands)):
                continue
            if flag_test is not None and not flag_test(words):
                continue
            return reason
    return "redirect" if redirect else None


def bash_mutation_reason(command: str) -> Optional[str]:
    """Which named write-ish verb (if any) this shell command runs.

    Tokenised, not regex-scanned: heredoc bodies and the contents
    of quoted strings are dropped, and a verb counts only at COMMAND
    POSITION. So `git log --grep="rm"`, `printf "please touch nothing"` and
    `python3 -c "print(1)"` are reads, while `rm -rf build`, `sed -i s/a/b/ x`
    and `cat x > y` are writes. Remaining known limits, stated:
      * a verb run through an unrecognised wrapper (`make deploy`, a project
        script) is invisible -- an unfamiliar tool reads as read-only;
      * a redirection to a path is a write even if the command fails, which
        is why the ATTEMPT/SUCCESS split lives in `finalise`, not here.
    """
    if not command:
        return None
    for tokens in shell_segments(command):
        reason = _segment_mutation_reason(tokens)
        if reason:
            return reason
    return None


def paths_from_tool(name: str, payload: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    # A Grep/Glob/LS `path` is the folder searched, not a file read: counting
    # it would give every session in that folder a shared "file" and weaken
    # the shared-files join rule.
    keys = (() if name in SEARCH_TOOLS else
            ("file_path", "notebook_path", "path", "filePath", "uri", "target_file"))
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    if name == "Bash":
        command = str(payload.get("command") or "")
        for match in PATH_TOKEN_RE.findall(command):
            token = match.strip("'\"`,;:()[]{}")
            if len(token) > 2 and not token.startswith("-"):
                found.append(token)
            if len(found) >= 12:      # a shell one-liner is not a file list
                break
    for key in ("edits", "files"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for sub in ("file_path", "path"):
                        if isinstance(item.get(sub), str):
                            found.append(item[sub])
                elif isinstance(item, str):
                    found.append(item)
    out, seen = [], set()
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def tool_argline(name: str, payload: Dict[str, Any]) -> Optional[str]:
    """One line of tool argument, for interpretation. Full fidelity by default."""
    if not isinstance(payload, dict):
        return None
    if name == "Bash":
        return one_line(payload.get("command"))
    for key in ("file_path", "notebook_path", "path", "pattern", "query", "url",
                "prompt", "description", "command"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            prefix = "" if key in ("file_path", "notebook_path", "path") else key + "="
            return one_line(prefix + value)
    return None


def make_tool_call(turn_index: int, name: str, payload: Any,
                   use_id: Optional[str] = None) -> ToolCall:
    payload = payload if isinstance(payload, dict) else {}
    mutating = name in MUTATING_TOOLS
    reason = "tool:" + name if mutating else None
    if name == "Bash":
        reason = bash_mutation_reason(str(payload.get("command") or ""))
        mutating = reason is not None
    return ToolCall(turn_index, name, tool_argline(name, payload),
                    paths_from_tool(name, payload), mutating, reason, use_id)


def _read_candidates(call: ToolCall) -> List[str]:
    """The paths this call READ, if it was a read at all."""
    if call.name in READ_TOOLS:
        return list(call.paths) or ([call.argline] if call.argline else [])
    if call.name == "Bash":
        if not BASH_READ_RE.search(call.argline or ""):
            return []
        return list(call.paths)
    return []


def is_notes_open(call: ToolCall, pattern: Any = NOTES_BASENAME_RE,
                  extra: Optional[Sequence[str]] = None) -> Optional[str]:
    """Did this call OPEN a note/handoff-shaped file?

    "Open" means a READ-class tool that is not a search (a search gets its own
    column -- see `notes_search_target`) naming a note-shaped basename. The
    caller is responsible for skipping calls whose result was an error.

    `extra` carries basenames that are a handoff *for this thread* even though
    they do not match the pattern -- specifically, files the IMMEDIATE
    PREDECESSOR session wrote (it used to be every earlier session
    in the thread, which let ordinary source reads count as handoffs).
    """
    if call.name in SEARCH_TOOLS:
        return None
    extra_set = {os.path.basename(str(e)).lower() for e in (extra or [])}
    for candidate in _read_candidates(call):
        base = os.path.basename(str(candidate).rstrip("/\\"))
        if pattern.search(base) or base.lower() in extra_set:
            return candidate
    return None


def notes_search_target(call: ToolCall, pattern: Any = HANDOFF_BASENAME_RE) -> Optional[str]:
    """Did this call SEARCH for a note (Glob/Grep/LS) without opening one?

    Kept apart from `is_notes_open` because a `Glob **/HANDOFF.md` that
    matches nothing used to count as "the handoff was opened".
    """
    if call.name not in SEARCH_TOOLS:
        return None
    for candidate in list(call.paths) + ([call.argline] if call.argline else []):
        for part in re.split(r"[\s,]+", str(candidate)):
            base = os.path.basename(part.rstrip("/\\"))
            if pattern.search(base):
                return part
    return None


def stage_path(session: "Session", boundary: Optional[int]) -> List[Tuple[str, int]]:
    """Coarse `orient -> explore -> mutate -> verify` path, from the tool order.

    Helicone's session PATH (`Helicone-Session-Path`, e.g. `/abstract/outline`)
    made the case that a session's *shape* is more legible than its totals;
    Coherence Collapse (arXiv 2603.24631) supplies these four stage names.
    Runs are collapsed, so a session that mutates, verifies, then mutates
    again reads `mutate -> verify -> mutate` and the loop is visible.
    """
    path: List[Tuple[str, int]] = []
    # MAIN conversation only, and `boundary` indexes THIS list.
    for position, turn in enumerate(t for t in session.turns if t.agent is None):
        after = boundary is not None and position >= boundary
        if after and any(c.mutating for c in turn.tools):
            stage = "mutate"
        elif any(c.name == "Bash" and VERIFY_RE.search(c.argline or "") for c in turn.tools):
            stage = "verify"
        elif not turn.tools:
            # A text-only response after the boundary is NOT a verification:
            # it is unclassifiable from the tool order alone.
            stage = "orient" if not after else "unclassified"
        elif after:
            stage = "explore"
        else:
            stage = "orient" if position < 2 else "explore"
        if path and path[-1][0] == stage:
            path[-1] = (stage, path[-1][1] + 1)
        else:
            path.append((stage, 1))
    return path


def classify_strategy(edits: int, mutating_bash: int, other_bash: int) -> str:
    """scripted | by-hand | read-only | mixed, from the tool histogram.

    "Edit+Write <= 50 with Bash-led work" = scripted.
    """
    if edits == 0 and mutating_bash == 0:
        return "read-only"
    if edits > SCRIPTED_EDIT_MAX:
        return "by-hand"
    if edits and mutating_bash == 0:
        return "by-hand"
    if mutating_bash >= max(1, edits):
        return "scripted"
    return "mixed"


COMMITTED_RE = re.compile(r"\bgit\s+(commit|push)\b")


def classify_outcome(session: "Session") -> str:
    """success | failure | unknown -- the split panels (a) and (b) draw on.

    Local history has no pass/fail oracle, so this is INFERRED and the report
    says so:
    a session that ended by committing, or whose last verification call was
    not followed by a failure, reads `success`; one that ended on a failure
    signal reads `failure`; everything else is `unknown` and is drawn as its
    own band rather than being folded into either (an unknown
    value is never zero, and here it is never "success" either).
    """
    if session.tool_failures and not session.metrics.get("edit_calls"):
        return "failure"
    committed = any(call.name == "Bash" and COMMITTED_RE.search(call.argline or "")
                    for turn in session.turns for call in turn.tools)
    if committed:
        return "success"
    verified = any(call.name == "Bash" and VERIFY_RE.search(call.argline or "")
                   for turn in session.turns[-3:] for call in turn.tools)
    if verified and not session.tool_failures:
        return "success"
    if session.tool_failures:
        return "failure"
    return "unknown"


INTERROGATIVE_RE = re.compile(
    r"^\s*(?:what|why|how|where|when|which|who|whose|whom|is|are|was|were|do|does|did|can|could|"
    r"should|would|will|has|have|had|any|explain|tell me|walk me|help me understand)\b",
    re.IGNORECASE)


def question_first(session: "Session", boundary: Optional[int]) -> Optional[bool]:
    """Did this session OPEN with a question and only later get asked to change something?

    THE CONFOUND: some sessions ask an exploratory question first and only
    then follow up with a request to change something, which would skew the
    analysis. Such a session's pre-change
    tokens are answering a question, not re-learning — so its orientation
    share is not all re-orientation.

    True requires BOTH halves: the first human turn reads as a question, AND
    the first change lands after a LATER human turn. Either alone is not the
    pattern. A store that carries no user text returns None, never False.
    """
    messages = session.user_messages
    if not messages:
        return None
    text = (messages[0].get("text") or "").strip()
    if not text:
        return None
    asks = text.rstrip().endswith("?") or bool(INTERROGATIVE_RE.match(text))
    if not asks or boundary is None:
        return False
    # `boundary` indexes the MAIN-only sequence; user-message turns are
    # recorded against the full sequence, so map it back.
    main = [t for t in session.turns if t.agent is None]
    cutoff = main[boundary].index if boundary < len(main) else boundary
    return any(m.get("turn") is not None and m["turn"] <= cutoff
               for m in messages[1:])


def turn_cost_usd(turn: "Turn", prices: Dict[str, Any]) -> float:
    """List-rate cost of ONE response, from its own buckets at its own model.

    The single place a response is priced. Every orientation,
    thread-tax and stream-tax dollar figure now sums this over the responses
    it is about, instead of multiplying a session total by a token share.
    """
    rate = price_for(turn.model, prices)
    if not rate:
        return 0.0
    return (turn.cread * CACHE_READ_MULT * rate[0]
            + turn.cw5 * CACHE_WRITE_5M_MULT * rate[0]
            + turn.cw1 * CACHE_WRITE_1H_MULT * rate[0]
            + turn.inp * rate[0]
            + turn.out * rate[1]) / 1e6


def finalise(session: Session, prices: Dict[str, Tuple[float, float]]) -> None:
    """Compute every per-session metric. Idempotent."""
    turns = session.turns
    total_tokens = sum(t.tokens for t in turns)
    total_out = sum(t.out for t in turns)
    thinking_reported = sum(t.thinking_tokens for t in turns)
    thinking_est = sum(t.thinking_chars for t in turns) / 4.0
    thinking_tokens = thinking_reported or thinking_est
    thinking_source = "reported" if thinking_reported else (
        "estimated-from-chars" if thinking_est else "absent")

    # ---- the boundary: FIRST SUCCESSFUL MUTATION, on the MAIN conversation.
    #
    # Two rules, both of which move real sessions:
    #  1.  a mutating call whose RESULT came back an error (or a permission
    #      refusal) is an ATTEMPT, not the boundary. The boundary is the first
    #      call that actually changed something; attempts get their own column.
    #  2.  child-agent responses are excluded from the boundary search and
    #      from the first-two-responses window. They are a DIFFERENT agent's
    #      startup; a child's early edit used to end the parent's orientation.
    #      Their spend is rolled up separately below.
    #
    # DECISION: the boundary response
    # ITSELF is NOT orientation. The metric is "tokens before the first
    # successful mutation", and the response that performs the mutation is the
    # first response of the work. The counter-argument -- that the reasoning
    # which produced the edit was orientation -- is real, and the size of the
    # excluded response is published as `boundary_turn_tokens` so a reader can
    # see exactly how much sits on the line. Including it would let one
    # batched response (twenty reads and one edit) count wholly as
    # orientation, which is the larger error.
    main = [t for t in turns if t.agent is None]
    child = [t for t in turns if t.agent is not None]
    failed_ids = getattr(session, "failed_tool_ids", set()) or set()

    boundary: Optional[int] = None
    boundary_reason: Optional[str] = None
    boundary_tool: Optional[str] = None
    attempt_turn: Optional[int] = None
    failed_attempts = 0
    for position, turn in enumerate(main):
        for call in turn.tools:
            if not call.mutating:
                continue
            if attempt_turn is None:
                attempt_turn = position
            if call.use_id is not None and call.use_id in failed_ids:
                failed_attempts += 1
                continue
            if boundary is None:
                boundary = position
                boundary_reason = call.mutate_reason
                boundary_tool = call.argline

    if boundary is None:
        # NO OBSERVED BOUNDARY. The share is undefined and so are
        # the tokens: this session is neither 0 % nor 100 % re-orientation, it
        # is UNKNOWN, and every tax that used to silently add its whole token
        # count now excludes it and reports it on its own line.
        orientation_tokens = None
        orientation_turns = len(main)
        orientation_share = None       # never a fake boundary
        no_mutation = True
        boundary_turn_tokens = None
    else:
        orientation_tokens = sum(t.tokens for t in main[:boundary])
        orientation_turns = boundary
        orientation_share = (orientation_tokens / total_tokens) if total_tokens else None
        no_mutation = False
        boundary_turn_tokens = main[boundary].tokens

    # orientation_breadth -- RepoMirage's "exploration drift" discriminator.
    # High orientation tokens over FEW distinct paths is re-reading the same
    # thing (churn); high breadth is genuine discovery. Without it panel (a)
    # cannot tell diligence from thrash. Counted over the pre-boundary window
    # only, so it is the same window as orientation_tokens.
    pre_paths: Dict[str, None] = {}
    for turn in (main[:boundary] if boundary is not None else main):
        for call in turn.tools:
            for p in call.paths:
                pre_paths.setdefault(os.path.basename(str(p).rstrip("/\\")) or str(p), None)
    # Undefined when there is no boundary: there is no "pre-boundary window"
    # to count paths in.
    orientation_breadth = len(pre_paths) if boundary is not None else None

    # The mediator (handoff) and the confound (README) are found separately.
    # `handoff_extra` is set by build_threads once a thread is known: the file
    # the previous session in the thread WROTE last is a handoff for this
    # thread whatever it is called.
    # "Opened" requires a READ-class tool whose result
    # was not an error, an ANCHORED whole-word basename match on a note-shaped
    # file, and the first two responses of the MAIN conversation. A Glob/Grep
    # for a note is "searched for a note" and gets its own column. One file can
    # no longer make a session both README-opened and handoff-opened.
    handoff_file = readme_file = notes_file = searched = None
    extra = getattr(session, "handoff_extra", None)
    for turn in main[:NOTES_TURN_WINDOW]:
        for call in turn.tools:
            if call.use_id is not None and call.use_id in failed_ids:
                continue                    # the read failed; nothing was opened
            if call.name in SEARCH_TOOLS:
                searched = searched or notes_search_target(call, HANDOFF_BASENAME_RE)
                continue
            readme_file = readme_file or is_notes_open(call, README_BASENAME_RE)
            hit = is_notes_open(call, HANDOFF_BASENAME_RE, extra)
            if hit and not README_BASENAME_RE.search(os.path.basename(str(hit))):
                handoff_file = handoff_file or hit
            notes_file = notes_file or is_notes_open(call, NOTES_BASENAME_RE)

    histogram: "collections.Counter[str]" = collections.Counter()
    edits = mutating_bash = other_bash = 0
    for turn in turns:
        for call in turn.tools:
            histogram[call.name] += 1
            if call.name in MUTATING_TOOLS:
                edits += 1
            elif call.name == "Bash":
                if call.mutating:
                    mutating_bash += 1
                else:
                    other_bash += 1

    cost = 0.0
    coverage = Coverage()
    for turn in turns:
        rate = price_for(turn.model, prices)
        coverage.add(turn.model, turn.tokens, rate is not None)
        if not rate:
            continue
        cost += turn_cost_usd(turn, prices)
    priced_tokens = coverage.priced_tokens

    # ---- The price of the ORIENTATION RESPONSES.
    # Not `session cost x raw-token share`: that prices startup at the
    # session's AVERAGE rate. Cached input, fresh input and output have
    # different rates and different mixes before and after the boundary, and
    # different responses can run on different models, so the shortcut errs
    # in BOTH directions per session and changes the "where to intervene
    # first" ranking. Directly priced, from each pre-boundary response's own
    # buckets at its own model's rates.
    # A session that records no token counts at all (Copilot stores usually
    # carry AI credits, not tokens) has an UNKNOWN cost, never $0: it would
    # otherwise pull every median down and read as free.
    has_tokens = total_tokens > 0
    orientation_cost = (sum(turn_cost_usd(t, prices) for t in main[:boundary])
                        if boundary is not None and has_tokens else None)

    # ---- The child agents, rolled up rather than merged.
    child_cost = sum(turn_cost_usd(t, prices) for t in child)
    child_agents = sorted({str(t.agent) for t in child})

    per_model: Dict[str, Dict[str, Any]] = {}
    per_cell: Dict[str, Dict[str, Any]] = {}
    effort_mix: "collections.Counter[str]" = collections.Counter()
    for turn in turns:
        seen = turn.thinking_tokens or (turn.thinking_chars / 4.0)
        if turn.effort:
            effort_mix[turn.effort] += 1
        for table, key in ((per_model, turn.model),
                           (per_cell, "%s|%s" % (turn.model, turn.effort or "unrecorded"))):
            row = table.setdefault(key, {"turns": 0, "out": 0, "think": 0.0,
                                         "observed": False})
            row["turns"] += 1
            row["out"] += turn.out
            row["think"] += seen
            row["observed"] = row["observed"] or bool(seen)

    # `<synthetic>` is dropped here so no downstream table has to remember to:
    # it is a placeholder Claude Code stamps on its own injected records, not
    # a model anyone chose. Its TOKENS stay counted.
    session.models = sorted(set(real_models([t.model for t in turns if t.model])))
    session.metrics = {
        "total_tokens": total_tokens,
        "output_tokens": total_out,
        "turns": len(turns),
        # TOKENS BEFORE THE FIRST SUCCESSFUL MUTATION, on the main
        # conversation. None when no boundary was observed --
        # UNKNOWN, never 0 and never the whole session.
        "orientation_tokens": orientation_tokens,
        "orientation_turns": orientation_turns,
        "orientation_share": round(orientation_share, 4) if orientation_share is not None else None,
        # DIRECTLY PRICED, not share x session cost.
        "orientation_cost_usd": (round(orientation_cost, 6)
                                 if orientation_cost is not None else None),
        "orientation_breadth": orientation_breadth,
        # tokens spent per distinct thing looked at: high = re-reading the
        # same file, low = genuine discovery across the tree.
        "orientation_tokens_per_path": (round(orientation_tokens / orientation_breadth, 1)
                                        if orientation_breadth and orientation_tokens else None),
        "no_mutation": no_mutation,
        "first_mutation_turn": boundary,
        "first_mutation_reason": boundary_reason,
        "first_mutation_arg": boundary_tool,
        # The response that performs the boundary mutation is NOT counted as
        # orientation; its size is published so the reader can see the line.
        "boundary_turn_tokens": boundary_turn_tokens,
        # ATTEMPT vs SUCCESS. A refused or errored write is an
        # attempt; it does not end orientation, and it is counted here.
        "first_mutation_attempt_turn": attempt_turn,
        "failed_mutation_attempts": failed_attempts,
        # CHILD AGENTS. The boundary and the first-two-responses
        # window above are the MAIN conversation only; these are the workflow
        # roll-up.
        "main_turns": len(main),
        "child_agents": len(child_agents),
        "child_turns": len(child),
        "child_tokens": sum(t.tokens for t in child),
        "child_cost_usd": round(child_cost, 6),
        # The MEDIATOR. A handoff-shaped file, or the file the thread's
        # previous session wrote last. README deliberately excluded.
        "handoff_opened_by_turn_2": handoff_file is not None,
        "handoff_file": handoff_file,
        # Searching for a note is NOT opening one.
        "handoff_searched_by_turn_2": searched is not None,
        "handoff_search_target": searched,
        # Kept as a column, never as the mediator: reading a README IS
        # orientation work, not a substitute for it.
        "readme_opened_by_turn_2": readme_file is not None,
        "readme_file": readme_file,
        # The pooled question (note OR README). Kept as a column; it can point
        # the wrong way, so nothing splits on it.
        "notes_opened_by_turn_2": notes_file is not None,
        "notes_file": notes_file,
        "strategy": classify_strategy(edits, mutating_bash, other_bash),
        "tool_histogram": dict(histogram.most_common()),
        "edit_calls": edits,
        "mutating_bash_calls": mutating_bash,
        "other_bash_calls": other_bash,
        "thinking_tokens": round(thinking_tokens, 1),
        # A share of 0 would read as "ran at low effort"; absent thinking
        # records (headless/SDK transcripts serialise no thinking block) are
        # NOT observable, so they are None, not 0.
        "thinking_share": (round(thinking_tokens / total_out, 4)
                           if total_out and thinking_source != "absent" else None),
        "thinking_source": thinking_source,
        # ESTIMATED COST AT LIST API RATES. Named `est_cost_usd`, not
        # "proxy": on API billing "proxy" reads as wrong. It is still an
        # attribution weight, never an invoice -- what the same tokens WOULD
        # cost at published rates.
        "est_cost_usd": round(cost, 6) if has_tokens else None,
        # Deprecated alias, kept so any saved JSON keeps reading. Never
        # rendered in a user-facing report.
        "proxy_cost_usd": round(cost, 6) if has_tokens else None,
        # Shared denominator (audit_common.Coverage): priced tokens over
        # PRICEABLE tokens. `<synthetic>` is not a model and is excluded, so
        # this number equals what claude_cache_audit.py prints for the same
        # history rather than differing by the synthetic share.
        "price_coverage": round(coverage.fraction, 3) if coverage.total_tokens else None,
        "priced_tokens": coverage.priced_tokens,
        "priceable_tokens": coverage.total_tokens,
        "per_model": {m: {"turns": v["turns"], "output_tokens": v["out"],
                          "thinking_share": (round(v["think"] / v["out"], 4)
                                             if v["out"] and v["observed"] else None)}
                      for m, v in per_model.items()},
        "per_model_effort": {k: {"turns": v["turns"], "output_tokens": v["out"],
                                 "thinking_share": (round(v["think"] / v["out"], 4)
                                                    if v["out"] and v["observed"] else None)}
                             for k, v in per_cell.items()},
        # Effort IS recorded: a top-level `effort` field on each assistant
        # record. Absent on older CLI versions -> None (never guessed), and
        # thinking share stays the cross-check and the fallback estimate.
        "effort_mix": dict(effort_mix.most_common()),
        "effort": effort_mix.most_common(1)[0][0] if effort_mix else None,
        "effort_recorded_turns": sum(effort_mix.values()),
        "denials": session.denials,
        "tool_failures": session.tool_failures,
        "user_messages": len(session.user_messages),
        # CONFOUND COLUMN. None = this store carries no
        # user text, so the question cannot be asked of it.
        "question_first": question_first(session, boundary),
    }
    session.metrics["stage_path"] = stage_path(session, boundary)
    # OUTCOME, INFERRED. Majgaonkar et al. (arXiv 2511.00197): failure
    # trajectories run 12-82% longer, so a pooled orientation histogram hides
    # a bimodal-by-outcome distribution. Local history carries no pass/fail
    # oracle, so the rule is (ended with a commit / a passing verification) vs (ended
    # with a failure signal). UNKNOWN is a real third value and is never
    # collapsed into either side.
    session.metrics["outcome"] = classify_outcome(session)
    # OpenTelemetry GenAI agent-span attribute names, mirrored exactly, so a
    # future ingest from an OTLP exporter (or Claude Code's own) drops in
    # without a rename. The conventions warn that agent/tool attributes are
    # still provisional -- read 2026-09-06 at
    # github.com/open-telemetry/semantic-conventions-genai, docs/gen-ai/
    # gen-ai-agent-spans.md. Our native names stay authoritative; these are an
    # alias layer, and a field the store does not carry stays absent.
    session.metrics["otel"] = {
        "gen_ai.agent.name": session.source,
        "gen_ai.request.model": (session.models or [None])[0],
        "gen_ai.usage.input_tokens": sum(t.inp for t in turns),
        "gen_ai.usage.output_tokens": total_out,
        "gen_ai.usage.cache_read.input_tokens": sum(t.cread for t in turns),
        "gen_ai.usage.cache_write.input_tokens": sum(t.cw5 + t.cw1 for t in turns),
        "gen_ai.usage.reasoning_tokens": (round(thinking_tokens, 1)
                                          if thinking_source != "absent" else None),
    }


# ==========================================================================
# INGEST ADAPTERS -- one interface (Session records), three stores.
# Fields a store does not carry are None, never guessed.
# ==========================================================================
def default_claude_root() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / ".claude"


def resolve_projects_root(root: Path) -> Path:
    """Accept either ~/.claude or ~/.claude/projects."""
    root = Path(root).expanduser()
    nested = root / "projects"
    return nested if nested.is_dir() else root


def vscode_store_roots() -> List[Path]:
    """Every plausible VS Code chat store on this OS. Missing ones are skipped."""
    roots: List[Path] = []
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    appdata = os.environ.get("APPDATA")
    bases: List[Path] = []
    if appdata:                                   # Windows
        for flavour in ("Code", "Code - Insiders", "VSCodium"):
            bases.append(Path(appdata) / flavour / "User")
    for flavour in ("Code", "Code - Insiders", "VSCodium"):
        bases.append(home / "Library" / "Application Support" / flavour / "User")   # macOS
        bases.append(home / ".config" / flavour / "User")                            # Linux
    for base in bases:
        if base.is_dir():
            roots.append(base)
    return roots


def copilot_cli_roots() -> List[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    candidates = [home / ".copilot", home / ".config" / "copilot"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "copilot")
        candidates.append(Path(appdata) / "GitHub Copilot CLI")
    return [c for c in candidates if c.is_dir()]


# GitHub's own docs (docs.github.com/en/copilot/concepts/agents/copilot-cli/
# chronicle, read 2026-09-06) say Copilot CLI keeps sessions as files under
# `~/.copilot/session-state/` AND in a local SQLite "session store" -- a
# SUBSET of the full data -- which is what `/chronicle` reads. Legacy data
# lives in `history-session-state/`, logs in `logs/`. The schema is not
# documented publicly, which is exactly why `probe-store` exists: it prints
# table and column NAMES and counts, never values, so its output is safe to
# attach to an issue and an adapter can be finished from it.
COPILOT_SQLITE_NAMES = ("*.db", "*.sqlite", "*.sqlite3", "state.vscdb")
COPILOT_SESSION_SUBDIRS = ("session-state", "history-session-state")


def _sqlite_ro(path: Path):
    """Open a SQLite file READ-ONLY, via the immutable URI. stdlib only.

    `mode=ro` alone still takes a lock and can create a -wal file next to a
    store the CLI owns; `immutable=1` promises we will not write and takes no
    lock at all. We are auditing someone's live store -- opening it read-write
    is not an option, and neither is copying it somewhere first.
    """
    import sqlite3
    # as_uri() percent-encodes `?`, `#` and spaces and writes a Windows drive
    # as file:///C:/..., the form SQLite documents.
    uri = Path(path).resolve().as_uri() + "?immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=1.0)


def sqlite_schema(path: Path, max_tables: int = 40) -> Dict[str, Any]:
    """Table and column NAMES (and row counts) from a SQLite store. No values."""
    report: Dict[str, Any] = {"path": str(path), "tables": {}, "row_counts": {}}
    try:
        connection = _sqlite_ro(path)
    except Exception as exc:
        report["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        return report
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                       "AND name NOT LIKE 'sqlite_%' ORDER BY name")
        names = [row[0] for row in cursor.fetchall()][:max_tables]
        for name in names:
            try:
                cursor.execute('PRAGMA table_info("%s")' % name.replace('"', '""'))
                report["tables"][name] = [row[1] for row in cursor.fetchall()]
                cursor.execute('SELECT COUNT(*) FROM "%s"' % name.replace('"', '""'))
                report["row_counts"][name] = cursor.fetchone()[0]
            except Exception as exc:
                report["tables"][name] = ["<unreadable: %s>" % exc.__class__.__name__]
    finally:
        connection.close()
    report["privacy"] = "table and column names and row counts only; no stored value was read"
    return report


def _copilot_sqlite_docs(path: Path) -> Iterable[Tuple[Optional[Dict[str, Any]], Optional[str]]]:
    """Yield session-shaped JSON documents out of a SQLite session store.

    The schema is undocumented, so this is written to RECOGNISE rather than to
    assume: any column whose value is a JSON object carrying a message/turn
    list is treated as a session document, and anything else is left alone. A
    store that yields nothing yields nothing -- it does not yield a guess.
    """
    try:
        connection = _sqlite_ro(path)
    except Exception as exc:
        yield None, "%s: %s" % (exc.__class__.__name__, exc)
        return
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name NOT LIKE 'sqlite_%'")
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            quoted = '"%s"' % table.replace('"', '""')
            try:
                cursor.execute("SELECT * FROM %s LIMIT 5000" % quoted)
            except Exception:
                continue
            columns = [d[0] for d in cursor.description or []]
            for row in cursor.fetchall():
                for column, value in zip(columns, row):
                    if isinstance(value, (bytes, bytearray)):
                        try:
                            value = value.decode("utf-8", "replace")
                        except Exception:
                            continue
                    if not isinstance(value, str) or not value.lstrip().startswith("{"):
                        continue
                    try:
                        doc = json.loads(value)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(doc, dict) and any(
                            isinstance(doc.get(k), list) and doc.get(k)
                            for k in ("messages", "turns", "events", "history",
                                      "exchanges", "entries")):
                        doc.setdefault("sessionId", "%s:%s" % (table, column))
                        yield doc, None
    except Exception as exc:
        yield None, "%s: %s" % (exc.__class__.__name__, exc)
    finally:
        connection.close()


# ---------------------------------------------------------------- Claude Code
def ingest_claude_code(root: Path, since, until, include_cwd, exclude_cwd,
                       prices, max_files=None, progress=False) -> Tuple[List[Session], Dict[str, Any]]:
    """Full-fidelity reader for Claude Code JSONL.

    One JSONL file is one session; files that continue an earlier session
    (same `sessionId`) are joined, and assistant events are deduplicated on
    (sessionId, message.id) -- the rule references/log-schema.md defines.
    Counting raw rows instead multiplies the token ledger by the content-block
    count (see `_claude_assistant`).
    """
    projects = resolve_projects_root(root)
    # A session's own transcript is read BEFORE its subagents' files
    # (`<session>/subagents/**.jsonl`): a plain sort puts the subagent file
    # first, and its prompt would become the session's first prompt.
    paths = (sorted(projects.rglob("*.jsonl"),
                    key=lambda p: ("subagents" in p.parts, str(p)))
             if projects.is_dir() else [])
    if max_files:
        paths = paths[:max_files]
    stats = {"store": "claude-code", "root": str(projects), "files": len(paths),
             "rows": 0, "json_errors": 0, "assistant_rows": 0,
             "duplicate_rows": 0, "filtered_rows": 0, "files_unreadable": 0,
             "meter_rows": 0}
    by_id: Dict[str, Session] = {}
    seen_events: Dict[str, Dict[Any, Turn]] = collections.defaultdict(dict)
    excluded: Dict[str, Dict[str, Any]] = {}

    for number, path in enumerate(paths, 1):
        if progress and number % 250 == 0:
            print("  … %d/%d files" % (number, len(paths)), file=sys.stderr)
        project_hint = path.parent.name
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            stats["files_unreadable"] += 1
            continue
        try:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        except OSError:
            mtime = None
        got_prompt: Dict[str, bool] = {}
        with handle:
            for line in handle:
                stats["rows"] += 1
                is_assistant = '"assistant"' in line
                is_system = '"system"' in line and '"subtype"' in line
                is_user = not is_assistant and ('"type":"user"' in line or '"type": "user"' in line)
                if not (is_assistant or is_system or is_user):
                    continue
                # Past the first prompt we still want HUMAN turns (the
                # missed-shift detector reads corrections and re-sends) and
                # any row carrying a refusal or a failed tool result -- but
                # not the bulk of tool_result payloads, which are the
                # expensive rows and carry nothing this module reads.
                if is_user and got_prompt and all(got_prompt.values()):
                    if '"tool_result"' in line:
                        # `is_error` is kept as well as the failure words: it is
                        # the only unambiguous "this call did not happen" signal
                        # and the attempt/success boundary split reads it.
                        if not (DENIAL_MARK in line or '"is_error"' in line
                                or FAILUREISH_RE.search(line)):
                            continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    stats["json_errors"] += 1
                    continue
                rtype = record.get("type")
                if rtype not in ("assistant", "user", "system"):
                    continue
                raw_cwd = str(record.get("cwd") or project_hint)
                dropped_by = next((p.pattern for p in exclude_cwd if p.search(raw_cwd)), None)
                if (include_cwd and not any(p.search(raw_cwd) for p in include_cwd)) or dropped_by:
                    stats["filtered_rows"] += 1
                    # What the scope filter removed, by pattern, so
                    # the report can publish an inclusion/exclusion breakdown
                    # instead of asserting that every session is real work.
                    key = dropped_by or "(not matched by --include-cwd-pattern)"
                    row = excluded.setdefault(key, {"sessions": set(), "rows": 0,
                                                    "tokens": 0, "est_cost_usd": 0.0})
                    row["rows"] += 1
                    row["sessions"].add(str(record.get("sessionId") or path.stem))
                    if rtype == "assistant":
                        message = record.get("message") or {}
                        usage = message.get("usage")
                        if isinstance(usage, dict):
                            creation = usage.get("cache_creation") or {}
                            tokens = (numeric(usage.get("input_tokens"))
                                      + numeric(usage.get("output_tokens"))
                                      + numeric(usage.get("cache_read_input_tokens"))
                                      + numeric(creation.get("ephemeral_5m_input_tokens"))
                                      + numeric(creation.get("ephemeral_1h_input_tokens"))
                                      + (0 if creation else
                                         numeric(usage.get("cache_creation_input_tokens"))))
                            row["tokens"] += tokens
                            rate = price_for(str(message.get("model") or ""), prices)
                            if rate:
                                row["est_cost_usd"] += (
                                    numeric(usage.get("cache_read_input_tokens"))
                                    * CACHE_READ_MULT * rate[0]
                                    + numeric(usage.get("input_tokens")) * rate[0]
                                    + numeric(usage.get("output_tokens")) * rate[1]) / 1e6
                    continue
                sid = str(record.get("sessionId") or path.stem)
                stamp = parse_timestamp(record.get("timestamp"))
                if stamp and ((since and stamp < since) or (until and stamp >= until)):
                    continue

                session = by_id.get(sid)
                if session is None:
                    session = Session("claude-code", sid, str(path))
                    session.file_mtime = mtime
                    by_id[sid] = session
                    got_prompt[sid] = False
                got_prompt.setdefault(sid, bool(session.first_prompt))
                if record.get("cwd"):
                    session.cwd = str(record["cwd"])
                elif session.cwd is None:
                    session.cwd = project_hint
                session.version = record.get("version") or session.version
                session.git_branch = record.get("gitBranch") or session.git_branch

                if rtype == "assistant":
                    _claude_assistant(record, session, seen_events[sid], stats)
                elif rtype == "user":
                    if _is_human_prompt(record):
                        text = _prompt_text(record)
                        if text.strip():
                            session.user_messages.append(
                                {"turn": len(session.turns), "ts": stamp, "text": text})
                        if not session.first_prompt:
                            session.first_prompt = one_line(text)
                            got_prompt[sid] = True
                    else:
                        _claude_tool_result(record, session)
                elif rtype == "system":
                    if record.get("subtype") == "compact_boundary":
                        session.compactions.append(_compact_boundary(record, stamp))
                        stats["compact_boundaries"] = stats.get("compact_boundaries", 0) + 1
                    reading = _claude_meter_row(record, stamp)
                    if reading:
                        session.meter_readings.append(reading)
                        stats["meter_rows"] += 1
                if rtype == "assistant" and record.get("error") == "rate_limit":
                    reading = _claude_meter_row(record, stamp)
                    if reading:
                        session.meter_readings.append(reading)
                        stats["meter_rows"] += 1

    sessions = []
    for session in by_id.values():
        session.turns.sort(key=lambda t: (t.ts or dt.datetime.min.replace(tzinfo=dt.timezone.utc), t.index))
        main = 0
        for index, turn in enumerate(session.turns):
            for call in turn.tools:
                call.turn = index
            turn.index = index
            if turn.agent is None:
                turn.main_index = main
                main += 1
            else:
                turn.main_index = None
        if not session.turns:
            continue
        finalise(session, prices)
        sessions.append(session)
    stats["excluded_by_pattern"] = sorted(
        ({"pattern": key, "sessions": len(row["sessions"]), "rows": row["rows"],
          "tokens": row["tokens"], "est_cost_usd": round(row["est_cost_usd"], 2)}
         for key, row in excluded.items()),
        key=lambda r: -r["tokens"])
    stats["included_sessions"] = len(sessions)
    return sessions, stats


def _is_human_prompt(record: Dict[str, Any]) -> bool:
    if record.get("isMeta") or record.get("isCompactSummary") or record.get("toolUseResult"):
        return False
    # A subagent's task prompt is written by the parent agent, not by a human.
    if record.get("isSidechain") or record.get("agentId"):
        return False
    if str(record.get("promptSource") or "").lower() in {"sdk", "system"}:
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list) or not content:
        return False
    kinds = {str(b.get("type", "")) for b in content if isinstance(b, dict)}
    return bool(kinds - {"tool_result"})


def _claude_tool_result(record: Dict[str, Any], session: Session) -> None:
    """Count refusals and failed tool results. Covariate, never a decision input.

    The refusal string is the harness's own, verbatim; a
    paraphrase would silently stop matching the day the wording changes, and
    a miss here must read as 0 refusals *observed*, not 0 refusals.
    """
    blocks = (record.get("message") or {}).get("content")
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        body = block.get("content")
        text = body if isinstance(body, str) else json.dumps(body, default=str)
        use_id = block.get("tool_use_id")
        if DENIAL_MARK in text:
            session.denials += 1
            if use_id:
                session.failed_tool_ids.add(use_id)
        elif block.get("is_error") or FAILUREISH_RE.search(text):
            session.tool_failures += 1
            if use_id and block.get("is_error"):
                session.failed_tool_ids.add(use_id)


def _prompt_text(record: Dict[str, Any]) -> str:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return " ".join(parts)


def _add_content_blocks(turn: "Turn", message: Dict[str, Any]) -> None:
    """Fold a row's content blocks onto its turn. IDEMPOTENT.

    A row replayed on a resume or a continuation file used to be merged again:
    its tool_use blocks were appended a second time (doubling the tool
    histogram, and pushing the boundary and the strategy class around) and its
    thinking characters were added again (pushing thinking share above 1).
    Blocks are now keyed by `audit_common.block_key`, so replaying an
    identical row changes nothing.
    """
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        key = audit_common.block_key(block)
        if key in turn.seen_blocks:
            continue
        turn.seen_blocks.add(key)
        kind = block.get("type")
        if kind == "tool_use":
            turn.tools.append(make_tool_call(turn.index, str(block.get("name") or "?"),
                                             block.get("input"), block.get("id")))
        elif kind == "thinking":
            turn.thinking_chars += len(str(block.get("thinking") or ""))


def _claude_assistant(record: Dict[str, Any], session: Session,
                      seen: Dict[Any, "Turn"], stats: Dict[str, Any]) -> None:
    """One API RESPONSE becomes one Turn, however many JSONL rows carry it.

    Claude Code serialises a multi-block assistant message as one row PER
    CONTENT BLOCK -- `thinking`, then `text`, then each `tool_use` -- and
    repeats the *identical* `usage` object on every one of them (verified on
    real transcripts: usage is byte-identical within every message id). So:

      * counting rows instead of responses inflates totals about 2x (more
        with thinking on and several tool calls per turn); and
      * dropping the repeat rows outright THROWS AWAY THEIR TOOL CALLS,
        which pushes the first edit late and corrupts the tool histogram,
        the strategy class, the notes-opened mediator and every thread join
        built on files_touched.

    The rule is therefore MERGE, not drop: usage is taken once, content
    blocks are accumulated onto the turn the first row opened.
    """
    message = record.get("message") or {}
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return
    stats["assistant_rows"] += 1
    key = audit_common.response_key(record)[1]      # the one response-identity rule
    existing = seen.get(key)
    if existing is not None:
        stats["duplicate_rows"] += 1
        session.duplicate_rows += 1
        _add_content_blocks(existing, message)      # keep the blocks, drop the usage
        return
    # Effort is a TOP-LEVEL field on the assistant record: `effort` on newer
    # CLI versions, `perTurnEffort` on some; absent on older ones -> None.
    # A child-agent record carries `agentId` and/or `isSidechain` (verified on
    # real transcripts: nearly every file under <session>/subagents/** carries
    # `isSidechain:true`, every assistant row there carries `agentId`, and no
    # top-level session file does).
    agent = record.get("agentId") or ("side" if record.get("isSidechain") else None)
    turn = Turn(len(session.turns), parse_timestamp(record.get("timestamp")),
                str(message.get("model") or "unknown"),
                audit_common.record_effort(record),
                str(agent) if agent else None)
    seen[key] = turn
    turn.inp = numeric(usage.get("input_tokens"))
    turn.out = numeric(usage.get("output_tokens"))
    turn.cread = numeric(usage.get("cache_read_input_tokens"))
    creation = usage.get("cache_creation") or {}
    turn.cw5 = numeric(creation.get("ephemeral_5m_input_tokens"))
    turn.cw1 = numeric(creation.get("ephemeral_1h_input_tokens"))
    if not (turn.cw5 or turn.cw1):
        turn.cw5 = numeric(usage.get("cache_creation_input_tokens"))
    details = usage.get("output_tokens_details") or {}
    turn.thinking_tokens = numeric(details.get("thinking_tokens")) if isinstance(details, dict) else 0
    _add_content_blocks(turn, message)
    session.turns.append(turn)


METER_KEYS = ("rate_limit_event", "rateLimit", "unifiedRateLimit", "rate_limit",
              "usageLimit", "limits")
RESET_TEXT_RE = re.compile(r"resets?\s+([0-9:apmAPM\s.]+)(?:\s*\(([^)]+)\))?")


def _compact_boundary(record: Dict[str, Any], stamp) -> Dict[str, Any]:
    """One compaction, read off the `compact_boundary` system row.

    `compactMetadata` is written by the CLI and carries the sizes directly:
    `preTokens` (context immediately before), `postTokens` (the summary the
    conversation continues from), `cumulativeDroppedTokens` and `durationMs`.
    Fields a row does not carry stay None -- never guessed, and never derived
    from a length-of-JSON estimate.
    """
    meta = record.get("compactMetadata") or {}
    return {
        "ts": stamp,
        "trigger": meta.get("trigger"),
        "pre_tokens": (numeric(meta.get("preTokens")) if meta.get("preTokens") is not None
                       else None),
        "post_tokens": (numeric(meta.get("postTokens")) if meta.get("postTokens") is not None
                        else None),
        "dropped_tokens": (numeric(meta.get("cumulativeDroppedTokens"))
                           if meta.get("cumulativeDroppedTokens") is not None else None),
        "duration_ms": (numeric(meta.get("durationMs"))
                        if meta.get("durationMs") is not None else None),
    }


def _claude_meter_row(record: Dict[str, Any], stamp) -> Optional[Dict[str, Any]]:
    """rate_limit_event-style records, in whatever shape this CLI writes them.

    NOTE, and the report says so on the card: a `rate_limit_event` payload is
    stamped at SESSION START, so a reading is dated by the session's start
    time, not by when you read it. Where the CLI writes
    no such record at all, the fallback is the 429 refusal message, which
    carries only the reset clock time.
    """
    payload = None
    for key in METER_KEYS:
        value = record.get(key)
        if isinstance(value, dict):
            payload = value
            break
    subtype = str(record.get("subtype") or "")
    if payload is None and subtype not in ("rate_limit_event", "rate_limit"):
        if record.get("error") != "rate_limit":
            return None
    text = ""
    content = (record.get("message") or {}).get("content") or record.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    reading: Dict[str, Any] = {
        "at": iso(stamp),
        "kind": subtype or ("http-429" if record.get("error") == "rate_limit" else "payload"),
        "session": record.get("sessionId"),
        "raw_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
    }
    for field, aliases in (("five_hour_utilization", ("five_hour_utilization", "fiveHourUtilization", "session_utilization")),
                           ("weekly_utilization", ("weekly_utilization", "weeklyUtilization", "week_utilization")),
                           ("five_hour_resets_at", ("five_hour_resets_at", "fiveHourResetsAt", "resetsAt", "resets_at")),
                           ("weekly_resets_at", ("weekly_resets_at", "weeklyResetsAt"))):
        if isinstance(payload, dict):
            for alias in aliases:
                if alias in payload:
                    reading[field] = payload[alias]
                    break
        reading.setdefault(field, None)
    match = RESET_TEXT_RE.search(text or "")
    reading["reset_text"] = one_line(match.group(0)) if match else None
    reading["limit_kind"] = ("weekly" if "weekly" in (text or "").lower()
                             else "session" if "session" in (text or "").lower() else None)
    return reading


# ------------------------------------------------------- VS Code Copilot chat
# Two stores exist -- an older `*.json` store and a newer `*.jsonl` store.
# Read BOTH.
# The newer store wraps the same object as {"kind": 0, "v": {...}} per line.
VSCODE_REQUEST_KEYS = ("requests", "exchanges", "turns", "messages")


def _unwrap(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The newer VS Code store wraps the session as {"kind": 0, "v": {...}}."""
    inner = doc.get("v")
    return inner if isinstance(inner, dict) else doc


def _vscode_session_docs(path: Path) -> Iterable[Tuple[Optional[Dict[str, Any]], Optional[str]]]:
    """Yield (document, parse_error) from either store shape.

    Whole-file JSON first (the old store, and the new store's single-line
    form), then true multi-line JSONL. A file that is neither yields exactly
    one error, not one per line -- otherwise a config file with a BOM or a
    trailing comma reports as a dozen unparsed "sessions".
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        yield None, "unreadable: %s" % exc.__class__.__name__
        return
    if not text.strip():
        return
    try:
        doc = json.loads(text)
        whole_error = None
    except json.JSONDecodeError as exc:
        doc, whole_error = None, "not JSON (%s at line %d)" % (exc.msg, exc.lineno)
    if isinstance(doc, dict):
        yield _unwrap(doc), None
        return
    if isinstance(doc, list):
        for item in doc:
            if isinstance(item, dict):
                yield _unwrap(item), None
        return
    parsed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            parsed += 1
            yield _unwrap(item), None
    if parsed == 0:
        yield None, whole_error or "neither JSON nor JSONL"


def _vscode_tool_calls(request: Dict[str, Any], turn_index: int) -> List[ToolCall]:
    """Best-effort: VS Code records tool invocations inside the response parts."""
    calls: List[ToolCall] = []
    parts: List[Any] = []
    for key in ("response", "result", "parts", "steps"):
        value = request.get(key)
        if isinstance(value, list):
            parts.extend(value)
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("kind") or part.get("type") or "")
        invocation = part.get("toolInvocation") if isinstance(part.get("toolInvocation"), dict) else {}
        name = (part.get("toolName") or part.get("toolId") or part.get("name")
                or invocation.get("toolId") or invocation.get("toolName"))
        if kind in ("toolInvocationSerialized", "toolInvocation", "prepareToolInvocation") or name:
            payload = part.get("toolSpecificData") or part.get("input") or part.get("parameters") or {}
            if not isinstance(payload, dict):
                payload = {}
            uri = part.get("uri") or part.get("resourceUri")
            if isinstance(uri, dict) and uri.get("path"):
                payload = dict(payload, path=uri["path"])
            elif isinstance(uri, str):
                payload = dict(payload, path=uri)
            calls.append(make_tool_call(turn_index, str(name or "tool"), payload))
        elif kind in ("inlineReference", "textEditGroup") or "edits" in part:
            uri = part.get("uri") or part.get("resource")
            path_value = uri.get("path") if isinstance(uri, dict) else uri
            calls.append(ToolCall(turn_index, "Edit", one_line(path_value),
                                  [str(path_value)] if path_value else [],
                                  True, "tool:Edit"))
    return calls


def ingest_vscode_copilot(roots: Sequence[Path], since, until, prices,
                          progress=False) -> Tuple[List[Session], Dict[str, Any]]:
    stats = {"store": "vscode-copilot", "roots": [str(r) for r in roots],
             "files": 0, "sessions": 0, "sessions_with_turns": 0,
             "parse_errors": 0, "unknown_request_keys": collections.Counter()}
    sessions: List[Session] = []
    for root in roots:
        patterns = [str(root / "workspaceStorage" / "*" / "chatSessions" / "*.json*"),
                    str(root / "workspaceStorage" / "*" / "chatEditingSessions" / "*" / "*.json*"),
                    str(root / "globalStorage" / "emptyWindowChatSessions" / "*.json*"),
                    str(root / "globalStorage" / "*" / "chatSessions" / "*.json*")]
        for pattern in patterns:
            for name in sorted(glob.glob(pattern)):
                path = Path(name)
                stats["files"] += 1
                workspace = path.parent.parent.name if path.parent.name == "chatSessions" else path.parent.name
                for doc, error in _vscode_session_docs(path):
                    if error:
                        stats["parse_errors"] += 1
                        continue
                    if not isinstance(doc, dict) or not doc:
                        continue
                    stats["sessions"] += 1
                    session = _vscode_session_from_doc(doc, path, workspace, stats)
                    if not session.turns:
                        continue
                    start = session.start
                    if start and ((since and start < since) or (until and start >= until)):
                        continue
                    stats["sessions_with_turns"] += 1
                    finalise(session, prices)
                    sessions.append(session)
    stats["unknown_request_keys"] = dict(stats["unknown_request_keys"].most_common(20))
    return sessions, stats


def _vscode_session_from_doc(doc: Dict[str, Any], path: Path, workspace: str,
                             stats: Dict[str, Any]) -> Session:
    sid = str(doc.get("sessionId") or path.stem)
    session = Session("vscode-copilot", sid, str(path))
    session.cwd = None
    for key in ("workspaceFolder", "workspaceUri", "cwd", "folder"):
        value = doc.get(key)
        if isinstance(value, str):
            session.cwd = value
        elif isinstance(value, dict) and value.get("path"):
            session.cwd = value["path"]
    if session.cwd is None:
        session.cwd = "vscode-workspace:%s" % workspace
    session.title = one_line(doc.get("customTitle") or doc.get("title"))
    created = parse_timestamp(doc.get("creationDate"))
    requests: List[Any] = []
    for key in VSCODE_REQUEST_KEYS:
        value = doc.get(key)
        if isinstance(value, list) and value:
            requests = value
            break
    for index, request in enumerate(requests):
        if not isinstance(request, dict):
            continue
        for key in request:
            if key not in ("requestId", "message", "response", "result", "modelId",
                           "timestamp", "agent", "variableData", "contentReferences",
                           "codeCitations", "isCanceled", "followups", "responseId",
                           "slashCommand", "editedFileEvents", "confirmation"):
                stats["unknown_request_keys"][key] += 1
        stamp = (parse_timestamp(request.get("timestamp"))
                 or parse_timestamp((request.get("result") or {}).get("timings", {}).get("firstProgress")
                                    if isinstance(request.get("result"), dict) else None)
                 or created)
        model = str(request.get("modelId") or doc.get("modelId") or "unknown")
        turn = Turn(index, stamp, model)
        # Token counts are usually absent in this store: leave them 0 and let
        # price coverage report the gap rather than invent numbers.
        result = request.get("result") if isinstance(request.get("result"), dict) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
        turn.inp = numeric(usage.get("promptTokens") or usage.get("input_tokens"))
        turn.out = numeric(usage.get("completionTokens") or usage.get("output_tokens"))
        turn.tools = _vscode_tool_calls(request, index)
        session.turns.append(turn)
        if index == 0:
            message = request.get("message")
            text = message.get("text") if isinstance(message, dict) else message
            session.first_prompt = one_line(text if isinstance(text, str) else None)
    if session.title is None and session.first_prompt:
        session.title = session.first_prompt
    return session


# ------------------------------------------------------------- Copilot CLI
def ingest_copilot_cli(roots: Sequence[Path], since, until, prices,
                       progress=False) -> Tuple[List[Session], Dict[str, Any]]:
    """`~/.copilot` sessions. Shape is only partly known; read defensively."""
    stats = {"store": "copilot-cli", "roots": [str(r) for r in roots],
             "files": 0, "sessions": 0, "sessions_with_turns": 0,
             "parse_errors": 0, "skipped_logs": 0, "sqlite_files": 0,
             "sqlite_sessions": 0, "unknown_top_keys": collections.Counter()}
    sessions: List[Session] = []
    for root in roots:
        # BOTH halves of the store: the session-state JSON files and the local
        # SQLite session store that /chronicle reads (GitHub docs, 2026-09-06).
        sources: List[Tuple[Path, Any]] = []
        for name in sorted(glob.glob(str(root / "**" / "*.json*"), recursive=True)):
            path = Path(name)
            if path.name == "config.json" or "logs" in path.parts:
                stats["skipped_logs"] += 1
                continue
            sources.append((path, _vscode_session_docs(path)))
        for pattern in COPILOT_SQLITE_NAMES:
            for name in sorted(glob.glob(str(root / "**" / pattern), recursive=True)):
                path = Path(name)
                if "logs" in path.parts:
                    stats["skipped_logs"] += 1
                    continue
                stats["sqlite_files"] += 1
                sources.append((path, _copilot_sqlite_docs(path)))
        for path, stream in sources:
            stats["files"] += 1
            from_sqlite = path.suffix.lower() in (".db", ".sqlite", ".sqlite3", ".vscdb")
            for doc, error in stream:
                if error:
                    stats["parse_errors"] += 1
                    continue
                if not isinstance(doc, dict) or not doc:
                    continue
                turns_raw = None
                for key in ("messages", "turns", "events", "history", "exchanges", "entries"):
                    value = doc.get(key)
                    if isinstance(value, list) and value:
                        turns_raw = value
                        break
                if turns_raw is None:
                    continue
                stats["sessions"] += 1
                if from_sqlite:
                    stats["sqlite_sessions"] += 1
                for key in doc:
                    if key not in ("sessionId", "id", "cwd", "workingDirectory", "model",
                                   "createdAt", "startTime", "timestamp", "title",
                                   "messages", "turns", "events", "history", "exchanges", "entries"):
                        stats["unknown_top_keys"][key] += 1
                session = Session("copilot-cli", str(doc.get("sessionId") or doc.get("id") or path.stem), str(path))
                session.cwd = doc.get("cwd") or doc.get("workingDirectory")
                session.title = one_line(doc.get("title"))
                created = parse_timestamp(doc.get("createdAt") or doc.get("startTime") or doc.get("timestamp"))
                for index, raw in enumerate(turns_raw):
                    if not isinstance(raw, dict):
                        continue
                    role = str(raw.get("role") or raw.get("type") or "")
                    stamp = parse_timestamp(raw.get("timestamp") or raw.get("createdAt")) or created
                    if role in ("user", "human") and session.first_prompt is None:
                        session.first_prompt = one_line(raw.get("content") if isinstance(raw.get("content"), str) else None)
                        continue
                    if role not in ("assistant", "model", "response", ""):
                        continue
                    turn = Turn(len(session.turns), stamp,
                                str(raw.get("model") or doc.get("model") or "unknown"))
                    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
                    turn.inp = numeric(usage.get("input_tokens") or usage.get("prompt_tokens"))
                    turn.out = numeric(usage.get("output_tokens") or usage.get("completion_tokens"))
                    for call in (raw.get("toolCalls") or raw.get("tool_calls") or []):
                        if not isinstance(call, dict):
                            continue
                        name_ = call.get("name") or (call.get("function") or {}).get("name") or "tool"
                        args = call.get("arguments") or call.get("input") or (call.get("function") or {}).get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {"command": args}
                        turn.tools.append(make_tool_call(turn.index, str(name_), args))
                    session.turns.append(turn)
                if not session.turns:
                    continue
                start = session.start
                if start and ((since and start < since) or (until and start >= until)):
                    continue
                stats["sessions_with_turns"] += 1
                finalise(session, prices)
                sessions.append(session)
    stats["unknown_top_keys"] = dict(stats["unknown_top_keys"].most_common(20))
    return sessions, stats


STORES = ("claude-code", "vscode-copilot", "copilot-cli")


def collect_sessions(args, prices) -> Tuple[List[Session], List[Dict[str, Any]]]:
    since = parse_bound(getattr(args, "since", None))
    until = parse_bound(getattr(args, "until", None))
    include_cwd = [re.compile(p) for p in getattr(args, "include_cwd_pattern", []) or []]
    patterns = list(getattr(args, "exclude_cwd_pattern", []) or [])
    exclude_cwd = [re.compile(p) for p in patterns]
    wanted = set(getattr(args, "store", None) or STORES)
    sessions: List[Session] = []
    all_stats: List[Dict[str, Any]] = []
    if "claude-code" in wanted:
        rows, stats = ingest_claude_code(Path(args.root), since, until, include_cwd,
                                         exclude_cwd, prices,
                                         getattr(args, "max_files", None),
                                         getattr(args, "progress", False))
        sessions += rows
        all_stats.append(stats)
    if "vscode-copilot" in wanted:
        roots = [Path(p) for p in (getattr(args, "vscode_root", None) or [])] or vscode_store_roots()
        rows, stats = ingest_vscode_copilot(roots, since, until, prices)
        sessions += rows
        all_stats.append(stats)
    if "copilot-cli" in wanted:
        roots = [Path(p) for p in (getattr(args, "copilot_root", None) or [])] or copilot_cli_roots()
        rows, stats = ingest_copilot_cli(roots, since, until, prices)
        sessions += rows
        all_stats.append(stats)
    # cwd filters apply to every store, not only Claude Code
    if include_cwd or exclude_cwd:
        kept = []
        for session in sessions:
            if session.source == "claude-code":
                kept.append(session)
                continue
            target = str(session.cwd or "")
            if include_cwd and not any(p.search(target) for p in include_cwd):
                continue
            if any(p.search(target) for p in exclude_cwd):
                continue
            kept.append(session)
        sessions = kept
    sessions.sort(key=lambda s: (s.start or dt.datetime.min.replace(tzinfo=dt.timezone.utc)))
    return sessions, all_stats


# ==========================================================================
# THREADS -- the multi-session unit. A metadata-only join over-merges badly
# on real histories (one "thread" swallowing dozens of unrelated sessions), so
# the join rule here is deliberately conservative AND calibratable.
# ==========================================================================
def resource_tokens(session: Session, use_basenames: bool = True) -> set:
    tokens = set()
    for path in session.files_touched:
        clean = str(path).strip().rstrip("/\\")
        if not clean or clean in (".", ".."):
            continue
        tokens.add(os.path.basename(clean) if use_basenames else clean)
    tokens.discard("")
    return tokens


class Thread:
    def __init__(self, thread_id: str, session: Session):
        self.id = thread_id
        self.sessions: List[Session] = [session]
        self.tokens = resource_tokens(session)
        self.justifications: List[str] = []

    @property
    def cwd(self) -> Optional[str]:
        return self.sessions[0].cwd

    @property
    def start(self):
        return min((s.start for s in self.sessions if s.start), default=None)

    @property
    def end(self):
        return max((s.end or s.start for s in self.sessions if s.start), default=None)

    def add(self, session: Session, shared: set) -> None:
        self.sessions.append(session)
        self.tokens |= resource_tokens(session)

    def last_written(self) -> List[str]:
        """Basenames the most recent member session MUTATED, latest first.

        A thread's handover file need not be called HANDOFF.md; if the
        previous session's last write was `pipeline-state.md`, a successor
        that opens it early is reading a handoff.
        """
        for session in reversed(self.sessions):
            written = [os.path.basename(str(p).rstrip("/\\"))
                       for turn in session.turns for call in turn.tools
                       if call.mutating for p in call.paths]
            if written:
                return list(reversed(written))[:5]
        return []


def build_threads(sessions: Sequence[Session], gap_hours: float, min_shared: int,
                  max_sessions: int, by_basename: bool = True,
                  verbose_caps: bool = False) -> List[Thread]:
    """(same cwd) AND (start within --gap-hours of the thread's last session)
    AND (>= --min-shared shared resource tokens). A thread never grows past
    --max-sessions without a printed shared-token justification."""
    gap = dt.timedelta(hours=gap_hours)
    open_threads: Dict[str, List[Thread]] = collections.defaultdict(list)
    threads: List[Thread] = []
    counter = 0
    ordered = sorted([s for s in sessions if s.start],
                     key=lambda s: s.start)
    for session in ordered:
        key = str(session.cwd or "unknown")
        best: Optional[Thread] = None
        best_shared: set = set()
        for thread in open_threads[key]:
            last = max((m.start for m in thread.sessions if m.start), default=None)
            if last is None or (session.start - last) > gap:
                continue
            shared = resource_tokens(session, by_basename) & thread.tokens
            if len(shared) < min_shared:
                continue
            if len(thread.sessions) >= max_sessions and len(shared) < 2 * max(1, min_shared):
                if verbose_caps:
                    print("  cap: %s at %d sessions; %s not joined (%d shared token(s), "
                          "needs %d past the cap)" % (thread.id, len(thread.sessions),
                                                      session.session_id[:8], len(shared),
                                                      2 * max(1, min_shared)))
                continue
            if len(shared) > len(best_shared):
                best, best_shared = thread, shared
        if best is None:
            counter += 1
            thread = Thread("T%03d" % counter, session)
            threads.append(thread)
            open_threads[key].append(thread)
        else:
            if len(best.sessions) >= max_sessions:
                justification = ("%s joined past the %d-session cap on %d shared token(s): %s"
                                 % (session.session_id[:8], max_sessions, len(best_shared),
                                    ", ".join(sorted(best_shared)[:6])))
                best.justifications.append(justification)
                if verbose_caps:
                    print("  cap-override: " + justification)
            best.add(session, best_shared)
    return threads


def apply_thread_handoffs(threads: Sequence[Thread],
                          prices: Dict[str, Tuple[float, float]]) -> None:
    """Second pass: tell each successor what its predecessor wrote last.

    `finalise` runs at ingest, before threads exist, so the thread-specific
    half of the handoff mediator can only be applied once the join is known.
    Only sessions whose answer could CHANGE are recomputed.
    """
    for thread in threads:
        if len(thread.sessions) < 2:
            continue
        ordered = sorted(thread.sessions,
                         key=lambda s: s.start or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        for index, session in enumerate(ordered[1:], 1):
            # The IMMEDIATE predecessor only. This used to
            # collect up to eight basenames from EVERY earlier session in the
            # thread, which let ordinary source-code reads count as opening a
            # handoff. README is excluded here too, so one file can no longer
            # make a session both README-opened and handoff-opened.
            predecessor = ordered[index - 1]
            written = [os.path.basename(str(p).rstrip("/\\"))
                       for turn in predecessor.turns for call in turn.tools
                       if call.mutating for p in call.paths
                       if not README_BASENAME_RE.search(os.path.basename(str(p)))]
            if not written:
                continue
            session.handoff_extra = list(dict.fromkeys(reversed(written)))[:PREDECESSOR_HANDOFF_CAP]
            if not session.metrics.get("handoff_opened_by_turn_2"):
                finalise(session, prices)


def unmeasured_boundary(members: Sequence["Session"]) -> Dict[str, Any]:
    """The "no observed boundary" category.

    A session with no detected mutation has an UNKNOWN orientation share. It
    used to contribute its WHOLE token count to every tax total while
    contributing ZERO dollars. It is now excluded from both and reported here,
    beside every tax figure, so the reader can see how much spend the tax
    number is silent about.
    """
    rows = [m for m in members if m.metrics.get("orientation_tokens") is None]
    return {"sessions": len(rows),
            "tokens": sum(m.metrics.get("total_tokens") or 0 for m in rows),
            "est_cost_usd": round(sum(m.metrics.get("est_cost_usd") or 0.0 for m in rows), 4)}


def thread_record(thread: Thread, redact: bool) -> Dict[str, Any]:
    members = thread.sessions
    orientation = [m.metrics.get("orientation_tokens") for m in members]
    tax = sum(v for v in orientation[1:] if v is not None) if len(orientation) > 1 else 0
    # Directly priced, not share x session cost.
    tax_cost = sum(m.metrics.get("orientation_cost_usd") or 0.0 for m in members[1:])
    unmeasured = unmeasured_boundary(members[1:])
    total = sum(m.metrics.get("total_tokens", 0) for m in members)
    cost = sum(m.metrics.get("est_cost_usd") or 0.0 for m in members)
    tools = sorted({m.source for m in members})
    shared = set.intersection(*[resource_tokens(m) for m in members]) if len(members) > 1 else resource_tokens(members[0])
    record = {
        "thread_id": thread.id,
        "sessions": len(members),
        "tools": tools,
        "cross_tool": len(tools) > 1,
        "first": iso(thread.start),
        "last": iso(thread.end),
        "elapsed_days": round(((thread.end - thread.start).total_seconds() / 86400.0), 2)
        if thread.start and thread.end else None,
        "est_cost_usd": round(cost, 4),
        "total_tokens": total,
        "orientation_tokens": sum(v for v in orientation if v is not None),
        "reorientation_tax_tokens": tax,
        "reorientation_tax_usd": round(tax_cost, 4),
        "reorientation_tax_share": round(tax / total, 4) if total else None,
        "successors_without_boundary": unmeasured,
        "shared_resource_tokens": sorted(shared)[:25] if not redact else len(shared),
        "cap_justifications": thread.justifications,
        "members": [session_record(m, redact, brief=True) for m in members],
    }
    if redact:
        record["cwd"] = stable_hash(thread.cwd)
    else:
        record["cwd"] = thread.cwd
    return record


# ==========================================================================
# STREAMS -- the second level above threads.
#
# WHY. The 72-hour gap rule comes from the query-log session-detection
# literature and it is right about EPISODES: two bursts of work a month apart
# are two sittings. But people think in recurring pieces of work -- "the
# upload I do once a month" -- and the gap rule splits that into two threads,
# which reads as two unrelated things. Both levels are
# real and they answer different questions:
#
#   thread  = an episode. Gap-bounded. "How much did this sitting cost?"
#   stream  = the recurring piece of work. NO gap limit. "How much do I pay,
#             every month, to re-learn this?"
#
# Re-orienting after a 21-day gap is re-orientation too -- probably a bigger
# one -- so the tax is computed at BOTH levels and reported side by side.
#
# THE KEY IS NOT cwd ALONE: many people do all their work for different
# streams from the same working directory, and a cwd-only key would fuse a
# whole year of such work into one stream. So a link needs cwd
# PLUS a discriminator, or a ticket token that crosses cwds:
#
#   folder+branch : same cwd AND the same non-trivial gitBranch. Claude Code
#                   records `gitBranch` on every transcript record (verified on
#                   real transcripts: present on every assistant row).
#   folder+files  : same cwd AND >= 2 shared file basenames.
#   ticket        : a shared work-item / ticket / PR token in the first prompt
#                   or the session title -- this one joins ACROSS cwds, which
#                   is the case where the same ticket is worked in two repos.
#
# Every stream reports WHICH rule named it, and the join count per rule is
# printed so an over-joining rule (a generic `#1`) can be switched off with
# --stream-ignore-token.
# ==========================================================================

# Every pattern that matches adds a token; a stream joined on tickets is
# named by the alphabetically first token it shares. `#123` is GitHub-style
# (an issue or a PR), `PR 123` an explicit pull request, `ABC-123` Jira- or
# Linear-shaped, `feature/x` a branch-like token; Azure Boards (`AB#123`,
# `work item 123`) is recognised too. A bare `#123` is the token most likely
# to over-join, which is why --stream-ignore-token exists.
TICKET_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("hash", re.compile(r"(?<![\w#])#(\d{2,})\b")),
    ("pr", re.compile(r"\bPR[ #]*(\d+)\b", re.IGNORECASE)),
    ("jira", re.compile(r"\b([A-Z]{2,}-\d+)\b")),
    ("branch-token", re.compile(r"\b((?:feature|feat|fix|bugfix|hotfix|release|chore)/[\w.-]+)")),
    ("azure-boards", re.compile(r"\bAB#(\d+)\b")),
    ("work-item", re.compile(r"\bwork item[ #]*(\d+)\b", re.IGNORECASE)),
]
# Branches everyone is always on carry no stream information.
GENERIC_BRANCHES = {"main", "master", "trunk", "develop", "dev", "default", "head", ""}


def ticket_tokens(session: "Session", ignore: Sequence[str] = ()) -> Dict[str, str]:
    """kind -> token, read from the session TITLE and FIRST PROMPT only.

    Deliberately not from tool arguments or file contents: a ticket id that
    appears in a grep result is not evidence the session is about that ticket.
    """
    text = " ".join(filter(None, [session.title, session.first_prompt]))[:600]
    found: Dict[str, str] = {}
    lowered = {str(i).lower() for i in ignore}
    for kind, pattern in TICKET_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        token = "%s:%s" % (kind, match.group(1))
        if token.lower() in lowered or match.group(1).lower() in lowered:
            continue
        found[kind] = token
    return found


class Stream:
    """A connected component of threads. Contains episodes, has no gap limit."""

    def __init__(self, stream_id: str, threads: Sequence["Thread"],
                 named_by: str, name: str, evidence: Sequence[str] = ()):
        self.id = stream_id
        self.threads = sorted(threads, key=lambda t: t.start or dt.datetime.max.replace(
            tzinfo=dt.timezone.utc))
        self.named_by = named_by            # folder | branch | ticket
        self.name = name
        self.evidence = list(evidence)

    @property
    def sessions(self) -> List["Session"]:
        every = [s for t in self.threads for s in t.sessions]
        return sorted(every, key=lambda s: s.start or dt.datetime.max.replace(
            tzinfo=dt.timezone.utc))

    @property
    def start(self):
        return min((t.start for t in self.threads if t.start), default=None)

    @property
    def end(self):
        return max((t.end for t in self.threads if t.end), default=None)

    def gaps_days(self) -> List[float]:
        """Days between two episodes, ON THE SAME REFERENCE AS THE RULE.

        `build_threads` splits on the gap between one
        session's START and the previous session's START, but this used to
        report the gap between the previous episode's END and the next one's
        start. The two differ by the length of the last sitting, so the
        timeline annotated roughly-two-day gaps as ">72 h, so the rule split
        them". One reference point now: START to START, the same quantity the
        rule tests. `wall_gaps_days` keeps the end-to-start figure, which is
        the one a human means by "how long was I away".
        """
        out = []
        ordered = [t for t in self.threads if t.start and t.end]
        for previous, following in zip(ordered, ordered[1:]):
            last_start = max((s.start for s in previous.sessions if s.start),
                             default=previous.start)
            out.append(round((following.start - last_start).total_seconds() / 86400.0, 2))
        return out

    def wall_gaps_days(self) -> List[float]:
        """Days of wall clock between one episode ending and the next starting."""
        out = []
        ordered = [t for t in self.threads if t.start and t.end]
        for previous, following in zip(ordered, ordered[1:]):
            out.append(round((following.start - previous.end).total_seconds() / 86400.0, 2))
        return out


def _stream_links(threads: Sequence["Thread"], stream_by: str,
                  ignore_tokens: Sequence[str]) -> Tuple[List[Tuple[int, int, str, str]],
                                                         "collections.Counter[str]"]:
    """Every pairwise link, with the rule that made it. O(n^2) over threads."""
    profile = []
    for thread in threads:
        cwd = str(thread.cwd or "unknown")
        branches = {s.git_branch for s in thread.sessions
                    if s.git_branch and str(s.git_branch).strip().lower() not in GENERIC_BRANCHES}
        files: set = set()
        for session in thread.sessions:
            files |= resource_tokens(session)
        tickets: set = set()
        for session in thread.sessions:
            tickets |= set(ticket_tokens(session, ignore_tokens).values())
        profile.append((cwd, branches, files, tickets))
    links: List[Tuple[int, int, str, str]] = []
    counts: "collections.Counter[str]" = collections.Counter()
    for i in range(len(threads)):
        for j in range(i + 1, len(threads)):
            cwd_i, br_i, f_i, tk_i = profile[i]
            cwd_j, br_j, f_j, tk_j = profile[j]
            rule = evidence = None
            shared_tickets = tk_i & tk_j
            if shared_tickets:
                rule, evidence = "ticket", sorted(shared_tickets)[0]
            elif cwd_i == cwd_j:
                if stream_by == "cwd":
                    rule, evidence = "folder", folder_name(cwd_i)
                else:
                    shared_branch = br_i & br_j
                    shared_files = f_i & f_j
                    if shared_branch:
                        rule, evidence = "branch", sorted(shared_branch)[0]
                    elif len(shared_files) >= 2:
                        rule, evidence = "files", ", ".join(sorted(shared_files)[:3])
            if rule:
                links.append((i, j, rule, evidence))
                counts[rule] += 1
    return links, counts


def build_streams(threads: Sequence["Thread"], stream_by: str = "auto",
                  ignore_tokens: Sequence[str] = ()) -> Tuple[List[Stream], Dict[str, Any]]:
    """Connected components over the links. Returns (streams, join stats).

    A single-thread stream is still a stream: it is one episode of a piece of
    work that has not recurred yet, and dropping it would make the stream tax
    look smaller than it is.
    """
    parent = list(range(len(threads)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    links, counts = _stream_links(threads, stream_by, ignore_tokens)
    merges: "collections.Counter[str]" = collections.Counter()
    reasons: Dict[int, List[Tuple[str, str]]] = collections.defaultdict(list)
    for i, j, rule, evidence in links:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
            merges[rule] += 1
        reasons[find(i)].append((rule, evidence))

    groups: Dict[int, List[int]] = collections.defaultdict(list)
    for index in range(len(threads)):
        groups[find(index)].append(index)

    streams: List[Stream] = []
    for counter, (root, members) in enumerate(
            sorted(groups.items(),
                   key=lambda kv: min((threads[i].start or dt.datetime.max.replace(
                       tzinfo=dt.timezone.utc)) for i in kv[1])), 1):
        member_threads = [threads[i] for i in members]
        # What NAMED the stream: the most specific rule that joined it, else
        # the folder it lives in. A reader must be able to see why these
        # sessions were grouped, so the evidence travels with the name.
        rules = reasons.get(root) or []
        by_rule = {rule: evidence for rule, evidence in rules}
        if "ticket" in by_rule:
            named_by, name = "ticket", by_rule["ticket"]
        elif "branch" in by_rule:
            named_by, name = "branch", by_rule["branch"]
        else:
            named_by = "folder"
            name = folder_name(member_threads[0].cwd)
        streams.append(Stream("S%03d" % counter, member_threads, named_by, name,
                              sorted({"%s: %s" % (r, e) for r, e in rules})[:6]))
    stats = {
        "stream_by": stream_by,
        "streams": len(streams),
        "multi_episode_streams": sum(1 for s in streams if len(s.threads) > 1),
        "threads_in": len(threads),
        "links_by_rule": dict(counts.most_common()),
        "merges_by_rule": dict(merges.most_common()),
        "ignored_tokens": list(ignore_tokens),
        "rule_note": ("auto: two sittings are one work item if they share a ticket token "
                      "(any folder), or the same folder plus the same non-default branch, "
                      "or the same folder plus at least 2 shared file names. The folder "
                      "alone is not enough: many people run all their work from one folder."
                      if stream_by != "cwd" else
                      "cwd: every sitting in the same folder is one work item. Simple, and "
                      "wrong for anyone who works from one folder."),
    }
    return streams, stats


def stream_record(stream: Stream, redact: bool) -> Dict[str, Any]:
    members = stream.sessions
    orientation = [m.metrics.get("orientation_tokens") for m in members]
    # STARTUP SPEND IN CANDIDATE RECURRING WORK: the
    # orientation of every session after the stream's FIRST -- including the
    # first session of every later episode, which the per-thread figure cannot
    # see. It is NOT a measure of what a gap COST: no uninterrupted-work
    # counterfactual is observed.
    tax = sum(v for v in orientation[1:] if v is not None) if len(orientation) > 1 else 0
    total = sum(m.metrics.get("total_tokens", 0) for m in members)
    cost = sum(m.metrics.get("est_cost_usd") or 0.0 for m in members)
    # Directly priced. The old formula was
    # `session cost x share`, which also silently priced every no-boundary
    # session at $0 while counting all of its tokens in `tax`.
    tax_cost = sum(m.metrics.get("orientation_cost_usd") or 0.0 for m in members[1:])
    unmeasured = unmeasured_boundary(members[1:])
    return {
        "stream_id": stream.id,
        "name": stream.name if not redact else stable_hash(stream.name),
        "named_by": stream.named_by,
        "evidence": stream.evidence if not redact else len(stream.evidence),
        "threads": [t.id for t in stream.threads],
        "episodes": len(stream.threads),
        "sessions": len(members),
        "first": iso(stream.start),
        "last": iso(stream.end),
        "gaps_days": stream.gaps_days(),
        "max_gap_days": max(stream.gaps_days()) if stream.gaps_days() else None,
        "total_tokens": total,
        "orientation_tokens": sum(v for v in orientation if v is not None),
        "reorientation_tax_tokens": tax,
        "reorientation_tax_share": round(tax / total, 4) if total else None,
        "est_cost_usd": round(cost, 4),
        "reorientation_tax_usd": round(tax_cost, 4),
        "successors_without_boundary": unmeasured,
        "handoff_opened_sessions": sum(1 for m in members
                                       if m.metrics.get("handoff_opened_by_turn_2")),
        "cwd": (stable_hash(stream.threads[0].cwd) if redact else stream.threads[0].cwd),
    }


# ==========================================================================
# CARRY OR RESTART? The cost of continuing a session versus the cost of
# starting a fresh one.
#
# WHY. The common advice for orientation cost is to have the model write a
# handoff note. Whether a note helps is not measured here. What history CAN
# show is the other option: not starting a new session at all.
#
# HOW A RESUME APPEARS IN CLAUDE CODE TRANSCRIPTS: there is NO explicit
# marker.
#
#   * one file is one `sessionId`, and the file's basename is that id. A
#     resume does not open a new transcript and does not mint a new id.
#   * `entrypoint` is constant within a session, so it does not record a
#     re-entry.
#   * `away_summary` system rows are the idle recap the CLI writes minutes
#     after you STEP AWAY, not a return marker. Not usable as the classifier.
#   * a change of the CLI `version` field inside one session DOES prove the
#     process restarted, but it is rare. A validation signal, not a detector.
#   * `compact_boundary` system rows carry the compaction directly and are
#     what R1 is keyed on.
#
# So R0 is observable ONLY as a wall-clock gap between two consecutive
# main-conversation responses inside one session id -- and the cost
# consequence of that gap is directly visible in the next response's own
# usage buckets. Within the one-hour cache lifetime a resume usually reads its
# context from the cache; after it, the context is written again. Hence the
# default `--resume-gap-hours 1.0`. The pricing NEVER assumes which: every
# figure is summed from the response's own buckets by `turn_cost_usd`.
# ==========================================================================
RESUME_GAP_HOURS = 1.0

CONTINUATION_KINDS = ("R0", "R1", "R2", "first", "parallel")
# The kinds are machine values (ledger, JSON); a reader only ever sees these.
CONTINUATION_LABEL = {
    "R0": "resumed after a pause",
    "R1": "resumed after compaction",
    "R2": "new session",
    "first": "first session",
    "parallel": "ran in parallel",
}


def _prefix_mode(turn: "Turn") -> str:
    """Did this response re-establish its prefix by READING or by WRITING it?

    Read straight off the response's own buckets. `mixed` is a real third
    answer: a resume can read a surviving prefix and write the part of it the
    cache no longer holds, and calling that either name would be a guess.
    """
    read = turn.cread
    write = turn.cw5 + turn.cw1
    if write and read and min(read, write) / float(max(read, write)) > 0.25:
        return "mixed"
    return "cache-write" if write >= read else "cache-read"


def carry_events(session: "Session", prices: Dict[str, Any],
                 gap_hours: float = RESUME_GAP_HOURS) -> List[Dict[str, Any]]:
    """Every IN-SESSION continuation of this session's work: R0 and R1.

    R1 is keyed on a `compact_boundary` row, with or without a clock gap --
    a compaction is a re-entry into a context the session no longer holds
    even when it fires mid-flow. R0 is keyed on a clock gap of at least
    `gap_hours` between consecutive main-conversation responses, EXCLUDING
    gaps that contain a compaction (that gap's continuation is the
    compaction, and counting it twice would double the stream's carry bill).

    The cost of one event is the responses from the first one after the
    re-entry up to, but not including, the first response that performs a
    successful mutation after it -- the same window rule the orientation
    boundary uses, so an R0/R1 figure and an R2 figure are the same kind of
    quantity and may be put side by side. The prefix write or read is inside
    that first response's own buckets, so it is priced without being modelled.
    """
    main = [t for t in session.turns if t.agent is None and t.ts]
    if len(main) < 2:
        return []
    failed = getattr(session, "failed_tool_ids", set()) or set()

    def mutates(turn: "Turn") -> bool:
        return any(call.mutating and not (call.use_id is not None and call.use_id in failed)
                   for call in turn.tools)

    compactions = [c for c in getattr(session, "compactions", []) or [] if c.get("ts")]
    events: List[Dict[str, Any]] = []
    used: set = set()

    def add(kind: str, index: int, gap: Optional[float],
            compaction: Optional[Dict[str, Any]]) -> None:
        if index in used:
            return
        used.add(index)
        stop = len(main)
        for j in range(index, len(main)):
            if mutates(main[j]):
                stop = j
                break
        window = main[index:stop]
        first = main[index]
        events.append({
            "kind": kind,
            "session_id": session.session_id,
            "at": iso(first.ts),
            "turn": first.main_index,
            "gap_hours": (round(gap, 3) if gap is not None else None),
            "carry_tokens": sum(t.tokens for t in window),
            "carry_cost_usd": round(sum(turn_cost_usd(t, prices) for t in window), 6),
            "responses": len(window),
            # The response that performs the first change after the re-entry
            # is NOT counted, exactly as the orientation boundary is not
            # counted -- otherwise one batched response (re-read everything,
            # then edit) would count wholly as carry. When that response IS
            # the first one after the gap the window is empty and the carry
            # reads 0, so its size is published here the way the orientation
            # boundary publishes `boundary_turn_tokens`, and the panel prints
            # how many events that happened to.
            "boundary_turn_tokens": (main[stop].tokens if stop < len(main) else None),
            "empty_window": not window,
            "reached_mutation": stop < len(main),
            "prefix_mode": _prefix_mode(first),
            "prefix_read_tokens": first.cread,
            "prefix_write_tokens": first.cw5 + first.cw1,
            "model": first.model,
            "effort": first.effort,
            # MODELLED, never added to carry_cost_usd: the summarising call is
            # not written to the transcript with a usage object, so its price
            # cannot be observed. compact_economics.py models it the same way.
            "modeled_summary_usd": (_modeled_summary_usd(compaction, first, prices)
                                    if compaction else None),
            "compact_trigger": (compaction or {}).get("trigger"),
            "compact_pre_tokens": (compaction or {}).get("pre_tokens"),
            "compact_post_tokens": (compaction or {}).get("post_tokens"),
        })

    for compaction in compactions:
        after = [i for i, t in enumerate(main) if t.ts > compaction["ts"]]
        if not after:
            continue
        index = after[0]
        before = [t for t in main[:index] if t.ts]
        gap = ((main[index].ts - before[-1].ts).total_seconds() / 3600.0) if before else None
        add("R1", index, gap, compaction)

    for index in range(1, len(main)):
        gap = (main[index].ts - main[index - 1].ts).total_seconds() / 3600.0
        if gap < gap_hours:
            continue
        if any(main[index - 1].ts < c["ts"] <= main[index].ts for c in compactions):
            continue                       # already counted as the R1 event
        add("R0", index, gap, None)

    events.sort(key=lambda e: (e["turn"] if e["turn"] is not None else 0))
    return events


def _modeled_summary_usd(compaction: Dict[str, Any], first: "Turn",
                         prices: Dict[str, Any]) -> Optional[float]:
    """MODELLED price of the compaction's own summarising call.

    Not observed: no assistant record with a usage object is written for it
    (checked against real transcripts). Modelled the way
    `compact_economics.py` models it -- the pre-compaction context at the
    cache-read multiplier, plus the summary itself at the output rate --
    and reported in its own column so no observed total is contaminated.
    """
    rate = price_for(first.model, prices)
    if not rate or compaction.get("pre_tokens") is None:
        return None
    post = compaction.get("post_tokens") or 0
    return round((compaction["pre_tokens"] * CACHE_READ_MULT * rate[0]
                  + post * rate[1]) / 1e6, 6)


def classify_continuations(sessions: Sequence["Session"], streams: Sequence["Stream"],
                           prices: Dict[str, Any],
                           gap_hours: float = RESUME_GAP_HOURS) -> Dict[str, Any]:
    """Every continuation event in the history, plus the per-session column.

    THE UNIT IS AN EVENT, not a session. A session can be entered as a fresh
    restart AND resume inside itself; both cost something and both are
    counted. The ledger needs one value per session, so `continuation_kind`
    collapses them by a stated precedence -- **R2 > R1 > R0 > first** -- and
    `carry_tokens`/`carry_cost_usd` are the cost of THAT event, with the full
    event list kept in `orientation.json`. The precedence is R2-first because
    R2 describes how the work arrived at this session, which is the
    continuation the "carry or restart" question is about; R0/R1 describe
    re-entries within a session that had already started.

    R2's cost is the successor's directly-priced orientation -- the tokens it
    spent before its own first successful change. A successor with no
    observed boundary has an UNKNOWN restart cost and is kept in its own
    category, never counted as zero.

    PARALLEL SESSIONS ARE NOT CONTINUATIONS. A session that starts before an
    earlier session of the same stream has ended ran beside it; it did not
    pick the work back up, and its "gap" would be negative. It gets the kind
    `parallel`, no R2 event, and is counted in `parallel_sessions`. The gap
    of a real restart is measured from the LATEST end among the earlier
    sessions, so a parallel session in between cannot shorten it.
    """
    by_id = {s.session_id: s for s in sessions}
    events: List[Dict[str, Any]] = []
    per_session: Dict[str, Dict[str, Any]] = {}
    stream_of: Dict[str, "Stream"] = {}
    unknown_restart = {"sessions": 0, "tokens": 0, "est_cost_usd": 0.0}
    parallel: set = set()

    for stream in streams:
        members = stream.sessions
        for session in members:
            stream_of[session.session_id] = stream
        latest_end = None
        for index, session in enumerate(members):
            previous_end = latest_end
            if session.end and (latest_end is None or session.end > latest_end):
                latest_end = session.end
            if index == 0:
                continue
            gap = None
            if previous_end and session.start:
                gap = round((session.start - previous_end).total_seconds() / 3600.0, 3)
            if gap is not None and gap < 0:
                parallel.add(session.session_id)      # ran beside the earlier one
                continue
            metrics = session.metrics
            if metrics.get("orientation_tokens") is None:
                unknown_restart["sessions"] += 1
                unknown_restart["tokens"] += metrics.get("total_tokens") or 0
                unknown_restart["est_cost_usd"] += metrics.get("est_cost_usd") or 0.0
            events.append({
                "kind": "R2",
                "session_id": session.session_id,
                "stream_id": stream.id,
                "at": iso(session.start),
                "turn": 0,
                "gap_hours": gap,
                "carry_tokens": metrics.get("orientation_tokens"),
                "carry_cost_usd": metrics.get("orientation_cost_usd"),
                "responses": metrics.get("orientation_turns"),
                "reached_mutation": not metrics.get("no_mutation"),
                "prefix_mode": None,        # a fresh session has no carried prefix
                "boundary_turn_tokens": metrics.get("boundary_turn_tokens"),
                "empty_window": metrics.get("orientation_tokens") == 0,
                "model": (session.models or [None])[0],
                "effort": metrics.get("effort"),
                "modeled_summary_usd": None,
            })

    for session in sessions:
        stream = stream_of.get(session.session_id)
        inner = carry_events(session, prices, gap_hours)
        for event in inner:
            event["stream_id"] = stream.id if stream else None
        events.extend(inner)
        per_session[session.session_id] = {
            "events": inner,
            "compactions": len(getattr(session, "compactions", []) or []),
        }

    restart_of = {e["session_id"]: e for e in events if e["kind"] == "R2"}
    for session_id, row in per_session.items():
        inner = row["events"]
        restart = restart_of.get(session_id)
        if restart is not None:
            chosen, kind = restart, "R2"
        else:
            r1 = next((e for e in inner if e["kind"] == "R1"), None)
            r0 = next((e for e in inner if e["kind"] == "R0"), None)
            chosen = r1 or r0
            kind = ("R1" if r1 else "R0" if r0
                    else "parallel" if session_id in parallel else "first")
        row["continuation_kind"] = kind
        row["carry_tokens"] = chosen.get("carry_tokens") if chosen else None
        row["carry_cost_usd"] = chosen.get("carry_cost_usd") if chosen else None
        row["gap_hours_before"] = chosen.get("gap_hours") if chosen else None

    kinds: Dict[str, List[Dict[str, Any]]] = {k: [] for k in ("R0", "R1", "R2")}
    for event in events:
        kinds[event["kind"]].append(event)
    by_stream: Dict[str, "collections.Counter[str]"] = collections.defaultdict(
        collections.Counter)
    for event in events:
        if event.get("stream_id"):
            by_stream[event["stream_id"]][event["kind"]] += 1
    paired = sorted(sid for sid, counter in by_stream.items()
                    if counter.get("R2") and (counter.get("R0") or counter.get("R1")))
    unknown_restart["est_cost_usd"] = round(unknown_restart["est_cost_usd"], 2)
    return {
        "gap_hours": gap_hours,
        "events": events,
        "per_session": per_session,
        "counts": {k: len(v) for k, v in kinds.items()},
        "sessions_by_kind": dict(collections.Counter(
            row["continuation_kind"] for row in per_session.values())),
        "paired_streams": paired,
        "streams_with_events": len(by_stream),
        "restarts_without_boundary": unknown_restart,
        "parallel_sessions": len(parallel),
        "rule": ("a pause = a gap of at least %s between two responses in one session "
                 "with no compaction in it; a compaction = a /compact; a new session = a "
                 "later session of the same work item that did not run in parallel with "
                 "an earlier one. Cost = the responses from the restart to the first edit "
                 "after it, each priced from its own buckets." % hours_text(gap_hours)),
    }


def continuation_summary(continuations: Dict[str, Any]) -> Dict[str, Any]:
    """Medians per kind, over EVENTS, with the denominator for each one."""
    out: Dict[str, Any] = {}
    for kind in ("R0", "R1", "R2"):
        rows = [e for e in continuations["events"] if e["kind"] == kind]
        priced = [e for e in rows if e.get("carry_tokens") is not None]
        out[kind] = {
            "events": len(rows),
            "measured": len(priced),
            "median_tokens": median_or_none([e["carry_tokens"] for e in priced]),
            "median_cost_usd": median_or_none([e.get("carry_cost_usd") or 0.0
                                               for e in priced]),
            "total_tokens": sum(e["carry_tokens"] for e in priced),
            "total_cost_usd": round(sum(e.get("carry_cost_usd") or 0.0 for e in priced), 4),
            "median_gap_hours": median_or_none([e["gap_hours"] for e in rows
                                                if e.get("gap_hours") is not None]),
            # Events whose very next response was itself the first change, so
            # the measured window is empty and the figure is 0 by definition.
            "empty_windows": sum(1 for e in priced if e.get("empty_window")),
            "median_boundary_turn_tokens": median_or_none(
                [e["boundary_turn_tokens"] for e in rows
                 if e.get("boundary_turn_tokens") is not None]),
        }
    return out


# ---------------------------------------------------------------- calibration
def load_labels(path: str) -> Dict[str, Any]:
    """thread_id -> yes|no|<int k pieces of work>. TSV, CSV or JSON."""
    labels: Dict[str, Any] = {}
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        for key, value in json.loads(text).items():
            labels[str(key)] = value
        return labels
    for row in csv.reader(text.splitlines(), delimiter="\t" if "\t" in text else ","):
        if not row or row[0].strip().startswith("#"):
            continue
        if len(row) < 2:
            continue
        labels[row[0].strip()] = row[1].strip()
    return labels


def label_value(raw: Any) -> Optional[int]:
    """Return k = number of distinct pieces of work, or None if unusable."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in ("yes", "y", "true", "coherent", "1"):
        return 1
    if text in ("no", "n", "false", "incoherent"):
        return 2                      # at least two pieces of work
    if text.isdigit():
        return max(1, int(text))
    return None


def calibrate(threads: Sequence[Thread], labels: Dict[str, Any],
              sessions: Sequence[Session], gap_hours: float, min_shared: int,
              max_sessions: int) -> Dict[str, Any]:
    """Pair-level precision against hand labels, plus a threshold sweep.

    A label of `yes` means the thread is one piece of work; `no` means at
    least two; an integer k means exactly k. Pair precision assumes the k
    pieces split the thread evenly -- stated, because it is an assumption.
    RECALL is reported only when the label file marks a thread as continuing
    another (`same_as:<thread_id>`); labels of the yes/no form cannot see a
    join the rule MISSED, so recall is null and says why.
    """
    # A label binds to the SET OF SESSION IDS it was
    # written about, never to the `Txxx` id. `Txxx` ids are assigned by
    # `build_threads` in encounter order, so the moment the sweep rebuilds the
    # clusters at a different threshold, `T007` is a DIFFERENT set of sessions
    # and the old label lands on it. A four-session probe reported
    # precision 0.75 for a partition with exactly one correctly grouped pair,
    # because a "two pieces of work" label had migrated onto a new singleton.
    by_id = {t.id: t for t in threads}
    label_sets: Dict[str, "frozenset[str]"] = {
        t.id: frozenset(s.session_id for s in t.sessions) for t in threads}
    grouped = coherent = 0
    labelled = 0
    migrated = 0
    per_thread = []
    same_as: Dict[str, str] = {}
    for thread_id, raw in labels.items():
        text = str(raw).strip()
        if text.lower().startswith("same_as:"):
            same_as[thread_id] = text.split(":", 1)[1].strip()
            continue
        k = label_value(raw)
        thread = by_id.get(thread_id)
        if thread is None or k is None:
            continue
        labelled += 1
        m = len(thread.sessions)
        pairs = m * (m - 1) // 2
        piece = m / k
        coherent_pairs = k * (piece * (piece - 1) / 2.0)
        grouped += pairs
        coherent += coherent_pairs
        per_thread.append({"thread_id": thread_id, "sessions": m, "pieces": k,
                           "grouped_pairs": pairs, "coherent_pairs": round(coherent_pairs, 1)})
    precision = (coherent / grouped) if grouped else None

    recall = None
    recall_note = ("labels of the yes/no/k form cannot identify a join the rule MISSED; "
                   "add `same_as:<thread_id>` rows to measure recall")
    if same_as:
        # Compare the SESSION-ID SETS, not the thread objects. `by_id[a] is
        # by_id[b]` compares two distinct dictionary values and is therefore
        # False for every distinct pair of ids -- recall could never be
        # anything but 0.
        hits = 0
        for a, b in same_as.items():
            set_a, set_b = label_sets.get(a), label_sets.get(b)
            if set_a is not None and set_b is not None and (set_a & set_b):
                hits += 1
        recall = round(hits / len(same_as), 3)
        recall_note = ("over %d declared continuations, matched on shared session ids"
                       % len(same_as))

    sweep = []
    for gap_candidate in (24, 48, 72, 120, 168):
        for shared_candidate in (1, 2, 3, 4):
            candidate = build_threads(sessions, gap_candidate, shared_candidate, max_sessions)
            g = c = 0
            matched = 0
            for thread_id, raw in labels.items():
                k = label_value(raw)
                wanted = label_sets.get(thread_id)
                if k is None or not wanted:
                    continue
                # The candidate cluster this label is ABOUT: the one holding
                # the labelled sessions. A label scores only when the
                # partition still keeps those sessions together; when the
                # threshold splits them, that is a real MISS, not a free pass.
                hit = next((t for t in candidate
                            if frozenset(s.session_id for s in t.sessions) == wanted), None)
                if hit is None:
                    continue
                matched += 1
                m = len(hit.sessions)
                g += m * (m - 1) // 2
                piece = m / k
                c += k * (piece * (piece - 1) / 2.0)
            multi = sum(1 for t in candidate if len(t.sessions) > 1)
            sweep.append({"gap_hours": gap_candidate, "min_shared": shared_candidate,
                          "threads": len(candidate), "multi_session_threads": multi,
                          "largest": max((len(t.sessions) for t in candidate), default=0),
                          "labels_matched": matched,
                          "labels_total": sum(1 for r in labels.values()
                                              if label_value(r) is not None),
                          "pair_precision": round(c / g, 3) if g else None})
    scored = [row for row in sweep if row["pair_precision"] is not None]
    best = max(scored, key=lambda r: (r["pair_precision"], r["multi_session_threads"])) if scored else None
    if not labelled:
        # With no usable labels there is no precision to report, and
        # the sweep must say so rather than printing a blank column that reads
        # as "fine".
        best = None
    return {
        "status": "ok" if labelled else "uncalibrated",
        "uncalibrated_note": (None if labelled else
                              "no usable label matched a cluster in this partition — no "
                              "precision figure is reported"),
        "label_binding": ("labels bind to the SET OF SESSION IDS they were written about, "
                          "never to a Txxx id, which is reassigned whenever the thresholds "
                          "change"),
        "labelled_threads": labelled,
        "labels_migrated": migrated,
        "pair_precision": round(precision, 3) if precision is not None and labelled else None,
        "recall": recall,
        "recall_note": recall_note,
        "per_thread": per_thread,
        "sweep": sweep,
        "suggested": best,
        "current": {"gap_hours": gap_hours, "min_shared": min_shared,
                    "max_sessions": max_sessions},
        "assumption": "pair precision assumes a thread's k pieces of work split its sessions evenly",
    }


# ==========================================================================
# HANDOFF-NOTE READ-BACK -- does writing a short handover note pay for itself?
#
# The user logs one line per session END: a coin flip decided whether a short
# handover note was written. This joins each logged end to the NEXT session in
# the same thread and reports the paired comparison. The CSV format and the
# rules the read-back follows: references/orientation-and-threads.md §2b.
#
# Nothing here dispatches anything. It reads a CSV and local session logs.
# ==========================================================================
# The log records the SESSION IDS on both sides of
# the handover. Pairing used to be inferred -- "the thread whose latest
# session started at or before this timestamp", then "the next session in that
# thread" -- which mis-assigns concurrent same-folder sessions and cannot see
# a long-gap successor. The two id columns are OPTIONAL (an older log still
# parses, and its rows are marked `inferred`), but they are what the template
# asks for now.
HANDOFF_COLUMNS = ("timestamp", "tool", "cwd", "arm", "note_path_or_empty", "note_minutes",
                   "predecessor_session_id", "successor_session_id")
HANDOFF_ARMS = ("NOTE", "NONE")

# The stopping rule, stated in advance. Printed whenever the log is
# under-powered, so the honest reading travels with the table instead of being
# reconstructed later.
UNDERPOWERED_BANNER = (
    "Smallest effect this can detect, set in advance: 16 usable pairs detect \"the note\n"
    "wins in about 3 sittings out of 4\" and nothing smaller. A real but modest effect\n"
    "(the note helps 60 % of the time) will not show at that size. The honest report\n"
    "of that result is \"underpowered\", not \"no effect\".\n"
    "  (references/orientation-and-threads.md §2b, \"What the read-back does\")")

MIN_USABLE_PAIRS = 16          # the stated floor: 13 of 16 for p = 0.021
# A pair is a sitting contributing BOTH arms, so it needs at least two
# successors, and N successors yield at most floor(N/2) pairs. A rule that
# stopped on a successor count could stop before reaching its own floor, so the
# stopping unit is the pair.
MIN_SUCCESSORS_FOR_FLOOR = 2 * MIN_USABLE_PAIRS
STOPPING_RULE = (
    "stop at %d usable pairs (a sitting with successors in both arms) or 12 weeks, "
    "whichever comes first. %d pairs need at least %d successor sessions, and in "
    "practice more, because two successors in the same arm make no pair. The "
    "successor count shows progress; it is never the stopping unit."
    % (MIN_USABLE_PAIRS, MIN_USABLE_PAIRS, MIN_SUCCESSORS_FOR_FLOOR))
MEDIATOR_FLOOR = 0.80          # below this the result is about discoverability


def parse_handoff_log(path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Read the hand-maintained field log. Returns (entries, skipped).

    Written to survive a file a human edits in place: a trailing comma, a
    blank `note_minutes`, a re-typed header, a `#` note to self. A line that
    cannot be read is REPORTED AND SKIPPED with its line number and the reason
    -- never silently dropped, and never coerced to a default. An unknown
    value is never zero. A blank `note_minutes` is therefore None
    (the pair still counts for orientation, and is excluded from net value
    with that exclusion printed), not 0.
    """
    entries: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = next(csv.reader([line]), [])
        fields = [f.strip() for f in fields]
        while fields and fields[-1] == "" and len(fields) > len(HANDOFF_COLUMNS):
            fields.pop()                       # a trailing comma from the shell helper
        if [f.lower() for f in fields[:4]] == list(HANDOFF_COLUMNS[:4]):
            continue                           # a header line, re-pasted
        if len(fields) < 4:
            skipped.append({"line": number, "reason": "expected %d columns, found %d"
                            % (len(HANDOFF_COLUMNS), len(fields)), "text": line[:120]})
            continue
        fields += [""] * (len(HANDOFF_COLUMNS) - len(fields))
        stamp = parse_timestamp(fields[0])
        if stamp is None:
            skipped.append({"line": number, "reason": "unreadable timestamp %r" % fields[0],
                            "text": line[:120]})
            continue
        arm = fields[3].upper()
        if arm not in HANDOFF_ARMS:
            skipped.append({"line": number, "reason": "arm %r is not NOTE or NONE" % fields[3],
                            "text": line[:120]})
            continue
        minutes: Optional[float]
        if fields[5] == "":
            minutes = None                     # unknown, NOT zero
        else:
            try:
                minutes = float(fields[5])
            except ValueError:
                skipped.append({"line": number,
                                "reason": "note_minutes %r is not a number (left as unknown "
                                          "would be fine; a wrong number is not)" % fields[5],
                                "text": line[:120]})
                continue
        entries.append({"line": number, "at": stamp, "tool": fields[1] or None,
                        "cwd": fields[2] or None, "arm": arm,
                        "note_path": fields[4] or None, "note_minutes": minutes,
                        # explicit ids beat an inferred join
                        "predecessor_session_id": fields[6] or None,
                        "successor_session_id": fields[7] or None})
    entries.sort(key=lambda e: e["at"])
    return entries, skipped


def join_handoff_log(entries: Sequence[Dict[str, Any]], threads: Sequence[Thread],
                     redact: bool = False) -> Dict[str, Any]:
    """Join each logged END to the NEXT session in the SAME thread.

    Uses this module's own thread objects -- there is deliberately no second
    threading implementation here, because a comparison whose two halves
    cluster differently is not a comparison. The key is
    (thread, first session strictly after the logged timestamp).

    Three outcomes, kept apart because they mean different things:
      PAIRED    -- a successor exists and is measured.
      PENDING   -- the thread is real but has no successor YET. Not a data
                   point, and counted so it can never be mistaken for one.
      UNMATCHED -- no thread in the local history matches that cwd at all
                   (the tool's store is not ingested here, or the log's cwd
                   does not match the session's).
    """
    by_cwd: Dict[str, List[Thread]] = collections.defaultdict(list)
    by_session: Dict[str, Tuple[Thread, Session]] = {}
    for thread in threads:
        by_cwd[str(thread.cwd or "unknown")].append(thread)
        for member in thread.sessions:
            by_session[member.session_id] = (thread, member)

    rows: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    for entry in entries:
        target = str(entry["cwd"] or "unknown")
        # An EXPLICIT successor id short-circuits the inference.
        declared = entry.get("successor_session_id")
        if declared and declared in by_session:
            best, successor = by_session[declared]
            rows.append(_handoff_row(entry, best, successor, target, redact, "declared"))
            continue
        if declared:
            unmatched.append({"line": entry["line"], "at": iso(entry["at"]),
                              "cwd": stable_hash(target) if redact else target,
                              "arm": entry["arm"],
                              "why": "the log names successor %s, which is not in the "
                                     "ingested history" % declared[:8]})
            continue
        candidates = by_cwd.get(target)
        if not candidates:                     # tolerate a trailing slash, case drift or ~
            wanted = os.path.normpath(os.path.expanduser(target)).rstrip("/\\").lower()
            candidates = [t for key, group in by_cwd.items() for t in group
                          if os.path.normpath(key).rstrip("/\\").lower() == wanted]
        if not candidates:
            unmatched.append({"line": entry["line"], "at": iso(entry["at"]),
                              "cwd": stable_hash(target) if redact else target,
                              "arm": entry["arm"],
                              "why": "no thread in the ingested history has this cwd"})
            continue
        # The thread being ended is the one with a session closest at-or-before
        # the logged moment; ties broken by the later start.
        best: Optional[Thread] = None
        best_gap: Optional[float] = None
        for thread in candidates:
            prior = [s for s in thread.sessions if s.start and s.start <= entry["at"]]
            if not prior:
                continue
            gap = (entry["at"] - max(s.start for s in prior)).total_seconds()
            if best_gap is None or gap < best_gap:
                best, best_gap = thread, gap
        if best is None:
            unmatched.append({"line": entry["line"], "at": iso(entry["at"]),
                              "cwd": stable_hash(target) if redact else target,
                              "arm": entry["arm"],
                              "why": "this cwd has threads, but none with a session at or "
                                     "before the logged end"})
            continue
        successors = sorted([s for s in best.sessions if s.start and s.start > entry["at"]],
                            key=lambda s: s.start)
        if not successors:
            pending.append({"line": entry["line"], "at": iso(entry["at"]),
                            "thread_id": best.id, "arm": entry["arm"],
                            "cwd": stable_hash(target) if redact else target})
            continue
        rows.append(_handoff_row(entry, best, successors[0], target, redact, "inferred"))
    return {"rows": rows, "pending": pending, "unmatched": unmatched}


def _handoff_row(entry: Dict[str, Any], best: "Thread", successor: "Session",
                 target: str, redact: bool, pairing: str) -> Dict[str, Any]:
    metrics = successor.metrics
    return ({
        "thread_id": best.id,
        # `declared` = the log named this successor; `inferred` = it was
        # found by the thread heuristic.
        "pairing": pairing,
        "predecessor_session": (None if not entry.get("predecessor_session_id") else
                                (stable_hash(entry["predecessor_session_id"]) if redact
                                 else entry["predecessor_session_id"])),
        "cwd": stable_hash(target) if redact else target,
        "logged_tool": entry["tool"],
        "successor_tool": successor.source,
        "arm": entry["arm"],
        "end_at": iso(entry["at"]),
        "successor_session": (stable_hash(successor.session_id) if redact
                              else successor.session_id),
        "successor_start": iso(successor.start),
        "gap_hours": round((successor.start - entry["at"]).total_seconds() / 3600.0, 2),
        "orientation_tokens": metrics.get("orientation_tokens"),
        "orientation_share": metrics.get("orientation_share"),
        "orientation_breadth": metrics.get("orientation_breadth"),
        "total_tokens": metrics.get("total_tokens"),
        # "cost if the tool records one" -- a store with no token counts
        # gets None here, never a zero that would read as "it was free".
        "est_cost_usd": (metrics.get("est_cost_usd")
                           if metrics.get("total_tokens") else None),
        "turns_to_first_mutation": metrics.get("first_mutation_turn"),
        "no_mutation": metrics.get("no_mutation"),
        "handoff_opened_by_turn_2": metrics.get("handoff_opened_by_turn_2"),
        "readme_opened_by_turn_2": metrics.get("readme_opened_by_turn_2"),
        "handoff_file": None if redact else metrics.get("handoff_file"),
        "note_path": None if redact else entry["note_path"],
        "note_minutes": entry["note_minutes"],
        "outcome": metrics.get("outcome"),
    })


# ------------------------------------------------------- paired statistics
def _rank(values: Sequence[float]) -> List[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def wilcoxon_signed_rank(differences: Sequence[float]) -> Dict[str, Any]:
    """Two-sided Wilcoxon signed-rank. EXACT below n=21, else normal approx.

    Zero differences are dropped and n reduced (Wilcoxon's own rule), and the
    number dropped is returned so it is visible rather than absorbed. At the
    n this log will ever reach, the exact null is cheap and the normal
    approximation is not trustworthy, so exact is the default path.
    """
    nonzero = [d for d in differences if d != 0]
    dropped = len(differences) - len(nonzero)
    n = len(nonzero)
    if n < 1:
        return {"n": 0, "zeros_dropped": dropped, "w": None, "p": None,
                "method": "not computed — no non-zero difference"}
    ranks = _rank([abs(d) for d in nonzero])
    w_plus = sum(r for r, d in zip(ranks, nonzero) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, nonzero) if d < 0)
    w = min(w_plus, w_minus)
    if n <= 20:
        # Exact null: enumerate the 2^n sign assignments of the OBSERVED
        # ranks by dynamic programming over their doubled values (mid-ranks
        # from ties are always half-integers, so 2r is always an integer).
        # No sampling, no normal approximation -- at the n this log will
        # ever reach, the approximation is the thing that would be wrong.
        doubled = [int(round(r * 2)) for r in ranks]
        total = sum(doubled)
        counts = [0] * (total + 1)
        counts[0] = 1
        for value in doubled:
            for s in range(total, value - 1, -1):
                counts[s] += counts[s - value]
        cut = int(round(w * 2))
        tail = sum(counts[s] for s in range(0, cut + 1))
        p = min(1.0, 2.0 * tail / float(2 ** n))
        method = "exact (n=%d, all 2^%d sign assignments enumerated by DP)" % (n, n)
    else:
        mean = n * (n + 1) / 4.0
        tie = collections.Counter(ranks)
        correction = sum(t ** 3 - t for t in tie.values())
        var = n * (n + 1) * (2 * n + 1) / 24.0 - correction / 48.0
        if var <= 0:
            return {"n": n, "zeros_dropped": dropped, "w": w, "p": None,
                    "method": "not computed — zero variance"}
        z = (w_plus - mean) / math.sqrt(var)
        p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
        method = "normal approximation with tie correction (n=%d)" % n
    return {"n": n, "zeros_dropped": dropped, "w": w, "w_plus": w_plus,
            "w_minus": w_minus, "p": round(p, 4), "method": method}


def sign_test(differences: Sequence[float]) -> Dict[str, Any]:
    """Two-sided exact binomial sign test, ties dropped. Assumption-free.

    This is the robustness check the read-back leads with, because it
    survives any distribution: with n = 20 usable pairs the note must win 15
    for two-sided p < 0.05; 14/20 gives p = 0.115, which is not enough.
    """
    wins = sum(1 for d in differences if d < 0)      # NOTE below NONE = note wins
    losses = sum(1 for d in differences if d > 0)
    ties = len(differences) - wins - losses
    n = wins + losses
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "ties": ties, "p": None,
                "note": "every pair tied"}
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(0, k + 1))
    p = min(1.0, 2.0 * tail / float(2 ** n))
    return {"n": n, "wins": wins, "losses": losses, "ties": ties, "p": round(p, 4),
            "note": "wins = the NOTE arm's orientation came in lower"}


def wilson(k: int, n: int, z: float = 1.645) -> Tuple[Optional[float], Optional[float]]:
    """Wilson interval; z=1.645 is 90 %, a conventional choice at small n."""
    if n == 0:
        return (None, None)
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((centre - half) / denom, 3), round((centre + half) / denom, 3))


def handoff_report(joined: Dict[str, Any], skipped: Sequence[Dict[str, Any]],
                   entries: Sequence[Dict[str, Any]],
                   minute_rate_usd: float) -> Dict[str, Any]:
    """The paired comparison, blocked on THREAD.

    METHODOLOGICAL CALL, recorded on the output: **a pair is a THREAD that
    contributed both arms**, and a thread's value in an arm is the MEDIAN of
    its successors in that arm. "Successors" and "pairs" are not the same
    number, and blocking on thread identity is what makes the paired test
    correct here. Both counts are printed, and
    the underpowered banner keys on the PAIR count, which is the smaller and
    therefore the honest one.
    """
    rows = joined["rows"]
    by_thread: Dict[str, Dict[str, List[Dict[str, Any]]]] = collections.defaultdict(
        lambda: {"NOTE": [], "NONE": []})
    for row in rows:
        by_thread[row["thread_id"]][row["arm"]].append(row)

    pairs = []
    for thread_id, arms in sorted(by_thread.items()):
        if not (arms["NOTE"] and arms["NONE"]):
            continue
        note_tokens = median_or_none([r["orientation_tokens"] or 0 for r in arms["NOTE"]])
        none_tokens = median_or_none([r["orientation_tokens"] or 0 for r in arms["NONE"]])
        minutes = [r["note_minutes"] for r in arms["NOTE"] if r["note_minutes"] is not None]
        # ---- THE ESTIMAND ------------------------------------------------
        # PRIMARY: TOTAL successor cost, including the cost of producing the
        # note. It used to be `orientation-token difference x pooled dollars
        # per token`, which scores REALLOCATION as saving: a probe that moved
        # 490 tokens from before the boundary to after it, with an identical
        # $1.00 total in both arms, and the old formula reported +$0.49 of
        # "net value". The mechanism measure (pre-boundary tokens) is kept as
        # a SECONDARY line, which is what it is.
        note_total = median_or_none([r["est_cost_usd"] for r in arms["NOTE"]
                                     if r["est_cost_usd"] is not None])
        none_total = median_or_none([r["est_cost_usd"] for r in arms["NONE"]
                                     if r["est_cost_usd"] is not None])
        saved_usd = ((none_total - note_total)
                     if (note_total is not None and none_total is not None) else None)
        saved_tokens = none_tokens - note_tokens
        note_minutes = median_or_none(minutes) if minutes else None
        pairs.append({
            "thread_id": thread_id,
            "cwd": (arms["NOTE"] + arms["NONE"])[0]["cwd"],
            "note_successors": len(arms["NOTE"]),
            "none_successors": len(arms["NONE"]),
            # SECONDARY — the mechanism: did the note move the boundary?
            "note_orientation_tokens": note_tokens,
            "none_orientation_tokens": none_tokens,
            "difference": note_tokens - none_tokens,     # negative = the note won
            "orientation_tokens_saved": saved_tokens,
            # PRIMARY — total successor cost, and the cost of the note itself.
            "note_total_cost_usd": note_total,
            "none_total_cost_usd": none_total,
            "note_minutes": note_minutes,
            "saved_usd": saved_usd,
            "note_cost_usd": (note_minutes * minute_rate_usd / 60.0
                              if note_minutes is not None else None),
            "net_usd": (saved_usd - (note_minutes * minute_rate_usd / 60.0)
                        if saved_usd is not None and note_minutes is not None else None),
            "estimand": ("net = (median NONE total successor cost) - (median NOTE total "
                         "successor cost) - (the cost of writing the note). Moving tokens "
                         "from before the first edit to after it nets $0."),
            "notes_opened": sum(1 for r in arms["NOTE"] if r["handoff_opened_by_turn_2"]),
        })

    differences = [p["difference"] for p in pairs]
    note_rows = [r for r in rows if r["arm"] == "NOTE"]
    opened = sum(1 for r in note_rows if r["handoff_opened_by_turn_2"])
    mediator_lo, mediator_hi = wilson(opened, len(note_rows))
    nets = [p["net_usd"] for p in pairs if p["net_usd"] is not None]
    unknown_minutes = sum(1 for r in note_rows if r["note_minutes"] is None)

    return {
        "minute_rate_usd": minute_rate_usd,
        "rate_note": ("note_minutes converted at $%.2f/hour = $%.4f/minute; change it with "
                      "--minute-rate-usd. Every net-value number below moves with this rate, "
                      "so it is stated here rather than buried in the code."
                      % (minute_rate_usd, minute_rate_usd / 60.0)),
        "pair_definition": ("a pair is a sitting with sessions in both arms; its value in "
                            "an arm is the median of its sessions in that arm (the sitting "
                            "is the block)"),
        "ends_logged": len(entries),
        "lines_skipped": len(skipped),
        "skipped": list(skipped),
        "pending_ends": len(joined["pending"]),
        "unmatched_ends": len(joined["unmatched"]),
        "successor_rows": len(rows),
        "successors_by_arm": dict(collections.Counter(r["arm"] for r in rows)),
        "usable_pairs": len(pairs),
        "min_usable_pairs": MIN_USABLE_PAIRS,
        "stopping_rule": STOPPING_RULE,
        "successors_needed_at_minimum": MIN_SUCCESSORS_FOR_FLOOR,
        "pairing_counts": dict(collections.Counter(r.get("pairing") for r in rows)),
        "underpowered": len(pairs) < MIN_USABLE_PAIRS,
        "pairs": pairs,
        "wilcoxon": wilcoxon_signed_rank(differences) if pairs else None,
        "sign_test": sign_test(differences) if pairs else None,
        "mediator": {
            "note_arm_successors": len(note_rows),
            "opened_by_turn_2": opened,
            "rate": round(opened / len(note_rows), 3) if note_rows else None,
            "wilson90": [mediator_lo, mediator_hi],
            "floor": MEDIATOR_FLOOR,
            "below_floor": bool(note_rows) and (opened / len(note_rows)) < MEDIATOR_FLOOR,
            "branch": ("below %.0f %% the result is about whether notes get found, not "
                       "whether they help: a note nobody reads cannot be the cause of anything"
                       % (100 * MEDIATOR_FLOOR)),
        },
        "net_value": {
            "pairs_with_net": len(nets),
            "pairs_without_minutes": len(pairs) - len(nets),
            "note_successors_with_unknown_minutes": unknown_minutes,
            "median_net_usd": round(median_or_none(nets), 4) if nets else None,
            "positive": sum(1 for v in nets if v > 0),
            "negative": sum(1 for v in nets if v < 0),
            "caveat": ("a blank note_minutes means unknown, never 0; those pairs are left "
                       "out of net value and counted here"),
        },
    }


# ==========================================================================
# RECORDS -- full fidelity by default; --redact is opt-in, for export only.
# ==========================================================================
def scrub(text: Optional[str]) -> Optional[str]:
    """Replace a secret/GUID/URL/email-shaped token with its hash.

    Only used on --redact output: a basename can itself be a GUID, and the
    export must survive the privacy grep.
    """
    if text is None:
        return None
    return SECRETISH_RE.sub(lambda m: "h:" + stable_hash(m.group(0)), str(text))


def session_record(session: Session, redact: bool, brief: bool = False) -> Dict[str, Any]:
    m = session.metrics
    record: Dict[str, Any] = {
        "source": session.source,
        "session_id": session.session_id if not redact else stable_hash(session.session_id),
        "start": iso(session.start),
        "end": iso(session.end),
        "duration_s": round(session.duration_s, 1) if session.duration_s is not None else None,
        "cwd_hash": session.cwd_hash,
        "cwd_basename": scrub(session.cwd_base) if redact else session.cwd_base,
        "models": session.models,
        "turns": m.get("turns"),
        "total_tokens": m.get("total_tokens"),
        "orientation_tokens": m.get("orientation_tokens"),
        "orientation_turns": m.get("orientation_turns"),
        "orientation_share": m.get("orientation_share"),
        "no_mutation": m.get("no_mutation"),
        "handoff_opened_by_turn_2": m.get("handoff_opened_by_turn_2"),
        "readme_opened_by_turn_2": m.get("readme_opened_by_turn_2"),
        "question_first": m.get("question_first"),
        "orientation_breadth": m.get("orientation_breadth"),
        "outcome": m.get("outcome"),
        "strategy": m.get("strategy"),
        "thinking_share": m.get("thinking_share"),
        "thinking_source": m.get("thinking_source"),
        "effort": m.get("effort"),
        "effort_mix": m.get("effort_mix"),
        "est_cost_usd": m.get("est_cost_usd"),
        # DIRECTLY PRICED. Present in the JSON so a downstream reader never
        # has to reconstruct it as cost x share, the arithmetic this replaced.
        "orientation_cost_usd": m.get("orientation_cost_usd"),
        "boundary_turn_tokens": m.get("boundary_turn_tokens"),
        "proxy_cost_usd": m.get("est_cost_usd"),   # deprecated alias
        "price_coverage": m.get("price_coverage"),
    }
    if redact:
        record["cwd"] = None
        record["files_touched"] = [scrub(os.path.basename(str(p)))
                                   for p in session.files_touched][:60]
        record["notes_file"] = scrub(os.path.basename(str(m.get("notes_file")))) if m.get("notes_file") else None
        record["title"] = None
        record["first_prompt"] = None
        record["first_mutation_arg"] = None
        record["path"] = None
    else:
        record["cwd"] = session.cwd
        record["files_touched"] = session.files_touched[:200]
        record["notes_file"] = m.get("notes_file")
        record["title"] = session.title
        record["first_prompt"] = session.first_prompt
        record["first_mutation_arg"] = m.get("first_mutation_arg")
        record["path"] = session.path
    if not brief:
        record["first_mutation_turn"] = m.get("first_mutation_turn")
        record["first_mutation_reason"] = m.get("first_mutation_reason")
        record["tool_histogram"] = m.get("tool_histogram")
        record["edit_calls"] = m.get("edit_calls")
        record["mutating_bash_calls"] = m.get("mutating_bash_calls")
        record["output_tokens"] = m.get("output_tokens")
        record["thinking_tokens"] = m.get("thinking_tokens")
        record["per_model"] = m.get("per_model")
        record["per_model_effort"] = m.get("per_model_effort")
        record["effort_recorded_turns"] = m.get("effort_recorded_turns")
        record["version"] = session.version
        record["duplicate_rows"] = session.duplicate_rows
    return record


# ------------------------------------------------------------------- meter
def meter_report(sessions: Sequence[Session], bucket_hours: float = 5.0) -> Dict[str, Any]:
    """5-hour and weekly buckets side by side, plus every reset reading found.

    A `rate_limit_event`-style payload is stamped at SESSION START, so each
    READING is dated by its session's start time -- not by the file's mtime
    and not by when you ran this. The card says so. Where the CLI writes no
    such record, the fallback is the 429 refusal, which carries a reset clock
    time and nothing else.
    """
    readings = []
    for session in sessions:
        for raw in session.meter_readings:
            row = dict(raw)
            row["dated_by"] = "session-start"
            row["session_start"] = iso(session.start)
            row["at"] = iso(session.start) or row.get("at")
            row["observed_at"] = raw.get("at")
            row["file_mtime"] = iso(session.file_mtime)
            readings.append(row)
    readings.sort(key=lambda r: r.get("at") or "")

    five: "collections.OrderedDict[str, Dict[str, float]]" = collections.OrderedDict()
    weekly: "collections.OrderedDict[str, Dict[str, float]]" = collections.OrderedDict()
    for session in sessions:
        for turn in session.turns:
            if not turn.ts:
                continue
            anchor = turn.ts.replace(minute=0, second=0, microsecond=0,
                                     hour=int(turn.ts.hour // bucket_hours * bucket_hours))
            week = (turn.ts - dt.timedelta(days=turn.ts.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0)
            for table, key in ((five, iso(anchor)), (weekly, iso(week))):
                row = table.setdefault(key, {"tokens": 0.0, "turns": 0.0, "cost": 0.0})
                row["tokens"] += turn.tokens
                row["turns"] += 1
    for table in (five, weekly):
        for key in table:
            table[key]["tokens"] = int(table[key]["tokens"])
            table[key]["turns"] = int(table[key]["turns"])
    # These buckets are CLOCK-ALIGNED subdivisions of a
    # UTC day, so with bucket_hours=5 the last bucket of each day runs 20:00 to
    # 24:00 and is FOUR hours long. They are NOT the provider's rolling usage
    # windows, which start when a session starts. Every bucket now carries its
    # own length and the panel says which scheme this is.
    def _span(key: str) -> float:
        hour = int(str(key)[11:13]) if len(str(key)) >= 13 else 0
        return min(bucket_hours, 24 - hour)
    return {
        "bucket_hours": bucket_hours,
        "bucket_scheme": ("clock-aligned subdivisions of a UTC day, NOT the provider's "
                          "rolling usage windows. With a %g-hour bucket the last bucket of "
                          "each day is %g hours long; each row carries its own `hours`."
                          % (bucket_hours, 24 - int(24 // bucket_hours) * bucket_hours
                             or bucket_hours)),
        "five_hour_buckets": [dict(bucket=k, hours=_span(k), **v) for k, v in sorted(five.items())],
        "weekly_buckets": [dict(week_starting=k, **v) for k, v in sorted(weekly.items())],
        "readings": readings,
        "reading_count": len(readings),
        "dating_note": ("every reading is dated by its SESSION START, because a "
                        "rate_limit_event payload is stamped when the session opens; "
                        "to date one by wall clock use file mtime minus session duration"),
        "source_note": ("no rate_limit_event record type was found in this history; "
                        "readings below come from HTTP-429 refusal messages"
                        if readings and all(r.get("kind") == "http-429" for r in readings)
                        else "mixed or native rate_limit_event records"),
    }


# -------------------------------------------------------------- aggregates
def aggregate(sessions: Sequence[Session], threads: Sequence[Thread]) -> Dict[str, Any]:
    with_boundary = [s for s in sessions if not s.metrics.get("no_mutation")]
    no_mutation = [s for s in sessions if s.metrics.get("no_mutation")]
    shares = [s.metrics["orientation_share"] for s in with_boundary
              if s.metrics.get("orientation_share") is not None]
    notes_yes = [s.metrics["orientation_share"] for s in with_boundary
                 if s.metrics.get("handoff_opened_by_turn_2") and s.metrics.get("orientation_share") is not None]
    notes_no = [s.metrics["orientation_share"] for s in with_boundary
                if not s.metrics.get("handoff_opened_by_turn_2") and s.metrics.get("orientation_share") is not None]
    by_strategy: Dict[str, List[float]] = collections.defaultdict(list)
    for session in sessions:
        cost = session.metrics.get("est_cost_usd")
        if cost is not None:
            by_strategy[session.metrics.get("strategy", "?")].append(cost)
    by_model_thinking: Dict[str, List[float]] = collections.defaultdict(list)
    for session in sessions:
        for model, row in (session.metrics.get("per_model") or {}).items():
            if model in SYNTHETIC_MODELS or str(model).strip().lower() in SYNTHETIC_MODELS:
                continue
            if row.get("thinking_share") is not None:
                by_model_thinking[model].append(row["thinking_share"])
    by_effort_thinking: Dict[str, List[float]] = collections.defaultdict(list)
    by_effort_cost: Dict[str, List[float]] = collections.defaultdict(list)
    effort_turns: "collections.Counter[str]" = collections.Counter()
    for session in sessions:
        for key, row in (session.metrics.get("per_model_effort") or {}).items():
            if str(key.rpartition("|")[0]).strip().lower() in SYNTHETIC_MODELS:
                continue
            if row.get("thinking_share") is not None:
                by_effort_thinking[key].append(row["thinking_share"])
        for level, count in (session.metrics.get("effort_mix") or {}).items():
            effort_turns[level] += count
        if session.metrics.get("effort") and session.metrics.get("est_cost_usd") is not None:
            by_effort_cost[session.metrics["effort"]].append(session.metrics["est_cost_usd"])
    multi = [t for t in threads if len(t.sessions) > 1]
    cross = [t for t in threads if len({s.source for s in t.sessions}) > 1]
    # Orientation DOLLARS, not just tokens: on API billing the decision is a
    # budget one, and the token figure has to be converted before anyone can
    # act on it.
    # Each pre-boundary response priced from its own buckets at its own
    # model's rates. A `session cost x token share` shortcut mis-ranks
    # individual sessions in both directions.
    orientation_cost = sum(s.metrics.get("orientation_cost_usd") or 0.0
                           for s in with_boundary)
    orientation_cost_allocated = sum((s.metrics.get("est_cost_usd") or 0.0)
                                     * (s.metrics.get("orientation_share") or 0.0)
                                     for s in with_boundary)
    return {
        "sessions": len(sessions),
        "sessions_by_store": dict(collections.Counter(s.source for s in sessions)),
        "sessions_with_boundary": len(with_boundary),
        "sessions_no_mutation": len(no_mutation),
        "no_mutation_share": round(len(no_mutation) / len(sessions), 3) if sessions else None,
        "median_orientation_share": round(median_or_none(shares), 4) if shares else None,
        "median_orientation_share_notes_opened": round(median_or_none(notes_yes), 4) if notes_yes else None,
        "median_orientation_share_no_notes": round(median_or_none(notes_no), 4) if notes_no else None,
        "handoff_opened_sessions": len(notes_yes),
        # BOTH are reported, because the pooled version is what pointed the
        # wrong way and a reader is entitled to see the difference that made.
        "readme_opened_sessions": sum(
            1 for s in with_boundary if s.metrics.get("readme_opened_by_turn_2")),
        "median_orientation_share_readme_opened": (
            round(median_or_none([s.metrics["orientation_share"] for s in with_boundary
                                  if s.metrics.get("readme_opened_by_turn_2")
                                  and s.metrics.get("orientation_share") is not None]), 4)
            if any(s.metrics.get("readme_opened_by_turn_2") for s in with_boundary) else None),
        "notes_opened_sessions_pooled": sum(
            1 for s in with_boundary if s.metrics.get("notes_opened_by_turn_2")),
        "median_orientation_share_pooled_notes": (
            round(median_or_none([s.metrics["orientation_share"] for s in with_boundary
                                  if s.metrics.get("notes_opened_by_turn_2")
                                  and s.metrics.get("orientation_share") is not None]), 4)
            if any(s.metrics.get("notes_opened_by_turn_2") for s in with_boundary) else None),
        "mediator_note": ("the mediator is handoff_opened_by_turn_2. README is EXCLUDED "
                          "from it and reported separately: reading a README is "
                          "orientation work, not a substitute for it, and pooling the "
                          "two made the lever look like the cost"),
        "orientation_tokens_total": sum(s.metrics.get("orientation_tokens") or 0 for s in with_boundary),
        # The category the tax figures are silent about.
        "sessions_without_boundary": unmeasured_boundary(sessions),
        # Published beside the direct figure so the change is auditable.
        "orientation_cost_usd_total_allocated_old": round(orientation_cost_allocated, 2),
        "tokens_total": sum(s.metrics.get("total_tokens", 0) for s in sessions),
        "est_cost_usd_total": round(sum(s.metrics.get("est_cost_usd") or 0 for s in sessions), 2),
        "strategy_counts": dict(collections.Counter(s.metrics.get("strategy") for s in sessions)),
        "median_cost_by_strategy": {k: round(statistics.median(v), 4) for k, v in sorted(by_strategy.items())},
        "median_thinking_share_by_model": {k: round(statistics.median(v), 4)
                                           for k, v in sorted(by_model_thinking.items())},
        "effort_turns": dict(effort_turns.most_common()),
        "effort_recorded_share": (round(sum(effort_turns.values())
                                        / max(1, sum(s.metrics.get("turns", 0) for s in sessions)), 3)),
        "sessions_by_effort": dict(collections.Counter(
            s.metrics.get("effort") or "unrecorded" for s in sessions).most_common()),
        "median_cost_by_effort": {k: round(statistics.median(v), 4)
                                  for k, v in sorted(by_effort_cost.items())},
        "median_thinking_share_by_model_effort": {k: round(statistics.median(v), 4)
                                                  for k, v in sorted(by_effort_thinking.items())},
        "threads": len(threads),
        "multi_session_threads": len(multi),
        "cross_tool_threads": len(cross),
        "largest_thread": max((len(t.sessions) for t in threads), default=0),
        "reorientation_tax_tokens": sum(
            sum(m.metrics.get("orientation_tokens") or 0 for m in t.sessions[1:]) for t in multi),
        "reorientation_tax_usd": round(sum(
            sum(m.metrics.get("orientation_cost_usd") or 0.0 for m in t.sessions[1:])
            for t in multi), 2),
        "reorientation_tax_without_boundary": unmeasured_boundary(
            [m for t in multi for m in t.sessions[1:]]),
        # THE CONFOUND, MEASURED. The same tax with every
        # session that opened with a question dropped: a session that was
        # ASKED a question and only later asked for a change was not
        # re-learning, so counting its pre-change tokens as tax overstates it.
        # The gap between the two numbers is the size of the confound.
        "reorientation_tax_tokens_excl_question_first": sum(
            sum(m.metrics.get("orientation_tokens") or 0 for m in t.sessions[1:]
                if not m.metrics.get("question_first")) for t in multi),
        "question_first_sessions": sum(1 for s in sessions
                                       if s.metrics.get("question_first")),
        "question_first_observable": sum(1 for s in sessions
                                         if s.metrics.get("question_first") is not None),
        "orientation_cost_usd_total": round(orientation_cost, 2),
        "orientation_cost_share": (round(orientation_cost
                                         / sum(s.metrics.get("est_cost_usd") or 0.0
                                               for s in sessions), 4)
                                   if sum(s.metrics.get("est_cost_usd") or 0.0
                                          for s in sessions) else None),
    }


# ------------------------------------------------------------- probe a store
def probe_store(path: Path, limit: int = 40) -> Dict[str, Any]:
    """Say what we recognise, what we cannot parse, and which KEYS exist.

    Prints KEY NAMES and counts only, never values -- so the output is safe to
    attach to an issue, and an adapter can be finished from it.
    """
    path = Path(path).expanduser()
    report: Dict[str, Any] = {
        "path": str(path), "exists": path.exists(),
        "files_seen": 0, "files_parsed": 0, "files_unparsed": 0,
        "shapes": collections.Counter(), "top_level_keys": collections.Counter(),
        "nested_keys": collections.Counter(), "request_keys": collections.Counter(),
        "response_part_kinds": collections.Counter(),
        "timestamp_like_keys": collections.Counter(),
        "token_like_keys": collections.Counter(),
        "unparsed_examples": [],
    }
    report["sqlite"] = {}
    if not path.exists():
        return _finish_probe(report)
    sqlite_paths = ([path] if path.is_file() and path.suffix.lower() in
                    (".db", ".sqlite", ".sqlite3", ".vscdb")
                    else [p for pattern in COPILOT_SQLITE_NAMES
                          for p in (sorted(path.rglob(pattern)) if path.is_dir() else [])])
    for store in sqlite_paths[:12]:
        report["sqlite"][str(store)] = sqlite_schema(store)
    candidates = ([path] if path.is_file() else
                  sorted(p for p in path.rglob("*") if p.is_file()
                         and p.suffix.lower() in (".json", ".jsonl", ".ndjson"))[:4000])
    for candidate in candidates[:limit * 25]:
        report["files_seen"] += 1
        parsed_any = False
        for doc, error in _vscode_session_docs(candidate):
            if error:
                report["unparsed_examples"].append(candidate.name + ": " + error)
                continue
            if not isinstance(doc, dict) or not doc:
                continue
            parsed_any = True
            report["shapes"][_shape_name(doc)] += 1
            for key, value in doc.items():
                report["top_level_keys"][key] += 1
                if re.search(r"(date|time|stamp|created|updated)", key, re.I):
                    report["timestamp_like_keys"][key] += 1
                if re.search(r"(token|usage|cost|credit)", key, re.I):
                    report["token_like_keys"][key] += 1
                if isinstance(value, dict):
                    for sub in list(value)[:40]:
                        report["nested_keys"]["%s.%s" % (key, sub)] += 1
                elif isinstance(value, list) and value and isinstance(value[0], dict):
                    for sub in list(value[0])[:40]:
                        report["nested_keys"]["%s[].%s" % (key, sub)] += 1
                    if key in VSCODE_REQUEST_KEYS:
                        for item in value[:20]:
                            if not isinstance(item, dict):
                                continue
                            for sub in item:
                                report["request_keys"][sub] += 1
                            for part_key in ("response", "result", "parts", "steps"):
                                for part in (item.get(part_key) or []):
                                    if isinstance(part, dict):
                                        report["response_part_kinds"][
                                            str(part.get("kind") or part.get("type") or "?")] += 1
        report["files_parsed" if parsed_any else "files_unparsed"] += 1
    return _finish_probe(report)


def _shape_name(doc: Dict[str, Any]) -> str:
    keys = set(doc)
    if {"requests", "sessionId"} <= keys:
        return "vscode-chat-session"
    if {"messages"} & keys:
        return "message-list"
    if {"type", "message", "sessionId"} <= keys:
        return "claude-code-row"
    if {"turns", "events", "history"} & keys:
        return "turn-list"
    return "unknown"


def _finish_probe(report: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("shapes", "top_level_keys", "nested_keys", "request_keys",
                "response_part_kinds", "timestamp_like_keys", "token_like_keys"):
        report[key] = dict(report[key].most_common(60))
    report["unparsed_examples"] = report["unparsed_examples"][:10]
    report["privacy"] = "key names and counts only; no values were read out of this store"
    return report


# ==========================================================================
# HTML REPORT -- self-contained: inline SVG + vanilla JS, no CDN, no external
# fonts; the page loads nothing from the network. Light/dark aware.
# To add a panel: write a function returning SVG markup, then call panel().
# See references/visual-report-spec.md, "How to build or extend the graphical
# support".
# ==========================================================================
# ARM COLOURS. One CATEGORICAL pair from a colour-blind-safe palette --
# slot 1 blue and slot 2 orange,
# which are also this report's existing --accent / --accent2, so the
# Experiment panel reads as part of the same document. Explicitly NOT a
# red/green pair: red/green is the status palette, it reads as pass/fail, and
# NOTE vs NONE is an identity distinction, not an outcome. The net-value bar
# below zero is a different job (polarity), so it uses the palette's DIVERGING
# pair -- blue to red across a neutral grey midpoint -- and that is the only
# place red appears.
CSS = """
:root{--ink:#172033;--muted:#5b6779;--paper:#f4f7fb;--card:#fff;--line:#dce3ee;
 --accent:#2a78d6;--accent2:#eb6834;--good:#147d64;--bad:#b24835;--grid:#e6ecf5;
 --chip:#eaf0ff;--arm-note:#2a78d6;--arm-none:#eb6834;--div-pos:#2a78d6;--c3:#1baf7a;
 --div-neg:#e34948;--div-mid:#f0efec;--stage1:#cde2fb;--stage2:#86b6ef;
 --stage3:#2a78d6;--stage4:#184f95;--rest:#dde2ea;--rest-edge:#8a94a6}
html[data-theme="dark"]{--ink:#e7edf7;--muted:#9aa7bb;--paper:#0e1420;--card:#161d2b;
 --line:#263042;--accent:#3987e5;--accent2:#d95926;--good:#4ade80;
 --bad:#f98a76;--grid:#222c3d;--chip:#1d2740;--arm-note:#3987e5;--arm-none:#d95926;--c3:#199e70;
 --div-pos:#3987e5;--div-neg:#e66767;--div-mid:#383835;--stage1:#184f95;
 --stage2:#256abf;--stage3:#3987e5;--stage4:#86b6ef;--rest:#3a4256;--rest-edge:#6f7a8e}
@media (prefers-color-scheme:dark){html:not([data-theme="light"]){--ink:#e7edf7;
 --muted:#9aa7bb;--paper:#0e1420;--card:#161d2b;--line:#263042;--accent:#3987e5;
 --accent2:#d95926;--good:#4ade80;--bad:#f98a76;--grid:#222c3d;--chip:#1d2740;
 --arm-note:#3987e5;--arm-none:#d95926;--div-pos:#3987e5;--div-neg:#e66767;--c3:#199e70;
 --div-mid:#383835;--stage1:#184f95;--stage2:#256abf;--stage3:#3987e5;--stage4:#86b6ef;
 --rest:#3a4256;--rest-edge:#6f7a8e}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
 font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1120px;margin:auto;padding:32px 22px 90px}
h1{font-size:31px;margin:0 0 6px} h2{font-size:20px;margin:34px 0 10px}
h3{font-size:16px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 20px}
.pointer{font-size:17px;margin:4px 0 8px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:12px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.metric{font-size:25px;font-weight:750;line-height:1.15}
.label{color:var(--muted);font-size:12.5px;margin-top:3px}
.idx{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.idx .card{display:flex;flex-direction:column;gap:6px}
.q{color:var(--muted);font-size:13px;flex:1}
.tag{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.03em;
 padding:2px 8px;border-radius:99px;background:var(--chip);color:var(--accent);width:fit-content}
.tag.no{background:transparent;border:1px dashed var(--line);color:var(--muted)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;
 padding:16px 18px;margin:14px 0}
.decision{color:var(--muted);font-size:13.5px;margin:2px 0 10px}
.chartbox{overflow-x:auto} svg{min-width:640px;width:100%;height:auto;display:block}
.g-title{font-size:15px;font-weight:700;fill:var(--ink)}
.axis{font-size:11px;fill:var(--muted)} .axis-strong{font-size:11.5px;fill:var(--ink);font-weight:600}
.val{font-size:10.5px;font-weight:700;fill:var(--ink)}
.grid{stroke:var(--grid);stroke-width:1} .baseline{stroke:var(--muted);stroke-width:1}
.s1{fill:var(--accent)} .s2{fill:var(--accent2)}
.s1s{stroke:var(--accent);fill:none} .s2s{stroke:var(--accent2);fill:none}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
 font-size:13.5px;margin-top:10px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child,.l{text-align:left;white-space:normal}
th.r,td.r{text-align:right;white-space:nowrap}
th{background:var(--chip);position:sticky;top:0;cursor:pointer;user-select:none}
tbody tr:hover{background:var(--chip)}
details{margin-top:10px} summary{cursor:pointer;color:var(--accent);font-size:13.5px}
.scroll{max-height:420px;overflow:auto;border-radius:10px}
.scroll.tall{max-height:78vh}
/* The ledger page carries a ten-column table and nothing else, so it gets the
   window rather than the reading measure the prose pages use. */
main.wide{max-width:1640px}
td.nw,th.nw{white-space:nowrap}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin:6px 0 2px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:11px;height:11px;border-radius:3px;display:inline-block}
.note{border-left:4px solid var(--accent);background:var(--chip);padding:9px 13px;
 border-radius:0 8px 8px 0;font-size:13.5px;margin:10px 0}
.tt{position:fixed;pointer-events:none;background:var(--card);color:var(--ink);
 border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:12.5px;
 box-shadow:0 6px 18px #0003;opacity:0;transition:opacity .08s;max-width:420px;z-index:9}
#themebtn{position:fixed;top:14px;right:16px;z-index:10;background:var(--card);
 color:var(--ink);border:1px solid var(--line);border-radius:99px;padding:6px 13px;
 font:inherit;font-size:13px;cursor:pointer}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12.5px}
a{color:var(--accent)}
/* The page-set header: the same strip on every page, so "where am I, how do
   I get home, where are the definitions" never needs scrolling. */
.nav{position:sticky;top:0;z-index:8;background:var(--paper);border-bottom:1px solid var(--line);
 margin:-32px -22px 18px;padding:10px 100px 10px 22px;display:flex;gap:14px;flex-wrap:wrap;
 align-items:baseline;font-size:13px}
.nav a{text-decoration:none;padding:3px 9px;border-radius:99px;border:1px solid var(--line)}
.nav a.here{background:var(--chip);font-weight:700;border-color:var(--accent)}
.nav .jump{margin-left:auto}
/* Numbers read as numbers: right-aligned, one glyph width per digit. */
td,th{font-variant-numeric:tabular-nums}
/* Hidden-by-default ledger columns. */
table.cols:not(.allcols) .x{display:none}
.colbtn{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:99px;
 padding:4px 11px;font:inherit;font-size:12.5px;cursor:pointer;margin:8px 0}
.idxrow{display:grid;grid-template-columns:minmax(190px,1fr) 2fr minmax(230px,1.2fr);gap:10px;
 padding:11px 14px;border-bottom:1px solid var(--line);align-items:baseline}
.idxrow:last-child{border-bottom:0}
.idxrow .t{font-weight:700}
.idxrow .st{font-size:12px;color:var(--muted)}
.idxwrap{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:12px 0 4px}
/* The home page's analysis checklist, and the one "what this shows / what it
   doesn't" box a panel may carry. */
table.check td{white-space:normal;vertical-align:top}
table.check th{cursor:default}
.ok{color:var(--good);font-weight:700}
.shows{display:grid;grid-template-columns:1fr 1fr;gap:6px 22px;background:var(--chip);
 border-radius:10px;padding:10px 16px;margin:12px 0;font-size:13.5px}
.shows h4{margin:2px 0 2px;font-size:13px}
.shows ul{margin:0 0 4px;padding-left:18px}
@media (max-width:760px){.shows{grid-template-columns:1fr}}
details.method{margin:8px 0;font-size:13.5px}
details.method>div{padding:2px 4px}
"""

JS = """
(function(){
 var b=document.getElementById('themebtn');
 function cur(){var t=document.documentElement.getAttribute('data-theme');
  if(t)return t;return window.matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light';}
 function set(t){document.documentElement.setAttribute('data-theme',t);
  b.textContent=(t==='dark'?'\\u2600 light':'\\u263e dark');}
 set(cur()); b.addEventListener('click',function(){set(cur()==='dark'?'light':'dark');});
 var tip=document.createElement('div'); tip.className='tt'; document.body.appendChild(tip);
 document.addEventListener('mouseover',function(e){
  var t=e.target.closest('[data-tip]'); if(!t){tip.style.opacity=0;return;}
  tip.textContent=t.getAttribute('data-tip'); tip.style.opacity=1;});
 document.addEventListener('mousemove',function(e){
  tip.style.left=Math.min(e.clientX+14,window.innerWidth-tip.offsetWidth-10)+'px';
  tip.style.top=(e.clientY+16)+'px';});
 // The column index MUST be read inside the header's own row. The first
 // version took `i` from a document-wide querySelectorAll over every
 // sortable th, so on the second table onwards the index was offset by the
 // width of every table before it and clicking a header sorted by some other
 // table's column -- which is why the session ledger looked unsortable,
 // and sorting is the main way that table gets used.
 document.querySelectorAll('table.sortable th').forEach(function(th){
  th.addEventListener('click',function(){
   var i=[].indexOf.call(th.parentNode.children,th);
   var tb=th.closest('table').tBodies[0], rows=[].slice.call(tb.rows);
   var dir=th.dataset.dir==='asc'?-1:1; th.dataset.dir=dir===1?'asc':'desc';
   [].forEach.call(th.parentNode.children,function(o){if(o!==th)o.removeAttribute('data-dir');});
   rows.sort(function(a,b){
    if(!a.cells[i]||!b.cells[i])return 0;
    var x=a.cells[i].dataset.v||a.cells[i].textContent,
        y=b.cells[i].dataset.v||b.cells[i].textContent;
    var nx=parseFloat(x),ny=parseFloat(y);
    if(!isNaN(nx)&&!isNaN(ny))return (nx-ny)*dir;
    return String(x).localeCompare(String(y))*dir;});
   rows.forEach(function(r){tb.appendChild(r);});});});
 // "show all columns" on the session ledger: the default view is <= 10
 // columns so the table is readable; the rest are present in the DOM and
 // hidden, so the toggle costs no reload and the page stays one file.
 document.querySelectorAll('[data-colstoggle]').forEach(function(btn){
  var t=document.getElementById(btn.getAttribute('data-colstoggle'));
  if(!t)return;
  btn.addEventListener('click',function(){
   var on=t.classList.toggle('allcols');
   btn.textContent=on?'show fewer columns':'show all columns';});});
})();
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def panel(title: str, decision: str, body: str, note: str = "", anchor: str = "") -> str:
    return ('<section class="panel"%s><h3>%s</h3><p class="decision"><strong>Decides:</strong> '
            '%s</p>%s%s</section>' % ((' id="%s"' % anchor) if anchor else "", esc(title),
                                      esc(decision), body,
                                      ('<p class="note">%s</p>' % note) if note else ""))


def legend(items: Sequence[Tuple[str, str]]) -> str:
    return '<div class="legend">' + "".join(
        '<span><i class="sw" style="background:%s"></i>%s</span>' % (colour, esc(label))
        for label, colour in items) + '</div>'


def swatch_solid(colour: str) -> str:
    return '<i class="sw" style="background:%s"></i>' % colour


def swatch_edged(fill: str, edge: str) -> str:
    """A pale fill with a visible edge, so it does not vanish on the page colour."""
    return '<i class="sw" style="background:%s;box-shadow:inset 0 0 0 1px %s"></i>' % (fill, edge)


def swatch_band(colour: str, opacity: float = 0.16) -> str:
    """A shaded region, drawn as the region — not described in words."""
    return ('<i class="sw" style="background:%s;opacity:%.2f;border:1px solid %s;'
            'width:26px;height:9px;border-radius:5px"></i>' % (colour, opacity, colour))


def swatch_dotted(colour: str) -> str:
    return ('<i class="sw" style="width:26px;height:0;border-radius:0;'
            'border-top:2px dotted %s"></i>' % colour)


def legend_rich(items: Sequence[Tuple[str, str]]) -> str:
    """A legend whose swatches SHOW the encoding, rather than naming it.

    The old legend could only draw a filled square, so a shaded region and a
    dotted rule were described in prose ("orange = ...") instead of shown.
    Each item here carries its own swatch markup, so the same component draws
    a fill, a translucent band and a dotted rule.
    """
    return '<div class="legend">' + "".join(
        '<span>%s%s</span>' % (mark, esc(label)) for label, mark in items) + '</div>'


# --------------------------------------------------------------- text wrapping
# Every clipped line a reader reported ("opened a handoff note first — measus")
# was an SVG <text> whose single long line ran past the viewBox: SVG does not
# wrap. So no long label is emitted as one <text> any more — it goes through
# wrap_lines()/text_block(), which breaks it into <tspan> lines that fit a
# width the caller states.
#
# The report CSS and the screen-pack CSS give the same class DIFFERENT sizes
# (.axis is 11 px in the browser, 12 px on a screen-pack page), and the same markup
# is used in both. Wrapping is therefore computed at the LARGER size, so one
# wrap point is safe in both documents.
SVG_FONT = {"axis": 12.0, "axis-strong": 12.5, "g-title": 15.0, "val": 12.0}
# Average advance width per character as a fraction of the font size, for the
# report's system sans at mixed-case English. Chrome renders ~0.50 em; 0.55 is
# used so a wrapped line lands inside its box rather than one glyph past it.
CHAR_EM = 0.55


def wrap_lines(text: str, max_width: float, font_size: float) -> List[str]:
    """Greedy word wrap to a pixel width, in the SVG's own user units."""
    limit = max(6, int(max_width / (font_size * CHAR_EM)))
    lines: List[str] = []
    current = ""
    for word in str(text).split():
        trial = (current + " " + word).strip()
        if len(trial) <= limit or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def text_lines(text: str, max_width: float, cls: str = "axis") -> int:
    return len(wrap_lines(text, max_width, SVG_FONT.get(cls, 12.0)))


def text_height(text: str, max_width: float, cls: str = "axis") -> float:
    size = SVG_FONT.get(cls, 12.0)
    return len(wrap_lines(text, max_width, size)) * size * 1.28


def text_block(x: float, y: float, max_width: float, text: str, cls: str = "axis",
               anchor: str = "start", tip: str = "") -> str:
    """A wrapped <text>: one <tspan> per line, all anchored at the same x."""
    size = SVG_FONT.get(cls, 12.0)
    lines = wrap_lines(text, max_width, size)
    spans = "".join('<tspan x="%.1f" dy="%.2f">%s</tspan>'
                    % (x, 0.0 if index == 0 else size * 1.28, esc(line))
                    for index, line in enumerate(lines))
    return ('<text x="%.1f" y="%.1f" class="%s"%s%s>%s</text>'
            % (x, y, cls,
               (' text-anchor="%s"' % anchor) if anchor != "start" else "",
               (' data-tip="%s"' % esc(tip)) if tip else "", spans))


def svg(width: int, height: int, title: str, body: str) -> str:
    return ('<div class="chartbox"><svg viewBox="0 0 %d %d" role="img" aria-label="%s">'
            '<title>%s</title>%s</svg></div>' % (width, height, esc(title), esc(title), body))


def linscale(d0: float, d1: float, r0: float, r1: float):
    span = (d1 - d0) or 1.0
    return lambda v: r0 + (float(v) - d0) * (r1 - r0) / span


def logscale(d0: float, d1: float, r0: float, r1: float):
    lo = math.log10(max(d0, 1e-6))
    hi = math.log10(max(d1, lo * 1.0001 + 1e-6))
    span = (hi - lo) or 1.0
    return lambda v: r0 + (math.log10(max(float(v), 1e-6)) - lo) * (r1 - r0) / span


# ------------------------------------------------------------- the 7 panels
GLYPH = {"claude-code": "circle", "vscode-copilot": "square", "copilot-cli": "triangle"}


def _mark(kind: str, x: float, y: float, size: float, cls: str, tip: str) -> str:
    if kind == "square":
        return ('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" class="%s" '
                'data-tip="%s"/>' % (x - size, y - size, size * 2, size * 2, cls, esc(tip)))
    if kind == "triangle":
        return ('<polygon points="%.1f,%.1f %.1f,%.1f %.1f,%.1f" class="%s" data-tip="%s"/>'
                % (x, y - size, x + size, y + size, x - size, y + size, cls, esc(tip)))
    return '<circle cx="%.1f" cy="%.1f" r="%.1f" class="%s" data-tip="%s"/>' % (
        x, y, size, cls, esc(tip))


def panel_a_histogram(sessions: Sequence[Session], title: Optional[str] = None,
                      width: int = 880, height: int = 330,
                      bands_note: bool = True) -> str:
    bins = [0.0] * 10
    bins_notes = [0.0] * 10
    for session in sessions:
        share = session.metrics.get("orientation_share")
        if share is None:
            continue
        index = min(9, int(share * 10))
        if session.metrics.get("handoff_opened_by_turn_2"):
            bins_notes[index] += 1
        else:
            bins[index] += 1
    left, right, top, bottom = 62, 20, 34, 60
    pw, ph = width - left - right, height - top - bottom
    # Clean integer ticks: the axis counts sessions, so 0 / 2 / 4 / 6 / 8, never 1.8.
    tick = max(1, int(math.ceil(max([a + b for a, b in zip(bins, bins_notes)] + [1]) / 4.0)))
    peak = tick * 4
    slot = pw / 10.0
    parts = ['<text x="%d" y="20" class="g-title">%s</text>'
             % (left, esc(title or "Most sessions spend a share of their tokens before the "
                                   "first edit; here is how much"))]
    # NO NORMATIVE BANDS. No outcome- or cost-validated threshold exists, so
    # painting green/amber/red under the bars would tell a reader a share is
    # "healthy" or "expensive" when nothing here establishes either.
    for step in range(5):
        y = top + ph - ph * step / 4.0
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>' % (left, y, left + pw, y))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis">%d</text>'
                     % (left - 6, y + 4, tick * step))
    for index in range(10):
        x = left + index * slot + slot * 0.16
        bar_w = slot * 0.68
        total = bins[index] + bins_notes[index]
        h_no = ph * bins[index] / peak
        h_yes = ph * bins_notes[index] / peak
        y_no = top + ph - h_no
        y_yes = y_no - h_yes
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" class="s1" '
                     'data-tip="share %.1f-%.1f · no handoff note read · %d sessions"/>'
                     % (x, y_no, bar_w, h_no, index / 10.0, (index + 1) / 10.0, bins[index]))
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" class="s2" '
                     'data-tip="share %.1f-%.1f · read a handoff note · %d sessions"/>'
                     % (x, y_yes, bar_w, h_yes, index / 10.0, (index + 1) / 10.0, bins_notes[index]))
        if total:
            parts.append('<text x="%.1f" y="%.1f" text-anchor="middle" class="val">%d</text>'
                         % (x + bar_w / 2, max(top + 10, y_yes - 5), int(total)))
        parts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis">%.1f</text>'
                     % (x + bar_w / 2, height - 34, index / 10.0))
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="baseline"/>'
                 % (left, top + ph, left + pw, top + ph))
    parts.append('<text x="%d" y="%d" class="axis">startup share (tokens before the first '
                 'edit ÷ all of the session\'s tokens)</text>' % (left, height - 12))
    parts.append('<text x="14" y="%d" transform="rotate(-90 14 %d)" class="axis">sessions</text>'
                 % (top + ph / 2, top + ph / 2))
    out = (legend([("no handoff note read", "var(--accent)"),
                   ("read a handoff note (README excluded)", "var(--accent2)")])
           + svg(width, height, "Startup share histogram", "".join(parts)))
    if bands_note:
        counted = sum(bins) + sum(bins_notes)
        out += ('<p class="axis">%d of %d sessions are plotted: the ones that made an edit. '
                'The other %d are no-edit sessions, not drawn as zero.</p>'
                % (int(counted), len(sessions), len(sessions) - int(counted))
                + panel_a_normalised(bins, bins_notes)
                + '<p class="note">%s</p>' % esc(NO_THRESHOLD_NOTE))
    return out


def panel_a_normalised(bins: Sequence[float], bins_notes: Sequence[float]) -> str:
    """The handoff/no-handoff split as TWO NORMALISED distributions.

    The groups are usually very unequal -- few sessions open a handoff note --
    so a stacked count chart shows one distribution and a sliver, and invites
    the eye to read a treatment effect off group sizes. Each group is now drawn as a share of
    ITSELF, with its n printed beside it, and the caption says plainly that
    this is an association between two heuristic labels.
    """
    n_no, n_yes = sum(bins), sum(bins_notes)
    if not (n_no or n_yes):
        return ""
    width, height = 880, 210
    left, right, top, bottom = 62, 190, 30, 44
    pw, ph = width - left - right, height - top - bottom
    slot = pw / 10.0
    peak = max([b / n_no for b in bins] if n_no else [0]
               + ([b / n_yes for b in bins_notes] if n_yes else [0]) + [0.01])
    parts = ['<text x="%d" y="18" class="g-title">The same split, each group as a share of '
             'itself</text>' % left]
    for step in range(3):
        gy = top + ph - ph * step / 2.0
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>'
                     % (left, gy, left + pw, gy))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis">%.0f %%</text>'
                     % (left - 6, gy + 4, 100 * peak * step / 2.0))
    for index in range(10):
        x0 = left + index * slot
        for series, total, colour, offset in ((bins, n_no, "var(--accent)", 0.14),
                                              (bins_notes, n_yes, "var(--accent2)", 0.52)):
            if not total:
                continue
            share = series[index] / total
            bar_w = slot * 0.34
            h = ph * (share / peak if peak else 0)
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" fill="%s" '
                         'data-tip="%s"/>'
                         % (x0 + slot * offset, top + ph - h, bar_w, h, colour,
                            esc("share %.1f–%.1f · %d of %d session(s) in this group (%.0f %%)"
                                % (index / 10.0, (index + 1) / 10.0, int(series[index]),
                                   int(total), 100 * share))))
        parts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis">%.1f</text>'
                     % (x0 + slot / 2, height - 24, index / 10.0))
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="baseline"/>'
                 % (left, top + ph, left + pw, top + ph))
    parts.append(text_block(left + pw + 8, top + 12, right - 14,
                            "n = %d, no handoff" % int(n_no), cls="axis-strong"))
    parts.append(text_block(left + pw + 8, top + 44, right - 14,
                            "n = %d, note read" % int(n_yes), cls="axis-strong"))
    parts.append('<text x="%d" y="%d" class="axis">startup share</text>'
                 % (left, height - 8))
    return (svg(width, height, "Startup share, normalised by group", "".join(parts))
            + '<p class="axis">%s</p>' % esc(
                "Each group sums to 100 %% of itself, so the shapes compare. It is an "
                "association on sessions nobody assigned, not an effect of the note."))


def panel_a_by_outcome(sessions: Sequence[Session]) -> str:
    """Panel (a), faceted by outcome. The pooled histogram is drawn too, above.

    Majgaonkar et al. (arXiv 2511.00197) found failure trajectories run 12-82 %
    longer, so a distribution that is bimodal by outcome is itself the finding
    and the pooled chart hides it. Facets rather than a four-way stack: past
    three categories on one mark, facet.

    Local history has no pass/fail oracle, so `outcome` is INFERRED (commit or a
    clean final verification = success; a failed tool result = failure) and
    `unknown` is drawn as its own facet, never folded into either.
    """
    buckets: Dict[str, List[Session]] = collections.defaultdict(list)
    for session in sessions:
        if session.metrics.get("orientation_share") is None:
            continue
        buckets[session.metrics.get("outcome") or "unknown"].append(session)
    present = [k for k in ("success", "failure", "unknown") if buckets.get(k)]
    if len(present) < 2:
        return ('<p class="axis">Only one outcome is present here, so there is nothing '
                'to split.</p>')
    out = ['<p class="axis">Outcome is inferred: success = ended on a commit or a clean '
           'check; failure = a failed tool result; unknown stays its own group.</p>']
    for name in present:
        out.append(panel_a_histogram(
            buckets[name],
            title="%s — %d session%s" % (name, len(buckets[name]),
                                         "" if len(buckets[name]) == 1 else "s"),
            width=880, height=210, bands_note=False))
    return "".join(out)


def panel_i_breadth(sessions: Sequence[Session]) -> str:
    """Orientation tokens against orientation BREADTH — diligence vs thrash.

    RepoMirage's "exploration drift": broad context access that never converts
    to structure. Up-and-left (many tokens, few distinct paths) is re-reading
    the same file; right (many distinct paths) is genuine discovery. Without
    this axis panel (a) cannot tell the two apart, and they want opposite
    fixes -- a handoff note for the first, a better index for the second.
    """
    points = [s for s in sessions
              if s.metrics.get("orientation_breadth") and s.metrics.get("orientation_tokens")]
    if not points:
        return '<p class="axis">No session touched a recognisable file before its first edit.</p>'
    width, height = 880, 330
    left, right, top, bottom = 66, 24, 34, 56
    pw, ph = width - left - right, height - top - bottom
    xs = [s.metrics["orientation_breadth"] for s in points]
    ys = [s.metrics["orientation_tokens"] for s in points]
    x = linscale(0, max(xs) * 1.08, left, left + pw)
    y = logscale(max(min(ys), 1), max(ys) * 1.3, top + ph, top)
    parts = ['<text x="%d" y="20" class="g-title">Startup tokens against files read first: '
             'high and left is re-reading</text>' % left]
    # NUMERIC TICKS on both axes. The values were in tooltips only, and a
    # tooltip does not survive a screenshot or a projected slide.
    for step in range(5):
        gy = top + ph - ph * step / 4.0
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>'
                     % (left, gy, left + pw, gy))
    lo, hi = max(min(ys), 1), max(ys) * 1.3
    for step in range(5):
        value = lo * (hi / lo) ** (step / 4.0)
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis">%s</text>'
                     % (left - 6, y(value) + 4, esc(compact_number(value))))
    for step in range(5):
        value = max(xs) * 1.08 * step / 4.0
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="grid"/>'
                     % (x(value), top, x(value), top + ph))
        parts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis">%d</text>'
                     % (x(value), top + ph + 16, int(value)))
    for session in points:
        cls = "s2" if session.metrics.get("handoff_opened_by_turn_2") else "s1"
        parts.append(_mark(GLYPH.get(session.source, "circle"),
                           x(session.metrics["orientation_breadth"]),
                           y(max(session.metrics["orientation_tokens"], 1)), 4.5, cls,
                           "%s · %d path(s) · %s tokens · %s per path · %s"
                           % (session.cwd_base, session.metrics["orientation_breadth"],
                              compact_number(session.metrics["orientation_tokens"]),
                              compact_number(session.metrics.get("orientation_tokens_per_path") or 0),
                              session.metrics.get("outcome"))))
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="baseline"/>'
                 % (left, top + ph, left + pw, top + ph))
    parts.append('<text x="%d" y="%d" class="axis">files read before the first edit →   '
                 '(left and high = re-reading the same thing; right = discovery)</text>'
                 % (left, height - 12))
    parts.append('<text x="16" y="%d" transform="rotate(-90 16 %d)" class="axis">startup '
                 'tokens (log)</text>' % (top + ph / 2, top + ph / 2))
    return (legend([("no handoff note read", "var(--accent)"),
                    ("read a handoff note (README excluded)", "var(--accent2)")])
            + svg(width, height, "Files read first", "".join(parts))
            # What "breadth" can and cannot see (RepoMirage draws the same
            # line), printed rather than left to a comment.
            + '<p class="axis">%s</p>' % esc(
                "Files are matched by name, so two files with the same name count once, "
                "and a file counts when a tool named it. %d session(s) are plotted; no-edit "
                "sessions are not." % len(points)))


def panel_j_turn_table(threads: Sequence[Thread], redact: bool = False,
                       top_n: int = 12) -> str:
    """Weave-style thread -> turn table: the members of a thread as rows.

    W&B Weave renders a thread as its turns, ordered by start time, with
    per-turn latency and tokens. Ours is one level up -- a thread's TURNS are
    its member sessions -- which is the level our threads are inferred at, and
    it doubles as the calibration view for `threads --label-candidates`.
    """
    rows = sorted([t for t in threads if len(t.sessions) > 1],
                  key=lambda t: -len(t.sessions))[:top_n]
    if not rows:
        return '<p class="axis">No sitting has more than one session.</p>'
    body = []
    for thread in rows:
        body.append('<tr><td class="l" colspan="8" style="background:var(--chip);'
                    'font-weight:700">%s · %d sessions</td></tr>'
                    % (esc(stable_hash(thread.cwd) if redact else short_path(thread.cwd, 60)),
                       len(thread.sessions)))
        for order, session in enumerate(sorted(thread.sessions,
                                               key=lambda s: s.start or dt.datetime.min.replace(
                                                   tzinfo=dt.timezone.utc)), 1):
            m = session.metrics
            share = m.get("orientation_share")
            duration = session.duration_s
            body.append(
                "<tr><td class='l'>&nbsp;&nbsp;%d</td><td class='l'>%s</td><td>%s</td>"
                "<td class='l'>%s</td><td class='l'>%s</td><td>%s</td><td>%s</td>"
                "<td class='l'>%s</td></tr>"
                % (order, esc((iso(session.start) or "")[:16]),
                   ("%.0f min" % (duration / 60.0)) if duration else "—",
                   esc(", ".join(session.models) or "—"), esc(m.get("effort") or "—"),
                   compact_number(m.get("total_tokens") or 0),
                   ("%.0f%%" % (100 * share)) if share is not None else "no edit",
                   esc("" if redact else (session.first_prompt or session.title or ""))[:90]))
    return ('<div class="scroll"><table><thead><tr><th class="l">#</th><th class="l">start</th>'
            '<th>duration</th><th class="l">model</th><th class="l">effort</th><th>tokens</th>'
            '<th>startup</th><th class="l">first prompt line</th></tr></thead>'
            '<tbody>%s</tbody></table></div>' % "".join(body))


def session_label(session: Session, redact: bool) -> Tuple[str, str]:
    if redact:
        return session.cwd_hash, stable_hash(session.session_id)[:8]
    return session.cwd_base, session.session_id[:8]


def panel_b_scatter(sessions: Sequence[Session], redact: bool = False) -> str:
    points = [s for s in sessions
              if s.metrics.get("orientation_share") is not None
              and (s.metrics.get("est_cost_usd") or 0) > 0]
    width, height = 880, 380
    left, right, top, bottom = 70, 20, 34, 58
    pw, ph = width - left - right, height - top - bottom
    if not points:
        return svg(width, 90, "Cost vs orientation share",
                   '<text x="20" y="50" class="axis">no priced session made an edit</text>')
    costs = [s.metrics["est_cost_usd"] for s in points]
    x = linscale(0, 1, left, left + pw)
    y = logscale(min(costs), max(costs), top + ph, top)
    turns = [s.metrics.get("turns") or 1 for s in points]
    r = linscale(min(turns), max(turns), 3.0, 13.0)
    parts = ['<text x="%d" y="20" class="g-title">Est. cost against startup share '
             '(size = turns, shape = store)</text>' % left]
    for step in range(5):
        gx = x(step / 4.0)
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="grid"/>' % (gx, top, gx, top + ph))
        parts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis">%.2f</text>'
                     % (gx, height - 34, step / 4.0))
    decade = math.floor(math.log10(max(min(costs), 1e-6)))
    while 10 ** decade <= max(costs) * 1.05:
        gy = y(10 ** decade)
        if top - 2 <= gy <= top + ph + 2:
            parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>' % (left, gy, left + pw, gy))
            parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis">$%s</text>'
                         % (left - 6, gy + 4, ("%g" % (10 ** decade))))
        decade += 1
    for session in sorted(points, key=lambda s: -(s.metrics.get("turns") or 0)):
        cls = "s2" if session.metrics.get("handoff_opened_by_turn_2") else "s1"
        where, who = session_label(session, redact)
        tip = "%s · %s · share %.2f · $%.2f · %d turns%s" % (
            where, who, session.metrics["orientation_share"],
            session.metrics["est_cost_usd"], session.metrics.get("turns") or 0,
            " · read a handoff note" if session.metrics.get("handoff_opened_by_turn_2") else "")
        parts.append('<g opacity="0.72">' + _mark(
            GLYPH.get(session.source, "circle"), x(session.metrics["orientation_share"]),
            y(session.metrics["est_cost_usd"]), r(session.metrics.get("turns") or 1), cls, tip) + "</g>")
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="baseline"/>'
                 % (left, top + ph, left + pw, top + ph))
    parts.append('<text x="%d" y="%d" class="axis">startup share</text>' % (left, height - 12))
    parts.append('<text x="14" y="%d" transform="rotate(-90 14 %d)" class="axis">'
                 'est. $ (log)</text>' % (top + ph / 2, top + ph / 2))
    return (legend([("no handoff note read", "var(--accent)"),
                    ("read a handoff note", "var(--accent2)")])
            + svg(width, height, "Cost against startup share", "".join(parts)))


def panel_c_thread_stacks(records: Sequence[Dict[str, Any]], top_n: int = 25) -> str:
    rows = [r for r in records if r["sessions"] > 1]
    rows.sort(key=lambda r: -r["reorientation_tax_tokens"])
    rows = rows[:top_n]
    if not rows:
        return '<p class="axis">No sitting has more than one session.</p>'
    # STARTUP SPEND GETS ITS OWN SCALE. Drawn only on
    # the thread's total-token scale, a 200k startup inside a 40M thread is a
    # hairline and the panel "locates large workflows" instead of showing the
    # component it is ranked by. Each row is now two bars: the whole thread on
    # the shared token scale, and beneath it the startup spend of sessions
    # 2..n on a scale of its own.
    row_h = 34
    width = 880
    left, right, top = 176, 180, 40
    pw = width - left - right
    footer = ("Top bar: all tokens of the sitting. Thin bar: its repeat startup, on its own "
              "scale. Right: repeat startup (share of the sitting) and the sitting's est. cost.")
    height = int(top + row_h * len(rows) + 26 + text_height(footer, pw, "axis"))
    peak = max(r["total_tokens"] for r in rows) or 1
    tax_peak = max((r["reorientation_tax_tokens"] for r in rows), default=1) or 1
    x = linscale(0, peak, 0, pw)
    xt = linscale(0, tax_peak, 0, pw)
    parts = ['<text x="14" y="20" class="g-title">Repeat startup is a thin slice of most '
             'sittings (top %d by repeat startup)</text>' % len(rows)]
    for index, row in enumerate(rows):
        y = top + index * row_h
        tax = row["reorientation_tax_tokens"]
        first = max(0, row["orientation_tokens"] - tax)
        work = max(0, row["total_tokens"] - row["orientation_tokens"])
        cursor = left
        for value, fill, what in ((tax, "var(--accent2)", "repeat startup"),
                                  (first, "var(--accent)", "first session's startup"),
                                  (work, "var(--stage1)", "the rest of the sitting")):
            w = x(value)
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" fill="%s" '
                         'data-tip="%s · %s · %s tok"/>'
                         % (cursor, y + 3, max(w, 0.4), 13, fill,
                            esc(row["thread_id"]), esc(what), compact_number(value)))
            cursor += w
        parts.append('<rect x="%d" y="%.1f" width="%.1f" height="6" rx="2" '
                     'fill="var(--accent2)" opacity="0.85" data-tip="%s"/>'
                     % (left, y + 19, max(xt(tax), 0.6),
                        esc("%s · repeat startup · %s tokens · $%.2f "
                            "(the sitting as a whole cost $%.2f)"
                            % (row["thread_id"], compact_number(tax),
                               row.get("reorientation_tax_usd") or 0.0,
                               row["est_cost_usd"]))))
        label = folder_name(row.get("cwd"), "")[:24] or row["thread_id"]
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong" '
                     'data-tip="%s">%s</text>'
                     % (left - 8, y + row_h / 2 + 1, esc("%s · %d sessions · %s · $%.2f" % (
                         row.get("cwd"), row["sessions"], ", ".join(row["tools"]),
                         row["est_cost_usd"])), esc(label)))
        parts.append(text_block(left + pw + 6, y + 8, right - 12,
                                "%s (%s%%) · $%.2f sitting"
                                % (compact_number(tax),
                                   "%.0f" % (100 * (row["reorientation_tax_share"] or 0)),
                                   row["est_cost_usd"]), cls="val"))
    parts.append(text_block(left, top + row_h * len(rows) + 16, pw, footer, cls="axis"))
    return (legend([("repeat startup (2nd session onward)", "var(--accent2)"),
                    ("first session's startup", "var(--accent)"),
                    ("the rest of the sitting", "var(--stage1)")])
            + svg(width, height, "Repeat startup per sitting", "".join(parts)))


# The stage names are machine values (`stage_path` in the JSON); a reader sees these.
STAGE_LABEL = {"orient": "startup", "explore": "explore", "mutate": "edit",
               "verify": "check", "unclassified": "unclassified"}
STAGE_CLASS = {"orient": "var(--stage1)", "explore": "var(--stage2)",
               "mutate": "var(--stage3)", "verify": "var(--stage4)",
               "unclassified": "var(--div-mid)"}


def panel_c2_stage_paths(threads: Sequence[Thread], redact: bool = False,
                         top_n: int = 20) -> str:
    """Per-thread stacked bars by STAGE, not by orientation-vs-work.

    Helicone's `Helicone-Session-Path` made the case that a session's shape is
    more legible than its totals; the four stage names come from Coherence
    Collapse (arXiv 2603.24631). Stacking four segments instead of two is what
    makes a thread that keeps re-entering `orient` visible as a shape rather
    than as a slightly larger number.
    """
    rows = sorted([t for t in threads if len(t.sessions) > 1],
                  key=lambda t: -sum(s.metrics.get("total_tokens", 0) for s in t.sessions))[:top_n]
    if not rows:
        return '<p class="axis">No sitting has more than one session.</p>'
    row_h, width = 24, 880
    left, right, top = 190, 96, 36
    pw = width - left - right
    footer = ("turns →   ↺ = a session that went back to startup after starting work. "
              "Segments run in time order. unclassified = a text-only response after the "
              "first edit.")
    height = int(top + row_h * len(rows) + 30 + text_height(footer, pw, "axis"))
    # ORDERED SEGMENTS, in the order they happened.
    # This used to sum the stages into a Counter and draw them in a fixed
    # `orient -> explore -> mutate -> verify` order, which is exactly the
    # temporal information the panel claims to show -- a thread that re-enters
    # `orient` late looked identical to one that never did.
    totals = []
    for thread in rows:
        ordered: List[Tuple[str, int]] = []
        for session in sorted(thread.sessions,
                              key=lambda s: s.start or dt.datetime.min.replace(tzinfo=dt.timezone.utc)):
            for stage, turns in (session.metrics.get("stage_path") or []):
                if ordered and ordered[-1][0] == stage:
                    ordered[-1] = (stage, ordered[-1][1] + turns)
                else:
                    ordered.append((stage, turns))
        totals.append(ordered)
    peak = max((sum(n for _, n in seq) for seq in totals), default=1) or 1
    x = linscale(0, peak, 0, pw)
    parts = ['<text x="14" y="20" class="g-title">What each sitting did, in order: startup, '
             'explore, edit, check (width = turns)</text>']
    for thread, ordered in zip(rows, totals):
        index = rows.index(thread)
        y = top + index * row_h
        cursor = left
        run = sum(n for _, n in ordered)
        for position, (stage, value) in enumerate(ordered, 1):
            w = x(value)
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="3" fill="%s" '
                         'data-tip="%s"/>'
                         % (cursor, y + 3, max(w - 2, 0.6), row_h - 8, STAGE_CLASS[stage],
                            esc("%s · segment %d of %d · %s · %d turn(s) of %d"
                                % (folder_name(thread.cwd, "") or thread.id,
                                   position, len(ordered), STAGE_LABEL[stage], value, run))))
            cursor += w                      # the 2px gap above is the surface spacer
        where = stable_hash(thread.cwd) if redact else folder_name(thread.cwd, "")[:20]
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong">%s</text>'
                     % (left - 8, y + row_h / 2 + 3, esc(where or thread.id)))
        reentry = sum(1 for session in thread.sessions
                      for i, (stage, _) in enumerate((session.metrics.get("stage_path") or []))
                      if stage == "orient" and i > 0)
        parts.append('<text x="%d" y="%.1f" class="val">%d turns%s</text>'
                     % (left + pw + 8, y + row_h / 2 + 3, run,
                        "  ↺%d" % reentry if reentry else ""))
    parts.append(text_block(left, top + row_h * len(rows) + 20, pw, footer, cls="axis"))
    return (legend([(STAGE_LABEL[name], STAGE_CLASS[name]) for name in STAGE_ORDER])
            + svg(width, height, "What each sitting did", "".join(parts)))


def panel_d_timeline(threads: Sequence[Thread], redact: bool = False, top_n: int = 30) -> str:
    rows = sorted([t for t in threads if len(t.sessions) > 1],
                  key=lambda t: -len(t.sessions))[:top_n]
    if not rows:
        return '<p class="axis">No multi-session thread to plot.</p>'
    starts = [t.start for t in rows if t.start]
    ends = [t.end for t in rows if t.end]
    t0, t1 = min(starts), max(ends)
    width = 880
    left, right, top, row_h = 180, 24, 40, 20
    height = top + row_h * len(rows) + 44
    pw = width - left - right
    span = max((t1 - t0).total_seconds(), 1.0)
    x = lambda moment: left + (moment - t0).total_seconds() * pw / span
    parts = ['<text x="14" y="20" class="g-title">Thread timeline — one row per thread, '
             'one mark per session, shape = tool</text>']
    ticks = 6
    for step in range(ticks + 1):
        moment = t0 + dt.timedelta(seconds=span * step / ticks)
        gx = left + pw * step / ticks
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="grid"/>'
                     % (gx, top - 6, gx, top + row_h * len(rows)))
        parts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis">%s</text>'
                     % (gx, height - 22, moment.strftime("%Y-%m-%d")))
    for index, thread in enumerate(rows):
        y = top + index * row_h + row_h / 2
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" class="grid"/>'
                     % (x(thread.start), y, x(thread.end), y))
        where = (stable_hash(thread.cwd) if redact
                 else folder_name(thread.cwd, "")[:20])
        label = where or thread.id
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong" '
                     'data-tip="%s">%s</text>'
                     % (left - 8, y + 4,
                        esc("%s · %d sessions" % (stable_hash(thread.cwd) if redact else thread.cwd,
                                                  len(thread.sessions))),
                        esc(label)))
        for session in thread.sessions:
            if not session.start:
                continue
            share = session.metrics.get("orientation_share")
            cls = "s2" if session.metrics.get("handoff_opened_by_turn_2") else "s1"
            tip = "%s · %s · %s · %d turns · orientation %s" % (
                session.source, session_label(session, redact)[1], iso(session.start),
                session.metrics.get("turns") or 0,
                ("%.0f%%" % (100 * share)) if share is not None else "no mutation")
            parts.append(_mark(GLYPH.get(session.source, "circle"), x(session.start), y, 5, cls, tip))
    parts.append('<text x="%d" y="%d" class="axis">● Claude Code   ■ VS Code Copilot   '
                 '▲ Copilot CLI   (orange = notes opened by turn 2)</text>' % (left, height - 6))
    return svg(width, height, "Thread timeline", "".join(parts))


def panel_stream_timeline(streams: Sequence["Stream"], redact: bool = False,
                          top_n: int = 22, gap_hours: float = 72.0) -> str:
    """The timeline, one row per STREAM, episodes drawn as clusters on that row.

    Work that picks up for short periods each month — an upload done once a
    month, a side project touched every few weeks — shows as different
    threads. Should those be on the same line? They should. The 72-hour gap rule is
    right that they are separate SITTINGS and wrong that they are separate
    WORK, so the stream is the line and the episodes are clusters on it, with
    the gap printed in days and the reason two clusters are separate said out
    loud.
    """
    rows = sorted([s for s in streams if s.sessions],
                  key=lambda s: (-len(s.threads), -len(s.sessions)))[:top_n]
    rows = [s for s in rows if s.start and s.end]
    if not rows:
        return '<p class="axis">No stream with a timestamp to plot.</p>'
    t0 = min(s.start for s in rows)
    t1 = max(s.end for s in rows)
    width = 900
    left, right, top, row_h = 210, 40, 44, 26
    pw = width - left - right
    span = max((t1 - t0).total_seconds(), 1.0)
    x = lambda moment: left + (moment - t0).total_seconds() * pw / span
    footer = ("Several bands on one row: the same work picked up again. An orange dot is "
              "a session that read a handoff note.")
    foot_h = text_height(footer, width - 28, "axis")
    height = int(top + row_h * len(rows) + 34 + foot_h + 10)
    parts = [text_block(14, 20, width - 28,
                        "Each work item over time: one row each, one band per sitting",
                        cls="g-title")]
    ticks = 6
    for step in range(ticks + 1):
        moment = t0 + dt.timedelta(seconds=span * step / ticks)
        gx = left + pw * step / ticks
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="grid"/>'
                     % (gx, top - 6, gx, top + row_h * len(rows)))
        parts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis">%s</text>'
                     % (gx, top + row_h * len(rows) + 18, moment.strftime("%Y-%m-%d")))
    for index, stream in enumerate(rows):
        y = top + index * row_h + row_h / 2
        name = stable_hash(stream.name) if redact else stream.name
        label = str(name)[:26]
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong" '
                     'data-tip="%s">%s</text>'
                     % (left - 8, y + 4,
                        esc("%s · grouped by %s · %d sitting(s), %d session(s) · %s"
                            % (name, stream.named_by, len(stream.threads),
                               len(stream.sessions),
                               "; ".join(stream.evidence) or "no shared token recorded")),
                        esc(label)))
        ordered = [t for t in stream.threads if t.start and t.end]
        for episode in ordered:
            # the episode itself: a light rule under its sessions
            parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--accent)" '
                         'stroke-width="6" opacity="0.16" stroke-linecap="round"/>'
                         % (x(episode.start) - 3, y, x(episode.end) + 3, y))
            for session in episode.sessions:
                if not session.start:
                    continue
                share = session.metrics.get("orientation_share")
                cls = "s2" if session.metrics.get("handoff_opened_by_turn_2") else "s1"
                parts.append(_mark(
                    GLYPH.get(session.source, "circle"), x(session.start), y, 4, cls,
                    "%s · %s · %s · startup %s"
                    % (session.source, session_label(session, redact)[1], iso(session.start),
                       ("%.0f%%" % (100 * share)) if share is not None else "n/a: no edit")))
        # The breaks BETWEEN sittings. The label measures the SAME quantity
        # the 72-hour rule tests (start of one sitting to start of the next),
        # and only claims the rule split them when it actually did.
        for previous, following in zip(ordered, ordered[1:]):
            last_start = max((s.start for s in previous.sessions if s.start),
                             default=previous.start)
            days = (following.start - last_start).total_seconds() / 86400.0
            wall = (following.start - previous.end).total_seconds() / 86400.0
            mx = (x(previous.end) + x(following.start)) / 2
            parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--muted)" '
                         'stroke-width="1" stroke-dasharray="2 3"/>'
                         % (x(previous.end), y, x(following.start), y))
            if days >= 0.5:
                why = ("%s between sittings, more than the %s that ends a sitting, so "
                       "these are separate sittings of the same work item"
                       % (days_text(days), hours_text(gap_hours))
                       if days * 24 >= gap_hours
                       else "%s between sittings, less than %s; the shared-file test "
                            "separated them, not time" % (days_text(days),
                                                          hours_text(gap_hours)))
                parts.append('<text x="%.1f" y="%.1f" text-anchor="middle" class="axis" '
                             'data-tip="%s">%s</text>'
                             % (mx, y - 8,
                                esc("%s (%s of wall clock between the last response and "
                                    "the next)" % (why, days_text(wall))),
                                esc("%.0fd" % days)))
    parts.append(text_block(14, top + row_h * len(rows) + 40, width - 28, footer, cls="axis"))
    # Legends must SHOW the encoding, not name it: the orange swatch is drawn
    # in orange, and the shaded band and the dotted gap line are drawn as
    # themselves rather than described. Same component, four encodings, each drawn.
    timeline = svg(width, height, "Work items over time", "".join(parts))
    return timeline + (legend_rich([
        ("a session", swatch_solid("var(--accent)")),
        ("a session that read a handoff note", swatch_solid("var(--accent2)")),
        ("band = one sitting: sessions less than 72 hours apart", swatch_band("var(--accent)")),
        ("dotted line = the break between sittings, in days", swatch_dotted("var(--muted)")),
        ("● Claude Code · ■ VS Code Copilot · ▲ Copilot CLI",
         '<i class="sw" style="background:transparent"></i>'),
    ]))


def panel_e_matrix(sessions: Sequence[Session]) -> str:
    grid: Dict[Tuple[str, str], List[float]] = collections.defaultdict(list)
    for session in sessions:
        cost = session.metrics.get("est_cost_usd")
        model = primary_model(session).replace("claude-", "")
        if cost is None:
            continue                     # no token counts: unpriced, not $0
        grid[(session.metrics.get("strategy", "?"), model)].append(cost)
    strategies = ["scripted", "mixed", "by-hand", "read-only"]
    strategies = [s for s in strategies if any(k[0] == s for k in grid)] or ["?"]
    models = sorted({k[1] for k in grid})
    cell_w, cell_h = max(90, min(150, int(700 / max(1, len(models))))), 54
    left = 110
    # Wide enough for the title on one line even with a single model column.
    width = max(640, left + cell_w * len(models) + 20)
    top = 72
    height = top + cell_h * len(strategies) + 40
    medians = [statistics.median(v) for v in grid.values() if v]
    peak = max(medians) if medians else 1.0
    parts = [text_block(14, 20, width - 28, "Median est. cost per session, by strategy "
                        "and model", cls="g-title"),
             text_block(14, 38, width - 28, "scripted = Bash-led, at most %d hand edits; "
                        "by-hand = many Edit/Write calls" % SCRIPTED_EDIT_MAX, cls="axis")]
    for column, model in enumerate(models):
        parts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis-strong">%s</text>'
                     % (left + cell_w * (column + 0.5), top - 8, esc(model[:16])))
    for row, strategy in enumerate(strategies):
        y = top + row * cell_h
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong">%s</text>'
                     % (left - 8, y + cell_h / 2 + 4, esc(strategy)))
        for column, model in enumerate(models):
            values = grid.get((strategy, model)) or []
            x = left + column * cell_w
            if not values:
                parts.append('<rect x="%.1f" y="%.1f" width="%d" height="%d" rx="6" '
                             'fill="none" stroke="var(--line)"/>' % (x + 2, y + 2, cell_w - 6, cell_h - 6))
                continue
            med = statistics.median(values)
            alpha = 0.12 + 0.78 * (med / peak if peak else 0)
            parts.append('<rect x="%.1f" y="%.1f" width="%d" height="%d" rx="6" class="s1" '
                         'opacity="%.2f" data-tip="%s · %s · n=%d · median $%.3f · max $%.2f"/>'
                         % (x + 2, y + 2, cell_w - 6, cell_h - 6, alpha,
                            esc(strategy), esc(model), len(values), med, max(values)))
            parts.append('<text x="%.1f" y="%.1f" text-anchor="middle" class="val">$%s</text>'
                         % (x + cell_w / 2 - 2, y + cell_h / 2, ("%.2f" % med) if med >= 0.01 else "%.4f" % med))
            parts.append('<text x="%.1f" y="%.1f" text-anchor="middle" class="axis">n=%d</text>'
                         % (x + cell_w / 2 - 2, y + cell_h / 2 + 15, len(values)))
    return svg(width, height, "Strategy by model", "".join(parts))


EFFORT_ORDER = ["low", "medium", "high", "xhigh", "max", "unrecorded"]

# COLOUR ONLY — one hue per effort level, no shape encoding. Small shapes are
# hard to tell apart, so colour is the distinguishing factor and shape is
# taken out. Every mark is now the same
# circle at the same size, and the legend names the hue. The ramp is ordered
# (low -> max) because effort is ordinal, so a sequential ramp is the correct
# encoding; it runs pale-blue to deep-blue with a warm accent for `max` so the
# top of the scale separates, and `unrecorded` is neutral grey because it is
# an absence, not a level.
EFFORT_COLOUR = {
    "low": "#cde2fb", "medium": "#86b6ef", "high": "#2a78d6",
    "xhigh": "#184f95", "max": "#eb6834", "unrecorded": "#9aa7bb",
}


def _effort_mark(level: str, x: float, y: float, tip: str) -> str:
    return ('<circle cx="%.1f" cy="%.1f" r="3.6" fill="%s" opacity="0.75" '
            'stroke="var(--card)" stroke-width="0.6" data-tip="%s"/>'
            % (x, y, EFFORT_COLOUR.get(level, EFFORT_COLOUR["unrecorded"]), esc(tip)))


def panel_f_thinking(sessions: Sequence[Session]) -> str:
    """Effort AND its cross-check.

    `effort` is recorded on every assistant record on a recent CLI; where it
    is absent the thinking share is the only stand-in, so both are shown and the
    table lets you check that the recorded level and the reasoning volume
    agree.
    """
    cells: Dict[Tuple[str, str], List[float]] = collections.defaultdict(list)
    for session in sessions:
        for key, row in (session.metrics.get("per_model_effort") or {}).items():
            if row.get("thinking_share") is None:
                continue
            model, _, level = key.rpartition("|")
            if str(model).strip().lower() in SYNTHETIC_MODELS:
                continue
            cells[(model.replace("claude-", ""), level)].append(row["thinking_share"])
    if not cells:
        return '<p class="axis">No thinking-token signal in this history.</p>'
    models = sorted({m for m, _ in cells},
                    key=lambda m: -statistics.median(
                        [v for (mm, _), vs in cells.items() if mm == m for v in vs]))
    width = 880
    left, right, top, row_h = 150, 78, 44, 34
    height = top + row_h * len(models) + 42
    pw = width - left - right
    peak = max(max(v) for v in cells.values()) or 1.0
    x = linscale(0, peak, left, left + pw)
    parts = ['<text x="14" y="18" class="g-title">Thinking share per model — reasoning tokens ÷ '
             'output tokens, one mark per session</text>',
             '<text x="14" y="34" class="axis">one dot per session · colour = the effort '
             'level recorded on the record · the vertical rule is the model median</text>']
    for step in range(6):
        gx = left + pw * step / 5
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="grid"/>'
                     % (gx, top - 8, gx, top + row_h * len(models)))
        parts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis">%.2f</text>'
                     % (gx, height - 20, peak * step / 5))
    for index, model in enumerate(models):
        y = top + index * row_h + row_h / 2
        every = [v for (mm, _), vs in cells.items() if mm == model for v in vs]
        med = statistics.median(every)
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong">%s</text>'
                     % (left - 8, y + 4, esc(model[:18])))
        for level in EFFORT_ORDER:
            for value in cells.get((model, level), []):
                parts.append(_effort_mark(level, x(value), y,
                                          "%s · effort %s · thinking share %.3f"
                                          % (model, level, value)))
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--accent2)" '
                     'stroke-width="2.5" data-tip="%s median %.3f (n=%d)"/>'
                     % (x(med), y - 11, x(med), y + 11, esc(model), med, len(every)))
        parts.append('<text x="%d" y="%.1f" class="val">%.2f (n=%d)</text>'
                     % (left + pw + 8, y + 4, med, len(every)))
    parts.append('<text x="%d" y="%d" class="axis">Where `effort` is absent (older CLI), '
                 'thinking volume is the only stand-in — the table shows both.</text>'
                 % (left, height - 4))
    levels = [l for l in EFFORT_ORDER if any(k[1] == l for k in cells)]
    header = "".join("<th>%s</th>" % esc(l) for l in levels)
    rows = ""
    for model in models:
        cellhtml = ""
        for level in levels:
            values = cells.get((model, level)) or []
            cellhtml += ("<td>%s</td>" % ("%.2f <small>(n=%d)</small>"
                                          % (statistics.median(values), len(values))
                                          if values else "—"))
        rows += "<tr><td class='l'>%s</td>%s</tr>" % (esc(model), cellhtml)
    table = ("<table><thead><tr><th class='l'>model</th>%s</tr></thead><tbody>%s</tbody></table>"
             % (header, rows))
    return (legend([(l, EFFORT_COLOUR[l]) for l in EFFORT_ORDER
                    if any(k[1] == l for k in cells)])
            + svg(width, height, "Thinking share per model and effort", "".join(parts))
            + table)


def panel_g_meter(meter: Dict[str, Any]) -> str:
    five = meter["five_hour_buckets"][-42:]
    weekly = meter["weekly_buckets"][-16:]
    def bars(rows, key, title, colour_class):
        if not rows:
            return '<p class="axis">no data</p>'
        width, height = 860, 170
        left, right, top, bottom = 60, 14, 26, 42
        pw, ph = width - left - right, height - top - bottom
        peak = max(r["tokens"] for r in rows) or 1
        slot = pw / len(rows)
        parts = ['<text x="%d" y="16" class="g-title">%s</text>' % (left, esc(title))]
        for index, row in enumerate(rows):
            h = ph * row["tokens"] / peak
            x = left + index * slot + slot * 0.14
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" class="%s" '
                         'data-tip="%s · %s tok · %d turns"/>'
                         % (x, top + ph - h, max(slot * 0.72, 1.0), h, colour_class,
                            esc(row[key]), compact_number(row["tokens"]), row["turns"]))
        parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="baseline"/>'
                     % (left, top + ph, left + pw, top + ph))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis">%s</text>'
                     % (left - 6, top + 10, compact_number(peak)))
        parts.append('<text x="%d" y="%d" class="axis">%s → %s</text>'
                     % (left, height - 12, esc(rows[0][key]), esc(rows[-1][key])))
        return svg(width, height, title, "".join(parts))
    readings = meter["readings"][-12:]
    rows = "".join(
        "<tr><td class='l'>%s</td><td class='l'>%s</td><td class='l'>%s</td><td class='l'>%s</td></tr>"
        % (esc(r.get("at")), esc(r.get("kind")), esc(r.get("limit_kind") or "—"),
           esc(r.get("reset_text") or r.get("five_hour_resets_at") or "—"))
        for r in readings) or "<tr><td colspan='4' class='l'>no rate-limit record found</td></tr>"
    return (bars(five, "bucket", "Token volume per %g-hour bucket" % meter["bucket_hours"], "s1")
            + bars(weekly, "week_starting", "Token volume per week (Monday-anchored, UTC)", "s2")
            + "<table><thead><tr><th class='l'>reading dated by session start</th><th class='l'>kind</th>"
              "<th class='l'>limit</th><th class='l'>reset</th></tr></thead><tbody>%s</tbody></table>" % rows)


# --------------------------------------------------- analysis index + summary
# THE ANALYSIS INDEX goes at the top, so the reader knows everything else the
# agent can do. A button labelled "run here" read as if clicking it would run
# a program.
#
# So: no buttons, nothing that looks clickable-to-run. Each row is either
# INCLUDED, with a link to the page it is on, or NOT RUN, with the exact
# command to hand to your agent. `page` is filled in per report, because
# whether an analysis is included depends on the flags this run was given.
ANALYSIS_INDEX = [
    # key, title, the question it answers, the command, the ledger columns it
    # contributes. The last field is what makes the index a map of the LEDGER
    # rather than a list of scripts; it is shown as a tooltip.
    ("orientation", "Startup cost",
     "How many tokens go before each session's first edit?",
     "orientation_profile.py report --output-dir OUT --ledger OUT/sessions.jsonl",
     "orientation_share, orientation_tokens, orientation_position, orientation_breadth, "
     "handoff_opened_by_turn_2, question_first, strategy, effort, est_cost_usd"),
    ("streams", "Work items and sittings",
     "Which work keeps coming back, and what does each return cost?",
     "orientation_profile.py threads --stream-by auto",
     "thread_id, stream_id, stream_name, stream_named_by"),
    ("carry", "Pick-up cost",
     "Is resuming a session cheaper than starting a new one on the same work?",
     "orientation_profile.py report --output-dir OUT --ledger OUT/sessions.jsonl "
     "--resume-gap-hours 1",
     "continuation_kind, carry_tokens, carry_cost_usd, gap_hours_before, compactions"),
    ("ledger", "Session ledger",
     "One row per session; every analysis adds its own columns.",
     "orientation_profile.py report --output-dir OUT --ledger OUT/sessions.jsonl",
     "the table the other analyses join on"),
    ("cache", "Cache lifetime",
     "Should the prompt cache last 5 minutes or 1 hour?",
     "claude_cache_audit.py --root ~/.claude --output-dir OUT --ledger OUT/sessions.jsonl",
     "cache_hit_ratio, cache_cold_events, cache_ttl_band"),
    ("bloat", "Tool-result bloat",
     "Which tool's results cost the most to carry along?",
     "tool_bloat.py --since YYYY-MM-DD --ledger OUT/sessions.jsonl",
     "tool_result_bytes, top_tool, top_tool_bytes"),
    ("lanes", "Conversations and subagents",
     "Where did the week's usage go?",
     "lane_profile.py --root ~/.claude/projects --since YYYY-MM-DD",
     "none (it prints its own report)"),
    ("compact", "Compaction",
     "Does each /compact pay for itself?",
     "compact_economics.py --since YYYY-MM-DD",
     "none (it prints its own report)"),
    ("meter", "Usage meter",
     "How close is this history to the 5-hour and weekly limits?",
     "orientation_profile.py meter    # subscription (Max/Pro) plans",
     "nothing: API billing has no limit to be close to"),
]


# The script behind each analysis that keeps its own report. Whether it ran is
# read from the LEDGER it wrote into, never assumed.
INDEX_PRODUCER = {"cache": "claude_cache_audit.py", "bloat": "tool_bloat.py",
                  "lanes": "lane_profile.py", "compact": "compact_economics.py"}
# These two have no --ledger option yet, so the ledger cannot say they ran.
PRINTS_OWN_REPORT = {"lanes", "compact"}


def ledger_runs(path: Optional[str]) -> Dict[str, str]:
    """producer -> the UTC date it last wrote values into this ledger.

    A producer counts only when one of its columns holds a value: a column
    that is registered but empty is an analysis that has not run here. The
    date is the run recorded in the sidecar, else the column's own stamp.
    """
    if not path:
        return {}
    try:
        import session_ledger
        rows, provenance = session_ledger.load(path)
    except Exception:                              # an unreadable ledger proves nothing
        return {}
    stamps: Dict[str, str] = {}
    for column, meta in (provenance.get("columns") or {}).items():
        producer, updated = meta.get("producer"), meta.get("updated")
        if (producer and updated
                and any(row.get(column) is not None for row in rows.values())):
            stamps[producer] = max(stamps.get(producer, ""), str(updated))
    scopes = provenance.get("scopes") or {}
    return {producer: str((scopes.get(producer) or {}).get("at") or stamp)[:10]
            for producer, stamp in stamps.items()}


def analysis_index(included: Dict[str, str],
                   unavailable: Optional[Dict[str, str]] = None,
                   ran: Optional[Dict[str, str]] = None) -> str:
    """The index as a compact checklist: name, question, and either a link
    (in this report), the ledger an analysis wrote into, or the command to
    hand to an agent.

    A third state: an analysis that RAN and found nothing to compare. That is
    not "not run" -- re-running would change nothing -- so it says what was
    missing instead of offering a command. Nothing here looks clickable-to-run.
    `ran` is `ledger_runs()`: an analysis with its own report shows a tick
    when its columns are in the ledger.
    """
    unavailable = unavailable or {}
    ran = ran or {}
    rows = ""
    for key, title, question, command, adds in ANALYSIS_INDEX:
        href = included.get(key)
        producer = INDEX_PRODUCER.get(key)
        if href:
            state = '<span class="ok">✓</span> <a href="%s">in this report</a>' % esc(href)
        elif producer in ran:
            target = included.get("ledger")
            state = ('<span class="ok">✓</span> %s · ran %s'
                     % ('<a href="%s">in the ledger</a>' % esc(target) if target
                        else "in the ledger", esc(ran[producer])))
        elif key in unavailable:
            state = '<span class="st">ran — %s</span>' % esc(unavailable[key])
        elif key in PRINTS_OWN_REPORT:
            state = ('<span class="st">prints its own report — ask your agent:</span> '
                     '<code>%s</code>' % esc(command))
        else:
            state = '<span class="st">not run — ask your agent:</span> <code>%s</code>' % esc(command)
        rows += ('<tr data-tip="%s"><td class="l"><strong>%s</strong></td>'
                 '<td class="l">%s</td><td class="l">%s</td></tr>'
                 % (esc("adds ledger columns: " + adds), esc(title), esc(question), state))
    return ('<table class="check"><thead><tr><th class="l">analysis</th>'
            '<th class="l">question</th><th class="l">status</th></tr></thead>'
            '<tbody>%s</tbody></table>' % rows)


# ------------------------------------------------------ the Experiment panel
def panel_h_experiment(report: Optional[Dict[str, Any]]) -> str:
    """The handoff experiment: paired dots, net-value bars, mediator tile — in that order.

    The DOTS are the result. One row per thread, a dot per arm, joined by a
    line: the line's slope IS the answer and a reader gets it without reading
    a number. Colours are the categorical arm pair (blue NOTE / orange NONE);
    the net-value bar is a different job -- polarity around zero -- so it uses
    the diverging pair, which is the only place red appears.

    Renders in BOTH degenerate states, because both are real states of a
    handoff log in its first weeks: zero pairs ("N ends logged, 0 successors
    yet") and exactly one pair.
    """
    if report is None:
        return ('<p class="axis">No <code>--handoff-log</code> was given, so the field '
                'experiment has nothing to read back. The CSV format is in '
                '<code>references/orientation-and-threads.md</code> §2b.</p>')
    pairs = report["pairs"]
    header = ('<p class="axis">%d end%s logged · %d pending (no successor yet) · %d unmatched · '
              '%d successor%s joined · <strong>%d usable pair%s</strong></p>'
              % (report["ends_logged"], "" if report["ends_logged"] == 1 else "s",
                 report["pending_ends"], report["unmatched_ends"],
                 report["successor_rows"], "" if report["successor_rows"] == 1 else "s",
                 report["usable_pairs"], "" if report["usable_pairs"] == 1 else "s"))
    banner = ""
    if report["underpowered"]:
        banner = ('<p class="note"><strong>UNDERPOWERED — %d usable pair(s), floor is %d.</strong><br>%s</p>'
                  % (report["usable_pairs"], MIN_USABLE_PAIRS,
                     esc(UNDERPOWERED_BANNER).replace("\n", "<br>")))
    mediator = _mediator_tile(report["mediator"])
    if not pairs:
        return (header + banner
                + '<p class="axis" style="padding:28px 0;text-align:center;font-size:15px">'
                  '<strong>%d end%s logged, 0 successors yet.</strong><br>'
                  'The paired dots appear once a logged sitting is picked up again. '
                  'Keep logging.'
                  '</p>' % (report["ends_logged"], "" if report["ends_logged"] == 1 else "s")
                + mediator)

    # ---- paired dots -----------------------------------------------------
    row_h = 26
    width, left, right, top = 880, 200, 120, 40
    height = top + row_h * len(pairs) + 52
    pw = width - left - right
    values = [v for p in pairs for v in (p["note_orientation_tokens"], p["none_orientation_tokens"]) if v]
    lo, hi = (min(values) or 1, max(values) or 1) if values else (1, 1)
    x = logscale(max(lo * 0.6, 1), hi * 1.6, left, left + pw)
    parts = ['<text x="14" y="20" class="g-title">Startup tokens per sitting, with a note '
             'and without (log scale)</text>']
    for step in range(5):
        gx = left + pw * step / 4.0
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" class="grid"/>'
                     % (gx, top - 8, gx, top + row_h * len(pairs)))
    for index, pair in enumerate(pairs):
        y = top + index * row_h + row_h / 2
        xn, xo = x(max(pair["note_orientation_tokens"], 1)), x(max(pair["none_orientation_tokens"], 1))
        won = pair["difference"] < 0
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--muted)" '
                     'stroke-width="2" opacity="0.55"/>' % (xn, y, xo, y))
        # 2px surface ring on each mark so the two never merge where they meet
        for cx, colour, label in ((xo, "var(--arm-none)", "NONE"), (xn, "var(--arm-note)", "NOTE")):
            parts.append('<circle cx="%.1f" cy="%.1f" r="6.5" fill="var(--card)"/>' % (cx, y))
            parts.append('<circle cx="%.1f" cy="%.1f" r="5" fill="%s" data-tip="%s"/>'
                         % (cx, y, colour,
                            esc("%s · %s · %s startup tokens · %d session(s)"
                                % (folder_name(pair["cwd"], "") or pair["thread_id"],
                                   label,
                                   compact_number(pair["note_orientation_tokens"] if label == "NOTE"
                                                  else pair["none_orientation_tokens"]),
                                   pair["note_successors"] if label == "NOTE" else pair["none_successors"]))))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong" data-tip="%s">%s</text>'
                     % (left - 10, y + 4, esc(str(pair["cwd"] or "")),
                        esc(folder_name(pair["cwd"], "")[:24] or pair["thread_id"])))
        parts.append('<text x="%d" y="%.1f" class="val">%s%s</text>'
                     % (left + pw + 10, y + 4, "−" if won else "+",
                        compact_number(abs(pair["difference"]))))
    parts.append('<text x="%d" y="%d" class="axis">startup tokens (log) →   '
                 'right: NOTE minus NONE; a minus sign means the note won</text>'
                 % (left, height - 14))
    dots = (legend([("NOTE — a short handover note was written", "var(--arm-note)"),
                    ("NONE — nothing was written", "var(--arm-none)")])
            + svg(width, height, "Paired startup by arm", "".join(parts)))

    # ---- net-value bars --------------------------------------------------
    priced = [p for p in pairs if p["net_usd"] is not None]
    if not priced:
        bars = ('<p class="axis">No pair has both a recorded <code>note_minutes</code> and a '
                'priced successor, so net value is not computable yet. A blank '
                '<code>note_minutes</code> is UNKNOWN, never 0.</p>')
    else:
        bw, bleft, btop, brow = 880, 200, 40, 24
        bheight = btop + brow * len(priced) + 46
        span = bw - bleft - 90
        peak = max(abs(p["net_usd"]) for p in priced) or 1.0
        zero = bleft + span / 2.0
        scale = (span / 2.0) / peak            # symmetric: a negative bar can never clip
        bparts = ['<text x="14" y="20" class="g-title">Net value per sitting: cost saved '
                  'minus the cost of writing the note</text>']
        bparts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" class="baseline"/>'
                      % (zero, btop - 8, zero, btop + brow * len(priced)))
        bparts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis-strong">'
                      '0 — the note exactly paid for itself</text>' % (zero, btop - 14))
        for index, pair in enumerate(sorted(priced, key=lambda p: -p["net_usd"])):
            y = btop + index * brow
            value = pair["net_usd"]
            w = abs(value) * scale
            bparts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="4" fill="%s" '
                          'data-tip="%s"/>'
                          % (zero if value >= 0 else zero - w, y + 3, max(w, 1.0), brow - 8,
                             "var(--div-pos)" if value >= 0 else "var(--div-neg)",
                             esc("%s · total successor cost saved $%.3f · note cost $%.3f "
                                 "(%.0f min at the stated rate) · net $%+.3f"
                                 % (folder_name(pair["cwd"], "") or pair["thread_id"],
                                    pair["saved_usd"] or 0,
                                    pair["note_cost_usd"] or 0, pair["note_minutes"] or 0, value))))
            bparts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong">%s</text>'
                          % (bleft - 10, y + brow / 2 + 3,
                             esc(folder_name(pair["cwd"], "")[:24]
                                 or pair["thread_id"])))
            bparts.append('<text x="%d" y="%.1f" class="val">$%+.2f</text>'
                          % (bw - 80, y + brow / 2 + 3, value))
        bparts.append('<text x="%d" y="%d" class="axis">%s</text>'
                      % (bleft, bheight - 12, esc(report["rate_note"][:118])))
        bars = (legend([("net positive — the note paid", "var(--div-pos)"),
                        ("net negative — it did not", "var(--div-neg)")])
                + svg(bw, bheight, "Net value per thread", "".join(bparts)))
        if report["net_value"]["pairs_without_minutes"]:
            bars += ('<p class="axis">%d pair(s) are not on this chart: their '
                     '<code>note_minutes</code> is blank, which means unknown, never '
                     '0.</p>'
                     % report["net_value"]["pairs_without_minutes"])

    w, s = report["wilcoxon"], report["sign_test"]
    stats = ('<p class="axis"><strong>Wilcoxon signed-rank</strong> (primary, two-sided '
             'α&nbsp;=&nbsp;0.05): n&nbsp;=&nbsp;%s, W&nbsp;=&nbsp;%s, p&nbsp;=&nbsp;%s — %s.<br>'
             '<strong>Sign test</strong> (robustness, ties dropped): the note won %s of %s, '
             'p&nbsp;=&nbsp;%s.<br>Pairing: %s.</p>'
             % (w["n"], w.get("w"), w.get("p"), esc(w.get("method") or ""),
                s["wins"], s["n"], s.get("p"), esc(report["pair_definition"])))
    return header + banner + dots + bars + stats + mediator


def _mediator_tile(mediator: Dict[str, Any]) -> str:
    """A single stat tile. Below 80 % it carries the caveat INLINE, not in a
    footnote — the whole point is that a reader cannot take the headline
    without it (the mediator rate is a gate, not a finding)."""
    rate = mediator["rate"]
    value = "n/a" if rate is None else "%d/%d" % (mediator["opened_by_turn_2"],
                                                  mediator["note_arm_successors"])
    caveat = ""
    if mediator["below_floor"]:
        caveat = ('<div class="note" style="margin:8px 0 0">Below the %.0f&nbsp;%% floor. '
                  '%s</div>' % (100 * mediator["floor"], esc(mediator["branch"])))
    return ('<div class="cards"><div class="card" style="border-left:5px solid var(--arm-note)">'
            '<div class="metric">%s</div>'
            '<div class="label">note read in the first two turns%s</div>%s</div></div>'
            % (esc(value),
               "" if rate is None else " (%.0f&nbsp;%%, Wilson 90&nbsp;%% CI %s–%s)"
               % (100 * rate, mediator["wilson90"][0], mediator["wilson90"][1]),
               caveat))


# ==========================================================================
# THE EXPLAINER, the decision panels, and the definitions box.
#
# A reader must be able to tell how to use the startup metric, and whether a
# high number is good or bad, before any real number appears. So the report
# opens with a GRAPHIC that teaches the metric on a toy example, and every
# panel after it carries a definition and a direction.
# ==========================================================================

# WHAT THE STARTUP PAGE SHOWS, AND WHAT IT DOESN'T. One box, three bullets a
# side, instead of a stack of callouts. Shared by the HTML and the Markdown.
STARTUP_SHOWS = [
    "Which sessions and work items spent the most before their first edit.",
    "That later sessions touched files an earlier session on the same work had touched.",
    "How much repeat startup came after a long break.",
]
STARTUP_NOT_SHOWN = [
    "That the same thing was re-learned: the same files came back, which is not proof.",
    "That a handoff note would help. That needs the note read, on the right subject, and "
    "cheaper than re-reading; measure it with --handoff-log.",
    "Why a note was skipped: it may be about other work, and then skipping it is right.",
]


def shows_box(shows: Sequence[str], not_shown: Sequence[str]) -> str:
    """The one "what this shows / what it doesn't" box a panel may carry."""
    def items(rows: Sequence[str]) -> str:
        return "<ul>%s</ul>" % "".join("<li>%s</li>" % esc(row) for row in rows)
    return ('<div class="shows"><div><h4>What this shows</h4>%s</div>'
            '<div><h4>What it doesn\'t show</h4>%s</div></div>'
            % (items(shows), items(not_shown)))


NO_THRESHOLD_NOTE = (
    "No chart draws a good/bad line: no outcome-validated threshold exists yet. Read a "
    "share as a position in this history, not a verdict on a session.")


def _band_of(share: Optional[float]) -> str:
    """Which tenth of the distribution a share sits in: a position, not a verdict.

    Words such as "healthy" or "expensive" would claim a threshold that no
    evidence here supports, so the ledger column carries a neutral position.
    """
    if share is None:
        return "no edit"
    return "%d-%d %%" % (int(share * 10) * 10, min(100, int(share * 10) * 10 + 10))


TOY_GREY = "var(--div-mid)"                 # orientation — before the first successful change
TOY_BLUE = "var(--accent)"                  # the work — after it


def _toy_bar(x: float, y: float, width: float, height: float, grey: float,
             tip: str = "") -> str:
    """One toy session drawn as its tokens, with time running left to right.

    The full bar is blue (the work) and the startup segment is grey, drawn
    from the bar's left edge, so grey is both first in time and the colour the
    legend names. A blue startup segment on top of a grey bar would make the
    legend and the drawing disagree. The two rects carry
    `data-seg` so the selftest can assert order and fill without rendering.
    """
    grey_w = width * max(0.0, min(1.0, grey))
    return (
        '<rect data-seg="whole" x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" '
        'fill="%s" stroke="var(--line)" data-tip="%s"/>'
        '<rect data-seg="startup" x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" '
        'fill="%s" stroke="var(--line)" data-tip="%s"/>'
        % (x, y, width, height, TOY_BLUE,
           esc(tip or "the work — after the first edit"),
           x, y, max(2.0, grey_w), height, TOY_GREY,
           esc("startup — before the first edit, %.0f %% of the session" % (100 * grey))))


TOY_LEGEND = legend_rich([
    ("grey — startup: tokens before the first edit (reading, searching)",
     swatch_solid(TOY_GREY)),
    ("blue — the work, after the first edit", swatch_solid(TOY_BLUE)),
])


def panel_explainer(appendix_href: str = "appendix.html") -> str:
    """The metric, taught on toy data, before any real number.

    (a) one session: grey = startup (tokens before the first edit), blue = the
        work. Time runs left to right, so grey comes first.
    (b) one work item, three sessions in a sitting: the first grey is the
        price of new work; the second and third are repeat startup.
    """
    width, height = 900, 196
    bar_w, bar_h = 430, 26
    left = 150
    label_w = width - left - bar_w - 24
    parts = [text_block(14, 20, width - 28,
                        "Startup share is the grey part: tokens a session spends before "
                        "its first edit", cls="g-title")]
    # Toy values: 0.30 and ~0. The sliver is drawn at 0.03 so it is visible
    # at all; the label says "almost none", so nobody reads a drawing
    # tolerance as a number.
    for index, (grey, name) in enumerate(
            ((0.30, "starts cold: 30 % of its tokens come before the first edit"),
             (0.03, "read a handoff note first: almost none before the first edit"))):
        y = 52 + index * 62
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong">%s</text>'
                     % (left - 12, y + bar_h * 0.72,
                        esc("session B" if index else "session A")))
        parts.append(_toy_bar(left, y, bar_w, bar_h, grey))
        # The share label sits ON the grey segment when it is wide enough to
        # hold it, and just above it when it is a sliver -- never over the blue.
        wide = bar_w * grey > 30
        parts.append('<text x="%.1f" y="%.1f" text-anchor="middle" class="val">%d %%</text>'
                     % (left + max(2.0, bar_w * grey) / 2,
                        (y + bar_h * 0.72) if wide else (y - 5), round(100 * grey)))
        parts.append(text_block(left + bar_w + 14, y + 11, label_w, name, cls="axis"))
    parts.append(text_block(14, height - 22, width - 28,
                            "Lower is better once a session edits something. A no-edit "
                            "session has no startup share and is counted on its own.",
                            cls="axis"))
    one = svg(width, height, "What startup share is", "".join(parts))

    # (b) one work item, three sessions, and the repeat startup
    width2, height2 = 900, 268
    bar_w2, bar_h2 = 150, 24
    gap = 22
    left2 = 218
    parts2 = [text_block(14, 20, width2 - 28,
                         "Every later session on the same work pays its startup again",
                         cls="g-title")]
    rows = [("no handoff note", [0.30, 0.30, 0.30], 70),
            ("a handoff note read each time", [0.30, 0.03, 0.03], 166)]
    for label, greys, y in rows:
        parts2.append(text_block(left2 - 12, y + 6, left2 - 26, label,
                                 cls="axis-strong", anchor="end"))
        for index, grey in enumerate(greys):
            x = left2 + index * (bar_w2 + gap)
            parts2.append(_toy_bar(x, y, bar_w2, bar_h2, grey,
                                   "session %d · startup %.0f %%" % (index + 1, 100 * grey)))
            parts2.append('<text x="%.1f" y="%.1f" text-anchor="middle" class="axis">'
                          'session %d</text>' % (x + bar_w2 / 2, y - 6, index + 1))
            parts2.append('<text x="%.1f" y="%.1f" text-anchor="middle" class="%s">%s</text>'
                          % (x + bar_w2 / 2, y + bar_h2 + 14,
                             "axis" if index == 0 else "val",
                             "the price of new work" if index == 0 else "repeat startup"))
    parts2.append(text_block(14, height2 - 36, width2 - 28,
                             "The first grey segment is the price of new work: nothing earlier "
                             "exists to hand off from. Each later one is repeat startup.",
                             cls="axis-strong"))
    two = svg(width2, height2, "Repeat startup", "".join(parts2))
    return (one + TOY_LEGEND + two
            + '<p class="note">Toy data, no real sessions. No chart in this report draws a '
              'good/bad line: no threshold has been validated. What is not shown is '
              '<a href="%s#not-shown">in the appendix</a>.</p>' % esc(appendix_href))


DEFINITIONS = [
    ("startup share",
     "Of every 100 tokens a session cost, how many came before its first edit: reading "
     "and searching.",
     "Lower, once the session edits something.",
     "No threshold. Read it as a position in the distribution."),
    ("startup cost",
     "Those same responses in dollars, each priced at its own model's list API rate.",
     "Lower.",
     "An estimate for comparing sessions, never a bill."),
    ("first edit",
     "The first successful change to a file: an edit, a write, or a file-changing command.",
     "Neither: it is where startup ends.",
     "A refused or failed edit does not count."),
    ("no-edit session",
     "A session that never changed a file: questions, reviews, research.",
     "Neither.",
     "It has no startup share and is counted on its own."),
    ("sitting",
     "Sessions on the same work with no gap longer than 72 hours.",
     "Neither.",
     "72 hours is a setting (--gap-hours)."),
    ("work item",
     "The same folder, branch or ticket, however long between sessions.",
     "Neither.",
     "Each work item says which rule grouped it."),
    ("repeat startup",
     "Startup tokens of every session after the first in its sitting: a per-sitting "
     "amount, not a share.",
     "Lower. The first session's startup is the price of new work.",
     "No band: rank work items by it."),
    ("repeat startup, incl. long breaks",
     "The same, counted across the whole work item, so a long break is counted too.",
     "Lower. Never below repeat startup.",
     "The difference is startup after a long break."),
    ("started with a question",
     "The first request was a question, and the first edit came after a later request.",
     "Neither: something else that could explain startup.",
     "Repeat startup is shown with and without them."),
    ("read a handoff note",
     "In its first two turns the session opened a HANDOFF/STATUS-style note, or the file "
     "its predecessor wrote last. README does not count.",
     "Yes is what a note is meant to cause.",
     "A note nobody reads cannot help."),
    ("files read first",
     "Distinct files touched before the first edit.",
     "Neither on its own.",
     "Many tokens on few files: re-reading. Many files: discovery."),
    ("est. cost",
     "What the tokens would cost at published list API rates for the model that ran them.",
     "For comparing, not billing.",
     "The price table is in the appendix."),
    ("resumed after a pause / after compaction / new session",
     "How work was picked up again: the same session after a pause, the same session "
     "after /compact, or a new session.",
     "Neither: it describes what happened.",
     "A pause is a clock gap; transcripts carry no resume marker."),
    ("pick-up cost",
     "What it cost to get going again, up to the next edit. For a new session, its startup.",
     "Lower, within one kind.",
     "Across kinds it compares situations, not treatments."),
]


def definitions_box() -> str:
    rows = "".join(
        "<tr><td class='l'><strong>%s</strong></td><td class='l'>%s</td><td class='l'>%s</td>"
        "<td class='l'>%s</td></tr>" % (esc(name), esc(sentence), esc(direction), esc(band))
        for name, sentence, direction, band in DEFINITIONS)
    return ("<table><thead><tr><th class='l'>term</th><th class='l'>what it is</th>"
            "<th class='l'>which way is good</th><th class='l'>how to read it</th>"
            "</tr></thead><tbody>%s</tbody></table>" % rows)


def _orientation_dollars(session: "Session") -> float:
    """List-rate cost of this session's pre-boundary responses.

    Directly priced. It used to be `session cost x raw-token share`, which
    prices startup at the session's average rate and therefore mis-ranked
    sessions whose cache/output mix differs before and after the boundary.
    0.0 when no boundary was observed -- those sessions are reported as their
    own category, never as $0 of orientation.
    """
    return session.metrics.get("orientation_cost_usd") or 0.0


def first_in_thread_ids(threads: Sequence["Thread"]) -> set:
    """Session ids that open their thread — the ones with nothing to hand off from."""
    first: set = set()
    for thread in threads:
        ordered = [s for s in thread.sessions if s.start]
        ordered.sort(key=lambda s: s.start)
        if ordered:
            first.add(ordered[0].session_id)
        elif thread.sessions:
            first.add(thread.sessions[0].session_id)
    return first


def panel_money_went(sessions: Sequence["Session"], redact: bool = False,
                     top_n: int = 10, first_ids: Optional[set] = None) -> str:
    """Where the startup money went: the top sessions by startup DOLLARS.

    A ranked list with names, each row a two-segment bar (startup $, then the
    rest of the session) with whether a handoff note was read. Each row says
    "first" or "later": a first session of a sitting had nothing earlier to
    read, so it is in this chart but not in repeat startup.
    """
    first_ids = first_ids or set()
    ranked = sorted(((s, _orientation_dollars(s)) for s in sessions
                     if s.metrics.get("orientation_share") is not None),
                    key=lambda pair: -pair[1])
    if not ranked or ranked[0][1] <= 0:
        return '<p class="axis">No priced session made an edit here.</p>'
    total = sum(value for _, value in ranked) or 1.0
    top = ranked[:top_n]
    share_of_spend = sum(value for _, value in top) / total
    width = 940
    left, right, top_pad, row_h = 300, 150, 66, 30
    pos_x = left + 6                                   # the first/later column
    bar_x = left + 62
    footer = ("These %d sessions are %.0f %% of all startup cost ($%.2f of $%.2f at list "
              "API rates). A later session had an earlier one in its sitting to hand off "
              "from." % (len(top), 100 * share_of_spend, sum(v for _, v in top), total))
    foot_h = text_height(footer, width - 28, "axis")
    height = top_pad + row_h * len(top) + foot_h + 22
    pw = width - bar_x - right
    peak = max((s.metrics.get("est_cost_usd") or 0.0) for s, _ in top) or 1.0
    x = linscale(0, peak, 0, pw)
    parts = [text_block(14, 20, width - 28,
                        "%d sessions hold %.0f %% of the startup cost"
                        % (len(top), 100 * share_of_spend), cls="g-title"),
             text_block(14, 40, width - 28,
                        "dark = startup cost · pale = the rest of the session · "
                        "✎ = read a handoff note · first / later = place in its sitting",
                        cls="axis")]
    for index, (session, dollars) in enumerate(top):
        y = top_pad + index * row_h
        cost = session.metrics.get("est_cost_usd") or 0.0
        title = None if redact else (session.title or session.first_prompt)
        where = stable_hash(session.cwd) if redact else session.cwd_base
        label = "%s · %s" % (where[:16], (title or "(no prompt recorded)")[:34])
        is_first = session.session_id in first_ids
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis" data-tip="%s">%s</text>'
                     % (left - 10, y + row_h * 0.62,
                        esc("%s · %s · %s · %d turns"
                            % (where, title or "(no prompt recorded)",
                               primary_model(session), session.metrics.get("turns") or 0)),
                        esc(label)))
        parts.append('<text x="%d" y="%.1f" class="%s" data-tip="%s">%s</text>'
                     % (pos_x, y + row_h * 0.62, "axis" if is_first else "val",
                        esc("first session of its sitting: nothing earlier to hand off from"
                            if is_first else
                            "a later session: an earlier one in the same sitting came first"),
                        "first" if is_first else "later"))
        parts.append('<rect x="%d" y="%.1f" width="%.1f" height="%d" rx="3" class="s1" '
                     'data-tip="%s"/>'
                     % (bar_x, y + 5, max(1.0, x(dollars)), row_h - 13,
                        esc("startup $%.2f of $%.2f (%.0f %%)"
                            % (dollars, cost, 100 * (session.metrics.get("orientation_share") or 0)))))
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="3" class="s1" '
                     'opacity="0.22" data-tip="%s"/>'
                     % (bar_x + x(dollars) + 2, y + 5, max(1.0, x(cost) - x(dollars) - 2),
                        row_h - 13, esc("the rest of the session: $%.2f"
                                        % max(0.0, cost - dollars))))
        parts.append('<text x="%d" y="%.1f" class="val">$%.2f%s</text>'
                     % (bar_x + pw + 8, y + row_h * 0.62, dollars,
                        "  ✎" if session.metrics.get("handoff_opened_by_turn_2") else ""))
    parts.append(text_block(14, top_pad + row_h * len(top) + 18, width - 28, footer,
                            cls="axis"))
    return svg(width, int(height), "Where the startup money went", "".join(parts))


def panel_tax_definitions(with_table: bool = True) -> str:
    """Work item, sitting and the two repeat-startup figures, drawn before any number.

    The definition is DRAWN, on the same timeline shape the work-items chart
    uses: one row of sessions, two brackets under it. Words alone did not
    explain how one figure forgives what the other counts.
    """
    width, height = 900, 236
    left, right = 40, 40
    pw = width - left - right
    # Five toy sessions: three close together, then a month's gap, then two.
    xs = [0.02, 0.09, 0.15, 0.62, 0.70]
    y = 84
    parts = [text_block(14, 20, width - 28,
                        "One work item, five sessions, and which ones each figure counts",
                        cls="g-title"),
             text_block(14, 40, width - 28,
                        "Each dot is a session. Sessions less than 72 hours apart form one "
                        "sitting; a longer break starts a new one.", cls="axis")]
    px = [left + pw * f for f in xs]
    # the two sittings, drawn as the shaded band the work-items chart uses
    for lo, hi in ((0, 2), (3, 4)):
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="var(--accent)" '
                     'stroke-width="10" opacity="0.16" stroke-linecap="round"/>'
                     % (px[lo] - 6, y, px[hi] + 6, y))
    parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="var(--muted)" '
                 'stroke-width="1" stroke-dasharray="2 3"/>' % (px[2], y, px[3], y))
    parts.append(text_block((px[2] + px[3]) / 2, y - 12, 180,
                            "31-day break: a new sitting", cls="axis", anchor="middle"))
    for index, cx in enumerate(px):
        parts.append('<circle cx="%.1f" cy="%d" r="6" class="s1"/>' % (cx, y))
        parts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis">%d</text>'
                     % (cx, y + 24, index + 1))
    # The LABEL is left-aligned at the chart's margin and wrapped to the full
    # width -- a label centred under a narrow bracket runs off the edge.
    def bracket(y0: int, lo: float, hi: float, colour: str) -> str:
        return ('<path d="M%.1f %d V%d H%.1f V%d" fill="none" stroke="%s" stroke-width="1.5"/>'
                % (lo, y0 - 6, y0, hi, y0 - 6, colour))
    parts.append(bracket(148, px[1] - 8, px[2] + 8, "var(--accent)"))
    parts.append(bracket(148, px[4] - 8, px[4] + 8, "var(--accent)"))
    parts.append(text_block(14, 166, width - 28,
                            "Repeat startup counts sessions 2, 3 and 5. Each sitting starts "
                            "over, so its first session is not counted.", cls="axis"))
    parts.append(bracket(198, px[1] - 8, px[4] + 8, "var(--accent2)"))
    parts.append(text_block(14, 216, width - 28,
                            "Repeat startup, incl. long breaks, counts sessions 2 to 5: "
                            "everything after the work item's first session.", cls="axis"))
    drawing = svg(width, height, "Repeat startup, with and without long breaks",
                  "".join(parts))
    rows = [
        ("work item", "the same folder, branch or ticket, however long between sessions."),
        ("sitting", "sessions on the same work with no gap longer than 72 hours."),
        ("repeat startup", "startup tokens of every session after the first in its sitting."),
        ("… incl. long breaks", "the same, counted across the whole work item."),
        ("the difference", "startup of sessions that came back after a long break."),
    ]
    table = ("<table><thead><tr><th class='l'>term</th><th class='l'>what it is</th></tr>"
             "</thead><tbody>%s</tbody></table>"
             % "".join("<tr><td class='l'><strong>%s</strong></td><td class='l'>%s</td></tr>"
                       % (esc(name), esc(text)) for name, text in rows))
    return drawing + table if with_table else drawing


# The one metric the stream table is ranked by, and the one the summary line
# must name when it points at a stream.
STREAM_RANK_METRIC = "reorientation_tax_tokens"
STREAM_RANK_LABEL = "repeat startup"


def _sum_unmeasured(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [r.get("successors_without_boundary") or {} for r in records]
    return {"sessions": sum(r.get("sessions") or 0 for r in rows),
            "tokens": sum(r.get("tokens") or 0 for r in rows),
            "est_cost_usd": round(sum(r.get("est_cost_usd") or 0.0 for r in rows), 2)}


def no_boundary_line(unmeasured: Optional[Dict[str, Any]]) -> str:
    """The line beside a repeat-startup figure, when later sessions never edited.

    Such a session has no startup share: its startup is unknown, not zero and
    not the whole session, so it is left out of the figure and named here.
    """
    if not unmeasured or not unmeasured.get("sessions"):
        return ""
    return ('<p class="note">Not in the figure: %s, %s tokens, $%.2f. Their startup '
            'is unknown, so it is neither zero nor the whole session.</p>'
            % (_n(unmeasured["sessions"], "later no-edit session"),
               compact_number(unmeasured["tokens"]),
               unmeasured["est_cost_usd"]))


def _handoff_pattern(record: Dict[str, Any]) -> str:
    """What the handoff pattern of a work item IS -- descriptive, never advice.

    A note that exists but is not read every time may be about other work, in
    which case skipping it is right, so the column describes and stops there.
    """
    opened = record["handoff_opened_sessions"]
    if record["sessions"] < 2:
        return "one session so far"
    if opened == 0:
        return "returns never read a handoff note"
    if opened < record["sessions"] - 1:
        return "a note exists but is not read every time"
    return "a handoff note is read on every return"


def _question_first_sentence(agg: Optional[Dict[str, Any]]) -> str:
    """One sentence: what setting aside sessions that started with a question does."""
    if not agg or agg.get("reorientation_tax_tokens_excl_question_first") is None:
        return ""
    excluded = agg["reorientation_tax_tokens_excl_question_first"]
    full = agg.get("reorientation_tax_tokens") or 0
    flagged = agg.get("question_first_sessions") or 0
    if not agg.get("question_first_observable"):
        return ("No session here records its first request, so sessions that started "
                "with a question cannot be set aside.")
    if not flagged:
        return ""
    if full == excluded:
        return ("Setting aside the %s that started with a question changes nothing here."
                % _n(flagged, "session"))
    return ("Without the %s that started with a question, repeat startup is %s tokens "
            "instead of %s." % (_n(flagged, "session"), compact_number(excluded),
                                compact_number(full)))


def panel_stream_tax(stream_records: Sequence[Dict[str, Any]],
                     thread_records: Sequence[Dict[str, Any]], top_n: int = 12,
                     agg: Optional[Dict[str, Any]] = None) -> str:
    """Which work items pay the most repeat startup -- as a bar per work item.

    Each bar is the work item's repeat startup incl. long breaks, split into
    the part inside sittings and the part after a long break. ONE ranking
    metric (STREAM_RANK_METRIC), the same one the five-line summary names.
    Tokens are the unit; the full table, with dollars, is one click away.
    """
    if not stream_records:
        return '<p class="axis">No work item to report.</p>'
    ranked = sorted(stream_records, key=lambda r: -r[STREAM_RANK_METRIC])[:top_n]
    thread_tax = {r["thread_id"]: r["reorientation_tax_tokens"] for r in thread_records}
    rows = []
    for record in ranked:
        inner = min(record[STREAM_RANK_METRIC],
                    sum(thread_tax.get(t, 0) for t in record["threads"]))
        rows.append((record, inner, max(0, record[STREAM_RANK_METRIC] - inner)))
    drawn = [row for row in rows if row[0][STREAM_RANK_METRIC] > 0]
    unmeasured = _sum_unmeasured(ranked)
    note_bits = []
    if unmeasured.get("sessions"):
        note_bits.append("Not counted: %s, %s tokens; their startup is unknown."
                         % (_n(unmeasured["sessions"], "later no-edit session"),
                            compact_number(unmeasured["tokens"])))
    question = _question_first_sentence(agg)
    if question:
        note_bits.append(question)
    note = ('<p class="note">%s</p>' % esc(" ".join(note_bits))) if note_bits else ""
    if not drawn:
        chart = '<p class="axis">No work item has a later session that made an edit.</p>'
    else:
        width, left, right, top, row_h, bar_h = 900, 230, 70, 46, 26, 14
        pw = width - left - right
        peak = max(row[0][STREAM_RANK_METRIC] for row in drawn) or 1
        x = linscale(0, peak, 0, pw)
        top_row = drawn[0][0]
        title = ("%s carries the most repeat startup: %s tokens"
                 % (top_row["name"], compact_number(top_row[STREAM_RANK_METRIC])))
        height = top + row_h * len(drawn) + 10
        parts = [text_block(14, 20, width - 28, title, cls="g-title")]
        for index, (record, inner, after) in enumerate(drawn):
            y = top + index * row_h
            parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong" '
                         'data-tip="%s">%s</text>'
                         % (left - 10, y + bar_h * 0.85,
                            esc("%s · %d sessions in %d sitting(s) · %s"
                                % (record["name"], record["sessions"], record["episodes"],
                                   _handoff_pattern(record))),
                            esc(str(record["name"])[:30])))
            cursor = left
            for value, fill, what in ((inner, "var(--accent)", "inside sittings"),
                                      (after, "var(--accent2)", "after a long break")):
                if value <= 0:
                    continue
                w = max(x(value) - 2, 1.0)
                parts.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" rx="3" fill="%s" '
                             'data-tip="%s"/>'
                             % (cursor, y, w, bar_h, fill,
                                esc("%s · %s: %s tokens" % (record["name"], what,
                                                            compact_number(value)))))
                cursor += w + 2                      # 2px surface gap between segments
            parts.append('<text x="%.1f" y="%.1f" class="val">%s</text>'
                         % (cursor + 6, y + bar_h * 0.85,
                            esc(compact_number(record[STREAM_RANK_METRIC]))))
        chart = (svg(width, int(height), title, "".join(parts))
                 + legend([("repeat startup inside sittings", "var(--accent)"),
                           ("after a long break (over 72 hours)", "var(--accent2)")]))
    body = "".join(
        "<tr><td class='l'>%s</td><td class='l'>%s</td><td>%d</td><td>%d</td>"
        "<td class='l'>%s</td><td data-v='%d'>%s</td><td data-v='%d'>%s</td>"
        "<td data-v='%d'>%s</td><td>$%.2f</td><td class='l'>%s</td></tr>"
        % (esc(record["name"]), esc(record["named_by"]), record["episodes"],
           record["sessions"],
           esc(", ".join("%.0f" % g for g in record["gaps_days"][:4]) or "—"),
           record[STREAM_RANK_METRIC], compact_number(record[STREAM_RANK_METRIC]),
           inner, compact_number(inner), after, compact_number(after),
           record.get("reorientation_tax_usd") or 0.0, esc(_handoff_pattern(record)))
        for record, inner, after in rows)
    table = ("<details><summary>All %d work items as a table, with dollars</summary>"
             "<table class='sortable'><thead><tr><th class='l'>work item</th>"
             "<th class='l'>grouped by</th><th>sittings</th><th>sessions</th>"
             "<th class='l'>breaks (days)</th><th>repeat startup, incl. long breaks</th>"
             "<th>inside sittings</th><th>after long breaks</th><th>est. $</th>"
             "<th class='l'>handoff notes</th></tr></thead><tbody>%s</tbody></table>"
             "</details>" % (len(rows), body))
    return chart + note + table


def five_line_summary(agg: Dict[str, Any], threads: Sequence[Dict[str, Any]],
                      scope: Dict[str, Any],
                      streams: Sequence[Dict[str, Any]] = ()) -> str:
    """Five lines, in the words a developer would use.

    Five lines or fewer, readable cold. No metric name appears without what it
    measures, no dollar without "list API rates", and the last line names what
    the number depends on rather than giving an instruction.
    """
    med = agg.get("median_orientation_share")
    # The SAME metric the work-item ranking uses, and named as such.
    worst_stream = max(streams, key=lambda r: r[STREAM_RANK_METRIC], default=None)
    worst_thread = max(threads, key=lambda r: r["reorientation_tax_tokens"], default=None)
    with_boundary = agg.get("sessions_with_boundary") or 0
    no_edit = agg.get("sessions_no_mutation") or 0
    biggest = (worst_stream["name"] if worst_stream and worst_stream.get(STREAM_RANK_METRIC)
               else folder_name((worst_thread or {}).get("cwd"), "") or None)
    lines = [
        "%d sessions, %s tokens, about $%.2f at list API rates (an estimate, not a bill)."
        % (agg["sessions"], compact_number(agg["tokens_total"]), agg["est_cost_usd_total"]),
        "%s made an edit; half of them spent more than %s of their tokens before it. %s"
        % (_n(with_boundary, "session"),
           ("%.0f %%" % (100 * med)) if med is not None else "n/a",
           "1 no-edit session has no startup share." if no_edit == 1 else
           "%d no-edit sessions have no startup share." % no_edit),
        "Startup cost about $%.2f%s, each response priced at its own model's rates."
        % (agg.get("orientation_cost_usd_total") or 0.0,
           (" (%.0f %% of the total)" % (100 * agg["orientation_cost_share"]))
           if agg.get("orientation_cost_share") else ""),
        "%s came back in more than one sitting; %s: %s tokens, %s with long "
        "breaks. The same files came back, which is not proof of re-learning."
        % (_n(agg.get("multi_episode_streams") or 0, "work item"), STREAM_RANK_LABEL,
           compact_number(agg["reorientation_tax_tokens"]),
           compact_number(agg.get("stream_tax_tokens") or agg["reorientation_tax_tokens"])),
        "%s. Whether a handoff note would shrink it depends on that startup being "
        "re-reading, which is not established here."
        % (("Most repeat startup is in %s" % biggest) if biggest
           else "No work item has repeat startup yet"),
    ]
    return "\n".join(lines)


def cohort_line(scope: Dict[str, Any]) -> str:
    """Which sessions are included -- one line, with the exclusions named."""
    cohort = scope.get("cohort") or {}
    rows = cohort.get("excluded_by_pattern") or []
    if not rows:
        return ("Included: %s session(s); no folder filter removed anything."
                % compact_number(cohort.get("included_sessions") or 0))
    parts = ", ".join("`%s` −%d session(s) / %s tokens / $%.2f"
                      % (r["pattern"], r["sessions"], compact_number(r["tokens"]),
                         r["est_cost_usd"])
                      for r in rows)
    return ("Included: %s session(s). Removed by the folder filter: %s."
            % (compact_number(cohort.get("included_sessions") or 0), parts))


def cohort_table(scope: Dict[str, Any]) -> str:
    """The same breakdown as an HTML table: what each folder filter removed."""
    cohort = scope.get("cohort") or {}
    rows = cohort.get("excluded_by_pattern") or []
    body = "".join("<tr><td class='l'><code>%s</code></td><td>%d</td><td data-v='%d'>%s</td>"
                   "<td>$%.2f</td></tr>"
                   % (esc(r["pattern"]), r["sessions"], r["tokens"],
                      compact_number(r["tokens"]), r["est_cost_usd"])
                   for r in rows)
    body += ("<tr><td class='l'><strong>included</strong></td><td>%d</td><td>—</td>"
             "<td>—</td></tr>" % (cohort.get("included_sessions") or 0))
    return ("<table class='sortable'><thead><tr><th class='l'>folder filter</th>"
            "<th>sessions</th><th>tokens</th><th>est. $</th></tr></thead>"
            "<tbody>%s</tbody></table>" % body)


def unpriced_note(scope: Dict[str, Any]) -> str:
    """The sessions that record no token counts, named beside the coverage figure."""
    n = scope.get("sessions_without_token_counts") or 0
    if not n:
        return ""
    return ("%s no token counts, so %s unpriced: no est. cost, and left out of every "
            "median and total." % ("1 session records" if n == 1 else "%d sessions record" % n,
                                   "it is" if n == 1 else "they are"))


def scope_block(scope: Dict[str, Any]) -> List[str]:
    return [
        "Window: %s → %s (UTC; --until is exclusive)."
        % (scope.get("since") or "beginning", scope.get("until") or "now"),
        "Stores: %s.%s" % (", ".join(scope["stores"]),
                           " Roots: %s." % "; ".join(scope["roots"]) if scope["roots"] else ""),
        "Folder filters: include %s / exclude %s." % (scope.get("include_cwd") or "—",
                                                      scope.get("exclude_cwd") or "—"),
        cohort_line(scope),
        "Unique responses %s; duplicate rows removed %s (deduplicated on "
        "(sessionId, message.id))."
        % (compact_number(scope["events"]), compact_number(scope["duplicates"])),
        ("Prices: %s, %s of tokens. Every dollar is an estimate at list API rates, never "
         "a bill. %s" % (scope["price_table"], scope.get("price_coverage") or "n/a",
                         unpriced_note(scope))).strip(),
        "Work-item rule (%s): %s" % (scope.get("stream_by") or "auto",
                                     scope.get("stream_rule") or "not built"),
        "Effort is a recorded field on each assistant record (CLI >= 2.1.212); older "
        "versions omit it, and thinking volume is then the only stand-in.",
        "Meter readings are dated by session start. Tool-schema changes and server-side "
        "cache keys cannot be seen locally.",
        "Redaction: %s." % ("on: folders hashed, paths cut to file names, prompts and tool "
                            "arguments dropped" if scope["redact"] else
                            "off (default): this report shows your real paths, titles, first "
                            "prompt lines and tool arguments"),
    ]


def _appendix_calibration(scope: Dict[str, Any], stream_rule: Dict[str, Any]) -> str:
    """The thread and stream rules, and how much they have been calibrated.

    Appendix, because methodology-soundness charts belong there —
    but it is not optional: no thread-level number in this report may be read
    without it, so the panel is linked from the threads section.
    """
    rule = scope.get("thread_rule") or {}
    rows = "".join(
        "<tr><td class='l'>%s</td><td class='l'>%s</td></tr>" % (esc(k), esc(v))
        for k, v in [
            ("sitting rule", "same folder, starts within %s of the sitting's last "
                             "session, and shares at least %d file names; at most %d sessions"
                            % (hours_text(rule.get("gap_hours")), rule.get("min_shared"),
                               rule.get("max_sessions"))),
            ("where 72 hours comes from", "query-log session detection: right about sittings, "
                                      "wrong about work, which is why work items exist."),
            ("work-item rule", (stream_rule or {}).get("rule_note") or "—"),
            ("work items found", "%s from %s sittings (%s recur)"
                              % ((stream_rule or {}).get("streams"),
                                 (stream_rule or {}).get("threads_in"),
                                 (stream_rule or {}).get("multi_episode_streams"))),
            ("what each rule joined", ", ".join("%s %d" % (k, v) for k, v in
                                                ((stream_rule or {}).get("merges_by_rule") or {}).items())
                                      or "nothing joined"),
            ("measured precision", "not measured on this machine; every sitting and "
                                   "work-item number carries that until it is calibrated."),
            ("how to calibrate it", "threads --label-candidates 25 > labels.tsv, hand-label, "
                                    "then threads --labels labels.tsv"),
        ])
    return ("<table><tbody>%s</tbody></table>"
            "<p class=\"note\">If one rule joins too much (a generic <code>#1</code> in a "
            "prompt will), turn that token off with <code>--stream-ignore-token</code>.</p>"
            % rows)


def _appendix_dedupe(scope: Dict[str, Any]) -> str:
    return ("<table><tbody>%s</tbody></table>" % "".join(
        "<tr><td class='l'>%s</td><td class='l'>%s</td></tr>" % (esc(k), esc(v))
        for k, v in [
            ("unique assistant turns", compact_number(scope.get("events") or 0)),
            ("duplicate rows removed", compact_number(scope.get("duplicates") or 0)),
            ("the rule", "one API response is one turn, however many JSONL rows carry it; "
                         "rows are deduplicated on (sessionId, message.id) and their "
                         "content blocks merged."),
            ("why it matters", "counting rows would multiply the ledger by the number of "
                               "content blocks per response, and that factor varies from "
                               "response to response — so it does not cancel in a ratio."),
            ("reference", "references/log-schema.md"),
        ]))


def _appendix_pricing(scope: Dict[str, Any]) -> str:
    rows = "".join(
        "<tr><td class='l'>%s</td><td>%.2f</td><td>%.2f</td></tr>" % (esc(model), rate[0], rate[1])
        for model, rate in price_rows(_ACTIVE_PRICES))
    return ("<p class=\"axis\">Every dollar here is an <strong>estimate at list API "
            "rates</strong>, for comparing sessions, never a bill. Table in use: "
            "<code>%s</code>; %s of tokens. %s</p>"
            "<table><thead><tr><th class='l'>model</th><th>$ / M input</th>"
            "<th>$ / M output</th></tr></thead><tbody>%s</tbody></table>"
            "<p class=\"note\">Cache reads cost %.2f× input, 5-minute cache writes "
            "%.2f× and 1-hour writes %.2f×. An unpriced model lowers coverage and is never "
            "charged at another model's rate. Copilot stores record AI credits, usually not "
            "tokens, so their sessions carry little or no est. cost — see "
            "<code>copilot_sessions</code> in "
            "<code>references/pricing-public-example.json</code>.</p>"
            % (esc(scope.get("price_table")), esc(scope.get("price_coverage") or "n/a"),
               esc(unpriced_note(scope)), rows,
               CACHE_READ_MULT, CACHE_WRITE_5M_MULT, CACHE_WRITE_1H_MULT))


# ==========================================================================
# THE REPORT IS A SET OF LINKED PAGES, NOT ONE DOCUMENT: a home page with the
# analysis index, and each analysis on its own page linked from it, added as
# the agent runs it.
#
# So `report` writes index.html (home + the analysis index), orientation.html
# (the explainer and the two decisions), definitions.html, ledger.html and
# appendix.html. Each file is still
# SELF-CONTAINED -- inline CSS, inline JS, inline SVG, no external asset --
# and the links between them are relative, so the whole directory opens
# from file:// with no network.
# ==========================================================================
PAGE_INDEX = "index.html"
PAGE_ORIENT = "orientation.html"
PAGE_DEFS = "definitions.html"
PAGE_LEDGER = "ledger.html"
PAGE_APPENDIX = "appendix.html"
# Written only when the history actually contains a stream that was
# both restarted and resumed; otherwise the analysis index says so and the page
# is not written, because an empty comparison page reads as a null result.
PAGE_CARRY = "carry-vs-restart.html"


# ==========================================================================
# CARRY-OR-RESTART PANELS.
# Every panel here follows visual-report-spec.md's five rules and four more:
# the denominator is printed in words, no value is coloured
# normatively (no threshold here has been validated against an outcome),
# money is summed per response and never allocated, and every boundary is the
# MAIN conversation's.
# ==========================================================================
def panel_carry_definitions(gap_hours: float, with_table: bool = True) -> str:
    """The three ways work is picked up, DRAWN on one work item before any number.

    Same shape as `panel_tax_definitions`: the reader has to be able to point
    at the thing each name means before a chart uses the name. The machine
    values (R0/R1/R2) stay in the ledger and the JSON; a reader never sees them.
    """
    width, height = 900, 300
    left, right = 40, 40
    pw = width - left - right
    xs = [0.02, 0.10, 0.24, 0.62, 0.72]
    y = 96
    parts = [text_block(14, 20, width - 28,
                        "One work item, two sessions, and the three ways it was picked up",
                        cls="g-title"),
             text_block(14, 40, width - 28,
                        "Each dot is a response. The shaded bars are the two sessions.",
                        cls="axis")]
    px = [left + pw * f for f in xs]
    for lo, hi in ((0, 2), (3, 4)):
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="var(--accent)" '
                     'stroke-width="12" opacity="0.14" stroke-linecap="round"/>'
                     % (px[lo] - 8, y, px[hi] + 8, y))
    parts.append(text_block(px[0] - 8, y - 26, 170, "session A", cls="axis-strong"))
    parts.append(text_block(px[3] - 8, y - 26, 170, "session B", cls="axis-strong"))
    for cx in px:
        parts.append('<circle cx="%.1f" cy="%d" r="6" class="s1"/>' % (cx, y))

    def gap_mark(a: float, b: float, label: str, colour: str, yy: int) -> str:
        """The pause, dashed on the timeline, bracketed below and NAMED: three
        unlabelled brackets over one row of dots is a puzzle."""
        return ('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1.5" '
                'stroke-dasharray="3 3"/>'
                '<path d="M%.1f %d V%d H%.1f V%d" fill="none" stroke="%s" stroke-width="1.5"/>'
                '<text x="%.1f" y="%d" text-anchor="end" class="axis-strong">%s</text>'
                % (a, y, b, y, colour, a, yy - 8, yy, b, yy - 8, colour,
                   a - 8, yy + 4, esc(label)))
    parts.append(gap_mark(px[0] + 8, px[1] - 8, "1", "var(--accent2)", 140))
    parts.append(text_block(14, 158, width - 28,
                            "1 · Resumed after a pause of at least %s. Nothing was thrown "
                            "away; the next request reads the conversation from the cache or "
                            "writes it again." % hours_text(gap_hours), cls="axis"))
    parts.append(gap_mark(px[1] + 8, px[2] - 8, "2", "var(--c3)", 198))
    parts.append('<text x="%.1f" y="%d" text-anchor="middle" class="axis-strong">/compact</text>'
                 % ((px[1] + px[2]) / 2, y - 12))
    parts.append(text_block(14, 216, width - 28,
                            "2 · Resumed after compaction: a summary replaced most of the "
                            "conversation, and the work goes on from the summary.", cls="axis"))
    parts.append(gap_mark(px[2] + 8, px[3] - 8, "3", "var(--accent)", 256))
    parts.append(text_block(14, 274, width - 28,
                            "3 · New session: nothing is carried; it pays its own startup "
                            "before its first edit.", cls="axis"))
    drawing = svg(width, height, "Three ways work is picked up", "".join(parts))
    note = ('<p class="note">Transcripts carry no resume marker, so a pause is a clock gap '
            'of at least %s between two responses.</p>' % hours_text(gap_hours))
    if not with_table:
        return drawing + note
    rows = [
        ("resumed after a pause",
         "the same session, after a gap of at least %s between two responses."
         % hours_text(gap_hours)),
        ("resumed after compaction",
         "the same session, past a /compact, with or without a clock gap."),
        ("new session",
         "a later session of the same work item. Its first session continues nothing."),
        ("pick-up cost",
         "the responses from the restart up to, not including, the next edit."),
        ("not in the cost",
         "the summarising call of a /compact: it has no usage record, so its price is "
         "modelled and shown on its own."),
    ]
    table = ("<table><thead><tr><th class='l'>term</th><th class='l'>what it is</th></tr>"
             "</thead><tbody>%s</tbody></table>"
             % "".join("<tr><td class='l'><strong>%s</strong></td><td class='l'>%s</td></tr>"
                       % (esc(name), esc(text)) for name, text in rows))
    return drawing + table + note


# One colour per way of picking work up, the same on every chart of the page:
# categorical slots 1-3 (validated all-pairs, light and dark). Aqua sits below
# 3:1 on the light surface, so every chart here has a legend and a table view.
KIND_COLOUR = {"R2": "var(--accent)", "R0": "var(--accent2)", "R1": "var(--c3)"}
KIND_ORDER = ("R0", "R1", "R2")


def _kind_legend() -> str:
    return legend([(CONTINUATION_LABEL[k], KIND_COLOUR[k]) for k in KIND_ORDER])


def panel_carry_by_kind(summary: Dict[str, Any], continuations: Dict[str, Any],
                        streams: Sequence[Dict[str, Any]], redact: bool,
                        top_n: int = 14) -> str:
    """What each way of picking up cost: one bar per kind, the median pick-up in
    tokens, with n and the median dollars at the tip. The full table is one
    click away."""
    totals = {k: summary[k] for k in KIND_ORDER}
    drawn = [k for k in KIND_ORDER if totals[k].get("median_tokens") is not None]
    unknown = continuations["restarts_without_boundary"]
    parallel = continuations.get("parallel_sessions") or 0
    events = continuations["events"]
    bits = ["%s across %s."
            % (_n(len(events), "pick-up"), _n(continuations["streams_with_events"], "work item"))]
    left_out = []
    if unknown["sessions"]:
        left_out.append("%s that never made an edit" % _n(unknown["sessions"], "new session"))
    if parallel:
        left_out.append("%s that ran in parallel with an earlier one" % _n(parallel, "session"))
    if left_out:
        bits.append("Not counted: %s." % " and ".join(left_out))
    note = '<p class="note">%s</p>' % esc(" ".join(bits))
    if not drawn:
        chart = '<p class="axis">No pick-up has a measured cost here.</p>'
    else:
        width, left, right, top, row_h, bar_h = 900, 210, 230, 46, 34, 16
        pw = width - left - right
        peak = max(totals[k]["median_tokens"] for k in drawn) or 1
        x = linscale(0, peak, 0, pw)
        dearest = max(drawn, key=lambda k: totals[k]["median_tokens"])
        title = ("%s costs the most to get going: median %s tokens"
                 % (CONTINUATION_LABEL[dearest].capitalize(),
                    compact_number(totals[dearest]["median_tokens"])))
        parts = [text_block(14, 20, width - 28, title, cls="g-title")]
        for index, kind in enumerate(drawn):
            row = totals[kind]
            y = top + index * row_h
            parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong">%s</text>'
                         % (left - 10, y + bar_h * 0.85, esc(CONTINUATION_LABEL[kind])))
            w = max(x(row["median_tokens"]), 1.0)
            parts.append('<rect x="%d" y="%d" width="%.1f" height="%d" rx="4" fill="%s" '
                         'data-tip="%s"/>'
                         % (left, y, w, bar_h, KIND_COLOUR[kind],
                            esc("%s · median %s tokens · median gap %s · n=%d"
                                % (CONTINUATION_LABEL[kind], compact_number(row["median_tokens"]),
                                   hours_text(row["median_gap_hours"]), row["measured"]))))
            parts.append('<text x="%.1f" y="%.1f" class="val">%s</text>'
                         % (left + w + 8, y + bar_h * 0.85,
                            esc("%s · $%.2f · n=%d"
                                % (compact_number(row["median_tokens"]),
                                   row["median_cost_usd"] or 0.0, row["measured"]))))
        chart = svg(width, top + row_h * len(drawn) + 4, title, "".join(parts))
    body = "".join(
        "<tr><td class='l'>%s</td><td>%d</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td>"
        "<td>%s</td><td>%d</td></tr>"
        % (esc(CONTINUATION_LABEL[k]), totals[k]["events"], totals[k]["measured"],
           compact_number(totals[k]["median_tokens"])
           if totals[k]["median_tokens"] is not None else "—",
           ("%.2f" % totals[k]["median_cost_usd"])
           if totals[k]["median_cost_usd"] is not None else "—",
           ("%.1f" % totals[k]["median_gap_hours"])
           if totals[k]["median_gap_hours"] is not None else "—",
           compact_number(totals[k]["total_tokens"]), totals[k]["empty_windows"])
        for k in KIND_ORDER)
    table = ("<details><summary>The numbers, as a table</summary><table><thead><tr>"
             "<th class='l'>pick-up</th><th>pick-ups</th><th>priced</th><th>median tokens</th>"
             "<th>median est. $</th><th>median gap (h)</th><th>total tokens</th>"
             "<th>next response was the edit</th></tr></thead><tbody>%s</tbody></table>"
             "<p class='axis'>Rule: %s When the very next response is the edit itself, "
             "the measured cost is 0 by definition; that response's own size is in "
             "<code>orientation.json</code>.</p></details>"
             % (body, esc(continuations["rule"])))
    return chart + _kind_legend() + note + table


def panel_carry_per_item(continuations: Dict[str, Any], streams: Sequence[Dict[str, Any]],
                         top_n: int = 14) -> str:
    """One row per work item, one dot per way it was picked up (the median pick-up
    tokens), on a log scale. Replaces a long table; the table stays one click away."""
    names = {r["stream_id"]: r["name"] for r in streams}
    per_stream: Dict[str, Dict[str, List[Dict[str, Any]]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for event in continuations["events"]:
        if event.get("stream_id") and event.get("carry_tokens") is not None:
            per_stream[event["stream_id"]][event["kind"]].append(event)
    ranked = sorted(per_stream.items(),
                    key=lambda kv: -sum(e.get("carry_tokens") or 0
                                        for rows in kv[1].values() for e in rows))[:top_n]
    if not ranked:
        return '<p class="axis">No work item has a priced pick-up.</p>'
    cells = []
    for stream_id, by_kind in ranked:
        for kind in KIND_ORDER:
            rows = by_kind.get(kind) or []
            if rows:
                cells.append((stream_id, kind, len(rows),
                              median_or_none([e["carry_tokens"] for e in rows]),
                              median_or_none([e.get("carry_cost_usd") or 0.0 for e in rows]),
                              median_or_none([e["gap_hours"] for e in rows
                                              if e.get("gap_hours") is not None])))
    values = [max(c[3], 1) for c in cells]
    lo, hi = min(values), max(values)
    if hi <= lo:
        hi = lo * 10
    width, left, right, top, row_h = 900, 230, 30, 52, 26
    x = logscale(lo / 1.5, hi * 1.5, left, width - right)
    biggest = ranked[0]
    title = ("%s costs the most to pick up, across %s"
             % (names.get(biggest[0]) or biggest[0],
                _n(sum(len(r) for r in biggest[1].values()), "pick-up")))
    height = top + row_h * len(ranked) + 34
    parts = [text_block(14, 20, width - 28, title, cls="g-title")]
    base = top + row_h * len(ranked) - 6
    for value in (lo, (lo * hi) ** 0.5, hi):
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" class="grid"/>'
                     % (x(value), top - 10, x(value), base))
        parts.append('<text x="%.1f" y="%.1f" class="axis" text-anchor="middle">%s</text>'
                     % (x(value), base + 16, esc(compact_number(value))))
    parts.append('<text x="%d" y="%.1f" class="axis">median pick-up tokens (log scale) →</text>'
                 % (left, base + 30))
    row_of = {stream_id: index for index, (stream_id, _) in enumerate(ranked)}
    for stream_id, _ in ranked:
        y = top + row_of[stream_id] * row_h
        parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="grid"/>'
                     % (left, y, width - right, y))
        parts.append('<text x="%d" y="%d" text-anchor="end" class="axis-strong">%s</text>'
                     % (left - 10, y + 4, esc(str(names.get(stream_id) or stream_id)[:30])))
    for stream_id, kind, n, tokens, cost, gap in cells:
        y = top + row_of[stream_id] * row_h
        # 2px surface ring so two dots that meet stay two dots
        parts.append('<circle cx="%.1f" cy="%d" r="7" fill="var(--card)"/>'
                     '<circle cx="%.1f" cy="%d" r="5" fill="%s" data-tip="%s"/>'
                     % (x(max(tokens, 1)), y, x(max(tokens, 1)), y, KIND_COLOUR[kind],
                        esc("%s · %s · median %s tokens, $%.2f · gap %s · n=%d"
                            % (names.get(stream_id) or stream_id, CONTINUATION_LABEL[kind],
                               compact_number(tokens), cost or 0.0, hours_text(gap), n))))
    chart = svg(width, int(height), title, "".join(parts))
    body = "".join(
        "<tr><td class='l'>%s</td><td class='l'>%s</td><td>%d</td><td>%s</td><td>%s</td>"
        "<td>%s</td></tr>"
        % (esc(names.get(stream_id) or stream_id), esc(CONTINUATION_LABEL[kind]), n,
           compact_number(tokens), ("%.2f" % cost) if cost is not None else "—",
           ("%.1f" % gap) if gap is not None else "—")
        for stream_id, kind, n, tokens, cost, gap in cells)
    table = ("<details><summary>All %d rows as a table</summary>"
             "<table class='sortable'><thead><tr><th class='l'>work item</th>"
             "<th class='l'>pick-up</th><th>pick-ups</th><th>median tokens</th>"
             "<th>median est. $</th><th>median gap (h)</th></tr></thead><tbody>%s</tbody>"
             "</table></details>" % (len(cells), body))
    return chart + _kind_legend() + table


def panel_carry_paired(continuations: Dict[str, Any], streams: Sequence[Dict[str, Any]],
                       redact: bool) -> str:
    """Work items that did BOTH -- a new session and a resume.

    Only these rows compare like with like: the same work, the same person,
    the same repository, picked up two ways. Everything else on the page is
    descriptive.
    """
    paired = continuations["paired_streams"]
    names = {r["stream_id"]: r["name"] for r in streams}
    by_stream: Dict[str, Dict[str, List[Dict[str, Any]]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    # A pick-up whose next response WAS the edit reads 0 by definition: the
    # context it paid for sits inside that excluded response. On a log axis
    # beside real values a 0 would say the opposite, so it is left out here.
    dropped = 0
    for event in continuations["events"]:
        if event.get("stream_id") in paired and event.get("carry_tokens") is not None:
            if event.get("empty_window"):
                dropped += 1
                continue
            by_stream[event["stream_id"]][event["kind"]].append(event)
    rows = []
    for stream_id in paired:
        by_kind = by_stream.get(stream_id) or {}
        restart = median_or_none([e["carry_tokens"] for e in by_kind.get("R2", [])])
        resume = median_or_none([e["carry_tokens"]
                                 for e in (by_kind.get("R0", []) + by_kind.get("R1", []))])
        if restart is None or resume is None:
            continue
        rows.append((stream_id, names.get(stream_id) or "—", restart, resume,
                     len(by_kind.get("R2", [])),
                     len(by_kind.get("R0", [])) + len(by_kind.get("R1", []))))
    if not rows:
        return ('<p class="note">No work item here has both a new session with a measured '
                'startup and a resume, so nothing compares the two.</p>')
    rows.sort(key=lambda r: -max(r[2], r[3]))
    cheaper = sum(1 for r in rows if r[3] < r[2])
    width, row_h, top = 880, 26, 52
    height = top + row_h * len(rows) + 30
    left, right = 232, 90
    lo = max(1.0, min(min(r[2], r[3]) for r in rows))
    hi = max(max(r[2], r[3]) for r in rows)
    x = logscale(lo, hi, left, width - right)
    title = ("Resuming was cheaper in %d of %s that did both"
             % (cheaper, _n(len(rows), "work item")))
    parts = [text_block(14, 20, width - 28, title, cls="g-title"),
             text_block(14, 38, width - 28,
                        "Median tokens to get going, log scale. Right: resume ÷ new session.",
                        cls="axis")]
    for index, (stream_id, name, restart, resume, n2, nr) in enumerate(rows):
        y = top + 12 + row_h * index
        parts.append(text_block(14, y + 4, left - 26, name, cls="axis-strong",
                                tip="%s — %d new session(s), %d resume(s)" % (name, n2, nr)))
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--line)" '
                     'stroke-width="2"/>' % (x(restart), y, x(resume), y))
        for cx, colour, tip in ((x(restart), KIND_COLOUR["R2"],
                                 "new session: %s tokens, n=%d" % (compact_number(restart), n2)),
                                (x(resume), KIND_COLOUR["R0"],
                                 "resumed: %s tokens, n=%d" % (compact_number(resume), nr))):
            parts.append('<circle cx="%.1f" cy="%.1f" r="7" fill="var(--card)"/>'
                         '<circle cx="%.1f" cy="%.1f" r="5" fill="%s" data-tip="%s"/>'
                         % (cx, y, cx, y, colour, esc(tip)))
        parts.append('<text x="%.1f" y="%.1f" class="val">%s</text>'
                     % (width - right + 8, y + 4,
                        esc("%.2fx" % (resume / restart)) if restart else "—"))
    base = top + 12 + row_h * len(rows) + 2
    parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="baseline"/>'
                 % (left, base, width - right, base))
    for value in (lo, (lo * hi) ** 0.5, hi):
        parts.append('<text x="%.1f" y="%.1f" class="axis" text-anchor="middle">%s</text>'
                     % (x(value), base + 16, esc(compact_number(value))))
    drawing = svg(width, height + 12, title, "".join(parts))
    note = ("%d of %d work items with a pick-up did both and have a cost for each%s. "
            "The person chose the route each time, so this describes what happened; it is "
            "not a fair test."
            % (len(rows), continuations["streams_with_events"],
               (" (%s whose next response was the edit itself %s left out)"
                % (_n(dropped, "pick-up"), "is" if dropped == 1 else "are")) if dropped else ""))
    return (drawing
            + legend([("new session — tokens before its first edit", KIND_COLOUR["R2"]),
                      ("resumed — tokens to get going again", KIND_COLOUR["R0"])])
            + '<p class="note">%s</p>' % esc(note))


def panel_carry_strata(continuations: Dict[str, Any]) -> str:
    """Model x effort: both change cost, so a group that mixes them is not one comparison."""
    cells: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = collections.defaultdict(list)
    for event in continuations["events"]:
        if event.get("carry_tokens") is None:
            continue
        model = (event.get("model") or "unrecorded")
        if not audit_common.is_priceable(model):
            continue
        cells[(model, event.get("effort") or "unrecorded", event["kind"])].append(event)
    keys = sorted({(m, e) for m, e, _ in cells})
    rows = ""
    for model, effort in keys:
        cells_by_kind = [(kind, cells.get((model, effort, kind)) or []) for kind in KIND_ORDER]
        if not any(rows_ for _, rows_ in cells_by_kind):
            continue
        cell_html = ""
        for kind, rows_ in cells_by_kind:
            if not rows_:
                cell_html += "<td>—</td>"
                continue
            median = median_or_none([r["carry_tokens"] for r in rows_])
            cell_html += ("<td data-v='%d'>%s<div class='st'>n=%d</div></td>"
                          % (int(median or 0), compact_number(median), len(rows_)))
        rows += ("<tr><td class='l'>%s</td><td class='l'>%s</td>%s</tr>"
                 % (esc(model), esc(effort), cell_html))
    table = ("<table class='sortable'><thead><tr><th class='l'>model</th>"
             "<th class='l'>recorded effort</th>%s</tr></thead><tbody>%s</tbody></table>"
             % ("".join("<th>%s</th>" % esc(CONTINUATION_LABEL[k]) for k in KIND_ORDER),
                rows or "<tr><td class='l' colspan='5'>no priced pick-up</td></tr>"))
    return (table
            + '<p class="note">Median pick-up tokens, with n under each. "unrecorded" effort '
              'means the CLI wrote none; it is never guessed.</p>')


def carry_shows_box(continuations: Dict[str, Any], gap_hours: float) -> str:
    """What the pick-up page shows, and what it can't -- one box."""
    r0 = [e for e in continuations["events"] if e["kind"] == "R0"]
    reads = sum(1 for e in r0 if e.get("prefix_mode") == "cache-read")
    writes = sum(1 for e in r0 if e.get("prefix_mode") == "cache-write")
    return shows_box(
        ["What each way of picking up cost, in tokens and est. $.",
         "Which work items did both, and which way was cheaper there.",
         "How the %s got their context back: %d read from the cache, %d written again, "
         "%d both." % (_n(len(r0), "pause"), reads, writes, len(r0) - reads - writes)],
        ["A fair test: the person chose each route; nothing was assigned.",
         "A deliberate resume versus a session left open over lunch: both are a gap of "
         "%s or more." % hours_text(gap_hours),
         "Whether the resumed context was needed: a resume pays for the whole conversation."])


def page_carry(payload: Dict[str, Any], streams: Sequence[Dict[str, Any]],
               pages: Sequence[Tuple[str, str]]) -> str:
    continuations = payload["continuations"]
    summary = payload["continuation_summary"]
    scope = payload["scope"]
    redact = bool(scope.get("redact"))
    gap_hours = continuations["gap_hours"]
    return page(
        PAGE_CARRY, pages, "Pick-up cost — agent session audit",
        "Pick-up cost: resuming a session, or starting a new one",
        "The person chose each time whether to resume or start again; this page shows what "
        "those choices cost. Tokens first; dollars are the same tokens at list API rates.",
        panel("Three ways work gets picked up",
              "What does each name mean?",
              panel_carry_definitions(gap_hours))
        + panel("What each way of picking up cost",
                "Which kind of pick-up is big enough to look at?",
                panel_carry_by_kind(summary, continuations, streams, redact))
        + panel("Each work item, each way it was picked up",
                "Where is getting going again most expensive?",
                panel_carry_per_item(continuations, streams))
        + panel("Work items that did both",
                "On the same work, was resuming cheaper than a new session?",
                panel_carry_paired(continuations, streams, redact))
        + panel("By model and reasoning effort",
                "Does the pattern hold within one model and effort level?",
                panel_carry_strata(continuations))
        + carry_shows_box(continuations, gap_hours))


def page_set(with_carry: bool = False) -> List[Tuple[str, str]]:
    pages = [(PAGE_INDEX, "Home"),
             (PAGE_ORIENT, "Startup")]
    if with_carry:
        pages.append((PAGE_CARRY, "Pick-up cost"))
    pages += [(PAGE_DEFS, "Definitions"),
              (PAGE_LEDGER, "Session ledger"),
              (PAGE_APPENDIX, "Appendix")]
    return pages


def nav(current: str, pages: Sequence[Tuple[str, str]]) -> str:
    """The same strip on every page: where am I, how do I get home, where are
    the definitions (the header strip and its jump link)."""
    links = "".join('<a href="%s"%s>%s</a>'
                    % (esc(href), ' class="here"' if href == current else "", esc(label))
                    for href, label in pages)
    jump = ('<a class="jump" href="%s">what every term means →</a>' % PAGE_DEFS
            if current != PAGE_DEFS else '<span class="jump st">you are here</span>')
    return '<nav class="nav">%s%s</nav>' % (links, jump)


def page(current: str, pages: Sequence[Tuple[str, str]], title: str, heading: str,
         sub: str, body: str, wide: bool = False) -> str:
    """One self-contained page of the set."""
    return ("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title><style>%(css)s</style></head>
<body><button id="themebtn">theme</button><main%(wide)s>
%(nav)s
<h1>%(h1)s</h1>
<p class="sub">%(sub)s</p>
%(body)s
<hr style="margin:44px 0 10px;border:0;border-top:1px solid var(--line)">
<p class="sub">One report: %(links)s. Each page is self-contained and works offline.</p>
</main><script>%(js)s</script></body></html>"""
            % {"title": esc(title), "css": CSS, "js": JS, "nav": nav(current, pages),
               "h1": esc(heading), "sub": sub, "body": body,
               "wide": ' class="wide"' if wide else "",
               "links": " · ".join('<a href="%s">%s</a>' % (esc(h), esc(l)) for h, l in pages)})


# The five questions a sceptical reader asks first, each with today's honest
# answer. Shown in the appendix, collapsed.
SCEPTIC_QUESTIONS = [
    ("What counts, and out of how many?",
     "{with_boundary} of {sessions} sessions made an edit; every startup figure is out of "
     "those. The other {no_boundary} are no-edit sessions, never counted as zero. Success "
     "is inferred from the transcript (a commit or a passing check, or a failure), and "
     "unknown is kept apart."),
    ("Are these repeated facts, or new context?",
     "Not known. The same files came back, which is not proof the same thing was "
     "re-learned. That would need file versions kept between sittings."),
    ("Does a handoff note cut total cost, or just move the first edit earlier?",
     "Untested here. Log session ends with --handoff-log: the read-back compares total "
     "cost, including writing the note, so moving tokens later nets zero."),
    ("Which charge on a subscription does this reduce?",
     "Not mapped. Everything is tokens; dollars are list API rates for comparing "
     "sessions, not a bill and not a subscription meter."),
    ("How much of the history can this read?",
     "{coverage} of tokens carry a price. {label_note} Each store and what the filter "
     "removed are listed under A10."),
]


def decision_sensitivity(sessions: Sequence["Session"],
                         stream_records: Sequence[Dict[str, Any]],
                         first_ids: Optional[set] = None) -> str:
    """DOES THE ANSWER SURVIVE A REASONABLE CHANGE OF DEFINITION?

    The check to run before any recommendation: if the
    top-ranked directory or the top stream flips when ambiguous boundaries are
    excluded, when child-agent records are ignored, or when dollars replace
    tokens, the report should show that rather than pick one framing. Cheap,
    because every variant is already computed per session.
    """
    def top_directory(key) -> str:
        totals: Dict[str, float] = collections.defaultdict(float)
        for session in sessions:
            value = key(session)
            if value:
                totals[session.cwd_base] += value
        if not totals:
            return "—"
        best = max(totals.items(), key=lambda kv: kv[1])
        return "%s (%s)" % (best[0], ("%.2f" % best[1]) if best[1] < 10_000
                            else compact_number(best[1]))

    def top_stream(field) -> str:
        rows = [r for r in stream_records if r.get(field)]
        if not rows:
            return "—"
        best = max(rows, key=lambda r: r[field])
        return "%s %s" % (best["stream_id"], best["name"])

    variants = [
        ("as published: startup dollars, sessions with an edit, subagents left out of "
         "the first-edit search",
         top_directory(lambda s: s.metrics.get("orientation_cost_usd") or 0.0),
         top_stream("reorientation_tax_usd")),
        ("(i) no-edit sessions left out of every total (they already are; this row is "
         "the check)",
         top_directory(lambda s: (s.metrics.get("orientation_cost_usd") or 0.0)
                       if not s.metrics.get("no_mutation") else 0.0),
         top_stream("reorientation_tax_usd")),
        ("(ii) subagent spend ignored entirely",
         top_directory(lambda s: max(0.0, (s.metrics.get("orientation_cost_usd") or 0.0))
                       if not s.metrics.get("child_agents") else 0.0),
         top_stream("reorientation_tax_usd")),
        ("(iii) tokens instead of dollars",
         top_directory(lambda s: s.metrics.get("orientation_tokens") or 0),
         top_stream("reorientation_tax_tokens")),
    ]
    rows = "".join("<tr><td class='l'>%s</td><td class='l'>%s</td><td class='l'>%s</td></tr>"
                   % (esc(name), esc(directory), esc(stream))
                   for name, directory, stream in variants)
    stable = len({v[1] for v in variants}) == 1 and len({v[2] for v in variants}) == 1
    return ('<details class="method"><summary>Does the ranking survive a change of '
            'definition?</summary>'
            "<table><thead><tr><th class='l'>definition</th><th class='l'>top folder by "
            "startup cost</th><th class='l'>top work item</th></tr></thead><tbody>%s</tbody>"
            '</table><p class="note">%s</p></details>'
            % (rows, esc("Every variant picks the same top folder and work item, so the "
                         "ranking does not hang on one definition."
                         if stable else
                         "The variants disagree: the top of this ranking depends on the "
                         "definition, and nothing here picks between them.")))


def sceptic_panel(agg: Dict[str, Any], scope: Dict[str, Any]) -> str:
    """The five questions a sceptic asks first, collapsed: honest answers, kept
    out of the way of the reader who wants the result."""
    fields = {
        "sessions": agg.get("sessions") or 0,
        "with_boundary": agg.get("sessions_with_boundary") or 0,
        "no_boundary": agg.get("sessions_no_mutation") or 0,
        "coverage": scope.get("price_coverage") or "an unmeasured share",
        "label_note": ("%d of %d sessions record their first request."
                       % (agg.get("question_first_observable") or 0,
                          agg.get("sessions") or 0)),
    }
    rows = "".join(
        "<tr><td class='l'><strong>%s</strong></td><td class='l'>%s</td></tr>"
        % (esc(question), esc(answer.format(**fields)))
        for question, answer in SCEPTIC_QUESTIONS)
    return ('<details class="method"><summary>Five questions a sceptic asks first</summary>'
            "<table><thead><tr><th class='l'>question</th><th class='l'>answer, as of this "
            "run</th></tr></thead><tbody>%s</tbody></table></details>" % rows)


def panel_headline(agg: Dict[str, Any]) -> str:
    """The home page's one picture: est. cost split startup / the rest, and the
    sessions split made-an-edit / no-edit. Two 100 % bars on one scale, the part
    that matters in the accent hue and the rest in neutral grey (emphasis, not
    two categories), each segment direct-labelled where the label fits."""
    total_cost = agg.get("est_cost_usd_total") or 0.0
    startup_cost = agg.get("orientation_cost_usd_total") or 0.0
    sessions = agg.get("sessions") or 0
    edited = agg.get("sessions_with_boundary") or 0
    rows = []
    if total_cost > 0:
        rows.append(("est. cost", "$%.2f" % total_cost,
                     (startup_cost / total_cost, "startup $%.2f" % startup_cost,
                      "spent before the first edit"),
                     ("the rest $%.2f" % max(0.0, total_cost - startup_cost),
                      "the work itself, plus no-edit sessions")))
    if sessions:
        rows.append(("sessions", "%d" % sessions,
                     (edited / float(sessions), "%d made an edit" % edited,
                      "these have a startup share"),
                     ("%d no-edit" % (sessions - edited),
                      "questions, reviews, research: no startup share")))
    if not rows:
        return '<p class="axis">No priced session to draw.</p>'
    share = (startup_cost / total_cost) if total_cost else 0.0
    title = ("%.0f %% of the est. cost went on startup — before each session's first edit"
             % (100 * share) if total_cost else "Sessions that made an edit, and those that did not")
    width, left, right, top, row_h, bar_h = 880, 96, 70, 46, 46, 22
    pw = width - left - right
    height = top + row_h * len(rows) + 8
    parts = [text_block(14, 20, width - 28, title, cls="g-title")]
    for index, (label, total, (frac, lab_a, tip_a), (lab_b, tip_b)) in enumerate(rows):
        y = top + index * row_h
        wa = pw * max(0.0, min(1.0, frac))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" class="axis-strong">%s</text>'
                     % (left - 10, y + bar_h * 0.7, esc(label)))
        # 2px surface gap between the two segments; rounded data ends only
        parts.append('<rect x="%d" y="%d" width="%.1f" height="%d" rx="4" fill="var(--accent)" '
                     'data-tip="%s"/>' % (left, y, max(wa - 1, 0.5), bar_h,
                                          esc("%s — %s" % (lab_a, tip_a))))
        parts.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" rx="4" '
                     'fill="var(--rest)" stroke="var(--rest-edge)" data-tip="%s"/>'
                     % (left + wa + 1.5, y + 0.5, max(pw - wa - 2, 0.5), bar_h - 1,
                        esc("%s — %s" % (lab_b, tip_b))))
        for text, x0, x1 in ((lab_a, left, left + wa), (lab_b, left + wa, left + pw)):
            if len(text) * SVG_FONT["val"] * CHAR_EM + 14 <= (x1 - x0):
                # a label inside a filled mark picks its ink by the fill
                parts.append('<text x="%.1f" y="%.1f" class="val" style="fill:%s">%s</text>'
                             % (x0 + 8, y + bar_h * 0.7,
                                "#fff" if x0 == left else "var(--ink)", esc(text)))
        parts.append('<text x="%d" y="%.1f" class="val">%s</text>'
                     % (left + pw + 8, y + bar_h * 0.7, esc(total)))
    return ('<section class="panel">%s%s</section>'
            % (svg(width, int(height), title, "".join(parts)),
               legend_rich([("startup / made an edit", swatch_solid("var(--accent)")),
                            ("the rest / no-edit",
                             swatch_edged("var(--rest)", "var(--rest-edge)"))])))


def finding_pointer(payload: Dict[str, Any]) -> str:
    """One line under the home chart: the biggest finding, and where it is drawn.

    The tiles and the chart already say what the five-line summary says, so
    the home page points on instead; the five lines stay in the MD and stdout.
    """
    agg = payload["aggregates"]
    streams = payload.get("streams") or []
    worst = max(streams, key=lambda r: r[STREAM_RANK_METRIC], default=None)
    if worst and worst.get(STREAM_RANK_METRIC):
        members = set(worst.get("threads") or [])
        inside = sum(r["reorientation_tax_tokens"] for r in payload.get("threads") or []
                     if r.get("thread_id") in members)
        inside = min(inside, worst[STREAM_RANK_METRIC])
        after = worst[STREAM_RANK_METRIC] - inside
        where = ("all inside sittings" if not after else "all after long breaks" if not inside
                 else "mostly after long breaks" if after > inside else "mostly inside sittings")
        text = ("Most repeat startup: <strong>%s</strong> — %s tokens, %s"
                % (esc(str(worst["name"])), compact_number(worst[STREAM_RANK_METRIC]), where))
        target = "%s#repeat-startup" % PAGE_ORIENT
    elif agg.get("median_orientation_share") is not None:
        text = ("Half the sessions that made an edit spent more than %.0f %% of their "
                "tokens before it" % (100 * agg["median_orientation_share"]))
        target = PAGE_ORIENT
    else:
        return ""
    return '<p class="pointer">%s → <a href="%s">Startup page</a></p>' % (text, target)


def page_index(payload: Dict[str, Any], pages: Sequence[Tuple[str, str]],
               included: Dict[str, str], sensitivity: str = "",
               unavailable: Optional[Dict[str, str]] = None) -> str:
    """The HOME page: the answer as a picture first, then everything else on offer.

    `sensitivity` is accepted for compatibility and ignored: the check on
    whether the ranking survives a change of definition lives in the appendix.
    """
    agg = payload["aggregates"]
    scope = payload["scope"]
    tiles = "".join(
        '<div class="card"><div class="metric">%s</div><div class="label">%s</div></div>'
        % (m, esc(l)) for m, l in [
            ("$%.2f" % (agg.get("est_cost_usd_total") or 0.0),
             "est. cost of %d sessions, at list API rates%s"
             % (agg.get("sessions") or 0,
                "; %d with no token counts are unpriced"
                % scope["sessions_without_token_counts"]
                if scope.get("sessions_without_token_counts") else "")),
            ("$%.2f" % (agg.get("orientation_cost_usd_total") or 0.0),
             "startup cost: tokens spent before each session's first edit"),
            (("%.0f%%" % (100 * agg["median_orientation_share"]))
             if agg.get("median_orientation_share") is not None else "n/a",
             "median startup share, over the %d sessions that made an edit"
             % (agg.get("sessions_with_boundary") or 0)),
            (compact_number(agg.get("stream_tax_tokens") or agg.get("reorientation_tax_tokens") or 0),
             "tokens of repeat startup, incl. long breaks"),
        ])
    return page(
        PAGE_INDEX, pages,
        "Agent session audit",
        "What your coding sessions cost, and how much went on startup",
        "%s from %s on <strong>%s</strong>, %s → %s. Every dollar is an estimate "
        "at list API rates, not a bill. Generated %s; offline and read-only."
        % (_n(agg["sessions"], "session"), esc(", ".join(scope["stores"])),
           esc(scope.get("label") or "local history"),
           esc(scope.get("since") or "the beginning"), esc(scope.get("until") or "now"),
           esc((iso(dt.datetime.now(dt.timezone.utc)) or "")[:16].replace("T", " "))),
        '<div class="cards">%s</div>' % tiles
        + panel_headline(agg)
        + finding_pointer(payload)
        + "<h2>Everything this skill can measure</h2>"
        + analysis_index(included, unavailable, payload.get("ledger_runs"))
        + '<p class="sub" style="margin-top:14px">Every term is on the '
          '<a href="%s">definitions page</a>. How this was measured, what it cannot show, '
          'and which sessions were left out are in the <a href="%s">appendix</a>.</p>'
          % (PAGE_DEFS, PAGE_APPENDIX))


def page_orientation(payload: Dict[str, Any], sessions: Sequence[Session],
                     threads: Sequence[Thread], streams: Sequence["Stream"],
                     pages: Sequence[Tuple[str, str]]) -> str:
    """Startup: the metric taught on toy data, then the two questions.

    ORDER: explainer -> where the startup money went -> what a work item, a
    sitting and repeat startup ARE, drawn -> the real work-item timeline ->
    which work items pay the most. No term appears in a number before it has
    been defined.
    """
    agg = payload["aggregates"]
    scope = payload["scope"]
    redact = bool(scope.get("redact"))
    first_ids = first_in_thread_ids(threads)
    return page(
        PAGE_ORIENT, pages, "Startup cost",
        "Where your sessions spend tokens before their first edit",
        "Two questions: which sessions cost the most to get started, and which work costs "
        'the most each time it comes back. <a href="%s">Every term, defined</a>.' % PAGE_DEFS,
        "<h2>How to read startup share</h2>"
        + panel("Startup share and repeat startup, on toy data",
                "Is a high share good or bad?",
                panel_explainer(PAGE_APPENDIX))
        + "<h2>Where the startup money went</h2>"
        + panel("The sessions that spent the most before their first edit",
                "Which sessions and folders cost the most to get started?",
                panel_money_went(sessions, redact, first_ids=first_ids),
                "Startup cost prices each response before the first edit at its own "
                "model's rates, not the session's average.")
        + "<h2>Work that comes back</h2>"
        + panel("Work items, sittings and repeat startup",
                "What exactly is counted?",
                panel_tax_definitions())
        + panel("Work items over time",
                "One long piece of work, or the same work picked up again?",
                panel_stream_timeline(streams, redact))
        + panel("Which work items pay the most repeat startup",
                "Where does getting going again cost the most?",
                panel_stream_tax(payload.get("streams") or [], payload["threads"], agg=agg),
                anchor="repeat-startup")
        + shows_box(STARTUP_SHOWS, STARTUP_NOT_SHOWN)
        + '<p class="sub">One row per session: the <a href="%s">session ledger</a>. '
          'Distributions, calibration and prices: the <a href="%s">appendix</a>.</p>'
          % (PAGE_LEDGER, PAGE_APPENDIX))


DEFINITIONS_NOT_SHOWN = [
    "Whether a session re-learned something: the same files coming back is not proof.",
    "Whether a skipped note was about this work: if it was not, skipping it was right.",
    "Whether a handoff note pays for itself: log session ends and use --handoff-log.",
]


def page_definitions(payload: Dict[str, Any], pages: Sequence[Tuple[str, str]]) -> str:
    return page(
        PAGE_DEFS, pages, "Definitions",
        "What each number means, and which way is good",
        "Every page links here from its header.",
        panel("Terms", "What is being counted?", definitions_box())
        + panel("Work items, sittings and repeat startup, drawn",
                "What is counted above one session?",
                panel_tax_definitions(with_table=False))
        + panel("Three ways work gets picked up again, drawn",
                "What each kind of pick-up names, and what its cost covers.",
                panel_carry_definitions(
                    (payload.get("continuations") or {}).get("gap_hours", RESUME_GAP_HOURS),
                    with_table=False))
        + shows_box(["Tokens and est. cost, per session, sitting and work item.",
                     "Where startup ends: the first edit.",
                     "Which sessions read a handoff note."],
                    DEFINITIONS_NOT_SHOWN))


def page_ledger(payload: Dict[str, Any], ledger_path: Optional[str],
                pages: Sequence[Tuple[str, str]]) -> str:
    return page(
        PAGE_LEDGER, pages, "Session ledger",
        "One row per session",
        "Each analysis adds its own columns. Click a header to sort. The full row is in "
        "<code>sessions.jsonl</code>.",
        ledger_browser(ledger_path, [r["session_id"] for r in payload["sessions"]]),
        wide=True)


def page_appendix(payload: Dict[str, Any], sessions: Sequence[Session],
                  threads: Sequence[Thread], pages: Sequence[Tuple[str, str]]) -> str:
    """Method, checks and the full tables. What has NOT been shown lives here,
    under the #not-shown anchor the explainer links to."""
    agg = payload["aggregates"]
    scope = payload["scope"]
    redact = bool(scope.get("redact"))
    thread_records = payload["threads"]
    thread_rows = "".join(
        "<tr><td class='l' data-tip='%s'>%s</td><td class='r'>%s</td>"
        "<td class='l'>%s</td><td class='r'>%s</td><td class='r' data-v='%s'>%s</td>"
        "<td class='r' data-v='%s'>%s</td></tr>"
        % (esc("; ".join(filter(None, [m.get("first_prompt") or m.get("title") or ""
                                       for m in r["members"]]))[:400]),
           esc(short_path(r.get("cwd") or "", 48)), r["sessions"], esc(", ".join(r["tools"])),
           r.get("elapsed_days"), r["reorientation_tax_tokens"],
           compact_number(r["reorientation_tax_tokens"]),
           r["est_cost_usd"], "$%.2f" % r["est_cost_usd"])
        for r in sorted(thread_records, key=lambda r: -r["reorientation_tax_tokens"])[:200])
    scope_lines = "".join("<li>%s</li>" % esc(line) for line in scope_block(scope))
    readable = agg.get("question_first_observable") or 0
    qf = ("%d of %d sessions record their first request; %d of those started with a "
          "question. %s"
          % (readable, agg.get("sessions") or 0, agg.get("question_first_sessions") or 0,
             _question_first_sentence(agg)))
    return page(
        PAGE_APPENDIX, pages, "Appendix — how these numbers were measured",
        "How these numbers were measured",
        "Method, checks and full tables. Nothing here changes the answer on the home page.",
        '<h2 id="not-shown">A0 · What has not been shown</h2>'
        + '<ul>%s</ul>' % "".join("<li>%s</li>" % esc(line) for line in (
            "A good/bad line for startup share: no threshold has been validated against "
            "outcomes.",
            "That repeat startup is re-learning: the same files came back, which is not proof.",
            "That a handoff note pays for itself: log session ends and see A8."))
        + '<p class="note">%s</p>' % esc(qf)
        + sceptic_panel(agg, scope)
        + decision_sensitivity(sessions, payload.get("streams") or [])
        + "<h2>A1 · Startup share across sessions</h2>"
        + panel("Startup share per session",
                "A fixed overhead, or a long tail of sessions slow to start?",
                panel_a_histogram(sessions))
        + panel("Startup share, by how the session ended",
                "One population, or two hiding in one chart?",
                panel_a_by_outcome(sessions),
                "Failed runs tend to be longer (arXiv 2511.00197), so two humps would be "
                "the finding.")
        + panel("Files read first",
                "Are those tokens discovering the code, or re-reading one file?",
                panel_i_breadth(sessions),
                "Only 7 % of human programming sessions skip reading before editing "
                "(Parnin & Rugaber, 2011): some startup is normal.")
        + panel("Est. cost against startup share",
                "Is startup cost in the expensive sessions or the cheap ones?",
                panel_b_scatter(sessions, redact))
        + "<h2>A2 · Scripted or by hand</h2>"
        + panel("Strategy and model",
                "Is the cost difference the model, or scripting versus editing by hand?",
                panel_e_matrix(sessions),
                "The strategy is read from the tool calls, not self-reported. "
                "<code>&lt;synthetic&gt;</code> is not a model and is left out.")
        + "<h2>A3 · Effort, and whether reasoning volume agrees</h2>"
        + panel("Effort and thinking share per model",
                "Which effort level did this run at, and does the reasoning volume agree?",
                panel_f_thinking(sessions))
        + "<h2>A4 · The shape of the work</h2>"
        + panel("What each sitting did",
                "Which sittings keep going back to startup?",
                panel_c2_stage_paths(threads, redact))
        + panel("Repeat startup per sitting",
                "How big is repeat startup next to the whole sitting?",
                panel_c_thread_stacks(thread_records))
        + '<details class="method"><summary>Each sitting, session by session</summary>%s'
          '</details>' % panel_j_turn_table(threads, redact)
        + "<h2>A5 · How sittings and work items were built</h2>"
        + '<details class="method"><summary>The rules, and how far they are calibrated'
          '</summary>%s</details>' % _appendix_calibration(scope, payload.get("stream_rule") or {})
        + "<h2>A6 · What was deduplicated</h2>"
        + '<details class="method"><summary>One response, many rows</summary>%s</details>'
          % _appendix_dedupe(scope)
        + "<h2>A7 · The price table</h2>"
        + '<details class="method"><summary>Rates per model</summary>%s</details>'
          % _appendix_pricing(scope)
        + "<h2>A8 · The handoff experiment</h2>"
        + panel("Does a handoff note pay for itself?",
                "Does writing a short note at the end of a session save more than it costs?",
                panel_h_experiment(payload.get("handoff")),
                "Paired within a sitting: the coin is flipped per session end. A note nobody "
                "reads cannot be the cause of anything.")
        + "<h2>A9 · All sittings</h2>"
        + '<details class="method"><summary>%d sittings (hover a row for its first '
          'prompt lines)</summary><div class="scroll"><table class="sortable"><thead><tr>'
          '<th class="l">folder</th><th class="r">sessions</th><th class="l">tools</th>'
          '<th class="r">days</th><th class="r">repeat startup</th><th class="r">est. $</th>'
          '</tr></thead><tbody>%s</tbody></table></div></details>'
          % (len(thread_records), thread_rows)
        + "<h2>A10 · Which sessions are included</h2>"
        + cohort_table(scope)
        + '<details class="method"><summary>Scope and integrity</summary><ul>%s</ul>'
          '</details>' % scope_lines
        + '<p class="note">The usage meter is not in this report: API billing has no 5-hour '
          'or weekly limit. <code>orientation_profile.py meter</code> shows it on a '
          'subscription plan.</p>')


def write_report_pages(payload: Dict[str, Any], sessions: Sequence[Session],
                       threads: Sequence[Thread], streams: Sequence["Stream"],
                       out: Path, ledger_path: Optional[str] = None) -> List[Tuple[str, str]]:
    """Write every page of the set. Returns (filename, label) in nav order."""
    # The carry-vs-restart page is written when at least one stream
    # contains BOTH a fresh restart and a resume — those rows are the only
    # ones that answer its question. Otherwise the index says what was
    # missing, and the page is not written at all: an empty comparison page
    # would be read as a null result rather than as an absent one.
    continuations = payload.get("continuations") or {}
    paired = continuations.get("paired_streams") or []
    with_carry = bool(paired)
    pages = page_set(with_carry)
    included = {"orientation": PAGE_ORIENT, "streams": PAGE_ORIENT}
    unavailable: Dict[str, str] = {}
    if with_carry:
        included["carry"] = PAGE_CARRY
    elif continuations:
        counts = continuations.get("counts") or {}
        unavailable["carry"] = (
            "no work item here was both resumed and restarted, so there is nothing to "
            "compare. Seen: %d %s, %d %s, %s, in %s."
            % (counts.get("R0", 0), CONTINUATION_LABEL["R0"],
               counts.get("R1", 0), CONTINUATION_LABEL["R1"],
               _n(counts.get("R2", 0), CONTINUATION_LABEL["R2"]),
               _n(continuations.get("streams_with_events", 0), "work item")))
    if ledger_path:
        included["ledger"] = PAGE_LEDGER
    written = [
        (PAGE_INDEX, page_index(payload, pages, included,
                                decision_sensitivity(sessions, payload.get("streams") or []),
                                unavailable)),
        (PAGE_ORIENT, page_orientation(payload, sessions, threads, streams, pages)),
        (PAGE_DEFS, page_definitions(payload, pages)),
        (PAGE_LEDGER, page_ledger(payload, ledger_path, pages)),
        (PAGE_APPENDIX, page_appendix(payload, sessions, threads, pages)),
    ]
    if with_carry:
        written.append((PAGE_CARRY, page_carry(payload, payload.get("streams") or [], pages)))
    for name, document in written:
        write_output(out / name, document)
    return pages


# ==========================================================================
# SCREEN PACK -- numbered one-screen pages sized for a projector, a
# screenshot, or reading aloud in a meeting: one screen per page at 1920x1080
# with NO scrolling, >= 18px monospace, dark on white, a header band carrying
# page number and date, <= ~40 rows, numbers right-aligned with thousands
# separators, and one chart per page with labels big enough to read at a
# distance.
#
# The row caps are enforced in CODE, not by eyeballing a render: a page that
# would overflow is truncated with a printed "+N more" line, and the selftest
# asserts the cap rather than measuring pixels.
# ==========================================================================
SCREEN_W, SCREEN_H = 1920, 1080
SCREEN_ROWS = 40                 # body rows that fit at 18px with the header band
SCREEN_CSS = """
:root{--ink:#111;--muted:#333;--card:#fff;--line:#bbb;--grid:#ddd;--chip:#eee;
 --accent:#2a78d6;--accent2:#eb6834;--arm-note:#2a78d6;--arm-none:#eb6834;
 --div-pos:#2a78d6;--div-neg:#e34948;--div-mid:#f0efec;
 --stage1:#cde2fb;--stage2:#86b6ef;--stage3:#2a78d6;--stage4:#184f95}
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:%(w)dpx;height:%(h)dpx;overflow:hidden;background:#fff;color:#111;
 font:19px/1.34 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.page{width:%(w)dpx;height:%(h)dpx;padding:0 34px 22px;display:flex;flex-direction:column}
.band{background:#111;color:#fff;margin:0 -34px 16px;padding:11px 34px;display:flex;
 justify-content:space-between;align-items:baseline;font-weight:700;font-size:23px}
.band .n{font-size:30px}
.band .d{font-weight:400;font-size:17px;color:#ddd}
h2{font-size:21px;margin:12px 0 6px;font-weight:700}
table{width:100%%;border-collapse:collapse;font-size:18px;font-variant-numeric:tabular-nums}
th,td{padding:3px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid #ccc}
th:first-child,td:first-child,.l{text-align:left}
th{background:#111;color:#fff;font-weight:700;border-bottom:2px solid #111}
tr:nth-child(even) td{background:#f2f2f2}
.big{font-size:26px;line-height:1.5}
.sum{font-size:22px;line-height:1.55;white-space:pre-line}
.more{font-size:17px;padding-top:6px}
ul{margin-left:22px;font-size:19px;line-height:1.5}
.k{display:inline-block;min-width:26px;font-weight:700}
/* The panels are authored in an 880-wide viewBox. Scaling them to the full
   1920 page blows every label up ~2.2x and the titles run off the edge, so a
   chart page pins the SVG to 1240px: ~1.4x, which lands the 15px labels near
   21px -- big enough to read on a projected screen, small enough to fit. */
.chartbox{width:1240px;margin:2px auto}
svg{width:1240px;height:auto;display:block}
.g-title{font-size:15px;font-weight:700;fill:#111}
.axis{font-size:12px;fill:#333} .axis-strong{font-size:12.5px;fill:#111;font-weight:700}
.val{font-size:12px;font-weight:700;fill:#111}
.grid{stroke:#ddd;stroke-width:1} .baseline{stroke:#111;stroke-width:1.5}
.s1{fill:#2a78d6} .s2{fill:#eb6834} .s1s{stroke:#2a78d6;fill:none} .s2s{stroke:#eb6834;fill:none}
.legend{display:flex;gap:30px;flex-wrap:wrap;font-size:18px;color:#111;
 margin:6px auto 2px;width:1240px}
.legend span{display:inline-flex;align-items:center;gap:8px}
.sw{width:15px;height:15px;border-radius:3px;display:inline-block}
.note{border-left:5px solid #111;background:#eee;padding:8px 14px;font-size:17px;margin:8px 0}
.cards{display:flex;gap:14px;margin:10px 0}
.card{border:2px solid #111;padding:10px 16px}
.metric{font-size:30px;font-weight:700}
.label{font-size:16px;color:#333}
.decision{font-size:17px;color:#333;margin:2px 0 6px}
""" % {"w": SCREEN_W, "h": SCREEN_H}


# Paging lives on EVERY page, not only on INDEX: whoever is presenting pages
# with the arrow keys and never has to touch the trackpad.
SCREEN_JS = """
var P=%s;var I=%d;
document.addEventListener('keydown',function(e){
 if(e.key==='ArrowRight'&&I+1<P.length)location.href=P[I+1];
 else if(e.key==='ArrowLeft'&&I>0)location.href=P[I-1];
 else if(e.key==='i'||e.key==='I')location.href='INDEX.html';
 else if(e.key==='f'||e.key==='F'){var d=document.documentElement;
  if(document.fullscreenElement)document.exitFullscreen();else d.requestFullscreen();}});
"""


def _screen_page(number: int, total: int, title: str, body: str, when: str,
                names: Sequence[str] = ()) -> str:
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>%(n)02d %(t)s</title><style>%(css)s</style></head><body><div class="page">
<div class="band"><span class="n">%(n)02d/%(tot)02d</span><span>%(t)s</span>
<span class="d">%(when)s &nbsp; &larr;/&rarr; page &nbsp; F full screen</span></div>
%(body)s</div><script>%(js)s</script></body></html>"""
            % {"n": number, "tot": total, "t": esc(title), "css": SCREEN_CSS,
               "body": body, "when": esc(when),
               "js": SCREEN_JS % (json.dumps(list(names)), number - 1)})


def _screen_table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
                 cap: int = SCREEN_ROWS) -> str:
    """A table that CANNOT overflow the page: the cap is applied here."""
    shown, hidden = list(rows[:cap]), max(0, len(rows) - cap)
    head = "".join("<th%s>%s</th>" % (' class="l"' if not index else "", esc(h))
                   for index, h in enumerate(headers))
    body = "".join("<tr>" + "".join(
        "<td%s>%s</td>" % (' class="l"' if not index else "", esc(value))
        for index, value in enumerate(row)) + "</tr>" for row in shown)
    more = ('<p class="more">+ %d more row(s) not shown — this page is capped at %d '
            'so it fits one screen.</p>' % (hidden, cap)) if hidden else ""
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>%s" % (head, body, more)


def _thousands(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—" if value is None else str(value)
    return "{:,.0f}".format(number) if abs(number) >= 1000 or number == int(number) \
        else "{:,.2f}".format(number)


def _screen_ledger(ledger_path: Optional[str]) -> str:
    """The ledger's column list, as a one-screen page.

    The point of the page is the LAST column: how many sessions actually carry
    a value. A column registered by an analysis that has not been run here
    shows 0 and says NOT RUN, which is a different statement from a zero.
    """
    if not ledger_path:
        return ('<p class="big">No ledger was written for this run.</p>'
                '<p class="more">Add <tt>--ledger OUT/sessions.jsonl</tt> to build the table '
                'every analysis in this skill adds its columns to.</p>')
    try:
        import session_ledger
        stats = session_ledger.summary(ledger_path)
    except Exception as exc:
        return '<p class="big">Ledger unreadable: %s</p>' % esc(exc.__class__.__name__)
    rows = [(column,
             (meta.get("producer") or "?").replace(".py", ""),
             "yes" if meta.get("updated") else "NOT RUN",
             _thousands(stats["coverage"].get(column, 0)))
            for column, meta in sorted(stats["provenance"].items(),
                                       key=lambda kv: ((kv[1].get("producer") or ""), kv[0]))]
    return ('<p class="big">%s</p>' % esc(session_ledger.headline(ledger_path))
            + _screen_table(["column", "written by", "run here", "sessions with a value"], rows))


def _screen_rules(scope: Dict[str, Any], stream_rule: Dict[str, Any]) -> List[Tuple[str, str]]:
    rule = scope.get("thread_rule") or {}
    return [
        ("sitting: gap", "%g hours" % rule.get("gap_hours", 0)),
        ("sitting: shared files needed", str(rule.get("min_shared"))),
        ("sitting: session cap", str(rule.get("max_sessions"))),
        ("work item: rule", str(stream_rule.get("stream_by") or "auto")),
        ("work items found", str(stream_rule.get("streams") or 0)),
        ("work items that recur", str(stream_rule.get("multi_episode_streams") or 0)),
        ("joins by rule", ", ".join("%s %d" % (k, v) for k, v in
                                    (stream_rule.get("merges_by_rule") or {}).items()) or "none"),
        ("measured precision", "not measured here — calibrate with threads --labels"),
        ("dedupe", "%s duplicate rows removed" % _thousands(scope.get("duplicates") or 0)),
        ("prices", str(scope.get("price_table"))),
    ]


def screen_pack(payload: Dict[str, Any], sessions: Sequence[Session],
               threads: Sequence[Thread], store_stats: Sequence[Dict[str, Any]],
               out: Path, streams: Sequence["Stream"] = (),
               ledger_path: Optional[str] = None) -> List[Tuple[str, str]]:
    """Write the numbered one-screen pages plus INDEX.html. Returns the titles.

    The REDACTED variant is the one to show or share; the full-fidelity pack
    is for the owner's own reading. Which one this is comes from the payload's
    own scope block, so a pack can never claim a fidelity it does not have.
    """
    out = Path(out)
    audit_common.make_private_dir(out)
    agg = payload["aggregates"]
    scope = payload["scope"]
    redact = bool(scope.get("redact"))
    when = (scope.get("generated") or "")[:16].replace("T", " ") + "Z"
    mode = "REDACTED — safe to share" if redact else "FULL FIDELITY — do not share"
    pages: List[Tuple[str, str]] = []

    def add(title, body):
        pages.append((title, body))

    # ORDER IS THE DELIVERABLE. Decision pages first, appendix pages last,
    # matching the HTML report exactly -- the pages are shown and read in
    # order, so a methodology chart on page 3 is
    # the same mistake in this medium as it was in the browser.

    # 1 — inventory
    inventory = [(str(st.get("store")), _thousands(st.get("sessions_with_turns")
                                                   or st.get("sessions") or 0),
                  _thousands(st.get("files") or 0),
                  "yes" if st.get("rows") or st.get("sessions") else "no")
                 for st in store_stats]
    fields = [("timestamps", "yes"), ("model per session", "yes"),
              ("effort per turn", "%.0f%% of turns" % (100 * (agg.get("effort_recorded_share") or 0))),
              ("token counts", scope.get("price_coverage") or "n/a"),
              ("tool calls", "yes"), ("file paths", "yes")]
    add("Inventory — what was found on this machine",
        '<p class="big">%s</p>' % esc(mode)
        + _screen_table(["store", "sessions", "files", "usable"], inventory)
        + "<h2>Fields present</h2>"
        + _screen_table(["field", "coverage"], fields)
        + '<p class="more">%s</p>' % esc("Window: %s → %s"
                                         % (scope.get("since") or "beginning",
                                            scope.get("until") or "now")))

    # 2 — the answer, in five lines
    headline = [
        ("sessions", _thousands(agg["sessions"])),
        ("tokens", _thousands(agg["tokens_total"])),
        ("est. cost at list API rates (not a bill)", "%.2f" % agg["est_cost_usd_total"]),
        ("…of which was startup cost", "%.2f" % (agg.get("orientation_cost_usd_total") or 0.0)),
        ("median startup share (of the %s)"
         % _n(agg.get("sessions_with_boundary") or 0, "session that made an edit",
              "sessions that made an edit"),
         "%.0f%%" % (100 * agg["median_orientation_share"])
         if agg.get("median_orientation_share") is not None else "n/a"),
        ("repeat startup, within sittings (tokens)", _thousands(agg["reorientation_tax_tokens"])),
        ("repeat startup, incl. long breaks (tokens)",
         _thousands(agg.get("stream_tax_tokens") or agg["reorientation_tax_tokens"])),
        ("work items with more than one sitting / all",
         "%d / %d" % (agg.get("multi_episode_streams") or 0, agg.get("streams") or 0)),
        ("no-edit sessions", _thousands(agg["sessions_no_mutation"])),
    ]
    add("The answer", '<p class="sum">%s</p>' % esc(payload["summary"])
        + _screen_table(["measure", "value"], headline))

    # 3 — what the metric IS, on toy data, before any real number
    add("What startup share and repeat startup are — toy example", panel_explainer())

    # 4 — DECISION 1
    add("Decision 1 — where the startup money went",
        panel_money_went(sessions, redact, first_ids=first_in_thread_ids(threads)))

    # 5 — DECISION 2, terms before numbers: the drawn definition of a
    # stream, an episode and the two taxes gets its own page, because each
    # page can be shown and read on its own.
    add("Decision 2 — work item, sitting and repeat startup, drawn",
        panel_tax_definitions())

    # 6 — DECISION 2, over time
    add("Decision 2 — the same work, picked up again and again",
        panel_stream_timeline(streams, redact, top_n=16))

    # 7 — DECISION 2, the numbers
    add("Decision 2 — which work items pay the most repeat startup",
        panel_stream_tax(payload.get("streams") or [], payload["threads"], top_n=16, agg=agg))

    # 8 — the definitions, so any page can be read alone
    add("Definitions — what each number means and which way is good",
        _screen_table(["term", "what it is", "which way is good"],
                     [(name, sentence, direction)
                      for name, sentence, direction, _ in DEFINITIONS], cap=8))

    # 9 — the ledger: which analyses have contributed columns here
    add("Session ledger — the table every analysis adds columns to",
        _screen_ledger(ledger_path))

    # 10, 11 — sessions, two orderings
    def session_rows(records):
        return [(short_path(r.get("cwd") or r["cwd_basename"], 34),
                 (r.get("start") or "")[:10],
                 str((real_models(r.get("models") or []) or ["—"])[0]).replace("claude-", "")[:14],
                 r.get("effort") or "—", (r.get("strategy") or "")[:9],
                 ("%.0f%%" % (100 * r["orientation_share"]))
                 if r.get("orientation_share") is not None else "no edit",
                 "yes" if r.get("handoff_opened_by_turn_2") else "—",
                 "—" if r.get("est_cost_usd") is None else "%.2f" % r["est_cost_usd"])
                for r in records]

    columns = ["where", "date", "model", "effort", "strategy", "startup", "note read", "est. $"]
    by_cost = sorted(payload["sessions"], key=lambda r: -(r.get("est_cost_usd") or 0))
    by_share = sorted([r for r in payload["sessions"] if r.get("orientation_share") is not None],
                      key=lambda r: -r["orientation_share"])
    add("Sessions — top 40 by est. cost", _screen_table(columns, session_rows(by_cost)))
    add("Sessions — top 40 by startup share", _screen_table(columns, session_rows(by_share)))

    # 12 — sittings
    add("Sittings — top 30 by repeat startup",
        _screen_table(["folder", "sess", "days", "tools", "repeat startup", "est. $"],
                     [(short_path(r.get("cwd") or "", 34),
                       r["sessions"], r.get("elapsed_days"), len(r["tools"]),
                       _thousands(r["reorientation_tax_tokens"]),
                       "%.2f" % r["est_cost_usd"])
                      for r in sorted(payload["threads"],
                                      key=lambda r: -r["reorientation_tax_tokens"])[:30]], cap=30))

    # --- APPENDIX -------------------------------------------------------
    # Everything below is here to show the numbers above are measured, not
    # asserted. Nothing here is a decision.
    add("Appendix — startup share per session",
        panel_a_histogram(sessions, height=560))
    add("Appendix — startup tokens against files read first", panel_i_breadth(sessions))
    add("Appendix — repeat startup per sitting",
        panel_c_thread_stacks(payload["threads"], top_n=20))
    add("Appendix — what each sitting did", panel_c2_stage_paths(threads, redact, top_n=18))

    grid: Dict[Tuple[str, str], List[float]] = collections.defaultdict(list)
    for session in sessions:
        if session.metrics.get("est_cost_usd") is None:
            continue                     # no token counts: unpriced, not $0
        grid[(session.metrics.get("strategy", "?"),
              primary_model(session).replace("claude-", ""))].append(
                  session.metrics["est_cost_usd"])
    add("Appendix — strategy and model, median est. $ per session",
        _screen_table(["strategy", "model", "n", "median $", "max $"],
                     sorted([(k[0], k[1], len(v), "%.3f" % statistics.median(v),
                              "%.2f" % max(v)) for k, v in grid.items()],
                            key=lambda r: (r[0], r[1]))))

    add("Appendix — effort and thinking share",
        _screen_table(["effort", "turns", "sessions", "median est. $"],
                     [(level, _thousands(turns),
                       (agg.get("sessions_by_effort") or {}).get(level, 0),
                       (agg.get("median_cost_by_effort") or {}).get(level, "—"))
                      for level, turns in (agg.get("effort_turns") or {}).items()])
        + "<h2>Median thinking share per model</h2>"
        + _screen_table(["model", "median thinking share"],
                       [(m, v) for m, v in agg["median_thinking_share_by_model"].items()]))

    add("Appendix — how sittings and work items were built",
        _screen_table(["rule", "value"], _screen_rules(scope, payload.get("stream_rule") or {})))

    if payload.get("handoff"):
        # ONE chart per page: the paired dots are the result, the net-value
        # bars are a different question, and crammed together neither reads.
        add("Appendix — handoff experiment: startup per sitting, NOTE vs NONE",
            _screen_experiment(payload["handoff"], "dots"))
        add("Appendix — handoff experiment: net value, and is the note found?",
            _screen_experiment(payload["handoff"], "value"))


    total = len(pages)
    names = ["%02d-%s.html" % (index, re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40])
             for index, (title, _) in enumerate(pages, 1)]
    titles = []
    for index, ((title, body), name) in enumerate(zip(pages, names), 1):
        write_output(out / name,
                     _screen_page(index, total, title, body, when, names))
        titles.append((name, title))
    write_output(out / "INDEX.html", _screen_index(titles, when, mode))
    return titles


def _screen_experiment(report: Dict[str, Any], half: str) -> str:
    """The experiment, split across two pages so neither is crowded.

    `dots` is the result (the slope of each line). `value` is the separate
    question of whether it paid, plus the mediator gate. Both carry n, the
    pending count and the underpowered banner, because one page seen on its
    own must not be readable as more than it is.
    """
    head = ('<p class="big">%d end(s) logged · %d pending · %d unmatched · '
            '%d successor(s) · <b>%d usable pair(s)</b></p>'
            % (report["ends_logged"], report["pending_ends"], report["unmatched_ends"],
               report["successor_rows"], report["usable_pairs"]))
    if report["underpowered"]:
        head += ('<p class="note"><b>UNDERPOWERED — %d pair(s), floor %d.</b><br>%s</p>'
                 % (report["usable_pairs"], MIN_USABLE_PAIRS,
                    esc(UNDERPOWERED_BANNER).replace("\n", "<br>")))
    pairs = report["pairs"]
    if not pairs:
        return head + ('<p class="big">%d end(s) logged, 0 successors yet. The paired '
                       'dots appear once a logged sitting is picked up again.</p>'
                       % report["ends_logged"])
    if half == "dots":
        return head + _screen_table(
            ["where", "n NOTE", "n NONE", "NOTE startup", "NONE startup", "difference"],
            [(short_path(p["cwd"] or "", 30),
              p["note_successors"], p["none_successors"],
              _thousands(p["note_orientation_tokens"]),
              _thousands(p["none_orientation_tokens"]),
              ("-" if p["difference"] < 0 else "+") + _thousands(abs(p["difference"])))
             for p in sorted(pairs, key=lambda p: p["difference"])], cap=22)
    w, s_ = report["wilcoxon"], report["sign_test"]
    m, nv = report["mediator"], report["net_value"]
    return head + _screen_table(
        ["where", "note min", "saved $", "note cost $", "net $"],
        [(short_path(p["cwd"] or "", 30),
          "—" if p["note_minutes"] is None else "%.0f" % p["note_minutes"],
          "—" if p["saved_usd"] is None else "%.2f" % p["saved_usd"],
          "—" if p["note_cost_usd"] is None else "%.2f" % p["note_cost_usd"],
          "—" if p["net_usd"] is None else "%+.2f" % p["net_usd"])
         for p in sorted(pairs, key=lambda p: -(p["net_usd"] if p["net_usd"] is not None else -1e9))],
        cap=18) + _screen_table(
        ["statistic", "value"],
        [("Wilcoxon signed-rank (primary, two-sided a=0.05)",
          "n=%s  W=%s  p=%s" % (w["n"], w.get("w"), w.get("p"))),
         ("sign test (robustness, ties dropped)",
          "note won %s of %s, p=%s" % (s_["wins"], s_["n"], s_.get("p"))),
         ("note read in the first two turns",
          "%d/%d  (Wilson 90%% CI %s-%s)" % (m["opened_by_turn_2"], m["note_arm_successors"],
                                             m["wilson90"][0], m["wilson90"][1])),
         ("net value (median)", "$%s over %d pair(s)" % (nv["median_net_usd"],
                                                         nv["pairs_with_net"])),
         ("$/minute for note time", "%.4f" % (report["minute_rate_usd"] / 60.0))], cap=6)


def _screen_index(titles: Sequence[Tuple[str, str]], when: str, mode: str) -> str:
    items = "".join('<li><span class="k">%02d</span> <a href="%s">%s</a></li>'
                    % (index, name, esc(title))
                    for index, (name, title) in enumerate(titles, 1))
    pages = json.dumps([name for name, _ in titles])
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Screen pack — %(when)s</title><style>%(css)s
body{overflow:auto} .page{height:auto;min-height:%(h)dpx}
a{color:#111} button{font:inherit;font-size:20px;padding:8px 16px;margin-right:10px;
 border:2px solid #111;background:#fff;cursor:pointer}
</style></head><body><div class="page">
<div class="band"><span class="n">PACK</span><span>Every page, in order</span>
<span class="d">%(when)s</span></div>
<p class="big">%(mode)s</p>
<p class="big">Open page 01, press <b>F</b> for full screen, then press
<b>&rarr;</b> for the next. Each page fits one screen with no scrolling.</p>
<button onclick="go(0)">start at page 01</button>
<h2>Pages</h2><ul>%(items)s</ul></div>
<script>var P=%(pages)s;function go(i){location.href=P[i]+'#p='+i;}
document.addEventListener('keydown',function(e){
 var m=/#p=(\\d+)/.exec(location.hash); var i=m?parseInt(m[1]):-1;
 if(e.key==='ArrowRight'&&i+1<P.length)go(i+1);
 if(e.key==='ArrowLeft'&&i>0)go(i-1);
 if(e.key==='f'||e.key==='F'){var d=document.documentElement;
  if(document.fullscreenElement)document.exitFullscreen();else d.requestFullscreen();}});
</script></body></html>""" % {"css": SCREEN_CSS, "h": SCREEN_H, "when": esc(when),
                              "mode": esc(mode), "items": items, "pages": pages})


MD_MEASURE_LABELS = [
    ("sessions", "sessions"),
    ("sessions_with_boundary", "sessions that made an edit"),
    ("sessions_no_mutation", "no-edit sessions"),
    ("median_orientation_share", "median startup share"),
    ("median_orientation_share_notes_opened", "median startup share, read a handoff note"),
    ("median_orientation_share_no_notes", "median startup share, no note read"),
    ("handoff_opened_sessions", "sessions that read a handoff note"),
    ("median_orientation_share_readme_opened", "median startup share, read a README first"),
    ("readme_opened_sessions", "sessions that read a README first"),
    ("median_orientation_share_pooled_notes", "median startup share, note or README"),
    ("notes_opened_sessions_pooled", "sessions that read a note or README"),
    ("orientation_tokens_total", "startup tokens"),
    ("orientation_cost_usd_total", "startup cost ($)"),
    ("orientation_cost_share", "startup share of est. cost"),
    ("tokens_total", "tokens"),
    ("est_cost_usd_total", "est. cost ($)"),
    ("streams", "work items"),
    ("multi_episode_streams", "work items with more than one sitting"),
    ("stream_tax_tokens", "repeat startup incl. long breaks (tokens)"),
    ("stream_tax_usd", "repeat startup incl. long breaks ($)"),
]


def markdown_report(payload: Dict[str, Any]) -> str:
    """The whole report in ONE Markdown file, in the order the page set reads:
    answer, definitions, the two questions, pick-up cost, the ledger index,
    then the appendix. Not split: a Markdown file is read in a terminal or
    pasted into a ticket, where a link to another file is a dead end."""
    agg = payload["aggregates"]
    streams = payload.get("streams") or []
    lines = ["# Where your sessions spend tokens before their first edit", "",
             "Every dollar is an **estimate at list API rates**, for comparing sessions, never "
             "a bill. Price table: `%s`." % payload["scope"].get("price_table"), "",
             payload["summary"], "",
             "## Definitions", "",
             "| term | what it is | which way is good | how to read it |", "|---|---|---|---|"]
    for name, sentence, direction, band in DEFINITIONS:
        lines.append("| **%s** | %s | %s | %s |" % (name, sentence, direction, band))

    lines += ["", "## Where the startup money went", "",
              "Startup cost prices each response before the first edit at its own model's "
              "rates, not the session's average.", "",
              "| where | when | model | startup $ | of est. $ | share | read a note? |",
              "|---|---|---|---:|---:|---:|---|"]
    ranked = sorted(payload["sessions"],
                    key=lambda r: -(r.get("orientation_cost_usd") or 0.0))
    for record in ranked[:10]:
        share = record.get("orientation_share")
        if share is None:
            continue
        cost = record.get("est_cost_usd") or 0.0
        lines.append("| %s | %s | %s | %.2f | %.2f | %.0f %% | %s |"
                     % (short_path(record.get("cwd") or record["cwd_basename"], 40),
                        (record.get("start") or "")[:10],
                        (real_models(record.get("models") or []) or ["—"])[0],
                        record.get("orientation_cost_usd") or 0.0, cost, 100 * share,
                        "yes" if record.get("handoff_opened_by_turn_2") else "—"))

    lines += ["", "## Work that comes back", "",
              "A **sitting** is sessions with no gap over 72 hours. A **work item** is the same "
              "folder, branch or ticket, however long the break. Repeat startup incl. long "
              "breaks counts every session after the work item's first.", "",
              "| work item | grouped by | sittings | sessions | breaks (days) | repeat startup "
              "incl. long breaks | est. $ | handoff notes |",
              "|---|---|---:|---:|---|---:|---:|---|"]
    for record in sorted(streams, key=lambda r: -r["reorientation_tax_tokens"])[:20]:
        lines.append("| %s | %s | %d | %d | %s | %s | %.2f | %s |"
                     % (record["name"], record["named_by"], record["episodes"],
                        record["sessions"],
                        ", ".join("%.0f" % g for g in record["gaps_days"][:4]) or "—",
                        compact_number(record["reorientation_tax_tokens"]),
                        record["reorientation_tax_usd"], _handoff_pattern(record)))
    question = _question_first_sentence(agg)
    if question:
        lines += ["", question]

    # Always printed, including when there is nothing to compare: "no work item
    # did both" is a result about this history, and leaving the section out
    # would read as the analysis not having been run.
    continuations = payload.get("continuations") or {}
    if continuations:
        summary = payload.get("continuation_summary") or {}
        unknown = continuations["restarts_without_boundary"]
        lines += ["", "## Pick-up cost: resuming a session, or starting a new one", "",
                  "Rule: %s" % continuations["rule"], "",
                  "| pick-up | pick-ups | median tokens | median est. $ | median gap (h) "
                  "| total tokens |", "|---|---:|---:|---:|---:|---:|"]
        for kind in ("R0", "R1", "R2"):
            row = summary.get(kind) or {}
            lines.append("| %s | %d | %s | %s | %s | %s |"
                         % (CONTINUATION_LABEL[kind], row.get("events") or 0,
                            compact_number(row.get("median_tokens"))
                            if row.get("median_tokens") is not None else "—",
                            ("%.2f" % row["median_cost_usd"])
                            if row.get("median_cost_usd") is not None else "—",
                            ("%.1f" % row["median_gap_hours"])
                            if row.get("median_gap_hours") is not None else "—",
                            compact_number(row.get("total_tokens") or 0)))
        lines += ["",
                  "%d of %d work item(s) with a pick-up did both; the person chose the route, "
                  "so this is not a fair test. Not counted: %d new session(s) that never made "
                  "an edit (%s tokens) and %d session(s) that ran in parallel with an earlier one."
                  % (len(continuations["paired_streams"]), continuations["streams_with_events"],
                     unknown["sessions"], compact_number(unknown["tokens"]),
                     continuations.get("parallel_sessions") or 0)]

    lines += ["", "## The session ledger", "",
              "One row per session in `sessions.jsonl`; each analysis adds its own columns "
              "and never drops another's. Schema: `references/session-ledger.md`.", "",
              "| analysis | question | ledger columns | status |", "|---|---|---|---|"]
    ran = payload.get("ledger_runs") or {}
    for key, title, question_, command, adds in ANALYSIS_INDEX:
        producer = INDEX_PRODUCER.get(key)
        lines.append("| %s | %s | %s | %s |"
                     % (title, question_, adds,
                        "in this report" if key in ("orientation", "streams", "ledger")
                        else "in the ledger, ran %s" % ran[producer] if producer in ran
                        else "prints its own report — `%s`" % command
                        if key in PRINTS_OWN_REPORT
                        else "not run — `%s`" % command))

    lines += ["", "---", "", "# Appendix", "",
              "Method, checks and full tables. Nothing here changes the answer above.", "",
              "## A1 · Startup share across sessions", "",
              NO_THRESHOLD_NOTE, "",
              "**What this shows.** " + " ".join(STARTUP_SHOWS), "",
              "**What it doesn't show.** " + " ".join(STARTUP_NOT_SHOWN), "",
              "| measure | value |", "|---|---|"]
    for key, label in MD_MEASURE_LABELS:
        value = agg.get(key)
        if key.endswith("tokens_total") or key.endswith("_tokens"):
            value = compact_number(value or 0)          # 33.9M, not 33860469
        lines.append("| %s | %s |" % (label, value))

    lines += ["", "## A2 · Scripted or by hand", "",
              "Read from the tool calls, not self-reported. `<synthetic>` is not a model and "
              "is left out.", "",
              "| strategy | sessions | median est. $ |", "|---|---:|---:|"]
    for strategy, count in sorted(agg["strategy_counts"].items()):
        lines.append("| %s | %d | %s |" % (strategy, count,
                                           agg["median_cost_by_strategy"].get(strategy, "—")))
    lines += ["", "## A3 · Effort, and whether reasoning volume agrees", "",
              "Effort is a recorded field on each assistant record; older CLI versions omit "
              "it. Recorded on %s of turns."
              % ("%.0f%%" % (100 * (agg.get("effort_recorded_share") or 0))), "",
              "| effort | turns | sessions | median est. $ |", "|---|---:|---:|---:|"]
    for level, turns in (agg.get("effort_turns") or {}).items():
        lines.append("| %s | %d | %s | %s |" % (
            level, turns, (agg.get("sessions_by_effort") or {}).get(level, 0),
            (agg.get("median_cost_by_effort") or {}).get(level, "—")))
    lines += ["", "| model | median thinking share |", "|---|---:|"]
    for model, value in agg["median_thinking_share_by_model"].items():
        lines.append("| %s | %s |" % (model, value))

    lines += ["", "## A4 · How sittings and work items were built", ""]
    rule = payload["scope"].get("thread_rule") or {}
    stream_rule = payload.get("stream_rule") or {}
    lines += ["- sitting rule: same folder, starts within %s of the sitting's last session, "
              "at least %d shared file names, at most %d sessions"
              % (hours_text(rule.get("gap_hours")), rule.get("min_shared"),
                 rule.get("max_sessions")),
              "- work-item rule (`%s`): %s" % (stream_rule.get("stream_by"),
                                               stream_rule.get("rule_note")),
              "- %s work item(s) from %s sitting(s); joins by rule: %s"
              % (stream_rule.get("streams"), stream_rule.get("threads_in"),
                 ", ".join("%s %d" % (k, v)
                           for k, v in (stream_rule.get("merges_by_rule") or {}).items())
                 or "none"),
              "- measured precision: not measured on this machine; calibrate with "
              "`threads --labels`.",
              "- if one rule joins too much (a generic `#1` will), switch it off with "
              "`--stream-ignore-token`."]

    lines += ["", "## A5 · Scope", ""]
    lines += ["- " + line for line in scope_block(payload["scope"])]

    handoff = payload.get("handoff")
    if handoff:
        lines += ["", "## A6 · The handoff experiment", "",
                  "Log format and read-back rules: `references/orientation-and-threads.md` "
                  "§2b.", "",
                  "- pairing: %s" % handoff["pair_definition"],
                  "- rate: %s" % handoff["rate_note"],
                  "- %d end(s) logged · %d skipped as malformed · %d pending (no successor "
                  "yet, not a data point) · %d unmatched · %d successor(s) joined"
                  % (handoff["ends_logged"], handoff["lines_skipped"], handoff["pending_ends"],
                     handoff["unmatched_ends"], handoff["successor_rows"]),
                  "- **%d usable pair(s)**" % handoff["usable_pairs"]]
        for row in handoff["skipped"]:
            lines.append("- skipped line %d: %s — `%s`" % (row["line"], row["reason"], row["text"]))
        if handoff["underpowered"]:
            lines += ["", "> **Underpowered: %d usable pair(s), floor is %d.**"
                      % (handoff["usable_pairs"], MIN_USABLE_PAIRS), ">"]
            lines += ["> " + line for line in UNDERPOWERED_BANNER.splitlines()]
        if handoff["pairs"]:
            lines += ["", "| where | n NOTE | n NONE | NOTE startup | NONE startup | "
                          "difference | note min | net $ |",
                      "|---|---:|---:|---:|---:|---:|---:|---:|"]
            for pair in sorted(handoff["pairs"], key=lambda p: p["difference"]):
                lines.append("| %s | %d | %d | %s | %s | %s | %s | %s |" % (
                    short_path(pair["cwd"] or "", 40), pair["note_successors"],
                    pair["none_successors"], compact_number(pair["note_orientation_tokens"]),
                    compact_number(pair["none_orientation_tokens"]),
                    ("-" if pair["difference"] < 0 else "+")
                    + compact_number(abs(pair["difference"])),
                    "—" if pair["note_minutes"] is None else "%.0f" % pair["note_minutes"],
                    "—" if pair["net_usd"] is None else "%+.2f" % pair["net_usd"]))
            w, s = handoff["wilcoxon"], handoff["sign_test"]
            lines += ["", "- **Wilcoxon signed-rank** (primary, two-sided α = 0.05): "
                          "n = %s, W = %s, p = %s — %s" % (w["n"], w.get("w"), w.get("p"),
                                                           w.get("method")),
                      "- **Sign test** (robustness, ties dropped): the note won %s of %s, p = %s"
                      % (s["wins"], s["n"], s.get("p"))]
        m = handoff["mediator"]
        lines += ["", "- **Note read in the first two turns**: %d/%d%s (Wilson 90 %% CI %s–%s)"
                  % (m["opened_by_turn_2"], m["note_arm_successors"],
                     "" if m["rate"] is None else " = %.0f %%" % (100 * m["rate"]),
                     m["wilson90"][0], m["wilson90"][1])]
        if m["below_floor"]:
            lines.append("  - **Below the 80 %% floor**: %s" % m["branch"])
        nv = handoff["net_value"]
        lines += ["- **Net value**: median $%s over %d pair(s) (%d positive, %d negative); "
                  "%d pair(s) left out because %s"
                  % (nv["median_net_usd"], nv["pairs_with_net"], nv["positive"], nv["negative"],
                     nv["pairs_without_minutes"], nv["caveat"])]

    lines += ["", "## A7 · Sittings", "",
              "| folder | sessions | tools | days | repeat startup | share | est. $ |",
              "|---|---:|---|---:|---:|---:|---:|"]
    for record in sorted(payload["threads"], key=lambda r: -r["reorientation_tax_tokens"])[:40]:
        lines.append("| %s | %d | %s | %s | %s | %s | %.2f |" % (
            short_path(record.get("cwd") or "", 48), record["sessions"],
            ", ".join(record["tools"]), record.get("elapsed_days"),
            compact_number(record["reorientation_tax_tokens"]),
            record.get("reorientation_tax_share"), record["est_cost_usd"]))

    lines += ["", "## A8 · The price table", "",
              "| model | $ / M input | $ / M output |", "|---|---:|---:|"]
    for model, rate in price_rows(_ACTIVE_PRICES):
        lines.append("| %s | %.2f | %.2f |" % (model, rate[0], rate[1]))
    lines += ["", "Cache reads cost %.2f× input, 5-minute writes %.2f× and 1-hour writes "
              "%.2f×. An unpriced model lowers coverage and is never charged at another "
              "model's rate. Copilot stores record AI credits, usually not tokens, so their "
              "sessions carry little or no est. cost — see `copilot_sessions` in "
              "`references/pricing-public-example.json`."
              % (CACHE_READ_MULT, CACHE_WRITE_5M_MULT, CACHE_WRITE_1H_MULT), "",
              "The usage meter is not in this report: API billing has no 5-hour or weekly "
              "limit. `orientation_profile.py meter` shows it on a subscription plan.", ""]
    return "\n".join(lines)


# ==========================================================================
# CLI
# ==========================================================================
# The session ledger's DEFAULT view. Ten columns, in reading order:
# which session, when, where, which piece of work, what it cost, how much of
# that was orientation, did it open a note, was it asked a question first, did
# it change anything, how did it end. Every other column stays in the DOM
# behind "show all columns", and every column is always in sessions.jsonl.
LEDGER_DEFAULT_COLUMNS = [
    "session_id", "start", "cwd_basename", "stream_name", "est_cost_usd",
    "orientation_share", "orientation_position", "handoff_opened_by_turn_2",
    "question_first", "outcome",
]
# Columns that must never wrap: a timestamp broken over three lines triples
# the row height and pushes the columns that follow it off the screen.
LEDGER_NOWRAP_COLUMNS = {"session_id", "start", "end", "thread_id", "stream_id"}
LEDGER_NUMERIC_COLUMNS = {
    "turns", "total_tokens", "orientation_tokens", "orientation_share",
    "orientation_breadth", "orientation_tokens_per_path", "est_cost_usd",
    "orientation_cost_usd", "carry_tokens", "carry_cost_usd", "gap_hours_before",
    "thinking_share", "cache_hit_ratio", "cache_cold_events",
    "tool_result_bytes", "top_tool_bytes", "compactions",
    "tokens_before_compact", "tokens_after_compact", "fat_context_read_share",
}


def ledger_rows(sessions: Sequence["Session"], threads: Sequence["Thread"],
                streams: Sequence["Stream"], redact: bool,
                continuations: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The orientation columns of the session LEDGER, one row per session.

    These are the FIRST columns of `sessions.jsonl`; every other analysis in
    the skill upserts its own beside them. Thread and stream ids travel with
    the row so a reader can sort the browser by stream and see a month of
    returns to one piece of work on adjacent lines.
    """
    thread_of = {s.session_id: t.id for t in threads for s in t.sessions}
    stream_of: Dict[str, "Stream"] = {}
    for stream in streams:
        for session in stream.sessions:
            stream_of[session.session_id] = stream
    carry_of = (continuations or {}).get("per_session") or {}
    rows: List[Dict[str, Any]] = []
    for session in sessions:
        m = session.metrics
        stream = stream_of.get(session.session_id)
        title = session.title or session.first_prompt
        carry = carry_of.get(session.session_id) or {}
        rows.append({
            "session_id": (stable_hash(session.session_id) if redact else session.session_id),
            "source": session.source,
            "start": iso(session.start),
            "end": iso(session.end),
            "cwd": (stable_hash(session.cwd) if redact else session.cwd),
            "cwd_basename": (scrub(session.cwd_base) if redact else session.cwd_base),
            "title": None if redact else one_line(title, 120),
            "models": session.models,
            "turns": m.get("turns"),
            "total_tokens": m.get("total_tokens"),
            "orientation_tokens": m.get("orientation_tokens"),
            "orientation_share": m.get("orientation_share"),
            "orientation_position": _band_of(m.get("orientation_share")),
            "orientation_breadth": m.get("orientation_breadth"),
            # Named in references/session-ledger.md and written here, so the
            # ledger's dollars are not the whole-session figure only: the
            # carry-or-restart comparison puts a restart's price beside a
            # resume's.
            "orientation_cost_usd": m.get("orientation_cost_usd"),
            # `carry_*` is the cost of the continuation named by
            # `continuation_kind`; the full event list is in orientation.json.
            "continuation_kind": carry.get("continuation_kind"),
            "carry_tokens": carry.get("carry_tokens"),
            "carry_cost_usd": carry.get("carry_cost_usd"),
            "gap_hours_before": carry.get("gap_hours_before"),
            "compactions": carry.get("compactions"),
            "handoff_opened_by_turn_2": m.get("handoff_opened_by_turn_2"),
            "readme_opened_by_turn_2": m.get("readme_opened_by_turn_2"),
            "question_first": m.get("question_first"),
            "strategy": m.get("strategy"),
            "effort": m.get("effort"),
            "thinking_share": m.get("thinking_share"),
            "outcome": m.get("outcome"),
            "est_cost_usd": m.get("est_cost_usd"),
            "thread_id": thread_of.get(session.session_id),
            "stream_id": stream.id if stream else None,
            "stream_name": ((stable_hash(stream.name) if redact else stream.name)
                            if stream else None),
            "stream_named_by": stream.named_by if stream else None,
        })
    return [{key: home_relative(value) for key, value in row.items()} for row in rows]


def write_ledger(path: str, sessions: Sequence["Session"], threads: Sequence["Thread"],
                 streams: Sequence["Stream"], redact: bool,
                 scope: Optional[str] = None,
                 continuations: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Upsert the orientation columns, then REGISTER every other producer's.

    Registering the columns this run did not compute is the point of the
    index: a column that exists with no values is an analysis you have not run
    here, which reads very differently from a column of zeroes.
    """
    try:
        import session_ledger
    except Exception as exc:                       # never take a report down for this
        print("ledger skipped: %s: %s" % (exc.__class__.__name__, exc), file=sys.stderr)
        return None
    rows = ledger_rows(sessions, threads, streams, redact, continuations)
    stats = session_ledger.upsert(path, rows, "orientation_profile.py",
                                  sorted(session_ledger.REGISTRY["orientation_profile.py"]),
                                  scope=home_relative(scope))
    for producer in session_ledger.REGISTRY:
        if producer != "orientation_profile.py":
            session_ledger.register(path, producer)
    stats["headline"] = session_ledger.headline(path)
    return stats


def ledger_browser(path: Optional[str],
                   in_scope: Optional[Sequence[str]] = None) -> str:
    """The report's session browser, read back OUT of the ledger.

    Deliberately not rendered from the in-memory sessions: the browser shows
    whatever columns are in the ledger, including the ones other analyses put
    there, which is the whole reason the ledger exists. Every header carries a
    provenance tooltip naming the script that wrote the column.
    """
    if not path:
        return ('<p class="axis">No ledger was written for this run. Add '
                '<code>--ledger OUT/sessions.jsonl</code> to build the table every analysis '
                'adds columns to.</p>')
    try:
        import session_ledger
        rows, provenance = session_ledger.load(path)
        stats = session_ledger.summary(path)
    except Exception as exc:
        return ('<p class="axis">The ledger at %s could not be read (%s).</p>'
                % (esc(path), esc(exc.__class__.__name__)))
    if not rows:
        return '<p class="axis">The ledger at %s is empty.</p>' % esc(path)
    # SCOPE. The ledger is a union: `tool_bloat.py --since ...` and
    # `orientation_profile.py --exclude-cwd-pattern ...` see different session sets,
    # so a browser that showed every row would silently widen this report's
    # own scope. Show this run's sessions; say how many others are in the file
    # and which analysis put them there.
    out_of_scope = 0
    if in_scope is not None:
        keep = set(in_scope)
        out_of_scope = sum(1 for key in rows if key not in keep)
        rows = {k: v for k, v in rows.items() if k in keep}
        if not rows:
            return ('<p class="axis">The ledger at %s holds %d session(s), none of them from '
                    'this run\'s scope.</p>' % (esc(path), out_of_scope))
    columns = [c for c in provenance.get("columns", {}) if stats["coverage"].get(c)]
    # The session id is the ledger's KEY, so it is not one of the provenance
    # columns -- but without it a row written by an analysis that contributed
    # no other column renders as a line of dashes, which is unreadable and
    # unjoinable. Put it first.
    columns = ["session_id"] + columns
    # A ledger with every column at once is not readable, so the DEFAULT is
    # at most ten: who, when, where, what it cost,
    # how much of it was orientation, did it open a note, did it change
    # anything, how did it end. Everything else is in the DOM but hidden
    # behind "show all columns", and the full row is always in sessions.jsonl.
    lead = [c for c in LEDGER_DEFAULT_COLUMNS if c in columns]
    rest = [c for c in columns if c not in lead]
    ordered = lead + rest
    hidden = set(rest)
    def _tip(column: str) -> str:
        meta = provenance.get("columns", {}).get(column)
        if not meta:                       # session_id: the key, not a column
            return "%s — the ledger key every analysis joins on" % column
        return "%s — written by %s%s%s" % (
            column, meta.get("producer") or "?",
            (", " + meta["updated"]) if meta.get("updated") else "",
            ("\n" + meta["definition"]) if meta.get("definition") else "")

    def _class(column: str) -> str:
        bits = []
        if column in hidden:
            bits.append("x")
        # Text left, numbers right — the ledger is read by scanning a column
        # of figures, and a ragged right edge defeats that.
        bits.append("r" if column in LEDGER_NUMERIC_COLUMNS else "l")
        if column in LEDGER_NOWRAP_COLUMNS:
            bits.append("nw")          # a timestamp wrapped over three lines
        return " ".join(bits)

    header = "".join('<th class="%s" data-tip="%s">%s</th>'
                     % (_class(c), esc(_tip(c)), esc(c)) for c in ordered)
    body = ""
    for row in sorted(rows.values(), key=lambda r: -(r.get("est_cost_usd") or 0))[:400]:
        cells = ""
        for column in ordered:
            value = row.get(column)
            if isinstance(value, float):
                text = "%.3f" % value
            elif isinstance(value, bool):
                text = "yes" if value else "—"
            elif isinstance(value, list):
                text = ", ".join(str(v) for v in value)[:40]
            else:
                text = "—" if value is None else str(value)
            # Two columns are wide enough to squeeze every other column out of
            # view: a 36-character UUID wraps to four lines, and an ISO stamp
            # carries microseconds nobody reads. Both are SHORTENED for
            # display only -- the full value stays in data-v, so sorting,
            # the tooltip and sessions.jsonl are unaffected.
            if column == "continuation_kind" and value in CONTINUATION_LABEL:
                value = text = CONTINUATION_LABEL[value]   # machine value -> words
            if column == "session_id" and len(text) > 12:
                text = text[:8] + "…"
            elif column in ("cwd", "path"):
                text = short_path(text, 60)        # cut at a separator, not mid-name
            elif (column.endswith("_tokens") and isinstance(value, int)
                  and not isinstance(value, bool)):
                text = compact_number(value)       # 33.9M, not 33860469
            elif column in ("start", "end") and len(text) >= 16:
                text = text[:10] + " " + text[11:16]
            cells += "<td class='%s' data-v='%s' data-tip='%s'>%s</td>" % (
                _class(column),
                esc(value if not isinstance(value, (list, dict)) else text),
                esc("%s: %s" % (column, value)), esc(text[:60]))
        body += "<tr>%s</tr>" % cells
    not_run = stats["registered_not_run"]
    scopes = "".join(
        "<li><code>%s</code> — %s (%s row(s), %s)</li>"
        % (esc(producer), esc(meta.get("scope") or "unrecorded"),
           esc(meta.get("rows_written")), esc((meta.get("at") or "")[:16]))
        for producer, meta in sorted((stats.get("scopes") or {}).items()))
    scope_note = ""
    if out_of_scope:
        scope_note = ('<p class="note">Showing the %d session(s) in this report. The file '
                      'also holds %d session(s) written by another analysis with a different '
                      'filter:<ul>%s</ul></p>' % (len(rows), out_of_scope, scopes))
    elif scopes:
        scope_note = ('<details class="method"><summary>Filters each analysis ran under'
                      '</summary><ul>%s</ul></details>' % scopes)
    note = (scope_note
            + "<p class=\"note\">%s. Hover a header to see which script wrote it.%s</p>"
            % (esc(session_ledger.headline(path)),
               (" %d column(s) are registered but not run here: empty, not zero."
                % len(not_run)) if not_run else ""))
    toggle = ('<button class="colbtn" data-colstoggle="ledgertable">show all columns</button> '
              '<span class="axis" style="font-size:12.5px;color:var(--muted)">'
              '%d shown, %d hidden</span>' % (len(ordered) - len(hidden), len(hidden)))
    return (note + toggle
            + '<div class="scroll tall"><table id="ledgertable" class="sortable cols"><thead><tr>%s'
              '</tr></thead><tbody>%s</tbody></table></div>' % (header, body))


def build_payload(args, sessions: Sequence[Session], threads: Sequence[Thread],
                  store_stats: Sequence[Dict[str, Any]], prices_id: str,
                  streams: Sequence["Stream"] = (),
                  stream_stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    redact = bool(getattr(args, "redact", False))
    agg = aggregate(sessions, threads)
    thread_records = [thread_record(t, redact) for t in threads]
    stream_records = [stream_record(s, redact) for s in streams]
    # Stream-level aggregates live here rather than in aggregate(), because
    # streams are built one level up from threads and aggregate() is also
    # called on its own by `sessions`.
    agg["streams"] = len(stream_records)
    agg["multi_episode_streams"] = sum(1 for r in stream_records if r["episodes"] > 1)
    agg["stream_tax_tokens"] = sum(r["reorientation_tax_tokens"] for r in stream_records)
    agg["stream_tax_usd"] = round(sum(r["reorientation_tax_usd"] for r in stream_records), 2)
    events = sum(s.metrics.get("turns", 0) for s in sessions)
    duplicates = sum(s.duplicate_rows for s in sessions)
    # Token-WEIGHTED, so it matches the other analyses. A mean of per-session
    # coverages let one tiny unpriced session outweigh a huge priced one.
    priced_tok = sum(s.metrics.get("priced_tokens") or 0 for s in sessions)
    priceable_tok = sum(s.metrics.get("priceable_tokens") or 0 for s in sessions)
    coverage_fraction = (priced_tok / priceable_tok) if priceable_tok else None
    scope = {
        "generated": iso(dt.datetime.now(dt.timezone.utc)),
        "schema_version": SCHEMA_VERSION,
        "since": getattr(args, "since", None),
        "until": getattr(args, "until", None),
        "stores": sorted({s.source for s in sessions}) or list(getattr(args, "store", None) or STORES),
        "roots": [str(st.get("root") or st.get("roots")) for st in store_stats],
        "include_cwd": ", ".join(getattr(args, "include_cwd_pattern", []) or []),
        "exclude_cwd": ", ".join(getattr(args, "exclude_cwd_pattern", []) or []),
        "events": events, "duplicates": duplicates,
        "price_table": prices_id,
        "price_coverage": ("coverage %.0f %%" % (100 * coverage_fraction))
                          if coverage_fraction is not None else None,
        "price_coverage_fraction": (round(coverage_fraction, 3)
                                    if coverage_fraction is not None else None),
        "price_coverage_caveat": (
            None if coverage_fraction is None or coverage_fraction >= audit_common.COVERAGE_FLOOR
            else audit_common.coverage_caveat(coverage_fraction)),
        # Sessions with no token counts are unpriced: no est. cost, out of
        # every median and total, and named beside the coverage figure.
        "sessions_without_token_counts": sum(
            1 for s in sessions if not s.metrics.get("total_tokens")),
        "redact": redact,
        "thread_rule": {"gap_hours": args.gap_hours, "min_shared": args.min_shared,
                        "max_sessions": args.max_sessions},
        "stream_rule": (stream_stats or {}).get("rule_note"),
        "stream_by": (stream_stats or {}).get("stream_by"),
        "boundary_rule": ("first Edit/Write/MultiEdit/NotebookEdit call, or a Bash command matching "
                          "one of %d named write-ish verbs at command position, or an "
                          "unquoted redirection" % len(BASH_MUTATE_VERBS)),
        # The SUBTITLE names the machine, not a truncated list of
        # directories. The directories are on the cohort table below it, in
        # full, where they can be read rather than guessed at. The default is
        # deliberately generic: no hostname, so a shared report does not name
        # the computer it came from. --label overrides it.
        "label": (getattr(args, "label", None) or "this machine"),
        # Under --redact a basename can itself be a GUID, so it goes through
        # the same scrubber as every other exported string.
        "directories": sorted({(scrub(s.cwd_base) if redact else s.cwd_base)
                               for s in sessions}),
        # What the scope filter kept and what it removed, pattern by pattern.
        # A cohort presented as "your real working history" has to publish its
        # own inclusion rule, or synthetic scratch runs hide inside it unseen.
        "cohort": {
            "included_sessions": len(sessions),
            "excluded_by_pattern": [row for st in store_stats
                                    for row in (st.get("excluded_by_pattern") or [])],
        },
    }
    # Computed for every report — the columns are cheap and the
    # ledger is the skill's analysis index, so a column that exists with no
    # values has to mean "not run", not "not computed".
    continuations = classify_continuations(
        sessions, streams, _ACTIVE_PRICES,
        float(getattr(args, "resume_gap_hours", RESUME_GAP_HOURS) or RESUME_GAP_HOURS))
    agg["continuation_events"] = len(continuations["events"])
    agg["continuation_counts"] = continuations["counts"]
    agg["paired_streams"] = len(continuations["paired_streams"])
    payload = {
        "scope": scope,
        "store_stats": [dict(st) for st in store_stats],
        "aggregates": agg,
        "continuations": continuations,
        "continuation_summary": continuation_summary(continuations),
        "meter": meter_report(sessions, getattr(args, "bucket_hours", 5.0)),
        "threads": thread_records,
        "streams": stream_records,
        "stream_rule": stream_stats or {},
        "sessions": [session_record(s, redact) for s in sessions],
    }
    if getattr(args, "handoff_log", None):
        entries, skipped = parse_handoff_log(args.handoff_log)
        joined = join_handoff_log(entries, threads, redact)
        payload["handoff"] = handoff_report(joined, skipped, entries,
                                            getattr(args, "minute_rate_usd", 100.0))
        # The field CSV can carry project names. The joined
        # per-successor rows stay OUT of an export; the aggregates may travel.
        payload["handoff_rows"] = [] if redact else joined["rows"]
    payload["summary"] = five_line_summary(agg, thread_records, scope, stream_records)
    if redact:
        # Store folders name the account: an export carries neither them nor a hash.
        scope["roots"] = []
        payload["store_stats"] = [{k: v for k, v in st.items() if k not in ("root", "roots")}
                                  for st in payload["store_stats"]]
        scope["label"] = None
        meter = payload["meter"]
        meter["readings"] = []
        meter["redaction_note"] = ("readings dropped under --redact: they carry session ids "
                                   "and local reset clock times")
    return payload


def _load_all(args):
    prices = load_prices(getattr(args, "pricing_file", None))
    table_id = prices["table_id"]
    global _ACTIVE_PRICES
    _ACTIVE_PRICES = prices
    sessions, store_stats = collect_sessions(args, prices)
    threads = build_threads(sessions, args.gap_hours, args.min_shared, args.max_sessions,
                            verbose_caps=getattr(args, "explain_caps", False))
    apply_thread_handoffs(threads, prices)
    streams, stream_stats = build_streams(threads,
                                          getattr(args, "stream_by", "auto") or "auto",
                                          getattr(args, "stream_ignore_token", None) or [])
    return prices, table_id, sessions, store_stats, threads, streams, stream_stats


def print_handoff(report: Dict[str, Any]) -> None:
    """The handoff-note read-back, printed so it cannot be read as more than it is.

    Order is deliberate: what was thrown away, then what is still pending,
    then n, and only then a number. A reader who stops after four lines has
    read the honest part.
    """
    print("Handoff-note read-back — does writing a short handover note pay for itself?")
    print("  format    references/orientation-and-threads.md §2b (the CSV and the read-back rules)")
    print("  pairing   %s" % report["pair_definition"])
    print("  rate      %s" % report["rate_note"])
    print()
    print("  %-34s %d" % ("session ends logged", report["ends_logged"]))
    print("  %-34s %d%s" % ("log lines skipped as malformed", report["lines_skipped"],
                            "  (listed below)" if report["lines_skipped"] else ""))
    print("  %-34s %d   (a real sitting, no successor yet: not a data point)"
          % ("PENDING ends", report["pending_ends"]))
    print("  %-34s %d   (no sitting in the history has that folder)"
          % ("UNMATCHED ends", report["unmatched_ends"]))
    print("  %-34s %d   (%s)" % ("successor sessions joined", report["successor_rows"],
                                 ", ".join("%s %d" % kv for kv in
                                           sorted(report["successors_by_arm"].items())) or "—"))
    print("  %-34s %d" % ("usable pairs (sittings, both arms)", report["usable_pairs"]))
    for row in report["skipped"]:
        print("    ! line %d skipped: %s" % (row["line"], row["reason"]))
        print("      %s" % row["text"])

    if report["underpowered"]:
        print("\n" + "-" * 78)
        print("UNDERPOWERED: %d usable pair(s), below the stated floor of %d."
              % (report["usable_pairs"], MIN_USABLE_PAIRS))
        print(UNDERPOWERED_BANNER)
        print("-" * 78)

    if not report["usable_pairs"]:
        print("\nNo pair yet, so there is no comparison to print. %d end(s) are logged and "
              "waiting\nfor a successor; keep logging — the CSV is the artefact and the "
              "read-back is\nretrospective." % report["pending_ends"])
        return

    print("\n%-22s %5s %5s %13s %13s %12s %9s %9s"
          % ("where", "n_NOT", "n_NON", "NOTE startup", "NONE startup",
             "difference", "note min", "net $"))
    for pair in sorted(report["pairs"], key=lambda p: p["difference"]):
        print("%-22s %5d %5d %13s %13s %12s %9s %9s" % (
            short_path(pair["cwd"] or "", 22),
            pair["note_successors"], pair["none_successors"],
            compact_number(pair["note_orientation_tokens"]),
            compact_number(pair["none_orientation_tokens"]),
            ("-" if pair["difference"] < 0 else "+") + compact_number(abs(pair["difference"])),
            "—" if pair["note_minutes"] is None else "%.0f" % pair["note_minutes"],
            "—" if pair["net_usd"] is None else "%+.2f" % pair["net_usd"]))

    w, s = report["wilcoxon"], report["sign_test"]
    print("\n  primary     Wilcoxon signed-rank on startup tokens, two-sided α = 0.05")
    print("              n = %d, W = %s, p = %s   [%s]%s"
          % (w["n"], w.get("w"), w.get("p"), w.get("method"),
             ("  (%d zero difference(s) dropped)" % w["zeros_dropped"]) if w["zeros_dropped"] else ""))
    print("  robustness  sign test, ties dropped, two-sided")
    print("              note won %d of %d, p = %s%s"
          % (s["wins"], s["n"], s.get("p"),
             ("  (%d tie(s) dropped)" % s["ties"]) if s["ties"] else ""))

    m = report["mediator"]
    print("\n  note read   in the first two turns: %d/%d = %s  (Wilson 90 %% CI %s–%s)"
          % (m["opened_by_turn_2"], m["note_arm_successors"],
             "n/a" if m["rate"] is None else "%.0f%%" % (100 * m["rate"]),
             m["wilson90"][0], m["wilson90"][1]))
    if m["below_floor"]:
        print("              BELOW THE %.0f %% FLOOR — %s" % (100 * m["floor"], m["branch"]))

    nv = report["net_value"]
    print("\n  net value   median $%s across %d pair(s) with a recorded note time "
          "(%d positive, %d negative)"
          % (nv["median_net_usd"], nv["pairs_with_net"], nv["positive"], nv["negative"]))
    print("              %d pair(s) excluded: %s" % (nv["pairs_without_minutes"], nv["caveat"]))
    print("              net = (startup tokens saved × that sitting's own $/token) "
          "− (note minutes × $%.4f)" % (report["minute_rate_usd"] / 60.0))


def cmd_sessions(args) -> int:
    (prices, table_id, sessions, store_stats, threads,
     streams, stream_stats) = _load_all(args)
    if getattr(args, "handoff_log", None):
        entries, skipped = parse_handoff_log(args.handoff_log)
        joined = join_handoff_log(entries, threads, bool(args.redact))
        report = handoff_report(joined, skipped, entries, args.minute_rate_usd)
        print_handoff(report)
        if args.json_out:
            write_output(
                args.json_out,
                json.dumps({"handoff": report, "joined": joined}, indent=1, default=str))
            print("\nwrote %s" % args.json_out)
        return 0
    if not sessions:
        print("no sessions matched")
        return 0
    redact = bool(args.redact)
    print("%-19s %-9s %6s %5s %9s %-9s %-7s %-4s %8s  %s"
          % ("start", "store", "startup", "turns", "tokens", "strategy", "effort", "note",
             "est.$", "where"))
    for record in sorted((session_record(s, redact) for s in sessions),
                         key=lambda r: -(r.get("est_cost_usd") or 0))[:args.top]:
        share = record.get("orientation_share")
        print("%-19s %-9s %6s %5s %9s %-9s %-7s %-4s %8s  %s" % (
            (record.get("start") or "")[:19], record["source"][:9],
            ("%.2f" % share) if share is not None else "none",
            record.get("turns"), compact_number(record.get("total_tokens") or 0),
            (record.get("strategy") or "")[:9], (record.get("effort") or "-")[:7],
            "yes" if record.get("handoff_opened_by_turn_2") else "-",
            "—" if record.get("est_cost_usd") is None else "%.3f" % record["est_cost_usd"],
            short_path(record.get("cwd") or record["cwd_basename"], 52)))
    agg = aggregate(sessions, threads)
    print()
    print(five_line_summary(agg, [thread_record(t, redact) for t in threads], {}))
    if args.json_out:
        payload = build_payload(args, sessions, threads, store_stats, table_id,
                                streams, stream_stats)
        write_output(args.json_out, json.dumps(payload, indent=1, default=str))
        print("\nwrote %s" % args.json_out)
    return 0


def cmd_threads(args) -> int:
    (prices, table_id, sessions, store_stats, threads,
     streams, stream_stats) = _load_all(args)
    redact = bool(args.redact)
    records = [thread_record(t, redact) for t in threads]
    if args.label_candidates:
        candidates = sorted(threads, key=lambda t: -len(t.sessions))[:args.label_candidates]
        print("# hand-label these: thread_id<TAB>yes|no|<k pieces of work>")
        print("# yes = one piece of work; no = at least two; k = exactly k")
        for thread in candidates:
            print("\n=== %s  %d sessions  %s → %s  cwd=%s"
                  % (thread.id, len(thread.sessions), iso(thread.start), iso(thread.end),
                     stable_hash(thread.cwd) if redact else thread.cwd))
            shared = sorted(set.intersection(*[resource_tokens(m) for m in thread.sessions])
                            if len(thread.sessions) > 1 else resource_tokens(thread.sessions[0]))
            print("    shared file names (%d): %s" % (len(shared), ", ".join(shared[:12])))
            for member in thread.sessions:
                print("    - %s %-13s %s | %s" % (
                    iso(member.start), member.source, member.session_id[:8],
                    "" if redact else (member.title or member.first_prompt or "(no prompt recorded)")[:110]))
            for note in thread.justifications:
                print("    ! cap override: %s" % note)
        return 0
    if args.labels:
        result = calibrate(threads, load_labels(args.labels), sessions,
                           args.gap_hours, args.min_shared, args.max_sessions)
        print(json.dumps(result, indent=1))
        return 0
    multi = [r for r in records if r["sessions"] > 1]
    print("%d sittings, %d with more than one session, %d across tools (rule: same folder, "
          "gaps up to %s, ≥%d shared file names, cap %d)" % (len(records), len(multi),
                                        sum(1 for r in records if r["cross_tool"]),
                                        hours_text(args.gap_hours), args.min_shared,
                                        args.max_sessions))
    print("\n%-6s %5s %8s %6s %10s %9s  %s" % ("id", "sess", "days", "tools", "repeat", "est.$", "where"))
    for record in sorted(multi, key=lambda r: -r["reorientation_tax_tokens"])[:args.top]:
        print("%-6s %5d %8s %6d %10s %9s  %s" % (
            record["thread_id"], record["sessions"], record.get("elapsed_days"),
            len(record["tools"]), compact_number(record["reorientation_tax_tokens"]),
            "%.2f" % record["est_cost_usd"],
            short_path(str(record.get("cwd")), 52)))
        for note in record["cap_justifications"]:
            print("       ! %s" % note)
    print("\n%d work item(s) over those sittings, %d of which recur (%s). Joins: %s"
          % (stream_stats["streams"], stream_stats["multi_episode_streams"],
             stream_stats["stream_by"],
             ", ".join("%s %d" % (k, v) for k, v in stream_stats["merges_by_rule"].items())
             or "none"))
    print("%-6s %-9s %8s %6s %8s %12s  %s"
          % ("id", "by", "sittings", "sess", "maxgap", "repeat+brk", "name"))
    for record in sorted((stream_record(s, redact) for s in streams),
                         key=lambda r: -r["reorientation_tax_tokens"])[:args.top]:
        print("%-6s %-9s %8d %6d %8s %12s  %s"
              % (record["stream_id"], record["named_by"], record["episodes"],
                 record["sessions"],
                 "—" if record["max_gap_days"] is None else "%.0fd" % record["max_gap_days"],
                 compact_number(record["reorientation_tax_tokens"]), str(record["name"])[:44]))
    if args.json_out:
        write_output(
            args.json_out,
            json.dumps(build_payload(args, sessions, threads, store_stats, table_id,
                                     streams, stream_stats),
                       indent=1, default=str))
        print("\nwrote %s" % args.json_out)
    return 0


def cmd_meter(args) -> int:
    (prices, table_id, sessions, store_stats, threads,
     streams, stream_stats) = _load_all(args)
    report = meter_report(sessions, args.bucket_hours)
    print("5-hour buckets (%d) and weekly buckets (%d) — every reading dated by SESSION START."
          % (len(report["five_hour_buckets"]), len(report["weekly_buckets"])))
    print(report["dating_note"])
    print(report["source_note"])
    print("\n%-22s %12s %7s   |  %-14s %12s %7s"
          % ("5h bucket", "tokens", "turns", "week", "tokens", "turns"))
    five = report["five_hour_buckets"][-args.top:]
    weekly = report["weekly_buckets"][-args.top:]
    report = dict(report, weekly_buckets=weekly)
    for index in range(max(len(five), len(report["weekly_buckets"]))):
        a = five[index] if index < len(five) else {}
        b = report["weekly_buckets"][index] if index < len(report["weekly_buckets"]) else {}
        print("%-22s %12s %7s   |  %-14s %12s %7s" % (
            (a.get("bucket") or "")[:19],
            compact_number(a["tokens"]) if a else "", a.get("turns", ""),
            (b.get("week_starting") or "")[:10],
            compact_number(b["tokens"]) if b else "", b.get("turns", "")))
    print("\nrate-limit readings: %d" % report["reading_count"])
    for reading in report["readings"][-15:]:
        print("  %s  %-10s %-8s %s" % (reading.get("at"), reading.get("kind"),
                                       reading.get("limit_kind") or "-",
                                       reading.get("reset_text") or "-"))
    if args.json_out:
        write_output(args.json_out, json.dumps(report, indent=1, default=str))
        print("wrote %s" % args.json_out)
    return 0


def cmd_report(args) -> int:
    (prices, table_id, sessions, store_stats, threads,
     streams, stream_stats) = _load_all(args)
    out = Path(args.output_dir).expanduser()
    # Owner-only: the report shows real paths, titles and prompt lines.
    audit_common.make_private_dir(out)
    # The payload is built BEFORE the ledger is written: the continuation
    # columns are computed there, and the ledger has to carry them.
    payload = build_payload(args, sessions, threads, store_stats, table_id,
                            streams, stream_stats)
    ledger_path = None
    if getattr(args, "ledger", None):
        ledger_path = str(Path(args.ledger).expanduser())
        stats = write_ledger(ledger_path, sessions, threads, streams, bool(args.redact),
                             scope="since=%s until=%s include=%s exclude=%s"
                             % (getattr(args, "since", None), getattr(args, "until", None),
                                ",".join(getattr(args, "include_cwd_pattern", []) or []) or "-",
                                ",".join(getattr(args, "exclude_cwd_pattern", []) or []) or "-"),
                             continuations=payload.get("continuations"))
        if stats:
            print("ledger: %s — %s" % (stats["ledger"], stats["headline"]))
        # Which analyses with their own report have written into this ledger:
        # the home page's index ticks them instead of saying "not run".
        payload["ledger_runs"] = ledger_runs(ledger_path)
    if args.redact:
        # export mode: aggregates only -- no per-session records, no thread members
        document = {k: v for k, v in payload.items()
                    if k not in ("sessions", "threads", "streams")}
        document["streams"] = [{k: v for k, v in r.items() if k != "evidence"}
                               for r in payload.get("streams") or []]
        document["threads"] = [{k: v for k, v in r.items() if k != "members"}
                               for r in payload["threads"]]
        # The event list is per-session detail and its ids are
        # GUIDs. Export mode keeps the counts and the rule, never the rows.
        document["continuations"] = {
            k: v for k, v in (payload.get("continuations") or {}).items()
            if k not in ("events", "per_session")}
    else:
        document = payload
    write_output(out / "orientation.json",
                 json.dumps(document, indent=1, default=str))
    write_output(out / "orientation.md", markdown_report(payload))
    pages = write_report_pages(payload, sessions, threads, streams, out, ledger_path)
    if not (payload.get("continuations") or {}).get("paired_streams"):
        # A carry-vs-restart page left over from a run on a wider scope would
        # be linked by nothing and read as current. Remove it.
        stale = out / PAGE_CARRY
        if stale.exists():
            stale.unlink()
    pack = (Path(args.screen_pack).expanduser() if getattr(args, "screen_pack", None)
            else out / "screen-pack")
    titles = screen_pack(payload, sessions, threads, store_stats, pack, streams, ledger_path)
    # The answer first, then where to look -- the same order as the home page.
    print(payload["summary"])
    if payload.get("handoff"):
        print()
        print_handoff(payload["handoff"])
    print("\nReport: open %s — %d linked pages (%s)."
          % (out / PAGE_INDEX, len(pages), ", ".join(label for _, label in pages)))
    print("Screen pack: %s in %s (F = full screen, arrow keys page)."
          % (_n(len(titles), "one-screen page"), pack / "INDEX.html"))
    print("Also written: %s and %s." % (out / "orientation.json", out / "orientation.md"))
    print("The usage meter is not in the report: `orientation_profile.py meter` shows it on "
          "a subscription plan.")
    return 0


def cmd_probe_store(args) -> int:
    result = probe_store(Path(args.path), args.limit)
    print(json.dumps(result, indent=1, default=str))
    print("\n# key names and counts only — no values were read out of this store.", file=sys.stderr)
    print("# safe to attach to an issue: names and counts only, never values.",
          file=sys.stderr)
    return 0


def cmd_ingest(args) -> int:
    """Show the adapter interface: which stores exist here and what they yield."""
    prices = load_prices(getattr(args, "pricing_file", None))
    rows = []
    for store in STORES:
        sub = argparse.Namespace(**vars(args))
        sub.store = [store]
        try:
            sessions, stats = collect_sessions(sub, prices)
        except Exception as exc:                       # a missing store is not fatal
            rows.append((store, "error: %s" % exc.__class__.__name__, 0, 0, ""))
            continue
        stat = stats[0] if stats else {}
        rows.append((store, str(stat.get("root") or stat.get("roots") or "—"),
                     len(sessions), sum(s.metrics.get("turns", 0) for s in sessions),
                     "tokens" if any(s.metrics.get("total_tokens") for s in sessions) else "no token counts"))
    print("%-15s %6s %7s  %-18s %s" % ("store", "sess", "turns", "coverage", "root"))
    for store, root, n, turns, coverage in rows:
        print("%-15s %6d %7d  %-18s %s" % (store, n, turns, coverage, root))
    print("\nFields a store does not carry are null, never guessed. "
          "Run `probe-store PATH` on an unknown store: its key listing is safe to attach "
          "to an issue.")
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=str(default_claude_root()),
                        help="Claude config dir or its projects/ subdir "
                             "(default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--store", action="append", choices=list(STORES),
                        help="repeatable; default = every store found")
    parser.add_argument("--vscode-root", action="append", help="override VS Code User dir")
    parser.add_argument("--copilot-root", action="append", help="override Copilot CLI dir")
    parser.add_argument("--since", help="YYYY-MM-DD (UTC)")
    parser.add_argument("--until", help="YYYY-MM-DD (UTC, exclusive)")
    add_filter_args(parser)
    parser.add_argument("--pricing-file", default=audit_common.DEFAULT_PRICING_FILE,
                        help="the shared price table (default: the skill's "
                             "references/pricing-public-example.json). Every analysis "
                             "in this skill reads the same file and reports the same "
                             "coverage number.")
    parser.add_argument("--gap-hours", type=float, default=72.0,
                        help="longest gap inside one sitting, in hours (default: 72)")
    parser.add_argument("--resume-gap-hours", type=float, default=RESUME_GAP_HOURS,
                        help="how long a pause between two responses counts as the session "
                             "being picked up again (default: %s). Transcripts carry no "
                             "resume marker, so this clock gap is the only signal; around "
                             "1 hour a resume stops reading its context from the cache and "
                             "writes it again" % hours_text(RESUME_GAP_HOURS))
    parser.add_argument("--min-shared", type=int, default=2,
                        help="file names two sessions must share to be one sitting (default: 2)")
    parser.add_argument("--max-sessions", type=int, default=12,
                        help="sessions in one sitting before a join needs extra evidence "
                             "(default: 12)")
    parser.add_argument("--bucket-hours", type=float, default=5.0)
    parser.add_argument("--max-files", type=int, help="cap Claude JSONL files read (debug)")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--explain-caps", action="store_true",
                        help="print every sitting-cap decision")
    parser.add_argument("--redact", action="store_true",
                        help="opt-in export mode: folders hashed, file names only, no prompts "
                             "or tool arguments. Off by default: the local report is yours, "
                             "in full.")
    parser.add_argument("--stream-by", choices=("auto", "cwd"), default="auto",
                        help="how to group sittings into work items (no gap limit). auto = "
                             "a shared ticket token, or the same folder plus the same branch "
                             "or >= 2 shared files. cwd = the folder alone, which is wrong "
                             "for anyone who works from one folder (default: auto)")
    parser.add_argument("--stream-ignore-token", action="append", default=[],
                        help="repeatable: a ticket token to ignore when joining work items, "
                             "for when a generic one (`#1`) joins too much. The report counts "
                             "joins per rule so you can see it happen")
    parser.add_argument("--ledger", metavar="PATH",
                        help="the session ledger (sessions.jsonl): one row per session that "
                             "every analysis adds its own columns to. This command writes the "
                             "startup columns; other analyses' columns are never dropped")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--json-out")
    parser.add_argument("--label", help="a name for this history, shown in the report title "
                                        "(default: \"this machine\" -- never the hostname)")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="orientation_profile.py",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    for name, handler, helptext in (
            ("sessions", cmd_sessions, "per-session startup share, strategy, thinking share, cost"),
            ("threads", cmd_threads, "group sessions into sittings and work items, and price "
                                     "the repeat startup"),
            ("meter", cmd_meter, "5-hour and weekly buckets beside the rate-limit readings"),
            ("report", cmd_report, "write orientation.json / .md / .html"),
            ("ingest", cmd_ingest, "list the ingest adapters and what each store yields here")):
        p = sub.add_parser(name, help=helptext, description=helptext)
        add_common(p)
        p.set_defaults(handler=handler)
        if name in ("sessions", "report"):
            p.add_argument("--handoff-log",
                           help="handoff-note field log CSV (timestamp,tool,cwd,arm,"
                                "note_path_or_empty,note_minutes; format in "
                                "references/orientation-and-threads.md §2b): join each logged "
                                "session end to the next session in the same sitting and "
                                "report the paired comparison")
            p.add_argument("--minute-rate-usd", type=float, default=100.0,
                           help="$/hour used to price note_minutes in the net-value "
                                "column; printed on every output (default: 100)")
        if name == "threads":
            p.add_argument("--label-candidates", type=int, default=0,
                           help="print N candidate sittings for hand-labelling")
            p.add_argument("--labels", help="labels file (thread_id -> yes|no|k); reports precision "
                                            "and sweeps thresholds")
        if name == "report":
            p.add_argument("--output-dir", required=True)
            p.add_argument("--screen-pack", metavar="DIR",
                           help="where to write the screen pack: numbered one-screen pages "
                                "sized for a projector, a screenshot, or reading aloud in a "
                                "meeting (default: <output-dir>/screen-pack). The pack is "
                                "written either way; --redact decides whether it is "
                                "the variant that is safe to share.")
    p = sub.add_parser("probe-store", help="say what we recognise in an unknown store (KEY NAMES ONLY)")
    p.add_argument("path")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(handler=cmd_probe_store)
    p = sub.add_parser("selftest", help="run the bundled unit tests")
    p.set_defaults(handler=lambda a: _run_selftest())
    return ap


def _run_selftest() -> int:
    here = Path(__file__).resolve().parent / "test_orientation_profile.py"
    if not here.exists():
        print("test file not found: %s" % here)
        return 1
    import subprocess
    return subprocess.call([sys.executable, str(here)])


def main(argv: Optional[Sequence[str]] = None) -> int:
    audit_common.utf8_stdio()
    args = parser().parse_args(argv)
    try:
        with audit_common.home_relative_stdout():
            return args.handler(args)
    except BrokenPipeError:
        return 0
    except (FileNotFoundError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
