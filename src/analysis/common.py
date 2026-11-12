"""Names, folder layout and formatting shared by the comparison scripts.

The static-analysis tool's three analyses stand for the three dependency types
(DF, CF, OA); the agents' runs live in `<results-dir>/<scenario>/<model>_run<N>/`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path

MODE_OF_TYPE = {
    "DF": "svfa-interprocedural",
    "CF": "dfp-confluence-interprocedural",
    "OA": "ioa",
}
TYPES = list(MODE_OF_TYPE)
TYPE_OF_MODE = {mode: type_key for type_key, mode in MODE_OF_TYPE.items()}
TYPE_NAME = {"DF": "Direct Flow", "CF": "Confluence Flow", "OA": "Overriding Assignment"}

RUN_DIR_RE = re.compile(r"^(?P<model>.+)_run(?P<n>\d+)$")


def run_dirs(scenario_results: Path) -> Iterator[tuple[str, Path]]:
    """(model folder name, run dir) of each `<model>_run<N>/` of a scenario's results
    that holds an output.json."""
    for run_dir in sorted(scenario_results.iterdir()):
        match = RUN_DIR_RE.match(run_dir.name)
        if run_dir.is_dir() and match and (run_dir / "output.json").is_file():
            yield match.group("model"), run_dir


def outer_simple(class_name: str) -> str:
    """'org.example.Outer$Inner' -> 'Outer': package and nested classes dropped."""
    return class_name.split("$")[0].rsplit(".", 1)[-1]


def outcome(label: str, positive: bool) -> str:
    """Confusion-matrix cell of a prediction against the interference label
    ('Yes' = positive); 'unlabeled' when the label is neither Yes nor No."""
    if label not in ("Yes", "No"):
        return "unlabeled"
    if label == "Yes":
        return "TP" if positive else "FN"
    return "FP" if positive else "TN"


def pct(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.0%}" if denominator else "n/a"


def text_table(header: list[str], rows: list[list], right_cols: Iterable[int] | None = None) -> str:
    """Plain-text table; the columns in `right_cols` are right-aligned (default: all but the first)."""
    right = set(range(1, len(header))) if right_cols is None else set(right_cols)
    cells = [[str(c) for c in header]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(header))]
    lines = []
    for n, row in enumerate(cells):
        lines.append("  ".join(c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(row)))
        if n == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def md_table(header: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)
