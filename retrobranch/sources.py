"""
Maintained branch sources for Retrobranch.
Provides a generic abstraction for discovering target backport branches from various sources.
"""

from abc import ABC, abstractmethod
import logging
import os
from typing import Any, Callable, List, Optional, Union
import yaml

logger = logging.getLogger(__name__)


class BaseBranchSource(ABC):
    """Abstract base class for maintained branch sources."""

    @abstractmethod
    def get_branches(self) -> List[str]:
        """Returns a list of branch names discovered by this source."""
        pass


class ExplicitBranchSource(BaseBranchSource):
    """Source for explicitly provided branch names (list or comma-separated string)."""

    def __init__(self, branches: Union[List[str], str]):
        if isinstance(branches, str):
            self.branches = [b.strip() for b in branches.split(",") if b.strip()]
        elif isinstance(branches, (list, tuple)):
            self.branches = [str(b).strip() for b in branches if b and str(b).strip()]
        else:
            self.branches = []

    def get_branches(self) -> List[str]:
        return list(self.branches)


def parse_mergify_branches_from_data(mergify_yaml: Any) -> List[str]:
    """Extracts target backport branches from parsed Mergify YAML data."""
    branches = []
    if isinstance(mergify_yaml, dict):
        for rule in mergify_yaml.get("pull_request_rules", []):
            if isinstance(rule, dict):
                backport = rule.get("actions", {}).get("backport", {})
                if isinstance(backport, dict):
                    for b in backport.get("branches", []):
                        if b and b not in branches:
                            branches.append(str(b).strip())
    return branches


def parse_branches_from_content(content: str, format_type: str = "auto") -> List[str]:
    """
    Parses maintained branches from raw string content (Mergify YAML, generic YAML/JSON, or line-separated text).
    """
    if not content or not content.strip():
        return []

    format_type = (format_type or "auto").lower()

    if format_type == "mergify":
        try:
            mergify_yaml = yaml.safe_load(content) or {}
            return parse_mergify_branches_from_data(mergify_yaml)
        except Exception as e:
            logger.warning(f"Failed to parse Mergify YAML content: {e}")
            return []

    if format_type in ("auto", "yaml", "yml", "json"):
        try:
            data = yaml.safe_load(content)

            # Check if it's Mergify configuration structure
            if isinstance(data, dict) and "pull_request_rules" in data:
                return parse_mergify_branches_from_data(data)

            # Top-level list: ["release-1.0", "release-2.0"]
            if isinstance(data, list):
                return [str(item).strip() for item in data if item and str(item).strip()]

            # Top-level dict: search for known branch keys
            if isinstance(data, dict):
                possible_keys = [
                    "maintained_branches",
                    "target_branches",
                    "branches",
                    "backport_branches",
                    "maintained-branches",
                    "target-branches",
                    "backport-branches",
                ]
                for key in possible_keys:
                    if key in data:
                        val = data[key]
                        if isinstance(val, list):
                            return [str(item).strip() for item in val if item and str(item).strip()]
                        elif isinstance(val, str):
                            return [b.strip() for b in val.split(",") if b.strip()]
        except Exception as e:
            if format_type in ("yaml", "yml", "json"):
                logger.warning(f"Failed to parse content as {format_type}: {e}")
                return []

    # Plain text file parsing (fallback or format_type in ("text", "txt", "auto"))
    if format_type in ("auto", "text", "txt"):
        lines = content.splitlines()
        branches = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = [p.strip() for p in line.split(",") if p.strip()]
                for p in parts:
                    if p not in branches:
                        branches.append(p)
        return branches

    return []


class MergifyBranchSource(BaseBranchSource):
    """Source for extracting target backport branches from mergify.yml configuration."""

    def __init__(self, config_path: str):
        self.config_path = config_path

    def get_branches(self) -> List[str]:
        if not self.config_path or not os.path.exists(self.config_path):
            if self.config_path:
                logger.warning(f"Mergify config file '{self.config_path}' not found.")
            return []

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                content = f.read()
            return parse_branches_from_content(content, "mergify")
        except Exception as e:
            logger.warning(f"Failed to read Mergify config '{self.config_path}': {e}")
            return []


class GenericFileBranchSource(BaseBranchSource):
    """
    Source for parsing target branches from generic configuration files.
    Supports:
    - Mergify YAML config
    - Standard YAML / JSON files (top-level list or dict with branch keys like 'maintained_branches', 'branches')
    - Plain text files (line-separated branch names, ignoring '#' comments)
    """

    def __init__(self, file_path: str, format_type: str = "auto"):
        self.file_path = file_path
        self.format_type = (format_type or "auto").lower()

    def get_branches(self) -> List[str]:
        if not self.file_path or not os.path.exists(self.file_path):
            if self.file_path:
                logger.warning(f"Branch config file '{self.file_path}' not found.")
            return []

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read()
            return parse_branches_from_content(content, self.format_type)
        except Exception as e:
            logger.warning(f"Failed to read branch config file '{self.file_path}': {e}")
            return []


class RawContentBranchSource(BaseBranchSource):
    """
    Source for parsing target branches directly from raw in-memory content.
    Used when configuration is fetched remotely via API or generated dynamically.
    """

    def __init__(self, content: str, format_type: str = "auto"):
        self.content = content
        self.format_type = (format_type or "auto").lower()

    def get_branches(self) -> List[str]:
        return parse_branches_from_content(self.content, self.format_type)


class CallableBranchSource(BaseBranchSource):
    """Source wrapping a Python callable function that returns a list of branch names."""

    def __init__(self, func: Callable[[], List[str]]):
        self.func = func

    def get_branches(self) -> List[str]:
        try:
            res = self.func()
            if isinstance(res, (list, tuple)):
                return [str(b).strip() for b in res if b and str(b).strip()]
        except Exception as e:
            logger.warning(f"Error calling custom branch source: {e}")
        return []


class CompositeBranchSource(BaseBranchSource):
    """Combines branch names from multiple sources, preserving discovery order and removing duplicates."""

    def __init__(self, sources: List[BaseBranchSource]):
        self.sources = sources

    def get_branches(self) -> List[str]:
        discovered = []
        for source in self.sources:
            if isinstance(source, BaseBranchSource):
                branch_list = source.get_branches()
            elif callable(source):
                branch_list = CallableBranchSource(source).get_branches()
            else:
                continue

            for b in branch_list:
                if b and b not in discovered:
                    discovered.append(b)
        return discovered
