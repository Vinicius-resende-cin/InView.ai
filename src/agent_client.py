"""Bridge to a running (or auto-started) opencode server.

Replaces the old direct LangChain call: sends the same system/human prompt
text to an opencode subagent over its HTTP API, with the result model's JSON
schema enforced via opencode's built-in structured-output support, and
returns the same {"structured_result", "raw_response", "parse_error"} shape
the old call_llm() node produced, so downstream code (save_output,
compare.py) is unaffected.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from src.config import AgentConfig, LLMConfig

# Maps this project's llm.provider values to opencode provider IDs.
# "ollama" is a custom provider we register in opencode.json; "google" and
# "openrouter" are opencode's built-in provider IDs (to be confirmed against
# a live account during Phase 4 parity testing).
_PROVIDER_MAP = {
    "ollama": "ollama",
    "gemini": "google",
    "openrouter": "openrouter",
}


class OpencodeServer:
    """Connects to agent_config.base_url, starting `opencode serve` if needed."""

    def __init__(self, agent_config: AgentConfig):
        self.base_url = agent_config.base_url.rstrip("/")
        self._proc: Optional[subprocess.Popen] = None
        if not self._reachable():
            if not agent_config.auto_start:
                raise RuntimeError(
                    f"No opencode server reachable at {self.base_url} and "
                    "agent.auto_start is false."
                )
            self._start(agent_config.startup_timeout)

    def _reachable(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.base_url}/doc", timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            return False

    def _start(self, timeout: float) -> None:
        port = self.base_url.rsplit(":", 1)[-1]
        self._proc = subprocess.Popen(
            ["opencode", "serve", "--port", port],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._reachable():
                return
            time.sleep(0.5)
        raise RuntimeError(f"opencode serve did not become ready within {timeout}s")

    def close(self) -> None:
        # Only ever set when we spawned the process ourselves.
        if self._proc is not None:
            self._proc.terminate()


def _with_query(url: str, **params: Optional[str]) -> str:
    query = {k: v for k, v in params.items() if v is not None}
    if not query:
        return url
    return f"{url}?{urllib.parse.urlencode(query)}"


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _raw_text(response: dict) -> Optional[str]:
    text = "".join(
        part.get("text", "")
        for part in response.get("parts", [])
        if part.get("type") == "text"
    )
    return text or None


def run_agent(
    agent_config: AgentConfig,
    llm_config: LLMConfig,
    agent_name: str,
    system_text: str,
    human_text: str,
    result_model: type[BaseModel],
    source_root: Optional[Path] = None,
) -> dict:
    """Send one message to `agent_name` and return the pipeline's result dict.

    When `source_root` is given, the session's read/grep/glob tools are
    scoped to that directory, so the agent can open the full source files
    the diff only shows hunks of instead of working from the diff text alone.
    """
    server = OpencodeServer(agent_config)
    try:
        directory = str(source_root) if source_root is not None else None
        session = _post_json(
            _with_query(f"{server.base_url}/session", directory=directory),
            {},
            agent_config.request_timeout,
        )
        session_id = session["id"]

        provider_id = _PROVIDER_MAP.get(llm_config.provider, llm_config.provider)
        body = {
            "agent": agent_name,
            "model": {"providerID": provider_id, "modelID": llm_config.model},
            "system": system_text,
            "parts": [{"type": "text", "text": human_text}],
            "format": {
                "type": "json_schema",
                "schema": result_model.model_json_schema(),
            },
        }
        response = _post_json(
            _with_query(
                f"{server.base_url}/session/{session_id}/message", directory=directory
            ),
            body,
            agent_config.request_timeout,
        )

        info = response.get("info", {})
        structured = info.get("structured")
        raw_response = _raw_text(response)

        if structured is None:
            return {
                "structured_result": None,
                "raw_response": raw_response,
                "parse_error": (
                    "opencode agent returned no structured output "
                    f"(finish={info.get('finish')!r})"
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
    finally:
        server.close()
