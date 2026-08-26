"""Loader for Mode 2's precomputed semantic-dependency data.

The static-analysis tool emits a JSON array of raw findings, e.g.::

    [
      {
        "type": "OAINTER",
        "label": "OA conflict",
        "body": {
          "description": "...",
          "interference": [
            {
              "type": "declaration",
              "branch": "L",
              "text": "...",
              "location": {"file": "", "class": "org.example.Clue", "method": "<init>", "line": 22},
              "stackTrace": [{"class": "...", "method": "...", "line": 9}, ...]
            },
            ...
          ]
        }
      },
      ...
    ]

Findings are classified into one of the three target dependency types by
inspecting `type`/`label`:

- contains "OA"  -> Overriding Assignment
- contains "CF"  -> Confluence Flow
- `type == "CONFLICT"` (Sparse Value-Flow / "SVFA conflict") -> Direct Flow

Any finding that doesn't match one of these is dropped (out of scope).
Exact-duplicate findings (the tool is known to repeat entries) are collapsed
to one. Each interference node's `branch` field ("L"/"R") is often empty; in
that case, authorship is resolved by matching each stack-trace frame's
(class, line) against the modified-lines.txt data, since state-element line
numbers here correspond to the merged version's line numbers.
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
    model_config = ConfigDict(populate_by_name=True)

    role: str = Field(alias="type")
    branch: str = ""
    text: str = ""
    location: RawLocation
    stack_trace: list[RawLocation] = Field(default_factory=list, alias="stackTrace")


class PathStep(BaseModel):
    class_name: str
    line: int
    method: str
    author: Optional[Author] = None


class InterferenceNode(BaseModel):
    role: str
    branch: str
    text: str
    path: list[PathStep]


class PrecomputedDependency(BaseModel):
    type: DependencyType
    description: str
    nodes: list[InterferenceNode]


class DependencySet(BaseModel):
    dependencies: list[PrecomputedDependency]


def classify_dependency_type(tool_type: str, label: str) -> Optional[DependencyType]:
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
    authorship = authorship_by_class.get(class_name)
    if authorship is None:
        return None
    return authorship.author_of(line)  # type: ignore[return-value]


def _build_path(
    node: RawInterferenceNode, authorship_by_class: dict[str, ClassAuthorship]
) -> list[PathStep]:
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
    return parse_dependencies(Path(path).read_text(encoding="utf-8"), authorship_by_class)
