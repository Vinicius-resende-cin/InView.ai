"""Mode 1 ("detect"): ask the LLM to find semantic dependencies itself."""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from src.config import DependencyTypeConfig
from src.parsing.annotate import AnnotatedFile
from src.prompts.definitions import render_definitions, render_types_to_detect
from src.prompts.diff_rendering import render_annotated_diff

_SYSTEM_TEMPLATE = """\
You are a code reviewer analyzing a merge scenario in which two developers, \
Left and Right, independently modified the same codebase starting from a \
common base version. The diff you will see is the result of merging their \
changes; each modified line is tagged with the developer who introduced it.

Your task is to detect semantic dependencies between Left's and Right's \
changes: cases where a change made by one developer interacts with a change \
made by the other in a way that a purely textual merge cannot catch.

{definitions}

{types_to_detect}

For each dependency you find, report: its type, the specific lines from Left \
and from Right that are involved, and a brief explanation of why they are \
dependent.
"""

_HUMAN_TEMPLATE = """\
Here is the annotated diff of the merge scenario. Lines are tagged \
[Left] or [Right] according to which developer introduced them; untagged \
lines are unchanged context.

{diff}
"""


def build_detect_messages(
    dependency_types: list[DependencyTypeConfig], annotated_diff: list[AnnotatedFile]
) -> list[BaseMessage]:
    system = _SYSTEM_TEMPLATE.format(
        definitions=render_definitions(dependency_types),
        types_to_detect=render_types_to_detect(dependency_types),
    )
    human = _HUMAN_TEMPLATE.format(diff=render_annotated_diff(annotated_diff))
    return [SystemMessage(content=system), HumanMessage(content=human)]
