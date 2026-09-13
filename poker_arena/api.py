from __future__ import annotations

import logging
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlencode

import psycopg
from authlib.integrations.starlette_client import OAuthError
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from dotenv import load_dotenv
from starlette.middleware.sessions import SessionMiddleware

# auth reads the GitHub credentials at import, so this has to run first.
load_dotenv()

from . import auth, db
from .sandbox import IMAGE, MAX_SOURCE_BYTES, image_exists
from .submission import BotLoadError, check_submission

log = logging.getLogger(__name__)
checks = ThreadPoolExecutor(max_workers=int(os.environ.get("ARENA_CHECK_WORKERS", 2)))
LOGIN_REDIRECT = os.environ.get("ARENA_LOGIN_REDIRECT")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Without the image every check would fail and be blamed on the submission.
    if not image_exists():
        raise RuntimeError(f"sandbox image {IMAGE!r} is unavailable (is docker running?)")
    with db.connect() as conn:
        db.init_schema(conn)
        conn.execute("DELETE FROM sessions WHERE expires_at <= now()")
        for bot in conn.execute("SELECT id, source FROM bots WHERE status = 'pending' ORDER BY id"):
            checks.submit(run_check, bot["id"], bot["source"])
    yield


app = FastAPI(title="poker-arena", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("ARENA_SECRET_KEY") or secrets.token_hex(32),
    max_age=600,
)


def get_conn():
    with db.connect() as conn:
        yield conn


bearer = HTTPBearer(auto_error=False)


def token_hash(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> bytes:
    if credentials is None:
        raise HTTPException(401, "not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return auth.hash_token(credentials.credentials)


def current_account(token_hash: bytes = Depends(token_hash), conn: psycopg.Connection = Depends(get_conn)):
    account = conn.execute(
        "SELECT a.id, a.username FROM sessions s JOIN accounts a ON a.id = s.account_id "
        "WHERE s.token_hash = %s AND s.expires_at > now()",
        (token_hash,),
    ).fetchone()
    if account is None:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return account


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


@app.get("/login")
async def login(request: Request):
    return await auth.oauth.github.authorize_redirect(request, request.url_for("github_callback"))


@app.get("/auth/github/callback")
async def github_callback(request: Request):
    try:
        try:
            token = await auth.oauth.github.authorize_access_token(request)
        except OAuthError as exc:
            raise HTTPException(400, f"github login failed: {exc.description or exc.error}")
        user = await auth.oauth.github.userinfo(token=token)
        session = await run_in_threadpool(start_session, user["id"], user["login"])
    except HTTPException as exc:
        if LOGIN_REDIRECT is None:
            raise
        return RedirectResponse(f"{LOGIN_REDIRECT}#{urlencode({'error': exc.detail})}", 302)
    if LOGIN_REDIRECT is None:
        return session
    # The fragment keeps the token out of server logs and Referer headers.
    return RedirectResponse(f"{LOGIN_REDIRECT}#{urlencode(session)}", 302)


def start_session(github_id: int, login: str) -> dict:
    with db.connect() as conn:
        try:
            account = conn.execute(
                "INSERT INTO accounts (username, github_id) VALUES (%s, %s) "
                "ON CONFLICT (github_id) DO UPDATE SET github_id = EXCLUDED.github_id "
                "RETURNING id, username",
                (login, github_id),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, f"username {login!r} is taken by another account")
        token, token_hash = auth.new_token()
        conn.execute(
            "INSERT INTO sessions (token_hash, account_id, expires_at) "
            "VALUES (%s, %s, now() + %s::interval)",
            (token_hash, account["id"], auth.SESSION_TTL),
        )
    return {"username": account["username"], "access_token": token, "token_type": "bearer"}


@app.get("/me")
def me(account=Depends(current_account)):
    return {"username": account["username"]}


@app.post("/logout", status_code=204)
def logout(token_hash: bytes = Depends(token_hash), conn: psycopg.Connection = Depends(get_conn)):
    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))


@app.post("/accounts/{username}/bot", status_code=202)
def submit_bot(
    username: str,
    file: UploadFile,
    account=Depends(current_account),
    conn: psycopg.Connection = Depends(get_conn),
):
    if account["username"] != username:
        raise HTTPException(403, "cannot submit bots for another account")

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


