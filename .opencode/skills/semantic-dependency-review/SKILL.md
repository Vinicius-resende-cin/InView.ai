---
name: semantic-dependency-review
description: Use when analyzing a two-developer merge scenario (Left/Right annotated diff) for semantic dependencies between their changes - either detecting them from scratch or reviewing dependencies a static-analysis tool already found. Read the full source files before reporting anything; never rely on the diff hunk alone.
---

# Semantic dependency review

The diff you're given only shows changed lines plus a few lines of context. That
is not enough to judge a semantic dependency - the state elements involved
(fields, parameters, locals) are frequently declared, assigned, or read outside
the visible hunk. Before reporting or evaluating any dependency:

1. Identify every class/file referenced by the diff and by any precomputed
   dependency you were given.
2. Use `read` to open the full current source of each one (don't guess from
   the hunk). Use `grep`/`glob` to find related files if a referenced class,
   field, or method isn't in the diff itself (e.g. a field declared in a
   superclass, a method defined in another file).
3. Trace the actual data/control flow through the real code - who reads a
   state element, who writes it, in what order - rather than pattern-matching
   on line proximity in the diff.
4. Only report a dependency (Mode 1) or confirm/reject one (Mode 2) once
   you've verified it against the real source, not just the annotated diff
   text. If you cannot verify a dependency because the relevant file isn't
   reachable, say so explicitly in your explanation rather than silently
   guessing.
5. Keep line numbers in your output anchored to the file version the diff
   itself uses for that side (Left/Right/merged) - don't renumber based on a
   file you opened that may be a different revision.

Your final answer must still be ONLY the structured result requested in the
task instructions (the JSON schema is enforced by the caller) - use these
steps to verify your findings, not to add narration to the output.
