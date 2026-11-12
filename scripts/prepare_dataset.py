"""Converts the MergeDataSet into per-scenario inputs for this pipeline.

A scenario is one merge commit with at least one row in
`<dataset>/semantic-conflicts/sample-semantic-conflicts.csv` (the per-method
"Locally Observable Interference" labels). For each one this writes, under
`<out>/<project>_<commit[:8]>/`:

- `diff.txt`            unified diff base -> merge, one file per labeled class
- `modified-lines.txt`  per-class Left/Right modified lines (merge numbering)
- `source/`             the merged version of those classes, laid out as
                        `src/main/java/<package>/<Class>.java` (use as
                        `input.source_root`)
- `scenario.json`       the labeled methods and their labels

The diffs come from the dataset's own `<project>/<commit>/source/<class>/
{base,left,right,merge}.java` files, so no clone of the upstream project is
needed. The Left/Right modified lines are always computed, never taken from the
dataset's own results, with the study's own algorithm (ported from
miningframework's modifiedLinesCollector; needs `--diffj`, plus `java` and `diff`
on PATH): DiffJ says which lines each method changed, plain diff gives those
lines' text, and a merge line is Left's or Right's if its text is among that
developer's changed lines in the same method. `--validate` checks the port
against the dataset's own lines for the static-analysis study's 16 methods (all
match). If DiffJ fails on a class (it parses Java only up to 1.6) or no jar is
given, a line-based approximation is used for it (11 of 16 exact on those methods).
The dataset's own `changed-methods/<class>/<method>/left-right-lines.csv` of the commit
has priority: a labeled method that has one takes its Left/Right lines from it
(`<class>,<source|sink>,<line>`, merge numbering; in this file Left's lines are the sink
and Right's the source), and the algorithms above run only for the methods without one.
Those methods are listed after preparing.

After preparing, the labeled methods that ended up with no attributed lines are
listed with the reason, since the runner skips scenarios without any.

`--methods-csv` takes the scenarios, methods and their Left/Right lines from another CSV
(`project;merge commit;className;method;left modifications;right modifications;left deletions;right deletions`)
in place of the sample CSV and of the algorithms: nothing else of the dataset's CSVs is read, the
methods get no label ('-'), and a method the CSV lists no line for is reported as unresolved.

Usage:
    python -m scripts.prepare_dataset --dataset D:/.../mergedataset --out dataset
"""

from __future__ import annotations

import argparse
import ast
import csv
import difflib
import glob
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.javaspans import (
    CONSTRUCTOR_NAMES,
    MethodSpan,
    find_method,
    index_methods,
    parse_declaration,
)

