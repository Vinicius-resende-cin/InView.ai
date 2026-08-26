"""Renders precomputed (Mode 2) semantic dependencies into prompt text."""

from __future__ import annotations

from src.parsing.dependency_parser import DependencySet, LineRef


def _render_line_refs(refs: list[LineRef]) -> str:
    return ", ".join(f"{ref.class_name}:{ref.line}" for ref in refs)


def render_dependencies(dependencies: DependencySet) -> str:
    if not dependencies.dependencies:
        return "No semantic dependencies were reported by static analysis."

    blocks: list[str] = ["Dependencies already found by static analysis:"]
    for i, dep in enumerate(dependencies.dependencies, start=1):
        blocks.append(f"{i}. Type: {dep.type}")
        blocks.append(f"   Left lines: {_render_line_refs(dep.left_lines)}")
        blocks.append(f"   Right lines: {_render_line_refs(dep.right_lines)}")
        path_str = " -> ".join(
            f"{step.class_name}:{step.line} ({step.statement})" for step in dep.path
        )
        blocks.append(f"   Path: {path_str}")

    return "\n".join(blocks)
