from __future__ import annotations

import argparse
import configparser
import json
import ipaddress
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import tty
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from re import Match, Pattern
from typing import Sequence, TextIO
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w)"
)
LT_PHONE_RE = re.compile(
    r"(?<![\w+])(?:\+370[\s.*-]*|[08][\s.-]*)[3-9]\d{2}[\s.-]*\d{5}(?!\w)"
)
INTL_PHONE_RE = re.compile(r"(?<!\w)\+(?:[2-9]\d{1,2})[\s-]*(?:\d[\s-]*){6,10}\d(?!\w)")
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
URL_RE = re.compile(r"https?://[^\s()<>`'\"]+")
HOST_HEADER_RE = re.compile(r"(?im)^(\s*Host:\s*)([A-Za-z0-9.-]+\.[A-Za-z]{2,})(:\d+)?(\s*)$")
HOST_RE = re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:local|corp|internal)\b")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
AUTH_BEARER_RE = re.compile(r"(?im)(Authorization:[ \t]*Bearer[ \t]+)([^\s;`]+)")
COOKIE_VALUE_RE = re.compile(
    r"(?i)\b(session|sessionid|jsessionid|phpsessid|auth_token|csrftoken|xsrf-token|x-csrf-token)=([^;\s`'\"]+)"
)
AWS_KEY_RE = re.compile(r"\bAKIA[A-Z0-9.]{6,}\b")
GITHUB_TOKEN_RE = re.compile(r"\bgh[opsu]_[A-Za-z0-9_.-]{6,}\b")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")
DISCORD_TOKEN_RE = re.compile(
    r"\b(?![A-Za-z0-9_-]*__)[A-Za-z0-9_-]{20,}\."
    r"(?![A-Za-z0-9_-]*__)[A-Za-z0-9_-]{6,}\."
    r"(?![A-Za-z0-9_-]*__)[A-Za-z0-9_-]{10,}\b"
)
PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?token|access[_-]?token|secret|client[_-]?secret|password|token)"
    r"(['\"]?[ \t]*[:=][ \t]*['\"]?)(?!https?://)([^\s`'\";&{},]+)"
)
BASIC_AUTH_URL_RE = re.compile(r"https?://[^\s/@:]+:[^\s/@]+@[^\s()<>`'\"]+")
PLACEHOLDER_OR_MASK_RE = re.compile(
    r"\[[A-Z_]+_\d+\]|\*\*\*|\[(?:[A-Z_]+\s+)?REDACTED\]|<redacted>|\bREDACTED\b",
    re.IGNORECASE,
)
RESTORE_PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+)_(\d+)\]")
REDACTED_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|client[_-]?secret|password|token)[ \t]*[:=][ \t]*(?:\[[A-Z_]+_\d+\]|\*\*\*|\[(?:[A-Z_]+\s+)?REDACTED\]|<redacted>|\bREDACTED\b)"
)
REDACTED_AUTH_BEARER_RE = re.compile(
    r"(?im)Authorization:[ \t]*Bearer[ \t]+(?:\*\*\*|\[[A-Z_]+_\d+\]|\[(?:[A-Z_]+\s+)?REDACTED\]|<redacted>|\bREDACTED\b)"
)
PATH_RE = re.compile(r"(?<!\w)/(?:home|Users|root|workspace|vault)/[^\s`'\":;,)]+")
WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\(?:Users|Documents and Settings|workspace|vault)\\[^\s`'\":;,)]+")
DEFAULT_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "redloc"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "profiles.ini"
GLOBAL_TERMS_FILENAME = "global-terms.txt"
DEFAULT_SETTINGS_PATH = DEFAULT_CONFIG_DIR / "settings.ini"
DEFAULT_STATE_FILE = DEFAULT_CONFIG_DIR / "current-profile"
DEFAULT_SESSION_STATE_FILE = DEFAULT_CONFIG_DIR / "current-session"
DEFAULT_SESSION_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "redloc" / "sessions"
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TERM_KIND_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]*):\s*(.+)$")
DISABLED_LIST_LINE_RE = re.compile(r"^#\s*disabled:\s*(.+)$", re.IGNORECASE)
DEFAULT_MANUAL_DETECTORS = ("CLIENT", "PERSON", "ORG", "PROJECT", "LOCATION", "CONTEXT")
TERM_KINDS = set(DEFAULT_MANUAL_DETECTORS)
TERM_KIND_ORDER = DEFAULT_MANUAL_DETECTORS
UNASSIGNED_IGNORED_KIND = "UNASSIGNED"
AI_SUGGESTION_CATEGORIES = ("person", "organization", "project", "location", "context")
TERM_FILE_TEMPLATE = """# redloc term file
# Format: CATEGORY: term
# Categories: CLIENT, PERSON, ORG, PROJECT, LOCATION, CONTEXT

ORG: ExampleCo
PROJECT: Project Squirrel
PERSON: Jane Doe
LOCATION: Vilnius office
CONTEXT: exampleco.internal
CLIENT: ExampleCo VPN
"""
ABOUT_TEXT = r"""              _ _
             | | |
 _ __ ___  __| | | ___   ___
| '__/ _ \/ _` | |/ _ \ / __|
| | |  __/ (_| | | (_) | (__
|_|  \___|\__,_|_|\___/ \___|

local-first redaction for operator notes

Local-first CLI for sanitizing logs, notes, HTTP snippets,
paths, tokens, client names, and other sensitive text before sharing.

Author: Brian Brandson
License: Apache-2.0
"""
DEFAULT_AI_TIMEOUT_SECONDS = 30.0
DEFAULT_AI_CHUNK_MAX_CHARS = 8_000
DEFAULT_AI_CHUNK_MAX_LINES = 80
DEFAULT_AI_CHECK_MAX_TOKENS = 512
DEFAULT_AI_SUGGEST_MAX_TOKENS = 512
AI_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
AI_CATEGORY_RANK = {"person": 5, "organization": 4, "project": 3, "location": 2, "context": 1}
AI_GENERIC_SUGGESTION_TERMS = {
    "admin$",
    "administrator",
    "administrators",
    "bloodhound",
    "bloodhound.py",
    "c$",
    "default domain policy",
    "default-first-site-name",
    "domain admins",
    "enterprise admins",
    "evil-winrm",
    "group policy creator owners",
    "guest",
    "ipc$",
    "krbtgt",
    "netlogon",
    "note",
    "notes",
    "notes.txt",
    "remote desktop users",
    "remote management users",
    "sysvol",
}
CATEGORY_DESCRIPTIONS = {
    "EMAIL": "email addresses",
    "PHONE": "phone numbers",
    "HOST": "domains and internal hostnames",
    "INTERNAL_IP": "RFC1918/private IPv4 addresses",
    "PUBLIC_IP": "public IPv4 addresses",
    "TOKEN": "bearer, API, JWT, private-key, credential-like values",  # nosec B105 - category label, not a credential
    "COOKIE": "session and CSRF cookie values",
    "UUID": "UUID/GUID values",
    "PATH": "user/workspace/vault paths",
    "TERMS": "configured profile/client/project terms",
}
REDACTION_CATEGORIES = tuple(CATEGORY_DESCRIPTIONS)


def _chmod_private_file(path: Path) -> None:
    path.chmod(0o600)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_private_text(path: Path, text: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _chmod_private_file(path)


def _write_private_state_text(path: Path, text: str) -> None:
    path = path.expanduser()
    _ensure_private_dir(path.parent)
    path.write_text(text, encoding="utf-8")
    _chmod_private_file(path)


def _append_private_text(path: Path, text: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
    _chmod_private_file(path)


def _touch_private_file(path: Path) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    _chmod_private_file(path)


def _write_private_config(parser: configparser.ConfigParser, path: Path) -> None:
    path = path.expanduser()
    _ensure_private_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    _chmod_private_file(path)


@dataclass
class PlaceholderVault:
    counters: dict[str, int] = field(default_factory=dict)
    values: dict[tuple[str, str], str] = field(default_factory=dict)
    replacement_counts: dict[str, int] = field(default_factory=dict)
    changed: bool = False

    def placeholder_for(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self.values:
            self.counters[kind] = self.counters.get(kind, 0) + 1
            self.values[key] = f"[{kind}_{self.counters[kind]}]"
            self.changed = True
        self.replacement_counts[kind] = self.replacement_counts.get(kind, 0) + 1
        return self.values[key]

    def status_counts(self) -> dict[str, int]:
        return dict(self.counters)


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int]
    vault: PlaceholderVault


@dataclass
class SessionData:
    name: str
    vault: PlaceholderVault
    path: Path


@dataclass
class RedactorOptions:
    client_terms: list[str] = field(default_factory=list)
    term_files: list[Path] = field(default_factory=list)
    ignored_suggestion_files: list[Path] = field(default_factory=list)
    disabled_categories: set[str] = field(default_factory=set)
    copy: bool = False


@dataclass(frozen=True)
class AIConfig:
    endpoint: str | None = None
    model: str | None = None
    timeout_seconds: float | None = None
    chunk_max_lines: int | None = None
    chunk_max_chars: int | None = None


@dataclass(frozen=True)
class RedactionTerm:
    term: str
    kind: str = "CLIENT"


@dataclass(frozen=True)
class ManagedListEntry:
    line: str
    enabled: bool = True


@dataclass(frozen=True)
class ManualDetector:
    kind: str
    enabled: bool = True


@dataclass(frozen=True)
class AIWarning:
    category: str
    line: int | None = None
    confidence: str | None = None

    def message(self) -> str:
        parts = [f"possible {self.category} remains"]
        if self.line is not None:
            parts.append(f"on line {self.line}")
        if self.confidence:
            parts.append(f"({self.confidence})")
        return " ".join(parts)

    def as_report_dict(self) -> dict[str, str | int]:
        payload: dict[str, str | int] = {"category": self.category}
        if self.line is not None:
            payload["line"] = self.line
        if self.confidence:
            payload["confidence"] = self.confidence
        return payload


@dataclass(frozen=True)
class AISuggestion:
    term: str
    category: str
    lines: tuple[int, ...] = ()
    confidence: str | None = None

    def message(self) -> str:
        parts = [f"possible {self.category} term: {self.term}"]
        if self.lines:
            rendered_lines = ",".join(str(line) for line in self.lines)
            parts.append(f"lines {rendered_lines}")
        if self.confidence:
            parts.append(f"({self.confidence})")
        return " ".join(parts)

    def as_report_dict(self, *, include_term: bool = True) -> dict[str, str | int | list[int]]:
        payload: dict[str, str | int | list[int]] = {"category": self.category}
        if include_term:
            payload["term"] = self.term
        if self.lines:
            payload["lines"] = list(self.lines)
        if self.confidence:
            payload["confidence"] = self.confidence
        return payload


def _is_internal_ipv4(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(
        ip in network
        for network in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
    )


def _ipv4_placeholder_kind(value: str) -> str | None:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return "INTERNAL_IP" if _is_internal_ipv4(value) else "PUBLIC_IP"


def _normalize_manual_detector_kind(name: str) -> str:
    normalized = re.sub(r"[_\s-]+", "_", name.strip().upper()).strip("_")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,62}", normalized):
        return ""
    return normalized


def _manual_detector_display(kind: str) -> str:
    normalized = _normalize_manual_detector_kind(kind)
    return normalized.replace("_", " ") if normalized else ""


def _manual_detector_placeholder(kind: str) -> str:
    normalized = _normalize_manual_detector_kind(kind)
    return f"[{normalized}_N]" if normalized else ""


def _parse_term_line(line: str) -> RedactionTerm | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = TERM_KIND_PREFIX_RE.match(stripped)
    if match:
        kind = _normalize_manual_detector_kind(match.group(1))
        term = match.group(2).strip()
        if kind and term:
            return RedactionTerm(term=term, kind=kind)
    return RedactionTerm(term=stripped, kind="CLIENT")


def _strip_disabled_list_marker(line: str) -> tuple[str, bool]:
    stripped = line.strip()
    match = DISABLED_LIST_LINE_RE.match(stripped)
    if match:
        return match.group(1).strip(), False
    return stripped, True


def _parse_managed_term_line(line: str) -> ManagedListEntry | None:
    stripped, enabled = _strip_disabled_list_marker(line)
    term = _parse_term_line(stripped)
    if not term:
        return None
    return ManagedListEntry(line=_render_redaction_term(term), enabled=enabled)


def _parse_managed_plain_line(line: str) -> ManagedListEntry | None:
    stripped, enabled = _strip_disabled_list_marker(line)
    term = _parse_term_line(stripped)
    if not term:
        return None
    return ManagedListEntry(line=term.term, enabled=enabled)


def _parse_managed_ignored_line(line: str) -> ManagedListEntry | None:
    stripped, enabled = _strip_disabled_list_marker(line)
    term = _parse_term_line(stripped)
    if not term:
        return None
    if TERM_KIND_PREFIX_RE.match(stripped):
        return ManagedListEntry(line=_render_redaction_term(term), enabled=enabled)
    return ManagedListEntry(line=_render_redaction_term(replace(term, kind=UNASSIGNED_IGNORED_KIND)), enabled=enabled)


def _parse_explicit_term_line(line: str) -> RedactionTerm | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = TERM_KIND_PREFIX_RE.match(stripped)
    if not match:
        return None
    kind = _normalize_manual_detector_kind(match.group(1))
    term = match.group(2).strip()
    if kind and term:
        return RedactionTerm(term=term, kind=kind)
    return None


def _guess_term_kind(term: str) -> str:
    stripped = term.strip()
    lowered = stripped.casefold()
    if lowered.startswith("project "):
        return "PROJECT"
    if re.search(r"\b(inc|llc|ltd|uab|corp|corporation|company|gmbh|oy|ab)\b\.?$", lowered):
        return "ORG"
    if HOST_RE.fullmatch(stripped) or ("." in stripped and re.fullmatch(r"[A-Za-z0-9.-]+", stripped)):
        return "CONTEXT"
    parts = stripped.split()
    if len(parts) == 2 and all(part[:1].isupper() and part[1:].islower() for part in parts):
        return "PERSON"
    return "CLIENT"


def _coerce_redaction_term(value: str | RedactionTerm) -> RedactionTerm:
    if isinstance(value, RedactionTerm):
        return replace(value, kind=_normalize_manual_detector_kind(value.kind) or "CLIENT")
    parsed = _parse_term_line(value)
    return parsed or RedactionTerm(term="", kind="CLIENT")


def _compile_terms(terms: Sequence[str | RedactionTerm] | None) -> list[tuple[Pattern[str], str]]:
    redaction_terms = [_coerce_redaction_term(term) for term in terms or []]
    ordered_terms = sorted((term for term in redaction_terms if term.term), key=lambda item: len(item.term), reverse=True)
    return [(re.compile(rf"\b{re.escape(term.term)}\b", re.IGNORECASE), _normalize_manual_detector_kind(term.kind) or "CLIENT") for term in ordered_terms]


def _read_term_file(path: Path) -> list[str]:
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        managed = _parse_managed_term_line(line)
        if managed and managed.enabled:
            terms.append(managed.line)
    return terms


def _term_identity(raw_term: str) -> str:
    term = _parse_term_line(raw_term)
    return term.term.casefold() if term else ""


def _suggestion_identity(suggestion: AISuggestion) -> str:
    return suggestion.term.strip().casefold()


def _is_generic_ai_suggestion(suggestion: AISuggestion) -> bool:
    identity = _suggestion_identity(suggestion)
    if identity in AI_GENERIC_SUGGESTION_TERMS:
        return True
    return bool(re.fullmatch(r"[a-z]\$", identity))


def _read_ignored_suggestion_file(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ignored_terms: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        managed = _parse_managed_ignored_line(line)
        if managed and managed.enabled:
            parsed = _parse_term_line(managed.line)
            if parsed:
                ignored_terms.add(parsed.term.casefold())
    return ignored_terms


def list_ignored_suggestions(paths: Sequence[Path]) -> list[str]:
    ignored: dict[str, str] = {}
    for path in paths:
        path = path.expanduser()
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            managed = _parse_managed_ignored_line(line)
            if managed and managed.enabled:
                ignored.setdefault(managed.line.casefold(), managed.line)
    return [ignored[key] for key in sorted(ignored)]


def _merge_managed_entries(entries: list[ManagedListEntry]) -> list[ManagedListEntry]:
    merged: dict[str, ManagedListEntry] = {}
    for entry in entries:
        identity = _term_identity(entry.line)
        if not identity:
            continue
        if identity not in merged or (entry.enabled and not merged[identity].enabled):
            merged[identity] = entry
    return [merged[key] for key in sorted(merged)]


def list_profile_terms_with_state(paths: Sequence[Path]) -> list[ManagedListEntry]:
    entries: list[ManagedListEntry] = []
    for path in paths:
        path = path.expanduser()
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            managed = _parse_managed_term_line(line)
            if managed:
                entries.append(managed)
    return _merge_managed_entries(entries)


def list_ignored_suggestions_with_state(paths: Sequence[Path]) -> list[ManagedListEntry]:
    entries: list[ManagedListEntry] = []
    for path in paths:
        path = path.expanduser()
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            managed = _parse_managed_ignored_line(line)
            if managed:
                entries.append(managed)
    return _merge_managed_entries(entries)


def list_profile_terms(paths: Sequence[Path]) -> list[str]:
    terms: dict[str, str] = {}
    for path in paths:
        path = path.expanduser()
        if not path.exists():
            continue
        for raw_term in _read_term_file(path):
            identity = _term_identity(raw_term)
            if identity:
                terms.setdefault(identity, raw_term)
    return [terms[key] for key in sorted(terms)]


def _disabled_list_line(line: str) -> str:
    return f"# disabled: {line}"


def _set_list_line_states(
    paths: Sequence[Path], *, disabled: Sequence[str], removed: Sequence[str], updated: Sequence[str] = ()
) -> tuple[int, int]:
    disabled_identities = {_term_identity(term) for term in disabled if _term_identity(term)}
    removed_identities = {_term_identity(term) for term in removed if _term_identity(term)}
    updated_by_identity = {_term_identity(term): term for term in updated if _term_identity(term)}
    disabled_count = 0
    removed_count = 0
    for path in paths:
        path = path.expanduser()
        if not path.exists():
            continue
        new_lines: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            managed = _parse_managed_term_line(line) or _parse_managed_plain_line(line)
            identity = _term_identity(managed.line) if managed else ""
            if identity and identity in removed_identities:
                removed_count += 1
                continue
            if identity and identity in disabled_identities:
                if managed and managed.enabled:
                    disabled_count += 1
                new_lines.append(_disabled_list_line(updated_by_identity.get(identity, managed.line if managed else line.strip())))
                continue
            if managed and not managed.enabled and identity:
                new_lines.append(updated_by_identity.get(identity, managed.line))
                continue
            if managed and identity in updated_by_identity:
                new_lines.append(updated_by_identity[identity])
                continue
            new_lines.append(line)
        _write_private_text(path, "\n".join(new_lines) + ("\n" if new_lines else ""))
    return disabled_count, removed_count


def set_profile_term_states(
    paths: Sequence[Path], *, disabled: Sequence[str], removed: Sequence[str], updated: Sequence[str] = ()
) -> tuple[int, int]:
    return _set_list_line_states(paths, disabled=disabled, removed=removed, updated=updated)


def set_ignored_suggestion_states(
    paths: Sequence[Path], *, disabled: Sequence[str], removed: Sequence[str], updated: Sequence[str] = ()
) -> tuple[int, int]:
    return _set_list_line_states(paths, disabled=disabled, removed=removed, updated=updated)


def _remove_term_line(path: Path, raw_term: str) -> int:
    path = path.expanduser()
    identity = _term_identity(raw_term)
    if not identity or not path.exists():
        return 0
    removed_count = 0
    kept_lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if _term_identity(line) == identity:
            removed_count += 1
            continue
        kept_lines.append(line)
    if removed_count:
        _write_private_text(path, "\n".join(kept_lines) + ("\n" if kept_lines else ""))
    return removed_count


def remove_ignored_suggestion(path: Path, raw_term: str) -> int:
    return _remove_term_line(path, raw_term)


def remove_profile_term(path: Path, raw_term: str) -> int:
    return _remove_term_line(path, raw_term)


def global_terms_path(config_path: Path = DEFAULT_CONFIG_PATH) -> Path:
    return config_path.expanduser().parent / GLOBAL_TERMS_FILENAME


def list_global_terms(config_path: Path = DEFAULT_CONFIG_PATH) -> list[str]:
    return list_profile_terms([global_terms_path(config_path)])


def list_global_terms_with_state(config_path: Path = DEFAULT_CONFIG_PATH) -> list[ManagedListEntry]:
    return list_profile_terms_with_state([global_terms_path(config_path)])


def remove_global_term(config_path: Path, raw_term: str) -> int:
    return remove_profile_term(global_terms_path(config_path), raw_term)


def set_global_term_states(
    config_path: Path, *, disabled: Sequence[str], removed: Sequence[str], updated: Sequence[str] = ()
) -> tuple[int, int]:
    return set_profile_term_states([global_terms_path(config_path)], disabled=disabled, removed=removed, updated=updated)


def _filter_ignored_suggestions(suggestions: list[AISuggestion], ignored_terms: set[str]) -> list[AISuggestion]:
    if not ignored_terms:
        return suggestions
    return [suggestion for suggestion in suggestions if _suggestion_identity(suggestion) not in ignored_terms]


def _ai_suggestion_term_kind(suggestion: AISuggestion) -> str:
    return {
        "person": "PERSON",
        "organization": "ORG",
        "project": "PROJECT",
        "location": "LOCATION",
        "context": "CONTEXT",
    }.get(suggestion.category, "CLIENT")


def _preferred_ai_suggestion(left: AISuggestion, right: AISuggestion) -> AISuggestion:
    left_rank = (
        AI_CONFIDENCE_RANK.get(left.confidence or "", 0),
        AI_CATEGORY_RANK.get(left.category, 0),
    )
    right_rank = (
        AI_CONFIDENCE_RANK.get(right.confidence or "", 0),
        AI_CATEGORY_RANK.get(right.category, 0),
    )
    return right if right_rank > left_rank else left


def _split_config_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _normalize_redaction_category(category: str) -> str:
    normalized = category.strip().upper().replace("-", "_")
    if normalized not in REDACTION_CATEGORIES:
        raise ValueError(f"unknown detector: {category}")
    return normalized


def _disabled_categories_from_config(value: str) -> set[str]:
    disabled_categories: set[str] = set()
    for raw_category in _split_config_lines(value.replace(",", "\n")):
        disabled_categories.add(_normalize_redaction_category(raw_category))
    return disabled_categories


def _format_disabled_categories(disabled_categories: set[str] | None) -> str:
    return ",".join(sorted(disabled_categories or []))


def _category_enabled(category: str, disabled_categories: set[str] | None) -> bool:
    return category not in (disabled_categories or set())


def _manual_detectors_from_config(value: str) -> list[str]:
    detectors: list[str] = list(DEFAULT_MANUAL_DETECTORS)
    seen = set(detectors)
    for raw_detector in _split_config_lines(value.replace(",", "\n")):
        detector = _normalize_manual_detector_kind(raw_detector)
        if detector and detector not in seen:
            detectors.append(detector)
            seen.add(detector)
    return detectors


def _disabled_manual_detectors_from_config(value: str) -> set[str]:
    return {
        detector
        for detector in (_normalize_manual_detector_kind(raw) for raw in _split_config_lines(value.replace(",", "\n")))
        if detector
    }


def _format_manual_detectors(detectors: Sequence[str]) -> str:
    custom_detectors = [detector for detector in detectors if detector not in DEFAULT_MANUAL_DETECTORS]
    return ",".join(custom_detectors)


def load_profile_manual_detectors(profile: str, *, config_path: Path = DEFAULT_CONFIG_PATH) -> list[ManualDetector]:
    parser = configparser.ConfigParser()
    read_paths = parser.read(config_path.expanduser(), encoding="utf-8")
    if not read_paths:
        raise FileNotFoundError(f"redactor config not found: {config_path}")
    if profile not in parser:
        raise KeyError(f"redactor profile not found: {profile}")
    section = parser[profile]
    detectors = _manual_detectors_from_config(section.get("manual_detectors", ""))
    disabled = _disabled_manual_detectors_from_config(section.get("disabled_manual_detectors", ""))
    return [ManualDetector(kind=detector, enabled=detector not in disabled) for detector in detectors]


def save_profile_manual_detectors(
    profile: str,
    detectors: Sequence[str],
    disabled_detectors: set[str],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> None:
    parser = configparser.ConfigParser()
    read_paths = parser.read(config_path.expanduser(), encoding="utf-8")
    if not read_paths:
        raise FileNotFoundError(f"redactor config not found: {config_path}")
    if profile not in parser:
        raise KeyError(f"redactor profile not found: {profile}")
    normalized_detectors = _manual_detectors_from_config("\n".join(detectors))
    parser[profile]["manual_detectors"] = _format_manual_detectors(normalized_detectors)
    parser[profile]["disabled_manual_detectors"] = ",".join(sorted(disabled_detectors))
    _write_private_config(parser, config_path)


def add_profile_manual_detectors(
    profile: str, detector_names: Sequence[str], *, config_path: Path = DEFAULT_CONFIG_PATH
) -> int:
    existing = load_profile_manual_detectors(profile, config_path=config_path)
    detectors = [detector.kind for detector in existing]
    disabled = {detector.kind for detector in existing if not detector.enabled}
    seen = set(detectors)
    added = 0
    for raw_name in detector_names:
        detector = _normalize_manual_detector_kind(raw_name)
        if not detector:
            continue
        if detector not in seen:
            detectors.append(detector)
            seen.add(detector)
            added += 1
        disabled.discard(detector)
    if added or detector_names:
        save_profile_manual_detectors(profile, detectors, disabled, config_path=config_path)
    return added


def set_profile_manual_detector_enabled(
    profile: str, detector_name: str, *, enabled: bool, config_path: Path = DEFAULT_CONFIG_PATH
) -> set[str]:
    detector = _normalize_manual_detector_kind(detector_name)
    if not detector:
        raise ValueError(f"unknown manual detector: {detector_name}")
    existing = load_profile_manual_detectors(profile, config_path=config_path)
    detectors = [item.kind for item in existing]
    if detector not in detectors:
        raise ValueError(f"unknown manual detector: {detector_name}")
    disabled = {item.kind for item in existing if not item.enabled}
    if enabled:
        disabled.discard(detector)
    else:
        disabled.add(detector)
    save_profile_manual_detectors(profile, detectors, disabled, config_path=config_path)
    return disabled


def remove_profile_manual_detector(
    profile: str, detector_name: str, *, config_path: Path = DEFAULT_CONFIG_PATH
) -> int:
    detector = _normalize_manual_detector_kind(detector_name)
    existing = load_profile_manual_detectors(profile, config_path=config_path)
    detectors = [item.kind for item in existing]
    if detector in DEFAULT_MANUAL_DETECTORS:
        raise ValueError(f"default manual detector cannot be removed: {_manual_detector_display(detector)}")
    kept_detectors = [item for item in detectors if item != detector]
    removed = len(detectors) - len(kept_detectors)
    if removed:
        disabled = {item.kind for item in existing if not item.enabled and item.kind != detector}
        save_profile_manual_detectors(profile, kept_detectors, disabled, config_path=config_path)
    return removed


def format_manual_detector_list(profile: str, detectors: Sequence[ManualDetector]) -> list[str]:
    lines = [f"Manual detectors for profile: {profile}"]
    for detector in detectors:
        marker = "x" if detector.enabled else " "
        lines.append(f"[{marker}] {_manual_detector_display(detector.kind)}: {_manual_detector_placeholder(detector.kind)}")
    return lines


def _manual_detector_list_item(detector: ManualDetector) -> str:
    return f"{_manual_detector_display(detector.kind)}: {_manual_detector_placeholder(detector.kind)}"


def _manual_detector_kind_from_list_item(item: str) -> str:
    name, _sep, _placeholder = item.partition(":")
    return _normalize_manual_detector_kind(name)


def load_global_copy_enabled(settings_path: Path = DEFAULT_SETTINGS_PATH) -> bool:
    parser = configparser.ConfigParser()
    if not parser.read(settings_path.expanduser(), encoding="utf-8"):
        return False
    return parser.getboolean("settings", "copy", fallback=False)


def set_global_copy_enabled(enabled: bool, settings_path: Path = DEFAULT_SETTINGS_PATH) -> None:
    settings_path = settings_path.expanduser()
    parser = configparser.ConfigParser()
    parser.read(settings_path, encoding="utf-8")
    if "settings" not in parser:
        parser["settings"] = {}
    parser["settings"]["copy"] = "true" if enabled else "false"
    _write_private_config(parser, settings_path)


def load_ai_config(settings_path: Path = DEFAULT_SETTINGS_PATH) -> AIConfig:
    parser = configparser.ConfigParser()
    if not parser.read(settings_path.expanduser(), encoding="utf-8"):
        return AIConfig()
    timeout_value = parser.get("ai", "timeout_seconds", fallback=None)
    chunk_lines_value = parser.get("ai", "chunk_max_lines", fallback=None)
    chunk_chars_value = parser.get("ai", "chunk_max_chars", fallback=None)
    return AIConfig(
        endpoint=parser.get("ai", "endpoint", fallback=None),
        model=parser.get("ai", "model", fallback=None),
        timeout_seconds=float(timeout_value) if timeout_value else None,
        chunk_max_lines=int(chunk_lines_value) if chunk_lines_value else None,
        chunk_max_chars=int(chunk_chars_value) if chunk_chars_value else None,
    )


def set_ai_config(
    endpoint: str,
    model: str,
    settings_path: Path = DEFAULT_SETTINGS_PATH,
    *,
    timeout_seconds: float | None = None,
    chunk_max_lines: int | None = None,
    chunk_max_chars: int | None = None,
) -> None:
    if not _endpoint_is_local_or_private(endpoint):
        raise ValueError("AI config endpoint must be localhost or a private IP")
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("AI timeout must be greater than zero seconds")
    if chunk_max_lines is not None and chunk_max_lines <= 0:
        raise ValueError("AI chunk lines must be greater than zero")
    if chunk_max_chars is not None and chunk_max_chars <= 0:
        raise ValueError("AI chunk chars must be greater than zero")
    settings_path = settings_path.expanduser()
    parser = configparser.ConfigParser()
    parser.read(settings_path, encoding="utf-8")
    if "ai" not in parser:
        parser["ai"] = {}
    parser["ai"]["endpoint"] = endpoint
    parser["ai"]["model"] = model
    if timeout_seconds is not None:
        parser["ai"]["timeout_seconds"] = f"{timeout_seconds:g}"
    if chunk_max_lines is not None:
        parser["ai"]["chunk_max_lines"] = str(chunk_max_lines)
    if chunk_max_chars is not None:
        parser["ai"]["chunk_max_chars"] = str(chunk_max_chars)
    _write_private_config(parser, settings_path)


def clear_ai_config(settings_path: Path = DEFAULT_SETTINGS_PATH) -> None:
    settings_path = settings_path.expanduser()
    parser = configparser.ConfigParser()
    parser.read(settings_path, encoding="utf-8")
    if "ai" in parser:
        parser.remove_section("ai")
    _write_private_config(parser, settings_path)


def resolve_ai_config(args: argparse.Namespace) -> AIConfig:
    saved_config = load_ai_config(args.settings)
    return AIConfig(
        endpoint=args.ai_endpoint or saved_config.endpoint,
        model=args.ai_model or saved_config.model,
        timeout_seconds=args.ai_timeout or saved_config.timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS,
        chunk_max_lines=args.ai_chunk_lines or saved_config.chunk_max_lines or DEFAULT_AI_CHUNK_MAX_LINES,
        chunk_max_chars=args.ai_chunk_chars or saved_config.chunk_max_chars or DEFAULT_AI_CHUNK_MAX_CHARS,
    )


def format_ai_config_status(ai_config: AIConfig) -> list[str]:
    return [
        f"ai_endpoint: {ai_config.endpoint} (saved)" if ai_config.endpoint else "ai_endpoint: unset",
        f"ai_model: {ai_config.model} (saved)" if ai_config.model else "ai_model: unset",
        f"ai_timeout: {ai_config.timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS:g} ({'saved' if ai_config.timeout_seconds else 'default'})",
        f"ai_chunk_lines: {ai_config.chunk_max_lines or DEFAULT_AI_CHUNK_MAX_LINES} ({'saved' if ai_config.chunk_max_lines else 'default'})",
        f"ai_chunk_chars: {ai_config.chunk_max_chars or DEFAULT_AI_CHUNK_MAX_CHARS} ({'saved' if ai_config.chunk_max_chars else 'default'})",
    ]


def profile_dir(config_path: Path, profile: str) -> Path:
    return config_path.expanduser().parent / "profiles" / profile


def init_profile(profile: str, *, config_path: Path = DEFAULT_CONFIG_PATH) -> Path:
    profile_path = profile_dir(config_path, profile)
    terms_path = profile_path / "terms.txt"
    ignored_suggestions_path = profile_path / "ignored-suggestions.txt"
    redacted_path = profile_path / "redacted"
    _ensure_private_dir(profile_path)
    _ensure_private_dir(redacted_path)
    _touch_private_file(terms_path)
    _touch_private_file(ignored_suggestions_path)

    parser = configparser.ConfigParser()
    parser.read(config_path.expanduser(), encoding="utf-8")
    if profile not in parser:
        parser[profile] = {}
    if not parser[profile].get("term_files"):
        parser[profile]["term_files"] = str(terms_path)
    if not parser[profile].get("ignored_suggestion_files"):
        parser[profile]["ignored_suggestion_files"] = str(ignored_suggestions_path)
    if not parser[profile].get("disabled_categories"):
        parser[profile]["disabled_categories"] = ""
    _write_private_config(parser, config_path)
    return profile_path


def ensure_default_profile(config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    parser = configparser.ConfigParser()
    if not parser.read(config_path.expanduser(), encoding="utf-8") or "default" not in parser:
        init_profile("default", config_path=config_path)


def load_profile_options(profile: str, config_path: Path = DEFAULT_CONFIG_PATH) -> RedactorOptions:
    parser = configparser.ConfigParser()
    read_paths = parser.read(config_path, encoding="utf-8")
    if not read_paths:
        raise FileNotFoundError(f"redactor config not found: {config_path}")
    if profile not in parser:
        raise KeyError(f"redactor profile not found: {profile}")

    section = parser[profile]
    terms = _split_config_lines(section.get("terms", ""))
    term_files = [Path(term_file).expanduser() for term_file in _split_config_lines(section.get("term_files", ""))]
    ignored_suggestion_files = [
        Path(ignore_file).expanduser()
        for ignore_file in _split_config_lines(section.get("ignored_suggestion_files", ""))
    ]
    disabled_categories = _disabled_categories_from_config(section.get("disabled_categories", ""))
    if not ignored_suggestion_files:
        ignored_suggestion_files = [profile_dir(config_path, profile) / "ignored-suggestions.txt"]
    for term_file in term_files:
        terms.extend(_read_term_file(term_file))
    return RedactorOptions(
        client_terms=terms,
        term_files=term_files,
        ignored_suggestion_files=ignored_suggestion_files,
        disabled_categories=disabled_categories,
        copy=section.getboolean("copy", fallback=False),
    )


def save_profile_disabled_categories(
    profile: str, disabled_categories: set[str], *, config_path: Path = DEFAULT_CONFIG_PATH
) -> None:
    parser = configparser.ConfigParser()
    read_paths = parser.read(config_path.expanduser(), encoding="utf-8")
    if not read_paths:
        raise FileNotFoundError(f"redactor config not found: {config_path}")
    if profile not in parser:
        raise KeyError(f"redactor profile not found: {profile}")
    parser[profile]["disabled_categories"] = _format_disabled_categories(disabled_categories)
    _write_private_config(parser, config_path)


def set_profile_detector_enabled(
    profile: str, category: str, *, enabled: bool, config_path: Path = DEFAULT_CONFIG_PATH
) -> set[str]:
    normalized_category = _normalize_redaction_category(category)
    profile_options = load_profile_options(profile, config_path)
    disabled_categories = set(profile_options.disabled_categories)
    if enabled:
        disabled_categories.discard(normalized_category)
    else:
        disabled_categories.add(normalized_category)
    save_profile_disabled_categories(profile, disabled_categories, config_path=config_path)
    return disabled_categories


def format_detector_list(profile: str, disabled_categories: set[str]) -> list[str]:
    lines = [f"Built-in detectors for profile: {profile}"]
    for category in REDACTION_CATEGORIES:
        marker = " " if category in disabled_categories else "x"
        lines.append(f"[{marker}] {category:<11} {CATEGORY_DESCRIPTIONS[category]}")
    return lines


def resolve_profile_name(
    explicit_profile: str | None,
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    state_file: Path = DEFAULT_STATE_FILE,
) -> str | None:
    if explicit_profile:
        return explicit_profile
    ensure_default_profile(config_path)
    parser = configparser.ConfigParser()
    if not parser.read(config_path, encoding="utf-8"):
        return None
    if state_file.exists():
        selected = state_file.read_text(encoding="utf-8").strip()
        if selected and selected in parser:
            return selected
    if "default" in parser:
        return "default"
    return None


def select_profile(profile: str, *, config_path: Path, state_file: Path) -> None:
    load_profile_options(profile, config_path)
    _write_private_state_text(state_file, f"{profile}\n")


def list_profiles(config_path: Path, *, state_file: Path = DEFAULT_STATE_FILE) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(config_path.expanduser(), encoding="utf-8")
    selected = state_file.read_text(encoding="utf-8").strip() if state_file.exists() else ""
    return [f"{'*' if name == selected else ' '} {name}" for name in sorted(parser.sections())]


def profile_names(config_path: Path) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(config_path.expanduser(), encoding="utf-8")
    return sorted(parser.sections())


def _validate_session_name(name: str) -> None:
    if not SESSION_NAME_RE.fullmatch(name):
        raise ValueError("session names may only contain letters, numbers, dot, dash, and underscore")


def _session_path(session_dir: Path, name: str) -> Path:
    _validate_session_name(name)
    return session_dir.expanduser() / f"{name}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_session(name: str, session_dir: Path = DEFAULT_SESSION_DIR) -> SessionData:
    path = _session_path(session_dir, name)
    if not path.exists():
        return SessionData(name=name, vault=PlaceholderVault(), path=path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    values = {
        (entry["kind"], entry["value"]): entry["placeholder"]
        for entry in raw.get("values", [])
    }
    counters = {str(kind): int(count) for kind, count in raw.get("counters", {}).items()}
    return SessionData(name=name, vault=PlaceholderVault(counters=counters, values=values), path=path)


def save_session(session: SessionData) -> None:
    _ensure_private_dir(session.path.parent)
    values = [
        {"kind": kind, "value": value, "placeholder": placeholder}
        for (kind, value), placeholder in sorted(session.vault.values.items(), key=lambda item: item[1])
    ]
    existing_created = None
    if session.path.exists():
        try:
            existing_created = json.loads(session.path.read_text(encoding="utf-8")).get("created")
        except json.JSONDecodeError:
            existing_created = None
    payload = {
        "name": session.name,
        "created": existing_created or _now_iso(),
        "updated": _now_iso(),
        "counters": session.vault.counters,
        "values": values,
    }
    _write_private_text(session.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def select_session(name: str, *, session_dir: Path, state_file: Path) -> None:
    session = load_session(name, session_dir)
    save_session(session)
    _write_private_state_text(state_file, f"{name}\n")


def init_session(name: str, *, session_dir: Path, state_file: Path) -> Path:
    session = load_session(name, session_dir)
    save_session(session)
    _write_private_state_text(state_file, f"{name}\n")
    return session.path


def resolve_session_name(explicit_session: str | None, *, state_file: Path) -> str | None:
    if explicit_session:
        return explicit_session
    if state_file.exists():
        selected = state_file.read_text(encoding="utf-8").strip()
        if selected:
            return selected
    return None


def clear_selected_session(state_file: Path) -> None:
    if state_file.exists():
        state_file.unlink()


def forget_session(name: str, *, session_dir: Path, state_file: Path) -> None:
    path = _session_path(session_dir, name)
    if path.exists():
        path.unlink()
    if state_file.exists() and state_file.read_text(encoding="utf-8").strip() == name:
        state_file.unlink()


def format_counts(counts: dict[str, int]) -> str:
    return " ".join(f"{kind}={counts[kind]}" for kind in sorted(counts) if counts[kind] > 0)


def auto_output_path(input_file: Path, *, timestamp: bool = False, output_dir: Path | None = None) -> Path:
    input_file = input_file.expanduser()
    suffix = input_file.suffix
    stem = input_file.stem
    timestamp_part = f"-{datetime.now().strftime('%Y%m%d-%H%M%S')}" if timestamp else ""
    redacted_dir = output_dir.expanduser() if output_dir else input_file.parent / "redacted"
    return redacted_dir / f"{stem}-redacted{timestamp_part}{suffix}"


def auto_paste_output_path(*, output_dir: Path | None = None) -> Path:
    redacted_dir = output_dir.expanduser() if output_dir else Path.cwd() / "redacted"
    timestamp_part = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = redacted_dir / f"paste-redacted-{timestamp_part}.txt"
    counter = 2
    while candidate.exists():
        candidate = redacted_dir / f"paste-redacted-{timestamp_part}-{counter}.txt"
        counter += 1
    return candidate


def explicit_output_path(requested_output: Path, *, input_file: Path | None) -> tuple[Path, bool]:
    requested_output = requested_output.expanduser()
    if requested_output.is_dir():
        if input_file:
            return auto_output_path(input_file, output_dir=requested_output), True
        return auto_paste_output_path(output_dir=requested_output), True
    return requested_output, False


def auto_output_dir_for_profile(profile_options: RedactorOptions | None, *, config_path: Path, profile_name: str | None) -> Path | None:
    if profile_options and profile_options.term_files:
        return profile_options.term_files[0].expanduser().parent / "redacted"
    if profile_name:
        return profile_dir(config_path, profile_name) / "redacted"
    return None


def session_summaries(session_dir: Path) -> list[tuple[str, str]]:
    session_dir = session_dir.expanduser()
    if not session_dir.exists():
        return []
    summaries: list[tuple[str, str]] = []
    for path in sorted(session_dir.glob("*.json")):
        session = load_session(path.stem, session_dir)
        counts = format_counts(session.vault.status_counts())
        summaries.append((session.name, counts))
    return summaries


def list_sessions(session_dir: Path, *, state_file: Path = DEFAULT_SESSION_STATE_FILE) -> list[str]:
    selected = state_file.read_text(encoding="utf-8").strip() if state_file.exists() else ""
    return [f"{'*' if name == selected else ' '} {_format_session_label(name, counts)}" for name, counts in session_summaries(session_dir)]


def session_selector_labels(session_dir: Path) -> tuple[list[str], list[str]]:
    summaries = session_summaries(session_dir)
    return [name for name, _counts in summaries], [_format_session_label(name, counts) for name, counts in summaries]


def _format_session_label(name: str, counts: str = "") -> str:
    return f"{name}: {counts}" if counts else f"{name}:"


def _format_mapping_lines(matches: list[tuple[str, str]]) -> list[str]:
    return [f"{placeholder} = {value}" for placeholder, value in sorted(matches)]


def show_mapping(session: SessionData, selector: str) -> list[str]:
    normalized = selector.strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    matches: list[tuple[str, str]] = []
    if re.fullmatch(r"[A-Z_]+_\d+", normalized):
        placeholder = f"[{normalized}]"
        for (_kind, value), stored_placeholder in session.vault.values.items():
            if stored_placeholder == placeholder:
                matches.append((stored_placeholder, value))
    else:
        kind = normalized.upper()
        for (stored_kind, value), placeholder in session.vault.values.items():
            if stored_kind == kind:
                matches.append((placeholder, value))
    return _format_mapping_lines(matches)


def show_all_mappings(session: SessionData) -> list[str]:
    return _format_mapping_lines([(placeholder, value) for (_kind, value), placeholder in session.vault.values.items()])


def restore_session_placeholders(text: str, session: SessionData) -> tuple[str, dict[str, int], list[str]]:
    values_by_placeholder = {
        placeholder: (kind, value)
        for (kind, value), placeholder in session.vault.values.items()
    }
    counts: dict[str, int] = {}
    unknown: list[str] = []
    seen_unknown: set[str] = set()

    def replace_match(match: Match[str]) -> str:
        placeholder = match.group(0)
        normalized = placeholder.strip("[]")
        mapped = values_by_placeholder.get(placeholder)
        if not mapped:
            if normalized not in seen_unknown:
                seen_unknown.add(normalized)
                unknown.append(normalized)
            return placeholder
        kind, value = mapped
        counts[kind] = counts.get(kind, 0) + 1
        return value

    return RESTORE_PLACEHOLDER_RE.sub(replace_match, text), counts, unknown


def append_terms(path: Path, raw_terms: str) -> int:
    path = path.expanduser()
    new_terms = [line.strip() for line in raw_terms.splitlines() if line.strip()]
    existing_terms = {_term_identity(term) for term in _read_term_file(path)} if path.exists() else set()
    terms_to_add = [term for term in new_terms if _term_identity(term) and _term_identity(term) not in existing_terms]
    if not terms_to_add:
        return 0

    existing_content = path.read_text(encoding="utf-8") if path.exists() else ""
    text = ""
    if existing_content and not existing_content.endswith("\n"):
        text += "\n"
    text += "".join(f"{term}\n" for term in terms_to_add)
    _append_private_text(path, text)
    return len(terms_to_add)


def _render_redaction_term(term: RedactionTerm) -> str:
    return f"{term.kind}: {term.term}"


def append_redaction_terms(path: Path, terms: Sequence[RedactionTerm]) -> int:
    return append_terms(path, "\n".join(_render_redaction_term(term) for term in terms))


def _term_add_candidate(line: str) -> tuple[RedactionTerm | None, str | None]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None, None
    parsed = _parse_explicit_term_line(stripped)
    if parsed is None:
        parsed = RedactionTerm(term=stripped, kind=_guess_term_kind(stripped))
    try:
        ipaddress.ip_address(parsed.term)
    except ValueError:
        return parsed, None
    return None, f"skipped {parsed.term}: IP addresses are already redacted by deterministic IP detectors"


def term_add_candidates(raw_terms: str) -> tuple[list[RedactionTerm], list[str]]:
    candidates: list[RedactionTerm] = []
    notices: list[str] = []
    seen: set[str] = set()
    for line in raw_terms.splitlines():
        candidate, notice = _term_add_candidate(line)
        if notice:
            notices.append(notice)
        if candidate is None:
            continue
        identity = candidate.term.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(candidate)
    return candidates, notices


def _next_term_kind(kind: str, kind_order: Sequence[str] = TERM_KIND_ORDER) -> str:
    if not kind_order:
        return "CLIENT"
    normalized = _normalize_manual_detector_kind(kind)
    try:
        index = list(kind_order).index(normalized)
    except ValueError:
        return kind_order[0]
    return kind_order[(index + 1) % len(kind_order)]


def append_suggestion_terms(path: Path, suggestions: list[AISuggestion]) -> int:
    return append_redaction_terms(
        path, [RedactionTerm(term=suggestion.term, kind=_ai_suggestion_term_kind(suggestion)) for suggestion in suggestions]
    )


def append_ignored_suggestions(path: Path, suggestions: list[AISuggestion]) -> int:
    if not suggestions:
        return 0
    ignored_terms = [
        _render_redaction_term(RedactionTerm(term=suggestion.term, kind=_ai_suggestion_term_kind(suggestion)))
        for suggestion in suggestions
    ]
    return append_terms(path, "\n".join(ignored_terms))


def append_unassigned_ignored_terms(path: Path, raw_terms: str) -> int:
    ignored_terms = [
        _render_redaction_term(RedactionTerm(term=line.strip(), kind=UNASSIGNED_IGNORED_KIND))
        for line in raw_terms.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return append_terms(path, "\n".join(ignored_terms))


def _ignored_term_line(raw_term: str) -> str:
    parsed = _parse_term_line(raw_term)
    return _render_redaction_term(parsed) if parsed else raw_term.strip()


def _term_line_from_ignored(raw_term: str) -> str:
    parsed = _parse_term_line(raw_term)
    if parsed:
        return _render_redaction_term(parsed)
    term = raw_term.strip()
    return _render_redaction_term(RedactionTerm(term=term, kind=_guess_term_kind(term))) if term else ""


def move_profile_terms_to_ignored(term_files: Sequence[Path], ignored_file: Path, terms: Sequence[str]) -> tuple[int, int]:
    terms_to_ignore = [_ignored_term_line(term) for term in terms if _ignored_term_line(term)]
    added_count = append_terms(ignored_file, "\n".join(terms_to_ignore)) if terms_to_ignore else 0
    removed_count = sum(remove_profile_term(term_file, term) for term in terms for term_file in term_files)
    return removed_count, added_count


def move_ignored_terms_to_profile(ignored_files: Sequence[Path], term_file: Path, terms: Sequence[str]) -> tuple[int, int]:
    terms_to_add = [_term_line_from_ignored(term) for term in terms if _term_line_from_ignored(term)]
    added_count = append_terms(term_file, "\n".join(terms_to_add)) if terms_to_add else 0
    removed_count = sum(remove_ignored_suggestion(ignored_file, term) for term in terms for ignored_file in ignored_files)
    return removed_count, added_count


def _clipboard_env_for_wayland() -> dict[str, str]:
    env = os.environ.copy()
    if env.get("WAYLAND_DISPLAY"):
        return env
    runtime_dir = env.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        return env
    runtime = Path(runtime_dir)
    for socket in sorted(runtime.glob("wayland-*")):
        env["WAYLAND_DISPLAY"] = socket.name
        return env
    return env


def copy_to_clipboard(text: str) -> str | None:
    copied_with: list[str] = []
    verification_error: RuntimeError | None = None
    if shutil.which("wl-copy"):
        try:
            wayland_env = _clipboard_env_for_wayland()
            subprocess.run(
                ["wl-copy", "--type", "text/plain"],
                input=text,
                text=True,
                check=True,
                env=wayland_env,
                stderr=subprocess.DEVNULL,
            )
            copied_with.append("wl-copy")
            if shutil.which("wl-paste"):
                pasted = subprocess.run(
                    ["wl-paste", "--no-newline"],
                    text=True,
                    check=True,
                    capture_output=True,
                    env=wayland_env,
                ).stdout
                if pasted != text:
                    verification_error = RuntimeError("clipboard verification failed after wl-copy")
                    copied_with.remove("wl-copy")
        except subprocess.CalledProcessError:
            pass
    if shutil.which("xclip"):
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text,
                text=True,
                check=True,
                stderr=subprocess.DEVNULL,
            )
            copied_with.append("xclip")
        except subprocess.CalledProcessError:
            pass
    elif shutil.which("xsel"):
        try:
            subprocess.run(
                ["xsel", "--clipboard", "--input"],
                input=text,
                text=True,
                check=True,
                stderr=subprocess.DEVNULL,
            )
            copied_with.append("xsel")
        except subprocess.CalledProcessError:
            pass
    if not copied_with and shutil.which("pbcopy"):
        try:
            subprocess.run(["pbcopy"], input=text, text=True, check=True)
            copied_with.append("pbcopy")
        except subprocess.CalledProcessError:
            pass
    if copied_with:
        return "+".join(copied_with)
    if verification_error:
        raise verification_error
    return None


def _replace_with(kind: str, vault: PlaceholderVault):
    return lambda match: vault.placeholder_for(kind, match.group(0))


def _redact_url_host(match: Match[str], vault: PlaceholderVault) -> str:
    value = match.group(0)
    try:
        parsed = urlsplit(value)
    except ValueError:
        if PLACEHOLDER_OR_MASK_RE.search(value):
            return value
        return vault.placeholder_for("URL", value)
    if not parsed.hostname:
        return vault.placeholder_for("URL", value)
    host_kind = _ipv4_placeholder_kind(parsed.hostname) or "HOST"
    host = vault.placeholder_for(host_kind, parsed.hostname)
    userinfo = ""
    if "@" in parsed.netloc:
        raw_userinfo = parsed.netloc.rsplit("@", 1)[0]
        userinfo = f"{vault.placeholder_for('TOKEN', raw_userinfo)}@"
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    netloc = f"{userinfo}{netloc}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _looks_like_product_version_dotted_quad(text: str, start: int) -> bool:
    if start == 0 or text[start - 1] != "/":
        line_start = text.rfind("\n", 0, start) + 1
        prefix = text[line_start:start].rstrip()
        return bool(
            re.search(
                r"(?i)(?:\b(?:apache(?: tomcat)?|tomcat|nginx|chrome|firefox|safari|edge|curl|python|java|node|php))$",
                prefix,
            )
        )
    token_start = max(text.rfind(" ", 0, start), text.rfind("\t", 0, start), text.rfind("\n", 0, start)) + 1
    product = text[token_start : start - 1]
    return bool(product and re.search(r"[A-Za-z]", product))


def _redact_host_header(match: Match[str], vault: PlaceholderVault) -> str:
    port = match.group(3) or ""
    return f"{match.group(1)}{vault.placeholder_for('HOST', match.group(2))}{port}{match.group(4)}"


def redact_with_counts(
    text: str,
    *,
    client_terms: Sequence[str | RedactionTerm] | None = None,
    vault: PlaceholderVault | None = None,
    disabled_categories: set[str] | None = None,
) -> RedactionResult:
    vault = vault or PlaceholderVault()
    output = text

    if _category_enabled("TERMS", disabled_categories):
        for term_re, kind in _compile_terms(client_terms):
            output = term_re.sub(lambda match, term_kind=kind: vault.placeholder_for(term_kind, match.group(0).casefold()), output)

    if _category_enabled("TOKEN", disabled_categories):
        output = AUTH_BEARER_RE.sub(
            lambda match: match.group(1) + vault.placeholder_for("TOKEN", match.group(2)), output
        )
        output = PRIVATE_KEY_BLOCK_RE.sub(_replace_with("TOKEN", vault), output)
        output = JWT_RE.sub(_replace_with("TOKEN", vault), output)
        output = SLACK_TOKEN_RE.sub(_replace_with("TOKEN", vault), output)
        output = DISCORD_TOKEN_RE.sub(_replace_with("TOKEN", vault), output)
        output = ASSIGNMENT_SECRET_RE.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{vault.placeholder_for('TOKEN', match.group(3))}",
            output,
        )
        output = AWS_KEY_RE.sub(_replace_with("TOKEN", vault), output)
        output = GITHUB_TOKEN_RE.sub(_replace_with("TOKEN", vault), output)
    if _category_enabled("COOKIE", disabled_categories):
        output = COOKIE_VALUE_RE.sub(
            lambda match: f"{match.group(1)}={vault.placeholder_for('COOKIE', match.group(2))}", output
        )
    if _category_enabled("HOST", disabled_categories):
        output = URL_RE.sub(lambda match: _redact_url_host(match, vault), output)
        output = HOST_HEADER_RE.sub(lambda match: _redact_host_header(match, vault), output)
        output = HOST_RE.sub(_replace_with("HOST", vault), output)
    if _category_enabled("EMAIL", disabled_categories):
        output = EMAIL_RE.sub(_replace_with("EMAIL", vault), output)
    if _category_enabled("PHONE", disabled_categories):
        output = LT_PHONE_RE.sub(_replace_with("PHONE", vault), output)
        output = INTL_PHONE_RE.sub(_replace_with("PHONE", vault), output)
        output = PHONE_RE.sub(_replace_with("PHONE", vault), output)
    if _category_enabled("UUID", disabled_categories):
        output = UUID_RE.sub(_replace_with("UUID", vault), output)
    if _category_enabled("PATH", disabled_categories):
        output = WINDOWS_PATH_RE.sub(_replace_with("PATH", vault), output)
        output = PATH_RE.sub(_replace_with("PATH", vault), output)

    def ip_replacer(match: Match[str]) -> str:
        value = match.group(0)
        if _looks_like_product_version_dotted_quad(output, match.start()):
            return value
        if _is_internal_ipv4(value):
            if not _category_enabled("INTERNAL_IP", disabled_categories):
                return value
            return vault.placeholder_for("INTERNAL_IP", value)
        if not _category_enabled("PUBLIC_IP", disabled_categories):
            return value
        return vault.placeholder_for("PUBLIC_IP", value)

    output = IPV4_RE.sub(ip_replacer, output)
    return RedactionResult(text=output, counts=dict(vault.replacement_counts), vault=vault)


def redact_text(text: str, *, client_terms: Sequence[str | RedactionTerm] | None = None) -> str:
    return redact_with_counts(text, client_terms=client_terms).text


def format_summary(counts: dict[str, int], warnings: list[str], disabled_categories: set[str] | None = None) -> str:
    parts = [f"{kind}={counts[kind]}" for kind in sorted(counts) if counts[kind] > 0]
    warning_text = ", ".join(warnings) if warnings else "none"
    parts.append(f"warnings={warning_text}")
    if disabled_categories:
        parts.append(f"disabled_categories={_format_disabled_categories(disabled_categories)}")
    return "summary: " + " ".join(parts)


def write_report(
    path: Path,
    *,
    counts: dict[str, int],
    profile: str | None,
    copy_enabled: bool,
    warnings: list[str],
    ai_warnings: list[AIWarning] | None = None,
    ai_suggestions: list[AISuggestion] | None = None,
    disabled_categories: set[str] | None = None,
) -> None:
    payload = {
        "copy_enabled": copy_enabled,
        "counts": {kind: counts[kind] for kind in sorted(counts) if counts[kind] > 0},
        "profile": profile,
        "warnings": warnings,
    }
    if ai_warnings is not None:
        payload["ai_warnings"] = [warning.as_report_dict() for warning in ai_warnings]
    if ai_suggestions is not None:
        payload["ai_suggestions"] = [suggestion.as_report_dict(include_term=False) for suggestion in ai_suggestions]
    if disabled_categories:
        payload["disabled_categories"] = sorted(disabled_categories)
    _write_private_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _endpoint_is_local_or_private(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_AI_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler)


def _open_ai_request(request: Request, *, timeout: float):
    return _AI_NO_REDIRECT_OPENER.open(request, timeout=timeout)  # nosec B310


def _numbered_lines(text: str) -> tuple[str, int]:
    lines = text.splitlines() or [""]
    return "\n".join(f"{index}: {line}" for index, line in enumerate(lines, start=1)), len(lines)


def _numbered_line_chunks(
    text: str,
    *,
    max_chars: int | None = None,
    max_lines: int | None = None,
) -> tuple[list[tuple[str, int, int]], int]:
    max_chars = max_chars or DEFAULT_AI_CHUNK_MAX_CHARS
    max_lines = max_lines or DEFAULT_AI_CHUNK_MAX_LINES
    lines = text.splitlines() or [""]
    chunks: list[tuple[str, int, int]] = []
    current: list[str] = []
    current_start = 1
    current_chars = 0

    for index, line in enumerate(lines, start=1):
        numbered_line = f"{index}: {line}"
        line_chars = len(numbered_line) + 1
        if line_chars > max_chars:
            if current:
                chunks.append(("\n".join(current), current_start, index - 1))
                current = []
                current_chars = 0
            prefix = f"{index}: "
            segment_size = max(1, max_chars - len(prefix) - 1)
            for start in range(0, len(line), segment_size):
                chunks.append((f"{prefix}{line[start:start + segment_size]}", index, index))
            current_start = index + 1
            continue
        if current and (len(current) >= max_lines or current_chars + line_chars > max_chars):
            chunks.append(("\n".join(current), current_start, index - 1))
            current = []
            current_start = index
            current_chars = 0
        current.append(numbered_line)
        current_chars += line_chars

    if current:
        chunks.append(("\n".join(current), current_start, current_start + len(current) - 1))
    return chunks, len(lines)


def _normalize_ai_warning(raw: object, *, max_line: int | None = None) -> AIWarning | None:
    if not isinstance(raw, dict):
        return None
    category = str(raw.get("category", "context")).strip().lower()
    category = re.sub(r"[^a-z0-9_-]+", "-", category).strip("-") or "context"
    allowed_categories = {"person", "organization", "project", "location", "context"}
    if category not in allowed_categories:
        matched_categories = [known for known in allowed_categories if known in category.split("-")]
        category = matched_categories[0] if len(matched_categories) == 1 else "context"
    line_value = raw.get("line")
    line = int(line_value) if isinstance(line_value, int) and line_value > 0 else None
    if line is not None and max_line is not None and line > max_line:
        return None
    confidence_value = raw.get("confidence")
    confidence = str(confidence_value).strip().lower() if confidence_value is not None else None
    if confidence:
        confidence = re.sub(r"[^a-z0-9_-]+", "-", confidence).strip("-") or None
    return AIWarning(category=category, line=line, confidence=confidence)


def _normalize_category(value: object) -> str:
    category = str(value or "context").strip().lower()
    category = re.sub(r"[^a-z0-9_-]+", "-", category).strip("-") or "context"
    allowed_categories = set(AI_SUGGESTION_CATEGORIES)
    if category not in allowed_categories:
        matched_categories = [known for known in allowed_categories if known in category.split("-")]
        category = matched_categories[0] if len(matched_categories) == 1 else "context"
    return category


def _next_ai_suggestion_category(category: str) -> str:
    category = _normalize_category(category)
    current_index = AI_SUGGESTION_CATEGORIES.index(category)
    return AI_SUGGESTION_CATEGORIES[(current_index + 1) % len(AI_SUGGESTION_CATEGORIES)]


def _normalize_confidence(value: object) -> str | None:
    confidence = str(value).strip().lower() if value is not None else None
    if confidence:
        confidence = re.sub(r"[^a-z0-9_-]+", "-", confidence).strip("-") or None
    return confidence


def _normalize_ai_suggestion(raw: object, *, max_line: int | None = None) -> AISuggestion | None:
    if not isinstance(raw, dict):
        return None
    term = str(raw.get("term", "")).strip()
    if not term or PLACEHOLDER_OR_MASK_RE.fullmatch(term):
        return None
    category = _normalize_category(raw.get("category", "context"))
    raw_lines = raw.get("lines", raw.get("line", []))
    if isinstance(raw_lines, int):
        line_values = [raw_lines]
    elif isinstance(raw_lines, list):
        line_values = raw_lines
    else:
        line_values = []
    lines = tuple(
        sorted(
            {
                int(line)
                for line in line_values
                if isinstance(line, int) and line > 0 and (max_line is None or line <= max_line)
            }
        )
    )
    return AISuggestion(term=term, category=category, lines=lines, confidence=_normalize_confidence(raw.get("confidence")))


def _extract_ai_warnings(payload: dict[str, object], *, max_line: int | None = None) -> list[AIWarning]:
    content = ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = str(message.get("content", ""))
    if not content:
        return []
    try:
        parsed_content = json.loads(content)
    except json.JSONDecodeError:
        return [AIWarning(category="ai-checker-unparseable-response")]
    warnings = parsed_content.get("warnings") if isinstance(parsed_content, dict) else None
    if not isinstance(warnings, list):
        return []
    return [warning for warning in (_normalize_ai_warning(item, max_line=max_line) for item in warnings) if warning is not None]


def _extract_ai_suggestions(payload: dict[str, object], *, max_line: int | None = None) -> list[AISuggestion]:
    content = ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = str(message.get("content", ""))
    if not content:
        return []
    try:
        parsed_content = json.loads(content)
    except json.JSONDecodeError:
        return []
    candidates = parsed_content.get("candidates") if isinstance(parsed_content, dict) else None
    if not isinstance(candidates, list):
        return []
    suggestions = [
        suggestion
        for suggestion in (_normalize_ai_suggestion(item, max_line=max_line) for item in candidates)
        if suggestion is not None
    ]
    deduped: dict[tuple[str, str], AISuggestion] = {}
    for suggestion in suggestions:
        key = (suggestion.category, suggestion.term.lower())
        if key not in deduped:
            deduped[key] = suggestion
            continue
        existing = deduped[key]
        merged_lines = tuple(sorted(set(existing.lines) | set(suggestion.lines)))
        deduped[key] = AISuggestion(
            term=existing.term,
            category=existing.category,
            lines=merged_lines,
            confidence=existing.confidence or suggestion.confidence,
        )
    return list(deduped.values())


def _chat_completion_message_content(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content", "")
                return str(content) if content is not None else ""
            delta = first.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content", "")
                return str(content) if content is not None else ""
    return ""


def _read_chat_completion_content(response: object) -> str:
    if not hasattr(response, "readline"):
        raw_payload = response.read()  # type: ignore[attr-defined]
        payload = json.loads(raw_payload.decode("utf-8"))
        return _chat_completion_message_content(payload) if isinstance(payload, dict) else ""

    collected_content: list[str] = []
    raw_lines: list[bytes] = []
    saw_stream_event = False
    while True:
        line = response.readline()  # type: ignore[attr-defined]
        if not line:
            break
        raw_lines.append(line)
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(b"data:"):
            saw_stream_event = True
            event_data = stripped[5:].strip()
            if event_data == b"[DONE]":
                break
            try:
                event_payload = json.loads(event_data.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(event_payload, dict):
                collected_content.append(_chat_completion_message_content(event_payload))

    if saw_stream_event:
        return "".join(collected_content)
    raw_payload = b"".join(raw_lines)
    payload = json.loads(raw_payload.decode("utf-8")) if raw_payload else {}
    return _chat_completion_message_content(payload) if isinstance(payload, dict) else ""


def run_ai_check(
    redacted_text: str,
    *,
    endpoint: str,
    model: str,
    timeout_seconds: float = DEFAULT_AI_TIMEOUT_SECONDS,
    chunk_max_chars: int | None = None,
    chunk_max_lines: int | None = None,
) -> list[AIWarning]:
    if not _endpoint_is_local_or_private(endpoint):
        raise ValueError("AI checker endpoint must be localhost or a private IP")
    reviewed_chunks, line_count = _numbered_line_chunks(
        redacted_text, max_chars=chunk_max_chars, max_lines=chunk_max_lines
    )
    prompt = (
        "Review only the numbered already-redacted lines in the user message for possible remaining sensitive context. "
        "Line numbers are the numeric prefixes before ':'. Use only those line numbers. "
        "Flag realistic human names, organization names, project codenames, office/location names, product names, tenant names, or other client-identifying phrases that are not already placeholders like [CLIENT_1] or [EMAIL_1]. "
        "Return at most 20 warnings per request; prioritize distinct high-confidence categories and avoid repeating the same apparent issue on many lines. "
        "Return only JSON: {\"warnings\":[{\"category\":\"person|organization|project|location|context\","
        "\"line\":1,\"confidence\":\"low|medium|high\"}]}. "
        "Do not quote raw text or include matched values. Return {\"warnings\":[]} when only placeholders remain."
    )
    warnings: list[AIWarning] = []
    for reviewed_text, _start_line, _end_line in reviewed_chunks:
        body = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": reviewed_text},
                ],
                "chat_template_kwargs": {"reasoning_effort": "low"},
                "max_tokens": DEFAULT_AI_CHECK_MAX_TOKENS,
                "stream": True,
                "temperature": 0,
            }
        ).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        # Endpoint scheme/host is validated above; redirects are disabled so a
        # trusted local/private URL cannot hand the request to another host.
        with _open_ai_request(request, timeout=timeout_seconds) as response:
            response_content = _read_chat_completion_content(response)
        if not response_content:
            warnings.append(AIWarning(category="ai-checker-unparseable-response"))
            continue
        warnings.extend(_extract_ai_warnings({"choices": [{"message": {"content": response_content}}]}, max_line=line_count))
    return warnings


def run_ai_suggest(
    redacted_text: str,
    *,
    endpoint: str,
    model: str,
    timeout_seconds: float = DEFAULT_AI_TIMEOUT_SECONDS,
    chunk_max_chars: int | None = None,
    chunk_max_lines: int | None = None,
) -> list[AISuggestion]:
    if not _endpoint_is_local_or_private(endpoint):
        raise ValueError("AI suggestion endpoint must be localhost or a private IP")
    reviewed_chunks, line_count = _numbered_line_chunks(
        redacted_text, max_chars=chunk_max_chars, max_lines=chunk_max_lines
    )
    prompt = (
        "Review only the numbered already-redacted lines in the user message. "
        "Line numbers are the numeric prefixes before ':'. Use only those line numbers. "
        "Extract candidate remaining sensitive terms that an operator may want to add to a redaction profile. "
        "Only include terms still visible in the reviewed text. Do not include placeholders like [CLIENT_1], [EMAIL_1], or ***. "
        "Focus on human names, organization names, project codenames, office/location names, product names, tenant names, or other client-identifying phrases. "
        "Do not include generic AD/Windows built-ins, default shares, stock groups, common tool names, or generic filenames such as Administrator, Guest, krbtgt, Domain Admins, SYSVOL, NETLOGON, BloodHound.py, Evil-WinRM, notes, or notes.txt. "
        "Deduplicate candidates inside this request; return one object per unique visible term and include at most five representative line numbers per candidate. "
        "Return only JSON: {\"candidates\":[{\"term\":\"Visible Term\",\"category\":\"person|organization|project|location|context\","
        "\"lines\":[1],\"confidence\":\"low|medium|high\"}]}. "
        "Return {\"candidates\":[]} when no visible candidate terms remain."
    )
    suggestions: list[AISuggestion] = []
    for reviewed_text, _start_line, _end_line in reviewed_chunks:
        body = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": reviewed_text},
                ],
                "chat_template_kwargs": {"reasoning_effort": "low"},
                "max_tokens": DEFAULT_AI_SUGGEST_MAX_TOKENS,
                "stream": True,
                "temperature": 0,
            }
        ).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        # Endpoint scheme/host is validated above; redirects are disabled so a
        # trusted local/private URL cannot hand the request to another host.
        with _open_ai_request(request, timeout=timeout_seconds) as response:
            response_content = _read_chat_completion_content(response)
        if response_content:
            suggestions.extend(
                _extract_ai_suggestions({"choices": [{"message": {"content": response_content}}]}, max_line=line_count)
            )

    deduped: dict[str, AISuggestion] = {}
    for suggestion in suggestions:
        if _is_generic_ai_suggestion(suggestion):
            continue
        key = suggestion.term.strip().casefold()
        if key not in deduped:
            deduped[key] = suggestion
            continue
        existing = deduped[key]
        preferred = _preferred_ai_suggestion(existing, suggestion)
        deduped[key] = AISuggestion(
            term=preferred.term,
            category=preferred.category,
            lines=tuple(sorted(set(existing.lines) | set(suggestion.lines))),
            confidence=preferred.confidence,
        )
    return list(deduped.values())


def apply_ai_suggestions_to_profile(
    original_text: str,
    *,
    term_file: Path,
    terms: list[str],
    suggestions: list[AISuggestion],
    vault: PlaceholderVault | None,
    disabled_categories: set[str] | None = None,
) -> tuple[RedactionResult | None, int]:
    added_count = append_suggestion_terms(term_file, suggestions)
    if not added_count:
        return None, 0
    terms.extend(
        f"{_ai_suggestion_term_kind(suggestion)}: {suggestion.term}"
        if _ai_suggestion_term_kind(suggestion) != "CLIENT"
        else suggestion.term
        for suggestion in suggestions
    )
    if vault:
        vault.replacement_counts = {}
    redaction_result = redact_with_counts(
        original_text, client_terms=terms, vault=vault or PlaceholderVault(), disabled_categories=disabled_categories
    )
    return redaction_result, added_count


def review_ai_suggestions(
    suggestions: list[AISuggestion],
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    ignored_suggestions: list[AISuggestion] | None = None,
) -> list[AISuggestion] | None:
    if not suggestions:
        print("AI suggestions: none", file=output_stream)
        return []
    suggestions = list(suggestions)
    selected = {index for index in range(1, len(suggestions) + 1)}
    ignored = set[int]()

    def ignore_candidate(index: int) -> None:
        if index in ignored:
            ignored.remove(index)
            selected.add(index)
        else:
            ignored.add(index)
            selected.discard(index)

    def set_candidate_category(index: int, category: str) -> None:
        suggestions[index - 1] = replace(suggestions[index - 1], category=_normalize_category(category))

    def render() -> None:
        print("AI suggestions:", file=output_stream)
        for index, suggestion in enumerate(suggestions, start=1):
            marker = "i" if index in ignored else "x" if index in selected else " "
            lines = ",".join(str(line) for line in suggestion.lines) if suggestion.lines else "-"
            confidence = suggestion.confidence or "-"
            print(
                f"  {index}. [{marker}] {suggestion.term}  {suggestion.category}  lines {lines}  {confidence}",
                file=output_stream,
            )
        print(
            "Commands: Enter=accept checked, number=toggle, i NUMBER=ignore, "
            "c NUMBER CATEGORY=category, a=select all, n=select none, q=cancel",
            file=output_stream,
        )

    while True:
        render()
        print("review> ", end="", file=output_stream, flush=True)
        command = input_stream.readline()
        if command == "":
            return None
        command = command.strip().lower()
        if command in {"", "y", "yes"}:
            if ignored_suggestions is not None:
                ignored_suggestions.extend(
                    suggestion for index, suggestion in enumerate(suggestions, start=1) if index in ignored
                )
            return [suggestion for index, suggestion in enumerate(suggestions, start=1) if index in selected]
        if command in {"q", "quit", "cancel"}:
            return None
        if command in {"a", "all"}:
            selected = {index for index in range(1, len(suggestions) + 1)}
            ignored = set()
            continue
        if command in {"n", "none"}:
            selected = set()
            continue
        if command.startswith("i ") or command.startswith("ignore "):
            _verb, _, raw_index = command.partition(" ")
            raw_index = raw_index.strip()
            if raw_index.isdigit() and 1 <= int(raw_index) <= len(suggestions):
                ignore_candidate(int(raw_index))
                continue
        if command.startswith("c ") or command.startswith("category "):
            _verb, _, raw_change = command.partition(" ")
            raw_index, _, raw_category = raw_change.strip().partition(" ")
            if raw_index.isdigit() and 1 <= int(raw_index) <= len(suggestions) and raw_category:
                set_candidate_category(int(raw_index), raw_category)
                continue
        tokens = [token for token in re.split(r"[\s,]+", command) if token]
        if tokens and all(token.isdigit() and 1 <= int(token) <= len(suggestions) for token in tokens):
            for token in tokens:
                index = int(token)
                ignored.discard(index)
                if index in selected:
                    selected.remove(index)
                else:
                    selected.add(index)
            continue
        print("error: enter a candidate number, i NUMBER, c NUMBER CATEGORY, a, n, q, or Enter", file=output_stream)


def render_ai_suggestion_review_screen(
    suggestions: list[AISuggestion],
    *,
    selected: set[int],
    ignored: set[int] | None = None,
    cursor: int,
    height: int = 15,
) -> str:
    if not suggestions:
        return "AI suggestions: none\n"
    ignored = ignored or set()
    height = max(1, height)
    half_window = height // 2
    start = max(0, min(cursor - half_window, len(suggestions) - height))
    stop = min(len(suggestions), start + height)
    lines = [
        "\x1b[?25l\x1b[2J\x1b[HAI suggestions review",
        "[↑/↓] move  [Enter/Space] toggle  [i] ignore  [c] change category  [a] all  [n] none  [d] accept  [q] cancel",
    ]
    if start > 0:
        lines.append(f"  ... {start} earlier candidate(s) ...")
    for index in range(start, stop):
        suggestion = suggestions[index]
        item_number = index + 1
        pointer = ">" if index == cursor else " "
        marker = "i" if item_number in ignored else "x" if item_number in selected else " "
        rendered_lines = ",".join(str(line) for line in suggestion.lines) if suggestion.lines else "-"
        confidence = suggestion.confidence or "-"
        lines.append(
            f"{pointer} {item_number:>2}. [{marker}] {suggestion.term}  {suggestion.category}  "
            f"lines {rendered_lines}  {confidence}"
        )
    if stop < len(suggestions):
        lines.append(f"  ... {len(suggestions) - stop} later candidate(s) ...")
    lines.append(f"checked: {len(selected)}/{len(suggestions)}  ignored: {len(ignored)}")
    return "\r\n".join(lines) + "\r\n"


def render_term_add_review_screen(
    terms: Sequence[RedactionTerm], *, selected: set[int], cursor: int, height: int = 15, kind_order: Sequence[str] = TERM_KIND_ORDER
) -> str:
    if not terms:
        return "Profile term review: none\n"
    height = max(1, height)
    half_window = height // 2
    start = max(0, min(cursor - half_window, len(terms) - height))
    stop = min(len(terms), start + height)
    lines = [
        "\x1b[?25l\x1b[2J\x1b[HProfile term review",
        "[↑/↓] move  [Enter/Space] toggle add  [c] change manual detector  [a] all  [n] none  [d] accept  [q] cancel",
        "Manual detectors: " + ", ".join(_manual_detector_display(kind) for kind in kind_order),
    ]
    if start > 0:
        lines.append(f"  ... {start} earlier term(s) ...")
    for index in range(start, stop):
        term = terms[index]
        item_number = index + 1
        pointer = ">" if index == cursor else " "
        marker = "x" if item_number in selected else " "
        lines.append(f"{pointer} {item_number:>2}. [{marker}] {term.kind}: {term.term}")
    if stop < len(terms):
        lines.append(f"  ... {len(terms) - stop} later term(s) ...")
    lines.append(f"selected: {len(selected)}/{len(terms)}")
    return "\r\n".join(lines) + "\r\n"


def _read_review_key(input_stream: TextIO) -> str:
    fd = input_stream.fileno()
    key = os.read(fd, 1)
    if not key:
        return ""
    if key != b"\x1b":
        return key.decode("utf-8", errors="replace")

    sequence = bytearray(key)
    ready, _, _ = select.select([fd], [], [], 0.1)
    if not ready:
        return "\x1b"
    suffix = os.read(fd, 1)
    if not suffix:
        return "\x1b"
    sequence.extend(suffix)
    if suffix not in {b"[", b"O"}:
        return sequence.decode("utf-8", errors="replace")

    while len(sequence) < 8:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            break
        part = os.read(fd, 1)
        if not part:
            break
        sequence.extend(part)
        if 0x40 <= part[0] <= 0x7E:
            break
    return sequence.decode("utf-8", errors="replace")


def _filtered_item_indexes(items: Sequence[str], filter_text: str) -> list[int]:
    filter_text = filter_text.casefold()
    if not filter_text:
        return list(range(len(items)))
    return [index for index, item in enumerate(items) if filter_text in item.casefold()]


@dataclass(frozen=True)
class ListEditorState:
    kept: set[int]
    removed: set[int] = field(default_factory=set)
    moved: set[int] = field(default_factory=set)
    filter_text: str = ""
    cursor: int = 0
    filter_mode: bool = False


@dataclass(frozen=True)
class ListEditResult:
    removed: list[str]
    disabled: list[str]
    moved: list[str]
    updated: list[str] = field(default_factory=list)


def _cycle_list_item_detector(item: str, kind_order: Sequence[str] = TERM_KIND_ORDER) -> str:
    parsed = _parse_term_line(item)
    if not parsed:
        return item
    return _render_redaction_term(replace(parsed, kind=_next_term_kind(parsed.kind, kind_order)))


def _clamp_list_editor_cursor(items: Sequence[str], state: ListEditorState) -> ListEditorState:
    visible_indexes = _filtered_item_indexes(items, state.filter_text)
    cursor = max(0, min(state.cursor, len(visible_indexes) - 1)) if visible_indexes else 0
    if cursor == state.cursor:
        return state
    return replace(state, cursor=cursor)


def apply_list_editor_key(
    key: str, *, items: Sequence[str], state: ListEditorState, move_key: str | None = None
) -> tuple[ListEditorState, str | None]:
    state = _clamp_list_editor_cursor(items, state)
    visible_indexes = _filtered_item_indexes(items, state.filter_text)
    if key == "\x03":
        return state, "cancel"
    if key in {"\x1b", "\x1b["}:
        if not state.filter_text and not state.filter_mode:
            return state, None
        return ListEditorState(
            kept=set(state.kept),
            removed=set(state.removed),
            moved=set(state.moved),
            filter_text="",
            cursor=0,
            filter_mode=False,
        ), None
    if state.filter_mode:
        if key in {"\r", "\n"}:
            return replace(state, filter_mode=False), None
        if key in {"\x7f", "\b"}:
            if not state.filter_text:
                return state, None
            return replace(state, filter_text=state.filter_text[:-1], cursor=0), None
        if len(key) == 1 and key.isprintable():
            return replace(state, filter_text=state.filter_text + key, cursor=0), None
        return state, None
    if key == "q":
        return state, "cancel"
    if key in {"\x1b[A", "\x1bOA", "k"}:
        return replace(state, cursor=max(0, state.cursor - 1)), None
    if key in {"\x1b[B", "\x1bOB", "j"}:
        return replace(state, cursor=min(max(0, len(visible_indexes) - 1), state.cursor + 1)), None
    if key in {"\x1b[C", "\x1bOC", "\x1b[D", "\x1bOD"}:
        return state, None
    if key in {"\r", "\n", " "}:
        if visible_indexes:
            item_number = visible_indexes[state.cursor] + 1
            kept = set(state.kept)
            removed = set(state.removed)
            moved = set(state.moved)
            removed.discard(item_number)
            moved.discard(item_number)
            if item_number in kept:
                kept.remove(item_number)
            else:
                kept.add(item_number)
            return replace(state, kept=kept, removed=removed, moved=moved), None
        return state, None
    if key == "a":
        return replace(state, kept={index for index in range(1, len(items) + 1)}, removed=set(), moved=set()), None
    if key == "n":
        return replace(state, kept=set(), removed=set(), moved=set()), None
    if key == "r":
        if visible_indexes:
            item_number = visible_indexes[state.cursor] + 1
            kept = set(state.kept)
            removed = set(state.removed)
            moved = set(state.moved)
            if item_number in removed:
                removed.remove(item_number)
                kept.add(item_number)
            else:
                removed.add(item_number)
                kept.discard(item_number)
                moved.discard(item_number)
            return replace(state, kept=kept, removed=removed, moved=moved), None
        return state, None
    if move_key and key == move_key:
        if visible_indexes:
            item_number = visible_indexes[state.cursor] + 1
            moved = set(state.moved)
            removed = set(state.removed)
            kept = set(state.kept)
            removed.discard(item_number)
            if item_number in moved:
                moved.remove(item_number)
                kept.add(item_number)
            else:
                moved.add(item_number)
                kept.discard(item_number)
            return replace(state, kept=kept, removed=removed, moved=moved), None
        return state, None
    if key in {"d", "y"}:
        return state, "accept"
    if key in {"f", "/"}:
        return replace(state, filter_mode=True), None
    if key in {"\x7f", "\b"}:
        if not state.filter_text:
            return state, None
        return replace(state, filter_text=state.filter_text[:-1], cursor=0), None
    return state, None


def render_list_editor_screen(
    title: str,
    items: Sequence[str],
    *,
    kept: set[int],
    removed: set[int] | None = None,
    moved: set[int] | None = None,
    move_key: str | None = None,
    move_label: str | None = None,
    change_label: str | None = "change detector",
    cursor: int,
    filter_text: str = "",
    filter_mode: bool = False,
    height: int = 15,
) -> str:
    removed = removed or set()
    moved = moved or set()
    if not items:
        return f"{title}: none\n"
    visible_indexes = _filtered_item_indexes(items, filter_text)
    if not visible_indexes:
        controls = (
            "typing filter: [Enter] confirm filter  [Esc] clear filter"
            if filter_mode
            else _list_editor_controls(move_key=move_key, move_label=move_label, change_label=change_label, empty=True)
        )
        lines = [
            f"\x1b[?25l\x1b[2J\x1b[H{title}",
            controls,
            f"filter: {filter_text}{' (typing)' if filter_mode else ''}",
            "no matching item(s)",
            f"enabled: {len(kept)}/{len(items)}  remove: {len(removed)}",
        ]
        return "\r\n".join(lines) + "\r\n"
    height = max(1, height)
    half_window = height // 2
    cursor = max(0, min(cursor, len(visible_indexes) - 1))
    start = max(0, min(cursor - half_window, len(visible_indexes) - height))
    stop = min(len(visible_indexes), start + height)
    controls = (
        "typing filter: [Enter] confirm filter  [Esc] clear filter"
        if filter_mode
        else _list_editor_controls(move_key=move_key, move_label=move_label, change_label=change_label)
    )
    lines = [
        f"\x1b[?25l\x1b[2J\x1b[H{title}",
        controls,
        f"filter: {filter_text or '-'}{' (typing)' if filter_mode else ''}",
    ]
    if start > 0:
        lines.append(f"  ... {start} earlier item(s) ...")
    for visible_position in range(start, stop):
        item_index = visible_indexes[visible_position]
        item_number = item_index + 1
        pointer = ">" if visible_position == cursor else " "
        if item_number in removed:
            marker = "r"
        elif item_number in moved and move_key:
            marker = move_key
        else:
            marker = "x" if item_number in kept else " "
        lines.append(f"{pointer} {item_number:>2}. [{marker}] {items[item_index]}")
    if stop < len(visible_indexes):
        lines.append(f"  ... {len(visible_indexes) - stop} later item(s) ...")
    lines.append(f"enabled: {len(kept)}/{len(items)}  remove: {len(removed)}")
    return "\r\n".join(lines) + "\r\n"


def _list_editor_controls(
    *,
    move_key: str | None = None,
    move_label: str | None = None,
    change_label: str | None = "change detector",
    empty: bool = False,
) -> str:
    if empty:
        base = "[f] filter  [Esc] clear filter"
    else:
        base = "[↑/↓] move  [Enter/Space] toggle enabled  [r] remove"
        if change_label:
            base = f"{base}  [c] {change_label}"
        base = f"{base}  [f] filter  [Esc] clear  [a] all  [n] none"
    if move_key and move_label:
        base = f"{base}  [{move_key}] {move_label}"
    return f"{base}  [d] accept  [q] cancel"


def render_single_select_screen(
    title: str,
    items: Sequence[str],
    *,
    selected: int | None,
    cursor: int,
    filter_text: str = "",
    filter_mode: bool = False,
    height: int = 15,
) -> str:
    if not items:
        return f"{title}: none\n"
    visible_indexes = _filtered_item_indexes(items, filter_text)
    if not visible_indexes:
        controls = (
            "typing filter: [Enter] confirm filter  [Esc] clear filter"
            if filter_mode
            else "[f] filter  [Esc] clear filter  [d] accept  [q] cancel"
        )
        lines = [
            f"\x1b[?25l\x1b[2J\x1b[H{title}",
            controls,
            f"filter: {filter_text}{' (typing)' if filter_mode else ''}",
            "no matching item(s)",
            f"selected: {selected or '-'}",
        ]
        return "\r\n".join(lines) + "\r\n"
    height = max(1, height)
    half_window = height // 2
    cursor = max(0, min(cursor, len(visible_indexes) - 1))
    start = max(0, min(cursor - half_window, len(visible_indexes) - height))
    stop = min(len(visible_indexes), start + height)
    controls = (
        "typing filter: [Enter] confirm filter  [Esc] clear filter"
        if filter_mode
        else "[↑/↓] move  [Enter/Space] select  [f] filter  [Esc] clear  [d] accept  [q] cancel"
    )
    lines = [
        f"\x1b[?25l\x1b[2J\x1b[H{title}",
        controls,
        f"filter: {filter_text or '-'}{' (typing)' if filter_mode else ''}",
    ]
    if start > 0:
        lines.append(f"  ... {start} earlier item(s) ...")
    for visible_position in range(start, stop):
        item_index = visible_indexes[visible_position]
        item_number = item_index + 1
        pointer = ">" if visible_position == cursor else " "
        marker = "x" if item_number == selected else " "
        lines.append(f"{pointer} {item_number:>2}. [{marker}] {items[item_index]}")
    if stop < len(visible_indexes):
        lines.append(f"  ... {len(visible_indexes) - stop} later item(s) ...")
    lines.append(f"selected: {selected or '-'}")
    return "\r\n".join(lines) + "\r\n"


def select_single_item_screen(
    names: Sequence[str],
    *,
    labels: Sequence[str],
    title: str,
    selected_name: str = "",
    input_stream: TextIO,
    output_stream: TextIO,
) -> str | None:
    names = list(names)
    labels = list(labels)
    if not names:
        print(f"{title}: none", file=output_stream)
        return None
    selected = names.index(selected_name) + 1 if selected_name in names else 1
    state = ListEditorState(kept={selected})
    fd = input_stream.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            state = _clamp_list_editor_cursor(labels, state)
            visible_indexes = _filtered_item_indexes(labels, state.filter_text)
            selected = next(iter(state.kept), 1)
            output_stream.write(
                render_single_select_screen(
                    title,
                    labels,
                    selected=selected,
                    cursor=state.cursor,
                    filter_text=state.filter_text,
                    filter_mode=state.filter_mode,
                )
            )
            output_stream.flush()
            key = _read_review_key(input_stream)
            if key == "\x03":
                return None
            if key in {"\x1b", "\x1b["}:
                if not state.filter_text and not state.filter_mode:
                    continue
                state = ListEditorState(kept=set(state.kept), filter_text="", cursor=0, filter_mode=False)
                continue
            if state.filter_mode:
                if key in {"\r", "\n"}:
                    state = replace(state, filter_mode=False)
                elif key in {"\x7f", "\b"}:
                    if state.filter_text:
                        state = replace(state, filter_text=state.filter_text[:-1], cursor=0)
                elif len(key) == 1 and key.isprintable():
                    state = replace(state, filter_text=state.filter_text + key, cursor=0)
                continue
            if key == "q":
                return None
            if key in {"\x1b[A", "\x1bOA", "k"}:
                state = replace(state, cursor=max(0, state.cursor - 1))
                continue
            if key in {"\x1b[B", "\x1bOB", "j"}:
                state = replace(state, cursor=min(max(0, len(visible_indexes) - 1), state.cursor + 1))
                continue
            if key in {"\x1b[C", "\x1bOC", "\x1b[D", "\x1bOD"}:
                continue
            if key in {"\r", "\n", " "}:
                if visible_indexes:
                    state = replace(state, kept={visible_indexes[state.cursor] + 1})
                continue
            if key in {"d", "y"}:
                selected = next(iter(state.kept), 1)
                return names[selected - 1]
            if key in {"f", "/"}:
                state = replace(state, filter_mode=True)
                continue
            if key in {"\x7f", "\b"}:
                if state.filter_text:
                    state = replace(state, filter_text=state.filter_text[:-1], cursor=0)
                continue
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        output_stream.write("\x1b[?25h\r\n")
        output_stream.flush()


def edit_list_items(
    items: Sequence[str],
    *,
    title: str,
    input_stream: TextIO,
    output_stream: TextIO,
    initial_kept: set[int] | None = None,
) -> list[str] | None:
    items = list(items)
    if not items:
        print(f"{title}: none", file=output_stream)
        return []
    kept = set(initial_kept) if initial_kept is not None else {index for index in range(1, len(items) + 1)}
    filter_text = ""

    def render() -> None:
        print(f"{title}:", file=output_stream)
        if filter_text:
            print(f"filter: {filter_text}", file=output_stream)
        for index, item in enumerate(items, start=1):
            if filter_text and filter_text.casefold() not in item.casefold():
                continue
            marker = "x" if index in kept else " "
            print(f"  {index}. [{marker}] {item}", file=output_stream)
        print(
            "Commands: number=toggle enabled, /TEXT=filter, /=clear filter, "
            "a=enable all, n=disable all, d=accept, q=cancel",
            file=output_stream,
        )

    while True:
        render()
        print("remove> ", end="", file=output_stream, flush=True)
        command = input_stream.readline()
        if command == "":
            return None
        command = command.strip().lower()
        if command in {"q", "quit", "cancel"}:
            return None
        if command in {"a", "all"}:
            kept = {index for index in range(1, len(items) + 1)}
            continue
        if command in {"n", "none", ""}:
            kept = set()
            continue
        if command.startswith("/"):
            filter_text = command[1:]
            continue
        if command in {"d", "done", "accept"}:
            return [item for index, item in enumerate(items, start=1) if index not in kept]
        tokens = [token for token in re.split(r"[\s,]+", command) if token]
        if tokens and all(token.isdigit() and 1 <= int(token) <= len(items) for token in tokens):
            for token in tokens:
                index = int(token)
                if index in kept:
                    kept.remove(index)
                else:
                    kept.add(index)
            continue
        print("error: enter item number(s), a, n, d, or q", file=output_stream)


def edit_list_items_screen(
    items: Sequence[str],
    *,
    title: str,
    input_stream: TextIO,
    output_stream: TextIO,
    initial_kept: set[int] | None = None,
    change_label: str | None = None,
) -> list[str] | None:
    items = list(items)
    if not items:
        print(f"{title}: none", file=output_stream)
        return []
    state = ListEditorState(
        kept=set(initial_kept) if initial_kept is not None else {index for index in range(1, len(items) + 1)}
    )
    fd = input_stream.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            state = _clamp_list_editor_cursor(items, state)
            output_stream.write(
                render_list_editor_screen(
                    title,
                    items,
                    kept=state.kept,
                    cursor=state.cursor,
                    filter_text=state.filter_text,
                    filter_mode=state.filter_mode,
                    change_label=change_label,
                )
            )
            output_stream.flush()
            key = _read_review_key(input_stream)
            state, outcome = apply_list_editor_key(key, items=items, state=state)
            if outcome == "cancel":
                return None
            if outcome == "accept":
                return [item for index, item in enumerate(items, start=1) if index not in state.kept]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        output_stream.write("\x1b[?25h\r\n")
        output_stream.flush()


def edit_list_items_via_tty(
    items: Sequence[str],
    *,
    title: str,
    output_stream: TextIO,
    initial_kept: set[int] | None = None,
    change_label: str | None = None,
) -> list[str] | None:
    if sys.stdin.isatty() and output_stream.isatty():
        return edit_list_items_screen(
            items,
            title=title,
            input_stream=sys.stdin,
            output_stream=output_stream,
            initial_kept=initial_kept,
            change_label=change_label,
        )
    if sys.stdin.isatty():
        return edit_list_items(
            items, title=title, input_stream=sys.stdin, output_stream=output_stream, initial_kept=initial_kept
        )
    try:
        with Path("/dev/tty").open("r", encoding="utf-8") as tty_file:
            if output_stream.isatty():
                return edit_list_items_screen(
                    items,
                    title=title,
                    input_stream=tty_file,
                    output_stream=output_stream,
                    initial_kept=initial_kept,
                    change_label=change_label,
                )
            return edit_list_items(
                items, title=title, input_stream=tty_file, output_stream=output_stream, initial_kept=initial_kept
            )
    except OSError:
        raise RuntimeError("interactive list editing needs a terminal")


def _list_edit_result(original_items: Sequence[str], items: Sequence[str], state: ListEditorState) -> ListEditResult:
    moved = [item for index, item in enumerate(items, start=1) if index in state.moved]
    removed = [item for index, item in enumerate(items, start=1) if index in state.removed]
    disabled = [
        item
        for index, item in enumerate(items, start=1)
        if index not in state.kept and index not in state.moved and index not in state.removed
    ]
    updated = [
        item
        for index, (original, item) in enumerate(zip(original_items, items), start=1)
        if item != original and index not in state.moved and index not in state.removed
    ]
    return ListEditResult(removed=removed, disabled=disabled, moved=moved, updated=updated)


def edit_transferable_list_items(
    items: Sequence[str],
    *,
    title: str,
    move_key: str | None = None,
    move_label: str | None = None,
    input_stream: TextIO,
    output_stream: TextIO,
    initial_kept: set[int] | None = None,
    change_label: str | None = "change detector",
) -> ListEditResult | None:
    items = list(items)
    original_items = list(items)
    if not items:
        print(f"{title}: none", file=output_stream)
        return ListEditResult(removed=[], disabled=[], moved=[])
    state = ListEditorState(
        kept=set(initial_kept) if initial_kept is not None else {index for index in range(1, len(items) + 1)}
    )

    def render() -> None:
        print(f"{title}:", file=output_stream)
        if state.filter_text:
            print(f"filter: {state.filter_text}", file=output_stream)
        for index, item in enumerate(items, start=1):
            if state.filter_text and state.filter_text.casefold() not in item.casefold():
                continue
            if index in state.moved:
                marker = move_key
            else:
                marker = "x" if index in state.kept else " "
            print(f"  {index}. [{marker}] {item}", file=output_stream)
        print(
            "Commands: number=toggle enabled, r NUMBER=remove, "
            f"{f'c NUMBER={change_label}, ' if change_label else ''}"
            "/TEXT=filter, /=clear filter, "
            f"a=enable all, n=disable all{f', {move_key} NUMBER={move_label}' if move_key and move_label else ''}, d=accept, q=cancel",
            file=output_stream,
        )

    while True:
        render()
        print("edit> ", end="", file=output_stream, flush=True)
        command = input_stream.readline()
        if command == "":
            return None
        command = command.strip().lower()
        if command in {"q", "quit", "cancel"}:
            return None
        if command in {"a", "all"}:
            state = replace(state, kept={index for index in range(1, len(items) + 1)}, removed=set(), moved=set())
            continue
        if command in {"n", "none", ""}:
            state = replace(state, kept=set(), removed=set(), moved=set())
            continue
        if command.startswith("/"):
            state = replace(state, filter_text=command[1:], cursor=0)
            continue
        if command in {"d", "done", "accept"}:
            return _list_edit_result(original_items, items, state)
        category_match = re.fullmatch(r"c(?:ategory)?\s+(.+)", command) if change_label else None
        if category_match:
            tokens = [token for token in re.split(r"[\s,]+", category_match.group(1)) if token]
            if tokens and all(token.isdigit() and 1 <= int(token) <= len(items) for token in tokens):
                for token in tokens:
                    item_index = int(token) - 1
                    items[item_index] = _cycle_list_item_detector(items[item_index])
                continue
        action_match = re.fullmatch(r"([A-Za-z])\s+(.+)", command)
        if action_match and action_match.group(1) in ({move_key, "r"} if move_key else {"r"}):
            action_key = action_match.group(1)
            tokens = [token for token in re.split(r"[\s,]+", action_match.group(2)) if token]
            if tokens and all(token.isdigit() and 1 <= int(token) <= len(items) for token in tokens):
                kept = set(state.kept)
                removed = set(state.removed)
                moved = set(state.moved)
                for token in tokens:
                    index = int(token)
                    if action_key == "r":
                        if index in removed:
                            removed.remove(index)
                            kept.add(index)
                        else:
                            removed.add(index)
                            kept.discard(index)
                            moved.discard(index)
                    elif move_key and index in moved:
                        moved.remove(index)
                        kept.add(index)
                    elif move_key:
                        moved.add(index)
                        kept.discard(index)
                        removed.discard(index)
                state = replace(state, kept=kept, removed=removed, moved=moved)
                continue
        tokens = [token for token in re.split(r"[\s,]+", command) if token]
        if tokens and all(token.isdigit() and 1 <= int(token) <= len(items) for token in tokens):
            kept = set(state.kept)
            removed = set(state.removed)
            moved = set(state.moved)
            for token in tokens:
                index = int(token)
                removed.discard(index)
                moved.discard(index)
                if index in kept:
                    kept.remove(index)
                else:
                    kept.add(index)
            state = replace(state, kept=kept, removed=removed, moved=moved)
            continue
        move_error = f", {move_key} NUMBER" if move_key else ""
        change_error = ", c NUMBER" if change_label else ""
        print(f"error: enter item number(s), r NUMBER{change_error}{move_error}, a, n, d, or q", file=output_stream)


def edit_transferable_list_items_screen(
    items: Sequence[str],
    *,
    title: str,
    move_key: str | None = None,
    move_label: str | None = None,
    input_stream: TextIO,
    output_stream: TextIO,
    initial_kept: set[int] | None = None,
    change_label: str | None = "change detector",
) -> ListEditResult | None:
    items = list(items)
    original_items = list(items)
    if not items:
        print(f"{title}: none", file=output_stream)
        return ListEditResult(removed=[], disabled=[], moved=[])
    state = ListEditorState(
        kept=set(initial_kept) if initial_kept is not None else {index for index in range(1, len(items) + 1)}
    )
    fd = input_stream.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            state = _clamp_list_editor_cursor(items, state)
            output_stream.write(
                render_list_editor_screen(
                    title,
                    items,
                    kept=state.kept,
                    removed=state.removed,
                    moved=state.moved,
                    move_key=move_key,
                    move_label=move_label,
                    cursor=state.cursor,
                    filter_text=state.filter_text,
                    filter_mode=state.filter_mode,
                    change_label=change_label,
                )
            )
            output_stream.flush()
            key = _read_review_key(input_stream)
            if change_label and key == "c":
                visible_indexes = _filtered_item_indexes(items, state.filter_text)
                if visible_indexes:
                    item_index = visible_indexes[state.cursor]
                    items[item_index] = _cycle_list_item_detector(items[item_index])
                continue
            state, outcome = apply_list_editor_key(key, items=items, state=state, move_key=move_key)
            if outcome == "cancel":
                return None
            if outcome == "accept":
                return _list_edit_result(original_items, items, state)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        output_stream.write("\x1b[?25h\r\n")
        output_stream.flush()


def edit_transferable_list_items_via_tty(
    items: Sequence[str],
    *,
    title: str,
    move_key: str | None = None,
    move_label: str | None = None,
    output_stream: TextIO,
    initial_kept: set[int] | None = None,
    change_label: str | None = "change detector",
) -> ListEditResult | None:
    if sys.stdin.isatty() and output_stream.isatty():
        return edit_transferable_list_items_screen(
            items,
            title=title,
            move_key=move_key,
            move_label=move_label,
            input_stream=sys.stdin,
            output_stream=output_stream,
            initial_kept=initial_kept,
            change_label=change_label,
        )
    if sys.stdin.isatty():
        return edit_transferable_list_items(
            items,
            title=title,
            move_key=move_key,
            move_label=move_label,
            input_stream=sys.stdin,
            output_stream=output_stream,
            initial_kept=initial_kept,
            change_label=change_label,
        )
    try:
        with Path("/dev/tty").open("r", encoding="utf-8") as tty_file:
            if output_stream.isatty():
                return edit_transferable_list_items_screen(
                    items,
                    title=title,
                    move_key=move_key,
                    move_label=move_label,
                    input_stream=tty_file,
                    output_stream=output_stream,
                    initial_kept=initial_kept,
                    change_label=change_label,
                )
            return edit_transferable_list_items(
                items,
                title=title,
                move_key=move_key,
                move_label=move_label,
                input_stream=tty_file,
                output_stream=output_stream,
                initial_kept=initial_kept,
                change_label=change_label,
            )
    except OSError:
        raise RuntimeError("interactive list editing needs a terminal")


def review_ai_suggestions_screen(
    suggestions: list[AISuggestion],
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    ignored_suggestions: list[AISuggestion] | None = None,
) -> list[AISuggestion] | None:
    if not suggestions:
        print("AI suggestions: none", file=output_stream)
        return []
    suggestions = list(suggestions)
    selected = {index for index in range(1, len(suggestions) + 1)}
    ignored = set[int]()
    cursor = 0

    def ignore_current_candidate() -> None:
        item_number = cursor + 1
        if item_number in ignored:
            ignored.remove(item_number)
            selected.add(item_number)
        else:
            ignored.add(item_number)
            selected.discard(item_number)

    def cycle_current_category() -> None:
        suggestions[cursor] = replace(
            suggestions[cursor], category=_next_ai_suggestion_category(suggestions[cursor].category)
        )

    fd = input_stream.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            output_stream.write(render_ai_suggestion_review_screen(suggestions, selected=selected, ignored=ignored, cursor=cursor))
            output_stream.flush()
            key = _read_review_key(input_stream)
            if key in {"\x03", "q"}:
                return None
            if key in {"\x1b[A", "\x1bOA", "k"}:
                cursor = max(0, cursor - 1)
                continue
            if key in {"\x1b[B", "\x1bOB", "j"}:
                cursor = min(len(suggestions) - 1, cursor + 1)
                continue
            if key in {"\r", "\n", " "}:
                item_number = cursor + 1
                ignored.discard(item_number)
                if item_number in selected:
                    selected.remove(item_number)
                else:
                    selected.add(item_number)
                continue
            if key == "a":
                selected = {index for index in range(1, len(suggestions) + 1)}
                ignored = set()
                continue
            if key == "n":
                selected = set()
                continue
            if key == "i":
                ignore_current_candidate()
                continue
            if key == "c":
                cycle_current_category()
                continue
            if key in {"d", "y"}:
                if ignored_suggestions is not None:
                    ignored_suggestions.extend(
                        suggestion for index, suggestion in enumerate(suggestions, start=1) if index in ignored
                    )
                return [suggestion for index, suggestion in enumerate(suggestions, start=1) if index in selected]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        output_stream.write("\x1b[?25h\r\n")
        output_stream.flush()


def review_terms_to_add_screen(
    terms: list[RedactionTerm], *, input_stream: TextIO, output_stream: TextIO, kind_order: Sequence[str] = TERM_KIND_ORDER
) -> list[RedactionTerm] | None:
    if not terms:
        print("Profile term review: none", file=output_stream)
        return []
    terms = list(terms)
    selected = {index for index in range(1, len(terms) + 1)}
    cursor = 0
    fd = input_stream.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            output_stream.write(render_term_add_review_screen(terms, selected=selected, cursor=cursor, kind_order=kind_order))
            output_stream.flush()
            key = _read_review_key(input_stream)
            if key in {"\x03", "q"}:
                return None
            if key in {"\x1b[A", "\x1bOA", "k"}:
                cursor = max(0, cursor - 1)
                continue
            if key in {"\x1b[B", "\x1bOB", "j"}:
                cursor = min(len(terms) - 1, cursor + 1)
                continue
            if key in {"\r", "\n", " "}:
                item_number = cursor + 1
                if item_number in selected:
                    selected.remove(item_number)
                else:
                    selected.add(item_number)
                continue
            if key == "a":
                selected = {index for index in range(1, len(terms) + 1)}
                continue
            if key == "n":
                selected = set()
                continue
            if key == "c":
                terms[cursor] = replace(terms[cursor], kind=_next_term_kind(terms[cursor].kind, kind_order))
                continue
            if key in {"d", "y"}:
                return [term for index, term in enumerate(terms, start=1) if index in selected]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        output_stream.write("\x1b[?25h\r\n")
        output_stream.flush()


def review_terms_to_add_via_tty(
    terms: list[RedactionTerm], *, output_stream: TextIO, kind_order: Sequence[str] = TERM_KIND_ORDER
) -> list[RedactionTerm] | None:
    if sys.stdin.isatty() and output_stream.isatty():
        return review_terms_to_add_screen(terms, input_stream=sys.stdin, output_stream=output_stream, kind_order=kind_order)
    return terms


def review_ai_suggestions_via_tty(
    suggestions: list[AISuggestion], *, output_stream: TextIO, ignored_suggestions: list[AISuggestion] | None = None
) -> list[AISuggestion] | None:
    if sys.stdin.isatty() and output_stream.isatty():
        return review_ai_suggestions_screen(
            suggestions, input_stream=sys.stdin, output_stream=output_stream, ignored_suggestions=ignored_suggestions
        )
    if sys.stdin.isatty():
        return review_ai_suggestions(
            suggestions, input_stream=sys.stdin, output_stream=output_stream, ignored_suggestions=ignored_suggestions
        )
    try:
        with Path("/dev/tty").open("r", encoding="utf-8") as tty_file:
            if output_stream.isatty():
                return review_ai_suggestions_screen(
                    suggestions, input_stream=tty_file, output_stream=output_stream, ignored_suggestions=ignored_suggestions
                )
            return review_ai_suggestions(
                suggestions, input_stream=tty_file, output_stream=output_stream, ignored_suggestions=ignored_suggestions
            )
    except OSError:
        raise RuntimeError("--ai-suggest needs an interactive terminal for suggestion review")


def check_residual(text: str, *, client_terms: Sequence[str | RedactionTerm] | None = None) -> list[str]:
    checked_text = REDACTED_AUTH_BEARER_RE.sub("", text)
    checked_text = REDACTED_ASSIGNMENT_SECRET_RE.sub("", checked_text)
    checked_text = PLACEHOLDER_OR_MASK_RE.sub("", checked_text)
    warnings: list[str] = []
    if any(term_re.search(checked_text) for term_re, _kind in _compile_terms(client_terms)):
        warnings.append("possible client term remains")
    if EMAIL_RE.search(checked_text):
        warnings.append("possible email remains")
    if LT_PHONE_RE.search(checked_text) or INTL_PHONE_RE.search(checked_text) or PHONE_RE.search(checked_text):
        warnings.append("possible phone remains")
    if (
        AUTH_BEARER_RE.search(checked_text)
        or AWS_KEY_RE.search(checked_text)
        or GITHUB_TOKEN_RE.search(checked_text)
        or JWT_RE.search(checked_text)
        or SLACK_TOKEN_RE.search(checked_text)
        or DISCORD_TOKEN_RE.search(checked_text)
        or PRIVATE_KEY_BLOCK_RE.search(checked_text)
        or ASSIGNMENT_SECRET_RE.search(checked_text)
        or BASIC_AUTH_URL_RE.search(checked_text)
    ):
        warnings.append("possible token remains")
    if COOKIE_VALUE_RE.search(checked_text):
        warnings.append("possible cookie remains")
    if UUID_RE.search(checked_text):
        warnings.append("possible uuid remains")
    if HOST_RE.search(checked_text):
        warnings.append("possible internal host remains")
    if WINDOWS_PATH_RE.search(checked_text) or PATH_RE.search(checked_text):
        warnings.append("possible local path remains")
    ip_values = [
        match.group(0)
        for match in IPV4_RE.finditer(checked_text)
        if not _looks_like_product_version_dotted_quad(checked_text, match.start())
    ]
    if any(_is_internal_ipv4(value) for value in ip_values):
        warnings.append("possible internal ip remains")
    if any(not _is_internal_ipv4(value) for value in ip_values):
        warnings.append("possible public ip remains")
    return warnings


def _eof_hint() -> str:
    return "Ctrl-D"


def _stdin_is_tty() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


def read_tty_paste_input(input_stream: TextIO = sys.stdin) -> str:
    fd = input_stream.fileno()
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
        if not chunk.endswith((b"\n", b"\r")):
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


class _OperatorHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Show value names without argparse's confusing nested brackets."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_help_position", 40)
        super().__init__(*args, **kwargs)

    def _format_args(self, action: argparse.Action, default_metavar: str) -> str:
        if action.option_strings and action.nargs == argparse.OPTIONAL:
            return str(action.metavar or self._get_default_metavar_for_optional(action))
        return super()._format_args(action, default_metavar)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redloc",
        description=(
            "$ redloc\n"
            "local-first redaction for operator notes\n\n"
            "Redact sensitive text before you paste or share it.\n\n"
            "Status: alpha. Redaction is not a guarantee; review output before sharing.\n"
            "Normal pasted/file input is not saved. Sessions retain original mappings locally.\n\n"
            "START HERE\n"
            "  redloc --summary\n"
            "    Paste text, then finish input with Ctrl-D.\n"
            "    Redacted text goes to stdout; counts and warnings go to stderr.\n\n"
            "  redloc --in raw.log --out redacted.log --summary\n"
            "    Redact a file without modifying the original.\n\n"
            "  redloc --profile-init exampleco\n"
            "  redloc --profile-term-add\n"
            "    Create a reusable local profile, then add one client/project term per line."
        ),
        epilog=(
            "EXAMPLES\n"
            "  printf 'Email jane.doe@example.com\\n' | redloc --summary\n"
            "  redloc --in raw.log --out redacted.log --summary\n"
            "  redloc --interactive\n"
            "  redloc --session-init exampleco-webapp\n"
            "\n"
            "OPTIONAL LOCAL AI\n"
            "  redloc --ai-config-set --ai-endpoint URL --ai-model MODEL --ai-timeout 60\n"
            "  redloc --profile exampleco --ai-suggest\n"
            "\n"
            "See README.md for workflows and INSTALL.md for installation and upgrade.\n"
        ),
        formatter_class=_OperatorHelpFormatter,
        allow_abbrev=False,
    )
    everyday = parser.add_argument_group("Everyday redaction")
    profiles = parser.add_argument_group("Profiles and reusable terms")
    sessions = parser.add_argument_group("Stable labels / sessions")
    reveal = parser.add_argument_group("Reveal local session secrets")
    review = parser.add_argument_group("Review and automation")
    advanced = parser.add_argument_group("Advanced paths/settings")

    parser.add_argument("--about", action="store_true", help="show the redloc wordmark, purpose, author, and license")

    everyday.add_argument(
        "--term",
        action="append",
        default=[],
        metavar="TERM",
        help="redact TERM as [CLIENT_N] for this run; repeatable",
    )
    everyday.add_argument(
        "--term-file",
        action="append",
        default=[],
        metavar="FILE",
        help="load categorized terms from FILE for this run",
    )
    everyday.add_argument(
        "--ignore-file",
        action="append",
        default=[],
        metavar="FILE",
        help="skip AI suggestions listed in FILE for this run",
    )
    everyday.add_argument(
        "--term-file-template",
        nargs="?",
        const="-",
        metavar="FILE",
        help="print a categorized-term template, or save it to FILE",
    )
    advanced.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        metavar="FILE",
        help="read profiles from FILE",
    )
    profiles.add_argument("--profile", metavar="NAME", help="use an existing profile for this run")
    profiles.add_argument("--profile-init", dest="init_profile", metavar="NAME", help="create and select a local profile for reusable terms")
    profiles.add_argument(
        "--profile-select",
        dest="select_profile",
        nargs="?",
        const="",
        metavar="NAME",
        help="make NAME the default profile",
    )
    profiles.add_argument("--profile-list", action="store_true", help="list profiles; in a TTY, pick one to select")
    advanced.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        metavar="FILE",
        help="store the default profile in FILE",
    )
    everyday.add_argument("--copy", action="store_true", help="copy redacted output to clipboard for this run; still prints/saves normally")
    everyday.add_argument("--interactive", action="store_true", help="stay open for repeated pasted chunks; blank line redacts current paste, q quits")
    advanced.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        metavar="FILE",
        help="read global settings from FILE",
    )
    advanced.add_argument("--copy-enable", action="store_true", help="copy redacted output by default")
    advanced.add_argument("--copy-disable", action="store_true", help="stop copying redacted output by default")
    advanced.add_argument("--copy-status", action="store_true", help="show the default clipboard-copy setting")
    profiles.add_argument(
        "--profile-term-add",
        action="store_true",
        dest="add_term",
        help="add pasted terms to the active profile; one per line",
    )
    profiles.add_argument(
        "--profile-term-list",
        dest="terms_list",
        action="store_true",
        help="review and edit saved profile terms",
    )
    profiles.add_argument("--profile-term-remove", dest="term_remove", metavar="TERM", help="remove TERM from the active profile")
    profiles.add_argument(
        "--global-term-add",
        action="store_true",
        help="add pasted terms to the global always-redact list; one per line",
    )
    profiles.add_argument(
        "--global-term-list",
        action="store_true",
        help="review and edit global always-redact terms",
    )
    profiles.add_argument("--global-term-remove", metavar="TERM", help="remove TERM from the global always-redact list")
    profiles.add_argument(
        "--ignore-add",
        action="store_true",
        help="add pasted terms to the active profile's AI ignore list",
    )
    profiles.add_argument("--ignore-remove", metavar="TERM", help="remove TERM from the active profile's AI ignore list")
    profiles.add_argument("--ignore-list", action="store_true", help="review and edit the active profile's AI ignore list")
    profiles.add_argument("--detector-list", action="store_true", help="review and edit built-in detectors for the active profile")
    profiles.add_argument("--detector-disable", metavar="DETECTOR", help="disable one built-in detector for the active profile")
    profiles.add_argument("--detector-enable", metavar="DETECTOR", help="enable one built-in detector for the active profile")
    profiles.add_argument("--manual-detector-list", action="store_true", help="review and edit custom label types for profile terms")
    profiles.add_argument(
        "--manual-detector-add",
        action="store_true",
        help="add pasted custom label types to the active profile",
    )
    profiles.add_argument("--manual-detector-remove", metavar="DETECTOR", help="remove custom label type DETECTOR from the active profile")
    profiles.add_argument("--manual-detector-disable", metavar="DETECTOR", help="disable custom label type DETECTOR")
    profiles.add_argument("--manual-detector-enable", metavar="DETECTOR", help="enable custom label type DETECTOR")
    review.add_argument("--check-only", action="store_true", help="check redacted output without printing it; exit 1 if warnings remain")
    review.add_argument("--no-redact", action="store_true", help="with --check-only, scan the original input instead")
    review.add_argument("--ai-check", action="store_true", help="check redacted text with local AI for possible identifying context")
    review.add_argument("--ai-suggest", action="store_true", help="suggest visible terms to add to the active profile")

    review.add_argument(
        "--ai-endpoint",
        default=None,
        metavar="URL",
        help="AI server URL for this run",
    )
    review.add_argument("--ai-model", default=None, metavar="MODEL", help="AI model to use for this run")
    review.add_argument(
        "--ai-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="set the AI request timeout for this run; default 30; use with --ai-config-set to save/update it",
    )
    review.add_argument(
        "--ai-chunk-lines",
        type=int,
        default=None,
        metavar="LINES",
        help="send at most LINES lines in each AI request; default 80; use with --ai-config-set to save/update it",
    )
    review.add_argument(
        "--ai-chunk-chars",
        type=int,
        default=None,
        metavar="CHARS",
        help="send about CHARS characters in each AI request; default 8000; use with --ai-config-set to save/update it",
    )
    review.add_argument(
        "--ai-config-set",
        action="store_true",
        help="save this AI server and model for future AI checks; include --ai-timeout or chunk flags to save/update those too",
    )
    review.add_argument("--ai-config-status", action="store_true", help="show saved AI settings")
    review.add_argument("--ai-config-clear", action="store_true", help="clear saved AI settings")
    everyday.add_argument("--in", dest="input_file", type=Path, metavar="FILE", help="read input text from a file instead of pasted stdin")
    everyday.add_argument(
        "--out",
        dest="output_file",
        type=Path,
        metavar="FILE_OR_DIR",
        help="save redacted output to FILE, or derive a redacted filename when DIR is given; refuses existing files unless --force",
    )
    everyday.add_argument(
        "--auto-out",
        action="store_true",
        help="save redacted output to a redacted/ folder and print the path to stderr",
    )
    everyday.add_argument("--timestamp", action="store_true", help="with --auto-out and --in, add a timestamp to the generated filename")
    everyday.add_argument("--force", action="store_true", help="overwrite an existing output or term-template file")
    review.add_argument(
        "--summary",
        action="store_true",
        help="print raw-free redaction counts and warning names to stderr",
    )
    review.add_argument(
        "--report",
        type=Path,
        metavar="FILE",
        help="write a JSON redaction report to FILE; replaces FILE if it exists",
    )
    sessions.add_argument("--session", metavar="NAME", help="use NAME's stable labels for this run; matched values are saved locally")
    sessions.add_argument("--session-init", dest="init_session", metavar="NAME", help="create and select session NAME for stable labels")
    sessions.add_argument("--session-select", dest="select_session", nargs="?", const="", metavar="NAME", help="make NAME the default session; matched values are saved locally")
    sessions.add_argument("--session-clear", dest="clear_session", action="store_true", help="stop using the selected session; saved mappings are not deleted")
    sessions.add_argument("--session-list", dest="list_sessions", action="store_true", help="list saved sessions with raw-free counts; in a TTY, pick one to select")
    sessions.add_argument("--session-status", dest="map_status", action="store_true", help="show raw-free counts for the active/current session")
    sessions.add_argument("--session-delete", dest="delete_session", metavar="NAME", help="permanently delete saved mappings for NAME")
    reveal.add_argument("--restore", action="store_true", help="restore placeholders from the active/current session; intentionally prints raw values locally")
    reveal.add_argument("--show-secret", dest="show_secret", metavar="PLACEHOLDER", help="reveal the local value behind PLACEHOLDER for the active/current session")
    reveal.add_argument("--show-secret-all", dest="show_secret_all", action="store_true", help="reveal every placeholder mapping for the active/current session")
    advanced.add_argument(
        "--session-dir",
        type=Path,
        default=DEFAULT_SESSION_DIR,
        metavar="DIR",
        help="store saved sessions in DIR",
    )
    advanced.add_argument(
        "--session-state-file",
        type=Path,
        default=DEFAULT_SESSION_STATE_FILE,
        metavar="FILE",
        help="store the default session in FILE",
    )
    return parser


RESTORE_ALLOWED_FLAGS = {
    "--restore",
    "--session",
    "--session-dir",
    "--session-state-file",
    "--in",
    "--out",
    "--force",
    "--summary",
}

REVEAL_ALLOWED_FLAGS = {
    "--show-secret",
    "--show-secret-all",
    "--session",
    "--session-dir",
    "--session-state-file",
}


def _flag_name(token: str) -> str:
    return token.split("=", 1)[0]


def unsupported_restore_flag(args: argparse.Namespace, raw_argv: Sequence[str]) -> str | None:
    action_flags = (
        (args.about, "--about"),
        (bool(args.term), "--term"),
        (bool(args.term_file), "--term-file"),
        (bool(args.ignore_file), "--ignore-file"),
        (bool(args.term_file_template), "--term-file-template"),
        (bool(args.init_profile), "--profile-init"),
        (bool(args.select_profile), "--profile-select"),
        (args.profile_list, "--profile-list"),
        (args.copy, "--copy"),
        (args.interactive, "--interactive"),
        (args.copy_enable, "--copy-enable"),
        (args.copy_disable, "--copy-disable"),
        (args.copy_status, "--copy-status"),
        (args.add_term, "--profile-term-add"),
        (args.terms_list, "--profile-term-list"),
        (bool(args.term_remove), "--profile-term-remove"),
        (args.global_term_add, "--global-term-add"),
        (args.global_term_list, "--global-term-list"),
        (bool(args.global_term_remove), "--global-term-remove"),
        (args.ignore_add, "--ignore-add"),
        (bool(args.ignore_remove), "--ignore-remove"),
        (args.ignore_list, "--ignore-list"),
        (args.detector_list, "--detector-list"),
        (bool(args.detector_disable), "--detector-disable"),
        (bool(args.detector_enable), "--detector-enable"),
        (args.manual_detector_list, "--manual-detector-list"),
        (args.manual_detector_add, "--manual-detector-add"),
        (bool(args.manual_detector_remove), "--manual-detector-remove"),
        (bool(args.manual_detector_disable), "--manual-detector-disable"),
        (bool(args.manual_detector_enable), "--manual-detector-enable"),
        (args.check_only, "--check-only"),
        (args.no_redact, "--no-redact"),
        (args.ai_check, "--ai-check"),
        (args.ai_suggest, "--ai-suggest"),
        (bool(args.ai_config_set), "--ai-config-set"),
        (args.ai_config_status, "--ai-config-status"),
        (args.ai_config_clear, "--ai-config-clear"),
        (args.auto_out, "--auto-out"),
        (args.timestamp, "--timestamp"),
        (bool(args.report), "--report"),
        (bool(args.init_session), "--session-init"),
        (args.select_session is not None, "--session-select"),
        (args.clear_session, "--session-clear"),
        (args.list_sessions, "--session-list"),
        (args.map_status, "--session-status"),
        (bool(args.delete_session), "--session-delete"),
        (bool(args.show_secret), "--show-secret"),
        (args.show_secret_all, "--show-secret-all"),
    )
    for enabled, flag in action_flags:
        if enabled:
            return flag
    for token in raw_argv:
        if token.startswith("--") and _flag_name(token) not in RESTORE_ALLOWED_FLAGS:
            return _flag_name(token)
    return None


def unsupported_reveal_flag(args: argparse.Namespace, raw_argv: Sequence[str]) -> str | None:
    action_flags = (
        (args.about, "--about"),
        (args.restore, "--restore"),
        (bool(args.term), "--term"),
        (bool(args.term_file), "--term-file"),
        (bool(args.ignore_file), "--ignore-file"),
        (bool(args.term_file_template), "--term-file-template"),
        (bool(args.init_profile), "--profile-init"),
        (bool(args.select_profile), "--profile-select"),
        (args.profile_list, "--profile-list"),
        (args.copy, "--copy"),
        (args.interactive, "--interactive"),
        (args.copy_enable, "--copy-enable"),
        (args.copy_disable, "--copy-disable"),
        (args.copy_status, "--copy-status"),
        (args.add_term, "--profile-term-add"),
        (args.terms_list, "--profile-term-list"),
        (bool(args.term_remove), "--profile-term-remove"),
        (args.global_term_add, "--global-term-add"),
        (args.global_term_list, "--global-term-list"),
        (bool(args.global_term_remove), "--global-term-remove"),
        (args.ignore_add, "--ignore-add"),
        (bool(args.ignore_remove), "--ignore-remove"),
        (args.ignore_list, "--ignore-list"),
        (args.detector_list, "--detector-list"),
        (bool(args.detector_disable), "--detector-disable"),
        (bool(args.detector_enable), "--detector-enable"),
        (args.manual_detector_list, "--manual-detector-list"),
        (args.manual_detector_add, "--manual-detector-add"),
        (bool(args.manual_detector_remove), "--manual-detector-remove"),
        (bool(args.manual_detector_disable), "--manual-detector-disable"),
        (bool(args.manual_detector_enable), "--manual-detector-enable"),
        (args.check_only, "--check-only"),
        (args.no_redact, "--no-redact"),
        (args.ai_check, "--ai-check"),
        (args.ai_suggest, "--ai-suggest"),
        (bool(args.ai_config_set), "--ai-config-set"),
        (args.ai_config_status, "--ai-config-status"),
        (args.ai_config_clear, "--ai-config-clear"),
        (args.auto_out, "--auto-out"),
        (args.timestamp, "--timestamp"),
        (bool(args.output_file), "--out"),
        (args.force, "--force"),
        (args.summary, "--summary"),
        (bool(args.report), "--report"),
        (bool(args.init_session), "--session-init"),
        (args.select_session is not None, "--session-select"),
        (args.clear_session, "--session-clear"),
        (args.list_sessions, "--session-list"),
        (args.map_status, "--session-status"),
        (bool(args.delete_session), "--session-delete"),
    )
    for enabled, flag in action_flags:
        if enabled:
            return flag
    for token in raw_argv:
        if token.startswith("--") and _flag_name(token) not in REVEAL_ALLOWED_FLAGS:
            return _flag_name(token)
    return None


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    config_was_explicit = "--config" in raw_argv
    config_existed_before_profile_resolution = args.config.expanduser().exists()
    selected_profile_before_resolution = (
        args.state_file.read_text(encoding="utf-8").strip() if args.state_file.exists() else ""
    )
    if args.restore:
        unsupported = unsupported_restore_flag(args, raw_argv)
        if unsupported:
            print(f"error: --restore cannot be used with {unsupported}", file=sys.stderr)
            return 2
    if args.show_secret and args.show_secret_all:
        print("error: use either --show-secret or --show-secret-all, not both", file=sys.stderr)
        return 2
    if args.show_secret or args.show_secret_all:
        unsupported = unsupported_reveal_flag(args, raw_argv)
        if unsupported:
            reveal_flag = "--show-secret-all" if args.show_secret_all else "--show-secret"
            print(f"error: {reveal_flag} cannot be used with {unsupported}", file=sys.stderr)
            return 2
    if args.about:
        sys.stdout.write(ABOUT_TEXT)
        return 0
    if args.auto_out and args.output_file:
        print("error: use either --auto-out or --out, not both", file=sys.stderr)
        return 2
    if args.term_file_template:
        if args.term_file_template == "-":
            sys.stdout.write(TERM_FILE_TEMPLATE)
            return 0
        template_path = Path(args.term_file_template).expanduser()
        if template_path.exists() and not args.force:
            print(f"error: refusing to overwrite existing term-file template: {template_path}; pass --force", file=sys.stderr)
            return 2
        try:
            _write_private_text(template_path, TERM_FILE_TEMPLATE)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"wrote term-file template to {template_path}")
        return 0
    if args.timestamp and not args.auto_out:
        print("error: --timestamp requires --auto-out", file=sys.stderr)
        return 2
    if args.interactive and args.input_file:
        print("error: --interactive reads pasted stdin; do not use --in", file=sys.stderr)
        return 2
    if args.interactive and args.add_term:
        print("error: use either --interactive or --profile-term-add, not both", file=sys.stderr)
        return 2
    if args.interactive and args.ignore_add:
        print("error: use either --interactive or --ignore-add, not both", file=sys.stderr)
        return 2
    selected_profile_list_count = sum(
        bool(flag)
        for flag in (
            args.terms_list,
            args.global_term_list,
            args.ignore_list,
            args.detector_list,
            args.manual_detector_list,
        )
    )
    if selected_profile_list_count > 1:
        print(
            "error: use only one of --profile-term-list, --global-term-list, --ignore-list, --detector-list, or --manual-detector-list",
            file=sys.stderr,
        )
        return 2
    if selected_profile_list_count and any(
        (
            args.add_term,
            args.global_term_add,
            args.global_term_remove,
            args.term_remove,
            args.ignore_add,
            args.ignore_remove,
            args.manual_detector_add,
            args.manual_detector_remove,
            args.manual_detector_enable,
            args.manual_detector_disable,
        )
    ):
        print("error: add/remove/enable/disable actions are standalone; do not combine them with list flags", file=sys.stderr)
        return 2
    profile_term_action_count = sum(
        bool(flag)
        for flag in (
            args.add_term,
            args.global_term_add,
            args.term_remove,
            args.global_term_remove,
            args.ignore_add,
            args.ignore_remove,
        )
    )
    if profile_term_action_count > 1:
        print(
            "error: use only one of --profile-term-add, --global-term-add, --profile-term-remove, --global-term-remove, --ignore-add, or --ignore-remove",
            file=sys.stderr,
        )
        return 2
    if args.detector_enable and args.detector_disable:
        print("error: use either --detector-enable or --detector-disable, not both", file=sys.stderr)
        return 2
    manual_detector_action_count = sum(
        bool(flag)
        for flag in (
            args.manual_detector_add,
            args.manual_detector_remove,
            args.manual_detector_enable,
            args.manual_detector_disable,
        )
    )
    if manual_detector_action_count > 1:
        print(
            "error: use only one of --manual-detector-add, --manual-detector-remove, --manual-detector-enable, or --manual-detector-disable",
            file=sys.stderr,
        )
        return 2
    if args.ai_timeout is not None and args.ai_timeout <= 0:
        print("error: --ai-timeout must be greater than zero seconds", file=sys.stderr)
        return 2
    if args.ai_chunk_lines is not None and args.ai_chunk_lines <= 0:
        print("error: --ai-chunk-lines must be greater than zero", file=sys.stderr)
        return 2
    if args.ai_chunk_chars is not None and args.ai_chunk_chars <= 0:
        print("error: --ai-chunk-chars must be greater than zero", file=sys.stderr)
        return 2

    copy_control_count = sum(bool(flag) for flag in (args.copy_enable, args.copy_disable, args.copy_status))
    if copy_control_count > 1:
        print("error: use only one of --copy-enable, --copy-disable, or --copy-status", file=sys.stderr)
        return 2
    ai_config_control_count = sum(bool(flag) for flag in (args.ai_config_set, args.ai_config_status, args.ai_config_clear))
    if ai_config_control_count > 1:
        print("error: use only one of --ai-config-set, --ai-config-status, or --ai-config-clear", file=sys.stderr)
        return 2
    if args.ai_config_set:
        if not args.ai_endpoint or not args.ai_model:
            print("error: --ai-config-set requires --ai-endpoint and --ai-model", file=sys.stderr)
            return 2
        try:
            set_ai_config(
                args.ai_endpoint,
                args.ai_model,
                args.settings,
                timeout_seconds=args.ai_timeout,
                chunk_max_lines=args.ai_chunk_lines,
                chunk_max_chars=args.ai_chunk_chars,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print("AI config saved")
        return 0
    if args.ai_config_status:
        ai_config = load_ai_config(args.settings)
        for line in format_ai_config_status(ai_config):
            print(line)
        return 0
    if args.ai_config_clear:
        clear_ai_config(args.settings)
        print("AI config cleared")
        return 0
    if args.copy_enable:
        set_global_copy_enabled(True, args.settings)
        print("copy enabled")
        return 0
    if args.copy_disable:
        set_global_copy_enabled(False, args.settings)
        print("copy disabled")
        return 0
    if args.copy_status:
        print(f"copy: {'enabled' if load_global_copy_enabled(args.settings) else 'disabled'}")
        return 0
    if args.global_term_add:
        global_term_file = global_terms_path(args.config)
        if not args.input_file and _stdin_is_tty():
            print(f"Add one global always-redact term per line. Finish with {_eof_hint()}.", file=sys.stderr)
            print("Use MANUAL DETECTOR: term for explicit placeholders, e.g. PERSON: Operator One.", file=sys.stderr)
            print(f"Global terms will be saved to: {global_term_file}", file=sys.stderr)
        try:
            if args.input_file:
                text = args.input_file.expanduser().read_text(encoding="utf-8")
            elif not args.input_file and _stdin_is_tty():
                text = read_tty_paste_input()
            else:
                text = sys.stdin.read()
        except KeyboardInterrupt:
            print("cancelled", file=sys.stderr)
            return 130
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        candidate_terms, notices = term_add_candidates(text)
        for notice in notices:
            print(notice, file=sys.stderr)
        try:
            reviewed_terms = review_terms_to_add_via_tty(
                candidate_terms, output_stream=sys.stderr, kind_order=TERM_KIND_ORDER
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if reviewed_terms is None:
            print("cancelled", file=sys.stderr)
            return 130
        added_count = append_redaction_terms(global_term_file, reviewed_terms)
        print(f"added {added_count} global term(s) to {global_term_file}")
        return 0
    if args.global_term_remove:
        removed_count = remove_global_term(args.config, args.global_term_remove)
        print(f"removed {removed_count} global term(s)")
        return 0

    if args.global_term_list:
        global_term_file = global_terms_path(args.config)
        if not _stdin_is_tty():
            for term in list_global_terms_with_state(args.config):
                print(f"[{'x' if term.enabled else ' '}] {term.line}")
            return 0
        term_entries = list_global_terms_with_state(args.config)
        try:
            list_result = edit_transferable_list_items_via_tty(
                [entry.line for entry in term_entries],
                title="Global always-redact terms",
                output_stream=sys.stderr,
                initial_kept={index for index, entry in enumerate(term_entries, start=1) if entry.enabled},
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if list_result is None:
            print("cancelled", file=sys.stderr)
            return 130
        disabled_count, removed_count = set_global_term_states(
            args.config, disabled=list_result.disabled, removed=list_result.removed, updated=list_result.updated
        )
        print(f"removed {removed_count} global term(s)")
        if list_result.disabled:
            print(f"disabled {disabled_count} global term(s)")
        if list_result.updated:
            print(f"updated {len(list_result.updated)} global term detector(s)")
        if not global_term_file.exists():
            _touch_private_file(global_term_file)
        return 0
    if args.init_profile:
        path = init_profile(args.init_profile, config_path=args.config)
        print(f"initialized profile: {args.init_profile} ({path})")
        select_profile(args.init_profile, config_path=args.config, state_file=args.state_file)
        print(f"selected profile: {args.init_profile}")
        return 0
    if args.profile_list:
        if sys.stdin.isatty() and sys.stdout.isatty():
            selected = args.state_file.read_text(encoding="utf-8").strip() if args.state_file.exists() else ""
            names = profile_names(args.config)
            picked_profile = select_single_item_screen(
                names,
                labels=names,
                title="Profiles",
                selected_name=selected,
                input_stream=sys.stdin,
                output_stream=sys.stdout,
            )
            if picked_profile:
                try:
                    select_profile(picked_profile, config_path=args.config, state_file=args.state_file)
                except (FileNotFoundError, KeyError) as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                print(f"selected profile: {picked_profile}")
        else:
            for line in list_profiles(args.config, state_file=args.state_file):
                print(line)
        return 0
    if args.select_profile is not None:
        if not args.select_profile:
            print("error: --profile-select needs a profile name", file=sys.stderr)
            return 2
        try:
            select_profile(args.select_profile, config_path=args.config, state_file=args.state_file)
        except (FileNotFoundError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"selected profile: {args.select_profile}")
        return 0

    try:
        if args.init_session:
            path = init_session(args.init_session, session_dir=args.session_dir, state_file=args.session_state_file)
            print(f"initialized session: {args.init_session} ({path})")
            print(f"active session set: {args.init_session}")
            return 0
        if args.select_session is not None:
            if not args.select_session:
                print("error: --session-select needs a session name", file=sys.stderr)
                return 2
            select_session(args.select_session, session_dir=args.session_dir, state_file=args.session_state_file)
            print(f"active session set: {args.select_session}")
            return 0
        if args.clear_session:
            clear_selected_session(args.session_state_file)
            print("active session cleared")
            return 0
        if args.delete_session:
            forget_session(args.delete_session, session_dir=args.session_dir, state_file=args.session_state_file)
            print(f"deleted session: {args.delete_session}")
            return 0
        if args.list_sessions:
            if sys.stdin.isatty() and sys.stdout.isatty():
                selected = args.session_state_file.read_text(encoding="utf-8").strip() if args.session_state_file.exists() else ""
                names, labels = session_selector_labels(args.session_dir)
                picked_session = select_single_item_screen(
                    names,
                    labels=labels,
                    title="Sessions",
                    selected_name=selected,
                    input_stream=sys.stdin,
                    output_stream=sys.stdout,
                )
                if picked_session:
                    select_session(picked_session, session_dir=args.session_dir, state_file=args.session_state_file)
                    print(f"active session set: {picked_session}")
            else:
                for line in list_sessions(args.session_dir, state_file=args.session_state_file):
                    print(line)
            return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    session_name = resolve_session_name(args.session, state_file=args.session_state_file)
    session_data = load_session(session_name, args.session_dir) if session_name else None
    if args.map_status or args.show_secret or args.show_secret_all:
        if not session_data:
            print("error: no active session", file=sys.stderr)
            return 2
        print(f"session: {session_data.name}", file=sys.stderr)
        if args.map_status:
            print(format_counts(session_data.vault.status_counts()))
            return 0
        if args.show_secret_all:
            for line in show_all_mappings(session_data):
                print(line)
        else:
            for line in show_mapping(session_data, args.show_secret):
                print(line)
        return 0

    if args.restore:
        if not session_data:
            print("error: --restore needs an active session or --session NAME", file=sys.stderr)
            return 2
        try:
            if args.input_file:
                text = args.input_file.expanduser().read_text(encoding="utf-8")
            else:
                if _stdin_is_tty():
                    print(f"Paste placeholder text to restore. Finish with {_eof_hint()}.", file=sys.stderr)
                    print("Restored raw values print to stdout unless --out is used.", file=sys.stderr)
                text = sys.stdin.read()
        except KeyboardInterrupt:
            print("cancelled", file=sys.stderr)
            return 130
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        output, restore_counts, unknown_placeholders = restore_session_placeholders(text, session_data)
        warnings = [f"unknown placeholder {placeholder}" for placeholder in unknown_placeholders]
        print(f"session: {session_data.name}", file=sys.stderr)
        output_file = args.output_file
        output_file_is_directory = False
        if output_file:
            output_file, output_file_is_directory = explicit_output_path(output_file, input_file=args.input_file)
            if output_file.exists() and not args.force:
                print(f"error: refusing to overwrite existing output file: {output_file}; pass --force", file=sys.stderr)
                return 2
            try:
                _write_private_text(output_file, output)
            except OSError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            if output_file_is_directory:
                print(f"wrote restored output to {output_file}", file=sys.stderr)
        else:
            sys.stdout.write(output)
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if args.summary:
            print(format_summary(restore_counts, warnings), file=sys.stderr)
        return 0

    terms = list(args.term)
    copy_output = args.copy or load_global_copy_enabled(args.settings)
    profile_term_files: list[Path] = []
    profile_ignored_suggestion_files: list[Path] = []
    ignored_suggestion_terms: set[str] = set()
    profile_options: RedactorOptions | None = None
    disabled_categories: set[str] = set()
    profile_name = resolve_profile_name(args.profile, config_path=args.config, state_file=args.state_file)
    should_print_profile = (
        bool(args.profile)
        or (selected_profile_before_resolution and selected_profile_before_resolution == profile_name)
        or (config_was_explicit and config_existed_before_profile_resolution)
    )
    try:
        global_term_file = global_terms_path(args.config)
        if global_term_file.exists():
            terms.extend(_read_term_file(global_term_file))
        if profile_name:
            profile_options = load_profile_options(profile_name, args.config)
            copy_output = copy_output or profile_options.copy
            disabled_categories = set(profile_options.disabled_categories)
            terms.extend(profile_options.client_terms)
            profile_term_files = profile_options.term_files
            profile_ignored_suggestion_files = profile_options.ignored_suggestion_files
            for ignored_suggestion_file in profile_ignored_suggestion_files:
                ignored_suggestion_terms.update(_read_ignored_suggestion_file(ignored_suggestion_file))
        for term_file in args.term_file:
            terms.extend(_read_term_file(Path(term_file).expanduser()))
        for ignore_file in args.ignore_file:
            ignored_suggestion_terms.update(_read_ignored_suggestion_file(Path(ignore_file).expanduser()))
    except (FileNotFoundError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.detector_list and (args.detector_disable or args.detector_enable):
        if not profile_name:
            print("error: --detector-list needs an active profile", file=sys.stderr)
            return 2
        try:
            if args.detector_disable:
                disabled_categories = set_profile_detector_enabled(
                    profile_name, args.detector_disable, enabled=False, config_path=args.config
                )
                print(f"disabled detector {_normalize_redaction_category(args.detector_disable)} for profile: {profile_name}")
            else:
                disabled_categories = set_profile_detector_enabled(
                    profile_name, args.detector_enable, enabled=True, config_path=args.config
                )
                print(f"enabled detector {_normalize_redaction_category(args.detector_enable)} for profile: {profile_name}")
        except (FileNotFoundError, KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.detector_list:
        if not profile_name:
            print("error: --detector-list needs an active profile", file=sys.stderr)
            return 2
        category_items = [f"{category:<11} {CATEGORY_DESCRIPTIONS[category]}" for category in REDACTION_CATEGORIES]
        if not _stdin_is_tty():
            for line in format_detector_list(profile_name, disabled_categories):
                print(line)
            return 0
        initial_kept = {
            index
            for index, category in enumerate(REDACTION_CATEGORIES, start=1)
            if category not in disabled_categories
        }
        try:
            disabled_items = edit_list_items_via_tty(
                category_items,
                title=f"Built-in detectors for profile: {profile_name}",
                output_stream=sys.stderr,
                initial_kept=initial_kept,
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if disabled_items is None:
            print("cancelled", file=sys.stderr)
            return 130
        disabled_categories = {
            category
            for category, item in zip(REDACTION_CATEGORIES, category_items, strict=True)
            if item in disabled_items
        }
        try:
            save_profile_disabled_categories(profile_name, disabled_categories, config_path=args.config)
        except (FileNotFoundError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"updated built-in detectors for profile: {profile_name}")
        return 0
    if args.manual_detector_add:
        if not profile_name:
            print("error: --manual-detector-add needs an active profile", file=sys.stderr)
            return 2
        if _stdin_is_tty():
            print("add manual detectors", file=sys.stderr)
            print("One detector name per line, e.g. PLATE NUMBER. Finish with EOF.", file=sys.stderr)
            print("Manual detectors define placeholder classes; they do not auto-detect by regex.", file=sys.stderr)
        raw_detector_text = sys.stdin.read()
        detector_names = [line.strip() for line in raw_detector_text.splitlines() if line.strip() and not line.strip().startswith("#")]
        try:
            added_count = add_profile_manual_detectors(profile_name, detector_names, config_path=args.config)
        except (FileNotFoundError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"added {added_count} manual detector(s) to profile: {profile_name}")
        return 0
    if args.manual_detector_remove:
        if not profile_name:
            print("error: --manual-detector-remove needs an active profile", file=sys.stderr)
            return 2
        try:
            removed_count = remove_profile_manual_detector(
                profile_name, args.manual_detector_remove, config_path=args.config
            )
        except (FileNotFoundError, KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"removed {removed_count} manual detector(s) from profile: {profile_name}")
        return 0
    if args.manual_detector_enable or args.manual_detector_disable:
        if not profile_name:
            print("error: --manual-detector-enable/--manual-detector-disable needs an active profile", file=sys.stderr)
            return 2
        try:
            if args.manual_detector_disable:
                set_profile_manual_detector_enabled(
                    profile_name, args.manual_detector_disable, enabled=False, config_path=args.config
                )
                print(
                    f"disabled manual detector {_manual_detector_display(args.manual_detector_disable)} for profile: {profile_name}"
                )
            else:
                set_profile_manual_detector_enabled(
                    profile_name, args.manual_detector_enable, enabled=True, config_path=args.config
                )
                print(
                    f"enabled manual detector {_manual_detector_display(args.manual_detector_enable)} for profile: {profile_name}"
                )
        except (FileNotFoundError, KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.manual_detector_list:
        if not profile_name:
            print("error: --manual-detector-list needs an active profile", file=sys.stderr)
            return 2
        try:
            manual_detectors = load_profile_manual_detectors(profile_name, config_path=args.config)
        except (FileNotFoundError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not _stdin_is_tty():
            for line in format_manual_detector_list(profile_name, manual_detectors):
                print(line)
            return 0
        manual_items = [_manual_detector_list_item(detector) for detector in manual_detectors]
        try:
            list_result = edit_transferable_list_items_via_tty(
                manual_items,
                title=f"Manual detectors for profile: {profile_name}",
                output_stream=sys.stderr,
                initial_kept={index for index, detector in enumerate(manual_detectors, start=1) if detector.enabled},
                change_label=None,
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if list_result is None:
            print("cancelled", file=sys.stderr)
            return 130
        removed_detectors = {_manual_detector_kind_from_list_item(item) for item in list_result.removed}
        disabled_detectors = {_manual_detector_kind_from_list_item(item) for item in list_result.disabled}
        kept_detectors = [detector.kind for detector in manual_detectors if detector.kind not in removed_detectors]
        if removed_detectors & set(DEFAULT_MANUAL_DETECTORS):
            print("error: default manual detectors cannot be removed; disable them instead", file=sys.stderr)
            return 2
        try:
            save_profile_manual_detectors(profile_name, kept_detectors, disabled_detectors, config_path=args.config)
        except (FileNotFoundError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"removed {len(removed_detectors)} manual detector(s)")
        if disabled_detectors:
            print(f"disabled {len(disabled_detectors)} manual detector(s)")
        return 0
    if args.term_remove:
        if not profile_term_files:
            print("error: --profile-term-remove needs an active profile with term_files", file=sys.stderr)
            return 2
        removed_count = sum(remove_profile_term(term_file, args.term_remove) for term_file in profile_term_files)
        print(f"removed {removed_count} profile term(s)")
        return 0
    if args.terms_list:
        if not _stdin_is_tty():
            for term in list_profile_terms_with_state(profile_term_files):
                print(f"[{'x' if term.enabled else ' '}] {term.line}")
            return 0
        if not profile_ignored_suggestion_files:
            print("error: --profile-term-list needs a profile with ignored_suggestion_files", file=sys.stderr)
            return 2
        term_entries = list_profile_terms_with_state(profile_term_files)
        try:
            list_result = edit_transferable_list_items_via_tty(
                [entry.line for entry in term_entries],
                title="Profile terms",
                move_key="m",
                move_label="move to ignore list",
                output_stream=sys.stderr,
                initial_kept={index for index, entry in enumerate(term_entries, start=1) if entry.enabled},
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if list_result is None:
            print("cancelled", file=sys.stderr)
            return 130
        disabled_count, removed_count = set_profile_term_states(
            profile_term_files, disabled=list_result.disabled, removed=list_result.removed, updated=list_result.updated
        )
        moved_removed_count, moved_added_count = move_profile_terms_to_ignored(
            profile_term_files, profile_ignored_suggestion_files[0], list_result.moved
        )
        print(f"removed {removed_count} profile term(s)")
        if list_result.disabled:
            print(f"disabled {disabled_count} profile term(s)")
        if list_result.updated:
            print(f"updated {len(list_result.updated)} profile term detector(s)")
        if list_result.moved:
            print(f"moved {moved_removed_count} profile term(s) to ignored AI suggestions ({moved_added_count} new)")
        return 0
    if args.ignore_remove:
        if not profile_ignored_suggestion_files:
            print("error: --ignore-remove needs an active profile with ignored_suggestion_files", file=sys.stderr)
            return 2
        ignored_term_to_remove = args.ignore_remove
        removed_count = sum(
            remove_ignored_suggestion(ignored_file, ignored_term_to_remove)
            for ignored_file in profile_ignored_suggestion_files
        )
        print(f"removed {removed_count} ignored AI suggestion term(s)")
        return 0
    if args.ignore_list:
        if not _stdin_is_tty():
            for term in list_ignored_suggestions_with_state(profile_ignored_suggestion_files):
                print(f"[{'x' if term.enabled else ' '}] {term.line}")
            return 0
        if not profile_term_files:
            print("error: --ignore-list needs a profile with term_files", file=sys.stderr)
            return 2
        ignored_entries = list_ignored_suggestions_with_state(profile_ignored_suggestion_files)
        try:
            list_result = edit_transferable_list_items_via_tty(
                [entry.line for entry in ignored_entries],
                title="Ignored AI suggestions",
                move_key="m",
                move_label="move to terms list",
                output_stream=sys.stderr,
                initial_kept={index for index, entry in enumerate(ignored_entries, start=1) if entry.enabled},
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if list_result is None:
            print("cancelled", file=sys.stderr)
            return 130
        disabled_count, removed_count = set_ignored_suggestion_states(
            profile_ignored_suggestion_files,
            disabled=list_result.disabled,
            removed=list_result.removed,
            updated=list_result.updated,
        )
        moved_removed_count, moved_added_count = move_ignored_terms_to_profile(
            profile_ignored_suggestion_files, profile_term_files[0], list_result.moved
        )
        print(f"removed {removed_count} ignored AI suggestion term(s)")
        if list_result.disabled:
            print(f"disabled {disabled_count} ignored AI suggestion term(s)")
        if list_result.updated:
            print(f"updated {len(list_result.updated)} ignored AI suggestion detector(s)")
        if list_result.moved:
            print(f"moved {moved_removed_count} ignored AI suggestion term(s) to profile terms ({moved_added_count} new)")
        return 0
    apply_profile_term_file: Path | None = profile_term_files[0] if args.ai_suggest and profile_term_files else None
    ai_config = resolve_ai_config(args)
    if (args.ai_check or args.ai_suggest) and (not ai_config.endpoint or not ai_config.model):
        ai_flag = "--ai-suggest" if args.ai_suggest else "--ai-check"
        print(f"error: {ai_flag} needs --ai-endpoint and --ai-model, or saved values from --ai-config-set", file=sys.stderr)
        return 2
    ai_endpoint = ai_config.endpoint or ""
    ai_model = ai_config.model or ""
    ai_timeout = ai_config.timeout_seconds or DEFAULT_AI_TIMEOUT_SECONDS
    ai_chunk_max_lines = ai_config.chunk_max_lines or DEFAULT_AI_CHUNK_MAX_LINES
    ai_chunk_max_chars = ai_config.chunk_max_chars or DEFAULT_AI_CHUNK_MAX_CHARS

    if args.interactive:
        vault = session_data.vault if session_data else PlaceholderVault()
        print("interactive mode", file=sys.stderr)
        print("Paste text, then press Enter on a blank line to redact it.", file=sys.stderr)
        print("Type q or quit, then Enter, to exit.", file=sys.stderr)
        chunk_lines: list[str] = []
        chunk_index = 0

        def process_interactive_chunk(chunk_text: str) -> int:
            nonlocal chunk_index
            if not chunk_text:
                return 0
            chunk_index += 1
            if args.no_redact:
                output = chunk_text
                redaction_counts: dict[str, int] = {}
            else:
                redaction_result = redact_with_counts(
                    chunk_text, client_terms=terms, vault=vault, disabled_categories=disabled_categories
                )
                output = redaction_result.text
                redaction_counts = redaction_result.counts
                if session_data and redaction_result.vault.changed:
                    save_session(session_data)
            warnings = check_residual(output, client_terms=terms)
            ai_warnings: list[AIWarning] = []
            ai_suggestions: list[AISuggestion] = []
            if args.ai_check:
                try:
                    ai_warnings = run_ai_check(
                        output,
                        endpoint=ai_endpoint,
                        model=ai_model,
                        timeout_seconds=ai_timeout,
                        chunk_max_lines=ai_chunk_max_lines,
                        chunk_max_chars=ai_chunk_max_chars,
                    )
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                warnings.extend(warning.message() for warning in ai_warnings)
            if args.ai_suggest:
                try:
                    ai_suggestions = run_ai_suggest(
                        output,
                        endpoint=ai_endpoint,
                        model=ai_model,
                        timeout_seconds=ai_timeout,
                        chunk_max_lines=ai_chunk_max_lines,
                        chunk_max_chars=ai_chunk_max_chars,
                    )
                    reviewed_suggestions = review_ai_suggestions_via_tty(ai_suggestions, output_stream=sys.stderr)
                    if reviewed_suggestions is None:
                        print("cancelled", file=sys.stderr)
                        return 130
                    ai_suggestions = reviewed_suggestions
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                except RuntimeError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
            if args.ai_suggest and apply_profile_term_file is not None:
                redaction_result, added_count = apply_ai_suggestions_to_profile(
                    chunk_text,
                    term_file=apply_profile_term_file,
                    terms=terms,
                    suggestions=ai_suggestions,
                    vault=session_data.vault if session_data else None,
                    disabled_categories=disabled_categories,
                )
                print(f"added {added_count} AI suggestion term(s) to {apply_profile_term_file}", file=sys.stderr)
                if redaction_result:
                    output = redaction_result.text
                    redaction_counts = redaction_result.counts
                    if session_data and redaction_result.vault.changed:
                        save_session(session_data)
                    warnings = check_residual(output, client_terms=terms)
                    if args.ai_check:
                        try:
                            ai_warnings = run_ai_check(
                                output,
                                endpoint=ai_endpoint,
                                model=ai_model,
                                timeout_seconds=ai_timeout,
                                chunk_max_lines=ai_chunk_max_lines,
                                chunk_max_chars=ai_chunk_max_chars,
                            )
                        except (OSError, ValueError, json.JSONDecodeError) as exc:
                            print(f"error: {exc}", file=sys.stderr)
                            return 2
                        warnings.extend(warning.message() for warning in ai_warnings)
            if args.report:
                try:
                    write_report(
                        args.report,
                        counts=redaction_counts,
                        profile=profile_name,
                        copy_enabled=copy_output,
                        warnings=warnings,
                        ai_warnings=ai_warnings if args.ai_check else None,
                        ai_suggestions=ai_suggestions if args.ai_suggest else None,
                        disabled_categories=disabled_categories,
                    )
                except OSError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
            if args.check_only:
                for warning in warnings:
                    print(f"warning: {warning}", file=sys.stderr)
                if args.summary:
                    print(format_summary(redaction_counts, warnings, disabled_categories), file=sys.stderr)
                return 1 if warnings or ai_suggestions else 0
            output_file = args.output_file
            output_file_is_directory = False
            printed_chunk_label = False
            if args.auto_out:
                auto_output_dir = auto_output_dir_for_profile(profile_options, config_path=args.config, profile_name=profile_name)
                output_file = auto_paste_output_path(output_dir=auto_output_dir)
            elif output_file:
                output_file, output_file_is_directory = explicit_output_path(output_file, input_file=None)
            if output_file:
                if output_file.exists() and not args.force:
                    print(f"error: refusing to overwrite existing output file: {output_file}; pass --force", file=sys.stderr)
                    return 2
                try:
                    _write_private_text(output_file, output)
                except OSError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                if args.auto_out or output_file_is_directory:
                    print(f"wrote redacted output to {output_file}", file=sys.stderr)
            else:
                printed_chunk_label = bool(args.ai_suggest or (profile_name and should_print_profile) or session_data)
                if printed_chunk_label:
                    print(f"\nRedacted text ({chunk_index}):\n", file=sys.stderr, flush=True)
                sys.stdout.write(f"--- redacted {chunk_index} ---\n")
                sys.stdout.write(output)
            if copy_output:
                clipboard_command = copy_to_clipboard(output)
                if clipboard_command:
                    print(f"copied to clipboard via {clipboard_command}", file=sys.stderr)
                else:
                    print("warning: no clipboard command found; install wl-clipboard/xclip/xsel", file=sys.stderr)
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
            if args.summary:
                if not args.auto_out and printed_chunk_label:
                    print(file=sys.stderr)
                print(format_summary(redaction_counts, warnings, disabled_categories), file=sys.stderr)
            return 0

        try:
            for line in sys.stdin:
                if line.rstrip("\r\n") in {"q", "quit", "exit", ":q", ":quit"}:
                    break
                if line in {"\n", "\r\n"}:
                    status = process_interactive_chunk("".join(chunk_lines))
                    if status:
                        return status
                    chunk_lines = []
                else:
                    chunk_lines.append(line)
            if chunk_lines:
                return process_interactive_chunk("".join(chunk_lines))
        except KeyboardInterrupt:
            print("cancelled", file=sys.stderr)
            return 130
        return 0

    add_term_target_file: Path | None = None
    ignore_add_target_file: Path | None = None
    manual_detector_order: list[str] = list(TERM_KIND_ORDER)
    reading_tty_add_list = not args.input_file and (args.add_term or args.ignore_add) and _stdin_is_tty()
    reading_bare_tty_paste = not args.input_file and not args.add_term and not args.ignore_add and _stdin_is_tty()
    if args.add_term:
        target_files = [Path(path).expanduser() for path in args.term_file] or profile_term_files
        if not target_files:
            print("error: --profile-term-add needs --term-file or a profile with term_files", file=sys.stderr)
            return 2
        if profile_name:
            try:
                manual_detector_order = [
                    detector.kind
                    for detector in load_profile_manual_detectors(profile_name, config_path=args.config)
                    if detector.enabled
                ]
            except (FileNotFoundError, KeyError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        if not manual_detector_order:
            print("error: no enabled manual detectors; enable one before using --profile-term-add", file=sys.stderr)
            return 2
        add_term_target_file = target_files[0]
        if not args.input_file and _stdin_is_tty():
            print(f"Add one manual term per line. Finish with {_eof_hint()}.", file=sys.stderr)
            print("Do not add values already covered by built-in detectors:", file=sys.stderr)
            print(", ".join(REDACTION_CATEGORIES), file=sys.stderr)
            print("Use MANUAL DETECTOR: term for explicit placeholders, e.g. ORG: ExampleCo.", file=sys.stderr)
            print(f"Terms will be saved to: {add_term_target_file}", file=sys.stderr)
    elif args.ignore_add:
        if not profile_ignored_suggestion_files:
            print("error: --ignore-add needs an active profile with ignored_suggestion_files", file=sys.stderr)
            return 2
        ignore_add_target_file = profile_ignored_suggestion_files[0]
        if not args.input_file and _stdin_is_tty():
            print(f"Add one ignored AI suggestion term per line. Finish with {_eof_hint()}.", file=sys.stderr)
            print(f"Ignored terms will be saved to: {ignore_add_target_file}", file=sys.stderr)
    elif reading_bare_tty_paste:
        print(f"Paste text to redact. Finish with {_eof_hint()}.", file=sys.stderr)
        print("Redacted text prints to stdout unless --out or --auto-out is used.", file=sys.stderr)

    try:
        if args.input_file:
            text = args.input_file.expanduser().read_text(encoding="utf-8")
        elif reading_bare_tty_paste or reading_tty_add_list:
            text = read_tty_paste_input()
        else:
            text = sys.stdin.read()
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if reading_bare_tty_paste and text and not text.endswith("\n"):
        print(file=sys.stderr)
    if args.add_term:
        if add_term_target_file is None:
            print("error: --profile-term-add needs --term-file or a profile with term_files", file=sys.stderr)
            return 2
        candidate_terms, notices = term_add_candidates(text)
        for notice in notices:
            print(notice, file=sys.stderr)
        try:
            reviewed_terms = review_terms_to_add_via_tty(
                candidate_terms, output_stream=sys.stderr, kind_order=manual_detector_order
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if reviewed_terms is None:
            print("cancelled", file=sys.stderr)
            return 130
        added_count = append_redaction_terms(add_term_target_file, reviewed_terms)
        print(f"added {added_count} term(s) to {add_term_target_file}")
        return 0
    if args.ignore_add:
        if ignore_add_target_file is None:
            print("error: --ignore-add needs an active profile with ignored_suggestion_files", file=sys.stderr)
            return 2
        added_count = append_unassigned_ignored_terms(ignore_add_target_file, text)
        print(f"added {added_count} ignored AI suggestion term(s) to {ignore_add_target_file}")
        return 0

    redaction_counts: dict[str, int] = {}
    if args.no_redact:
        output = text
    else:
        redaction_result = redact_with_counts(
            text,
            client_terms=terms,
            vault=session_data.vault if session_data else None,
            disabled_categories=disabled_categories,
        )
        output = redaction_result.text
        redaction_counts = redaction_result.counts
        if session_data and redaction_result.vault.changed:
            save_session(session_data)
    warnings = check_residual(output, client_terms=terms)
    ai_warnings: list[AIWarning] = []
    ai_suggestions: list[AISuggestion] = []
    if args.ai_check:
        try:
            ai_warnings = run_ai_check(
                output,
                endpoint=ai_endpoint,
                model=ai_model,
                timeout_seconds=ai_timeout,
                chunk_max_lines=ai_chunk_max_lines,
                chunk_max_chars=ai_chunk_max_chars,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        warnings.extend(warning.message() for warning in ai_warnings)
    if args.ai_suggest:
        ignored_suggestions: list[AISuggestion] = []
        try:
            ai_suggestions = run_ai_suggest(
                output,
                endpoint=ai_endpoint,
                model=ai_model,
                timeout_seconds=ai_timeout,
                chunk_max_lines=ai_chunk_max_lines,
                chunk_max_chars=ai_chunk_max_chars,
            )
            ai_suggestions = _filter_ignored_suggestions(ai_suggestions, ignored_suggestion_terms)
            reviewed_suggestions = review_ai_suggestions_via_tty(
                ai_suggestions, output_stream=sys.stderr, ignored_suggestions=ignored_suggestions
            )
            if reviewed_suggestions is None:
                print("cancelled", file=sys.stderr)
                return 130
            ai_suggestions = reviewed_suggestions
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if ignored_suggestions and profile_ignored_suggestion_files:
            ignored_count = append_ignored_suggestions(profile_ignored_suggestion_files[0], ignored_suggestions)
            ignored_suggestion_terms.update(_suggestion_identity(suggestion) for suggestion in ignored_suggestions)
            print(
                f"ignored {ignored_count} AI suggestion term(s) in {profile_ignored_suggestion_files[0]}",
                file=sys.stderr,
            )
    if args.ai_suggest and apply_profile_term_file is not None:
        redaction_result, added_count = apply_ai_suggestions_to_profile(
            text,
            term_file=apply_profile_term_file,
            terms=terms,
            suggestions=ai_suggestions,
            vault=session_data.vault if session_data else None,
            disabled_categories=disabled_categories,
        )
        print(f"added {added_count} AI suggestion term(s) to {apply_profile_term_file}", file=sys.stderr)
        if redaction_result:
            output = redaction_result.text
            redaction_counts = redaction_result.counts
            if session_data and redaction_result.vault.changed:
                save_session(session_data)
            warnings = check_residual(output, client_terms=terms)
            if args.ai_check:
                try:
                    ai_warnings = run_ai_check(
                        output,
                        endpoint=ai_endpoint,
                        model=ai_model,
                        timeout_seconds=ai_timeout,
                        chunk_max_lines=ai_chunk_max_lines,
                        chunk_max_chars=ai_chunk_max_chars,
                    )
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                warnings.extend(warning.message() for warning in ai_warnings)

    if args.report:
        try:
            write_report(
                args.report,
                counts=redaction_counts,
                profile=profile_name,
                copy_enabled=copy_output,
                warnings=warnings,
                ai_warnings=ai_warnings if args.ai_check else None,
                ai_suggestions=ai_suggestions if args.ai_suggest else None,
                disabled_categories=disabled_categories,
            )
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.check_only:
        if args.summary:
            print(format_summary(redaction_counts, warnings, disabled_categories), file=sys.stderr)
        else:
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
        return 1 if warnings or ai_suggestions else 0

    output_file = args.output_file
    output_file_is_directory = False
    if args.auto_out:
        auto_output_dir = auto_output_dir_for_profile(profile_options, config_path=args.config, profile_name=profile_name)
        output_file = (
            auto_output_path(args.input_file, timestamp=args.timestamp, output_dir=auto_output_dir)
            if args.input_file
            else auto_paste_output_path(output_dir=auto_output_dir)
        )
    elif output_file:
        output_file, output_file_is_directory = explicit_output_path(output_file, input_file=args.input_file)
    if output_file:
        if output_file.exists() and not args.force:
            print(f"error: refusing to overwrite existing output file: {output_file}; pass --force", file=sys.stderr)
            return 2
        try:
            _write_private_text(output_file, output)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.auto_out or output_file_is_directory:
        print(f"wrote redacted output to {output_file}", file=sys.stderr)
    if profile_name and should_print_profile:
        print(f"profile: {profile_name}", file=sys.stderr)
    if session_data:
        print(f"session: {session_data.name}", file=sys.stderr)
    if copy_output:
        clipboard_command = copy_to_clipboard(output)
        if clipboard_command:
            print(f"copied to clipboard via {clipboard_command}", file=sys.stderr)
        else:
            print("warning: no clipboard command found; install wl-clipboard/xclip/xsel", file=sys.stderr)
    printed_redacted_text_label = False
    if not output_file:
        printed_redacted_text_label = bool(args.ai_suggest or (profile_name and should_print_profile) or session_data)
        if printed_redacted_text_label:
            print("\nRedacted text:\n", file=sys.stderr, flush=True)
        sys.stdout.write(output)
        if reading_bare_tty_paste and output and not output.endswith("\n"):
            sys.stdout.write("\n")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.summary:
        if printed_redacted_text_label:
            print(file=sys.stderr)
        print(format_summary(redaction_counts, warnings, disabled_categories), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
