"""Telegram Bot API: direct chats with people + posting to a channel."""
from __future__ import annotations

import os

import httpx


class TelegramConnector:
    channel = "telegram"

    def __init__(self) -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "")
        self._offset = 0

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, chat_id: str, text: str) -> str:
        r = httpx.post(self._url("sendMessage"), json={"chat_id": chat_id, "text": text[:4096]}, timeout=30)
        r.raise_for_status()
        return str(r.json().get("result", {}).get("message_id", ""))

    def post_to_channel(self, text: str, channel_id: str | None = None) -> str:
        return self.send(channel_id or self.channel_id, text)

    def fetch_updates(self) -> list[dict]:
        """People can only be messaged in Telegram after they write to the bot first."""
        r = httpx.get(self._url("getUpdates"), params={"offset": self._offset, "timeout": 0}, timeout=30)
        r.raise_for_status()
        out = []
        for upd in r.json().get("result", []):
            self._offset = max(self._offset, upd["update_id"] + 1)
            msg = upd.get("message") or {}
            text = msg.get("text")
            if not text or msg.get("chat", {}).get("type") != "private":
                continue
            user = msg.get("from", {})
            out.append({
                "channel": "telegram",
                "from_addr": str(msg["chat"]["id"]),
                "from_name": " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
                             or user.get("username", ""),
                "subject": "",
                "body": text,
                "external_id": f"tg-{upd['update_id']}",
            })
        return out
