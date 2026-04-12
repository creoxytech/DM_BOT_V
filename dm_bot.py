"""
Instagram DM Auto-Reply Bot
Uses instagrapi + Ollama Cloud API
"""

import time
import os
import json
import requests
from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ChallengeRequired

load_dotenv()

# Patch instagrapi: make video_url optional on ALL types that have it
import instagrapi.types as _ig_types
for _name in dir(_ig_types):
    _model = getattr(_ig_types, _name)
    try:
        if hasattr(_model, "model_fields") and "video_url" in _model.model_fields:
            _model.model_fields["video_url"].default = None
            _model.model_fields["video_url"].is_required = lambda: False
            _model.model_rebuild(force=True)
    except Exception:
        pass

# Patch direct_threads to skip threads that fail parsing
_orig_direct_threads = Client.direct_threads
def _safe_direct_threads(self, amount=20, selected_filter="", thread_message_limit=10):
    try:
        return _orig_direct_threads(self, amount=amount, selected_filter=selected_filter, thread_message_limit=thread_message_limit)
    except Exception:
        results = []
        for i in range(min(amount, 20)):
            try:
                batch = _orig_direct_threads(self, amount=1, selected_filter=selected_filter, thread_message_limit=1)
                results.extend(batch)
            except Exception:
                pass
        return results
Client.direct_threads = _safe_direct_threads

# ── CONFIG ──────────────────────────────────────────────────────────────────
INSTAGRAM_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "yash__niwane")
INSTAGRAM_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD", "")
INSTAGRAM_SESSION  = os.environ.get("INSTAGRAM_SESSION", "")
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL       = "gemma-3-4b-it"
CHECK_INTERVAL     = 5
MAX_HISTORY        = 6   # messages to include for context

# Load prompt and senders
with open("prompt.txt") as f:
    YOUR_STYLE = f.read()

with open("senders.json") as f:
    SENDERS = json.load(f)
ALLOWED_FRIENDS = list(SENDERS.keys())

# Build system turns once at startup (Gemma doesn't support system_instruction)
_SYSTEM_TURNS = [{"role": "user", "parts": [{"text": YOUR_STYLE}]},
                 {"role": "model", "parts": [{"text": "ok"}]}]

# ── GEMINI ───────────────────────────────────────────────────────────────────
def generate_reply(conversation_history: list, sender_username: str) -> str:
    sender_info = SENDERS.get(sender_username, {})
    sender_name = sender_info.get("name", sender_username)
    sender_context = sender_info.get("context", "")

    history = _SYSTEM_TURNS.copy()
    if sender_context:
        history += [{"role": "user", "parts": [{"text": f"Friend: {sender_name}. {sender_context}"}]},
                    {"role": "model", "parts": [{"text": "ok"}]}]
    history += [{"role": "user" if msg["sender"] != "Yash" else "model",
                 "parts": [{"text": msg["text"]}]} for msg in conversation_history[-MAX_HISTORY:]]
    if not history or history[-1]["role"] != "user":
        history.append({"role": "user", "parts": [{"text": "reply"}]})

    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": history,
                "generationConfig": {"maxOutputTokens": 80, "temperature": 0.8}
            },
            timeout=60
        )
        response.raise_for_status()
        text = response.json()["candidates"][0]["content"]["parts"]
        # filter out thinking parts, only keep text output
        output = " ".join(p["text"] for p in text if p.get("thought") is not True).strip()
        return output
    except Exception as e:
        print(f"❌ Gemini error: {e}")
        return None

# ── INSTAGRAM HELPERS ────────────────────────────────────────────────────────
def get_reel_context(cl, msg) -> str:
    """Extract caption from a shared reel/clip message."""
    try:
        if msg.item_type in ("clip", "felix_share", "reel_share"):
            media = getattr(msg, "clip", None) or getattr(msg, "reel_share", None)
            if media:
                caption = getattr(getattr(media, "media", media), "caption_text", "") or ""
                return f"[shared a reel: {caption.strip()[:100] if caption else 'no caption'}]"
    except Exception:
        pass
    return ""

