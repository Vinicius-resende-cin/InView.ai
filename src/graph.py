"""LangGraph pipeline: load inputs -> build prompt -> call agent -> save output."""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from src.agent_client import run_agent
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
    messages: list[BaseMessage]
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
    messages = state["messages"]
    result_model = _result_model_for_mode(config.mode)

    return run_agent(
        agent_config=config.agent,
        llm_config=config.llm,
        agent_name=_agent_name_for_mode(config.mode),
        system_text=str(messages[0].content),
        human_text=str(messages[1].content),
        result_model=result_model,
    )


def save_output(state: GraphState) -> dict:
    write_output(state["config"].output.path, state)
    return {}


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("load_inputs", load_inputs)
    graph.add_node("build_prompt", build_prompt)
    graph.add_node("call_agent", call_agent)
    graph.add_node("save_output", save_output)

    graph.set_entry_point("load_inputs")
    graph.add_edge("load_inputs", "build_prompt")
    graph.add_edge("build_prompt", "call_agent")
    graph.add_edge("call_agent", "save_output")
    graph.add_edge("save_output", END)

    return graph.compile()


def run_pipeline(config: AppConfig) -> dict[str, Any]:
    graph = build_graph()
    return graph.invoke({"config": config})
