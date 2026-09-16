"""Agent backends: opencode (any llm.provider) and claude_code (subscription-
compliant, via the `claude` CLI). Both implement AgentClient's interface."""

from __future__ import annotations

from src.clients.base import AgentClient
from src.clients.claude_code import ClaudeCodeAgentClient
from src.clients.opencode import OpencodeAgentClient
from src.config import AgentConfig

_CLIENTS: dict[str, type[AgentClient]] = {
    "opencode": OpencodeAgentClient,
    "claude_code": ClaudeCodeAgentClient,
}


def get_client(agent_config: AgentConfig) -> AgentClient:
    """Instantiate the AgentClient for agent_config.backend."""
    return _CLIENTS[agent_config.backend](agent_config)


__all__ = [
    "AgentClient",
    "ClaudeCodeAgentClient",
    "OpencodeAgentClient",
    "get_client",
]
