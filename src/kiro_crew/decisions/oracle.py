"""The one seam every decision implementation is behind.

``decide`` (gate.py) constructs a :class:`DecisionOracle` and awaits exactly one
method on it. That is the whole extension surface: a second provider is a new
module with an ``ask``, not a change to the gate.

``ask`` returns :data:`~.types.Answers` and nothing else. An earlier revision
wrapped it in a result object carrying ``in_tokens``/``cost_usd``, which existed
only for a spend column in the log; the log records what a decision COST in
latency and whether it failed, so the wrapper was a type every implementation and
every test had to name for a field nothing read.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from kiro_crew.decisions.types import Answers, Question


@runtime_checkable
class DecisionOracle(Protocol):
    """Answer *questions* against *state*, or raise.

    An implementation NEVER returns a partial or empty mapping to signal
    failure: it raises, and the gate converts that into ``None`` plus a logged
    error category. Two reasons the direction matters. A caller that got an empty
    mapping back could not tell "the provider said nothing" from "there was
    nothing to ask", so it would have to re-derive the question list to find out.
    And an implementation that swallowed its own exception would put
    ``error: null`` on a row that in fact never reached the model.

    Timeouts are the gate's job, not the implementation's: the gate wraps this
    call in ``asyncio.wait_for(provider.timeout_ms)`` so one budget governs
    every implementation. An implementation MAY pass its own transport timeout
    as well, but must not make the deadline longer than the gate's.
    """

    async def ask(self, state: dict | str, questions: list[Question]) -> Answers:
        """Evaluate every question in *questions* against *state*."""
        ...
