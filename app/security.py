"""Credentials: per-agent internal-API tokens and signed reviewer session tokens (HMAC-SHA256)."""
import base64
import hashlib
import hmac
import json
import time

from app.config import get_settings, internal_secret, reviewer_secret


def _mac(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------------------- agents
def agent_token(agent_id: str) -> str:
    """Each agent identity has its own credential, so an agent cannot claim another agent's grants."""
    return _mac(internal_secret(), f"agent:{agent_id}")


def verify_agent(agent_id: str, token: str) -> bool:
    return hmac.compare_digest(agent_token(agent_id), token)


# ---------------------------------------------------------- reviewers
def hash_passcode(passcode: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt.encode(), 200_000).hex()


def verify_passcode(passcode: str, salt: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_passcode(passcode, salt), expected_hash)


def issue_reviewer_token(reviewer_id: str) -> tuple[str, int]:
    exp = int(time.time()) + get_settings().reviewer_token_ttl_seconds
    body = base64.urlsafe_b64encode(json.dumps({"sub": reviewer_id, "exp": exp}).encode()).decode().rstrip("=")
    return f"{body}.{_mac(reviewer_secret(), body)}", exp


def verify_reviewer_token(token: str) -> str | None:
    """Returns the reviewer id, or None if the token is malformed, forged or expired."""
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(_mac(reviewer_secret(), body), sig):
            return None
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return claims["sub"] if claims["exp"] > time.time() else None
    except Exception:
        return None
