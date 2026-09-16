"""Bridge to the Claude Code CLI, run headlessly via `claude -p`.

Unlike opencode.py (which talks to opencode, a third-party tool that
Anthropic's terms don't allow using with Claude Pro/Max subscription auth),
this shells out to the actual `claude` CLI binary in print/headless mode -
Anthropic's own first-party client, which is allowed to use the subscription
(already logged in interactively, or via `claude setup-token` on a machine
that isn't). claude-agent-sdk is deliberately not used here: its own terms
restrict it to API-key auth, same restriction as opencode.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from src.clients.base import AgentClient
from src.config import LLMConfig


class ClaudeCodeAgentClient(AgentClient):
    """Runs reviews through the `claude` CLI, headlessly, using whatever
    auth the CLI already has (subscription login, or an API key).

    `agent_name` is accepted for interface parity with OpencodeAgentClient
    (which uses it to pick an opencode subagent) but unused here - the task
    framing already lives entirely in `system_text`.
    """

    def _run_once(
        self,
        llm_config: LLMConfig,
        agent_name: str,
        system_text: str,
        human_text: str,
        result_model: type[BaseModel],
        source_root: Optional[Path],
    ) -> dict:
        del agent_name
        executable = shutil.which("claude")
        if executable is None:
            raise RuntimeError(
                "agent.backend is 'claude_code' but no 'claude' executable was "
                "found on PATH. Install Claude Code (see claude.com/code) and "
                "log in - interactively, or via `claude setup-token` on a "
                "headless machine (requires a Claude subscription)."
            )

        # The resolved executable is npm's claude.CMD shim, which Windows can
        # only launch via cmd.exe /c - capped at ~8191 characters per command
        # line, regardless of subprocess's own (much higher) limit. The
        # prompt (the annotated diff) easily blows past that inline, so it
        # goes over stdin instead; the system prompt goes through a temp
        # file via --system-prompt-file, a documented but unlisted sibling
        # of --system-prompt (see --bare's help text). Only the schema stays
        # inline - schemas here are small enough not to matter.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(system_text)
            system_prompt_path = f.name

        # Run from this project's own directory (not source_root) so Claude
        # Code auto-discovers .claude/skills/ here; --add-dir grants read
        # access into source_root as well, without changing where skills/
        # settings resolve from - unlike opencode, no separate global-config
        # sync is needed.
        cmd = [
            executable,
            "-p",
            "--system-prompt-file", system_prompt_path,
            "--output-format", "json",
            "--json-schema", json.dumps(result_model.model_json_schema()),
            "--allowed-tools", "Read,Glob,Grep",
            "--permission-mode", "dontAsk",
            "--setting-sources", "project",
            "--no-session-persistence",
        ]
        if llm_config.model:
            cmd += ["--model", llm_config.model]
        if source_root is not None:
            cmd += ["--add-dir", str(source_root)]

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(Path.cwd()),
                input=human_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.agent_config.request_timeout,
            )
        finally:
            Path(system_prompt_path).unlink(missing_ok=True)

        if proc.returncode != 0:
            return {
                "structured_result": None,
                "raw_response": proc.stdout or None,
                "parse_error": f"claude CLI exited {proc.returncode}: {proc.stderr.strip()}",
            }

        try:
            response = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return {
                "structured_result": None,
                "raw_response": proc.stdout,
                "parse_error": f"claude CLI output was not valid JSON: {exc}",
            }

        raw_response = response.get("result")
        structured = response.get("structured_output")

        if response.get("is_error") or structured is None:
            return {
                "structured_result": None,
                "raw_response": raw_response,
                "parse_error": (
                    "claude CLI returned no structured output "
                    f"(subtype={response.get('subtype')!r})"
                ),
            }

        try:
            parsed = result_model.model_validate(structured)
        except Exception as exc:
            return {
                "structured_result": None,
                "raw_response": raw_response,
                "parse_error": f"structured output failed schema validation: {exc}",
            }

        return {
            "structured_result": parsed.model_dump(),
            "raw_response": raw_response,
            "parse_error": None,
        }