RESULTS_CSV = Path("studies") / "static-analysis-results" / "with-dependencies" / "data" / "results.csv"
SAMPLE_CSV = Path("semantic-conflicts") / "sample-semantic-conflicts.csv"
LOI_COLUMN = "Locally Observable Interference"
VERSIONS = ("base", "left", "right", "merge")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def load_labeled_methods(dataset: Path) -> dict[tuple[str, str], list[dict[str, str]]]:
    """{(project, commit): [{class, declaration, label}]} from
    sample-semantic-conflicts.csv, one entry per labeled method. The file's
    columns are space-padded, hence the stripping; its method signatures use
    '|' where Java uses ',' between parameters, restored here. `project` is the
    dataset's folder name (last segment of the project URL)."""
    methods: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with (dataset / SAMPLE_CSV).open(newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
            project = row["Project"].rstrip("/").rsplit("/", 1)[-1]
            methods[(project, row["Commit"])].append(
                {
                    "class": row["Class"],
                    "declaration": row["Declaration"].replace("|", ","),
                    "label": row[LOI_COLUMN],
                }
            )
    return methods


def load_methods_csv(path: Path) -> dict[tuple[str, str], list[dict]]:
    """Same shape as `load_labeled_methods`, from a semicolon-separated CSV with the columns
    `project, merge commit, className, method, left modifications, right modifications,
    left deletions, right deletions` (e.g. results-with-build-information.csv), and nothing
    else: no sample CSV, so there is no label ('-') and the project is spelled as in the CSV.
    The line columns (merge numbering) are the Left/Right lines of the method, kept in
    `lines` as [left added, left deleted, right added, right deleted]; no algorithm runs."""
    methods: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in read_csv(path):
        methods[(row["project"].strip(), row["merge commit"].strip())].append(
            {
                "class": row["className"].strip(),
                "declaration": row["method"].strip(),
                "label": "-",
                "lines": [sorted(parse_lines(row[c])) for c in
                          ("left modifications", "left deletions", "right modifications", "right deletions")],
            }
        )
    return methods


def apply_labels_csv(methods: dict[tuple[str, str], list[dict]], path: Path) -> list[str]:
    """Set the label of every method from a comma-separated CSV with the columns `project, class,
    method, merge commit, Locally Observable Interference` (e.g. matheus-results-99units-mergedataset.csv),
    matched on (project, commit, class, method) ignoring case in the project and whitespace in the
    method. Returns a description of each method that got no label (no row, or an empty value)."""
    def norm(s: str) -> str:
        return re.sub(r"\s+", "", s)

    given: dict[tuple[str, str, str, str], str] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = (row["project"].strip().lower(), row["merge commit"].strip(), row["class"].strip(), norm(row["method"]))
            given[key] = row[LOI_COLUMN].strip()
    unlabeled: list[str] = []
    for (project, commit), rows in methods.items():
        for row in rows:
            label = given.get((project.lower(), commit, row["class"], norm(row["declaration"])), "")
            if label in ("Yes", "No"):
                row["label"] = label
            else:
                unlabeled.append(f"{project}_{commit[:8]} {row['class']}.{row['declaration']}")
    return unlabeled


def scenario_label(class_labels: dict[str, str]) -> str:
    """'Yes' if any class of the scenario has locally observable
    interference, else 'No' if any is labeled, else '-' (or '' if unlisted)."""
    values = set(class_labels.values())
    if "Yes" in values:
        return "Yes"
    if "No" in values:
        return "No"
    return "-" if values else ""


def parse_lines(value: str) -> list[int]:
    return [int(n) for n in ast.literal_eval(value or "[]")]


def make_diff(rel_path: str, base: str, merge: str) -> str:
    diff = difflib.unified_diff(
        base.splitlines(keepends=True),
        merge.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
    )
    body = "".join(line if line.endswith("\n") else line + "\n" for line in diff)
    return f"diff --git a/{rel_path} b/{rel_path}\n{body}" if body else ""


# --- locating a class's files -------------------------------------------------


def resolve_class(scenario_src: Path, csv_class: str) -> tuple[str, Path] | None:
    """Find the folder holding a labeled class's {base,left,right,merge}.java.

    Returns (clean class name, folder) or None. The CSV isn't always clean: some
    names carry a trailing method name ('pkg.Class.method'), and some classes
    were moved between packages, so the folder is looked up by the full name,
    then with trailing segments stripped, then by simple class name if that is
    unambiguous. The clean name keeps any '$Inner' suffix."""
    parts = csv_class.split(".")
    for strip in range(min(3, len(parts))):
        name = ".".join(parts[: len(parts) - strip])
        folder = scenario_src / name.split("$")[0].replace(".", "/")
        if (folder / "merge.java").is_file():
            return name, folder
    simple = csv_class.split("$")[0].rsplit(".", 1)[-1]
    hits = [
        Path(p)
        for p in glob.glob(str(scenario_src / "**" / simple), recursive=True)
        if (Path(p) / "merge.java").is_file()
    ]
    if len(hits) == 1:
        return csv_class, hits[0]
    return None


# --- computing Left/Right modified lines ---------------------------------------
#
# The merge version is what the line numbers refer to, so the base->merge diff is
# the oracle for *which* merge lines were modified, and the base->left and
# base->right diffs only say *who* modified them. Left's changed lines are carried
# onto the merge through the left->merge alignment (likewise for right), which
# absorbs the renumbering caused by the other developer's concurrent edits.

_BRACE_ONLY = re.compile(r"^[\s{}()\[\];,]*$|^\}\s*(else|catch|finally)\b.*\{\s*$")


def _norm(lines: list[str]) -> list[str]:
    return [re.sub(r"\s+", "", line) for line in lines]


def _added_lines(base: list[str], other: list[str]) -> set[int]:
    """1-based lines of `other` that differ from `base` (whitespace ignored)."""
    matcher = difflib.SequenceMatcher(None, _norm(base), _norm(other), autojunk=False)
    added: set[int] = set()
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            added.update(range(j1 + 1, j2 + 1))
    return added


def _carry_to_merge(other: list[str], merge: list[str], lines: set[int]) -> set[int]:
    """Map 1-based line numbers of `other` onto `merge`, dropping lines that
    didn't survive into the merge."""
    matcher = difflib.SequenceMatcher(None, _norm(other), _norm(merge), autojunk=False)
    carried: set[int] = set()
    for a, b, size in matcher.get_matching_blocks():
        carried.update(b + k + 1 for k in range(size) if a + k + 1 in lines)
    return carried


def _is_blank_or_comment(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith(("//", "/*", "*", "*/"))


def _is_brace(line: str) -> bool:
    s = line.strip()
    return bool(s) and bool(_BRACE_ONLY.match(s))


def _refine(lines: set[int], merge: list[str]) -> set[int]:
    """Keep the lines that carry meaning: drop blank and comment-only lines, and
    keep a brace-only line ('}', '} else {') only next to a real changed line of
    the same developer, i.e. when it closes a block that developer wrote."""
    real = {n for n in lines if not _is_blank_or_comment(merge[n - 1]) and not _is_brace(merge[n - 1])}
    braces = {n for n in lines if _is_brace(merge[n - 1]) and (n - 1 in real or n + 1 in real)}
    return real | braces


def compute_modified_lines(versions: dict[str, list[str]]) -> tuple[set[int], set[int]]:
    """(Left, Right) modified lines of the whole merged class, in merge numbering."""
    base, left, right, merge = (versions[v] for v in VERSIONS)
    oracle = _added_lines(base, merge)
    left_added, right_added = _added_lines(base, left), _added_lines(base, right)
    left_claim = _carry_to_merge(left, merge, left_added)
    right_claim = _carry_to_merge(right, merge, right_added)

    left_lines = left_claim & oracle
    right_lines = right_claim & oracle

    # Modified lines the alignment could not place (typically ambiguous
    # repeats): attribute them by content to whoever wrote that text.
    left_text = {n_ for n in left_added if (n_ := _norm([left[n - 1]])[0])}
    right_text = {n_ for n in right_added if (n_ := _norm([right[n - 1]])[0])}
    for n in oracle - left_claim - right_claim:
        text = _norm([merge[n - 1]])[0]
        if text in left_text:
            left_lines.add(n)
        if text in right_text:
            right_lines.add(n)

    # A line both developers claim can't be attributed to just one of them.
    both = left_lines & right_lines
    return _refine(left_lines - both, merge), _refine(right_lines - both, merge)


# --- The study's own algorithm (DiffJ + textual diff) --------------------------------
#
# Ported from miningframework's ModifiedLinesCollector (DiffJParser,
# TextualDiffParser, MethodModifiedLinesMatcher, ModifiedLine). For each of
# base->merge, base->left and base->right:
#
# - `diffj --brief` says, per method signature, which lines of the new file it
#   modified ('329a357: code added in decode(...)' -> decode(...): line 357).
# - plain `diff` says which lines were added/changed/removed, with their text.
# - A method's modified lines are the textual-diff lines sitting at the line
#   numbers DiffJ listed for it. They are kept in a set keyed by the line's
#   trimmed *text*, so repeated text ('}') counts once.
#
# A merge line is then Left's (Right's) if its text is among the left (right)
# method's modified lines: ownership is decided by content, not by line number,
# which is what makes it immune to renumbering. Removed lines go to the
# deletions column, everything else to the modifications column. The study only
# reports methods modified by both developers.


class DiffjError(RuntimeError):
    """DiffJ or diff failed on a pair of files (typically Java newer than
    DiffJ parses), or printed something the study's parsers don't understand."""


@dataclass(frozen=True)
class ModifiedLine:
    """Equal when the text is equal, as in the study: the number and kind don't count."""

    number: int = field(compare=False)
    content: str
    kind: str = field(compare=False)  # 'Added' | 'Changed' | 'Removed'


_METHOD_HEADER = re.compile(r"^.* method (added|changed|removed): .*")
_CODE_HEADER = re.compile(r"^.* code (added|changed|removed) in .*")
_TEXT_HEADER = re.compile(r"([0-9]+(,[0-9]+)?(a|c|d)[0-9]+(,[0-9]+)?)$")


def _run_lines(command: list[str]) -> list[str]:
    try:
        proc = subprocess.run(command, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiffjError(f"{command[0]}: {exc}") from exc
    # diff exits 1 when the files differ; only a failure to run is an error.
    if proc.returncode not in (0, 1) and not proc.stdout:
        raise DiffjError(proc.stderr.decode("utf-8", errors="replace").strip()[:200] or f"{command[0]} failed")
    return proc.stdout.decode("utf-8", errors="replace").splitlines()


def _new_range(header: str) -> tuple[int, int]:
    """'241,280d244' -> (244, 244); '329,330a357,359' -> (357, 359): the range on the new side."""
    parts = re.split(r"a|c|d", header)
    if len(parts) <= 1:
        raise DiffjError(f"cannot read a line range from {header!r}")
    try:
        numbers = [int(n) for n in parts[1].split(",")]
    except ValueError as exc:
        raise DiffjError(f"cannot read a line range from {header!r}") from exc
    return numbers[0], numbers[-1] if len(numbers) > 1 else numbers[0]


def parse_diffj_brief(lines: list[str]) -> dict[str, list[int]]:
    """{method signature: new-file lines DiffJ reports for it}, in output order."""
    result: dict[str, list[int]] = {}
    for line in lines[1:]:  # the first line names the two files
        method_match = _METHOD_HEADER.search(line)
        if not (method_match or _CODE_HEADER.search(line)):
            continue
        if ":" not in line:
            raise DiffjError(f"unexpected DiffJ line {line!r}")
        begin, end = _new_range(line.split(":")[0])
        if method_match:
            signature = re.sub(r"^.* method (added|changed|removed): ", "", line, count=1)
        else:
            signature = re.sub(r"^.* code (added|changed|removed) in ", "", line, count=1)
        result.setdefault(signature, []).extend(range(begin, end + 1))
    return result


def parse_textual_diff(lines: list[str]) -> list[ModifiedLine]:
    """The lines of a plain `diff` (normal format) as ModifiedLines numbered on
    the new side. A removal is numbered by the new-side position it was cut from
    and carries only its first removed line, as in the study."""
    result: list[ModifiedLine] = []
    iterator = iter(lines)
    try:
        for raw in iterator:
            line = raw.strip()
            if not _TEXT_HEADER.search(line):
                continue
            kind = "Added" if "a" in line else "Changed" if "c" in line else "Removed"
            begin, end = _new_range(line.split(" ")[0])
            if kind == "Changed":
                for skipped in iterator:  # the old lines, up to the '---' marker
                    if skipped.strip() == "---":
                        break
            for number in range(begin, end + 1):
                result.append(ModifiedLine(number, next(iterator)[1:].strip(), kind))
    except StopIteration as exc:
        raise DiffjError("truncated textual diff") from exc
    return result


def modified_lines_by_method(jar: Path, old: Path, new: Path) -> dict[str, dict[str, ModifiedLine]]:
    """{signature: {line text: ModifiedLine}} for old -> new, keeping the first
    line seen for each distinct text."""
    if shutil.which("diff") is None:
        raise DiffjError("no 'diff' executable on PATH")
    by_method = parse_diffj_brief(_run_lines(["java", "-jar", str(jar), "--brief", str(old), str(new)]))
    first_at: dict[int, ModifiedLine] = {}
    for line in parse_textual_diff(_run_lines(["diff", str(old), str(new)])):
        first_at.setdefault(line.number, line)

    result: dict[str, dict[str, ModifiedLine]] = {}
    for signature, numbers in by_method.items():
        lines: dict[str, ModifiedLine] = {}
        for number in numbers:
            if number in first_at:
                lines.setdefault(first_at[number].content, first_at[number])
        result[signature] = lines
    return result


def study_class_maps(folder: Path, jar: Path) -> dict[str, dict[str, dict[str, ModifiedLine]]]:
    """Run the study's diffs for one class: {'left'|'right'|'merge': by-method map}."""
    return {
        version: modified_lines_by_method(jar, folder / "base.java", folder / f"{version}.java")
        for version in ("merge", "left", "right")
    }


def study_method_lines(maps: dict, signature: str) -> tuple[set[int], set[int], set[int], set[int]]:
    """(left modified, left deleted, right modified, right deleted) lines of one
    method, in merge numbering. Empty unless both developers modified it."""
    merge, left, right = (maps[v].get(signature) for v in ("merge", "left", "right"))
    out: tuple[set[int], ...] = (set(), set(), set(), set())
    if merge is None or left is None or right is None:
        return out  # type: ignore[return-value]
    for content, line in merge.items():
        for side, (modified, deleted) in ((left, out[0:2]), (right, out[2:4])):
            if content in side:
                (deleted if line.kind == "Removed" else modified).add(line.number)
    return out  # type: ignore[return-value]


def resolve_signatures(
    declaration: str, class_name: str, signatures: set[str], mutually_modified: set[str] = frozenset()
) -> list[str]:
    """The DiffJ signature(s) a labeled declaration refers to: an exact match, else
    by name, arity and parameter types (the dataset spells constructors 'constructor').

    A constructor label is often loose: 'constructor()' for a class whose only
    changed constructor takes arguments. When a constructor declaration matches no
    signature by arity, it falls back to the constructors in `mutually_modified`
    (the signatures modified by left, right and merge)."""
    if declaration in signatures:
        return [declaration]
    simple_name = class_name.split("$")[-1].rsplit(".", 1)[-1]
    name, params = parse_declaration(declaration)
    is_constructor = name in CONSTRUCTOR_NAMES or name == simple_name
    if name in CONSTRUCTOR_NAMES:
        name = simple_name
    same_name = [s for s in signatures if parse_declaration(s)[0] == name and len(parse_declaration(s)[1]) == len(params)]
    exact = [s for s in same_name if parse_declaration(s)[1] == params]
    if exact or same_name:
        return exact or same_name
    if is_constructor:
        return sorted(s for s in mutually_modified if parse_declaration(s)[0] == simple_name)
    return []


# In `changed-methods/<class>/<method>/left-right-lines.csv` the rows are `<class>,<source|sink>,<line>`, with
# the lines of the merged file. In the left->right analysis Left's lines are the sink and Right's the
# source (checked on the prepared scenarios: 77 of 77 methods fit this, none the opposite).
LEFT_KIND, RIGHT_KIND = "sink", "source"


def _method_key(declaration: str) -> str:
    # On Windows, git stores the characters a file name can't hold (`<`, `>`, `|`, `*`, `?`, `:`, `"`) in
    # the private-use area (U+F0xx): map them back.
    declaration = re.sub("[-]", lambda m: chr(ord(m.group()) - 0xF000), declaration)
    return re.sub(r"\s+", "", declaration).replace("|", ",")


def changed_methods_lines(commit_dir: Path, csv_class: str, class_name: str, declaration: str) -> tuple[set[int], set[int]] | None:
    """Fallback for the Left/Right lines of one labeled method: the dataset's own
    `changed-methods/**/left-right-lines.csv` of the commit. Returns (left lines, right lines),
    or None when no such file matches the method (the folder of the class, then the folder of
    the method, are matched by name)."""
    root = commit_dir / "changed-methods"
    if not root.is_dir():
        return None
    wanted_classes = {csv_class, class_name}
    wanted_key = _method_key(declaration)
    simple = class_name.split(".")[-1].split("$")[-1]
    found: set[Path] = set()
    for csv_path in root.glob("*/*/left-right-lines.csv"):
        class_dir, method_dir = csv_path.parent.parent.name, csv_path.parent.name
        # a label can carry the method name after the class (`pkg.Bootstrap.Bootstrap`)
        if not any(w == class_dir or w.startswith((class_dir + ".", class_dir + "$")) for w in wanted_classes):
            continue
        key = _method_key(method_dir)
        if key == wanted_key or (wanted_key.startswith("constructor(") and key == simple + wanted_key[len("constructor"):]):
            found.add(csv_path)
    if not found:
        return None
    left: set[int] = set()
    right: set[int] = set()
    for csv_path in found:
        for row in read_csv_rows(csv_path):
            if len(row) != 3 or not row[2].strip().isdigit():
                continue
            kind, line = row[1].strip(), int(row[2])
            if kind == LEFT_KIND:
                left.add(line)
            elif kind == RIGHT_KIND:
                right.add(line)
    return (left, right) if left or right else None


def read_csv_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        return [row for row in csv.reader(f)]


def restrict_to_spans(lines: set[int], spans: list[MethodSpan]) -> set[int]:
    return {n for n in lines if any(s.start <= n <= s.end for s in spans)}


def validate_against_study(dataset: Path, diffj: Path) -> None:
    """Run the port on every method of the static-analysis study and compare its
    four line columns with the dataset's own results.csv."""
    columns = ("left modifications", "left deletions", "right modifications", "right deletions")
    exact = total = 0
    cache: dict[Path, dict] = {}
    for row in read_csv(dataset / RESULTS_CSV):
        folder = dataset / row["project"] / row["merge commit"] / "source" / row["className"].replace(".", "/")
        if not all((folder / f"{v}.java").is_file() for v in VERSIONS):
            continue
        try:
            maps = cache.setdefault(folder, study_class_maps(folder, diffj))
        except DiffjError as exc:
            print(f"  {row['project']}/{row['className'].rsplit('.', 1)[-1]}: DiffJ failed ({exc})")
            continue
        got = study_method_lines(maps, row["method"])
        truth = tuple(set(parse_lines(row[c])) for c in columns)
        total += 1
        exact += got == truth
        if got != truth:
            print(
                f"  {row['project']}/{row['className'].rsplit('.', 1)[-1]}.{row['method'].split('(')[0]}: "
                + " ".join(
                    f"{c.split()[0][0].upper()}{c.split()[1][0]} +{sorted(g - t)} -{sorted(t - g)}"
                    for c, g, t in zip(columns, got, truth)
                    if g != t
                )
            )
    print(f"validation: {exact}/{total} methods match the dataset's lines exactly")


# --- writing a scenario ---------------------------------------------------------


def _write_scenario(
    out_dir: Path,
    project: str,
    commit: str,
    diffs: list[str],
    blocks: list[str],
    labeled_methods: list[dict[str, str]],
) -> None:
    class_labels = {m["class"]: m["label"] for m in labeled_methods}
    (out_dir / "diff.txt").write_text("".join(diffs), encoding="utf-8")
    (out_dir / "modified-lines.txt").write_text("\n".join(blocks), encoding="utf-8")
    (out_dir / "scenario.json").write_text(
        json.dumps(
            {
                "project": project,
                "commit": commit,
                "locally_observable_interference": scenario_label(class_labels),
                "interference_by_class": class_labels,
                "labeled_methods": labeled_methods,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _lines_block(class_name: str, la: set[int], ld: set[int], ra: set[int], rd: set[int]) -> str:
    return (
        f"Class: {class_name}\n"
        f"Left added lines: {sorted(la)}\n"
        f"Left deleted lines: {sorted(ld)}\n"
        f"Right added lines: {sorted(ra)}\n"
        f"Right deleted lines: {sorted(rd)}\n"
    )


def _write_class_source(out_dir: Path, class_name: str, merge: str) -> str:
    """Write the merged class under source/ and return its repo-relative path."""
    rel_path = f"src/main/java/{class_name.split('$')[0].replace('.', '/')}.java"
    target = out_dir / "source" / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(merge, encoding="utf-8")
    return rel_path


def prepare_scenario(
    dataset: Path,
    out_dir: Path,
    project: str,
    commit: str,
    labeled_methods: list[dict[str, str]],
    diffj: Path | None,
    unresolved: list[dict[str, str]],
    from_csv: list[dict[str, str]] | None = None,
) -> bool:
    """Write one scenario's inputs, computing the Left/Right lines of its labeled
    methods. With `diffj`, the study's own algorithm is used; if it fails on a
    class (or no jar is given), the line-based algorithm is used for that class
    instead.

    A labeled method that ends up with no attributed lines is appended to
    `unresolved` with the reason, as are all methods of a scenario that can't be
    prepared. A method the algorithms can't attribute takes its lines from the commit's
    `changed-methods/**/left-right-lines.csv` when there is one (noted in `from_csv`). Returns whether the scenario was written."""
    name = f"{project}_{commit[:8]}"
    if from_csv is None:
        from_csv = []

    def give_up(rows: list[dict[str, str]], reason: str) -> None:
        for row in rows:
            unresolved.append({"scenario": name, "commit": commit, "class": row["class"],
                               "declaration": row["declaration"], "label": row["label"], "reason": reason})

    scenario_src = dataset / project / commit / "source"
    if not scenario_src.is_dir():
        give_up(labeled_methods, "the dataset has no source/ folder for this commit")
        return False

    rows_by_class: dict[str, list[dict[str, str]]] = defaultdict(list)
    folders: dict[str, Path] = {}
    for row in labeled_methods:
        if "lines" in row and not any(row["lines"]):
            give_up([row], "the CSV lists no modified lines for it")
            continue
        resolved = resolve_class(scenario_src, row["class"])
        if resolved is None:
            give_up([row], "no base/left/right/merge files found for the class")
            continue
        clean_name, folder = resolved
        folders[clean_name] = folder
        rows_by_class[clean_name].append({**row, "class": clean_name, "csv_class": row["class"]})

    diffs: list[str] = []
    blocks: list[str] = []
    kept: list[dict[str, str]] = []
    for clean_name, rows in rows_by_class.items():
        folder = folders[clean_name]

        # The dataset's own changed-methods CSVs have priority: a method that has one takes its
        # Left/Right lines from it, and only the others are computed.
        csv_left: set[int] = set()
        csv_right: set[int] = set()
        given: tuple[set[int], set[int], set[int], set[int]] = (set(), set(), set(), set())  # lines the CSV lists
        todo: list[dict[str, str]] = []
        for row in rows:
            if "lines" in row:
                for total, part in zip(given, row["lines"]):
                    total.update(part)
                continue
            found = changed_methods_lines(dataset / project / commit, row["csv_class"], clean_name, row["declaration"])
            if found is None:
                todo.append(row)
                continue
            csv_left.update(found[0])
            csv_right.update(found[1])
            from_csv.append({"scenario": name, "class": clean_name, "declaration": row["declaration"]})

        missing = [v for v in VERSIONS if not (folder / f"{v}.java").is_file()]
        reason = f"the class folder lacks {', '.join(v + '.java' for v in missing)}"
        if {"base", "merge"} & set(missing):  # the diff and the source written below need these two
            give_up(rows, reason)
            from_csv[:] = [c for c in from_csv if (c["scenario"], c["class"]) != (name, clean_name)]
            continue
        if missing and todo:  # computing the lines needs all four versions
            give_up(todo, reason)
            todo = []
        texts = {v: (folder / f"{v}.java").read_text(encoding="utf-8", errors="replace") for v in VERSIONS if (folder / f"{v}.java").is_file()}

        lines: tuple[set[int], set[int], set[int], set[int]] | None = None if todo else (set(), set(), set(), set())
        if diffj and todo:
            try:
                maps = study_class_maps(folder, diffj)
            except DiffjError as exc:
                print(f"  warning: DiffJ failed on {clean_name} ({exc}); using the line-based fallback", file=sys.stderr)
            else:
                signatures = set().union(*(m.keys() for m in maps.values()))
                lines = (set(), set(), set(), set())
                for row in todo:
                    found = resolve_signatures(
                        row["declaration"], clean_name, signatures,
                        {s for s in signatures if all(s in maps[v] for v in ("left", "right", "merge"))},
                    )
                    if not found:
                        give_up([row], "DiffJ reports no method matching the declaration")
                        continue
                    if any(len(parse_declaration(s)[1]) != len(parse_declaration(row["declaration"])[1]) for s in found):
                        # The label was resolved loosely (different parameter count): keep
                        # the signatures so the evaluation looks at the same method.
                        row["resolved_signatures"] = found
                    own = (set(), set(), set(), set())
                    for signature in found:
                        for total, part in zip(own, study_method_lines(maps, signature)):
                            total.update(part)
                    if not any(own):
                        modified_by = {v: any(sig in maps[v] for sig in found) for v in ("left", "right")}
                        if not all(modified_by.values()):
                            who = " nor ".join(v for v, hit in modified_by.items() if not hit)
                            give_up([row], f"not modified by {who} according to DiffJ")
                        else:
                            give_up([row], "modified by both, but couldn't attribute modified lines")
                    for total, part in zip(lines, own):
                        total.update(part)
        if lines is None:
            try:
                all_spans = index_methods(texts["merge"])
            except Exception as exc:  # noqa: BLE001 - javalang raises assorted parse errors
                give_up(todo, f"DiffJ failed and the class can't be parsed for the fallback ({exc})")
                lines = (set(), set(), set(), set())
            else:
                spans: list[MethodSpan] = []
                for row in todo:
                    found_spans = find_method(all_spans, clean_name, row["declaration"])
                    if not found_spans:
                        give_up([row], "method not found in the merged source (fallback)")
                    spans.extend(found_spans)
                left, right = compute_modified_lines({v: t.splitlines() for v, t in texts.items()})
                lines = (restrict_to_spans(left, spans), set(), restrict_to_spans(right, spans), set())
                if not any(lines):
                    give_up(todo, "no modified lines (fallback)")
        lines = (lines[0] | csv_left | given[0], lines[1] | given[1], lines[2] | csv_right | given[2], lines[3] | given[3])

        rel_path = _write_class_source(out_dir, clean_name, texts["merge"])
        diffs.append(make_diff(rel_path, texts["base"], texts["merge"]))
        blocks.append(_lines_block(clean_name, *lines))
        kept.extend(rows)

    if not blocks:
        return False
    _write_scenario(out_dir, project, commit, diffs, blocks, kept)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", required=True, help="Path to the MergeDataSet checkout.")
    parser.add_argument("--out", default="dataset", help="Output directory (default: dataset).")
    parser.add_argument(
        "--methods-csv",
        help="Take the scenarios, methods and Left/Right lines from this CSV (';'-separated, columns "
        "project, merge commit, className, method, left/right modifications, left/right deletions) "
        "instead of the dataset's sample-semantic-conflicts.csv and the line algorithms.",
    )
    parser.add_argument(
        "--labels-csv",
        help="With --methods-csv: take each method's 'Locally Observable Interference' label from this CSV "
        "(columns project, class, method, merge commit, Locally Observable Interference) instead of '-'.",
    )
    parser.add_argument(
        "--diffj",
        help="Path to the study's DiffJ jar (needs `java` and `diff` on PATH). Used to "
        "compute the Left/Right modified lines, and required by --validate; without it "
        "a line-based approximation is used.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Only check the study's algorithm (needs --diffj) against the "
        "static-analysis study's own results, and exit.",
    )
    args = parser.parse_args(argv)

    dataset = Path(args.dataset)
    out_root = Path(args.out)
    if args.validate:
        if not args.diffj:
            parser.error("--validate needs --diffj")
        validate_against_study(dataset, Path(args.diffj))
        return 0

    labels = load_methods_csv(Path(args.methods_csv)) if args.methods_csv else load_labeled_methods(dataset)
    if args.labels_csv:
        if not args.methods_csv:
            parser.error("--labels-csv needs --methods-csv")
        unlabeled = apply_labels_csv(labels, Path(args.labels_csv))
        print(f"Methods without a label in {args.labels_csv}: {len(unlabeled)}")
        for item in unlabeled:
            print(f"  {item}")
    out_root.mkdir(parents=True, exist_ok=True)
    jar =Path(args.diffj) if args.diffj else None

    unresolved: list[dict[str, str]] = []
    from_csv: list[dict[str, str]] = []
    ok = 0
    for (project, commit), methods in labels.items():
        out_dir = out_root / f"{project}_{commit[:8]}"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        print(f"{project} {commit[:8]}")
        if prepare_scenario(dataset, out_dir, project, commit, methods, jar, unresolved, from_csv):
            ok += 1
        else:
            shutil.rmtree(out_dir)

    # Drop stale scenarios from earlier runs that this one no longer produces.
    wanted = {f"{p}_{c[:8]}" for p, c in labels}
    for stale in (d for d in out_root.iterdir() if d.is_dir() and d.name not in wanted):
        shutil.rmtree(stale)

    total = sum(len(m) for m in labels.values())
    print(f"\nPrepared {ok}/{len(labels)} scenarios in {out_root}")
    print(f"Methods whose lines came from the dataset's changed-methods CSVs: {len(from_csv)}")
    for item in from_csv:
        print(f"  {item['scenario']:<36} {item['class'].rsplit('.', 1)[-1]}.{item['declaration']}")
    print(f"Labeled methods without attributed lines: {len(unresolved)} of {total}")
    for item in sorted(unresolved, key=lambda u: (u["scenario"], u["class"], u["declaration"])):
        print(
            f"  [{item['label']:>3}] {item['scenario']:<36} {item['commit']}  "
            f"{item['class'].rsplit('.', 1)[-1]}.{item['declaration'] or '(empty declaration)'} -> {item['reason']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
