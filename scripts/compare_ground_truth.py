"""Scores the static-analysis tool and the LLMs against the manual analysis of the
researchers (the columns "Manual analysis of DF / CF / OA" of the spreadsheet with the
manual evaluation of the conflicts), per labeled method and per dependency type.

Ground truth: a row of the spreadsheet is one method (or field) changed by both branches,
with TRUE / FALSE for each of Direct Flow (DF), Confluence Flow (CF) and Overriding
Assignment (OA); a blank means that type was not analysed manually. Rows with no manual
analysis at all are left out.

Detection, per method and type:
- Tool: some finding of that type has one of its two changed lines (the L and R nodes of
  the finding, not the nodes it passes through or where a confluence ends) inside the method. Findings of both
  directions count.
- LLM: some dependency of that type, in most of the model's valid runs, has a left or
  right line inside the method.
The method's lines are those of its declaration in the merged source (`dataset/`); for a
field, or a method that can't be found there, the lines of the spreadsheet's columns "Lines
in the merge commit with changes made by left / right" are used.

The report (markdown, also written to <analysis-dir>/ground_truth_report.md) gives the tables and
explains every row that could not be scored, and every (row, source, type) that one source could
not be scored for, with its reason.

A (method, type) pair is compared only when the tool AND the model both have a result for it: the
scenario has results of both, the class is one the dataset prepared, and every run of that analysis
of the tool finished. If one side has no result, the pair is left out for both. Positive = TRUE.

Usage:
    python -m scripts.compare_ground_truth --manual "<spreadsheet>.csv" \\
        --analysis-dir results/analysis_fixed [--models claude-sonnet]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.common import MODE_OF_TYPE, TYPE_NAME, TYPES, md_table, outer_simple, pct, run_dirs
from src.analysis.errors import CAUSES, collect as collect_errors
from src.analysis.loaders import frame_key, load_llm, load_tool, parse_out_json
from src.javaspans import class_source_path, find_method, index_methods
from src.parsing.modified_lines_parser import has_attributed_lines, load_modified_lines

ANALYSIS_COLUMNS = {"DF": "Manual analysis of DF", "CF": "Manual analysis of CF", "OA": "Manual analysis of OA"}
LEFT_LINES = "Lines in the merge commit with changes made by left"
RIGHT_LINES = "Lines in the merge commit with changes made by right"
DECLARATION = "Method or field declaration changed by the two merged branches"
# Rows whose commit is wrong in the spreadsheet: (project, wrong commit prefix, class) -> the commit of its neighbours and of the dataset.
# Row 78 of static-analysis-results-3 has e825a7fd (a swagger-maven-plugin commit) for an elasticsearch method that sits between two
# d8968869 rows of the same class and is d8968869 in the dataset's own sample CSV.
COMMIT_CORRECTIONS = {
    ("elasticsearch", "e825a7fd", "org.elasticsearch.action.DocWriteResponse"): "d896886973660785aac45275ddb110c1a6babc57",
}


def read_manual(path: Path) -> tuple[list[dict], list[dict]]:
    """(rows with a manual analysis, rows without one). Each row has the truth per type
    (True / False / None when blank)."""
    with path.open(newline="", encoding="cp1252", errors="replace") as f:
        raw = list(csv.DictReader(f))
    rows, blank = [], []
    for r in raw:
        if not any((v or "").strip() for v in r.values()):
            continue  # an empty line of the spreadsheet
        truth = {t: {"TRUE": True, "FALSE": False}.get(r[col].strip().upper()) for t, col in ANALYSIS_COLUMNS.items()}
        fixed = COMMIT_CORRECTIONS.get((r["Project"].strip(), r["Merge Commit"].strip()[:8], r["Class Name"].strip()))
        if fixed:
            r = {**r, "Merge Commit": fixed}
        row = {
            "scenario": f"{r['Project'].strip()}_{r['Merge Commit'].strip()[:8]}",
            "commit": r["Merge Commit"].strip(),
            "class": r["Class Name"].strip(),
            "declaration": r[DECLARATION].strip().replace("|", ","),
            "lines": sorted({int(n) for n in re.findall(r"\d+", r[LEFT_LINES] + " " + r[RIGHT_LINES])}),
            "truth": truth,
        }
        (blank if all(v is None for v in truth.values()) else rows).append(row)
    return rows, blank


def method_lines(dataset_dir: Path, row: dict) -> tuple[set[int], str]:
    """Lines of the row's member in the merged source, and how they were found: the lines listed
    in the spreadsheet first, the method's span in the source when the row lists none."""
    if row["lines"]:
        return set(row["lines"]), "listed lines"
    scenario_dir = dataset_dir / row["scenario"]
    source = class_source_path(scenario_dir, row["class"])
    if source.is_file() and "(" in row["declaration"]:
        try:
            spans = find_method(index_methods(source.read_text(encoding="utf-8", errors="replace")), row["class"], row["declaration"])
        except Exception:  # noqa: BLE001 - javalang raises assorted parse errors
            spans = []
        if spans:
            return {n for s in spans for n in range(s.start, s.end + 1)}, "method"
    return set(), "listed lines"


