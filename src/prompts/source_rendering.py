"""Prompt cue pointing the agent at the checked-out source files."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def render_source_root(source_root: Optional[Path]) -> str:
    """Returns a paragraph telling the agent where the full source lives, or
    an empty string when no source_root is configured."""
    if source_root is None:
        return ""
    return (
        "The full source files of this codebase are checked out at:\n"
        f"{source_root.resolve()}\n"
        "The diff above only shows hunks. Before reporting a dependency, "
        "verify it by opening the relevant files in that directory (use "
        "absolute paths) to check declarations, other methods, superclasses, "
        "and callers that the hunks omit."
    )
