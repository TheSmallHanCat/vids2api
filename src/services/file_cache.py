"""Media cache for URL-mode API responses, served from the project-local tmp/ directory."""

import asyncio
import hashlib
import shutil
import threading
import time
import uuid
from pathlib import Path

from ..core.config import config


class FileCache:
    def __init__(self, cache_dir: str | Path | None = None):
        self.cache_dir = Path(cache_dir) if cache_dir else config.tmp_dir / "media"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_task: asyncio.Task | None = None
        self._file_lock = threading.Lock()

    def _cache_path(self, key: str, suffix: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.cache_dir / f"{digest}{suffix}"

    def store(self, key: str, source: Path, suffix: str) -> Path:
        with self._file_lock:
            target = self._cache_path(key, suffix)
            if target.exists():
                target.touch()
                return target
            temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
            try:
                shutil.copyfile(source, temporary)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            return target

    def cleanup_expired(self):
        timeout = config.cache_timeout
        if timeout <= 0:
            return
        deadline = time.time() - timeout
        for path in self.cache_dir.iterdir():
            try:
                with self._file_lock:
                    if path.is_file() and path.stat().st_mtime < deadline:
                        path.unlink()
            except OSError:
                continue

    async def start_cleanup_loop(self):
        if self._cleanup_task is not None:
            return

        async def loop():
            while True:
                await asyncio.sleep(600)
                try:
                    await asyncio.to_thread(self.cleanup_expired)
                except Exception:
                    pass

        self._cleanup_task = asyncio.create_task(loop())

    async def stop_cleanup_loop(self):
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
            self._cleanup_task = None

    def public_url(self, path: Path, base_url: str) -> str:
        base = (base_url or "").rstrip("/")
        return f"{base}/tmp/media/{path.name}"
