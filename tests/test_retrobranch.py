#!/usr/bin/env python3
"""
Unit tests for retrobranch.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure retrobranch package can be imported
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PACKAGE_DIR)

from retrobranch.engine import (
    add_pr_label,
    do_modified_lines_exist_in_target,
    fetch_pr_info,
    fetch_remote_file_content,
    format_label,
    get_maintained_branches,
    is_feature_pr,
    parse_pr_number,
    post_pr_comment,
    run_subproc,
    was_commit_previously_backported,
)
from retrobranch.exceptions import (
    BranchConfigError,
    GHAuthError,
    GHCommandError,
    GitCommandError,
    PRNotFoundError,
    RetrobranchError,
)
from retrobranch.sources import (
    BaseBranchSource,
    CallableBranchSource,
    CompositeBranchSource,
    ExplicitBranchSource,
    GenericFileBranchSource,
    MergifyBranchSource,
    RawContentBranchSource,
)


class TestExceptions(unittest.TestCase):
    """Tests non-CLI exception handling and custom exception hierarchy."""

    def test_pr_not_found_exception(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            mock_cmd.return_value = (1, "", "GraphQL: Could not resolve to a PullRequest with the number of 999")
            with self.assertRaises(PRNotFoundError) as ctx:
                fetch_pr_info(999)
            self.assertIn("Pull Request #999 could not be found", str(ctx.exception))
            self.assertTrue(issubclass(PRNotFoundError, RetrobranchError))

    def test_gh_auth_exception(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            mock_cmd.return_value = (1, "", "To use 'gh', set GH_TOKEN or run 'gh auth login'")
            with self.assertRaises(GHAuthError) as ctx:
                fetch_pr_info(123)
            self.assertIn("not authenticated", str(ctx.exception))
            self.assertTrue(issubclass(GHAuthError, RetrobranchError))

    def test_git_command_exception(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value.returncode = 128
            mock_sub.return_value.stderr = "fatal: not a git repository"
            with self.assertRaises(GitCommandError) as ctx:
                run_subproc(["git", "status"], check=True)
            self.assertIn("Command failed", str(ctx.exception))
            self.assertTrue(issubclass(GitCommandError, RetrobranchError))


class TestPRParsing(unittest.TestCase):
    """Tests parse_pr_number for integer, string, '#123', and full GitHub PR URL formats."""

    def test_integer_input(self):
        self.assertEqual(parse_pr_number(1234), 1234)

    def test_string_digits(self):
        self.assertEqual(parse_pr_number("5678"), 5678)

    def test_hash_prefix(self):
        self.assertEqual(parse_pr_number("#999"), 999)

    def test_github_pr_url(self):
        self.assertEqual(parse_pr_number("https://github.com/owner/repo/pull/4321"), 4321)
        self.assertEqual(parse_pr_number("https://github.com/owner/repo/pull/4321/files"), 4321)

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            parse_pr_number("invalid_string")


class TestFeatureClassification(unittest.TestCase):
    """Tests is_feature_pr classification."""

    def test_conventional_commit_feature(self):
        is_feat, reason = is_feature_pr("feat: add new planner algorithm")
        self.assertTrue(is_feat)
        self.assertIn("feature", reason.lower())

        is_feat, _ = is_feature_pr("feature(core): allow custom collision matrix")
        self.assertTrue(is_feat)

        is_feat, _ = is_feature_pr("[Feature] New Cartesian planner")
        self.assertTrue(is_feat)

    def test_conventional_commit_bugfix(self):
        is_feat, reason = is_feature_pr("fix: synchronize execution complete flag (#3853)")
        self.assertFalse(is_feat)
        self.assertIn("eligible", reason.lower())

        is_feat, _ = is_feature_pr("fix(ik): avoid divide by zero in Jacobian")
        self.assertFalse(is_feat)

        is_feat, _ = is_feature_pr("docs: update README installation steps")
        self.assertFalse(is_feat)

        is_feat, _ = is_feature_pr("chore: clean up deprecated compiler flags")
        self.assertFalse(is_feat)

    def test_feature_labels(self):
        is_feat, reason = is_feature_pr("Fix planning bug", labels=["enhancement"])
        self.assertTrue(is_feat)
        self.assertIn("enhancement", reason)

        is_feat, _ = is_feature_pr("Fix planning bug", labels=["feature-request"])
        self.assertTrue(is_feat)

    def test_head_branch_prefix(self):
        is_feat, reason = is_feature_pr("Improve Cartesian interpolator", head_branch="feat/interpolator-v2")
        self.assertTrue(is_feat)
        self.assertIn("head branch", reason.lower())

    def test_bot_backport_pr(self):
        is_feat, reason = is_feature_pr("Fix forward kinematics (#3800) (backport #3805)")
        self.assertTrue(is_feat)
        self.assertIn("already a backport", reason.lower())

    def test_release_pr(self):
        is_feat, reason = is_feature_pr("2.12.0")
        self.assertTrue(is_feat)
        self.assertIn("release", reason.lower())


class TestBranchSources(unittest.TestCase):
    """Tests individual branch source implementations."""

    def test_explicit_branch_source(self):
        src1 = ExplicitBranchSource(["release-1.0", "release-2.0"])
        self.assertEqual(src1.get_branches(), ["release-1.0", "release-2.0"])

        src2 = ExplicitBranchSource("release-1.0, release-2.0, release-3.0")
        self.assertEqual(src2.get_branches(), ["release-1.0", "release-2.0", "release-3.0"])

    def test_generic_yaml_list_source(self):
        content = "- release-1.0\n- release-2.0\n"
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(content)
            temp_path = f.name

        try:
            src = GenericFileBranchSource(temp_path)
            self.assertEqual(src.get_branches(), ["release-1.0", "release-2.0"])
        finally:
            os.remove(temp_path)

    def test_generic_yaml_dict_source(self):
        content = "maintained_branches:\n  - release-1.0\n  - release-2.0\n"
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(content)
            temp_path = f.name

        try:
            src = GenericFileBranchSource(temp_path)
            self.assertEqual(src.get_branches(), ["release-1.0", "release-2.0"])
        finally:
            os.remove(temp_path)

    def test_generic_text_file_source(self):
        content = "# Maintained branches\nrelease-1.0\nrelease-2.0\n\n# deprecated\n# release-0.9\n"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(content)
            temp_path = f.name

        try:
            src = GenericFileBranchSource(temp_path, format_type="text")
            self.assertEqual(src.get_branches(), ["release-1.0", "release-2.0"])
        finally:
            os.remove(temp_path)

    def test_callable_branch_source(self):
        src = CallableBranchSource(lambda: ["release-1.0", "release-2.0"])
        self.assertEqual(src.get_branches(), ["release-1.0", "release-2.0"])

    def test_composite_branch_source(self):
        src1 = ExplicitBranchSource(["release-1.0"])
        src2 = ExplicitBranchSource(["release-2.0", "release-1.0"])  # duplicate release-1.0
        composite = CompositeBranchSource([src1, src2])
        self.assertEqual(composite.get_branches(), ["release-1.0", "release-2.0"])

    def test_raw_content_yaml_list_source(self):
        content = "- humble\n- jazzy\n- kilted\n"
        src = RawContentBranchSource(content)
        self.assertEqual(src.get_branches(), ["humble", "jazzy", "kilted"])

    def test_raw_content_mergify_source(self):
        content = """
