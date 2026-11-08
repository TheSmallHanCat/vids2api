"""Public generation API: OpenAI-shaped media endpoints over the Vids pool.

All generation endpoints require an Idempotency-Key; receipts are durable and
replays never resubmit paid work. Authentication uses the global API key.
"""

import asyncio
import base64
import hashlib
import json
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import UploadFile as FormUploadFile

from ..core.auth import verify_api_key_flexible
from ..core.config import config
from ..core.logger import debug_logger
from ..services.vids.client import PRESET_VOICE_OPTIONS
from ..services.vids.dispatcher import VidsDispatchError
from ..services.vids.gateway import GatewayError, VidsGateway

router = APIRouter()

generation_logger = None
pool = None
account_manager = None
file_cache = None


def set_dependencies(*, logger, pool_instance, account_manager_instance, file_cache_instance):
    global generation_logger, pool, account_manager, file_cache
    generation_logger = logger
    pool = pool_instance
    account_manager = account_manager_instance
    file_cache = file_cache_instance


MODELS = [
    {
        "id": "vids-image",
        "object": "model",
        "owned_by": "google",
        "provider": "vids",
        "kind": "image",
        "capabilities": {
            "modes": ["text", "edit"],
            "counts": [1, 2, 3, 4],
            "aspect_ratios": ["1:1", "16:9", "9:16"],
            "styles": ["auto", "photography", "background", "vector", "sketch",
                       "watercolor", "cyberpunk"],
        },
        "supported_endpoints": ["/v1/images/generations", "/v1/images/edits"],
    },
    {
        "id": "vids-speech",
        "object": "model",
        "owned_by": "google",
        "provider": "vids",
        "kind": "speech",
        "capabilities": {
            "voices": [option["id"] for option in PRESET_VOICE_OPTIONS],
            "input_limit": 4096,
            "response_format": "wav",
        },
        "supported_endpoints": ["/v1/audio/speech"],
    },
    {
        "id": "vids-video",
        "object": "model",
        "owned_by": "google",
        "provider": "vids",
        "kind": "video",
        "capabilities": {
            "modes": ["text", "animate", "ingredients", "edit", "extend", "upscale"],
            "seconds": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
            "sizes": ["1280x720", "720x1280", "1920x1080", "1080x1920"],
            "shot_modes": ["auto", "long_take", "montage"],
        },
        "supported_endpoints": ["/v1/videos"],
    },
]

STATUS_MAP = {
    "generating": "in_progress",
    "received": "in_progress",
    "completed": "completed",
    "failed": "failed",
    "unknown": "unknown",
    "no_result": "failed",
    "specification_mismatch": "failed",
    "download_failed": "in_progress",
    "inserting": "in_progress",
}


def _error(status: int, message: str, code: str, error_type: str = "invalid_request_error"):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": error_type, "code": code, "param": None}},
    )


def _gateway_error(error: GatewayError):
    response = _error(error.status, str(error), error.code)
    # Availability errors mean the request never ran; tell clients to back
    # off instead of hammering through a short upstream outage window.
    if error.status in (429, 503):
        response.headers["Retry-After"] = "60"
    return response


def _generation_id(idempotency_key: str) -> str:
    return "vids_" + hashlib.sha256(idempotency_key.encode()).hexdigest()


def _claimed_account(generation_id: str) -> int | None:
    return pool.dispatcher._claims.get(generation_id.removeprefix("vids_"))


def _require_idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key", "").strip()
    if not 1 <= len(key) <= 200:
        raise HTTPException(
            status_code=400,
            detail={"error": {
                "message": "Idempotency-Key must contain 1 to 200 characters",
                "type": "invalid_request_error", "code": "idempotency_required",
            }},
        )
    return key


def _base_url(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}" if host else str(request.base_url).rstrip("/")


def _safe_request_log(body: dict) -> str:
    def summarize(value, media=False, depth=0):
        if depth > 12:
            return "<nested payload>"
        if isinstance(value, str):
            return f"<{len(value)} chars>" if media or len(value) > 256 else value
        if isinstance(value, list):
            return [summarize(item, media, depth + 1) for item in value]
        if isinstance(value, dict):
            return {
                key: summarize(item, media or key in {"image_base64", "images_base64", "video_base64"}, depth + 1)
                for key, item in value.items()
            }
        return value

    return json.dumps(summarize(body), ensure_ascii=False, default=str)


# ---- models ---------------------------------------------------------------
@router.get("/v1/models")
@router.get("/models")
async def list_models(_api_key: str = Depends(verify_api_key_flexible)):
    return {"object": "list", "data": MODELS}


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str, _api_key: str = Depends(verify_api_key_flexible)):
    for model in MODELS:
        if model["id"] == model_id:
            return model
    raise HTTPException(status_code=404, detail="Model not found")


