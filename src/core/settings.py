"""Validated contracts shared by configuration writes and admin endpoints."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class GenerationSettings(SettingsUpdate):
    image_timeout: Annotated[int, Field(ge=30, le=3600)] | None = None
    speech_timeout: Annotated[int, Field(ge=30, le=3600)] | None = None
    video_timeout: Annotated[int, Field(ge=60, le=7200)] | None = None
    max_retries: Annotated[int, Field(ge=0, le=10)] | None = None


class PoolSettings(SettingsUpdate):
    call_mode: Literal["default", "polling"] | None = None
    quota_preflight: bool | None = None
    error_ban_threshold: Annotated[int, Field(ge=1, le=100)] | None = None
    keepalive_enabled: bool | None = None
    keepalive_interval: Annotated[int, Field(ge=5, le=1440)] | None = None
    yescaptcha_api_key: str | None = None


class ProxySettings(SettingsUpdate):
    enabled: bool
    proxy_url: Annotated[str, Field(max_length=2048)] = ""

    @model_validator(mode="after")
    def enabled_proxy_has_address(self):
        if self.enabled and not self.proxy_url.strip():
            raise ValueError("启用代理时必须填写代理地址")
        return self


class ProxyTestSettings(SettingsUpdate):
    proxy_url: Annotated[str, Field(min_length=1, max_length=2048)]


class APIKeySettings(SettingsUpdate):
    api_key: Annotated[str, Field(min_length=8, max_length=512)]

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value):
        if any(ord(char) <= 32 or ord(char) >= 127 for char in value):
            raise ValueError("API Key 须使用非空白 ASCII 字符")
        return value


class CacheSettings(SettingsUpdate):
    enabled: bool | None = None
    cache_timeout: Annotated[int, Field(ge=0, le=86400)] | None = None
    cache_base_url: Annotated[str, Field(max_length=2048)] | None = None

    @field_validator("cache_base_url")
    @classmethod
    def validate_base_url(cls, value):
        if value is None or not value.strip():
            return value if value is None else ""
        value = value.strip().rstrip("/")
        try:
            address = urlsplit(value)
            valid = (
                address.scheme in {"http", "https"}
                and address.hostname
                and address.port != 0
                and not address.username
                and not address.password
                and not address.query
                and not address.fragment
                and not any(char.isspace() or ord(char) < 32 for char in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("访问地址须为不含认证信息、查询参数或片段的 HTTP(S) 地址")
        return value


class ExtensionSettings(SettingsUpdate):
    enabled: bool | None = None
    push_token: Annotated[str, Field(min_length=8, max_length=256)] | None = None
    regenerate: bool = False
    auto_enable_on_update: bool | None = None

    @field_validator("push_token")
    @classmethod
    def validate_push_token(cls, value):
        if value is not None and any(
            ord(char) <= 32 or ord(char) >= 127 for char in value
        ):
            raise ValueError("推送令牌须使用非空白 ASCII 字符")
        return value


class DebugSettings(SettingsUpdate):
    enabled: bool | None = None
    log_requests: bool | None = None
    log_responses: bool | None = None
    mask_token: bool | None = None


class LoginSettings(SettingsUpdate):
    auto_relogin_enabled: bool | None = None
    relogin_cooldown_seconds: Annotated[int, Field(ge=60, le=86400)] | None = None
    login_timeout_seconds: Annotated[int, Field(ge=60, le=1800)] | None = None


class CredentialsSettings(SettingsUpdate):
    username: Annotated[str, Field(min_length=1, max_length=128)]
    old_password: Annotated[str, Field(min_length=1, max_length=1024)]
    password: Annotated[str, Field(min_length=4, max_length=1024)]
    confirm_password: Annotated[str, Field(min_length=4, max_length=1024)]

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("请输入有效的管理员用户名")
        return value

    @model_validator(mode="after")
    def matching_passwords(self):
        if self.password != self.confirm_password:
            raise ValueError("两次输入的新密码不一致")
        return self
