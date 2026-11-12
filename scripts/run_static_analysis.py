"""Runs the conflict-static-analysis tool on the prepared scenarios, in Docker.

For every runnable scenario of `dataset/` (the ones the agents run on), the tool
is run with the three analyses compared against the agents:

    DF  Direct Flow           -mode svfa-interprocedural
    CF  Confluence Flow       -mode dfp-confluence-interprocedural
    OA  Overriding Assignment -mode ioa

each in both directions (left -> right and right -> left), because the analyses
follow data flow from sources to sinks. This follows how the study ran the tool
(miningframework's run_analysis.sh):

- `-cp` is the jar of the scenario's `original-without-dependencies/merge` build
  that contains the class. Unlike the study's grep over `jar tf`, only a `.class`
  entry counts, so `*-sources.jar` is never picked. If the scenario's classes are
  in different jars they are analysed in separate runs, since ioa takes a single
  jar. If no jar has the class, the build directory is passed instead.
- The changes CSV is `<class>,<source|sink>,<line>`. In `left-right` the left
  lines are the sinks and the right lines the sources; `right-left` swaps them
  (the convention of the dataset's own `left-right-lines.csv` files). Lines are
  also given for the class's nested classes found in the jar, because the tool
  matches lines per class and nested code reports its own class name.
- 30 minutes per run, as in the study; on a timeout the tool exports the partial
  results it has.

The tool is the one built from `--tool-dir` (default: the conflict-static-analysis
checkout), run inside the `conflict-analysis-runner` image (docker/Dockerfile:
the tool's own Docker environment, Ubuntu 22.04 + OpenJDK 8 + Maven). Results go
to `<results-dir>/<scenario>/<jar>/<direction>/<mode>/`: `changes.csv`,
`console.txt`, `status.json` and, when something was found, `out.json`.

Usage:
    python -m scripts.run_static_analysis --mergedataset D:/.../mergedataset --build-tool
    python -m scripts.run_static_analysis --mergedataset D:/.../mergedataset --jobs 3
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.parsing.modified_lines_parser import has_attributed_lines, load_modified_lines

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "conflict-analysis-runner"
DEFAULT_TOOL_DIR = Path(r"D:\Arquivos\Documentos\Mestrado\agentes\repos\conflict-static-analysis")
MODES = {
    "DF": "svfa-interprocedural",
    "CF": "dfp-confluence-interprocedural",
    "OA": "ioa",
}
DIRECTIONS = ("left-right", "right-left")
BUILD_DIRS = ("original-without-dependencies", "original")

_ERROR_LINE = re.compile(
    r"^(Exception in thread|java\.[\w.]*(Exception|Error)|Caused by:|\s+at [\w.$<>]+\()", re.MULTILINE
)
_print_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


# --- the tool and the image ---------------------------------------------------------


def find_tool_jar(tool_dir: Path) -> Path | None:
    jars = sorted((tool_dir / "target").glob("soot-analysis-*-jar-with-dependencies.jar"))
    return jars[-1] if jars else None


@functools.lru_cache(maxsize=None)
def tool_version(tool_jar: Path) -> dict:
    """Which build a jar is: the commit and tag of the checkout it sits in (`<checkout>/target/`)
    and the jar's own SHA-256, recorded in every run's status.json so builds can't be mixed unnoticed."""
    repo = tool_jar.parent.parent

    def git(*args: str) -> str | None:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    return {
        "commit": git("rev-parse", "HEAD"),
        "describe": git("describe", "--tags", "--always"),  # nearest tag, e.g. article_ioa_2025-3-gad6b0a0
        "jar": tool_jar.name,
        "jar_sha256": hashlib.sha256(tool_jar.read_bytes()).hexdigest(),
    }


def ensure_image() -> None:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0:
        return
    log(f"Building the {IMAGE} image from docker/Dockerfile ...")
    subprocess.run(["docker", "build", "-t", IMAGE, str(ROOT / "docker")], check=True)


