#!/usr/bin/env python3
"""PreToolUse hook guard to deny git force-push and destructive rm commands.

Exposes contains_forced_git_push(command: str) -> bool, contains_forbidden_rm(command: str) -> bool,
and a CLI entrypoint conforming to Claude PreToolUse hook protocol.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Callable

COMMAND_SEPARATORS = {
    ";",
    "\n",
    "&&",
    "||",
    "|",
    "|&",
    "&",
    ";;",
    ";&",
    ";;&",
    "(",
    ")",
    "{",
    "}",
}

SHELL_KEYWORDS = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "do",
    "done",
    "while",
    "until",
    "for",
    "in",
    "select",
    "case",
    "esac",
    "!",
    "{",
    "}",
}

GIT_GLOBAL_OPTS_WITH_ARG = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--super-prefix",
    "--config-env",
    "--exec-path",
}

SHELL_BINARIES = {
    "sh",
    "bash",
    "zsh",
    "ksh",
    "dash",
    "fish",
}

XARGS_REQ_SHORT_OPTS = {
    "a",
    "d",
    "E",
    "I",
    "J",
    "L",
    "n",
    "P",
    "R",
    "S",
    "s",
}

XARGS_OPT_SHORT_OPTS = {
    "e",
    "i",
    "l",
}

XARGS_REQ_LONG_OPTS = {
    "--arg-file",
    "--delimiter",
    "--max-args",
    "--max-procs",
    "--max-chars",
    "--process-slot-var",
}

XARGS_OPT_LONG_OPTS = {
    "--eof",
    "--replace",
    "--max-lines",
}

XARGS_INPUT_SENTINEL = "$XARGS_INPUT"

FLOCK_NO_ARG_SHORT_OPTS = {"s", "e", "x", "n", "o", "F", "u"}
FLOCK_REQ_ARG_SHORT_OPTS = {"w", "E"}
FLOCK_TERMINAL_SHORT_OPTS = {"h", "V"}

FLOCK_NO_ARG_LONG_OPTS = {
    "--shared",
    "--exclusive",
    "--unlock",
    "--nonblock",
    "--nonblocking",
    "--nb",
    "--close",
    "--no-fork",
    "--verbose",
    "--fcntl",
}

FLOCK_REQ_ARG_LONG_OPTS = {
    "--timeout",
    "--wait",
    "--conflict-exit-code",
    "--start",
    "--length",
    "--fd",
}

FLOCK_TERMINAL_LONG_OPTS = {
    "--help",
    "--version",
}


FIND_EXEC_ACTIONS = {
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
}

FIND_INPUT_SENTINEL = "$FIND_INPUT"

_LITERAL_SEMICOLON_SENTINEL = "__KEVIN_HOOK_LITERAL_SEMICOLON_SENTINEL_PR212__"
_LITERAL_OPEN_PAREN_SENTINEL = "__KEVIN_HOOK_LITERAL_OPEN_PAREN_SENTINEL_PR212__"
_LITERAL_CLOSE_PAREN_SENTINEL = "__KEVIN_HOOK_LITERAL_CLOSE_PAREN_SENTINEL_PR212__"
_SUBST_SENTINEL_PREFIX = "__KEVIN_HOOK_SUBST_SENTINEL_"


def _restore_sentinels(token: str) -> str:
    """Restore sentinel replacements back to literal characters."""
    return (
        token.replace(_LITERAL_SEMICOLON_SENTINEL, ";")
        .replace(_LITERAL_OPEN_PAREN_SENTINEL, "(")
        .replace(_LITERAL_CLOSE_PAREN_SENTINEL, ")")
    )


def _extract_find_actions(tokens: list[str]) -> list[list[str]]:
    """Extract command actions from find arguments.

    Recognizes find execution actions (-exec, -execdir, -ok, -okdir).
    An action command starts immediately after the action flag and ends at '+', ';',
    or the end of the current token segment.

    Occurrences of '{}' in action arguments are replaced with FIND_INPUT_SENTINEL.
    Returns a list of command token lists.
    """
    actions: list[list[str]] = []
    i = 1
    n = len(tokens)

    while i < n:
        tok = tokens[i]
        if tok in FIND_EXEC_ACTIONS:
            i += 1
            cmd_tokens: list[str] = []
            while i < n and tokens[i] not in {"+", ";", _LITERAL_SEMICOLON_SENTINEL}:
                cmd_tokens.append(tokens[i])
                i += 1
            if i < n and tokens[i] in {"+", ";", _LITERAL_SEMICOLON_SENTINEL}:
                i += 1
            if cmd_tokens:
                replaced = [
                    _restore_sentinels(t).replace("{}", FIND_INPUT_SENTINEL)
                    for t in cmd_tokens
                ]
                actions.append(replaced)
        else:
            i += 1

    return actions


def _extract_initial_backtick_args(tokens: list[str]) -> list[str] | None:
    """If tokens starts with a literal backtick, find matching closing backtick and return trailing arguments.

    If token zero is not a literal backtick, returns None.
    If token zero is a literal backtick but no matching closing backtick exists, raises ValueError.
    """
    if not tokens or tokens[0] != "`":
        return None

    for idx in range(1, len(tokens)):
        if tokens[idx] == "`":
            return [_restore_sentinels(t) for t in tokens[idx + 1 :]]

    raise ValueError("Unmatched opening backtick in command substitution")


def _is_all_parens(token: str) -> bool:
    """Return True if token consists solely of parenthesis characters ('(' or ')')."""
    return bool(token) and all(c in "()" for c in token)


def _extract_initial_dynamic_args(tokens: list[str]) -> list[str] | None:
    """If tokens start with a dynamic executable prefix, return trailing arguments.

    Recognizes:
    - substitution sentinels: __KEVIN_HOOK_SUBST_SENTINEL_...
    - literal backtick: `...`
    - dollar + identifier token: $GIT, $RM
    - dollar + braced identifier token: ${GIT}, ${RM}
    - dollar + open parenthesis: $(...) command substitution

    If token zero is not a dynamic executable prefix, returns None.
    If the prefix is malformed or unmatched, raises ValueError.
    """
    if not tokens:
        return None

    if _SUBST_SENTINEL_PREFIX in tokens[0]:
        return [_restore_sentinels(t) for t in tokens[1:]]

    if tokens[0] == "`":
        for idx in range(1, len(tokens)):
            if tokens[idx] == "`":
                return [_restore_sentinels(t) for t in tokens[idx + 1 :]]
        raise ValueError("Unmatched opening backtick in command substitution")

    if tokens[0] == "$":
        if len(tokens) < 2:
            raise ValueError("Incomplete variable expansion '$'")
        next_tok = tokens[1]
        if _is_all_parens(next_tok) and next_tok.startswith("("):
            paren_depth = 0
            for char in next_tok:
                if char == "(":
                    paren_depth += 1
                elif char == ")":
                    paren_depth -= 1
            if paren_depth <= 0:
                raise ValueError(
                    f"Malformed command substitution: '${next_tok}'"
                )
            idx = 2
            while idx < len(tokens):
                tok = tokens[idx]
                if _is_all_parens(tok):
                    closed_idx = None
                    for p_idx, char in enumerate(tok):
                        if char == "(":
                            paren_depth += 1
                        elif char == ")":
                            paren_depth -= 1
                            if paren_depth == 0:
                                closed_idx = p_idx
                                break
                    if closed_idx is not None:
                        remainder = tok[closed_idx + 1 :]
                        if remainder:
                            raw_trailing = [remainder] + tokens[idx + 1 :]
                        else:
                            raw_trailing = tokens[idx + 1 :]
                        return [_restore_sentinels(t) for t in raw_trailing]
                idx += 1
            raise ValueError("Unmatched opening parenthesis in command substitution")
        if next_tok.startswith("{"):
            if not next_tok.endswith("}"):
                raise ValueError(
                    f"Unmatched opening brace in variable expansion: '${next_tok}'"
                )
            var_name = next_tok[1:-1]
            if not var_name.isidentifier():
                raise ValueError(
                    f"Malformed variable name in variable expansion: '${next_tok}'"
                )
            return [_restore_sentinels(t) for t in tokens[2:]]
        if next_tok.isidentifier():
            return [_restore_sentinels(t) for t in tokens[2:]]
        raise ValueError(f"Malformed variable expansion: '${next_tok}'")

    return None


def _reconstruct_git_args(git_args: list[str]) -> list[str]:
    """Reconstruct git argument tokens where literal colons split config values.

    Narrowly repairs Git config values (-c and --config-env) when a literal colon
    token directly follows a config value token.
    """
    reconstructed: list[str] = []
    i = 0
    n = len(git_args)
    while i < n:
        arg = git_args[i]
        if arg == "--":
            reconstructed.extend(git_args[i:])
            break

        if arg == "-c":
            reconstructed.append(arg)
            i += 1
            if i < n:
                val = git_args[i]
                if _has_shell_expansion(val):
                    raise ValueError(
                        f"Dynamic git config entry is not supported: {val!r}"
                    )
                if i + 1 < n and git_args[i + 1] == "$":
                    raise ValueError(
                        f"Dynamic git config entry is not supported: {val!r}"
                    )
                i += 1
                while i + 1 < n and git_args[i] == ":":
                    val = val + ":" + git_args[i + 1]
                    i += 2
                reconstructed.append(val)
            continue

        if arg.startswith("-c") and not arg.startswith("-C"):
            val = arg[2:]
            if _has_shell_expansion(val):
                raise ValueError(
                    f"Dynamic git config entry is not supported: {val!r}"
                )
            if not val and i + 1 < n and _has_shell_expansion(git_args[i + 1]):
                raise ValueError(
                    f"Dynamic git config entry is not supported: {git_args[i + 1]!r}"
                )
            if i + 1 < n and git_args[i + 1] == "$":
                raise ValueError(
                    f"Dynamic git config entry is not supported: {val!r}"
                )
            val = arg
            i += 1
            while i + 1 < n and git_args[i] == ":":
                val = val + ":" + git_args[i + 1]
                i += 2
            reconstructed.append(val)
            continue

        if arg == "--config-env":
            reconstructed.append(arg)
            i += 1
            if i < n:
                val = git_args[i]
                if _has_shell_expansion(val):
                    raise ValueError(
                        f"Dynamic git config entry is not supported: {val!r}"
                    )
                if i + 1 < n and git_args[i + 1] == "$":
                    raise ValueError(
                        f"Dynamic git config entry is not supported: {val!r}"
                    )
                i += 1
                while i + 1 < n and git_args[i] == ":":
                    val = val + ":" + git_args[i + 1]
                    i += 2
                reconstructed.append(val)
            continue

        if arg.startswith("--config-env="):
            val = arg[len("--config-env=") :]
            if _has_shell_expansion(val):
                raise ValueError(
                    f"Dynamic git config entry is not supported: {val!r}"
                )
            if not val and i + 1 < n and _has_shell_expansion(git_args[i + 1]):
                raise ValueError(
                    f"Dynamic git config entry is not supported: {git_args[i + 1]!r}"
                )
            if i + 1 < n and git_args[i + 1] == "$":
                raise ValueError(
                    f"Dynamic git config entry is not supported: {val!r}"
                )
            val = arg
            i += 1
            while i + 1 < n and git_args[i] == ":":
                val = val + ":" + git_args[i + 1]
                i += 2
            reconstructed.append(val)
            continue

        if not arg.startswith("-"):
            reconstructed.extend(git_args[i:])
            break

        if arg in GIT_GLOBAL_OPTS_WITH_ARG:
            reconstructed.append(arg)
            i += 1
            if i < n:
                reconstructed.append(git_args[i])
                i += 1
            continue

        if any(
            arg.startswith(opt + "=")
            for opt in [
                "--git-dir",
                "--work-tree",
                "--namespace",
                "--super-prefix",
                "--exec-path",
            ]
        ):
            reconstructed.append(arg)
            i += 1
            continue

        if arg.startswith("-C") and len(arg) > 2:
            reconstructed.append(arg)
            i += 1
            continue

        reconstructed.append(arg)
        i += 1

    return reconstructed


def _record_alias_config(
    alias_configs: dict[str, tuple[str, str]],
    val: str,
    kind: str,
) -> None:
    """Record an alias config entry if val starts with alias."""
    if _has_shell_expansion(val):
        raise ValueError(
            f"Dynamic git config entry is not supported: {val!r}"
        )
    if "=" in val:
        key, setting = val.split("=", 1)
        key_stripped = key.strip()
        if key_stripped.lower().startswith("alias."):
            alias_name = key_stripped[len("alias."):].lower()
            if alias_name and alias_name != "push":
                setting_stripped = setting.strip()
                if (
                    len(setting_stripped) >= 2
                    and setting_stripped.startswith("'")
                    and setting_stripped.endswith("'")
                ):
                    setting = setting_stripped[1:-1]
                else:
                    setting = setting_stripped
                alias_configs[alias_name] = (kind, setting)


def _parse_git_global_options(
    git_args: list[str],
) -> tuple[
    dict[str, tuple[str, str]],
    dict[str, tuple[str, str]],
    list[tuple[str, str, str]],
    list[str],
]:
    """Parse git global options, extracting alias configurations, mirror configs, push configs, and remaining args.

    Returns (alias_configs, mirror_configs, push_configs, remaining_tokens).
    """
    git_args = _reconstruct_git_args(git_args)
    alias_configs: dict[str, tuple[str, str]] = {}
    mirror_configs: dict[str, tuple[str, str]] = {}
    push_configs: list[tuple[str, str, str]] = []
    i = 0
    while i < len(git_args):
        arg = git_args[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-"):
            break

        if arg == "-c":
            i += 1
            if i < len(git_args):
                val = git_args[i]
                if _has_shell_expansion(val):
                    raise ValueError(
                        f"Dynamic git config entry is not supported: {val!r}"
                    )
                _record_alias_config(alias_configs, val, "c")
                _record_forcing_config(mirror_configs, push_configs, val, "c")
                i += 1
            continue

        if arg.startswith("-c") and not arg.startswith("-C"):
            val = arg[2:]
            if _has_shell_expansion(val):
                raise ValueError(
                    f"Dynamic git config entry is not supported: {val!r}"
                )
            if not val and i + 1 < len(git_args) and _has_shell_expansion(git_args[i + 1]):
                raise ValueError(
                    f"Dynamic git config entry is not supported: {git_args[i + 1]!r}"
                )
            _record_alias_config(alias_configs, val, "c")
            _record_forcing_config(mirror_configs, push_configs, val, "c")
            i += 1
            continue

        if arg == "--config-env":
            i += 1
            if i < len(git_args):
                val = git_args[i]
                if _has_shell_expansion(val):
                    raise ValueError(
                        f"Dynamic git config entry is not supported: {val!r}"
                    )
                _record_alias_config(alias_configs, val, "config-env")
                _record_forcing_config(mirror_configs, push_configs, val, "config-env")
                i += 1
            continue

        if arg.startswith("--config-env="):
            val = arg[len("--config-env="):]
            if _has_shell_expansion(val):
                raise ValueError(
                    f"Dynamic git config entry is not supported: {val!r}"
                )
            if not val and i + 1 < len(git_args) and _has_shell_expansion(git_args[i + 1]):
                raise ValueError(
                    f"Dynamic git config entry is not supported: {git_args[i + 1]!r}"
                )
            _record_alias_config(alias_configs, val, "config-env")
            _record_forcing_config(mirror_configs, push_configs, val, "config-env")
            i += 1
            continue

        if arg in GIT_GLOBAL_OPTS_WITH_ARG:
            i += 2
            continue

        if any(
            arg.startswith(opt + "=")
            for opt in [
                "--git-dir",
                "--work-tree",
                "--namespace",
                "--super-prefix",
                "--exec-path",
            ]
        ):
            i += 1
            continue

        if arg.startswith("-C") and len(arg) > 2:
            i += 1
            continue

        i += 1

    return alias_configs, mirror_configs, push_configs, git_args[i:]


def _parse_git_global_configs(
    git_args: list[str],
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Parse git global options, extracting alias configurations and remaining args.

    Returns (alias_configs, remaining_tokens) where alias_configs maps lowercase alias name
    to ('c', value) or ('config-env', env_var_name).
    """
    alias_configs, _, _, remaining = _parse_git_global_options(git_args)
    return alias_configs, remaining


def _record_forcing_config(
    mirror_configs: dict[str, tuple[str, str]],
    push_configs: list[tuple[str, str, str]],
    entry: str,
    kind: str,
) -> None:
    """Record a git config entry for forcing inspection."""
    if _has_shell_expansion(entry):
        raise ValueError(
            f"Dynamic git config entry is not supported: {entry!r}"
        )
    if "=" in entry:
        key, setting = entry.split("=", 1)
        key_stripped = key.strip().lower()
        if key_stripped:
            if key_stripped == "include.path" or (
                key_stripped.startswith("includeif.")
                and key_stripped.endswith(".path")
                and len(key_stripped) > 15
            ):
                raise ValueError(
                    f"Arbitrary git config include is not supported: {key.strip()!r}"
                )
            setting_stripped = setting.strip()
            if (
                len(setting_stripped) >= 2
                and setting_stripped.startswith("'")
                and setting_stripped.endswith("'")
            ):
                setting = setting_stripped[1:-1]
            else:
                setting = setting_stripped
            if key_stripped.startswith("remote."):
                if key_stripped.endswith(".mirror") and len(key_stripped) > len("remote..mirror"):
                    mirror_configs[key_stripped] = (kind, setting)
                elif key_stripped.endswith(".push") and len(key_stripped) > len("remote..push"):
                    push_configs.append((key_stripped, kind, setting))
    else:
        key_stripped = entry.strip().lower()
        if key_stripped:
            if key_stripped == "include.path" or (
                key_stripped.startswith("includeif.")
                and key_stripped.endswith(".path")
                and len(key_stripped) > 15
            ):
                raise ValueError(
                    f"Arbitrary git config include is not supported: {entry.strip()!r}"
                )
            setting = "true" if kind == "c" else ""
            if key_stripped.startswith("remote."):
                if key_stripped.endswith(".mirror") and len(key_stripped) > len("remote..mirror"):
                    mirror_configs[key_stripped] = (kind, setting)
                elif key_stripped.endswith(".push") and len(key_stripped) > len("remote..push"):
                    push_configs.append((key_stripped, kind, setting))


def _has_forcing_git_config(git_args: list[str]) -> bool:
    """Scan leading Git global options for forcing config settings (-c and --config-env).

    Detects:
    - remote.<name>.mirror set truthy or supplied via --config-env
    - remote.<name>.push whose value begins with '+' or is supplied via --config-env

    Explicit false mirror values (false, no, off, 0) are safe.
    Does not inspect files, git config, or environment variables.
    """
    _, mirror_configs, push_configs, _ = _parse_git_global_options(git_args)
    for _key, kind, val in push_configs:
        if kind == "config-env" or val.strip().startswith("+"):
            return True

    for kind, val in mirror_configs.values():
        if kind == "config-env" or val.strip().lower() not in {"false", "no", "off", "0"}:
            return True

    return False


_scan_git_forcing_configs = _has_forcing_git_config


def _build_git_config_env(
    inherited_env: dict[str, str] | None,
    alias_configs: dict[str, tuple[str, str]],
    mirror_configs: dict[str, tuple[str, str]],
    push_configs: list[tuple[str, str, str]],
) -> dict[str, str]:
    """Build an inherited environment dictionary carrying the given Git configuration state."""
    new_env = dict(inherited_env) if inherited_env else {}
    new_env = {k: v for k, v in new_env.items() if not _is_git_config_protocol_key(k)}

    pairs: list[tuple[str, str]] = []
    for alias_name, (_kind, val) in alias_configs.items():
        pairs.append((f"alias.{alias_name}", val))
    for mirror_key, (_kind, val) in mirror_configs.items():
        pairs.append((mirror_key, val))
    for push_key, kind, val in push_configs:
        if kind == "config-env" and not val.strip().startswith("+"):
            pairs.append((push_key, f"+{val}"))
        else:
            pairs.append((push_key, val))

    if pairs:
        new_env["GIT_CONFIG_COUNT"] = str(len(pairs))
        for i, (k, v) in enumerate(pairs):
            new_env[f"GIT_CONFIG_KEY_{i}"] = k
            new_env[f"GIT_CONFIG_VALUE_{i}"] = v

    return new_env


