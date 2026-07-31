"""Trusted static gate for the synthetic-preparation verifier before import.

The caller supplies candidate bytes. This module never imports or executes the
candidate verifier, reads a file, resolves configuration, or starts a process.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
from typing import Literal


VERIFIER_SOURCE_SHA256 = "f0c16aeb37fad59757f9b589275a483fbe724b67597f7ddc12ed0e18499b0487"
_SAFE_IMPORT_ROOTS = {
    "__future__",
    "collections",
    "dataclasses",
    "hashlib",
    "json",
    "re",
    "typing",
}
_BLOCKED_IMPORT_ROOTS = {
    "asyncio",
    "boto3",
    "ctypes",
    "dotenv",
    "firebase_admin",
    "google",
    "http",
    "importlib",
    "marshal",
    "os",
    "pickle",
    "requests",
    "socket",
    "subprocess",
    "twilio",
    "urllib",
    "websocket",
    "websockets",
    "yaml",
}
_SAFE_CALL_NAMES = {
    "PackageDiagnostic",
    "PackageValidation",
    "_canonical_json",
    "_diagnostic",
    "_has_exact_keys",
    "_is_hex",
    "_parse_manifest",
    "_sha256",
    "_source_diagnostics",
    "_validate_artifacts",
    "_validate_candidate_changes",
    "_validate_schema",
    "all",
    "any",
    "dataclass",
    "enumerate",
    "isinstance",
    "len",
    "set",
    "sorted",
    "type",
    "tuple",
    "zip",
}
_SAFE_ATTRIBUTE_CALLS = {
    ("data", "decode"),
    ("artifacts", "items"),
    ("digest", "hexdigest"),
    ("encoded", "encode"),
    ("errors", "append"),
    ("errors", "extend"),
    ("hashlib", "sha256"),
    ("json", "dumps"),
    ("json", "loads"),
    ("paths", "append"),
    ("pattern", "search"),
    ("raw", "decode"),
    ("re", "compile"),
    ("text", "splitlines"),
    ("value", "update"),
}
_DYNAMIC_NAMES = {
    "__builtins__",
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "setattr",
    "vars",
}


@dataclass(frozen=True, slots=True)
class StaticDiagnostic:
    category: str
    location: str


@dataclass(frozen=True, slots=True)
class StaticInspection:
    status: Literal["invalid_local_package", "not_authorized"]
    diagnostics: tuple[StaticDiagnostic, ...]


def _result(category: str, location: str) -> StaticInspection:
    return StaticInspection(
        status="invalid_local_package",
        diagnostics=(StaticDiagnostic(category=category, location=location),),
    )


def _attribute_receiver(value: ast.expr) -> str | None:
    if isinstance(value, ast.Name):
        return value.id
    return None


def _assignment_names(target: ast.expr) -> tuple[str, ...] | None:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, ast.Tuple | ast.List):
        names: list[str] = []
        for item in target.elts:
            item_names = _assignment_names(item)
            if item_names is None:
                return None
            names.extend(item_names)
        return tuple(names)
    return None


def _inspect_structure(*, source: bytes, location: str) -> StaticInspection:
    """Inspect untrusted syntax without importing or executing its module."""
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text, filename=location)
    except (UnicodeDecodeError, SyntaxError):
        return _result("static_source_encoding", location)
    imported_bindings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_bindings.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_bindings.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _BLOCKED_IMPORT_ROOTS or root not in _SAFE_IMPORT_ROOTS:
                    return _result("static_import", location)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in _BLOCKED_IMPORT_ROOTS or root not in _SAFE_IMPORT_ROOTS:
                return _result("static_import", location)
        elif isinstance(node, ast.Name) and node.id in _DYNAMIC_NAMES:
            return _result("static_dynamic_code", location)
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return _result("static_dynamic_code", location)
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                names = _assignment_names(target)
                if names is None:
                    return _result("static_assignment", location)
                if any(name in imported_bindings for name in names):
                    return _result("static_rebinding", location)
        elif isinstance(node, ast.Delete):
            return _result("static_assignment", location)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in _SAFE_CALL_NAMES:
                    return _result("static_call", location)
            elif isinstance(node.func, ast.Attribute):
                receiver = _attribute_receiver(node.func.value)
                if (receiver, node.func.attr) not in _SAFE_ATTRIBUTE_CALLS:
                    return _result("static_call", location)
            else:
                return _result("static_dynamic_code", location)
    return StaticInspection(status="not_authorized", diagnostics=())


def inspect_verifier_source(*, source: bytes, location: str) -> StaticInspection:
    """Pin and inspect verifier bytes before any caller imports the module."""
    if type(source) is not bytes:
        return _result("static_source_input_type", location)
    if hashlib.sha256(source).hexdigest() != VERIFIER_SOURCE_SHA256:
        return _result("static_source_digest", location)
    return _inspect_structure(source=source, location=location)


__all__ = [
    "StaticDiagnostic",
    "StaticInspection",
    "VERIFIER_SOURCE_SHA256",
    "inspect_verifier_source",
]