pull_request_rules:
  - name: backport to humble
    actions:
      backport:
        branches:
          - humble
  - name: backport to jazzy
    actions:
      backport:
        branches:
          - jazzy
"""
        src = RawContentBranchSource(content)
        self.assertEqual(src.get_branches(), ["humble", "jazzy"])

    def test_raw_content_text_source(self):
        content = "# target branches\nhumble\njazzy\n"
        src = RawContentBranchSource(content, format_type="text")
        self.assertEqual(src.get_branches(), ["humble", "jazzy"])


class TestMaintainedBranches(unittest.TestCase):
    """Tests get_maintained_branches parsing and filtering."""

    def test_explicit_branches(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            mock_cmd.return_value = (
                0,
                "refs/heads/main\nrefs/heads/release-1.0\nrefs/heads/release-2.0\nrefs/heads/release-3.0",
                "",
            )
            branches = get_maintained_branches(explicit_branches=["release-1.0", "release-2.0"])
            self.assertEqual(branches, ["release-1.0", "release-2.0"])

    def test_generic_config_path(self):
        content = "branches:\n  - release-1.0\n  - release-2.0\n"
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(content)
            temp_path = f.name

        try:
            with patch("retrobranch.engine.run_subproc") as mock_cmd:
                mock_cmd.return_value = (
                    0,
                    "refs/heads/main\nrefs/heads/release-1.0\nrefs/heads/release-2.0\nrefs/heads/release-3.0",
                    "",
                )
                branches = get_maintained_branches(config_path=temp_path)
                self.assertEqual(branches, ["release-1.0", "release-2.0"])
        finally:
            os.remove(temp_path)

    def test_mergify_yaml_parsing(self):
        mock_mergify = """