def _inspect_git_invocation(
    git_args: list[str],
    env_alias_configs: dict[str, tuple[str, str]] | None = None,
    env_has_forcing: bool = False,
    env_mirror_configs: dict[str, tuple[str, str]] | None = None,
    env_push_configs: list[tuple[str, str, str]] | None = None,
    _depth: int = 0,
    _inherited_env: dict[str, str] | None = None,
) -> bool:
    """Inspect git command arguments (after 'git') for forced push with alias resolution."""
    cli_alias_configs, cli_mirror_configs, cli_push_configs, remaining = (
        _parse_git_global_options(git_args)
    )

    alias_configs: dict[str, tuple[str, str]] = {}
    if env_alias_configs:
        alias_configs.update(env_alias_configs)
    alias_configs.update(cli_alias_configs)

    mirror_configs: dict[str, tuple[str, str]] = {}
    if env_mirror_configs:
        mirror_configs.update(env_mirror_configs)
    mirror_configs.update(cli_mirror_configs)

    push_configs: list[tuple[str, str, str]] = []
    if env_push_configs:
        push_configs.extend(env_push_configs)
    push_configs.extend(cli_push_configs)

    if not remaining:
        return False

    visited: set[str] = set()
    depth = 0
    max_depth = 20
    current_tokens = list(remaining)

    while current_tokens:
        lead = current_tokens[0]
        lead_key = lead.lower()
        if lead_key != "push" and lead_key in alias_configs:
            if depth >= max_depth:
                raise ValueError(f"Git alias expansion depth exceeded for {lead!r}")
            if lead_key in visited:
                raise ValueError(f"Git alias cycle detected: {lead!r}")
            visited.add(lead_key)
            depth += 1
            kind, value = alias_configs[lead_key]
            if kind == "config-env":
                raise ValueError(
                    f"Git alias {lead!r} is configured via --config-env which requires forbidden environment inspection"
                )
            stripped_val = value.strip()
            if stripped_val.startswith("!"):
                shell_cmd = stripped_val[1:].strip()
                invocation_args = current_tokens[1:]
                if invocation_args:
                    shell_cmd = shell_cmd + " " + " ".join(shlex.quote(a) for a in invocation_args)
                subshell_env = _build_git_config_env(
                    _inherited_env, alias_configs, mirror_configs, push_configs
                )
                return contains_forced_git_push(
                    shell_cmd, _depth=_depth + 1, _inherited_env=subshell_env
                )

            try:
                expansion = shlex.split(value, posix=True)
            except Exception as exc:
                raise ValueError(
                    f"Failed to parse git alias value {value!r}: {exc}"
                ) from exc
            combined = expansion + current_tokens[1:]
            new_aliases, new_mirrors, new_pushes, remaining_tokens = (
                _parse_git_global_options(combined)
            )
            alias_configs.update(new_aliases)
            mirror_configs.update(new_mirrors)
            push_configs.extend(new_pushes)
            current_tokens = remaining_tokens
            continue
        break

    if not current_tokens:
        return False

    subcmd = current_tokens[0]
    if subcmd != "push":
        return False

    has_forcing_config = False
    if env_mirror_configs is None and env_push_configs is None and env_has_forcing:
        has_forcing_config = True

    for _key, kind, val in push_configs:
        if kind == "config-env" or val.strip().startswith("+"):
            has_forcing_config = True
            break

    if not has_forcing_config:
        for kind, val in mirror_configs.values():
            if kind == "config-env" or val.strip().lower() not in {"false", "no", "off", "0"}:
                has_forcing_config = True
                break

    if has_forcing_config:
        return True

    push_args = current_tokens[1:]
    return _is_forced_push_args(push_args)


def _inspect_git_invocation_for_rm(
    git_args: list[str],
    env_alias_configs: dict[str, tuple[str, str]] | None = None,
    _depth: int = 0,
    _inherited_env: dict[str, str] | None = None,
) -> bool:
    """Inspect git command arguments for shell aliases executing forbidden rm."""
    cli_alias_configs, remaining = _parse_git_global_configs(git_args)
    alias_configs: dict[str, tuple[str, str]] = {}
    if env_alias_configs:
        alias_configs.update(env_alias_configs)
    alias_configs.update(cli_alias_configs)
    if not remaining:
        return False

    visited: set[str] = set()
    depth = 0
    max_depth = 20
    current_tokens = list(remaining)

    while current_tokens:
        lead = current_tokens[0]
        lead_key = lead.lower()
        if lead_key != "push" and lead_key in alias_configs:
            if depth >= max_depth:
                raise ValueError(f"Git alias expansion depth exceeded for {lead!r}")
            if lead_key in visited:
                raise ValueError(f"Git alias cycle detected: {lead!r}")
            visited.add(lead_key)
            depth += 1
            kind, value = alias_configs[lead_key]
            if kind == "config-env":
                raise ValueError(
                    f"Git alias {lead!r} is configured via --config-env which requires forbidden environment inspection"
                )
            stripped_val = value.strip()
            if stripped_val.startswith("!"):
                shell_cmd = stripped_val[1:].strip()
                invocation_args = current_tokens[1:]
                if invocation_args:
                    shell_cmd = shell_cmd + " " + " ".join(shlex.quote(a) for a in invocation_args)
                subshell_env = _build_git_config_env(
                    _inherited_env, alias_configs, {}, []
                )
                return contains_forbidden_rm(
                    shell_cmd, _depth=_depth + 1, _inherited_env=subshell_env
                )

            try:
                expansion = shlex.split(value, posix=True)
            except Exception as exc:
                raise ValueError(
                    f"Failed to parse git alias value {value!r}: {exc}"
                ) from exc
            combined = expansion + current_tokens[1:]
            new_alias_configs, remaining_tokens = _parse_git_global_configs(combined)
            alias_configs.update(new_alias_configs)
            current_tokens = remaining_tokens
            continue
        break

    return False


def _unwrap_xargs(tokens: list[str]) -> list[str]:
    """Unwrap an xargs command segment into wrapped command tokens with dynamic sentinels.

    Parses portable GNU and BSD xargs options, records replacement strings (-I, -J, -i, --replace),
    replaces placeholder occurrences in wrapped command tokens with a dynamic sentinel,
    and appends a dynamic sentinel to model stdin-derived arguments.

    Returns an empty list if no wrapped command is present.
    """
    if not tokens:
        return []

    idx = 1
    repl_strings: list[str] = []

    while idx < len(tokens):
        token = tokens[idx]

        if token == "--":
            idx += 1
            break

        if token.startswith("--"):
            if "=" in token:
                opt_name, opt_val = token.split("=", 1)
                if opt_name in XARGS_REQ_LONG_OPTS:
                    idx += 1
                    continue
                elif opt_name in XARGS_OPT_LONG_OPTS:
                    if opt_name == "--replace" and opt_val:
                        repl_strings.append(opt_val)
                    idx += 1
                    continue
                else:
                    idx += 1
                    continue
            else:
                if token in XARGS_REQ_LONG_OPTS:
                    idx += 1
                    if idx < len(tokens):
                        idx += 1
                    continue
                elif token in XARGS_OPT_LONG_OPTS:
                    if token == "--replace":
                        repl_strings.append("{}")
                    idx += 1
                    continue
                else:
                    idx += 1
                    continue

        if token.startswith("-") and len(token) > 1:
            j = 1
            while j < len(token):
                char = token[j]
                if char in XARGS_REQ_SHORT_OPTS:
                    remainder = token[j + 1 :]
                    if remainder:
                        if char in {"I", "J"}:
                            repl_strings.append(remainder)
                    else:
                        idx += 1
                        if idx < len(tokens):
                            opt_val = tokens[idx]
                            if char in {"I", "J"} and opt_val:
                                repl_strings.append(opt_val)
                    break
                elif char in XARGS_OPT_SHORT_OPTS:
                    remainder = token[j + 1 :]
                    if remainder:
                        if char == "i":
                            repl_strings.append(remainder)
                    else:
                        if char == "i":
                            repl_strings.append("{}")
                    break
                else:
                    j += 1
            idx += 1
            continue

        break

    wrapped_cmd = tokens[idx:]
    if not wrapped_cmd:
        return []

    result: list[str] = []
    for tok in wrapped_cmd:
        replaced = tok
        for repl in repl_strings:
            if repl:
                replaced = replaced.replace(repl, XARGS_INPUT_SENTINEL)
        result.append(replaced)

    result.append(XARGS_INPUT_SENTINEL)
    return result


class _HereDocTarget:
    """Represents a pending here-doc redirection."""

    __slots__ = ("delimiter", "is_quoted", "strip_tabs")

    def __init__(self, delimiter: str, is_quoted: bool, strip_tabs: bool) -> None:
        self.delimiter = delimiter
        self.is_quoted = is_quoted
        self.strip_tabs = strip_tabs


def _parse_heredoc_delimiter(command: str, start: int) -> tuple[_HereDocTarget, int]:
    """Parse a here-doc redirection starting at index `start` (pointing at first '<' of '<<').

    Returns (_HereDocTarget, next_index_after_delimiter_word).
    Raises ValueError if delimiter word is missing or quote in delimiter is unclosed.
    """
    n = len(command)
    pos = start + 2
    strip_tabs = False
    if pos < n and command[pos] == "-":
        strip_tabs = True
        pos += 1

    # Skip optional horizontal whitespace
    while pos < n and command[pos] in " \t":
        pos += 1

    if pos >= n or command[pos] in "\r\n;&|()<>":
        raise ValueError("Missing delimiter word after here-doc redirection")

    delim_chars: list[str] = []
    is_quoted = False
    i = pos

    while i < n:
        ch = command[i]
        if ch in " \t\r\n;&|()<>":
            break
        elif ch == "\\":
            is_quoted = True
            i += 1
            if i < n:
                if command[i] == "\n":
                    i += 1
                    continue
                delim_chars.append(command[i])
                i += 1
            else:
                raise ValueError("Incomplete backslash in here-doc delimiter")
        elif ch == "'":
            is_quoted = True
            i += 1
            while i < n and command[i] != "'":
                delim_chars.append(command[i])
                i += 1
            if i >= n:
                raise ValueError("Unclosed single quote in here-doc delimiter")
            i += 1
        elif ch == '"':
            is_quoted = True
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\":
                    i += 1
                    if i >= n:
                        raise ValueError("Unclosed double quote in here-doc delimiter")
                    next_ch = command[i]
                    if next_ch == "\n":
                        i += 1
                        continue
                    elif next_ch in {"$", "`", '"', "\\"}:
                        delim_chars.append(next_ch)
                        i += 1
                        continue
                    else:
                        delim_chars.append("\\")
                        continue
                else:
                    delim_chars.append(command[i])
                    i += 1
            if i >= n:
                raise ValueError("Unclosed double quote in here-doc delimiter")
            i += 1
        elif ch == "$" and i + 1 < n and command[i + 1] == "'":
            is_quoted = True
            i += 2
            while i < n and command[i] != "'":
                if command[i] == "\\":
                    raise ValueError(
                        "ANSI-C backslash escapes in here-doc delimiters are unsupported and ambiguous"
                    )
                delim_chars.append(command[i])
                i += 1
            if i >= n:
                raise ValueError("Unclosed ANSI-C quote in here-doc delimiter")
            i += 1
        else:
            delim_chars.append(ch)
            i += 1

    if i == pos:
        raise ValueError("Missing delimiter word after here-doc redirection")

    delimiter = "".join(delim_chars)
    return _HereDocTarget(delimiter=delimiter, is_quoted=is_quoted, strip_tabs=strip_tabs), i


def _consume_heredoc_body(
    command: str, start: int, target: _HereDocTarget
) -> tuple[str, int]:
    """Consume a here-doc body from command starting at line start `start`.

    Returns (body_text, next_index_after_delimiter_line).
    Raises ValueError if delimiter line is not found before EOF.
    """
    n = len(command)
    i = start
    body_parts: list[str] = []

    while i <= n:
        if i == n:
            raise ValueError(
                f"Unclosed here-doc body: missing terminating delimiter {target.delimiter!r}"
            )

        line_start = i
        newline_idx = command.find("\n", line_start)
        if newline_idx == -1:
            line = command[line_start:]
            next_line_start = n
        else:
            line = command[line_start:newline_idx]
            next_line_start = newline_idx + 1

        if target.strip_tabs:
            check_line = line.lstrip("\t")
        else:
            check_line = line

        if check_line == target.delimiter:
            body_text = "".join(body_parts)
            return body_text, next_line_start

        if newline_idx == -1:
            body_parts.append(line)
            i = n
        else:
            body_parts.append(command[line_start:next_line_start])
            i = next_line_start

    raise ValueError(
        f"Unclosed here-doc body: missing terminating delimiter {target.delimiter!r}"
    )


def _strip_comments_preserving_newlines(command: str) -> str:
    """Strip unquoted shell comments while preserving newline command boundaries.

    POSIX shell comments begin with an unquoted '#' at a word boundary (start of
    line, after whitespace, or after a command separator/operator) and extend to
    the next newline or EOF. The terminating newline is preserved as a command
    separator.

    Escaped semicolons and parentheses in NORMAL state, as well as semicolons and
    parentheses inside quotes, are replaced with collision-resistant sentinels
    so they are not treated as structural delimiters.

    Here-doc bodies are skipped from the command token stream while preserving
    the here-doc redirection operator on the command line.
    """
    result: list[str] = []
    pending_heredocs: list[_HereDocTarget] = []
    i = 0
    n = len(command)
    state = "NORMAL"
    prev_char: str | None = None

    while i < n:
        ch = command[i]

        if state == "NORMAL":
            if ch == "\\":
                if i + 1 < n and command[i + 1] == ";":
                    result.append(_LITERAL_SEMICOLON_SENTINEL)
                    prev_char = _LITERAL_SEMICOLON_SENTINEL[-1]
                    i += 2
                    continue
                if i + 1 < n and command[i + 1] == "(":
                    result.append(_LITERAL_OPEN_PAREN_SENTINEL)
                    prev_char = _LITERAL_OPEN_PAREN_SENTINEL[-1]
                    i += 2
                    continue
                if i + 1 < n and command[i + 1] == ")":
                    result.append(_LITERAL_CLOSE_PAREN_SENTINEL)
                    prev_char = _LITERAL_CLOSE_PAREN_SENTINEL[-1]
                    i += 2
                    continue
                result.append(ch)
                i += 1
                if i < n:
                    result.append(command[i])
                    prev_char = command[i]
                    i += 1
                else:
                    prev_char = ch
                continue
            elif ch == "'":
                state = "SINGLE_QUOTE"
                result.append(ch)
                prev_char = ch
                i += 1
                continue
            elif ch == '"':
                state = "DOUBLE_QUOTE"
                result.append(ch)
                prev_char = ch
                i += 1
                continue
            elif ch == "#" and (prev_char is None or prev_char in " \t\r\n;&|(){}<>"):
                state = "COMMENT"
                i += 1
                continue
            elif ch == "<" and i + 1 < n and command[i + 1] == "<":
                if i + 2 < n and command[i + 2] == "<":
                    result.append(command[i : i + 3])
                    i += 3
                    prev_char = "<"
                    continue
                target, next_i = _parse_heredoc_delimiter(command, i)
                pending_heredocs.append(target)
                result.append(command[i:next_i])
                i = next_i
                prev_char = target.delimiter[-1] if target.delimiter else ">"
                continue
            elif ch == "\n":
                result.append("\n")
                if pending_heredocs:
                    next_start = i + 1
                    for hd in pending_heredocs:
                        _, next_start = _consume_heredoc_body(
                            command, next_start, hd
                        )
                    pending_heredocs.clear()
                    i = next_start
                    prev_char = "\n"
                    continue
                else:
                    prev_char = "\n"
                    i += 1
                    continue
            else:
                result.append(ch)
                prev_char = ch
                i += 1
                continue

        elif state == "SINGLE_QUOTE":
            if ch == "'":
                state = "NORMAL"
                result.append(ch)
                prev_char = ch
                i += 1
                continue
            elif ch == ";":
                result.append(_LITERAL_SEMICOLON_SENTINEL)
                prev_char = _LITERAL_SEMICOLON_SENTINEL[-1]
                i += 1
                continue
            elif ch == "(":
                result.append(_LITERAL_OPEN_PAREN_SENTINEL)
                prev_char = _LITERAL_OPEN_PAREN_SENTINEL[-1]
                i += 1
                continue
            elif ch == ")":
                result.append(_LITERAL_CLOSE_PAREN_SENTINEL)
                prev_char = _LITERAL_CLOSE_PAREN_SENTINEL[-1]
                i += 1
                continue
            else:
                result.append(ch)
                prev_char = ch
                i += 1
                continue

        elif state == "DOUBLE_QUOTE":
            if ch == '"':
                state = "NORMAL"
                result.append(ch)
                prev_char = '"'
                i += 1
                continue
            elif ch == "\\":
                if i + 1 < n and command[i + 1] == ";":
                    result.append(_LITERAL_SEMICOLON_SENTINEL)
                    prev_char = _LITERAL_SEMICOLON_SENTINEL[-1]
                    i += 2
                    continue
                if i + 1 < n and command[i + 1] == "(":
                    result.append(_LITERAL_OPEN_PAREN_SENTINEL)
                    prev_char = _LITERAL_OPEN_PAREN_SENTINEL[-1]
                    i += 2
                    continue
                if i + 1 < n and command[i + 1] == ")":
                    result.append(_LITERAL_CLOSE_PAREN_SENTINEL)
                    prev_char = _LITERAL_CLOSE_PAREN_SENTINEL[-1]
                    i += 2
                    continue
                result.append(ch)
                i += 1
                if i < n:
                    result.append(command[i])
                    prev_char = command[i]
                    i += 1
                else:
                    prev_char = ch
                continue
            elif ch == ";":
                result.append(_LITERAL_SEMICOLON_SENTINEL)
                prev_char = _LITERAL_SEMICOLON_SENTINEL[-1]
                i += 1
                continue
            elif ch == "(":
                result.append(_LITERAL_OPEN_PAREN_SENTINEL)
                prev_char = _LITERAL_OPEN_PAREN_SENTINEL[-1]
                i += 1
                continue
            elif ch == ")":
                result.append(_LITERAL_CLOSE_PAREN_SENTINEL)
                prev_char = _LITERAL_CLOSE_PAREN_SENTINEL[-1]
                i += 1
                continue
            else:
                result.append(ch)
                prev_char = ch
                i += 1
                continue

        elif state == "COMMENT":
            if ch == "\n":
                result.append("\n")
                if pending_heredocs:
                    next_start = i + 1
                    state = "NORMAL"
                    for hd in pending_heredocs:
                        _, next_start = _consume_heredoc_body(
                            command, next_start, hd
                        )
                    pending_heredocs.clear()
                    i = next_start
                    prev_char = "\n"
                    continue
                else:
                    state = "NORMAL"
                    prev_char = "\n"
                    i += 1
                    continue
            i += 1
            continue

    if pending_heredocs:
        raise ValueError("Unclosed here-doc body")

    return "".join(result)



def _tokenize_command_raw(command: str) -> list[list[str]]:
    """Tokenize a shell command string into command segments preserving internal sentinels."""
    cleaned = _strip_comments_preserving_newlines(command)
    lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=True)
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    lexer.wordchars += "+%{}"

    tokens = list(lexer)
    if not tokens:
        return []

    return _split_into_commands(tokens)


def _tokenize_command(command: str) -> list[list[str]]:
    """Tokenize a shell command string and split into individual command segments with restored literals."""
    commands = _tokenize_command_raw(command)
    return [
        [_restore_sentinels(tok) for tok in cmd]
        for cmd in commands
    ]


def _tokenize_split_string(split_str: str) -> list[str]:
    """Tokenize an env split-string argument into individual tokens.

    Raises ValueError if quotation or escaping is malformed, or if the string
    contains backslash escapes or dollar expansions outside the supported subset.
    """
    if "\\" in split_str or "$" in split_str:
        raise ValueError(
            "env split-string containing backslash escapes or variable expansions is not supported"
        )
    return shlex.split(split_str, posix=True)


