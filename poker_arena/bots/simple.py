
from __future__ import annotations

import random

from ..types import Action, Observation


class FoldBot:

    name = "fold"

    def act(self, obs: Observation) -> Action:
        return Action.check() if obs.can_check else Action.fold()


class CallBot:

    name = "call"

    def act(self, obs: Observation) -> Action:
        return Action.call()


class RandomBot:

    name = "random"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    def act(self, obs: Observation) -> Action:
        choices = ["fold", "call"]
        if obs.can_raise:
            choices.append("raise")
        choice = self.rng.choice(choices)

        if choice == "fold":
            return Action.check() if obs.can_check else Action.fold()
        if choice == "call":
            return Action.call()

        pot_sized = obs.pot + obs.to_call
        amount = min(max(pot_sized, obs.min_raise_to), obs.max_raise_to)
        return Action.raise_to(amount)
