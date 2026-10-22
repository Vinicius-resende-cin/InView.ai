"""Scores already-collected Mode 1 runs against the dataset's per-method
"Locally Observable Interference" labels. It never calls a model: it reads
`<results-dir>/<scenario>/<model>_run<N>/output.json` as written by
`scripts.run_dataset_experiment`.

Unlike the scenario-level summary, nothing is grouped per class. Each labeled
method of the dataset (`labeled_methods` in a scenario's `scenario.json`) is
its own case:

- A run that reports no dependencies detects none of the scenario's methods.
- Otherwise each reported dependency is mapped to the methods it touches: a
  dependency touches a method if any of its left/right lines falls inside that
  method's body in the merged source (`<dataset>/<scenario>/source/`). A
  labeled method counts as detected in that run if some dependency touches it.
- A labeled method is detected by a model if it is detected in most of the
  model's valid runs (runs with a parse_error are ignored), then scored against
  its label with "Yes" as the positive class (TP/FN/FP/TN).

Writes `<results-dir>/method_summary.csv`, one row per (labeled method, model).

Usage:
    python -m scripts.evaluate_by_method
    python -m scripts.evaluate_by_method --models claude-sonnet nemotron-3-super-cloud
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import javalang
from javalang.tokenizer import tokenize

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.compare import load_llm_result
from src.schemas import Mode1Result

RUN_DIR_RE = re.compile(r"^(?P<model>.+)_run(?P<n>\d+)$")
CONSTRUCTOR_NAMES = {"constructor", "contructor"}  # the dataset misspells it


@dataclass
class MethodSpan:
    name: str  # '<init>' for constructors
    param_types: list[str]
    enclosing_types: list[str]  # outermost first
    start: int
    end: int


def _simple_type(text: str) -> str:
    """'java.util.List<String>...' -> 'List': generics, varargs, package dropped."""
    text = re.sub(r"<.*>", "", text).replace("...", "").replace("[]", "").strip()
    return text.rsplit(".", 1)[-1]


def parse_declaration(declaration: str) -> tuple[str, list[str]]:
    """'decode(ChannelHandlerContext, Channel)' -> ('decode', ['ChannelHandlerContext', 'Channel'])."""
    name, _, rest = declaration.partition("(")
    params_text = rest.rsplit(")", 1)[0]
    params: list[str] = []
    depth, current = 0, ""
    for ch in params_text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            params.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        params.append(current)
    return name.strip(), [_simple_type(p.rsplit(" ", 1)[0] if " " in p.strip() else p) for p in params]


def _end_line(tokens: list, name_index: int) -> int:
    """Last line of the member whose name token is at `name_index`: the
    closing brace of its body, or the ';' of a bodiless declaration."""
    i = name_index
    depth = 0
    while i < len(tokens):  # skip the parameter list
        value = tokens[i].value
        if value == "(":
            depth += 1
        elif value == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    while i < len(tokens) and tokens[i].value not in ("{", ";"):  # throws ..., default ...
        i += 1
    if i >= len(tokens) or tokens[i].value == ";":
        return tokens[min(i, len(tokens) - 1)].position[0]
    depth = 0
    while i < len(tokens):
        value = tokens[i].value
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth == 0:
                return tokens[i].position[0]
        i += 1
    return tokens[-1].position[0]


def index_methods(source: str) -> list[MethodSpan]:
    """Every method/constructor in `source` (nested and anonymous ones too)
    with its line range."""
    tokens = list(tokenize(source))
    tree = javalang.parse.parse(source)
    spans: list[MethodSpan] = []
    for path, node in tree:
        if not isinstance(node, (javalang.tree.MethodDeclaration, javalang.tree.ConstructorDeclaration)):
            continue
        if node.position is None:
            continue
        line = node.position[0]
        name_index = next(
            (
                idx
                for idx, tok in enumerate(tokens)
                if tok.position[0] >= line
                and tok.value == node.name
                and idx + 1 < len(tokens)
                and tokens[idx + 1].value == "("
            ),
            None,
        )
        if name_index is None:
            continue
        # javalang's path interleaves ancestor nodes with the lists holding
        # them (whose entries are siblings, not ancestors): keep only nodes.
        enclosing = [n.name for n in path if isinstance(n, javalang.tree.TypeDeclaration)]
        spans.append(
            MethodSpan(
                name="<init>" if isinstance(node, javalang.tree.ConstructorDeclaration) else node.name,
                param_types=[_simple_type(p.type.name) for p in node.parameters],
                enclosing_types=enclosing,
                start=min(line, tokens[name_index].position[0]),
                end=_end_line(tokens, name_index),
            )
        )
    return spans


def class_source_path(scenario_dir: Path, class_name: str) -> Path:
    outer = class_name.split("$")[0]
    return scenario_dir / "source" / "src" / "main" / "java" / (outer.replace(".", "/") + ".java")


def find_method(spans: list[MethodSpan], class_name: str, declaration: str) -> list[MethodSpan]:
    """Spans matching a labeled method: by name and arity, narrowed by
    parameter types when that disambiguates overloads."""
    simple_class = class_name.split("$")[0].rsplit(".", 1)[-1]
    inner = class_name.split("$", 1)[1].split("$")[-1] if "$" in class_name else None
    name, params = parse_declaration(declaration)
    if name in CONSTRUCTOR_NAMES or name == simple_class:
        name = "<init>"

    def in_class(span: MethodSpan) -> bool:
        if inner is not None:
            return inner in span.enclosing_types
        return span.enclosing_types[:1] == [simple_class] and len(span.enclosing_types) == 1

    candidates = [s for s in spans if s.name == name and len(s.param_types) == len(params) and in_class(s)]
    if not candidates and inner is None:
        # The dataset sometimes names the outer class for a method that lives
        # in a nested one: fall back to any class of the file.
        candidates = [s for s in spans if s.name == name and len(s.param_types) == len(params)]
    exact = [s for s in candidates if s.param_types == params]
    return exact or candidates


def _outer_simple(class_name: str) -> str:
    return class_name.split("$")[0].rsplit(".", 1)[-1]


def touching_methods(result: Mode1Result, class_name: str, spans: list[MethodSpan]) -> list[int]:
    """Per reported dependency: 1 if it touches any span of `spans` (all in
    `class_name`'s file), else 0."""
    target = _outer_simple(class_name)
    hits: list[int] = []
    for dep in result.dependencies:
        lines = [
            ref.line
            for ref in (*dep.left_lines, *dep.right_lines)
            if _outer_simple(ref.class_name) == target
        ]
        hits.append(int(any(s.start <= ln <= s.end for s in spans for ln in lines)))
    return hits


def outcome(label: str, detected: bool) -> str:
    if label not in ("Yes", "No"):
        return "unlabeled"
    if label == "Yes":
        return "TP" if detected else "FN"
    return "FP" if detected else "TN"


def load_runs(scenario_results: Path) -> dict[str, list[Path]]:
    """{model: [output.json paths]} for one scenario's results folder."""
    runs: dict[str, list[Path]] = defaultdict(list)
    for run_dir in sorted(scenario_results.iterdir()):
        match = RUN_DIR_RE.match(run_dir.name)
        if run_dir.is_dir() and match and (run_dir / "output.json").is_file():
            runs[match.group("model")].append(run_dir / "output.json")
    return runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset-dir", default="dataset", help="Output of prepare_dataset (default: dataset).")
    parser.add_argument("--results-dir", default="results/dataset", help="Existing run results (default: results/dataset).")
    parser.add_argument("--models", nargs="+", help="Only these model folder names (e.g. claude-sonnet).")
    parser.add_argument("--out", help="Summary CSV (default: <results-dir>/method_summary.csv).")
    args = parser.parse_args(argv)

    dataset_dir, results_dir = Path(args.dataset_dir), Path(args.results_dir)
    rows: list[dict] = []

    for scenario_dir in sorted(p for p in dataset_dir.iterdir() if p.is_dir()):
        scenario_results = results_dir / scenario_dir.name
        if not scenario_results.is_dir():
            continue
        scenario = json.loads((scenario_dir / "scenario.json").read_text(encoding="utf-8"))
        runs_by_model = load_runs(scenario_results)
        if args.models:
            runs_by_model = {m: r for m, r in runs_by_model.items() if m in args.models}

        for labeled in scenario["labeled_methods"]:
            class_name, declaration, label = labeled["class"], labeled["declaration"], labeled["label"]
            source_file = class_source_path(scenario_dir, class_name)
            spans: list[MethodSpan] = []
            if source_file.is_file():
                all_spans = index_methods(source_file.read_text(encoding="utf-8", errors="replace"))
                # A loose label (e.g. 'constructor()' for a constructor with
                # parameters) is evaluated at the signature prepare_dataset
                # resolved it to, not at the literal declaration.
                for target in labeled.get("resolved_signatures") or [declaration]:
                    spans.extend(find_method(all_spans, class_name, target))
            if not spans:
                print(
                    f"warning: {scenario_dir.name}: could not locate {class_name}.{declaration} in the merged source",
                    file=sys.stderr,
                )

            for model, outputs in runs_by_model.items():
                detected_runs, valid, deps_per_run = 0, 0, []
                for output in outputs:
                    try:
                        result = load_llm_result(output)
                    except ValueError:
                        continue  # run failed (parse_error): ignored
                    valid += 1
                    deps_per_run.append(len(result.dependencies))
                    # No dependencies -> hits is empty -> not detected.
                    if any(touching_methods(result, class_name, spans)):
                        detected_runs += 1
                detected = valid > 0 and detected_runs * 2 > valid
                rows.append(
                    {
                        "scenario": scenario_dir.name,
                        "class": class_name,
                        "method": declaration,
                        "label": label,
                        "model": model,
                        "runs_ok": valid,
                        "runs_total": len(outputs),
                        "dependencies_per_run": " ".join(map(str, deps_per_run)),
                        "runs_detected": f"{detected_runs}/{valid}" if valid else "",
                        "method_located": bool(spans),
                        "outcome": outcome(label, detected) if valid else "no valid runs",
                    }
                )

    if not rows:
        print("No results found to evaluate.", file=sys.stderr)
        return 1

    out_path = Path(args.out) if args.out else results_dir / "method_summary.csv"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print(f"{'Scenario':<38}{'Method':<34}{'Model':<24}{'Label':<6}{'Detected':<10}Outcome")
    for r in rows:
        method = r["class"].rsplit(".", 1)[-1] + "." + r["method"].split("(")[0]
        print(
            f"{r['scenario']:<38}{method[:32]:<34}{r['model']:<24}{r['label']:<6}"
            f"{r['runs_detected']:<10}{r['outcome']}"
        )

    print("\n=== Outcomes per model (positive = interference 'Yes') ===")
    print(f"{'Model':<26}{'TP':>4}{'FN':>4}{'FP':>4}{'TN':>4}{'No valid':>10}")
    for model in sorted({r["model"] for r in rows}):
        tally = Counter(r["outcome"] for r in rows if r["model"] == model)
        print(
            f"{model:<26}{tally['TP']:>4}{tally['FN']:>4}{tally['FP']:>4}{tally['TN']:>4}"
            f"{tally['no valid runs']:>10}"
        )
    print(f"\nWritten to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