def _parse_assignment_str(token: str) -> tuple[str, str, bool] | None:
    """Parse a single token as a variable assignment (NAME=val or NAME+=val).

    Returns (name, val, is_append) if valid assignment, else None.
    Variable name must be a valid identifier.
    """
    if "+=" in token:
        name, val = token.split("+=", 1)
        if name.isidentifier():
            return name, val, True
    if "=" in token:
        name, val = token.split("=", 1)
        if name.isidentifier():
            return name, val, False
    return None


def _is_var_assignment(token: str) -> bool:
    """Check if token is an environment variable assignment like FOO=bar or FOO+=bar."""
    return _parse_assignment_str(token) is not None


def _is_redirection(token: str) -> bool:
    """Check if token is a redirection operator."""
    if token in {
        ">",
        ">>",
        "<",
        "<>",
        ">&",
        "<&",
        "&>",
        "&>>",
        ">|",
        "1>",
        "2>",
        "1>>",
        "2>>",
        "<<",
        "<<-",
        "<<<",
        "0<",
        "0<<",
        "0<<-",
        "1<<",
        "1<<-",
        "2<<",
        "2<<-",
    }:
        return True
    if len(token) >= 2 and token[0].isdigit() and (token[1] in {">", "<"} or token[1:3] in {">>", "<<"}):
        return True
    return False


_VALID_SHELL_OPERATORS_BY_LENGTH = (
    # Length 3
    ";;&",
    "<<-",
    "<<<",
    "&>>",
    # Length 2
    ";;",
    ";&",
    "&&",
    "||",
    "|&",
    ">>",
    ">&",
    "<&",
    "&>",
    ">|",
    "<>",
    "<<",
    # Length 1
    ";",
    "&",
    "|",
    "(",
    ")",
    ">",
    "<",
)


def _decompose_punctuation_run(tok: str) -> list[str]:
    """Decompose a structural punctuation run into longest valid shell operators.

    Raises ValueError if the punctuation run is malformed or ambiguous.
    """
    if not tok:
        return []
    if not all(c in "();<>|&" for c in tok):
        return [tok]

    if tok in {
        ";",
        "&",
        "|",
        "(",
        ")",
        ">",
        "<",
        "&&",
        "||",
        "|&",
        ";;",
        ";&",
        ";;&",
        ">>",
        ">&",
        "<&",
        "&>",
        ">|",
        "<>",
        "<<",
        "<<-",
        "<<<",
        "&>>",
    }:
        return [tok]

    decomposed: list[str] = []
    pos = 0
    n = len(tok)
    while pos < n:
        matched = False
        for op in _VALID_SHELL_OPERATORS_BY_LENGTH:
            if tok.startswith(op, pos):
                decomposed.append(op)
                pos += len(op)
                matched = True
                break
        if not matched:
            raise ValueError(f"Malformed or ambiguous punctuation sequence: {tok!r}")

    return decomposed


def _normalize_raw_tokens(raw_tokens: list[str]) -> list[str]:
    """Normalize a list of raw shell tokens by decomposing compound structural punctuation runs."""
    normalized: list[str] = []
    for tok in raw_tokens:
        normalized.extend(_decompose_punctuation_run(tok))
    return normalized


def _split_into_commands(tokens: list[str]) -> list[list[str]]:
    """Split a stream of shell tokens into individual command segments."""
    commands: list[list[str]] = []
    current: list[str] = []
    i = 0
    token_list = list(tokens)

    while i < len(token_list):
        tok = token_list[i]
        if (
            tok == "$"
            and i + 1 < len(token_list)
            and _is_all_parens(token_list[i + 1])
            and token_list[i + 1].startswith("(")
        ):
            next_tok = token_list[i + 1]
            paren_depth = 0
            for char in next_tok:
                if char == "(":
                    paren_depth += 1
                elif char == ")":
                    paren_depth -= 1
            if paren_depth <= 0:
                raise ValueError(
                    f"Malformed command substitution: '${next_tok}'"
                )
            current.append("$")
            current.append(next_tok)
            i += 2
            while i < len(token_list) and paren_depth > 0:
                inner_tok = token_list[i]
                if _is_all_parens(inner_tok):
                    closed_idx = None
                    for p_idx, char in enumerate(inner_tok):
                        if char == "(":
                            paren_depth += 1
                        elif char == ")":
                            paren_depth -= 1
                            if paren_depth == 0:
                                closed_idx = p_idx
                                break
                    if closed_idx is not None:
                        part_consumed = inner_tok[: closed_idx + 1]
                        current.append(part_consumed)
                        remainder = inner_tok[closed_idx + 1 :]
                        i += 1
                        if remainder:
                            token_list.insert(i, remainder)
                        break
                    else:
                        current.append(inner_tok)
                        i += 1
                else:
                    current.append(inner_tok)
                    i += 1
            if paren_depth > 0:
                raise ValueError("Unmatched '$(' in command substitution")
            continue

        if tok in COMMAND_SEPARATORS or _is_all_parens(tok):
            if current:
                commands.append(current)
                current = []
        else:
            current.append(tok)
        i += 1

    if current:
        commands.append(current)

    return commands


def _clean_command_segment(tokens: list[str]) -> list[str]:
    """Strip shell keywords, redirections, and assignments from a single command segment."""
    idx = 0
    while idx < len(tokens) and tokens[idx] in SHELL_KEYWORDS:
        idx += 1
    tokens = tokens[idx:]

    cleaned: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.isdigit() and i + 1 < len(tokens) and _is_redirection(tokens[i + 1]):
            i += 1
            continue
        if _is_redirection(token):
            if token in {
                ">",
                ">>",
                "<",
                "<>",
                ">&",
                "<&",
                "&>",
                "&>>",
                ">|",
                "1>",
                "2>",
                "1>>",
                "2>>",
                "<<",
                "<<-",
                "<<<",
                "0<",
                "0<<",
                "0<<-",
                "1<<",
                "1<<-",
                "2<<",
                "2<<-",
            } or (len(token) >= 2 and token[0].isdigit()):
                i += 2
            else:
                i += 1
        elif token in {"2>&1", "1>&2", ">&1", ">&2"}:
            i += 1
        else:
            cleaned.append(token)
            i += 1

    return cleaned


def _has_shell_expansion(token: str) -> bool:
    """Check if token contains unsupported shell expansion markers ($ or ` or substitution sentinels)."""
    return "$" in token or "`" in token or _SUBST_SENTINEL_PREFIX in token


def _is_forced_push_args(push_args: list[str]) -> bool:
    """Check if arguments to 'git push' indicate a forced push."""
    for arg in push_args:
        if _has_shell_expansion(arg):
            raise ValueError(
                f"git push argument containing shell expansion is not supported: {arg!r}"
            )

    after_double_dash = False

    for arg in push_args:
        if not after_double_dash and arg == "--":
            after_double_dash = True
            continue

        if after_double_dash:
            if arg.startswith("+"):
                return True
        else:
            if arg.startswith("+"):
                return True
            if arg.startswith("--force"):
                return True
            if arg in {"--m", "--mi", "--mir", "--mirr", "--mirro", "--mirror"}:
                return True
            if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1 and "f" in arg[1:]:
                return True

    return False


def _is_forbidden_rm_args(rm_args: list[str]) -> bool:
    """Check if arguments to 'rm' combine recursive and force semantics in any order or grouping."""
    for arg in rm_args:
        if arg == "--":
            # Any arguments after '--' are filenames/operands, not flags
            break

        if _has_shell_expansion(arg):
            raise ValueError(
                f"rm argument containing shell expansion before '--' is not supported: {arg!r}"
            )

    has_recursive = False
    has_force = False

    for arg in rm_args:
        if arg == "--":
            # Any arguments after '--' are filenames/operands, not flags
            break

        if arg == "--recursive":
            has_recursive = True
        elif arg == "--force":
            has_force = True
        elif arg.startswith("--"):
            pass
        elif arg.startswith("-") and len(arg) > 1:
            flags = arg[1:]
            if "r" in flags or "R" in flags:
                has_recursive = True
            if "f" in flags:
                has_force = True

        if has_recursive and has_force:
            return True

    return has_recursive and has_force


MAX_GIT_CONFIG_COUNT = 1000


def _inspect_fish_invocation(
    tokens: list[str],
    checker_fn: Callable[[str], bool],
) -> bool:
    """Inspect a fish shell invocation.

    Fish supports:
    - -c, --command, --command=<cmd> for main command strings.
    - -C, --init-command, --init-cmd, --init-command=<cmd>, --init-cmd=<cmd> for initial command strings executed before main input.
    - Options with arguments: -d, --debug, --debug-categories, -D, --debug-stack-frames,
      -o, --debug-output, -p, --profile, --profile-startup, -f, --features.
    - Flags without arguments: -P, --private, -N, --no-config, -i, --interactive,
      -l, --login, -n, --no-execute, -v, --version, -h, --help, -s, --stdin.
    - Explicit script operand is allowed if all init commands are safe.
    - If any init command or main command is dangerous, block (return True).
    - If all init commands are safe but no -c/--command or script operand is provided,
      the shell reads from stdin, so raise ValueError to fail closed.
    """
    args = tokens[1:]
    i = 0
    n = len(args)
    init_commands: list[str] = []
    main_command: str | None = None
    script_operand: str | None = None
    reads_stdin = False

    fish_opts_with_arg = {
        "-d",
        "--debug",
        "--debug-categories",
        "-D",
        "--debug-stack-frames",
        "-o",
        "--debug-output",
        "-p",
        "--profile",
        "--profile-startup",
        "-f",
        "--features",
    }

    while i < n:
        arg = args[i]

        if arg == "--":
            i += 1
            if i < n:
                script_operand = args[i]
                i += 1
            else:
                reads_stdin = True
            break

        if arg == "-":
            i += 1
            if i < n:
                script_operand = args[i]
                i += 1
            else:
                reads_stdin = True
            break

        if arg in {"-C", "--init-command", "--init-cmd"}:
            if i + 1 < n:
                init_commands.append(_restore_sentinels(args[i + 1]))
                i += 2
                continue
            raise ValueError("Fish shell -C/--init-command is missing command argument")

        if arg.startswith(("--init-command=", "--init-cmd=")):
            init_commands.append(_restore_sentinels(arg.split("=", 1)[1]))
            i += 1
            continue

        if arg.startswith("-C") and len(arg) > 2:
            init_commands.append(_restore_sentinels(arg[2:]))
            i += 1
            continue

        if arg in {"-c", "--command"}:
            if i + 1 < n:
                main_command = _restore_sentinels(args[i + 1])
                i += 2
                continue
            raise ValueError("Fish shell -c/--command is missing command argument")

        if arg.startswith("--command="):
            main_command = _restore_sentinels(arg.split("=", 1)[1])
            i += 1
            continue

        if arg.startswith("-c") and len(arg) > 2:
            main_command = _restore_sentinels(arg[2:])
            i += 1
            continue

        if arg in fish_opts_with_arg:
            if i + 1 < n:
                i += 2
                continue
            raise ValueError(f"Fish option {arg!r} is missing argument")

        if any(
            arg.startswith(opt + "=")
            for opt in fish_opts_with_arg
            if opt.startswith("--")
        ):
            i += 1
            continue

        if arg.startswith(("-d", "-D", "-o", "-p", "-f")) and len(arg) > 2:
            i += 1
            continue

        if arg.startswith("-") and len(arg) > 1:
            if arg.startswith("--"):
                if arg == "--stdin":
                    reads_stdin = True
                i += 1
                continue

            flags = arg[1:]
            if "C" in flags:
                if i + 1 < n:
                    init_commands.append(_restore_sentinels(args[i + 1]))
                    i += 2
                    continue
                raise ValueError("Fish shell -C flag is missing command argument")
            if "c" in flags:
                if i + 1 < n:
                    main_command = _restore_sentinels(args[i + 1])
                    i += 2
                    continue
                raise ValueError("Fish shell -c flag is missing command argument")
            if "s" in flags:
                reads_stdin = True
            if flags.endswith(("d", "D", "o", "p", "f")):
                if i + 1 < n:
                    i += 2
                    continue
                raise ValueError(f"Fish option {arg!r} is missing argument")
            i += 1
            continue

        # Positional operand (script file)
        script_operand = arg
        i += 1
        break

    for init_cmd in init_commands:
        if checker_fn(init_cmd):
            return True

    if main_command is not None:
        return checker_fn(main_command)

    if script_operand is not None and not reads_stdin:
        return False

    raise ValueError("Shell invocation reads command text from stdin")


def _inspect_posix_shell_invocation(
    tokens: list[str],
    checker_fn: Callable[..., bool],
) -> bool:
    """Inspect a POSIX shell invocation (sh, bash, zsh, dash, ksh).

    Handles options such as -c, -o/+o, -O/+O, --rcfile/--init-file, --command, -s, etc.
    Uppercase -C is noclobber, NOT a command option.
    """
    args = tokens[1:]
    reads_stdin = False
    i = 0
    n = len(args)
    shell_binary = os.path.basename(tokens[0])
    expand_aliases_opt: bool | None = None

    while i < n:
        arg = args[i]

        if arg == "--":
            if reads_stdin:
                raise ValueError("Shell invocation reads command text from stdin")
            if i + 1 < n:
                return False
            raise ValueError("Shell invocation reads command text from stdin")

        if arg == "-":
            if reads_stdin:
                raise ValueError("Shell invocation reads command text from stdin")
            if i + 1 < n:
                return False
            raise ValueError("Shell invocation reads command text from stdin")

        if arg.startswith("-") and len(arg) > 1:
            if arg.startswith("--"):
                if arg == "--stdin":
                    reads_stdin = True
                    i += 1
                elif arg in {"--rcfile", "--init-file"}:
                    i += 2
                elif arg.startswith(("--rcfile=", "--init-file=")):
                    i += 1
                elif arg == "--command":
                    if i + 1 < n:
                        init_expand = (
                            expand_aliases_opt
                            if expand_aliases_opt is not None
                            else (shell_binary in {"sh", "dash", "zsh", "ksh"})
                        )
                        return checker_fn(
                            _restore_sentinels(args[i + 1]),
                            _init_expand_aliases=init_expand,
                        )
                    raise ValueError("Shell invocation -c flag is missing command argument")
                elif arg.startswith("--command="):
                    init_expand = (
                        expand_aliases_opt
                        if expand_aliases_opt is not None
                        else (shell_binary in {"sh", "dash", "zsh", "ksh"})
                    )
                    return checker_fn(
                        _restore_sentinels(arg.split("=", 1)[1]),
                        _init_expand_aliases=init_expand,
                    )
                else:
                    i += 1
                continue

            if arg == "-c":
                if i + 1 < n:
                    init_expand = (
                        expand_aliases_opt
                        if expand_aliases_opt is not None
                        else (shell_binary in {"sh", "dash", "zsh", "ksh"})
                    )
                    return checker_fn(
                        _restore_sentinels(args[i + 1]),
                        _init_expand_aliases=init_expand,
                    )
                raise ValueError("Shell invocation -c flag is missing command argument")

            if arg.startswith("-c") and not arg.startswith("-C"):
                init_expand = (
                    expand_aliases_opt
                    if expand_aliases_opt is not None
                    else (shell_binary in {"sh", "dash", "zsh", "ksh"})
                )
                return checker_fn(
                    _restore_sentinels(arg[2:]),
                    _init_expand_aliases=init_expand,
                )

            if arg in {"-o", "-O"}:
                if i + 1 < n:
                    opt_val = args[i + 1]
                    if arg == "-O" and opt_val == "expand_aliases":
                        expand_aliases_opt = True
                    i += 2
                else:
                    raise ValueError(f"Shell invocation {arg} flag is missing option argument")
                continue

            if arg.startswith("-O") and len(arg) > 2:
                opt_val = arg[2:]
                if opt_val == "expand_aliases":
                    expand_aliases_opt = True
                i += 1
                continue

            flags = arg[1:]
            if "c" in flags:
                if i + 1 < n:
                    init_expand = (
                        expand_aliases_opt
                        if expand_aliases_opt is not None
                        else (shell_binary in {"sh", "dash", "zsh", "ksh"})
                    )
                    return checker_fn(
                        _restore_sentinels(args[i + 1]),
                        _init_expand_aliases=init_expand,
                    )
                raise ValueError("Shell invocation -c flag is missing command argument")

            if "s" in flags:
                reads_stdin = True

            if "o" in flags and flags.endswith("o"):
                if i + 1 < n:
                    i += 2
                else:
                    raise ValueError("Shell invocation -o flag is missing option argument")
            elif "O" in flags and flags.endswith("O"):
                if i + 1 < n:
                    if args[i + 1] == "expand_aliases":
                        expand_aliases_opt = True
                    i += 2
                else:
                    raise ValueError("Shell invocation -O flag is missing option argument")
            else:
                i += 1
            continue

        if arg.startswith("+") and len(arg) > 1:
            if arg in {"+o", "+O"}:
                if i + 1 < n:
                    if arg == "+O" and args[i + 1] == "expand_aliases":
                        expand_aliases_opt = False
                    i += 2
                else:
                    raise ValueError(f"Shell invocation {arg} flag is missing option argument")
                continue

            if arg.startswith("+O") and len(arg) > 2:
                if arg[2:] == "expand_aliases":
                    expand_aliases_opt = False
                i += 1
                continue

            flags = arg[1:]
            if "o" in flags and flags.endswith("o"):
                if i + 1 < n:
                    i += 2
                else:
                    raise ValueError("Shell invocation +o flag is missing option argument")
            elif "O" in flags and flags.endswith("O"):
                if i + 1 < n:
                    if args[i + 1] == "expand_aliases":
                        expand_aliases_opt = False
                    i += 2
                else:
                    raise ValueError("Shell invocation +O flag is missing option argument")
            else:
                i += 1
            continue

        if reads_stdin:
            raise ValueError("Shell invocation reads command text from stdin")
        return False

    raise ValueError("Shell invocation reads command text from stdin")


def _inspect_shell_invocation(
    tokens: list[str],
    checker_fn: Callable[..., bool],
) -> bool:
    """Inspect a shell invocation (e.g. sh, bash, zsh, dash, ksh, fish).

    If the invocation specifies a command string via -c (or grouped short options
    containing 'c'), evaluate the command string using checker_fn.
    If the invocation specifies an explicit script operand, return False (allow).
    If the invocation reads command text from stdin (bare shell, -s option,
    options-only without script operand, etc.), raise ValueError to fail closed.
    """
    if not tokens:
        return False

    shell_binary = os.path.basename(tokens[0])
    if shell_binary == "fish":
        return _inspect_fish_invocation(tokens, checker_fn)

    return _inspect_posix_shell_invocation(tokens, checker_fn)


def _is_git_config_protocol_key(key: str) -> bool:
    """Return True if key is an exact Git environment config protocol key."""
    return (
        key == "GIT_CONFIG_COUNT"
        or (
            key.startswith("GIT_CONFIG_KEY_")
            and key[len("GIT_CONFIG_KEY_") :].isdigit()
        )
        or (
            key.startswith("GIT_CONFIG_VALUE_")
            and key[len("GIT_CONFIG_VALUE_") :].isdigit()
        )
    )


def _parse_git_env_details(
    env_vars: dict[str, str],
) -> tuple[
    dict[str, tuple[str, str]],
    dict[str, tuple[str, str]],
    list[tuple[str, str, str]],
]:
    """Parse and validate Git config environment protocol variables into structured configs.

    Returns (alias_configs, mirror_configs, push_configs).
    Raises ValueError on missing required indexed members, out-of-bounds count, or dynamically expanded protocol variables.
    """
    for k, v in env_vars.items():
        if _is_git_config_protocol_key(k) and _has_shell_expansion(v):
            raise ValueError(f"{k} contains shell expansion: {v!r}")

    if "GIT_CONFIG_COUNT" not in env_vars:
        return {}, {}, []

    count_str = env_vars["GIT_CONFIG_COUNT"]
    if not count_str or not all(c in "0123456789" for c in count_str):
        raise ValueError(
            f"GIT_CONFIG_COUNT must be a literal nonnegative integer, got {count_str!r}"
        )

    count = int(count_str)
    if count < 0:
        raise ValueError(f"GIT_CONFIG_COUNT must be nonnegative, got {count}")
    if count > MAX_GIT_CONFIG_COUNT:
        raise ValueError(
            f"GIT_CONFIG_COUNT {count} exceeds bounded maximum ({MAX_GIT_CONFIG_COUNT})"
        )

    pairs: list[tuple[str, str]] = []
    for i in range(count):
        k_var = f"GIT_CONFIG_KEY_{i}"
        v_var = f"GIT_CONFIG_VALUE_{i}"

        if k_var not in env_vars:
            raise ValueError(f"Missing {k_var} for GIT_CONFIG_COUNT={count}")
        if v_var not in env_vars:
            raise ValueError(f"Missing {v_var} for GIT_CONFIG_COUNT={count}")

        k_val = env_vars[k_var]
        v_val = env_vars[v_var]

        pairs.append((k_val, v_val))

    alias_configs: dict[str, tuple[str, str]] = {}
    mirror_configs: dict[str, tuple[str, str]] = {}
    push_configs: list[tuple[str, str, str]] = []

    for k_val, v_val in pairs:
        entry = f"{k_val}={v_val}"
        _record_alias_config(alias_configs, entry, "c")
        _record_forcing_config(mirror_configs, push_configs, entry, "c")

    return alias_configs, mirror_configs, push_configs


