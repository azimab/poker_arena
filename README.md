# poker-arena

Heads-up no-limit Hold'em engine for bot-vs-bot competition.

Basic Rules: one hand, two bots, a chip result, and stacks are reset every round. Matchmaking,
sandboxing, the API and the frontend will be built around the engine.

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Writing a bot

A bot is anything with a `name` and an `act` method:

```python
from poker_arena import Action, Observation

class TightBot:
    name = "tight"

    def act(self, obs: Observation) -> Action:
        if obs.to_call == 0:
            return Action.check()
        if obs.to_call > obs.pot // 2:
            return Action.fold()
        return Action.call()
```

`Observation` carries what each can see: its hole cards, the
board, both stacks, the action history, and the legal action bounds
(`to_call`, `can_check`, `can_raise`, `min_raise_to`, `max_raise_to`).
`Action.raise_to(n)` is a raise *to* a street total, not by an increment.


## Playing a hand

```python
from poker_arena import play_hand
from poker_arena.bots import CallBot

result = play_hand([TightBot(), CallBot()], seed=42, button=0)
result.deltas   # (+350, -350) -- chips won by each seat, always sums to zero
result.history  # every decision, in order, enough to replay the hand
```

## Playing a match

`play_match` plays a series of hands, carrying stacks forward and
alternating the button, stopping early if a seat busts:

```python
from poker_arena import play_match

result = play_match([TightBot(), CallBot()], seed=42, hands=100)
result.final_stacks  # (10_650, 9_350) -- stacks after the last hand played
result.busted        # seat index that ran out of chips, or None
result.hands         # every HandResult in order
```


