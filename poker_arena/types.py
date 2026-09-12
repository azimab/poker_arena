from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Street(str, Enum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"


class ActionType(str, Enum):
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    RAISE = "raise"


class IllegalAction(Exception):
    """A bot returned an action the rules do not allow."""

    def __init__(self, seat: int, message: str):
        super().__init__(message)
        self.seat = seat


@dataclass(frozen=True)
class Action:
    type: ActionType
    amount: int = 0

    @staticmethod
    def fold() -> "Action":
        return Action(ActionType.FOLD)

    @staticmethod
    def check() -> "Action":
        return Action(ActionType.CHECK)

    @staticmethod
    def call() -> "Action":
        return Action(ActionType.CALL)

    @staticmethod
    def raise_to(amount: int) -> "Action":
        return Action(ActionType.RAISE, amount)


@dataclass(frozen=True)
class ActionRecord:
    street: Street
    seat: int
    type: ActionType
    amount: int


@dataclass(frozen=True)
class Observation:
    seat: int
    button: int
    street: Street
    hole: tuple[str, str]
    board: tuple[str, ...]
    stacks: tuple[int, int]
    committed: tuple[int, int]
    street_bets: tuple[int, int]
    pot: int
    to_call: int
    can_check: bool
    can_raise: bool
    min_raise_to: int
    max_raise_to: int
    sb: int
    bb: int
    history: tuple[ActionRecord, ...]


class Bot(Protocol):
    name: str
    def act(self, obs: Observation) -> Action: ...
