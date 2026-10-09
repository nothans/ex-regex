"""Every error ex-regex raises is an ExRegexError."""

from __future__ import annotations

from typing import Any, Optional


class ExRegexError(Exception):
    """Base class."""


class ConfigError(ExRegexError):
    """No backend could be set up (usually a missing API key)."""


class LimitError(ExRegexError, ValueError):
    """A question or request breaks a documented limit; caught before any request is sent."""


class BudgetExceeded(ExRegexError):
    """The next request would take spending past the engine's max_cost_usd."""


class BackendError(ExRegexError):
    """The backend failed, or answered with something that is not a valid answer."""

    def __init__(self, message: str, *, status: Optional[int] = None, body: Any = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable


class CacheMiss(ExRegexError):
    """Replay mode found no recorded decision for a request; nothing was sent."""


class RefusedError(ExRegexError):
    """A backend refused a question that ex-regex needed answered."""