BOT_SELECT = (
    "SELECT b.id, a.username, b.name, b.status, b.error, b.mu, b.sigma, b.created_at "
    "FROM bots b JOIN accounts a ON a.id = b.account_id "
)


@app.get("/bots")
def list_bots(
    username: str | None = None,
    status: Literal["pending", "active", "rejected", "retired"] | None = None,
    conn: psycopg.Connection = Depends(get_conn),
):
    return conn.execute(
        BOT_SELECT + "WHERE (%(username)s::text IS NULL OR a.username = %(username)s) "
        "AND (%(status)s::text IS NULL OR b.status = %(status)s) ORDER BY b.id",
        {"username": username, "status": status},
    ).fetchall()


@app.get("/bots/{bot_id}")
def get_bot(bot_id: int, conn: psycopg.Connection = Depends(get_conn)):
    bot = conn.execute(BOT_SELECT + "WHERE b.id = %s", (bot_id,)).fetchone()
    if bot is None:
        raise HTTPException(404, "bot not found")
    return bot


@app.get("/tournaments")
def list_tournaments(conn: psycopg.Connection = Depends(get_conn)):
    return conn.execute(
        "SELECT t.id, t.hands, t.seed, t.started_at, t.finished_at, count(m.id) AS matches "
        "FROM tournaments t LEFT JOIN matches m ON m.tournament_id = t.id "
        "GROUP BY t.id ORDER BY t.id DESC"
    ).fetchall()


@app.get("/tournaments/{tournament_id}")
def get_tournament(tournament_id: int, conn: psycopg.Connection = Depends(get_conn)):
    tournament = conn.execute(
        "SELECT id, hands, seed, started_at, finished_at FROM tournaments WHERE id = %s",
        (tournament_id,),
    ).fetchone()
    if tournament is None:
        raise HTTPException(404, "tournament not found")
    tournament["standings"] = conn.execute(
        "SELECT s.bot_id, a.username, b.name, sum(s.score) AS score, count(*) AS matches, "
        "count(*) FILTER (WHERE s.score > 0) AS wins, count(*) FILTER (WHERE s.score < 0) AS losses "
        "FROM (SELECT bot_a AS bot_id, score FROM matches WHERE tournament_id = %(id)s "
        "      UNION ALL SELECT bot_b, -score FROM matches WHERE tournament_id = %(id)s) s "
        "JOIN bots b ON b.id = s.bot_id JOIN accounts a ON a.id = b.account_id "
        "GROUP BY s.bot_id, a.username, b.name ORDER BY score DESC",
        {"id": tournament_id},
    ).fetchall()
    tournament["matches"] = conn.execute(
        f"SELECT {MATCH_COLUMNS} {MATCH_JOINS} WHERE m.tournament_id = %s ORDER BY m.id",
        (tournament_id,),
    ).fetchall()
    return tournament


MATCH_COLUMNS = (
    "m.id, m.tournament_id, m.bot_a, ba.name AS bot_a_name, aa.username AS bot_a_username, "
    "m.bot_b, bb.name AS bot_b_name, ab.username AS bot_b_username, m.score, m.played_at"
)
MATCH_JOINS = (
    "FROM matches m "
    "JOIN bots ba ON ba.id = m.bot_a JOIN accounts aa ON aa.id = ba.account_id "
    "JOIN bots bb ON bb.id = m.bot_b JOIN accounts ab ON ab.id = bb.account_id"
)


@app.get("/matches/{match_id}")
def get_match(match_id: int, conn: psycopg.Connection = Depends(get_conn)):
    match = conn.execute(
        f"SELECT {MATCH_COLUMNS}, m.legs {MATCH_JOINS} WHERE m.id = %s", (match_id,)
    ).fetchone()
    if match is None:
        raise HTTPException(404, "match not found")
    return match


@app.get("/leaderboard")
def leaderboard(conn: psycopg.Connection = Depends(get_conn)):
    return conn.execute(
        "SELECT a.username, b.id AS bot_id, b.name, b.mu, b.sigma, b.mu - 3 * b.sigma AS rating "
        "FROM bots b JOIN accounts a ON a.id = b.account_id "
        "WHERE b.status = 'active' ORDER BY rating DESC"
    ).fetchall()
