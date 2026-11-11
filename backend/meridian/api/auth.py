from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from meridian.core.config import get_settings
from meridian.core.util import utcnow
from meridian.db.models import User
from meridian.db.session import SessionLocal

COOKIE = "meridian_session"
_failures: dict[str, list[float]] = defaultdict(list)
MAX_FAILURES, WINDOW_S = 5, 900


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


@dataclass
class Principal:
    username: str
    role: str


def issue_token(user: User) -> str:
    st = get_settings()
    now = utcnow()
    return jwt.encode({"sub": user.username, "role": user.role, "iat": int(now.timestamp()),
                       "exp": int((now + timedelta(minutes=st.jwt_ttl_minutes)).timestamp())}, st.jwt_secret, algorithm="HS256")


def throttled(key: str) -> bool:
    now = time.time()
    _failures[key] = [t for t in _failures[key] if now - t < WINDOW_S]
    return len(_failures[key]) >= MAX_FAILURES


def note_failure(key: str) -> None:
    _failures[key].append(time.time())


def authenticate(s: Session, username: str, password: str) -> User | None:
    u = s.execute(select(User).where(User.username == username)).scalar()
    if u is None:
        bcrypt.checkpw(b"timing-equalizer", hash_password("x"))  # equalize timing for unknown users
        return None
    return u if verify_password(password, u.password_hash) else None


def get_db():  # noqa: ANN201
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def current_principal(request: Request) -> Principal:
    token = request.cookies.get(COOKIE)
    auth = request.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:]
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    try:
        claims = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session") from e
    return Principal(username=claims["sub"], role=claims["role"])


def require_cio(p: Principal = Depends(current_principal)) -> Principal:
    if p.role != "CIO":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CIO role required")
    return p
