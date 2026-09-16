"""Joins parsed diff files with per-class authorship into one annotated structure."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from src.parsing.diff_parser import DiffFile
from src.parsing.modified_lines_parser import ClassAuthorship

Author = Literal["Left", "Right"]


class AnnotatedLine(BaseModel):
    """A diff line tagged with which developer (if any) introduced it."""

    line_no: int
    content: str
    change_type: Literal["added", "removed", "context"]
    author: Optional[Author] = None


class AnnotatedFile(BaseModel):
    """One file's diff, with every changed line tagged by author."""

    path: str
    class_name: Optional[str] = None
    lines: list[AnnotatedLine]


def _author_for_line(
    authorship: Optional[ClassAuthorship], line_no: int, change_type: str
) -> Optional[Author]:
    """Look up which developer authored `line_no`, if any."""
    if authorship is None:
        return None
    if change_type == "added":
        if line_no in authorship.left_added:
            return "Left"
        if line_no in authorship.right_added:
            return "Right"
    elif change_type == "removed":
        if line_no in authorship.left_deleted:
            return "Left"
        if line_no in authorship.right_deleted:
            return "Right"
    return None


def annotate_diff_files(
    diff_files: list[DiffFile], authorship_by_class: dict[str, ClassAuthorship]
) -> list[AnnotatedFile]:
    """Tag each diff line with its author, using per-class authorship data."""
    annotated: list[AnnotatedFile] = []

    for diff_file in diff_files:
        authorship = (
            authorship_by_class.get(diff_file.class_name)
            if diff_file.class_name
            else None
        )
        annotated_lines = [
            AnnotatedLine(
                line_no=line.line_no,
                content=line.content,
                change_type=line.change_type,
                author=_author_for_line(authorship, line.line_no, line.change_type),
            )
            for line in diff_file.lines
        ]
        annotated.append(
            AnnotatedFile(
                path=diff_file.path,
                class_name=diff_file.class_name,
                lines=annotated_lines,
            )
        )

    return annotated
