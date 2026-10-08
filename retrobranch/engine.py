"""
Core qualification, ancestry verification, and labeling logic for Retrobranch.
"""

from __future__ import annotations

import argparse
from html import parser
import json
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
import yaml

logger = logging.getLogger(__name__)

from . import exceptions
from . import sources as branch_sources


DEFAULT_PATH_PATTERN_TARGET_BRANCHES = [
    ".github/retrobranch.yml",
    ".retrobranch.yml",
    ".github/mergify.yml",
    ".github/maintained_branches.yml",
    ".github/branches.txt",
    "maintained_branches.yml",
    "branches.txt",
]

PATH_PATTERN_TARGET_BRANCHES = list(DEFAULT_PATH_PATTERN_TARGET_BRANCHES)


def modify_path_pattern_target_branches(
    patterns: Union[str, Sequence[str]],
    append: bool = False,
    overwrite: Optional[bool] = None,
) -> List[str]:
    """
    Modifies the global PATH_PATTERN_TARGET_BRANCHES list.

    Args:
        patterns: A single path pattern string or a sequence (list, tuple) of path patterns.
        append: If True, appends the pattern(s) to the existing list.
                If False (default), overwrites the list.
        overwrite: Optional boolean. If provided, overrides append (overwrite=True means append=False,
                   and overwrite=False means append=True).

    Returns:
        The updated PATH_PATTERN_TARGET_BRANCHES list.

    Examples:
        # Overwrite with a list of patterns:
        modify_path_pattern_target_branches(["custom.yml", "configs/branches.txt"])

        # Append a list of patterns to the existing list:
        modify_path_pattern_target_branches(["more.yml"], append=True)

        # Add a single entry (appends to existing list):
        modify_path_pattern_target_branches("single.yml", append=True)
    """
    global PATH_PATTERN_TARGET_BRANCHES

    if overwrite is not None:
        append = not overwrite

    if isinstance(patterns, str):
        new_items = [patterns.strip()]
    elif isinstance(patterns, (list, tuple, set)):
        new_items = [str(p).strip() for p in patterns if str(p).strip()]
    else:
        raise TypeError(f"patterns must be a str or a sequence of str, got {type(patterns).__name__}")

    if append:
        for item in new_items:
            if item not in PATH_PATTERN_TARGET_BRANCHES:
                PATH_PATTERN_TARGET_BRANCHES.append(item)
    else:
        PATH_PATTERN_TARGET_BRANCHES = list(new_items)

    return PATH_PATTERN_TARGET_BRANCHES


def set_path_pattern_target_branches(
    patterns: Union[str, Sequence[str]],
    append: bool = False,
) -> List[str]:
    """
    Sets or updates PATH_PATTERN_TARGET_BRANCHES (overwrites by default).

    Args:
        patterns: A single path pattern or a sequence of path patterns.
        append: If True, appends instead of overwriting. Default is False.

    Returns:
        The updated PATH_PATTERN_TARGET_BRANCHES list.
    """
    return modify_path_pattern_target_branches(patterns, append=append)


def add_path_pattern_target_branches(
    patterns: Union[str, Sequence[str]],
    overwrite: bool = False,
) -> List[str]:
    """
    Adds path pattern(s) to PATH_PATTERN_TARGET_BRANCHES (appends by default).

    Args:
        patterns: A single path pattern or a sequence of path patterns to append.
        overwrite: If True, overwrites the existing list instead. Default is False.

    Returns:
        The updated PATH_PATTERN_TARGET_BRANCHES list.
    """
    return modify_path_pattern_target_branches(patterns, append=not overwrite)


def add_path_pattern_target_branch(
    pattern: str,
    append: bool = True,
) -> List[str]:
    """
    Adds a single path pattern entry to PATH_PATTERN_TARGET_BRANCHES.

    Args:
        pattern: A single path pattern string to add.
        append: If True (default), appends to the list; if False, replaces the list.

    Returns:
        The updated PATH_PATTERN_TARGET_BRANCHES list.
    """
    return modify_path_pattern_target_branches(pattern, append=append)


