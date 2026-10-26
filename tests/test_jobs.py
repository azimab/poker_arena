import os
import shutil
import subprocess
import time

import pytest

pytest.importorskip("psycopg")

from poker_arena import db, jobs, rating, tournament, worker
from poker_arena.sandbox import build_image, image_exists

DB_URL = os.environ.get("ARENA_TEST_DATABASE_URL")
HAS_DOCKER = shutil.which("docker") is not None and subprocess.run(
    ["docker", "info"], capture_output=True
).returncode == 0

pytestmark = pytest.mark.skipif(DB_URL is None, reason="needs ARENA_TEST_DATABASE_URL")

BOTS = {
    "caller": "Action.check() if obs.can_check else Action.call()",
    "folder": "Action.check() if obs.can_check else Action.fold()",
    "crasher": "1 / 0",
}


def bot_source(name: str, act: str) -> str:
    return f"""
from poker_arena import Action

class B:
    name = {name!r}
    def act(self, obs):
        return {act}

def create_bot():
    return B()
"""


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("ARENA_DATABASE_URL", DB_URL)
    with db.connect() as conn:
        conn.execute("DROP TABLE IF EXISTS jobs, legs, matches, tournaments, bots, sessions, accounts")
        conn.commit()
        db.init_schema(conn)
        yield conn


def test_reclaimed_job_cannot_be_finished_by_its_old_owner(conn):
    job_id = jobs.enqueue(conn, "check", {"bot": 1})
    conn.commit()
    stale = jobs.claim(conn, "a")
    assert jobs.claim(conn, "b") is None

    conn.execute("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id = %s", (job_id,))
    conn.commit()
    fresh = jobs.claim(conn, "b")
    assert (fresh.id, fresh.attempts) == (job_id, 2)

    with pytest.raises(jobs.LostLease):
        jobs.finish(conn, stale)
    conn.rollback()
    jobs.finish(conn, fresh)
    assert conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()["status"] == "done"


def test_failing_leg_backs_off_then_fails_tournament_until_requeued(conn, monkeypatch):
    tournament_id = conn.execute("INSERT INTO tournaments (hands, seed) VALUES (1, 1) RETURNING id").fetchone()["id"]
    job_id = jobs.enqueue(conn, "leg", {"tournament": tournament_id}, max_attempts=2)
    conn.commit()

    def explode(job):
        raise RuntimeError("docker went away")

    monkeypatch.setitem(worker.HANDLERS, "leg", explode)
    w = worker.Worker(concurrency=0)

    assert w.run_one()
    job = conn.execute("SELECT status, error, run_after > now() AS delayed FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert (job["status"], job["delayed"]) == ("queued", True)
    assert "docker went away" in job["error"]
    assert not w.run_one()

    conn.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (job_id,))
    conn.commit()
    assert w.run_one()
    assert conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()["status"] == "dead"
    assert conn.execute("SELECT failed_at FROM tournaments WHERE id = %s", (tournament_id,)).fetchone()["failed_at"]

    worker.main(["requeue", str(job_id)])
    conn.commit()
    assert conn.execute("SELECT status, attempts FROM jobs WHERE id = %s", (job_id,)).fetchone() == {
        "status": "queued", "attempts": 0,
    }
    assert conn.execute("SELECT failed_at FROM tournaments WHERE id = %s", (tournament_id,)).fetchone()["failed_at"] is None


@pytest.mark.skipif(not HAS_DOCKER, reason="needs a running docker daemon")
def test_parallel_tournament_matches_serial_ratings(conn, monkeypatch):
    if not image_exists():
        build_image()
    monkeypatch.setattr(tournament, "PACE", 0)
    ids = []
    for name, act in BOTS.items():
        account = conn.execute("INSERT INTO accounts (username) VALUES (%s) RETURNING id", (name,)).fetchone()["id"]
        ids.append(conn.execute(
            "INSERT INTO bots (account_id, name, source, status) VALUES (%s, %s, %s, 'active') RETURNING id",
            (account, name, bot_source(name, act)),
        ).fetchone()["id"])
    tournament_id = tournament.enqueue(conn, hands=5, seed=3)
    conn.commit()
    with pytest.raises(tournament.Busy):
        tournament.enqueue(conn, hands=5, seed=3)
    conn.rollback()

    w = worker.Worker(concurrency=6)
    w.start()
    try:
        for _ in range(600):
            done = conn.execute("SELECT finished_at FROM tournaments WHERE id = %s", (tournament_id,)).fetchone()
            conn.commit()
            if done["finished_at"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("tournament did not finish")
    finally:
        w.stop()

    matches = conn.execute(
        "SELECT bot_a, bot_b, score, legs FROM matches WHERE tournament_id = %s ORDER BY bot_a, bot_b",
        (tournament_id,),
    ).fetchall()
    assert [(m["bot_a"], m["bot_b"]) for m in matches] == [(ids[0], ids[1]), (ids[0], ids[2]), (ids[1], ids[2])]
    assert all(leg["forfeit"] == leg["bots"].index(ids[2]) for m in matches[1:] for leg in m["legs"])

    expected = {bot_id: (25.0, 25.0 / 3) for bot_id in ids}
    for m in matches:
        a, b = m["bot_a"], m["bot_b"]
        expected[a], expected[b] = rating.rate(expected[a], expected[b], m["score"])
    rows = conn.execute("SELECT mu, sigma FROM bots WHERE id = ANY(%s) ORDER BY id", (ids,)).fetchall()
    assert [v for r in rows for v in (r["mu"], r["sigma"])] == pytest.approx([v for i in ids for v in expected[i]])
    assert conn.execute("SELECT count(*) AS n FROM jobs WHERE status <> 'done'").fetchone()["n"] == 0
