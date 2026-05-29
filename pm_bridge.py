"""Telegram bridge for the Project Manager worker.

Architecture:
- Bridge receives Telegram messages → writes to inbox/ → PM processes → outbox/ → Telegram
- Two threads: Telegram long-poller + outbox watcher
"""

import fcntl
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

load_env(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not BOT_TOKEN or not CHAT_ID:
    print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).parent
DATA_ROOT = ROOT / "data" / "pm"
DATA_WORKERS = ROOT / "data" / "workers"
INBOX = DATA_ROOT / "inbox"
OUTBOX = DATA_ROOT / "outbox_telegram"
OUTBOX_SENT = DATA_ROOT / "outbox_sent"
WORKSPACE = DATA_ROOT / "workspace"
LOG_FILE = DATA_ROOT / "bridge_log.txt"
OFFSET_FILE = DATA_ROOT / "tg_offset.txt"
PID_FILE = DATA_ROOT / "bridge.pid"

for d in (INBOX, OUTBOX, OUTBOX_SENT, WORKSPACE):
    d.mkdir(parents=True, exist_ok=True)

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

import threading
_log_lock = threading.Lock()

def log(msg: str, thread: str = "main") -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}][{thread}] {msg}\n"
    with _log_lock:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    print(line, end="", file=sys.stderr)


def tg_get(method: str, params: dict | None = None, timeout: int = 30):
    try:
        r = requests.get(f"{TELEGRAM_API}/{method}", params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"tg_get {method} error: {e}")
        return None

def tg_post(method: str, data: dict, timeout: int = 30):
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=data, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"tg_post {method} error: {e}")
        return None

def send_message(text: str, reply_to: int | None = None, parse_mode: str = "Markdown") -> None:
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        data: dict = {"chat_id": CHAT_ID, "text": chunk, "parse_mode": parse_mode}
        if reply_to and i == 0:
            data["reply_to_message_id"] = reply_to
        result = tg_post("sendMessage", data)
        if not result or not result.get("ok"):
            data.pop("parse_mode", None)
            tg_post("sendMessage", data)


def load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip())
        except ValueError:
            pass
    return -1

def save_offset(offset: int) -> None:
    OFFSET_FILE.write_text(str(offset))


def submit_to_pm(task_text: str, reply_to_message_id: int | None = None) -> str:
    task_id = uuid.uuid4().hex[:16]
    payload = {
        "task_id": task_id,
        "task": task_text,
        "from": "telegram_bridge",
        "reply_to_message_id": reply_to_message_id,
        "submitted_at": datetime.now().isoformat(timespec="seconds"),
    }
    (INBOX / f"{task_id}.json").write_text(json.dumps(payload, indent=2))
    log(f"submitted to PM: {task_id}")
    return task_id


