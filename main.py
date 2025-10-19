from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict
from passlib.context import CryptContext
import jwt
import uvicorn

SECRET_KEY = "CHANGE_THIS_TO_A_RANDOM_SECRET_KEY"
ALGORITHM = "HS256"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# دیتابیس ساده در حافظه (برای تست)
users_db: Dict[str, str] = {}  # username -> hashed_password
messages_db: Dict[str, List[Dict]] = {"work": [], "school": []}  # دو چت روم
clients_db: Dict[str, List[WebSocket]] = {"work": [], "school": []}

# مدل‌ها
class User(BaseModel):
    username: str
    password: str

class Message(BaseModel):
    name: str
    text: str = None
    img: str = None

class WSMessage(BaseModel):
    action: str
    room: str
    message: Message = None
    index: int = None

# JWT helpers
def create_token(username: str):
    return jwt.encode({"username": username}, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("username")
    except:
        return None

# API ثبت نام
@app.post("/register")
def register(user: User):
    if user.username in users_db:
        raise HTTPException(status_code=400, detail="Username already exists")
    hashed = pwd_context.hash(user.password)
    users_db[user.username] = hashed
    token = create_token(user.username)
    return {"token": token, "username": user.username}

# API ورود
@app.post("/login")
def login(user: User):
    if user.username not in users_db or not pwd_context.verify(user.password, users_db[user.username]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user.username)
    return {"token": token, "username": user.username}

# WebSocket
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    token = websocket.query_params.get("token")
    room = websocket.query_params.get("room")
    username = verify_token(token)
    if not username or room not in messages_db:
        await websocket.close(code=1008)
        return

    clients_db[room].append(websocket)

    # ارسال پیام‌ها به کاربر جدید
    await websocket.send_json(messages_db[room])

    try:
        while True:
            data = await websocket.receive_json()
            ws_msg = WSMessage(**data)
            if ws_msg.action == "send":
                messages_db[room].append(ws_msg.message.dict())
            elif ws_msg.action == "edit":
                idx = ws_msg.index
                if 0 <= idx < len(messages_db[room]):
                    # فقط صاحب پیام می‌تواند ویرایش کند
                    if messages_db[room][idx]["name"] == username:
                        messages_db[room][idx] = ws_msg.message.dict()
            elif ws_msg.action == "delete":
                idx = ws_msg.index
                if 0 <= idx < len(messages_db[room]):
                    if messages_db[room][idx]["name"] == username:
                        messages_db[room].pop(idx)
            # ارسال پیام‌ها به همه کلاینت‌ها در اتاق
            for client in clients_db[room]:
                await client.send_json(messages_db[room])
    except WebSocketDisconnect:
        clients_db[room].remove(websocket)

#if __name__ == "__main__":
#    uvicorn.run(app, host="0.0.0.0", port=8000)
