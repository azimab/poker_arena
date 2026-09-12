import itertools
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

from poker_arena import DEFAULT_CONFIG, SandboxError, SandboxedBot, play_match

HANDS = int(os.environ.get("ARENA_HANDS", 100))
PACE = float(os.environ.get("ARENA_PACE", 1.0))
SEED = int(os.environ.get("ARENA_SEED", 1))
START = DEFAULT_CONFIG.starting_stack


def play(a: pathlib.Path, b: pathlib.Path) -> dict:
    try:
        with SandboxedBot(a, pace=PACE) as x, SandboxedBot(b, pace=PACE) as y:
            result = play_match([x, y], seed=SEED, hands=HANDS)
        return {
            "deltas": [s - START for s in result.final_stacks],
            "hands": len(result.hands),
        }
    except SandboxError as exc:
        # Whichever bot the sandbox named forfeits; if it named neither (a
        # daemon or image problem) the pairing is void and scores nothing.
        message = str(exc)
        loser = next((i for i, p in enumerate((a, b)) if repr(p.stem) in message), None)
        return {"error": message.splitlines()[0], "forfeit": loser}


def main(submissions: pathlib.Path, results: pathlib.Path) -> int:
    paths = sorted(submissions.glob("*.py"))
    if len(paths) < 2:
        print(f"need at least 2 submissions in {submissions}", file=sys.stderr)
        return 1

    standings = {p.stem: 0 for p in paths}
    pairings = []
    for a, b in itertools.combinations(paths, 2):
        outcome = play(a, b) | {"bots": [a.stem, b.stem]}
        if "deltas" in outcome:
            standings[a.stem] += outcome["deltas"][0]
            standings[b.stem] += outcome["deltas"][1]
        elif outcome["forfeit"] is not None:
            standings[(a, b)[outcome["forfeit"]].stem] -= START
        pairings.append(outcome)
        print(json.dumps(outcome), flush=True)

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
