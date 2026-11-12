"""Reads what the static-analysis tool and the agents reported, as sets of (outer class, line).

The tool side is `<analysis-dir>/<scenario>/<jar>/<direction>/<mode>/{status,out}.json`
(scripts.run_static_analysis); the agents' side is `<results-dir>/<scenario>/<model>_run<N>/output.json`
(scripts.run_dataset_experiment).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.analysis.common import TYPE_NAME, TYPE_OF_MODE, TYPES, run_dirs
from src.compare import load_llm_result

FINISHED = {"findings", "no-conflicts"}

LineSet = frozenset  # of (outer class, line)


def _key(class_name: str, line: int) -> tuple[str, int]:
    """Nested classes count as their outer class: the agents name the outer one."""
    return class_name.split("$")[0], int(line)


# --- the tool's side ---------------------------------------------------------------


def parse_out_json(text: str) -> list[dict] | None:
    """The tool's out.json is an array it appends to; a run that was cut short can
    leave it unterminated, so a missing closing bracket is repaired."""
    text = text.strip()
    for candidate in (text, re.sub(r",\s*$", "", text) + "\n]"):
        try:
            data = json.loads(candidate)
            return data if isinstance(data, list) else None
        except json.JSONDecodeError:
            continue
    return None


# The DF and CF analyses write a stack-trace frame as a string,
# "(<pkg.Class: ret method(args)>, statement, 71)", where OA writes an object.
_FRAME_STRING = re.compile(r"^\(<(?P<cls>[^:>]+):.*>,.*,\s*(?P<line>-?\d+)\)\s*$", re.DOTALL)


def frame_key(frame) -> tuple[str, int] | None:
    """(outer class, line) of a stack-trace frame or location, in either shape."""
    try:
        if isinstance(frame, dict):
            return _key(frame["class"], int(frame["line"]))
        match = _FRAME_STRING.match(frame)
        return _key(match.group("cls"), int(match.group("line"))) if match else None
    except (KeyError, TypeError, ValueError):
        return None


LENIENT_FILES: list[Path] = []  # out.json files that needed the tolerant reader (reported at the end)

_ENTRY_START = re.compile(r"(?m)^\{\s*$")
_LOCATION = re.compile(r'"class":\s*"(?P<cls>[^"]+)",\s*"method":.*?"line":\s*"?(?P<line>-?\d+)"?', re.DOTALL)
_FRAME_LINE = re.compile(r'^\s*"?(?P<frame>\(<[^:>]+:.*>,.*,\s*-?\d+\))"?,?\s*$')


def _lenient_line_sets(text: str) -> list[LineSet] | None:
    """Read locations straight from the text of an out.json that is not valid JSON.

    The confluence analysis writes its statements into the JSON without escaping
    their double quotes (a Jimple statement like `getDB("admin")` breaks the file),
    but class and line are all this comparison needs: every finding starts at a `{`
    in the first column, and its locations and stack-trace frames are recognisable
    on their own."""
    chunks = _ENTRY_START.split(text)[1:]
    sets: list[LineSet] = []
    for chunk in chunks:
        lines = set()
        for match in _LOCATION.finditer(chunk):
            lines.add(_key(match.group("cls"), int(match.group("line"))))
        for raw in chunk.splitlines():
            frame = _FRAME_LINE.match(raw)
            key = frame_key(frame.group("frame")) if frame else None
            if key is not None:
                lines.add(key)
        if lines:
            sets.append(frozenset(lines))
    return sets or None


def tool_line_sets(out_json: Path) -> list[LineSet] | None:
    """One line set per finding in an out.json (None if it can't be read): every
    (class, line) of the finding's stack traces and of its source and sink nodes.
    Findings the tool printed twice are collapsed."""
    text = out_json.read_text(encoding="utf-8", errors="replace")
    entries = parse_out_json(text)
    if entries is None:
        sets = _lenient_line_sets(text)
        if sets is not None:
            LENIENT_FILES.append(out_json)
        return sets
    seen: set[str] = set()
    sets: list[LineSet] = []
    for entry in entries:
        signature = json.dumps(entry, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        lines = set()
        for node in entry.get("body", {}).get("interference", []):
            for frame in [node.get("location", {}), *(node.get("stackTrace") or [])]:
                key = frame_key(frame)
                if key is not None:
                    lines.add(key)
        sets.append(frozenset(lines))
    return sets


def load_tool(analysis_dir: Path, scenario: str) -> dict[str, dict]:
    """{type: {"complete": bool, "reports": [LineSet], "states": [str]}} for one scenario."""
    out: dict[str, dict] = {t: {"complete": True, "reports": [], "states": []} for t in TYPES}
    found_any = False
    for status_path in sorted((analysis_dir / scenario).glob("*/*/*/status.json")):
        found_any = True
        status = json.loads(status_path.read_text(encoding="utf-8"))
        type_key = TYPE_OF_MODE.get(status.get("mode"))
        if type_key is None:
            continue
        entry = out[type_key]
        entry["states"].append(status["state"])
        out_json = status_path.parent / "out.json"
        sets = tool_line_sets(out_json) if out_json.is_file() else []
        if sets is None:
            entry["complete"] = False
        elif status["state"] not in FINISHED:
            entry["complete"] = False
            entry["reports"].extend(sets)  # partial results are kept out of the comparison but noted
        else:
            entry["reports"].extend(sets)
    if not found_any:
        for t in TYPES:
            out[t]["complete"] = False
    for t in TYPES:
        if not out[t]["states"]:
            out[t]["complete"] = False
    return out


# --- the LLMs' side ---------------------------------------------------------------


def load_llm(results_dir: Path, scenario: str, model_filter: list[str] | None) -> dict[str, dict]:
    """{model: {"runs": n_valid, "reports": {type: [[LineSet] per run]}}} for one scenario."""
    out: dict[str, dict] = {}
    scenario_dir = results_dir / scenario
    if not scenario_dir.is_dir():
        return out
    for model, run_dir in run_dirs(scenario_dir):
        if model_filter and model not in model_filter:
            continue
        try:
            result = load_llm_result(run_dir / "output.json")
        except ValueError:
            continue  # a failed run
        entry = out.setdefault(model, {"runs": 0, "reports": {t: [] for t in TYPES}})
        entry["runs"] += 1
        per_type: dict[str, list[LineSet]] = {t: [] for t in TYPES}
        for dep in result.dependencies:
            type_key = next((k for k, name in TYPE_NAME.items() if name == dep.type), None)
            if type_key is None:
                continue
            lines = frozenset(_key(r.class_name, r.line) for r in (*dep.left_lines, *dep.right_lines))
            per_type[type_key].append(lines)
        for t in TYPES:
            entry["reports"][t].append(per_type[t])
    return out
