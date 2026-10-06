"""
tests/test_http_client.py

backend.http_client replaced aiohttp. These pin the aiohttp behaviors it keeps:
redirects followed by default, proxy env vars ignored, and a whole-request
deadline that raises asyncio.TimeoutError.
"""

import asyncio

import httpx
import pytest

from backend.http_client import async_client, send


def _client(handler) -> httpx.AsyncClient:
    return async_client(transport=httpx.MockTransport(handler))


def test_redirects_followed_by_default():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, text="done")

    async def run():
        async with _client(handler) as client:
            return await send(client, "GET", "https://jobs.example/start", timeout=5)

    resp = asyncio.run(run())
    assert resp.status_code == 200 and resp.text == "done"
    assert str(resp.url) == "https://jobs.example/final"


def test_redirects_can_be_disabled_per_request():
    def handler(request):
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    async def run():
        async with _client(handler) as client:
            return await send(client, "GET", "https://jobs.example/start", timeout=5,
                              follow_redirects=False)

    assert asyncio.run(run()).status_code == 302


def test_proxy_env_vars_ignored():
    assert async_client().trust_env is False


def test_total_deadline_raises_asyncio_timeout():
    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200)

    async def run():
        async with _client(handler) as client:
            await send(client, "GET", "https://slow.example/", timeout=0.05)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())


def test_httpx_timeout_maps_to_asyncio_timeout():
    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    async def run():
        async with _client(handler) as client:
            await send(client, "GET", "https://slow.example/", timeout=5)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())
