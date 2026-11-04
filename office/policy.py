"""Guardrails. The owner sets the rules once in company.yaml; agents act freely inside them.

Everything an agent does that touches the outside world passes through here:
  * outbound messages: kill switch, opt-outs, anti-spam caps, AI disclosure, secrets filter
  * LLM spend: daily budget
  * prices / money: thresholds that require the owner
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config
from .db import DB, day_start

OPT_OUT_PATTERNS = re.compile(
    r"\b(unsubscribe|stop|remove me|opt[- ]?out|не пишите|отпишите|отписаться|"
    r"больше не пишите|удалите меня|стоп)\b", re.IGNORECASE)

# Things that must never leave the office
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk_(live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"whsec_[A-Za-z0-9]{10,}"),
    re.compile(r"\b\d{9,10}:[A-Za-z0-9_\-]{30,}\b"),            # telegram bot token
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),                        # card-number-like
    re.compile(r"(?i)password\s*[:=]\s*\S+|парол[ья]\s*[:=]\s*\S+"),
]

DISCLOSURE = {
    "ru": "— Сообщение подготовлено ИИ-ассистентом {company}. Если не хотите получать письма, просто ответьте «стоп».",
    "en": "— This message was prepared by an AI assistant at {company}. Reply \"stop\" to opt out.",
}


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    body: str = ""


class Policy:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db

    # ---------- global switches ----------
    def stopped(self) -> bool:
        return self.db.get_setting("kill_switch") == "1"

    def paused(self) -> bool:
        return self.stopped() or self.db.get_setting("paused") == "1"

    def budget_left(self) -> float:
        return float(self.cfg.limit("daily_llm_budget_usd", 10)) - self.db.spend_today()

    def budget_ok(self) -> bool:
        return self.budget_left() > 0

    # ---------- outbound ----------
    def is_cold(self, channel: str, to_addr: str) -> bool:
        """Cold = we never got a message from this person and they are not a customer."""
        if channel == "telegram_channel":
            return False
        contact = self.db.contact(to_addr)
        if contact and contact["stage"] in ("replied", "negotiating", "customer"):
            return False
        return not self.db.has_inbound_from(to_addr)

    def precheck_outbound(self, channel: str, to_addr: str, body: str) -> Verdict:
        """Checked when an agent drafts a message (so it gets instant feedback)."""
        if self.stopped():
            return Verdict(False, "Офис остановлен владельцем (аварийная остановка).")
        if not body.strip():
            return Verdict(False, "Пустое сообщение.")
        if channel not in ("email", "telegram", "telegram_channel"):
            return Verdict(False, f"Неизвестный канал {channel}. Доступны: email, telegram, telegram_channel.")
        if channel == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to_addr.strip()):
            return Verdict(False, f"Некорректный email: {to_addr}")
        for pat in SECRET_PATTERNS:
            if pat.search(body):
                return Verdict(False, "В сообщении похоже на секрет/пароль/номер карты — отправка запрещена.")
        contact = self.db.contact(to_addr)
        if contact and contact["opted_out"]:
            return Verdict(False, f"{to_addr} отказался от сообщений. Писать ему запрещено.")
        if channel != "telegram_channel":
            per_week = int(self.cfg.limit("max_messages_per_contact_per_week", 3))
            if self.db.messages_to_contact_since(to_addr, self.db.week_ago()) >= per_week:
                return Verdict(False, f"Лимит {per_week} сообщений в неделю для {to_addr} исчерпан. "
                                      "Не надоедайте человеку — дождитесь ответа.")
        today = day_start()
        if self.db.outbound_count_since(today) >= int(self.cfg.limit("max_outbound_per_day", 40)):
            return Verdict(False, "Дневной лимит исходящих сообщений исчерпан. Продолжим завтра.")
        if self.is_cold(channel, to_addr):
            cap = int(self.cfg.limit("max_cold_outreach_per_day", 15))
            if self.db.outbound_count_since(today, cold_only=True) >= cap:
                return Verdict(False, "Дневной лимит первых писем новым людям исчерпан.")
        return Verdict(True, body=body)

    def finalize_body(self, channel: str, body: str) -> str:
        """Appends the honest AI disclosure (and opt-out line) to messages to people."""
        if not self.cfg.limit("require_ai_disclosure", True) or channel == "telegram_channel":
            return body
        lang = self.cfg.company.get("language", "ru")
        line = DISCLOSURE.get(lang, DISCLOSURE["en"]).format(company=self.cfg.company.get("name", ""))
        if line in body:
            return body
        return f"{body.rstrip()}\n\n{line}"

    def final_check(self, msg: dict) -> Verdict:
        """Checked right before a connector actually sends."""
        if self.stopped():
            return Verdict(False, "Аварийная остановка.")
        contact = self.db.contact(msg["to_addr"])
        if contact and contact["opted_out"]:
            return Verdict(False, "Получатель отказался от сообщений.")
        for pat in SECRET_PATTERNS:
            if pat.search(msg["body"]):
                return Verdict(False, "Обнаружен секрет в тексте.")
        return Verdict(True, body=self.finalize_body(msg["channel"], msg["body"]))

    def needs_owner_approval_for_outbound(self) -> bool:
        return self.cfg.auto("mode", "autonomous") == "supervised"

    # ---------- inbound ----------
    @staticmethod
    def is_opt_out(text: str) -> bool:
        return bool(OPT_OUT_PATTERNS.search(text[:300]))

    # ---------- money ----------
    def price_ok(self, price: float) -> Verdict:
        cap = float(self.cfg.limit("max_price_without_owner_usd", 500))
        if price <= 0:
            return Verdict(False, "Цена должна быть больше нуля.")
        if price > cap:
            return Verdict(False, f"Цена {price} выше лимита {cap}. Нужен ответ владельца — используйте escalate_to_owner.")
        return Verdict(True)
