import os
import shutil
import subprocess
import time
import zlib
from urllib.parse import parse_qsl, urlsplit

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from poker_arena import api, auth, db
from poker_arena.api import app
from poker_arena.rating import rate
from poker_arena.sandbox import build_image, image_exists

DB_URL = os.environ.get("ARENA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    DB_URL is None
    or shutil.which("docker") is None
    or subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
    reason="needs ARENA_TEST_DATABASE_URL and a running docker daemon",
)

GOOD_BOT = """
import numpy as np
from poker_arena import Action

class NumpyBot:
    name = "numpy"
    def act(self, obs):
        return Action.call() if np.int64(obs.to_call) <= obs.bb else Action.check() if obs.can_check else Action.fold()

def create_bot():
    return NumpyBot()
"""

ILLEGAL_BOT = """
from poker_arena import Action

class Checker:
    name = "checker"
    def act(self, obs):
        return Action.check()

def create_bot():
    return Checker()
"""


@pytest.fixture(scope="module")
def client():
    if not image_exists():
        build_image()
    os.environ["ARENA_DATABASE_URL"] = DB_URL
    with db.connect() as conn:
        conn.execute("DROP TABLE IF EXISTS matches, tournaments, bots, sessions, accounts")
    with pytest.MonkeyPatch.context() as mp, TestClient(app) as client:
        mp.setattr(api, "LOGIN_REDIRECT", None)
        yield client


def signup(client, monkeypatch, username, github_id=None, **kwargs):
    github_id = github_id or zlib.crc32(username.encode())

    async def authorize_access_token(request):
        return {"access_token": "gho_fake"}

    async def userinfo(token):
        return {"id": github_id, "login": username}

    monkeypatch.setattr(auth.oauth.github, "authorize_access_token", authorize_access_token)
    monkeypatch.setattr(auth.oauth.github, "userinfo", userinfo)
    return client.get("/auth/github/callback", **kwargs)


