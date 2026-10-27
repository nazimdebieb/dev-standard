"""Tests for the agent hooks in enforce/hooks/ and the CLI in cli/dev-standard.

Each test builds a throwaway git repository, so the hooks and checks run against real git
state. Run with: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "enforce" / "hooks"
CLI = ROOT / "cli" / "dev-standard"
# Call bash and Python explicitly: Windows can't run a script from its shebang line.
BASH = shutil.which("bash") or "bash"

MANIFEST = {
    "version": "0.1.0",
    "source": ".dev-standard",
    "profiles": ["base", "react-web"],
    "deployed": True,
    "paths": {"source": ["app/"]},
    "enforcement": {"default": "block", "groups": {}},
    "waivers": [],
}


class GitRepo:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("commit", "-q", "--allow-empty", "-m", "chore: init")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.path), *args], check=True, capture_output=True, text=True).stdout

    def write(self, rel: str, text: str) -> None:
        file = self.path / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def manifest(self, **overrides: object) -> None:
        self.write(".dev-standard.json", json.dumps({**MANIFEST, **overrides}))

    def commit_all(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def close(self) -> None:
        self._tmp.cleanup()


class HookCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = GitRepo()
        self.repo.manifest()
        self.repo.commit_all("chore: add manifest")
        self.repo.git("checkout", "-q", "-b", "013-faster-restock")
        self.repo.write("specs/013-faster-restock/spec.md", "**Status**: Draft\n")

    def tearDown(self) -> None:
        self.repo.close()

    def hook(self, name: str, payload: dict, project_dir: str | None = None,
             bin_dir: str | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "CLAUDE_PROJECT_DIR": project_dir or str(self.repo.path)}
        if bin_dir:
            env["PATH"] = bin_dir + os.pathsep + env["PATH"]
        return subprocess.run([BASH, (HOOKS / name).as_posix()], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)

    def edit(self, rel: str, **tool_input: str) -> dict:
        return {"hook_event_name": "PreToolUse", "tool_input": {"file_path": str(self.repo.path / rel), **tool_input}}

    def bash(self, command: str) -> dict:
        return {"hook_event_name": "PreToolUse", "tool_input": {"command": command}}


class RequireSpecTest(HookCase):
    def test_blocks_source_edit_while_spec_is_draft(self) -> None:
        result = self.hook("require-spec.sh", self.edit("app/main.py"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("SDD-001", result.stderr)

    def test_allows_tests_while_spec_is_draft(self) -> None:
        self.assertEqual(self.hook("require-spec.sh", self.edit("tests/test_x.py")).returncode, 0)

    def test_allows_source_edit_on_task_branch_once_approved(self) -> None:
        self.repo.write("specs/013-faster-restock/spec.md", "**Status**: Approved\n")
        self.repo.git("checkout", "-q", "-b", "013-t004-bom-parser")
        self.assertEqual(self.hook("require-spec.sh", self.edit("app/main.py")).returncode, 0)

    def test_warn_level_reaches_agent_without_blocking(self) -> None:
        self.repo.manifest(enforcement={"default": "block", "groups": {"SDD": "warn"}})
        result = self.hook("require-spec.sh", self.edit("app/main.py"))
        self.assertEqual(result.returncode, 0)
        self.assertIn("SDD-001", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_does_nothing_in_repo_without_manifest(self) -> None:
        (self.repo.path / ".dev-standard.json").unlink()
        self.assertEqual(self.hook("require-spec.sh", self.edit("app/main.py")).returncode, 0)


class RequireBrainstormTest(HookCase):
    def test_blocks_spec_without_decided_brainstorm(self) -> None:
        result = self.hook("require-brainstorm.sh", self.edit("specs/013-faster-restock/spec.md"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("BRN-001", result.stderr)

    def test_hints_at_brainstorm_waiting_to_move(self) -> None:
        self.repo.write("brainstorms/faster-restock/brainstorm.md", "---\nstatus: decided\n---\n")
        result = self.hook("require-brainstorm.sh", self.edit("specs/013-faster-restock/spec.md"))
        self.assertIn("git mv brainstorms/<slug>/brainstorm.*", result.stderr)

    def test_allows_spec_with_decided_brainstorm(self) -> None:
        self.repo.write("specs/013-faster-restock/brainstorm.md", "---\nstatus: decided\n---\n")
        self.assertEqual(self.hook("require-brainstorm.sh", self.edit("specs/013-faster-restock/spec.md")).returncode, 0)

    def test_honors_waiver_scope(self) -> None:
        self.repo.manifest(waivers=[{"rule": "BRN-001", "scope": "specs/013-", "reason": "direction set by ADR-0002",
                                     "approved_by": "@owner", "expires": "2099-01-01"}])
        self.assertEqual(self.hook("require-brainstorm.sh", self.edit("specs/013-faster-restock/spec.md")).returncode, 0)

    def test_ignores_expired_waiver(self) -> None:
        self.repo.manifest(waivers=[{"rule": "BRN-001", "scope": "specs/013-", "reason": "expired waiver",
                                     "approved_by": "@owner", "expires": "2000-01-01"}])
        self.assertEqual(self.hook("require-brainstorm.sh", self.edit("specs/013-faster-restock/spec.md")).returncode, 2)


class GuardApprovalsTest(HookCase):
    def test_blocks_agent_approving_spec(self) -> None:
        payload = self.edit("specs/013-faster-restock/spec.md", new_string="**Status**: Approved")
        self.assertEqual(self.hook("guard-approvals.sh", payload).returncode, 2)

    def test_blocks_agent_deciding_brainstorm(self) -> None:
        payload = self.edit("brainstorms/x/brainstorm.md", content="---\nstatus: decided\n---")
        self.assertEqual(self.hook("guard-approvals.sh", payload).returncode, 2)

    def test_blocks_agent_accepting_adr(self) -> None:
        payload = self.edit("docs/adr/0004-use-keycloak.md", content="---\nstatus: accepted\n---")
        self.assertIn("ADR-005", self.hook("guard-approvals.sh", payload).stderr)

    def test_allows_superseding_adr(self) -> None:
        payload = self.edit("docs/adr/0004-use-keycloak.md", new_string="status: superseded by ADR-0007")
        self.assertEqual(self.hook("guard-approvals.sh", payload).returncode, 0)


class NoBypassTest(HookCase):
    def assert_blocked(self, command: str, rule: str) -> None:
        result = self.hook("no-bypass.sh", self.bash(command))
        self.assertEqual(result.returncode, 2, command)
        self.assertIn(rule, result.stderr)

    def assert_asks(self, command: str) -> None:
        result = self.hook("no-bypass.sh", self.bash(command))
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "ask", command)

    def assert_allowed(self, command: str) -> None:
        result = self.hook("no-bypass.sh", self.bash(command))
        self.assertEqual((result.returncode, result.stdout), (0, ""), command)

    def test_blocks_skipping_git_hooks(self) -> None:
        self.assert_blocked("git commit --no-verify -m x", "AGT-002")
        self.assert_blocked("git commit -nm x", "AGT-002")
        self.assert_blocked("LEFTHOOK=0 git commit -m x", "AGT-002")

    def test_blocks_force_push_to_main(self) -> None:
        self.assert_blocked("git push --force origin main", "GIT-005")

    def test_allows_lease_push_to_own_branch(self) -> None:
        self.assert_allowed("git push --force-with-lease origin 013-faster-restock")

    def test_blocks_commit_on_main(self) -> None:
        self.repo.git("checkout", "-q", "main")
        self.assert_blocked("git commit -m 'feat: x'", "GIT-005")

    def test_blocks_admin_merge(self) -> None:
        self.assert_blocked("gh pr merge 12 --squash --admin", "BRA-011")

    def test_blocks_shell_approvals_but_not_reads(self) -> None:
        self.assert_blocked("sed -i 's/proposed/accepted/;s/^status: .*/status: accepted/' docs/adr/0004-x.md", "ADR-005")
        self.assert_blocked("echo 'status: decided' > brainstorms/x/brainstorm.md", "BRN-004")
        self.assert_allowed("grep -l 'status: accepted' docs/adr/*.md")

    def test_asks_before_destructive_or_protection_changes(self) -> None:
        self.assert_asks("rm -rf build")
        self.assert_asks("git reset --hard origin/main")
        self.assert_asks("gh api repos/o/r/rulesets --method POST --input main.json")
        self.assert_allowed("gh api repos/o/r/rulesets")


class DefinitionOfDoneTest(HookCase):
    def run_stop(self) -> subprocess.CompletedProcess[str]:
        return self.hook("definition-of-done.sh", {"hook_event_name": "Stop", "session_id": f"t-{id(self)}"})

    def test_blocks_three_times_then_lets_agent_report(self) -> None:
        self.repo.manifest(checks={"fast": "false"})
        codes = [self.run_stop().returncode for _ in range(4)]
        self.assertEqual(codes, [2, 2, 2, 1])

    def test_passes_when_check_passes(self) -> None:
        self.repo.manifest(checks={"fast": "true"})
        self.assertEqual(self.run_stop().returncode, 0)


class WindowsTest(HookCase):
    """jq for Windows prints CRLF, and Claude Code on Windows sends C:\\x\\y payload paths."""

    def fake_bin(self, name: str, body: str) -> str:
        bin_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        script = bin_dir / name
        script.write_text(f"#!/usr/bin/env bash\n{body}\n", newline="\n")
        script.chmod(0o755)
        return str(bin_dir)

    def crlf_jq(self) -> str:
        real_jq = Path(shutil.which("jq") or "jq").as_posix()
        return self.fake_bin("jq", f"set -o pipefail\n\"{real_jq}\" \"$@\" | awk '{{ sub(/\\r$/, \"\"); printf \"%s\\r\\n\", $0 }}'")

    def backslash_edit(self, rel: str, **tool_input: str) -> dict:
        payload = self.edit(rel, **tool_input)
        payload["tool_input"]["file_path"] = payload["tool_input"]["file_path"].replace("/", "\\")
        return payload

    def test_blocks_source_edit_when_jq_prints_crlf(self) -> None:
        result = self.hook("require-spec.sh", self.edit("app/main.py"), bin_dir=self.crlf_jq())
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("SDD-001", result.stderr)

    def test_reads_enforcement_level_when_jq_prints_crlf(self) -> None:
        self.repo.manifest(enforcement={"default": "block", "groups": {"SDD": "warn"}})
        result = self.hook("require-spec.sh", self.edit("app/main.py"), bin_dir=self.crlf_jq())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SDD-001", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_runs_fast_check_when_jq_prints_crlf(self) -> None:
        self.repo.manifest(checks={"fast": "true"})
        payload = {"hook_event_name": "Stop", "session_id": f"t-{id(self)}"}
        self.assertEqual(self.hook("definition-of-done.sh", payload, bin_dir=self.crlf_jq()).returncode, 0)

    def test_blocks_source_edit_with_backslash_payload_path(self) -> None:
        result = self.hook("require-spec.sh", self.backslash_edit("app/main.py"), project_dir=self.repo.path.as_posix())
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("SDD-001", result.stderr)

    def test_guards_approval_with_backslash_payload_path(self) -> None:
        payload = self.backslash_edit("specs/013-faster-restock/spec.md", new_string="**Status**: Approved")
        self.assertEqual(self.hook("guard-approvals.sh", payload, project_dir=self.repo.path.as_posix()).returncode, 2)

    def test_formats_file_with_backslash_payload_path(self) -> None:
        self.repo.write("app/main.py", "x=1\n")
        marker = Path(self.enterContext(tempfile.TemporaryDirectory())) / "ruff-args"
        bin_dir = self.fake_bin("ruff", f"printf '%s\\n' \"$*\" > \"{marker.as_posix()}\"")
        payload = {"hook_event_name": "PostToolUse", **self.backslash_edit("app/main.py")}
        self.hook("format-file.sh", payload, project_dir=self.repo.path.as_posix(), bin_dir=bin_dir)
        self.assertTrue(marker.exists(), "ruff was not called")
        self.assertIn("format", marker.read_text())

    @unittest.skipUnless(os.name == "nt" and shutil.which("cygpath"), "needs Windows and a bash that ships cygpath")
    def test_matches_other_drive_spellings(self) -> None:
        native = str(self.repo.path)
        # /c/x on Git Bash and MSYS2, /cygdrive/c/x on Cygwin.
        unix_root = subprocess.run(["cygpath", "-u", native], check=True, capture_output=True, text=True).stdout.strip()
        payload = self.edit("app/main.py")
        payload["tool_input"]["file_path"] = native[0].lower() + payload["tool_input"]["file_path"][1:]
        result = self.hook("require-spec.sh", payload, project_dir=unix_root)
        self.assertEqual(result.returncode, 2, result.stderr)


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = GitRepo()
        self.repo.manifest()
        self.repo.commit_all("chore: add manifest")

    def tearDown(self) -> None:
        self.repo.close()

    def cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        full_env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_ACTIONS", "GITHUB_HEAD_REF", "PR_TITLE", "PR_BODY")}
        full_env.update(env or {})
        return subprocess.run([sys.executable, str(CLI), "check", *args, "--root", str(self.repo.path)],
                              capture_output=True, text=True, env=full_env)


class CliManifestAndBranchTest(CliCase):
    def test_clean_repo_has_no_findings(self) -> None:
        result = self.cli("manifest", "branch")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_reports_expired_waiver(self) -> None:
        self.repo.manifest(waivers=[{"rule": "UI-001", "scope": "web/", "reason": "legacy vue app",
                                     "approved_by": "@owner", "expires": "2000-01-01"}])
        self.assertIn("expired on 2000-01-01", self.cli("manifest").stdout)

    def test_rejects_unknown_branch_pattern(self) -> None:
        result = self.cli("branch", "--branch", "feature/new-thing")
        self.assertEqual(result.returncode, 1)
        self.assertIn("BRA-003", result.stdout)

    def test_accepts_task_and_bot_branches(self) -> None:
        for branch in ("013-t004-bom-parser", "fix/cart-rounding", "release/v1.4", "dependabot/pip/ruff-0.7"):
            self.assertEqual(self.cli("branch", "--branch", branch).returncode, 0, branch)


class CliSpecFlowTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo.git("checkout", "-q", "-b", "013-faster-restock")
        self.repo.write("specs/013-faster-restock/spec.md", "**Status**: Draft\n- **FR-001**: WHEN x THE SYSTEM SHALL y\n")
        self.repo.write("app/main.py", "x = 1\n")
        self.repo.commit_all("feat: start")

    def test_blocks_source_change_before_approval(self) -> None:
        result = self.cli("spec", "--base", "main")
        self.assertEqual(result.returncode, 1)
        self.assertIn("SDD-001", result.stdout)

    def test_flags_new_spec_without_brainstorm(self) -> None:
        self.assertIn("BRN-001", self.cli("brainstorm", "--base", "main").stdout)

    def test_traceability_for_implemented_spec(self) -> None:
        self.repo.write("specs/013-faster-restock/spec.md", "**Status**: Implemented\n- **FR-001**: x\n- **FR-002**: y\n")
        self.repo.write("tests/test_bom.py", '@pytest.mark.req("013/FR-001")\ndef test_x(): ...\n')
        result = self.cli("trace")
        self.assertIn("013/FR-002", result.stdout)
        self.assertNotIn("013/FR-001 has no test", result.stdout)


class CliAdrTest(CliCase):
    def adr(self, name: str, status: str) -> None:
        self.repo.write(f"docs/adr/{name}", f"---\nstatus: {status}\ndate: 2026-10-18\n---\n# T\n")

    def test_consistent_log_passes(self) -> None:
        self.adr("0001-record-architecture-decisions.md", "accepted")
        self.repo.write("docs/adr/README.md", "| ADR | Title | Status | Date |\n|--|--|--|--|\n"
                        "| [0001](0001-record-architecture-decisions.md) | Record | accepted | 2026-10-18 |\n")
        self.assertEqual(self.cli("adr").returncode, 0)

    def test_reports_index_drift_and_bad_status(self) -> None:
        self.adr("0001-record-architecture-decisions.md", "accepted")
        self.adr("0002-use-fastapi.md", "superseded by ADR-0009")
        self.adr("0002-duplicate-number.md", "maybe")
        self.repo.write("docs/adr/README.md", "| [0001](0001-record-architecture-decisions.md) | Record | proposed | x |\n")
        out = self.cli("adr").stdout
        self.assertIn("used twice", out)
        self.assertIn("ADR-0009 doesn't exist", out)
        self.assertIn("Index says 'proposed'", out)
        self.assertIn("not listed in the index", out)


class CliPrTest(CliCase):
    def test_reports_bad_title_and_missing_sections(self) -> None:
        body = self.repo.path / "body.md"
        body.write_text("## What and why\nAdds BOM paste.\n\n## Spec\n\n")
        result = self.cli("pr", "--title", "Added stuff", "--body-file", str(body))
        self.assertIn("PR-001", result.stdout)
        self.assertIn("'## Spec' is empty", result.stdout)
        self.assertIn("missing the section '## How I tested'", result.stdout)

    def test_new_ui_library_needs_adr_and_justification(self) -> None:
        self.repo.write("package.json", json.dumps({"dependencies": {"react": "19.0.0"}}))
        self.repo.commit_all("chore: add package.json")
        self.repo.git("checkout", "-q", "-b", "chore/add-mui")
        self.repo.write("package.json", json.dumps({"dependencies": {"react": "19.0.0", "@mui/material": "6.0.0"}}))
        self.repo.commit_all("chore: add mui")
        out = self.cli("deps", "--base", "main", env={"PR_BODY": "no reasons given"}).stdout
        self.assertIn("UI-002", out)
        self.assertIn("SEC-004", out)


if __name__ == "__main__":
    unittest.main()