def dataset_classes(dataset_dir: Path, scenario: str) -> set[str]:
    path = dataset_dir / scenario / "scenario.json"
    if not path.is_file():
        return set()
    labeled = json.loads(path.read_text(encoding="utf-8"))["labeled_methods"]
    return {m["class"].split("$")[0] for m in labeled}


def tool_changed_lines(analysis_dir: Path, scenario: str) -> dict[str, list[set[tuple[str, int]]]]:
    """{type: [changed lines (simple outer class, line) of each finding]} for the types whose
    runs all finished; a type that did not finish is left out."""
    loaded = load_tool(analysis_dir, scenario)
    out: dict[str, list[set]] = {}
    for t in TYPES:
        if not loaded[t]["complete"]:
            continue
        out[t] = []
    for t in out:
        for status_path in sorted((analysis_dir / scenario).glob(f"*/*/{MODE_OF_TYPE[t]}/status.json")):
            out_json = status_path.parent / "out.json"
            if not out_json.is_file():
                continue
            entries = parse_out_json(out_json.read_text(encoding="utf-8", errors="replace"))
            if entries is None:  # unreadable JSON: every line of the finding is all that is left
                continue
            for entry in entries:
                lines = set()
                for node in entry.get("body", {}).get("interference", []):
                    # the changed lines: the L and R nodes (DF, CF: source1/source2; OA: declaration/override),
                    # not the common point where a confluence ends
                    if node.get("type") != "confluence":
                        key = frame_key(node.get("location", {}))
                        if key is not None:
                            lines.add((outer_simple(key[0]), key[1]))
                out[t].append(lines)
    return out


def llm_runs(results_dir: Path, scenario: str, models: list[str] | None) -> dict[str, list[dict[str, list[set]]]]:
    """{model: [per run {type: [lines (simple outer class, line) of each dependency]}]}."""
    out: dict[str, list[dict[str, list[set]]]] = {}
    for model, entry in load_llm(results_dir, scenario, models).items():
        runs = []
        for run in range(entry["runs"]):
            runs.append({t: [{(outer_simple(c), n) for c, n in rep} for rep in entry["reports"][t][run]] for t in TYPES})
        out[model] = runs
    return out



CAUSE_TITLE = {key: title for key, _, title, *_ in CAUSES}


def class_has_lines(dataset_dir: Path, scenario: str, class_name: str) -> bool:
    """Whether modified-lines.txt attributes any added Left/Right line to the class: the runner of the
    agents and of the tool skips a scenario with none."""
    path = dataset_dir / scenario / "modified-lines.txt"
    return path.is_file() and has_attributed_lines(load_modified_lines(path), class_name)


def same_class_elsewhere(dataset_dir: Path, scenario: str, class_name: str) -> list[str]:
    project = scenario.rsplit("_", 1)[0].lower()
    found = []
    for d in sorted(dataset_dir.iterdir()):
        if d.is_dir() and d.name != scenario and d.name.rsplit("_", 1)[0].lower() == project and class_name.split("$")[0] in dataset_classes(dataset_dir, d.name):
            found.append(d.name)
    return found


