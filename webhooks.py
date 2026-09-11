import json
import os
import threading

WEBHOOK_FILE = "webhooks.json"
_lock = threading.Lock()


def _load():
    if not os.path.exists(WEBHOOK_FILE):
        return []
    try:
        with open(WEBHOOK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(hooks):
    with open(WEBHOOK_FILE, "w", encoding="utf-8") as f:
        json.dump(hooks, f, ensure_ascii=False, indent=2)


def get_all():
    with _lock:
        return _load()


def add(url: str) -> bool:
    url = url.strip()
    if not url.startswith("https://discord.com/api/webhooks/") and not url.startswith("https://discordapp.com/api/webhooks/"):
        return False
    with _lock:
        hooks = _load()
        if url not in hooks:
            hooks.append(url)
            _save(hooks)
    return True


def remove(url: str):
    url = url.strip()
    with _lock:
        hooks = _load()
        if url in hooks:
            hooks.remove(url)
            _save(hooks)
