#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
from flask import Flask, request, jsonify
from threading import Thread, Event
import os, traceback, logging, requests, importlib, types, re
from dotenv import load_dotenv
#from ws_event import create_socketio, notify
from session_store import current_mode, current_session, current_roles
from clova_roleplay import parse_roles_basic, call_start, call_talk, call_end
from flask_socketio import SocketIO

app = Flask(__name__)

stop_event = Event()
worker_thread = None

socketio: SocketIO | None = None
# ===== SocketIO 초기화 =====
def create_socketio(app):
    socketio = SocketIO(
        app,
        cors_allowed_origins="*",   # 모든 클라이언트 허용
        async_mode="eventlet",
        logger=True,                 # eventlet 기반 실행\
        engineio_logger=True,
        ping_interval=25,           # 클라 기본값과 맞춤 (25초)
        ping_timeout=60             # 클라 기본값과 맞춤 (20초)
    )
    return socketio

socketio = create_socketio(app)

def notify(event: str, data: dict = None):
    payload = data or {}
    print(f"[SOCKET EMIT] event={event}, data={payload}")
    if socketio:
        socketio.emit(event, payload)


# ===== 로깅/배너 =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app.logger.setLevel(logging.INFO)
VERSION = "pi_controller v2025-08-27"
print(f">>> {VERSION} :: __file__={__file__} :: cwd={os.getcwd()}")
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env")) 

# ===== 백엔드 설정 =====
BACKEND_BASE = os.getenv("BACKEND_BASE") or os.getenv("SERVER_URL") or "http://127.0.0.1:8080"
BACKEND_TIMEOUT = int(os.getenv("BACKEND_TIMEOUT", "30"))

# ===== 상태 =====
worker_thread: Thread | None = None
stop_event = Event()
volume_percent = 60

# ===== TTS/STT 자동 감지 =====
_TTS_FUNC = None
_STT_FUNC = None
_TTS_SRC = None
_STT_SRC = None

# pi_controller.py 상단
PROFILE_ID = None  # 글로벌 변수, 로그인/조회 시 세팅됨

@app.route("/")
def hello():
    return "SocketIO Server Running"


@socketio.on("connect")
def on_connect():
    app.logger.info("[SOCKET] client connected")

@socketio.on('disconnect')
def on_disconnect():
    from flask import request
    app.logger.info(f"[SOCKET] disconnected sid={request.sid}")

 
def set_profile_id(pid: int):
    global PROFILE_ID
    PROFILE_ID = pid
    print(f"[PROFILE] profile_id set = {PROFILE_ID}")

def get_profile_id() -> int:
    global PROFILE_ID
    if PROFILE_ID:
        return PROFILE_ID
    # fallback (.env 기본값)
    return int(os.getenv("PROFILE_ID", "1"))

def clean_role(role: str) -> str:
    if not role: 
        return role
    role = re.sub(r"(하고싶어|하고 싶어|할래|할께|할게|입니다|이다|예요|이요|야)$", "", role)
    role = role.strip()

    return role

def _import_attr(module_name: str, cand_names: list[str]):
    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return None, None
    for name in cand_names:
        if hasattr(mod, name) and isinstance(getattr(mod, name), (types.FunctionType, types.BuiltinFunctionType)):
            return getattr(mod, name), f"{module_name}.{name}"
    return None, None

def _resolve_tts_stt():
    global _TTS_FUNC, _STT_FUNC, _TTS_SRC, _STT_SRC
    if _TTS_FUNC and _STT_FUNC:
        return

    tts_candidates = [
        ("clova_conversation", ["tts_say", "speak", "tts", "say", "play_tts"]),
        ("clova_tts",         ["speak", "tts_say", "say", "tts"]),
    ]
    stt_candidates = [
        ("clova_conversation", ["stt_once", "listen_once", "stt", "record_and_transcribe", "asr_once"]),
        ("clova_stt",          ["stt_once", "listen_once", "stt"]),
    ]

    for mod, names in tts_candidates:
        func, src = _import_attr(mod, names)
        if func:
            _TTS_FUNC, _TTS_SRC = func, src
            break

    for mod, names in stt_candidates:
        func, src = _import_attr(mod, names)
        if func:
            _STT_FUNC, _STT_SRC = func, src
            break

    if not _TTS_FUNC or not _STT_FUNC:
        tried = {
            "tts_tried": [f"{m}.{n}" for m, ns in tts_candidates for n in ns],
            "stt_tried": [f"{m}.{n}" for m, ns in stt_candidates for n in ns],
        }
        raise RuntimeError("TTS/STT 함수 탐색 실패. 후보 중 하나씩 제공해 주세요.\n" + str(tried))
    app.logger.info(f"[audio] TTS={_TTS_SRC}, STT={_STT_SRC}")

