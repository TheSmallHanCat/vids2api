"""Proxy configuration with tolerant URL parsing, backed by the proxy_config table."""

import re
from urllib.parse import quote, urlsplit

_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def _parse_proxy_line(line: str) -> str | None:
    text = (line or "").strip()
    if not text:
        return None
    match = re.match(r"^(?:(\w+)://)?(.+)$", text)
    if not match:
        raise ValueError(f"无法解析代理配置: {line}")
    scheme, rest = match.group(1), match.group(2)
    if scheme == "st5":
        scheme = "socks5"
    elif scheme in ("st", "socks"):
        scheme = "socks5"
    elif scheme is None:
        scheme = "http"
    if scheme not in _PROXY_SCHEMES:
        raise ValueError(f"不支持的代理协议: {scheme}")
    rest = rest.replace("@", "@", 1) if "@" in rest else rest
    # Support protocol://host:port:user:pass style
    parts = rest.split(":")
    if len(parts) == 4 and "@" not in rest:
        host, port, user, password = parts
        netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    else:
        netloc = rest
    url = f"{scheme}://{netloc}"
    target = urlsplit(url)
    if not target.hostname or target.port is None and scheme in _PROXY_SCHEMES:
        if target.port is None and target.hostname:
            url = url.replace(f"://{target.hostname}", f"://{target.hostname}:1080", 1)
            target = urlsplit(url)
        if not target.hostname:
            raise ValueError(f"无法解析代理主机: {line}")
    return url


def normalize_proxy_url(url: str) -> str:
    if not url or not url.strip():
        return ""
    parsed = _parse_proxy_line(url.strip())
    return parsed or ""


class ProxyManager:
    def __init__(self, db):
        self.db = db
        self._cache: dict | None = None

    async def _load(self) -> dict:
        row = await self.db._fetchone("SELECT * FROM proxy_config WHERE id = 1")
        return dict(row) if row else {"enabled": 0, "proxy_url": ""}

    async def get_config(self) -> dict:
        data = await self._load()
        return {"enabled": bool(data.get("enabled")), "proxy_url": data.get("proxy_url", "")}

    def get_request_proxy_url(self, config_data: dict | None = None) -> str | None:
        data = config_data or {}
        if not data.get("enabled"):
            return None
        url = (data.get("proxy_url") or "").strip()
        return normalize_proxy_url(url) or None

    async def get_effective_proxy(self) -> str | None:
        return self.get_request_proxy_url(await self._load())

    async def update_config(self, enabled: bool, proxy_url: str) -> dict:
        normalized = normalize_proxy_url(proxy_url) if proxy_url.strip() else ""
        await self.db.update_proxy_config(bool(enabled), normalized)
        return {"enabled": bool(enabled), "proxy_url": normalized}

    async def test_proxy(self, proxy_url: str) -> dict:
        normalized = normalize_proxy_url(proxy_url)
        if not normalized:
            return {"ok": False, "error": "代理地址无效"}
        from curl_cffi.requests import AsyncSession

        try:
            async with AsyncSession(proxy=normalized, timeout=15, trust_env=False) as session:
                response = await session.get("https://docs.google.com/generate_204")
                return {"ok": response.status_code in (200, 204), "status": response.status_code}
        except Exception as error:
            return {"ok": False, "error": str(error)[:200]}