def _parse_git_env_configs(
    env_vars: dict[str, str],
) -> tuple[dict[str, tuple[str, str]], bool]:
    """Parse and validate Git config environment protocol variables.

    Recognizes:
    - GIT_CONFIG_COUNT
    - GIT_CONFIG_KEY_<nonnegative index>
    - GIT_CONFIG_VALUE_<nonnegative index>

    Dynamic protocol variables (containing shell expansions) fail closed immediately.
    If GIT_CONFIG_COUNT is absent, returns ({}, False) because Git ignores indexed variables.
    If GIT_CONFIG_COUNT is present, validates that all required indexed members (0 .. count-1) are present and literal.
    Returns (alias_configs, has_forcing_config).
    Raises ValueError on missing required indexed members, out-of-bounds count, or dynamically expanded protocol variables.
    """
    alias_configs, mirror_configs, push_configs = _parse_git_env_details(env_vars)

    has_forcing = False
    for _key, kind, val in push_configs:
        if kind == "config-env" or val.strip().startswith("+"):
            has_forcing = True
            break

    if not has_forcing:
        for kind, val in mirror_configs.values():
            if kind == "config-env" or val.strip().lower() not in {"false", "no", "off", "0"}:
                has_forcing = True
                break

    return alias_configs, has_forcing


def _consume_var_assignment(tokens: list[str]) -> tuple[str, str, bool] | None:
    """If tokens start with a variable assignment, pop it and return (name, val, is_append).

    Handles:
    - Normal assignments: VAR=val, VAR+=val, VAR="val", VAR='val'
    - Split assignments: VAR= followed by $, `, or tokens
    - Split append assignments: VAR+= or VAR+ = followed by tokens
    - Split with colons: VAR=+HEAD:main (where : was split by shlex)
    """
    if not tokens:
        return None

    tok0 = tokens[0]
    parsed_single = _parse_assignment_str(tok0)
    if parsed_single is not None:
        tokens.pop(0)
        name, val, is_append = parsed_single
        if val == "" and tokens:
            if tokens[0] == "$":
                tokens.pop(0)
                if tokens:
                    val = "$" + tokens.pop(0)
                else:
                    val = "$"
            elif tokens[0].startswith("$") or tokens[0].startswith("`"):
                val = tokens.pop(0)
        while tokens and tokens[0] == ":":
            tokens.pop(0)
            if tokens:
                val = val + ":" + tokens.pop(0)
        return name, _restore_sentinels(val), is_append

    # Multi-token patterns produced by shlex when wordchars includes '+'
    if len(tokens) >= 2 and tokens[0].endswith("+") and tokens[1] == "=":
        candidate_name = tokens[0][:-1]
        if candidate_name.isidentifier():
            name = candidate_name
            is_append = True
            tokens.pop(0)
            tokens.pop(0)
            val = ""
            if tokens:
                if tokens[0] == "$":
                    tokens.pop(0)
                    if tokens:
                        val = "$" + tokens.pop(0)
                    else:
                        val = "$"
                elif not _is_redirection(tokens[0]) and tokens[0] not in COMMAND_SEPARATORS:
                    val = tokens.pop(0)
            while tokens and tokens[0] == ":":
                tokens.pop(0)
                if tokens:
                    val = val + ":" + tokens.pop(0)
            return name, _restore_sentinels(val), is_append

    if len(tokens) >= 2 and tokens[0].isidentifier() and tokens[1] == "+=":
        name = tokens.pop(0)
        tokens.pop(0)
        is_append = True
        val = ""
        if tokens:
            if tokens[0] == "$":
                tokens.pop(0)
                if tokens:
                    val = "$" + tokens.pop(0)
                else:
                    val = "$"
            elif not _is_redirection(tokens[0]) and tokens[0] not in COMMAND_SEPARATORS:
                val = tokens.pop(0)
        while tokens and tokens[0] == ":":
            tokens.pop(0)
            if tokens:
                val = val + ":" + tokens.pop(0)
        return name, _restore_sentinels(val), is_append

    if len(tokens) >= 3 and tokens[0].isidentifier() and tokens[1] == "+" and tokens[2] == "=":
        name = tokens.pop(0)
        tokens.pop(0)
        tokens.pop(0)
        is_append = True
        val = ""
        if tokens:
            if tokens[0] == "$":
                tokens.pop(0)
                if tokens:
                    val = "$" + tokens.pop(0)
                else:
                    val = "$"
            elif not _is_redirection(tokens[0]) and tokens[0] not in COMMAND_SEPARATORS:
                val = tokens.pop(0)
        while tokens and tokens[0] == ":":
            tokens.pop(0)
            if tokens:
                val = val + ":" + tokens.pop(0)
        return name, _restore_sentinels(val), is_append

    if len(tokens) >= 2 and tokens[0].isidentifier() and tokens[1] == "=":
        name = tokens.pop(0)
        tokens.pop(0)
        is_append = False
        val = ""
        if tokens:
            if tokens[0] == "$":
                tokens.pop(0)
                if tokens:
                    val = "$" + tokens.pop(0)
                else:
                    val = "$"
            elif not _is_redirection(tokens[0]) and tokens[0] not in COMMAND_SEPARATORS:
                val = tokens.pop(0)
        while tokens and tokens[0] == ":":
            tokens.pop(0)
            if tokens:
                val = val + ":" + tokens.pop(0)
        return name, _restore_sentinels(val), is_append

    return None


