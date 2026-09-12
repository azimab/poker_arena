import itertools
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

from poker_arena import DEFAULT_CONFIG, IllegalAction, SandboxError, SandboxedBot, play_match
from poker_arena.sandbox import IMAGE, image_exists

HANDS = int(os.environ.get("ARENA_HANDS", 100))
PACE = float(os.environ.get("ARENA_PACE", 1.0))
SEED = int(os.environ.get("ARENA_SEED", 1))
START = DEFAULT_CONFIG.starting_stack


class Forfeit(Exception):
    def __init__(self, seat: int, cause: Exception):
        super().__init__(str(cause))
        self.seat = seat


class Seat:
    def __init__(self, seat: int, bot: SandboxedBot):
        self.seat = seat
        self.bot = bot
        self.name = bot.name

    def act(self, obs):
        try:
            return self.bot.act(obs)
        except SandboxError as exc:
            raise Forfeit(self.seat, exc) from exc


def play(paths: tuple[pathlib.Path, pathlib.Path]) -> dict:
    bots: list[SandboxedBot] = []
    try:
        for seat, path in enumerate(paths):
            try:
                bots.append(SandboxedBot(path, pace=PACE))
            except SandboxError as exc:
                raise Forfeit(seat, exc) from exc
        result = play_match([Seat(i, b) for i, b in enumerate(bots)], seed=SEED, hands=HANDS)
        return {
            "deltas": [s - START for s in result.final_stacks],
            "hands": len(result.hands),
        }
    except (Forfeit, IllegalAction) as exc:
        return {"error": str(exc).splitlines()[0], "forfeit": exc.seat}
    finally:
        for bot in bots:
            bot.close()


def main(submissions: pathlib.Path, results: pathlib.Path) -> int:
    paths = sorted(submissions.glob("*.py"))
    if len(paths) < 2:
        print(f"need at least 2 submissions in {submissions}", file=sys.stderr)
        return 1
    # Checked up front so a missing image or dead daemon aborts the run instead
    # of being blamed on whichever bot happened to load first.
    if not image_exists():
        print(f"sandbox image {IMAGE!r} is unavailable (is docker running?)", file=sys.stderr)
        return 1

    standings = {p.stem: 0 for p in paths}
    pairings = []
    for a, b in itertools.combinations(paths, 2):
        # Each pairing is played twice on the same seed with seats swapped, so
        # both bots are dealt the same cards and card luck largely cancels out.
        legs = []
        for order in ((a, b), (b, a)):
            outcome = play(order) | {"bots": [p.stem for p in order]}
            if "deltas" in outcome:
                for path, delta in zip(order, outcome["deltas"]):
                    standings[path.stem] += delta
            else:
                standings[order[outcome["forfeit"]].stem] -= START
            legs.append(outcome)
            print(json.dumps(outcome), flush=True)
        pairings.append({"bots": [a.stem, b.stem], "legs": legs})

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results.mkdir(parents=True, exist_ok=True)
    (results / f"{stamp}.json").write_text(
        json.dumps(
            {
                "finished": stamp,
                "hands": HANDS,
                "pace": PACE,
                "seed": SEED,
                "standings": sorted(standings.items(), key=lambda kv: -kv[1]),
                "pairings": pairings,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])))
