"""Loader for Mode 2's precomputed semantic-dependency data.

This is a PLACEHOLDER schema. It should be revisited once a real sample from
the static-analysis tool is available, and this module's models/loader
adjusted to match it.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class LineRef(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    class_name: str = Field(alias="class")
    line: int


class PathStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    class_name: str = Field(alias="class")
    line: int
    statement: str


class PrecomputedDependency(BaseModel):
    type: str
    left_lines: list[LineRef]
    right_lines: list[LineRef]
    path: list[PathStep]


class DependencySet(BaseModel):
    dependencies: list[PrecomputedDependency]


def parse_dependencies(text: str) -> DependencySet:
    raw = json.loads(text)
    raw.pop("_comment", None)
    return DependencySet.model_validate(raw)


def load_dependencies(path: str | Path) -> DependencySet:
    return parse_dependencies(Path(path).read_text(encoding="utf-8"))
