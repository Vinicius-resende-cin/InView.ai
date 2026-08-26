"""Renders the semantic dependency-type definitions from config into prompt text."""

from __future__ import annotations

from src.config import DependencyTypeConfig


def render_definitions(dependency_types: list[DependencyTypeConfig]) -> str:
    lines = ["Semantic dependency types:"]
    for dep_type in dependency_types:
        lines.append(f"- {dep_type.name}: {dep_type.definition.strip()}")
    return "\n".join(lines)


def render_types_to_detect(dependency_types: list[DependencyTypeConfig]) -> str:
    names = [dep_type.name for dep_type in dependency_types if dep_type.detect]
    if not names:
        return "No dependency types are enabled for detection."
    return "Look specifically for the following dependency types: " + ", ".join(names) + "."
