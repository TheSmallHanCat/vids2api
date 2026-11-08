"""Pydantic models for database rows and API payloads."""

from pydantic import BaseModel


class Account(BaseModel):
    """One Vids account backed by an encrypted cookie session snapshot."""

    id: int
    email: str = ""
    identity_hash: str
    editor_url: str
    auth_user: int = 0
    remark: str = ""
    is_active: bool = True
    image_enabled: bool = True
    video_enabled: bool = True
    speech_enabled: bool = True
    proxy_url: str = ""
    status: str = "starting"
    last_error: str = ""
    use_count: int = 0
    image_count: int = 0
    video_count: int = 0
    speech_count: int = 0
    error_count: int = 0
    consecutive_error_count: int = 0
    quota_image_total: int | None = None
    quota_image_available: int | None = None
    quota_image_checked_at: str | None = None
    quota_video_total: int | None = None
    quota_video_available: int | None = None
    quota_video_checked_at: str | None = None
    quota_speech_total: int | None = None
    quota_speech_available: int | None = None
    quota_speech_checked_at: str | None = None
    created_at: str = ""
    last_used_at: str | None = None
    banned_at: str | None = None
    ban_reason: str = ""


class AccountStats(BaseModel):
    account_id: int
    today_date: str = ""
    today_image: int = 0
    today_video: int = 0
    today_speech: int = 0
    today_error: int = 0
    total_image: int = 0
    total_video: int = 0
    total_speech: int = 0
    total_error: int = 0


class RequestLog(BaseModel):
    id: int
    account_id: int | None = None
    operation: str
    generation_id: str | None = None
    request_body: str = ""
    response_body: str = ""
    status_code: int | None = None
    status_text: str = ""
    progress: int = 0
    duration: float | None = None
    created_at: str = ""
    updated_at: str = ""
    account_email: str | None = None
    state: str = "processing"


class AdminConfig(BaseModel):
    username: str
    api_key: str
    error_ban_threshold: int = 5


class GenerationConfig(BaseModel):
    image_timeout: int = 300
    speech_timeout: int = 300
    video_timeout: int = 1500
    max_retries: int = 3


class PoolConfig(BaseModel):
    call_mode: str = "default"
    quota_preflight: bool = True
    error_ban_threshold: int = 5


class ProxyConfig(BaseModel):
    enabled: bool = False
    proxy_url: str = ""


class CacheConfig(BaseModel):
    enabled: bool = True
    cache_timeout: int = 7200
    cache_base_url: str = ""


class DebugConfig(BaseModel):
    enabled: bool = False
    log_requests: bool = False
    log_responses: bool = False
    mask_token: bool = True
