"""Card helpers. Internally cards are `treys` integers; bots only ever see strings."""

from __future__ import annotations

import random

from treys import Card, Evaluator

RANKS = "23456789TJQKA"
SUITS = "shdc"

DECK: tuple[int, ...] = tuple(Card.new(r + s) for r in RANKS for s in SUITS)

_EVALUATOR = Evaluator()


def to_str(card: int) -> str:
    """`Card.new('As') -> 'As'`"""
    return Card.int_to_str(card)


def shuffled_deck(seed: int) -> list[int]:
    """A deterministic shuffle. The same seed always produces the same deck."""
    deck = list(DECK)
    random.Random(seed).shuffle(deck)
    return deck


def evaluate(hole: list[int], board: list[int]) -> int:
    """Hand strength, where *lower is better* (1 = royal flush)."""
    return _EVALUATOR.evaluate(hole, board)


def hand_class(score: int) -> str:
    """Human-readable class of an `evaluate` score, e.g. "Two Pair"."""
    return _EVALUATOR.class_to_string(_EVALUATOR.get_rank_class(score))
