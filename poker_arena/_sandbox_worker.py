"""Entry point for a sandboxed bot subprocess -- run inside the container, not imported."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

BOT_PATH = "/tmp/bot.py"


def _claim_stdout():
    # The protocol gets a private copy of fd 1; whatever the bot prints goes to stderr.
    channel = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return channel


def main() -> None:
    channel = _claim_stdout()

    def send(payload: dict) -> None:
        channel.write(json.dumps(payload) + "\n")
        channel.flush()

    from .sandbox import _decode_observation
    from .submission import BotLoadError, load_bot
    from .types import Action

    with open(BOT_PATH, "w") as f:
        f.write(json.loads(sys.stdin.readline())["source"])

    try:
        bot = load_bot(BOT_PATH)
    except BotLoadError as exc:
        send({"error": str(exc)})
        return

    send({"name": bot.name})

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            action = bot.act(_decode_observation(json.loads(line)))
            if not isinstance(action, Action):
                raise TypeError(f"act() must return an Action, got {type(action).__name__}")
            send(asdict(action))
        except Exception as exc:
            send({"error": f"{type(exc).__name__}: {exc}"})
            return


if __name__ == "__main__":
    main()
