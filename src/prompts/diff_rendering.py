"""Renders an annotated diff into plain text suitable for an LLM prompt."""

from __future__ import annotations

from src.parsing.annotate import AnnotatedFile

_MARKER = {"added": "+", "removed": "-", "context": " "}


def render_annotated_diff(files: list[AnnotatedFile]) -> str:
    blocks: list[str] = []

    for file in files:
        header = f"File: {file.path}"
        if file.class_name:
            header += f" (class: {file.class_name})"
        lines = [header]

        for line in file.lines:
            marker = _MARKER[line.change_type]
            tag = f" [{line.author}]" if line.author else ""
            lines.append(f"{marker}{line.line_no:>5}{tag}: {line.content}")

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
