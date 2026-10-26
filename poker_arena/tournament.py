from __future__ import annotations

import itertools
import os
import tempfile
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from . import db, jobs, rating
from .engine import DEFAULT_CONFIG
from .match import play_match
from .sandbox import SandboxedBot, SandboxError, image_exists
from .types import IllegalAction

PACE = float(os.environ.get("ARENA_PACE", 1.0))
START = DEFAULT_CONFIG.starting_stack


class Busy(Exception):
    pass


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


def enqueue(conn: psycopg.Connection, hands: int, seed: int) -> int:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('tournament-enqueue'))")
    if conn.execute(
        "SELECT 1 FROM jobs WHERE kind IN ('leg', 'finalize') AND status IN ('queued', 'running') LIMIT 1"
    ).fetchone():
        raise Busy("a tournament is already in progress")
    bots = [r["id"] for r in conn.execute("SELECT id FROM bots WHERE status = 'active' ORDER BY id")]
    if len(bots) < 2:
        raise Busy("need at least 2 active bots")
    tournament_id = conn.execute(
        "INSERT INTO tournaments (hands, seed) VALUES (%s, %s) RETURNING id", (hands, seed)
    ).fetchone()["id"]
    # Each pairing is played twice on the same seed with seats swapped, so
    # both bots are dealt the same cards and card luck largely cancels out.
    for a, b in itertools.combinations(bots, 2):
        for leg in (0, 1):
            jobs.enqueue(conn, "leg", {"tournament": tournament_id, "bot_a": a, "bot_b": b, "leg": leg})
    return tournament_id


def play(sources: list[str], hands: int, seed: int) -> dict:
    bots: list[SandboxedBot] = []
    with tempfile.TemporaryDirectory() as tmp:
        try:
            for seat, source in enumerate(sources):
                path = Path(tmp) / f"{seat}.py"
                path.write_text(source)
                try:
                    bots.append(SandboxedBot(path, pace=PACE))
                except SandboxError as exc:
                    raise Forfeit(seat, exc) from exc
            result = play_match([Seat(i, b) for i, b in enumerate(bots)], seed=seed, hands=hands)
            return {"deltas": [s - START for s in result.final_stacks], "hands": len(result.hands)}
        except (Forfeit, IllegalAction) as exc:
            return {
                "error": str(exc).splitlines()[0],
                "forfeit": exc.seat,
                "deltas": [-START if seat == exc.seat else START for seat in range(2)],
            }
        finally:
            for bot in bots:
                bot.close()


def run_leg(job: jobs.Job) -> None:
    p = job.payload
    a, b = p["bot_a"], p["bot_b"]
    order = [a, b] if p["leg"] == 0 else [b, a]
    with db.connect() as conn:
        t = conn.execute("SELECT hands, seed FROM tournaments WHERE id = %s", (p["tournament"],)).fetchone()
        sources = {
            r["id"]: r["source"]
            for r in conn.execute("SELECT id, source FROM bots WHERE id = ANY(%s)", (order,))
        }
    outcome = play([sources[i] for i in order], t["hands"], t["seed"]) | {"bots": order}
    # A dead docker daemon looks like a crashed bot, so it must be retried instead of scored.
    if "forfeit" in outcome and not image_exists():
        raise RuntimeError(f"sandbox unavailable: {outcome['error']}")
    with db.connect() as conn:
        record_leg(conn, job, outcome)


def record_leg(conn: psycopg.Connection, job: jobs.Job, outcome: dict) -> None:
    p = job.payload
    tournament_id, a, b = p["tournament"], p["bot_a"], p["bot_b"]
    # Serializes leg completions per tournament so exactly one of them sees the last leg land.
    conn.execute("SELECT 1 FROM tournaments WHERE id = %s FOR UPDATE", (tournament_id,))
    jobs.finish(conn, job)
    conn.execute(
        "INSERT INTO legs (tournament_id, bot_a, bot_b, leg, outcome) VALUES (%s, %s, %s, %s, %s)",
        (tournament_id, a, b, p["leg"], Jsonb(outcome)),
    )
    legs = [r["outcome"] for r in conn.execute(
        "SELECT outcome FROM legs WHERE tournament_id = %s AND bot_a = %s AND bot_b = %s ORDER BY leg",
        (tournament_id, a, b),
    )]
    if len(legs) == 2:
        score = sum(leg["deltas"][leg["bots"].index(a)] for leg in legs)
        conn.execute(
            "INSERT INTO matches (tournament_id, bot_a, bot_b, score, legs) VALUES (%s, %s, %s, %s, %s)",
            (tournament_id, a, b, score, Jsonb(legs)),
        )
    remaining = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE kind = 'leg' AND payload ->> 'tournament' = %s AND status <> 'done'",
        (str(tournament_id),),
    ).fetchone()["n"]
    if remaining == 0:
        jobs.enqueue(conn, "finalize", {"tournament": tournament_id})


def finalize(job: jobs.Job) -> None:
    tournament_id = job.payload["tournament"]
    with db.connect() as conn:
        jobs.finish(conn, job)
        matches = conn.execute(
            "SELECT bot_a, bot_b, score FROM matches WHERE tournament_id = %s ORDER BY bot_a, bot_b",
            (tournament_id,),
        ).fetchall()
        ids = sorted({m["bot_a"] for m in matches} | {m["bot_b"] for m in matches})
        ratings = {
            r["id"]: (r["mu"], r["sigma"])
            for r in conn.execute("SELECT id, mu, sigma FROM bots WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (ids,))
        }
        # A fixed order makes the final ratings independent of which worker finished first.
        for m in matches:
            a, b = m["bot_a"], m["bot_b"]
            ratings[a], ratings[b] = rating.rate(ratings[a], ratings[b], m["score"])
        for bot_id, (mu, sigma) in ratings.items():
            conn.execute("UPDATE bots SET mu = %s, sigma = %s WHERE id = %s", (mu, sigma, bot_id))
        conn.execute("UPDATE tournaments SET finished_at = now() WHERE id = %s", (tournament_id,))


def fail(conn: psycopg.Connection, tournament_id: int) -> None:
    conn.execute("UPDATE tournaments SET failed_at = now() WHERE id = %s AND failed_at IS NULL", (tournament_id,))
