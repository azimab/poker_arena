import itertools
import json
import os
import pathlib
import sys
import tempfile

from psycopg.types.json import Jsonb

from poker_arena import DEFAULT_CONFIG, IllegalAction, SandboxError, SandboxedBot, db, play_match, rating
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
        return {
            "error": str(exc).splitlines()[0],
            "forfeit": exc.seat,
            "deltas": [-START if seat == exc.seat else START for seat in range(2)],
        }
    finally:
        for bot in bots:
            bot.close()


def record(a: int, b: int, score: int, legs: list[dict]) -> None:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, mu, sigma FROM bots WHERE id = ANY(%s) FOR UPDATE", ([a, b],)
        ).fetchall()
        current = {r["id"]: (r["mu"], r["sigma"]) for r in rows}
        for bot_id, (mu, sigma) in zip((a, b), rating.rate(current[a], current[b], score)):
            conn.execute("UPDATE bots SET mu = %s, sigma = %s WHERE id = %s", (mu, sigma, bot_id))
        conn.execute(
            "INSERT INTO matches (bot_a, bot_b, score, legs) VALUES (%s, %s, %s, %s)",
            (a, b, score, Jsonb(legs)),
        )


def main() -> int:
    # Checked up front so a missing image or dead daemon aborts the run instead
    # of being blamed on whichever bot happened to load first.
    if not image_exists():
        print(f"sandbox image {IMAGE!r} is unavailable (is docker running?)", file=sys.stderr)
        return 1
    with db.connect() as conn:
        bots = conn.execute("SELECT id, source FROM bots WHERE status = 'active' ORDER BY id").fetchall()
    if len(bots) < 2:
        print("need at least 2 active bots", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for bot in bots:
            paths[bot["id"]] = pathlib.Path(tmp) / f"{bot['id']}.py"
            paths[bot["id"]].write_text(bot["source"])

        for a, b in itertools.combinations(paths, 2):
            # Each pairing is played twice on the same seed with seats swapped, so
            # both bots are dealt the same cards and card luck largely cancels out.
            legs, score = [], 0
            for order in ((a, b), (b, a)):
                outcome = play((paths[order[0]], paths[order[1]])) | {"bots": list(order)}
                score += outcome["deltas"][order.index(a)]
                legs.append(outcome)
                print(json.dumps(outcome), flush=True)
            record(a, b, score, legs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
