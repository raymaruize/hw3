import os
import requests

class NotifyError(RuntimeError):
    pass

def send_telegram(chat_id: str, text: str, timeout: int = 10, parse_mode: str | None = "HTML") -> None:
    """Send a Telegram message.

    parse_mode:
      - "HTML" enables <b>bold</b> formatting without the pain of MarkdownV2 escaping.
      - Pass None to disable formatting.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise NotifyError("Missing env var TELEGRAM_BOT_TOKEN")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
        payload["disable_web_page_preview"] = True

    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
