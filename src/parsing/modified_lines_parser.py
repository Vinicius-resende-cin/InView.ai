"""Parser for the `modified-lines.txt` per-line authorship format.

Expected input, repeated in blocks separated by blank lines::

    Class: org.example.Main
    Left added lines: [7]
    Left deleted lines: []
    Right added lines: [9]
    Right deleted lines: []
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

_CLASS_RE = re.compile(r"^Class:\s*(?P<name>.+?)\s*$")
_LINES_RE = re.compile(
    r"^(?P<who>Left|Right)\s+(?P<kind>added|deleted)\s+lines:\s*\[(?P<values>[^\]]*)\]\s*$"
)


class ClassAuthorship(BaseModel):
    left_added: list[int] = []
    left_deleted: list[int] = []
    right_added: list[int] = []
    right_deleted: list[int] = []

    def author_of(self, line_no: int) -> str | None:
        """Return "Left", "Right", or None for the given (added-line) number."""
        if line_no in self.left_added:
            return "Left"
        if line_no in self.right_added:
            return "Right"
        return None


def _parse_int_list(values: str) -> list[int]:
    values = values.strip()
    if not values:
        return []
    return [int(v.strip()) for v in values.split(",")]


def parse_modified_lines(text: str) -> dict[str, ClassAuthorship]:
    """Parse the modified-lines.txt content into {class_name: ClassAuthorship}."""
    result: dict[str, ClassAuthorship] = {}
    current_class: str | None = None
    pending: dict[str, list[int]] = {}

    def flush() -> None:
        nonlocal current_class, pending
        if current_class is not None:
            result[current_class] = ClassAuthorship(
                left_added=pending.get("left_added", []),
                left_deleted=pending.get("left_deleted", []),
                right_added=pending.get("right_added", []),
                right_deleted=pending.get("right_deleted", []),
            )
        current_class = None
        pending = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        class_match = _CLASS_RE.match(line)
        if class_match:
            flush()
            current_class = class_match.group("name")
            continue

        lines_match = _LINES_RE.match(line)
        if lines_match and current_class is not None:
            key = f"{lines_match.group('who').lower()}_{lines_match.group('kind')}"
            pending[key] = _parse_int_list(lines_match.group("values"))
            continue

    flush()
    return result


def load_modified_lines(path: str | Path) -> dict[str, ClassAuthorship]:
    return parse_modified_lines(Path(path).read_text(encoding="utf-8"))
