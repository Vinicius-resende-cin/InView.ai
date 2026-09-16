"""Parser for unified diffs (diff.txt), built on top of `unidiff`."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel
from unidiff import PatchSet

ChangeType = Literal["added", "removed", "context"]

_JAVA_SRC_RE = re.compile(r"(?:^|/)(?:src/(?:main|test)/java|java)/(?P<rest>.+)\.java$")


class DiffLine(BaseModel):
    """One added, removed, or context line from a diff hunk."""

    line_no: int
    content: str
    change_type: ChangeType


class DiffFile(BaseModel):
    """One file's changes in a diff, with its inferred Java class name."""

    path: str
    class_name: Optional[str] = None
    lines: list[DiffLine]


def _strip_prefix(path: str) -> str:
    """Strip git's a/ or b/ prefix from a diff file path."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def infer_class_name(file_path: str) -> Optional[str]:
    """Best-effort mapping from a Java source path to its fully-qualified class name."""
    match = _JAVA_SRC_RE.search(file_path.replace("\\", "/"))
    if not match:
        return None
    return match.group("rest").replace("/", ".")


def parse_diff(text: str) -> list[DiffFile]:
    """Parse unified diff text into one DiffFile per changed file."""
    patch_set = PatchSet(text)
    files: list[DiffFile] = []

    for patched_file in patch_set:
        path = _strip_prefix(patched_file.target_file or patched_file.source_file)
        lines: list[DiffLine] = []

        for hunk in patched_file:
            for line in hunk:
                if not (line.is_added or line.is_removed or line.is_context):
                    # e.g. the "\ No newline at end of file" marker line.
                    continue
                content = line.value.rstrip("\n")
                if line.is_added:
                    lines.append(
                        DiffLine(
                            line_no=line.target_line_no,
                            content=content,
                            change_type="added",
                        )
                    )
                elif line.is_removed:
                    lines.append(
                        DiffLine(
                            line_no=line.source_line_no,
                            content=content,
                            change_type="removed",
                        )
                    )
                else:
                    lines.append(
                        DiffLine(
                            line_no=line.target_line_no,
                            content=content,
                            change_type="context",
                        )
                    )

        files.append(
            DiffFile(path=path, class_name=infer_class_name(path), lines=lines)
        )

    return files


def load_diff(path: str | Path) -> list[DiffFile]:
    """Read and parse a unified diff file."""
    return parse_diff(Path(path).read_text(encoding="utf-8"))
