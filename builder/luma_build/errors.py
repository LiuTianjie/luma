from __future__ import annotations


class BuildError(Exception):
    """Expected validation, auth, or runtime error in luma-build."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class BuildCanceled(Exception):
    """Raised on a runner when the active build is canceled by the server."""