def get_conversation_history(cl, thread_id, sender_username):
    messages = cl.direct_messages(thread_id, amount=MAX_HISTORY)
    history = []
    for msg in reversed(messages):
        sender = "Yash" if str(msg.user_id) == str(cl.user_id) else sender_username
        if msg.item_type == "text":
            history.append({"sender": sender, "text": msg.text})
        else:
            reel_ctx = get_reel_context(cl, msg)
            if reel_ctx:
                history.append({"sender": sender, "text": reel_ctx})
    return history

def should_reply(thread_id, last_msg_id, replied_threads):
    return replied_threads.get(thread_id) != last_msg_id

# ── LOGIN ────────────────────────────────────────────────────────────────────
def login(cl):
    if INSTAGRAM_SESSION:
        print("🔐 Loading session from env secret...")
        cl.set_settings(json.loads(INSTAGRAM_SESSION))
        try:
            cl.get_timeline_feed()
            print("✅ Session valid")
        except LoginRequired:
            cl.relogin()
            print("✅ Relogin successful")

    elif os.path.exists("session.json"):
        print("🔐 Loading session.json...")
        cl.load_settings("session.json")
        try:
            cl.get_timeline_feed()
            print("✅ Session valid")
        except LoginRequired:
            cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            cl.dump_settings("session.json")
            print("✅ Logged in, session.json updated")

    else:
        print("🔐 Fresh login...")
        try:
            cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            cl.dump_settings("session.json")
            print("✅ Logged in! session.json saved")
        except ChallengeRequired:
            print("❌ Instagram requires verification — approve in the app, then retry.")
            raise

# ── MAIN BOT ─────────────────────────────────────────────────────────────────
def run_bot(single_pass=False):
    print("🤖 Instagram DM Bot starting...")
    cl = Client()
    replied_threads = {}

    login(cl)
    print(f"👤 Account : {INSTAGRAM_USERNAME}")
    print(f"🧠 Model   : {GEMINI_MODEL}")

    while True:
        try:
            print(f"\n🔍 Checking DMs... ({time.strftime('%H:%M:%S')})")
            try:
                threads = cl.direct_threads(amount=20)
            except Exception as e:
                print(f"  ⚠️  direct_threads failed ({e}), retrying with amount=5...")
                try:
                    threads = cl.direct_threads(amount=5)
                except Exception as e2:
                    print(f"  ❌ Could not fetch threads: {e2}")
                    threads = []

            for thread in threads:
                try:
                    others = [u for u in thread.users if str(u.pk) != str(cl.user_id)]
                    if not others or not thread.messages:
                        continue

                    sender_username = others[0].username

                    if ALLOWED_FRIENDS and sender_username not in ALLOWED_FRIENDS:
                        continue

                    last_msg = thread.messages[0]
                    if str(last_msg.user_id) == str(cl.user_id):
                        continue
                    is_text = last_msg.item_type == "text"
                    is_reel = last_msg.item_type in ("clip", "felix_share", "reel_share")
                    if not is_text and not is_reel:
                        continue

                    thread_id = str(thread.id)
                    last_msg_id = str(last_msg.id)
                    if not should_reply(thread_id, last_msg_id, replied_threads):
                        continue

                    print(f"  💬 {sender_username}: \"{last_msg.text}\"")
                    history = get_conversation_history(cl, thread_id, sender_username)
                    reply = generate_reply(history, sender_username)

                    # Mark this exact message as replied
                    replied_threads[thread_id] = last_msg_id

                    if not reply:
                        continue

                    print(f"  ✍️  Reply: \"{reply}\"")
                    cl.direct_answer(thread_id, reply)
                    print(f"  ✅ Sent to {sender_username}")

                except Exception as thread_err:
                    print(f"  ⚠️  Skipping thread (parse error): {thread_err}")

        except LoginRequired:
            print("⚠️  Session expired, relogging...")
            try:
                cl.relogin()
                print("✅ Relogin successful")
            except Exception as e:
                print(f"❌ Relogin failed: {e}")
                break

        except Exception as e:
            print(f"❌ Error: {e}")

        if single_pass:
            print("✅ Single pass done.")
            break

        print(f"😴 Sleeping {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run_bot(single_pass=False)
