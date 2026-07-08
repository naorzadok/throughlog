"""Directory allowlist — only path-bearing events under an allowed root pass.

Only directories belonging to a tracked project are observable. Non-path events
(focus, narration, clipboard, idle) are not path-filtered here; they are handled
by the redactors.
"""

from __future__ import annotations

import os
from pathlib import Path


class Allowlist:
    def __init__(self, roots: list[Path] | list[str]):
        self._roots: list[str] = [self._norm(r) for r in roots]

    @staticmethod
    def _norm(p: Path | str) -> str:
        # realpath resolves symlinks/junctions on BOTH the roots and the candidate
        # path, so a link whose name sits under an allowed root but resolves OUTSIDE
        # it cannot bypass the boundary check below (risk register #6). normcase
        # handles Windows case-insensitivity; realpath also makes the path absolute.
        return os.path.normcase(os.path.realpath(str(p)))

    @property
    def roots(self) -> list[str]:
        return list(self._roots)

    def allows(self, path: str | None) -> bool:
        """True if `path` is one of, or nested under, an allowed root."""
        if not path:
            return False
        np = self._norm(path)
        for r in self._roots:
            if np == r or np.startswith(r + os.sep):
                return True
        return False

    def __bool__(self) -> bool:
        return bool(self._roots)
