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
                print(f"❌ 잘못된 웹훅, 제거 → {url[:50]}...")
                webhooks.remove(url)
            else:
                print(f"❌ 알림 실패({r.status_code}) → {url[:50]}...")
        except Exception as e:
            print(f"❌ 알림 오류: {e}")


def check_all_dates(context):
    """날짜별로 페이지를 열고, 브라우저가 실제로 호출하는
    reservationPossTime 응답을 가로채서 결과를 모은다."""
    results = {}
    page = context.new_page()
    captured = {}

    def on_response(response):
        m = API_URL_PATTERN.search(response.url)
        if m and response.status == 200:
            rsv_dt = m.group(1)
            try:
                captured[rsv_dt] = response.json()
            except Exception:
                pass

    page.on("response", on_response)

    try:
        page.goto(PAGE_URL, wait_until="networkidle", timeout=30000)
        for rsv_dt in TARGET_DATES:
            day_num = str(int(rsv_dt[6:]))
            try:
                page.get_by_text(day_num, exact=True).first.click(timeout=3000)
                page.wait_for_timeout(1200)
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ 페이지 탐색 오류: {e}")
    finally:
        page.close()

    for rsv_dt, data in captured.items():
        slots = []
        items = data if isinstance(data, list) else data.get("data", data.get("list", []))
        for item in items or []:
            remain = item.get("rsvPossQty", item.get("remainQty", item.get("possQty", -1)))
            if isinstance(remain, (int, float)) and remain > 0:
                slots.append(item)
        results[rsv_dt] = slots

    return results


def monitor_loop():
    print("🚀 현대백화점 예약 취소표 모니터 시작! (Playwright 브라우저 모드)")
    print(f"📅 모니터링 날짜: {TARGET_DATES[0]} ~ {TARGET_DATES[-1]}")

    last_found = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

        while True:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{now}] 전체 날짜 체크 중...")

            try:
                available_raw = check_all_dates(context)
            except Exception as e:
                print(f"⚠️ 체크 오류: {e}")
                available_raw = {}

            available = {k: v for k, v in available_raw.items() if v}
            for rsv_dt in TARGET_DATES:
                cnt = len(available.get(rsv_dt, []))
                print(f"  {'🎉' if cnt else '—'} {rsv_dt}: {cnt if cnt else '없음'}")

            new_available = {k: v for k, v in available.items() if k not in last_found or last_found[k] != len(v)}
            if new_available:
                send_discord(new_available)
                last_found.update({k: len(v) for k, v in new_available.items()})

            for k in list(last_found.keys()):
                if k not in available:
                    del last_found[k]

            print(f"[{now}] 30초 후 재확인...")
            time.sleep(30)


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