def tts_say(text: str) -> None:
    _resolve_tts_stt()
    try:
        _TTS_FUNC(text)
    except TypeError:
        _TTS_FUNC(text=text)

def stt_once(seconds: float = 6.0) -> str:
    _resolve_tts_stt()
    try:
        return _STT_FUNC(seconds=seconds)
    except TypeError:
        try:
            return _STT_FUNC(timeout=seconds)
        except TypeError:
            return _STT_FUNC()

# ===== 백엔드 API =====
def _auth_headers() -> dict:
    h = {"Content-Type": "application/json"}
    token = os.getenv("ACCESS_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ---- Animal_Quiz
def backend_animal_quiz_talk(session_id: str, profile_id: int, user_input: str, animal_name: str | None = None) -> dict:
    url = f"{BACKEND_BASE}/api/animal-quiz/talk"
    payload = {
        "user_input": user_input,
        "session_id": session_id,
        "profile_id": profile_id
    }
    if animal_name:
        payload["animal_name"] = animal_name   # 처음 시작일 때만 포함
    try:
        r = requests.post(url, json=payload, headers=_auth_headers(), timeout=BACKEND_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        app.logger.error(f"[backend_animal_quiz_talk] 실패: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": "동물 퀴즈 응답 실패"}

# ---- Animal_Quiz
def backend_quiz_talk(session_id: str, profile_id: int, user_input: str, topic: str | None = None) -> dict:
    url = f"{BACKEND_BASE}/api/quiz/talk"
    payload = {
        "user_input": user_input,
        "session_id": session_id,
        "profile_id": profile_id
    }
    if topic:
        payload["topic"] = topic   # 처음 시작일 때만 포함
def start_worker(target, *args):
    global worker_thread
    stop_event.clear()
    worker_thread = Thread(target=target, args=args, daemon=True)
    worker_thread.start()
    print(f"[worker] starting {target.__name__}{args}")

def stop_worker() -> None:
    global worker_thread, current_mode
    stop_event.set()
    if worker_thread and worker_thread.is_alive():
        worker_thread.join(timeout=1.0)
    current_mode = None
    print("[worker] stopped")   
    try:
        r = requests.post(url, json=payload, headers=_auth_headers(), timeout=BACKEND_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        app.logger.error(f"[backend_quiz_talk] 실패: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": "퀴즈 응답 실패"}



# ---- Chosung
def backend_chosung_talk(session_id: str, profile_id: int, user_input: str) -> dict:
    url = f"{BACKEND_BASE}/api/chosung/talk"
    payload = {
        "user_input": user_input,
        "session_id": session_id,
        "profile_id": profile_id,
    }
    try:
        r = requests.post(url, json=payload, headers=_auth_headers(), timeout=BACKEND_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        app.logger.error(f"[backend_chosung_talk] 실패: {e}\n{traceback.format_exc()}")
        return {"status": "error", "message": "퀴즈 응답 실패"}


# ---- Roleplay
def backend_roleplay_start(session_id: str, user_role: str, bot_role: str) -> dict:
    url = f"{BACKEND_BASE}/api/roleplay/start"
    payload = {
        "session_id": session_id,
        "profile_id": get_profile_id(),
        "user_role": user_role,
        "bot_role": bot_role,
    }
    try:
        r = requests.post(url, json=payload, headers=_auth_headers(), timeout=BACKEND_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        app.logger.error(f"[backend_roleplay_start] 실패: {e}\n{traceback.format_exc()}")
        return {"status": "error", "response": "역할놀이 시작 중 문제가 생겼어요."}


def backend_roleplay_talk(chatroom_id: int, session_id: str, user_input: str) -> dict:
    url = f"{BACKEND_BASE}/api/roleplay/{chatroom_id}/talk"
    payload = {
        "user_input": user_input,
        "session_id": session_id,
        "profile_id": get_profile_id(),
    }
    try:
        r = requests.post(url, json=payload, headers=_auth_headers(), timeout=BACKEND_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        app.logger.error(f"[backend_roleplay_talk] 실패: {e}\n{traceback.format_exc()}")
        return {"status": "error", "response": "꾸로가 응답하지 못했어요."}

# ---- Conversation (신규: AI 응답)

def backend_conversation_talk(session_id: str, utterance: str, profile_id: int, access_token: str) -> dict:
    url = f"{BACKEND_BASE}/api/conversation/talk"
    payload = {"user_input": utterance, "session_id": session_id, "profile_id": profile_id}
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.post(url, json=payload, headers=headers, timeout=BACKEND_TIMEOUT)
    r.raise_for_status()
    return r.json()

def backend_conversation_end(session_id: str, access_token: str) -> None:
    url = f"{BACKEND_BASE}/api/conversation/end"
    payload = {"session_id": session_id}
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=BACKEND_TIMEOUT)
        if r.status_code >= 400:
            app.logger.warning(f"/api/conversation/end 실패 status={r.status_code} body={r.text}")
    except Exception as e:
        app.logger.warning(f"/api/conversation/end 예외: {e}")

# ===== 워커 =====
import clova_roleplay as rp
import traceback

def roleplay_loop(session_id: str, profile_id: int, chatroom_id: int | None = None):
    """
    역할놀이 루프
    - chatroom_id가 없으면: STT로 역할(user_role, bot_role) 수집 후 /api/roleplay/start 호출
    - chatroom_id가 있으면: 바로 talk 시작
    """
    app.logger.info(f"[roleplay_loop] start session_id={session_id}, chatroom_id={chatroom_id}")

    try:
        # (A) chatroom_id가 없으면 역할부터 수집
        if not chatroom_id:
            rp.say("역할놀이를 시작하자! 예: 나는 아기고 꾸로는 엄마야. 이렇게 말해줘!")
            notify("ask_roles")

            while True:
                notify("listening")  # 🎤 아이 말 차례
                user_text = rp.stt_once()
                if not user_text:
                    continue
                
                user_text = normalize_gguro(user_text)

                app.logger.info(f"[STT 결과] {user_text}")
                notify("user_text", {"text": user_text})

                # 역할 파싱
                ur, br = rp.parse_roles_basic(user_text)
                if ur and br:
                    current_roles["user_role"] = ur
                    current_roles["bot_role"] = br
                    current_roles["profile_id"] = profile_id
                    notify("confirm_roles", {"user_role": ur, "bot_role": br})

                    # 백엔드 start 호출
                    try:
                        res = rp.call_start(ur, br, session_id)
                        chatroom_id = res.get("chatroom_id")
                        current_session["chatroom_id"] = chatroom_id
                        reply = res.get("response", "역할놀이가 시작되었어!")
                        rp.say(reply)
                        notify("reply", {"text": reply})
                    except Exception as e:
                        app.logger.error(f"[START ERROR] {e}\n{traceback.format_exc()}")
                        rp.say("역할놀이를 시작할 수 없어. 다시 시도해줄래?")
                        notify("error", {"message": "start_failed"})
                        return
                    break
                else:
                    rp.say("조금 더 또렷하게 말해줘! 예: 나는 학생이고 꾸로는 선생님이야.")
                    notify("error", {"message": "role_parse_failed"})

        else:
            # (B) chatroom_id가 이미 있으면 바로 시작
            rp.say("역할놀이 준비 완료! 시작해보자!")
            notify("ready")

        # ===== 메인 대화 루프 =====
        while True:
            notify("listening")
            user_text = rp.stt_once()
            if not user_text:
                continue

            app.logger.info(f"[STT 결과] {user_text}")
            notify("user_text", {"text": user_text})

            # 종료 키워드
            if rp.STOP_KEYWORD in user_text:
                rp.say("오늘 역할놀이 즐거웠어! 정리하고 마칠게!")
                notify("ended")
                try:
                    rp.call_end(session_id)
                except Exception as e:
                    app.logger.error(f"[END ERROR] {e}")
                break

            try:
                notify("thinking")
                srv = rp.call_talk(chatroom_id, user_text, session_id)
                app.logger.info(f"[TALK RESPONSE RAW] {srv}")

                reply = srv.get("response")
                status = srv.get("status", "continue")

                if reply:
                    rp.say(reply)
                    notify("reply", {"text": reply})

                if status == "end":
                    rp.say("여기까지 할게! 고마워!")
                    notify("ended")
                    try:
                        rp.call_end(session_id)
                    except Exception as e:
                        app.logger.error(f"[END ERROR] {e}")
                    break

            except Exception as e:
                app.logger.error(f"[TALK ERROR] {e}\n{traceback.format_exc()}")
                rp.say("지금은 연결이 불안정해요. 잠시 후 다시 해보자!")
                notify("error", {"message": "talk_failed"})

    except KeyboardInterrupt:
        app.logger.info("[roleplay_loop] interrupted by user")
    except Exception as e:
        app.logger.error(f"[roleplay_loop] error: {e}\n{traceback.format_exc()}")
        notify("error", {"message": str(e)})
    finally:
        app.logger.info("[roleplay_loop] stop")

def conversation_loop(session_id: str, profile_id: int, access_token: str):
    try:
        # 1. 첫 안내 멘트
        msg = "일상 대화를 시작할게요. 언제든지 '그만'이라고 말하면 종료할 수 있어요."
        notify("ready", {"text": msg})
        tts_say(msg)

        # 2. 안내 멘트 끝 → 사용자 발화 대기
        notify("listening", {"text": msg})

        while True:
            # 🎤 사용자 발화
            user_text = stt_once().strip()
            if not user_text:
                continue

            notify("user_input", {"text": user_text})

            # 종료 처리
            if any(k in user_text for k in ["그만", "끝내", "종료", "stop", "quit"]):
                end_msg = "대화를 종료할게요."
                tts_say(end_msg)
                notify("ended", {"text": end_msg})
                backend_conversation_end(session_id, access_token)
                break

            try:
                # 3. 응답 대기 (thinking 뷰)
                notify("thinking")

                # 4. 꾸로 답변 생성
                reply = backend_conversation_talk(session_id, user_text, profile_id, access_token)
                resp_text = reply.get("response") or reply.get("reply") or ""

                if resp_text:
                    tts_say(resp_text)

                    # 응답 끝나면 listening 뷰로
                    notify("listening", {"text": resp_text})

            except Exception as e:
                err_msg = "죄송해요. 잠시 통신 문제가 있었어요."
                tts_say(err_msg)
                notify("error", {"message": str(e), "text": err_msg})
                continue

    finally:
        app.logger.info("[conversation_loop] stop")


# ===== 초성 루프 =====
def quiz_loop(session_id: str, profile_id: int):
    app.logger.info(f"[quiz_loop] start session_id={session_id}, profile_id={profile_id}")
    try:
        # (1) 첫 문제 요청 (빈 입력)
        res = backend_chosung_talk(session_id, profile_id, "")
        msg = res.get("message", "")
        if msg:
            tts_say(msg)

        # (2) 루프 돌면서 유저 답변 듣기
        while not stop_event.is_set():
            user_text = stt_once().strip()
            if not user_text:
                continue

            # 종료 키워드 처리
            if any(k in user_text for k in ["그만", "끝내", "종료", "퀴즈 종료", "stop"]):
                tts_say("퀴즈를 종료할게요.")
                break

            res = backend_chosung_talk(session_id, profile_id, user_text)
            status = res.get("status")
            msg = res.get("message", "")

            if msg:
                tts_say(msg)

            if status == "end":
                break

    except Exception as e:
        app.logger.error(f"[quiz_loop] error: {e}\n{traceback.format_exc()}")
        tts_say("퀴즈 중 오류가 발생했어요.")
    finally:
        app.logger.info("[quiz_loop] stop")

# ===== 바른생활 퀴즈 루프 =====
def safety_quiz_loop(session_id: str, profile_id: int, topic: str):
    app.logger.info(f"[safety_quiz_loop] start session_id={session_id}, profile_id={profile_id}, topic={topic}")
    try:
        # (1) 첫 요청
        res = backend_quiz_talk(session_id, profile_id, "", topic)
        msg = res.get("message", "")
        if msg:
            tts_say(msg)

        # (2) 반복
        while not stop_event.is_set():
            user_text = stt_once().strip()
            if not user_text:
                tts_say("잘 못 들었어. 다시 한 번 말해줄래?")
                continue

            # 종료 키워드
            if any(k in user_text for k in ["그만", "끝내", "종료", "퀴즈 종료", "stop"]):
                tts_say("퀴즈를 종료할게요.")
                break

            res = backend_quiz_talk(session_id, profile_id, user_text)
            status = res.get("status")
            msg = res.get("message", "")

            if msg:
                tts_say(msg)

            if status == "end":
                break

    except Exception as e:
        app.logger.error(f"[safety_quiz_loop] error: {e}\n{traceback.format_exc()}")
        tts_say("퀴즈 중 오류가 발생했어요.")
    finally:
        app.logger.info("[safety_quiz_loop] stop")


# ===== 동물 퀴즈 루프 =====
def animal_quiz_loop(session_id: str, profile_id: int, animal_name: str):
    app.logger.info(f"[animal_quiz_loop] start session_id={session_id}, profile_id={profile_id}, animal={animal_name}")
    try:
        # (1) 첫 요청
        res = backend_animal_quiz_talk(session_id, profile_id, "", animal_name)
        msg = res.get("message", "")
        if msg:
            tts_say(msg)

        # (2) 반복
        while not stop_event.is_set():
            user_text = stt_once().strip()
            if not user_text:
                tts_say("잘 못 들었어. 다시 한 번 말해줄래?")
                continue

            # 종료 키워드
            if any(k in user_text for k in ["그만", "끝내", "종료", "퀴즈 종료", "stop"]):
                tts_say("동물 퀴즈를 종료할게요.")
                break

            res = backend_animal_quiz_talk(session_id, profile_id, user_text)
            status = res.get("status")
            msg = res.get("message", "")

            if msg:
                tts_say(msg)

            if status == "end":
                break

    except Exception as e:
        app.logger.error(f"[animal_quiz_loop] error: {e}\n{traceback.format_exc()}")
        tts_say("동물 퀴즈 중 오류가 발생했어요.")
    finally:
        app.logger.info("[animal_quiz_loop] stop")



def start_worker(target, *args) -> None:
    global worker_thread
    stop_event.clear()
    worker_thread = Thread(target=target, args=args, daemon=True)
    worker_thread.start()
    print(f"[worker] starting {target.__name__}{args}")

def stop_worker() -> None:
    global worker_thread, current_mode
    stop_event.set()
    if worker_thread and worker_thread.is_alive():
        worker_thread.join(timeout=1.0)
    current_mode = None
    print("[worker] stopped")

def ask_and_confirm_roles() -> tuple[str, str]:
    global current_roles

    # 안내 멘트
    tts_say("역할놀이를 시작하자! 예: 나는 엄마고, 꾸로는 아이야. 이렇게 말해줘!")
    notify("ask_roles")   # 📺 UI: 역할 지정 요청

    user_role, bot_role = None, None
    while not (user_role and bot_role):
        notify("listening")  # 🎤 아이 말하는 중
        text = stt_once().strip()
        if not text:
            tts_say("잘 못 들었어. 다시 한 번 말해줄래?")
            notify("error", {"message": "no_input"})
            continue

        # 꾸로 보정
        text = normalize_gguro(text)

        ur, br = parse_roles_basic(text)
        if ur and br:
            user_role, bot_role = ur, br
            notify("confirm_roles", {"user_role": user_role, "bot_role": bot_role})
            break
        else:
            tts_say("조금 더 또렷하게 말해줘! 예: 나는 학생이고 꾸로는 선생님이야.")
            notify("error", {"message": "role_parse_failed"})
            continue

    # 역할 확인
    tts_say(f"네 역할은 {user_role}, 꾸로의 역할은 {bot_role} 맞아? 맞으면 응!, 아니면 아니야라고 말해줘.")
    notify("confirm_roles", {"user_role": user_role, "bot_role": bot_role})

    confirm = stt_once().strip()

    positives = [
        "네", "네.", "네에", "네에에", "예", "예스",
        "응", "응.", "응응", "음", "으응", "웅", "웅웅",
        "맞아", "맞아요", "맞습니다", "맞음",
        "그래", "그래요", "그럼", "그렇지",
        "좋아", "좋습니다", "좋죠",
        "옳소", "옳습니다",
    ]
    negatives = ["아니", "아니야", "아냐", "싫어"]

    if confirm:
        if any(p in confirm for p in positives):
            notify("roles_confirmed", {"user_role": user_role, "bot_role": bot_role})

            # ✅ 전역 저장
            current_roles["user_role"] = user_role
            current_roles["bot_role"] = bot_role

            # ✅ 백엔드 알리기
            notify_backend_roleplay_start()

            return user_role, bot_role

        if any(n in confirm for n in negatives):
            notify("roles_rejected")
            tts_say("그럼 다시 설정할게!")
            return ask_and_confirm_roles()
    else:
        # 짧아서 인식 안 된 경우 → 긍정으로 간주
        notify("roles_auto_confirmed", {"user_role": user_role, "bot_role": bot_role})

        current_roles["user_role"] = user_role
        current_roles["bot_role"] = bot_role
        notify_backend_roleplay_start()

        return user_role, bot_role

    # 애매하면 다시 시도
    tts_say("잘 못 들었어. 다시 말해줄래?")
    notify("error", {"message": "confirm_failed"})
    return ask_and_confirm_roles()

# ===== 라우트 =====
@app.route("/start/roleplay", methods=["POST"])
def http_start_roleplay():
    body = request.get_json(silent=True) or {}
    profile_id = int(body.get("profile_id") or 0)
    session_id = (body.get("session_id") or "").strip()
    chatroom_id = int(body.get("chatroom_id") or 0)

    # ✅ 토큰: 헤더 우선
    auth_header = request.headers.get("Authorization", "")
    access_token = None
    if auth_header.startswith("Bearer "):
        access_token = auth_header.split(" ", 1)[1]
        print(f"[DEBUG] 받은 access_token={access_token}")
    else:
        access_token = body.get("access_token")
        print(f"[DEBUG] 받은 access_token={access_token}")

    if not access_token:
        return jsonify({"ok": False, "error": "access_token is required"}), 401
    if profile_id <= 0:
        return jsonify({"ok": False, "error": "profile_id is required"}), 400
    if not session_id:
        session_id = f"{profile_id}_역할놀이"

    # 세션 저장
    current_session.clear()
    current_session.update({
        "session_id": session_id,
        "chatroom_id": 0,   # 아직 없음
        "access_token": access_token,
    })

    current_roles.clear()
    current_roles.update({
        "user_role": None,
        "bot_role": None,
        "profile_id": profile_id,
    })

    # 워커 실행 (STT/TTS 루프 → 역할 수집)
    stop_worker()
    start_worker(roleplay_loop, session_id, profile_id, chatroom_id)

    return jsonify({
        "ok": True,
        "mode": "roleplay",
        "session_id": session_id,
        "chatroom_id": 0,
        "profile_id": profile_id,
        "message": "세션 시작됨. 역할 수집 후 /confirm/roles 호출 필요"
    }), 202


@app.route("/confirm/roles", methods=["POST"])
def http_confirm_roles():
    body = request.get_json(silent=True) or {}
    user_role = body.get("user_role")
    bot_role = body.get("bot_role")

    if not user_role or not bot_role:
        return jsonify({"ok": False, "error": "roles are required"}), 400

    current_roles["user_role"] = user_role
    current_roles["bot_role"] = bot_role

    # ✅ 백엔드에 start 호출
    try:
        headers = {"Authorization": f"Bearer {current_session['access_token']}"}
        start_res = requests.post(
            f"{BACKEND_BASE}/api/roleplay/start",
            headers=headers,
            json={
                "session_id": current_session["session_id"],
                "profile_id": current_roles["profile_id"],
                "user_role": user_role,
                "bot_role": bot_role,
            },
            timeout=10,
        )
        start_res.raise_for_status()
        result = start_res.json()
        chatroom_id = result.get("chatroom_id")
        current_session["chatroom_id"] = chatroom_id
    except Exception as e:
        app.logger.error(f"[confirm_roles] backend start 실패: {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False, "error": "backend_roleplay_start_failed"}), 500

    return jsonify({
        "ok": True,
        "chatroom_id": current_session["chatroom_id"],
        "user_role": user_role,
        "bot_role": bot_role,
    })


def notify_backend_roleplay_start():
    global current_session, current_roles

    if not current_roles["user_role"] or not current_roles["bot_role"]:
        app.logger.warning("[notify_backend_roleplay_start] 역할 미정 → 호출 안 함")
        return False

    try:
        headers = {"Authorization": f"Bearer {current_session['access_token']}"}
        start_res = requests.post(
            f"{BACKEND_BASE}/api/roleplay/start",
            headers=headers,
            json={
                "session_id": current_session["session_id"],
                "user_role": current_roles["user_role"],
                "bot_role": current_roles["bot_role"],
                "profile_id": current_roles["profile_id"],
            },
            timeout=BACKEND_TIMEOUT,
        )
        start_res.raise_for_status()
        result = start_res.json().get("result", {})
        current_session["session_id"] = result.get("session_id") or current_session["session_id"]
        current_session["chatroom_id"] = result.get("chatroom_id") or result.get("chatRoomId")
        app.logger.info("[notify_backend_roleplay_start] backend 시작 성공")
        return True
    except Exception as e:
        app.logger.error(f"[notify_backend_roleplay_start] backend 시작 실패: {e}\n{traceback.format_exc()}")
        return False


@app.route("/start/safety-quiz", methods=["POST"])
def http_start_safety_quiz():
    global current_mode, current_session
    body = request.get_json(silent=True) or {}

    profile_id = int(body.get("profile_id") or 0) or get_profile_id()
    session_id = (body.get("session_id") or "").strip()
    topic = (body.get("topic") or "").strip()

    if profile_id <= 0 or not topic:
        return jsonify({"ok": False, "error": "profile_id와 topic은 필수입니다."})

    if not session_id:
        session_id = f"{profile_id}_quiz"

    stop_worker()
    current_mode = "safety_quiz"
    current_session = {"session_id": session_id, "chatroom_id": None}

    start_worker(safety_quiz_loop, session_id, profile_id, topic)

    return jsonify({
        "ok": True,
        "mode": "safety_quiz",
        "session_id": session_id,
        "profile_id": profile_id,
        "topic": topic
    }), 202


# ===== Flask 라우트 =====
@app.route("/start/quiz", methods=["POST"])
def http_start_quiz():
    global current_mode, current_session
    body = request.get_json(silent=True) or {}

    profile_id = int(body.get("profile_id") or 0) or get_profile_id()
    session_id = (body.get("session_id") or "").strip()

    if profile_id <= 0:
        return jsonify({"ok": False, "error": "profile_id는 필수입니다."})

    if not session_id:
        session_id = f"{profile_id}_초성퀴즈"

    stop_worker()
    current_mode = "quiz"
    current_session = {"session_id": session_id, "chatroom_id": None}

    start_worker(quiz_loop, session_id, profile_id)

    return jsonify({
        "ok": True,
        "mode": "quiz",
        "session_id": session_id,
        "profile_id": profile_id
    }), 202


@app.route("/start/animal-quiz", methods=["POST"])
def http_start_animal_quiz():
    global current_mode, current_session
    body = request.get_json(silent=True) or {}

    profile_id = int(body.get("profile_id") or 0) or get_profile_id()
    session_id = (body.get("session_id") or "").strip()
    animal_name = (body.get("animal_name") or "").strip()

    if profile_id <= 0 or not animal_name:
        return jsonify({"ok": False, "error": "profile_id와 animal_name은 필수입니다."})

    if not session_id:
        session_id = f"{profile_id}_animal_quiz"

    stop_worker()
    current_mode = "animal_quiz"
    current_session = {"session_id": session_id, "chatroom_id": None}

    start_worker(animal_quiz_loop, session_id, profile_id, animal_name)

    return jsonify({
        "ok": True,
        "mode": "animal_quiz",
        "session_id": session_id,
        "profile_id": profile_id,
        "animal_name": animal_name
    }), 202

@app.route("/start/conversation", methods=["POST"])
def http_start_conversation():
    global current_mode, current_session
    body = request.get_json(silent=True) or {}

    stop_worker()

    session_id = (body.get("session_id") or "").strip()

    profile_id = body.get("profile_id")
    if profile_id:
        profile_id = int(profile_id)
    else:
        profile_id = get_profile_id()
    access_token = body.get("access_token")
    chatroom_id = None

    if not session_id and profile_id:
        try:
            data = backend_conversation_start(int(profile_id))
            session_id = str(data.get("session_id") or "").strip()
            chatroom_id = data.get("chatroom_id")  # 없을 수도 있음
            if not session_id:
                raise RuntimeError("백엔드 응답에 session_id가 없습니다.")
        except Exception as e:
            app.logger.error(f"[start_conversation] backend start 실패: {e}\n{traceback.format_exc()}")
            # 웹소켓으로도 오류 알림
            notify("error", {"message": "대화 시작 실패"})
            return jsonify({"ok": False, "error": f"backend_conversation_start_failed: {e}"}), 500

    if not session_id:
        session_id = "conv_session"  # 데모 세션

    current_mode = "conversation"
    current_session = {
        "session_id": session_id,
        "chatroom_id": chatroom_id,
        "profile_id": profile_id,
        "access_token": access_token,   # ✅ 추가
    }

    notify("ready", {"text": "대화 세션이 준비됐어요. 곧 안내 멘트가 나와요!"})

    # 워커 시작
    start_worker(conversation_loop, session_id, profile_id, access_token)

    return jsonify({
        "ok": True,
        "mode": current_mode,
        "session_id": session_id,
        "chatroom_id": chatroom_id,
        "profile_id": profile_id
    }), 202



@app.route("/stop", methods=["POST"])
def http_stop():
    stop_worker()
    try:
        if current_session.get("session_id"):
            backend_conversation_end(current_session["session_id"])
    except Exception as e:
        app.logger.warning(f"[stop] /api/conversation/end 호출 중 예외: {e}")
    return jsonify({"ok": True, "mode": current_mode})


@app.route("/state", methods=["GET"])
def http_state():
    return jsonify({
        "mode": current_mode,
        "running": bool(worker_thread and worker_thread.is_alive()),
        "volume": volume_percent,
        "roles": current_roles,
        "session": current_session
    })

@app.route("/volume", methods=["POST"])
def http_volume():
    body = request.get_json(silent=True) or {}
    global volume_percent
    volume_percent = max(0, min(100, int(body.get("percent", 60))))
    return jsonify({"ok": True, "volume": volume_percent})

@app.route("/debug/ping")
def debug_ping():
    return jsonify({
        "ok": True,
        "version": VERSION,
        "pid": os.getpid(),
        "file": __file__,
        "cwd": os.getcwd(),
        "mode": current_mode,
        "tts_src": _TTS_SRC,
        "stt_src": _STT_SRC,
    })

@app.route("/set-profile", methods=["POST"])
def http_set_profile():
    body = request.get_json(silent=True) or {}
    pid = int(body.get("profile_id") or 0)
    if pid <= 0:
        return jsonify({"ok": False, "error": "유효하지 않은 profile_id"})
    set_profile_id(pid)
    return jsonify({"ok": True, "profile_id": get_profile_id()})

import re

def normalize_gguro(text: str) -> str:
    """STT 결과에서 '꾸로'/'꾸로는'을 강력 보정"""
    out = text

    # --- 직접적으로 들린 단어들 교정 ---
    replacements = {
        "구로": "꾸로",
        "쿠로": "꾸로",
        "고로": "꾸로",
        "꾸루": "꾸로",
        "꾸르": "꾸로",
        "프로": "꾸로",
        "프로는": "꾸로는",
        "쿠루": "꾸로",
        "구루": "꾸로",
        "코로나": "꾸로",
        "코 로 나": "꾸로",
        "코로나는": "꾸로는",
        "코로는": "꾸로는",
        "그 러는": "꾸로는",
        "고르는": "꾸로는",
        "구르는": "꾸로는",
        "쿠르는": "꾸로는",
        "부르는": "꾸로는",
    }
    for k, v in replacements.items():
        out = out.replace(k, v)

    # --- 정규식 기반 교정 ---
    out = re.sub(r"곧\s*그\s*러는", "꾸로는", out)
    out = re.sub(r"고\s*그\s*러는", "꾸로는", out)
    out = re.sub(r"꾸\s*론은", "꾸로는", out)
    out = re.sub(r"꾸\s*로\s*는", "꾸로는", out)
    out = re.sub(r"(고|구|쿠)\s*르는", "꾸로는", out)
    out = re.sub(r"(고|구|쿠)\s*부르는", "꾸로는", out)

    # 자모 분리
    out = re.sub(r"ㄲ\s*ㅜ\s*ㄹ\s*ㅗ", "꾸로", out)
    out = re.sub(r"ㄲㅜ\s*로", "꾸로", out)

    # 불필요한 공백 제거
    out = re.sub(r"\s+", " ", out).strip()

    return out


if __name__ == "__main__":
    print(f">>> pi_controller v2025-08-27 :: __file__={__file__} :: cwd={os.getcwd()}")
    # allow_unsafe_werkzeug ❌ 제거
    socketio.run(app, host="0.0.0.0", port=8787)
