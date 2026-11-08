"""Transport proxy resolution for upstream Vids requests.

The account's logical proxy binding stays stable; an optional fixed host hop can be
injected through the VIDS2API_HOST_PROXY environment variable for network egress that
requires a chained proxy.
"""

import os
from contextlib import asynccontextmanager

HOST_PROXY_ENV = "VIDS2API_HOST_PROXY"


@asynccontextmanager
async def transport_proxy(proxy: str | None):
    """Yield the effective proxy for one request without mutating session state."""
    hop = os.environ.get(HOST_PROXY_ENV, "").strip()
    yield hop if hop else proxy
