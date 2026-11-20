"""Bounded downloads for URLs supplied by users or image search providers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass

import aiohttp
from yarl import URL

MAX_IMAGE_BYTES = 15 * 1024 * 1024


class PublicFetchError(ValueError):
    """A public resource could not be retrieved safely."""


@dataclass(frozen=True)
class PublicResource:
    body: bytes
    content_type: str
    charset: str | None


async def _public_addresses(url: URL) -> list[str]:
    host = url.raw_host
    if url.scheme not in {"http", "https"} or not host or "@" in url.raw_authority or "%" in host:
        raise PublicFetchError("只支持不含账户信息的公网图片或网页地址")
    records = await asyncio.get_running_loop().getaddrinfo(host, url.port, type=socket.SOCK_STREAM)
    addresses = []
    for _, _, _, _, sockaddr in records:
        addr = ipaddress.ip_address(sockaddr[0])
        if (
            not addr.is_global
            or addr.is_multicast
            or (isinstance(addr, ipaddress.IPv6Address) and (addr not in _PUBLIC_V6 or addr.sixtofour or addr.teredo))
        ):
            raise PublicFetchError("目标不是公网地址")
        if str(addr) not in addresses:
            addresses.append(str(addr))
    if not addresses:
        raise PublicFetchError("无法解析公网地址")
    return addresses


_PUBLIC_V6 = ipaddress.IPv6Network("2000::/3")


async def fetch_public_resource(
    url: str, *, proxy: str | None, timeout: int, max_bytes: int, user_agent: str = "Mozilla/5.0"
) -> PublicResource:
    """Pin each hop to its checked IP, keeping the original HTTP Host and TLS SNI."""
    target = URL(url)
    try:
        async with (
            asyncio.timeout(timeout),
            aiohttp.ClientSession(
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=min(4, timeout), sock_read=timeout),
            ) as session,
        ):
            for _ in range(6):
                addresses = await _public_addresses(target)
                for index, ip in enumerate(addresses):
                    # Use the numeric target for proxy requests too: never let the
                    # proxy look up the untrusted name independently after validation.
                    headers = {"Host": target.raw_authority, "User-Agent": user_agent, "Accept-Encoding": "identity"}
                    try:
                        async with session.get(
                            target.with_host(ip),
                            headers=headers,
                            proxy=proxy or None,
                            server_hostname=target.raw_host,
                            allow_redirects=False,
                        ) as response:
                            if response.status in {301, 302, 303, 307, 308}:
                                location = response.headers.get("Location")
                                if not location:
                                    raise PublicFetchError("重定向缺少目标地址")
                                target = target.join(URL(location))
                                break
                            if response.status != 200:
                                raise PublicFetchError(f"下载失败（HTTP {response.status}）")
                            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                                raise PublicFetchError("不支持该响应的压缩方式")
                            if response.content_length is not None and response.content_length > max_bytes:
                                raise PublicFetchError("下载内容过大")
                            data = bytearray()
                            async for chunk in response.content.iter_chunked(65536):
                                if len(data) + len(chunk) > max_bytes:
                                    raise PublicFetchError("下载内容过大")
                                data.extend(chunk)
                            return PublicResource(bytes(data), response.content_type, response.charset)
                    except (aiohttp.ClientConnectorError, aiohttp.ServerTimeoutError):
                        if index == len(addresses) - 1:
                            raise
            raise PublicFetchError("重定向次数过多")
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        raise PublicFetchError("无法连接到公网资源") from exc
