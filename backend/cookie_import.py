"""
cookie_import.py — Parse browser cookie exports into Playwright storage_state.

Accepts the three formats users can realistically produce:
  - Cookie-Editor / EditThisCookie style JSON: a bare array of cookie objects.
  - Playwright storage_state JSON: {"cookies": [...], "origins": [...]}.
  - Netscape cookies.txt: tab-delimited, as written by yt-dlp/curl extensions.

Everything here is pure functions over bytes/str — no file or network I/O —
so the endpoint in routers/sessions.py owns all path decisions.
"""

import json
from typing import Optional

# Chromium/extension-specific cookie fields Playwright's add_cookies rejects.
_DROP_FIELDS = {
    "priority", "sourceScheme", "sourcePort", "size", "hostOnly",
    "session", "storeId", "id", "firstPartyDomain",
}

_SAMESITE_MAP = {
    "no_restriction": "None",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
}


def detect_format(raw) -> str:
    """Classify raw upload bytes/text as 'cookie_editor', 'storage_state',
    or 'netscape'. Raises ValueError when it is none of them."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = raw.strip()
    if not text:
        raise ValueError("Empty file")

    if text[0] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Looks like JSON but failed to parse: {e}") from e
        if isinstance(data, list):
            return "cookie_editor"
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            return "storage_state"
        raise ValueError("JSON is neither a cookie array nor a storage_state object")

    # Netscape format: at least one non-comment line with 7 tab-separated fields
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or (stripped.startswith("#") and not stripped.startswith("#HttpOnly_")):
            continue
        if len(stripped.split("\t")) == 7:
            return "netscape"
    raise ValueError("Unrecognized cookie format (expected Cookie-Editor JSON, storage_state JSON, or Netscape cookies.txt)")


def parse_netscape(text: str) -> list:
    """Parse Netscape cookies.txt lines into raw cookie dicts (pre-normalize)."""
    cookies = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        http_only = False
        if stripped.startswith("#HttpOnly_"):
            stripped = stripped[len("#HttpOnly_"):]
            http_only = True
        elif stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) != 7:
            continue
        domain, _include_subdomains, path, secure, expiry, name, value = parts
        try:
            expires = float(expiry)
        except ValueError:
            expires = -1
        cookies.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "expires": expires,
            "httpOnly": http_only,
            "secure": secure.upper() == "TRUE",
        })
    return cookies


def normalize_cookies(cookies: list) -> list:
    """Map arbitrary exported cookie dicts to the exact shape Playwright's
    add_cookies/storage_state accepts, dropping fields it rejects."""
    out = []
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c:
            continue
        cookie = {
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": str(c.get("domain", "")),
            "path": str(c.get("path") or "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
        }
        if not cookie["domain"]:
            continue

        expires = c.get("expires", c.get("expirationDate"))
        if c.get("session") or expires is None:
            cookie["expires"] = -1
        else:
            try:
                cookie["expires"] = float(expires)
            except (TypeError, ValueError):
                cookie["expires"] = -1

        same_site = c.get("sameSite")
        if isinstance(same_site, str):
            mapped = _SAMESITE_MAP.get(same_site.lower())
            if mapped is None and same_site in ("None", "Lax", "Strict"):
                mapped = same_site
            if mapped:
                cookie["sameSite"] = mapped

        # partitionKey and the _DROP_FIELDS are intentionally not carried over
        # (mirrors the sanitize in auto_apply/linkedin_auth.py).
        out.append(cookie)
    return out


def parse_upload(raw) -> list:
    """One-shot: detect format, parse, normalize. Returns Playwright cookies."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    fmt = detect_format(raw)
    if fmt == "netscape":
        cookies = parse_netscape(raw)
    elif fmt == "cookie_editor":
        cookies = json.loads(raw)
    else:  # storage_state
        cookies = json.loads(raw)["cookies"]
    return normalize_cookies(cookies)


def to_storage_state(cookies: list) -> dict:
    return {"cookies": cookies, "origins": []}


def detect_site(cookies: list) -> Optional[str]:
    """Infer the target site from cookie domains: 'linkedin', 'indeed', or the
    dominant registrable domain (e.g. 'glassdoor.com'). None if no cookies."""
    votes: dict = {}
    for c in cookies:
        domain = c.get("domain", "").lstrip(".").lower()
        if not domain:
            continue
        if domain.endswith("linkedin.com"):
            key = "linkedin"
        elif domain.endswith("indeed.com"):
            key = "indeed"
        else:
            parts = domain.split(".")
            key = ".".join(parts[-2:]) if len(parts) >= 2 else domain
        votes[key] = votes.get(key, 0) + 1
    if not votes:
        return None
    return max(votes, key=lambda k: votes[k])
