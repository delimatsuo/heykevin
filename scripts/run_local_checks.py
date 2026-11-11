#!/usr/bin/env python3
"""Offline local contract verifier and hermetic test runner for Kevin."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Callable, Mapping, Sequence, TextIO

_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPO_ROOT))

from scripts.partition_tests import (  # noqa: E402
    DEFAULT_TOTAL_SHARDS,
    PartitionError,
    discover_test_files,
    partition_test_files,
    render_test_paths,
)

VALID_CATEGORIES = frozenset({"a", "b", "c", "d"})
VALID_REACHABILITY = frozenset(
    {"live_or_worker", "isolated_demo_entrypoint", "unmounted"}
)
LIVE_ENTRYPOINT_ROOTS = (
    "app/main.py",
    "app/worker_main.py",
    "app/worker_runner.py",
    "app/__init__.py",
)
DEMO_ENTRYPOINT_ROOTS = (
    "app/public_demo_main.py",
    "app/public_demo_breaker_main.py",
)
CLASSIFICATION_DOC_REL_PATH = "docs/backend-module-classification.md"
EXPECTED_TABLE_COLUMNS = ("Module Path", "Category", "Reachability", "Notes")
RUFF_FATAL_SELECTORS = "E9,F63,F7,F82"

ALLOWED_SYSTEM_ENV_KEYS = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "SYSTEMROOT",
)

MACOS_SANDBOX_EXEC_BIN = Path("/usr/bin/sandbox-exec")
MACOS_SANDBOX_PROFILE = "(version 1) (allow default) (deny network*)"

_CONTROL_CHARS = frozenset(range(0, 32)) | {127}
_ALLOWED_REQ_WS = frozenset({9})  # Horizontal tab is legal wsp in PEP 508.
_FORBIDDEN_APP_DYNAMIC_IMPORTS = frozenset({"__import__", "import_module"})

_PEP508_IDENT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_PEP440_VERSION_RE = re.compile(
    r"^[vV]?"
    r"(?:(?P<epoch>[0-9]+)! )?".replace(" ", "")
    + r"(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?P<pre>[-_.]?(?:a|alpha|b|beta|c|rc|pre|preview)[-_.]?[0-9]*)?"
    r"(?P<post>(?:-[0-9]+)|(?:[-_.]?(?:post|rev|r)[-_.]?[0-9]*))?"
    r"(?P<dev>[-_.]?dev[-_.]?[0-9]*)?"
    r"(?:\+(?P<local>[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*))?$",
    re.IGNORECASE,
)
_PEP440_WILDCARD_RE = re.compile(
    r"^[vV]?(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*"
    r"(?:[-_.]?(?:a|alpha|b|beta|c|rc|pre|preview)[-_.]?[0-9]*)?"
    r"(?:(?:-[0-9]+)|(?:[-_.]?(?:post|rev|r)[-_.]?[0-9]*))?"
    r"(?:[-_.]?dev[-_.]?[0-9]*)?\.\*$",
    re.IGNORECASE,
)
_ARBITRARY_VERSION_RE = re.compile(r"^[A-Za-z0-9._*+-]+$")
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^\s;]+$")

_PEP508_MARKER_ENV_VARS = frozenset(
    {
        "python_version",
        "python_full_version",
        "os_name",
        "sys_platform",
        "platform_release",
        "platform_system",
        "platform_version",
        "platform_machine",
        "platform_python_implementation",
        "implementation_name",
        "implementation_version",
        "extra",
    }
)

_NETWORK_GUARD_MODULE_NAME = "_kevin_offline_network_guard"
_NETWORK_GUARD_PLUGIN_SOURCE = '''"""Pre-collection pytest offline network guard."""

from __future__ import annotations

import errno
import ipaddress
import socket

_BLOCKED_FAMILIES = frozenset(
    family
    for family in (
        getattr(socket, "AF_INET", None),
        getattr(socket, "AF_INET6", None),
    )
    if family is not None
)

_ORIG_CONNECT = socket.socket.connect
_ORIG_CONNECT_EX = socket.socket.connect_ex
_ORIG_SENDTO = socket.socket.sendto
_ORIG_CREATE_CONNECTION = socket.create_connection
_ORIG_GETADDRINFO = socket.getaddrinfo


def _deny_network(operation: str, target: object) -> PermissionError:
    return PermissionError(
        errno.EPERM,
        f"Offline test runner blocked {operation} to {target!r}",
    )


def _is_numeric_or_wildcard_host(host: object) -> bool:
    if host is None or host == "" or host == b"":
        return True
    if isinstance(host, bytes):
        try:
            text = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    elif isinstance(host, str):
        text = host
    else:
        return False
    addr_part = text.split("%", 1)[0]
    if addr_part.startswith("[") and addr_part.endswith("]"):
        addr_part = addr_part[1:-1]
    try:
        ipaddress.ip_address(addr_part)
        return True
    except ValueError:
        return False


def _guarded_connect(self: socket.socket, address: object) -> object:
    if self.family in _BLOCKED_FAMILIES:
        raise _deny_network("socket.connect", address)
    return _ORIG_CONNECT(self, address)


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    if self.family in _BLOCKED_FAMILIES:
        raise _deny_network("socket.connect_ex", address)
    return _ORIG_CONNECT_EX(self, address)


def _guarded_sendto(self: socket.socket, data: object, *args: object) -> int:
    if self.family in _BLOCKED_FAMILIES:
        target = args[-1] if args else None
        raise _deny_network("socket.sendto", target)
    return _ORIG_SENDTO(self, data, *args)


def _guarded_create_connection(
    address: object, *args: object, **kwargs: object
) -> socket.socket:
    raise _deny_network("socket.create_connection", address)


def _guarded_getaddrinfo(
    host: object,
    port: object,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> object:
    if not _is_numeric_or_wildcard_host(host):
        raise _deny_network("socket.getaddrinfo", host)
    effective_flags = flags
    if host not in (None, "", b""):
        effective_flags |= getattr(socket, "AI_NUMERICHOST", 0)
    return _ORIG_GETADDRINFO(host, port, family, type, proto, effective_flags)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
socket.socket.sendto = _guarded_sendto  # type: ignore[method-assign]
socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
'''


class LocalChecksError(Exception):
    """Base error raised when local contract checks or runner preconditions fail."""


class ContractViolationError(LocalChecksError):
    """Raised when dependency, manifest, AST firewall, or shard contracts fail."""


class EnvironmentContractError(LocalChecksError):
    """Raised when interpreter, toolchain, or CLI argument preconditions fail."""


class SandboxUnavailableError(LocalChecksError):
    """Raised when --require-os-sandbox is set and OS network sandboxing fails."""


@dataclass(frozen=True)
class ParsedRequirement:
    raw: str
    name: str
    canonical_name: str
    extras: tuple[str, ...]
    specifier: str
    url: str | None
    marker: str | None


@dataclass(frozen=True)
class ModuleRecord:
    module_path: str
    category: str
    reachability: str
    notes: str


@dataclass(frozen=True)
class ClassificationReport:
    records: dict[str, ModuleRecord]
    live_closure: frozenset[str]
    demo_only_closure: frozenset[str]
    unmounted_modules: frozenset[str]


@dataclass(frozen=True)
class SandboxResolution:
    prefix: tuple[str, ...]
    mode: str
    reason: str


def canonicalize_package_name(name: str) -> str:
    """Canonicalize a Python distribution name per PEP 503."""
    if not isinstance(name, str) or not name.strip():
        raise ContractViolationError(f"Invalid package name: {name!r}")
    return re.sub(r"[-_.]+", "-", name).lower()


def _skip_wsp(text: str, idx: int) -> int:
    while idx < len(text) and text[idx] in (" ", "\t"):
        idx += 1
    return idx


def _validate_version_clause(operator: str, version_str: str, raw: str) -> None:
    if not version_str:
        raise ContractViolationError(
            f"Malformed PEP 508 version specifier in {raw!r}: missing version after {operator!r}"
        )
    if operator == "===":
        if not _ARBITRARY_VERSION_RE.match(version_str):
            raise ContractViolationError(
                f"Malformed arbitrary version {version_str!r} in {raw!r}"
            )
        return

    if version_str.endswith(".*"):
        if operator not in ("==", "!="):
            raise ContractViolationError(
                f"Wildcard version {version_str!r} only permitted with == or != in {raw!r}"
            )
        if not _PEP440_WILDCARD_RE.match(version_str):
            raise ContractViolationError(
                f"Malformed PEP 440 wildcard version {version_str!r} in {raw!r}"
            )
        return

    match = _PEP440_VERSION_RE.match(version_str)
    if not match:
        raise ContractViolationError(
            f"Malformed PEP 440 version {version_str!r} in {raw!r}"
        )
    local_part = match.group("local")
    release_part = match.group("release")
    if operator == "~=":
        if "." not in release_part:
            raise ContractViolationError(
                f"Compatible release operator ~= requires at least two segments in {raw!r}"
            )
        if local_part is not None:
            raise ContractViolationError(
                f"Local version label not permitted with ~= in {raw!r}"
            )
    elif operator in ("<", ">", "<=", ">=") and local_part is not None:
        raise ContractViolationError(
            f"Local version label not permitted with {operator} in {raw!r}"
        )


def _parse_version_many(body: str, raw: str) -> str:
    body_stripped = body.strip(" \t")
    if not body_stripped:
        raise ContractViolationError(
            f"Empty version specifier group in requirement {raw!r}"
        )
    parts = body_stripped.split(",")
    normalized_clauses: list[str] = []
    op_re = re.compile(r"^(===|~=|==|!=|<=|>=|<|>)\s*(.*)$")
    for part in parts:
        clause = part.strip(" \t")
        if not clause:
            raise ContractViolationError(
                f"Empty or trailing comma in version specifier {raw!r}"
            )
        m = op_re.match(clause)
        if not m:
            raise ContractViolationError(
                f"Invalid version operator in clause {clause!r} of {raw!r}"
            )
        op, ver = m.group(1), m.group(2).strip(" \t")
        _validate_version_clause(op, ver, raw)
        normalized_clauses.append(f"{op}{ver}")
    return ",".join(normalized_clauses)


class _MarkerParser:
    """Recursive-descent parser for PEP 508 environment markers."""

    def __init__(self, text: str, raw_requirement: str) -> None:
        self._text = text
        self._raw = raw_requirement
        self._pos = 0

    def parse(self) -> str:
        self._skip_ws()
        if self._pos >= len(self._text):
            raise ContractViolationError(
                f"Empty environment marker after ';' in {self._raw!r}"
            )
        self._parse_or()
        self._skip_ws()
        if self._pos != len(self._text):
            raise ContractViolationError(
                f"Unexpected trailing marker syntax at {self._text[self._pos:]!r} in {self._raw!r}"
            )
        return self._text.strip(" \t")

    def _skip_ws(self) -> None:
        self._pos = _skip_wsp(self._text, self._pos)

    def _peek_keyword(self, kw: str) -> bool:
        end = self._pos + len(kw)
        if self._text[self._pos : end] != kw:
            return False
        if end < len(self._text) and (
            self._text[end].isalnum() or self._text[end] == "_"
        ):
            return False
        return True

    def _parse_or(self) -> None:
        self._parse_and()
        while True:
            self._skip_ws()
            if self._peek_keyword("or"):
                self._pos += 2
                self._parse_and()
            else:
                break

    def _parse_and(self) -> None:
        self._parse_atom()
        while True:
            self._skip_ws()
            if self._peek_keyword("and"):
                self._pos += 3
                self._parse_atom()
            else:
                break

    def _parse_atom(self) -> None:
        self._skip_ws()
        if self._pos < len(self._text) and self._text[self._pos] == "(":
            self._pos += 1
            self._parse_or()
            self._skip_ws()
            if self._pos >= len(self._text) or self._text[self._pos] != ")":
                raise ContractViolationError(
                    f"Unclosed '(' in environment marker of {self._raw!r}"
                )
            self._pos += 1
            return

        lhs_kind = self._parse_marker_value()
        self._parse_marker_op()
        rhs_kind = self._parse_marker_value()
        if "env_var" not in (lhs_kind, rhs_kind):
            raise ContractViolationError(
                f"Environment marker comparison must reference a PEP 508 variable in {self._raw!r}"
            )

    def _parse_marker_value(self) -> str:
        self._skip_ws()
        if self._pos >= len(self._text):
            raise ContractViolationError(
                f"Unexpected end of environment marker in {self._raw!r}"
            )
        ch = self._text[self._pos]
        if ch in ("'", '"'):
            quote = ch
            self._pos += 1
            start = self._pos
            while self._pos < len(self._text) and self._text[self._pos] != quote:
                if self._text[self._pos] == "\\":
                    self._pos += 2
                    continue
                self._pos += 1
            if self._pos >= len(self._text):
                raise ContractViolationError(
                    f"Unterminated string literal in marker of {self._raw!r}"
                )
            _ = self._text[start : self._pos]
            self._pos += 1
            return "str"

        ident_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", self._text[self._pos :])
        if not ident_match:
            raise ContractViolationError(
                f"Invalid marker operand at {self._text[self._pos:]!r} in {self._raw!r}"
            )
        ident = ident_match.group(0)
        if ident not in _PEP508_MARKER_ENV_VARS:
            raise ContractViolationError(
                f"Unknown PEP 508 marker variable {ident!r} in {self._raw!r}"
            )
        self._pos += len(ident)
        return "env_var"

    def _parse_marker_op(self) -> None:
        self._skip_ws()
        for op in ("===", "~=", "==", "!=", "<=", ">=", "<", ">"):
            if self._text.startswith(op, self._pos):
                self._pos += len(op)
                return
        if self._peek_keyword("in"):
            self._pos += 2
            return
        if self._peek_keyword("not"):
            saved = self._pos
            self._pos += 3
            ws_after = _skip_wsp(self._text, self._pos)
            if ws_after > self._pos:
                self._pos = ws_after
                if self._peek_keyword("in"):
                    self._pos += 2
                    return
            self._pos = saved
        raise ContractViolationError(
            f"Invalid marker operator at {self._text[self._pos:]!r} in {self._raw!r}"
        )


def parse_pep508_requirement(raw: str) -> ParsedRequirement:
    """Strictly parse a PEP 508 requirement string using Python stdlib only."""
    if not isinstance(raw, str):
        raise ContractViolationError(
            f"Requirement entry must be a string, got {type(raw).__name__}"
        )
    if any((ord(c) in _CONTROL_CHARS and ord(c) not in _ALLOWED_REQ_WS) for c in raw):
        raise ContractViolationError(
            f"Control characters are not permitted in requirement {raw!r}"
        )

    idx = _skip_wsp(raw, 0)
    if idx >= len(raw):
        raise ContractViolationError("Requirement string cannot be empty.")

    name_match = _PEP508_IDENT_RE.match(raw, idx)
    if not name_match:
        raise ContractViolationError(
            f"Malformed PEP 508 distribution name in {raw!r}"
        )
    name = name_match.group(0)
    idx = name_match.end()
    # Ensure name wasn't truncated right before invalid punctuation like 'foo-'
    if idx < len(raw) and raw[idx] in ("-", "_", "."):
        raise ContractViolationError(
            f"Malformed PEP 508 distribution name trailing punctuation in {raw!r}"
        )

    idx = _skip_wsp(raw, idx)
    extras: list[str] = []
    if idx < len(raw) and raw[idx] == "[":
        idx += 1
        idx = _skip_wsp(raw, idx)
        if idx >= len(raw):
            raise ContractViolationError(f"Unclosed '[' in extras of {raw!r}")
        if raw[idx] != "]":
            while True:
                extra_match = _PEP508_IDENT_RE.match(raw, idx)
                if not extra_match:
                    raise ContractViolationError(
                        f"Invalid extra identifier in {raw!r}"
                    )
                extra_end = extra_match.end()
                if extra_end < len(raw) and raw[extra_end] in ("-", "_", "."):
                    raise ContractViolationError(
                        f"Invalid extra identifier trailing punctuation in {raw!r}"
                    )
                extras.append(canonicalize_package_name(extra_match.group(0)))
                idx = _skip_wsp(raw, extra_end)
                if idx >= len(raw):
                    raise ContractViolationError(f"Unclosed '[' in extras of {raw!r}")
                if raw[idx] == ",":
                    idx = _skip_wsp(raw, idx + 1)
                    if idx < len(raw) and raw[idx] == "]":
                        raise ContractViolationError(
                            f"Trailing comma in extras of {raw!r}"
                        )
                elif raw[idx] == "]":
                    break
                else:
                    raise ContractViolationError(
                        f"Unexpected character {raw[idx]!r} in extras of {raw!r}"
                    )
        idx += 1

    idx = _skip_wsp(raw, idx)

    # Split optional '; marker' outside quotes
    semi_pos: int | None = None
    in_quote: str | None = None
    pos = idx
    while pos < len(raw):
        ch = raw[pos]
        if in_quote is not None:
            if ch == "\\":
                pos += 2
                continue
            if ch == in_quote:
                in_quote = None
        else:
            if ch in ("'", '"'):
                in_quote = ch
            elif ch == ";":
                semi_pos = pos
                break
        pos += 1
    if in_quote is not None:
        raise ContractViolationError(f"Unterminated quote in requirement {raw!r}")

    spec_segment = raw[idx : semi_pos if semi_pos is not None else len(raw)].strip(
        " \t"
    )
    marker_segment = raw[semi_pos + 1 :] if semi_pos is not None else None

    url: str | None = None
    specifier = ""

    if spec_segment:
        if spec_segment.startswith("@"):
            # PEP 508 requires whitespace between name/extras and '@' when bare name precedes '@'
            # unless ']' immediately preceded '@'; enforce valid URI after '@'.
            if raw[idx] == "@" and idx > 0 and raw[idx - 1] not in (" ", "\t", "]"):
                raise ContractViolationError(
                    f"PEP 508 direct URL requires whitespace before '@' in {raw!r}"
                )
            uri_candidate = spec_segment[1:].strip(" \t")
            if not _URI_SCHEME_RE.match(uri_candidate):
                raise ContractViolationError(
                    f"Malformed PEP 508 direct URL {uri_candidate!r} in {raw!r}"
                )
            url = uri_candidate
        elif "@" in spec_segment:
            raise ContractViolationError(
                f"Cannot combine version specifier and direct URL '@' in {raw!r}"
            )
        elif spec_segment.startswith("("):
            if not spec_segment.endswith(")") or "(" in spec_segment[1:-1] or ")" in spec_segment[1:-1]:
                raise ContractViolationError(
                    f"Unbalanced parentheses in version specifier of {raw!r}"
                )
            specifier = _parse_version_many(spec_segment[1:-1], raw)
        else:
            if "(" in spec_segment or ")" in spec_segment:
                raise ContractViolationError(
                    f"Unexpected parenthesis in version specifier of {raw!r}"
                )
            specifier = _parse_version_many(spec_segment, raw)

    marker: str | None = None
    if marker_segment is not None:
        marker = _MarkerParser(marker_segment, raw).parse()

    return ParsedRequirement(
        raw=raw,
        name=name,
        canonical_name=canonicalize_package_name(name),
        extras=tuple(sorted(set(extras))),
        specifier=specifier,
        url=url,
        marker=marker,
    )


def verify_pyproject_contract(pyproject_path: Path) -> dict[str, ParsedRequirement]:
    """Validate pyproject.toml dependency specifiers and canonical name uniqueness."""
    if not pyproject_path.exists():
        raise ContractViolationError(f"Missing pyproject.toml: {pyproject_path}")
    if pyproject_path.is_symlink() or os.path.islink(pyproject_path):
        raise ContractViolationError(
            f"Symlink pyproject.toml is not permitted: {pyproject_path}"
        )
    if not pyproject_path.is_file():
        raise ContractViolationError(
            f"pyproject.toml must be a regular file: {pyproject_path}"
        )

    try:
        raw_bytes = pyproject_path.read_bytes()
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractViolationError(
            f"Failed to parse {pyproject_path}: {exc}"
        ) from exc

    project = data.get("project")
    if not isinstance(project, dict):
        raise ContractViolationError("pyproject.toml missing [project] table.")

    deps = project.get("dependencies")
    if not isinstance(deps, list):
        raise ContractViolationError(
            "pyproject.toml [project.dependencies] must be a list of PEP 508 strings."
        )

    runtime_by_canonical: dict[str, ParsedRequirement] = {}
    for entry in deps:
        parsed = parse_pep508_requirement(entry)
        prev = runtime_by_canonical.get(parsed.canonical_name)
        if prev is not None:
            raise ContractViolationError(
                f"Duplicate canonical dependency {parsed.canonical_name!r} in "
                f"[project.dependencies]: {prev.raw!r} and {parsed.raw!r}"
            )
        runtime_by_canonical[parsed.canonical_name] = parsed

    opt_deps = project.get("optional-dependencies", {})
    if opt_deps is None or not isinstance(opt_deps, dict):
        raise ContractViolationError(
            "pyproject.toml [project.optional-dependencies] must be a table."
        )

    optional_by_canonical: dict[str, tuple[str, ParsedRequirement]] = {}
    for group_name, group_entries in sorted(opt_deps.items()):
        if not isinstance(group_name, str) or not group_name.strip():
            raise ContractViolationError(
                f"Invalid optional dependency group name: {group_name!r}"
            )
        if not isinstance(group_entries, list):
            raise ContractViolationError(
                f"[project.optional-dependencies.{group_name}] must be a list."
            )
        seen_in_group: dict[str, ParsedRequirement] = {}
        for entry in group_entries:
            parsed = parse_pep508_requirement(entry)
            if parsed.canonical_name in seen_in_group:
                prev_group = seen_in_group[parsed.canonical_name]
                raise ContractViolationError(
                    f"Duplicate canonical dependency {parsed.canonical_name!r} in "
                    f"[project.optional-dependencies.{group_name}]: "
                    f"{prev_group.raw!r} and {parsed.raw!r}"
                )
            seen_in_group[parsed.canonical_name] = parsed

            if parsed.canonical_name in runtime_by_canonical:
                runtime_prev = runtime_by_canonical[parsed.canonical_name]
                raise ContractViolationError(
                    f"Dependency {parsed.canonical_name!r} in "
                    f"[project.optional-dependencies.{group_name}] ({parsed.raw!r}) "
                    f"redundantly redeclares runtime dependency {runtime_prev.raw!r}"
                )

            if parsed.canonical_name in optional_by_canonical:
                other_group, other_req = optional_by_canonical[parsed.canonical_name]
                if (
                    other_req.extras != parsed.extras
                    or other_req.specifier != parsed.specifier
                    or other_req.url != parsed.url
                    or other_req.marker != parsed.marker
                ):
                    raise ContractViolationError(
                        f"Conflicting optional dependency {parsed.canonical_name!r} "
                        f"across groups {other_group!r} ({other_req.raw!r}) and "
                        f"{group_name!r} ({parsed.raw!r})"
                    )
            else:
                optional_by_canonical[parsed.canonical_name] = (group_name, parsed)

    return runtime_by_canonical


def _strip_optional_backticks(cell: str) -> str:
    val = cell.strip()
    if len(val) >= 2 and val.startswith("`") and val.endswith("`"):
        inner = val[1:-1].strip()
        if not inner:
            raise ContractViolationError("Empty backticked cell in classification table.")
        return inner
    return val


def _validate_manifest_module_path(module_path: str) -> None:
    if not module_path:
        raise ContractViolationError("Module Path cell cannot be empty.")
    if "\\" in module_path or any(ord(c) in _CONTROL_CHARS for c in module_path):
        raise ContractViolationError(
            f"Invalid characters in Module Path {module_path!r}"
        )
    if module_path.startswith("/") or "//" in module_path:
        raise ContractViolationError(
            f"Module Path must be normalized repo-relative POSIX: {module_path!r}"
        )
    pure = PurePosixPath(module_path)
    if ".." in pure.parts or "." in pure.parts:
        raise ContractViolationError(
            f"Traversal segments are not permitted in Module Path: {module_path!r}"
        )
    if len(pure.parts) < 2 or pure.parts[0] != "app" or not module_path.endswith(".py"):
        raise ContractViolationError(
            f"Module Path must target an app/**/*.py file, got {module_path!r}"
        )


def _split_markdown_row(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        raise ContractViolationError(f"Malformed markdown table row: {line!r}")
    cells = [c.strip() for c in stripped[1:-1].split("|")]
    return tuple(cells)


def _is_separator_row(cells: Sequence[str]) -> bool:
    return bool(cells) and all(re.match(r"^:?-{3,}:?$", c) is not None for c in cells)


def parse_classification_manifest(manifest_path: Path) -> dict[str, ModuleRecord]:
    """Parse docs/backend-module-classification.md into validated ModuleRecord rows."""
    if not manifest_path.exists():
        raise ContractViolationError(
            f"Missing module classification manifest: {manifest_path}"
        )
    if manifest_path.is_symlink() or os.path.islink(manifest_path):
        raise ContractViolationError(
            f"Symlink manifest file rejected: {manifest_path}"
        )
    if not manifest_path.is_file():
        raise ContractViolationError(
            f"Classification manifest must be a regular file: {manifest_path}"
        )

    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractViolationError(
            f"Cannot read classification manifest {manifest_path}: {exc}"
        ) from exc

    records: dict[str, ModuleRecord] = {}
    table_count = 0
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if not line.startswith("|"):
            idx += 1
            continue

        header_cells = _split_markdown_row(line)
        if header_cells != EXPECTED_TABLE_COLUMNS:
            idx += 1
            continue

        table_count += 1
        if table_count > 1:
            raise ContractViolationError(
                f"Multiple module classification tables found in {manifest_path}"
            )

        idx += 1
        if idx >= len(lines) or not lines[idx].strip().startswith("|"):
            raise ContractViolationError(
                f"Classification table in {manifest_path} missing separator row."
            )
        sep_cells = _split_markdown_row(lines[idx])
        if len(sep_cells) != len(EXPECTED_TABLE_COLUMNS) or not _is_separator_row(
            sep_cells
        ):
            raise ContractViolationError(
                f"Invalid separator row in {manifest_path}: {lines[idx]!r}"
            )

        idx += 1
        while idx < len(lines) and lines[idx].strip().startswith("|"):
            row_cells = _split_markdown_row(lines[idx])
            if len(row_cells) != 4:
                raise ContractViolationError(
                    f"Classification table row must have 4 columns, got {len(row_cells)}: {lines[idx]!r}"
                )
            mod_path = _strip_optional_backticks(row_cells[0])
            category = _strip_optional_backticks(row_cells[1])
            reachability = _strip_optional_backticks(row_cells[2])
            notes = row_cells[3].strip()

            _validate_manifest_module_path(mod_path)
            if category not in VALID_CATEGORIES:
                raise ContractViolationError(
                    f"Invalid category {category!r} for {mod_path}; expected one of {sorted(VALID_CATEGORIES)}"
                )
            if reachability not in VALID_REACHABILITY:
                raise ContractViolationError(
                    f"Invalid reachability {reachability!r} for {mod_path}; "
                    f"expected one of {sorted(VALID_REACHABILITY)}"
                )
            if mod_path in records:
                raise ContractViolationError(
                    f"Duplicate module path in classification manifest: {mod_path}"
                )

            records[mod_path] = ModuleRecord(
                module_path=mod_path,
                category=category,
                reachability=reachability,
                notes=notes,
            )
            idx += 1

    if table_count == 0 or not records:
        raise ContractViolationError(
            f"No valid '| Module Path | Category | Reachability | Notes |' table found in {manifest_path}"
        )

    return records


def _discover_app_py_files(repo_root: Path) -> set[str]:
    app_dir = repo_root / "app"
    if not app_dir.exists():
        raise ContractViolationError(f"Missing app/ directory under {repo_root}")
    if app_dir.is_symlink() or os.path.islink(app_dir):
        raise ContractViolationError(f"Symlink app/ directory rejected: {app_dir}")
    if not app_dir.is_dir():
        raise ContractViolationError(f"app/ must be a directory: {app_dir}")

    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(str(app_dir), followlinks=False):
        if os.path.islink(dirpath):
            raise ContractViolationError(f"Symlink directory under app/ rejected: {dirpath}")
        dir_p = Path(dirpath)
        for d in sorted(dirnames):
            sub_p = dir_p / d
            if sub_p.is_symlink() or os.path.islink(sub_p):
                raise ContractViolationError(
                    f"Symlink directory under app/ rejected: {sub_p.as_posix()}"
                )
        for fname in sorted(filenames):
            file_p = dir_p / fname
            if file_p.is_symlink() or os.path.islink(file_p):
                raise ContractViolationError(
                    f"Symlink file under app/ rejected: {file_p.as_posix()}"
                )
            if not file_p.is_file():
                continue
            if fname.endswith(".py"):
                rel_posix = file_p.relative_to(repo_root).as_posix()
                found.add(rel_posix)

    if not found:
        raise ContractViolationError(f"No .py files discovered under {app_dir}")
    return found


def _module_dotted_name(rel_posix: str) -> str:
    pure = PurePosixPath(rel_posix)
    if pure.name == "__init__.py":
        return ".".join(pure.parent.parts)
    return ".".join((*pure.parent.parts, pure.stem))


def _collect_ast_imports_and_check_dynamic(
    repo_root: Path,
    rel_posix: str,
    path_to_mod: Mapping[str, str],
    mod_to_path: Mapping[str, str],
) -> set[str]:
    file_path = repo_root / rel_posix
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel_posix)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise ContractViolationError(
            f"Failed to parse {rel_posix}: {exc}"
        ) from exc

    cur_mod = path_to_mod[rel_posix]
    is_pkg = rel_posix.endswith("/__init__.py")
    pkg_parts = cur_mod.split(".") if is_pkg else cur_mod.split(".")[:-1]

    dynamic_aliases = set(_FORBIDDEN_APP_DYNAMIC_IMPORTS)
    importlib_aliases = {"importlib", "builtins"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("importlib", "builtins"):
                    importlib_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module in ("importlib", "builtins"):
            for alias in node.names:
                if alias.name in _FORBIDDEN_APP_DYNAMIC_IMPORTS:
                    dynamic_aliases.add(alias.asname or alias.name)

    deps: set[str] = set()

    def add_mod_chain(dotted: str) -> None:
        parts = dotted.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            target_rel = mod_to_path.get(prefix)
            if target_rel is not None and target_rel != rel_posix:
                deps.add(target_rel)

    # Importing any submodule implicitly loads its ancestor package __init__.py chain.
    add_mod_chain(cur_mod)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in dynamic_aliases:
                raise ContractViolationError(
                    f"Dynamic import call {node.func.id}() is forbidden in {rel_posix}:{node.lineno}"
                )
            if isinstance(node.func, ast.Attribute) and (
                node.func.attr in _FORBIDDEN_APP_DYNAMIC_IMPORTS
                or (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id in importlib_aliases
                    and node.func.attr in _FORBIDDEN_APP_DYNAMIC_IMPORTS
                )
            ):
                raise ContractViolationError(
                    f"Dynamic import call .{node.func.attr}() is forbidden in {rel_posix}:{node.lineno}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app" or alias.name.startswith("app."):
                    if alias.name not in mod_to_path:
                        raise ContractViolationError(
                            f"Unresolved internal import {alias.name!r} in {rel_posix}:{node.lineno}"
                        )
                    add_mod_chain(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                up = node.level - 1
                if up >= len(pkg_parts):
                    raise ContractViolationError(
                        f"Relative import escapes top-level package in {rel_posix}:{node.lineno}"
                    )
                base_parts = list(pkg_parts[: len(pkg_parts) - up])
                if node.module:
                    base_parts.extend(node.module.split("."))
                base = ".".join(base_parts)
            else:
                base = node.module or ""

            if base == "app" or base.startswith("app."):
                if base not in mod_to_path:
                    raise ContractViolationError(
                        f"Unresolved internal import base {base!r} in {rel_posix}:{node.lineno}"
                    )
                add_mod_chain(base)
                for alias in node.names:
                    if alias.name == "*":
                        pkg_prefix = f"{base}."
                        for cand_mod in mod_to_path:
                            if cand_mod.startswith(pkg_prefix):
                                add_mod_chain(cand_mod)
                    else:
                        cand_mod = f"{base}.{alias.name}"
                        if cand_mod in mod_to_path:
                            add_mod_chain(cand_mod)

    return deps


def _compute_closure(
    roots: Sequence[str], edges: Mapping[str, set[str]]
) -> frozenset[str]:
    visited: set[str] = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for nxt in edges.get(current, ()):
            if nxt not in visited:
                stack.append(nxt)
    return frozenset(visited)


def verify_module_classification_contract(repo_root: Path) -> ClassificationReport:
    """Verify 1:1 manifest bijection, AST import firewall, and reachability closures."""
    docs_dir = repo_root / "docs"
    if docs_dir.is_symlink() or os.path.islink(docs_dir):
        raise ContractViolationError(f"Symlink docs/ directory rejected: {docs_dir}")

    manifest_path = repo_root / CLASSIFICATION_DOC_REL_PATH
    records = parse_classification_manifest(manifest_path)
    disk_files = _discover_app_py_files(repo_root)

    manifest_files = set(records.keys())
    unclassified = sorted(disk_files - manifest_files)
    stale = sorted(manifest_files - disk_files)
    if unclassified or stale:
        details: list[str] = []
        if unclassified:
            details.append(f"unclassified on disk={unclassified}")
        if stale:
            details.append(f"missing from disk={stale}")
        raise ContractViolationError(
            f"Module classification bijection failed ({'; '.join(details)})"
        )

    if "app/main.py" not in records:
        raise ContractViolationError(
            "Live entrypoint 'app/main.py' must exist in app/ and classification manifest."
        )

    for mod_path, rec in sorted(records.items()):
        if rec.category == "a" and rec.reachability != "live_or_worker":
            raise ContractViolationError(
                f"Category 'a' module {mod_path} must have reachability 'live_or_worker', "
                f"got {rec.reachability!r}"
            )
        if rec.category == "c" and rec.reachability != "unmounted":
            raise ContractViolationError(
                f"Category 'c' module {mod_path} must have reachability 'unmounted', "
                f"got {rec.reachability!r}"
            )
        if rec.reachability == "isolated_demo_entrypoint" and rec.category != "d":
            raise ContractViolationError(
                f"isolated_demo_entrypoint module {mod_path} must have category 'd', "
                f"got {rec.category!r}"
            )

    path_to_mod = {rel: _module_dotted_name(rel) for rel in records}
    mod_to_path: dict[str, str] = {}
    for rel, dotted in sorted(path_to_mod.items()):
        if dotted in mod_to_path:
            raise ContractViolationError(
                f"Ambiguous Python module mapping for {dotted!r}: "
                f"{mod_to_path[dotted]} and {rel}"
            )
        mod_to_path[dotted] = rel

    edges: dict[str, set[str]] = {}
    for rel in sorted(records):
        edges[rel] = _collect_ast_imports_and_check_dynamic(
            repo_root, rel, path_to_mod, mod_to_path
        )

    # Firewall rule 1: Zero imports from (a) | (b) | (d) into (c).
    violations_into_c: list[str] = []
    for src_rel, targets in sorted(edges.items()):
        src_cat = records[src_rel].category
        if src_cat in ("a", "b", "d"):
            for dst_rel in sorted(targets):
                if records[dst_rel].category == "c":
                    violations_into_c.append(f"{src_rel} ({src_cat}) -> {dst_rel} (c)")
    if violations_into_c:
        raise ContractViolationError(
            f"Forbidden AST import(s) into isolated category 'c': {violations_into_c}"
        )

    live_roots = [r for r in LIVE_ENTRYPOINT_ROOTS if r in records]
    live_closure = _compute_closure(live_roots, edges)

    demo_roots = [r for r in DEMO_ENTRYPOINT_ROOTS if r in records]
    demo_closure = _compute_closure(demo_roots, edges) if demo_roots else frozenset()
    demo_only_closure = demo_closure - live_closure
    unmounted_modules = frozenset(set(records.keys()) - live_closure - demo_only_closure)

    # Firewall rule 2: Every category 'a' module must be reachable from live/worker roots.
    unreachable_a = sorted(
        rel for rel, rec in records.items() if rec.category == "a" and rel not in live_closure
    )
    if unreachable_a:
        raise ContractViolationError(
            f"Category 'a' module(s) unreachable from live/worker entrypoints: {unreachable_a}"
        )

    # Firewall rule 3: Exact live_or_worker closure equivalence and permitted categories.
    bad_live_cat = sorted(
        rel for rel in live_closure if records[rel].category not in ("a", "b", "d")
    )
    if bad_live_cat:
        raise ContractViolationError(
            f"Live/worker closure contains disallowed category module(s): {bad_live_cat}"
        )

    expected_live_tagged = {
        rel for rel, rec in records.items() if rec.reachability == "live_or_worker"
    }
    if set(live_closure) != expected_live_tagged:
        missing_tag = sorted(set(live_closure) - expected_live_tagged)
        extra_tag = sorted(expected_live_tagged - set(live_closure))
        raise ContractViolationError(
            f"live_or_worker reachability mismatch: "
            f"in closure but tagged otherwise={missing_tag}; "
            f"tagged live_or_worker but unreachable={extra_tag}"
        )

    # Firewall rule 4: Exact isolated_demo_entrypoint closure equivalence and category == 'd'.
    bad_demo_cat = sorted(
        rel for rel in demo_only_closure if records[rel].category != "d"
    )
    if bad_demo_cat:
        raise ContractViolationError(
            f"Demo-only closure contains non-'d' module(s): {bad_demo_cat}"
        )

    expected_demo_tagged = {
        rel
        for rel, rec in records.items()
        if rec.reachability == "isolated_demo_entrypoint"
    }
    if set(demo_only_closure) != expected_demo_tagged:
        missing_demo = sorted(set(demo_only_closure) - expected_demo_tagged)
        extra_demo = sorted(expected_demo_tagged - set(demo_only_closure))
        raise ContractViolationError(
            f"isolated_demo_entrypoint reachability mismatch: "
            f"in demo closure but tagged otherwise={missing_demo}; "
            f"tagged isolated_demo_entrypoint but outside demo-only closure={extra_demo}"
        )

    expected_unmounted = {
        rel for rel, rec in records.items() if rec.reachability == "unmounted"
    }
    if set(unmounted_modules) != expected_unmounted:
        raise ContractViolationError(
            "unmounted reachability set does not match complement of live and demo closures."
        )

    return ClassificationReport(
        records=records,
        live_closure=live_closure,
        demo_only_closure=demo_only_closure,
        unmounted_modules=unmounted_modules,
    )


def verify_contracts(repo_root: Path) -> ClassificationReport:
    """Run all static repository maintenance contracts against repo_root."""
    verify_pyproject_contract(repo_root / "pyproject.toml")
    return verify_module_classification_contract(repo_root)


def _probe_python_minor_version(executable: str) -> str | None:
    if not executable or not os.path.isfile(executable) or not os.access(executable, os.X_OK):
        return None
    if os.path.abspath(executable) == os.path.abspath(sys.executable):
        return f"{sys.version_info[0]}.{sys.version_info[1]}"
    try:
        proc = subprocess.run(
            [
                executable,
                "-c",
                "import sys; sys.stdout.write(f'{sys.version_info[0]}.{sys.version_info[1]}')",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def resolve_python312(
    repo_root: Path, *, current_executable: str | Path | None = None
) -> str:
    """Resolve a Python 3.12 interpreter, preferring <repo_root>/.venv/bin/python on fallback."""
    candidate_current = (
        os.path.abspath(str(current_executable))
        if current_executable is not None
        else os.path.abspath(sys.executable)
    )
    current_ver = _probe_python_minor_version(candidate_current)
    if current_ver == "3.12":
        return candidate_current

    venv_python = repo_root / ".venv" / "bin" / "python"
    venv_candidate = os.path.abspath(str(venv_python))
    venv_ver = _probe_python_minor_version(venv_candidate)
    if venv_ver == "3.12":
        return venv_candidate

    raise EnvironmentContractError(
        f"Python 3.12 is required for local checks (current={candidate_current!r} "
        f"[{current_ver or 'unavailable'}], venv={venv_candidate!r} "
        f"[{venv_ver or 'unavailable'}]). Create a Python 3.12 virtualenv at "
        f"<repo_root>/.venv or invoke scripts/run_local_checks.py with Python 3.12."
    )


def build_pristine_child_env(
    repo_root: Path,
    *,
    python_executable: str | Path | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct a minimal allowlisted environment for child ruff/pytest processes."""
    source_env = os.environ if base_env is None else base_env
    clean_env: dict[str, str] = {}

    for key in ALLOWED_SYSTEM_ENV_KEYS:
        if key == "PATH":
            continue
        val = source_env.get(key)
        if isinstance(val, str) and val and "\x00" not in val:
            clean_env[key] = val

    path_entries: list[str] = []
    seen_paths: set[str] = set()

    def add_path_entry(entry: str) -> None:
        if entry and "\x00" not in entry and entry not in seen_paths:
            seen_paths.add(entry)
            path_entries.append(entry)

    venv_bin = repo_root / ".venv" / "bin"
    if venv_bin.exists() and not venv_bin.is_symlink() and venv_bin.is_dir():
        add_path_entry(os.path.abspath(str(venv_bin)))

    if python_executable is not None:
        py_bin_dir = Path(os.path.abspath(str(python_executable))).parent
        if py_bin_dir.is_dir():
            add_path_entry(str(py_bin_dir))

    raw_path = source_env.get("PATH", "")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raw_path = os.defpath
    for seg in raw_path.split(os.pathsep):
        add_path_entry(seg.strip())

    clean_env["PATH"] = os.pathsep.join(path_entries) if path_entries else "/usr/bin:/bin"
    clean_env["KEVIN_DISABLE_DOTENV"] = "1"
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
    clean_env["PYTHONNOUSERSITE"] = "1"
    return clean_env


