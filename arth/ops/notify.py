"""Push the daily summary to your phone. Configure one (or both) in /etc/arth.env:
    ARTH_NTFY_TOPIC=<a long random topic name>      # install the ntfy app and subscribe to the same topic
    ARTH_TG_TOKEN=<bot token>  ARTH_TG_CHAT=<chat id>
With neither set, the summary is only printed and written to reports/latest.txt.
"""
from __future__ import annotations
import os
import requests


def send(text: str, title: str = "Arth", attach: str | None = None) -> list[str]:
    sent = []
    topic = os.environ.get("ARTH_NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=text.encode(), headers={"Title": title}, timeout=15)
            sent.append("ntfy")
        except requests.RequestException:
            pass
    tok, chat = os.environ.get("ARTH_TG_TOKEN"), os.environ.get("ARTH_TG_CHAT")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": text}, timeout=15)
            if attach and os.path.exists(attach):          # the dashboard, openable on the phone
                with open(attach, "rb") as fh:
                    requests.post(f"https://api.telegram.org/bot{tok}/sendDocument", data={"chat_id": chat},
                                  files={"document": ("arth-dashboard.html", fh, "text/html")}, timeout=30)
            sent.append("telegram")
        except requests.RequestException:
            pass
    print(text)
    return sent
