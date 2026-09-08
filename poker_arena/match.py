from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

from .engine import DEFAULT_CONFIG, HandResult, TableConfig, play_hand
from .types import Bot


@dataclass(frozen=True)
class MatchResult:
    hands: tuple[HandResult, ...]
    final_stacks: tuple[int, int]
    busted: int | None


def play_match(
    bots: Sequence[Bot],
    seed: int,
    hands: int,
    config: TableConfig = DEFAULT_CONFIG,
) -> MatchResult:
    """Play up to `hands` hands between `bots[0]` and `bots[1]`, carrying
    stacks forward and alternating the button every hand. Stops early if a
    seat busts.
    """
    if len(bots) != 2:
        raise ValueError("heads-up takes exactly 2 bots")

    rng = random.Random(seed)
    stacks = [config.starting_stack, config.starting_stack]
    results: list[HandResult] = []
    busted: int | None = None

    for i in range(hands):
        if min(stacks) <= 0:
            busted = 0 if stacks[0] <= 0 else 1
            break
        result = play_hand(
            bots,
            seed=rng.randrange(2**31),
            button=i % 2,
            config=config,
            starting_stacks=(stacks[0], stacks[1]),
        )
        results.append(result)
        stacks[0] += result.deltas[0]
        stacks[1] += result.deltas[1]

    if busted is None and min(stacks) <= 0:
        busted = 0 if stacks[0] <= 0 else 1

    return MatchResult(
        hands=tuple(results),
        final_stacks=(stacks[0], stacks[1]),
        busted=busted,
    )
