"""Common interface for agent backends (opencode, claude_code)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from src.config import AgentConfig, LLMConfig


class AgentClient(ABC):
    """Runs one review task against a backend, returning
    {"structured_result", "raw_response", "parse_error"}.

    Owns the retry loop shared by every backend: models vary a lot in how
    reliably they end their turn with a valid structured-output result
    rather than free-form text, so `run()` retries (each attempt fresh, per
    `_run_once`) up to `agent_config.max_retries` extra times before giving
    up. Subclasses only need to implement a single attempt.
    """

    def __init__(self, agent_config: AgentConfig):
        self.agent_config = agent_config

    def run(
        self,
        llm_config: LLMConfig,
        agent_name: str,
        system_text: str,
        human_text: str,
        result_model: type[BaseModel],
        source_root: Optional[Path] = None,
    ) -> dict:
        self._setup()
        try:
            attempts = max(1, self.agent_config.max_retries + 1)
            result: dict = {}
            for _ in range(attempts):
                result = self._run_once(
                    llm_config, agent_name, system_text, human_text,
                    result_model, source_root,
                )
                if result["parse_error"] is None:
                    return result

            result["parse_error"] = f"{result['parse_error']} (after {attempts} attempt(s))"
            return result
        finally:
            self._teardown()

    def _setup(self) -> None:
        """Optional: prepare resources shared across every attempt in a run()."""

    def _teardown(self) -> None:
        """Optional: release resources acquired in _setup()."""

    @abstractmethod
    def _run_once(
        self,
        llm_config: LLMConfig,
        agent_name: str,
        system_text: str,
        human_text: str,
        result_model: type[BaseModel],
        source_root: Optional[Path],
    ) -> dict:
        """One attempt. Must return
        {"structured_result", "raw_response", "parse_error"}."""