# ---- shared generation runner ---------------------------------------------
async def _run_generation(request: Request, operation: str, body: dict,
                          response_format: str = "url") -> JSONResponse:
    idempotency_key = _require_idempotency_key(request)
    if operation == "image" and response_format == "url" and not config.cache_enabled:
        return _error(400, "媒体缓存未启用，请使用 b64_json 响应格式", "cache_disabled")
    generation_id = _generation_id(idempotency_key)
    log_id = await generation_logger.start(
        operation=operation,
        generation_id=generation_id,
        request_body=_safe_request_log(body),
    )
    started = time.monotonic()
    try:
        await generation_logger.progress(log_id, progress=5, status_text="selecting_account")
        result = await pool.generate(None, body, idempotency_key)
        account_id = _claimed_account(result["id"])
        await generation_logger.progress(
            log_id, progress=60, status_text=result.get("status", "processing"),
            account_id=account_id,
        )
        completed = result.get("status") == "completed"
        status = 200 if completed else 202
        await generation_logger.finish(
            log_id, status_code=status, status_text=result.get("status", "processing"),
            response_body=json.dumps(result, ensure_ascii=False, default=str),
            started_at=started,
        )
        if account_id is not None:
            await account_manager.record_receipt(account_id, result)
        if completed and operation == "image":
            payload = await _image_payload(request, result["id"], response_format)
            response = JSONResponse(status_code=status, content=payload)
        else:
            response = JSONResponse(status_code=status, content=result)
        response.headers["X-Vids-Generation-Id"] = result["id"]
        return response
    except (GatewayError, VidsDispatchError) as error:
        await generation_logger.finish(
            log_id, status_code=error.status, status_text=error.code,
            response_body=str(error), started_at=started,
        )
        return _gateway_error(error)
    except asyncio.CancelledError:
        await generation_logger.finish(
            log_id, status_code=499, status_text="cancelled", started_at=started,
        )
        raise
    except Exception as error:
        debug_logger.log_error(operation, error)
        await generation_logger.finish(
            log_id, status_code=500, status_text="internal_error",
            response_body=str(error), started_at=started,
        )
        return _error(500, "内部服务错误", "internal_error", "server_error")


# ---- images ----------------------------------------------------------------
SIZE_TO_ASPECT = {
    "auto": "1:1", "1024x1024": "1:1", "1:1": "1:1",
    "16:9": "16:9", "1792x1024": "16:9", "1024x1792": "9:16", "9:16": "9:16",
}


async def _image_payload(request: Request, generation_id: str,
                         response_format: str) -> dict:
    record = pool.get(None, generation_id)
    outputs = record.get("outputs") or [{"index": 0, "metadata": record.get("metadata", {})}]
    data = []
    for output in outputs:
        index = output.get("index", 0)
        path, _mime = pool.content(None, generation_id, index=index)
        if response_format == "b64_json":
            data.append({"b64_json": base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode("ascii")})
        else:
            cached = await asyncio.to_thread(file_cache.store, f"{generation_id}_{index}", path, ".jpg")
            base = config.cache_base_url or _base_url(request)
            data.append({"url": file_cache.public_url(cached, base)})
    return {"created": record.get("created_at", 0), "data": data}


@router.post("/v1/images/generations")
async def create_image(request: Request, _api_key: str = Depends(verify_api_key_flexible)):
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "请求体必须是 JSON", "invalid_request")
    if not isinstance(body, dict) or not isinstance(body.get("prompt"), str):
        return _error(400, "prompt 为必填字符串", "invalid_request")
    request_body = {"kind": "image", "prompt": body["prompt"], "n": body.get("n", 1)}
    size = str(body.get("size", "auto"))
    request_body["aspect_ratio"] = body.get("aspect_ratio") or SIZE_TO_ASPECT.get(size, "1:1")
    if body.get("style"):
        request_body["style"] = body["style"]
    return await _run_generation(
        request, "image", request_body, response_format=body.get("response_format", "url")
    )


@router.post("/v1/images/edits")
async def edit_image(
    request: Request,
    image: UploadFile = File(...),
    prompt: str = Form(...),
    _api_key: str = Depends(verify_api_key_flexible),
):
    data = await image.read()
    if not 0 < len(data) <= 10 * 1024**2:
        return _error(400, "图片必须小于 10 MiB", "invalid_image")
    request_body = {
        "kind": "image",
        "mode": "edit",
        "prompt": prompt,
        "image_base64": base64.b64encode(data).decode("ascii"),
    }
    return await _run_generation(request, "image", request_body)


