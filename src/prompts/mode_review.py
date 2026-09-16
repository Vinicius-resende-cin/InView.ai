"""Mode 2 ("review"): review the merge given dependencies already found by static analysis."""

from __future__ import annotations

from src.config import DependencyTypeConfig
from src.parsing.annotate import AnnotatedFile
from src.parsing.dependency_parser import DependencySet
from src.prompts.definitions import render_definitions
from src.prompts.dependency_rendering import render_dependencies
from src.prompts.diff_rendering import render_annotated_diff

_SYSTEM_TEMPLATE = """\
You are a code reviewer analyzing a merge scenario in which two developers, \
Left and Right, independently modified the same codebase starting from a \
common base version. The diff you will see is the result of merging their \
changes; each modified line is tagged with the developer who introduced it.

A static analysis tool has already identified semantic dependencies between \
Left's and Right's changes: cases where a change made by one developer \
interacts with a change made by the other in a way that a purely textual \
merge cannot catch.

{definitions}

Your task is to review the merge using the dependencies already found. For \
each one, assess whether it represents a genuine risk (e.g., unintended \
behavior, broken invariant, semantic conflict) or is safe/benign, and \
explain your reasoning. You may also flag any additional concerns you \
notice, but your primary job is to evaluate the reported dependencies.
"""

_HUMAN_TEMPLATE = """\
Here is the annotated diff of the merge scenario. Lines are tagged \
[Left] or [Right] according to which developer introduced them; untagged \
lines are unchanged context.

{diff}

{dependencies}
"""


def build_review_messages(
    dependency_types: list[DependencyTypeConfig],
    annotated_diff: list[AnnotatedFile],
    dependencies: DependencySet,
) -> tuple[str, str]:
    """Returns (system_text, human_text)."""
    system = _SYSTEM_TEMPLATE.format(definitions=render_definitions(dependency_types))
    human = _HUMAN_TEMPLATE.format(
        diff=render_annotated_diff(annotated_diff),
        dependencies=render_dependencies(dependencies),
    )
    return system, human
