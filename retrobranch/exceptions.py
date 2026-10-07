"""
Custom exception classes for Retrobranch.
Provides structured error handling for programmatic (non-CLI) and CLI usage.
"""


class RetrobranchError(Exception):
    """Base exception for all Retrobranch errors."""

    pass


class PRNotFoundError(RetrobranchError):
    """Raised when a specified PR cannot be found on GitHub."""

    pass


class GHAuthError(RetrobranchError):
    """Raised when GitHub CLI authentication is missing or invalid."""

    pass


class GHCommandError(RetrobranchError):
    """Raised when GitHub CLI execution fails or is missing."""

    pass


class GitCommandError(RetrobranchError):
    """Raised when underlying git commands fail unexpectedly."""

    pass


class BranchConfigError(RetrobranchError):
    """Raised when branch configuration files are invalid or unparseable."""

    pass
