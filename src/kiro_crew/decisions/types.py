"""Typed Jev questions and answers.

Choice selects an option; Noul returns a probability. The only business caller,
``skills.select``, uses Choice. The gate checks answer identifiers and domains
before any caller can act on a response. Probability metadata is not a guarantee
of correctness and is not used as a skill-selection threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Choice:
    """Pick one member of the declared option domain."""

    id: str
    prompt: str
    options: list[str] = field(default_factory=list)


@dataclass
class Noul:
    """Ask for the probability that a statement holds, without rounding to bool."""

    id: str
    prompt: str


Question = Choice | Noul


@dataclass
class Answer:
    """One answer, keyed by its question id in the response mapping.

    ``value`` is a chosen option or a probability. ``p`` is the probability
    supplied for that value; ``confidence`` is optional provider metadata.
    """

    id: str
    value: object
    p: float
    confidence: float | None = None


Answers = dict[str, Answer]