def reset_path_pattern_target_branches() -> List[str]:
    """
    Resets PATH_PATTERN_TARGET_BRANCHES to its default configuration.

    Returns:
        The reset PATH_PATTERN_TARGET_BRANCHES list.
    """
    global PATH_PATTERN_TARGET_BRANCHES
    PATH_PATTERN_TARGET_BRANCHES = list(DEFAULT_PATH_PATTERN_TARGET_BRANCHES)
    return PATH_PATTERN_TARGET_BRANCHES


def get_path_pattern_target_branches() -> List[str]:
    """
    Returns a copy of the current PATH_PATTERN_TARGET_BRANCHES list.
    """
    return list(PATH_PATTERN_TARGET_BRANCHES)


def run_subproc(cmd, cwd=None, check=False):
    """Executes a subprocess command and returns (returncode, stdout, stderr)."""
    res = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and res.returncode != 0:
        raise exceptions.GitCommandError(f"Command failed ({res.returncode}): {' '.join(cmd)}\n{res.stderr}")
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
        source_list.append(branch_sources.ExplicitBranchSource(explicit_branches))

    if sources:
        for s in sources:
            if isinstance(s, branch_sources.BaseBranchSource):
                source_list.append(s)
            elif callable(s):
                source_list.append(branch_sources.CallableBranchSource(s))
            elif isinstance(s, (list, tuple)):
                source_list.append(branch_sources.ExplicitBranchSource(s))
            elif isinstance(s, str):
                source_list.append(branch_sources.GenericFileBranchSource(s, source_type))

    if config_path:
        source_list.append(branch_sources.GenericFileBranchSource(config_path, source_type))

    if mergify_path:
        source_list.append(branch_sources.MergifyBranchSource(mergify_path))

    discovered_branches = branch_sources.CompositeBranchSource(source_list).get_branches()

    # Filter invalid branches e.g. non-existent on the remote origin
    rc, stdout, _ = run_subproc(["git", "ls-remote", "--heads", "origin"])
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
    rc, stdout, _ = run_subproc(["git", "log", target_ref, f"--grep={commit_sha}", "-n", "1", "--oneline"])
    if rc == 0 and stdout:
        return True, f"Commit {commit_sha[:9]} was previously backported in commit: {stdout}"

    # 2. Search git log on target branch for backport PR title referencing the original PR number
    rc, commit_msg, _ = run_subproc(["git", "log", "-1", "--format=%s%n%b", commit_sha])
    if rc == 0 and commit_msg:
        pr_matches = re.findall(r"#(\d+)", commit_msg)
        for pr_num in pr_matches:
            rc, stdout, _ = run_subproc(
                ["git", "log", target_ref, f"--grep=backport.*#{pr_num}", "-n", "1", "--oneline"]
            )
            if rc == 0 and stdout:
                return True, f"Original PR #{pr_num} was previously backported in commit: {stdout}"

    return False, "No previous backport found in target branch log"


