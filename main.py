# server.py
# usage: python server.py
# requirements: flask pusher flask-cors

import os
import time
import sqlite3
from flask import Flask, request, jsonify
from flask_cors import CORS
import pusher

# ---- Pusher credentials (تو میتونی اینها رو به ENV منتقل کنی) ----
PUSHER_APP_ID = os.environ.get("PUSHER_APP_ID", "2066365")
PUSHER_KEY = os.environ.get("PUSHER_KEY", "92a7377f88ebced2486a")
PUSHER_SECRET = os.environ.get("PUSHER_SECRET", "fe54fbb4527f2dc39d04")
PUSHER_CLUSTER = os.environ.get("PUSHER_CLUSTER", "ap2")

# ---- init pusher client ----
pusher_client = pusher.Pusher(
    app_id=PUSHER_APP_ID,
    key=PUSHER_KEY,
    secret=PUSHER_SECRET,
    cluster=PUSHER_CLUSTER,
    ssl=True
)

# ---- Flask app ----
app = Flask(__name__)
CORS(app)  # برای دسترسی از فرانت محلی

DB_FILE = "chat_pusher.db"

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            text TEXT,
            img TEXT,
            reply_id INTEGER,
            ts INTEGER NOT NULL
        );
    """)
    conn.commit()
    conn.close()

init_db()

def list_messages():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, text, img, reply_id, ts FROM messages ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    msgs = []
    # expand reply info
    for r in rows:
        msg = dict(r)
        if msg['reply_id']:
            # fetch reply target
            conn = get_db()
            c2 = conn.cursor()
            c2.execute("SELECT id, name, text, img FROM messages WHERE id = ?", (msg['reply_id'],))
            rr = c2.fetchone()
            conn.close()
            if rr:
                msg['reply'] = {"id": rr["id"], "name": rr["name"], "text": rr["text"], "img": rr["img"]}
            else:
                msg['reply'] = None
        else:
            msg['reply'] = None
        msgs.append(msg)
    return msgs

def broadcast_messages():
    msgs = list_messages()
    # ارسال یک رویداد کلی که کل لیست پیام‌ها را می‌فرستد
    pusher_client.trigger('chat-room', 'messages-updated', {'messages': msgs})

@app.route("/api/messages", methods=["GET"])
def api_get_messages():
    return jsonify({"messages": list_messages()})

@app.route("/api/messages", methods=["POST"])
def api_send_message():
    data = request.get_json() or {}
    name = data.get("name")
    text = data.get("text")
    img = data.get("img")
    reply_id = data.get("reply_id")
    if not name:
        return jsonify({"error": "name required"}), 400
    ts = int(time.time())
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (name, text, img, reply_id, ts) VALUES (?, ?, ?, ?, ?)",
                (name, text, img, reply_id, ts))
    conn.commit()
    conn.close()
    broadcast_messages()
    return jsonify({"ok": True}), 201

@app.route("/api/messages/<int:msg_id>", methods=["PUT"])
def api_edit_message(msg_id):
    data = request.get_json() or {}
    new_text = data.get("text")
    new_img = data.get("img")
    # فقط به‌روزرسانی متن و تصویر
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET text = ?, img = ? WHERE id = ?", (new_text, new_img, msg_id))
    conn.commit()
    conn.close()
    broadcast_messages()
    return jsonify({"ok": True})

@app.route("/api/messages/<int:msg_id>", methods=["DELETE"])
def api_delete_message(msg_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    conn.commit()
    conn.close()
    broadcast_messages()
    return jsonify({"ok": True})

if __name__ == "__main__":
    # برای اجرا در لوکال
    app.run(host="0.0.0.0", port=5000, debug=True)
