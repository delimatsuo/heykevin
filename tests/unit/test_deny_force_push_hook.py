"""Unit and contract tests for the Claude PreToolUse deny force-push and rm hook."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.deny_force_push_hook import (
    FIND_EXEC_ACTIONS,
    FIND_INPUT_SENTINEL,
    MAX_GIT_CONFIG_COUNT,
    XARGS_INPUT_SENTINEL,
    _build_git_config_env,
    _clean_command_segment,
    _extract_find_actions,
    _extract_initial_backtick_args,
    _extract_initial_dynamic_args,
    _extract_raw_substitutions,
    _has_forcing_git_config,
    _has_shell_expansion,
    _inspect_git_invocation,
    _inspect_git_invocation_for_rm,
    _is_all_parens,
    _is_forced_push_args,
    _is_git_config_protocol_key,
    _parse_backtick_body,
    _parse_git_env_configs,
    _parse_git_global_configs,
    _parse_paren_body,
    _reconstruct_git_args,
    _scan_git_forcing_configs,
    _split_into_commands,
    _tokenize_command,
    _tokenize_split_string,
    _unwrap_command_and_env,
    _unwrap_xargs,
    contains_forbidden_rm,
    contains_forced_git_push,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT_PATH = PROJECT_ROOT / "scripts" / "deny_force_push_hook.py"
SETTINGS_PATH = PROJECT_ROOT / ".claude" / "settings.json"

BLOCKED_GIT_PUSH_COMMANDS = [
    # Basic short option and grouped forms
    "git push -f",
    "git push -fu",
    "git push -qf",
    "git push -unf",
    "git push -uf origin main",
    "git push -f origin main",
    "git push -fu origin HEAD",
    # Options after remote/refspec
    "git push origin -f",
    "git push origin main -f",
    "git push origin main -fu",
    "git push origin main --force",
    # Long force options
    "git push --force",
    "git push --force origin main",
    "git push --force-with-lease",
    "git push --force-with-lease origin main",
    "git push --force-if-includes",
    "git push --force-if-includes origin main",
    "git push --force-with-lease=refs/heads/main:refs/heads/main origin main",
    # Forced refspecs
    "git push origin +main",
    "git push origin +main:main",
    "git push origin +HEAD:main",
    "git push origin +refs/heads/*:refs/heads/*",
    "git push origin +codex/new-feature",
    # Leading plus refspec after literal -- separator
    "git push origin -- +main",
    "git push -- +main",
    "git push origin -- +HEAD:main",
    # Absolute / path-qualified git binary
    "/usr/bin/git push -f",
    "/usr/local/bin/git push -fu origin main",
    "/opt/homebrew/bin/git push --force origin main",
    # Git global options
    "git --no-pager push -f",
    "git -P push -fu origin main",
    "git -C /path/to/repo push -f",
    "git -c user.name=Test push -f",
    "git --git-dir=/foo/.git push --force origin main",
    "git --work-tree=/foo push -fu origin main",
    # Compound commands and pipelines
    "git checkout main && git push -f",
    "git push origin main ; git push -fu origin main",
    "git push origin main || git push --force",
    "git push -f | cat",
    "git push -f |& tee log",
    "git push -f &",
    "git push origin main\ngit push -unf origin HEAD",
    "(git push -f)",
    "(git push -f origin HEAD)",
    "((git push -f origin HEAD))",
    "{ git push -f; }",
    # Variable assignments and wrappers
    "VAR=1 git push -f",
    "GIT_SSH_COMMAND=ssh git push -fu origin main",
    "sudo git push -f",
    "sudo -u deploy git push -f",
    "env FOO=bar git push -f",
    "command git push -f",
    "nohup git push -f",
    "timeout 30 git push -unf origin HEAD",
    "nice git push -f origin HEAD",
    "stdbuf -oL git push --force origin HEAD",
    "time git push -f",
    "/usr/bin/time -f %E git push -f origin HEAD",
    "time -f %E git push -f origin HEAD",
    "exec git push -f",
    'bash -c "git push -f"',
    'eval "git push -f"',
    "if true; then git push -f; fi",
    # Shell option combinations and unquoted comments with newlines
    "bash -lc 'git push -f'",
    "sh -ec 'git push --force origin main'",
    "true # comment\ngit push -f",
    "true # comment\ngit push -f origin main",
    "sh -c 'git push -f'",
    "zsh -c 'git push -f'",
    "ksh -c 'git push -f'",
    "dash -c 'git push -f'",
    # GNU env split-string options
    "env -S 'git push -f origin main'",
    "env --split-string 'git push --force origin main'",
    "env --split-string='git push -unf origin HEAD'",
    "env -S 'VAR=1 git push origin +HEAD:main'",
    "env -S 'VAR=1' git push -f origin main",
    # Git ordinary aliases starting with Git global options
    "git -c alias.fp='-c color.ui=false push -f' fp origin HEAD",
    "git -c alias.fp='--no-pager push -f' fp origin HEAD",
    "git -c alias.fp='--no-pager push --force' fp origin HEAD",
    "git -c alias.outer='-c alias.inner=\"push -f\" inner' outer origin HEAD",
    "git -c alias.a='-c color.ui=false b' -c alias.b='push -f' a origin HEAD",
    # Find execution multi-action forced push
    "find /tmp/tree -exec echo {} \\; -exec git push -f origin HEAD \\;",
    "find /tmp/tree -exec echo {} ';' -exec git push -f origin HEAD ';'",
    # Dynamic command substitutions with quoted or escaped literal parentheses
    "$(printf %s ')' >/dev/null; printf git) push -f origin HEAD",
    "$(printf %s ')' >/dev/null; printf git) push --mirror origin",
    '$(printf %s ")" >/dev/null; printf git) push -f origin HEAD',
    "$(printf %s \\) >/dev/null; printf git) push -f origin HEAD",
    "$(printf %s '(' >/dev/null; printf git) push -f origin HEAD",
    "$(printf %s '(' >/dev/null; printf git) push --mirror origin",
    "$(printf %s \\( >/dev/null; printf git) push -f origin HEAD",
    "command $(printf %s ')' >/dev/null; printf git) push -f origin HEAD",
    '"$(which git)" push -f origin HEAD',
    '"$(which git)" push --mirror origin',
    # Command-scoped Git config environment protocol forced push
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git fp origin HEAD",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=ALIAS.FP GIT_CONFIG_VALUE_0='push --force' git fp origin HEAD",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.forceit GIT_CONFIG_VALUE_0='push -f' git forceit origin main",
    "env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push --mirror' git fp origin",
    'env -S "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0=\'push -f\' git fp origin HEAD"',
    "GIT_CONFIG_VALUE_0='push -f' GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_COUNT=1 git fp origin HEAD",
    "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=core.foo GIT_CONFIG_VALUE_0=bar GIT_CONFIG_KEY_1=alias.fp GIT_CONFIG_VALUE_1='push -f' git fp origin HEAD",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0='+HEAD:main' git push origin",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=+HEAD:main git push origin",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=true git push origin",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' sh -c 'git fp origin HEAD'",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' eval 'git fp origin HEAD'",
    "find . -exec git -c alias.fp='push -f' fp origin HEAD ';'",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' find . -exec git fp origin HEAD ';'",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0=status git -c alias.fp='push -f' fp origin HEAD",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=false git -c remote.origin.mirror=true push origin HEAD",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0='+HEAD:main' git -c remote.origin.push=HEAD:other push origin",
    "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=false GIT_CONFIG_KEY_1=remote.origin.mirror GIT_CONFIG_VALUE_1=true git push origin HEAD",
    # Shell alias outer CLI config propagation into nested git
    "git -c alias.inner='push -f' -c alias.outer='!git inner origin HEAD' outer",
    "git -c remote.origin.mirror=true -c alias.outer='!git push origin' outer",
    "git -c remote.origin.push=+HEAD:main -c alias.outer='!git push origin' outer",
    "git -c remote.origin.mirror=false -c alias.outer='!git -c remote.origin.mirror=true push origin' outer",
    "git -c remote.origin.push=+HEAD:main -c alias.outer='!git -c remote.origin.push=HEAD:other push origin' outer",
    # Ignored alias.push regressions (Git ignores aliases named after existing built-in commands)
    "git -c alias.push=status push -f origin HEAD",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.push GIT_CONFIG_VALUE_0=status git push --force origin HEAD",
    "git -c alias.push='push origin' push --mirror origin",
    "git -c alias.push='!echo safe' push +HEAD:main",
    "git -c alias.push=status -c alias.outer='!git push -f origin HEAD' outer",
]

ALLOWED_GIT_PUSH_COMMANDS = [
    # Normal branches with hyphen or plus in branch name
    "git push origin codex/new-feature",
    "git push origin feature/--force-docs",
    "git push origin codex/c++",
    # Safe wrapper controls
    "env -S 'git push origin codex/new-feature'",
    "env --split-string='git push --follow-tags origin HEAD'",
    "env -S 'VAR=1' git push -u origin HEAD",
    "timeout 30 git push --follow-tags origin HEAD",
    "nice git push origin codex/new-feature",
    "stdbuf -oL git push origin codex/c++",
    "time git push -u origin HEAD",
    "/usr/bin/time -f %E git push -u origin HEAD",
    "time -f %E git push origin codex/c++",
    "bash -lc 'git push origin codex/new-feature'",
    "sh -ec 'git push --follow-tags origin HEAD'",
    # Non-force flags and standard pushes
    "git push --follow-tags origin HEAD",
    "git push -u origin HEAD",
    "git push origin main",
    "git push origin HEAD",
    "git push -v origin main",
    "git push --dry-run origin main",
    "git push -- origin main",
    # Other git subcommands (not push)
    "git log push -f",
    "git commit -m 'git push -f'",
    "git diff --check",
    "git status",
    "(git status)",
    "((git status))",
    "(git push origin main)",
    "git pull origin main",
    "git checkout -b feature/test",
    # Non-git commands or quoted git strings
    'echo "git push -f"',
    "echo 'git push -f'",
    'echo "git push origin +main"',
    "echo git push -f",
    "echo '# git push -f'",
    "echo 'rm -rf target'",
    "command rm -r target",
    "env rm -f target",
    "printf '%s' 'rm -rf target'",
    "true # comment\ngit push origin codex/c++",
    "true # comment\ncommand rm -r target",
    "pytest tests/unit",
    # Safe Git alias controls starting with Git global options
    "git -c alias.st='-c color.ui=false status --short' st",
    "git -c alias.st='--no-pager status --short' st",
    'git -c alias.outer=\'-c alias.inner="status --short" inner\' outer',
    "git -c alias.a='-c color.ui=false b' -c alias.b='status --short' a",
    # Safe Command-scoped Git config environment protocol controls
    "GIT_CONFIG_COUNT=0 git status",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.foo GIT_CONFIG_VALUE_0=bar git status",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.st GIT_CONFIG_VALUE_0=status git st",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push origin' git fp HEAD",
    "GIT_CONFIG_COUNT=1 echo safe",
    "GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git status",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' env -u GIT_CONFIG_COUNT git fp",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' env -i git fp",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=false git push origin",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=true git -c remote.origin.mirror=false push origin HEAD",
    "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=true GIT_CONFIG_KEY_1=remote.origin.mirror GIT_CONFIG_VALUE_1=false git push origin HEAD",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git -c alias.fp=status fp",
    "GIT_CONFIG_KEYBOARD=1 git status",
    "FOO=bar git status",
    # Safe shell alias precedence and env isolation controls
    "git -c remote.origin.mirror=true -c alias.outer='!git -c remote.origin.mirror=false push origin' outer",
    "git -c alias.st=status -c alias.outer='!git st' outer",
    "git -c alias.inner='push -f' -c alias.outer='!env -u GIT_CONFIG_COUNT git inner origin HEAD' outer",
    "git -c alias.inner='push -f' -c alias.outer='!env -i git inner origin HEAD' outer",
    # Ignored alias.push safe control (Git ignores alias.push; actual push is safe)
    "git -c alias.push='push -f' push origin HEAD",
    # Safe xargs controls
    "xargs echo hello </dev/null",
    "xargs -0 printf %s </dev/null",
    "xargs rm -- target </dev/null",
    "xargs -I{} rm -- {} </dev/null",
    "xargs -J% command rm -- % </dev/null",
    # Safe multi-action find controls
    "find /tmp/tree -exec echo {} \\; -exec rm -- {} \\;",
    "find /tmp/tree -exec echo {} ';' -exec printf '%s\\n' {} ';'",
    # Safe command substitutions with quoted or escaped literal parentheses
    "$(printf %s ')' >/dev/null; printf git) push origin main",
    "$(printf %s '(' >/dev/null; printf git) status",
    "command $(printf %s ')' >/dev/null; printf git) push origin main",
    '"$(which git)" push origin main',
    "echo ')'",
    "printf '%s' '('",
]

BLOCKED_RM_COMMANDS = [
    "rm -rf target",
    "rm -fr target",
    "rm -r -f target",
    "rm -R -f target",
    "rm -f -r target",
    "rm -f -R target",
    "rm --recursive --force target",
    "rm --force --recursive target",
    "rm -r --force target",
    "rm --recursive -f target",
    "rm -rfv target",
    "rm -vfr target",
    "/bin/rm -rf target",
    "/usr/bin/rm -rf target",
    "command rm -rf target",
    "env rm -fr target",
    "sudo /bin/rm -r -f target",
    "timeout 5 /usr/bin/rm --recursive --force target",
    "bash -lc 'rm -rf target'",
    "sh -ec 'rm -rf target'",
    "true # comment\ncommand rm -rf target",
    "sudo env command rm -rf target",
    "nice rm -rf target",
    "stdbuf -oL rm -rf target",
    "time rm -rf target",
    "nohup rm -rf target",
    "exec rm -rf target",
    "eval 'rm -rf target'",
    "if true; then rm -rf target; fi",
    "(rm -rf target)",
    "{ rm -rf target; }",
    # GNU env split-string options
    "env -S 'command rm -rf target'",
    "env --split-string 'rm -r -f target'",
    "env --split-string='VAR=1 /bin/rm --recursive --force target'",
    "env -S 'VAR=1' command rm -rf target",
    # Git alias with global options before shell rm
    "git -c alias.outer='--no-pager inner' -c alias.inner='!rm -rf target' outer",
    "git -c alias.outer='-c color.ui=false inner' -c alias.inner='!rm -rf target' outer",
    "git -c alias.outer='-c alias.inner=\"!rm -rf target\" inner' outer",
    "git -c alias.inner='!rm -rf target' -c alias.outer='!git inner' outer",
    # Command-scoped Git config environment protocol destructive rm
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wipe GIT_CONFIG_VALUE_0='!rm -rf target' git wipe",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wipe GIT_CONFIG_VALUE_0='!rm -rf target' sh -c 'git wipe'",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wipe GIT_CONFIG_VALUE_0='!rm -rf target' eval 'git wipe'",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wipe GIT_CONFIG_VALUE_0='!rm -rf target' find . -exec git wipe ';'",
    "env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wipe GIT_CONFIG_VALUE_0='!rm --recursive --force target' git wipe",
    # Multi-action find destructive rm and ordinary boundary controls
    "find /tmp/tree -exec echo {} \\; -exec rm -rf target \\;",
    "find /tmp/tree -exec echo {} ';' -exec rm -rf target ';'",
    "find /tmp/tree -exec echo {} \\; -exec rm -rf -- {} \\;",
    "find /tmp/tree -exec echo {} ';' -exec rm -rf -- {} ';'",
    "true; rm -rf target",
    "echo ';'; rm -rf target",
    "true # comment\nrm -rf target",
    # Dynamic command substitutions with quoted or escaped literal parentheses
    "$(printf %s ')' >/dev/null; printf rm) -rf /tmp/example",
    "$(printf %s '(' >/dev/null; printf rm) --recursive --force /tmp/example",
    "command $(printf %s ')' >/dev/null; printf rm) -r -f /tmp/example",
]

ALLOWED_RM_COMMANDS = [
    # GNU env split-string safe controls
    "env -S 'command rm -r target'",
    "env --split-string='rm -f target'",
    "env -S 'VAR=1' command rm -r target",
    "echo 'rm -rf target'",
    "printf '%s' 'rm -rf target'",
    "command rm -r target",
    "env rm -f target",
    "rm target",
    "rm -r target",
    "rm -R target",
    "rm -f target",
    "/bin/rm -r target",
    "/usr/bin/rm -f target",
    "sudo rm -r target",
    "timeout 5 rm -f target",
    "bash -lc 'rm -r target'",
    "sh -ec 'rm -f target'",
    "true # comment\ncommand rm -r target",
    "true # comment\ncommand rm -f target",
    "rm -- -rf",
    "rm -r -- -f",
    # Safe xargs controls
    "xargs echo hello </dev/null",
    "xargs -0 printf %s </dev/null",
    "xargs rm -- target </dev/null",
    "xargs -I{} rm -- {} </dev/null",
    "xargs -J% command rm -- % </dev/null",
    # Safe multi-action find and quoted semicolon controls
    "find /tmp/tree -exec echo {} \\; -exec rm -- {} \\;",
    "find /tmp/tree -exec echo {} ';' -exec printf '%s\\n' {} ';'",
    "echo ';'",
    'echo ";"',
    # Safe rm controls with quoted or escaped literal parentheses
    "$(printf %s ')' >/dev/null; printf rm) -r /tmp/example",
    "$(printf %s '(' >/dev/null; printf rm) --force /tmp/example",
    "echo ')'",
    # Safe shell alias rm controls
    "git -c alias.inner='!echo safe' -c alias.outer='!git inner' outer",
    # Ignored alias.push safe rm control (Git ignores alias.push; does not execute shell alias)
    "git -c alias.push='!rm -rf target' push origin HEAD",
]


class TestContainsForcedGitPush:
    """Pure-function tests for contains_forced_git_push."""

    @pytest.mark.parametrize("command", BLOCKED_GIT_PUSH_COMMANDS)
    def test_blocks_forced_push_variants(self, command: str) -> None:
        assert contains_forced_git_push(command) is True, f"Expected {command!r} to be blocked"

    @pytest.mark.parametrize("command", ALLOWED_GIT_PUSH_COMMANDS)
    def test_allows_safe_command_variants(self, command: str) -> None:
        assert contains_forced_git_push(command) is False, f"Expected {command!r} to be allowed"

    def test_empty_command_is_allowed(self) -> None:
        assert contains_forced_git_push("") is False
        assert contains_forced_git_push("   ") is False

    def test_unclosed_quote_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            contains_forced_git_push("git push 'unclosed string")

    def test_unclosed_split_string_quote_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            contains_forced_git_push("env -S 'git push \"unclosed string'")

    def test_split_string_with_backslash_separator_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="backslash escapes or variable expansions"):
            contains_forced_git_push("env -S 'git\\_push\\_-f\\_origin\\_main'")
        with pytest.raises(ValueError, match="backslash escapes or variable expansions"):
            contains_forced_git_push("env --split-string 'git\\_push\\_-f\\_origin\\_main'")

    @pytest.mark.parametrize(
        "command",
        [
            'FORCE=-f; git push "$FORCE" origin HEAD',
            'git push "${FORCE}" origin HEAD',
            'git push origin "$REFSPEC"',
            'git push "$(printf -- -f)" origin HEAD',
            "git push `printf -- -f` origin HEAD",
        ],
    )
    def test_shell_expansion_in_push_raises_value_error(self, command: str) -> None:
        with pytest.raises(ValueError, match="shell expansion"):
            contains_forced_git_push(command)

    @pytest.mark.parametrize(
        "command",
        [
            "xargs git push -f origin HEAD </dev/null",
            "xargs git push origin HEAD </dev/null",
            "xargs -0 -n 1 command git push origin HEAD </dev/null",
            "xargs -n1 -- git push --follow-tags origin HEAD </dev/null",
            "/usr/bin/xargs env VAR=1 git push origin HEAD </dev/null",
            "xargs -0n 1 git push -f origin HEAD </dev/null",
            "xargs -0n1 git push -f origin HEAD </dev/null",
            "xargs -rtP 2 git push origin HEAD </dev/null",
            "xargs -I{} {} push -f origin HEAD </dev/null",
            "xargs -I{} command {} push -f origin HEAD </dev/null",
        ],
    )
    def test_xargs_git_push_raises_value_error(self, command: str) -> None:
        with pytest.raises(ValueError, match=r"(shell expansion|xargs dynamic executable)"):
            contains_forced_git_push(command)


class TestContainsForbiddenRm:
    """Pure-function tests for contains_forbidden_rm."""

    @pytest.mark.parametrize("command", BLOCKED_RM_COMMANDS)
    def test_blocks_forbidden_rm_variants(self, command: str) -> None:
        assert contains_forbidden_rm(command) is True, f"Expected {command!r} to be blocked"

    @pytest.mark.parametrize("command", ALLOWED_RM_COMMANDS)
    def test_allows_safe_rm_variants(self, command: str) -> None:
        assert contains_forbidden_rm(command) is False, f"Expected {command!r} to be allowed"

    def test_empty_command_is_allowed(self) -> None:
        assert contains_forbidden_rm("") is False
        assert contains_forbidden_rm("   ") is False

    def test_unclosed_quote_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            contains_forbidden_rm("rm -rf 'unclosed string")

    def test_unclosed_split_string_quote_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            contains_forbidden_rm("env -S 'rm -rf \"unclosed string'")

    def test_split_string_with_backslash_separator_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="backslash escapes or variable expansions"):
            contains_forbidden_rm("env -S 'command\\_rm\\_-rf\\_target'")
        with pytest.raises(ValueError, match="backslash escapes or variable expansions"):
            contains_forbidden_rm("env --split-string 'command\\_rm\\_-rf\\_target'")

    @pytest.mark.parametrize(
        "command",
        [
            'OPTS=-rf; rm "$OPTS" target',
            'rm "${OPTS}" target',
            'rm "$(printf -- -rf)" target',
            "rm `printf -- -rf` target",
        ],
    )
    def test_shell_expansion_in_rm_raises_value_error(self, command: str) -> None:
        with pytest.raises(ValueError, match="shell expansion"):
            contains_forbidden_rm(command)

    @pytest.mark.parametrize(
        "command",
        [
            'rm -- "$TARGET"',
            'rm -r -- "$TARGET"',
        ],
    )
    def test_dynamic_operand_after_double_dash_is_allowed(self, command: str) -> None:
        assert contains_forbidden_rm(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "xargs rm -rf target </dev/null",
            "xargs rm -f target </dev/null",
            "xargs -I{} rm {} -- target </dev/null",
            "xargs -J% command rm % -- target </dev/null",
            "xargs -0n 1 rm -rf target </dev/null",
            "xargs -0n1 rm -rf target </dev/null",
            "xargs -rtP 2 rm -rf target </dev/null",
            "xargs -I{} {} -rf target </dev/null",
            "xargs -I{} command {} -rf target </dev/null",
        ],
    )
    def test_xargs_rm_raises_value_error(self, command: str) -> None:
        with pytest.raises(ValueError, match=r"(shell expansion|xargs dynamic executable)"):
            contains_forbidden_rm(command)

    @pytest.mark.parametrize(
        "command",
        [
            "xargs echo hello </dev/null",
            "xargs -0 printf %s </dev/null",
            "xargs rm -- target </dev/null",
            "xargs -I{} rm -- {} </dev/null",
            "xargs -J% command rm -- % </dev/null",
        ],
    )
    def test_xargs_safe_controls_return_false(self, command: str) -> None:
        assert contains_forbidden_rm(command) is False
        assert contains_forced_git_push(command) is False


class TestDenyForcePushHookCLI:
    """End-to-end subprocess tests for the hook CLI entrypoint."""

    def _run_hook(self, stdin_payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT_PATH)],
            input=stdin_payload,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_denies_forced_push_tool_input(self) -> None:
        payload = json.dumps({"toolInput": {"command": "git push -f"}})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        hook_out = data["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"
        assert "no-force-push" in hook_out["permissionDecisionReason"].lower()

    def test_cli_denies_grouped_short_option(self) -> None:
        payload = json.dumps({"tool_input": {"command": "git push -fu origin main"}})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_cli_denies_top_level_command_forced_refspec(self) -> None:
        payload = json.dumps({"command": "git push origin +main"})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_cli_denies_comment_newline_force_push(self) -> None:
        payload = json.dumps({"command": "true # comment\ngit push -f"})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "no-force-push" in data["hookSpecificOutput"]["permissionDecisionReason"].lower()

    def test_cli_denies_wrapped_rm(self) -> None:
        payload = json.dumps({"toolInput": {"command": "command rm -rf target"}})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        hook_out = data["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"
        assert "destructive" in hook_out["permissionDecisionReason"].lower()

    def test_cli_denies_comment_newline_wrapped_rm(self) -> None:
        payload = json.dumps({"command": "true # comment\ncommand rm -rf target"})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "destructive" in data["hookSpecificOutput"]["permissionDecisionReason"].lower()

    def test_cli_denies_sudo_rm_separate_flags(self) -> None:
        payload = json.dumps({"command": "sudo /bin/rm -r -f target"})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "destructive" in data["hookSpecificOutput"]["permissionDecisionReason"].lower()

    def test_cli_denies_timeout_rm_long_flags(self) -> None:
        payload = json.dumps({"command": "timeout 5 /usr/bin/rm --recursive --force target"})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "destructive" in data["hookSpecificOutput"]["permissionDecisionReason"].lower()

    def test_cli_denies_shell_grouped_rm(self) -> None:
        payload = json.dumps({"command": "bash -lc 'rm -rf target'"})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "destructive" in data["hookSpecificOutput"]["permissionDecisionReason"].lower()

    def test_cli_denies_split_string_force_push(self) -> None:
        payload = json.dumps({"toolInput": {"command": "env -S 'git push -f origin main'"}})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        hook_out = data["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"
        assert "no-force-push" in hook_out["permissionDecisionReason"].lower()

    def test_cli_denies_split_string_rm(self) -> None:
        payload = json.dumps(
            {"toolInput": {"command": "env --split-string='command rm -rf target'"}}
        )
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        hook_out = data["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"
        assert "destructive" in hook_out["permissionDecisionReason"].lower()

    def test_cli_allows_safe_push(self) -> None:
        payload = json.dumps({"toolInput": {"command": "git push origin main"}})
        res = self._run_hook(payload)
        assert res.returncode == 0
        assert res.stdout == ""

    def test_cli_allows_echo(self) -> None:
        payload = json.dumps({"toolInput": {"command": 'echo "git push -f"'}})
        res = self._run_hook(payload)
        assert res.returncode == 0
        assert res.stdout == ""

    @pytest.mark.parametrize(
        "cmd",
        [
            "bash -lc 'git push origin codex/new-feature'",
            "sh -ec 'git push --follow-tags origin HEAD'",
            "echo '# git push -f'",
            "echo 'rm -rf target'",
            "command rm -r target",
            "env rm -f target",
            "printf '%s' 'rm -rf target'",
            "true # comment\ngit push origin codex/c++",
            "true # comment\ncommand rm -r target",
            "env -S 'git push origin codex/new-feature'",
            "env --split-string='git push --follow-tags origin HEAD'",
            "env -S 'command rm -r target'",
            "env --split-string='rm -f target'",
            "env -S 'VAR=1' git push -u origin HEAD",
            "env -S 'VAR=1' command rm -r target",
        ],
    )
    def test_cli_allows_safe_mutation_controls(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        assert res.stdout == ""

    def test_cli_malformed_json_fails_closed(self) -> None:
        res = self._run_hook("{invalid json")
        assert res.returncode == 2
        assert "Malformed JSON" in res.stderr

    def test_cli_empty_stdin_fails_closed(self) -> None:
        res = self._run_hook("")
        assert res.returncode == 2
        assert "Empty input" in res.stderr

    def test_cli_missing_command_fails_closed(self) -> None:
        payload = json.dumps({"toolInput": {}})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Missing or non-string" in res.stderr

    def test_cli_non_string_command_fails_closed(self) -> None:
        payload = json.dumps({"command": 12345})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Missing or non-string" in res.stderr

    def test_cli_tokenization_error_fails_closed(self) -> None:
        payload = json.dumps({"command": "git push 'unclosed string"})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr

    def test_cli_malformed_split_string_fails_closed(self) -> None:
        payload = json.dumps({"command": "env -S 'git push \"unclosed string'"})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr

    def test_cli_env_split_string_backslash_separator_git_push_fails_closed(self) -> None:
        payload = json.dumps({"command": "env -S 'git\\_push\\_-f\\_origin\\_main'"})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr

    def test_cli_env_split_string_backslash_separator_rm_fails_closed(self) -> None:
        payload = json.dumps(
            {"command": "env --split-string 'command\\_rm\\_-rf\\_target'"}
        )
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr

    def test_cli_env_split_string_equals_backslash_separator_rm_fails_closed(self) -> None:
        payload = json.dumps(
            {"command": "env --split-string='command\\_rm\\_-rf\\_target'"}
        )
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr

    def test_cli_env_split_string_dollar_brace_expansion_fails_closed(self) -> None:
        payload = json.dumps({"command": "env -S '${CMD:-git} push -f origin main'"})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr

    @pytest.mark.parametrize(
        "command",
        [
            'FORCE=-f; git push "$FORCE" origin HEAD',
            'git push "${FORCE}" origin HEAD',
            'git push origin "$REFSPEC"',
            'git push "$(printf -- -f)" origin HEAD',
            "git push `printf -- -f` origin HEAD",
            'OPTS=-rf; rm "$OPTS" target',
            'rm "${OPTS}" target',
            'rm "$(printf -- -rf)" target',
            "rm `printf -- -rf` target",
        ],
    )
    def test_cli_shell_expansion_fails_closed(self, command: str) -> None:
        payload = json.dumps({"command": command})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr
        assert res.stdout == ""

    @pytest.mark.parametrize(
        "command",
        [
            "git push origin main",
            'rm -- "$TARGET"',
            'rm -r -- "$TARGET"',
            'echo "$VALUE"',
        ],
    )
    def test_cli_allows_safe_controls_with_dynamic_operands_or_unrelated(
        self, command: str
    ) -> None:
        payload = json.dumps({"command": command})
        res = self._run_hook(payload)
        assert res.returncode == 0
        assert res.stdout == ""

    @pytest.mark.parametrize(
        "command",
        [
            "xargs git push -f origin HEAD </dev/null",
            "xargs git push origin HEAD </dev/null",
            "xargs -0 -n 1 command git push origin HEAD </dev/null",
            "xargs -n1 -- git push --follow-tags origin HEAD </dev/null",
            "/usr/bin/xargs env VAR=1 git push origin HEAD </dev/null",
            "xargs -0n 1 git push -f origin HEAD </dev/null",
            "xargs -0n1 git push -f origin HEAD </dev/null",
            "xargs -rtP 2 git push origin HEAD </dev/null",
            "xargs -I{} {} push -f origin HEAD </dev/null",
            "xargs -I{} command {} push -f origin HEAD </dev/null",
            "xargs rm -rf target </dev/null",
            "xargs rm -f target </dev/null",
            "xargs -I{} rm {} -- target </dev/null",
            "xargs -J% command rm % -- target </dev/null",
            "xargs -0n 1 rm -rf target </dev/null",
            "xargs -0n1 rm -rf target </dev/null",
            "xargs -rtP 2 rm -rf target </dev/null",
            "xargs -I{} {} -rf target </dev/null",
            "xargs -I{} command {} -rf target </dev/null",
        ],
    )
    def test_cli_xargs_uncertainty_fails_closed(self, command: str) -> None:
        payload = json.dumps({"command": command})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr
        assert res.stdout == ""

    @pytest.mark.parametrize(
        "command",
        [
            "xargs echo hello </dev/null",
            "xargs -0 printf %s </dev/null",
            "xargs rm -- target </dev/null",
            "xargs -I{} rm -- {} </dev/null",
            "xargs -J% command rm -- % </dev/null",
        ],
    )
    def test_cli_xargs_safe_controls_allowed(self, command: str) -> None:
        payload = json.dumps({"command": command})
        res = self._run_hook(payload)
        assert res.returncode == 0
        assert res.stdout == ""


class TestHasShellExpansion:
    """Unit tests for _has_shell_expansion helper."""

    @pytest.mark.parametrize(
        "token",
        [
            "$VAR",
            "${VAR}",
            "$((1+1))",
            "$(cmd)",
            "$1",
            "$@",
            "$?",
            "`cmd`",
            "foo$bar",
            "foo`bar`",
        ],
    )
    def test_detects_expansion_markers(self, token: str) -> None:
        assert _has_shell_expansion(token) is True

    @pytest.mark.parametrize(
        "token",
        [
            "main",
            "origin",
            "-f",
            "--force",
            "+main",
            "--",
            "target",
            "feature/test-1",
        ],
    )
    def test_allows_plain_tokens(self, token: str) -> None:
        assert _has_shell_expansion(token) is False


class TestTokenizeCommand:
    """Direct unit tests for _tokenize_command and brace handling."""

    def test_xargs_braces_preserved_in_command_segment(self) -> None:
        raw_segments = _tokenize_command("xargs -I{} rm {} -- target </dev/null")
        assert raw_segments == [
            ["xargs", "-I{}", "rm", "{}", "--", "target", "<", "/dev/null"]
        ]
        cleaned = _clean_command_segment(raw_segments[0])
        assert cleaned == ["xargs", "-I{}", "rm", "{}", "--", "target"]

    def test_standalone_shell_group_braces_split_and_denied(self) -> None:
        segments = _tokenize_command("{ rm -rf target; }")
        assert segments == [["rm", "-rf", "target"]]
        assert contains_forbidden_rm("{ rm -rf target; }") is True

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ('echo "hello {world}"', [["echo", "hello {world}"]]),
            ("echo 'hello {world}'", [["echo", "hello {world}"]]),
            ('echo "{}"', [["echo", "{}"]]),
            ("echo '{}'", [["echo", "{}"]]),
            ("git push origin '+main'", [["git", "push", "origin", "+main"]]),
            ("git push origin +main", [["git", "push", "origin", "+main"]]),
            (
                "git push origin +HEAD:main",
                [["git", "push", "origin", "+HEAD", ":", "main"]],
            ),
            (
                "xargs -J% command rm % -- target",
                [["xargs", "-J%", "command", "rm", "%", "--", "target"]],
            ),
            (
                "time -f %E git push -f origin HEAD",
                [["time", "-f", "%E", "git", "push", "-f", "origin", "HEAD"]],
            ),
        ],
    )
    def test_quoted_braces_and_plus_percent_tokens_unaffected(
        self, command: str, expected: list[list[str]]
    ) -> None:
        assert _tokenize_command(command) == expected

    def test_multi_action_find_tokenization_escaped_and_quoted(self) -> None:
        cmd1 = "find /tmp/tree -exec echo {} \\; -exec rm -rf {} \\;"
        assert _tokenize_command(cmd1) == [
            [
                "find",
                "/tmp/tree",
                "-exec",
                "echo",
                "{}",
                ";",
                "-exec",
                "rm",
                "-rf",
                "{}",
                ";",
            ]
        ]
        cmd2 = "find /tmp/tree -exec echo {} ';' -exec git push -f origin HEAD {} ';'"
        assert _tokenize_command(cmd2) == [
            [
                "find",
                "/tmp/tree",
                "-exec",
                "echo",
                "{}",
                ";",
                "-exec",
                "git",
                "push",
                "-f",
                "origin",
                "HEAD",
                "{}",
                ";",
            ]
        ]

    def test_ordinary_shell_boundaries_with_quoted_semicolons_and_comments(self) -> None:
        assert _tokenize_command("true; rm -rf target") == [
            ["true"],
            ["rm", "-rf", "target"],
        ]
        assert _tokenize_command("echo ';'; rm -rf target") == [
            ["echo", ";"],
            ["rm", "-rf", "target"],
        ]
        assert _tokenize_command("true # comment\nrm -rf target") == [
            ["true"],
            ["rm", "-rf", "target"],
        ]


class TestTokenizeSplitString:
    """Direct unit tests for _tokenize_split_string helper."""

    def test_ordinary_plain_strings_tokenize_successfully(self) -> None:
        assert _tokenize_split_string("git push origin main") == [
            "git",
            "push",
            "origin",
            "main",
        ]
        assert _tokenize_split_string("command rm -r target") == [
            "command",
            "rm",
            "-r",
            "target",
        ]
        assert _tokenize_split_string("VAR=1 git push -u origin HEAD") == [
            "VAR=1",
            "git",
            "push",
            "-u",
            "origin",
            "HEAD",
        ]
        assert _tokenize_split_string("git push 'origin' \"main\"") == [
            "git",
            "push",
            "origin",
            "main",
        ]

    @pytest.mark.parametrize(
        "unsupported_str",
        [
            "git\\_push\\_-f\\_origin\\_main",
            "command\\_rm\\_-rf\\_target",
            "git\\ push",
            "git\\npush",
            "$VAR",
            "${CMD}",
            "${CMD:-git} push -f origin main",
            "git push $BRANCH",
            "VAR=$VAL git push",
        ],
    )
    def test_backslash_or_dollar_strings_raise_value_error(
        self, unsupported_str: str
    ) -> None:
        with pytest.raises(ValueError, match="backslash escapes or variable expansions"):
            _tokenize_split_string(unsupported_str)


class TestUnwrapXargs:
    """Unit tests for _unwrap_xargs helper."""

    def test_empty_tokens_returns_empty_list(self) -> None:
        assert _unwrap_xargs([]) == []

    @pytest.mark.parametrize(
        "tokens",
        [
            ["xargs"],
            ["xargs", "-0"],
            ["xargs", "-n", "1", "--"],
            ["xargs", "-n1", "--"],
            ["xargs", "--"],
            ["xargs", "-I"],
            ["xargs", "--replace"],
            ["xargs", "-P"],
        ],
    )
    def test_no_command_returns_empty_list(self, tokens: list[str]) -> None:
        assert _unwrap_xargs(tokens) == []

    def test_plain_xargs_appends_sentinel(self) -> None:
        tokens = ["xargs", "git", "push", "-f", "origin", "HEAD"]
        assert _unwrap_xargs(tokens) == [
            "git",
            "push",
            "-f",
            "origin",
            "HEAD",
            XARGS_INPUT_SENTINEL,
        ]

    def test_separate_short_options(self) -> None:
        tokens = [
            "xargs",
            "-a", "file.txt",
            "-d", "\n",
            "-E", "EOF",
            "-L", "10",
            "-n", "5",
            "-P", "4",
            "-R", "2",
            "-S", "256",
            "-s", "1024",
            "echo", "hi",
        ]
        assert _unwrap_xargs(tokens) == ["echo", "hi", XARGS_INPUT_SENTINEL]

    def test_attached_short_options(self) -> None:
        tokens = [
            "xargs",
            "-afile.txt",
            "-d\n",
            "-EEOF",
            "-L10",
            "-n5",
            "-P4",
            "-R2",
            "-S256",
            "-s1024",
            "echo", "hi",
        ]
        assert _unwrap_xargs(tokens) == ["echo", "hi", XARGS_INPUT_SENTINEL]

    def test_grouped_short_options_with_argument(self) -> None:
        tokens = ["xargs", "-0n", "1", "git", "push"]
        assert _unwrap_xargs(tokens) == ["git", "push", XARGS_INPUT_SENTINEL]

        tokens_attached = ["xargs", "-0n1", "git", "push"]
        assert _unwrap_xargs(tokens_attached) == ["git", "push", XARGS_INPUT_SENTINEL]

        tokens_grouped = ["xargs", "-rtP", "2", "git", "push"]
        assert _unwrap_xargs(tokens_grouped) == ["git", "push", XARGS_INPUT_SENTINEL]

    def test_optional_short_and_long_replace_default_braces(self) -> None:
        tokens_i = ["xargs", "-i", "git", "push"]
        assert _unwrap_xargs(tokens_i) == ["git", "push", XARGS_INPUT_SENTINEL]

        tokens_i_placeholder = ["xargs", "-i", "rm", "{}", "--", "target"]
        assert _unwrap_xargs(tokens_i_placeholder) == [
            "rm",
            XARGS_INPUT_SENTINEL,
            "--",
            "target",
            XARGS_INPUT_SENTINEL,
        ]

        tokens_replace = ["xargs", "--replace", "git", "push"]
        assert _unwrap_xargs(tokens_replace) == ["git", "push", XARGS_INPUT_SENTINEL]

        tokens_replace_placeholder = ["xargs", "--replace", "rm", "{}", "--", "target"]
        assert _unwrap_xargs(tokens_replace_placeholder) == [
            "rm",
            XARGS_INPUT_SENTINEL,
            "--",
            "target",
            XARGS_INPUT_SENTINEL,
        ]

    def test_optional_replace_explicit_string(self) -> None:
        tokens_i = ["xargs", "-i%", "echo", "%"]
        assert _unwrap_xargs(tokens_i) == [
            "echo",
            XARGS_INPUT_SENTINEL,
            XARGS_INPUT_SENTINEL,
        ]

        tokens_replace = ["xargs", "--replace=%", "echo", "%"]
        assert _unwrap_xargs(tokens_replace) == [
            "echo",
            XARGS_INPUT_SENTINEL,
            XARGS_INPUT_SENTINEL,
        ]

    def test_optional_short_e_and_l_do_not_consume_command_token(self) -> None:
        tokens_e = ["xargs", "-e", "git", "push"]
        assert _unwrap_xargs(tokens_e) == ["git", "push", XARGS_INPUT_SENTINEL]

        tokens_l = ["xargs", "-l", "git", "push"]
        assert _unwrap_xargs(tokens_l) == ["git", "push", XARGS_INPUT_SENTINEL]

        tokens_e_att = ["xargs", "-eEOF", "echo", "hi"]
        assert _unwrap_xargs(tokens_e_att) == ["echo", "hi", XARGS_INPUT_SENTINEL]

        tokens_l_att = ["xargs", "-l10", "echo", "hi"]
        assert _unwrap_xargs(tokens_l_att) == ["echo", "hi", XARGS_INPUT_SENTINEL]

    def test_optional_long_eof_and_max_lines_semantics(self) -> None:
        tokens_equals = ["xargs", "--eof=END", "--max-lines=2", "git", "push"]
        assert _unwrap_xargs(tokens_equals) == ["git", "push", XARGS_INPUT_SENTINEL]

        tokens_bare_eof = ["xargs", "--eof", "git", "push"]
        assert _unwrap_xargs(tokens_bare_eof) == ["git", "push", XARGS_INPUT_SENTINEL]

        tokens_bare_lines = ["xargs", "--max-lines", "git", "push"]
        assert _unwrap_xargs(tokens_bare_lines) == ["git", "push", XARGS_INPUT_SENTINEL]

    def test_long_options_separate(self) -> None:
        tokens = [
            "xargs",
            "--arg-file", "file.txt",
            "--delimiter", "\n",
            "--max-args", "5",
            "--max-procs", "4",
            "--max-chars", "1024",
            "--process-slot-var", "SLOT",
            "echo", "hi",
        ]
        assert _unwrap_xargs(tokens) == ["echo", "hi", XARGS_INPUT_SENTINEL]

    def test_long_options_equals(self) -> None:
        tokens = [
            "xargs",
            "--arg-file=file.txt",
            "--delimiter=\n",
            "--eof=EOF",
            "--max-lines=10",
            "--max-args=5",
            "--max-procs=4",
            "--max-chars=1024",
            "--process-slot-var=SLOT",
            "echo", "hi",
        ]
        assert _unwrap_xargs(tokens) == ["echo", "hi", XARGS_INPUT_SENTINEL]

    def test_no_arg_options_skipped(self) -> None:
        tokens = [
            "xargs",
            "-0",
            "-r",
            "-t",
            "-x",
            "-p",
            "-o",
            "--null",
            "--no-run-if-empty",
            "--verbose",
            "echo", "hi",
        ]
        assert _unwrap_xargs(tokens) == ["echo", "hi", XARGS_INPUT_SENTINEL]

    def test_double_dash_terminates_options(self) -> None:
        tokens = ["xargs", "-n1", "--", "git", "push", "--follow-tags", "origin", "HEAD"]
        assert _unwrap_xargs(tokens) == [
            "git",
            "push",
            "--follow-tags",
            "origin",
            "HEAD",
            XARGS_INPUT_SENTINEL,
        ]

    @pytest.mark.parametrize(
        ("tokens", "expected"),
        [
            (
                ["xargs", "-I{}", "rm", "{}", "--", "target"],
                ["rm", XARGS_INPUT_SENTINEL, "--", "target", XARGS_INPUT_SENTINEL],
            ),
            (
                ["xargs", "-I", "{}", "rm", "{}", "--", "target"],
                ["rm", XARGS_INPUT_SENTINEL, "--", "target", XARGS_INPUT_SENTINEL],
            ),
            (
                ["xargs", "-J%", "rm", "%", "--", "target"],
                ["rm", XARGS_INPUT_SENTINEL, "--", "target", XARGS_INPUT_SENTINEL],
            ),
            (
                ["xargs", "-J", "%", "rm", "%", "--", "target"],
                ["rm", XARGS_INPUT_SENTINEL, "--", "target", XARGS_INPUT_SENTINEL],
            ),
            (
                ["xargs", "--replace={}", "rm", "{}", "--", "target"],
                ["rm", XARGS_INPUT_SENTINEL, "--", "target", XARGS_INPUT_SENTINEL],
            ),
            (
                ["xargs", "--replace", "rm", "{}", "--", "target"],
                ["rm", XARGS_INPUT_SENTINEL, "--", "target", XARGS_INPUT_SENTINEL],
            ),
            (
                ["xargs", "-I{}", "rm", "--", "{}"],
                ["rm", "--", XARGS_INPUT_SENTINEL, XARGS_INPUT_SENTINEL],
            ),
        ],
    )
    def test_replacement_placeholders_substituted_and_appended(
        self, tokens: list[str], expected: list[str]
    ) -> None:
        assert _unwrap_xargs(tokens) == expected


class TestSettingsJsonContract:
    """Contract tests for .claude/settings.json hooks, permissions, and sandbox."""

    def test_settings_json_hooks_and_preservation(self) -> None:
        assert SETTINGS_PATH.exists(), f"Missing settings file at {SETTINGS_PATH}"
        raw = SETTINGS_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)

        # 1. Assert pre-existing permissions.deny remain intact
        expected_deny_entries = [
            "Read(./.env)",
            "Read(./.env.*)",
            "Read(./**/.env)",
            "Read(./**/.env.*)",
            "Read(.env)",
            "Read(.env.*)",
            "Read(**/.env)",
            "Read(**/.env.*)",
            "Bash(rm)",
            "Bash(rm *)",
            "Bash(/bin/rm)",
            "Bash(/bin/rm *)",
            "Bash(/usr/bin/rm)",
            "Bash(/usr/bin/rm *)",
            "Bash(git push -f)",
            "Bash(git push -f *)",
            "Bash(git push * -f)",
            "Bash(git push * -f *)",
            "Bash(git push --force*)",
            "Bash(git push * --force*)",
            "Bash(git push +*)",
            "Bash(git push * +*)",
        ]
        assert "permissions" in data
        assert "deny" in data["permissions"]
        for entry in expected_deny_entries:
            assert entry in data["permissions"]["deny"], f"Missing deny entry: {entry}"

        # 2. Assert sandbox configuration remains intact
        assert "sandbox" in data
        assert data["sandbox"]["enabled"] is True
        expected_sandbox_deny = [
            ".env",
            ".env.*",
            "**/.env",
            "**/.env.*",
            "./.env",
            "./.env.*",
            "./**/.env",
            "./**/.env.*",
        ]
        for entry in expected_sandbox_deny:
            assert (
                entry in data["sandbox"]["filesystem"]["denyRead"]
            ), f"Missing sandbox denyRead: {entry}"

        # 3. Assert hooks.PreToolUse contract
        assert "hooks" in data, "Top-level hooks object missing"
        assert "PreToolUse" in data["hooks"], "PreToolUse group missing in hooks"
        pre_tool_use = data["hooks"]["PreToolUse"]
        assert isinstance(pre_tool_use, list)
        assert len(pre_tool_use) == 1

        group = pre_tool_use[0]
        assert group.get("matcher") == "Bash"
        assert "hooks" in group
        assert isinstance(group["hooks"], list)
        assert len(group["hooks"]) == 1

        command_hook = group["hooks"][0]
        assert command_hook == {
            "type": "command",
            "command": 'python3 "$CLAUDE_PROJECT_DIR/scripts/deny_force_push_hook.py"',
            "timeout": 5,
        }
        assert "if" not in command_hook


class TestGitAliasResolution:
    """Direct unit tests for Git alias parsing and resolution."""

    def test_parse_git_global_configs_empty(self) -> None:
        configs, remaining = _parse_git_global_configs([])
        assert configs == {}
        assert remaining == []

    def test_parse_git_global_configs_extracts_c_and_config_env(self) -> None:
        args = [
            "-c", "alias.fp=push -f",
            "-calias.force=push --force",
            "-c", "user.name=Tester",
            "--config-env", "alias.env_fp=MY_FP",
            "--config-env=alias.env_att=ATT_VAR",
            "-C", "/path/to/repo",
            "--git-dir=/foo/.git",
            "--no-pager",
            "fp",
            "origin",
            "HEAD",
        ]
        configs, remaining = _parse_git_global_configs(args)
        assert configs == {
            "fp": ("c", "push -f"),
            "force": ("c", "push --force"),
            "env_fp": ("config-env", "MY_FP"),
            "env_att": ("config-env", "ATT_VAR"),
        }
        assert remaining == ["fp", "origin", "HEAD"]

    def test_parse_git_global_configs_last_value_wins_and_case_insensitivity(self) -> None:
        args = [
            "-c", "ALIAS.FP=status",
            "-c", "alias.fp=push -f",
            "fp",
        ]
        configs, remaining = _parse_git_global_configs(args)
        assert configs["fp"] == ("c", "push -f")
        assert remaining == ["fp"]

    def test_parse_git_global_configs_double_dash_terminates(self) -> None:
        args = [
            "-c", "alias.fp=push -f",
            "--",
            "-c", "alias.ignored=val",
            "fp",
        ]
        configs, remaining = _parse_git_global_configs(args)
        assert "fp" in configs
        assert "ignored" not in configs
        assert remaining == ["-c", "alias.ignored=val", "fp"]

    def test_parse_git_global_configs_ignores_alias_push(self) -> None:
        args = [
            "-c", "alias.push=status",
            "-c", "alias.fp=push -f",
            "push",
        ]
        configs, remaining = _parse_git_global_configs(args)
        assert configs == {"fp": ("c", "push -f")}
        assert remaining == ["push"]

    @pytest.mark.parametrize(
        "git_args",
        [
            ["-c", "alias.fp=push -f", "fp", "origin", "HEAD"],
            ["-calias.fp=push --force", "fp", "origin", "HEAD"],
            ["-c", "alias.fp=!git push -f", "fp", "origin", "HEAD"],
            ["-c", "alias.a=b", "-c", "alias.b=push -f", "a", "origin", "HEAD"],
            ["-c", "alias.a=b", "-c", "alias.b=c", "-c", "alias.c=push -f", "a", "origin", "HEAD"],
            ["-c", "ALIAS.FP=push -f", "fp", "origin", "HEAD"],
            ["-c", "alias.fp=push -f", "FP", "origin", "HEAD"],
            ["-c", "alias.fp=push --force-with-lease", "fp", "origin", "HEAD"],
            ["-c", "alias.fp=push origin +main", "fp"],
            # Ordinary aliases starting with Git global options
            ["-c", "alias.fp=-c color.ui=false push -f", "fp", "origin", "HEAD"],
            ["-c", "alias.fp=--no-pager push --force", "fp", "origin", "HEAD"],
            ["-c", "alias.outer=-c alias.inner='push -f' inner", "outer", "origin", "HEAD"],
            ["-c", "alias.a=-c color.ui=false b", "-c", "alias.b=push -f", "a", "origin", "HEAD"],
            ["-c", "alias.a=--no-pager b", "-c", "alias.b=-c color.ui=false push -f", "a", "origin", "HEAD"],
        ],
    )
    def test_inspect_git_invocation_blocks_forced_push_aliases(
        self, git_args: list[str]
    ) -> None:
        assert _inspect_git_invocation(git_args) is True

    @pytest.mark.parametrize(
        "git_args",
        [
            ["-c", "alias.co=checkout", "co", "main"],
            ["-c", "alias.say=!echo hi", "say"],
            ["-c", "alias.fp=push -f", "-c", "alias.fp=status", "fp"],
            ["-c", "alias.p=push", "p", "origin", "main"],
            ["-c", "alias.p=push", "p", "--follow-tags", "origin", "HEAD"],
            ["push", "origin", "main"],
            # Safe controls starting with Git global options
            ["-c", "alias.st=-c color.ui=false status --short", "st"],
            ["-c", "alias.st=--no-pager status --short", "st"],
            ["-c", "alias.outer=-c alias.inner='status --short' inner", "outer"],
            ["-c", "alias.a=-c color.ui=false b", "-c", "alias.b=status --short", "a"],
        ],
    )
    def test_inspect_git_invocation_allows_safe_aliases(
        self, git_args: list[str]
    ) -> None:
        assert _inspect_git_invocation(git_args) is False

    def test_inspect_git_invocation_cycle_raises_value_error(self) -> None:
        args = ["-c", "alias.a=b", "-c", "alias.b=a", "a", "origin", "HEAD"]
        with pytest.raises(ValueError, match="Git alias cycle detected"):
            _inspect_git_invocation(args)

    def test_inspect_git_invocation_self_cycle_raises_value_error(self) -> None:
        args = ["-c", "alias.self=self", "self", "origin", "HEAD"]
        with pytest.raises(ValueError, match="Git alias cycle detected"):
            _inspect_git_invocation(args)

    def test_inspect_git_invocation_config_env_raises_value_error(self) -> None:
        args = ["--config-env", "alias.fp=MY_VAR", "fp", "origin", "HEAD"]
        with pytest.raises(ValueError, match=r"--config-env which requires forbidden environment inspection"):
            _inspect_git_invocation(args)

    def test_inspect_git_invocation_config_env_unrelated_is_allowed(self) -> None:
        args = ["--config-env", "alias.other=MY_VAR", "push", "origin", "main"]
        assert _inspect_git_invocation(args) is False

    def test_inspect_git_invocation_ignores_alias_push(self) -> None:
        assert (
            _inspect_git_invocation(
                ["-c", "alias.push=status", "push", "-f", "origin", "HEAD"]
            )
            is True
        )
        assert (
            _inspect_git_invocation(
                ["-c", "alias.push=push -f", "push", "origin", "HEAD"]
            )
            is False
        )

    @pytest.mark.parametrize(
        "git_args",
        [
            ["-c", "alias.nuke=!rm -rf target", "nuke"],
            ["-c", "alias.outer=--no-pager inner", "-c", "alias.inner=!rm -rf target", "outer"],
            ["-c", "alias.outer=-c color.ui=false inner", "-c", "alias.inner=!rm -rf target", "outer"],
            ["-c", "alias.outer=-c alias.inner='!rm -rf target' inner", "outer"],
        ],
    )
    def test_inspect_git_invocation_for_rm_blocks_shell_nuke_alias(
        self, git_args: list[str]
    ) -> None:
        assert _inspect_git_invocation_for_rm(git_args) is True

    def test_inspect_git_invocation_for_rm_allows_ordinary_alias(self) -> None:
        args = ["-c", "alias.fp=push -f", "fp", "origin", "HEAD"]
        assert _inspect_git_invocation_for_rm(args) is False

    def test_inspect_git_invocation_for_rm_allows_safe_shell_alias(self) -> None:
        args = ["-c", "alias.say=!echo hi", "say"]
        assert _inspect_git_invocation_for_rm(args) is False

    def test_inspect_git_invocation_for_rm_ignores_alias_push(self) -> None:
        assert (
            _inspect_git_invocation_for_rm(
                ["-c", "alias.push=!rm -rf target", "push", "origin", "HEAD"]
            )
            is False
        )

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c alias.inner='push -f' -c alias.outer='!git inner origin HEAD' outer",
            "git -c remote.origin.mirror=true -c alias.outer='!git push origin' outer",
            "git -c remote.origin.push=+HEAD:main -c alias.outer='!git push origin' outer",
            "git -c remote.origin.mirror=false -c alias.outer='!git -c remote.origin.mirror=true push origin' outer",
            "git -c remote.origin.push=+HEAD:main -c alias.outer='!git -c remote.origin.push=HEAD:other push origin' outer",
        ],
    )
    def test_shell_alias_propagates_forcing_configs(self, cmd: str) -> None:
        assert contains_forced_git_push(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c remote.origin.mirror=true -c alias.outer='!git -c remote.origin.mirror=false push origin' outer",
            "git -c alias.st=status -c alias.outer='!git st' outer",
            "git -c alias.inner='push -f' -c alias.outer='!env -u GIT_CONFIG_COUNT git inner origin HEAD' outer",
            "git -c alias.inner='push -f' -c alias.outer='!env -i git inner origin HEAD' outer",
        ],
    )
    def test_shell_alias_safe_precedence_and_env_controls(self, cmd: str) -> None:
        assert contains_forced_git_push(cmd) is False

    def test_shell_alias_propagates_destructive_rm(self) -> None:
        assert (
            contains_forbidden_rm(
                "git -c alias.inner='!rm -rf target' -c alias.outer='!git inner' outer"
            )
            is True
        )
        assert (
            contains_forbidden_rm(
                "git -c alias.inner='!echo safe' -c alias.outer='!git inner' outer"
            )
            is False
        )

    def test_build_git_config_env_serializes_cleanly(self) -> None:
        alias_configs = {"inner": ("c", "push -f")}
        mirror_configs = {"remote.origin.mirror": ("c", "true")}
        push_configs = [("remote.origin.push", "c", "+HEAD:main")]
        env = _build_git_config_env(
            {"EXISTING": "1"}, alias_configs, mirror_configs, push_configs
        )
        assert env["EXISTING"] == "1"
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert env["GIT_CONFIG_KEY_0"] == "alias.inner"
        assert env["GIT_CONFIG_VALUE_0"] == "push -f"
        assert env["GIT_CONFIG_KEY_1"] == "remote.origin.mirror"
        assert env["GIT_CONFIG_VALUE_1"] == "true"
        assert env["GIT_CONFIG_KEY_2"] == "remote.origin.push"
        assert env["GIT_CONFIG_VALUE_2"] == "+HEAD:main"


class TestGitEnvConfigProtocol:
    """Direct unit, contract, and CLI tests for Git command-scoped environment config protocol."""

    def _run_hook(self, stdin_payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT_PATH)],
            input=stdin_payload,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_is_git_config_protocol_key(self) -> None:
        assert _is_git_config_protocol_key("GIT_CONFIG_COUNT") is True
        assert _is_git_config_protocol_key("GIT_CONFIG_COUNT_0") is False
        assert _is_git_config_protocol_key("GIT_CONFIG_KEY_0") is True
        assert _is_git_config_protocol_key("GIT_CONFIG_KEY_12") is True
        assert _is_git_config_protocol_key("GIT_CONFIG_KEY") is False
        assert _is_git_config_protocol_key("GIT_CONFIG_KEYBOARD") is False
        assert _is_git_config_protocol_key("GIT_CONFIG_VALUE_0") is True
        assert _is_git_config_protocol_key("GIT_CONFIG_VALUE_12") is True
        assert _is_git_config_protocol_key("GIT_CONFIG_VALUE") is False
        assert _is_git_config_protocol_key("FOO") is False
        assert _is_git_config_protocol_key("GIT_SSH_COMMAND") is False

    def test_parse_git_env_configs_empty_and_unrelated(self) -> None:
        configs, forcing = _parse_git_env_configs({})
        assert configs == {}
        assert forcing is False

        configs, forcing = _parse_git_env_configs({"FOO": "bar", "VAR": "1"})
        assert configs == {}
        assert forcing is False

        configs, forcing = _parse_git_env_configs({"GIT_CONFIG_KEYBOARD": "1"})
        assert configs == {}
        assert forcing is False

        configs, forcing = _parse_git_env_configs({
            "GIT_CONFIG_KEY_0": "alias.fp",
            "GIT_CONFIG_VALUE_0": "push -f",
        })
        assert configs == {}
        assert forcing is False

    def test_parse_git_env_configs_zero_count(self) -> None:
        configs, forcing = _parse_git_env_configs({"GIT_CONFIG_COUNT": "0"})
        assert configs == {}
        assert forcing is False

        configs, forcing = _parse_git_env_configs({
            "GIT_CONFIG_COUNT": "0",
            "GIT_CONFIG_KEY_0": "alias.fp",
            "GIT_CONFIG_VALUE_0": "push -f",
        })
        assert configs == {}
        assert forcing is False

    def test_parse_git_env_configs_valid_alias_and_forcing(self) -> None:
        env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "alias.fp",
            "GIT_CONFIG_VALUE_0": "push -f",
        }
        configs, forcing = _parse_git_env_configs(env)
        assert configs == {"fp": ("c", "push -f")}
        assert forcing is False

        env_upper = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "ALIAS.FP",
            "GIT_CONFIG_VALUE_0": "push --force",
        }
        configs, forcing = _parse_git_env_configs(env_upper)
        assert configs == {"fp": ("c", "push --force")}
        assert forcing is False

        env_push_alias = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "alias.push",
            "GIT_CONFIG_VALUE_0": "status",
        }
        configs, forcing = _parse_git_env_configs(env_push_alias)
        assert configs == {}
        assert forcing is False

        env_mirror = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "remote.origin.mirror",
            "GIT_CONFIG_VALUE_0": "true",
        }
        configs, forcing = _parse_git_env_configs(env_mirror)
        assert configs == {}
        assert forcing is True

        env_push = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "remote.origin.push",
            "GIT_CONFIG_VALUE_0": "+refs/heads/*:refs/heads/*",
        }
        configs, forcing = _parse_git_env_configs(env_push)
        assert configs == {}
        assert forcing is True

        env_push_colon = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "remote.origin.push",
            "GIT_CONFIG_VALUE_0": "+HEAD:main",
        }
        configs, forcing = _parse_git_env_configs(env_push_colon)
        assert configs == {}
        assert forcing is True

        env_safe_mirror = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "remote.origin.mirror",
            "GIT_CONFIG_VALUE_0": "false",
        }
        configs, forcing = _parse_git_env_configs(env_safe_mirror)
        assert configs == {}
        assert forcing is False

        env_multi = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.foo",
            "GIT_CONFIG_VALUE_0": "bar",
            "GIT_CONFIG_KEY_1": "alias.fp",
            "GIT_CONFIG_VALUE_1": "push -f",
        }
        configs, forcing = _parse_git_env_configs(env_multi)
        assert configs == {"fp": ("c", "push -f")}
        assert forcing is False

        env_extra_ignored = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "alias.fp",
            "GIT_CONFIG_VALUE_0": "push -f",
            "GIT_CONFIG_KEY_1": "core.foo",
            "GIT_CONFIG_VALUE_1": "bar",
        }
        configs, forcing = _parse_git_env_configs(env_extra_ignored)
        assert configs == {"fp": ("c", "push -f")}
        assert forcing is False

        env_mirror_last_safe = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "remote.origin.mirror",
            "GIT_CONFIG_VALUE_0": "true",
            "GIT_CONFIG_KEY_1": "remote.origin.mirror",
            "GIT_CONFIG_VALUE_1": "false",
        }
        configs, forcing = _parse_git_env_configs(env_mirror_last_safe)
        assert configs == {}
        assert forcing is False

        env_mirror_last_forced = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "remote.origin.mirror",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_KEY_1": "remote.origin.mirror",
            "GIT_CONFIG_VALUE_1": "true",
        }
        configs, forcing = _parse_git_env_configs(env_mirror_last_forced)
        assert configs == {}
        assert forcing is True

        env_push_multi_forced = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "remote.origin.push",
            "GIT_CONFIG_VALUE_0": "+HEAD:main",
            "GIT_CONFIG_KEY_1": "remote.origin.push",
            "GIT_CONFIG_VALUE_1": "HEAD:other",
        }
        configs, forcing = _parse_git_env_configs(env_push_multi_forced)
        assert configs == {}
        assert forcing is True

        env_push_multi_safe = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "remote.origin.push",
            "GIT_CONFIG_VALUE_0": "HEAD:main",
            "GIT_CONFIG_KEY_1": "remote.origin.push",
            "GIT_CONFIG_VALUE_1": "HEAD:other",
        }
        configs, forcing = _parse_git_env_configs(env_push_multi_safe)
        assert configs == {}
        assert forcing is False

    @pytest.mark.parametrize(
        "env",
        [
            {"GIT_CONFIG_COUNT": "-1"},
            {"GIT_CONFIG_COUNT": "one", "GIT_CONFIG_KEY_0": "alias.fp", "GIT_CONFIG_VALUE_0": "push -f"},
            {"GIT_CONFIG_COUNT": "$COUNT", "GIT_CONFIG_KEY_0": "alias.fp", "GIT_CONFIG_VALUE_0": "push -f"},
            {"GIT_CONFIG_COUNT": " 1 ", "GIT_CONFIG_KEY_0": "alias.fp", "GIT_CONFIG_VALUE_0": "push -f"},
            {"GIT_CONFIG_COUNT": "+1", "GIT_CONFIG_KEY_0": "alias.fp", "GIT_CONFIG_VALUE_0": "push -f"},
            {"GIT_CONFIG_COUNT": ""},
            {"GIT_CONFIG_COUNT": str(MAX_GIT_CONFIG_COUNT + 1)},
        ],
    )
    def test_parse_git_env_configs_malformed_count_raises_value_error(
        self, env: dict[str, str]
    ) -> None:
        with pytest.raises(ValueError):
            _parse_git_env_configs(env)

    @pytest.mark.parametrize(
        "env",
        [
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "alias.fp"},
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_VALUE_0": "push -f"},
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_1": "alias.fp", "GIT_CONFIG_VALUE_1": "push -f"},
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "$KEY", "GIT_CONFIG_VALUE_0": "push -f"},
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "alias.fp", "GIT_CONFIG_VALUE_0": "$VALUE"},
            {"GIT_CONFIG_COUNT": "$COUNT"},
            {"GIT_CONFIG_KEY_0": "$KEY"},
            {"GIT_CONFIG_VALUE_0": "$VALUE"},
        ],
    )
    def test_parse_git_env_configs_malformed_pairs_raises_value_error(
        self, env: dict[str, str]
    ) -> None:
        with pytest.raises(ValueError):
            _parse_git_env_configs(env)

    def test_unwrap_command_and_env(self) -> None:
        tokens = ["VAR1=a", "VAR2=b", "git", "status"]
        env_vars, remaining = _unwrap_command_and_env(tokens)
        assert env_vars == {"VAR1": "a", "VAR2": "b"}
        assert remaining == ["git", "status"]

        tokens_repeat = ["VAR=1", "VAR=2", "git", "status"]
        env_vars_repeat, remaining_repeat = _unwrap_command_and_env(tokens_repeat)
        assert env_vars_repeat == {"VAR": "2"}
        assert remaining_repeat == ["git", "status"]

        tokens_unset = ["VAR1=1", "env", "-u", "VAR1", "VAR2=2", "git", "status"]
        env_vars_unset, remaining_unset = _unwrap_command_and_env(tokens_unset)
        assert env_vars_unset == {"VAR2": "2"}
        assert remaining_unset == ["git", "status"]

        tokens_split = ["env", "-S", "VAR=1 git status"]
        env_vars_split, remaining_split = _unwrap_command_and_env(tokens_split)
        assert env_vars_split == {"VAR": "1"}
        assert remaining_split == ["git", "status"]

        tokens_sudo = ["sudo", "-u", "root", "VAR=1", "git", "status"]
        env_vars_sudo, remaining_sudo = _unwrap_command_and_env(tokens_sudo)
        assert env_vars_sudo == {"VAR": "1"}
        assert remaining_sudo == ["git", "status"]

        tokens_dynamic_val = ["GIT_CONFIG_VALUE_0=", "$", "VALUE", "git", "fp"]
        env_vars_dyn, remaining_dyn = _unwrap_command_and_env(tokens_dynamic_val)
        assert env_vars_dyn == {"GIT_CONFIG_VALUE_0": "$VALUE"}
        assert remaining_dyn == ["git", "fp"]

        tokens_colon = ["GIT_CONFIG_VALUE_0=+HEAD", ":", "main", "git", "push"]
        env_vars_col, remaining_col = _unwrap_command_and_env(tokens_colon)
        assert env_vars_col == {"GIT_CONFIG_VALUE_0": "+HEAD:main"}
        assert remaining_col == ["git", "push"]

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_CONFIG_COUNT=$COUNT GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=$KEY GIT_CONFIG_VALUE_0='push -f' git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0=$VALUE git fp",
            "GIT_CONFIG_VALUE_0=$VALUE git fp",
            "GIT_CONFIG_KEY_0=$KEY git fp",
            "GIT_CONFIG_COUNT=$COUNT git fp",
            "GIT_CONFIG_COUNT=one GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git fp",
            "GIT_CONFIG_COUNT=-1 git status",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_VALUE_0='push -f' git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_1=alias.fp GIT_CONFIG_VALUE_1='push -f' git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' env -u GIT_CONFIG_KEY_0 git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' GIT_CONFIG_COUNT=2 git fp",
        ],
    )
    def test_pinned_fail_closed_both_guards(self, cmd: str) -> None:
        with pytest.raises(ValueError):
            contains_forced_git_push(cmd)
        with pytest.raises(ValueError):
            contains_forbidden_rm(cmd)

    def test_last_assignment_wins_semantics(self) -> None:
        cmd_overwrite_to_forced = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp "
            "GIT_CONFIG_VALUE_0='push origin' GIT_CONFIG_VALUE_0='push -f' git fp origin HEAD"
        )
        assert contains_forced_git_push(cmd_overwrite_to_forced) is True

        cmd_overwrite_to_safe = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp "
            "GIT_CONFIG_VALUE_0='push -f' GIT_CONFIG_VALUE_0='push origin' git fp origin HEAD"
        )
        assert contains_forced_git_push(cmd_overwrite_to_safe) is False

        cmd_key_to_forced = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.st "
            "GIT_CONFIG_VALUE_0=status GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git fp origin HEAD"
        )
        assert contains_forced_git_push(cmd_key_to_forced) is True

        cmd_key_to_safe = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp "
            "GIT_CONFIG_VALUE_0='push -f' GIT_CONFIG_KEY_0=alias.st GIT_CONFIG_VALUE_0=status git fp origin HEAD"
        )
        assert contains_forced_git_push(cmd_key_to_safe) is False

        cmd_count_to_valid = (
            "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' "
            "GIT_CONFIG_COUNT=1 git fp origin HEAD"
        )
        assert contains_forced_git_push(cmd_count_to_valid) is True

    def test_nested_executors_preserve_environment(self) -> None:
        cmd_sh = "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' sh -c 'git fp origin HEAD'"
        assert contains_forced_git_push(cmd_sh) is True

        cmd_eval = "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' eval 'git fp origin HEAD'"
        assert contains_forced_git_push(cmd_eval) is True

        cmd_find = "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' find . -exec git fp origin HEAD ';'"
        assert contains_forced_git_push(cmd_find) is True

        cmd_reviewer_alias = "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.forceit GIT_CONFIG_VALUE_0='push -f' git forceit origin main"
        assert contains_forced_git_push(cmd_reviewer_alias) is True

        cmd_shell_alias_rm = "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wipe GIT_CONFIG_VALUE_0='!rm -rf target' git wipe"
        assert contains_forbidden_rm(cmd_shell_alias_rm) is True

        cmd_sh_wipe = "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wipe GIT_CONFIG_VALUE_0='!rm -rf target' sh -c 'git wipe'"
        assert contains_forbidden_rm(cmd_sh_wipe) is True

        cmd_eval_wipe = "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wipe GIT_CONFIG_VALUE_0='!rm -rf target' eval 'git wipe'"
        assert contains_forbidden_rm(cmd_eval_wipe) is True

        cmd_find_wipe = "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wipe GIT_CONFIG_VALUE_0='!rm -rf target' find . -exec git wipe ';'"
        assert contains_forbidden_rm(cmd_find_wipe) is True

    def test_cli_overrides_env_protocol(self) -> None:
        cmd_cli_override_safe = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git -c alias.fp=status fp"
        )
        assert contains_forced_git_push(cmd_cli_override_safe) is False

        cmd_cli_override_forced = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0=status git -c alias.fp='push -f' fp origin HEAD"
        )
        assert contains_forced_git_push(cmd_cli_override_forced) is True

        cmd_mirror_cli_override_safe = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=true git -c remote.origin.mirror=false push origin HEAD"
        )
        assert contains_forced_git_push(cmd_mirror_cli_override_safe) is False

        cmd_mirror_cli_override_forced = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=false git -c remote.origin.mirror=true push origin HEAD"
        )
        assert contains_forced_git_push(cmd_mirror_cli_override_forced) is True

        cmd_push_multi_valued_retained = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0='+HEAD:main' git -c remote.origin.push=HEAD:other push origin"
        )
        assert contains_forced_git_push(cmd_push_multi_valued_retained) is True

        cmd_env_mirror_last_safe = (
            "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=true GIT_CONFIG_KEY_1=remote.origin.mirror GIT_CONFIG_VALUE_1=false git push origin HEAD"
        )
        assert contains_forced_git_push(cmd_env_mirror_last_safe) is False

        cmd_env_mirror_last_forced = (
            "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=false GIT_CONFIG_KEY_1=remote.origin.mirror GIT_CONFIG_VALUE_1=true git push origin HEAD"
        )
        assert contains_forced_git_push(cmd_env_mirror_last_forced) is True

    def test_punctuation_in_forced_config_values(self) -> None:
        cmd_quoted_refspec = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0='+HEAD:main' git push origin"
        )
        assert contains_forced_git_push(cmd_quoted_refspec) is True

        cmd_unquoted_refspec = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=+HEAD:main git push origin"
        )
        assert contains_forced_git_push(cmd_unquoted_refspec) is True

        cmd_mirror_true = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=true git push origin"
        )
        assert contains_forced_git_push(cmd_mirror_true) is True

        cmd_mirror_false = (
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=false git push origin"
        )
        assert contains_forced_git_push(cmd_mirror_false) is False

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_CONFIG_COUNT=$COUNT GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=$KEY GIT_CONFIG_VALUE_0='push -f' git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0=$VALUE git fp",
            "GIT_CONFIG_VALUE_0=$VALUE git fp",
            "GIT_CONFIG_KEY_0=$KEY git fp",
            "GIT_CONFIG_COUNT=$COUNT git fp",
            "GIT_CONFIG_COUNT=one GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git fp",
            "GIT_CONFIG_COUNT=-1 git status",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_VALUE_0='push -f' git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_1=alias.fp GIT_CONFIG_VALUE_1='push -f' git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' env -u GIT_CONFIG_KEY_0 git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' GIT_CONFIG_COUNT=2 git fp",
        ],
    )
    def test_cli_fail_closed_exit_2(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr
        assert res.stdout == ""

    def test_cli_env_protocol_denies_forced_push_and_rm(self) -> None:
        payload_push = json.dumps({
            "command": "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git fp origin HEAD"
        })
        res_push = self._run_hook(payload_push)
        assert res_push.returncode == 0
        data_push = json.loads(res_push.stdout)
        assert data_push["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "no-force-push" in data_push["hookSpecificOutput"]["permissionDecisionReason"].lower()

        payload_reviewer = json.dumps({
            "command": "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.forceit GIT_CONFIG_VALUE_0='push -f' git forceit origin main"
        })
        res_reviewer = self._run_hook(payload_reviewer)
        assert res_reviewer.returncode == 0
        data_reviewer = json.loads(res_reviewer.stdout)
        assert data_reviewer["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "no-force-push" in data_reviewer["hookSpecificOutput"]["permissionDecisionReason"].lower()

        payload_rm = json.dumps({
            "command": "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wipe GIT_CONFIG_VALUE_0='!rm -rf target' git wipe"
        })
        res_rm = self._run_hook(payload_rm)
        assert res_rm.returncode == 0
        data_rm = json.loads(res_rm.stdout)
        assert data_rm["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "destructive" in data_rm["hookSpecificOutput"]["permissionDecisionReason"].lower()

        payload_refspec = json.dumps({
            "command": "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=+HEAD:main git push origin"
        })
        res_refspec = self._run_hook(payload_refspec)
        assert res_refspec.returncode == 0
        data_refspec = json.loads(res_refspec.stdout)
        assert data_refspec["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "no-force-push" in data_refspec["hookSpecificOutput"]["permissionDecisionReason"].lower()

        payload_mirror = json.dumps({
            "command": "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=true git push origin"
        })
        res_mirror = self._run_hook(payload_mirror)
        assert res_mirror.returncode == 0
        data_mirror = json.loads(res_mirror.stdout)
        assert data_mirror["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "no-force-push" in data_mirror["hookSpecificOutput"]["permissionDecisionReason"].lower()

        payload_mirror_cli_true = json.dumps({
            "command": (
                "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror "
                "GIT_CONFIG_VALUE_0=false git -c remote.origin.mirror=true push origin HEAD"
            )
        })
        res_mirror_cli_true = self._run_hook(payload_mirror_cli_true)
        assert res_mirror_cli_true.returncode == 0
        data_mirror_cli_true = json.loads(res_mirror_cli_true.stdout)
        assert data_mirror_cli_true["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "no-force-push" in data_mirror_cli_true["hookSpecificOutput"]["permissionDecisionReason"].lower()

        payload_push_multi = json.dumps({
            "command": (
                "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push "
                "GIT_CONFIG_VALUE_0='+HEAD:main' git -c remote.origin.push=HEAD:other push origin"
            )
        })
        res_push_multi = self._run_hook(payload_push_multi)
        assert res_push_multi.returncode == 0
        data_push_multi = json.loads(res_push_multi.stdout)
        assert data_push_multi["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "no-force-push" in data_push_multi["hookSpecificOutput"]["permissionDecisionReason"].lower()

        payload_mirror_last_true = json.dumps({
            "command": (
                "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=remote.origin.mirror "
                "GIT_CONFIG_VALUE_0=false GIT_CONFIG_KEY_1=remote.origin.mirror GIT_CONFIG_VALUE_1=true git push origin HEAD"
            )
        })
        res_mirror_last_true = self._run_hook(payload_mirror_last_true)
        assert res_mirror_last_true.returncode == 0
        data_mirror_last_true = json.loads(res_mirror_last_true.stdout)
        assert data_mirror_last_true["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "no-force-push" in data_mirror_last_true["hookSpecificOutput"]["permissionDecisionReason"].lower()

    def test_cli_env_protocol_allows_safe_commands(self) -> None:
        for cmd in [
            "GIT_CONFIG_COUNT=0 git status",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.foo GIT_CONFIG_VALUE_0=bar git status",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.st GIT_CONFIG_VALUE_0=status git st",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push origin' git fp HEAD",
            "GIT_CONFIG_COUNT=1 echo safe",
            "GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git status",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' env -u GIT_CONFIG_COUNT git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' env -i git fp",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=false git push origin",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=true git -c remote.origin.mirror=false push origin HEAD",
            "GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=remote.origin.mirror GIT_CONFIG_VALUE_0=true GIT_CONFIG_KEY_1=remote.origin.mirror GIT_CONFIG_VALUE_1=false git push origin HEAD",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.fp GIT_CONFIG_VALUE_0='push -f' git -c alias.fp=status fp",
            "GIT_CONFIG_KEYBOARD=1 git status",
        ]:
            payload = json.dumps({"command": cmd})
            res = self._run_hook(payload)
            assert res.returncode == 0
            assert res.stdout == ""


class TestFindExecutionActions:
    """Direct unit tests for find execution action extraction and inspection."""

    def test_extract_find_actions_empty(self) -> None:
        assert _extract_find_actions([]) == []
        assert _extract_find_actions(["find", "/tmp/tree", "-print"]) == []

    def test_extract_find_actions_all_action_types(self) -> None:
        for action in FIND_EXEC_ACTIONS:
            tokens = ["find", "/tmp/tree", action, "rm", "-rf", "{}", "+"]
            extracted = _extract_find_actions(tokens)
            assert extracted == [["rm", "-rf", FIND_INPUT_SENTINEL]]

    def test_extract_find_actions_multiple_actions(self) -> None:
        tokens = [
            "find", "/tmp/tree",
            "-exec", "echo", "{}", "+",
            "-name", "*.tmp",
            "-execdir", "command", "rm", "--", "{}", "+",
        ]
        extracted = _extract_find_actions(tokens)
        assert extracted == [
            ["echo", FIND_INPUT_SENTINEL],
            ["command", "rm", "--", FIND_INPUT_SENTINEL],
        ]

        tokens_semi = [
            "find", "/tmp/tree",
            "-exec", "echo", "{}", ";",
            "-execdir", "rm", "-rf", "{}", ";",
        ]
        extracted_semi = _extract_find_actions(tokens_semi)
        assert extracted_semi == [
            ["echo", FIND_INPUT_SENTINEL],
            ["rm", "-rf", FIND_INPUT_SENTINEL],
        ]

    def test_extract_find_actions_semicolon_terminator(self) -> None:
        tokens = ["find", "/tmp/tree", "-exec", "rm", "-rf", "{}", ";"]
        extracted = _extract_find_actions(tokens)
        assert extracted == [["rm", "-rf", FIND_INPUT_SENTINEL]]

    def test_extract_find_actions_end_of_segment_terminator(self) -> None:
        tokens = ["find", "/tmp/tree", "-exec", "rm", "-rf", "{}"]
        extracted = _extract_find_actions(tokens)
        assert extracted == [["rm", "-rf", FIND_INPUT_SENTINEL]]

    @pytest.mark.parametrize(
        "command",
        [
            "find /tmp/tree -exec git push -f origin HEAD +",
            "find /tmp/tree -execdir git push -fu origin main +",
            "find /tmp/tree -ok /usr/bin/git push --force origin HEAD +",
            "find /tmp/tree -okdir git push origin +main +",
            "find /tmp/tree -exec git -c alias.fp='push -f' fp origin HEAD +",
            "find /tmp/tree -exec sh -c 'git push -f origin HEAD' {} +",
            "sudo find /tmp/tree -exec git push -f origin HEAD +",
            "timeout 10 find /tmp/tree -exec git push -f origin HEAD +",
            "find /tmp/tree -exec echo {} \\; -exec git push -f origin HEAD \\;",
            "find /tmp/tree -exec echo {} ';' -exec git push -f origin HEAD ';'",
        ],
    )
    def test_contains_forced_git_push_blocks_find_actions(self, command: str) -> None:
        assert contains_forced_git_push(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "find /tmp/tree -exec git push -f origin HEAD {} +",
            "find /tmp/tree -exec {} push -f origin HEAD +",
            "find /tmp/tree -execdir {} push -f origin HEAD +",
            "find /tmp/tree -ok {} push -f origin HEAD +",
            "find /tmp/tree -okdir {} push -f origin HEAD +",
            "find /tmp/tree -exec echo {} ';' -exec git push -f origin HEAD {} ';'",
            "find /tmp/tree -exec echo {} \\; -exec git push -f origin HEAD {} \\;",
        ],
    )
    def test_contains_forced_git_push_find_dynamic_executable_raises_value_error(
        self, command: str
    ) -> None:
        with pytest.raises(ValueError, match=r"(shell expansion|dynamic executable)"):
            contains_forced_git_push(command)

    @pytest.mark.parametrize(
        "command",
        [
            "find /tmp/tree -print",
            "find /tmp/tree -exec echo {} +",
            "find /tmp/tree -exec rm -- {} +",
            "find /tmp/tree -execdir command rm -- {} +",
            "find /tmp/tree -exec rm -- {} \\;",
            "find /tmp/tree -execdir command rm -- {} \\;",
            "find /tmp/tree -name '*.txt' -print",
            "find /tmp/tree -type f -exec printf '%s\\n' {} +",
            "find /tmp/tree -exec echo {} \\; -exec rm -- {} \\;",
            "find /tmp/tree -exec echo {} ';' -exec printf '%s\\n' {} ';'",
        ],
    )
    def test_contains_forced_git_push_allows_safe_find_controls(
        self, command: str
    ) -> None:
        assert contains_forced_git_push(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "find /tmp/tree -exec rm -rf target +",
            "find /tmp/tree -exec rm -rf -- {} +",
            "find /tmp/tree -execdir command rm -rf -- {} +",
            "find /tmp/tree -ok rm -rf target +",
            "find /tmp/tree -okdir rm -rf target +",
            "find /tmp/tree -exec git -c alias.nuke='!rm -rf target' nuke +",
            "find /tmp/tree -exec sh -c 'rm -rf target' {} +",
            "sudo find /tmp/tree -exec rm -rf target +",
            "timeout 10 find /tmp/tree -exec rm -rf target +",
            "find /tmp/tree -exec echo {} \\; -exec rm -rf target \\;",
            "find /tmp/tree -exec echo {} ';' -exec rm -rf target ';'",
            "find /tmp/tree -exec echo {} \\; -exec rm -rf -- {} \\;",
            "find /tmp/tree -exec echo {} ';' -exec rm -rf -- {} ';'",
        ],
    )
    def test_contains_forbidden_rm_blocks_find_actions(self, command: str) -> None:
        assert contains_forbidden_rm(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "find /tmp/tree -exec rm -rf {} +",
            "find /tmp/tree -execdir command rm -rf {} +",
            "find /tmp/tree -ok rm -rf {} +",
            "find /tmp/tree -okdir rm -rf {} +",
            "find /tmp/tree -exec rm -rf {} \\;",
            "sudo find /tmp/tree -exec rm -rf {} +",
            "find /tmp/tree -exec {} -rf target +",
            "find /tmp/tree -exec command {} -rf target +",
            "find /tmp/tree -exec echo {} \\; -exec rm -rf {} \\;",
            "find /tmp/tree -exec echo {} ';' -exec rm -rf {} ';'",
        ],
    )
    def test_contains_forbidden_rm_find_uncertainty_raises_value_error(
        self, command: str
    ) -> None:
        with pytest.raises(ValueError, match=r"(shell expansion|dynamic executable)"):
            contains_forbidden_rm(command)

    @pytest.mark.parametrize(
        "command",
        [
            "find /tmp/tree -print",
            "find /tmp/tree -exec echo {} +",
            "find /tmp/tree -exec rm -- {} +",
            "find /tmp/tree -execdir command rm -- {} +",
            "find /tmp/tree -exec rm -- {} \\;",
            "find /tmp/tree -execdir command rm -- {} \\;",
            "find /tmp/tree -name '*.txt' -print",
            "find /tmp/tree -type f -exec printf '%s\\n' {} +",
            "find /tmp/tree -exec echo {} \\; -exec rm -- {} \\;",
            "find /tmp/tree -exec echo {} ';' -exec printf '%s\\n' {} ';'",
        ],
    )
    def test_contains_forbidden_rm_allows_safe_find_controls(
        self, command: str
    ) -> None:
        assert contains_forbidden_rm(command) is False


class TestReviewFindingsCLI:
    """CLI end-to-end contract tests covering exact review findings and safe controls."""

    def _run_hook(self, stdin_payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT_PATH)],
            input=stdin_payload,
            capture_output=True,
            text=True,
            check=False,
        )

    # Finding A - Exact review reproductions & variations
    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c alias.fp='push -f' fp origin HEAD",
            "git -calias.fp='push --force' fp origin HEAD",
            "git -c alias.fp='!git push -f' fp origin HEAD",
            "git -c alias.a=b -c alias.b='push -f' a origin HEAD",
            "git -c alias.a=b -c alias.b=c -c alias.c='push -f' a origin HEAD",
            "git -c ALIAS.FP='push -f' fp origin HEAD",
            "git -c alias.fp='push -f' FP origin HEAD",
            "sudo git -c alias.fp='push -f' fp origin HEAD",
            "env -S 'git -c alias.fp=\"push -f\" fp origin HEAD'",
            # Ordinary aliases starting with Git global options
            "git -c alias.fp='-c color.ui=false push -f' fp origin HEAD",
            "git -c alias.fp='--no-pager push -f' fp origin HEAD",
            "git -c alias.fp='--no-pager push --force' fp origin HEAD",
            "git -c alias.outer='-c alias.inner=\"push -f\" inner' outer origin HEAD",
            "git -c alias.a='-c color.ui=false b' -c alias.b='push -f' a origin HEAD",
        ],
    )
    def test_cli_denies_git_alias_forced_push(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        hook_out = data["hookSpecificOutput"]
        assert hook_out["permissionDecision"] == "deny"
        assert "no-force-push" in hook_out["permissionDecisionReason"].lower()

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c alias.nuke='!rm -rf target' nuke",
            "sudo git -c alias.nuke='!rm -rf target' nuke",
            "git -c ALIAS.NUKE='!rm -rf target' nuke",
            # Ordinary aliases with global options before shell rm
            "git -c alias.outer='--no-pager inner' -c alias.inner='!rm -rf target' outer",
            "git -c alias.outer='-c color.ui=false inner' -c alias.inner='!rm -rf target' outer",
            "git -c alias.outer='-c alias.inner=\"!rm -rf target\" inner' outer",
        ],
    )
    def test_cli_denies_git_alias_destructive_rm(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        hook_out = data["hookSpecificOutput"]
        assert hook_out["permissionDecision"] == "deny"
        assert "destructive" in hook_out["permissionDecisionReason"].lower()

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c alias.a=b -c alias.b=a a origin HEAD",
            "git -c alias.self=self self origin HEAD",
            "git --config-env alias.fp=MY_VAR fp origin HEAD",
            "git --config-env=alias.fp=MY_VAR fp origin HEAD",
        ],
    )
    def test_cli_git_alias_cycles_and_config_env_fail_closed(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -c alias.co=checkout co main",
            "git -c alias.say='!echo hi' say",
            "git -c alias.p=push p origin main",
            "git --config-env alias.other=MY_VAR push origin main",
            "git -c alias.fp='push -f' -c alias.fp='status' fp origin HEAD",
            # Safe controls starting with Git global options
            "git -c alias.st='-c color.ui=false status --short' st",
            "git -c alias.st='--no-pager status --short' st",
            'git -c alias.outer=\'-c alias.inner="status --short" inner\' outer',
            "git -c alias.a='-c color.ui=false b' -c alias.b='status --short' a",
        ],
    )
    def test_cli_allows_safe_git_aliases(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        assert res.stdout == ""

    # Finding B - Exact review reproductions & variations
    @pytest.mark.parametrize(
        "cmd",
        [
            "find /tmp/tree -exec rm -rf {} +",
            "find /tmp/tree -execdir command rm -rf {} +",
            "find /tmp/tree -ok rm -rf {} +",
            "find /tmp/tree -okdir rm -rf {} +",
            "find /tmp/tree -exec git push -f origin HEAD {} +",
            "find /tmp/tree -exec {} push -f origin HEAD +",
            "find /tmp/tree -execdir {} push -f origin HEAD +",
            "sudo find /tmp/tree -exec rm -rf {} +",
            "find /tmp/tree -exec rm -rf {} \\;",
            "find /tmp/tree -exec echo {} \\; -exec rm -rf {} \\;",
            "find /tmp/tree -exec echo {} ';' -exec rm -rf {} ';'",
            "find /tmp/tree -exec echo {} ';' -exec git push -f origin HEAD {} ';'",
            "find /tmp/tree -exec echo {} \\; -exec git push -f origin HEAD {} \\;",
        ],
    )
    def test_cli_find_actions_fail_closed(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr

    @pytest.mark.parametrize(
        "cmd",
        [
            "find /tmp/tree -exec git push -f origin HEAD +",
            "find /tmp/tree -exec sh -c 'git push -f origin HEAD' {} +",
            "find /tmp/tree -exec git -c alias.fp='push -f' fp origin HEAD +",
            "find /tmp/tree -exec echo {} \\; -exec git push -f origin HEAD \\;",
            "find /tmp/tree -exec echo {} ';' -exec git push -f origin HEAD ';'",
        ],
    )
    def test_cli_find_actions_deny_git_force_push(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "no-force-push" in data["hookSpecificOutput"]["permissionDecisionReason"].lower()

    @pytest.mark.parametrize(
        "cmd",
        [
            "find /tmp/tree -exec rm -rf target +",
            "find /tmp/tree -exec rm -rf -- {} +",
            "find /tmp/tree -execdir command rm -rf -- {} +",
            "find /tmp/tree -exec sh -c 'rm -rf target' {} +",
            "find /tmp/tree -exec git -c alias.nuke='!rm -rf target' nuke +",
            "find /tmp/tree -exec echo {} \\; -exec rm -rf target \\;",
            "find /tmp/tree -exec echo {} ';' -exec rm -rf target ';'",
            "find /tmp/tree -exec echo {} \\; -exec rm -rf -- {} \\;",
            "find /tmp/tree -exec echo {} ';' -exec rm -rf -- {} ';'",
        ],
    )
    def test_cli_find_actions_deny_destructive_rm(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "destructive" in data["hookSpecificOutput"]["permissionDecisionReason"].lower()

    @pytest.mark.parametrize(
        "cmd",
        [
            "find /tmp/tree -print",
            "find /tmp/tree -exec echo {} +",
            "find /tmp/tree -exec rm -- {} +",
            "find /tmp/tree -execdir command rm -- {} +",
            "find /tmp/tree -exec rm -- {} \\;",
            "find /tmp/tree -execdir command rm -- {} \\;",
            "find /tmp/tree -name '*.txt' -print",
            "find /tmp/tree -type f -exec printf '%s\\n' {} +",
            "find /tmp/tree -exec echo {} \\; -exec rm -- {} \\;",
            "find /tmp/tree -exec echo {} ';' -exec printf '%s\\n' {} ';'",
        ],
    )
    def test_cli_find_safe_controls_allowed(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        assert res.stdout == ""


class TestIsForcedPushArgs:
    """Pure-helper tests for _is_forced_push_args covering --mirror, forcing forms, and safe controls."""

    @pytest.mark.parametrize(
        "push_args",
        [
            ["--mirror"],
            ["--mirror", "origin"],
            ["origin", "--mirror"],
            ["--mirror", "origin", "main"],
            ["-u", "--mirror", "origin"],
            ["--mirror", "-u", "origin"],
            ["-f"],
            ["-fu", "origin", "main"],
            ["--force"],
            ["--force", "origin", "main"],
            ["--force-with-lease"],
            ["--force-with-lease", "origin", "main"],
            ["--force-if-includes"],
            ["--force-if-includes", "origin", "main"],
            ["+main"],
            ["origin", "+HEAD:main"],
            ["--", "+main"],
            ["origin", "--", "+HEAD:main"],
        ],
    )
    def test_blocks_forced_push_args(self, push_args: list[str]) -> None:
        assert _is_forced_push_args(push_args) is True

    @pytest.mark.parametrize(
        "push_args",
        [
            ["origin", "main"],
            ["origin", "HEAD"],
            ["-u", "origin", "HEAD"],
            ["--follow-tags", "origin", "HEAD"],
            ["--all", "origin"],
            ["--prune", "origin"],
            ["--tags", "origin"],
            ["--atomic", "origin", "main"],
            ["--all", "--prune", "--tags", "--follow-tags", "--atomic", "origin"],
            ["--dry-run", "origin", "main"],
            ["-v", "origin", "main"],
            ["--", "origin", "main"],
            ["--", "--mirror"],
        ],
    )
    def test_allows_safe_push_args(self, push_args: list[str]) -> None:
        assert _is_forced_push_args(push_args) is False

    @pytest.mark.parametrize(
        "push_args",
        [
            ["$FORCE"],
            ["origin", "$REFSPEC"],
            ["${FORCE}"],
            ["`printf -- -f`"],
            ["$(printf -- -f)"],
        ],
    )
    def test_shell_expansion_in_args_raises_value_error(self, push_args: list[str]) -> None:
        with pytest.raises(ValueError, match="shell expansion"):
            _is_forced_push_args(push_args)


class TestHasForcingGitConfig:
    """Pure-helper tests for _has_forcing_git_config and _scan_git_forcing_configs."""

    @pytest.mark.parametrize(
        "git_args",
        [
            ["-c", "remote.origin.mirror=true"],
            ["-c", "remote.origin.mirror=1"],
            ["-c", "remote.origin.mirror=yes"],
            ["-c", "remote.origin.mirror=on"],
            ["-c", "remote.origin.mirror=TRUE"],
            ["-c", "remote.origin.mirror=Yes"],
            ["-c", "remote.origin.mirror=ON"],
            ["-c", "remote.origin.mirror"],
            ["-cremote.origin.mirror=true"],
            ["-cremote.origin.mirror"],
            ["-c", "REMOTE.ORIGIN.MIRROR=true"],
            ["-c", "remote.upstream.mirror=true"],
            ["-c", "remote.custom-remote.mirror=true"],
            ["--config-env", "remote.origin.mirror=MY_ENV"],
            ["--config-env=remote.origin.mirror=MY_ENV"],
            ["--config-env", "remote.origin.mirror"],
            ["--config-env=remote.origin.mirror"],
            ["--config-env", "REMOTE.ORIGIN.MIRROR=MY_ENV"],
            ["-c", "remote.origin.push=+refs/heads/*:refs/remotes/origin/*"],
            ["-c", "remote.origin.push=+main"],
            ["-c", "remote.origin.push=+HEAD:main"],
            ["-cremote.origin.push=+main"],
            ["-c", "REMOTE.ORIGIN.PUSH=+main"],
            ["--config-env", "remote.origin.push=PUSH_SPEC"],
            ["--config-env=remote.origin.push=PUSH_SPEC"],
            ["--config-env", "REMOTE.ORIGIN.PUSH=PUSH_SPEC"],
            ["-C", "/path/to/repo", "-c", "remote.origin.mirror=true"],
            ["--git-dir=/foo/.git", "-c", "remote.origin.push=+main"],
            ["-c", "remote.origin.push=+main", "-c", "remote.origin.push=main"],
            ["-c", "remote.origin.push=main", "-c", "remote.origin.push=+main"],
        ],
    )
    def test_detects_forcing_git_configs(self, git_args: list[str]) -> None:
        assert _has_forcing_git_config(git_args) is True
        assert _scan_git_forcing_configs(git_args) is True

    @pytest.mark.parametrize(
        "git_args",
        [
            ["-c", "remote.origin.mirror=false"],
            ["-c", "remote.origin.mirror=no"],
            ["-c", "remote.origin.mirror=off"],
            ["-c", "remote.origin.mirror=0"],
            ["-c", "remote.origin.mirror=FALSE"],
            ["-c", "remote.origin.mirror=No"],
            ["-c", "remote.origin.mirror=OFF"],
            ["-cremote.origin.mirror=false"],
            ["-cremote.origin.mirror=0"],
            ["-c", "remote.origin.push=refs/heads/*:refs/remotes/origin/*"],
            ["-c", "remote.origin.push=main"],
            ["-c", "remote.origin.push=HEAD:main"],
            ["-cremote.origin.push=main"],
            ["-c", "user.name=Tester"],
            ["-c", "core.bare=true"],
            ["-c", "push.default=current"],
            ["-c", "branch.main.pushRemote=origin"],
            ["--config-env", "user.name=USER_NAME"],
            ["--config-env=user.email=USER_EMAIL"],
            ["-C", "/path/to/repo"],
            ["--git-dir=/repo/.git"],
            ["--work-tree=/repo"],
            ["--no-pager"],
            ["-c", "remote.origin.mirror=true", "-c", "remote.origin.mirror=false"],
            ["-c", "remote.origin.push=main", "-c", "remote.origin.push=refs/heads/*:refs/remotes/origin/*"],
            ["-c", "remote.origin.push=HEAD:main", "-c", "remote.origin.push=main"],
        ],
    )
    def test_allows_safe_and_explicit_false_configs(self, git_args: list[str]) -> None:
        assert _has_forcing_git_config(git_args) is False
        assert _scan_git_forcing_configs(git_args) is False

    def test_later_forcing_config_overrides_earlier_false(self) -> None:
        args = ["-c", "remote.origin.mirror=false", "-c", "remote.origin.mirror=true"]
        assert _has_forcing_git_config(args) is True

    def test_earlier_forcing_push_retains_forcing_with_later_safe(self) -> None:
        args = ["-c", "remote.origin.push=+main", "-c", "remote.origin.push=main"]
        assert _has_forcing_git_config(args) is True

    def test_later_forcing_push_overrides_earlier_safe(self) -> None:
        args = ["-c", "remote.origin.push=main", "-c", "remote.origin.push=+main"]
        assert _has_forcing_git_config(args) is True

    def test_safe_only_repeated_push_configs_are_not_forcing(self) -> None:
        args = ["-c", "remote.origin.push=main", "-c", "remote.origin.push=HEAD:main"]
        assert _has_forcing_git_config(args) is False


class TestExtractInitialBacktickArgs:
    """Unit tests for _extract_initial_backtick_args helper."""

    def test_non_backtick_starts_returns_none(self) -> None:
        assert _extract_initial_backtick_args([]) is None
        assert _extract_initial_backtick_args(["git", "push"]) is None
        assert _extract_initial_backtick_args(['"$GIT"', "push", "-f"]) is None

    @pytest.mark.parametrize(
        ("tokens", "expected"),
        [
            (
                ["`", "which", "git", "`", "push", "-f", "origin", "HEAD"],
                ["push", "-f", "origin", "HEAD"],
            ),
            (
                ["`", "which", "git", "`", "push", "--mirror", "origin"],
                ["push", "--mirror", "origin"],
            ),
            (
                ["`", "which", "git", "`", "push", "origin", "main"],
                ["push", "origin", "main"],
            ),
            (
                ["`", "which", "git", "`", "status"],
                ["status"],
            ),
            (
                ["`", "which", "rm", "`", "-rf", "target"],
                ["-rf", "target"],
            ),
            (
                ["`", "which", "rm", "`", "-r", "target"],
                ["-r", "target"],
            ),
            (
                ["`", "which", "git", "`"],
                [],
            ),
        ],
    )
    def test_matched_backtick_returns_trailing_args(
        self, tokens: list[str], expected: list[str]
    ) -> None:
        assert _extract_initial_backtick_args(tokens) == expected

    @pytest.mark.parametrize(
        "tokens",
        [
            ["`"],
            ["`", "which", "git"],
            ["`", "which", "rm", "-rf", "target"],
        ],
    )
    def test_unmatched_backtick_raises_value_error(self, tokens: list[str]) -> None:
        with pytest.raises(ValueError, match="Unmatched opening backtick"):
            _extract_initial_backtick_args(tokens)


class TestIsAllParens:
    """Unit tests for _is_all_parens helper."""

    @pytest.mark.parametrize(
        "token",
        [
            "(",
            ")",
            "((",
            "))",
            "(((",
            ")))",
            "()",
            ")(",
            "(())",
            "()()",
        ],
    )
    def test_recognizes_all_parenthesis_tokens(self, token: str) -> None:
        assert _is_all_parens(token) is True

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "&&",
            "||",
            ";",
            ";;",
            "|&",
            "&",
            ">",
            "<",
            "push",
            "-f",
            "$(",
            "${",
            "git",
            "which",
            "a(b)",
            "(a)",
        ],
    )
    def test_rejects_non_parenthesis_tokens(self, token: str) -> None:
        assert _is_all_parens(token) is False


class TestExtractInitialDynamicArgs:
    """Unit tests for _extract_initial_dynamic_args helper."""

    def test_non_dynamic_starts_returns_none(self) -> None:
        assert _extract_initial_dynamic_args([]) is None
        assert _extract_initial_dynamic_args(["git", "push"]) is None
        assert _extract_initial_dynamic_args(['"$GIT"', "push", "-f"]) is None

    @pytest.mark.parametrize(
        ("tokens", "expected"),
        [
            (
                ["`", "which", "git", "`", "push", "-f", "origin", "HEAD"],
                ["push", "-f", "origin", "HEAD"],
            ),
            (
                ["$", "GIT", "push", "-f", "origin", "HEAD"],
                ["push", "-f", "origin", "HEAD"],
            ),
            (
                ["$", "{GIT}", "push", "--mirror", "origin"],
                ["push", "--mirror", "origin"],
            ),
            (
                ["$", "(", "which", "git", ")", "push", "-f", "origin", "HEAD"],
                ["push", "-f", "origin", "HEAD"],
            ),
            (
                ["$", "(", "which", "git", ")", "push", "--mirror", "origin"],
                ["push", "--mirror", "origin"],
            ),
            (
                ["$", "(", "$", "(", "which", "echo", ")", "git", ")", "push", "-f", "origin", "HEAD"],
                ["push", "-f", "origin", "HEAD"],
            ),
            (
                ["$", "(", "which", "$", "(", "echo", "git", "))", "push", "-f", "origin", "HEAD"],
                ["push", "-f", "origin", "HEAD"],
            ),
            (
                ["$", "(", "echo", "$", "(", "which", "git", "))", "push", "-f", "origin", "HEAD"],
                ["push", "-f", "origin", "HEAD"],
            ),
            (
                ["$", "(", "echo", "$", "(", "which", "git", "))", "push", "--mirror", "origin"],
                ["push", "--mirror", "origin"],
            ),
            (
                ["$", "(", "echo", "$", "(", "which", "git", "))", "push", "origin", "main"],
                ["push", "origin", "main"],
            ),
            (
                ["$", "(", "echo", "$", "(", "which", "git", "))", "status"],
                ["status"],
            ),
            (
                ["$", "(", "echo", "$", "(", "which", "rm", "))", "-rf", "/tmp/example"],
                ["-rf", "/tmp/example"],
            ),
            (
                ["$", "(", "echo", "$", "(", "which", "rm", "))", "--recursive", "--force", "/tmp/example"],
                ["--recursive", "--force", "/tmp/example"],
            ),
            (
                ["$", "(", "echo", "$", "(", "which", "rm", "))", "-r", "/tmp/example"],
                ["-r", "/tmp/example"],
            ),
            (
                ["$", "(", "echo", "$", "(", "which", "rm", "))", "--force", "/tmp/example"],
                ["--force", "/tmp/example"],
            ),
            (
                ["$", "(", "which", "$", "(", "echo", "git", ")))", "push", "-f", "origin", "HEAD"],
                [")", "push", "-f", "origin", "HEAD"],
            ),
            (
                ["$", "RM", "-rf", "target"],
                ["-rf", "target"],
            ),
            (
                ["$", "{RM}", "--recursive", "--force", "target"],
                ["--recursive", "--force", "target"],
            ),
            (
                ["$", "(", "which", "rm", ")", "-rf", "target"],
                ["-rf", "target"],
            ),
            (
                ["$", "(", "which", "git", ")", "push", "origin", "main"],
                ["push", "origin", "main"],
            ),
            (
                ["$", "GIT"],
                [],
            ),
            (
                ["$", "{GIT}"],
                [],
            ),
            (
                ["$", "(", "which", "git", ")"],
                [],
            ),
            (
                ["$", "(", "which", "$", "(", "echo", "git", "))"],
                [],
            ),
        ],
    )
    def test_matched_dynamic_prefixes_return_trailing_args(
        self, tokens: list[str], expected: list[str]
    ) -> None:
        assert _extract_initial_dynamic_args(tokens) == expected

    @pytest.mark.parametrize(
        "tokens",
        [
            ["`"],
            ["`", "which", "git"],
            ["$"],
            ["$", "("],
            ["$", "(", "which", "git"],
            ["$", "(", "which", "$", "(", "echo", "git", ")"],
            ["$", "(", "$", "(", "which", "git"],
            ["$", "()"],
            ["$", "{GIT"],
            ["$", "{}"],
            ["$", "{123}"],
            ["$", "123"],
            ["$", "$"],
        ],
    )
    def test_unmatched_or_malformed_dynamic_prefix_raises_value_error(
        self, tokens: list[str]
    ) -> None:
        with pytest.raises(ValueError):
            _extract_initial_dynamic_args(tokens)


class TestSplitIntoCommandsDynamicSubstitution:
    """Unit tests for _split_into_commands preserving command substitution groups."""

    def test_command_substitution_preserved_in_segment(self) -> None:
        tokens = ["$", "(", "which", "git", ")", "push", "-f", "origin", "HEAD"]
        assert _split_into_commands(tokens) == [
            ["$", "(", "which", "git", ")", "push", "-f", "origin", "HEAD"]
        ]

    def test_command_substitution_after_wrapper(self) -> None:
        tokens = ["command", "$", "(", "which", "git", ")", "push", "-f", "origin", "HEAD"]
        assert _split_into_commands(tokens) == [
            ["command", "$", "(", "which", "git", ")", "push", "-f", "origin", "HEAD"]
        ]

    def test_nested_command_substitution(self) -> None:
        tokens = ["$", "(", "$", "(", "which", "echo", ")", "git", ")", "push", "-f"]
        assert _split_into_commands(tokens) == [
            ["$", "(", "$", "(", "which", "echo", ")", "git", ")", "push", "-f"]
        ]

    def test_nested_command_substitution_adjacent_closing_parens(self) -> None:
        tokens = ["$", "(", "which", "$", "(", "echo", "git", "))", "push", "-f", "origin", "HEAD"]
        assert _split_into_commands(tokens) == [
            ["$", "(", "which", "$", "(", "echo", "git", "))", "push", "-f", "origin", "HEAD"]
        ]

    def test_nested_command_substitution_after_wrapper_adjacent_closing_parens(self) -> None:
        tokens = ["command", "$", "(", "echo", "$", "(", "which", "git", "))", "push", "-f", "origin", "HEAD"]
        assert _split_into_commands(tokens) == [
            ["command", "$", "(", "echo", "$", "(", "which", "git", "))", "push", "-f", "origin", "HEAD"]
        ]

    def test_command_substitution_with_extra_closing_paren_remainder(self) -> None:
        tokens = ["$", "(", "which", "$", "(", "echo", "git", ")))", "push", "-f"]
        assert _split_into_commands(tokens) == [
            ["$", "(", "which", "$", "(", "echo", "git", "))"],
            ["push", "-f"],
        ]

    def test_unmatched_command_substitution_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unmatched '\\$\\(' in command substitution"):
            _split_into_commands(["$", "(", "which", "git"])

    def test_unmatched_nested_command_substitution_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unmatched '\\$\\(' in command substitution"):
            _split_into_commands(["$", "(", "which", "$", "(", "echo", "git", ")"])
        with pytest.raises(ValueError, match="Unmatched '\\$\\(' in command substitution"):
            _split_into_commands(["$", "(", "$", "(", "which", "git"])


class TestReconstructGitArgs:
    """Unit tests for _reconstruct_git_args helper."""

    def test_empty_and_plain_args_unaffected(self) -> None:
        assert _reconstruct_git_args([]) == []
        assert _reconstruct_git_args(["push", "origin", "main"]) == ["push", "origin", "main"]
        assert _reconstruct_git_args(["--no-pager", "status"]) == ["--no-pager", "status"]

    def test_colon_separated_c_config_reconstruction(self) -> None:
        raw = ["-c", "remote.origin.push=+refs/heads/*", ":", "refs/remotes/origin/*", "push", "origin"]
        expected = ["-c", "remote.origin.push=+refs/heads/*:refs/remotes/origin/*", "push", "origin"]
        assert _reconstruct_git_args(raw) == expected

    def test_attached_c_and_config_env_reconstruction(self) -> None:
        raw_att = ["-cremote.origin.push=+refs/heads/*", ":", "refs/remotes/origin/*", "push", "origin"]
        expected_att = ["-cremote.origin.push=+refs/heads/*:refs/remotes/origin/*", "push", "origin"]
        assert _reconstruct_git_args(raw_att) == expected_att

        raw_env = ["--config-env", "remote.origin.push=+refs/heads/*", ":", "refs/remotes/origin/*", "push", "origin"]
        expected_env = ["--config-env", "remote.origin.push=+refs/heads/*:refs/remotes/origin/*", "push", "origin"]
        assert _reconstruct_git_args(raw_env) == expected_env

        raw_env_eq = ["--config-env=remote.origin.push=+refs/heads/*", ":", "refs/remotes/origin/*", "push", "origin"]
        expected_env_eq = ["--config-env=remote.origin.push=+refs/heads/*:refs/remotes/origin/*", "push", "origin"]
        assert _reconstruct_git_args(raw_env_eq) == expected_env_eq

    def test_multiple_colons_and_global_options(self) -> None:
        raw = ["-C", "/path/to/repo", "-c", "remote.origin.push=+refs/heads/*", ":", "refs/remotes/origin/*", ":", "more", "push", "origin"]
        expected = ["-C", "/path/to/repo", "-c", "remote.origin.push=+refs/heads/*:refs/remotes/origin/*:more", "push", "origin"]
        assert _reconstruct_git_args(raw) == expected


class TestShellExpandedExecutables:
    """Tests for dynamic shell-expanded executable tokens ($ or backticks)."""

    @pytest.mark.parametrize(
        "command",
        [
            'GIT=git; "$GIT" push -f origin HEAD',
            '"$GIT" push --mirror origin',
            'command "$GIT" push --force origin HEAD',
            'env "$GIT" push origin +HEAD:main',
            'sudo "$GIT" push -f origin HEAD',
            'timeout 30 "$GIT" push -f origin HEAD',
            'nice "$GIT" push --mirror origin',
            'stdbuf -oL "$GIT" push -fu origin main',
            'time "$GIT" push -f origin HEAD',
            'bash -c \'"$GIT" push -f origin HEAD\'',
            'sh -c \'"$GIT" push --mirror origin\'',
            '"$GIT" -c alias.fp=\'push -f\' fp origin HEAD',
            '"$GIT" -c remote.origin.mirror=true push origin',
            '"$GIT" -c remote.origin.push=+main push origin',
            '"$GIT" --config-env remote.origin.mirror=VAR push origin',
            '"$GIT" --config-env remote.origin.push=VAR push origin',
            '`which git` push -f origin HEAD',
            '`which git` push --mirror origin',
            '`which git` -c alias.fp=\'push -f\' fp origin HEAD',
            "$GIT push -f origin HEAD",
            "${GIT} push --mirror origin",
            "$(which git) push -f origin HEAD",
            "$(which git) push --mirror origin",
            "command $(which git) push -f origin HEAD",
            "$($(which echo) git) push -f origin HEAD",
            "$(which $(echo git)) push -f origin HEAD",
            "$(echo $(which git)) push -f origin HEAD",
            "$(echo $(which git)) push --mirror origin",
            "command $(echo $(which git)) push -f origin HEAD",
            "command $(which $(echo git)) push -f origin HEAD",
            "$GIT -c alias.fp='push -f' fp origin HEAD",
            "${GIT} -c remote.origin.mirror=true push origin",
            "$(which git) -c remote.origin.push=+main push origin",
        ],
    )
    def test_blocks_dynamic_git_forced_push(self, command: str) -> None:
        assert contains_forced_git_push(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            'RM=rm; "$RM" -rf target',
            'command "$RM" --recursive --force target',
            'sudo "$RM" -r -f target',
            'env "$RM" -fr target',
            'timeout 10 "$RM" -rf target',
            'bash -c \'"$RM" -rf target\'',
            '"$CMD" -c "alias.nuke=\'!rm -rf target\'" nuke',
            '`which rm` -rf target',
            '`which git` -c "alias.nuke=\'!rm -rf target\'" nuke',
            "$RM -rf target",
            "${RM} --recursive --force target",
            "$(which rm) -rf target",
            "command $(which rm) -rf target",
            "$($(which echo) rm) -rf target",
            "$(echo $(which rm)) -rf /tmp/example",
            "command $(echo $(which rm)) --recursive --force /tmp/example",
            "$(which $(echo rm)) -rf /tmp/example",
            "command $(which $(echo rm)) --recursive --force /tmp/example",
            "$CMD -c \"alias.nuke='!rm -rf target'\" nuke",
            "$(which git) -c \"alias.nuke='!rm -rf target'\" nuke",
        ],
    )
    def test_blocks_dynamic_destructive_rm(self, command: str) -> None:
        assert contains_forbidden_rm(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            '"$GIT" status --short',
            '"$GIT" push origin main',
            '"$GIT" push --follow-tags origin HEAD',
            '"$GIT" push --all origin',
            '"$GIT" push --prune origin',
            '"$GIT" push --tags origin',
            '"$GIT" push --atomic origin main',
            '"$GIT" -c remote.origin.mirror=false push origin',
            '"$GIT" -c remote.origin.push=main push origin',
            '"$GIT" log -n 5',
            '"$GIT" diff --check',
            '`which git` status',
            '`which git` push origin main',
            '`which git` log -n 5',
            "$GIT push origin main",
            "${GIT} status",
            "$(which git) push origin main",
            "$(which git) status",
            "$(echo $(which git)) push origin main",
            "$(echo $(which git)) status",
            "command $(echo $(which git)) push origin main",
            "$(which $(echo git)) push origin main",
            "$(which $(echo git)) status",
            "command $(which $(echo git)) push origin main",
            "$GIT status --short",
            "${GIT} push --follow-tags origin HEAD",
            "$GIT log -n 5",
            "${GIT} diff --check",
        ],
    )
    def test_allows_dynamic_git_safe_controls(self, command: str) -> None:
        assert contains_forced_git_push(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            '"$RM" -r target',
            '"$RM" -f target',
            '"$RM" target',
            'command "$RM" -r target',
            'env "$RM" -f target',
            'sudo "$RM" -r target',
            '"$PYTHON" -m pytest -q',
            '"$FOO" bar baz',
            '`which rm` -r target',
            '`which rm` -f target',
            '`which rm` target',
            "$RM -r target",
            "${RM} -f target",
            "$(which rm) -r target",
            "command $(which rm) -r target",
            "$RM target",
            "${RM} target",
            "$(which rm) target",
            "command $(which rm) target",
            "$(echo $(which rm)) -r /tmp/example",
            "$(echo $(which rm)) --force /tmp/example",
            "command $(echo $(which rm)) -r /tmp/example",
            "command $(echo $(which rm)) --force /tmp/example",
            "$(which $(echo rm)) -r /tmp/example",
            "$(which $(echo rm)) --force /tmp/example",
        ],
    )
    def test_allows_dynamic_rm_safe_controls(self, command: str) -> None:
        assert contains_forbidden_rm(command) is False
        assert contains_forced_git_push(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "`which git push -f origin HEAD",
            "`which rm -rf target",
            "$(which git push -f origin HEAD",
            "$(which rm -rf target",
            "$($(which git) push -f origin HEAD",
            "$(which $(echo git) push -f origin HEAD",
            "$(echo $(which git) push -f origin HEAD",
            "$(echo $(which rm) -rf /tmp/example",
            "command $(which rm -rf target",
            "command $(which git push -f origin HEAD",
            "command $(which $(echo rm) -rf /tmp/example",
            "command $(which $(echo git) push -f origin HEAD",
        ],
    )
    def test_unmatched_initial_backtick_or_substitution_raises_value_error(
        self, command: str
    ) -> None:
        with pytest.raises(ValueError):
            contains_forced_git_push(command)
        with pytest.raises(ValueError):
            contains_forbidden_rm(command)


class TestMirrorPushesAndForcingConfigsCLI:
    """CLI end-to-end contract tests for mirror pushes, forcing configs, and shell-expanded executables."""

    def _run_hook(self, stdin_payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT_PATH)],
            input=stdin_payload,
            capture_output=True,
            text=True,
            check=False,
        )

    @pytest.mark.parametrize(
        "cmd",
        [
            "git push --mirror",
            "git push --mirror origin",
            "git push origin --mirror",
            "git -c alias.mp='push --mirror' mp origin",
            "find /tmp/tree -exec git push --mirror origin \\;",
            "find /tmp/tree -exec git push --mirror origin +",
            "command git push --mirror origin",
            "env git push --mirror origin",
            "sudo git push --mirror origin",
            "bash -c 'git push --mirror origin'",
            "git -c remote.origin.mirror=true push origin",
            "git -c remote.origin.mirror=1 push origin",
            "git -c remote.origin.mirror=yes push origin",
            "git -c remote.origin.mirror=on push origin",
            "git -c remote.origin.mirror push origin",
            "git -cremote.origin.mirror=true push origin",
            "git --config-env remote.origin.mirror=VAR push origin",
            "git --config-env=remote.origin.mirror=VAR push origin",
            "git -c remote.origin.push=+main push origin",
            "git -c remote.origin.push=+refs/heads/*:refs/remotes/origin/* push origin",
            "git -cremote.origin.push=+main push origin",
            "git --config-env remote.origin.push=VAR push origin",
            "git --config-env=remote.origin.push=VAR push origin",
            "git -c remote.origin.push=+main -c remote.origin.push=main push origin",
            "git -c remote.origin.push=main -c remote.origin.push=+main push origin",
            "git -c alias.mp='-c remote.origin.mirror=true push' mp origin",
            "git -c alias.pp='-c remote.origin.push=+main push' pp origin",
            'GIT=git; "$GIT" push -f origin HEAD',
            '"$GIT" push --mirror origin',
            'command "$GIT" push --force origin HEAD',
            'env "$GIT" push origin +HEAD:main',
            '`which git` push -f origin HEAD',
            '`which git` push --mirror origin',
            '`which git` -c alias.fp=\'push -f\' fp origin HEAD',
            "$GIT push -f origin HEAD",
            "${GIT} push --mirror origin",
            "$(which git) push -f origin HEAD",
            "$(which git) push --mirror origin",
            "command $(which git) push -f origin HEAD",
            "$($(which echo) git) push -f origin HEAD",
            "$(which $(echo git)) push -f origin HEAD",
            "$(echo $(which git)) push -f origin HEAD",
            "$(echo $(which git)) push --mirror origin",
            "command $(echo $(which git)) push -f origin HEAD",
            "command $(which $(echo git)) push -f origin HEAD",
        ],
    )
    def test_cli_denies_mirror_configs_and_expanded_git_pushes(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        hook_out = data["hookSpecificOutput"]
        assert hook_out["permissionDecision"] == "deny"
        assert "no-force-push" in hook_out["permissionDecisionReason"].lower()

    @pytest.mark.parametrize(
        "cmd",
        [
            'RM=rm; "$RM" -rf target',
            'command "$RM" --recursive --force target',
            'sudo "$RM" -r -f target',
            'env "$RM" -fr target',
            'timeout 10 "$RM" -rf target',
            'bash -c \'"$RM" -rf target\'',
            '"$CMD" -c "alias.nuke=\'!rm -rf target\'" nuke',
            '`which rm` -rf target',
            '`which git` -c "alias.nuke=\'!rm -rf target\'" nuke',
            "$RM -rf target",
            "${RM} --recursive --force target",
            "$(which rm) -rf target",
            "command $(which rm) -rf target",
            "$($(which echo) rm) -rf target",
            "$(echo $(which rm)) -rf /tmp/example",
            "command $(echo $(which rm)) --recursive --force /tmp/example",
            "$(which $(echo rm)) -rf /tmp/example",
            "command $(which $(echo rm)) --recursive --force /tmp/example",
        ],
    )
    def test_cli_denies_expanded_destructive_rm(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        hook_out = data["hookSpecificOutput"]
        assert hook_out["permissionDecision"] == "deny"
        assert "destructive" in hook_out["permissionDecisionReason"].lower()

    @pytest.mark.parametrize(
        "cmd",
        [
            "git push --all origin",
            "git push --prune origin",
            "git push --tags origin",
            "git push --follow-tags origin HEAD",
            "git push --atomic origin main",
            "git -c remote.origin.mirror=false push origin",
            "git -c remote.origin.mirror=no push origin",
            "git -c remote.origin.mirror=off push origin",
            "git -c remote.origin.mirror=0 push origin",
            "git -c remote.origin.push=main push origin",
            "git -c remote.origin.push=main -c remote.origin.push=HEAD:main push origin",
            "git -c remote.origin.mirror=true status",
            "git -c remote.origin.push=+main status",
            "git -c alias.st='-c remote.origin.mirror=true status' st",
            '"$GIT" status --short',
            '"$GIT" push origin main',
            '"$RM" -r target',
            '"$PYTHON" -m pytest -q',
            '"$FOO" bar baz',
            '`which git` status',
            '`which git` push origin main',
            '`which rm` -r target',
            '`which rm` -f target',
            '`which rm` target',
            "$GIT push origin main",
            "${GIT} status",
            "$(which git) push origin main",
            "$(which git) status",
            "$(echo $(which git)) push origin main",
            "$(echo $(which git)) status",
            "command $(echo $(which git)) push origin main",
            "$(which $(echo git)) push origin main",
            "$(which $(echo git)) status",
            "command $(which $(echo git)) push origin main",
            "$RM -r target",
            "${RM} -f target",
            "$(which rm) -r target",
            "command $(which rm) -r target",
            "$(echo $(which rm)) -r /tmp/example",
            "$(echo $(which rm)) --force /tmp/example",
            "command $(echo $(which rm)) -r /tmp/example",
            "command $(echo $(which rm)) --force /tmp/example",
            "$(which $(echo rm)) -r /tmp/example",
            "$(which $(echo rm)) --force /tmp/example",
        ],
    )
    def test_cli_allows_safe_mirror_config_and_expanded_controls(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        assert res.stdout == ""

    @pytest.mark.parametrize(
        "cmd",
        [
            "`which git push -f origin HEAD",
            "`which rm -rf target",
            "$(which git push -f origin HEAD",
            "$(which rm -rf target",
            "$($(which git) push -f origin HEAD",
            "$(which $(echo git) push -f origin HEAD",
            "$(echo $(which git) push -f origin HEAD",
            "$(echo $(which rm) -rf /tmp/example",
            "command $(which rm -rf target",
            "command $(which git push -f origin HEAD",
            "command $(which $(echo rm) -rf /tmp/example",
            "command $(which $(echo git) push -f origin HEAD",
        ],
    )
    def test_cli_unmatched_initial_backtick_or_substitution_fails_closed(
        self, cmd: str
    ) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr


class TestQuotedEscapedParentheses:
    """Unit and contract tests for preserving quote and escape context with literal parentheses."""

    def _run_hook(self, stdin_payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT_PATH)],
            input=stdin_payload,
            capture_output=True,
            text=True,
            check=False,
        )

    @pytest.mark.parametrize(
        "command",
        [
            # Single-quoted close parenthesis inside command substitution
            "$(printf %s ')' >/dev/null; printf git) push -f origin HEAD",
            "$(printf %s ')' >/dev/null; printf git) push --mirror origin",
            # Double-quoted close parenthesis inside command substitution
            '$(printf %s ")" >/dev/null; printf git) push -f origin HEAD',
            # Backslash-escaped close parenthesis inside command substitution
            "$(printf %s \\) >/dev/null; printf git) push -f origin HEAD",
            # Single-quoted open parenthesis inside command substitution
            "$(printf %s '(' >/dev/null; printf git) push -f origin HEAD",
            "$(printf %s '(' >/dev/null; printf git) push --mirror origin",
            # Backslash-escaped open parenthesis inside command substitution
            "$(printf %s \\( >/dev/null; printf git) push -f origin HEAD",
            # Wrapper preceding command substitution containing quoted paren
            "command $(printf %s ')' >/dev/null; printf git) push -f origin HEAD",
            # Double-quoted dynamic executables
            '"$(which git)" push -f origin HEAD',
            '"$(which git)" push --mirror origin',
        ],
    )
    def test_blocks_forced_push_with_quoted_or_escaped_parens(self, command: str) -> None:
        assert contains_forced_git_push(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            # Safe branch pushes and status commands
            "$(printf %s ')' >/dev/null; printf git) push origin main",
            "$(printf %s '(' >/dev/null; printf git) status",
            "command $(printf %s ')' >/dev/null; printf git) push origin main",
            '"$(which git)" push origin main',
            # Standalone commands outputting literal parens
            "echo ')'",
            "printf '%s' '('",
            'echo ")"',
            "echo \\)",
            "printf '%s' \\(",
        ],
    )
    def test_allows_safe_controls_with_quoted_or_escaped_parens(self, command: str) -> None:
        assert contains_forced_git_push(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "$(printf %s ')' >/dev/null; printf rm) -rf /tmp/example",
            "$(printf %s '(' >/dev/null; printf rm) --recursive --force /tmp/example",
            "command $(printf %s ')' >/dev/null; printf rm) -r -f /tmp/example",
        ],
    )
    def test_blocks_destructive_rm_with_quoted_or_escaped_parens(self, command: str) -> None:
        assert contains_forbidden_rm(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "$(printf %s ')' >/dev/null; printf rm) -r /tmp/example",
            "$(printf %s '(' >/dev/null; printf rm) --force /tmp/example",
            "echo ')'",
            "echo '('",
            'echo ")"',
            "echo \\)",
        ],
    )
    def test_allows_safe_rm_controls_with_quoted_or_escaped_parens(self, command: str) -> None:
        assert contains_forbidden_rm(command) is False

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo ')'", [["echo", ")"]]),
            ('echo ")"', [["echo", ")"]]),
            ("echo \\)", [["echo", ")"]]),
            ("printf '%s' '('", [["printf", "%s", "("]]),
            ('printf "%s" "("', [["printf", "%s", "("]]),
            ("printf '%s' \\(", [["printf", "%s", "("]]),
            (
                "$(printf %s ')' >/dev/null; printf git) push -f origin HEAD",
                [
                    [
                        "$",
                        "(",
                        "printf",
                        "%s",
                        ")",
                        ">",
                        "/dev/null",
                        ";",
                        "printf",
                        "git",
                        ")",
                        "push",
                        "-f",
                        "origin",
                        "HEAD",
                    ]
                ],
            ),
        ],
    )
    def test_tokenization_preserves_quoted_and_escaped_parens(
        self, command: str, expected: list[list[str]]
    ) -> None:
        assert _tokenize_command(command) == expected

    @pytest.mark.parametrize(
        "command",
        [
            "$(printf %s ')' >/dev/null; printf git push -f origin HEAD",
            "$(printf %s '(' >/dev/null; printf git push -f origin HEAD",
            "$(printf %s \\) >/dev/null; printf git push -f origin HEAD",
        ],
    )
    def test_unmatched_substitution_with_literal_parens_raises_value_error(
        self, command: str
    ) -> None:
        with pytest.raises(ValueError, match="Unmatched '\\$\\(' in command substitution"):
            contains_forced_git_push(command)
        with pytest.raises(ValueError, match="Unmatched '\\$\\(' in command substitution"):
            contains_forbidden_rm(command)

    @pytest.mark.parametrize(
        "cmd",
        [
            "$(printf %s ')' >/dev/null; printf git) push -f origin HEAD",
            "$(printf %s ')' >/dev/null; printf git) push --mirror origin",
            '$(printf %s ")" >/dev/null; printf git) push -f origin HEAD',
            "$(printf %s \\) >/dev/null; printf git) push -f origin HEAD",
            "$(printf %s '(' >/dev/null; printf git) push -f origin HEAD",
            "$(printf %s '(' >/dev/null; printf git) push --mirror origin",
            "$(printf %s \\( >/dev/null; printf git) push -f origin HEAD",
            "command $(printf %s ')' >/dev/null; printf git) push -f origin HEAD",
            '"$(which git)" push -f origin HEAD',
            '"$(which git)" push --mirror origin',
        ],
    )
    def test_cli_denies_forced_push_with_quoted_parens(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "no-force-push" in data["hookSpecificOutput"]["permissionDecisionReason"].lower()

    @pytest.mark.parametrize(
        "cmd",
        [
            "$(printf %s ')' >/dev/null; printf rm) -rf /tmp/example",
            "$(printf %s '(' >/dev/null; printf rm) --recursive --force /tmp/example",
            "command $(printf %s ')' >/dev/null; printf rm) -r -f /tmp/example",
        ],
    )
    def test_cli_denies_destructive_rm_with_quoted_parens(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "destructive" in data["hookSpecificOutput"]["permissionDecisionReason"].lower()

    @pytest.mark.parametrize(
        "cmd",
        [
            "$(printf %s ')' >/dev/null; printf git) push origin main",
            "$(printf %s '(' >/dev/null; printf git) status",
            "command $(printf %s ')' >/dev/null; printf git) push origin main",
            '"$(which git)" push origin main',
            "echo ')'",
            "printf '%s' '('",
            "$(printf %s ')' >/dev/null; printf rm) -r /tmp/example",
            "$(printf %s '(' >/dev/null; printf rm) --force /tmp/example",
        ],
    )
    def test_cli_allows_safe_controls_with_quoted_parens(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        assert res.stdout == ""

    @pytest.mark.parametrize(
        "cmd",
        [
            "$(printf %s ')' >/dev/null; printf git push -f origin HEAD",
            "$(printf %s '(' >/dev/null; printf git push -f origin HEAD",
        ],
    )
    def test_cli_unmatched_substitution_with_quoted_parens_fails_closed(
        self, cmd: str
    ) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr


NESTED_BLOCKED_GIT_PUSH_COMMANDS = [
    'echo "$(git push -f origin HEAD)"',
    'printf %s "prefix-$(git push --force-with-lease origin HEAD)-suffix"',
    "echo `git push --mirror origin`",
    'printf %s "`git -c remote.origin.push=+refs/heads/main:refs/heads/main push origin`"',
    'echo "$(printf \'%s\' "$(git push -f origin HEAD)")"',
    'echo "$(( $(git push -f origin HEAD; echo 0) + 1 ))"',
    "cat <(git push -f origin HEAD)",
    "tee >(git push --force origin HEAD)",
]

NESTED_BLOCKED_RM_COMMANDS = [
    'echo "$(rm -rf target)"',
    'printf %s "prefix-$(rm --recursive --force target)-suffix"',
    "echo `rm -fr target`",
    'echo "$(printf \'%s\' "$(rm -Rf target)")"',
    "cat <(rm -rf target)",
    "tee >(rm --force --recursive target)",
]

NESTED_SAFE_CONTROLS = [
    "echo '$(git push -f origin HEAD)'",
    "echo '\\$(git push -f origin HEAD)'",
    'echo "\\`git push -f origin HEAD\\`"',
    "echo '$(rm -rf target)'",
    "echo '\\$(rm -rf target)'",
    'echo "\\`rm -rf target\\`"',
    'echo "$(git push origin HEAD)"',
    'echo "$(rm -r target)"',
    'echo "$((1 + 2))"',
    "printf '%s' '<(git push -f origin HEAD)'",
    "printf '%s' '>(rm -rf target)'",
]


class TestNestedSubstitutionsPure:
    """Pure-function tests for nested command, backtick, process, and arithmetic substitutions."""

    @pytest.mark.parametrize("command", NESTED_BLOCKED_GIT_PUSH_COMMANDS)
    def test_blocks_nested_force_push_variants(self, command: str) -> None:
        assert contains_forced_git_push(command) is True, f"Expected {command!r} to be blocked"

    @pytest.mark.parametrize("command", NESTED_BLOCKED_RM_COMMANDS)
    def test_blocks_nested_destructive_rm_variants(self, command: str) -> None:
        assert contains_forbidden_rm(command) is True, f"Expected {command!r} to be blocked"

    @pytest.mark.parametrize("command", NESTED_SAFE_CONTROLS)
    def test_allows_nested_safe_controls(self, command: str) -> None:
        assert (
            contains_forced_git_push(command) is False
        ), f"Expected {command!r} to be allowed by git guard"
        assert (
            contains_forbidden_rm(command) is False
        ), f"Expected {command!r} to be allowed by rm guard"

    def test_nested_substitution_depth_within_limit(self) -> None:
        inner = "git push -f origin HEAD"
        for _ in range(19):
            inner = f"echo $({inner})"
        assert contains_forced_git_push(inner) is True

    def test_nested_substitution_depth_exceeded_raises_value_error(self) -> None:
        inner = "git push -f origin HEAD"
        for _ in range(21):
            inner = f"echo $({inner})"
        with pytest.raises(ValueError, match="Maximum substitution nesting depth"):
            contains_forced_git_push(inner)
        with pytest.raises(ValueError, match="Maximum substitution nesting depth"):
            contains_forbidden_rm(inner)

    @pytest.mark.parametrize(
        "command",
        [
            'echo "$(git push -f origin HEAD"',
            'echo "`git push -f origin HEAD"',
            "cat <(git push -f origin HEAD",
            'echo "$((1 + 2"',
            'echo "$(printf \'%s\' "$(git push -f origin HEAD)"',
            'echo "$(rm -rf target"',
            "tee >(rm -rf target",
            'echo "$(( $(git push -f origin HEAD; echo 0) + 1 "',
        ],
    )
    def test_malformed_unclosed_substitutions_raise_value_error(self, command: str) -> None:
        with pytest.raises(ValueError):
            contains_forced_git_push(command)
        with pytest.raises(ValueError):
            contains_forbidden_rm(command)


class TestNestedSubstitutionsCLI:
    """CLI end-to-end contract tests for nested and embedded substitutions."""

    def _run_hook(self, stdin_payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT_PATH)],
            input=stdin_payload,
            capture_output=True,
            text=True,
            check=False,
        )

    @pytest.mark.parametrize("cmd", NESTED_BLOCKED_GIT_PUSH_COMMANDS)
    def test_cli_denies_embedded_substitutions_git_push(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        hook_out = data["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"
        assert "no-force-push" in hook_out["permissionDecisionReason"].lower()

    @pytest.mark.parametrize("cmd", NESTED_BLOCKED_RM_COMMANDS)
    def test_cli_denies_embedded_substitutions_destructive_rm(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert "hookSpecificOutput" in data
        hook_out = data["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "PreToolUse"
        assert hook_out["permissionDecision"] == "deny"
        assert "destructive" in hook_out["permissionDecisionReason"].lower()

    @pytest.mark.parametrize("cmd", NESTED_SAFE_CONTROLS)
    def test_cli_allows_nested_safe_controls(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 0
        assert res.stdout == ""

    @pytest.mark.parametrize(
        "cmd",
        [
            'echo "$(git push -f origin HEAD"',
            'echo "`git push -f origin HEAD"',
            "cat <(git push -f origin HEAD",
            'echo "$((1 + 2"',
            'echo "$(printf \'%s\' "$(git push -f origin HEAD)"',
            'echo "$(rm -rf target"',
            "tee >(rm -rf target",
            'echo "$(( $(git push -f origin HEAD; echo 0) + 1 "',
        ],
    )
    def test_cli_malformed_nested_substitution_fails_closed(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr

    def test_cli_depth_exceeded_fails_closed(self) -> None:
        inner = "git push -f origin HEAD"
        for _ in range(21):
            inner = f"echo $({inner})"
        payload = json.dumps({"command": inner})
        res = self._run_hook(payload)
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr


class TestExtractRawSubstitutions:
    """Unit tests for _extract_raw_substitutions, _parse_backtick_body, and _parse_paren_body."""

    def test_extracts_various_substitution_types(self) -> None:
        cmd = 'echo "$(git push -f)" `rm -rf target` <(git push) >(rm -r) $((1 + 2))'
        extracted = _extract_raw_substitutions(cmd)
        assert extracted == [
            ("cmd", "git push -f"),
            ("backtick", "rm -rf target"),
            ("process_in", "git push"),
            ("process_out", "rm -r"),
            ("arith", "1 + 2"),
        ]

    def test_skips_single_quoted_and_escaped_constructs(self) -> None:
        cmd = (
            "echo '$(git push -f)' '\\$(git push -f)' "
            '"\\`git push -f\\`" "\\$(git push -f)" '
            "'<(git push -f)' '>(rm -rf target)'"
        )
        assert _extract_raw_substitutions(cmd) == []

    @pytest.mark.parametrize(
        "cmd",
        [
            "$(unclosed",
            "`unclosed",
            "<(",
            ">(",
            "$((1 + ",
        ],
    )
    def test_unmatched_substitutions_raise_value_error(self, cmd: str) -> None:
        with pytest.raises(ValueError):
            _extract_raw_substitutions(cmd)

    def test_parse_backtick_body_unescapes_special_chars(self) -> None:
        raw = "`echo \\`hello\\` \\$WORLD \\\\`"
        body, next_idx = _parse_backtick_body(raw, 0)
        assert body == "echo `hello` $WORLD \\"
        assert next_idx == len(raw)

    def test_parse_paren_body_handles_nested_quotes_and_parens(self) -> None:
        raw = "$(printf '%s' ')' >/dev/null; printf git)"
        body, next_idx = _parse_paren_body(raw, 0, prefix_len=2)
        assert body == "printf '%s' ')' >/dev/null; printf git"
        assert next_idx == len(raw)


class TestHereDocSubstitutions:
    """Unit and contract tests for lexical here-doc handling in push and rm guards."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat <<EOF\n$(git push -f origin HEAD)\nEOF",
            "cat <<EOF\n'$(git push --force origin HEAD)'\nEOF",
            "cat <<EOF\n`git push --mirror origin`\nEOF",
            "cat <<-EOF\n\t$(git push -f origin HEAD)\n\tEOF",
            "cat <<$EOF\n$(git push -f origin HEAD)\n$EOF",
            "cat <<'EOF1' <<EOF2\n$(git push -f)\nEOF1\n$(git push -f origin HEAD)\nEOF2",
            "echo $(cat <<EOF\n$(git push -f origin main)\nEOF\n)",
        ],
    )
    def test_unquoted_heredoc_containing_forced_push_is_blocked(self, cmd: str) -> None:
        assert contains_forced_git_push(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat <<EOF\n$(rm -rf target)\nEOF",
            'cat <<EOF\n"$(rm --recursive --force target)"\nEOF',
            "cat <<EOF\n`rm -r -f target`\nEOF",
            "cat <<-EOF\n\t$(rm -rf target)\n\tEOF",
            "cat <<$EOF\n$(rm -rf target)\n$EOF",
            "cat <<'EOF1' <<EOF2\n$(rm -rf target)\nEOF1\n$(rm -rf target)\nEOF2",
            "echo $(cat <<EOF\n$(rm -rf target)\nEOF\n)",
        ],
    )
    def test_unquoted_heredoc_containing_forbidden_rm_is_blocked(self, cmd: str) -> None:
        assert contains_forbidden_rm(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat <<'EOF'\n$(git push -f origin HEAD)\nEOF",
            'cat <<"EOF"\n$(git push -f origin HEAD)\nEOF',
            'cat <<"E\\OF"\n$(git push -f origin HEAD)\nE\\OF',
            "cat <<''\n$(git push -f origin HEAD)\n\n",
            'cat <<""\n$(git push -f origin HEAD)\n\n',
            "cat <<$'EOF'\n$(git push -f origin HEAD)\nEOF",
            "cat <<$''\n$(git push -f origin HEAD)\n\n",
            "cat <<pre$'FIX'\n$(git push -f origin HEAD)\npreFIX",
            'cat <<E"O"F\n$(git push -f origin HEAD)\nEOF',
            'cat <<"E\\$OF"\n$(git push -f origin HEAD)\nE$OF',
            "cat <<\\EOF\n$(git push -f origin HEAD)\nEOF",
            "cat <<-'EOF'\n\t$(git push -f origin HEAD)\n\tEOF",
            "cat <<-\"EOF\"\n\t$(git push -f origin HEAD)\n\tEOF",
            "cat <<-\\EOF\n\t$(git push -f origin HEAD)\n\tEOF",
            "cat <<EOF\n\\$(git push -f origin HEAD)\nEOF",
            "cat <<EOF\n<(git push -f origin HEAD)\nEOF",
            "cat <<EOF\n>(git push -f origin HEAD)\nEOF",
            "cat <<'EOF1' <<'EOF2'\n$(git push -f)\nEOF1\n$(git push -f origin HEAD)\nEOF2",
            "cat <<'EOF'\ngit push -f origin HEAD\nEOF",
            "cat <<EOF\ngit push -f origin HEAD\nEOF",
            "echo $(cat <<'EOF'\n$(git push -f origin main)\nEOF\n)",
        ],
    )
    def test_quoted_and_escaped_heredoc_push_is_safe(self, cmd: str) -> None:
        assert contains_forced_git_push(cmd) is False

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat <<'EOF'\n$(rm -rf target)\nEOF",
            'cat <<"EOF"\n$(rm -rf target)\nEOF',
            'cat <<"E\\OF"\n$(rm -rf target)\nE\\OF',
            "cat <<''\n$(rm -rf target)\n\n",
            'cat <<""\n$(rm -rf target)\n\n',
            "cat <<$'EOF'\n$(rm -rf target)\nEOF",
            "cat <<$''\n$(rm -rf target)\n\n",
            "cat <<pre$'FIX'\n$(rm -rf target)\npreFIX",
            'cat <<E"O"F\n$(rm -rf target)\nEOF',
            'cat <<"E\\$OF"\n$(rm -rf target)\nE$OF',
            "cat <<\\EOF\n$(rm -rf target)\nEOF",
            "cat <<-'EOF'\n\t$(rm -rf target)\n\tEOF",
            "cat <<-\"EOF\"\n\t$(rm -rf target)\n\tEOF",
            "cat <<-\\EOF\n\t$(rm -rf target)\n\tEOF",
            "cat <<EOF\n\\$(rm -rf target)\nEOF",
            "cat <<EOF\n<(rm -rf target)\nEOF",
            "cat <<EOF\n>(rm -rf target)\nEOF",
            "cat <<'EOF1' <<'EOF2'\n$(rm -rf target)\nEOF1\n$(rm -rf target)\nEOF2",
            "cat <<'EOF'\nrm -rf target\nEOF",
            "cat <<EOF\nrm -rf target\nEOF",
            "echo $(cat <<'EOF'\n$(rm -rf target)\nEOF\n)",
        ],
    )
    def test_quoted_and_escaped_heredoc_rm_is_safe(self, cmd: str) -> None:
        assert contains_forbidden_rm(cmd) is False

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat <<",
            "cat <<-",
            "cat <<EOF",
            "cat <<EOF\n$(git push -f origin HEAD)",
            "cat <<'EOF\nhello\nEOF",
            "cat <<\"EOF\nhello\nEOF",
            "cat <<\\EOF\nhello",
            "cat <<EOF1 <<EOF2\nbody1\nEOF1\nbody2",
            "cat <<$'",
            "cat <<$'EOF",
            "cat <<$'EOF\\'",
            "cat <<$'E\\u004fF'\nsafe\nEOF\n$(git push -f origin HEAD)\nEu004fF",
            "cat <<$'E\\u004fF'\nsafe\nEOF\n$(rm -rf target)\nEu004fF",
            "cat <<$'E\\x4fF'",
            "cat <<$'E\\117F'",
            "cat <<$'E\\qOF'",
            "cat <<$'E\\cOOF'",
            "cat <<$'E\\tOF'",
        ],
    )
    def test_malformed_heredoc_raises_value_error(self, cmd: str) -> None:
        with pytest.raises(ValueError):
            contains_forced_git_push(cmd)
        with pytest.raises(ValueError):
            contains_forbidden_rm(cmd)

    @pytest.mark.parametrize(
        "cmd",
        [
            "cat <<",
            "cat <<-",
            "cat <<EOF",
            "cat <<EOF\n$(git push -f origin HEAD)",
            "cat <<$'",
            "cat <<$'EOF",
            "cat <<$'EOF\\'",
            "cat <<$'E\\u004fF'\nsafe\nEOF\n$(git push -f origin HEAD)\nEu004fF",
            "cat <<$'E\\u004fF'\nsafe\nEOF\n$(rm -rf target)\nEu004fF",
            "cat <<$'E\\x4fF'",
            "cat <<$'E\\117F'",
            "cat <<$'E\\qOF'",
            "cat <<$'E\\cOOF'",
            "cat <<$'E\\tOF'",
        ],
    )
    def test_malformed_heredoc_cli_exit_2(self, cmd: str) -> None:
        payload = json.dumps({"command": cmd})
        res = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT_PATH)],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 2
        assert "Shell tokenization failed" in res.stderr

    def test_cli_blocks_unquoted_heredoc_push(self) -> None:
        payload = json.dumps({"command": "cat <<EOF\n$(git push -f origin HEAD)\nEOF"})
        res = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT_PATH)],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Git force-push is prohibited" in data["hookSpecificOutput"]["permissionDecisionReason"]

    def test_cli_blocks_unquoted_heredoc_rm(self) -> None:
        payload = json.dumps({"command": "cat <<EOF\n$(rm -rf target)\nEOF"})
        res = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT_PATH)],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Destructive rm commands" in data["hookSpecificOutput"]["permissionDecisionReason"]

    def test_cli_allows_quoted_heredoc_push_and_rm(self) -> None:
        for cmd in [
            "cat <<'EOF'\n$(git push -f origin HEAD)\nEOF",
            "cat <<'EOF'\n$(rm -rf target)\nEOF",
            'cat <<"E\\OF"\n$(git push -f origin HEAD)\nE\\OF',
            "cat <<''\n$(git push -f origin HEAD)\n\n",
            "cat <<$'EOF'\n$(git push -f origin HEAD)\nEOF",
            "cat <<$'EOF'\n$(rm -rf target)\nEOF",
            "cat <<$''\n$(git push -f origin HEAD)\n\n",
            "cat <<$''\n$(rm -rf target)\n\n",
            "cat <<pre$'FIX'\n$(git push -f origin HEAD)\npreFIX",
        ]:
            payload = json.dumps({"command": cmd})
            res = subprocess.run(
                [sys.executable, str(HOOK_SCRIPT_PATH)],
                input=payload,
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 0
            assert res.stdout == ""
