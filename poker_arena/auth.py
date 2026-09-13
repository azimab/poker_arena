from __future__ import annotations

import hashlib
import os
import secrets

from authlib.integrations.starlette_client import OAuth

SESSION_TTL = "30 days"

oauth = OAuth()
oauth.register(
    name="github",
    client_id=os.environ.get("ARENA_GITHUB_CLIENT_ID"),
    client_secret=os.environ.get("ARENA_GITHUB_CLIENT_SECRET"),
    authorize_url="https://github.com/login/oauth/authorize",
    access_token_url="https://github.com/login/oauth/access_token",
    userinfo_endpoint="https://api.github.com/user",
)


def new_token() -> tuple[str, bytes]:
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