def tool_unavailable(analysis_dir: Path, dataset_dir: Path, scenario: str, class_name: str, errors_by_mode: dict, t: str, loaded: dict | None) -> str:
    """Why the tool has no usable result of type `t` for this scenario."""
    if not class_has_lines(dataset_dir, scenario, class_name):
        return "the dataset attributed no Left/Right lines to the class, so the tool was not run on it"
    if loaded is None:
        return "no result of the tool for this scenario"
    causes = errors_by_mode.get((scenario, MODE_OF_TYPE[t]), [])
    if causes:
        return "an analysis run ended in error: " + "; ".join(sorted({CAUSE_TITLE.get(c, "an error not classified") for c in causes}))
    return "the analysis did not finish"


def llm_unavailable(results_dir: Path, dataset_dir: Path, scenario: str, class_name: str) -> str:
    if not class_has_lines(dataset_dir, scenario, class_name):
        return "the dataset attributed no Left/Right lines to the class, so the agents skip the scenario"
    return "no valid run of the model for this scenario in the results folder"


def touches(reports: list[set], class_simple: str, lines: set[int]) -> bool:
    return any(cls == class_simple and n in lines for rep in reports for cls, n in rep)


def metrics(pairs: list[tuple[bool, bool]]) -> list:
    tp = sum(1 for truth, found in pairs if truth and found)
    fn = sum(1 for truth, found in pairs if truth and not found)
    fp = sum(1 for truth, found in pairs if not truth and found)
    tn = sum(1 for truth, found in pairs if not truth and not found)
    return [len(pairs), tp, fp, fn, tn, pct(tp, tp + fp), pct(tp, tp + fn), pct(tp + tn, len(pairs))]


