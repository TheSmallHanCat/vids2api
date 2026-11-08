"""Request-log recorder for generation calls, feeding the admin dashboard."""

import time


class GenerationLogger:
    """Track one API generation through the request_logs table with progress updates."""

    def __init__(self, db):
        self.db = db

    async def start(self, *, operation: str, request_body: str,
                    generation_id: str | None = None) -> int:
        return await self.db.add_request_log(
            account_id=None,
            operation=operation,
            generation_id=generation_id,
            request_body=request_body,
            status_text="pending",
            progress=1,
        )

    async def progress(self, log_id: int, *, progress: int, status_text: str,
                       account_id: int | None = None):
        await self.db.update_request_log(
            log_id, progress=progress, status_text=status_text, account_id=account_id
        )

    async def finish(self, log_id: int, *, status_code: int, status_text: str,
                     response_body: str = "", started_at: float | None = None,
                     progress: int = 100):
        duration = round(time.monotonic() - started_at, 3) if started_at else None
        await self.db.update_request_log(
            log_id,
            status_code=status_code,
            status_text=status_text,
            response_body=response_body,
            duration=duration,
            progress=progress,
        )
