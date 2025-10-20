# server.py
"""
FastAPI chat server with:
- SQLite persistence for users and messages
- JWT-based authentication for register/login
- WebSocket endpoint for real-time messaging (rooms: "work" and "school")
- Edit/Delete allowed only to message owner (validated on server)
- Messages include id, room, name, text, img (base64), ts (timestamp)
"""

import sqlite3
from sqlite3 import Connection
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from passlib.context import CryptContext
import jwt
import time
import asyncio
from typing import Dict, List, Optional, Any
import os

# ----------------------------
# Configuration
# ----------------------------
# Use env var if available
SECRET_KEY = os.environ.get("CHAT_SECRET_KEY", "please_change_this_secret_to_a_random_value")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_SECONDS = 60 * 60 * 24 * 7  # 7 days
DB_PATH = os.environ.get("CHAT_DB_PATH", "chat.db")

# ----------------------------
# App & security
# ----------------------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change to specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# clients per room
clients: Dict[str, List[WebSocket]] = {"work": [], "school": []}
clients_lock = asyncio.Lock()

# ----------------------------
# Pydantic models
# ----------------------------
class UserIn(BaseModel):
    username: str
    password: str

class MessageIn(BaseModel):
    name: str
    text: Optional[str] = None
    img: Optional[str] = None

class WSIncoming(BaseModel):
    action: str  # send|edit|delete
    room: Optional[str] = None
    message: Optional[MessageIn] = None
    id: Optional[int] = None
    index: Optional[int] = None
    name: Optional[str] = None

# ----------------------------
# Database helpers (SQLite)
# ----------------------------
def get_db() -> Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        hashed_password TEXT NOT NULL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room TEXT NOT NULL,
        name TEXT NOT NULL,
        text TEXT,
        img TEXT,
        ts INTEGER NOT NULL
    );
    """)
    conn.commit()
    conn.close()

init_db()

# ----------------------------
# Auth helpers (JWT)
# ----------------------------
def create_token(username: str) -> str:
    payload = {
        "username": username,
        "exp": int(time.time()) + ACCESS_TOKEN_EXPIRE_SECONDS
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    # pyjwt returns str in v2+, bytes in older; ensure str
    if isinstance(token, bytes):
        token = token.decode()
    return token

def verify_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("username")
    except Exception:
        return None

# ----------------------------
# DB operations: users & messages
# ----------------------------
def create_user(username: str, password: str) -> None:
    conn = get_db()
    cur = conn.cursor()
    hashed = pwd_ctx.hash(password)
    cur.execute("INSERT INTO users (username, hashed_password) VALUES (?, ?)", (username, hashed))
    conn.commit()
    conn.close()

def verify_user(username: str, password: str) -> bool:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT hashed_password FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    return pwd_ctx.verify(password, row["hashed_password"])

def insert_message(room: str, name: str, text: Optional[str], img: Optional[str]) -> int:
    conn = get_db()
    cur = conn.cursor()
    ts = int(time.time())
    cur.execute("INSERT INTO messages (room, name, text, img, ts) VALUES (?, ?, ?, ?, ?)",
                (room, name, text, img, ts))
    conn.commit()
    last_id = cur.lastrowid
    conn.close()
    return last_id

def list_messages_for_room(room: str) -> List[Dict[str, Any]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, room, name, text, img, ts FROM messages WHERE room = ? ORDER BY id ASC", (room,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_message_by_id(msg_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, room, name, text, img, ts FROM messages WHERE id = ?", (msg_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def update_message(msg_id: int, text: Optional[str], img: Optional[str]) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET text = ?, img = ? WHERE id = ?", (text, img, msg_id))
    conn.commit()
    conn.close()

def delete_message(msg_id: int) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    conn.commit()
    conn.close()

# ----------------------------
# API endpoints
# ----------------------------
@app.post("/register")
async def api_register(user: UserIn):
    if not user.username or not user.password:
        return JSONResponse(status_code=400, content={"error": "username and password required"})
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE username = ?", (user.username,))
    if cur.fetchone():
        conn.close()
        return JSONResponse(status_code=400, content={"error": "username exists"})
    conn.close()
    create_user(user.username, user.password)
    token = create_token(user.username)
    return {"token": token, "username": user.username}

@app.post("/login")
async def api_login(user: UserIn):
    if not user.username or not user.password:
        return JSONResponse(status_code=400, content={"error": "username and password required"})
    ok = verify_user(user.username, user.password)
    if not ok:
        return JSONResponse(status_code=401, content={"error": "invalid credentials"})
    token = create_token(user.username)
    return {"token": token, "username": user.username}

@app.get("/ping")
async def ping():
    return {"ok": True}

# ----------------------------
# WebSocket endpoint
# ----------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # accept first, then validate
    await websocket.accept()
    token = websocket.query_params.get("token")
    room = websocket.query_params.get("room")
    username = verify_token(token) if token else None

    # if token required: allow clients without token? we require token for auth.
    if not username or room not in ("work", "school"):
        # unauthorized
        await websocket.close(code=1008)
        return

    # register client
    async with clients_lock:
        clients[room].append(websocket)

    # send current messages for the room
    try:
        current = list_messages_for_room(room)
        await websocket.send_json(current)

        while True:
            data = await websocket.receive_json()
            # validate incoming structure
            try:
                incoming = WSIncoming(**data)
            except Exception:
                # skip bad messages
                continue

            action = incoming.action
            if action == "send":
                # server trusts username from token, not from client-provided name field
                msg = incoming.message
                name = username
                text = msg.text if msg else None
                img = msg.img if msg else None
                insert_message(room, name, text, img)
                updated = list_messages_for_room(room)
                # broadcast
                async with clients_lock:
                    to_remove = []
                    for c in clients[room]:
                        try:
                            await c.send_json(updated)
                        except Exception:
                            to_remove.append(c)
                    for r in to_remove:
                        if r in clients[room]:
                            clients[room].remove(r)

            elif action == "edit":
                msg_id = incoming.id
                if msg_id is None:
                    continue
                msg_record = get_message_by_id(msg_id)
                if msg_record and msg_record["name"] == username:
                    # perform update
                    new_text = incoming.message.text if incoming.message else None
                    new_img = incoming.message.img if incoming.message else None
                    update_message(msg_id, new_text, new_img)
                    updated = list_messages_for_room(room)
                    async with clients_lock:
                        to_remove = []
                        for c in clients[room]:
                            try:
                                await c.send_json(updated)
                            except Exception:
                                to_remove.append(c)
                        for r in to_remove:
                            if r in clients[room]:
                                clients[room].remove(r)

            elif action == "delete":
                msg_id = incoming.id
                if msg_id is None:
                    continue
                existing = get_message_by_id(msg_id)
                if existing and existing["name"] == username:
                    delete_message(msg_id)
                    updated = list_messages_for_room(room)
                    async with clients_lock:
                        to_remove = []
                        for c in clients[room]:
                            try:
                                await c.send_json(updated)
                            except Exception:
                                to_remove.append(c)
                        for r in to_remove:
                            if r in clients[room]:
                                clients[room].remove(r)

            else:
                # ignore unknown actions
                continue

    except WebSocketDisconnect:
        async with clients_lock:
            if websocket in clients[room]:
                clients[room].remove(websocket)
    except Exception:
        async with clients_lock:
            if websocket in clients[room]:
                clients[room].remove(websocket)
        try:
            await websocket.close()
        except:
            pass

# ----------------------------
# run via: uvicorn server:app --host 0.0.0.0 --port 8000
# ----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="info")
