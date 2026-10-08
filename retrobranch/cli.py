import argparse
import logging
import os
import re
import sys
import yaml

from . import engine

logger = logging.getLogger("retrobranch")


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

    raw_pr = args.pr or args.pr_number
    if not raw_pr:
        parser.error("PR number or URL is required. Example: 'retrobr 1234' or 'retrobr --pr 1234'")

    engine.do(args, raw_pr)


if __name__ == "__main__":
    main()

