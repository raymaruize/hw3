import os
import requests

class NotifyError(RuntimeError):
    pass

def send_telegram(chat_id: str, text: str, timeout: int = 10) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise NotifyError("Missing env var TELEGRAM_BOT_TOKEN")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=timeout)
    r.raise_for_status()
