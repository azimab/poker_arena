from __future__ import annotations

import queue
import random
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from .bots import CallBot, FoldBot, RandomBot
from .engine import DEFAULT_CONFIG, HandResult, play_hand
from .sandbox import SandboxedBot, SandboxError
from .types import Action, ActionType, IllegalAction, Observation

BUILTIN = {"call": CallBot, "fold": FoldBot, "random": RandomBot}
HANDS = 100
IDLE_SECONDS = 600


class Quit(Exception):
    pass


class Game:
    name = "you"

    def __init__(self, opponent: str, source: str | None = None):
        self.opponent = opponent
        self.source = source
        self.status = "starting"
        self.error: str | None = None
        self.stacks = [DEFAULT_CONFIG.starting_stack] * 2
        self.hands = 0
        self.obs: Observation | None = None
        self.last_hand: HandResult | None = None
        self.version = 0
        self._actions: queue.Queue = queue.Queue()
        self._changed = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, action: Action, wait: float = 10.0) -> None:
        with self._changed:
            obs = self.obs
            if self.status != "your_turn":
                raise ValueError("it is not your turn")
            if action.type is ActionType.CHECK and not obs.can_check:
                raise ValueError(f"cannot check facing a bet of {obs.to_call}")
            if action.type is ActionType.RAISE and not (
                obs.can_raise and obs.min_raise_to <= action.amount <= obs.max_raise_to
            ):
                raise ValueError(f"raise must be between {obs.min_raise_to} and {obs.max_raise_to}")
            self._publish("thinking")
            version = self.version
            self._actions.put(action)
            # Answer with the bot's reply when it is quick, so clients rarely need to poll.
            self._changed.wait_for(lambda: self.version != version, timeout=wait)

    def quit(self, wait: float = 0) -> None:
        self._actions.put(None)
        with self._changed:
            self._changed.wait_for(lambda: self.status == "over", timeout=wait)

    def state(self) -> dict:
        with self._changed:
            last = None
            if self.last_hand is not None:
                last = asdict(self.last_hand)
                del last["config"], last["seed"]
                last["holes"] = [last["holes"][0], last["holes"][1] if last["showdown"] else None]
            return {
                "opponent": self.opponent,
                "status": self.status,
                "error": self.error,
                "stacks": self.stacks,
                "hands": self.hands,
                "max_hands": HANDS,
                "observation": asdict(self.obs) if self.status == "your_turn" else None,
                "last_hand": last,
            }

    def act(self, obs: Observation) -> Action:
        with self._changed:
            self.obs = obs
            self._publish("your_turn")
        try:
            action = self._actions.get(timeout=IDLE_SECONDS)
        except queue.Empty:
            raise Quit("abandoned")
        if action is None:
            raise Quit("quit")
        return action

    def _publish(self, status: str) -> None:
        self.status = status
        self.version += 1
        self._changed.notify_all()

    def _run(self) -> None:
        bot = None
        try:
            if self.source is None:
                bot = BUILTIN[self.opponent]()
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "bot.py"
                    path.write_text(self.source)
                    bot = SandboxedBot(path)
            rng = random.Random()
            while self.hands < HANDS and min(self.stacks) > 0:
                result = play_hand(
                    [self, bot],
                    seed=rng.randrange(2**31),
                    button=self.hands % 2,
                    starting_stacks=tuple(self.stacks),
                )
                with self._changed:
                    self.stacks = [s + d for s, d in zip(self.stacks, result.deltas)]
                    self.hands += 1
                    self.last_hand = result
            self._finish(None)
        except Quit as exc:
            self._finish(str(exc))
        except (SandboxError, IllegalAction) as exc:
            self._finish(str(exc).splitlines()[0])
        finally:
            if isinstance(bot, SandboxedBot):
                bot.close()

    def _finish(self, error: str | None) -> None:
        with self._changed:
            self.error = error
            self._publish("over")
