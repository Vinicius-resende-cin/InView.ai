"""Prints a method_summary.csv (from scripts.evaluate_by_method) as a readable
table, one line per labeled method with a column per model, followed by the
outcome summary per model.

Models don't always have results for the same methods (a run may be partial), so
the summary is given twice: over each model's own methods, and over only the
methods every model has a valid result for, which is the fair comparison.

Usage:
    python -m scripts.show_method_summary [results/dataset/method_summary.csv]
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

from src.analysis.common import pct, text_table

OUTCOMES = ("TP", "FN", "FP", "TN")


def summarize(models: list[str], cells: dict[tuple, dict[str, dict]], keys: list[tuple]) -> list[list[str]]:
    """One summary row per model over the given method keys."""
    out = []
    for model in models:
        tally = Counter(cells[k][model]["outcome"] for k in keys if model in cells[k])
        tp, fn, fp, tn = (tally[o] for o in OUTCOMES)
        precision, recall = pct(tp, tp + fp), pct(tp, tp + fn)
        f1 = pct(2 * tp, 2 * tp + fp + fn)
        out.append(
            [
                model,
                str(sum(1 for k in keys if model in cells[k])),
                str(tp), str(fn), str(fp), str(tn),
                str(tally["unlabeled"]), str(tally["no valid runs"]),
                precision, recall, pct(tn, tn + fp), pct(tp + tn, tp + tn + fp + fn), f1,
            ]
        )
    return out


def print_table(header: list[str], rows: list[list[str]], align_right: set[int] = frozenset()) -> None:
    print(text_table(header, rows, align_right))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("csv_path", nargs="?", default="results/dataset/method_summary.csv")
    args = parser.parse_args(argv)

    rows = list(csv.DictReader(open(args.csv_path, encoding="utf-8")))
    models = sorted({r["model"] for r in rows})

    # A method can legitimately be listed twice in a scenario, so rows are keyed
    # by their occurrence number as well, and a repeat shows up as its own line.
    cells: dict[tuple, dict[str, dict]] = defaultdict(dict)
    order: list[tuple] = []
    seen: Counter = Counter()
    for r in rows:
        base = (r["scenario"], r["class"], r["method"], r["label"])
        key = (*base, seen[(base, r["model"])])
        seen[(base, r["model"])] += 1
        if key not in cells:
            order.append(key)
        cells[key][r["model"]] = r

    # --- per-method table
    header = ["Scenario", "Method", "Label", *models]
    table = []
    for key in sorted(order, key=lambda k: (k[0].lower(), k[1], k[2])):
        scenario, cls, method, label, _ = key
        name = f"{cls.rsplit('.', 1)[-1]}.{method.split('(')[0]}".rstrip(".")
        row = [scenario, name[:42], label]
        for model in models:
            c = cells[key].get(model)
            if c is None:
                row.append("-")
            elif c["outcome"] == "no valid runs":
                row.append("no valid runs")
            else:
                row.append(f"{c['runs_detected']} {c['outcome']}")
        table.append(row)
    print(f"Per labeled method ({len(order)} methods; cell = runs that flagged it / valid runs, outcome; '-' = no result)\n")
    print_table(header, table)

    # --- summaries
    summary_header = ["Model", "Methods", "TP", "FN", "FP", "TN", "Unlabeled", "No valid", "Precision", "Recall", "Specificity", "Accuracy", "F1"]
    right = set(range(1, len(summary_header)))
    print("\n\nOutcomes per model, over each model's own methods (positive = interference 'Yes')\n")
    print_table(summary_header, summarize(models, cells, order), right)

    common = [k for k in order if all(m in cells[k] and cells[k][m]["outcome"] != "no valid runs" for m in models)]
    if len(models) > 1:
        print(f"\n\nOutcomes per model, over the {len(common)} methods every model has a valid result for\n")
        print_table(summary_header, summarize(models, cells, common), right)

    # --- the unlabeled / unlocated caveats
    unlocated = sorted({(k[0], k[1].rsplit(".", 1)[-1], k[2]) for k in order if any(c["method_located"] == "False" for c in cells[k].values())})
    if unlocated:
        print("\n\nNot located in the merged source (counted as never detected):")
        for scenario, cls, method in unlocated:
            label = next(k[3] for k in order if k[0] == scenario and k[2] == method)
            print(f"  {scenario}: {cls}.{method or '(empty declaration)'} [label {label}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