def _download_tg_file(file_id: str, fallback_ext: str = ".bin") -> Path | None:
    resp = tg_get("getFile", {"file_id": file_id})
    if not resp or not resp.get("ok"):
        return None
    file_path = resp["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        ext = Path(file_path).suffix or fallback_ext
        dest = WORKSPACE / "incoming" / f"{file_id}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
    except Exception as e:
        log(f"file download error ({file_id}): {e}")
        return None


def telegram_poller_thread() -> None:
    offset = load_offset()
    if offset == -1:
        resp = tg_get("getUpdates", {"offset": -1, "timeout": 0}, timeout=10)
        if resp and resp.get("result"):
            offset = resp["result"][-1]["update_id"] + 1
            save_offset(offset)
            log(f"First run: skipping past update_id={offset - 1}", "tg_poll")
        else:
            offset = 0

    log(f"Telegram poller started (offset={offset})", "tg_poll")
    while True:
        try:
            resp = tg_get("getUpdates", {"offset": offset, "timeout": 25}, timeout=30)
            if not resp or not resp.get("ok"):
                time.sleep(5)
                continue

            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                save_offset(offset)

                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue
                if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
                    continue

                text = (msg.get("text") or msg.get("caption") or "").strip()
                msg_id = msg.get("message_id")

                # Handle photos
                if msg.get("photo"):
                    saved = _download_tg_file(msg["photo"][-1]["file_id"], ".jpg")
                    if saved:
                        task = f"The user sent an image at {saved}. Describe it and help if needed."
                        if text:
                            task += f' Caption: "{text}"'
                        submit_to_pm(task, reply_to_message_id=msg_id)
                    else:
                        send_message("Sorry, couldn't download that image.", reply_to=msg_id)
                    continue

                # Handle documents
                if msg.get("document"):
                    doc = msg["document"]
                    filename = doc.get("file_name", "file")
                    saved = _download_tg_file(doc["file_id"], Path(filename).suffix or ".bin")
                    if saved:
                        task = f'The user sent "{filename}" at {saved}. Read it and help if needed.'
                        if text:
                            task += f' Message: "{text}"'
                        submit_to_pm(task, reply_to_message_id=msg_id)
                    else:
                        send_message("Sorry, couldn't download that file.", reply_to=msg_id)
                    continue

                if not text:
                    continue

                log(f"incoming msg_id={msg_id}: {text[:60]}", "tg_poll")

                if text.lower() in ("/status", "/ping"):
                    state_file = DATA_ROOT / "state.json"
                    if state_file.exists():
                        s = json.loads(state_file.read_text())
                        status_text = (
                            f"*Project Manager Status*\n"
                            f"Status: `{s.get('status', '?')}`\n"
                            f"Current task: `{s.get('current_task') or 'idle'}`\n"
                            f"Tasks done: {s.get('tasks_done', 0)}\n"
                            f"Updated: {s.get('updated_at', '?')}"
                        )
                    else:
                        status_text = "PM worker not running."
                    send_message(status_text, reply_to=msg_id)
                    continue

                if text.lower() == "/workers":
                    workers_file = WORKSPACE / "workers.json"
                    if workers_file.exists():
                        try:
                            data = json.loads(workers_file.read_text())
                            workers = data.get("workers", {})
                            if workers:
                                lines = ["*Active Workers*"]
                                for name, info in workers.items():
                                    state_f = DATA_WORKERS / name / "state.json"
                                    status = "unknown"
                                    if state_f.exists():
                                        try:
                                            st = json.loads(state_f.read_text())
                                            status = st.get("status", "unknown")
                                        except Exception:
                                            pass
                                    lines.append(f"• `{name}` [{status}] — {info.get('skills', '')[:60]}")
                                send_message("\n".join(lines), reply_to=msg_id)
                            else:
                                send_message("No workers created yet.", reply_to=msg_id)
                        except Exception:
                            send_message("Could not read workers registry.", reply_to=msg_id)
                    else:
                        send_message("No workers created yet.", reply_to=msg_id)
                    continue

                threading.Thread(
                    target=tg_post,
                    args=("sendChatAction", {"chat_id": CHAT_ID, "action": "typing"}),
                    daemon=True,
                ).start()
                submit_to_pm(text, reply_to_message_id=msg_id)

        except KeyboardInterrupt:
            return
        except Exception as e:
            log(f"poller error: {e}", "tg_poll")
            time.sleep(5)


_RATE_LIMIT_MARKERS = ("429", "rate_limit", "overloaded", "too many requests")

def outbox_watcher_thread() -> None:
    skipped = 0
    for f in OUTBOX.glob("*.json"):
        try:
            f.rename(OUTBOX_SENT / f.name)
            skipped += 1
        except (FileNotFoundError, OSError):
            pass
    log(f"Outbox watcher started (skipped {skipped} existing files)", "outbox")

    while True:
        try:
            for outfile in sorted(OUTBOX.glob("*.json")):
                dest = OUTBOX_SENT / outfile.name
                try:
                    outfile.rename(dest)
                except (FileNotFoundError, OSError):
                    continue

                try:
                    payload = json.loads(dest.read_text())
                except (json.JSONDecodeError, OSError):
                    continue

                task_id = dest.stem
                result = (payload.get("result") or "").strip()
                reply_to = payload.get("reply_to_message_id")
                error = payload.get("error")
                from_cron = payload.get("source") == "cron"

                if error and not result:
                    err_lower = error.lower()
                    if any(x in err_lower for x in _RATE_LIMIT_MARKERS):
                        log(f"outbox {task_id}: rate-limit suppressed", "outbox")
                    else:
                        send_message(f"⚠️ Error: `{error}`", reply_to=reply_to)
                elif result:
                    send_message(result, reply_to=reply_to)
                    log(f"outbox {task_id}: sent", "outbox")
                elif not from_cron:
                    send_message("Couldn't generate a response. Please rephrase.", reply_to=reply_to)

            time.sleep(2)

        except KeyboardInterrupt:
            return
        except Exception as e:
            log(f"outbox watcher error: {e}", "outbox")
            time.sleep(5)


def main() -> None:
    _pid_fd = open(PID_FILE, "w")
    try:
        fcntl.flock(_pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("ERROR: another bridge instance is already running. Exiting.", file=sys.stderr)
        sys.exit(1)
    _pid_fd.write(str(os.getpid()))
    _pid_fd.flush()

    log("Project Manager bridge starting")
    send_message(
        "🤖 *Project Manager online*\n"
        "Give me a project goal and I'll delegate it to specialist workers.\n\n"
        "`/status` — PM status\n"
        "`/workers` — list active workers"
    )

    threads = [
        threading.Thread(target=telegram_poller_thread, name="tg_poll", daemon=True),
        threading.Thread(target=outbox_watcher_thread, name="outbox", daemon=True),
    ]
    for t in threads:
        t.start()

    log("All threads running")
    try:
        while True:
            for t in threads:
                if not t.is_alive():
                    log(f"thread {t.name} died, restarting", "main")
                    nt = threading.Thread(target=t._target, name=t.name, daemon=True)  # type: ignore
                    nt.start()
                    threads[threads.index(t)] = nt
            time.sleep(10)
    except KeyboardInterrupt:
        log("Bridge shutting down")


if __name__ == "__main__":
    main()
