"""Pydantic models for the LLM's structured output, for both modes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class LineRef(BaseModel):
    class_name: str
    line: int


class DetectedDependency(BaseModel):
    type: str
    left_lines: list[LineRef]
    right_lines: list[LineRef]
    explanation: str


class Mode1Result(BaseModel):
    """Structured output for Mode 1 ("detect")."""

    dependencies: list[DetectedDependency]


class ReviewFinding(BaseModel):
    dependency_type: str
    lines_involved: list[LineRef]
    risk_level: Literal["conflict", "safe", "uncertain"]
    explanation: str
    recommendation: str


class Mode2Result(BaseModel):
    """Structured output for Mode 2 ("review")."""

    findings: list[ReviewFinding]
    summary: str
