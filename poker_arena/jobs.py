from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

LEASE = "60 seconds"
BACKOFF_SECONDS = 30


class LostLease(Exception):
    """The job was reclaimed by another worker, so this attempt's results must not be written."""


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int
    max_attempts: int
    owner: str


def enqueue(conn: psycopg.Connection, kind: str, payload: dict, priority: int = 0, max_attempts: int = 3) -> int:
    return conn.execute(
        "INSERT INTO jobs (kind, payload, priority, max_attempts) VALUES (%s, %s, %s, %s) RETURNING id",
        (kind, Jsonb(payload), priority, max_attempts),
    ).fetchone()["id"]


def claim(conn: psycopg.Connection, owner: str) -> Job | None:
    # An expired lease means its worker died or stalled, so the job is fair game again.
    row = conn.execute(
        "UPDATE jobs SET status = 'running', attempts = attempts + 1, "
        "locked_by = %(owner)s, locked_until = now() + %(lease)s::interval "
        "WHERE id = ("
        "  SELECT id FROM jobs "
        "  WHERE (status = 'queued' AND run_after <= now()) OR (status = 'running' AND locked_until < now()) "
        "  ORDER BY priority DESC, id LIMIT 1 FOR UPDATE SKIP LOCKED"
        ") RETURNING id, kind, payload, attempts, max_attempts",
        {"owner": owner, "lease": LEASE},
    ).fetchone()
    conn.commit()
    return None if row is None else Job(owner=owner, **row)


def heartbeat(conn: psycopg.Connection, owner: str) -> None:
    conn.execute(
        "UPDATE jobs SET locked_until = now() + %s::interval WHERE status = 'running' AND locked_by = %s",
        (LEASE, owner),
    )


def finish(conn: psycopg.Connection, job: Job) -> None:
    _fenced(conn, job, "status = 'done', finished_at = now(), error = NULL")


def retry(conn: psycopg.Connection, job: Job, error: str) -> None:
    _fenced(
        conn, job,
        "status = 'queued', error = %(error)s, "
        "run_after = now() + make_interval(secs => %(backoff)s)",
        error=error, backoff=BACKOFF_SECONDS * 2 ** (job.attempts - 1),
    )


def bury(conn: psycopg.Connection, job: Job, error: str) -> None:
    _fenced(conn, job, "status = 'dead', finished_at = now(), error = %(error)s", error=error)


def requeue(conn: psycopg.Connection, ids: list[int] | None = None) -> list[Job]:
    rows = conn.execute(
        "UPDATE jobs SET status = 'queued', attempts = 0, run_after = now(), finished_at = NULL "
        "WHERE status = 'dead' AND (%(ids)s::bigint[] IS NULL OR id = ANY(%(ids)s)) "
        "RETURNING id, kind, payload, attempts, max_attempts",
        {"ids": ids},
    ).fetchall()
    return [Job(owner="", **row) for row in rows]


def _fenced(conn: psycopg.Connection, job: Job, assignments: str, **params) -> None:
    # Matching the attempt as well as the owner means a worker whose lease lapsed
    # cannot finish a job that was reclaimed, even by its own process.
    cur = conn.execute(
        f"UPDATE jobs SET {assignments}, locked_by = NULL, locked_until = NULL "
        "WHERE id = %(id)s AND status = 'running' AND locked_by = %(owner)s AND attempts = %(attempts)s",
        {"id": job.id, "owner": job.owner, "attempts": job.attempts, **params},
    )
    if cur.rowcount != 1:
        raise LostLease(f"job {job.id} attempt {job.attempts} no longer belongs to {job.owner}")
