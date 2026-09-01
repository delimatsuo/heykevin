"""Unit and contract tests for the Claude PreToolUse deny force-push and rm hook."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.deny_force_push_hook import (
    FIND_EXEC_ACTIONS,
    FIND_INPUT_SENTINEL,
    XARGS_INPUT_SENTINEL,
    _clean_command_segment,
    _extract_find_actions,
    _has_shell_expansion,
    _inspect_git_invocation,
    _inspect_git_invocation_for_rm,
    _parse_git_global_configs,
    _tokenize_command,
    _tokenize_split_string,
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
    # Safe xargs controls
    "xargs echo hello </dev/null",
    "xargs -0 printf %s </dev/null",
    "xargs rm -- target </dev/null",
    "xargs -I{} rm -- {} </dev/null",
    "xargs -J% command rm -- % </dev/null",
    # Safe multi-action find controls
    "find /tmp/tree -exec echo {} \\; -exec rm -- {} \\;",
    "find /tmp/tree -exec echo {} ';' -exec printf '%s\\n' {} ';'",
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
    # Multi-action find destructive rm and ordinary boundary controls
    "find /tmp/tree -exec echo {} \\; -exec rm -rf target \\;",
    "find /tmp/tree -exec echo {} ';' -exec rm -rf target ';'",
    "find /tmp/tree -exec echo {} \\; -exec rm -rf -- {} \\;",
    "find /tmp/tree -exec echo {} ';' -exec rm -rf -- {} ';'",
    "true; rm -rf target",
    "echo ';'; rm -rf target",
    "true # comment\nrm -rf target",
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