def do_modified_lines_exist_in_target(target_ref: str, file_path: str, deleted_lines: List[str]) -> bool:
    """
    Checks if non-trivial lines being modified/deleted by the PR exist in target_ref:file_path.
    """
    rc, content, _ = run_subproc(["git", "show", f"{target_ref}:{file_path}"])
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
    rc, _, _ = run_subproc(["git", "rev-parse", "--verify", ref])
    if rc != 0:
        return False, f"Target branch '{ref}' does not exist."

    # Check modified/deleted files in commit
    rc, stdout, stderr = run_subproc(
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
            rc, _, _ = run_subproc(["git", "cat-file", "-e", f"{ref}:{dir_name}"])
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
        rc, _, _ = run_subproc(["git", "cat-file", "-e", f"{ref}:{f}"])
        if rc != 0:
            missing_files.append(f)

    if missing_files:
        return False, f"Modified file(s) do not exist in '{target_branch}': {', '.join(missing_files)}"

    # Check code ancestry on modified lines
    rc, merge_base, _ = run_subproc(["git", "merge-base", f"{commit}~1", ref])
    if rc == 0 and merge_base:
        for f in modified_files:
            rc, diff_out, _ = run_subproc(["git", "diff", "-U0", f"{commit}~1", commit, "--", f])
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

                rc, blame_out, _ = run_subproc(
                    ["git", "blame", "-l", f"-L{start_line},{start_line+count-1}", f"{commit}~1", "--", f]
                )
                if rc != 0:
                    continue

                line_commits = [line.split()[0] for line in blame_out.splitlines() if line]
                for c in set(line_commits):
                    rc_anc, _, _ = run_subproc(["git", "merge-base", "--is-ancestor", c, ref])
                    if rc_anc != 0:
                        # c is not in target_branch ancestry. Was it introduced after merge_base?
                        rc_after, _, _ = run_subproc(["git", "merge-base", "--is-ancestor", merge_base, c])
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


def fetch_pr_info(pr_number: Union[int, str], repo: Optional[str] = None) -> Dict[str, Any]:
    """Fetches PR metadata using GitHub CLI."""
    cmd = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--json",
        "number,title,labels,headRefName,mergeCommit,mergedAt,baseRefName,url",
    ]
    if repo:
        cmd.extend(["-R", repo])
    try:
        rc, stdout, stderr = run_subproc(cmd)
    except Exception as e:
        raise exceptions.GHCommandError(f"Could not execute 'gh' CLI. Is GitHub CLI installed? Details: {e}")

    if rc != 0:
        if "Could not resolve to a PullRequest" in stderr or "not found" in stderr.lower():
            raise exceptions.PRNotFoundError(f"Pull Request #{pr_number} could not be found on {repo}.")
        elif "auth login" in stderr.lower() or "gh_token" in stderr.lower() or "authentication" in stderr.lower():
            raise exceptions.GHAuthError("GitHub CLI ('gh') is not authenticated. Please run 'gh auth login' or export GH_TOKEN.")
        else:
            clean_msg = stderr.replace("GraphQL: ", "").strip()
            raise exceptions.GHCommandError(f"Failed to fetch PR #{pr_number}: {clean_msg}")

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise exceptions.GHCommandError(f"Failed to parse JSON response from GitHub CLI: {e}")


def detect_repo_default_branch(repo: Optional[str] = None) -> Optional[str]:
    """
    Attempts to programmatically detect the remote repository's default / main branch.

    Resolution strategy:
    1. Local Git symbolic ref (`refs/remotes/origin/HEAD` or `refs/remotes/upstream/HEAD`)
    2. Git remote symref query (`git ls-remote --symref <remote> HEAD`)
    3. GitHub CLI (`gh repo view [<repo>] --json defaultBranchRef ...`)

    Args:
        repo: Optional repository in 'owner/name' format (e.g. 'kinu-garage/retrobranch').

    Returns:
        The detected default branch name (e.g. 'main', 'master', 'rolling'), or None if undetected.
    """
    # 1. Local Git symbolic ref (fast, offline)
    for remote in ("origin", "upstream"):
        try:
            rc, stdout, _ = run_subproc(["git", "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"])
            if rc == 0 and stdout:
                branch = stdout.strip()
                prefix = f"{remote}/"
                if branch.startswith(prefix):
                    branch = branch[len(prefix):]
                if branch:
                    return branch
        except Exception:
            pass

    # 2. Git remote symref query (Git protocol)
    for remote in ("origin", "upstream"):
        try:
            rc, stdout, _ = run_subproc(["git", "ls-remote", "--symref", remote, "HEAD"])
            if rc == 0 and stdout:
                match = re.search(r"ref:\s*refs/heads/(\S+)\s+HEAD", stdout)
                if match:
                    return match.group(1).strip()
        except Exception:
            pass

    # 3. GitHub CLI / API (authoritative repository settings on GitHub)
    try:
        cmd = ["gh", "repo", "view"]
        if repo:
            cmd.append(repo)
        cmd.extend(["--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"])
        rc, stdout, _ = run_subproc(cmd)
        if rc == 0 and stdout:
            return stdout.strip()
    except Exception:
        pass

    return None


