"""auto_apply/ats_accounts.py — per-tenant ATS account registry accessors.

Thin async helpers over the `ats_accounts` table (created by
`database.init_db` via SCHEMA_MIGRATIONS). The registry remembers which company
tenants already have an account — e.g. every Workday tenant is
`{company}.wd{N}.myworkdayjobs.com` and needs its own account — so every surface
can go straight to sign-in instead of re-deriving "sign in vs create account"
from the DOM each time. It travels between devices as the `ats_account` sync
entity (see backend/sync/entities.py::AtsAccountAdapter).

NEVER stores a password — only the email, status, and timestamps.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..database import _get_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get(tenant_host: str) -> Optional[dict]:
    """Return the registry row for a tenant host, or None."""
    host = (tenant_host or "").strip().lower()
    if not host:
        return None
    db = await _get_db()
    try:
        cur = await db.execute(
            "SELECT tenant_host, provider, email, status, created_at, "
            "last_sign_in_at, updated_at FROM ats_accounts WHERE tenant_host = ?",
            (host,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def all_accounts(provider: Optional[str] = None) -> list[dict]:
    """Return every registered account, optionally filtered by provider."""
    db = await _get_db()
    try:
        if provider:
            cur = await db.execute(
                "SELECT tenant_host, provider, email, status, created_at, "
                "last_sign_in_at, updated_at FROM ats_accounts WHERE provider = ? "
                "ORDER BY tenant_host",
                (provider,),
            )
        else:
            cur = await db.execute(
                "SELECT tenant_host, provider, email, status, created_at, "
                "last_sign_in_at, updated_at FROM ats_accounts ORDER BY tenant_host"
            )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def upsert(
    tenant_host: str,
    email: str,
    status: str = "active",
    provider: str = "workday",
) -> Optional[dict]:
    """Record (or update) an account for a tenant.

    Preserves the original `created_at` on update; always refreshes
    `updated_at`. Returns the resulting row.
    """
    host = (tenant_host or "").strip().lower()
    if not host:
        return None
    now = _now()
    db = await _get_db()
    try:
        cur = await db.execute(
            "SELECT created_at FROM ats_accounts WHERE tenant_host = ?", (host,)
        )
        existing = await cur.fetchone()
        created_at = (existing["created_at"] if existing else None) or now
        await db.execute(
            """INSERT INTO ats_accounts
                   (tenant_host, provider, email, status, created_at,
                    last_sign_in_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?)
               ON CONFLICT(tenant_host) DO UPDATE SET
                   provider = excluded.provider,
                   email = excluded.email,
                   status = excluded.status,
                   created_at = excluded.created_at,
                   updated_at = excluded.updated_at""",
            (host, provider, email, status, created_at, now),
        )
        await db.commit()
    finally:
        await db.close()
    return await get(host)


async def mark_signed_in(tenant_host: str) -> Optional[dict]:
    """Stamp a successful sign-in. Flips `pending_verification` → `active`
    (a completed sign-in proves the account is real)."""
    host = (tenant_host or "").strip().lower()
    if not host:
        return None
    now = _now()
    db = await _get_db()
    try:
        await db.execute(
            """UPDATE ats_accounts
               SET last_sign_in_at = ?, updated_at = ?,
                   status = CASE WHEN status = 'pending_verification'
                                 THEN 'active' ELSE status END
               WHERE tenant_host = ?""",
            (now, now, host),
        )
        await db.commit()
    finally:
        await db.close()
    return await get(host)
