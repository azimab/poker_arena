#Heads-up no-limit hold'em engine

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import cards
from .types import (
    Action,
    ActionRecord,
    ActionType,
    Bot,
    IllegalAction,
    Observation,
    Street,
)

_MAX_DECISIONS_PER_ROUND = 1000


@dataclass(frozen=True)
class TableConfig:
    starting_stack: int = 10_000
    sb: int = 50
    bb: int = 100


DEFAULT_CONFIG = TableConfig()


@dataclass(frozen=True)
class HandResult:
    seed: int
    button: int
    holes: tuple[tuple[str, str], tuple[str, str]]
    board: tuple[str, ...]
    history: tuple[ActionRecord, ...]
    pot: int
    deltas: tuple[int, int]
    winners: tuple[int, ...]
    showdown: bool
    hand_classes: tuple[str, str] | None
    config: TableConfig


def play_hand(
    bots: Sequence[Bot],
    seed: int,
    button: int = 0,
    config: TableConfig = DEFAULT_CONFIG,
    starting_stacks: tuple[int, int] | None = None,
) -> HandResult:
    """Play one hand between `bots[0]` and `bots[1]` and return the result.

    `starting_stacks` overrides `config.starting_stack` per seat, so a match
    runner can carry stacks forward from one hand to the next.
    """
    return _Hand(bots, seed, button, config, starting_stacks).run()


