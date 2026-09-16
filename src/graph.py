"""Pipeline: load inputs -> build prompt -> call agent -> save output."""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from pydantic import BaseModel

from src.agent_client import run_agent as run_opencode_agent
from src.claude_code_client import run_agent as run_claude_code_agent
from src.config import AppConfig
from src.output import write_output
from src.parsing.annotate import AnnotatedFile, annotate_diff_files
from src.parsing.dependency_parser import DependencySet, load_dependencies
from src.parsing.diff_parser import load_diff
from src.parsing.modified_lines_parser import load_modified_lines
from src.prompts.mode_detect import build_detect_messages
from src.prompts.mode_review import build_review_messages
from src.schemas import Mode1Result, Mode2Result


class GraphState(TypedDict, total=False):
    config: AppConfig
    annotated_diff: list[AnnotatedFile]
    dependencies: Optional[DependencySet]
    messages: tuple[str, str]
    structured_result: Optional[dict]
    raw_response: Optional[str]
    parse_error: Optional[str]


def _result_model_for_mode(mode: str) -> type[BaseModel]:
    return Mode1Result if mode == "detect" else Mode2Result


def _agent_name_for_mode(mode: str) -> str:
    return "dependency-detector" if mode == "detect" else "dependency-reviewer"


def load_inputs(state: GraphState) -> dict:
    config = state["config"]
    authorship_by_class = load_modified_lines(config.input.modified_lines_file)
    diff_files = load_diff(config.input.diff_file)
    annotated_diff = annotate_diff_files(diff_files, authorship_by_class)

    dependencies = None
    if config.mode == "review":
        dependencies = load_dependencies(
            config.input.dependencies_file, authorship_by_class
        )
    return {"annotated_diff": annotated_diff, "dependencies": dependencies}


def build_prompt(state: GraphState) -> dict:
    config = state["config"]
    if config.mode == "detect":
        messages = build_detect_messages(config.dependency_types, state["annotated_diff"])
    else:
        messages = build_review_messages(
            config.dependency_types, state["annotated_diff"], state["dependencies"]
        )
    return {"messages": messages}


def call_agent(state: GraphState) -> dict:
    config = state["config"]
    system_text, human_text = state["messages"]
    result_model = _result_model_for_mode(config.mode)
    run = run_claude_code_agent if config.agent.backend == "claude_code" else run_opencode_agent

    return run(
        agent_config=config.agent,
        llm_config=config.llm,
        agent_name=_agent_name_for_mode(config.mode),
        system_text=system_text,
        human_text=human_text,
        result_model=result_model,
        source_root=config.input.source_root,
    )


def save_output(state: GraphState) -> dict:
    write_output(state["config"].output.path, state)
    return {}


_PIPELINE = (load_inputs, build_prompt, call_agent, save_output)


def run_pipeline(config: AppConfig) -> dict[str, Any]:
    state: GraphState = {"config": config}
    for step in _PIPELINE:
        state.update(step(state))
    return dict(state)
