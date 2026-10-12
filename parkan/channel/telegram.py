"""Отправка в Telegram через Bot API (без сторонних библиотек). Транспорт подменяется в тестах."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024


class TelegramError(RuntimeError):
    pass


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, path in files.items():
        p = Path(path)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{p.name}"\r\n'
                     f"Content-Type: image/png\r\n\r\n".encode() + p.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def urllib_transport(url: str, body: bytes, content_type: str, timeout: float = 60.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Bot:
    def __init__(self, token: str, transport=None, sleep=time.sleep):
        if not token:
            raise TelegramError("не задан TELEGRAM_BOT_TOKEN")
        self.token, self.transport, self.sleep = token, transport or urllib_transport, sleep

    def call(self, method: str, fields: dict, files: dict | None = None) -> dict:
        fields = {k: v for k, v in fields.items() if v is not None}
        if fields or files:
            body, ctype = _multipart(fields, files or {})
        else:   # пустой multipart Telegram отвергает (400), а методам без параметров он и не нужен
            body, ctype = b"", "application/x-www-form-urlencoded"
        for attempt in range(3):
            status, raw = self.transport(API.format(token=self.token, method=method), body, ctype)
            data = json.loads(raw.decode("utf-8") or "{}")
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if status == 429 and retry and attempt < 2:   # ограничение частоты: ждём, сколько просит Telegram
                self.sleep(retry)
                continue
            raise TelegramError(f"{method}: {data.get('description') or status}")
        raise TelegramError(f"{method}: превышено число попыток")

    def send_post(self, chat: str, text: str, card_path=None) -> None:
        """Карточка с подписью; если текст длиннее лимита подписи — карточка и текст отдельно."""
        if card_path and len(text) <= CAPTION_LIMIT:
            self.call("sendPhoto", {"chat_id": chat, "caption": text, "parse_mode": "HTML"}, {"photo": card_path})
            return
        if card_path:
            self.call("sendPhoto", {"chat_id": chat}, {"photo": card_path})
        self.call("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                  "link_preview_options": json.dumps({"is_disabled": True})})
