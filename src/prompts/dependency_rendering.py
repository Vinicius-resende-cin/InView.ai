"""Renders precomputed (Mode 2) semantic dependencies into prompt text."""

from __future__ import annotations

from src.parsing.dependency_parser import DependencySet, InterferenceNode, PathStep


def _render_path_step(step: PathStep) -> str:
    """Render one path step as "class:line [author] (method)"."""
    tag = f" [{step.author}]" if step.author else ""
    return f"{step.class_name}:{step.line}{tag} ({step.method})"


def _render_node(node: InterferenceNode) -> str:
    """Render one interference node with its full path."""
    branch_tag = f" (reported branch: {node.branch})" if node.branch else ""
    path_str = " -> ".join(_render_path_step(step) for step in node.path)
    return f"   - {node.role}{branch_tag}: {node.text}\n     path: {path_str}"


def render_dependencies(dependencies: DependencySet) -> str:
    """Render the static-analysis tool's precomputed dependencies for the prompt."""
    if not dependencies.dependencies:
        return "No semantic dependencies were reported by static analysis."

    blocks: list[str] = ["Dependencies already found by static analysis:"]
    for i, dep in enumerate(dependencies.dependencies, start=1):
        blocks.append(f"{i}. Type: {dep.type}")
        blocks.append(f"   Description: {dep.description}")
        for node in dep.nodes:
            blocks.append(_render_node(node))

    return "\n".join(blocks)
