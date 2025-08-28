from pi_controller import socketio

socketio: SocketIO | None = None

def create_socketio(app):
    global socketio
    socketio = SocketIO(app, cors_allowed_origins="*")
    return socketio

def notify(event: str, data: dict = None):
    payload = data or {}
    print(f"[SOCKET EMIT] event={event}, data={payload}")  # 🔥 로그 찍기
    if socketio:
        socketio.emit(event, payload)
    else:
        print("[SOCKET EMIT] socketio is None")
	