# ---- speech ----------------------------------------------------------------
@router.post("/v1/audio/speech")
async def create_speech(request: Request, _api_key: str = Depends(verify_api_key_flexible)):
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "请求体必须是 JSON", "invalid_request")
    if not isinstance(body, dict) or not isinstance(body.get("input"), str):
        return _error(400, "input 为必填字符串", "invalid_request")
    request_body = {
        "kind": "speech",
        "prompt": body["input"][:4096],
        "voice": body.get("voice", "Puck"),
    }
    idempotency_key = _require_idempotency_key(request)
    generation_id = _generation_id(idempotency_key)
    log_id = await generation_logger.start(
        operation="speech",
        generation_id=generation_id,
        request_body=json.dumps(
            {"input_length": len(body["input"]), "voice": request_body["voice"]},
            ensure_ascii=False,
        ),
    )
    started = time.monotonic()
    try:
        await generation_logger.progress(log_id, progress=10, status_text="selecting_account")
        result = await pool.generate(None, request_body, idempotency_key)
        account_id = _claimed_account(result["id"])
        if account_id is not None:
            await account_manager.record_receipt(account_id, result)
        if result.get("status") != "completed":
            await generation_logger.finish(
                log_id, status_code=202, status_text=result.get("status", "processing"),
                response_body=json.dumps(result, ensure_ascii=False, default=str),
                started_at=started, progress=50,
            )
            return JSONResponse(
                status_code=202,
                content={"error": {
                    "message": "语音尚未就绪，请稍后通过收据查询",
                    "type": "not_ready_error", "code": "speech_not_ready",
                    "vids_receipt": result,
                }},
            )
        path, mime = pool.content(None, result["id"])
        await generation_logger.finish(
            log_id, status_code=200, status_text="completed",
            response_body=json.dumps(result, ensure_ascii=False, default=str),
            started_at=started,
        )
        return FileResponse(
            path, media_type=mime, headers={"X-Vids-Generation-Id": result["id"]},
        )
    except (GatewayError, VidsDispatchError) as error:
        await generation_logger.finish(
            log_id, status_code=error.status, status_text=error.code,
            response_body=str(error), started_at=started,
        )
        return _gateway_error(error)


# ---- videos ----------------------------------------------------------------
async def _read_video_request(request: Request) -> dict | JSONResponse:
    content_type = request.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "请求体必须是 JSON 或 multipart", "invalid_request")
        if not isinstance(body, dict) or not isinstance(body.get("prompt"), str):
            return _error(400, "prompt 为必填字符串", "invalid_request")
        mapped = {"kind": "video", "prompt": body["prompt"]}
        for key in ("mode", "seconds", "size", "shot_mode", "source_id", "image_base64",
                    "images_base64", "video_base64"):
            if key in body:
                mapped[key] = body[key]
        return mapped
    async with request.form(max_files=8) as form:
        prompt = form.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _error(400, "prompt 为必填字符串", "invalid_request")
        body = {"kind": "video", "prompt": prompt}
        mode = form.get("mode")
        if isinstance(mode, str) and mode:
            body["mode"] = mode
        videos = form.getlist("input_video")
        if videos:
            if len(videos) != 1 or not isinstance(videos[0], FormUploadFile):
                return _error(400, "input_video 必须是单个视频文件", "invalid_video")
            data = await videos[0].read(50 * 1024**2 + 1)
            if not 0 < len(data) <= 50 * 1024**2:
                return _error(400, "视频不能为空且不得超过 50 MiB", "invalid_video")
            body["video_base64"] = base64.b64encode(data).decode("ascii")
        references = []
        total_bytes = 0
        for field in ("input_reference", "images"):
            for item in form.getlist(field):
                if not isinstance(item, FormUploadFile):
                    return _error(400, "参考图必须是文件", "invalid_image")
                if len(references) >= 7:
                    return _error(400, "参考图最多为 7 张", "invalid_image")
                data = await item.read(10 * 1024**2 - total_bytes + 1)
                total_bytes += len(data)
                if not data or total_bytes > 10 * 1024**2:
                    return _error(400, "参考图不能为空且总大小不得超过 10 MiB", "invalid_image")
                references.append(base64.b64encode(data).decode("ascii"))
        if references:
            if len(references) == 1 and body.get("mode") not in {"ingredients", "edit"}:
                body["image_base64"] = references[0]
            else:
                body["images_base64"] = references
        for scalar in ("size", "shot_mode"):
            value = form.get(scalar)
            if value is not None:
                body[scalar] = str(value)
        seconds = form.get("seconds")
        if seconds is not None:
            try:
                body["seconds"] = int(seconds)
            except (TypeError, ValueError):
                return _error(400, "seconds 必须是整数", "invalid_request")
        return body