def build_tool(tool_dir: Path, settings: Path) -> Path:
    """Build the tool's jar-with-dependencies from its source, in the image
    (README: `mvn compile assembly:single`). Maven needs the GitHub packages
    credentials of `settings` (the tool's README explains how to create it); the
    container only sees it read-only."""
    ensure_image()
    log(f"Building {tool_dir} in Docker (a first build takes several minutes) ...")
    subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{tool_dir}:/src",
            "-v", f"{settings}:/root/.m2/settings.xml:ro",
            "-v", "conflict-analysis-m2:/root/.m2/repository",
            "-w", "/src", IMAGE,
            "mvn", "-B", "clean", "compile", "assembly:single", "-DskipTests",
        ],
        check=True,
    )
    jar = find_tool_jar(tool_dir)
    if jar is None:
        raise SystemExit("The build finished but produced no soot-analysis jar-with-dependencies.")
    return jar


# --- planning the runs ----------------------------------------------------------------


class JarIndex:
    """Class entries of the jars of a build directory, read once."""

    def __init__(self) -> None:
        self._entries: dict[Path, set[str]] = {}
        self.unreadable: list[Path] = []

    def entries(self, jar: Path) -> set[str]:
        if jar not in self._entries:
            try:
                with zipfile.ZipFile(jar) as zf:
                    self._entries[jar] = set(zf.namelist())
            except zipfile.BadZipFile:
                # e.g. a build the dataset stores in split parts (see its prepareDataset.sh)
                self.unreadable.append(jar)
                self._entries[jar] = set()
        return self._entries[jar]

    def jars(self, build_dir: Path) -> list[Path]:
        return sorted(
            p for p in build_dir.glob("*.jar") if not re.search(r"(sources|javadoc)\.jar$", p.name)
        )

    def locate(self, build_dirs: list[Path], class_name: str) -> Path | None:
        entry = class_name.split("$")[0].replace(".", "/") + ".class"
        for build_dir in build_dirs:
            for jar in self.jars(build_dir):
                if entry in self.entries(jar):
                    return jar
        return None

    def nested_classes(self, jar: Path, class_name: str) -> list[str]:
        prefix = class_name.split("$")[0].replace(".", "/") + "$"
        return sorted(e[:-6].replace("/", ".") for e in self.entries(jar) if e.startswith(prefix) and e.endswith(".class"))


@dataclass
class Job:
    scenario: str
    group: str
    direction: str
    mode_key: str
    classpath: str  # as seen inside the container
    csv_text: str
    workdir: Path

    @property
    def mode(self) -> str:
        return MODES[self.mode_key]

    @property
    def label(self) -> str:
        return f"{self.scenario}/{self.group} {self.direction} {self.mode_key}"


def _csv_rows(direction: str, lines_by_class: dict[str, tuple[list[int], list[int]]]) -> str:
    left_kind, right_kind = ("sink", "source") if direction == "left-right" else ("source", "sink")
    rows = []
    for class_names, (left, right) in lines_by_class.items():
        for name in class_names.split("|"):
            rows += [f"{name},{left_kind},{n}" for n in left] + [f"{name},{right_kind},{n}" for n in right]
    return "\n".join(rows) + "\n"


