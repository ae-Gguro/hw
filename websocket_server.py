# websocket_server.py
import asyncio
import websockets
import json
import threading

clients = set()
loop = None

async def _handler(websocket):
    clients.add(websocket)
    try:
        async for _ in websocket:
            pass  # 클라이언트 메시지는 무시 (단방향 알림)
    finally:
        clients.remove(websocket)

async def _broadcast(msg: dict):
    if not clients:
        return
    data = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in clients:
        try:
            await ws.send(data)
        except Exception:
            dead.append(ws)
    # 끊어진 클라이언트 정리
    for ws in dead:
        clients.discard(ws)

def start_ws_server(host="0.0.0.0", port=9001):
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    server = websockets.serve(_handler, host, port)
    loop.run_until_complete(server)
    print(f"[WS] WebSocket server started on ws://{host}:{port}")
    loop.run_forever()

def run_ws_in_thread():
    thread = threading.Thread(target=start_ws_server, daemon=True)
    thread.start()

def send_ws_event(event: str, payload: dict = None):
    """
    라즈베리파이에서 상태 변화를 iPad로 push
    """
    global loop
    if not loop:
        return
    msg = {"event": event, "payload": payload or {}}
    # 안전하게 루프에 작업 던지기
    asyncio.run_coroutine_threadsafe(_broadcast(msg), loop)
