"""
http_client.py — Shared httpx settings for outbound HTTP.

Replaced aiohttp. Three aiohttp behaviors are kept on purpose so the swap changes
nothing for callers: redirects are followed by default, HTTP(S)_PROXY env vars
are ignored, and each request has a whole-request deadline.
"""

import asyncio

import httpx


def async_client(**kwargs) -> httpx.AsyncClient:
    """AsyncClient with aiohttp's defaults (follow redirects, no env proxies)."""
    return httpx.AsyncClient(follow_redirects=True, trust_env=False, **kwargs)


async def send(
    client: httpx.AsyncClient, method: str, url: str, *, timeout: float, **kwargs
) -> httpx.Response:
    """Send one request and read its body within *timeout* seconds in total.

    httpx timeouts apply per phase (connect, each read), so a slow-dripping
    server could run past them; aiohttp's ClientTimeout(total=...) could not.
    Either kind of expiry raises asyncio.TimeoutError, as aiohttp did.
    """
    try:
        return await asyncio.wait_for(
            client.request(method, url, timeout=timeout, **kwargs), timeout
        )
    except httpx.TimeoutException as exc:
        raise asyncio.TimeoutError(str(exc) or type(exc).__name__) from exc