def login(client, monkeypatch, username):
    response = signup(client, monkeypatch, username)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def submit(client, username, source, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(f"/accounts/{username}/bot", files={"file": ("bot.py", source)}, headers=headers)


def checked(client, username, source, token):
    response = submit(client, username, source, token)
    assert response.status_code == 202, response.text
    for _ in range(300):
        bot = client.get(f"/bots/{response.json()['id']}").json()
        if bot["status"] != "pending":
            return bot
        time.sleep(0.1)
    raise AssertionError("check did not finish")


def test_login_redirects_to_github(client):
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://github.com/login/oauth/authorize")


def test_github_login_reuses_account_and_rejects_taken_username(client, monkeypatch):
    first = signup(client, monkeypatch, "alice", github_id=1)
    again = signup(client, monkeypatch, "alice", github_id=1)
    assert first.json()["access_token"] != again.json()["access_token"]
    assert signup(client, monkeypatch, "alice", github_id=2).status_code == 409


def test_github_login_redirects_to_frontend(client, monkeypatch):
    monkeypatch.setattr(api, "LOGIN_REDIRECT", "http://frontend.test/auth/callback")

    response = signup(client, monkeypatch, "ivan", github_id=10, follow_redirects=False)
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert location._replace(fragment="").geturl() == "http://frontend.test/auth/callback"
    session = dict(parse_qsl(location.fragment))
    assert (session["username"], session["token_type"]) == ("ivan", "bearer")
    assert client.post("/logout", headers={"Authorization": f"Bearer {session['access_token']}"}).status_code == 204

    taken = signup(client, monkeypatch, "ivan", github_id=11, follow_redirects=False)
    assert taken.status_code == 302
    assert "taken" in dict(parse_qsl(urlsplit(taken.headers["location"]).fragment))["error"]


def test_submission_requires_owner_token(client, monkeypatch):
    dave = login(client, monkeypatch, "dave")
    erin = login(client, monkeypatch, "erin")
    assert submit(client, "dave", GOOD_BOT).status_code == 401
    assert submit(client, "dave", GOOD_BOT, "bogus").status_code == 401
    assert submit(client, "dave", GOOD_BOT, erin).status_code == 403

    assert client.get("/me", headers={"Authorization": f"Bearer {dave}"}).json() == {"username": "dave"}
    assert client.get("/me").status_code == 401

    assert client.post("/logout", headers={"Authorization": f"Bearer {dave}"}).status_code == 204
    assert submit(client, "dave", GOOD_BOT, dave).status_code == 401
    assert client.get("/me", headers={"Authorization": f"Bearer {dave}"}).status_code == 401


def test_submission_is_checked_and_replaces_active_bot(client, monkeypatch):
    token = login(client, monkeypatch, "bob")

    first = checked(client, "bob", GOOD_BOT, token)
    assert (first["status"], first["name"]) == ("active", "numpy")

    rejected = checked(client, "bob", ILLEGAL_BOT, token)
    assert rejected["status"] == "rejected"
    assert "checked facing a bet" in rejected["error"]

    second = checked(client, "bob", GOOD_BOT, token)
    assert second["status"] == "active"
    assert client.get(f"/bots/{first['id']}").json()["status"] == "retired"

    board = [row for row in client.get("/leaderboard").json() if row["username"] == "bob"]
    assert [row["bot_id"] for row in board] == [second["id"]]


def test_broken_import(client, monkeypatch):
    token = login(client, monkeypatch, "carol")
    bot = checked(client, "carol", "import pandas\n", token)
    assert bot["status"] == "rejected"
    assert "pandas" in bot["error"]


def test_bot_list_and_tournament_results(client):
    with db.connect() as conn:
        bots = []
        for username in ("frank", "grace", "heidi"):
            account = conn.execute(
                "INSERT INTO accounts (username) VALUES (%s) RETURNING id", (username,)
            ).fetchone()
            bots.append(conn.execute(
                "INSERT INTO bots (account_id, name, source, status) VALUES (%s, %s, '', 'active') RETURNING id",
                (account["id"], username),
            ).fetchone()["id"])
        tournament = conn.execute("INSERT INTO tournaments (hands, seed) VALUES (100, 1) RETURNING id").fetchone()["id"]
        matches = [
            conn.execute(
                "INSERT INTO matches (tournament_id, bot_a, bot_b, score, legs) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (tournament, a, b, score, Jsonb([{"deltas": [score, -score]}])),
            ).fetchone()["id"]
            for a, b, score in ((bots[0], bots[1], 40), (bots[0], bots[2], -10), (bots[1], bots[2], 0))
        ]

    listed = client.get("/bots", params={"username": "frank"}).json()
    assert [bot["id"] for bot in listed] == [bots[0]]
    assert "source" not in listed[0]
    assert all(bot["status"] == "active" for bot in client.get("/bots", params={"status": "active"}).json())
    assert client.get("/bots", params={"status": "bogus"}).status_code == 422

    assert client.get("/tournaments").json()[0] | {"started_at": None} == {
        "id": tournament, "hands": 100, "seed": 1, "started_at": None, "finished_at": None, "matches": 3,
    }
    result = client.get(f"/tournaments/{tournament}").json()
    assert [(s["username"], s["score"], s["wins"], s["losses"]) for s in result["standings"]] == [
        ("frank", 30, 1, 1), ("heidi", 10, 1, 0), ("grace", -40, 0, 1),
    ]
    assert [m["id"] for m in result["matches"]] == matches

    match = client.get(f"/matches/{matches[0]}").json()
    assert (match["bot_a_username"], match["bot_b_username"], match["score"]) == ("frank", "grace", 40)
    assert match["legs"] == [{"deltas": [40, -40]}]
    assert client.get("/tournaments/999999").status_code == 404
    assert client.get("/matches/999999").status_code == 404


def test_rate_moves_winner_up_and_draw_is_symmetric():
    (wa, _), (lb, _) = rate((25.0, 8.0), (25.0, 8.0), score=100)
    assert wa > 25.0 > lb
    (da, _), (db_, _) = rate((25.0, 8.0), (25.0, 8.0), score=0)
    assert da == pytest.approx(db_)
