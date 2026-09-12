from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import psycopg
from fastapi import Depends, FastAPI, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import db
from .sandbox import IMAGE, MAX_SOURCE_BYTES, image_exists
from .submission import BotLoadError, check_submission

log = logging.getLogger(__name__)
checks = ThreadPoolExecutor(max_workers=int(os.environ.get("ARENA_CHECK_WORKERS", 2)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Without the image every check would fail and be blamed on the submission.
    if not image_exists():
        raise RuntimeError(f"sandbox image {IMAGE!r} is unavailable (is docker running?)")
    with db.connect() as conn:
        db.init_schema(conn)
        for bot in conn.execute("SELECT id, source FROM bots WHERE status = 'pending' ORDER BY id"):
            checks.submit(run_check, bot["id"], bot["source"])
    yield


app = FastAPI(title="poker-arena", lifespan=lifespan)


def get_conn():
    with db.connect() as conn:
        yield conn


def run_check(bot_id: int, source: str) -> None:
    try:
        _run_check(bot_id, source)
    except Exception:
        log.exception("check for bot %s failed", bot_id)


def _run_check(bot_id: int, source: str) -> None:
    try:
        name = check_submission(source)
    except BotLoadError as exc:
        with db.connect() as conn:
            conn.execute(
                "UPDATE bots SET status = 'rejected', error = %s WHERE id = %s AND status = 'pending'",
                (str(exc), bot_id),
            )
        return

    with db.connect() as conn:
        row = conn.execute(
            "SELECT a.id, b.status FROM accounts a JOIN bots b ON b.account_id = a.id "
            "WHERE b.id = %s FOR UPDATE OF a",
            (bot_id,),
        ).fetchone()
        if row["status"] != "pending":
            return
        account_id = row["id"]
        # Checks run concurrently, so an older upload can finish after a newer one
        # and must not displace it.
        params = {"account": account_id, "bot": bot_id, "name": name}
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


class NewAccount(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_-]{3,32}$")


@app.post("/accounts", status_code=201)
def create_account(body: NewAccount, conn: psycopg.Connection = Depends(get_conn)):
    try:
        return conn.execute(
            "INSERT INTO accounts (username) VALUES (%s) RETURNING id, username, created_at",
            (body.username,),
        ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "username is taken")


@app.post("/accounts/{username}/bot", status_code=202)
def submit_bot(username: str, file: UploadFile, conn: psycopg.Connection = Depends(get_conn)):
    account = conn.execute("SELECT id FROM accounts WHERE username = %s", (username,)).fetchone()
    if account is None:
        raise HTTPException(404, "account not found")

    raw = file.file.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise HTTPException(413, f"submissions are limited to {MAX_SOURCE_BYTES} bytes")
    try:
        source = raw.decode()
    except UnicodeDecodeError as exc:
        raise HTTPException(422, f"submission is not valid utf-8: {exc}")
    if "\x00" in source:
        raise HTTPException(422, "submission contains NUL bytes")

    bot = conn.execute(
        "INSERT INTO bots (account_id, source) VALUES (%s, %s) RETURNING id, status, created_at",
        (account["id"], source),
    ).fetchone()
    conn.commit()
    checks.submit(run_check, bot["id"], source)
    return bot


@app.get("/bots/{bot_id}")
def get_bot(bot_id: int, conn: psycopg.Connection = Depends(get_conn)):
    bot = conn.execute(
        "SELECT b.id, a.username, b.name, b.status, b.error, b.mu, b.sigma, b.created_at "
        "FROM bots b JOIN accounts a ON a.id = b.account_id WHERE b.id = %s",
        (bot_id,),
    ).fetchone()
    if bot is None:
        raise HTTPException(404, "bot not found")
    return bot


@app.get("/leaderboard")
def leaderboard(conn: psycopg.Connection = Depends(get_conn)):
    return conn.execute(
        "SELECT a.username, b.id AS bot_id, b.name, b.mu, b.sigma, b.mu - 3 * b.sigma AS rating "
        "FROM bots b JOIN accounts a ON a.id = b.account_id "
        "WHERE b.status = 'active' ORDER BY rating DESC"
    ).fetchall()
