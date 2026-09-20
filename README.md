# InView.ai

InView.ai is an agent-based pipeline that reviews a two-developer merge
scenario for **semantic dependencies**: cases where independent changes made
by two developers interact through data flow, even though they never touch
the same line — the kind of conflict a textual (git) merge cannot detect.

Given a diff and per-line authorship, the agent reads the full source files
involved (not just the diff hunks) and either detects these dependencies
itself, or reviews dependencies already reported by a static-analysis tool.
Results can then be scored against that tool's own output.

## Index

- [Background](#background)
- [Modes](#modes)
- [How it works](#how-it-works)
  - [Agent backends](#agent-backends)
  - [Reading the real source, not just the diff](#reading-the-real-source-not-just-the-diff)
- [Project structure](#project-structure)
  - [Static-analysis input format (Mode 2)](#static-analysis-input-format-mode-2)
- [Installation](#installation)
  - [Using the ollama provider](#using-the-ollama-provider)
  - [Adding ollama models](#adding-ollama-models)
- [Usage](#usage)
  - [Comparing against static analysis](#comparing-against-static-analysis)
- [Implementation notes](#implementation-notes)
- [License](#license)

## Background

Definitions are adapted from Jesus et al., *"Comparing static analyses for
improved semantic conflict detection"* (Automated Software Engineering,
2026). The pipeline targets three dependency types:

- **Direct Flow** — a value written or used in a line changed by one
  developer flows (through data flow) to a state element written or used in
  a line changed by the other.
- **Overriding Assignment** — a state element written by one developer is
  later overwritten by the other, with no intermediate write from the common
  base version, silently discarding one change's effect.
- **Confluence Flow** — data-flow paths from each developer's distinct
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

## How it works

The pipeline (`src/graph.py`) is a fixed sequence of four steps:

1. **`load_inputs`** — parses the diff (`src/parsing/diff_parser.py`),
   per-line authorship (`src/parsing/modified_lines_parser.py`), and, in
   Mode 2, the static-analysis tool's findings
   (`src/parsing/dependency_parser.py`); joins the diff with authorship into
   an annotated diff (`src/parsing/annotate.py`).
2. **`build_prompt`** — renders the annotated diff, dependency-type
   definitions, and (Mode 2) precomputed dependencies into a system/human
   prompt pair (`src/prompts/`).
3. **`call_agent`** — sends that prompt to an agent backend and gets back a
   structured, schema-validated result (`src/clients/`).
4. **`save_output`** — writes `{mode, result, raw_response, parse_error}` as
   JSON to `output.path` (`src/output.py`).

The result schema (`src/schemas.py`, `Mode1Result`/`Mode2Result`) is enforced
by the backend itself, not parsed out of free text after the fact.

### Agent backends

Two backends implement the same interface (`src/clients/base.py`,
`AgentClient`), selected via `agent.backend` in `config.yaml`:

- **`opencode`** (default) — talks to an [opencode](https://opencode.ai)
  server. Supports any `llm.provider` (Ollama, Gemini, OpenRouter,
  Anthropic), each via its own API key. Two subagents are defined in
  `opencode.json`: `dependency-detector` (Mode 1) and `dependency-reviewer`
  (Mode 2), both restricted to read-only tools (`read`/`grep`/`glob`).
  A companion skill (`.opencode/skills/semantic-dependency-review/`)
  instructs the agent to read the real source files before answering, and to
  call the `StructuredOutput` tool as its final action.

- **`claude_code`** — shells out to the `claude` CLI headlessly instead
  (`claude -p ...`). This is the only integration path allowed to use a
  Claude Pro/Max **subscription** login rather than a pay-per-token API key:
  Anthropic's terms restrict subscription auth to Anthropic's own
  first-party clients, which rules out both opencode and `claude-agent-sdk`
  (the official Python/TS SDK) for that purpose — only the CLI itself
  qualifies. The companion skill lives at
  `.claude/skills/semantic-dependency-review/`, auto-discovered by Claude
  Code with no extra setup.

Both backends retry (a fresh attempt each time, up to `agent.max_retries`
extra attempts) if a run doesn't come back with schema-valid structured
output — models vary a lot in how reliably they end their turn with an
actual structured result instead of free-form text.

### Reading the real source, not just the diff

Setting `input.source_root` to a checkout of the codebase the diff applies
to scopes the agent's read/grep/glob tools to that directory, so it can open
full files instead of reasoning from the diff hunk alone (which often omits
the declarations, other methods, or superclasses a dependency depends on).
Leave it unset to fall back to diff-only reasoning.

## Project structure

```
config.yaml                    Pipeline configuration (mode, LLM, inputs, agent backend)
opencode.json                  opencode plugin/provider/subagent config (Mode 1 & 2)
scripts/
  install_opencode_config.py   Syncs opencode.json + .opencode/skills/ into opencode's global config
.opencode/skills/               Review skill for the opencode backend
.claude/skills/                 Review skill for the claude_code backend
samples/                       Example diff, authorship, and static-analysis output
src/
  cli.py                       Entry point: `python -m src.cli --config config.yaml`
  compare.py                   Scores a Mode 1 output against the static-analysis tool's findings
  config.py                    YAML config schema and loading
  graph.py                     The four-step pipeline
  output.py                    Writes the pipeline's result to JSON
  schemas.py                   Pydantic schemas for the agent's structured output
  parsing/                     Diff, authorship, and static-analysis-output parsers
  prompts/                     Renders parsed inputs into system/human prompt text
  clients/                     Agent backends (opencode, claude_code) behind a common interface
```

### Static-analysis input format (Mode 2)

`input.dependencies_file` is a JSON array of raw findings from the
static-analysis tool, e.g.:

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

`src/parsing/dependency_parser.py` classifies each finding into one of the
three target types by inspecting `type`/`label` (contains "OA" ->
Overriding Assignment; contains "CF" -> Confluence Flow; `type == "CONFLICT"`
or contains "SVFA" -> Direct Flow) and drops anything else as out of scope.
Exact-duplicate findings (the tool is known to repeat entries) are collapsed
to one. Each interference node's `branch` field ("L"/"R") is often empty; in
that case, authorship is resolved by matching each stack-trace frame's
(class, line) against the `modified-lines.txt` data, since these line
numbers correspond to the merged version.

## Installation

1. **Python dependencies**
   ```
   pip install -r requirements.txt
   ```

2. **Pick one agent backend** and set it up — see [Agent backends](#agent-backends)
   for what each one is and when to use it. You only need to follow the
   steps for the backend you intend to use (`agent.backend` in
   `config.yaml`); the other backend's setup can be skipped entirely.

   <b>Option A — <code>opencode</code></b> (default, pay-per-token API keys, multiple providers)

   - Install [opencode](https://opencode.ai/docs) and Node.js.
   - Sync this project's agent/skill config into opencode's global config
     (needed because opencode scopes both config discovery and tool-root
     access to the same target directory — see *Implementation notes*):
     ```
     python scripts/install_opencode_config.py
     ```
   - Authenticate whichever `llm.provider` you'll use:
     `opencode auth login --provider <gemini|openrouter|anthropic>`. Ollama
     doesn't go through opencode's auth — see below.

   <b>Option B — <code>claude_code</code></b> (Claude Pro/Max subscription login)

   - Install [Claude Code](https://code.claude.com/docs/quickstart).
   - Log in once: interactively (`claude`), or on a headless machine,
     `claude setup-token` (requires a Claude subscription).

### Using the Ollama provider

`llm.provider: ollama` is one of the four
`llm.provider` choices (alongside `gemini`, `openrouter`, `anthropic`) and
only applies to the `opencode` backend — `claude_code` always talks to
Claude, regardless of `llm.provider`. It's worth reaching for when you
want to avoid pay-per-token API costs, keep the diff/source files on your
own machine instead of sending them to a hosted API, or just experiment
with an open-weight model:

- How to use it: install Ollama, `ollama pull <tag>` the model you want,
  then set `llm.provider: ollama` and `llm.model: <tag>` in
  `config.yaml`. Two Ollama-cloud models (`gemma4:31b-cloud`,
  `gpt-oss:20b-cloud`) are already registered in `opencode.json`; any
  other tag needs to be added first — see
  [Adding Ollama models](#adding-ollama-models).
- Login: not needed for models running fully locally. For
  Ollama-cloud-proxied tags (the `*-cloud` suffixed ones above), run
  `ollama signin` instead — that's Ollama's own login, unrelated to
  `opencode auth`.

### Adding Ollama models

The `opencode` backend's Ollama models aren't auto-discovered; each one
needs an entry in `opencode.json` under `provider.ollama.models`:

```json
"provider": {
  "ollama": {
    "models": {
      "qwen2.5-coder:14b": { "name": "Qwen2.5 Coder 14B" }
    }
  }
}
```

The key is the exact model tag Ollama uses (`ollama list` to check), the
`name` is just a display label. Then:

1. Make sure Ollama actually has that model: `ollama pull <tag>` (this is
   also required for Ollama-cloud-proxied tags, e.g. `*-cloud` suffixed
   models — pulling registers the alias even though no weights are
   downloaded locally).
2. Re-sync opencode's global config: `python scripts/install_opencode_config.py`,
   then restart any running `opencode serve`.
3. Point `config.yaml`'s `llm.model` at the new tag.

## Usage

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
  backend: claude_code       # or: opencode
output:
  path: results/output.json
```

Run it:

```
python -m src.cli --config config.yaml
```

Output is written to `output.path` as `{mode, result, raw_response,
parse_error}`. A non-null `parse_error` means the run failed after
exhausting retries; `raw_response` holds whatever the model actually said,
for debugging.

### Comparing against static analysis

For Mode 1 output, score it against the static-analysis tool's own
dependencies:

```
python -m src.compare --config config.yaml --llm-output results/output.json --out comparison.csv
```

This matches dependencies by type and by overlapping (class, line) sets
(greedy, largest overlap first) and reports how many were found by both,
only by the LLM, and only by the tool.

## Implementation notes

- **opencode's global config.** opencode ties "which project config
  applies" and "what directory the agent's tools operate in" to the same
  `directory` request parameter. Since Mode 1/2 needs that directory to be
  the merge scenario's own checkout (`input.source_root`), not this
  project's own directory, the custom agents/provider/skill can't live in a
  project-local `opencode.json` — they have to be merged into opencode's
  global config instead, which loads regardless of the target directory.
  `scripts/install_opencode_config.py` does that merge; re-run it after
  editing `opencode.json` or `.opencode/skills/`.

- **`opencode`/`claude` CLIs on Windows.** Both resolve to npm's `.CMD`
  shims. `subprocess.Popen`/`.run` won't find a bare `"opencode"`/`"claude"`
  the way a shell would, so both clients resolve the executable via
  `shutil.which` first. `.CMD` files are also only launchable via
  `cmd.exe /c`, which imposes two further limits: a process spawned through
  it needs its whole tree killed (`taskkill /T /F` on Windows) since
  `Popen.terminate()` only kills the `cmd.exe` wrapper, not opencode's actual
  server child; and its command line is capped at roughly 8191 characters,
  which the annotated diff and JSON schema would blow past as inline
  arguments to `claude` — so the prompt goes over stdin and the system
  prompt through a temp file (`--system-prompt-file`) instead.
  `subprocess.run`'s default text encoding on Windows also isn't UTF-8,
  which was silently corrupting non-ASCII characters in responses;
  `src/clients/claude_code.py` sets `encoding="utf-8"` explicitly.

## License

Licensed under [CC BY 4.0](LICENSE) — Creative Commons Attribution 4.0
International.
