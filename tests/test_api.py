import os
import shutil
import subprocess
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from poker_arena import db
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
        conn.execute("DROP TABLE IF EXISTS matches, bots, accounts")
    with TestClient(app) as client:
        yield client


def submit(client, username, source):
    return client.post(f"/accounts/{username}/bot", files={"file": ("bot.py", source)})


def checked(client, username, source):
    response = submit(client, username, source)
    assert response.status_code == 202, response.text
    for _ in range(300):
        bot = client.get(f"/bots/{response.json()['id']}").json()
        if bot["status"] != "pending":
            return bot
        time.sleep(0.1)
    raise AssertionError("check did not finish")


def test_account_usernames_are_unique(client):
    assert client.post("/accounts", json={"username": "alice"}).status_code == 201
    assert client.post("/accounts", json={"username": "alice"}).status_code == 409


def test_submission_is_checked_and_replaces_active_bot(client):
    client.post("/accounts", json={"username": "bob"})

    first = checked(client, "bob", GOOD_BOT)
    assert (first["status"], first["name"]) == ("active", "numpy")

    rejected = checked(client, "bob", ILLEGAL_BOT)
    assert rejected["status"] == "rejected"
    assert "checked facing a bet" in rejected["error"]

    second = checked(client, "bob", GOOD_BOT)
    assert second["status"] == "active"
    assert client.get(f"/bots/{first['id']}").json()["status"] == "retired"

    board = [row for row in client.get("/leaderboard").json() if row["username"] == "bob"]
    assert [row["bot_id"] for row in board] == [second["id"]]


def test_unknown_account_and_broken_import(client):
    assert submit(client, "nobody", GOOD_BOT).status_code == 404
    client.post("/accounts", json={"username": "carol"})
    bot = checked(client, "carol", "import pandas\n")
    assert bot["status"] == "rejected"
    assert "pandas" in bot["error"]


def test_rate_moves_winner_up_and_draw_is_symmetric():
    (wa, _), (lb, _) = rate((25.0, 8.0), (25.0, 8.0), score=100)
    assert wa > 25.0 > lb
    (da, _), (db_, _) = rate((25.0, 8.0), (25.0, 8.0), score=0)
    assert da == pytest.approx(db_)
