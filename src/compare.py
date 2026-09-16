"""Compares Mode 1 ("detect") LLM output against the static-analysis tool's
precomputed dependencies and exports a summary table.

Two dependencies are considered a match if they share the same type and
their sets of involved (class, line) locations overlap; matching within a
type is done greedily, from largest overlap to smallest, one-to-one.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterable

from src.config import load_config
from src.parsing.dependency_parser import (
    DependencySet,
    PrecomputedDependency,
    load_dependencies,
)
from src.parsing.modified_lines_parser import load_modified_lines
from src.schemas import DetectedDependency, LineRef, Mode1Result

LineKey = tuple[str, int]

DEPENDENCY_TYPES = ["Direct Flow", "Overriding Assignment", "Confluence Flow"]


def _line_set(refs: Iterable[LineRef]) -> set[LineKey]:
    """Convert LineRefs to a set of (class, line) keys."""
    return {(ref.class_name, ref.line) for ref in refs}


def _llm_line_set(dep: DetectedDependency) -> set[LineKey]:
    """All locations (both sides) involved in an LLM-detected dependency."""
    return _line_set(dep.left_lines) | _line_set(dep.right_lines)


def _tool_line_set(dep: PrecomputedDependency) -> set[LineKey]:
    """All locations across every node's path in a tool-found dependency."""
    lines: set[LineKey] = set()
    for node in dep.nodes:
        for step in node.path:
            lines.add((step.class_name, step.line))
    return lines


def _greedy_match(
    llm_deps: list[tuple[int, set[LineKey]]],
    tool_deps: list[tuple[int, set[LineKey]]],
) -> int:
    """Greedy one-to-one matching by largest line-set overlap; returns match count."""
    candidates = [
        (len(lset & tset), li, ti)
        for li, lset in llm_deps
        for ti, tset in tool_deps
        if lset & tset
    ]
    candidates.sort(key=lambda c: c[0], reverse=True)

    matched_llm: set[int] = set()
    matched_tool: set[int] = set()
    count = 0
    for _, li, ti in candidates:
        if li in matched_llm or ti in matched_tool:
            continue
        matched_llm.add(li)
        matched_tool.add(ti)
        count += 1
    return count


def load_llm_result(path: str | Path) -> Mode1Result:
    """Load and validate a Mode 1 pipeline output file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(
            f"'{path}' does not look like a pipeline output file "
            "(expected a JSON object with a 'mode' field)"
        )
    if payload.get("mode") != "detect":
        raise ValueError(
            f"'{path}' is not a Mode 1 ('detect') output file "
            f"(mode={payload.get('mode')!r})"
        )
    if payload.get("parse_error"):
        raise ValueError(
            f"'{path}' has a parse_error and cannot be compared: "
            f"{payload['parse_error']}"
        )
    result = payload.get("result")
    if result is None:
        raise ValueError(f"'{path}' has no structured result to compare")
    return Mode1Result.model_validate(result)


def compare(llm_result: Mode1Result, tool_deps: DependencySet) -> list[dict]:
    """Match LLM-detected against tool-found dependencies per type and
    return one summary row per type plus a totals row."""
    rows: list[dict] = []
    total_llm = total_tool = total_added_llm = total_added_tool = 0

    for dep_type in DEPENDENCY_TYPES:
        llm_of_type = [
            (i, _llm_line_set(d))
            for i, d in enumerate(llm_result.dependencies)
            if d.type == dep_type
        ]
        tool_of_type = [
            (i, _tool_line_set(d))
            for i, d in enumerate(tool_deps.dependencies)
            if d.type == dep_type
        ]
        matched = _greedy_match(llm_of_type, tool_of_type)
        added_llm = len(llm_of_type) - matched
        added_tool = len(tool_of_type) - matched

        rows.append(
            {
                "type": dep_type,
                "detected_by_llm": len(llm_of_type),
                "found_by_tool": len(tool_of_type),
                "added_llm": added_llm,
                "added_tool": added_tool,
            }
        )
        total_llm += len(llm_of_type)
        total_tool += len(tool_of_type)
        total_added_llm += added_llm
        total_added_tool += added_tool

    rows.append(
        {
            "type": "Total",
            "detected_by_llm": total_llm,
            "found_by_tool": total_tool,
            "added_llm": total_added_llm,
            "added_tool": total_added_tool,
        }
    )
    return rows


def write_csv(rows: list[dict], path: str | Path) -> None:
    """Write the comparison rows to a CSV file."""
    fieldnames = ["type", "detected_by_llm", "found_by_tool", "added_llm", "added_tool"]
    header_labels = {
        "detected_by_llm": "Detected (LLM)",
        "found_by_tool": "Found (tool)",
        "added_llm": "Added (LLM)",
        "added_tool": "Added (tool)",
    }
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["type"] + [header_labels[name] for name in fieldnames[1:]])
        for row in rows:
            writer.writerow([row[name] for name in fieldnames])


def print_table(rows: list[dict]) -> None:
    """Print the comparison rows as an aligned text table."""
    header = (
        f"{'Type':<24}{'Detected (LLM)':>16}{'Found (tool)':>14}"
        f"{'Added (LLM)':>14}{'Added (tool)':>14}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['type']:<24}{row['detected_by_llm']:>16}"
            f"{row['found_by_tool']:>14}{row['added_llm']:>14}{row['added_tool']:>14}"
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `python -m src.compare --llm-output <path>`."""
    parser = argparse.ArgumentParser(
        description="Compare Mode 1 ('detect') LLM output against the "
        "static-analysis tool's dependencies and export a summary table."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML config file (used for the modified-lines file).",
    )
    parser.add_argument(
        "--llm-output",
        required=True,
        help="Path to the Mode 1 pipeline output JSON (e.g. output.json).",
    )
    parser.add_argument(
        "--dependencies",
        help="Path to the static-analysis tool's dependencies JSON. "
        "Defaults to the config's input.dependencies_file.",
    )
    parser.add_argument(
        "--out",
        default="comparison.csv",
        help="Path to write the comparison CSV table (default: comparison.csv).",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    dependencies_path = args.dependencies or config.input.dependencies_file
    if dependencies_path is None:
        parser.error(
            "No dependencies file given (pass --dependencies or set "
            "input.dependencies_file in the config)."
        )

    authorship_by_class = load_modified_lines(config.input.modified_lines_file)
    tool_deps = load_dependencies(dependencies_path, authorship_by_class)
    llm_result = load_llm_result(args.llm_output)

    rows = compare(llm_result, tool_deps)
    print_table(rows)
    write_csv(rows, args.out)
    print(f"\nWritten to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