def probe_os_network_sandbox(
    *,
    require_os_sandbox: bool = False,
    platform_name: str | None = None,
    sandbox_bin: Path = MACOS_SANDBOX_EXEC_BIN,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    status_stream: TextIO | None = None,
) -> SandboxResolution:
    """Probe macOS sandbox-exec network denial and degrade cleanly when nested or on Linux."""
    stream = sys.stderr if status_stream is None else status_stream
    plat = sys.platform if platform_name is None else platform_name

    if plat != "darwin":
        msg = (
            f"[network-guard] OS sandbox-exec unavailable on platform {plat!r}; "
            "using Python pre-collection socket guard."
        )
        if require_os_sandbox:
            raise SandboxUnavailableError(
                f"--require-os-sandbox failed: sandbox-exec unsupported on {plat!r}"
            )
        stream.write(msg + "\n")
        return SandboxResolution(
            prefix=(),
            mode="python-socket-guard-only",
            reason=f"platform={plat}",
        )

    if not sandbox_bin.is_file() or not os.access(sandbox_bin, os.X_OK):
        if require_os_sandbox:
            raise SandboxUnavailableError(
                f"--require-os-sandbox failed: missing executable {sandbox_bin}"
            )
        stream.write(
            f"[network-guard] {sandbox_bin} not present; using Python pre-collection socket guard.\n"
        )
        return SandboxResolution(
            prefix=(),
            mode="python-socket-guard-only",
            reason="sandbox-exec-missing",
        )

    try:
        probe = runner(
            [str(sandbox_bin), "-p", MACOS_SANDBOX_PROFILE, "/usr/bin/true"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        if require_os_sandbox:
            raise SandboxUnavailableError(
                f"--require-os-sandbox failed executing {sandbox_bin}: {exc}"
            ) from exc
        stream.write(
            f"[network-guard] sandbox-exec probe raised {exc}; falling back to Python socket guard.\n"
        )
        return SandboxResolution(
            prefix=(),
            mode="python-socket-guard-fallback",
            reason=str(exc),
        )

    if probe.returncode == 0:
        stream.write(
            "[network-guard] macOS sandbox-exec network denial + Python socket guard active.\n"
        )
        return SandboxResolution(
            prefix=(str(sandbox_bin), "-p", MACOS_SANDBOX_PROFILE),
            mode="sandbox-exec+python-socket-guard",
            reason="ok",
        )

    stderr_detail = (probe.stderr or "").strip() or f"returncode={probe.returncode}"
    if require_os_sandbox:
        raise SandboxUnavailableError(
            f"--require-os-sandbox failed: sandbox-exec probe rejected ({stderr_detail})"
        )
    stream.write(
        f"[network-guard] macOS sandbox-exec unavailable ({stderr_detail}); "
        "falling back to Python pre-collection socket guard.\n"
    )
    return SandboxResolution(
        prefix=(),
        mode="python-socket-guard-fallback",
        reason=stderr_detail,
    )


def validate_target_test_paths(
    repo_root: Path, raw_targets: Sequence[str]
) -> list[str]:
    """Validate positional pytest target paths for --mode fast."""
    tests_root = repo_root / "tests"
    if not tests_root.exists() or tests_root.is_symlink() or not tests_root.is_dir():
        raise EnvironmentContractError(
            f"Valid non-symlink tests/ directory required under {repo_root}"
        )
    resolved_repo = repo_root.resolve()
    resolved_tests = tests_root.resolve()

    validated: list[str] = []
    for raw in raw_targets:
        if not isinstance(raw, str) or not raw.strip():
            raise EnvironmentContractError("Target test path cannot be empty.")
        if "\\" in raw or any(ord(c) in _CONTROL_CHARS for c in raw):
            raise EnvironmentContractError(
                f"Invalid characters in target test path: {raw!r}"
            )
        if raw.startswith("-"):
            raise EnvironmentContractError(
                f"Option-like positional target rejected: {raw!r}"
            )

        path_part, sep, node_part = raw.partition("::")
        if sep and not node_part.strip():
            raise EnvironmentContractError(
                f"Empty pytest node selector in target: {raw!r}"
            )

        p = Path(path_part)
        if ".." in p.parts:
            raise EnvironmentContractError(
                f"Directory traversal ('..') is not permitted in target: {raw!r}"
            )

        candidate = p if p.is_absolute() else (repo_root / p)
        if not candidate.exists():
            raise EnvironmentContractError(
                f"Target test path does not exist: {raw!r}"
            )
        if candidate.is_symlink() or os.path.islink(candidate):
            raise EnvironmentContractError(
                f"Symlink target test path rejected: {raw!r}"
            )

        resolved_cand = candidate.resolve()
        try:
            resolved_cand.relative_to(resolved_tests)
            rel_to_repo = resolved_cand.relative_to(resolved_repo).as_posix()
        except ValueError as exc:
            raise EnvironmentContractError(
                f"Target test path must reside inside {tests_root}: {raw!r}"
            ) from exc

        # Walk intermediate segments inside tests/ to reject symlinked parent dirs.
        cur = tests_root
        for part in resolved_cand.relative_to(resolved_tests).parts:
            cur = cur / part
            if cur.is_symlink() or os.path.islink(cur):
                raise EnvironmentContractError(
                    f"Symlink segment in target test path rejected: {raw!r}"
                )

        validated.append(f"{rel_to_repo}::{node_part}" if sep else rel_to_repo)

    return validated


def verify_test_partition_contract(
    repo_root: Path, *, total_shards: int = DEFAULT_TOTAL_SHARDS
) -> dict[int, list[str]]:
    """Verify full 7-shard partition invariants via scripts.partition_tests."""
    tests_dir = repo_root / "tests"
    try:
        discovered = discover_test_files(tests_dir)
    except PartitionError as exc:
        raise ContractViolationError(
            f"Test discovery failed under {tests_dir}: {exc}"
        ) from exc

    resolved_repo = repo_root.resolve()
    shard_map: dict[int, list[str]] = {}
    seen_union: set[str] = set()

    for shard_idx in range(1, total_shards + 1):
        try:
            selected = partition_test_files(
                discovered, shard=shard_idx, total_shards=total_shards
            )
            rendered = render_test_paths(selected, root=tests_dir)
        except PartitionError as exc:
            raise ContractViolationError(
                f"Shard {shard_idx}/{total_shards} partition contract failed: {exc}"
            ) from exc

        if not selected or not rendered:
            raise ContractViolationError(
                f"Shard {shard_idx}/{total_shards} produced zero test files."
            )

        repo_rel_paths: list[str] = []
        for raw_rendered in rendered:
            cand = Path(raw_rendered)
            abs_cand = cand.resolve() if cand.is_absolute() else (Path.cwd() / cand).resolve()
            try:
                rel_posix = abs_cand.relative_to(resolved_repo).as_posix()
            except ValueError as exc:
                raise ContractViolationError(
                    f"Rendered shard test path escapes repo root: {raw_rendered!r}"
                ) from exc
            if rel_posix in seen_union:
                raise ContractViolationError(
                    f"Duplicate test path across shards: {rel_posix!r}"
                )
            seen_union.add(rel_posix)
            repo_rel_paths.append(rel_posix)

        shard_map[shard_idx] = repo_rel_paths

    expected_repo_rel = {f"tests/{rel}" for rel in discovered}
    if seen_union != expected_repo_rel:
        raise ContractViolationError(
            "7-shard union does not match discovered tests under tests/."
        )

    return shard_map


def _resolve_required_binary(name: str, child_env: Mapping[str, str]) -> str:
    found = shutil.which(name, path=child_env.get("PATH"))
    if not found:
        raise EnvironmentContractError(
            f"Required tool {name!r} was not found on allowlisted PATH ({child_env.get('PATH', '')!r})."
        )
    return found


def _run_checked_subprocess(
    cmd: Sequence[str], *, cwd: Path, env: Mapping[str, str]
) -> int:
    proc = subprocess.run(list(cmd), cwd=str(cwd), env=dict(env), check=False)
    return proc.returncode


def _run_pytest_with_network_guard(
    *,
    repo_root: Path,
    python_bin: str,
    child_env: Mapping[str, str],
    sandbox_prefix: Sequence[str],
    pytest_args: Sequence[str],
    targets: Sequence[str],
) -> int:
    resolved_repo = repo_root.resolve()
    plugin_dir = Path(tempfile.mkdtemp(prefix="kevin-local-checks-"))
    try:
        if plugin_dir.resolve().is_relative_to(resolved_repo):
            raise EnvironmentContractError(
                "Ephemeral pytest plugin directory must not reside inside repo_root."
            )
        plugin_file = plugin_dir / f"{_NETWORK_GUARD_MODULE_NAME}.py"
        plugin_file.write_text(_NETWORK_GUARD_PLUGIN_SOURCE, encoding="utf-8")

        pytest_env = dict(child_env)
        pytest_env["PYTHONPATH"] = f"{resolved_repo}{os.pathsep}{plugin_dir}"

        cmd = [
            *sandbox_prefix,
            python_bin,
            "-m",
            "pytest",
            *pytest_args,
            "-p",
            "no:cacheprovider",
            "-p",
            _NETWORK_GUARD_MODULE_NAME,
            "--",
            *targets,
        ]
        return _run_checked_subprocess(cmd, cwd=resolved_repo, env=pytest_env)
    finally:
        shutil.rmtree(plugin_dir, ignore_errors=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline contract verifier and hermetic local check runner for Kevin.",
        exit_on_error=False,
    )
    parser.add_argument(
        "--repo-root",
        default=str(_SCRIPT_REPO_ROOT),
        help="Repository root directory (default: parent of scripts/).",
    )
    parser.add_argument(
        "--verify-contracts-only",
        action="store_true",
        help="Verify pyproject.toml and app/ classification contracts without running ruff/pytest.",
    )
    parser.add_argument(
        "--mode",
        choices=("fast", "ci-parity"),
        default="fast",
        help="Execution mode: 'fast' (default) or 'ci-parity'.",
    )
    parser.add_argument(
        "--shard",
        type=int,
        default=None,
        help=f"Optional 1-based shard index (1..{DEFAULT_TOTAL_SHARDS}) for --mode ci-parity.",
    )
    parser.add_argument(
        "--require-os-sandbox",
        action="store_true",
        help="Fail closed if macOS sandbox-exec network denial is unavailable.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Optional target test paths under tests/ (only valid with --mode fast).",
    )

    try:
        opts = parser.parse_args(list(argv) if argv is not None else None)
    except (argparse.ArgumentError, SystemExit) as exc:
        raise EnvironmentContractError(f"CLI argument error: {exc}") from exc

    if opts.verify_contracts_only:
        if opts.shard is not None:
            raise EnvironmentContractError(
                "--shard cannot be combined with --verify-contracts-only."
            )
        if opts.targets:
            raise EnvironmentContractError(
                "Positional test targets cannot be combined with --verify-contracts-only."
            )

    if opts.shard is not None:
        if opts.mode != "ci-parity":
            raise EnvironmentContractError(
                "--shard is only valid with --mode ci-parity."
            )
        if opts.shard < 1 or opts.shard > DEFAULT_TOTAL_SHARDS:
            raise EnvironmentContractError(
                f"--shard must be between 1 and {DEFAULT_TOTAL_SHARDS}, got {opts.shard}."
            )

    if opts.mode == "ci-parity" and opts.targets:
        raise EnvironmentContractError(
            "Positional test targets are not permitted with --mode ci-parity."
        )

    return opts


def _validate_repo_root(raw_repo_root: str) -> Path:
    if not raw_repo_root or "\\" in raw_repo_root or any(
        ord(c) in _CONTROL_CHARS for c in raw_repo_root
    ):
        raise EnvironmentContractError(f"Invalid --repo-root: {raw_repo_root!r}")
    root_p = Path(raw_repo_root)
    if not root_p.exists():
        raise EnvironmentContractError(f"Repository root does not exist: {raw_repo_root}")
    if root_p.is_symlink() or os.path.islink(root_p):
        raise EnvironmentContractError(
            f"Repository root cannot be a symlink: {raw_repo_root}"
        )
    if not root_p.is_dir():
        raise EnvironmentContractError(
            f"Repository root must be a directory: {raw_repo_root}"
        )
    return root_p.resolve()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        opts = parse_args(argv)
        repo_root = _validate_repo_root(opts.repo_root)

        report = verify_contracts(repo_root)
        if opts.verify_contracts_only:
            if opts.require_os_sandbox:
                probe_os_network_sandbox(require_os_sandbox=True)
            sys.stdout.write(
                f"[contracts-ok] Verified pyproject.toml and {len(report.records)} "
                f"app/ modules (live={len(report.live_closure)}, "
                f"demo={len(report.demo_only_closure)}, "
                f"unmounted={len(report.unmounted_modules)}).\n"
            )
            sys.stdout.flush()
            return 0

        python_bin = resolve_python312(repo_root)
        child_env = build_pristine_child_env(
            repo_root, python_executable=python_bin
        )
        sandbox = probe_os_network_sandbox(
            require_os_sandbox=opts.require_os_sandbox
        )
        ruff_bin = _resolve_required_binary("ruff", child_env)

        if opts.mode == "fast":
            validated_targets = (
                validate_target_test_paths(repo_root, opts.targets)
                if opts.targets
                else []
            )
            rc = _run_checked_subprocess(
                [
                    *sandbox.prefix,
                    ruff_bin,
                    "check",
                    "--no-cache",
                    "--select",
                    RUFF_FATAL_SELECTORS,
                    ".",
                ],
                cwd=repo_root,
                env=child_env,
            )
            if rc != 0:
                return rc

            if validated_targets:
                rc = _run_pytest_with_network_guard(
                    repo_root=repo_root,
                    python_bin=python_bin,
                    child_env=child_env,
                    sandbox_prefix=sandbox.prefix,
                    pytest_args=("-q",),
                    targets=validated_targets,
                )
                if rc != 0:
                    return rc
            return 0

        # --mode ci-parity
        for required_dir_name in ("app", "scripts", "tests"):
            req_dir = repo_root / required_dir_name
            if not req_dir.exists() or req_dir.is_symlink() or not req_dir.is_dir():
                raise ContractViolationError(
                    f"Required non-symlink directory {required_dir_name}/ missing under {repo_root}"
                )

        shard_map = verify_test_partition_contract(
            repo_root, total_shards=DEFAULT_TOTAL_SHARDS
        )
        git_bin = _resolve_required_binary("git", child_env)

        rc = _run_checked_subprocess(
            [
                *sandbox.prefix,
                ruff_bin,
                "check",
                "--no-cache",
                "--select",
                RUFF_FATAL_SELECTORS,
                ".",
            ],
            cwd=repo_root,
            env=child_env,
        )
        if rc != 0:
            return rc

        rc = _run_checked_subprocess(
            [
                *sandbox.prefix,
                python_bin,
                "-m",
                "compileall",
                "-q",
                "app",
                "scripts",
                "tests",
            ],
            cwd=repo_root,
            env=child_env,
        )
        if rc != 0:
            return rc

        rc = _run_checked_subprocess(
            [*sandbox.prefix, git_bin, "diff", "--check"],
            cwd=repo_root,
            env=child_env,
        )
        if rc != 0:
            return rc

        shards_to_run = (
            [opts.shard]
            if opts.shard is not None
            else list(range(1, DEFAULT_TOTAL_SHARDS + 1))
        )
        for shard_idx in shards_to_run:
            rc = _run_pytest_with_network_guard(
                repo_root=repo_root,
                python_bin=python_bin,
                child_env=child_env,
                sandbox_prefix=sandbox.prefix,
                pytest_args=("--tb=short", "-q"),
                targets=shard_map[shard_idx],
            )
            if rc != 0:
                return rc

        return 0
    except LocalChecksError as err:
        sys.stderr.write(f"run_local_checks error: {err}\n")
        sys.stderr.flush()
        return 2
    except Exception as err:
        sys.stderr.write(f"run_local_checks unexpected error: {err}\n")
        sys.stderr.flush()
        return 2


if __name__ == "__main__":
    sys.exit(main())