@router.post("/v1/videos")
async def create_video(request: Request, _api_key: str = Depends(verify_api_key_flexible)):
    body = await _read_video_request(request)
    if isinstance(body, JSONResponse):
        return body
    return await _run_generation(request, "video", body)


def _public_video(record: dict) -> dict:
    result = {
        "id": record["id"],
        "object": "video",
        "created_at": record.get("created_at", 0),
        "status": STATUS_MAP.get(record.get("status", "unknown"), "unknown"),
        "model": "vids-video",
        "kind": "vids",
    }
    error = record.get("error")
    if error:
        result["error"] = {"code": error}
    if isinstance(record.get("upstream_error"), dict):
        result["upstream_error"] = {
            key: value for key, value in record["upstream_error"].items()
            if isinstance(value, str)
        }
    specification = record.get("video_specification") or {}
    if specification:
        result["seconds"] = specification.get("seconds")
        result["size"] = specification.get("size")
    if record.get("status") == "completed":
        result["content_url"] = f"/v1/videos/{record['id']}/content"
    return result


@router.get("/v1/videos")
async def list_videos(
    limit: int = 20, after: str = "", order: str = "desc",
    _api_key: str = Depends(verify_api_key_flexible),
):
    if not 1 <= limit <= 100:
        return _error(400, "limit 必须在 1 到 100 之间", "invalid_request")
    records = pool.list(None)
    items = [_public_video(record) for record in records if record.get("kind") == "video"]
    items.sort(key=lambda item: item["created_at"], reverse=(order != "asc"))
    if after:
        index = next((i for i, item in enumerate(items) if item["id"] == after), -1)
        items = items[index + 1:] if index >= 0 else []
    page = items[:limit]
    return {
        "object": "list",
        "data": page,
        "has_more": len(items) > limit,
        "first_id": page[0]["id"] if page else None,
        "last_id": page[-1]["id"] if page else None,
    }


@router.get("/v1/videos/{video_id}")
async def get_video(video_id: str, _api_key: str = Depends(verify_api_key_flexible)):
    try:
        record = pool.get(None, video_id)
    except (GatewayError, VidsDispatchError) as error:
        return _gateway_error(error)
    return _public_video(record)


@router.get("/v1/videos/{video_id}/content")
async def get_video_content(video_id: str, _api_key: str = Depends(verify_api_key_flexible)):
    try:
        path, mime = pool.content(None, video_id)
    except (GatewayError, VidsDispatchError) as error:
        return _gateway_error(error)
    return FileResponse(path, media_type=mime, headers={"X-Vids-Generation-Id": video_id})


# ---- raw generation receipts -------------------------------------------------
@router.post("/v1/vids/generations")
async def create_generation(request: Request, _api_key: str = Depends(verify_api_key_flexible)):
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "请求体必须是 JSON", "invalid_request")
    if not isinstance(body, dict):
        return _error(400, "请求体必须是 JSON 对象", "invalid_request")
    kind = body.get("kind")
    if kind == "music":
        return _error(400, "音乐生成不在当前支持范围内", "invalid_kind")
    return await _run_generation(request, kind if isinstance(kind, str) else "generation", body)


@router.get("/v1/vids/generations/{generation_id}")
async def get_generation(generation_id: str, _api_key: str = Depends(verify_api_key_flexible)):
    try:
        record = pool.get(None, generation_id)
    except (GatewayError, VidsDispatchError) as error:
        return _gateway_error(error)
    return VidsGateway.public(record)


@router.post("/v1/vids/generations/{generation_id}/download")
async def retry_download(generation_id: str, _api_key: str = Depends(verify_api_key_flexible)):
    try:
        result = await pool.retry_download(None, generation_id)
        account_id = _claimed_account(generation_id)
        if account_id is not None:
            await account_manager.record_receipt(account_id, result)
    except (GatewayError, VidsDispatchError) as error:
        return _gateway_error(error)
    return result


@router.get("/v1/vids/generations/{generation_id}/content")
async def generation_content(generation_id: str, index: int = 0,
                             _api_key: str = Depends(verify_api_key_flexible)):
    try:
        path, mime = pool.content(None, generation_id, index=index)
    except (GatewayError, VidsDispatchError) as error:
        return _gateway_error(error)
    return FileResponse(path, media_type=mime)
