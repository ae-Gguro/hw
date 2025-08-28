import os, subprocess, shlex, traceback, requests
from session_store import current_session, current_roles
from dotenv import load_dotenv
import re


load_dotenv()


# ===== NAVER Clova Key =====
NCP_KEY_ID = os.getenv("NCP_KEY_ID")
NCP_KEY    = os.getenv("NCP_KEY")

# ===== Endpoint =====
STT_URL = "https://naveropenapi.apigw.ntruss.com/recog/v1/stt?lang=Kor"
TTS_URL = "https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts"

# ===== Device =====
IN_DEV     = "hw:4,0"                   # 마이크 (ReSpeaker HAT)
MPG123_OUT = "-a plughw:3,0 -f 18000"   # 스피커 (USB)
SR         = 16000

# ===== 파일 경로 =====
TMP_RAW = "/tmp/utt_raw.wav"
TMP_ST  = "/tmp/utt_st.wav"
TMP_WAV = "/tmp/utt.wav"
TMP_MP3 = "/tmp/tts.mp3"

STOP_KEYWORD = "그만하고 싶어"

# ---------- TTS ----------
def say(text: str, speaker="ndain", speed="0"):
    """Premium TTS -> USB speaker"""
    try:
        preview = text[:60] + ("..." if len(text) > 60 else "")
        print(f"[TTS req] '{preview}'")
        data = {"speaker": speaker, "speed": speed, "text": text}
        r = requests.post(TTS_URL, headers={
            "X-NCP-APIGW-API-KEY-ID": NCP_KEY_ID,
            "X-NCP-APIGW-API-KEY": NCP_KEY,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }, data=data, timeout=60)
        r.raise_for_status()
        with open(TMP_MP3, "wb") as f:
            f.write(r.content)
        cmd = f"mpg123 {MPG123_OUT} {shlex.quote(TMP_MP3)} >/dev/null 2>&1"
        subprocess.call(cmd, shell=True)
        print("[TTS] done")
    except Exception as e:
        print("[TTS ERROR]", e)
        traceback.print_exc()

# ---------- STT ----------
def stt_once() -> str:
    """arecord -> sox -> Clova STT"""
    rec = f"arecord -D {IN_DEV} -f S16_LE -c2 -r48000 -d 8 {TMP_RAW}"
    print("[ARECORD]", rec)
    subprocess.call(rec, shell=True)

    conv = f"sox {TMP_RAW} -c 1 -r {SR} -b 16 -e signed-integer -t wavpcm {TMP_WAV}"
    subprocess.call(conv, shell=True)

    with open(TMP_WAV, "rb") as f:
        r = requests.post(STT_URL, headers={
            "X-NCP-APIGW-API-KEY-ID": NCP_KEY_ID,
            "X-NCP-APIGW-API-KEY": NCP_KEY,
            "Content-Type": "application/octet-stream",
        }, data=f.read(), timeout=60)

    print(f"[STT] status={r.status_code} ct={r.headers.get('Content-Type')}")
    try:
        data = r.json()
        txt = data.get("text", "").strip()
        print("[STT 결과]", txt)
        return txt
    except Exception:
        return ""

# ---------- 역할 파싱 ----------
BOT_ALIASES = r"(?:꾸로|쿠로|구로|구 론|구론|그룹|구글|고로|고고론|너|너는|AI|봇)"
ME_ALIASES  = r"(?:나는|난|제가|전|는)"

def parse_roles_basic(text: str):
    if not text:
        return None, None
    t = re.sub(r"\s+", "", text)

    # 패턴 1: 나는 XXX, 꾸로는 YYY
    m = re.search(
        rf"{ME_ALIASES}(.+?)(?:이|가|고|이고|야|이야)?{BOT_ALIASES}는(.+?)(?:이야|야|입니다|이에요|예요)?$",
        t,
    )

    # 패턴 2: 꾸로는 XXX, 나는 YYY
    m = re.search(rf"{BOT_ALIASES}는(.+?)(?:이|가|고|이고|야|이야)?{ME_ALIASES}(.+?)(?:이|가|고|이고|야|이야)?", t)
    if m: return _clean_role(m.group(2)), _clean_role(m.group(1))

    # 패턴 3: 나는 빠졌지만 꾸로만 있는 경우 (ex: 선생님이고 꾸로는 학생이야)
    m = re.search(rf"(.+?)(?:이|가|고|이고)?{BOT_ALIASES}는(.+?)(?:야|이야)?$", t)
    if m: return _clean_role(m.group(1)), _clean_role(m.group(2))

    return None, None


def _clean_role(s: str) -> str:
    orig = s
    s = re.sub(r"\s+", "", s or "")
    s = s.strip(" '\"“”‘’()[]{}<>")

    # ✅ 문장 앞쪽 "나는/난/제가/전" 제거
    s = re.sub(r"^(나는|난|제가|전)", "", s)

    # ✅ 문장 앞쪽 "은/는/이/가" 같은 조사 제거
    s = re.sub(r"^(은|는|이|가)", "", s)

    # ✅ 끝쪽 불필요 표현 제거
    s = re.sub(r"(입니다|이에요|예요|할래|할게|할께|야|이야)$", "", s)
    s = re.sub(r"(은|는|이|가|을|를)$", "", s)

    print(f"[_clean_role] before='{orig}' after='{s}'")
    return s
	
# ---------- 서버 연동 ----------
def _auth_headers():
    h = {
        "Content-Type": "application/json",
        "ngrok-skip-browser-warning": "true",
    }
    token = current_session.get("access_token") 
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

# SERVER_URL 은 환경변수 그대로 유지
SERVER_URL = (os.getenv("SERVER_URL") or "").rstrip("/")

def _auth_headers():
    h = {"Content-Type": "application/json"}
    token = current_session.get("access_token")
    print(f"[DEBUG] _auth_headers token={token}")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def call_start(user_role: str, bot_role: str, session_id: str) -> dict:
    url = f"{SERVER_URL}/api/roleplay/start"
    payload = {
        "session_id": session_id,
        "profile_id": current_roles.get("profile_id"),
        "user_role": user_role,
        "bot_role": bot_role,
    }
    print(f"[CALL_START] payload={payload}")
    r = requests.post(url, headers=_auth_headers(), json=payload, timeout=20)
    r.raise_for_status()
    res = r.json()
    print(f"[CALL_START] response={res}")
    return res

def call_talk(chatroom_id: int, user_text: str, session_id: str) -> dict:
    url = f"{SERVER_URL}/api/roleplay/{chatroom_id}/talk"
    payload = {
        "user_input": user_text,
        "session_id": session_id,
        "profile_id": current_roles.get("profile_id"),
    }
    print(f"[CALL_TALK] url={url} payload={payload}")
    r = requests.post(url, headers=_auth_headers(), json=payload, timeout=20)
    r.raise_for_status()
    res = r.json()
    print(f"[CALL_TALK] response={res}")
    return res

def call_end(session_id: str) -> dict:
    url = f"{SERVER_URL}/api/conversation/end"
    payload = {
        "session_id": session_id,
        "profile_id": current_roles.get("profile_id"),
    }
    print(f"[CALL_END] payload={payload}")
    r = requests.post(url, headers=_auth_headers(), json=payload, timeout=20)
    r.raise_for_status()
    res = r.json()
    print(f"[CALL_END] response={res}")
    return res