def format_label(template: Optional[str], branch: str) -> str:
    """
    Formats a branch name into a label using a template string.

    Supports placeholders:
    - '{branch}', '{target}', '{target_branch}'
    - '{}'
    - '%s'
    If no placeholder is present:
    - If template ends with a separator ('-', '_', '/', ':'), appends branch name.
    - Otherwise returns template formatted with branch if possible or template as-is.

    Args:
        template: The template string (e.g. 'backport-{branch}', 'backport-%s', 'bp/{branch}').
        branch: The target branch name (e.g. 'humble').

    Returns:
        str: Formatted label name (e.g. 'backport-humble').
    """
    if not template:
        template = "backport-{branch}"

    if "%s" in template:
        return template % branch
    if "{}" in template:
        return template.format(branch)
    if "{branch}" in template or "{target}" in template or "{target_branch}" in template:
        return template.format(branch=branch, target=branch, target_branch=branch)
    if template.endswith(("-", "_", "/", ":")):
        return f"{template}{branch}"
    return template


def add_pr_label(pr_number: int, label: str, repo: Optional[str] = None, dry_run: bool = False) -> bool:
    """
    Adds a label to the PR.
    Uses the GitHub REST API via 'gh api' to avoid GraphQL deprecation errors (e.g. Projects classic).
    Falls back to 'gh pr edit' if necessary.
    """
    if dry_run:
        logger.info(f"[DRY-RUN] Adding label '{label}' to PR #{pr_number}")
        return True

    # 1. Primary: Use GitHub REST API endpoint to avoid GraphQL query issues
    endpoint = f"repos/{repo}/issues/{pr_number}/labels" if repo else f"repos/{{owner}}/{{repo}}/issues/{pr_number}/labels"
    rc, stdout, stderr = run_subproc(["gh", "api", endpoint, "-f", f"labels[]={label}"])
    if rc == 0:
        logger.info(f"Successfully added label '{label}' to PR #{pr_number}")
        return True

    # If label does not exist in repository (HTTP 404), create it and retry
    if "Not Found" in stderr or "404" in stderr or "Resource not found" in stderr:
        create_endpoint = f"repos/{repo}/labels" if repo else "repos/{owner}/{repo}/labels"
        run_subproc(["gh", "api", create_endpoint, "-f", f"name={label}"])
        rc_retry, _, stderr_retry = run_subproc(["gh", "api", endpoint, "-f", f"labels[]={label}"])
        if rc_retry == 0:
            logger.info(f"Successfully created and added label '{label}' to PR #{pr_number}")
            return True

    # 2. Fallback to standard 'gh pr edit'
    edit_cmd = ["gh", "pr", "edit", str(pr_number), "--add-label", label]
    if repo:
        edit_cmd.extend(["-R", repo])
    rc_edit, _, stderr_edit = run_subproc(edit_cmd)
    if rc_edit == 0:
        logger.info(f"Successfully added label '{label}' to PR #{pr_number}")
        return True

    logger.error(f"Error adding label '{label}' to PR #{pr_number}: {stderr} (fallback error: {stderr_edit})")
    return False


def post_pr_comment(pr_number: int, comment_body: str, repo: Optional[str] = None, dry_run: bool = False) -> bool:
    """Posts a comment on the PR."""
    if dry_run:
        logger.info(f"[DRY-RUN] Posting comment on PR #{pr_number}:\n{comment_body}")
        return True

    cmd = ["gh", "pr", "comment", str(pr_number), "--body", comment_body]
    if repo:
        cmd.extend(["-R", repo])
    rc, _, stderr = run_subproc(cmd)
    if rc == 0:
        logger.info(f"Successfully posted comment on PR #{pr_number}")
        return True

    # Fallback to GitHub REST API via gh api
    endpoint = f"repos/{repo}/issues/{pr_number}/comments" if repo else f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments"
    rc_api, _, stderr_api = run_subproc(["gh", "api", endpoint, "-f", f"body={comment_body}"])
    if rc_api == 0:
        logger.info(f"Successfully posted comment on PR #{pr_number} (via REST API fallback)")
        return True

    logger.error(f"Error posting comment to PR #{pr_number}: {stderr} (fallback error: {stderr_api})")


