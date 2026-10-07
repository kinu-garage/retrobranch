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

from retrobranch.cli import parse_pr_number
from retrobranch.engine import (
    add_pr_label,
    do_modified_lines_exist_in_target,
    fetch_pr_info,
    format_label,
    get_maintained_branches,
    is_feature_pr,
    post_pr_comment,
    run_cmd,
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
)


class TestExceptions(unittest.TestCase):
    """Tests non-CLI exception handling and custom exception hierarchy."""

    def test_pr_not_found_exception(self):
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
            mock_cmd.return_value = (1, "", "GraphQL: Could not resolve to a PullRequest with the number of 999")
            with self.assertRaises(PRNotFoundError) as ctx:
                fetch_pr_info(999)
            self.assertIn("Pull Request #999 could not be found", str(ctx.exception))
            self.assertTrue(issubclass(PRNotFoundError, RetrobranchError))

    def test_gh_auth_exception(self):
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
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
                run_cmd(["git", "status"], check=True)
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


class TestMaintainedBranches(unittest.TestCase):
    """Tests get_maintained_branches parsing and filtering."""

    def test_explicit_branches(self):
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
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
            with patch("retrobranch.engine.run_cmd") as mock_cmd:
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
            with patch("retrobranch.engine.run_cmd") as mock_cmd:
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
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
            mock_cmd.return_value = (0, file_content, "")
            res = do_modified_lines_exist_in_target(
                "origin/release-1.0", "test.cpp", ["line 2 with meaningful code"]
            )
            self.assertTrue(res)

    def test_lines_do_not_match(self):
        file_content = "different content here\n"
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
            mock_cmd.return_value = (0, file_content, "")
            res = do_modified_lines_exist_in_target(
                "origin/release-1.0", "test.cpp", ["nonexistent meaningful code string"]
            )
            self.assertFalse(res)


class TestPreviousBackportCheck(unittest.TestCase):
    """Tests was_commit_previously_backported."""

    def test_found_by_commit_sha(self):
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "abc1234 Cherry-pick of commit 123456789", "")
            was_bp, detail = was_commit_previously_backported("123456789abcdef", "origin/release-1.0")
            self.assertTrue(was_bp)
            self.assertIn("previously backported", detail)

    def test_not_found(self):
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            was_bp, _ = was_commit_previously_backported("123456789abcdef", "origin/release-1.0")
            self.assertFalse(was_bp)


class TestPRInteractions(unittest.TestCase):
    """Tests add_pr_label, post_pr_comment, and repo targeting."""

    def test_dry_run_label(self):
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
            res = add_pr_label(1234, "backport-release-1.0", dry_run=True)
            self.assertTrue(res)
            mock_cmd.assert_not_called()

    def test_add_pr_label_rest_success(self):
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "[]", "")
            res = add_pr_label(3593, "backport-humble", repo="moveit/moveit2")
            self.assertTrue(res)
            mock_cmd.assert_called_once_with([
                "gh", "api", "repos/moveit/moveit2/issues/3593/labels", "-f", "labels[]=backport-humble"
            ])

    def test_add_pr_label_fallback_to_edit(self):
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
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
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
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
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            res = post_pr_comment(3593, "Backport report", repo="moveit/moveit2")
            self.assertTrue(res)
            mock_cmd.assert_called_once_with([
                "gh", "pr", "comment", "3593", "--body", "Backport report", "-R", "moveit/moveit2"
            ])

    def test_fetch_pr_info_with_repo(self):
        with patch("retrobranch.engine.run_cmd") as mock_cmd:
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


if __name__ == "__main__":
    unittest.main()

