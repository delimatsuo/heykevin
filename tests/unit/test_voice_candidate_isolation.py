"""AST isolation checks for the offline-only candidate adapter package."""

import ast
from pathlib import Path


_CANDIDATE_PATHS = tuple(
    Path("app/services/voice_candidates") / name
    for name in (
        "__init__.py",
        "native_gemini.py",
        "chained_streaming.py",
        "conversation_relay.py",
        "manual_native.py",
    )
)
_FORBIDDEN = (
    "google",
    "twilio",
    "deepgram",
    "elevenlabs",
    "socket",
    "requests",
    "httpx",
    "websockets",
    "subprocess",
    "app.main",
    "app.webhooks",
    "app.services.gemini_pipeline",
    "app.services.voice_pipeline",
)


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _imported_modules(source: str, *, module_name: str) -> set[str]:
    tree = ast.parse(source)
    package = module_name.split(".")[:-1]
    imported: set[str] = set()
    importlib_names = {"importlib"}
    import_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_names.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                base_parts = package[: max(keep, 0)]
                if node.module:
                    base_parts.extend(node.module.split("."))
                base = ".".join(base_parts)
            else:
                base = node.module or ""
            if base:
                imported.add(base)
            for alias in node.names:
                if alias.name != "*":
                    imported.add(".".join(part for part in (base, alias.name) if part))
        elif isinstance(node, ast.Call) and node.args:
            target = _literal_string(node.args[0])
            if target is None:
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                imported.add(target)
            elif (
                isinstance(node.func, ast.Name)
                and node.func.id in import_module_names
            ):
                imported.add(target)
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_names
            ):
                imported.add(target)
    return imported


def _is_forbidden(module: str) -> bool:
    return any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for forbidden in _FORBIDDEN
    )


def test_ast_import_detector_covers_from_relative_and_dynamic_forms():
    modules = _imported_modules(
        """
import google.genai
from google import genai
from ...webhooks import media_stream
import importlib as il
from importlib import import_module as load_module
il.import_module("app.services.voice_pipeline")
load_module("deepgram")
__import__("twilio.rest")
""",
        module_name="app.services.voice_candidates.fixture",
    )
    assert {
        "google.genai",
        "google",
        "app.webhooks",
        "app.webhooks.media_stream",
        "app.services.voice_pipeline",
        "deepgram",
        "twilio.rest",
    } <= modules
    assert all(
        _is_forbidden(module)
        for module in {
            "google.genai",
            "app.webhooks.media_stream",
            "app.services.voice_pipeline",
            "deepgram",
            "twilio.rest",
        }
    )


def test_candidate_package_has_no_provider_network_or_live_route_imports():
    violations: dict[str, list[str]] = {}
    for path in _CANDIDATE_PATHS:
        module_name = ".".join(path.with_suffix("").parts)
        modules = _imported_modules(
            path.read_text(encoding="utf-8"),
            module_name=module_name,
        )
        forbidden = sorted(module for module in modules if _is_forbidden(module))
        if forbidden:
            violations[str(path)] = forbidden
    assert violations == {}
