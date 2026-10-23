"""Telegram Bot API sender. Message layout ported from alert() in the GPU scanner.

The bot token sits in the request URL, so httpx request logging is turned off and
every error is redacted before it is logged or returned.
"""

import logging

import httpx

from .runner import Notifier

log = logging.getLogger("dealwatch.telegram")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the URL, i.e. the token

API = "https://api.telegram.org/bot{token}/sendMessage"
TEST_TEXT = "✅ Deal Watch test message — Telegram alerts are working."


def format_alert(a: dict, base_url: str | None = None) -> str:
    n = a["count"]
    item = f"{n}x {a['watch']}" if n > 1 else a["watch"]
    pct = round((1 - a["ratio"]) * 100) if a["ratio"] is not None else None
    below = f" — {pct}% below typical" if pct is not None else ""
    if a["tier"] == "LONG SHOT":
        head = f"🎲 LONG SHOT: {item}{' in a bundle' if a['is_bundle'] else ''}{below}"
    elif a["is_bundle"]:
        head = f"🖥️🚨 BUNDLE DEAL: {item} inside a bundle" + (f" — {pct}% below the item alone" if pct is not None else "")
    else:
        head = f"{'🚨🚨' if a['ratio'] is not None and a['ratio'] <= 0.5 else '🚨'} DEAL: {item}{below}"
    price = f"${a['price_total']:,.0f} all-in"
    if a["typical"]:
        sub = f"{price} vs ~${a['typical'] * n:,.0f} typical ({a['typical_src']})"
    else:
        sub = f"{price}, at or under your ${a['max_price'] * n:,.0f} max price"
    lines = [head, sub, a["title"][:200], f"Source: {a['source']}" + (f" · {a['location']}" if a.get("location") else "")]
    if a.get("condition"):
        lines.append(f"Condition: {a['condition']}" + (" · Best Offer" if a.get("best_offer") else ""))
    if a.get("seller"):
        lines.append(f"Seller: {a['seller']}")
    if a["reasons"]:
        lines.append("Why it's a long shot: " + "; ".join(a["reasons"]))
    if a.get("note"):
        lines.append(f"({a['note']})")
    if a["is_bundle"]:
        lines.append(f"Confirm the {a['watch']} in the photos/specs; sellers sometimes list what a system 'supports'.")
    lines.append(a["url"])
    if base_url:
        lines.append(f"Deal Watch: {base_url.rstrip('/')}/watches/{a['watch_id']}")
    lines.append("Check photos, seller history & returns before buying.")
    return "\n".join(lines)


class TelegramNotifier(Notifier):
    def __init__(self, token: str | None, chat_ids: list[str], *, base_url: str | None = None,
                 dry_run: bool = False, enabled=lambda: True):
        self.token = token
        self.chat_ids = [c for c in chat_ids if c]
        self.base_url = base_url
        self.dry_run = dry_run
        self.enabled = enabled  # callable: reads the Settings on/off switch at send time

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_ids)

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "<token>") if self.token else text

    def send(self, text: str, quiet: bool = False, chat_ids: list[str] | None = None) -> tuple[bool, str]:
        """Send to every chat. (True, "") only if all succeeded; else (False, redacted reason)."""
        if self.dry_run:
            log.info("[dry-run telegram%s]\n%s", " (quiet)" if quiet else "", text)
            return False, "dry-run mode: not sent"
        if not self.configured:
            return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env"
        errors = []
        for chat in chat_ids or self.chat_ids:
            try:
                r = httpx.post(API.format(token=self.token), timeout=20, data={
                    "chat_id": chat, "text": text, "disable_web_page_preview": "true",
                    "disable_notification": "true" if quiet else "false"})
                body = r.json()
                if not body.get("ok"):
                    errors.append(f"chat {chat}: {body.get('description', r.status_code)}")
            except Exception as ex:
                errors.append(f"chat {chat}: {type(ex).__name__}: {ex}")
        if errors:
            reason = self._redact("; ".join(errors))
            log.warning("telegram send failed: %s", reason)
            return False, reason
        return True, ""

    def alert(self, a: dict) -> bool:
        if not self.enabled():
            return False
        return self.send(format_alert(a, self.base_url), quiet=a["quiet"])[0]

    def health(self, text: str) -> bool:
        if not self.enabled():
            return False
        return self.send(text)[0]

    def test(self) -> tuple[bool, str]:
        return self.send(TEST_TEXT)


def from_config(cfg, conn_factory=None, dry_run: bool = False) -> TelegramNotifier:
    """Notifier for the app: owner chat from .env; Settings' telegram_enabled switch read at send time."""
    def enabled():
        if conn_factory is None:
            return True
        from . import settings
        conn = conn_factory()
        try:
            return bool(settings.load(conn)["telegram_enabled"])
        finally:
            conn.close()
    return TelegramNotifier(cfg.telegram_bot_token, [cfg.telegram_chat_id or ""], base_url=cfg.base_url,
                            dry_run=dry_run or cfg.dry_run, enabled=enabled)
