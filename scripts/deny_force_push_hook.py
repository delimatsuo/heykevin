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
            while i < n and tokens[i] not in {"+", ";"}:
                cmd_tokens.append(tokens[i])
                i += 1
            if i < n and tokens[i] in {"+", ";"}:
                i += 1
            if cmd_tokens:
                replaced = [
                    t.replace("{}", FIND_INPUT_SENTINEL) for t in cmd_tokens
                ]
                actions.append(replaced)
        else:
            i += 1

    return actions


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
            if alias_name:
                alias_configs[alias_name] = (kind, setting)


def _parse_git_global_configs(
    git_args: list[str],
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Parse git global options, extracting alias configurations and remaining args.

    Returns (alias_configs, remaining_tokens) where alias_configs maps lowercase alias name
    to ('c', value) or ('config-env', env_var_name).
    """
    alias_configs: dict[str, tuple[str, str]] = {}
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
                i += 1
            continue

        if arg.startswith("-c") and not arg.startswith("-C"):
            val = arg[2:]
            _record_alias_config(alias_configs, val, "c")
            i += 1
            continue

        if arg == "--config-env":
            i += 1
            if i < len(git_args):
                _record_alias_config(alias_configs, git_args[i], "config-env")
                i += 1
            continue

        if arg.startswith("--config-env="):
            val = arg[len("--config-env="):]
            _record_alias_config(alias_configs, val, "config-env")
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

    return alias_configs, git_args[i:]


def _inspect_git_invocation(git_args: list[str]) -> bool:
    """Inspect git command arguments (after 'git') for forced push with alias resolution."""
    alias_configs, remaining = _parse_git_global_configs(git_args)
    if not remaining:
        return False

    visited: set[str] = set()
    depth = 0
    max_depth = 20
    current_tokens = list(remaining)

    while current_tokens:
        lead = current_tokens[0]
        lead_key = lead.lower()
        if lead_key in alias_configs:
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
                return contains_forced_git_push(shell_cmd)

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

    if not current_tokens:
        return False

    subcmd = current_tokens[0]
    if subcmd != "push":
        return False

    push_args = current_tokens[1:]
    return _is_forced_push_args(push_args)


def _inspect_git_invocation_for_rm(git_args: list[str]) -> bool:
    """Inspect git command arguments for shell aliases executing forbidden rm."""
    alias_configs, remaining = _parse_git_global_configs(git_args)
    if not remaining:
        return False

    visited: set[str] = set()
    depth = 0
    max_depth = 20
    current_tokens = list(remaining)

    while current_tokens:
        lead = current_tokens[0]
        lead_key = lead.lower()
        if lead_key in alias_configs:
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
                return contains_forbidden_rm(shell_cmd)

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


def _strip_comments_preserving_newlines(command: str) -> str:
    """Strip unquoted shell comments while preserving newline command boundaries.

    POSIX shell comments begin with an unquoted '#' at a word boundary (start of
    line, after whitespace, or after a command separator/operator) and extend to
    the next newline or EOF. The terminating newline is preserved as a command
    separator.

    Escaped semicolons in NORMAL state and semicolons inside quotes are replaced
    with _LITERAL_SEMICOLON_SENTINEL so they are not treated as command separators.
    """
    result: list[str] = []
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
            else:
                result.append(ch)
                prev_char = ch
                i += 1
                continue

        elif state == "DOUBLE_QUOTE":
            if ch == '"':
                state = "NORMAL"
                result.append(ch)
                prev_char = ch
                i += 1
                continue
            elif ch == "\\":
                if i + 1 < n and command[i + 1] == ";":
                    result.append(_LITERAL_SEMICOLON_SENTINEL)
                    prev_char = _LITERAL_SEMICOLON_SENTINEL[-1]
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
            else:
                result.append(ch)
                prev_char = ch
                i += 1
                continue

        elif state == "COMMENT":
            if ch == "\n":
                result.append("\n")
                state = "NORMAL"
                prev_char = "\n"
            i += 1
            continue

    return "".join(result)


def _tokenize_command(command: str) -> list[list[str]]:
    """Tokenize a shell command string and split into individual command segments."""
    cleaned = _strip_comments_preserving_newlines(command)
    lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=True)
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    lexer.wordchars += "+%{}"

    tokens = list(lexer)
    if not tokens:
        return []

    commands = _split_into_commands(tokens)
    return [
        [tok.replace(_LITERAL_SEMICOLON_SENTINEL, ";") for tok in cmd]
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
    if token in {">", ">>", "<", "<>", ">&", "<&", "&>", ">|", "1>", "2>", "1>>", "2>>"}:
        return True
    if len(token) >= 2 and token[0].isdigit() and token[1] in {">", "<"}:
        return True
    return False


def _split_into_commands(tokens: list[str]) -> list[list[str]]:
    """Split a stream of shell tokens into individual command segments."""
    commands: list[list[str]] = []
    current: list[str] = []

    for token in tokens:
        if token in COMMAND_SEPARATORS:
            if current:
                commands.append(current)
                current = []
        else:
            current.append(token)

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
            if token in {">", ">>", "<", "<>", ">&", "<&", "&>", ">|", "1>", "2>", "1>>", "2>>"}:
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


def _inspect_single_command_git(tokens: list[str]) -> bool:
    """Inspect a single clean command segment for forced git push."""
    idx = 0
    while idx < len(tokens) and _is_var_assignment(tokens[idx]):
        idx += 1
    tokens = tokens[idx:]

    if not tokens:
        return False

    while tokens:
        if XARGS_INPUT_SENTINEL in tokens[0]:
            raise ValueError(
                f"xargs dynamic executable is not supported: {tokens[0]!r}"
            )
        if FIND_INPUT_SENTINEL in tokens[0]:
            raise ValueError(
                f"find dynamic executable is not supported: {tokens[0]!r}"
            )

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
                elif _is_var_assignment(t):
                    tokens.pop(0)
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
                elif t in {"-u", "-C", "--unset", "--chdir"}:
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                elif (
                    t.startswith("--unset=")
                    or t.startswith("--chdir=")
                ):
                    tokens.pop(0)
                elif t.startswith("-"):
                    tokens.pop(0)
                elif _is_var_assignment(t):
                    tokens.pop(0)
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

        if cmd_word == "find":
            actions = _extract_find_actions(tokens)
            if not actions:
                return False
            for action in actions:
                if _inspect_single_command_git(action):
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
                    return contains_forced_git_push(tokens[i + 1])
            break

        if cmd_word == "eval":
            tokens.pop(0)
            return contains_forced_git_push(" ".join(tokens))

        break

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

    cmd_binary = os.path.basename(tokens[0])
    if cmd_binary != "git":
        return False

    return _inspect_git_invocation(tokens[1:])


def _inspect_single_command_rm(tokens: list[str]) -> bool:
    """Inspect a single clean command segment for forbidden destructive rm."""
    idx = 0
    while idx < len(tokens) and _is_var_assignment(tokens[idx]):
        idx += 1
    tokens = tokens[idx:]

    if not tokens:
        return False

    while tokens:
        if XARGS_INPUT_SENTINEL in tokens[0]:
            raise ValueError(
                f"xargs dynamic executable is not supported: {tokens[0]!r}"
            )
        if FIND_INPUT_SENTINEL in tokens[0]:
            raise ValueError(
                f"find dynamic executable is not supported: {tokens[0]!r}"
            )

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
                elif _is_var_assignment(t):
                    tokens.pop(0)
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
                elif t in {"-u", "-C", "--unset", "--chdir"}:
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                elif (
                    t.startswith("--unset=")
                    or t.startswith("--chdir=")
                ):
                    tokens.pop(0)
                elif t.startswith("-"):
                    tokens.pop(0)
                elif _is_var_assignment(t):
                    tokens.pop(0)
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

        if cmd_word == "find":
            actions = _extract_find_actions(tokens)
            if not actions:
                return False
            for action in actions:
                if _inspect_single_command_rm(action):
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
                    return contains_forbidden_rm(tokens[i + 1])
            break

        if cmd_word == "eval":
            tokens.pop(0)
            return contains_forbidden_rm(" ".join(tokens))

        break

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

    cmd_binary = os.path.basename(tokens[0])
    if cmd_binary == "git":
        return _inspect_git_invocation_for_rm(tokens[1:])
    if cmd_binary != "rm":
        return False

    rm_args = tokens[1:]
    return _is_forbidden_rm_args(rm_args)


def contains_forced_git_push(command: str) -> bool:
    """Pure function checking whether a shell command contains a forced git push.

    Raises ValueError if the shell command syntax is invalid (e.g. unclosed quotes).
    """
    if not command or not command.strip():
        return False

    commands = _tokenize_command(command)
    for cmd_tokens in commands:
        cleaned = _clean_command_segment(cmd_tokens)
        if _inspect_single_command_git(cleaned):
            return True

    return False


def contains_forbidden_rm(command: str) -> bool:
    """Pure function checking whether a shell command contains a forbidden destructive rm invocation.

    Raises ValueError if the shell command syntax is invalid (e.g. unclosed quotes).
    """
    if not command or not command.strip():
        return False

    commands = _tokenize_command(command)
    for cmd_tokens in commands:
        cleaned = _clean_command_segment(cmd_tokens)
        if _inspect_single_command_rm(cleaned):
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
