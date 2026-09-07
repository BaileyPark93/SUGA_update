# -*- coding: utf-8 -*-
"""
SUGA 참조데이터 주간 수집기
- GUR War&Sanctions 'Components in weapons'에서 부품(모델명·제조사·사용 무기)을 수집해 update.json을 갱신
- 브라우저만으로는 교차출처 차단 때문에 못 읽으므로, 이 스크립트가 GitHub Actions에서 매주 대신 실행됨
- KITA 전략물자 목록은 자동접속 차단(TRACER) + 품목분류 수준이라 자동 수집하지 않고 안내만 남김
"""
import json, re, time, datetime, pathlib, sys, os, urllib.request, urllib.parse
from playwright.sync_api import sync_playwright

BASE = "https://war-sanctions.gur.gov.ua"
OUT = pathlib.Path("update.json")
SEEN = pathlib.Path("seen_parts.json")
MAX_NEW_PER_RUN = 250          # 한 번에 너무 많이 열지 않도록 (차단 방지)
DELAY_SEC = 1.5                # 페이지 사이 대기
LAW_OC = os.environ.get("LAW_OC", "").strip()   # 법제처 Open API 아이디(OC). GitHub Secrets에 LAW_OC로 저장하면 활성화
LAW_ADMRUL_ID = "33993"                          # 전략물자수출입고시 (law.go.kr admRulId)
LAW_STATE = pathlib.Path("law_state.json")

LABELS = ["Name and marking", "Manufacturer's headquarters country", "Manufacturer",
          "Extended description", "Рік випуску", "Additional information", "Publication date"]

def log(*a): print("[collector]", *a, flush=True)

