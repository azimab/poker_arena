import os
import sys

from dotenv import load_dotenv

from poker_arena import db, tournament

load_dotenv()

HANDS = int(os.environ.get("ARENA_HANDS", 100))
SEED = int(os.environ.get("ARENA_SEED", 1))


def main() -> int:
    with db.connect() as conn:
        try:
            tournament_id = tournament.enqueue(conn, HANDS, SEED)
        except tournament.Busy as exc:
            print(exc, file=sys.stderr)
            return 1
    print(f"queued tournament {tournament_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
