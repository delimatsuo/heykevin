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
    - literal backtick: `...`
    - dollar + identifier token: $GIT, $RM
    - dollar + braced identifier token: ${GIT}, ${RM}
    - dollar + open parenthesis: $(...) command substitution

    If token zero is not a dynamic executable prefix, returns None.
    If the prefix is malformed or unmatched, raises ValueError.
    """
    if not tokens:
        return None

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
                i += 1
                while i + 1 < n and git_args[i] == ":":
                    val = val + ":" + git_args[i + 1]
                    i += 2
                reconstructed.append(val)
            continue

        if arg.startswith("-c") and not arg.startswith("-C"):
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
                i += 1
                while i + 1 < n and git_args[i] == ":":
                    val = val + ":" + git_args[i + 1]
                    i += 2
                reconstructed.append(val)
            continue

        if arg.startswith("--config-env="):
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

        reconstructed.append(arg)
        i += 1

    return reconstructed


def _record_alias_config(
    alias_configs: dict[str, tuple[str, str]],
    val: str,
    kind: str,
) -> None:
    """Record an alias config entry if val starts with alias."""
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
                _record_alias_config(alias_configs, git_args[i], "c")
                _record_forcing_config(mirror_configs, push_configs, git_args[i], "c")
                i += 1
            continue

        if arg.startswith("-c") and not arg.startswith("-C"):
            val = arg[2:]
            _record_alias_config(alias_configs, val, "c")
            _record_forcing_config(mirror_configs, push_configs, val, "c")
            i += 1
            continue

        if arg == "--config-env":
            i += 1
            if i < len(git_args):
                _record_alias_config(alias_configs, git_args[i], "config-env")
                _record_forcing_config(mirror_configs, push_configs, git_args[i], "config-env")
                i += 1
            continue

        if arg.startswith("--config-env="):
            val = arg[len("--config-env="):]
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
    if "=" in entry:
        key, setting = entry.split("=", 1)
        key_stripped = key.strip().lower()
        if key_stripped:
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


def _is_var_assignment(token: str) -> bool:
    """Check if token is an environment variable assignment like FOO=bar."""
    if "=" not in token:
        return False
    name = token.split("=", 1)[0]
    return name.isidentifier()


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
        if _is_redirection(token):
            if token in {
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
    """Check if token contains unsupported shell expansion markers ($ or `)."""
    return "$" in token or "`" in token


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
            if arg == "--mirror":
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


def _consume_var_assignment(tokens: list[str]) -> tuple[str, str] | None:
    """If tokens start with a variable assignment, pop it and return (name, val).

    Handles:
    - Normal assignments: VAR=val, VAR="val", VAR='val'
    - Split assignments: VAR= followed by $, `, or tokens
    - Split with colons: VAR=+HEAD:main (where : was split by shlex)
    """
    if not tokens:
        return None

    tok0 = tokens[0]
    if _is_var_assignment(tok0):
        tok = tokens.pop(0)
        name, val = tok.split("=", 1)
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
        return name, _restore_sentinels(val)

    if len(tokens) >= 2 and tokens[0].isidentifier() and tokens[1] == "=":
        name = tokens.pop(0)
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
        return name, _restore_sentinels(val)

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
            name, val = assignment
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
                        name, val = sub_assignment
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
                        name, val = sub_assignment
                        env_vars[name] = val
                    else:
                        break
            continue

        if cmd_word == "command":
            tokens.pop(0)
            while tokens and (tokens[0] in {"-p", "-v", "-V"} or tokens[0] == "--"):
                tokens.pop(0)
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

        if cmd_word == "xargs":
            tokens = _unwrap_xargs(tokens)
            continue

        break

    return env_vars, tokens


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
        for i in range(1, len(tokens) - 1):
            token = tokens[i]
            if token == "--":
                break
            if (
                token.startswith("-")
                and not token.startswith("--")
                and len(token) > 1
                and "c" in token[1:]
            ):
                return contains_forced_git_push(
                    _restore_sentinels(tokens[i + 1]),
                    _depth=_depth + 1,
                    _inherited_env=env_vars,
                )
        return False

    if cmd_word == "eval":
        tokens.pop(0)
        return contains_forced_git_push(
            " ".join(_restore_sentinels(t) for t in tokens),
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
        for i in range(1, len(tokens) - 1):
            token = tokens[i]
            if token == "--":
                break
            if (
                token.startswith("-")
                and not token.startswith("--")
                and len(token) > 1
                and "c" in token[1:]
            ):
                return contains_forbidden_rm(
                    _restore_sentinels(tokens[i + 1]),
                    _depth=_depth + 1,
                    _inherited_env=env_vars,
                )
        return False

    if cmd_word == "eval":
        tokens.pop(0)
        return contains_forbidden_rm(
            " ".join(_restore_sentinels(t) for t in tokens),
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

    if _inspect_substitutions(command, contains_forced_git_push, _depth):
        return True

    commands = _tokenize_command_raw(command)
    for cmd_tokens in commands:
        cleaned = _clean_command_segment(cmd_tokens)
        if _inspect_single_command_git(
            cleaned, _depth=_depth, _inherited_env=_inherited_env
        ):
            return True

    return False


def contains_forbidden_rm(
    command: str,
    _depth: int = 0,
    _inherited_env: dict[str, str] | None = None,
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

    if _inspect_substitutions(command, contains_forbidden_rm, _depth):
        return True

    commands = _tokenize_command_raw(command)
    for cmd_tokens in commands:
        cleaned = _clean_command_segment(cmd_tokens)
        if _inspect_single_command_rm(
            cleaned, _depth=_depth, _inherited_env=_inherited_env
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
