#!/usr/bin/env python3
"""PreToolUse hook guard to deny git force-push commands.

Exposes contains_forced_git_push(command: str) -> bool and a CLI entrypoint
conforming to Claude PreToolUse hook protocol.
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


def _is_forced_push_args(push_args: list[str]) -> bool:
    """Check if arguments to 'git push' indicate a forced push."""
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


def _inspect_single_command(tokens: list[str]) -> bool:
    """Inspect a single clean command segment for forced git push."""
    idx = 0
    while idx < len(tokens) and _is_var_assignment(tokens[idx]):
        idx += 1
    tokens = tokens[idx:]

    if not tokens:
        return False

    while tokens:
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
                if t in {"-u", "-C", "-S"}:
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
                elif (
                    t.startswith("--unset=")
                    or t.startswith("--chdir=")
                    or t.startswith("--split-string=")
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
            while tokens and tokens[0] in {"-p", "-v", "-V"}:
                tokens.pop(0)
            continue

        if cmd_word in {"nohup", "builtin", "exec"}:
            tokens.pop(0)
            while tokens and tokens[0].startswith("-"):
                if tokens[0] == "-a":
                    tokens.pop(0)
                    if tokens:
                        tokens.pop(0)
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

        if cmd_word in {"sh", "bash", "zsh", "ksh", "dash"}:
            for i in range(len(tokens) - 1):
                if tokens[i] == "-c":
                    return contains_forced_git_push(tokens[i + 1])
            break

        if cmd_word == "eval":
            tokens.pop(0)
            return contains_forced_git_push(" ".join(tokens))

        break

    if not tokens:
        return False

    cmd_binary = os.path.basename(tokens[0])
    if cmd_binary != "git":
        return False

    git_args = tokens[1:]
    subcmd_idx = 0

    while subcmd_idx < len(git_args):
        arg = git_args[subcmd_idx]
        if arg == "--":
            subcmd_idx += 1
            break
        if not arg.startswith("-"):
            break

        if arg in GIT_GLOBAL_OPTS_WITH_ARG:
            subcmd_idx += 2
        else:
            subcmd_idx += 1

    if subcmd_idx >= len(git_args):
        return False

    subcmd = git_args[subcmd_idx]
    if subcmd != "push":
        return False

    push_args = git_args[subcmd_idx + 1 :]
    return _is_forced_push_args(push_args)


def contains_forced_git_push(command: str) -> bool:
    """Pure function checking whether a shell command contains a forced git push.

    Raises ValueError if the shell command syntax is invalid (e.g. unclosed quotes).
    """
    if not command or not command.strip():
        return False

    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace = " \t\r"
    lexer.commenters = "#"
    lexer.wordchars += "+%"

    tokens = list(lexer)
    if not tokens:
        return False

    commands = _split_into_commands(tokens)
    for cmd_tokens in commands:
        cleaned = _clean_command_segment(cmd_tokens)
        if _inspect_single_command(cleaned):
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

    sys.exit(0)


if __name__ == "__main__":
    main()