def plan_jobs(
    dataset_dir: Path,
    mergedataset: Path,
    results_dir: Path,
    only: list[str] | None = None,
    modes: list[str] | None = None,
) -> list[Job]:
    index = JarIndex()
    jobs: list[Job] = []
    results_dir, mergedataset = results_dir.resolve(), mergedataset.resolve()  # docker mounts need absolute paths
    for scenario_dir in sorted(p for p in dataset_dir.iterdir() if p.is_dir()):
        if only and not any(token in scenario_dir.name for token in only):
            continue
        scenario = json.loads((scenario_dir / "scenario.json").read_text(encoding="utf-8"))
        authorship = load_modified_lines(scenario_dir / "modified-lines.txt")
        if not has_attributed_lines(authorship):
            continue  # nothing attributed: the agents skip this scenario too
        classes = {
            name: (sorted(a.left_added), sorted(a.right_added))
            for name, a in authorship.items()
            if a.left_added or a.right_added
        }

        commit_dir = mergedataset / scenario["project"] / scenario["commit"]
        build_dirs = [commit_dir / b / "merge" for b in BUILD_DIRS if (commit_dir / b / "merge").is_dir()]
        container_root = f"/data/{scenario['project']}/{scenario['commit']}"

        # classes sharing a jar are analysed together; a class in no jar gets the build dir
        groups: dict[str, dict] = {}
        for name, lines in classes.items():
            jar = index.locate(build_dirs, name) if build_dirs else None
            if jar is not None:
                key = jar.name
                classpath = f"{container_root}/{jar.parent.parent.name}/merge/{jar.name}"
                names = "|".join(dict.fromkeys([name, name.split("$")[0], *index.nested_classes(jar, name)]))
            else:
                key = "build-dir"
                first = build_dirs[0] if build_dirs else commit_dir / BUILD_DIRS[0] / "merge"
                classpath = f"{container_root}/{first.parent.name}/merge"
                names = name
            group = groups.setdefault(key, {"classpath": classpath, "lines": {}})
            group["lines"][names] = lines

        for key, group in groups.items():
            for direction in DIRECTIONS:
                for mode_key in modes or list(MODES):
                    workdir = results_dir / scenario_dir.name / re.sub(r"[^A-Za-z0-9._-]+", "-", key) / direction / MODES[mode_key]
                    jobs.append(
                        Job(
                            scenario=scenario_dir.name,
                            group=key,
                            direction=direction,
                            mode_key=mode_key,
                            classpath=group["classpath"],
                            csv_text=_csv_rows(direction, group["lines"]),
                            workdir=workdir,
                        )
                    )
    return jobs


# --- running one job ---------------------------------------------------------------


def run_job(job: Job, tool_jar: Path, mergedataset: Path, timeout: int, heap: str, force: bool) -> dict:
    status_path = job.workdir / "status.json"
    if status_path.is_file() and not force:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["reused"] = True
        log(f"[analysis] {job.label}: skipped, result exists ({status['state']})")
        return status

    job.workdir.mkdir(parents=True, exist_ok=True)
    # The tool appends to an existing out.json, so a rerun must start without one.
    (job.workdir / "out.json").unlink(missing_ok=True)
    (job.workdir / "changes.csv").write_text(job.csv_text, encoding="utf-8")
    name = "sa-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{job.scenario}-{job.group}-{job.direction}-{job.mode_key}")[:100]
    command = [
        "docker", "run", "--rm", "--name", name,
        "-v", f"{mergedataset}:/data:ro",
        "-v", f"{job.workdir}:/work",
        "-v", f"{tool_jar}:/tool/tool.jar:ro",
        "-e", f"JAVA_TOOL_OPTIONS=-Xmx{heap}",
        "-w", "/work", IMAGE,
        "timeout", str(timeout), "java", "-jar", "/tool/tool.jar",
        "-cp", job.classpath, "-csv", "/work/changes.csv", "-mode", job.mode,
    ]
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    start = time.monotonic()
    try:
        proc = subprocess.run(command, capture_output=True, timeout=timeout + 120)
        exit_code, console = proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        exit_code, console = 124, "docker run did not return in time"
    seconds = round(time.monotonic() - start, 1)
    (job.workdir / "console.txt").write_text(console, encoding="utf-8")

    out_json = job.workdir / "out.json"
    if exit_code == 124:
        state = "timeout"
    elif exit_code != 0 or _ERROR_LINE.search(console.replace("Error getting the previous content", "")):
        state = "error"
    elif out_json.is_file():
        state = "findings"
    elif "No conflicts detected" in console:
        state = "no-conflicts"
    else:
        state = "error"
    status = {
        "state": state, "exit_code": exit_code, "seconds": seconds, "mode": job.mode,
        "direction": job.direction, "classpath": job.classpath, "has_out_json": out_json.is_file(),
        "started_at": started_at, "ended_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": tool_version(tool_jar),
    }
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    log(f"[analysis] {job.label}: {state} ({seconds}s)")
    return status


