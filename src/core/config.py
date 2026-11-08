"""Vids2API configuration.

The TOML file only seeds first-boot defaults; the database configuration tables are
the runtime source of truth and hot-reload into this in-memory singleton.
"""

from pathlib import Path

import tomli

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = BASE_DIR / "config" / "setting.toml"
CONFIG_EXAMPLE_PATH = BASE_DIR / "config" / "setting_example.toml"


def _bounded_int(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _load_config() -> dict:
    path = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE_PATH
    if not path.exists():
        return {}
    try:
        return tomli.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomli.TOMLDecodeError):
        return {}


class Config:
    """Mutable runtime configuration seeded from TOML, then overridden by the database."""

    def __init__(self):
        self._data = _load_config()

    # ---- server -----------------------------------------------------------
    @property
    def server_host(self) -> str:
        return str(self._data.get("server", {}).get("host", "0.0.0.0"))

    @property
    def server_port(self) -> int:
        try:
            return int(self._data.get("server", {}).get("port", 8687))
        except (TypeError, ValueError):
            return 8687

    # ---- global / admin ---------------------------------------------------
    @property
    def api_key(self) -> str:
        value = str(self._data.get("global", {}).get("api_key", "")).strip()
        return value or "sk-vids2api-default-key"

    def set_api_key(self, value: str) -> None:
        self._data.setdefault("global", {})["api_key"] = value

    @property
    def admin_username(self) -> str:
        return str(self._data.get("admin", {}).get("username", "admin"))

    @property
    def admin_password(self) -> str:
        return str(self._data.get("admin", {}).get("password", "admin"))

    def set_admin_credentials(self, username: str, password: str) -> None:
        self._data.setdefault("admin", {}).update(username=username, password=password)

    # ---- generation -------------------------------------------------------
    @property
    def image_timeout(self) -> int:
        try:
            return max(30, min(3600, int(self._data.get("generation", {}).get("image_timeout", 300))))
        except (TypeError, ValueError):
            return 300

    @property
    def speech_timeout(self) -> int:
        try:
            return max(30, min(3600, int(self._data.get("generation", {}).get("speech_timeout", 300))))
        except (TypeError, ValueError):
            return 300

    @property
    def video_timeout(self) -> int:
        try:
            return max(60, min(7200, int(self._data.get("generation", {}).get("video_timeout", 1500))))
        except (TypeError, ValueError):
            return 1500

    @property
    def max_retries(self) -> int:
        try:
            return max(0, min(10, int(self._data.get("generation", {}).get("max_retries", 3))))
        except (TypeError, ValueError):
            return 3

    def set_generation(self, *, image_timeout=None, speech_timeout=None, video_timeout=None,
                       max_retries=None) -> None:
        section = self._data.setdefault("generation", {})
        if image_timeout is not None:
            section["image_timeout"] = _bounded_int(image_timeout, 300, 30, 3600)
        if speech_timeout is not None:
            section["speech_timeout"] = _bounded_int(speech_timeout, 300, 30, 3600)
        if video_timeout is not None:
            section["video_timeout"] = _bounded_int(video_timeout, 1500, 60, 7200)
        if max_retries is not None:
            section["max_retries"] = _bounded_int(max_retries, 3, 0, 10)

    # ---- pool -------------------------------------------------------------
    @property
    def call_mode(self) -> str:
        value = str(self._data.get("pool", {}).get("call_mode", "default"))
        return value if value in {"default", "polling"} else "default"

    @property
    def quota_preflight(self) -> bool:
        return bool(self._data.get("pool", {}).get("quota_preflight", True))

    @property
    def error_ban_threshold(self) -> int:
        try:
            return max(1, min(100, int(
                self._data.get("pool", {}).get("error_ban_threshold", 5)
            )))
        except (TypeError, ValueError):
            return 5

    @property
    def keepalive_enabled(self) -> bool:
        return bool(self._data.get("pool", {}).get("keepalive_enabled", True))

    @property
    def keepalive_interval(self) -> int:
        try:
            return max(5, min(1440, int(
                self._data.get("pool", {}).get("keepalive_interval", 10)
            )))
        except (TypeError, ValueError):
            return 10

    @property
    def yescaptcha_api_key(self) -> str:
        return str(self._data.get("pool", {}).get("yescaptcha_api_key", "")).strip()

    def set_pool(
        self,
        *,
        call_mode=None,
        quota_preflight=None,
        error_ban_threshold=None,
        keepalive_enabled=None,
        keepalive_interval=None,
        yescaptcha_api_key=None,
    ) -> None:
        section = self._data.setdefault("pool", {})
        if call_mode is not None and call_mode in {"default", "polling"}:
            section["call_mode"] = call_mode
        if quota_preflight is not None:
            section["quota_preflight"] = bool(quota_preflight)
        if error_ban_threshold is not None:
            section["error_ban_threshold"] = _bounded_int(error_ban_threshold, 5, 1, 100)
        if keepalive_enabled is not None:
            section["keepalive_enabled"] = bool(keepalive_enabled)
        if keepalive_interval is not None:
            section["keepalive_interval"] = _bounded_int(keepalive_interval, 10, 5, 1440)
        if yescaptcha_api_key is not None:
            section["yescaptcha_api_key"] = str(yescaptcha_api_key).strip()
    # ---- login recovery ---------------------------------------------------
    @property
    def auto_relogin_enabled(self) -> bool:
        return bool(self._data.get("login", {}).get("auto_relogin_enabled", True))

    @property
    def relogin_cooldown_seconds(self) -> int:
        return _bounded_int(self._data.get("login", {}).get("relogin_cooldown_seconds", 3600),
                            3600, 60, 86400)

    @property
    def login_timeout_seconds(self) -> int:
        return _bounded_int(self._data.get("login", {}).get("login_timeout_seconds", 600),
                            600, 60, 1800)

    def set_login(self, **fields) -> None:
        section = self._data.setdefault("login", {})
        if fields.get("auto_relogin_enabled") is not None:
            section["auto_relogin_enabled"] = bool(fields["auto_relogin_enabled"])
        for name, default, low, high in (("relogin_cooldown_seconds", 3600, 60, 86400),
                                         ("login_timeout_seconds", 600, 60, 1800)):
            if fields.get(name) is not None:
                section[name] = _bounded_int(fields[name], default, low, high)

    # ---- cache ------------------------------------------------------------
    @property
    def cache_enabled(self) -> bool:
        return bool(self._data.get("cache", {}).get("enabled", True))

    @property
    def cache_timeout(self) -> int:
        try:
            return max(0, int(self._data.get("cache", {}).get("cache_timeout", 7200)))
        except (TypeError, ValueError):
            return 7200

    @property
    def cache_base_url(self) -> str:
        return str(self._data.get("cache", {}).get("cache_base_url", "")).strip()

    def set_cache(self, *, enabled=None, cache_timeout=None, cache_base_url=None) -> None:
        section = self._data.setdefault("cache", {})
        if enabled is not None:
            section["enabled"] = bool(enabled)
        if cache_timeout is not None:
            section["cache_timeout"] = int(cache_timeout)
        if cache_base_url is not None:
            section["cache_base_url"] = str(cache_base_url)

    # ---- debug ------------------------------------------------------------
    @property
    def debug_enabled(self) -> bool:
        return bool(self._data.get("debug", {}).get("enabled", False))

    @property
    def debug_log_requests(self) -> bool:
        return bool(self._data.get("debug", {}).get("log_requests", False))

    @property
    def debug_log_responses(self) -> bool:
        return bool(self._data.get("debug", {}).get("log_responses", False))

    @property
    def debug_mask_token(self) -> bool:
        return bool(self._data.get("debug", {}).get("mask_token", True))

    def set_debug(self, *, enabled=None, log_requests=None, log_responses=None,
                  mask_token=None) -> None:
        section = self._data.setdefault("debug", {})
        if enabled is not None:
            section["enabled"] = bool(enabled)
        if log_requests is not None:
            section["log_requests"] = bool(log_requests)
        if log_responses is not None:
            section["log_responses"] = bool(log_responses)
        if mask_token is not None:
            section["mask_token"] = bool(mask_token)

    # ---- paths ------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return BASE_DIR / "data"

    @property
    def accounts_dir(self) -> Path:
        return self.data_dir / "accounts"

    @property
    def tmp_dir(self) -> Path:
        return BASE_DIR / "tmp"

    @property
    def session_key_path(self) -> Path:
        return self.data_dir / "vids2api.key"


config = Config()
