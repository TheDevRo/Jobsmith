"""
_auth.py — Shared auth dependency for the dashboard API.

Historically the dashboard API trusted its *network position*: every router in
ALL_ROUTERS was mounted with no auth at all, so anything that could open a
socket to the port could read config (passwords, API keys, PII), tail logs, and
drive the apply/delete endpoints. Only /api/ext/* was token-gated. In Docker
that port is published on the LAN, which made "trusted network" a fiction.

This module replaces "loopback == trusted" with an explicit check:

  * requests from this machine's loopback interface pass, and
  * everything else must present the extension token, as either an
    ``X-Jobsmith-Token`` header or a ``jobsmith_token`` cookie.

The cookie is what makes this usable from a browser. It matters more than it
looks: under Docker the container sees requests from the bridge gateway
(e.g. 172.17.0.1), *not* 127.0.0.1 — so every Docker/LAN dashboard user is a
non-loopback caller and would otherwise be locked out of their own app. The
SPA trades the token for a cookie once via POST /api/auth/login and rides on it
from then on. The token is readable at data/.extension_token (mode 0600).

Escape hatch: set JOBSMITH_ALLOW_INSECURE=1 to restore the old
trust-the-network behaviour on a genuinely trusted single-user LAN. Off by
default — that default is the entire point of this module.
"""

import logging
import os
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel

from .. import app_state as state
from ..extension_api import get_or_create_token

logger = logging.getLogger(__name__)

COOKIE_NAME = "jobsmith_token"

# Paths that must stay reachable unauthenticated, or auth could never be
# obtained in the first place:
#   /api/auth/*      — the token-for-cookie exchange itself
#   /api/health/live — container HEALTHCHECK + desktop readiness probe, which
#                      must answer before any session exists (no secrets in it)
_EXEMPT_PREFIXES = ("/api/auth/", "/api/health/live")

# Methods that don't change server state. Everything else is subject to the
# cross-site check below.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Browser extension pages are a legitimate cross-scheme caller (their Origin is
# chrome-extension:// etc.); they authenticate to /api/ext/* separately and are
# allowed to reach the dashboard API too.
_EXTENSION_SCHEMES = ("chrome-extension", "moz-extension", "safari-web-extension")


def _request_is_cross_site(request: Request) -> bool:
    """True only when browser-supplied metadata proves this is a cross-site request.

    Loopback trust (see request_is_authorized) means a state-changing request
    needs no token when it comes from this machine — which is exactly what lets
    a malicious web page the user is visiting drive our API via the browser's
    ambient loopback access (CSRF). CORS stops the page reading our *responses*
    but not the browser *sending* a "simple" cross-origin POST.

    We close that by rejecting state-changing requests whose origin metadata
    says cross-site. This returns False for genuine same-origin browser requests
    AND for non-browser callers (the desktop sidecar, the iOS app, curl, the
    test client) which send neither header — CSRF is a browser-only attack, so
    a caller with no Origin/Sec-Fetch-Site was never riding ambient authority.
    """
    # Modern browsers stamp every request with Sec-Fetch-Site; trust it first.
    sec_fetch_site = request.headers.get("sec-fetch-site")
    if sec_fetch_site:
        return sec_fetch_site not in ("same-origin", "same-site", "none")

    # Fallback for browsers that omit Sec-Fetch-Site: compare Origin to the host
    # we were actually dialled on (handles the LAN/Docker case, where the host
    # isn't 127.0.0.1) and allow extension origins.
    origin = request.headers.get("origin")
    if not origin:
        return False  # non-browser caller (no ambient authority to abuse)
    if origin == "null":
        return True  # sandboxed/opaque origin — treat as cross-site
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return True
    if parsed.scheme in _EXTENSION_SCHEMES:
        return False
    return parsed.netloc != request.headers.get("host", "")


def auth_disabled() -> bool:
    """True when the operator has explicitly opted out of API auth."""
    return os.environ.get("JOBSMITH_ALLOW_INSECURE", "").strip() == "1"


def token_matches(candidate: str | None) -> bool:
    """Constant-time compare of a caller-supplied token against ours."""
    if not candidate:
        return False
    return secrets.compare_digest(candidate, get_or_create_token())


def request_is_authorized(request: Request, header_token: str | None = None) -> bool:
    """Shared predicate: is this caller allowed to see privileged data?

    Also used by /api/config to decide whether to redact secrets, so that the
    redaction rule and the access rule can never drift apart.
    """
    if auth_disabled():
        return True
    if state.is_loopback_request(request):
        return True
    return token_matches(header_token) or token_matches(request.cookies.get(COOKIE_NAME))


async def require_local_or_token(
    request: Request,
    x_jobsmith_token: str | None = Header(default=None),
) -> None:
    """FastAPI dependency guarding every dashboard router."""
    if request.url.path.startswith(_EXEMPT_PREFIXES):
        return
    if not request_is_authorized(request, x_jobsmith_token):
        raise HTTPException(
            status_code=401,
            detail=(
                "Jobsmith requires a token for requests from off this machine. "
                "Paste the token from data/.extension_token into the dashboard, "
                "or send it as an X-Jobsmith-Token header."
            ),
        )
    # Authorized — but a state-changing request must not be a cross-site one,
    # or a page the user is merely visiting could drive our API through the
    # browser's ambient loopback/cookie access (CSRF). Applies even to loopback
    # callers, since loopback trust is precisely what CSRF abuses.
    if request.method not in _SAFE_METHODS and _request_is_cross_site(request):
        raise HTTPException(
            status_code=403,
            detail="Cross-site request blocked.",
        )


# ---------------------------------------------------------------------------
# Token -> cookie exchange (mounted without the dependency above)
# ---------------------------------------------------------------------------
router = APIRouter()


class LoginBody(BaseModel):
    token: str


@router.get("/api/auth/status")
async def auth_status(
    request: Request,
    x_jobsmith_token: str | None = Header(default=None),
):
    """Let the SPA discover whether it needs to prompt for a token."""
    return {
        "authenticated": request_is_authorized(request, x_jobsmith_token),
        "required": not (auth_disabled() or state.is_loopback_request(request)),
    }


@router.post("/api/auth/login")
async def auth_login(body: LoginBody, request: Request, response: Response):
    """Trade a valid token for a session cookie."""
    if not token_matches(body.token.strip()):
        # Don't leak which part was wrong, and don't log the attempted value.
        logger.warning("Rejected dashboard login attempt from %s",
                       getattr(getattr(request, "client", None), "host", "?"))
        raise HTTPException(status_code=401, detail="Invalid token")
    response.set_cookie(
        COOKIE_NAME,
        body.token.strip(),
        httponly=True,
        samesite="strict",
        max_age=60 * 60 * 24 * 30,
        # secure=True is deliberately NOT set: this app is served over plain
        # http on localhost/LAN, and a Secure cookie would never be sent back.
    )
    return {"ok": True}


@router.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
