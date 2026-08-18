from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

import requests


@dataclass(frozen=True)
class TelegramSendResult:
    ok: bool
    status_code: int
    delivery_ms: float
    error: str | None = None


class TelegramService:
    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token.strip()

    def send_message(self, chat_id: str, text: str) -> TelegramSendResult:
        if not self._bot_token:
            return TelegramSendResult(ok=False, status_code=0, delivery_ms=0.0, error="missing_bot_token")
        if not chat_id:
            return TelegramSendResult(ok=False, status_code=0, delivery_ms=0.0, error="missing_chat_id")

        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        started = perf_counter()
        try:
            response = requests.post(
                url,
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                timeout=10,
            )
            elapsed = (perf_counter() - started) * 1000.0
            if response.status_code >= 400:
                return TelegramSendResult(
                    ok=False,
                    status_code=response.status_code,
                    delivery_ms=elapsed,
                    error=f"telegram_http_{response.status_code}",
                )
            payload = response.json()
            if not bool(payload.get("ok", False)):
                return TelegramSendResult(
                    ok=False,
                    status_code=response.status_code,
                    delivery_ms=elapsed,
                    error=str(payload.get("description", "telegram_unknown_error")),
                )
            return TelegramSendResult(ok=True, status_code=response.status_code, delivery_ms=elapsed, error=None)
        except Exception as exc:  # pragma: no cover - defensive
            elapsed = (perf_counter() - started) * 1000.0
            return TelegramSendResult(ok=False, status_code=0, delivery_ms=elapsed, error=str(exc))
