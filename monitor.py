import os
import re
import time
import threading
import requests
from datetime import datetime, date
from flask import Flask, request, jsonify, send_from_directory
from playwright.sync_api import sync_playwright

import webhooks

WMONID          = os.environ["WMONID"]
AUTO_LOGIN_KEY  = os.environ["AUTO_LOGIN_KEY"]
REMEMBER_ID     = os.environ["REMEMBER_ID"]
PORT            = int(os.environ.get("PORT", 8080))

TARGET_DATES = [
    "20260915", "20260916", "20260917", "20260918",
    "20260919", "20260920", "20260921", "20260922", "20260923"
]

PAGE_URL = "https://hi.thehyundai.com/o4o/reservation/form?storeCd=400&brndLowCd=A91060&rsvItemCd=0000000540"
API_URL_PATTERN = re.compile(r"reservationPossTime\?.*rsvDt=(\d{8})")


def get_weekday(rsv_dt):
    d = date(int(rsv_dt[:4]), int(rsv_dt[4:6]), int(rsv_dt[6:]))
    return ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]


def send_discord(available):
    lines = []
    for rsv_dt, slots in available.items():
        date_str = f"{rsv_dt[:4]}/{rsv_dt[4:6]}/{rsv_dt[6:]} ({get_weekday(rsv_dt)})"
        for s in slots:
            time_str = s.get("strRsvTimeGbcd", s.get("rsvBsicTimeNm", s.get("timeName", s.get("time", "시간 확인 필요"))))
            remain   = s.get("rsvPossSeatQty", s.get("rsvPossQty", s.get("remainQty", "?")))
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
                print(f"❌ 잘못된 웹훅, 제거 → {url[:50]}...")
                webhooks.remove(url)
            else:
                print(f"❌ 알림 실패({r.status_code}) → {url[:50]}...")
        except Exception as e:
            print(f"❌ 알림 오류: {e}")


def extract_slots(data):
    """API 응답 구조가 무엇이든 안전하게 예약 가능 슬롯을 뽑아낸다.
    실패해도 절대 예외를 던지지 않고 빈 리스트를 반환한다."""
    try:
        # 후보 리스트 위치들을 순서대로 탐색
        candidates = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            for key in ("data", "list", "result", "items", "rsvPossTimeList", "rsvPossTimeInfo"):
                v = data.get(key)
                if isinstance(v, list):
                    candidates = v
                    break
                if isinstance(v, dict):
                    for k2 in ("data", "list", "items", "rsvPossTimeInfo"):
                        v2 = v.get(k2)
                        if isinstance(v2, list):
                            candidates = v2
                            break
                    if candidates:
                        break

        slots = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            remain = None
            for key in ("rsvPossQty", "remainQty", "possQty", "rsvPossCnt", "possCnt", "rsvPossSeatQty", "rsvRmndSeatQty"):
                v = item.get(key)
                if isinstance(v, (int, float)):
                    remain = v
                    break
            if remain is not None and remain > 0:
                slots.append(item)
        return slots
    except Exception as e:
        print(f"⚠️ 슬롯 파싱 실패(무시하고 계속): {e}")
        return []


def check_one_cycle(page, last_found):
    """이미 열려있는 페이지에서 날짜들을 다시 클릭해 최신 데이터를 갱신시킨다.
    API 응답이 오는 즉시(다른 날짜 체크를 기다리지 않고) 슬롯 발견 시 바로 알림을 보낸다."""
    for rsv_dt in TARGET_DATES:
        day_num = str(int(rsv_dt[6:]))
        try:
            btn = page.locator(f"button:has-text('{day_num}')").first
            if btn.count() == 0:
                btn = page.get_by_text(day_num, exact=True).first
            btn.click(timeout=1500)
        except Exception:
            pass


def monitor_loop():
    print("🚀 현대백화점 예약 취소표 모니터 시작! (Playwright 브라우저 모드)")
    print(f"📅 모니터링 날짜: {TARGET_DATES[0]} ~ {TARGET_DATES[-1]}")

    last_found = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
        )
        context.add_cookies([
            {"name": "WMONID", "value": WMONID, "domain": "hi.thehyundai.com", "path": "/"},
            {"name": "isLogin", "value": "true", "domain": "hi.thehyundai.com", "path": "/"},
            {"name": "autoLoginYn", "value": "Y", "domain": "hi.thehyundai.com", "path": "/"},
            {"name": "autoLoginKey", "value": AUTO_LOGIN_KEY, "domain": "hi.thehyundai.com", "path": "/"},
            {"name": "unified_rememberId", "value": REMEMBER_ID, "domain": "hi.thehyundai.com", "path": "/"},
        ])

        page = context.new_page()

        def on_response(response):
            try:
                m = API_URL_PATTERN.search(response.url)
                if not (m and response.status == 200):
                    return
                rsv_dt = m.group(1)
                data = response.json()
                slots = extract_slots(data)
                cnt = len(slots)
                if cnt > 0 and last_found.get(rsv_dt) != cnt:
                    print(f"  🎉 {rsv_dt}: {cnt}개 슬롯 발견! 즉시 알림 전송")
                    send_discord({rsv_dt: slots})
                    last_found[rsv_dt] = cnt
                elif cnt == 0 and rsv_dt in last_found:
                    del last_found[rsv_dt]
            except Exception as e:
                print(f"⚠️ 응답 처리 오류(무시하고 계속): {e}")

        page.on("response", on_response)

        # 페이지는 한 번만 로드하고, 이후엔 계속 재사용 (매 사이클 재로딩 없음 → 훨씬 빠름)
        page.goto(PAGE_URL, wait_until="networkidle", timeout=30000)

        last_reload = time.time()
        RELOAD_EVERY = 15 * 60  # 세션 만료 방지용으로만 15분마다 새로고침

        while True:
            now = datetime.now().strftime("%H:%M:%S")

            if time.time() - last_reload > RELOAD_EVERY:
                try:
                    page.reload(wait_until="networkidle", timeout=30000)
                except Exception as e:
                    print(f"⚠️ 새로고침 오류(무시하고 계속): {e}")
                last_reload = time.time()

            try:
                check_one_cycle(page, last_found)
            except Exception as e:
                print(f"⚠️ 체크 오류(무시하고 계속): {e}")

            time.sleep(0.2)


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
