import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import User

_PBKDF2_ROUNDS = 200_000
_bearer = HTTPBearer()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ROUNDS).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ROUNDS).hex()
    return hmac.compare_digest(candidate, digest)


def issue_token(user: User) -> str:
    exp = datetime.now(UTC) + timedelta(hours=settings.jwt_ttl_hours)
    return jwt.encode({"sub": str(user.id), "role": user.role.value, "exp": exp}, settings.jwt_secret, "HS256")


def user_id_from_token(token: str) -> int:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from e
    return int(claims["sub"])


def device_from_token(token: str) -> str | None:
    tokens = {settings.temi_token: "temi", settings.pi_token: "pi"}
    for known, name in tokens.items():
        if hmac.compare_digest(token, known):
            return name
    return None


async def current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    user = await db.get(User, user_id_from_token(creds.credentials))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    return user