class BackgroundAnalysis:
    """Runs jobs on a thread pool, so the caller can do other work meanwhile."""

    def __init__(self, jobs: list[Job], tool_jar: Path, mergedataset: Path, workers: int, timeout: int, heap: str, force: bool):
        self.jobs = jobs
        self.statuses: dict[str, dict] = {}
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="analysis")
        self._futures = [
            (job, self._pool.submit(run_job, job, tool_jar, mergedataset, timeout, heap, force)) for job in jobs
        ]

    def wait(self) -> dict[str, dict]:
        for job, future in self._futures:
            try:
                self.statuses[job.label] = future.result()
            except Exception as exc:  # noqa: BLE001 - one failed job must not hide the others
                log(f"[analysis] {job.label}: crashed ({exc})")
                self.statuses[job.label] = {"state": "error", "error": str(exc)}
        self._pool.shutdown()
        return self.statuses


def summarize(statuses: dict[str, dict]) -> str:
    counts: dict[str, int] = {}
    for status in statuses.values():
        counts[status["state"]] = counts.get(status["state"], 0) + 1
    return ", ".join(f"{n} {state}" for state, n in sorted(counts.items())) or "no runs"


def start_background(
    dataset_dir: Path,
    mergedataset: Path,
    results_dir: Path,
    tool_dir: Path = DEFAULT_TOOL_DIR,
    only: list[str] | None = None,
    jobs: int = 2,
    timeout: int = 1800,
    heap: str = "3g",
    force: bool = False,
    modes: list[str] | None = None,
) -> BackgroundAnalysis:
    tool_jar = find_tool_jar(tool_dir)
    if tool_jar is None:
        raise SystemExit(
            f"No built tool jar in {tool_dir / 'target'}. Build it first: "
            "python -m scripts.run_static_analysis --mergedataset <path> --build-tool"
        )
    tool_jar, mergedataset = tool_jar.resolve(), mergedataset.resolve()
    ensure_image()
    planned = plan_jobs(dataset_dir, mergedataset, results_dir, only, modes)
    log(f"[analysis] {len(planned)} runs planned with {tool_jar.name}, {max(1, jobs)} at a time")
    return BackgroundAnalysis(planned, tool_jar, mergedataset, jobs, timeout, heap, force)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mergedataset", required=True, help="Path to the MergeDataSet checkout (its jars are analysed).")
    parser.add_argument("--dataset-dir", default="dataset", help="Output of prepare_dataset (default: dataset).")
    parser.add_argument("--results-dir", default="results/analysis", help="Where to write results (default: results/analysis).")
    parser.add_argument("--tool-dir", default=str(DEFAULT_TOOL_DIR), help="The conflict-static-analysis checkout.")
    parser.add_argument("--build-tool", action="store_true", help="Build the tool's jar in Docker first (needs ~/.m2/settings.xml).")
    parser.add_argument("--settings", default=str(Path.home() / ".m2" / "settings.xml"), help="Maven settings with the GitHub packages credentials (for --build-tool).")
    parser.add_argument("--only", nargs="+", help="Only scenarios whose folder name contains one of these strings.")
    parser.add_argument("--modes", nargs="+", choices=list(MODES), help="Only these analyses (default: DF CF OA).")
    parser.add_argument("--jobs", type=int, default=3, help="Runs at a time (default: 3).")
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds per run (default: 1800, as in the study).")
    parser.add_argument("--heap", default="3g", help="JVM heap per run (default: 3g).")
    parser.add_argument("--force", action="store_true", help="Run again even if a result exists.")
    args = parser.parse_args(argv)

    tool_dir = Path(args.tool_dir)
    if args.build_tool:
        build_tool(tool_dir, Path(args.settings))
    analysis = start_background(
        Path(args.dataset_dir), Path(args.mergedataset), Path(args.results_dir), tool_dir,
        args.only, args.jobs, args.timeout, args.heap, args.force, args.modes,
    )
    statuses = analysis.wait()
    log(f"[analysis] done: {summarize(statuses)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
