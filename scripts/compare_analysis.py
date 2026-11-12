"""Compares what the static-analysis tool and the agents (LLMs) found, on the same
scenarios. There is no ground truth here: it only compares the results of the
runs of the tools against each other.

Inputs: `<analysis-dir>` (scripts.run_static_analysis) and `<results-dir>`
(scripts.run_dataset_experiment). The comparison is by dependency type, with the
tool's analyses standing for the types: DF = svfa-interprocedural, CF =
dfp-confluence-interprocedural, OA = ioa.

Part 1 - how many dependencies each tool/LLM reported. For the tool this is every
finding of its runs (both directions, every jar of the scenario); for an LLM it is
every dependency of its runs, summed over runs (the per-run mean is shown too).

Part 2 - how many *different* dependencies each reported, and how many of those
agree. Reports of the same type that share a (class, line) location are one
distinct dependency (so an LLM repeating itself over several runs counts once, as
does the tool repeating a finding in both directions). Then, per scenario and
type, the tool's and an LLM's distinct dependencies are matched one-to-one, largest
overlap first: `matched` ones were found by both, `tool only` and `LLM only` are the
detections each added over the other.

A (scenario, type) pair enters the comparison when every run of that type's
analysis finished (a timeout or error in one direction would make the tool's side
incomplete) and the LLM has at least one valid run on the scenario.

Usage:
    python -m scripts.compare_analysis [--models claude-sonnet ...]
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from src.analysis.common import TYPE_NAME, TYPES, pct, text_table
from src.analysis.errors import build_errors_report
from src.analysis.loaders import LENIENT_FILES, LineSet, load_llm, load_tool
from src.compare import _greedy_match

# --- distinct dependencies and matching ----------------------------------------------


def distinct(reports: list[LineSet]) -> list[LineSet]:
    """Merge reports that share a location into one dependency (union-find)."""
    parent = list(range(len(reports)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[tuple[str, int], int] = {}
    for i, lines in enumerate(reports):
        for loc in lines:
            if loc in owner:
                parent[find(i)] = find(owner[loc])
            else:
                owner[loc] = i
    merged: dict[int, set] = defaultdict(set)
    for i, lines in enumerate(reports):
        merged[find(i)] |= lines
    return [frozenset(s) for s in merged.values() if s]


def compare_scenario(tool_reports: list[LineSet], llm_reports: list[LineSet]) -> dict[str, int]:
    tool_d, llm_d = distinct(tool_reports), distinct(llm_reports)
    matched = _greedy_match(list(enumerate(llm_d)), list(enumerate(tool_d)))
    return {
        "tool_distinct": len(tool_d),
        "llm_distinct": len(llm_d),
        "matched": matched,
        "tool_only": len(tool_d) - matched,
        "llm_only": len(llm_d) - matched,
    }


# --- the report ------------------------------------------------------------------------


def build_report(analysis_dir: Path, results_dir: Path, dataset_dir: Path, model_filter: list[str] | None, csv_out: Path | None) -> str:
    LENIENT_FILES.clear()
    scenarios = sorted(p.name for p in analysis_dir.iterdir() if p.is_dir() and (dataset_dir / p.name).is_dir()) if analysis_dir.is_dir() else []

    tool_by_scenario = {s: load_tool(analysis_dir, s) for s in scenarios}
    llm_by_scenario = {s: load_llm(results_dir, s, model_filter) for s in scenarios}
    models = sorted({m for s in scenarios for m in llm_by_scenario[s]})

    # run states of the tool
    states: dict[str, int] = defaultdict(int)
    for per_type in tool_by_scenario.values():
        for t in TYPES:
            for state in per_type[t]["states"]:
                states[state] += 1
    state_text = ", ".join(f"{n} {s}" for s, n in sorted(states.items())) or "none"

    # per (scenario, model, type) comparison
    cell: dict[tuple, dict[str, int]] = {}
    tool_found = {t: 0 for t in TYPES}
    tool_distinct = {t: 0 for t in TYPES}
    tool_scenarios: dict[str, set[str]] = {t: set() for t in TYPES}
    llm_found = {m: {t: 0 for t in TYPES} for m in models}
    llm_runs = {m: 0 for m in models}
    llm_distinct = {m: {t: 0 for t in TYPES} for m in models}
    llm_scenarios = {m: {t: set() for t in TYPES} for m in models}
    pairs = {m: 0 for m in models}
    excluded = 0
    rows_csv = []

    for s in scenarios:
        for t in TYPES:
            tool = tool_by_scenario[s][t]
            if not tool["complete"]:
                excluded += 1
                continue
            tool_found[t] += len(tool["reports"])
            tool_distinct[t] += len(distinct(tool["reports"]))
            if tool["reports"]:
                tool_scenarios[t].add(s)
            for m, llm in llm_by_scenario[s].items():
                runs = llm["reports"][t]
                flat = [lines for run in runs for lines in run]
                result = compare_scenario(tool["reports"], flat)
                cell[(s, m, t)] = result
                llm_found[m][t] += len(flat)
                llm_distinct[m][t] += result["llm_distinct"]
                if flat:
                    llm_scenarios[m][t].add(s)
                rows_csv.append(
                    {
                        "scenario": s, "model": m, "type": TYPE_NAME[t], "llm_runs": llm["runs"],
                        "tool_reports": len(tool["reports"]), "llm_reports": len(flat), **result,
                    }
                )
        for m, llm in llm_by_scenario[s].items():
            if any(tool_by_scenario[s][t]["complete"] for t in TYPES):
                llm_runs[m] += llm["runs"]
                pairs[m] += 1

    out: list[str] = []
    out.append(f"Scenarios with analysis results: {len(scenarios)} | analysis runs: {state_text}")
    if LENIENT_FILES:
        out.append(
            f"{len(set(LENIENT_FILES))} out.json files are not valid JSON (the tool does not escape quotes in "
            "confluence findings); their locations were read from the raw text."
        )
    out.append(f"(scenario, type) pairs left out because an analysis run did not finish: {excluded}")
    for m in models:
        out.append(f"{m}: {pairs[m]} scenarios compared, {llm_runs[m]} valid runs")

    # Part 1
    out.append("\n\n=== Part 1 - dependencies found by each tool / LLM ===\n")
    rows = [["Analysis tool", *[tool_found[t] for t in TYPES], sum(tool_found.values()),
             sum(len(tool_scenarios[t]) for t in TYPES) and len(set().union(*tool_scenarios.values()))]]
    for m in models:
        total = sum(llm_found[m].values())
        scen = len(set().union(*llm_scenarios[m].values())) if llm_scenarios[m] else 0
        rows.append([f"{m} (all runs)", *[llm_found[m][t] for t in TYPES], total, scen])
        mean = lambda t: f"{llm_found[m][t] / llm_runs[m]:.2f}" if llm_runs[m] else "n/a"
        rows.append([f"{m} (mean per run)", *[mean(t) for t in TYPES], f"{total / llm_runs[m]:.2f}" if llm_runs[m] else "n/a", ""])
    out.append(text_table(["Source", *TYPES, "Total", "Scenarios with >=1"], rows))
    out.append("\nThe tool's count is every finding of its runs, both directions; an LLM's is summed over its runs.")

    # Part 2
    out.append("\n\n=== Part 2 - different dependencies, and how many agree ===\n")
    out.append("Reports of one type that share a (class, line) are one distinct dependency. 'Matched' = found by both;")
    out.append("'tool only' / 'LLM only' = what each added over the other.\n")
    for m in models:
        rows = []
        totals = {k: 0 for k in ("tool_distinct", "llm_distinct", "matched", "tool_only", "llm_only")}
        for t in TYPES:
            agg = {k: sum(cell[(s, m, t)][k] for s in scenarios if (s, m, t) in cell) for k in totals}
            for k in totals:
                totals[k] += agg[k]
            rows.append([TYPE_NAME[t], agg["tool_distinct"], agg["llm_distinct"], agg["matched"], agg["tool_only"], agg["llm_only"],
                         pct(agg["matched"], agg["tool_distinct"]), pct(agg["matched"], agg["llm_distinct"])])
        rows.append(["Total", totals["tool_distinct"], totals["llm_distinct"], totals["matched"], totals["tool_only"], totals["llm_only"],
                     pct(totals["matched"], totals["tool_distinct"]), pct(totals["matched"], totals["llm_distinct"])])
        out.append(f"Analysis tool vs {m}\n")
        out.append(text_table(["Type", "Tool distinct", "LLM distinct", "Matched", "Tool only", "LLM only", "% of tool matched", "% of LLM matched"], rows))
        out.append("")

    if csv_out and rows_csv:
        with csv_out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows_csv[0]))
            writer.writeheader()
            writer.writerows(rows_csv)
        out.append(f"Per-scenario numbers written to {csv_out}")
    return "\n".join(out)


def full_report(
    analysis_dir: Path, results_dir: Path, dataset_dir: Path, model_filter: list[str] | None,
    csv_out: Path | None = None, output: Path | None = None,
) -> str:
    """Both comparison tables plus the report of the tool's errors. Also written to
    <analysis-dir>/comparison_report.txt (`output`) and, the errors alone, to
    <analysis-dir>/errors_report.md."""

    csv_out = csv_out or analysis_dir / "comparison_by_scenario.csv"
    errors = build_errors_report(analysis_dir)
    (analysis_dir / "errors_report.md").write_text(errors, encoding="utf-8")
    report = build_report(analysis_dir, results_dir, dataset_dir, model_filter, csv_out)
    report += "\n\n\n=== Errors of the analysis tool ===\n\n" + errors
    (output or analysis_dir / "comparison_report.txt").write_text(report, encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--analysis-dir", default="results/analysis", help="Output of run_static_analysis (default: results/analysis).")
    parser.add_argument("--results-dir", default="results/dataset", help="Output of run_dataset_experiment (default: results/dataset).")
    parser.add_argument("--dataset-dir", default="dataset", help="Output of prepare_dataset (default: dataset).")
    parser.add_argument("--models", nargs="+", help="Only these model folder names (e.g. claude-sonnet).")
    parser.add_argument("--csv", help="Per-scenario CSV (default: <analysis-dir>/comparison_by_scenario.csv).")
    parser.add_argument("--output", help="Text file for the full report (default: <analysis-dir>/comparison_report.txt).")
    args = parser.parse_args(argv)

    analysis_dir = Path(args.analysis_dir)
    print(full_report(
        analysis_dir, Path(args.results_dir), Path(args.dataset_dir), args.models,
        Path(args.csv) if args.csv else None, Path(args.output) if args.output else None,
    ))
    print(f"\nReport written to {Path(args.output) if args.output else analysis_dir / 'comparison_report.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
