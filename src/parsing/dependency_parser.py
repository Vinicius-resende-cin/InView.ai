"""Loader for Mode 2's precomputed semantic-dependency data (the static-
analysis tool's raw JSON findings). See README's static-analysis input
format notes for the raw JSON shape and classification rules.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.parsing.modified_lines_parser import ClassAuthorship

DependencyType = Literal["Direct Flow", "Overriding Assignment", "Confluence Flow"]
Author = Literal["Left", "Right"]


class RawLocation(BaseModel):
    """A (class, method, line) location as reported by the tool's raw JSON."""

    model_config = ConfigDict(populate_by_name=True)

    class_name: str = Field(alias="class")
    method: str = ""
    line: int
    file: str = ""

    @field_validator("line", mode="before")
    @classmethod
    def _coerce_line(cls, value: object) -> int:
        return int(value)


class RawInterferenceNode(BaseModel):
    """One raw interference entry from the tool's `body.interference` array."""

    model_config = ConfigDict(populate_by_name=True)

    role: str = Field(alias="type")
    branch: str = ""
    text: str = ""
    location: RawLocation
    stack_trace: list[RawLocation] = Field(default_factory=list, alias="stackTrace")


class PathStep(BaseModel):
    """One location in an InterferenceNode's path, with resolved authorship."""

    class_name: str
    line: int
    method: str
    author: Optional[Author] = None


class InterferenceNode(BaseModel):
    """One node of a PrecomputedDependency, with authorship resolved."""

    role: str
    branch: str
    text: str
    path: list[PathStep]


class PrecomputedDependency(BaseModel):
    """One dependency reported by the static-analysis tool, classified into
    one of the three target types."""

    type: DependencyType
    description: str
    nodes: list[InterferenceNode]


class DependencySet(BaseModel):
    """All of the static-analysis tool's dependencies for one merge scenario."""

    dependencies: list[PrecomputedDependency]


def classify_dependency_type(tool_type: str, label: str) -> Optional[DependencyType]:
    """Map the tool's own `type`/`label` fields to one of the three target
    dependency types, or None if the finding is out of scope."""
    tool_type_u = tool_type.upper()
    label_u = label.upper()
    if "OA" in tool_type_u or "OA" in label_u:
        return "Overriding Assignment"
    if "CF" in tool_type_u or "CF" in label_u:
        return "Confluence Flow"
    if tool_type_u == "CONFLICT" or "SVFA" in label_u:
        return "Direct Flow"
    return None


def _resolve_author(
    class_name: str, line: int, authorship_by_class: dict[str, ClassAuthorship]
) -> Optional[Author]:
    """Resolve authorship for a (class, line) via the modified-lines.txt data."""
    authorship = authorship_by_class.get(class_name)
    if authorship is None:
        return None
    return authorship.author_of(line)  # type: ignore[return-value]


def _build_path(
    node: RawInterferenceNode, authorship_by_class: dict[str, ClassAuthorship]
) -> list[PathStep]:
    """Build a node's path with authorship resolved for each frame."""
    frames = node.stack_trace or [node.location]
    return [
        PathStep(
            class_name=frame.class_name,
            line=frame.line,
            method=frame.method,
            author=_resolve_author(frame.class_name, frame.line, authorship_by_class),
        )
        for frame in frames
    ]


def _dedupe_raw_entries(entries: list[dict]) -> list[dict]:
    """Collapse exact-duplicate raw findings (the tool is known to repeat entries)."""
    seen: set[str] = set()
    unique: list[dict] = []
    for entry in entries:
        signature = json.dumps(entry, sort_keys=True)
        if signature not in seen:
            seen.add(signature)
            unique.append(entry)
    return unique


def parse_dependencies(
    text: str, authorship_by_class: dict[str, ClassAuthorship]
) -> DependencySet:
    """Parse the static-analysis tool's raw JSON into a DependencySet,
    classifying and deduplicating findings and resolving authorship."""
    raw_entries: list[dict] = json.loads(text)
    dependencies: list[PrecomputedDependency] = []

    for entry in _dedupe_raw_entries(raw_entries):
        dep_type = classify_dependency_type(entry.get("type", ""), entry.get("label", ""))
        if dep_type is None:
            continue

        body = entry.get("body", {})
        raw_nodes = [
            RawInterferenceNode.model_validate(node)
            for node in body.get("interference", [])
        ]
        nodes = [
            InterferenceNode(
                role=node.role,
                branch=node.branch,
                text=node.text,
                path=_build_path(node, authorship_by_class),
            )
            for node in raw_nodes
        ]
        dependencies.append(
            PrecomputedDependency(
                type=dep_type,
                description=body.get("description", ""),
                nodes=nodes,
            )
        )

    return DependencySet(dependencies=dependencies)


def load_dependencies(
    path: str | Path, authorship_by_class: dict[str, ClassAuthorship]
) -> DependencySet:
    """Read and parse the static-analysis tool's dependencies file."""
    return parse_dependencies(Path(path).read_text(encoding="utf-8"), authorship_by_class)
