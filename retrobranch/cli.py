import argparse
import logging
import os
import re
import sys
import yaml

from . import engine

logger = logging.getLogger("retrobranch")


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


def _gen_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retrobranch: Pre-flight decision engine for backport eligibility and triggering."
    )
    parser.add_argument(
        "pr",
        nargs="?",
        type=str,
        default=None,
        help="PR number or GitHub PR URL to evaluate (e.g. '1234' or 'https://github.com/owner/repo/pull/1234')",
    )
    parser.add_argument(
        "--pr-number",
        "--pr",
        "-p",
        type=str,
        default=None,
        help="PR number or GitHub PR URL to evaluate",
    )
    parser.add_argument("--commit", type=str, default=None, help="PR merge commit SHA (auto-detected if omitted)")
    parser.add_argument(
        "--base-branch",
        type=str,
        default=None,
        help="Target base branch PR was merged into (default: resolved from config file, auto-detected, or 'main')",
    )
    parser.add_argument("--mergify-config", type=str, default=None, help="Path to mergify.yml config (legacy option)")
    parser.add_argument(
        "--config-file",
        type=str,
        default=None,
        help="Path to branch configuration file (supports Mergify YAML, generic YAML/JSON, or text file)",
    )
    parser.add_argument(
        "--source-type",
        type=str,
        default="auto",
        choices=["auto", "mergify", "yaml", "json", "text"],
        help="Format type for branch config file (default: auto)",
    )
    parser.add_argument(
        "--target-branches",
        type=str,
        default=None,
        help="Optional comma-separated list of target branches to evaluate (e.g. 'release-2.0,release-1.0')",
    )
    parser.add_argument(
        "--filter-branches",
        type=str,
        default=None,
        help="Optional comma-separated list of branch names to filter against (e.g. 'release-2.0,release-1.0')",
    )
    parser.add_argument(
        "--label-template",
        "--label-pattern",
        type=str,
        default=None,
        help="Template pattern for backport labels (default: 'backport-{branch}'). Supports '{branch}', '{target}', or '%%s'.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode without modifying labels or commenting")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print detailed debug tracebacks on error")
    return parser


def main():
    parser = _gen_argparser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(message)s")

    raw_pr = args.pr or args.pr_number
    if not raw_pr:
        parser.error("PR number or URL is required. Example: 'retrobr 1234' or 'retrobr --pr 1234'")

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
        for default_path in [
            ".github/retrobranch.yml",
            ".retrobranch.yml",
            ".github/mergify.yml",
            ".github/maintained_branches.yml",
            ".github/branches.txt",
            "maintained_branches.yml",
            "branches.txt",
        ]:
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
        pr_data = engine.fetch_pr_info(pr_number, repo=repo)
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
            detected_branch = engine.detect_repo_default_branch(repo=repo)
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
        is_feat, feat_reason = engine.is_feature_pr(title, labels, head_branch)
        if is_feat:
            logger.info(f"PR #{pr_number} is classified as a feature/capability: {feat_reason}. No backports added.")
            sys.exit(0)

        logger.info(f"PR #{pr_number} is NOT a feature ({feat_reason}). Evaluating target branches...")

        # 3. Discover target maintained branches
        explicit_branches = None
        if args.target_branches:
            explicit_branches = [b.strip() for b in args.target_branches.split(",") if b.strip()]

        target_branches = engine.get_maintained_branches(
            mergify_path=mergify_config,
            config_path=config_file,
            source_type=args.source_type,
            branch_filter=args.filter_branches,
            explicit_branches=explicit_branches,
        )
        if not target_branches and not mergify_config and os.path.exists(".github/mergify.yml") and config_file != ".github/mergify.yml":
            target_branches = engine.get_maintained_branches(
                mergify_path=".github/mergify.yml",
                branch_filter=args.filter_branches,
                explicit_branches=explicit_branches,
            )
        if not target_branches:
            logger.info("No maintained branches discovered.")
            sys.exit(0)

        logger.info(f"Discovered target branches: {target_branches}")

        # Fetch latest branches on origin to ensure accurate git ancestry
        engine.run_subproc(["git", "fetch", "origin", "--depth=200"] + target_branches)

        # 4. Verify presence in each target branch
        branch_results = {}
        labels_to_add = []
        skipped_branches = []

        for b in target_branches:
            label_name = engine.format_label(label_template, b)
            if label_name in labels:
                logger.info(f"PR #{pr_number} already has label '{label_name}' for branch '{b}'. Skipping check.")
                branch_results[b] = (True, "Label already present on PR", True, label_name)
                continue

            is_present, reason = engine.verify_issue_presence_in_branch(merge_commit, b)
            branch_results[b] = (is_present, reason, False, label_name)

            if is_present:
                labels_to_add.append((b, label_name))
            else:
                skipped_branches.append((b, label_name, reason))

        # 5. Apply labels for branches where issue is present
        for b, label_name in labels_to_add:
            engine.add_pr_label(pr_number, label_name, repo=repo, dry_run=args.dry_run)

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
            engine.post_pr_comment(pr_number, comment_body, repo=repo, dry_run=args.dry_run)
    except Exception as e:
        if getattr(args, "verbose", False):
            raise e
        logger.error(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

