from __future__ import annotations

import logging
import os
import queue
import random
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from .bots import CallBot, FoldBot, RandomBot
from .engine import DEFAULT_CONFIG, HandResult, play_hand
from .sandbox import SandboxedBot
from .types import Action, ActionType, Observation

log = logging.getLogger(__name__)

BUILTIN = {"call": CallBot, "fold": FoldBot, "random": RandomBot}
HANDS = 100
IDLE_SECONDS = 600
sandbox_slots = threading.BoundedSemaphore(int(os.environ.get("ARENA_SANDBOX_GAMES", 8)))


class Busy(Exception):
    pass


class Quit(Exception):
    pass


class Game:
    name = "you"

    def __init__(self, opponent: str, source: str | None = None):
        if source is not None and not sandbox_slots.acquire(blocking=False):
            raise Busy("too many games against submitted bots are running, try again later")
        self.opponent = opponent
        self.source = source
        self.status = "starting"
        self.error: str | None = None
        self.stacks = [DEFAULT_CONFIG.starting_stack] * 2
        self.hands = 0
        self.obs: Observation | None = None
        self.recent: list[HandResult] = []
        self._bot = None
        self._quit = False
        self._actions: queue.Queue = queue.Queue()
        self._changed = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, action: Action, wait: float) -> None:
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
            self.recent = []
            self._publish("thinking")
            self._actions.put(action)
            self._changed.wait_for(lambda: self.status != "thinking", timeout=wait)

    def quit(self, wait: float = 0) -> None:
        with self._changed:
            self._quit = True
        self._actions.put(None)
        # Closing the sandbox unblocks a bot that is mid-decision, so its container goes away now.
        if isinstance(self._bot, SandboxedBot):
            self._bot.close()
        with self._changed:
            self._changed.wait_for(lambda: self.status == "over", timeout=wait)

    def state(self) -> dict:
        with self._changed:
            recent = []
            for hand in self.recent:
                hand = asdict(hand)
                del hand["config"], hand["seed"]
                hand["holes"] = [hand["holes"][0], hand["holes"][1] if hand["showdown"] else None]
                recent.append(hand)
            return {
                "opponent": self.opponent,
                "status": self.status,
                "error": self.error,
                "stacks": self.stacks,
                "hands": self.hands,
                "max_hands": HANDS,
                "observation": asdict(self.obs) if self.status == "your_turn" else None,
                "recent_hands": recent,
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
        self._changed.notify_all()

    def _run(self) -> None:
        try:
            if self.source is None:
                self._bot = BUILTIN[self.opponent]()
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "bot.py"
                    path.write_text(self.source)
                    self._bot = SandboxedBot(path)
                if self._quit:
                    raise Quit("quit")
            rng = random.Random()
            while self.hands < HANDS and min(self.stacks) > 0:
                result = play_hand(
                    [self, self._bot],
                    seed=rng.randrange(2**31),
                    button=self.hands % 2,
                    starting_stacks=tuple(self.stacks),
                )
                with self._changed:
                    self.stacks = [s + d for s, d in zip(self.stacks, result.deltas)]
                    self.hands += 1
                    self.recent.append(result)
            self._finish(None)
        except Exception as exc:
            if self._quit or isinstance(exc, Quit):
                self._finish(str(exc) if isinstance(exc, Quit) else "quit")
            else:
                log.exception("game against %s failed", self.opponent)
                self._finish((str(exc).splitlines() or [type(exc).__name__])[0])
        finally:
            if isinstance(self._bot, SandboxedBot):
                self._bot.close()
            if self.source is not None:
                sandbox_slots.release()

    def _finish(self, error: str | None) -> None:
        with self._changed:
            self.error = error
            self._publish("over")
