"""
Core qualification, ancestry verification, and labeling logic for Retrobranch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple, Union
import yaml

logger = logging.getLogger(__name__)


from .exceptions import (
    GHAuthError,
    GHCommandError,
    GitCommandError,
    PRNotFoundError,
    RetrobranchError,
)
from .sources import (
    BaseBranchSource,
    CallableBranchSource,
    CompositeBranchSource,
    ExplicitBranchSource,
    GenericFileBranchSource,
    MergifyBranchSource,
)


def run_cmd(cmd, cwd=None, check=False):
    """Executes a subprocess command and returns (returncode, stdout, stderr)."""
    res = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and res.returncode != 0:
        raise GitCommandError(f"Command failed ({res.returncode}): {' '.join(cmd)}\n{res.stderr}")
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def get_maintained_branches(
    mergify_path: Optional[str] = None,
    config_path: Optional[str] = None,
    source_type: str = "auto",
    branch_filter: Optional[str] = None,
    explicit_branches: Optional[List[str]] = None,
    sources: Optional[List[Any]] = None,
) -> List[str]:
    """
    Discovers target backport branches from one or more branch sources.

    Args:
        mergify_path: Optional path to mergify.yml file (legacy shortcut).
        config_path: Optional path to generic configuration file (YAML, JSON, or text).
        source_type: Format type for config_path ('auto', 'mergify', 'yaml', 'json', 'text').
        branch_filter: Optional comma-separated list of branches to restrict evaluation to.
        explicit_branches: Optional list of branch names passed directly.
        sources: Optional list of BaseBranchSource instances, callables, or file paths.

    Returns:
        list[str]: Discovered and validated target branches.
    """
    source_list = []

    if explicit_branches:
        source_list.append(ExplicitBranchSource(explicit_branches))

    if sources:
        for s in sources:
            if isinstance(s, BaseBranchSource):
                source_list.append(s)
            elif callable(s):
                source_list.append(CallableBranchSource(s))
            elif isinstance(s, (list, tuple)):
                source_list.append(ExplicitBranchSource(s))
            elif isinstance(s, str):
                source_list.append(GenericFileBranchSource(s, source_type))

    if config_path:
        source_list.append(GenericFileBranchSource(config_path, source_type))

    if mergify_path:
        source_list.append(MergifyBranchSource(mergify_path))

    discovered_branches = CompositeBranchSource(source_list).get_branches()

    # Filter invalid branches e.g. non-existent on the remote origin
    rc, stdout, _ = run_cmd(["git", "ls-remote", "--heads", "origin"])
    if rc == 0:
        remote_heads = [
            line.split("refs/heads/")[1]
            for line in stdout.splitlines()
            if "refs/heads/" in line
        ]
        discovered_branches = [b for b in discovered_branches if b in remote_heads]

    if branch_filter:
        filter_set = set(b.strip() for b in branch_filter.split(",") if b.strip())
        discovered_branches = [b for b in discovered_branches if b in filter_set]

    return discovered_branches


def is_feature_pr(title: str, labels: Optional[List[str]] = None, head_branch: Optional[str] = None) -> Tuple[bool, str]:
    """
    Determines if a PR is a feature / capability (which should NOT be automatically backported).
    Returns (is_feature: bool, reason: str).

    Args:
        title: The title of the PR.
        labels (optional): The labels associated with the PR.
        head_branch (optional): The head branch of the PR.

    Returns:
        tuple[bool, str]: (is_feature, reason)
    """
    labels = [l.lower() for l in (labels or [])]
    title_lower = title.lower().strip()

    # Skip backport PRs created by bots
    if re.search(r"\(backport\s+#\d+\)", title_lower):
        return True, "PR is already a backport PR"

    # Feature labels
    feature_labels = {
        "enhancement",
        "feature",
        "feature-request",
        "new feature",
        "epic",
        "roadmap",
    }
    matched = feature_labels.intersection(set(labels))
    if matched:
        return True, f"PR is labeled with feature label(s): '{list(matched)[0]}'"

    # Release / bump PRs
    if "release" in labels or re.match(r"^\d+\.\d+\.\d+$", title.strip()):
        return True, "PR is a release or version bump"

    # Conventional commit feature prefix
    if re.match(r"^(feat|feature)(\([^\)]+\))?!?:", title_lower) or title_lower.startswith("[feature]") or title_lower.startswith("[feat]"):
        return True, f"PR title indicates a feature: '{title}'"

    # Head branch name convention
    if head_branch and (head_branch.startswith("feat/") or head_branch.startswith("feature/")):
        return True, f"PR head branch indicates a feature: '{head_branch}'"

    return False, "PR is a bugfix, maintenance, or non-feature change (eligible for backporting)"


def was_commit_previously_backported(commit_sha: str, target_ref: str) -> Tuple[bool, str]:
    """
    Checks if commit_sha (or its associated PR) was previously backported to target_ref.
    """
    # 1. Search git log on target branch for cherry-pick metadata referencing the commit SHA
    rc, stdout, _ = run_cmd(["git", "log", target_ref, f"--grep={commit_sha}", "-n", "1", "--oneline"])
    if rc == 0 and stdout:
        return True, f"Commit {commit_sha[:9]} was previously backported in commit: {stdout}"

    # 2. Search git log on target branch for backport PR title referencing the original PR number
    rc, commit_msg, _ = run_cmd(["git", "log", "-1", "--format=%s%n%b", commit_sha])
    if rc == 0 and commit_msg:
        pr_matches = re.findall(r"#(\d+)", commit_msg)
        for pr_num in pr_matches:
            rc, stdout, _ = run_cmd(
                ["git", "log", target_ref, f"--grep=backport.*#{pr_num}", "-n", "1", "--oneline"]
            )
            if rc == 0 and stdout:
                return True, f"Original PR #{pr_num} was previously backported in commit: {stdout}"

    return False, "No previous backport found in target branch log"


def do_modified_lines_exist_in_target(target_ref: str, file_path: str, deleted_lines: List[str]) -> bool:
    """
    Checks if non-trivial lines being modified/deleted by the PR exist in target_ref:file_path.
    """
    rc, content, _ = run_cmd(["git", "show", f"{target_ref}:{file_path}"])
    if rc != 0:
        return False
    target_lines = set(line.strip() for line in content.splitlines() if line.strip())
    meaningful = [l.strip() for l in deleted_lines if len(l.strip()) > 3]
    if not meaningful:
        return False
    matches = sum(1 for l in meaningful if l in target_lines)
    return matches >= len(meaningful) * 0.5


def verify_issue_presence_in_branch(commit: str, target_branch: str) -> Tuple[bool, str]:
    """
    Verifies if the issue/problem addressed by commit is present in origin/<target_branch>.

    The verification performs a multi-stage check against the target branch:
    1. Target branch existence:
       Ensures 'origin/<target_branch>' exists locally.
    2. File-level compatibility:
       - For newly added files ('A'): verifies their target parent directories exist
         in '<target_branch>'.
       - For modified ('M') or deleted ('D') files: verifies that every affected
         file exists in '<target_branch>'.
    3. Line-level code ancestry (issue presence):
       Checks whether the code being patched actually exists in '<target_branch>':
       - Finds the fork point ('merge_base') where '<target_branch>' split from 'main'.
       - Inspects the lines modified by this PR ('git diff -U0').
       - Traces back who originally wrote those lines ('git blame').
       - If those lines were added to 'main' AFTER '<target_branch>' split off:
         * Checks if the commit that introduced them was previously backported to '<target_branch>'.
         * Checks if the lines being changed actually exist in '<target_branch>'.
         * If neither is true, '<target_branch>' never had that code (and thus never had the bug),
           so backporting is skipped.

    Args:
        commit: The commit hash to verify.
        target_branch: The target branch name. Validity of the branch name is NOT checked i.e. it must be validated by the caller.

    Returns:
        tuple[bool, str]: (is_present, reason)
    """
    ref = f"origin/{target_branch}"

    # Verify target branch exists in local git
    rc, _, _ = run_cmd(["git", "rev-parse", "--verify", ref])
    if rc != 0:
        return False, f"Target branch '{ref}' does not exist."

    # Check modified/deleted files in commit
    rc, stdout, stderr = run_cmd(
        ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", f"{commit}~1", commit]
    )
    if rc != 0:
        return False, f"Could not inspect diff for commit {commit}: {stderr}"

    file_entries = [line.split("\t") for line in stdout.splitlines() if line]
    modified_files = [f for status, f in file_entries if status in ("M", "D")]
    added_files = [f for status, f in file_entries if status == "A"]

    # Verify newly added files: their parent directory must exist in target branch
    for f in added_files:
        dir_name = os.path.dirname(f)
        if dir_name:
            rc, _, _ = run_cmd(["git", "cat-file", "-e", f"{ref}:{dir_name}"])
            if rc != 0:
                return (
                    False,
                    f"Target directory '{dir_name}' for newly added file '{f}' does not exist in '{target_branch}'.",
                )

    if not modified_files:
        # Commit only adds new files into existing directories
        return True, f"New file(s) can be added to existing directory in '{target_branch}'."

    # Verify that pre-existing modified/deleted files exist in target branch
    missing_files = []
    for f in modified_files:
        rc, _, _ = run_cmd(["git", "cat-file", "-e", f"{ref}:{f}"])
        if rc != 0:
            missing_files.append(f)

    if missing_files:
        return False, f"Modified file(s) do not exist in '{target_branch}': {', '.join(missing_files)}"

    # Check code ancestry on modified lines
    rc, merge_base, _ = run_cmd(["git", "merge-base", f"{commit}~1", ref])
    if rc == 0 and merge_base:
        for f in modified_files:
            rc, diff_out, _ = run_cmd(["git", "diff", "-U0", f"{commit}~1", commit, "--", f])
            if rc != 0:
                continue

            # Extract deleted lines in diff
            deleted_lines = [
                line[1:] for line in diff_out.splitlines() if line.startswith("-") and not line.startswith("---")
            ]

            hunks = re.findall(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff_out)
            for h_start, h_count, _, _ in hunks:
                start_line = int(h_start)
                count = int(h_count) if h_count else 1
                if count == 0:
                    continue

                rc, blame_out, _ = run_cmd(
                    ["git", "blame", "-l", f"-L{start_line},{start_line+count-1}", f"{commit}~1", "--", f]
                )
                if rc != 0:
                    continue

                line_commits = [line.split()[0] for line in blame_out.splitlines() if line]
                for c in set(line_commits):
                    rc_anc, _, _ = run_cmd(["git", "merge-base", "--is-ancestor", c, ref])
                    if rc_anc != 0:
                        # c is not in target_branch ancestry. Was it introduced after merge_base?
                        rc_after, _, _ = run_cmd(["git", "merge-base", "--is-ancestor", merge_base, c])
                        if rc_after == 0 and c != merge_base:
                            # Check if commit c was previously backported to target branch
                            was_bp, bp_detail = was_commit_previously_backported(c, ref)
                            if was_bp:
                                logger.info(f"  Target '{target_branch}': {bp_detail}")
                                continue

                            # Check if the modified lines exist in target branch file
                            if do_modified_lines_exist_in_target(ref, f, deleted_lines):
                                continue

                            return (
                                False,
                                f"Modified code in '{f}' (lines {start_line}-{start_line+count-1}) was introduced in commit {c[:9]}, "
                                f"which post-dates the '{target_branch}' branch point, was not previously backported, and does not exist in '{target_branch}'.",
                            )

    return True, f"Codebase and modified lines verified present in '{target_branch}'."


def fetch_pr_info(pr_number: int) -> Dict[str, Any]:
    """Fetches PR metadata using GitHub CLI."""
    try:
        rc, stdout, stderr = run_cmd(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "number,title,labels,headRefName,mergeCommit,mergedAt,baseRefName",
            ]
        )
    except Exception as e:
        raise GHCommandError(f"Could not execute 'gh' CLI. Is GitHub CLI installed? Details: {e}")

    if rc != 0:
        if "Could not resolve to a PullRequest" in stderr or "not found" in stderr.lower():
            raise PRNotFoundError(f"Pull Request #{pr_number} could not be found on GitHub.")
        elif "auth login" in stderr.lower() or "gh_token" in stderr.lower() or "authentication" in stderr.lower():
            raise GHAuthError("GitHub CLI ('gh') is not authenticated. Please run 'gh auth login' or export GH_TOKEN.")
        else:
            clean_msg = stderr.replace("GraphQL: ", "").strip()
            raise GHCommandError(f"Failed to fetch PR #{pr_number}: {clean_msg}")

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise GHCommandError(f"Failed to parse JSON response from GitHub CLI: {e}")


def add_pr_label(pr_number: int, label: str, dry_run: bool = False) -> bool:
    """Adds a label to the PR."""
    if dry_run:
        logger.info(f"[DRY-RUN] Adding label '{label}' to PR #{pr_number}")
        return True
    rc, _, stderr = run_cmd(["gh", "pr", "edit", str(pr_number), "--add-label", label])
    if rc != 0:
        logger.error(f"Error adding label '{label}' to PR #{pr_number}: {stderr}")
        return False
    logger.info(f"Successfully added label '{label}' to PR #{pr_number}")
    return True


def post_pr_comment(pr_number: int, comment_body: str, dry_run: bool = False) -> bool:
    """Posts a comment on the PR."""
    if dry_run:
        logger.info(f"[DRY-RUN] Posting comment on PR #{pr_number}:\n{comment_body}")
        return True
    rc, _, stderr = run_cmd(["gh", "pr", "comment", str(pr_number), "--body", comment_body])
    if rc != 0:
        logger.error(f"Error posting comment to PR #{pr_number}: {stderr}")
        return False
    logger.info(f"Successfully posted comment on PR #{pr_number}")
    return True

