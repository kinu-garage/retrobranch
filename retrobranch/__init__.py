"""
Retrobranch: Pre-flight decision engine for backport eligibility and triggering.
"""

__version__ = "0.1.0"

from .engine import (
    add_pr_label,
    do_modified_lines_exist_in_target,
    fetch_pr_info,
    format_label,
    get_maintained_branches,
    is_feature_pr,
    post_pr_comment,
    run_cmd,
    verify_issue_presence_in_branch,
    was_commit_previously_backported,
)
from .exceptions import (
    BranchConfigError,
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

__all__ = [
    "run_cmd",
    "get_maintained_branches",
    "is_feature_pr",
    "was_commit_previously_backported",
    "do_modified_lines_exist_in_target",
    "verify_issue_presence_in_branch",
    "fetch_pr_info",
    "format_label",
    "add_pr_label",
    "post_pr_comment",
    "BaseBranchSource",
    "ExplicitBranchSource",
    "MergifyBranchSource",
    "GenericFileBranchSource",
    "CallableBranchSource",
    "CompositeBranchSource",
    "RetrobranchError",
    "PRNotFoundError",
    "GHAuthError",
    "GHCommandError",
    "GitCommandError",
    "BranchConfigError",
]