def load_json(p, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return default

def parse_part_page(text):
    """페이지 본문 텍스트에서 라벨 다음 줄을 값으로 읽음 (값이 비면 다음 라벨이 바로 옴)"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    data = {}
    for i, l in enumerate(lines):
        if l in LABELS:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            data[l] = "" if nxt in LABELS else nxt
    # 무기 이름: 제목(첫 줄) 다음 줄이 보통 무기명
    data["_title"] = lines[0] if lines else ""
    data["_weapon"] = lines[1] if len(lines) > 1 and lines[1] not in LABELS else ""
    return data

def looks_like_marking(name):
    # 부품 번호로 매칭할 수 있으려면 숫자/영문 조합 토큰이 있어야 함 ("Tracker"처럼 이름만 있으면 매칭 불가)
    return bool(re.search(r"[A-Z0-9][A-Z0-9\-\./]{3,}", name.upper())) and bool(re.search(r"\d", name))

def check_law_amendment(notes, errors):
    """법제처 Open API로 『전략물자수출입고시』 개정 여부 확인 → 바뀌었으면 팝업 안내문 추가.
    (통제목록은 품목분류 수준이라 자동 규칙화는 불가 → '개정됐으니 별표2·3 확인' 알림이 핵심)"""
    if not LAW_OC:
        notes.append("전략물자수출입고시 개정 감시는 꺼져 있습니다 (GitHub Secrets에 LAW_OC를 넣으면 켜짐 — 설치안내 7단계).")
        return
    try:
        url = ("https://www.law.go.kr/DRF/lawService.do?" +
               urllib.parse.urlencode({"OC": LAW_OC, "target": "admrul", "ID": LAW_ADMRUL_ID, "type": "XML"}))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        xml = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
        def tag(name):
            m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S); return (m.group(1) if m else "").strip()
        info = {"고시번호": tag("행정규칙번호") or tag("공포번호"), "시행일자": tag("시행일자"), "공포일자": tag("발령일자") or tag("공포일자"), "명칭": tag("행정규칙명")}
        attachments = re.findall(r"<별표서식PDF파일링크>(.*?)</별표서식PDF파일링크>", xml)
        prev = load_json(LAW_STATE, {})
        changed = prev.get("시행일자") != info["시행일자"] or prev.get("고시번호") != info["고시번호"]
        if changed and prev:
            notes.append(f"⚠ 『전략물자수출입고시』 개정 감지 — {info['고시번호']} (시행 {info['시행일자']}). 별표2(이중용도)·별표3(군용물자) 변경 여부를 확인하세요: https://law.go.kr/LSW/admRulLsInfoP.do?admRulId={LAW_ADMRUL_ID}"
                         + (f" / 별표 PDF: https://www.law.go.kr{attachments[0]}" if attachments else ""))
        elif not prev:
            notes.append(f"전략물자수출입고시 감시 시작 — 현재 {info['고시번호']} (시행 {info['시행일자']})")
        else:
            notes.append(f"전략물자수출입고시 변동 없음 ({info['고시번호']}, 시행 {info['시행일자']})")
        LAW_STATE.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        errors.append(f"law.go.kr: {e}")

def main():
    seen = load_json(SEEN, {"ids": []})
    seen_ids = set(seen["ids"])
    prev = load_json(OUT, {})
    parts = {p["model"].upper(): p for p in prev.get("gurRecoveredParts", [])}
    notes, errors = [], []
    new_count = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                                  locale="en-US")
        page = ctx.new_page()

        # 1) 뉴스 페이지에서 "COMPONENTS IN WEAPONS" 추가 공지를 그대로 메모로 수집
        try:
            page.goto(f"{BASE}/en/news", timeout=60000); page.wait_for_timeout(3000)
            body = page.inner_text("body")
            for m in re.finditer(r"COMPONENTS IN WEAPONS(.{0,600})", body, re.S):
                snippet = re.sub(r"\s+", " ", m.group(1)).strip()
                if snippet: notes.append("GUR 공지: " + snippet[:300])
        except Exception as e:
            errors.append(f"news page: {e}")

        # 2) 무기 목록 → 부품 페이지 링크 수집 (목록은 JS로 그려지므로 렌더링 후 링크 추출)
        part_links = []
        try:
            page.goto(f"{BASE}/en/components/weapon", timeout=60000); page.wait_for_timeout(4000)
            for _ in range(15):   # 무한스크롤/더보기 대응
                page.mouse.wheel(0, 4000); page.wait_for_timeout(800)
            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            weapon_links = sorted({h for h in hrefs if re.search(r"/en/(page-|components/weapon/)", h)})
            log("weapon pages:", len(weapon_links))
            for w in weapon_links[:60]:
                try:
                    page.goto(w, timeout=60000); page.wait_for_timeout(2000)
                    hs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                    part_links += [h for h in hs if re.search(r"/en/components/(part/)?\d+$", h)]
                    time.sleep(DELAY_SEC)
                except Exception as e:
                    errors.append(f"weapon {w}: {e}")
        except Exception as e:
            errors.append(f"weapon list: {e}")
        part_links = sorted(set(part_links))
        log("part links found:", len(part_links))

        # 3) 아직 안 본 부품 페이지만 열어서 파싱
        todo = [h for h in part_links if re.search(r"(\d+)$", h).group(1) not in seen_ids][:MAX_NEW_PER_RUN]
        for h in todo:
            pid = re.search(r"(\d+)$", h).group(1)
            try:
                page.goto(h, timeout=60000); page.wait_for_timeout(1500)
                d = parse_part_page(page.inner_text("body"))
                name = d.get("Name and marking") or d.get("_title", "")
                mfr = d.get("Manufacturer", "")
                weapon = d.get("_weapon", "")
                seen_ids.add(pid)
                if name and looks_like_marking(name):
                    key = name.upper()
                    if key not in parts:
                        parts[key] = {"model": name, "mfr": mfr, "use": f"{weapon} 부품으로 확인 (GUR {pid}, {d.get('Publication date','')})".strip(),
                                      "militaryOnly": False, "source": h}
                        new_count += 1
                time.sleep(DELAY_SEC)
            except Exception as e:
                errors.append(f"part {pid}: {e}")
        browser.close()

    # 국내 공식 통제목록(고시) 개정 감시
    check_law_amendment(notes, errors)

    # KITA 안내 (자동 수집 불가)
    notes.append("KITA 전략물자 목록(kita.net)은 자동접속 차단 시스템 때문에 수집하지 않습니다. 공식 통제목록은 국가법령정보센터의 전략물자수출입고시 별표2·3이며, 무역안보관리원(yestrade.go.kr) 공지에서 개정 행정예고를 확인할 수 있습니다.")
    if errors: notes.append(f"수집 중 오류 {len(errors)}건 (사이트 구조 변경·차단 가능성): " + " | ".join(errors[:3]))

    out = {
        "version": datetime.date.today().isoformat(),
        "generatedAt": datetime.datetime.utcnow().isoformat() + "Z",
        "source": ["GUR War & Sanctions", "법제처 국가법령정보센터(전략물자수출입고시)"],
        "productRules": [], "vendorWatchlist": [], "knowledgeBase": [],
        "gurRecoveredParts": sorted(parts.values(), key=lambda p: p["model"]),
        "notes": notes,
        "stats": {"newThisRun": new_count, "totalParts": len(parts), "errors": len(errors)},
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    SEEN.write_text(json.dumps({"ids": sorted(seen_ids)}, ensure_ascii=False), encoding="utf-8")
    log(f"done: new={new_count} total={len(parts)} errors={len(errors)}")

if __name__ == "__main__":
    main()
