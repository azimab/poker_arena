from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

from .bots import CallBot, RandomBot
from .match import play_match
from .sandbox import SandboxedBot, SandboxError
from .types import Bot, IllegalAction

CHECK_HANDS = 25


class BotLoadError(Exception):
    """A submitted bot script could not be loaded into a valid, working Bot."""


def check_submission(source: str) -> str:
    """Play short sandboxed matches from both seats; returns the bot's name or raises BotLoadError."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot.py"
        path.write_text(source)
        try:
            with SandboxedBot(path, pace=0) as bot:
                play_match([bot, RandomBot()], seed=0, hands=CHECK_HANDS)
                play_match([CallBot(), bot], seed=1, hands=CHECK_HANDS)
                return bot.name
        except (SandboxError, IllegalAction) as exc:
            raise BotLoadError(str(exc)) from exc


def load_bot(path: str | Path) -> Bot:
    """Load a submitted bot script.

    The script must be a Python file exposing a module-level `create_bot()`
    function returning a `Bot`. Loading calls it exactly once, so call
    `load_bot` again whenever a fresh, state-free instance is needed --
    a submission must never be reused across matches.
    """
    path = Path(path)
    if not path.is_file():
        raise BotLoadError(f"{path} is not a file")

    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise BotLoadError(f"{path} could not be loaded as a python module")

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise BotLoadError(f"{path} raised on import: {exc}") from exc

    factory = getattr(module, "create_bot", None)
    if not callable(factory):
        raise BotLoadError(f"{path} must define a create_bot() function")

    try:
        bot = factory()
    except Exception as exc:
        raise BotLoadError(f"{path}: create_bot() raised: {exc}") from exc

    _check_shape(bot, path)
    return bot


def _check_shape(bot: object, path: Path) -> None:
    name = getattr(bot, "name", None)
    if not isinstance(name, str) or not name:
        raise BotLoadError(f"{path}: create_bot() must return a bot with a non-empty name")
    if not callable(getattr(bot, "act", None)):
        raise BotLoadError(f"{path}: create_bot() must return a bot with an act(observation) method")
