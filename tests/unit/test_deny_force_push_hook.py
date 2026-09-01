"""Unit and contract tests for the Claude PreToolUse deny force-push and rm hook."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.deny_force_push_hook import contains_forbidden_rm, contains_forced_git_push

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
]

ALLOWED_GIT_PUSH_COMMANDS = [
    # Normal branches with hyphen or plus in branch name
    "git push origin codex/new-feature",
    "git push origin feature/--force-docs",
    "git push origin codex/c++",
    # Safe wrapper controls
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
]

ALLOWED_RM_COMMANDS = [
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
