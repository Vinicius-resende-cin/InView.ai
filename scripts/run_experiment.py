"""Runs Mode 1 ("detect") N times for each of a list of models, and reports
how each model's runs compare to the static-analysis tool's own findings.

For each `--model` given, the pipeline is run `--runs` times, with
`llm.model` overridden from the base `--config` file. A model prefixed with
'claude:' (e.g. 'claude:sonnet') is routed through the claude_code backend
(the Claude Code CLI); any other model runs through the opencode backend.
llm.provider and everything else stay as configured. Each run's output is
written to `results/<model>_run<N>/output.json`, and immediately scored
against the tool's dependencies (same logic as `src.compare`), writing a
per-run `comparison.csv` alongside it.

Once every run is done, a summary table is printed (and saved as
`results/summary.csv`). Its first row, "Static analysis", reports how many
dependencies the static-analysis tool found (repeated across the max/min/
median columns, since it's a single fixed number, not a distribution).
Each following row is one model, with the maximum, minimum, and median
number of dependencies detected by the LLM across its runs.

Usage:
    python -m scripts.run_experiment --models gpt-oss:20b-cloud claude:sonnet --runs 5
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.compare import DEPENDENCY_TYPES, compare, load_llm_result, write_csv
from src.config import AppConfig, load_config
from src.graph import run_pipeline
from src.parsing.dependency_parser import load_dependencies
from src.parsing.modified_lines_parser import load_modified_lines

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
CLAUDE_PREFIX = "claude:"


def sanitize_model_name(model: str) -> str:
    """Turn a model spec (e.g. 'gpt-oss:20b-cloud', 'claude:sonnet') into a
    filesystem-safe name."""
    return _UNSAFE_CHARS.sub("-", model).strip("-")


def resolve_model_spec(model: str) -> tuple[str, str]:
    """Resolve a `--models` entry into (model_tag, agent.backend).

    A 'claude:<model>' spec (e.g. 'claude:sonnet') routes through the
    claude_code backend (the Claude Code CLI). Anything else runs through
    the opencode backend. llm.provider is left as configured in the base
    config either way.
    """
    if model.startswith(CLAUDE_PREFIX):
        model_tag = model[len(CLAUDE_PREFIX):]
        if not model_tag:
            raise ValueError(f"'{model}' is missing a model name after '{CLAUDE_PREFIX}'")
        return model_tag, "claude_code"
    return model, "opencode"


def run_once(base_config: AppConfig, model: str, output_dir: Path) -> Path:
    """Run the detect-mode pipeline once with `model` substituted in, writing
    output.json under `output_dir`. Returns the output.json path."""
    model_tag, backend = resolve_model_spec(model)
    output_path = output_dir / "output.json"

    run_config = base_config.model_copy(
        deep=True,
        update={
            "mode": "detect",
            "llm": base_config.llm.model_copy(update={"model": model_tag}),
            "agent": base_config.agent.model_copy(update={"backend": backend}),
            "output": base_config.output.model_copy(update={"path": output_path}),
        },
    )
    run_pipeline(run_config)
    return output_path


def score_run(output_path: Path, tool_deps, comparison_out: Path) -> int | None:
    """Compare one run's output against the tool's dependencies. Returns the
    total number of dependencies detected by the LLM, or None if the run
    failed (parse_error / unparsable output)."""
    try:
        llm_result = load_llm_result(output_path)
    except ValueError as exc:
        print(f"    warning: {exc}", file=sys.stderr)
        return None
    rows = compare(llm_result, tool_deps)
    write_csv(rows, comparison_out)
    total_row = next(row for row in rows if row["type"] == "Total")
    return total_row["detected_by_llm"]


def print_summary(summary: list[dict]) -> None:
    header = (
        f"{'Model':<28}{'Max detected':>14}"
        f"{'Min detected':>14}{'Median':>10}{'Runs (ok/total)':>18}"
    )
    print(header)
    print("-" * len(header))
    for row in summary:
        max_detected = row["max_detected"] if row["max_detected"] is not None else "N/A"
        min_detected = row["min_detected"] if row["min_detected"] is not None else "N/A"
        median = row["median"] if row["median"] is not None else "N/A"
        ok_total = f"{row['ok']}/{row['total']}" if row["ok"] is not None else "-"
        print(
            f"{row['model']:<28}{max_detected!s:>14}"
            f"{min_detected!s:>14}{median!s:>10}{ok_total:>18}"
        )


def write_summary_csv(summary: list[dict], path: Path) -> None:
    fieldnames = [
        "model",
        "max_detected",
        "min_detected",
        "median",
        "ok",
        "total",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in summary:
            writer.writerow({k: row[k] for k in fieldnames})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run detect-mode N times per model and compare results "
        "against the static-analysis tool's findings."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Base YAML config file (default: config.yaml). Only llm.model "
        "and output.path are overridden per run.",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        required=True,
        help="List of model names/tags to run (space-separated). Prefix a "
        "model with 'claude:' (e.g. 'claude:sonnet') to run it through the "
        "claude_code backend; anything else runs through the opencode "
        "backend (llm.provider is left as configured either way).",
    )
    parser.add_argument(
        "--runs",
        "-n",
        type=int,
        required=True,
        help="Number of detect-mode runs per model.",
    )
    parser.add_argument(
        "--results-dir",
        default="results",
        help="Directory to write '<model>_run<N>/' folders into (default: results).",
    )
    parser.add_argument(
        "--dependencies",
        help="Static-analysis tool's dependencies JSON to compare against. "
        "Defaults to the config's input.dependencies_file.",
    )
    parser.add_argument(
        "--summary-out",
        help="Path to write the summary CSV (default: <results-dir>/summary.csv).",
    )
    args = parser.parse_args(argv)

    if args.runs < 1:
        parser.error("--runs must be at least 1")

    base_config = load_config(args.config)
    dependencies_path = args.dependencies or base_config.input.dependencies_file
    if dependencies_path is None:
        parser.error(
            "No dependencies file given (pass --dependencies or set "
            "input.dependencies_file in the config)."
        )

    authorship_by_class = load_modified_lines(base_config.input.modified_lines_file)
    tool_deps = load_dependencies(dependencies_path, authorship_by_class)
    detected_by_analysis = sum(
        1 for d in tool_deps.dependencies if d.type in DEPENDENCY_TYPES
    )

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = [
        {
            "model": "Static analysis",
            "max_detected": detected_by_analysis,
            "min_detected": detected_by_analysis,
            "median": detected_by_analysis,
            "ok": None,
            "total": None,
        }
    ]
    for model in args.models:
        safe_model = sanitize_model_name(model)
        print(f"\n=== Model: {model} ===")
        scores: list[int] = []
        for run_idx in range(1, args.runs + 1):
            output_dir = results_dir / f"{safe_model}_run{run_idx}"
            output_dir.mkdir(parents=True, exist_ok=True)
            print(f"  run {run_idx}/{args.runs} -> {output_dir}")
            try:
                output_path = run_once(base_config, model, output_dir)
            except Exception as exc:  # noqa: BLE001 - one bad run must not abort the whole experiment
                print(f"    run {run_idx} crashed, excluded from scoring: {exc}", file=sys.stderr)
                continue
            score = score_run(output_path, tool_deps, output_dir / "comparison.csv")
            if score is None:
                print(f"    run {run_idx} failed, excluded from scoring")
            else:
                print(f"    detected: {score}")
                scores.append(score)

        summary.append(
            {
                "model": model,
                "max_detected": max(scores) if scores else None,
                "min_detected": min(scores) if scores else None,
                "median": statistics.median(scores) if scores else None,
                "ok": len(scores),
                "total": args.runs,
            }
        )

    print("\n=== Summary ===")
    print_summary(summary)

    summary_out = Path(args.summary_out) if args.summary_out else results_dir / "summary.csv"
    write_summary_csv(summary, summary_out)
    print(f"\nWritten to: {summary_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
