from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading

from dotenv import load_dotenv

# tournament reads ARENA_PACE at import, so this has to run first.
load_dotenv()

from . import db, jobs, tournament
from .sandbox import IMAGE, image_exists
from .submission import BotLoadError, check_submission

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 20.0


def run_check(job: jobs.Job) -> None:
    bot_id = job.payload["bot"]
    with db.connect() as conn:
        source = conn.execute("SELECT source FROM bots WHERE id = %s", (bot_id,)).fetchone()["source"]
    try:
        name, error = check_submission(source), None
    except BotLoadError as exc:
        if not image_exists():
            raise
        name, error = None, str(exc)

    with db.connect() as conn:
        jobs.finish(conn, job)
        if error is not None:
            conn.execute(
                "UPDATE bots SET status = 'rejected', error = %s WHERE id = %s AND status = 'pending'",
                (error, bot_id),
            )
            return
        row = conn.execute(
            "SELECT a.id, b.status FROM accounts a JOIN bots b ON b.account_id = a.id "
            "WHERE b.id = %s FOR UPDATE OF a",
            (bot_id,),
        ).fetchone()
        if row["status"] != "pending":
            return
        # Checks run concurrently, so an older upload can finish after a newer one
        # and must not displace it.
        params = {"account": row["id"], "bot": bot_id, "name": name}
        conn.execute(
            "UPDATE bots SET status = 'retired' "
            "WHERE account_id = %(account)s AND status = 'active' AND id < %(bot)s",
            params,
        )
        conn.execute(
            "UPDATE bots SET name = %(name)s, status = CASE WHEN EXISTS ("
            "  SELECT 1 FROM bots WHERE account_id = %(account)s AND status = 'active'"
            ") THEN 'retired' ELSE 'active' END WHERE id = %(bot)s",
            params,
        )


HANDLERS = {"check": run_check, "leg": tournament.run_leg, "finalize": tournament.finalize}


def bury(conn, job: jobs.Job, error: str) -> None:
    jobs.bury(conn, job, error)
    if job.kind in ("leg", "finalize"):
        tournament.fail(conn, job.payload["tournament"])


class Worker:
    def __init__(self, concurrency: int):
        self.owner = f"{socket.gethostname()}:{os.getpid()}"
        self.stopping = threading.Event()
        self._idle = threading.Event()
        self._slots = [threading.Thread(target=self._loop, daemon=True) for _ in range(concurrency)]
        self._beat = threading.Thread(target=self._heartbeat, daemon=True)

    def start(self) -> None:
        self._beat.start()
        for t in self._slots:
            t.start()

    def stop(self) -> None:
        """Finishes in-flight jobs before returning; the heartbeat outlives them so their leases hold."""
        self.stopping.set()
        for t in self._slots:
            t.join()
        self._idle.set()
        self._beat.join()

    def _loop(self) -> None:
        while not self.stopping.is_set():
            try:
                ran = self.run_one()
            except Exception:
                log.exception("worker slot failed")
                ran = False
            if not ran:
                self.stopping.wait(POLL_SECONDS)

    def run_one(self) -> bool:
        with db.connect() as conn:
            job = jobs.claim(conn, self.owner)
        if job is None:
            return False
        log.info("job %s %s attempt %s: %s", job.id, job.kind, job.attempts, job.payload)
        if job.attempts > job.max_attempts:
            with db.connect() as conn:
                bury(conn, job, "worker lost its lease on the final attempt")
            return True
        try:
            HANDLERS[job.kind](job)
        except jobs.LostLease as exc:
            log.warning("%s", exc)
        except Exception as exc:
            log.exception("job %s failed", job.id)
            error = f"{type(exc).__name__}: {exc}"
            with db.connect() as conn:
                if job.attempts >= job.max_attempts:
                    bury(conn, job, error)
                else:
                    jobs.retry(conn, job, error)
        return True

    def _heartbeat(self) -> None:
        while not self._idle.wait(HEARTBEAT_SECONDS):
            try:
                with db.connect() as conn:
                    jobs.heartbeat(conn, self.owner)
            except Exception:
                log.exception("heartbeat failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m poker_arena.worker")
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("ARENA_WORKER_CONCURRENCY", 4)))
    sub = parser.add_subparsers(dest="command")
    requeue = sub.add_parser("requeue", help="revive dead jobs (all of them if no ids are given)")
    requeue.add_argument("ids", nargs="*", type=int)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")

    if args.command == "requeue":
        with db.connect() as conn:
            revived = jobs.requeue(conn, args.ids or None)
            for t in {j.payload["tournament"] for j in revived if j.kind in ("leg", "finalize")}:
                conn.execute("UPDATE tournaments SET failed_at = NULL WHERE id = %s", (t,))
        print(f"requeued {len(revived)} job(s)")
        return 0

    if not image_exists():
        print(f"sandbox image {IMAGE!r} is unavailable (is docker running?)", file=sys.stderr)
        return 1
    with db.connect() as conn:
        db.init_schema(conn)

    worker = Worker(args.concurrency)
    signal.signal(signal.SIGTERM, lambda *_: worker.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stopping.set())
    worker.start()
    log.info("worker %s running %s slot(s)", worker.owner, args.concurrency)
    worker.stopping.wait()
    log.info("draining in-flight jobs")
    worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