def build_report(manual: Path, dataset_dir: Path, results_dir: Path, analysis_dir: Path, models: list[str] | None, csv_out: Path | None) -> str:
    rows, blank_rows = read_manual(manual)
    # the spreadsheet and the dataset folders don't always agree on the case of a project name
    real_name = {d.name.lower(): d.name for d in dataset_dir.iterdir() if d.is_dir()}
    for row in [*rows, *blank_rows]:
        row["scenario"] = real_name.get(row["scenario"].lower(), row["scenario"])
    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_scenario[row["scenario"]].append(row)

    errors, _ = collect_errors(analysis_dir)
    errors_by_mode: dict[tuple[str, str], list[str]] = defaultdict(list)
    for e in errors:
        errors_by_mode[(e["scenario"], e["mode"])].append(e["cause"])

    detail: list[dict] = []
    excluded_rows: list[dict] = []  # rows no source could be scored on
    unscored: list[dict] = []  # (row, source, type) a source could not be scored for
    found_by = {"method": 0, "listed lines": 0}
    pairs: dict[str, dict[str, list[tuple[bool, bool]]]] = defaultdict(lambda: defaultdict(list))
    any_pairs: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    all_models = sorted({
        model for d in results_dir.iterdir() if d.is_dir() for model, _ in run_dirs(d) if not models or model in models
    })
    sources_seen: list[str] = ["tool", *all_models]

    for scenario, scenario_rows in sorted(by_scenario.items()):
        if not (dataset_dir / scenario).is_dir():
            for row in scenario_rows:
                others = same_class_elsewhere(dataset_dir, scenario, row["class"])
                reason = f"commit {row['commit'][:8]} is not a scenario of the dataset"
                if others:
                    reason += f"; the same class is in {', '.join(others)}, with another commit"
                excluded_rows.append({**row, "reason": reason})
            continue
        prepared = dataset_classes(dataset_dir, scenario)
        has_tool_results = (analysis_dir / scenario).is_dir()
        loaded = load_tool(analysis_dir, scenario) if has_tool_results else None
        tool = tool_changed_lines(analysis_dir, scenario) if has_tool_results else {}
        llms = llm_runs(results_dir, scenario, models)
        for model in llms:
            if model not in sources_seen:
                sources_seen.append(model)
        for row in scenario_rows:
            if row["class"].split("$")[0] not in prepared:
                excluded_rows.append({**row, "reason": "the class is not a labeled one of the sample CSV, and the dataset prepares only those, so neither the tool nor the agents analysed it"})
                continue
            lines, how = method_lines(dataset_dir, row)
            found_by[how] += 1
            simple = outer_simple(row["class"])
            record = {"scenario": scenario, "class": row["class"], "declaration": row["declaration"], "lines_from": how}
            sources: dict[str, dict[str, bool]] = {}
            if tool:
                sources["tool"] = {t: touches(tool[t], simple, lines) for t in tool}
            for model, runs in llms.items():
                sources[model] = {t: sum(touches(run[t], simple, lines) for run in runs) * 2 > len(runs) for t in TYPES}
            known = {t: v for t, v in row["truth"].items() if v is not None}
            for t, truth in known.items():
                record[f"truth_{t}"] = truth
            # a (method, type) pair is compared only when the tool AND every model have a result for it
            for t, truth in known.items():
                have = {s: t in sources.get(s, {}) for s in sources_seen}
                if all(have.values()):
                    for s in sources_seen:
                        pairs[s][t].append((truth, sources[s][t]))
                        record[f"{s}_{t}"] = sources[s][t]
                    continue
                lacking = [s for s in sources_seen if not have[s]]
                for s in sources_seen:
                    if not have[s]:
                        why = (
                            tool_unavailable(analysis_dir, dataset_dir, scenario, row["class"], errors_by_mode, t, loaded)
                            if s == "tool"
                            else llm_unavailable(results_dir, dataset_dir, scenario, row["class"])
                        )
                    else:
                        why = "not compared: no result from " + ", ".join(lacking)
                    unscored.append({"scenario": scenario, "class": row["class"], "declaration": row["declaration"], "source": s, "type": t, "reason": why})
            if known and all(t in sources.get(s, {}) for s in sources_seen for t in known):
                for s in sources_seen:
                    any_pairs[s].append((any(known.values()), any(sources[s][t] for t in known)))
            detail.append(record)

    # scenarios none of whose rows has a manual analysis
    with_truth = {r["scenario"] for r in rows}
    without: dict[str, int] = defaultdict(int)
    no_commit = 0
    for r in blank_rows:
        if not r["commit"]:
            no_commit += 1  # a row of the spreadsheet with no project and commit
        elif r["scenario"] not in with_truth:
            without[r["scenario"]] += 1

    compared = sum(1 for r in detail if any(k.startswith("tool_") for k in r))
    out = ["# Manual analysis vs the analysis tool and the LLMs", ""]
    out.append(
        f"The spreadsheet has {len(rows) + len(blank_rows)} rows (methods or fields changed by both branches). {len(rows)} have a manual analysis "
        f"of DF, CF or OA, which is the ground truth, and {len(blank_rows)} have none. Of those {len(rows)}, {len(excluded_rows)} are in a class or commit "
        f"the dataset did not prepare (see *Rows that were not analysed*), and {len(detail)} are in scope. Of the {len(detail)}, **{compared} are compared**: "
        f"the tool and the model both have a result for them; the others are listed under *Pairs a source could not be scored for*. The lines of the member "
        f"come from its source in the dataset for {found_by['method']} of the {len(detail)} in scope, and from the spreadsheet's own line columns for {found_by['listed lines']}."
    )
    out.append("")
    out.append("Truth of the scored rows: " + "; ".join(
        f"{t} {sum(1 for r in detail if r.get(f'truth_{t}') is True)} TRUE / {sum(1 for r in detail if r.get(f'truth_{t}') is False)} FALSE"
        for t in TYPES) + ". Positive = TRUE.")
    out.append("")
    header = ["Source", "Pairs", "TP", "FP", "FN", "TN", "Precision", "Recall", "Accuracy"]
    out.append("## Results")
    out.append("")
    for t in TYPES:
        out.append(f"### {TYPE_NAME[t]} ({t})")
        out.append("")
        out.append(md_table(header, [[name, *metrics(pairs[name][t])] for name in sources_seen]))
        out.append("")
    out.append("### Any type")
    out.append("")
    out.append("Positive when the manual analysis found DF, CF or OA, whatever type the tool or the model reported.")
    out.append("")
    out.append(md_table(header, [[name, *metrics(any_pairs[name])] for name in sources_seen]))
    out.append("")
    out.append(
        "How a method counts as detected: **tool**, some finding of that type has one of its two changed lines (the L and R nodes) inside the method, "
        "in either direction; **LLM**, some dependency of that type, in most of the valid runs, has a left or right line inside it. *Pairs* are the "
        "(method, type) pairs scored for that source; the ones it could not be scored for are listed below."
    )
    out.append("")

    out.append("## Rows that were not analysed")
    out.append("")
    out.append(f"{len(excluded_rows)} rows have a manual analysis but could not be scored for any source.")
    out.append("")
    if excluded_rows:
        out.append(md_table(["Scenario", "Class", "Member", "Why"], [[r["scenario"], r["class"].rsplit(".", 1)[-1], r["declaration"], r["reason"]] for r in excluded_rows]))
        out.append("")

    out.append("## Pairs a source could not be scored for")
    out.append("")
    out.append("A pair is compared only when the tool and the model both have a result for it; otherwise it is left out for both, and the reason is given for the one that lacks it.")
    out.append("")
    if unscored:
        grouped: dict[tuple, list[str]] = defaultdict(list)
        for u in unscored:
            grouped[(u["scenario"], u["class"], u["declaration"], u["source"], u["reason"])].append(u["type"])
        out.append(md_table(
            ["Scenario", "Class", "Member", "Source", "Types", "Why"],
            [[k[0], k[1].rsplit(".", 1)[-1], k[2], k[3], ", ".join(v), k[4]] for k, v in grouped.items()],
        ))
    else:
        out.append("None: every scored row is scored for every source and for every type it has a truth for.")
    out.append("")

    out.append("## Scenarios without a manual analysis")
    out.append("")
    out.append(f"{len(blank_rows)} rows have no manual analysis, so they cannot be compared: {no_commit} of them have no project or commit either, and in "
               f"{len(without)} scenarios none of the rows has one:")
    out.append("")
    out.append(", ".join(f"{s} ({n})" for s, n in sorted(without.items())) or "none")
    out.append("")

    if csv_out and detail:
        fields = sorted({k for r in detail for k in r}, key=lambda k: (k not in ("scenario", "class", "declaration", "lines_from"), k))
        with csv_out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(detail)
        out.append(f"Per-method results: `{csv_out}`.")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manual", required=True, help="The spreadsheet with the manual analysis (CSV).")
    parser.add_argument("--analysis-dir", default="results/analysis", help="Output of run_static_analysis (default: results/analysis).")
    parser.add_argument("--results-dir", default="results/dataset", help="Output of run_dataset_experiment (default: results/dataset).")
    parser.add_argument("--dataset-dir", default="dataset", help="Output of prepare_dataset (default: dataset).")
    parser.add_argument("--models", nargs="+", help="Only these model folder names (e.g. claude-sonnet).")
    parser.add_argument("--csv", help="Per-method CSV (default: <analysis-dir>/ground_truth_by_method.csv).")
    parser.add_argument("--output", help="Markdown report (default: <analysis-dir>/ground_truth_report.md).")
    args = parser.parse_args(argv)
    analysis_dir = Path(args.analysis_dir)
    csv_out = Path(args.csv) if args.csv else analysis_dir / "ground_truth_by_method.csv"
    output = Path(args.output) if args.output else analysis_dir / "ground_truth_report.md"
    report = build_report(Path(args.manual), Path(args.dataset_dir), Path(args.results_dir), analysis_dir, args.models, csv_out)
    output.write_text(report, encoding="utf-8")
    print(report)
    print(f"Report written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
