# poker-arena

Heads-up no-limit Hold'em engine for bot-vs-bot competition.

One hand, two bots, a chip result, with stacks reset every round. Matchmaking,
the API and the frontend will be built around the engine.

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The sandbox tests need a running Docker daemon and are skipped without one.

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

`Observation` carries what a bot can see: its hole cards, the board, both
stacks, the action history, and the legal action bounds (`to_call`,
`can_check`, `can_raise`, `min_raise_to`, `max_raise_to`).
`Action.raise_to(n)` is a raise *to* a street total, not by an increment.

## Playing

```python
from poker_arena import play_hand, play_match
from poker_arena.bots import CallBot

hand = play_hand([TightBot(), CallBot()], seed=42, button=0)
hand.deltas          # (+350, -350) -- chips won per seat, always sums to zero
hand.history         # every decision, in order, enough to replay the hand

match = play_match([TightBot(), CallBot()], seed=42, hands=100)
match.final_stacks   # stacks after the last hand played
match.busted         # seat that ran out of chips, or None
```

`play_match` carries stacks forward and alternates the button, stopping
early if a seat busts.

## Submissions

A submission is a `.py` file exposing a module-level `create_bot() -> Bot`:

```python
from poker_arena import Action

class MyBot:
    name = "my-bot"
    def act(self, obs):
        return Action.call()

def create_bot():
    return MyBot()
```

`load_bot("my_bot.py")` runs it **in-process with no isolation** -- only for
bots you already trust. For anything else use `SandboxedBot`, which runs
each submission in its own throwaway container:

```bash
docker build -t poker-arena-sandbox .   # or poker_arena.sandbox.build_image()
```

```python
from poker_arena import SandboxedBot

with SandboxedBot("their_bot.py") as bot:
    result = play_hand([bot, CallBot()], seed=42, button=0)
```

Crossing any limit tears the container down and raises `SandboxError` (or
`SandboxTimeout` for a single slow decision) on the next `act()` call.

What it enforces, one container per bot:

- **Filesystem** -- nothing from the host is mounted; the submission crosses
  as text on stdin. Read-only rootfs, private capped tmpfs.
- **Network** -- `--network none`: no interface, no DNS, no inbound.
- **Privilege** -- uid 65534, `--cap-drop ALL`, `no-new-privileges`, default
  seccomp, private PID/IPC/mount namespaces, no docker socket.
- **Resources** -- memory, CPU, pids, file descriptors, a per-decision
  timeout, and a match-wide time budget.
- **Timing** -- observations are delivered a fixed interval (`pace`, default
  `act_timeout`) after that bot's last reply, so the gap cannot be used to
  time how long the opponent thought. `pace=0` disables it.

Not covered: containers share the host kernel, so a kernel exploit needs a
hardened runtime (gVisor, Kata) or a VM per match.

## Deploying

`deploy/ec2-user-data.sh` provisions an arm64 EC2 host (Docker, the image, a
nightly systemd timer). `deploy/tournament.py` runs a round-robin over a
directory of submissions and writes JSON results.