class _Hand:
    def __init__(
        self,
        bots: Sequence[Bot],
        seed: int,
        button: int,
        config: TableConfig,
        starting_stacks: tuple[int, int] | None = None,
    ):
        if len(bots) != 2:
            raise ValueError("heads-up takes exactly 2 bots")
        if button not in (0, 1):
            raise ValueError("button must be seat 0 or 1")

        self.bots = bots
        self.seed = seed
        self.button = button
        self.cfg = config
        self.starting_stacks = starting_stacks or (config.starting_stack, config.starting_stack)

        deck = cards.shuffled_deck(seed)
        self.holes = [[deck.pop(), deck.pop()], [deck.pop(), deck.pop()]]
        self.runout = [deck.pop() for _ in range(5)]

        self.board: list[int] = []
        self.stacks = list(self.starting_stacks)
        self.committed = [0, 0]
        self.street_bets = [0, 0]
        self.folded = [False, False]
        self.all_in = [False, False]
        self.history: list[ActionRecord] = []
        self.street = Street.PREFLOP
        self.last_raise = config.bb


    def run(self) -> HandResult:
        sb_seat, bb_seat = self.button, 1 - self.button
        self._commit(sb_seat, self.cfg.sb)
        self._commit(bb_seat, self.cfg.bb)

        # Heads-up: the button acts first preflop, last on every other street.
        self._betting_round(first=self.button)

        for street, revealed in ((Street.FLOP, 3), (Street.TURN, 4), (Street.RIVER, 5)):
            if self._folded_out():
                break
            self.street = street
            self.board = self.runout[:revealed]
            self.street_bets = [0, 0]
            # Someone all-in means no chips are left to move; just run the board out.
            if not (self.all_in[0] or self.all_in[1]):
                self._betting_round(first=1 - self.button)

        return self._settle()

    def _betting_round(self, first: int) -> None:
        self.last_raise = self.cfg.bb
        acted = [False, False]
        seat = first

        for _ in range(_MAX_DECISIONS_PER_ROUND):
            if self._round_complete(acted):
                return
            if self._can_act(seat, acted):
                action = self._decide(seat)
                self._apply(seat, action, acted)
            seat = 1 - seat

        raise RuntimeError("betting round failed to terminate")

    def _round_complete(self, acted: list[bool]) -> bool:
        live = [s for s in (0, 1) if not self.folded[s] and not self.all_in[s]]
        if self._folded_out() or not live:
            return True
        highest = max(self.street_bets)
        return all(acted[s] and self.street_bets[s] == highest for s in live)

    def _can_act(self, seat: int, acted: list[bool]) -> bool:
        if self.folded[seat] or self.all_in[seat]:
            return False
        # Already acted and owes nothing: only a later raise brings them back.
        return not acted[seat] or self.street_bets[seat] < max(self.street_bets)

    def _folded_out(self) -> bool:
        return self.folded[0] or self.folded[1]

    # -- one decision ----------------------------------------------------

    def _decide(self, seat: int) -> Action:
        action = self.bots[seat].act(self._observe(seat))
        if not isinstance(action, Action):
            raise IllegalAction(f"seat {seat} returned {action!r}, not an Action")
        return action

    def _apply(self, seat: int, action: Action, acted: list[bool]) -> None:
        opp = 1 - seat
        to_call, can_check, can_raise, min_to, max_to = self._legal(seat)
        acted[seat] = True

        if action.type is ActionType.FOLD:
            self.folded[seat] = True
            self._record(seat, ActionType.FOLD, 0)

        elif action.type is ActionType.CHECK:
            if not can_check:
                raise IllegalAction(f"seat {seat} checked facing a bet of {to_call}")
            self._record(seat, ActionType.CHECK, 0)

        elif action.type is ActionType.CALL:
            # Calling nothing is a check; treat it as one rather than rejecting it.
            self._commit(seat, to_call)
            self._record(seat, ActionType.CALL if to_call else ActionType.CHECK, to_call)

        elif action.type is ActionType.RAISE:
            if not can_raise:
                raise IllegalAction(f"seat {seat} cannot raise here")
            if not min_to <= action.amount <= max_to:
                raise IllegalAction(
                    f"seat {seat} raised to {action.amount}, legal range is {min_to}-{max_to}"
                )
            increment = action.amount - self.street_bets[opp]
            put_in = action.amount - self.street_bets[seat]
            self._commit(seat, put_in)
            # A short all-in raise does not raise the bar for the next raise.
            self.last_raise = max(self.last_raise, increment)
            acted[opp] = False
            self._record(seat, ActionType.RAISE, put_in)

        else:
            raise IllegalAction(f"seat {seat} returned unknown action {action.type!r}")

    def _legal(self, seat: int) -> tuple[int, bool, bool, int, int]:
        opp = 1 - seat
        owed = self.street_bets[opp] - self.street_bets[seat]
        to_call = min(max(owed, 0), self.stacks[seat])
        can_check = owed <= 0
        max_to = self.street_bets[seat] + self.stacks[seat]
        # Raising needs an opponent with chips left to answer it.
        can_raise = self.stacks[opp] > 0 and max_to > self.street_bets[opp]
        min_to = min(self.street_bets[opp] + self.last_raise, max_to)
        return to_call, can_check, can_raise, min_to, max_to

    def _observe(self, seat: int) -> Observation:
        to_call, can_check, can_raise, min_to, max_to = self._legal(seat)
        hole = self.holes[seat]
        return Observation(
            seat=seat,
            button=self.button,
            street=self.street,
            hole=(cards.to_str(hole[0]), cards.to_str(hole[1])),
            board=tuple(cards.to_str(c) for c in self.board),
            stacks=(self.stacks[0], self.stacks[1]),
            committed=(self.committed[0], self.committed[1]),
            street_bets=(self.street_bets[0], self.street_bets[1]),
            pot=sum(self.committed),
            to_call=to_call,
            can_check=can_check,
            can_raise=can_raise,
            min_raise_to=min_to,
            max_raise_to=max_to,
            sb=self.cfg.sb,
            bb=self.cfg.bb,
            history=tuple(self.history),
        )

    def _commit(self, seat: int, amount: int) -> None:
        amount = min(amount, self.stacks[seat])
        self.stacks[seat] -= amount
        self.street_bets[seat] += amount
        self.committed[seat] += amount
        if self.stacks[seat] == 0:
            self.all_in[seat] = True

    def _record(self, seat: int, type_: ActionType, amount: int) -> None:
        self.history.append(ActionRecord(self.street, seat, type_, amount))


    def _settle(self) -> HandResult:
        # Return the uncalled part of the last bet before anyone is paid.
        over, under = (0, 1) if self.committed[0] > self.committed[1] else (1, 0)
        uncalled = self.committed[over] - self.committed[under]
        if uncalled:
            self.stacks[over] += uncalled
            self.committed[over] -= uncalled

        pot = sum(self.committed)
        showdown = not self._folded_out()
        hand_classes = None

        if not showdown:
            winners = (1,) if self.folded[0] else (0,)
        else:
            scores = [cards.evaluate(self.holes[s], self.runout) for s in (0, 1)]
            hand_classes = (cards.hand_class(scores[0]), cards.hand_class(scores[1]))
            if scores[0] == scores[1]:
                winners = (0, 1)
            else:
                winners = (0,) if scores[0] < scores[1] else (1,)

        for seat in winners:
            self.stacks[seat] += pot // len(winners)

        return HandResult(
            seed=self.seed,
            button=self.button,
            holes=tuple(
                tuple(cards.to_str(c) for c in self.holes[s]) for s in (0, 1)
            ),
            board=tuple(cards.to_str(c) for c in self.board),
            history=tuple(self.history),
            pot=pot,
            deltas=tuple(self.stacks[s] - self.starting_stacks[s] for s in (0, 1)),
            winners=winners,
            showdown=showdown,
            hand_classes=hand_classes,
            config=self.cfg,
        )
