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
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.common import outcome, outer_simple, run_dirs
from src.compare import load_llm_result
from src.javaspans import MethodSpan, class_source_path, find_method, index_methods
from src.schemas import Mode1Result


def touching_methods(result: Mode1Result, class_name: str, spans: list[MethodSpan]) -> list[int]:
    """Per reported dependency: 1 if it touches any span of `spans` (all in
    `class_name`'s file), else 0."""
    target = outer_simple(class_name)
    hits: list[int] = []
    for dep in result.dependencies:
        lines = [
            ref.line
            for ref in (*dep.left_lines, *dep.right_lines)
            if outer_simple(ref.class_name) == target
        ]
        hits.append(int(any(s.start <= ln <= s.end for s in spans for ln in lines)))
    return hits


def load_runs(scenario_results: Path) -> dict[str, list[Path]]:
    """{model: [output.json paths]} for one scenario's results folder."""
    runs: dict[str, list[Path]] = defaultdict(list)
    for model, run_dir in run_dirs(scenario_results):
        runs[model].append(run_dir / "output.json")
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
