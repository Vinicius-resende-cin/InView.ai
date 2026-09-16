"""Bridge to a running (or auto-started) opencode server.

Sends the system/human prompt text to an opencode subagent over its HTTP
API, with the result model's JSON schema enforced via opencode's built-in
structured-output support.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from src.clients.base import AgentClient
from src.config import AgentConfig, LLMConfig

# Maps this project's llm.provider values to opencode provider IDs (confirmed
# against models.dev, opencode's own provider catalog). "ollama" is a custom
# provider we register in opencode.json; "google", "openrouter", and
# "anthropic" are opencode's built-in provider IDs - each needs credentials
# set up on the opencode side (`opencode auth login`), not via this project's
# config.
_PROVIDER_MAP = {
    "ollama": "ollama",
    "gemini": "google",
    "openrouter": "openrouter",
    "anthropic": "anthropic",
}


class _OpencodeServer:
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
        """Whether a server is already responding at base_url."""
        try:
            urllib.request.urlopen(f"{self.base_url}/doc", timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            return False

    def _start(self, timeout: float) -> None:
        """Spawn `opencode serve` and block until it responds."""
        # shutil.which resolves npm's Windows .CMD shim; a bare "opencode"
        # passed straight to Popen would not (see README's Implementation notes).
        executable = shutil.which("opencode")
        if executable is None:
            raise RuntimeError(
                "opencode.auto_start is true but no 'opencode' executable was "
                "found on PATH. Install it (see opencode.ai/docs) or start "
                "`opencode serve` yourself and set agent.auto_start: false."
            )
        port = self.base_url.rsplit(":", 1)[-1]
        self._proc = subprocess.Popen(
            [executable, "serve", "--port", port],
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
        """Terminate the server if we spawned it ourselves; no-op otherwise."""
        if self._proc is None:
            return
        if sys.platform == "win32":
            # terminate() alone only kills the .CMD wrapper, not the actual
            # opencode.exe child it spawns (see README's Implementation notes).
            subprocess.run(
                ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            self._proc.terminate()


def _with_query(url: str, **params: Optional[str]) -> str:
    """Append non-None params to `url` as a query string."""
    query = {k: v for k, v in params.items() if v is not None}
    if not query:
        return url
    return f"{url}?{urllib.parse.urlencode(query)}"


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    """POST `payload` as JSON to `url` and return the parsed JSON response."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _raw_text(response: dict) -> Optional[str]:
    """Concatenate a message response's text parts, for debugging."""
    text = "".join(
        part.get("text", "")
        for part in response.get("parts", [])
        if part.get("type") == "text"
    )
    return text or None


class OpencodeAgentClient(AgentClient):
    """Runs reviews through an opencode subagent (dependency-detector /
    dependency-reviewer), any llm.provider opencode itself is configured for.
    """

    def _setup(self) -> None:
        self._server = _OpencodeServer(self.agent_config)

    def _teardown(self) -> None:
        self._server.close()

    def _run_once(
        self,
        llm_config: LLMConfig,
        agent_name: str,
        system_text: str,
        human_text: str,
        result_model: type[BaseModel],
        source_root: Optional[Path],
    ) -> dict:
        """Send one message to `agent_name` (a fresh session) and return the
        pipeline's result dict.

        When `source_root` is given, the session's read/grep/glob tools are
        scoped to that directory, so the agent can open the full source
        files the diff only shows hunks of instead of working from the diff
        text alone.
        """
        server = self._server
        # Always pass an explicit directory rather than leaving opencode to
        # pick its own default, so runs stay reproducible regardless of
        # whatever project the opencode server last had open.
        directory = str(source_root) if source_root is not None else str(Path.cwd())
        session = _post_json(
            _with_query(f"{server.base_url}/session", directory=directory),
            {},
            self.agent_config.request_timeout,
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
                # Have opencode retry the StructuredOutput tool call if the
                # model's first attempt doesn't validate against the schema,
                # rather than accepting a partial/invalid result outright
                # (observed live with a weaker local model).
                "retryCount": self.agent_config.format_retry_count,
            },
        }
        response = _post_json(
            _with_query(f"{server.base_url}/session/{session_id}/message", directory=directory),
            body,
            self.agent_config.request_timeout,
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
