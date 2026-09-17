"""
Authentication: PBKDF2 password hashing and HMAC-signed bearer tokens.

No third-party crypto library, but no hand-rolled crypto either -- this is
hashlib.pbkdf2_hmac and hmac.compare_digest, both stdlib, both the right
primitives. The token is a compact signed envelope rather than a real JWT;
the shape is the same (payload + signature) and it verifies in constant time.

For production you would move SECRET into the environment and put the tokens
behind HTTPS. The code reads SECRET from the environment already and only
falls back to a development constant.
"""

import base64
import hashlib
import hmac
import json
import os
import time

SECRET = os.environ.get("BIDVECTOR_SECRET", "dev-secret-change-me").encode()
TOKEN_TTL = 60 * 60 * 24 * 7           # one week
PBKDF2_ROUNDS = 120_000


# ------------------------------------------------------------------ passwords

def hash_password(password, salt=None):
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${_b64(salt)}${_b64(dk)}"


def verify_password(password, stored):
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------- tokens

def make_token(user_id, email):
    payload = {"uid": user_id, "email": email,
               "iat": int(time.time()), "exp": int(time.time()) + TOKEN_TTL}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(token):
    """Return the payload dict, or None if missing, tampered with or expired."""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s):
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)
