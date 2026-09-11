import os
import time
import threading
import requests
from datetime import datetime, date
from flask import Flask, request, jsonify, send_from_directory

import webhooks

# ── 환경변수 (관리자용 세션 정보만 유지, 웹훅은 사용자가 등록) ──
WMONID          = os.environ["WMONID"]
AUTO_LOGIN_KEY  = os.environ["AUTO_LOGIN_KEY"]
REMEMBER_ID     = os.environ["REMEMBER_ID"]
PORT            = int(os.environ.get("PORT", 8080))

# ── 예약 대상 날짜 (9/15 ~ 9/23) ─────────────────────
TARGET_DATES = [
    "20260915", "20260916", "20260917", "20260918",
    "20260919", "20260920", "20260921", "20260922", "20260923"
]

PAGE_URL = "https://hi.thehyundai.com/o4o/reservation/form?storeCd=400&brndLowCd=A91060&rsvItemCd=0000000540"
API_URL  = "https://hi.thehyundai.com/proxy/v1/rs/reservation/reservationPossTime"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
    "Referer": PAGE_URL,
    "Accept": "application/json, text/plain, */*",
}


def get_cookies():
    return {
        "WMONID":             WMONID,
        "isLogin":            "true",
        "autoLoginYn":        "Y",
        "autoLoginKey":       AUTO_LOGIN_KEY,
        "unified_rememberId": REMEMBER_ID,
    }


# ── 예약 가능 시간대 조회 ─────────────────────────────
def check_date(session, rsv_dt):
    params = {
        "storeCd":            "400",
        "brndLowCd":          "A91060",
        "rsvItemCd":          "0000000540",
        "rsvDt":              rsv_dt,
        "thdyRsvBsicTimeGbcd":"0",
    }
    try:
        r = session.get(API_URL, params=params, timeout=10)
        if r.status_code != 200:
            print(f"[{rsv_dt}] HTTP {r.status_code}")
            return []
        data = r.json()
        slots = []
        items = data if isinstance(data, list) else data.get("data", data.get("list", []))
        for item in items:
            remain = item.get("rsvPossQty", item.get("remainQty", item.get("possQty", -1)))
            if isinstance(remain, (int, float)) and remain > 0:
                slots.append(item)
        return slots
    except Exception as e:
        print(f"[{rsv_dt}] 오류: {e}")
        return []


def get_weekday(rsv_dt):
    d = date(int(rsv_dt[:4]), int(rsv_dt[4:6]), int(rsv_dt[6:]))
    return ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]


# ── 디스코드 알림 (등록된 모든 웹훅으로 전송) ─────────
def send_discord(available):
    lines = []
    for rsv_dt, slots in available.items():
        date_str = f"{rsv_dt[:4]}/{rsv_dt[4:6]}/{rsv_dt[6:]} ({get_weekday(rsv_dt)})"
        for s in slots:
            time_str = s.get("rsvBsicTimeNm", s.get("timeName", s.get("time", "시간 확인 필요")))
            remain   = s.get("rsvPossQty", s.get("remainQty", "?"))
            lines.append(f"• {date_str} {time_str} — 잔여 {remain}석")

    desc = "\n".join(lines) if lines else "예약 가능한 슬롯이 감지되었습니다."
    payload = {
        "username": "예약 모니터 🔔",
        "embeds": [{
            "title": "🎉 리센느 팝업 예약 취소표 발견!",
            "description": f"**예약 가능 슬롯:**\n{desc}\n\n🔗 [지금 바로 예약하기]({PAGE_URL})",
            "color": 5763719,
            "footer": {"text": "현대백화점 예약 모니터 • 실시간 감지"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    }

    hooks = webhooks.get_all()
    if not hooks:
        print("⚠️ 등록된 웹훅이 없습니다.")
        return

    for url in hooks:
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code in (200, 204):
                print(f"✅ 알림 전송 완료 → {url[:50]}...")
            elif r.status_code == 404:
                # 삭제되었거나 유효하지 않은 웹훅은 목록에서 제거
                print(f"❌ 잘못된 웹훅, 제거 → {url[:50]}...")
                webhooks.remove(url)
            else:
                print(f"❌ 알림 실패({r.status_code}) → {url[:50]}...")
        except Exception as e:
            print(f"❌ 알림 오류: {e}")


# ── 모니터링 백그라운드 루프 ──────────────────────────
def get_session_with_login():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(get_cookies())
    try:
        r = session.get(PAGE_URL, timeout=10)
        print(f"세션 초기화: HTTP {r.status_code}")
    except Exception as e:
        print(f"세션 초기화 오류: {e}")
    refresh_access_token(session)
    return session


def refresh_access_token(session):
    """페이지 재방문 시 서버가 내려주는 accessToken 쿠키를 읽어
    Authorization 헤더에 반영한다 (토큰은 약 30분마다 만료됨)."""
    try:
        session.get(PAGE_URL, timeout=10)
        token = session.cookies.get("accessToken")
        if token:
            session.headers["Authorization"] = f"Bearer {token}"
            print("🔑 accessToken 갱신 완료")
        else:
            print("⚠️ accessToken 쿠키를 찾지 못함")
    except Exception as e:
        print(f"⚠️ 토큰 갱신 오류: {e}")


def monitor_loop():
    print("🚀 현대백화점 예약 취소표 모니터 시작!")
    print(f"📅 모니터링 날짜: {TARGET_DATES[0]} ~ {TARGET_DATES[-1]}")

    last_found = {}
    interval = 3
    last_refresh = time.time()
    REFRESH_EVERY = 20 * 60  # 20분마다 토큰 갱신 (만료 30분보다 여유있게)

    session = get_session_with_login()

    while True:
        if time.time() - last_refresh > REFRESH_EVERY:
            refresh_access_token(session)
            last_refresh = time.time()

        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{now}] 전체 날짜 체크 중...")

        available = {}
        for rsv_dt in TARGET_DATES:
            slots = check_date(session, rsv_dt)
            if slots:
                available[rsv_dt] = slots
                print(f"  🎉 {rsv_dt}: {len(slots)}개 슬롯 발견!")
            else:
                print(f"  — {rsv_dt}: 없음")
            time.sleep(interval)

        new_available = {k: v for k, v in available.items() if k not in last_found or last_found[k] != len(v)}
        if new_available:
            send_discord(new_available)
            last_found.update({k: len(v) for k, v in new_available.items()})

        for k in list(last_found.keys()):
            if k not in available:
                del last_found[k]

        print(f"[{now}] 30초 후 재확인...")
        time.sleep(30)


# ── Flask 웹서버 (웹훅 등록용) ─────────────────────────
app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    url = data.get("webhook", "")
    if webhooks.add(url):
        return jsonify({"ok": True})
    return jsonify({"error": "올바른 디스코드 웹훅 URL이 아닙니다."}), 400


@app.route("/api/status")
def status():
    return jsonify({"registered_count": len(webhooks.get_all())})


if __name__ == "__main__":
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=PORT)