pull_request_rules:
  - name: backport to release-1.0
    actions:
      backport:
        branches:
          - release-1.0
  - name: backport to release-2.0
    actions:
      backport:
        branches:
          - release-2.0
  - name: backport to release-3.0
    actions:
      backport:
        branches:
          - release-3.0
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(mock_mergify)
            temp_path = f.name

        try:
            with patch("retrobranch.engine.run_subproc") as mock_cmd:
                mock_cmd.return_value = (
                    0,
                    "refs/heads/main\nrefs/heads/release-1.0\nrefs/heads/release-2.0\nrefs/heads/release-3.0",
                    "",
                )
                branches = get_maintained_branches(mergify_path=temp_path)
                self.assertEqual(branches, ["release-1.0", "release-2.0", "release-3.0"])

                filtered = get_maintained_branches(
                    mergify_path=temp_path, branch_filter="release-1.0,release-2.0"
                )
                self.assertEqual(filtered, ["release-1.0", "release-2.0"])
        finally:
            os.remove(temp_path)


class TestModifiedLinesCheck(unittest.TestCase):
    """Tests do_modified_lines_exist_in_target."""

    def test_lines_match(self):
        file_content = "line 1\nline 2 with meaningful code\nline 3\n"
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            mock_cmd.return_value = (0, file_content, "")
            res = do_modified_lines_exist_in_target(
                "origin/release-1.0", "test.cpp", ["line 2 with meaningful code"]
            )
            self.assertTrue(res)

    def test_lines_do_not_match(self):
        file_content = "different content here\n"
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            mock_cmd.return_value = (0, file_content, "")
            res = do_modified_lines_exist_in_target(
                "origin/release-1.0", "test.cpp", ["nonexistent meaningful code string"]
            )
            self.assertFalse(res)