def _unwrap_command_and_env(
    tokens: list[str],
    inherited_env: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Unwrap leading environment assignments and wrapper commands, accumulating env vars."""
    env_vars: dict[str, str] = dict(inherited_env) if inherited_env else {}
    tokens = list(tokens)

    while tokens:
        assignment = _consume_var_assignment(tokens)
        if assignment is not None:
            name, val, is_append = assignment
            if is_append:
                if _is_git_config_protocol_key(name):
                    if name not in env_vars:
                        raise ValueError(
                            f"Missing prior value for append assignment to Git config protocol key {name!r}"
                        )
                    env_vars[name] = env_vars[name] + val
                else:
                    env_vars[name] = env_vars.get(name, "") + val
            else:
                env_vars[name] = val
            continue

        if XARGS_INPUT_SENTINEL in tokens[0] or FIND_INPUT_SENTINEL in tokens[0]:
            break

        cmd_word = os.path.basename(tokens[0])

        if cmd_word == "sudo":
            tokens.pop(0)
            while tokens:
                t = tokens[0]
                if t == "--":
                    tokens.pop(0)
                    break
                if t in {"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-T"}:
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                elif t.startswith("-"):
                    tokens.pop(0)
                else:
                    sub_assignment = _consume_var_assignment(tokens)
                    if sub_assignment is not None:
                        name, val, is_append = sub_assignment
                        if is_append:
                            if _is_git_config_protocol_key(name):
                                if name not in env_vars:
                                    raise ValueError(
                                        f"Missing prior value for append assignment to Git config protocol key {name!r}"
                                    )
                                env_vars[name] = env_vars[name] + val
                            else:
                                env_vars[name] = env_vars.get(name, "") + val
                        else:
                            env_vars[name] = val
                    else:
                        break
            continue

        if cmd_word == "env":
            tokens.pop(0)
            while tokens:
                t = tokens[0]
                if t == "--":
                    tokens.pop(0)
                    break
                if t in {"-S", "--split-string"}:
                    tokens.pop(0)
                    if tokens:
                        raw_val = tokens.pop(0)
                        split_tokens = _tokenize_split_string(raw_val)
                        tokens = split_tokens + tokens
                    continue
                elif t.startswith("--split-string="):
                    tokens.pop(0)
                    raw_val = t.split("=", 1)[1]
                    split_tokens = _tokenize_split_string(raw_val)
                    tokens = split_tokens + tokens
                    continue
                elif t.startswith("-S") and len(t) > 2:
                    tokens.pop(0)
                    raw_val = t[2:]
                    split_tokens = _tokenize_split_string(raw_val)
                    tokens = split_tokens + tokens
                    continue
                elif t in {"-u", "--unset"}:
                    tokens.pop(0)
                    if tokens:
                        unset_name = tokens.pop(0)
                        env_vars.pop(unset_name, None)
                    continue
                elif t.startswith("--unset="):
                    tokens.pop(0)
                    unset_name = t.split("=", 1)[1]
                    env_vars.pop(unset_name, None)
                    continue
                elif t in {"-C", "--chdir"}:
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                    continue
                elif t.startswith("--chdir="):
                    tokens.pop(0)
                    continue
                elif t in {"-i", "--ignore-environment", "-"}:
                    tokens.pop(0)
                    env_vars.clear()
                    continue
                elif t.startswith("-"):
                    tokens.pop(0)
                else:
                    sub_assignment = _consume_var_assignment(tokens)
                    if sub_assignment is not None:
                        name, val, is_append = sub_assignment
                        if is_append:
                            if _is_git_config_protocol_key(name):
                                if name not in env_vars:
                                    raise ValueError(
                                        f"Missing prior value for append assignment to Git config protocol key {name!r}"
                                    )
                                env_vars[name] = env_vars[name] + val
                            else:
                                env_vars[name] = env_vars.get(name, "") + val
                        else:
                            env_vars[name] = val
                    else:
                        break
            continue

        if cmd_word == "command":
            idx = 1
            has_query = False
            while idx < len(tokens) and tokens[idx].startswith("-"):
                opt = tokens[idx]
                if opt == "--":
                    idx += 1
                    break
                if any(c in "vV" for c in opt[1:]):
                    has_query = True
                    break
                idx += 1
            if has_query:
                break
            tokens = tokens[idx:]
            continue

        if cmd_word in {"nohup", "builtin", "exec"}:
            tokens.pop(0)
            while tokens and tokens[0].startswith("-"):
                if tokens[0] == "-a":
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                elif tokens[0] == "--":
                    tokens.pop(0)
                    break
                else:
                    tokens.pop(0)
            continue

        if cmd_word == "timeout":
            tokens.pop(0)
            while tokens:
                t = tokens[0]
                if t == "--":
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                    break
                if t in {"-k", "-s", "--kill-after", "--signal"}:
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                elif t.startswith("-"):
                    tokens.pop(0)
                else:
                    tokens.pop(0)
                    break
            continue

        if cmd_word == "nice":
            tokens.pop(0)
            while tokens:
                t = tokens[0]
                if t == "--":
                    tokens.pop(0)
                    break
                if t in {"-n", "--adjustment"}:
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                elif t.startswith("-") or (len(t) > 1 and t.startswith("+") and t[1:].isdigit()):
                    tokens.pop(0)
                else:
                    break
            continue

        if cmd_word == "stdbuf":
            tokens.pop(0)
            while tokens:
                t = tokens[0]
                if t == "--":
                    tokens.pop(0)
                    break
                if t in {"-i", "-o", "-e", "--input", "--output", "--error"}:
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                elif t.startswith("-"):
                    tokens.pop(0)
                else:
                    break
            continue

        if cmd_word == "time":
            tokens.pop(0)
            while tokens:
                t = tokens[0]
                if t == "--":
                    tokens.pop(0)
                    break
                if t in {"-f", "-o", "--format", "--output"}:
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                elif t.startswith("-"):
                    tokens.pop(0)
                else:
                    break
            continue

        if cmd_word == "setsid":
            tokens.pop(0)
            is_terminal_info = False
            while tokens:
                t = tokens[0]
                if t == "--":
                    tokens.pop(0)
                    break
                if t in {"--help", "--version"}:
                    is_terminal_info = True
                    tokens.clear()
                    break
                if t in {"--ctty", "--fork", "--wait"}:
                    tokens.pop(0)
                    continue
                if t.startswith("--"):
                    raise ValueError(f"Unknown setsid option: {t!r}")
                if t.startswith("-") and len(t) > 1:
                    chars = set(t[1:])
                    if not chars.issubset({"c", "f", "w", "h", "V"}):
                        raise ValueError(f"Unknown setsid option: {t!r}")
                    if chars & {"h", "V"}:
                        is_terminal_info = True
                        tokens.clear()
                        break
                    tokens.pop(0)
                    continue
                break
            if is_terminal_info:
                tokens.clear()
            continue

        if cmd_word == "ionice":
            tokens.pop(0)
            is_terminal = False
            while tokens:
                t = tokens[0]
                if t == "--":
                    tokens.pop(0)
                    break
                if t == "-":
                    break
                if t.startswith("--"):
                    if t in {"--help", "--version"}:
                        is_terminal = True
                        tokens.clear()
                        break
                    if t in {"--pid", "--pgid", "--uid"} or t.startswith(
                        ("--pid=", "--pgid=", "--uid=")
                    ):
                        is_terminal = True
                        tokens.clear()
                        break
                    if t == "--ignore":
                        tokens.pop(0)
                        continue
                    if t.startswith("--class="):
                        val = t[len("--class=") :]
                        if _has_shell_expansion(val):
                            raise ValueError(
                                f"ionice option operand contains shell expansion: {val!r}"
                            )
                        tokens.pop(0)
                        if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                            raise ValueError(
                                f"ionice option operand contains shell expansion: {tokens[0]!r}"
                            )
                        continue
                    if t.startswith("--classdata="):
                        val = t[len("--classdata=") :]
                        if _has_shell_expansion(val):
                            raise ValueError(
                                f"ionice option operand contains shell expansion: {val!r}"
                            )
                        tokens.pop(0)
                        if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                            raise ValueError(
                                f"ionice option operand contains shell expansion: {tokens[0]!r}"
                            )
                        continue
                    if t in {"--class", "--classdata"}:
                        tokens.pop(0)
                        if not tokens:
                            is_terminal = True
                            tokens.clear()
                            break
                        val = tokens.pop(0)
                        if _has_shell_expansion(val) or val == "$":
                            raise ValueError(
                                f"ionice option operand contains shell expansion: {val!r}"
                            )
                        if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                            raise ValueError(
                                f"ionice option operand contains shell expansion: {tokens[0]!r}"
                            )
                        continue
                    raise ValueError(f"Unknown ionice option: {t!r}")

                if t.startswith("-") and len(t) > 1:
                    tok = tokens.pop(0)
                    idx = 1
                    while idx < len(tok):
                        ch = tok[idx]
                        if ch == "t":
                            idx += 1
                            continue
                        if ch in {"h", "V"}:
                            is_terminal = True
                            tokens.clear()
                            break
                        if ch in {"p", "P", "u"}:
                            is_terminal = True
                            tokens.clear()
                            break
                        if ch in {"c", "n"}:
                            rest = tok[idx + 1 :]
                            if rest:
                                operand = rest
                                if _has_shell_expansion(operand) or operand == "$":
                                    raise ValueError(
                                        f"ionice option operand contains shell expansion: {operand!r}"
                                    )
                                if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                                    raise ValueError(
                                        f"ionice option operand contains shell expansion: {tokens[0]!r}"
                                    )
                            else:
                                if not tokens:
                                    is_terminal = True
                                    tokens.clear()
                                    break
                                operand = tokens.pop(0)
                                if _has_shell_expansion(operand) or operand == "$":
                                    raise ValueError(
                                        f"ionice option operand contains shell expansion: {operand!r}"
                                    )
                                if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                                    raise ValueError(
                                        f"ionice option operand contains shell expansion: {tokens[0]!r}"
                                    )
                            break
                        raise ValueError(f"Unknown ionice option: -{ch}")
                    if is_terminal:
                        break
                    continue

                break

            if is_terminal:
                tokens.clear()
            continue

        if cmd_word == "watch":
            tokens.pop(0)
            is_terminal = False
            exec_mode = False
            while tokens:
                t = tokens[0]
                if t == "--":
                    tokens.pop(0)
                    break
                if t == "-":
                    break
                if t.startswith("--"):
                    if t in {"--help", "--version"}:
                        is_terminal = True
                        tokens.clear()
                        break
                    if t in {
                        "--beep",
                        "--color",
                        "--no-color",
                        "--errexit",
                        "--follow",
                        "--chgexit",
                        "--precise",
                        "--no-rerun",
                        "--no-title",
                        "--no-wrap",
                    }:
                        tokens.pop(0)
                        continue
                    if t == "--exec":
                        exec_mode = True
                        tokens.pop(0)
                        continue
                    if t == "--differences":
                        tokens.pop(0)
                        continue
                    if t.startswith("--differences="):
                        val = t.split("=", 1)[1]
                        if _has_shell_expansion(val) or val == "$":
                            raise ValueError(
                                f"watch option operand contains shell expansion: {val!r}"
                            )
                        tokens.pop(0)
                        if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                            raise ValueError(
                                f"watch option operand contains shell expansion: {tokens[0]!r}"
                            )
                        continue
                    if t.startswith(("--interval=", "--equexit=", "--shotsdir=")):
                        val = t.split("=", 1)[1]
                        if _has_shell_expansion(val) or val == "$":
                            raise ValueError(
                                f"watch option operand contains shell expansion: {val!r}"
                            )
                        tokens.pop(0)
                        if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                            raise ValueError(
                                f"watch option operand contains shell expansion: {tokens[0]!r}"
                            )
                        continue
                    if t in {"--interval", "--equexit", "--shotsdir"}:
                        tokens.pop(0)
                        if not tokens:
                            is_terminal = True
                            tokens.clear()
                            break
                        val = tokens.pop(0)
                        if _has_shell_expansion(val) or val == "$":
                            raise ValueError(
                                f"watch option operand contains shell expansion: {val!r}"
                            )
                        if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                            raise ValueError(
                                f"watch option operand contains shell expansion: {tokens[0]!r}"
                            )
                        continue
                    raise ValueError(f"Unknown watch option: {t!r}")

                if t.startswith("-") and len(t) > 1:
                    tok = tokens.pop(0)
                    idx = 1
                    while idx < len(tok):
                        ch = tok[idx]
                        if ch in {"h", "v"}:
                            is_terminal = True
                            tokens.clear()
                            break
                        if ch in {"b", "c", "C", "e", "f", "g", "p", "r", "t", "w"}:
                            idx += 1
                            continue
                        if ch == "x":
                            exec_mode = True
                            idx += 1
                            continue
                        if ch == "d":
                            rest = tok[idx + 1 :]
                            if rest:
                                operand = rest
                                if _has_shell_expansion(operand) or operand == "$":
                                    raise ValueError(
                                        f"watch option operand contains shell expansion: {operand!r}"
                                    )
                                if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                                    raise ValueError(
                                        f"watch option operand contains shell expansion: {tokens[0]!r}"
                                    )
                                break
                            if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                                raise ValueError(
                                    f"watch option operand contains shell expansion: {tokens[0]!r}"
                                )
                            idx += 1
                            continue
                        if ch in {"n", "q", "s"}:
                            rest = tok[idx + 1 :]
                            if rest:
                                operand = rest
                                if _has_shell_expansion(operand) or operand == "$":
                                    raise ValueError(
                                        f"watch option operand contains shell expansion: {operand!r}"
                                    )
                                if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                                    raise ValueError(
                                        f"watch option operand contains shell expansion: {tokens[0]!r}"
                                    )
                            else:
                                if not tokens:
                                    is_terminal = True
                                    tokens.clear()
                                    break
                                operand = tokens.pop(0)
                                if _has_shell_expansion(operand) or operand == "$":
                                    raise ValueError(
                                        f"watch option operand contains shell expansion: {operand!r}"
                                    )
                                if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                                    raise ValueError(
                                        f"watch option operand contains shell expansion: {tokens[0]!r}"
                                    )
                            break
                        raise ValueError(f"Unknown watch option: -{ch}")
                    if is_terminal:
                        break
                    continue

                break

            if is_terminal:
                tokens.clear()
                continue

            if not tokens:
                continue

            for payload_tok in tokens:
                if _has_shell_expansion(payload_tok) or payload_tok == "$":
                    raise ValueError(
                        f"watch command contains shell expansion: {payload_tok!r}"
                    )

            if not exec_mode:
                cmd_str = " ".join(_restore_sentinels(t) for t in tokens)
                tokens = ["sh", "-c", cmd_str]
                break

            continue

        if cmd_word == "flock":
            tokens.pop(0)
            is_terminal = False
            has_fd = False
            while tokens:
                t = tokens[0]
                if t == "--":
                    tokens.pop(0)
                    break
                if t == "-":
                    break
                if t in {"-c", "--command"}:
                    if not has_fd:
                        is_terminal = True
                        tokens.clear()
                        break
                    tokens.pop(0)
                    if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                        raise ValueError(
                            f"flock command contains shell expansion: {tokens[0]!r}"
                        )
                    if len(tokens) != 1:
                        is_terminal = True
                        tokens.clear()
                        break
                    cmd_str = tokens[0]
                    if _has_shell_expansion(cmd_str) or cmd_str == "$":
                        raise ValueError(
                            f"flock command contains shell expansion: {cmd_str!r}"
                        )
                    tokens = ["sh", "-c", _restore_sentinels(cmd_str)]
                    break
                if t.startswith("--"):
                    if t in FLOCK_TERMINAL_LONG_OPTS:
                        is_terminal = True
                        tokens.clear()
                        break
                    if t in FLOCK_NO_ARG_LONG_OPTS:
                        tokens.pop(0)
                        continue
                    if any(t.startswith(f"{opt}=") for opt in FLOCK_REQ_ARG_LONG_OPTS):
                        opt_name, val = t.split("=", 1)
                        if _has_shell_expansion(val) or val == "$":
                            raise ValueError(
                                f"flock option operand contains shell expansion: {val!r}"
                            )
                        tokens.pop(0)
                        if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                            raise ValueError(
                                f"flock option operand contains shell expansion: {tokens[0]!r}"
                            )
                        if opt_name == "--fd":
                            has_fd = True
                        continue
                    if t in FLOCK_REQ_ARG_LONG_OPTS:
                        opt_name = tokens.pop(0)
                        if not tokens:
                            is_terminal = True
                            tokens.clear()
                            break
                        val = tokens.pop(0)
                        if _has_shell_expansion(val) or val == "$":
                            raise ValueError(
                                f"flock option operand contains shell expansion: {val!r}"
                            )
                        if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                            raise ValueError(
                                f"flock option operand contains shell expansion: {tokens[0]!r}"
                            )
                        if opt_name == "--fd":
                            has_fd = True
                        continue
                    raise ValueError(f"Unknown flock option: {t!r}")

                if t.startswith("-") and len(t) > 1:
                    tok = tokens.pop(0)
                    idx = 1
                    while idx < len(tok):
                        ch = tok[idx]
                        if ch in FLOCK_TERMINAL_SHORT_OPTS:
                            is_terminal = True
                            tokens.clear()
                            break
                        if ch in FLOCK_NO_ARG_SHORT_OPTS:
                            idx += 1
                            continue
                        if ch in FLOCK_REQ_ARG_SHORT_OPTS:
                            rest = tok[idx + 1 :]
                            if rest:
                                operand = rest
                                if _has_shell_expansion(operand) or operand == "$":
                                    raise ValueError(
                                        f"flock option operand contains shell expansion: {operand!r}"
                                    )
                                if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                                    raise ValueError(
                                        f"flock option operand contains shell expansion: {tokens[0]!r}"
                                    )
                            else:
                                if not tokens:
                                    is_terminal = True
                                    tokens.clear()
                                    break
                                operand = tokens.pop(0)
                                if _has_shell_expansion(operand) or operand == "$":
                                    raise ValueError(
                                        f"flock option operand contains shell expansion: {operand!r}"
                                    )
                                if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                                    raise ValueError(
                                        f"flock option operand contains shell expansion: {tokens[0]!r}"
                                    )
                            break
                        raise ValueError(f"Unknown flock option: -{ch}")
                    if is_terminal:
                        break
                    continue

                break

            if is_terminal:
                tokens.clear()
                continue

            if tokens and tokens[0] == "sh" and len(tokens) == 3 and tokens[1] == "-c":
                break

            if not tokens:
                continue

            if has_fd:
                for payload_tok in tokens:
                    if _has_shell_expansion(payload_tok) or payload_tok == "$":
                        raise ValueError(
                            f"flock command contains shell expansion: {payload_tok!r}"
                        )
                continue

            lock_target = tokens.pop(0)
            if _has_shell_expansion(lock_target) or lock_target == "$":
                raise ValueError(
                    f"flock lock target contains shell expansion: {lock_target!r}"
                )

            if not tokens:
                continue

            if tokens[0] in {"-c", "--command"}:
                tokens.pop(0)
                if tokens and (_has_shell_expansion(tokens[0]) or tokens[0] == "$"):
                    raise ValueError(
                        f"flock command contains shell expansion: {tokens[0]!r}"
                    )
                if len(tokens) != 1:
                    tokens.clear()
                    continue
                cmd_str = tokens[0]
                if _has_shell_expansion(cmd_str) or cmd_str == "$":
                    raise ValueError(
                        f"flock command contains shell expansion: {cmd_str!r}"
                    )
                tokens = ["sh", "-c", _restore_sentinels(cmd_str)]
                break

            for payload_tok in tokens:
                if _has_shell_expansion(payload_tok) or payload_tok == "$":
                    raise ValueError(
                        f"flock command contains shell expansion: {payload_tok!r}"
                    )

            continue

        if cmd_word == "xargs":
            tokens = _unwrap_xargs(tokens)
            continue

        break

    return env_vars, tokens


class _ShellState:
    """Explicit shell state representation tracking shell variables, export status, allexport, and alias expansion."""

    __slots__ = (
        "allexport",
        "defined_aliases",
        "expand_aliases",
        "exported_keys",
        "shell_vars",
    )

    def __init__(
        self,
        inherited_env: dict[str, str] | None = None,
        shell_vars: dict[str, str] | None = None,
        exported_keys: set[str] | None = None,
        allexport: bool = False,
        expand_aliases: bool = False,
        defined_aliases: set[str] | None = None,
    ) -> None:
        self.shell_vars: dict[str, str] = (
            dict(shell_vars) if shell_vars is not None else {}
        )
        self.exported_keys: set[str] = (
            set(exported_keys) if exported_keys is not None else set()
        )
        self.allexport: bool = allexport
        self.expand_aliases: bool = expand_aliases
        self.defined_aliases: set[str] = (
            set(defined_aliases) if defined_aliases is not None else set()
        )
        if inherited_env:
            for k, v in inherited_env.items():
                if _is_git_config_protocol_key(k):
                    self.shell_vars[k] = v
                    self.exported_keys.add(k)

    def apply_assignment(
        self,
        name: str,
        val: str,
        is_append: bool,
        mark_exported: bool | None = None,
    ) -> None:
        """Apply variable assignment (set or append) to shell state.

        If mark_exported is True, marks key as exported.
        If mark_exported is False, marks key as unexported.
        If mark_exported is None, retains current export status (or exports if allexport is active).
        """
        if not _is_git_config_protocol_key(name):
            return

        if is_append:
            if name not in self.shell_vars:
                raise ValueError(
                    f"Missing prior value for append assignment to Git config protocol key {name!r}"
                )
            prior = self.shell_vars[name]
            new_val = prior + val
        else:
            new_val = val

        self.shell_vars[name] = new_val

        if mark_exported is True:
            self.exported_keys.add(name)
        elif mark_exported is False:
            self.exported_keys.discard(name)
        elif mark_exported is None and self.allexport:
            self.exported_keys.add(name)

    def get_exported_env(self) -> dict[str, str]:
        """Return the dictionary of currently exported Git config protocol environment variables."""
        return {
            k: self.shell_vars[k]
            for k in self.exported_keys
            if k in self.shell_vars
        }

    def copy(self) -> _ShellState:
        """Create a shallow copy of the shell state."""
        return _ShellState(
            shell_vars=self.shell_vars,
            exported_keys=self.exported_keys,
            allexport=self.allexport,
            expand_aliases=self.expand_aliases,
            defined_aliases=self.defined_aliases,
        )


def _unwrap_builtin_wrappers(tokens: list[str]) -> list[str]:
    """Unwrap shell builtin executable wrappers (builtin, command [-p] [--], literal time).

    Does NOT unwrap query wrappers (command -v / command -V) or non-shell wrappers (exec, nohup, sudo, env, /usr/bin/time).
    """
    tok_list = list(tokens)
    while tok_list:
        tok0 = tok_list[0]
        if tok0 == "builtin":
            tok_list.pop(0)
            if tok_list and tok_list[0] == "--":
                tok_list.pop(0)
            continue

        if tok0 == "command":
            idx = 1
            has_query = False
            while idx < len(tok_list) and tok_list[idx].startswith("-"):
                opt = tok_list[idx]
                if opt == "--":
                    idx += 1
                    break
                if any(c in "vV" for c in opt[1:]):
                    has_query = True
                    break
                idx += 1
            if has_query:
                return tokens
            tok_list = tok_list[idx:]
            continue

        if tok0 == "time":
            tok_list.pop(0)
            if tok_list and tok_list[0] == "-p":
                tok_list.pop(0)
            if tok_list and tok_list[0] == "--":
                tok_list.pop(0)
            continue

        break

    return tok_list


def _is_all_var_assignments(tokens: list[str]) -> bool:
    """Return True if tokens consist entirely of variable assignments and redirections with no command word."""
    if not tokens:
        return False
    toks = list(tokens)
    has_assignment = False
    while toks:
        asgn = _consume_var_assignment(toks)
        if asgn is not None:
            has_assignment = True
            continue
        tok0 = toks[0]
        if _is_redirection(tok0):
            if tok0 in {
                ">",
                ">>",
                "<",
                "<>",
                ">&",
                "<&",
                "&>",
                ">|",
                "1>",
                "2>",
                "1>>",
                "2>>",
                "<<",
                "<<-",
                "<<<",
                "0<",
                "0<<",
                "0<<-",
                "1<<",
                "1<<-",
                "2<<",
                "2<<-",
            } or (len(tok0) >= 2 and tok0[0].isdigit()):
                toks.pop(0)
                if toks:
                    toks.pop(0)
                continue
            toks.pop(0)
            continue
        if tok0 in {"2>&1", "1>&2", ">&1", ">&2"}:
            toks.pop(0)
            continue
        return False
    return has_assignment


def _parse_state_mutation_operand(
    tokens: list[str],
) -> tuple[str, str | None, bool, bool] | None:
    """Parse the next operand from state mutation command tokens.

    Returns (raw_name, val, is_append, is_dynamic_name) if an operand was consumed,
    or None if tokens is empty.
    - If assignment (NAME=val or NAME+=val), val is str, is_append indicates +=.
    - If bare variable name (NAME), val is None, is_append is False.
    - is_dynamic_name is True if raw_name contains any shell expansion ($ or ` or sentinels).
    """
    if not tokens:
        return None

    tok0 = tokens[0]

    if "+=" in tok0:
        tokens.pop(0)
        lhs, rhs = tok0.split("+=", 1)
        val = rhs
        if val == "" and tokens:
            if tokens[0] == "$":
                tokens.pop(0)
                if tokens:
                    val = "$" + tokens.pop(0)
                else:
                    val = "$"
            elif tokens[0].startswith("$") or tokens[0].startswith("`"):
                val = tokens.pop(0)
        while tokens and tokens[0] == ":":
            tokens.pop(0)
            if tokens:
                val = val + ":" + tokens.pop(0)
        is_dynamic = _has_shell_expansion(lhs)
        return lhs, _restore_sentinels(val), True, is_dynamic

    if "=" in tok0:
        tokens.pop(0)
        lhs, rhs = tok0.split("=", 1)
        val = rhs
        if val == "" and tokens:
            if tokens[0] == "$":
                tokens.pop(0)
                if tokens:
                    val = "$" + tokens.pop(0)
                else:
                    val = "$"
            elif tokens[0].startswith("$") or tokens[0].startswith("`"):
                val = tokens.pop(0)
        while tokens and tokens[0] == ":":
            tokens.pop(0)
            if tokens:
                val = val + ":" + tokens.pop(0)
        is_dynamic = _has_shell_expansion(lhs)
        return lhs, _restore_sentinels(val), False, is_dynamic

    if len(tokens) >= 2 and tokens[0].endswith("+") and tokens[1] == "=":
        lhs = tokens.pop(0)[:-1]
        tokens.pop(0)
        val = ""
        if tokens:
            if tokens[0] == "$":
                tokens.pop(0)
                if tokens:
                    val = "$" + tokens.pop(0)
                else:
                    val = "$"
            elif not _is_redirection(tokens[0]) and tokens[0] not in COMMAND_SEPARATORS:
                val = tokens.pop(0)
        while tokens and tokens[0] == ":":
            tokens.pop(0)
            if tokens:
                val = val + ":" + tokens.pop(0)
        is_dynamic = _has_shell_expansion(lhs)
        return lhs, _restore_sentinels(val), True, is_dynamic

    if len(tokens) >= 2 and tokens[1] == "+=":
        lhs = tokens.pop(0)
        tokens.pop(0)
        val = ""
        if tokens:
            if tokens[0] == "$":
                tokens.pop(0)
                if tokens:
                    val = "$" + tokens.pop(0)
                else:
                    val = "$"
            elif not _is_redirection(tokens[0]) and tokens[0] not in COMMAND_SEPARATORS:
                val = tokens.pop(0)
        while tokens and tokens[0] == ":":
            tokens.pop(0)
            if tokens:
                val = val + ":" + tokens.pop(0)
        is_dynamic = _has_shell_expansion(lhs)
        return lhs, _restore_sentinels(val), True, is_dynamic

    if len(tokens) >= 3 and tokens[1] == "+" and tokens[2] == "=":
        lhs = tokens.pop(0)
        tokens.pop(0)
        tokens.pop(0)
        val = ""
        if tokens:
            if tokens[0] == "$":
                tokens.pop(0)
                if tokens:
                    val = "$" + tokens.pop(0)
                else:
                    val = "$"
            elif not _is_redirection(tokens[0]) and tokens[0] not in COMMAND_SEPARATORS:
                val = tokens.pop(0)
        while tokens and tokens[0] == ":":
            tokens.pop(0)
            if tokens:
                val = val + ":" + tokens.pop(0)
        is_dynamic = _has_shell_expansion(lhs)
        return lhs, _restore_sentinels(val), True, is_dynamic

    if len(tokens) >= 2 and tokens[1] == "=":
        lhs = tokens.pop(0)
        tokens.pop(0)
        val = ""
        if tokens:
            if tokens[0] == "$":
                tokens.pop(0)
                if tokens:
                    val = "$" + tokens.pop(0)
                else:
                    val = "$"
            elif not _is_redirection(tokens[0]) and tokens[0] not in COMMAND_SEPARATORS:
                val = tokens.pop(0)
        while tokens and tokens[0] == ":":
            tokens.pop(0)
            if tokens:
                val = val + ":" + tokens.pop(0)
        is_dynamic = _has_shell_expansion(lhs)
        return lhs, _restore_sentinels(val), False, is_dynamic

    raw_tok = tokens.pop(0)
    restored = _restore_sentinels(raw_tok)
    is_dynamic = _has_shell_expansion(raw_tok)
    return restored, None, False, is_dynamic


def _apply_export_cmd(args: list[str], state: _ShellState) -> None:
    """Apply export command arguments to shell state."""
    is_unexport = False
    unsupported_options = False
    opt_args: list[str] = []

    i = 0
    while i < len(args):
        item = args[i]
        if item == "--":
            opt_args.extend(args[i + 1 :])
            break
        if item.startswith("-") and item != "-" and not _is_var_assignment(item):
            opt_chars = set(item[1:])
            if opt_chars and opt_chars.issubset({"n", "p"}):
                if "n" in opt_chars:
                    is_unexport = True
            else:
                unsupported_options = True
            i += 1
        else:
            opt_args.extend(args[i:])
            break

    while opt_args:
        parsed = _parse_state_mutation_operand(opt_args)
        if parsed is None:
            break
        name, val, is_append, is_dynamic = parsed
        if is_dynamic:
            raise ValueError(
                f"Dynamic variable name in export operand is not supported: {name!r}"
            )

        if _is_git_config_protocol_key(name):
            if unsupported_options:
                raise ValueError(
                    f"Unsupported export option shape targeting Git config protocol key {name!r}"
                )
            if val is not None:
                state.apply_assignment(
                    name, val, is_append, mark_exported=not is_unexport
                )
            else:
                if is_unexport:
                    state.exported_keys.discard(name)
                else:
                    if name in state.shell_vars:
                        state.exported_keys.add(name)
                    else:
                        raise ValueError(
                            f"Export of Git config protocol key {name!r} without literal assignment is not supported"
                        )


def _apply_unset_cmd(args: list[str], state: _ShellState) -> None:
    """Apply unset command arguments to shell state."""
    unsupported_options = False
    opt_args: list[str] = []

    i = 0
    while i < len(args):
        item = args[i]
        if item == "--":
            opt_args.extend(args[i + 1 :])
            break
        if item.startswith("-") and item != "-":
            opt_chars = set(item[1:])
            if opt_chars and opt_chars.issubset({"v"}):
                pass
            else:
                unsupported_options = True
            i += 1
        else:
            opt_args.extend(args[i:])
            break

    while opt_args:
        parsed = _parse_state_mutation_operand(opt_args)
        if parsed is None:
            break
        name, _val, _is_append, is_dynamic = parsed
        if is_dynamic:
            raise ValueError(
                f"Dynamic variable name in unset operand is not supported: {name!r}"
            )

        if _is_git_config_protocol_key(name):
            if unsupported_options:
                raise ValueError(
                    f"Unsupported unset option shape targeting Git config protocol key {name!r}"
                )
            state.shell_vars.pop(name, None)
            state.exported_keys.discard(name)


def _apply_declare_typeset_cmd(args: list[str], state: _ShellState) -> None:
    """Apply declare or typeset command arguments (Bash/Zsh) to shell state."""
    is_export = False
    is_unexport = False
    unsupported_options = False
    opt_args: list[str] = []

    i = 0
    while i < len(args):
        item = args[i]
        if item == "--":
            opt_args.extend(args[i + 1 :])
            break
        if item.startswith(("-", "+")) and len(item) > 1 and not _is_var_assignment(item):
            prefix = item[0]
            opt_chars = set(item[1:])
            if opt_chars and opt_chars.issubset({"x", "g", "p"}):
                if prefix == "-" and "x" in opt_chars:
                    is_export = True
                elif prefix == "+" and "x" in opt_chars:
                    is_unexport = True
            else:
                unsupported_options = True
            i += 1
        else:
            opt_args.extend(args[i:])
            break

    while opt_args:
        parsed = _parse_state_mutation_operand(opt_args)
        if parsed is None:
            break
        name, val, is_append, is_dynamic = parsed
        if is_dynamic:
            raise ValueError(
                f"Dynamic variable name in declare/typeset operand is not supported: {name!r}"
            )

        if _is_git_config_protocol_key(name):
            if unsupported_options:
                raise ValueError(
                    f"Unsupported declare/typeset option shape targeting Git config protocol key {name!r}"
                )
            if val is not None:
                if is_export:
                    state.apply_assignment(name, val, is_append, mark_exported=True)
                elif is_unexport:
                    state.apply_assignment(name, val, is_append, mark_exported=False)
                else:
                    state.apply_assignment(name, val, is_append, mark_exported=None)
            else:
                if is_export:
                    if name in state.shell_vars:
                        state.exported_keys.add(name)
                    else:
                        raise ValueError(
                            f"Export of Git config protocol key {name!r} without literal assignment is not supported"
                        )
                elif is_unexport:
                    state.exported_keys.discard(name)


def _apply_readonly_local_cmd(cmd: str, args: list[str], state: _ShellState) -> None:
    """Apply readonly or local command arguments to shell state."""
    has_export = False
    opt_args: list[str] = []

    i = 0
    while i < len(args):
        item = args[i]
        if item == "--":
            opt_args.extend(args[i + 1 :])
            break
        if item.startswith(("-", "+")) and len(item) > 1 and not _is_var_assignment(item):
            prefix = item[0]
            opt_chars = set(item[1:])
            if prefix == "-" and "x" in opt_chars:
                has_export = True
            i += 1
        else:
            opt_args.extend(args[i:])
            break

    while opt_args:
        parsed = _parse_state_mutation_operand(opt_args)
        if parsed is None:
            break
        name, val, is_append, is_dynamic = parsed
        if is_dynamic:
            raise ValueError(
                f"Dynamic variable name in {cmd} operand is not supported: {name!r}"
            )

        if _is_git_config_protocol_key(name):
            if has_export:
                raise ValueError(
                    f"{cmd} with export flag targeting Git config protocol key {name!r} is not supported"
                )
            if val is not None:
                state.apply_assignment(name, val, is_append, mark_exported=None)
            else:
                if name not in state.shell_vars:
                    raise ValueError(
                        f"{cmd} of Git config protocol key {name!r} without literal assignment is not supported"
                    )


def _apply_set_cmd(args: list[str], state: _ShellState) -> None:
    """Apply set command arguments (POSIX allexport and Fish variable manipulation) to shell state."""
    is_fish_export = False
    is_fish_unexport = False
    is_fish_erase = False
    is_allexport_enable = False
    is_allexport_disable = False
    unsupported_options = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if arg == "-o" and i + 1 < len(args):
            opt_name = args[i + 1]
            if opt_name == "allexport":
                is_allexport_enable = True
            i += 2
            continue
        if arg == "+o" and i + 1 < len(args):
            opt_name = args[i + 1]
            if opt_name == "allexport":
                is_allexport_disable = True
            i += 2
            continue
        if arg.startswith(("-", "+")) and len(arg) > 1:
            prefix = arg[0]
            if arg.startswith("--"):
                if arg == "--export":
                    is_fish_export = True
                elif arg == "--unexport":
                    is_fish_unexport = True
                elif arg == "--erase":
                    is_fish_erase = True
                elif arg in {"--global", "--local", "--universal"}:
                    pass
                else:
                    unsupported_options = True
                i += 1
                continue

            flags = arg[1:]
            if prefix == "-" and flags == "a":
                is_allexport_enable = True
                i += 1
                continue
            if prefix == "+" and flags == "a":
                is_allexport_disable = True
                i += 1
                continue

            recognized = True
            for c in flags:
                if prefix == "-":
                    if c in "gxU":
                        if c == "x":
                            is_fish_export = True
                    elif c == "u":
                        is_fish_unexport = True
                    elif c == "e":
                        is_fish_erase = True
                    elif c in "la":
                        if c == "a":
                            is_allexport_enable = True
                    else:
                        recognized = False
                elif prefix == "+":
                    if c == "a":
                        is_allexport_disable = True
                    elif c == "x":
                        is_fish_unexport = True
                    else:
                        recognized = False
            if not recognized:
                unsupported_options = True
            i += 1
            continue
        break

    if is_allexport_enable:
        state.allexport = True
    if is_allexport_disable:
        state.allexport = False

    operands = args[i:]
    if operands:
        if _has_shell_expansion(operands[0]):
            raise ValueError(
                f"Dynamic variable name in set operand is not supported: {operands[0]!r}"
            )
        var_name = _restore_sentinels(operands[0])
        var_values = [_restore_sentinels(v) for v in operands[1:]]
        if _is_git_config_protocol_key(var_name):
            if unsupported_options:
                raise ValueError(
                    f"Unsupported set option shape targeting Git config protocol key {var_name!r}"
                )
            if is_fish_erase:
                state.shell_vars.pop(var_name, None)
                state.exported_keys.discard(var_name)
            elif is_fish_unexport:
                if len(var_values) > 1:
                    raise ValueError(
                        f"Fish set targeting Git config protocol key {var_name!r} with multiple values is not supported"
                    )
                if len(var_values) == 1:
                    val = var_values[0]
                    state.shell_vars[var_name] = val
                state.exported_keys.discard(var_name)
            elif is_fish_export:
                if len(var_values) > 1:
                    raise ValueError(
                        f"Fish set targeting Git config protocol key {var_name!r} with multiple values is not supported"
                    )
                if len(var_values) == 1:
                    val = var_values[0]
                    state.shell_vars[var_name] = val
                    state.exported_keys.add(var_name)
                elif len(var_values) == 0:
                    if var_name in state.shell_vars:
                        state.exported_keys.add(var_name)
                    else:
                        raise ValueError(
                            f"Export of Git config protocol key {var_name!r} without literal value is not supported"
                        )
            else:
                if len(var_values) > 1:
                    if var_name in state.exported_keys or state.allexport:
                        raise ValueError(
                            f"Fish set targeting Git config protocol key {var_name!r} with multiple values is not supported"
                        )
                elif len(var_values) == 1:
                    val = var_values[0]
                    state.shell_vars[var_name] = val
                    if state.allexport:
                        state.exported_keys.add(var_name)


def _apply_shopt_cmd(args: list[str], state: _ShellState) -> None:
    """Apply shopt command arguments (tracking expand_aliases) to shell state."""
    if not args:
        return

    is_enable = False
    is_disable = False
    opt_names: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            opt_names.extend(args[i + 1 :])
            break
        if arg.startswith("-") and len(arg) > 1:
            flags = arg[1:]
            for c in flags:
                if c == "s":
                    is_enable = True
                elif c == "u":
                    is_disable = True
            i += 1
            continue
        opt_names.extend(args[i:])
        break

    for opt in opt_names:
        if _has_shell_expansion(opt):
            raise ValueError(
                f"Dynamic option name in shopt operand is not supported: {opt!r}"
            )
        opt_clean = _restore_sentinels(opt).strip()
        if opt_clean == "expand_aliases":
            if is_enable:
                if state.defined_aliases:
                    raise ValueError(
                        "Shell alias expansion enabled after literal alias definitions"
                    )
                state.expand_aliases = True
            elif is_disable:
                state.expand_aliases = False


def _extract_alias_definitions(args: list[str]) -> list[tuple[str, str]]:
    """Extract alias definitions (name, value) from alias command arguments.

    Returns a list of (name, val) definitions. Ignores query forms (bare names or flags).
    """
    definitions: list[tuple[str, str]] = []
    i = 0
    parsing_options = True
    while i < len(args):
        arg = args[i]
        if parsing_options and arg == "--":
            parsing_options = False
            i += 1
            continue
        if parsing_options and arg == "-p":
            i += 1
            continue
        if _has_shell_expansion(arg):
            raise ValueError(
                f"Dynamic alias operand is not supported: {arg!r}"
            )
        if "=" in arg:
            name, val = arg.split("=", 1)
            name_clean = name.rstrip("+").strip()
            if name_clean:
                definitions.append((name_clean, val))
            i += 1
            continue
        if i + 1 < len(args) and args[i + 1] == "=":
            name_clean = arg.rstrip("+").strip()
            val = args[i + 2] if i + 2 < len(args) else ""
            if name_clean:
                definitions.append((name_clean, val))
            i += 3
            continue
        if i + 1 < len(args) and args[i + 1].startswith("="):
            name_clean = arg.rstrip("+").strip()
            val = args[i + 1][1:]
            if name_clean:
                definitions.append((name_clean, val))
            i += 2
            continue
        i += 1
    return definitions


def _apply_alias_cmd(args: list[str], state: _ShellState) -> None:
    """Apply alias command arguments to shell state."""
    definitions = _extract_alias_definitions(args)
    if not definitions:
        return

    if state.expand_aliases:
        raise ValueError(
            "Literal shell alias defined while alias expansion is enabled"
        )

    for name, _val in definitions:
        state.defined_aliases.add(name)


def _apply_unalias_cmd(args: list[str], state: _ShellState) -> None:
    """Apply unalias command arguments to shell state."""
    if not args:
        return
    i = 0
    parsing_options = True
    while i < len(args):
        arg = args[i]
        if parsing_options and arg == "--":
            parsing_options = False
            i += 1
            continue
        if parsing_options and arg == "-a":
            state.defined_aliases.clear()
            i += 1
            continue
        if _has_shell_expansion(arg):
            raise ValueError(
                f"Dynamic unalias operand is not supported: {arg!r}"
            )
        state.defined_aliases.discard(_restore_sentinels(arg))
        i += 1


def _apply_shell_state_segment(
    tokens: list[str],
    state: _ShellState,
) -> bool:
    """Recognize shell state mutation commands and update Git config protocol keys in state.

    Returns True if the segment was recognized as a shell state mutation segment.
    Raises ValueError if an exact Git config protocol key has an unknowable value or unsupported options.
    """
    if not tokens:
        return False

    if _is_all_var_assignments(tokens):
        toks = list(tokens)
        while toks:
            asgn = _consume_var_assignment(toks)
            if asgn is not None:
                name, val, is_append = asgn
                if _is_git_config_protocol_key(name):
                    state.apply_assignment(name, val, is_append, mark_exported=None)
                continue
            tok0 = toks.pop(0)
            if (
                tok0
                in {
                    ">",
                    ">>",
                    "<",
                    "<>",
                    ">&",
                    "<&",
                    "&>",
                    ">|",
                    "1>",
                    "2>",
                    "1>>",
                    "2>>",
                    "<<",
                    "<<-",
                    "<<<",
                    "0<",
                    "0<<",
                    "0<<-",
                    "1<<",
                    "1<<-",
                    "2<<",
                    "2<<-",
                }
                or (len(tok0) >= 2 and tok0[0].isdigit())
            ) and toks:
                toks.pop(0)
        return True

    unwrapped = _unwrap_builtin_wrappers(tokens)
    if not unwrapped:
        return False

    cmd = os.path.basename(unwrapped[0])
    args = unwrapped[1:]

    if cmd == "export":
        _apply_export_cmd(args, state)
        return True

    if cmd == "unset":
        _apply_unset_cmd(args, state)
        return True

    if cmd in {"declare", "typeset"}:
        _apply_declare_typeset_cmd(args, state)
        return True

    if cmd == "set":
        _apply_set_cmd(args, state)
        return True

    if cmd in {"readonly", "local"}:
        _apply_readonly_local_cmd(cmd, args, state)
        return True

    if cmd == "shopt":
        _apply_shopt_cmd(args, state)
        return True

    if cmd == "alias":
        _apply_alias_cmd(args, state)
        return True

    if cmd == "unalias":
        _apply_unalias_cmd(args, state)
        return True

    return False


class _CommandSegment:
    """Represents a single command segment and its adjacent boundary operators."""

    __slots__ = ("following_op", "preceding_op", "subshell_depth", "tokens")

    def __init__(
        self,
        tokens: list[str],
        preceding_op: str | None,
        following_op: str | None,
        subshell_depth: int,
    ) -> None:
        self.tokens = tokens
        self.preceding_op = preceding_op
        self.following_op = following_op
        self.subshell_depth = subshell_depth


SEPARATORS_UNCONDITIONAL = {";", "\n"}
SEPARATORS_CONDITIONAL = {"&&", "||"}
SEPARATORS_PIPELINE = {"|", "|&"}
SEPARATORS_BACKGROUND = {"&"}
SEPARATORS_CASE = {";;", ";&", ";;&"}
ALL_SEPARATORS = (
    SEPARATORS_UNCONDITIONAL
    | SEPARATORS_CONDITIONAL
    | SEPARATORS_PIPELINE
    | SEPARATORS_BACKGROUND
    | SEPARATORS_CASE
)


def _parse_command_segments(tokens: list[str]) -> list[_CommandSegment]:
    """Parse a token stream into command segments tracking boundaries and subshell depth."""
    normalized_tokens = _normalize_raw_tokens(tokens)
    segments: list[_CommandSegment] = []
    current_tokens: list[str] = []
    subshell_depth = 0
    current_preceding_op: str | None = None
    i = 0
    token_list = list(normalized_tokens)

    while i < len(token_list):
        tok = token_list[i]

        if (
            tok == "$"
            and i + 1 < len(token_list)
            and _is_all_parens(token_list[i + 1])
            and token_list[i + 1].startswith("(")
        ):
            next_tok = token_list[i + 1]
            paren_depth = 0
            for char in next_tok:
                if char == "(":
                    paren_depth += 1
                elif char == ")":
                    paren_depth -= 1
            if paren_depth <= 0:
                raise ValueError(f"Malformed command substitution: '${next_tok}'")
            current_tokens.append("$")
            current_tokens.append(next_tok)
            i += 2
            while i < len(token_list) and paren_depth > 0:
                inner_tok = token_list[i]
                if _is_all_parens(inner_tok):
                    closed_idx = None
                    for p_idx, char in enumerate(inner_tok):
                        if char == "(":
                            paren_depth += 1
                        elif char == ")":
                            paren_depth -= 1
                            if paren_depth == 0:
                                closed_idx = p_idx
                                break
                    if closed_idx is not None:
                        part_consumed = inner_tok[: closed_idx + 1]
                        current_tokens.append(part_consumed)
                        remainder = inner_tok[closed_idx + 1 :]
                        i += 1
                        if remainder:
                            token_list.insert(i, remainder)
                        break
                    else:
                        current_tokens.append(inner_tok)
                        i += 1
                else:
                    current_tokens.append(inner_tok)
                    i += 1
            if paren_depth > 0:
                raise ValueError("Unmatched '$(' in command substitution")
            continue

        if _is_all_parens(tok):
            for char in tok:
                if char == "(":
                    if current_tokens:
                        segments.append(
                            _CommandSegment(
                                tokens=current_tokens,
                                preceding_op=current_preceding_op,
                                following_op="(",
                                subshell_depth=subshell_depth,
                            )
                        )
                        current_tokens = []
                    current_preceding_op = "("
                    subshell_depth += 1
                elif char == ")":
                    if subshell_depth <= 0:
                        raise ValueError("Unmatched closing parenthesis in command")
                    if current_tokens:
                        segments.append(
                            _CommandSegment(
                                tokens=current_tokens,
                                preceding_op=current_preceding_op,
                                following_op=")",
                                subshell_depth=subshell_depth,
                            )
                        )
                        current_tokens = []
                    current_preceding_op = ")"
                    subshell_depth -= 1
            i += 1
            continue

        if tok in ALL_SEPARATORS:
            if current_tokens:
                segments.append(
                    _CommandSegment(
                        tokens=current_tokens,
                        preceding_op=current_preceding_op,
                        following_op=tok,
                        subshell_depth=subshell_depth,
                    )
                )
                current_tokens = []
            current_preceding_op = tok
            i += 1
            continue

        current_tokens.append(tok)
        i += 1

    if subshell_depth != 0:
        raise ValueError("Unmatched opening parenthesis in command")

    if current_tokens:
        segments.append(
            _CommandSegment(
                tokens=current_tokens,
                preceding_op=current_preceding_op,
                following_op=None,
                subshell_depth=subshell_depth,
            )
        )

    return segments


def _segment_participates_in_boundary(seg: _CommandSegment) -> bool:
    """Return True if segment participates in conditional, subshell, or pipeline boundary."""
    if seg.subshell_depth > 0:
        return True

    uncertain_operators = {"||", "|", "|&", "&", "(", ")", ";;", ";&", ";;&"}
    if seg.preceding_op in uncertain_operators or seg.following_op in uncertain_operators:
        return True

    control_keywords = {
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "while",
        "until",
        "for",
        "do",
        "done",
        "case",
        "esac",
        "select",
    }
    return any(tok in control_keywords for tok in seg.tokens)


def _is_safety_relevant_mutation_segment(tokens: list[str]) -> bool:
    """Return True if tokens represent mutation targeting safety-relevant keys or aliases."""
    if not tokens:
        return False

    if _is_all_var_assignments(tokens):
        toks = list(tokens)
        while toks:
            asgn = _consume_var_assignment(toks)
            if asgn is not None:
                name, _val, _is_append = asgn
                if _is_git_config_protocol_key(name) or _has_shell_expansion(name):
                    return True
                continue
            toks.pop(0)
        return False

    cleaned = _clean_command_segment(tokens)
    if not cleaned:
        return False

    unwrapped = _unwrap_builtin_wrappers(cleaned)
    if not unwrapped:
        return False

    cmd = os.path.basename(unwrapped[0])
    args = unwrapped[1:]

    if cmd == "export":
        opt_args: list[str] = []
        i = 0
        while i < len(args):
            item = args[i]
            if item == "--":
                opt_args.extend(args[i + 1 :])
                break
            if item.startswith("-") and item != "-" and not _is_var_assignment(item):
                i += 1
            else:
                opt_args.extend(args[i:])
                break
        while opt_args:
            parsed = _parse_state_mutation_operand(opt_args)
            if parsed is None:
                break
            name, _val, _is_append, is_dynamic = parsed
            if is_dynamic or _is_git_config_protocol_key(name):
                return True
        return False

    if cmd == "unset":
        opt_args = []
        i = 0
        while i < len(args):
            item = args[i]
            if item == "--":
                opt_args.extend(args[i + 1 :])
                break
            if item.startswith("-") and item != "-":
                i += 1
            else:
                opt_args.extend(args[i:])
                break
        while opt_args:
            parsed = _parse_state_mutation_operand(opt_args)
            if parsed is None:
                break
            name, _val, _is_append, is_dynamic = parsed
            if is_dynamic or _is_git_config_protocol_key(name):
                return True
        return False

    if cmd in {"declare", "typeset", "readonly", "local"}:
        opt_args = []
        i = 0
        while i < len(args):
            item = args[i]
            if item == "--":
                opt_args.extend(args[i + 1 :])
                break
            if (
                item.startswith(("-", "+"))
                and len(item) > 1
                and not _is_var_assignment(item)
            ):
                i += 1
            else:
                opt_args.extend(args[i:])
                break
        while opt_args:
            parsed = _parse_state_mutation_operand(opt_args)
            if parsed is None:
                break
            name, _val, _is_append, is_dynamic = parsed
            if is_dynamic or _is_git_config_protocol_key(name):
                return True
        return False

    if cmd == "set":
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--":
                i += 1
                break
            if arg in {"-o", "+o"} and i + 1 < len(args):
                if args[i + 1] == "allexport":
                    return True
                i += 2
                continue
            if arg.startswith(("-", "+")) and len(arg) > 1:
                flags = arg[1:]
                if "a" in flags:
                    return True
                if arg in {"--export", "--unexport", "--erase"}:
                    pass
                i += 1
                continue
            break
        operands = args[i:]
        if operands:
            if _has_shell_expansion(operands[0]):
                return True
            var_name = _restore_sentinels(operands[0])
            if _is_git_config_protocol_key(var_name):
                return True
        return False

    if cmd == "shopt":
        is_mut = False
        opt_names: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--":
                opt_names.extend(args[i + 1 :])
                break
            if arg.startswith("-") and len(arg) > 1:
                flags = arg[1:]
                if "s" in flags or "u" in flags:
                    is_mut = True
                i += 1
                continue
            opt_names.extend(args[i:])
            break
        if is_mut:
            for opt in opt_names:
                if (
                    _has_shell_expansion(opt)
                    or _restore_sentinels(opt).strip() == "expand_aliases"
                ):
                    return True
        return False

    if cmd == "alias":
        definitions = _extract_alias_definitions(args)
        return bool(definitions)

    if cmd == "unalias":
        return bool(args)

    return False


def _apply_export_unset_segment(
    tokens: list[str],
    env: dict[str, str] | _ShellState,
) -> bool:
    """Compatibility wrapper for recognizing export/unset/state commands and updating exact Git config keys."""
    if isinstance(env, _ShellState):
        return _apply_shell_state_segment(tokens, env)
    temp_state = _ShellState(inherited_env=env)
    res = _apply_shell_state_segment(tokens, temp_state)
    if res:
        env.clear()
        env.update(temp_state.get_exported_env())
    return res


def _is_git_or_executes_git(tokens: list[str]) -> bool:
    """Return True if the tokens represent Git or a wrapper/executor that may execute Git."""
    if not tokens:
        return False
    cmd_word = os.path.basename(tokens[0])
    return (
        cmd_word == "git"
        or cmd_word in SHELL_BINARIES
        or cmd_word in {"eval", "find"}
        or _extract_initial_dynamic_args(tokens) is not None
        or _has_shell_expansion(tokens[0])
    )


def _inspect_single_command_git(
    tokens: list[str],
    _depth: int = 0,
    _inherited_env: dict[str, str] | None = None,
) -> bool:
    """Inspect a single clean command segment for forced git push."""
    env_vars, tokens = _unwrap_command_and_env(tokens, inherited_env=_inherited_env)
    if not tokens:
        return False

    if XARGS_INPUT_SENTINEL in tokens[0]:
        raise ValueError(
            f"xargs dynamic executable is not supported: {tokens[0]!r}"
        )
    if FIND_INPUT_SENTINEL in tokens[0]:
        raise ValueError(
            f"find dynamic executable is not supported: {tokens[0]!r}"
        )

    if _is_git_or_executes_git(tokens):
        env_alias_configs, env_mirror_configs, env_push_configs = (
            _parse_git_env_details(env_vars)
        )
    else:
        env_alias_configs, env_mirror_configs, env_push_configs = {}, {}, []

    cmd_word = os.path.basename(tokens[0])

    if cmd_word == "find":
        actions = _extract_find_actions(tokens)
        if not actions:
            return False
        for action in actions:
            if _inspect_single_command_git(action, _depth=_depth + 1, _inherited_env=env_vars):
                return True
        return False

    if cmd_word in SHELL_BINARIES:
        return _inspect_shell_invocation(
            tokens,
            lambda cmd, _init_expand_aliases=False: contains_forced_git_push(
                cmd,
                _depth=_depth + 1,
                _inherited_env=env_vars,
                _init_expand_aliases=_init_expand_aliases,
            ),
        )

    if cmd_word == "eval":
        tokens.pop(0)
        for arg in tokens:
            if _has_shell_expansion(arg):
                raise ValueError(
                    f"eval argument containing shell expansion is not supported: {arg!r}"
                )
        eval_payload = " ".join(_restore_sentinels(t) for t in tokens)
        if _has_shell_expansion(eval_payload):
            raise ValueError(
                f"eval payload containing shell expansion is not supported: {eval_payload!r}"
            )
        return contains_forced_git_push(
            eval_payload,
            _depth=_depth + 1,
            _inherited_env=env_vars,
        )

    dynamic_args = _extract_initial_dynamic_args(tokens)
    if dynamic_args is not None:
        return _inspect_git_invocation(
            [_restore_sentinels(t) for t in dynamic_args],
            env_alias_configs=env_alias_configs,
            env_mirror_configs=env_mirror_configs,
            env_push_configs=env_push_configs,
            _depth=_depth,
            _inherited_env=env_vars,
        )

    cmd_binary = os.path.basename(tokens[0])
    if cmd_binary == "git":
        return _inspect_git_invocation(
            [_restore_sentinels(t) for t in tokens[1:]],
            env_alias_configs=env_alias_configs,
            env_mirror_configs=env_mirror_configs,
            env_push_configs=env_push_configs,
            _depth=_depth,
            _inherited_env=env_vars,
        )
    if _has_shell_expansion(tokens[0]):
        return _inspect_git_invocation(
            [_restore_sentinels(t) for t in tokens[1:]],
            env_alias_configs=env_alias_configs,
            env_mirror_configs=env_mirror_configs,
            env_push_configs=env_push_configs,
            _depth=_depth,
            _inherited_env=env_vars,
        )

    return False


def _inspect_single_command_rm(
    tokens: list[str],
    _depth: int = 0,
    _inherited_env: dict[str, str] | None = None,
) -> bool:
    """Inspect a single clean command segment for forbidden destructive rm."""
    env_vars, tokens = _unwrap_command_and_env(tokens, inherited_env=_inherited_env)
    if not tokens:
        return False

    if XARGS_INPUT_SENTINEL in tokens[0]:
        raise ValueError(
            f"xargs dynamic executable is not supported: {tokens[0]!r}"
        )
    if FIND_INPUT_SENTINEL in tokens[0]:
        raise ValueError(
            f"find dynamic executable is not supported: {tokens[0]!r}"
        )

    if _is_git_or_executes_git(tokens):
        env_alias_configs, _env_has_forcing = _parse_git_env_configs(env_vars)
    else:
        env_alias_configs, _env_has_forcing = {}, False

    cmd_word = os.path.basename(tokens[0])

    if cmd_word == "find":
        actions = _extract_find_actions(tokens)
        if not actions:
            return False
        for action in actions:
            if _inspect_single_command_rm(action, _depth=_depth + 1, _inherited_env=env_vars):
                return True
        return False

    if cmd_word in SHELL_BINARIES:
        return _inspect_shell_invocation(
            tokens,
            lambda cmd, _init_expand_aliases=False: contains_forbidden_rm(
                cmd,
                _depth=_depth + 1,
                _inherited_env=env_vars,
                _init_expand_aliases=_init_expand_aliases,
            ),
        )

    if cmd_word == "eval":
        tokens.pop(0)
        for arg in tokens:
            if _has_shell_expansion(arg):
                raise ValueError(
                    f"eval argument containing shell expansion is not supported: {arg!r}"
                )
        eval_payload = " ".join(_restore_sentinels(t) for t in tokens)
        if _has_shell_expansion(eval_payload):
            raise ValueError(
                f"eval payload containing shell expansion is not supported: {eval_payload!r}"
            )
        return contains_forbidden_rm(
            eval_payload,
            _depth=_depth + 1,
            _inherited_env=env_vars,
        )

    dynamic_args = _extract_initial_dynamic_args(tokens)
    if dynamic_args is not None:
        restored_dynamic_args = [_restore_sentinels(t) for t in dynamic_args]
        if _is_forbidden_rm_args(restored_dynamic_args):
            return True
        return _inspect_git_invocation_for_rm(
            restored_dynamic_args,
            env_alias_configs=env_alias_configs,
            _depth=_depth,
            _inherited_env=env_vars,
        )

    cmd_binary = os.path.basename(tokens[0])
    if cmd_binary == "git":
        return _inspect_git_invocation_for_rm(
            [_restore_sentinels(t) for t in tokens[1:]],
            env_alias_configs=env_alias_configs,
            _depth=_depth,
            _inherited_env=env_vars,
        )
    if cmd_binary == "rm":
        return _is_forbidden_rm_args([_restore_sentinels(t) for t in tokens[1:]])

    if _has_shell_expansion(tokens[0]):
        restored_trailing = [_restore_sentinels(t) for t in tokens[1:]]
        if _is_forbidden_rm_args(restored_trailing):
            return True
        return _inspect_git_invocation_for_rm(
            restored_trailing,
            env_alias_configs=env_alias_configs,
            _depth=_depth,
            _inherited_env=env_vars,
        )

    return False


MAX_SUBSTITUTION_DEPTH = 20


def _parse_backtick_body(
    command: str, start: int, in_double_quotes: bool = False
) -> tuple[str, int]:
    """Parse a legacy backtick substitution `...` starting at `start`.

    Returns (unescaped_body, next_index_after_closing_backtick).
    Raises ValueError if unmatched/unclosed.
    """
    n = len(command)
    i = start + 1
    body_chars: list[str] = []

    while i < n:
        ch = command[i]
        if ch == "`":
            return "".join(body_chars), i + 1

        if ch == "\\":
            if i + 1 < n:
                next_ch = command[i + 1]
                if next_ch in {"`", "\\", "$"} or (in_double_quotes and next_ch == '"'):
                    body_chars.append(next_ch)
                    i += 2
                    continue
                else:
                    body_chars.append("\\")
                    body_chars.append(next_ch)
                    i += 2
                    continue
            else:
                body_chars.append("\\")
                i += 1
                continue

        body_chars.append(ch)
        i += 1

    raise ValueError("Unmatched opening backtick in command substitution")


def _parse_paren_body(
    command: str, start: int, prefix_len: int, is_arith: bool = False
) -> tuple[str, int]:
    """Parse $(...), <(...), >(...), or $((...)) starting at `start`.

    Returns (body, next_index_after_closing_paren).
    Raises ValueError if unmatched/unclosed.
    """
    n = len(command)
    i = start + prefix_len
    body_start = i
    paren_depth = 2 if is_arith else 1
    state = "NORMAL"
    prev_char: str | None = None
    pending_heredocs: list[_HereDocTarget] = []

    while i < n:
        ch = command[i]

        if state == "NORMAL":
            if ch == "\\":
                i += 2
                prev_char = "\\"
                continue

            elif ch == "'":
                state = "SINGLE_QUOTE"
                prev_char = "'"
                i += 1
                continue

            elif ch == '"':
                state = "DOUBLE_QUOTE"
                prev_char = '"'
                i += 1
                continue

            elif ch == "#" and (prev_char is None or prev_char in " \t\r\n;&|(){}<>"):
                state = "COMMENT"
                i += 1
                continue

            elif ch == "<" and i + 1 < n and command[i + 1] == "<":
                if i + 2 < n and command[i + 2] == "<":
                    i += 3
                    prev_char = "<"
                    continue
                target, next_i = _parse_heredoc_delimiter(command, i)
                pending_heredocs.append(target)
                i = next_i
                prev_char = target.delimiter[-1] if target.delimiter else ">"
                continue

            elif ch == "`":
                _, next_i = _parse_backtick_body(command, i, in_double_quotes=False)
                i = next_i
                prev_char = "`"
                continue

            elif ch == "$" and i + 1 < n:
                if command[i + 1 : i + 3] == "((":
                    _, next_i = _parse_paren_body(
                        command, i, prefix_len=3, is_arith=True
                    )
                    i = next_i
                    prev_char = ")"
                    continue
                elif command[i + 1] == "(":
                    _, next_i = _parse_paren_body(
                        command, i, prefix_len=2, is_arith=False
                    )
                    i = next_i
                    prev_char = ")"
                    continue
                else:
                    prev_char = "$"
                    i += 1
                    continue

            elif (ch == "<" or ch == ">") and i + 1 < n and command[i + 1] == "(":
                _, next_i = _parse_paren_body(
                    command, i, prefix_len=2, is_arith=False
                )
                i = next_i
                prev_char = ")"
                continue

            elif ch == "(":
                paren_depth += 1
                prev_char = "("
                i += 1
                continue

            elif ch == ")":
                paren_depth -= 1
                if paren_depth == 0:
                    if pending_heredocs:
                        raise ValueError("Unclosed here-doc body in command substitution")
                    body = command[body_start : (i - 1 if is_arith else i)]
                    return body, i + 1
                prev_char = ")"
                i += 1
                continue

            elif ch == "\n":
                if pending_heredocs:
                    next_start = i + 1
                    for hd in pending_heredocs:
                        _, next_start = _consume_heredoc_body(
                            command, next_start, hd
                        )
                    pending_heredocs.clear()
                    i = next_start
                    prev_char = "\n"
                    continue
                else:
                    prev_char = "\n"
                    i += 1
                    continue

            else:
                prev_char = ch
                i += 1
                continue

        elif state == "SINGLE_QUOTE":
            if ch == "'":
                state = "NORMAL"
                prev_char = "'"
                i += 1
                continue
            else:
                prev_char = ch
                i += 1
                continue

        elif state == "DOUBLE_QUOTE":
            if ch == '"':
                state = "NORMAL"
                prev_char = '"'
                i += 1
                continue

            elif ch == "\\":
                if i + 1 < n and command[i + 1] in {"$", "`", '"', "\\", "\n"}:
                    i += 2
                    prev_char = "\\"
                    continue
                else:
                    i += 1
                    prev_char = "\\"
                    continue

            elif ch == "`":
                _, next_i = _parse_backtick_body(command, i, in_double_quotes=True)
                i = next_i
                prev_char = "`"
                continue

            elif ch == "$" and i + 1 < n:
                if command[i + 1 : i + 3] == "((":
                    _, next_i = _parse_paren_body(
                        command, i, prefix_len=3, is_arith=True
                    )
                    i = next_i
                    prev_char = ")"
                    continue
                elif command[i + 1] == "(":
                    _, next_i = _parse_paren_body(
                        command, i, prefix_len=2, is_arith=False
                    )
                    i = next_i
                    prev_char = ")"
                    continue
                else:
                    prev_char = "$"
                    i += 1
                    continue

            else:
                prev_char = ch
                i += 1
                continue

        elif state == "COMMENT":
            if ch == "\n":
                if pending_heredocs:
                    next_start = i + 1
                    state = "NORMAL"
                    for hd in pending_heredocs:
                        _, next_start = _consume_heredoc_body(
                            command, next_start, hd
                        )
                    pending_heredocs.clear()
                    i = next_start
                    prev_char = "\n"
                    continue
                else:
                    state = "NORMAL"
                    prev_char = "\n"
                    i += 1
                    continue
            i += 1
            continue

    if pending_heredocs:
        raise ValueError("Unclosed here-doc body in command substitution")
    if state == "SINGLE_QUOTE":
        raise ValueError("Unclosed single quote in command substitution")
    if state == "DOUBLE_QUOTE":
        raise ValueError("Unclosed double quote in command substitution")
    if is_arith:
        raise ValueError("Unmatched '$((' in arithmetic expansion")
    if prefix_len == 2 and command[start] in {"<", ">"}:
        raise ValueError(f"Unmatched '{command[start]}(' in process substitution")
    raise ValueError("Unmatched '$(' in command substitution")


def _extract_heredoc_body_substitutions(body: str) -> list[tuple[str, str]]:
    """Extract executable substitutions from an unquoted here-doc body.

    Quotes are literal and do not suppress substitutions.
    Process substitutions <(...) and >(...) are literal text in here-doc bodies.
    Backslash escapes $, `, \\, and \\n.
    """
    substitutions: list[tuple[str, str]] = []
    i = 0
    n = len(body)

    while i < n:
        ch = body[i]
        if ch == "\\":
            if i + 1 < n:
                next_ch = body[i + 1]
                if next_ch in {"$", "`", "\\", "\n"}:
                    i += 2
                    continue
                else:
                    i += 2
                    continue
            else:
                i += 1
                continue

        elif ch == "`":
            body_str, next_i = _parse_backtick_body(body, i, in_double_quotes=True)
            substitutions.append(("backtick", body_str))
            i = next_i
            continue

        elif ch == "$" and i + 1 < n:
            if body[i + 1 : i + 3] == "((":
                body_str, next_i = _parse_paren_body(body, i, prefix_len=3, is_arith=True)
                substitutions.append(("arith", body_str))
                i = next_i
                continue
            elif body[i + 1] == "(":
                body_str, next_i = _parse_paren_body(body, i, prefix_len=2, is_arith=False)
                substitutions.append(("cmd", body_str))
                i = next_i
                continue
            else:
                i += 1
                continue

        else:
            i += 1

    return substitutions


def _extract_raw_substitutions(command: str) -> list[tuple[str, str]]:
    """Lexically extract immediate executable substitution bodies from a shell command string.

    Returns a list of (kind, body) tuples where kind is one of:
    'cmd', 'backtick', 'process_in', 'process_out', 'arith'.
    Raises ValueError on malformed or unclosed substitutions or unclosed here-docs.
    """
    substitutions: list[tuple[str, str]] = []
    pending_heredocs: list[_HereDocTarget] = []
    i = 0
    n = len(command)
    state = "NORMAL"
    prev_char: str | None = None

    while i < n:
        ch = command[i]

        if state == "NORMAL":
            if ch == "\\":
                i += 2
                prev_char = "\\"
                continue

            elif ch == "'":
                state = "SINGLE_QUOTE"
                prev_char = "'"
                i += 1
                continue

            elif ch == '"':
                state = "DOUBLE_QUOTE"
                prev_char = '"'
                i += 1
                continue

            elif ch == "#" and (prev_char is None or prev_char in " \t\r\n;&|(){}<>"):
                state = "COMMENT"
                i += 1
                continue

            elif ch == "<" and i + 1 < n and command[i + 1] == "<":
                if i + 2 < n and command[i + 2] == "<":
                    i += 3
                    prev_char = "<"
                    continue
                target, next_i = _parse_heredoc_delimiter(command, i)
                pending_heredocs.append(target)
                i = next_i
                prev_char = target.delimiter[-1] if target.delimiter else ">"
                continue

            elif ch == "`":
                body, next_i = _parse_backtick_body(command, i, in_double_quotes=False)
                substitutions.append(("backtick", body))
                i = next_i
                prev_char = "`"
                continue

            elif ch == "$" and i + 1 < n:
                if command[i + 1 : i + 3] == "((":
                    body, next_i = _parse_paren_body(
                        command, i, prefix_len=3, is_arith=True
                    )
                    substitutions.append(("arith", body))
                    i = next_i
                    prev_char = ")"
                    continue
                elif command[i + 1] == "(":
                    body, next_i = _parse_paren_body(
                        command, i, prefix_len=2, is_arith=False
                    )
                    substitutions.append(("cmd", body))
                    i = next_i
                    prev_char = ")"
                    continue
                else:
                    prev_char = ch
                    i += 1
                    continue

            elif ch == "<" and i + 1 < n and command[i + 1] == "(":
                body, next_i = _parse_paren_body(
                    command, i, prefix_len=2, is_arith=False
                )
                substitutions.append(("process_in", body))
                i = next_i
                prev_char = ")"
                continue

            elif ch == ">" and i + 1 < n and command[i + 1] == "(":
                body, next_i = _parse_paren_body(
                    command, i, prefix_len=2, is_arith=False
                )
                substitutions.append(("process_out", body))
                i = next_i
                prev_char = ")"
                continue

            elif ch == "\n":
                if pending_heredocs:
                    next_start = i + 1
                    for hd in pending_heredocs:
                        body, next_start = _consume_heredoc_body(
                            command, next_start, hd
                        )
                        if not hd.is_quoted:
                            substitutions.extend(
                                _extract_heredoc_body_substitutions(body)
                            )
                    pending_heredocs.clear()
                    i = next_start
                    prev_char = "\n"
                    continue
                else:
                    prev_char = "\n"
                    i += 1
                    continue

            else:
                prev_char = ch
                i += 1
                continue

        elif state == "SINGLE_QUOTE":
            if ch == "'":
                state = "NORMAL"
                prev_char = "'"
                i += 1
                continue
            else:
                prev_char = ch
                i += 1
                continue

        elif state == "DOUBLE_QUOTE":
            if ch == '"':
                state = "NORMAL"
                prev_char = '"'
                i += 1
                continue

            elif ch == "\\":
                if i + 1 < n and command[i + 1] in {"$", "`", '"', "\\", "\n"}:
                    i += 2
                    prev_char = "\\"
                    continue
                else:
                    i += 1
                    prev_char = "\\"
                    continue

            elif ch == "`":
                body, next_i = _parse_backtick_body(command, i, in_double_quotes=True)
                substitutions.append(("backtick", body))
                i = next_i
                prev_char = "`"
                continue

            elif ch == "$" and i + 1 < n:
                if command[i + 1 : i + 3] == "((":
                    body, next_i = _parse_paren_body(
                        command, i, prefix_len=3, is_arith=True
                    )
                    substitutions.append(("arith", body))
                    i = next_i
                    prev_char = ")"
                    continue
                elif command[i + 1] == "(":
                    body, next_i = _parse_paren_body(
                        command, i, prefix_len=2, is_arith=False
                    )
                    substitutions.append(("cmd", body))
                    i = next_i
                    prev_char = ")"
                    continue
                else:
                    prev_char = ch
                    i += 1
                    continue

            else:
                prev_char = ch
                i += 1
                continue

        elif state == "COMMENT":
            if ch == "\n":
                if pending_heredocs:
                    next_start = i + 1
                    state = "NORMAL"
                    for hd in pending_heredocs:
                        body, next_start = _consume_heredoc_body(
                            command, next_start, hd
                        )
                        if not hd.is_quoted:
                            substitutions.extend(
                                _extract_heredoc_body_substitutions(body)
                            )
                    pending_heredocs.clear()
                    i = next_start
                    prev_char = "\n"
                    continue
                else:
                    state = "NORMAL"
                    prev_char = "\n"
                    i += 1
                    continue
            i += 1
            continue

    if pending_heredocs:
        raise ValueError("Unclosed here-doc body")

    if state in {"SINGLE_QUOTE", "DOUBLE_QUOTE"}:
        raise ValueError(f"Unclosed quote in command: {state}")

    return substitutions


def _mask_and_collect_substitutions(
    command: str, depth: int
) -> tuple[str, dict[str, list[tuple[str, str]]]]:
    """Lexically scan and replace executable substitutions with unique sentinels.

    Returns (masked_command, subst_map) where subst_map maps sentinel token to
    a list of (kind, body) tuples.
    Raises ValueError on malformed or unclosed substitutions or unclosed here-docs.
    """
    if depth > MAX_SUBSTITUTION_DEPTH:
        raise ValueError(
            f"Maximum substitution nesting depth ({MAX_SUBSTITUTION_DEPTH}) exceeded"
        )

    masked_chars: list[str] = []
    subst_map: dict[str, list[tuple[str, str]]] = {}
    pending_heredocs: list[_HereDocTarget] = []
    i = 0
    n = len(command)
    state = "NORMAL"
    prev_char: str | None = None
    subst_counter = 0

    while i < n:
        ch = command[i]

        if state == "NORMAL":
            if ch == "\\":
                masked_chars.append(ch)
                i += 1
                if i < n:
                    masked_chars.append(command[i])
                    prev_char = command[i]
                    i += 1
                else:
                    prev_char = ch
                continue

            elif ch == "'":
                state = "SINGLE_QUOTE"
                masked_chars.append(ch)
                prev_char = "'"
                i += 1
                continue

            elif ch == '"':
                state = "DOUBLE_QUOTE"
                masked_chars.append(ch)
                prev_char = '"'
                i += 1
                continue

            elif ch == "#" and (prev_char is None or prev_char in " \t\r\n;&|(){}<>"):
                state = "COMMENT"
                i += 1
                continue

            elif ch == "<" and i + 1 < n and command[i + 1] == "<":
                if i + 2 < n and command[i + 2] == "<":
                    masked_chars.append(command[i : i + 3])
                    i += 3
                    prev_char = "<"
                    continue
                target, next_i = _parse_heredoc_delimiter(command, i)
                pending_heredocs.append(target)
                masked_chars.append(command[i:next_i])
                i = next_i
                prev_char = target.delimiter[-1] if target.delimiter else ">"
                continue

            elif ch == "`":
                body, next_i = _parse_backtick_body(command, i, in_double_quotes=False)
                sentinel = f"{_SUBST_SENTINEL_PREFIX}{subst_counter}__"
                subst_counter += 1
                subst_map[sentinel] = [("backtick", body)]
                masked_chars.append(sentinel)
                i = next_i
                prev_char = "`"
                continue

            elif ch == "$" and i + 1 < n:
                if command[i + 1 : i + 3] == "((":
                    body, next_i = _parse_paren_body(
                        command, i, prefix_len=3, is_arith=True
                    )
                    sentinel = f"{_SUBST_SENTINEL_PREFIX}{subst_counter}__"
                    subst_counter += 1
                    subst_map[sentinel] = [("arith", body)]
                    masked_chars.append(sentinel)
                    i = next_i
                    prev_char = ")"
                    continue
                elif command[i + 1] == "(":
                    body, next_i = _parse_paren_body(
                        command, i, prefix_len=2, is_arith=False
                    )
                    sentinel = f"{_SUBST_SENTINEL_PREFIX}{subst_counter}__"
                    subst_counter += 1
                    subst_map[sentinel] = [("cmd", body)]
                    masked_chars.append(sentinel)
                    i = next_i
                    prev_char = ")"
                    continue
                else:
                    masked_chars.append(ch)
                    prev_char = ch
                    i += 1
                    continue

            elif ch == "<" and i + 1 < n and command[i + 1] == "(":
                body, next_i = _parse_paren_body(
                    command, i, prefix_len=2, is_arith=False
                )
                sentinel = f"{_SUBST_SENTINEL_PREFIX}{subst_counter}__"
                subst_counter += 1
                subst_map[sentinel] = [("process_in", body)]
                masked_chars.append(sentinel)
                i = next_i
                prev_char = ")"
                continue

            elif ch == ">" and i + 1 < n and command[i + 1] == "(":
                body, next_i = _parse_paren_body(
                    command, i, prefix_len=2, is_arith=False
                )
                sentinel = f"{_SUBST_SENTINEL_PREFIX}{subst_counter}__"
                subst_counter += 1
                subst_map[sentinel] = [("process_out", body)]
                masked_chars.append(sentinel)
                i = next_i
                prev_char = ")"
                continue

            elif ch == "\n":
                if pending_heredocs:
                    next_start = i + 1
                    for hd in pending_heredocs:
                        body, next_start = _consume_heredoc_body(
                            command, next_start, hd
                        )
                        if not hd.is_quoted:
                            hd_substs = _extract_heredoc_body_substitutions(body)
                            if hd_substs:
                                sentinel = f"{_SUBST_SENTINEL_PREFIX}{subst_counter}__"
                                subst_counter += 1
                                subst_map[sentinel] = hd_substs
                                masked_chars.append(f" {sentinel} ")
                    body_chunk = command[i + 1 : next_start]
                    pending_heredocs.clear()
                    masked_chars.append("\n")
                    masked_chars.append(body_chunk)
                    i = next_start
                    prev_char = "\n"
                    continue
                else:
                    masked_chars.append("\n")
                    prev_char = "\n"
                    i += 1
                    continue

            else:
                masked_chars.append(ch)
                prev_char = ch
                i += 1
                continue

        elif state == "SINGLE_QUOTE":
            if ch == "'":
                state = "NORMAL"
                masked_chars.append(ch)
                prev_char = "'"
                i += 1
                continue
            else:
                masked_chars.append(ch)
                prev_char = ch
                i += 1
                continue

        elif state == "DOUBLE_QUOTE":
            if ch == '"':
                state = "NORMAL"
                masked_chars.append(ch)
                prev_char = '"'
                i += 1
                continue

            elif ch == "\\":
                if i + 1 < n and command[i + 1] in {"$", "`", '"', "\\", "\n"}:
                    masked_chars.append(ch)
                    masked_chars.append(command[i + 1])
                    i += 2
                    prev_char = command[i - 1]
                    continue
                else:
                    masked_chars.append(ch)
                    i += 1
                    prev_char = "\\"
                    continue

            elif ch == "`":
                body, next_i = _parse_backtick_body(command, i, in_double_quotes=True)
                sentinel = f"{_SUBST_SENTINEL_PREFIX}{subst_counter}__"
                subst_counter += 1
                subst_map[sentinel] = [("backtick", body)]
                masked_chars.append(sentinel)
                i = next_i
                prev_char = "`"
                continue

            elif ch == "$" and i + 1 < n:
                if command[i + 1 : i + 3] == "((":
                    body, next_i = _parse_paren_body(
                        command, i, prefix_len=3, is_arith=True
                    )
                    sentinel = f"{_SUBST_SENTINEL_PREFIX}{subst_counter}__"
                    subst_counter += 1
                    subst_map[sentinel] = [("arith", body)]
                    masked_chars.append(sentinel)
                    i = next_i
                    prev_char = ")"
                    continue
                elif command[i + 1] == "(":
                    body, next_i = _parse_paren_body(
                        command, i, prefix_len=2, is_arith=False
                    )
                    sentinel = f"{_SUBST_SENTINEL_PREFIX}{subst_counter}__"
                    subst_counter += 1
                    subst_map[sentinel] = [("cmd", body)]
                    masked_chars.append(sentinel)
                    i = next_i
                    prev_char = ")"
                    continue
                else:
                    masked_chars.append(ch)
                    prev_char = ch
                    i += 1
                    continue

            else:
                masked_chars.append(ch)
                prev_char = ch
                i += 1
                continue

        elif state == "COMMENT":
            if ch == "\n":
                if pending_heredocs:
                    next_start = i + 1
                    state = "NORMAL"
                    for hd in pending_heredocs:
                        body, next_start = _consume_heredoc_body(
                            command, next_start, hd
                        )
                        if not hd.is_quoted:
                            hd_substs = _extract_heredoc_body_substitutions(body)
                            if hd_substs:
                                sentinel = f"{_SUBST_SENTINEL_PREFIX}{subst_counter}__"
                                subst_counter += 1
                                subst_map[sentinel] = hd_substs
                                masked_chars.append(f" {sentinel} ")
                    body_chunk = command[i + 1 : next_start]
                    pending_heredocs.clear()
                    masked_chars.append("\n")
                    masked_chars.append(body_chunk)
                    i = next_start
                    prev_char = "\n"
                    continue
                else:
                    state = "NORMAL"
                    masked_chars.append("\n")
                    prev_char = "\n"
                    i += 1
                    continue
            i += 1
            continue

    if pending_heredocs:
        raise ValueError("Unclosed here-doc body")

    if state in {"SINGLE_QUOTE", "DOUBLE_QUOTE"}:
        raise ValueError(f"Unclosed quote in command: {state}")

    return "".join(masked_chars), subst_map


def _extract_segment_sentinels(
    tokens: list[str],
    subst_map: dict[str, list[tuple[str, str]]],
) -> list[str]:
    """Extract substitution sentinels appearing in the token list in ascending index order."""
    if not subst_map:
        return []
    found: set[str] = set()
    for tok in tokens:
        for sentinel in subst_map:
            if sentinel in tok:
                found.add(sentinel)
    if not found:
        return []
    return sorted(
        found,
        key=lambda s: int(s[len(_SUBST_SENTINEL_PREFIX) : -2])
        if s[len(_SUBST_SENTINEL_PREFIX) : -2].isdigit()
        else 0,
    )


def _inspect_substitutions_with_env(
    command: str,
    checker_fn: Callable[..., bool],
    depth: int,
    inherited_env: dict[str, str] | None,
) -> bool:
    """Inspect executable shell substitutions in command inheriting environment."""
    if depth > MAX_SUBSTITUTION_DEPTH:
        raise ValueError(
            f"Maximum substitution nesting depth ({MAX_SUBSTITUTION_DEPTH}) exceeded"
        )

    substitutions = _extract_raw_substitutions(command)
    for kind, body in substitutions:
        if kind == "arith":
            if _inspect_substitutions_with_env(body, checker_fn, depth + 1, inherited_env):
                return True
        else:
            if checker_fn(body, _depth=depth + 1, _inherited_env=inherited_env):
                return True

    return False


def _inspect_substitutions(
    command: str,
    checker_fn: Callable[[str, int], bool],
    depth: int,
) -> bool:
    """Inspect executable shell substitutions anywhere in command.

    Recursively scans command substitutions $(...), legacy backticks `...`,
    Bash process substitutions <(...) / >(...), and arithmetic expansions $((...)).
    Raises ValueError if depth exceeds MAX_SUBSTITUTION_DEPTH or syntax is unclosed.
    """
    if depth > MAX_SUBSTITUTION_DEPTH:
        raise ValueError(
            f"Maximum substitution nesting depth ({MAX_SUBSTITUTION_DEPTH}) exceeded"
        )

    substitutions = _extract_raw_substitutions(command)
    for kind, body in substitutions:
        if kind == "arith":
            if _inspect_substitutions(body, checker_fn, depth + 1):
                return True
        else:
            if checker_fn(body, depth + 1):
                return True

    return False


def contains_forced_git_push(
    command: str,
    _depth: int = 0,
    _inherited_env: dict[str, str] | None = None,
    _shell_state: _ShellState | None = None,
    _init_expand_aliases: bool = False,
) -> bool:
    """Pure function checking whether a shell command contains a forced git push.

    Raises ValueError if the shell command syntax is invalid (e.g. unclosed quotes
    or malformed/unmatched command substitutions).
    """
    if _depth > MAX_SUBSTITUTION_DEPTH:
        raise ValueError(
            f"Maximum substitution nesting depth ({MAX_SUBSTITUTION_DEPTH}) exceeded"
        )

    if not command or not command.strip():
        return False

    state = (
        _shell_state
        if _shell_state is not None
        else _ShellState(
            inherited_env=_inherited_env,
            expand_aliases=_init_expand_aliases,
        )
    )

    masked_command, subst_map = _mask_and_collect_substitutions(command, _depth)
    cleaned = _strip_comments_preserving_newlines(masked_command)
    lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=True)
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    lexer.wordchars += "+%{}"
    raw_tokens = list(lexer)
    if not raw_tokens:
        return False

    segments = _parse_command_segments(raw_tokens)
    valid_mutation_and_chain = True

    for seg in segments:
        cmd_tokens = seg.tokens
        segment_sentinels = _extract_segment_sentinels(cmd_tokens, subst_map)
        for sentinel in segment_sentinels:
            for kind, body in subst_map[sentinel]:
                if kind == "arith":
                    if _inspect_substitutions_with_env(
                        body,
                        contains_forced_git_push,
                        _depth + 1,
                        state.get_exported_env(),
                    ):
                        return True
                else:
                    if contains_forced_git_push(
                        body,
                        _depth=_depth + 1,
                        _inherited_env=state.get_exported_env(),
                    ):
                        return True

        if seg.preceding_op in SEPARATORS_UNCONDITIONAL or seg.preceding_op is None:
            valid_mutation_and_chain = True

        is_safety_mutation = _is_safety_relevant_mutation_segment(cmd_tokens)

        if is_safety_mutation:
            if _segment_participates_in_boundary(seg):
                op_desc = seg.preceding_op or seg.following_op or "subshell/control"
                raise ValueError(
                    "Safety-relevant shell state mutation participates in uncertain "
                    f"execution boundary ({op_desc})"
                )
            if seg.preceding_op == "&&" and not valid_mutation_and_chain:
                raise ValueError(
                    "Safety-relevant shell state mutation participates in uncertain "
                    "execution boundary (&&)"
                )

        cleaned_cmd = _clean_command_segment(cmd_tokens)
        if not cleaned_cmd:
            continue

        if _apply_shell_state_segment(cleaned_cmd, state):
            if seg.following_op in {"||", "|", "|&", "&", ";;", ";&", ";;&"}:
                valid_mutation_and_chain = False
            continue

        valid_mutation_and_chain = False

        unwrapped_eval = _unwrap_builtin_wrappers(cleaned_cmd)
        if unwrapped_eval and os.path.basename(unwrapped_eval[0]) == "eval":
            eval_args = unwrapped_eval[1:]
            for arg in eval_args:
                if _has_shell_expansion(arg):
                    raise ValueError(
                        f"eval argument containing shell expansion is not supported: {arg!r}"
                    )
            eval_payload = " ".join(_restore_sentinels(t) for t in eval_args)
            if _has_shell_expansion(eval_payload):
                raise ValueError(
                    f"eval payload containing shell expansion is not supported: {eval_payload!r}"
                )
            if contains_forced_git_push(
                eval_payload, _depth=_depth + 1, _shell_state=state
            ):
                return True
            continue

        current_env = state.get_exported_env()
        if _inspect_single_command_git(
            cleaned_cmd, _depth=_depth, _inherited_env=current_env
        ):
            return True

    return False


def contains_forbidden_rm(
    command: str,
    _depth: int = 0,
    _inherited_env: dict[str, str] | None = None,
    _shell_state: _ShellState | None = None,
    _init_expand_aliases: bool = False,
) -> bool:
    """Pure function checking whether a shell command contains a forbidden destructive rm invocation.

    Raises ValueError if the shell command syntax is invalid (e.g. unclosed quotes
    or malformed/unmatched command substitutions).
    """
    if _depth > MAX_SUBSTITUTION_DEPTH:
        raise ValueError(
            f"Maximum substitution nesting depth ({MAX_SUBSTITUTION_DEPTH}) exceeded"
        )

    if not command or not command.strip():
        return False

    state = (
        _shell_state
        if _shell_state is not None
        else _ShellState(
            inherited_env=_inherited_env,
            expand_aliases=_init_expand_aliases,
        )
    )

    masked_command, subst_map = _mask_and_collect_substitutions(command, _depth)
    cleaned = _strip_comments_preserving_newlines(masked_command)
    lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=True)
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    lexer.wordchars += "+%{}"
    raw_tokens = list(lexer)
    if not raw_tokens:
        return False

    segments = _parse_command_segments(raw_tokens)
    valid_mutation_and_chain = True

    for seg in segments:
        cmd_tokens = seg.tokens
        segment_sentinels = _extract_segment_sentinels(cmd_tokens, subst_map)
        for sentinel in segment_sentinels:
            for kind, body in subst_map[sentinel]:
                if kind == "arith":
                    if _inspect_substitutions_with_env(
                        body,
                        contains_forbidden_rm,
                        _depth + 1,
                        state.get_exported_env(),
                    ):
                        return True
                else:
                    if contains_forbidden_rm(
                        body,
                        _depth=_depth + 1,
                        _inherited_env=state.get_exported_env(),
                    ):
                        return True

        if seg.preceding_op in SEPARATORS_UNCONDITIONAL or seg.preceding_op is None:
            valid_mutation_and_chain = True

        is_safety_mutation = _is_safety_relevant_mutation_segment(cmd_tokens)

        if is_safety_mutation:
            if _segment_participates_in_boundary(seg):
                op_desc = seg.preceding_op or seg.following_op or "subshell/control"
                raise ValueError(
                    "Safety-relevant shell state mutation participates in uncertain "
                    f"execution boundary ({op_desc})"
                )
            if seg.preceding_op == "&&" and not valid_mutation_and_chain:
                raise ValueError(
                    "Safety-relevant shell state mutation participates in uncertain "
                    "execution boundary (&&)"
                )

        cleaned_cmd = _clean_command_segment(cmd_tokens)
        if not cleaned_cmd:
            continue

        if _apply_shell_state_segment(cleaned_cmd, state):
            if seg.following_op in {"||", "|", "|&", "&", ";;", ";&", ";;&"}:
                valid_mutation_and_chain = False
            continue

        valid_mutation_and_chain = False

        unwrapped_eval = _unwrap_builtin_wrappers(cleaned_cmd)
        if unwrapped_eval and os.path.basename(unwrapped_eval[0]) == "eval":
            eval_args = unwrapped_eval[1:]
            for arg in eval_args:
                if _has_shell_expansion(arg):
                    raise ValueError(
                        f"eval argument containing shell expansion is not supported: {arg!r}"
                    )
            eval_payload = " ".join(_restore_sentinels(t) for t in eval_args)
            if _has_shell_expansion(eval_payload):
                raise ValueError(
                    f"eval payload containing shell expansion is not supported: {eval_payload!r}"
                )
            if contains_forbidden_rm(
                eval_payload, _depth=_depth + 1, _shell_state=state
            ):
                return True
            continue

        current_env = state.get_exported_env()
        if _inspect_single_command_rm(
            cleaned_cmd, _depth=_depth, _inherited_env=current_env
        ):
            return True

    return False