def parse_pr_number(val: str) -> int:
    """Extracts PR number from integer, string ID, '#123', or full GitHub PR URL."""
    val_str = str(val).strip()
    match = re.search(r"/pull/(\d+)", val_str)
    if match:
        return int(match.group(1))
    if val_str.startswith("#"):
        val_str = val_str[1:]
    try:
        return int(val_str)
    except ValueError:
        raise ValueError(f"Invalid PR number or URL: '{val}'")


def do(args: argparse.ArgumentParser, raw_pr: str):

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(message)s")

    repo = None
    repo_match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", str(raw_pr))
    if repo_match:
        repo = f"{repo_match.group(1)}/{repo_match.group(2)}"

    try:
        pr_number = parse_pr_number(raw_pr)
    except ValueError as e:
        logger.error(f"Error: {e}")
        sys.exit(1)

    # Auto-detect config file if none specified explicitly
    config_file = args.config_file
    mergify_config = args.mergify_config
    if not config_file and not mergify_config and not args.target_branches:
        for default_path in PATH_PATTERN_TARGET_BRANCHES:
            if os.path.exists(default_path):
                config_file = default_path
                break

    # Load configuration from config file if available
    cfg_data = None
    if config_file and os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    cfg_data = loaded
        except Exception as e:
            logger.warning(f"Failed to read config file '{config_file}': {e}")

    # Resolve label template (CLI flag > config file > default "backport-{branch}")
    label_template = args.label_template
    if not label_template and cfg_data:
        for k in ("label_template", "label-template", "label_pattern", "label-pattern"):
            if k in cfg_data and isinstance(cfg_data[k], str):
                label_template = cfg_data[k]
                break
    if not label_template:
        label_template = "backport-{branch}"

    # Resolve base branch (CLI flag > config file > auto-detection > default "main")
    expected_base_branch = args.base_branch
    base_branch_source = None
    if expected_base_branch:
        base_branch_source = "CLI argument"
    elif cfg_data:
        for k in (
            "base_branch",
            "base-branch",
            "main_branch",
            "main-branch",
            "default_branch",
            "default-branch",
        ):
            if k in cfg_data and isinstance(cfg_data[k], str):
                expected_base_branch = cfg_data[k].strip()
                base_branch_source = f"config file '{config_file}'"
                break

    # Resolve mergify config override from config file if available
    if not mergify_config and cfg_data:
        for k in ("mergify_config", "mergify-config", "mergify_path", "mergify-path"):
            if k in cfg_data and isinstance(cfg_data[k], str):
                mergify_config = cfg_data[k].strip()
                break

    try:
        # 1. Fetch PR details
        pr_data = fetch_pr_info(pr_number, repo=repo)
        if not repo and pr_data.get("url"):
            url_match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_data["url"])
            if url_match:
                repo = f"{url_match.group(1)}/{url_match.group(2)}"
        title = pr_data.get("title", "")
        labels = [l["name"] for l in pr_data.get("labels", [])]
        head_branch = pr_data.get("headRefName", "")
        base_branch = pr_data.get("baseRefName", "")
        merge_commit = args.commit or (pr_data.get("mergeCommit") or {}).get("oid")

        if not pr_data.get("mergedAt"):
            logger.info(f"PR #{pr_number} is not merged. Skipping.")
            sys.exit(0)

        # Resolve expected base branch if not explicitly configured
        if not expected_base_branch:
            detected_branch = detect_repo_default_branch(repo=repo)
            if detected_branch:
                expected_base_branch = detected_branch
                base_branch_source = "auto-detected from repository"
            else:
                expected_base_branch = "main"
                base_branch_source = "default fallback"

        logger.info(f"Recognized main branch: '{expected_base_branch}' ({base_branch_source})")

        if base_branch != expected_base_branch:
            logger.info(f"PR #{pr_number} targeted '{base_branch}', not '{expected_base_branch}'. Skipping backport check.")
            sys.exit(0)

        if not merge_commit:
            logger.error(f"Error: Could not determine merge commit for PR #{pr_number}")
            sys.exit(1)

        logger.info(f"Evaluating PR #{pr_number}: '{title}' (Merge commit: {merge_commit[:9]})")

        # 2. Check if PR is a feature/capability
        is_feat, feat_reason = is_feature_pr(title, labels, head_branch)
        if is_feat:
            logger.info(f"PR #{pr_number} is classified as a feature/capability: {feat_reason}. No backports added.")
            sys.exit(0)

        logger.info(f"PR #{pr_number} is NOT a feature ({feat_reason}). Evaluating target branches...")

        # 3. Discover target maintained branches
        explicit_branches = None
        if args.target_branches:
            explicit_branches = [b.strip() for b in args.target_branches.split(",") if b.strip()]

        target_branches = get_maintained_branches(
            mergify_path=mergify_config,
            config_path=config_file,
            source_type=args.source_type,
            branch_filter=args.filter_branches,
            explicit_branches=explicit_branches,
        )
        if not target_branches and not mergify_config and os.path.exists(".github/mergify.yml") and config_file != ".github/mergify.yml":
            target_branches = get_maintained_branches(
                mergify_path=".github/mergify.yml",
                branch_filter=args.filter_branches,
                explicit_branches=explicit_branches,
            )
        if not target_branches:
            logger.info("No maintained branches discovered.")
            sys.exit(0)

        logger.info(f"Discovered target branches: {target_branches}")

        # Fetch latest branches on origin to ensure accurate git ancestry
        run_subproc(["git", "fetch", "origin", "--depth=200"] + target_branches)

        # 4. Verify presence in each target branch
        branch_results = {}
        labels_to_add = []
        skipped_branches = []

        for b in target_branches:
            label_name = format_label(label_template, b)
            if label_name in labels:
                logger.info(f"PR #{pr_number} already has label '{label_name}' for branch '{b}'. Skipping check.")
                branch_results[b] = (True, "Label already present on PR", True, label_name)
                continue

            is_present, reason = verify_issue_presence_in_branch(merge_commit, b)
            branch_results[b] = (is_present, reason, False, label_name)

            if is_present:
                labels_to_add.append((b, label_name))
            else:
                skipped_branches.append((b, label_name, reason))

        # 5. Apply labels for branches where issue is present
        for b, label_name in labels_to_add:
            add_pr_label(pr_number, label_name, repo=repo, dry_run=args.dry_run)

        # 6. If any branch was skipped due to issue not being present, post comment report
        if skipped_branches:
            table_rows = []
            for b, (is_present, reason, already_had, label_name) in branch_results.items():
                lbl = f"`{label_name}`"
                if already_had:
                    status = "ℹ️ Already present"
                    detail = "Label was already attached to the PR."
                elif is_present:
                    status = "✅ Label added"
                    detail = f"Verified present. Added {lbl} label."
                else:
                    status = "⏭️ Skipped"
                    detail = f"**Issue not present**: {reason}"
                table_rows.append(f"| `{b}` | {status} | {detail} |")

            comment_body = (
                f"### 🤖 Retrobranch Backport Verification Report\n\n"
                f"This PR was merged into `{expected_base_branch}` and evaluated for backporting to maintained branches:\n\n"
                f"| Target Branch | Status | Details |\n"
                f"| :--- | :--- | :--- |\n"
                + "\n".join(table_rows)
                + "\n\n"
                f"*Note: For skipped branches, the issue or code being addressed was not present. "
                f"If this fix is still desired on a skipped branch, maintainers can apply the backport label manually.*"
            )
            post_pr_comment(pr_number, comment_body, repo=repo, dry_run=args.dry_run)
    except Exception as e:
        if getattr(args, "verbose", False):
            raise e
        logger.error(f"Error: {e}")
        sys.exit(1)    