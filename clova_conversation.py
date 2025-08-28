import os, subprocess, shlex, requests, traceback
from dotenv import load_dotenv
import re

load_dotenv()

# ===== NAVER Clova Key =====
NCP_KEY_ID = os.getenv("NCP_KEY_ID", "")
NCP_KEY    = os.getenv("NCP_KEY", "")

# ===== Endpoint =====
STT_URL = "https://naveropenapi.apigw.ntruss.com/recog/v1/stt?lang=Kor"
TTS_URL = "https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts"

# ===== Device =====
IN_DEV     = "hw:4,0"                   # 🎤 ReSpeaker 마이크
MPG123_OUT = "-a plughw:3,0 -f 18000"   # 🔊 USB 스피커
SR         = 16000
MIN_SEC    = 0.3

# ===== Temp Files =====
TMP_RAW = "/tmp/utt_raw.wav"
TMP_ST  = "/tmp/utt_st.wav"
TMP_WAV = "/tmp/utt.wav"
TMP_MP3 = "/tmp/tts.mp3"

# ===== Headers =====
HEADERS_STT = {
    "X-NCP-APIGW-API-KEY-ID": NCP_KEY_ID,
    "X-NCP-APIGW-API-KEY": NCP_KEY,
    "Content-Type": "application/octet-stream",
}
HEADERS_TTS = {
    "X-NCP-APIGW-API-KEY-ID": NCP_KEY_ID,
    "X-NCP-APIGW-API-KEY": NCP_KEY,
    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
}

# ===== TTS =====
def say(text: str, speaker="ndain", speed="0"):
    try:
        print(f"[TTS req] '{text[:60] + ('...' if len(text)>60 else '')}'")
        data = {"speaker": speaker, "speed": speed, "text": text}
        r = requests.post(TTS_URL, headers=HEADERS_TTS, data=data, timeout=30)
        r.raise_for_status()
        with open(TMP_MP3, "wb") as f:
            f.write(r.content)
        cmd = f"mpg123 {MPG123_OUT} {shlex.quote(TMP_MP3)} >/dev/null 2>&1"
        subprocess.call(cmd, shell=True)
        print("[TTS] done")
    except Exception as e:
        print("[TTS ERROR]", e)
        traceback.print_exc()

# ===== STT =====
def stt_once() -> str:
    rec = f"arecord -D {IN_DEV} -f S16_LE -c2 -r48000 -d 8 {TMP_RAW}"
    print("[ARECORD]", rec)
    subprocess.call(rec, shell=True)

    if not os.path.exists(TMP_RAW) or os.path.getsize(TMP_RAW) < 500:
        print("[ARECORD] no audio captured or too small")
        return ""

    # VAD trim
    vad_rules = [
        "silence 1 0.05 -20d 1 0.8 -20d",
        "silence 1 0.05 -18d 1 0.7 -18d",
        "silence 1 0.05  7%  1 0.8  7%",
    ]
    trimmed = False
    for rule in vad_rules:
        cmd = f"sox {TMP_RAW} {TMP_ST} highpass 100 {rule}"
        print("[SOX TRIM]", cmd)
        subprocess.call(cmd, shell=True)
        if os.path.exists(TMP_ST) and os.path.getsize(TMP_ST) > 500:
            print(f"[SOX TRIM] ok, size={os.path.getsize(TMP_ST)} bytes")
            trimmed = True
            break
    src_for_conv = TMP_ST if trimmed else TMP_RAW

    # convert
    conv = f"sox {src_for_conv} -c 1 -r {SR} -b 16 -e signed-integer {TMP_WAV}"
    print("[SOX CONV]", conv)
    subprocess.call(conv, shell=True)

    if not os.path.exists(TMP_WAV) or os.path.getsize(TMP_WAV) < 500:
        print("[SOX CONV] failed or too small")
        return ""

    wav_size = os.path.getsize(TMP_WAV)
    dur = (wav_size - 44) / (SR * 2)
    if dur < MIN_SEC:
        print(f"[CHECK] too short: {dur:.2f}s -> skip")
        return ""

    try:
        with open(TMP_WAV, "rb") as f:
            print("[STT] request (CSR short sentence)…")
            r = requests.post(STT_URL, headers=HEADERS_STT, data=f.read(), timeout=60)

        print(f"[STT] status={r.status_code} ct={r.headers.get('Content-Type')}")
        if not r.ok:
            print("[STT ERROR]", r.text[:500])
            return ""

        txt = r.json().get("text", "").strip()
        txt = normalize_gguro(txt)

        print("[STT json]", txt)
        return txt
    except Exception as e:
        print("[STT ERROR]", e)
        traceback.print_exc()
        return ""


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