def main() -> None:
    """CLI entrypoint for Claude PreToolUse hook."""
    try:
        raw_input = sys.stdin.read()
    except Exception as exc:
        sys.stderr.write(f"Failed to read stdin: {exc}\n")
        sys.exit(2)

    if not raw_input.strip():
        sys.stderr.write("Empty input on stdin\n")
        sys.exit(2)

    try:
        payload = json.loads(raw_input)
    except Exception as exc:
        sys.stderr.write(f"Malformed JSON on stdin: {exc}\n")
        sys.exit(2)

    if not isinstance(payload, dict):
        sys.stderr.write("Payload must be a JSON object\n")
        sys.exit(2)

    command: str | None = None
    if "command" in payload:
        command = payload["command"]
    elif "tool_input" in payload and isinstance(payload["tool_input"], dict):
        command = payload["tool_input"].get("command")
    elif "toolInput" in payload and isinstance(payload["toolInput"], dict):
        command = payload["toolInput"].get("command")
    elif "input" in payload and isinstance(payload["input"], dict):
        command = payload["input"].get("command")

    if command is None or not isinstance(command, str):
        sys.stderr.write("Missing or non-string 'command' in payload\n")
        sys.exit(2)

    try:
        is_forced = contains_forced_git_push(command)
        is_forbidden_rm = contains_forbidden_rm(command)
    except Exception as exc:
        sys.stderr.write(f"Shell tokenization failed: {exc}\n")
        sys.exit(2)

    if is_forced:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Git force-push is prohibited by repository no-force-push policy."
                ),
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    if is_forbidden_rm:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Destructive rm commands with combined recursive and force flags are prohibited by repository destructive-command policy."
                ),
            }
        }
        print(json.dumps(output))
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