class TestPreviousBackportCheck(unittest.TestCase):
    """Tests was_commit_previously_backported."""

    def test_found_by_commit_sha(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            mock_cmd.return_value = (0, "abc1234 Cherry-pick of commit 123456789", "")
            was_bp, detail = was_commit_previously_backported("123456789abcdef", "origin/release-1.0")
            self.assertTrue(was_bp)
            self.assertIn("previously backported", detail)

    def test_not_found(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            was_bp, _ = was_commit_previously_backported("123456789abcdef", "origin/release-1.0")
            self.assertFalse(was_bp)


class TestPRInteractions(unittest.TestCase):
    """Tests add_pr_label, post_pr_comment, and repo targeting."""

    def test_dry_run_label(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            res = add_pr_label(1234, "backport-release-1.0", dry_run=True)
            self.assertTrue(res)
            mock_cmd.assert_not_called()

    def test_add_pr_label_rest_success(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            mock_cmd.return_value = (0, "[]", "")
            res = add_pr_label(3593, "backport-humble", repo="moveit/moveit2")
            self.assertTrue(res)
            mock_cmd.assert_called_once_with([
                "gh", "api", "repos/moveit/moveit2/issues/3593/labels", "-f", "labels[]=backport-humble"
            ])

    def test_add_pr_label_fallback_to_edit(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            # First call to gh api fails (e.g. unknown API error)
            # Second call to gh pr edit succeeds
            mock_cmd.side_effect = [
                (1, "", "API error"),
                (0, "", "")
            ]
            res = add_pr_label(3593, "backport-humble", repo="moveit/moveit2")
            self.assertTrue(res)
            self.assertEqual(mock_cmd.call_count, 2)
            self.assertEqual(mock_cmd.call_args_list[1][0][0], [
                "gh", "pr", "edit", "3593", "--add-label", "backport-humble", "-R", "moveit/moveit2"
            ])

    def test_add_pr_label_auto_create_when_missing(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            # First call: 404 Not Found
            # Second call: create label succeeds
            # Third call: add label succeeds
            mock_cmd.side_effect = [
                (1, "", "404 Not Found"),
                (0, "", ""),
                (0, "", "")
            ]
            res = add_pr_label(3593, "backport-new", repo="moveit/moveit2")
            self.assertTrue(res)
            self.assertEqual(mock_cmd.call_count, 3)

    def test_post_pr_comment_success(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            res = post_pr_comment(3593, "Backport report", repo="moveit/moveit2")
            self.assertTrue(res)
            mock_cmd.assert_called_once_with([
                "gh", "pr", "comment", "3593", "--body", "Backport report", "-R", "moveit/moveit2"
            ])

    def test_fetch_pr_info_with_repo(self):
        with patch("retrobranch.engine.run_subproc") as mock_cmd:
            mock_cmd.return_value = (0, '{"number": 3593, "title": "test", "url": "https://github.com/moveit/moveit2/pull/3593"}', "")
            data = fetch_pr_info(3593, repo="moveit/moveit2")
            self.assertEqual(data["number"], 3593)
            mock_cmd.assert_called_once_with([
                "gh", "pr", "view", "3593", "--json",
                "number,title,labels,headRefName,mergeCommit,mergedAt,baseRefName,url",
                "-R", "moveit/moveit2"
            ])


class TestLabelFormatting(unittest.TestCase):
    """Tests format_label template customization."""

    def test_default_template(self):
        self.assertEqual(format_label(None, "humble"), "backport-humble")
        self.assertEqual(format_label("", "humble"), "backport-humble")

    def test_branch_placeholder(self):
        self.assertEqual(format_label("backport-{branch}", "humble"), "backport-humble")
        self.assertEqual(format_label("cherry-pick:{branch}", "jazzy"), "cherry-pick:jazzy")
        self.assertEqual(format_label("bp/{branch}", "kilted"), "bp/kilted")

    def test_target_placeholder(self):
        self.assertEqual(format_label("bp-{target}", "humble"), "bp-humble")
        self.assertEqual(format_label("backport-{target_branch}", "jazzy"), "backport-jazzy")

    def test_percent_s_placeholder(self):
        self.assertEqual(format_label("backport-%s", "humble"), "backport-humble")
        self.assertEqual(format_label("bp/%s", "rolling"), "bp/rolling")

    def test_empty_braces(self):
        self.assertEqual(format_label("backport-{}", "humble"), "backport-humble")

    def test_prefix_with_separator(self):
        self.assertEqual(format_label("backport-", "humble"), "backport-humble")
        self.assertEqual(format_label("bp/", "humble"), "bp/humble")
        self.assertEqual(format_label("cherry-pick:", "humble"), "cherry-pick:humble")
        self.assertEqual(format_label("backport_", "humble"), "backport_humble")

    def test_static_label(self):
        self.assertEqual(format_label("needs-backport", "humble"), "needs-backport")


class TestBaseBranchConfig(unittest.TestCase):
    """Tests base branch resolution from configuration file and CLI flags."""

    def test_cli_resolves_base_branch_from_config(self):
        from retrobranch.cli import main
        mock_pr = {
            "number": 100,
            "title": "fix: bugfix",
            "labels": [],
            "headRefName": "fix-1",
            "baseRefName": "develop",
            "mergeCommit": {"oid": "abcdef1234567890"},
            "mergedAt": "2026-10-07T12:00:00Z",
            "url": "https://github.com/owner/repo/pull/100",
        }
        config_content = "base_branch: develop\nmaintained_branches:\n  - test-ci\n"
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(config_content)
            config_path = f.name

        try:
            with patch("sys.argv", ["retrobr", "100", "--config-file", config_path, "--dry-run"]), \
                 patch("retrobranch.engine.fetch_pr_info", return_value=mock_pr), \
                 patch("retrobranch.engine.verify_issue_presence_in_branch", return_value=(True, "matched")), \
                 patch("retrobranch.engine.run_subproc", return_value=(0, "refs/heads/test-ci", "")):
                main()
        finally:
            os.remove(config_path)

    def test_cli_flag_overrides_config_base_branch(self):
        from retrobranch.cli import main
        mock_pr = {
            "number": 100,
            "title": "fix: bugfix",
            "labels": [],
            "headRefName": "fix-1",
            "baseRefName": "staging",
            "mergeCommit": {"oid": "abcdef1234567890"},
            "mergedAt": "2026-10-07T12:00:00Z",
            "url": "https://github.com/owner/repo/pull/100",
        }
        config_content = "base_branch: develop\nmaintained_branches:\n  - test-ci\n"
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(config_content)
            config_path = f.name

        try:
            with patch("sys.argv", ["retrobr", "100", "--config-file", config_path, "--base-branch", "staging", "--dry-run"]), \
                 patch("retrobranch.engine.fetch_pr_info", return_value=mock_pr), \
                 patch("retrobranch.engine.verify_issue_presence_in_branch", return_value=(True, "matched")), \
                 patch("retrobranch.engine.run_subproc", return_value=(0, "refs/heads/test-ci", "")):
                main()
        finally:
            os.remove(config_path)

    def test_cli_auto_detects_default_branch(self):
        from retrobranch.cli import main
        mock_pr = {
            "number": 100,
            "title": "fix: bugfix",
            "labels": [],
            "headRefName": "fix-1",
            "baseRefName": "rolling",
            "mergeCommit": {"oid": "abcdef1234567890"},
            "mergedAt": "2026-10-07T12:00:00Z",
            "url": "https://github.com/owner/repo/pull/100",
        }
        with patch("sys.argv", ["retrobr", "100", "--target-branches", "test-ci", "--dry-run"]), \
             patch("retrobranch.engine.fetch_pr_info", return_value=mock_pr), \
             patch("retrobranch.engine.detect_repo_default_branch", return_value="rolling") as mock_detect, \
             patch("retrobranch.engine.verify_issue_presence_in_branch", return_value=(True, "matched")) as mock_verify, \
             patch("retrobranch.engine.run_subproc", return_value=(0, "refs/heads/test-ci", "")):
            with self.assertLogs("retrobranch", level="INFO") as cm:
                main()
            mock_detect.assert_called_once_with(repo="owner/repo")
            mock_verify.assert_called_once_with("abcdef1234567890", "test-ci")
            self.assertTrue(any("Recognized main branch: 'rolling' (auto-detected branch 'rolling' from repository: https://github.com/owner/repo)" in msg for msg in cm.output))

    def test_cli_falls_back_to_main_when_auto_detect_returns_none(self):
        from retrobranch.cli import main
        mock_pr = {
            "number": 100,
            "title": "fix: bugfix",
            "labels": [],
            "headRefName": "fix-1",
            "baseRefName": "main",
            "mergeCommit": {"oid": "abcdef1234567890"},
            "mergedAt": "2026-10-07T12:00:00Z",
            "url": "https://github.com/owner/repo/pull/100",
        }
        with patch("sys.argv", ["retrobr", "100", "--target-branches", "test-ci", "--dry-run"]), \
             patch("retrobranch.engine.fetch_pr_info", return_value=mock_pr), \
             patch("retrobranch.engine.detect_repo_default_branch", return_value=None), \
             patch("retrobranch.engine.verify_issue_presence_in_branch", return_value=(True, "matched")) as mock_verify, \
             patch("retrobranch.engine.run_subproc", return_value=(0, "refs/heads/test-ci", "")):
            with self.assertLogs("retrobranch", level="INFO") as cm:
                main()
            mock_verify.assert_called_once_with("abcdef1234567890", "test-ci")
            self.assertTrue(any("Recognized main branch: 'main' (default fallback)" in msg for msg in cm.output))

    def test_cli_skips_when_pr_already_has_label(self):
        from retrobranch.cli import main
        mock_pr = {
            "number": 100,
            "title": "fix: bugfix",
            "labels": [{"name": "backport-test-ci"}],
            "headRefName": "fix-1",
            "baseRefName": "main",
            "mergeCommit": {"oid": "abcdef1234567890"},
            "mergedAt": "2026-10-07T12:00:00Z",
            "url": "https://github.com/owner/repo/pull/100",
        }
        with patch("sys.argv", ["retrobr", "100", "--target-branches", "test-ci", "--dry-run"]), \
             patch("retrobranch.engine.fetch_pr_info", return_value=mock_pr), \
             patch("retrobranch.engine.detect_repo_default_branch", return_value="main"), \
             patch("retrobranch.engine.verify_issue_presence_in_branch") as mock_verify, \
             patch("retrobranch.engine.run_subproc", return_value=(0, "refs/heads/test-ci", "")):
            with self.assertLogs("retrobranch", level="INFO") as cm:
                main()
            mock_verify.assert_not_called()
            self.assertTrue(any("PR #100 already has label 'backport-test-ci' for branch 'test-ci'. Skipping check." in msg for msg in cm.output))


class TestDetectRepoDefaultBranch(unittest.TestCase):
    """Tests programmatic default branch detection."""

    def test_detect_from_symbolic_ref_origin(self):
        from retrobranch.engine import detect_repo_default_branch

        def mock_run(cmd, *args, **kwargs):
            if cmd[:3] == ["git", "symbolic-ref", "--short"]:
                return (0, "origin/master", "")
            return (1, "", "")

        with patch("retrobranch.engine.run_subproc", side_effect=mock_run):
            branch = detect_repo_default_branch()
            self.assertEqual(branch, "master")

    def test_detect_from_symbolic_ref_upstream(self):
        from retrobranch.engine import detect_repo_default_branch

        def mock_run(cmd, *args, **kwargs):
            if cmd == ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]:
                return (1, "", "")
            if cmd == ["git", "symbolic-ref", "--short", "refs/remotes/upstream/HEAD"]:
                return (0, "upstream/main", "")
            return (1, "", "")

        with patch("retrobranch.engine.run_subproc", side_effect=mock_run):
            branch = detect_repo_default_branch()
            self.assertEqual(branch, "main")

    def test_detect_from_ls_remote(self):
        from retrobranch.engine import detect_repo_default_branch

        def mock_run(cmd, *args, **kwargs):
            if cmd[:3] == ["git", "symbolic-ref", "--short"]:
                return (1, "", "")
            if cmd[:4] == ["git", "ls-remote", "--symref", "origin"]:
                return (0, "ref: refs/heads/rolling\tHEAD\nabcdef123\tHEAD", "")
            return (1, "", "")

        with patch("retrobranch.engine.run_subproc", side_effect=mock_run):
            branch = detect_repo_default_branch()
            self.assertEqual(branch, "rolling")

    def test_detect_from_gh_repo_view(self):
        from retrobranch.engine import detect_repo_default_branch

        def mock_run(cmd, *args, **kwargs):
            if cmd[0] == "git":
                return (1, "", "")
            if cmd[:3] == ["gh", "repo", "view"]:
                return (0, "develop", "")
            return (1, "", "")

        with patch("retrobranch.engine.run_subproc", side_effect=mock_run):
            branch = detect_repo_default_branch(repo="owner/repo")
            self.assertEqual(branch, "develop")

    def test_detect_all_fail_returns_none(self):
        from retrobranch.engine import detect_repo_default_branch

        with patch("retrobranch.engine.run_subproc", return_value=(1, "", "")):
            branch = detect_repo_default_branch(repo="owner/repo")
            self.assertIsNone(branch)

    def test_detect_with_repo_ignores_unrelated_local_git_repo(self):
        from retrobranch.engine import detect_repo_default_branch

        def mock_run(cmd, *args, **kwargs):
            # Local directory belongs to unrelated repo with 'origin/develop'
            if cmd[:3] == ["git", "remote", "get-url"]:
                return (0, "https://github.com/unrelated/hut_10sqft.git", "")
            if cmd[:3] == ["git", "symbolic-ref", "--short"]:
                return (0, "origin/develop", "")
            # gh repo view queries the target repository
            if cmd[:4] == ["gh", "repo", "view", "moveit/moveit2"]:
                return (0, "main", "")
            return (1, "", "")

        with patch("retrobranch.engine.run_subproc", side_effect=mock_run):
            branch = detect_repo_default_branch(repo="moveit/moveit2")
            self.assertEqual(branch, "main")

    def test_detect_with_repo_from_ls_remote(self):
        from retrobranch.engine import detect_repo_default_branch

        def mock_run(cmd, *args, **kwargs):
            # gh fails
            if cmd[:3] == ["gh", "repo", "view"]:
                return (1, "", "")
            # git ls-remote queries target repo URL directly
            if cmd[:3] == ["git", "ls-remote", "--symref"] and "https://github.com/moveit/moveit2" in cmd:
                return (0, "ref: refs/heads/main\tHEAD\na9004a43\tHEAD", "")
            return (1, "", "")

        with patch("retrobranch.engine.run_subproc", side_effect=mock_run):
            branch = detect_repo_default_branch(repo="moveit/moveit2")
            self.assertEqual(branch, "main")


class TestPathPatternTargetBranches(unittest.TestCase):
    """Tests interfaces for modifying PATH_PATTERN_TARGET_BRANCHES."""

    def tearDown(self):
        from retrobranch.engine import reset_path_pattern_target_branches
        reset_path_pattern_target_branches()

    def test_overwrite_with_list_of_path_patterns(self):
        from retrobranch.engine import (
            modify_path_pattern_target_branches,
            get_path_pattern_target_branches,
            set_path_pattern_target_branches,
        )
        custom_list = [".github/custom.yml", "configs/branches.txt"]
        result = modify_path_pattern_target_branches(custom_list)
        self.assertEqual(result, custom_list)
        self.assertEqual(get_path_pattern_target_branches(), custom_list)

        # Using set_path_pattern_target_branches
        new_list = ["one.yml", "two.yml"]
        result2 = set_path_pattern_target_branches(new_list)
        self.assertEqual(result2, new_list)
        self.assertEqual(get_path_pattern_target_branches(), new_list)

    def test_append_list_of_path_patterns(self):
        from retrobranch.engine import (
            modify_path_pattern_target_branches,
            add_path_pattern_target_branches,
            get_path_pattern_target_branches,
            DEFAULT_PATH_PATTERN_TARGET_BRANCHES,
        )
        extra_patterns = ["custom_appended.yml", "another.yml"]
        result = modify_path_pattern_target_branches(extra_patterns, append=True)
        expected = list(DEFAULT_PATH_PATTERN_TARGET_BRANCHES) + extra_patterns
        self.assertEqual(result, expected)
        self.assertEqual(get_path_pattern_target_branches(), expected)

        # Using add_path_pattern_target_branches (defaults to append)
        more_patterns = ["third.yml"]
        result2 = add_path_pattern_target_branches(more_patterns)
        self.assertEqual(result2, expected + more_patterns)

    def test_add_single_entry(self):
        from retrobranch.engine import (
            add_path_pattern_target_branch,
            modify_path_pattern_target_branches,
            get_path_pattern_target_branches,
            DEFAULT_PATH_PATTERN_TARGET_BRANCHES,
        )
        single = "single_config.yml"
        result = add_path_pattern_target_branch(single)
        expected = list(DEFAULT_PATH_PATTERN_TARGET_BRANCHES) + [single]
        self.assertEqual(result, expected)
        self.assertEqual(get_path_pattern_target_branches(), expected)

        # Adding single string via modify_path_pattern_target_branches with append=True
        single2 = "another_single.yml"
        result2 = modify_path_pattern_target_branches(single2, append=True)
        self.assertEqual(result2, expected + [single2])

    def test_reset_path_patterns(self):
        from retrobranch.engine import (
            modify_path_pattern_target_branches,
            reset_path_pattern_target_branches,
            get_path_pattern_target_branches,
            DEFAULT_PATH_PATTERN_TARGET_BRANCHES,
        )
        modify_path_pattern_target_branches(["temporary.yml"])
        self.assertEqual(get_path_pattern_target_branches(), ["temporary.yml"])

        reset_result = reset_path_pattern_target_branches()
        self.assertEqual(reset_result, list(DEFAULT_PATH_PATTERN_TARGET_BRANCHES))
        self.assertEqual(get_path_pattern_target_branches(), list(DEFAULT_PATH_PATTERN_TARGET_BRANCHES))

    def test_invalid_type_raises_type_error(self):
        from retrobranch.engine import modify_path_pattern_target_branches
        with self.assertRaises(TypeError):
            modify_path_pattern_target_branches(123)  # type: ignore


class TestGetRepoUrl(unittest.TestCase):
    """Tests repository URL resolution and normalization."""

    def test_repo_url_from_pr_url(self):
        from retrobranch.engine import get_repo_url
        url = get_repo_url(pr_url="https://github.com/owner/repo/pull/123")
        self.assertEqual(url, "https://github.com/owner/repo")

    def test_repo_url_from_repo_slug(self):
        from retrobranch.engine import get_repo_url
        url = get_repo_url(repo="owner/repo")
        self.assertEqual(url, "https://github.com/owner/repo")

    def test_repo_url_from_gh_cli(self):
        from retrobranch.engine import get_repo_url

        def mock_run(cmd, *args, **kwargs):
            if cmd[:3] == ["gh", "repo", "view"]:
                return (0, "https://github.com/cli-owner/cli-repo", "")
            return (1, "", "")

        with patch("retrobranch.engine.run_subproc", side_effect=mock_run):
            url = get_repo_url()
            self.assertEqual(url, "https://github.com/cli-owner/cli-repo")

    def test_repo_url_from_git_remote_ssh(self):
        from retrobranch.engine import get_repo_url

        def mock_run(cmd, *args, **kwargs):
            if cmd[:3] == ["git", "remote", "get-url"]:
                return (0, "git@github.com:ssh-owner/ssh-repo.git", "")
            return (1, "", "")

        with patch("retrobranch.engine.run_subproc", side_effect=mock_run):
            url = get_repo_url()
            self.assertEqual(url, "https://github.com/ssh-owner/ssh-repo")

    def test_repo_url_from_git_remote_https(self):
        from retrobranch.engine import get_repo_url

        def mock_run(cmd, *args, **kwargs):
            if cmd[:3] == ["git", "remote", "get-url"]:
                return (0, "https://github.com/https-owner/https-repo.git", "")
            return (1, "", "")

        with patch("retrobranch.engine.run_subproc", side_effect=mock_run):
            url = get_repo_url()
            self.assertEqual(url, "https://github.com/https-owner/https-repo")

    def test_repo_url_all_fail_returns_none(self):
        from retrobranch.engine import get_repo_url

        with patch("retrobranch.engine.run_subproc", return_value=(1, "", "")):
            url = get_repo_url()
            self.assertIsNone(url)


class TestFetchRemoteFileContent(unittest.TestCase):
    """Tests fetching file contents remotely via GitHub API."""

    def test_fetch_remote_file_content_success(self):
        import base64
        import json
        raw_text = "maintained_branches:\n  - humble\n"
        encoded = base64.b64encode(raw_text.encode("utf-8")).decode("utf-8")
        mock_json = json.dumps({"content": encoded})

        with patch("retrobranch.engine.run_subproc", return_value=(0, mock_json, "")) as mock_cmd:
            content = fetch_remote_file_content("owner/repo", ".github/retrobranch.yml")
            self.assertEqual(content, raw_text)
            mock_cmd.assert_called_once_with(["gh", "api", "repos/owner/repo/contents/.github/retrobranch.yml"])

    def test_fetch_remote_file_content_with_ref(self):
        import base64
        import json
        raw_text = "target"
        encoded = base64.b64encode(raw_text.encode("utf-8")).decode("utf-8")
        mock_json = json.dumps({"content": encoded})

        with patch("retrobranch.engine.run_subproc", return_value=(0, mock_json, "")) as mock_cmd:
            content = fetch_remote_file_content("owner/repo", ".github/retrobranch.yml", ref="mybranch")
            self.assertEqual(content, raw_text)
            mock_cmd.assert_called_once_with(
                ["gh", "api", "repos/owner/repo/contents/.github/retrobranch.yml", "-f", "ref=mybranch"]
            )

    def test_fetch_remote_file_content_failure(self):
        with patch("retrobranch.engine.run_subproc", return_value=(1, "", "Not Found")):
            content = fetch_remote_file_content("owner/repo", "nonexistent.yml")
            self.assertIsNone(content)


class TestRemoteConfigPrioritization(unittest.TestCase):
    """Tests that remote config is prioritized when PR URL is provided and elaborated messages are logged."""

    def test_remote_config_prioritized_when_pr_url_provided(self):
        from retrobranch.cli import main
        mock_pr = {
            "number": 100,
            "title": "fix: bugfix",
            "labels": [],
            "headRefName": "fix-1",
            "baseRefName": "main",
            "mergeCommit": {"oid": "abcdef1234567890"},
            "mergedAt": "2026-10-07T12:00:00Z",
            "url": "https://github.com/remoteowner/remoterepo/pull/100",
        }

        def mock_fetch_remote(repo, candidate, ref=None):
            if repo == "remoteowner/remoterepo" and candidate == ".github/retrobranch.yml":
                return "branches:\n  - remote-ci\n"
            return None

        def mock_run_cmd(cmd, *args, **kwargs):
            if "ls-remote" in cmd:
                return (0, "refs/heads/remote-ci\nrefs/heads/local-ci", "")
            return (0, "", "")

        with patch("sys.argv", ["retrobr", "https://github.com/remoteowner/remoterepo/pull/100", "--dry-run"]), \
             patch("retrobranch.engine.fetch_pr_info", return_value=mock_pr), \
             patch("retrobranch.engine.detect_repo_default_branch", return_value="main"), \
             patch("retrobranch.engine.fetch_remote_file_content", side_effect=mock_fetch_remote), \
             patch("retrobranch.engine.verify_issue_presence_in_branch", return_value=(True, "matched")) as mock_verify, \
             patch("retrobranch.engine.run_subproc", side_effect=mock_run_cmd):
            with self.assertLogs("retrobranch", level="INFO") as cm:
                main()
            mock_verify.assert_called_once_with("abcdef1234567890", "remote-ci", cwd=unittest.mock.ANY)
            self.assertTrue(any("Discovered target branches: ['remote-ci']" in msg for msg in cm.output))

    def test_remote_no_branches_discovered_logs_elaborated_message(self):
        from retrobranch.cli import main
        mock_pr = {
            "number": 100,
            "title": "fix: bugfix",
            "labels": [],
            "headRefName": "fix-1",
            "baseRefName": "main",
            "mergeCommit": {"oid": "abcdef1234567890"},
            "mergedAt": "2026-10-07T12:00:00Z",
            "url": "https://github.com/remoteowner/remoterepo/pull/100",
        }

        with patch("sys.argv", ["retrobr", "https://github.com/remoteowner/remoterepo/pull/100", "--dry-run"]), \
             patch("retrobranch.engine.fetch_pr_info", return_value=mock_pr), \
             patch("retrobranch.engine.detect_repo_default_branch", return_value="main"), \
             patch("retrobranch.engine.run_subproc", return_value=(0, "", "")), \
             patch("retrobranch.engine.fetch_remote_file_content", return_value=None):
            with self.assertLogs("retrobranch", level="INFO") as cm:
                with self.assertRaises(SystemExit) as exit_ctx:
                    main()
            self.assertEqual(exit_ctx.exception.code, 0)
            self.assertTrue(
                any(
                    "No maintained branches discovered for repository 'remoteowner/remoterepo' (checked remote config paths:"
                    in msg
                    for msg in cm.output
                )
            )

    def test_local_no_branches_discovered_logs_elaborated_message(self):
        from retrobranch.cli import main
        mock_pr = {
            "number": 100,
            "title": "fix: bugfix",
            "labels": [],
            "headRefName": "fix-1",
            "baseRefName": "main",
            "mergeCommit": {"oid": "abcdef1234567890"},
            "mergedAt": "2026-10-07T12:00:00Z",
            "url": "https://github.com/owner/repo/pull/100",
        }

        with patch("sys.argv", ["retrobr", "100", "--dry-run"]), \
             patch("retrobranch.engine.fetch_pr_info", return_value=mock_pr), \
             patch("retrobranch.engine.detect_repo_default_branch", return_value="main"), \
             patch("retrobranch.engine.run_subproc", return_value=(0, "", "")), \
             patch("os.path.exists", return_value=False):
            with self.assertLogs("retrobranch", level="INFO") as cm:
                with self.assertRaises(SystemExit) as exit_ctx:
                    main()
            self.assertEqual(exit_ctx.exception.code, 0)
            self.assertTrue(
                any(
                    "No maintained branches discovered in local repository" in msg and "checked local paths:" in msg
                    for msg in cm.output
                )
            )


if __name__ == "__main__":
    unittest.main()




