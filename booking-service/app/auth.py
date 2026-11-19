"""Login helpers: signed tokens and password hashing (standard library only, no extra packages)."""
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from fastapi import Header, HTTPException

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
TOKEN_TTL = 8 * 3600  # 8 hours
ITERATIONS = 120_000


def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def check_credentials(username: str, password: str) -> bool:
    ok_user = hmac.compare_digest(username.encode(), ADMIN_USER.encode())
    ok_pass = hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())
    return ok_user and ok_pass


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return salt.hex() + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), ITERATIONS)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def create_token(username: str, role: str = "admin", uid: Optional[int] = None,
                 name: str = "", email: str = "") -> str:
    data = {"u": username, "role": role, "uid": uid, "name": name, "email": email,
            "exp": int(time.time()) + TOKEN_TTL}
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    return f"{payload}.{_sign(payload)}"


def decode_token(token: str) -> Optional[dict]:
    try:
        payload, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return data if data["exp"] > time.time() else None
    except Exception:
        return None


def verify_token(token: str) -> bool:
    return decode_token(token) is not None


def optional_user(authorization: str = Header(default="")) -> Optional[dict]:
    """The logged-in person's token data, or None if not logged in."""
    token = authorization.removeprefix("Bearer ").strip()
    return decode_token(token) if token else None


def current_user(authorization: str = Header(default="")) -> dict:
    user = optional_user(authorization)
    if not user:
        raise HTTPException(401, "Please log in")
    return user


def require_admin(authorization: str = Header(default="")) -> dict:
    """Dependency: put on any route that only the organizer may use."""
    user = optional_user(authorization)
    if not user:
        raise HTTPException(401, "Admin login required")
    if user.get("role", "admin") not in ("admin", "organizer"):
        raise HTTPException(403, "Organizer access only")
    return user


def require_judge(authorization: str = Header(default="")) -> dict:
    user = optional_user(authorization)
    if not user:
        raise HTTPException(401, "Judge login required")
    if user.get("role", "admin") not in ("judge", "admin"):
        raise HTTPException(403, "Judges only")
    return user


def require_student(authorization: str = Header(default="")) -> dict:
    """Only an authenticated student account may make purchases."""
    user = optional_user(authorization)
    if not user:
        raise HTTPException(401, "Student login required. Please log in or create an account.")
    if user.get("role") != "student" or user.get("uid") is None:
        raise HTTPException(403, "A student account is required for this action")
    return user
