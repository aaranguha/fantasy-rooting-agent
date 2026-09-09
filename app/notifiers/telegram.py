"""Telegram bot notifications - free, reliable, works anywhere.

Setup:
  1. Message @BotFather on Telegram, /newbot, copy the token.
  2. Send your new bot any message.
  3. `fantasy-agent setup` (or curl the getUpdates URL) to grab your chat id.
"""

from __future__ import annotations

import os
from typing import Optional

import requests

from .base import Notifier, NotificationError

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier(Notifier):
    name = "telegram"
    supports_delete = True

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, **kw) -> None:
        super().__init__(**kw)
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

    @property
    def bot_id(self) -> str:
        """A bot token is '<bot_id>:<secret>', so the id is free to read."""
        return self.token.split(":", 1)[0] if ":" in self.token else ""

    def available(self) -> tuple[bool, str]:
        if not self.token:
            return False, "TELEGRAM_BOT_TOKEN is not set in .env"
        if not self.chat_id:
            return False, "TELEGRAM_CHAT_ID is not set in .env"
        if self.bot_id and str(self.chat_id).strip() == self.bot_id:
            # Telegram answers this with a 403 "the bot can't send messages to
            # the bot", which is easy to misread as an auth problem.
            return False, (
                "TELEGRAM_CHAT_ID is the BOT's own id (the number before ':' in the "
                "token), not yours. A bot cannot message itself. "
                "Run: fantasy-agent telegram-chat-id")
        return True, ""

    def _send(self, title: str, body: str) -> tuple[str, Optional[str]]:
        r = requests.post(
            API.format(token=self.token, method="sendMessage"),
            json={"chat_id": self.chat_id, "text": f"{title}\n\n{body}",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code >= 400:
            raise NotificationError(f"Telegram returned {r.status_code}: {r.text[:200]}")
        message_id = None
        try:
            message_id = str(r.json().get("result", {}).get("message_id") or "") or None
        except ValueError:
            pass  # message sent fine; just couldn't parse an id back out of it
        return "sent via Telegram bot", message_id

    def delete(self, message_id: str) -> bool:
        """Recall a message this bot sent earlier (used to swap the morning
        preview for the real kickoff push once it lands).

        Telegram only allows deleting a bot's own messages, and only within 48h -
        both true here by construction, so failures are quietly non-fatal: the
        old message just stays visible instead of disappearing.
        """
        if not message_id:
            return False
        try:
            r = requests.post(
                API.format(token=self.token, method="deleteMessage"),
                json={"chat_id": self.chat_id, "message_id": int(message_id)},
                timeout=15,
            )
        except (requests.RequestException, ValueError):
            return False
        try:
            return bool(r.json().get("ok"))
        except ValueError:
            return False

    def me(self) -> dict:
        """Which bot does this token actually belong to? (getMe)

        The decisive check when discovery finds nothing: if the username here
        isn't the bot you've been messaging, the token is for a different bot.
        """
        r = requests.get(API.format(token=self.token, method="getMe"), timeout=15)
        if r.status_code == 401:
            raise NotificationError("Telegram rejected the token (401 Unauthorized)")
        r.raise_for_status()
        return r.json().get("result", {}) or {}

    def raw_update_count(self) -> int:
        r = requests.get(API.format(token=self.token, method="getUpdates"),
                         params={"limit": 100}, timeout=15)
        r.raise_for_status()
        return len(r.json().get("result", []))

    def discover_chat_id(self) -> list[dict]:
        """Human chats that have messaged this bot (setup helper).

        Bots are filtered out: the only way a bot id ends up here is if the bot
        talked to itself, and it can never be a valid destination.
        """
        r = requests.get(API.format(token=self.token, method="getUpdates"),
                         params={"limit": 100}, timeout=15)
        r.raise_for_status()
        out, seen = [], set()
        for upd in r.json().get("result", []):
            msg = (upd.get("message") or upd.get("edited_message")
                   or upd.get("channel_post") or {})
            chat = msg.get("chat") or {}
            sender = msg.get("from") or {}
            cid = chat.get("id")
            if not cid or cid in seen:
                continue
            if sender.get("is_bot") or str(cid) == self.bot_id:
                continue
            seen.add(cid)
            out.append({
                "id": str(cid),
                "name": (chat.get("username") or chat.get("title")
                         or " ".join(x for x in (chat.get("first_name"),
                                                 chat.get("last_name")) if x)
                         or str(cid)),
                "type": chat.get("type", "private"),
            })
        return out
