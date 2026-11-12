"""Runs Mode 1 ("detect") on every scenario prepared by
`scripts.prepare_dataset`, and compares the outcome against the dataset's
"Locally Observable Interference" label (sample-semantic-conflicts.csv) at
scenario level.

The label says whether the semantic conflict is observable as interference
in the scenario ("Yes"/"No"; a scenario is "Yes" if any of its classes is).
The comparison is coarse: did the LLM report at least one dependency in most
of its runs? That is checked against the label as a confusion-matrix cell:
true/false positive/negative, with "Yes" as the positive class.

Per scenario and model, `<results-dir>/<scenario>/<model>_run<N>/output.json`
holds each run's raw output. `<results-dir>/dataset_summary.csv` has one row
per (scenario, model) with the label, each run's dependency count, the
fraction of runs that flagged anything, and the outcome. A final table counts
outcomes per model. Scenarios with no attributed (Left/Right) lines are skipped.

Usage:
    python -m scripts.run_dataset_experiment --models claude:sonnet gpt-oss:20b-cloud --runs 3
    python -m scripts.run_dataset_experiment --models claude:sonnet --runs 3 --evaluate-by-method
    python -m scripts.run_dataset_experiment --models claude:sonnet --runs 3 --skip-existing
    python -m scripts.run_dataset_experiment --models claude:sonnet --runs 3 --with-analysis --mergedataset D:/.../mergedataset
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.compare_analysis import full_report as build_analysis_report
from scripts.evaluate_by_method import main as evaluate_by_method
from scripts.run_static_analysis import add_tool_dir_argument, resolve_tool_dir, start_background as start_analysis, summarize as summarize_analysis
from src.analysis.common import outcome
from src.compare import load_llm_result
from src.config import AppConfig, load_config
from src.experiment import resolve_model_spec, run_once, sanitize_model_name
from src.parsing.modified_lines_parser import has_attributed_lines, load_modified_lines


def scenario_config(base: AppConfig, scenario_dir: Path) -> AppConfig:
    """Point the base config's inputs at one prepared scenario."""
    return base.model_copy(
        deep=True,
        update={
            "input": base.input.model_copy(
                update={
                    "diff_file": scenario_dir / "diff.txt",
                    "modified_lines_file": scenario_dir / "modified-lines.txt",
                    "dependencies_file": None,
                    "source_root": (scenario_dir / "source").resolve(),
                }
            )
        },
    )


def describe_usage(output_path: Path) -> str:
    """One line on the Claude plan's usage windows as recorded by a run, e.g.
    'five hour: 9% used, 91% left, resets 14:30'."""
    try:
        info = json.loads(output_path.read_text(encoding="utf-8")).get("rate_limit")
    except (OSError, ValueError):
        info = None
    if not info:
        return "session usage: not reported"

    parts = []
    for name, window in (info.get("unifiedWindows") or {}).items():
        used = window.get("utilization")
        if used is None:
            continue
        resets = window.get("resetsAt")
        reset_text = ""
        if resets:
            moment = datetime.fromtimestamp(resets)
            reset_text = f", resets {moment:%H:%M}" if moment.date() == datetime.now().date() else f", resets {moment:%d/%m %H:%M}"
        parts.append(f"{name.replace('_', ' ')}: {used:.0%} used, {1 - used:.0%} left{reset_text}")
    if not parts:
        parts.append(f"{str(info.get('rateLimitType', 'limit')).replace('_', ' ')} window, no utilization reported")
    if info.get("status") not in (None, "allowed"):
        parts.append(f"status: {info['status']}")
    return "session usage: " + "; ".join(parts)


def existing_count(output_path: Path) -> int | None:
    """Number of dependencies in an earlier, valid run's output, or None if
    there is no such file or that run failed (a failed run is worth retrying)."""
    if not output_path.is_file():
        return None
    try:
        return len(load_llm_result(output_path).dependencies)
    except (ValueError, OSError):
        return None


