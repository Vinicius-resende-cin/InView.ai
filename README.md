# InView.ai

InView.ai is an agent-based pipeline that reviews a two-developer merge
scenario for **semantic dependencies**: cases where independent changes made
by two developers interact through data flow, even though they never touch
the same line — the kind of conflict a textual (git) merge cannot detect.

Given a diff and per-line authorship, the agent reads the full source files
involved (not just the diff hunks) and either detects these dependencies
itself, or reviews dependencies already reported by a static-analysis tool.

There are two ways to use it:

- **One scenario** — run the pipeline on a single diff and score the result
  against a static-analysis tool's output ([Usage](#usage)).
- **A whole dataset** — prepare the dataset, run several models and the
  static-analysis tool on every scenario, and compare them
  ([Dataset experiments](#dataset-experiments)).

## Index

- [Background](#background)
- [Modes](#modes)
- [How it works](#how-it-works)
  - [Agent backends](#agent-backends)
  - [Reading the real source, not just the diff](#reading-the-real-source-not-just-the-diff)
- [Installation](#installation)
  - [Using the Ollama provider](#using-the-ollama-provider)
- [Usage](#usage)
  - [Commands at a glance](#commands-at-a-glance)
  - [Running one scenario](#running-one-scenario)
  - [Mode 2 input format](#mode-2-input-format)
  - [Comparing one run with static analysis](#comparing-one-run-with-static-analysis)
  - [Running several models on one scenario](#running-several-models-on-one-scenario)
- [Dataset experiments](#dataset-experiments)
  - [1. Prepare the dataset](#1-prepare-the-dataset)
  - [2. Run the static-analysis tool](#2-run-the-static-analysis-tool)
  - [3. Run the agents](#3-run-the-agents)
  - [4. Score and compare](#4-score-and-compare)
  - [Re-running](#re-running)
- [Project structure](#project-structure)
- [Implementation notes](#implementation-notes)
- [License](#license)

## Background

Definitions are adapted from Jesus et al., *"Comparing static analyses for
improved semantic conflict detection"* (Automated Software Engineering,
2026). The pipeline targets three dependency types:

- **Direct Flow (DF)** — a value written or used in a line changed by one
  developer flows (through data flow) to a state element written or used in
  a line changed by the other.
- **Overriding Assignment (OA)** — a state element written by one developer is
  later overwritten by the other, with no intermediate write from the common
  base version, silently discarding one change's effect.
- **Confluence Flow (CF)** — data-flow paths from each developer's distinct
  changes converge at a common program point affecting the same state
  element, even though they modified different variables initially.

The full definitions used in prompts live in `config.yaml` under
`dependency_types`, so they can be edited without touching code.

## Modes

- **Mode 1 (`detect`)** — the agent finds semantic dependencies on its own
  from the annotated diff and source files.
- **Mode 2 (`review`)** — a static-analysis tool has already reported
  dependencies (`input.dependencies_file`); the agent assesses whether each
  one is a genuine risk or safe/benign, and may flag anything it noticed on
  its own.

The experiment scripts and the dataset workflow use Mode 1 only.

## How it works

The pipeline (`src/graph.py`) is a fixed sequence of four steps:

1. **`load_inputs`** — parses the diff, per-line authorship and, in Mode 2,
   the static-analysis findings (`src/parsing/`), and joins diff and
   authorship into an annotated diff.
2. **`build_prompt`** — renders the annotated diff, dependency-type
   definitions and (Mode 2) precomputed dependencies into a system/human
   prompt pair (`src/prompts/`).
3. **`call_agent`** — sends the prompt to an agent backend and gets back a
   structured, schema-validated result (`src/clients/`).
4. **`save_output`** — writes `{mode, result, raw_response, parse_error}` as
   JSON to `output.path` (`src/output.py`).

The result schema (`src/schemas.py`) is enforced by the backend itself, not
parsed out of free text afterwards.

### Agent backends

Two backends implement the same interface (`AgentClient`), selected with
`agent.backend` in `config.yaml`:

- **`opencode`** (default) — talks to an [opencode](https://opencode.ai)
  server. Supports any `llm.provider` (Ollama, Gemini, OpenRouter,
  Anthropic), each with its own credentials. `opencode.json` defines two
  read-only subagents (`read`/`grep`/`glob` only): `dependency-detector`
  (Mode 1) and `dependency-reviewer` (Mode 2). A skill in
  `.opencode/skills/semantic-dependency-review/` tells the agent to read the
  real source files and to finish by calling the `StructuredOutput` tool.

- **`claude_code`** — runs the `claude` CLI headlessly (`claude -p ...`).
  This is the only integration allowed to use a Claude Pro/Max
  **subscription** login instead of a pay-per-token API key: Anthropic's
  terms limit subscription auth to its own first-party clients, which rules
  out opencode and the Agent SDK. The skill lives in
  `.claude/skills/semantic-dependency-review/` and is discovered
  automatically. `llm.provider` is ignored; it always talks to Claude.

Both backends retry (a fresh attempt each time, up to `agent.max_retries`
extra attempts) when a run doesn't end with schema-valid structured output,
since models vary in how reliably they do that.

### Reading the real source, not just the diff

Set `input.source_root` to a checkout of the code the diff applies to, and
the agent's read/grep/glob tools are scoped to that directory, so it can open
full files instead of reasoning from the hunk alone (which often omits the
declarations, other methods or superclasses a dependency depends on). Leave
it unset to fall back to diff-only reasoning.

## Installation

1. Install the Python dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Set up **one** agent backend (`agent.backend`); skip the other.

   **Option A — `opencode`** (API keys, many providers)

   - Install [opencode](https://opencode.ai/docs) and Node.js.
   - Sync this project's agents and skills into opencode's global config
     (see [Implementation notes](#implementation-notes) for why):
     ```
     python scripts/install_opencode_config.py
     ```
     Re-run it after editing `opencode.json` or `.opencode/skills/`.
   - Log in to the provider you'll use:
     `opencode auth login --provider <gemini|openrouter|anthropic>`.
     Ollama has its own login, below.

   **Option B — `claude_code`** (Claude Pro/Max subscription)

   - Install [Claude Code](https://code.claude.com/docs/quickstart).
   - Log in once with `claude`, or on a headless machine with
     `claude setup-token`.

The [dataset experiments](#dataset-experiments) need a few more tools; they
are listed there.

### Using the Ollama provider

`llm.provider: ollama` works with the `opencode` backend only. Use it to
avoid per-token costs, keep the code on your machine, or try open-weight
models.

1. Install Ollama and `ollama pull <tag>` the model.
2. Set `llm.provider: ollama` and `llm.model: <tag>` in `config.yaml`.
3. For `*-cloud` tags, log in with `ollama signin` (Ollama's own login,
   unrelated to `opencode auth`). Fully local models need no login.

`opencode.json` already registers `gemma4:31b-cloud`, `gpt-oss:20b-cloud`,
`gpt-oss:120b-cloud` and `nemotron-3-super:cloud`. Any other tag must be
added under `provider.ollama.models` (the key is the exact tag from
`ollama list`; `name` is only a label):

```json
"provider": { "ollama": { "models": {
  "qwen2.5-coder:14b": { "name": "Qwen2.5 Coder 14B" }
} } }
```

Then run `ollama pull <tag>` (also needed for `*-cloud` tags, which registers
the alias without downloading weights), re-run
`python scripts/install_opencode_config.py`, restart any running
`opencode serve`, and set `llm.model` to the tag.

## Usage

### Commands at a glance

All commands run from the repository root.

| Command | What it does |
|---|---|
| `python -m src.cli` | Runs the pipeline on one scenario |
| `python -m src.compare` | Scores one Mode 1 output against the tool's findings |
| `python -m scripts.run_experiment` | Runs several models N times on one scenario |
| `python scripts/install_opencode_config.py` | Syncs `opencode.json` and skills into opencode's global config |
| `python -m scripts.prepare_dataset` | Turns the MergeDataset into per-scenario inputs |
| `python -m scripts.run_static_analysis` | Runs the static-analysis tool on those scenarios, in Docker |
| `python -m scripts.run_dataset_experiment` | Runs the agents on every scenario (optionally with the tool) |
| `python -m scripts.evaluate_by_method` | Scores finished runs per labeled method |
| `python -m scripts.show_method_summary` | Prints that per-method score as a table |
| `python -m scripts.compare_analysis` | Compares the tool and the agents (no ground truth) |
| `python -m scripts.compare_ground_truth` | Scores both against the manual analysis |

Every command accepts `--help`.

### Running one scenario

Edit `config.yaml`:

```yaml
mode: detect                # or: review
llm:
  provider: anthropic       # ollama | gemini | openrouter | anthropic
  model: sonnet             # alias or full model name
input:
  diff_file: samples/diff.txt
  modified_lines_file: samples/modified-lines.txt
  dependencies_file: samples/dependencies.sample.json  # required for mode: review
  source_root: /path/to/checked-out/repo               # optional
agent:
  backend: claude_code      # or: opencode
output:
  path: results/output.json
```

The `config.yaml` in the repository is a working starting point (opencode +
Ollama); the values above are only an example. Then run:

```
python -m src.cli --config config.yaml
```

The output is `{mode, result, raw_response, parse_error}` at `output.path`.
A non-null `parse_error` means the run failed after all retries;
`raw_response` holds what the model actually said.

### Mode 2 input format

`input.dependencies_file` is a JSON array of raw findings from the
static-analysis tool (`samples/dependencies.sample.json` is a full example):

```json
[
  {
    "type": "OAINTER",
    "label": "OA conflict",
    "body": {
      "description": "...",
      "interference": [
        {
          "type": "declaration",
          "branch": "L",
          "text": "...",
          "location": {"file": "", "class": "org.example.Clue", "method": "<init>", "line": 22},
          "stackTrace": [{"class": "...", "method": "...", "line": 9}]
        }
      ]
    }
  }
]
```

`src/parsing/dependency_parser.py` classifies each finding by `type`/`label`
(contains "OA" → Overriding Assignment; contains "CF" → Confluence Flow;
`type == "CONFLICT"` or contains "SVFA" → Direct Flow) and drops anything
else. Exact duplicates are collapsed. An interference node's `branch`
("L"/"R") is often empty; then authorship is resolved by matching each
stack-trace frame's (class, line) against `modified-lines.txt`, since those
line numbers belong to the merged version.

### Comparing one run with static analysis

Score a Mode 1 output against the tool's dependencies:

```
python -m src.compare --config config.yaml --llm-output results/output.json --out comparison.csv
```

`--dependencies` overrides `input.dependencies_file` from the config.
Dependencies match when they have the same type and overlapping
(class, line) sets (greedy, largest overlap first). The table reports how
many were found by both, only by the LLM, and only by the tool.

### Running several models on one scenario

```
python -m scripts.run_experiment --models gpt-oss:20b-cloud claude:sonnet --runs 5
```

- `--models` — models to run. A plain name goes through the `opencode`
  backend (with `llm.provider` as configured); a `claude:` prefix
  (`claude:sonnet`) goes through `claude_code` with the rest as the model.
- `--runs` / `-n` — runs per model (required).
- `--config` — base config (default `config.yaml`). Per run, only `mode`
  (always `detect`), `llm.model`, `agent.backend` and `output.path` are
  overridden.
- `--results-dir` — where `<model>_run<N>/` folders go, each with
  `output.json` and `comparison.csv` (default `results`).
- `--dependencies` — tool findings to score against (default: the config's
  `input.dependencies_file`).
- `--summary-out` — summary CSV (default `<results-dir>/summary.csv`).

A run that crashes or ends in `parse_error` is left out of that model's
scoring without stopping the rest. The summary lists the tool's dependency
count next to each model's maximum, minimum and median across its runs.

## Dataset experiments

These commands run the whole study on the MergeDataSet: the agents and the
static-analysis tool on every scenario, then a comparison.

**Extra prerequisites**

- A checkout of the **MergeDataset** (its jars are analysed; its CSVs are
  read).
- A checkout of **[conflict-static-analysis](https://github.com/spgroup/conflict-static-analysis)**.
  Tell the scripts where it is with `--tool-dir`, or
  once for all of them with the `CONFLICT_STATIC_ANALYSIS_DIR` environment
  variable. There is no default: a command that needs the tool stops with a
  message if neither is set.
- **Docker**, and Maven's GitHub-packages credentials in `~/.m2/settings.xml`
  (see the tool's README) — only to build the tool.
- Optional, for exact Left/Right lines: Java and `diff` on `PATH`, and the
  study's DiffJ jar.

```
 prepare_dataset ──► dataset/ ──┬──► run_static_analysis ──► results/analysis/ ──┐
                                └──► run_dataset_experiment ► results/dataset/ ───┤
                                                                                  ▼
                     evaluate_by_method · compare_analysis · compare_ground_truth
```

### 1. Prepare the dataset

```
python -m scripts.prepare_dataset --dataset /.../mergedataset --out dataset --diffj /.../diffj.jar
```

A scenario is one merge commit with labeled methods in the dataset's
`sample-semantic-conflicts.csv`. For each one, `dataset/<project>_<commit8>/`
gets `diff.txt`, `modified-lines.txt`, `source/` (the merged classes, usable
as `input.source_root`) and `scenario.json` (the labeled methods).

- `--dataset` (required) — the MergeDataSet checkout. `--out` defaults to
  `dataset`.
- `--diffj` — DiffJ jar, used to compute the Left/Right lines with the
  study's algorithm. Without it (or when DiffJ fails on a class) a
  line-based approximation is used, and it is less exact.
- `--validate` — only check the algorithm against the study's own results
  (needs `--diffj`), then exit.
- `--methods-csv`, `--labels-csv` — take scenarios, methods, lines and
  labels from your own CSVs instead of the dataset's. See the script's
  docstring for the column layout.

After preparing, it lists labeled methods that ended up with no attributed
lines. A scenario where no developer **added** a line is skipped by both the
agents and the tool (deleted lines don't count: the tool takes added lines
as its sources and sinks).

### 2. Run the static-analysis tool

```
python -m scripts.run_static_analysis --mergedataset /.../mergedataset --build-tool   # once
python -m scripts.run_static_analysis --mergedataset /.../mergedataset --jobs 3
```

The tool runs in Docker, with the three analyses that stand for the
dependency types, each in both directions (left lines as sinks and right as
sources, then the reverse) and a 30-minute timeout per run, as in the study:

| Type | `--modes` key | Analysis (`-mode`) |
|---|---|---|
| Direct Flow | `DF` | `svfa-interprocedural` |
| Confluence Flow | `CF` | `dfp-confluence-interprocedural` |
| Overriding Assignment | `OA` | `ioa` |

The analysed jar is the merge's `original-without-dependencies` build; a
scenario whose classes are in different jars gets one run per jar.

- `--mergedataset` (required) — the MergeDataSet checkout.
- `--build-tool` — build the tool's jar first, in a container made from
  `docker/Dockerfile` (Ubuntu 22.04, OpenJDK 8, Maven 3.8.8). It reads
  `--settings` (default `~/.m2/settings.xml`) read-only.
- `--tool-dir` — the tool checkout (default: `$CONFLICT_STATIC_ANALYSIS_DIR`).
- `--only` — scenarios whose folder name contains one of these strings.
- `--modes` — only these analyses (default: all three).
- `--jobs` (3), `--timeout` (1800 s), `--heap` (JVM heap, `3g`) — the largest
  jars need more heap.
- `--force` — repeat runs that already have a `status.json`.
- `--dataset-dir`, `--results-dir` — default `dataset` and `results/analysis`.

Results go to `results/analysis/<scenario>/<jar>/<direction>/<mode>/`
(`out.json`, `console.txt`, `status.json`).

### 3. Run the agents

```
python -m scripts.run_dataset_experiment --models claude:sonnet gpt-oss:20b-cloud --runs 3
```

Runs Mode 1 on every prepared scenario and, per scenario and model, writes
`results/dataset/<scenario>/<model>_run<N>/output.json`, plus
`dataset_summary.csv`. A scenario counts as flagged when the model reported
at least one dependency in most of its runs; that is checked against the
scenario's "Locally Observable Interference" label (Yes = positive) and
printed as TP/FN/FP/TN per model.

- `--models` (required), `--runs` / `-n` (default **1**) — as in
  `run_experiment`.
- `--config`, `--dataset-dir`, `--results-dir` (default `results/dataset`).
- `--only` — scenarios whose folder name contains one of these strings.
- `--skip-existing` — reuse valid `output.json` files already in
  `--results-dir`; failed runs are run again.
- `--evaluate-by-method` — also score per labeled method afterwards (see
  [step 4](#4-score-and-compare)).
- `--with-analysis` — also run the static-analysis tool **in parallel**, then
  wait for it and print the full comparison report. Needs `--mergedataset`;
  tuned by `--analysis-jobs` (2), `--analysis-timeout` (1800),
  `--analysis-heap` (`3g`), `--analysis-results-dir`, `--force-analysis` and
  `--tool-dir` (as above). Build the tool once with `run_static_analysis --build-tool`.

```
python -m scripts.run_dataset_experiment --models claude:sonnet --runs 3 \
    --with-analysis --mergedataset /.../mergedataset
```

### 4. Score and compare

**Per labeled method** — never calls a model; reads finished runs:

```
python -m scripts.evaluate_by_method [--models claude-sonnet ...]
python -m scripts.show_method_summary [results/dataset/method_summary.csv]
```

`evaluate_by_method` treats each labeled method as its own case: a method is
detected in a run if some reported dependency has a left/right line inside
the method's body in the merged source, and by a model if it is detected in
most of its valid runs. It writes `<results-dir>/method_summary.csv`
(`--out` to change; also `--dataset-dir`, `--results-dir`).
`show_method_summary` prints that CSV as a table, with the outcomes per model
over each model's own methods and over the methods every model has a valid
result for (the fair comparison).

**Tool vs agents, no ground truth:**

```
python -m scripts.compare_analysis [--models claude-sonnet ...]
```

Part 1 counts the dependencies each reported. Part 2 counts *different*
ones (reports of one type sharing a (class, line) are one dependency) and how
many the tool and each model agree on (`matched`) or added over the other
(`tool only` / `LLM only`). A (scenario, type) pair is left out when any run
of that analysis timed out or failed, since the tool's side would be
incomplete. The report also lists every tool run that ended in error, with
cause, explanation and what can be done. Files, in `<analysis-dir>`:
`comparison_report.txt`, `errors_report.md`, `comparison_by_scenario.csv`
(`--output`, `--csv` to change; also `--analysis-dir`, `--results-dir`,
`--dataset-dir`). `run_dataset_experiment --with-analysis` produces the same
report at the end.

**Against the manual analysis (ground truth):**

```
python -m scripts.compare_ground_truth --manual "<spreadsheet>.csv" [--models claude-sonnet ...]
```

Scores the tool and each model per method and dependency type, against the
researchers' manual analysis (the "Manual analysis of DF / CF / OA" columns;
TRUE = positive). A pair is compared only when both sides have a result. It
writes `ground_truth_report.md` (with the reason for every row it could not
score) and `ground_truth_by_method.csv` in `<analysis-dir>`.

### Re-running

To redo everything from scratch, leave out `--skip-existing` (the agents run
again and overwrite `results/dataset`) and add `--force-analysis` (the tool
runs again too):

```
python -m scripts.run_dataset_experiment --models claude:sonnet --runs 3 \
    --with-analysis --force-analysis --evaluate-by-method --mergedataset /.../mergedataset
```

## Project structure

```
config.yaml                    Pipeline configuration (mode, LLM, inputs, agent backend)
opencode.json                  opencode provider and subagent config (Mode 1 & 2)
docker/Dockerfile              Image the static-analysis tool is built and run in
samples/                       Example diff, authorship and static-analysis output
.opencode/skills/              Review skill for the opencode backend
.claude/skills/                Review skill for the claude_code backend
src/
  cli.py                       Entry point of the pipeline
  compare.py                   Scores a Mode 1 output against the tool's findings
  config.py                    YAML config schema and loading
  experiment.py                Model specs (`claude:` prefix) and one detect-mode run
  graph.py                     The four-step pipeline
  javaspans.py                 Line ranges of a Java source's methods; lookup of a labeled method
  output.py                    Writes the pipeline's result to JSON
  schemas.py                   Pydantic schemas of the agent's structured output
  analysis/                    Shared by the comparison scripts
    common.py                    Dependency types, run folders, outcomes, table formatting
    loaders.py                   Readers of the tool's results and the agents' runs
    errors.py                    Report of the tool's runs that ended in error
  parsing/                     Diff, authorship and static-analysis-output parsers
  prompts/                     Renders parsed inputs into system/human prompt text
  clients/                     Agent backends (opencode, claude_code) behind one interface
scripts/                       One file per command in the table above
```

## Implementation notes

- **opencode's global config.** opencode ties "which project config
  applies" and "which directory the agent's tools operate in" to the same
  `directory` parameter. Since the agent must work in the scenario's own
  checkout (`input.source_root`), not this project's directory, the custom
  agents, provider and skill can't live in a project-local `opencode.json`;
  they are merged into opencode's global config, which loads regardless.
  `scripts/install_opencode_config.py` does the merge.

- **`opencode` and `claude` on Windows.** Both are npm `.CMD` shims, so the
  clients resolve them with `shutil.which`. Because they run through
  `cmd.exe`, a spawned opencode server is stopped with `taskkill /T /F`
  (`terminate()` would only kill the wrapper), and the roughly 8191-character
  command-line limit is avoided by sending the prompt over stdin and the
  system prompt through a temp file (`--system-prompt-file`).
  `src/clients/claude_code.py` also sets `encoding="utf-8"`, since the
  Windows default silently corrupted non-ASCII characters.

## License

Licensed under [CC BY 4.0](LICENSE) — Creative Commons Attribution 4.0
International.
