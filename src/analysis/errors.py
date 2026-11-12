"""Report of the runs of the static-analysis tool that ended in error.

Reads the `status.json` and `console.txt` of every run under the analysis results
directory (see scripts.run_static_analysis), classifies each error by the exception
the tool printed, and explains each cause. A run counts as an error when the tool
exited with a Java exception or a non-zero code. The comparison leaves out a
(scenario, type) pair when any run of that analysis errored.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from src.analysis.common import TYPE_OF_MODE, TYPES
from src.analysis.loaders import load_tool

# key, pattern in console.txt, title, explanation, what can be done, where the problem is
CAUSES = [
    ("head-of-empty-list", r"head of empty list",
     "`NoSuchElementException: head of empty list` in the confluence analysis",
     "A bug in the tool. `DFPConfluenceAnalysis.reportConflictsConfluence` calls `getPathVisitedMethods().head()` for each "
     "confluence flow, and for some flows that list is empty. It happens after the analysis ran: the console already lists the "
     "conflicts it found, but the exception stops the run before `out.json` is written, so the findings are lost.",
     "Fixed in the tool's branch `fix/confluence-report-crashes` (a flow with an empty path is skipped, as the DF analysis already "
     "does). Rebuild the tool from that branch and rerun.",
     "the tool"),
    ("npe-visited-methods", r"VisitedMethods\.toString",
     "`NullPointerException` in `VisitedMethods.toString` (confluence analysis)",
     "`br.unb.cic.soot.graph.VisitedMethods.toString` (Graph.scala:61, in the `svfa-scala` library) dereferences a null while the "
     "tool builds the text of a confluence path (`ConfluenceConflict.hashCode` -> `toString`). The run stops before `out.json` is written.",
     "Fixed in the tool's branch `fix/confluence-report-crashes` (`ConfluenceConflict.nodeToString` falls back to a short node description). "
     "The null itself comes from the library and was not investigated.",
     "svfa-scala (a library of the tool)"),
    ("no-fasthierarchy", r"no active FastHierarchy",
     "`RuntimeException: no active FastHierarchy present for scene` (Soot)",
     "Soot's Spark call-graph builder asked for the class hierarchy and the scene had none, because Soot loaded no classes. This "
     "happens when none of the jars of the merge build contains the analysed class. For spring-boot `c93ea54e` the merge's "
     "`spring-boot-autoconfigure` jar has 10 entries (only META-INF) and its `buildFinal.jar` has no `org/springframework/boot/autoconfigure` "
     "class at all (the left build is the same), so the runner falls back to passing the build directory. The class exists only in the "
     "base/right builds (version 1.3.3), which are a different version of the code, not the merge.",
     "A data problem: the dataset's merge build does not contain the class (analysing the right jar instead would analyse other code, whose lines do not match the merge). Nothing to run.",
     "the dataset's build"),
    ("path-joined", r"The path '",
     "`Error: The path '<dir>:<jar>:...'` (Soot, in `ioa`)",
     "`ioa` hands the whole `-cp` value to Soot as one `process_dir` entry. Given a directory, the tool expands it to `dir:jar1:jar2:...`, "
     "which Soot reads as one non-existent path. So `ioa` works only with a single jar; it fails when the runner has to pass the build directory.",
     "Same data problem as the previous cause; `ioa` cannot take a directory.",
     "the dataset's build + `ioa`'s single-path input"),
    ("oom", r"OutOfMemoryError|exit code 137",
     "`OutOfMemoryError` / process killed for memory (`ioa`, Spark points-to analysis)",
     "The JVM heap (`--heap`, 3 GB by default) was exhausted inside Soot's Spark points-to solver after several minutes, or the container "
     "was killed (exit 137) because the runs in parallel needed more memory than Docker has. It happens with large jars, for example druid's "
     "`selfcontained` fat jar.",
     "A resource limit, not a defect: rerun with a larger `--heap` and fewer parallel jobs (druid finished with `--heap 12g --jobs 1`).",
     "resource limit"),
    ("unresolved-type", r"Unresolved type",
     "`RuntimeException: Unresolved type ...` (`ioa`, Spark)",
     "Spark met a class it could not resolve. The runner gives `ioa` the one jar that contains the analysed class, but the class it needs "
     "can live in another jar of the same build (elasticsearch `0404db65`: `org.elasticsearch.common.inject.Module` is in "
     "`elasticsearch-3.0.0-SNAPSHOT.jar`, not in `framework-3.0.0-SNAPSHOT.jar`).",
     "A limit of the single-jar input (the study's script has it too). With one jar combining both, the run finishes.",
     "single-jar input"),
]


def classify(console: str, exit_code: int | None) -> str:
    for key, pattern, *_ in CAUSES:
        if re.search(pattern, console) or (key == "oom" and exit_code == 137):
            return key
    return "other"


def _console_tail(console: str) -> str:
    lines = [line for line in console.splitlines() if line.strip() and not line.startswith("SLF4J")]
    return " | ".join(lines[-2:])[:200]


def collect(analysis_dir: Path) -> tuple[list[dict], Counter]:
    errors, states = [], Counter()
    for status_path in sorted(analysis_dir.glob("*/*/*/*/status.json")):
        status = json.loads(status_path.read_text(encoding="utf-8"))
        states[status["state"]] += 1
        if status["state"] != "error":
            continue
        console_path = status_path.parent / "console.txt"
        console = console_path.read_text(encoding="utf-8", errors="replace") if console_path.is_file() else ""
        scenario, jar, direction, mode = status_path.parts[-5:-1]
        errors.append({
            "scenario": scenario, "jar": jar, "direction": direction, "mode": mode,
            "seconds": status.get("seconds"), "cause": classify(console, status.get("exit_code")),
            "tail": _console_tail(console),
        })
    return errors, states


def build_errors_report(analysis_dir: Path) -> str:
    errors, states = collect(analysis_dir)
    by_cause: dict[str, list[dict]] = defaultdict(list)
    for e in errors:
        by_cause[e["cause"]].append(e)
    excluded = sorted(
        (s.name, t) for s in analysis_dir.iterdir() if s.is_dir() for t in TYPES if not load_tool(analysis_dir, s.name)[t]["complete"]
    )
    state_text = ", ".join(f"{n} {state}" for state, n in sorted(states.items()))

    out = ["# Errors of the static-analysis runs", ""]
    out.append(f"{sum(states.values())} runs: {state_text}. {len(errors)} ended in `error`: the tool exited with a Java exception or a "
               "non-zero code. The comparison leaves out a (scenario, type) pair when any run of that analysis errored, since the tool's "
               f"side would be incomplete: {len(excluded)} pairs"
               + (f" ({', '.join(f'{sum(1 for _, t in excluded if t == k)} {k}' for k in TYPES if any(t == k for _, t in excluded))})." if excluded else "."))
    out.append("")
    if not errors:
        out.append("No run ended in error.")
        return "\n".join(out) + "\n"

    out += ["## Summary", "", "| Cause | Runs | Scenarios | Analysis | Where the problem is |", "|---|---|---|---|---|"]
    for key, _, title, _, _, where in CAUSES:
        items = by_cause.get(key, [])
        if items:
            modes = sorted({TYPE_OF_MODE.get(e["mode"], e["mode"]) for e in items})
            out.append(f"| {title} | {len(items)} | {len({e['scenario'] for e in items})} | {', '.join(modes)} | {where} |")
    if by_cause.get("other"):
        out.append(f"| Other (not classified) | {len(by_cause['other'])} | {len({e['scenario'] for e in by_cause['other']})} | | unknown |")
    out.append("")

    for key, _, title, explanation, remedy, _ in CAUSES:
        items = by_cause.get(key, [])
        if not items:
            continue
        out += [f"## {title}", "", f"{len(items)} runs in {len({e['scenario'] for e in items})} scenarios.", "",
                f"**Explanation.** {explanation}", "", f"**What can be done.** {remedy}", "",
                "| Scenario | Jar | Direction | Analysis | Seconds |", "|---|---|---|---|---|"]
        out += [f"| {e['scenario']} | {e['jar']} | {e['direction']} | {TYPE_OF_MODE.get(e['mode'], e['mode'])} ({e['mode']}) | {e['seconds']} |" for e in items]
        out.append("")
    if by_cause.get("other"):
        out += ["## Not classified", "", "| Scenario | Jar | Direction | Analysis | Last console lines |", "|---|---|---|---|---|"]
        out += [f"| {e['scenario']} | {e['jar']} | {e['direction']} | {TYPE_OF_MODE.get(e['mode'], e['mode'])} | {e['tail']} |" for e in by_cause["other"]]
        out.append("")

    out += ["## (scenario, type) pairs left out of the comparison", "", "| Scenario | Type left out |", "|---|---|"]
    out += [f"| {s} | {t} |" for s, t in excluded]
    return "\n".join(out) + "\n"