def count_detected(output_path: Path) -> int | None:
    """Number of dependencies the LLM reported, or None if the run failed."""
    try:
        return len(load_llm_result(output_path).dependencies)
    except (ValueError, OSError) as exc:
        print(f"    warning: {exc}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="config.yaml", help="Base YAML config (default: config.yaml).")
    parser.add_argument("--models", nargs="+", required=True, help="Models to run; 'claude:' prefix uses claude_code.")
    parser.add_argument("--runs", "-n", type=int, default=1, help="Runs per model per scenario (default: 1).")
    parser.add_argument("--dataset-dir", default="dataset", help="Output of prepare_dataset (default: dataset).")
    parser.add_argument("--results-dir", default="results/dataset", help="Where to write results (default: results/dataset).")
    parser.add_argument("--only", nargs="+", help="Only run scenarios whose folder name contains one of these strings.")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Don't run a (scenario, model, run) whose output.json already exists in --results-dir and "
        "is valid; its result is reused in the summary. Runs that failed are run again.",
    )
    parser.add_argument(
        "--evaluate-by-method",
        action="store_true",
        help="After the runs, also score them per labeled method (scripts.evaluate_by_method), "
        "for the models run now, over every scenario that has results for them in --results-dir.",
    )
    parser.add_argument(
        "--with-analysis",
        action="store_true",
        help="Also run the conflict-static-analysis tool (in Docker) on the same scenarios, in parallel "
        "with the agents, and compare its findings with theirs at the end (needs --mergedataset; see "
        "scripts.run_static_analysis).",
    )
    parser.add_argument("--mergedataset", help="Path to the MergeDataSet checkout, whose jars the tool analyses (for --with-analysis).")
    parser.add_argument("--analysis-results-dir", default="results/analysis", help="Where the tool's results go (default: results/analysis).")
    parser.add_argument("--analysis-jobs", type=int, default=2, help="Tool runs at a time next to the agents (default: 2).")
    parser.add_argument("--analysis-timeout", type=int, default=1800, help="Seconds per tool run (default: 1800, as in the study).")
    parser.add_argument("--analysis-heap", default="3g", help="JVM heap per tool run (default: 3g).")
    parser.add_argument("--force-analysis", action="store_true", help="Run the tool again even where --analysis-results-dir already has a result.")
    add_tool_dir_argument(parser, "The conflict-static-analysis checkout, with its jar built (for --with-analysis)")
    args = parser.parse_args(argv)

    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.with_analysis and not args.mergedataset:
        parser.error("--with-analysis needs --mergedataset")
    tool_dir = resolve_tool_dir(parser, args.tool_dir) if args.with_analysis else None

    base = load_config(args.config)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    # The tool runs on its own threads (each one a docker container) while the agents
    # below run; results already on disk are reused.
    analysis = None
    if args.with_analysis:
        analysis = start_analysis(
            Path(args.dataset_dir), Path(args.mergedataset), Path(args.analysis_results_dir),
            tool_dir, args.only, args.analysis_jobs, args.analysis_timeout,
            args.analysis_heap, args.force_analysis,
        )

    def finish_analysis() -> None:
        if analysis is None:
            return
        print("\n=== Static analysis ===")
        print(f"[analysis] waiting for the remaining runs (analysis jobs: {len(analysis.jobs)})")
        print(f"[analysis] finished: {summarize_analysis(analysis.wait())}")
        print("\n=== Analysis tool vs agents ===\n")
        print(build_analysis_report(
            Path(args.analysis_results_dir), results_dir, Path(args.dataset_dir),
            [sanitize_model_name(m) for m in args.models],
        ))
        print(f"\nFull report (both tables and the tool's errors): {Path(args.analysis_results_dir) / 'comparison_report.txt'}")

    rows: list[dict] = []
    for scenario_dir in sorted(p for p in Path(args.dataset_dir).iterdir() if p.is_dir()):
        if args.only and not any(token in scenario_dir.name for token in args.only):
            continue
        scenario = json.loads((scenario_dir / "scenario.json").read_text(encoding="utf-8"))
        label = scenario["locally_observable_interference"]
        if not has_attributed_lines(load_modified_lines(scenario_dir / "modified-lines.txt")):
            print(f"\n=== {scenario_dir.name}: skipped (no attributed lines) ===")
            continue
        print(f"\n=== {scenario_dir.name} (interference: {label}) ===")
        config = scenario_config(base, scenario_dir)

        for model in args.models:
            safe_model = sanitize_model_name(model)
            is_claude = resolve_model_spec(model)[1] == "claude_code"
            counts: list[int] = []
            for run_idx in range(1, args.runs + 1):
                run_dir = results_dir / scenario_dir.name / f"{safe_model}_run{run_idx}"
                run_dir.mkdir(parents=True, exist_ok=True)
                if args.skip_existing:
                    existing = existing_count(run_dir / "output.json")
                    if existing is not None:
                        print(f"  {model} run {run_idx}/{args.runs}: skipped, result exists (detected: {existing})")
                        counts.append(existing)
                        continue
                print(f"  {model} run {run_idx}/{args.runs}")
                try:
                    output_path = run_once(config, model, run_dir)
                except Exception as exc:  # noqa: BLE001 - one bad run must not abort the experiment
                    print(f"    crashed, excluded: {exc}", file=sys.stderr)
                    continue
                count = count_detected(output_path)
                if count is None:
                    print("    failed, excluded")
                else:
                    print(f"    detected: {count}")
                    counts.append(count)
                if is_claude:
                    print(f"    {describe_usage(output_path)}")

            flagged_runs = sum(1 for c in counts if c > 0)
            majority = bool(counts) and flagged_runs * 2 > len(counts)
            rows.append(
                {
                    "scenario": scenario_dir.name,
                    "model": model,
                    "interference_label": label,
                    "runs_ok": len(counts),
                    "runs_total": args.runs,
                    "detected_per_run": " ".join(map(str, counts)),
                    "median_detected": statistics.median(counts) if counts else "",
                    "runs_flagged": f"{flagged_runs}/{len(counts)}" if counts else "",
                    "outcome": outcome(label, majority) if counts else "no valid runs",
                }
            )

    if not rows:
        print("\nNo scenarios were run.")
        finish_analysis()
        return 1

    summary_path = results_dir / "dataset_summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print("\n=== Summary ===")
    print(f"{'Scenario':<40}{'Model':<22}{'Label':<7}{'Detected/run':<16}{'Flagged':<9}Outcome")
    for r in rows:
        print(
            f"{r['scenario']:<40}{r['model']:<22}{r['interference_label']:<7}"
            f"{r['detected_per_run']:<16}{r['runs_flagged']:<9}{r['outcome']}"
        )

    print("\n=== Outcomes per model (positive = interference 'Yes') ===")
    print(f"{'Model':<22}{'TP':>4}{'FN':>4}{'FP':>4}{'TN':>4}")
    for model in args.models:
        tally = Counter(r["outcome"] for r in rows if r["model"] == model)
        print(f"{model:<22}{tally['TP']:>4}{tally['FN']:>4}{tally['FP']:>4}{tally['TN']:>4}")
    print(f"\nWritten to: {summary_path}")

    finish_analysis()

    if args.evaluate_by_method:
        print("\n=== Evaluation by method ===")
        return evaluate_by_method(
            [
                "--dataset-dir", args.dataset_dir,
                "--results-dir", args.results_dir,
                "--models", *(sanitize_model_name(m) for m in args.models),
            ]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
