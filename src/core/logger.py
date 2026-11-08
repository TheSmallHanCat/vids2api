"""Debug logging to logs.txt with credential masking; quiet unless debug is enabled."""

import json
import logging
import re
from pathlib import Path

from .config import BASE_DIR, config

LOG_PATH = BASE_DIR / "logs.txt"

def _mask(value: str) -> str:
    if len(value) <= 12:
        return "***"
    return f"{value[:6]}***{value[-4:]}"


class DebugLogger:
    def __init__(self):
        self._logger = logging.getLogger("vids2api.debug")
        self._logger.propagate = False
        self._configured = False

    def _ensure_handler(self):
        if self._configured:
            return
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self._logger.addHandler(handler)
        self._logger.setLevel(logging.INFO)
        self._configured = True

    def _write(self, level: str, message: str):
        if not config.debug_enabled:
            return
        try:
            self._ensure_handler()
            self._logger.log(logging.getLevelName(level), message)
        except Exception:
            # Diagnostics must not discard an accepted generation response.
            return

    def _summary(self, value, depth=0):
        if depth >= 6:
            return "<nested>"
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:24]:
                name = str(key)
                label = name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", name) else "<field>"
                sensitive = any(part in name.lower() for part in (
                    "cookie", "token", "password", "secret", "authorization", "credential",
                    "base64", "session", "email", "phone", "key", "header",
                ))
                result[label] = "<redacted>" if sensitive else self._summary(item, depth + 1)
            return {"type": "object", "count": len(value), "fields": result}
        if isinstance(value, (list, tuple)):
            return {"type": "array", "count": len(value),
                    "items": [self._summary(item, depth + 1) for item in value[:12]]}
        if isinstance(value, str):
            return "<redacted>" if config.debug_mask_token else {"type": "string", "length": len(value)}
        if isinstance(value, (bytes, bytearray)):
            return {"type": "binary", "length": len(value)}
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return "<unsupported>"

    def _clean(self, payload) -> str:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                return json.dumps(self._summary(payload))
        if isinstance(payload, dict):
            cleaned = {}
            for key, value in payload.items():
                if key == "method" and value in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
                    cleaned[key] = value
                elif key == "path" and isinstance(value, str):
                    path = value.split("?", 1)[0].split("#", 1)[0]
                    path = re.sub(r"(/operations/).*", r"\1<id>", path)
                    cleaned[key] = path if path.startswith("/v1/genai/") or path.startswith("/v1/operations/") else "<path>"
                elif key in {"status", "duration_ms", "content_bytes"} and isinstance(value, (int, float)):
                    cleaned[key] = value
                else:
                    cleaned[key] = self._summary(value)
            return json.dumps(cleaned, ensure_ascii=False, default=str)[:8192]
        return json.dumps(self._summary(payload), ensure_ascii=False)[:8192]

    def log_request(self, operation: str, payload):
        if not config.debug_enabled or not config.debug_log_requests:
            return
        try:
            self._write("INFO", f"[{operation}] request: {self._clean(payload)}")
        except Exception:
            return

    def log_response(self, operation: str, payload):
        if not config.debug_enabled or not config.debug_log_responses:
            return
        try:
            self._write("INFO", f"[{operation}] response: {self._clean(payload)}")
        except Exception:
            return

    def log_error(self, operation: str, error):
        details = {}
        for key in ("status", "errno"):
            value = getattr(error, key, None)
            if isinstance(value, int):
                details[key] = value
        origin = ""
        traceback = getattr(error, "__traceback__", None)
        while traceback is not None:
            path = Path(traceback.tb_frame.f_code.co_filename)
            if path.is_relative_to(BASE_DIR / "src"):
                origin = f" {path.name}:{traceback.tb_lineno}"
            traceback = traceback.tb_next
        self._write("ERROR", f"[{operation}] {type(error).__name__}{origin}: {self._clean(details)}")

    def log_info(self, operation: str, message):
        self._write("INFO", f"[{operation}] {self._clean(message)}")


debug_logger = DebugLogger()
