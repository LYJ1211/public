# wevity_to_github.py -> main.py 로 사용
# - 위비티(네이밍/슬로건) 크롤링 + GitHub JSON 업서트
# - data/wevity_naming.json 가 '이미 존재'해야 동작
# - 신규 항목에 added_at(KST, ISO) 기록(알림 메시지에는 표시 X)
# - 텔레그램 알림(HTML / 링크미리보기 on)
# - 신규 알림은 오래된 것부터(최신이 맨 마지막에 오도록)

import os
import re, time, json, base64, html
from typing import List, Dict, Tuple, Optional
from urllib.parse import urljoin, urlparse, parse_qs
from datetime import datetime, timezone, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import cloudscraper
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────────────
# 설정: 새 계정/레포로 바꿔 넣기
# ─────────────────────────────────────────────────────────────────────
CONFIG = {
    "OWNER":  "LYJ1211",              # ← 새 깃허브 계정/조직
    "REPO":   "public",                # ← 레포 이름
    "PATH":   "data/wevity_naming.json",
    "BRANCH": "main",
    # 토큰: GH_PAT(있으면 우선) -> GITHUB_TOKEN(액션 기본) 순으로 사용
    "TOKEN":  (os.getenv("GH_PAT") or os.getenv("GITHUB_TOKEN") or "").strip(),
}

TG = {
    "BOT_TOKEN": os.getenv("TG_BOT_TOKEN", "").strip(),
    "CHAT_IDS": [c.strip() for c in os.getenv("TG_CHAT_IDS", "").split(",") if c.strip()],
}

# 범위/대기(차단 회피용)
SCRAPE_PAGE_FROM = 1
SCRAPE_PAGE_TO   = 3
SCRAPE_DELAY_SEC = 1.6

# ─────────────────────────────────────────────────────────────────────
# 크롤링
# ─────────────────────────────────────────────────────────────────────
BASE = "https://www.wevity.com/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.wevity.com/",
}

# requests 세션(retry)
_session = requests.Session()
_session.headers.update(HEADERS)
_session.mount(
    "https://",
    HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=0.7,
        status_forcelist=[429, 500, 502, 503, 504]
    ))
)

# cloudscraper (403 우회)
_scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
)

def _get_html(url: str) -> Optional[str]:
    try:
        r = _session.get(url, timeout=20)
        if r.status_code == 200 and "ms-list" in r.text:
            return r.text
        print(f"[WARN] HTTP {r.status_code} @ {url} (requests)")
    except Exception as e:
        print(f"[WARN] requests 예외: {e}")
    try:
        print("[INFO] cloudscraper 폴백 시도")
        r2 = _scraper.get(url, headers=HEADERS, timeout=25)
        if r2.status_code == 200:
            return r2.text
        print(f"[WARN] HTTP {r2.status_code} @ {url} (cloudscraper)")
    except Exception as e:
        print(f"[WARN] cloudscraper 예외: {e}")
    return None

def _parse_int(s: Optional[str]) -> Optional[int]:
    if not s: return None
    s = s.replace(",", "").strip()
    m = re.search(r"-?\d+", s)
    return int(m.group()) if m else None

def _extract_ix_from_url(u: str) -> Optional[str]:
    try:
        q = parse_qs(urlparse(u).query)
        v = q.get("ix", [None])[0]
        return str(v) if v else None
    except Exception:
        return None

def _parse_day_and_status(li) -> Tuple[Optional[int], Optional[str]]:
    day_div = li.select_one("div.day")
    if not day_div: return None, None
    dday = None
    m = re.search(r"D-?\s*(\d+)", day_div.get_text(" ", strip=True))
    if m: dday = int(m.group(1))
    st = None
    sp = day_div.select_one("span.dday")
    if sp: st = sp.get_text(strip=True)
    else:
        text = day_div.get_text(" ", strip=True)
        if "마감" in text: st = "마감"
    return dday, st

def _parse_item(li) -> Optional[Dict]:
    a = li.select_one("div.tit a")
    if not a: return None
    title = a.get_text(strip=True)
    href  = a.get("href", "")
    url   = urljoin(BASE, href)
    ix    = _extract_ix_from_url(url)

    organizer = li.select_one("div.organ")
    organizer = organizer.get_text(strip=True) if organizer else None

    dday, status = _parse_day_and_status(li)

    views_div = li.select_one("div.read")
    views = _parse_int(views_div.get_text(strip=True)) if views_div else None

    sub = li.select_one("div.sub-tit")
    category = None
    if sub:
        txt = sub.get_text(" ", strip=True)
        category = txt.split(":", 1)[1].strip() if ":" in txt else txt

    return {
        "title": title, "url": url, "ix": ix or url,
        "organizer": organizer, "dday": dday, "status": status,
        "views": views, "category": category,
    }

def scrape_wevity_naming(frm: int, to: int, delay_sec: float) -> List[Dict]:
    out: List[Dict] = []
    for gp in range(frm, to + 1):
        url = f"{BASE}?c=find&s=1&gub=1&cidx=25&gp={gp}"
        html_text = _get_html(url)
        if not html_text:
            time.sleep(delay_sec); continue
        soup = BeautifulSoup(html_text, "html.parser")
        lis = soup.select("div.ms-list ul.list > li")
        items = [li for li in lis if "top" not in (li.get("class") or [])]
        if not items: print(f"[WARN] 목록 미검출. 선택자 확인 필요: {url}")
        for li in items:
            it = _parse_item(li)
            if it: out.append(it)
        time.sleep(delay_sec)
    return out

# ─────────────────────────────────────────────────────────────────────
# GitHub Contents API
# ─────────────────────────────────────────────────────────────────────
def _api_url() -> str:
    return f"https://api.github.com/repos/{CONFIG['OWNER']}/{CONFIG['REPO']}/contents/{CONFIG['PATH']}"

def _headers() -> Dict:
    return {
        "Authorization": f"Bearer {CONFIG['TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def _normalize_ix(item: Dict) -> str:
    ix = item.get("ix") or item.get("url")
    if not ix: raise ValueError("각 항목에는 최소 'ix' 또는 'url'이 필요")
    return str(ix).strip()

def _get_current_strict() -> Tuple[str, List[Dict]]:
    params = {"ref": CONFIG["BRANCH"]} if CONFIG.get("BRANCH") else None
    r = requests.get(_api_url(), headers=_headers(), params=params, timeout=20)
    if r.status_code == 404:
        raise FileNotFoundError(
            f"GitHub에 '{CONFIG['PATH']}' 파일이 없음 (리포: {CONFIG['OWNER']}/{CONFIG['REPO']}, 브랜치: {CONFIG['BRANCH']})"
        )
    r.raise_for_status()
    data = r.json()
    content_b64 = data.get("content", "")
    text = base64.b64decode(content_b64).decode("utf-8") if content_b64 else "[]"
    try: arr = json.loads(text) if text.strip() else []
    except json.JSONDecodeError: arr = []
    return data["sha"], arr

def _put_json(new_list: List[Dict], prev_sha: str, message: str) -> Dict:
    body = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(new_list, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("utf-8"),
        "sha": prev_sha,
    }
    if CONFIG.get("BRANCH"): body["branch"] = CONFIG["BRANCH"]
    r = requests.put(_api_url(), headers=_headers(), json=body, timeout=25)
    r.raise_for_status()
    return r.json()

def merge_and_detect_new(current: List[Dict], scraped: List[Dict], *, added_at: Optional[str] = None, update_existing: bool = False):
    cur_map = { _normalize_ix(x): x for x in current }
    new_items = []
    for raw in scraped:
        ix = _normalize_ix(raw)
        item = dict(raw); item["ix"] = ix
        if ix not in cur_map:
            if "added_at" not in item and added_at:
                item["added_at"] = added_at
            new_items.append(item)
            cur_map[ix] = item
        elif update_existing:
            preserved_added = cur_map[ix].get("added_at")
            cur_map[ix] = {**cur_map[ix], **item}
            if preserved_added is not None:
                cur_map[ix]["added_at"] = preserved_added

    def sort_key(x):
        s = str(x.get("ix", ""))
        return int(s) if s.isdigit() else -1
    merged = sorted(cur_map.values(), key=sort_key, reverse=True)
    return merged, new_items

# ─────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────
def tg_send_message(token: str, chat_id: str, text: str, *, parse_mode="HTML", disable_preview=False):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,  # False면 미리보기 ON
    }
    r = requests.post(url, json=payload, timeout=15)
    if r.status_code == 429:
        time.sleep(1.2); r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()

def notify_telegram(new_items: List[Dict]):
    if not TG["BOT_TOKEN"] or not TG["CHAT_IDS"]:
        print("[알림] TG 설정 비어 있음 → 텔레그램 전송 생략"); return

    # 오래된 것 → 최신 순으로 발송
    def ix_as_int(x):
        s = str(x.get("ix",""))
        return int(s) if s.isdigit() else 10**12
    ordered = sorted(new_items, key=ix_as_int)

    for it in ordered:
        title = html.escape(it.get("title",""))
        url   = it.get("url","")
        safe_url = html.escape(url, quote=True)

        d     = it.get("dday")
        dtext = f"D-{d}" if isinstance(d, int) else "-"

        organizer = html.escape(it.get("organizer","") or "-")
        status    = html.escape(it.get("status","") or "-")
        views     = it.get("views")
        views_txt = f"{views:,}" if isinstance(views, int) else "-"

        text = (
            f"📣 <b>{title}</b>\n\n"
            f"주최: {organizer}\n"
            f"상태: {dtext} • {status} • 조회 {views_txt}\n\n"
            f"🔗 <a href=\"{safe_url}\">공고 바로가기</a>"
        )
        for cid in TG["CHAT_IDS"]:
            tg_send_message(TG["BOT_TOKEN"], cid, text, disable_preview=False)
            time.sleep(1.05)

def notify_print(new_items: List[Dict]):
    def ix_as_int(x):
        s = str(x.get("ix",""))
        return int(s) if s.isdigit() else 10**12
    ordered = sorted(new_items, key=ix_as_int)
    for it in ordered:
        d = it.get("dday"); dtext = f"D-{d}" if isinstance(d, int) else "-"
        print("\n".join([
            f"[신규] {it.get('title','')}",
            f"  • 주최: {it.get('organizer','') or '-'}",
            f"  • 상태: {dtext} • {it.get('status','') or '-'} • 조회 { (f'{it.get('views'):,}' if isinstance(it.get('views'),int) else '-') }",
            f"  • 링크: {it.get('url','')}",
        ])); print("-"*60)

# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────
def main():
    # 대상 JSON 반드시 존재
    try:
        sha, current = _get_current_strict()
    except FileNotFoundError as e:
        print(f"[중단] 대상 JSON 파일 없음 → {e}")
        print("GitHub UI에서 먼저 data/wevity_naming.json 에 '[]' 저장"); return
    except requests.HTTPError as e:
        print(f"[중단] GitHub 요청 실패: {e}"); return

    scraped = scrape_wevity_naming(SCRAPE_PAGE_FROM, SCRAPE_PAGE_TO, SCRAPE_DELAY_SEC)
    if not scraped:
        print("[중단] 크롤러 결과 비어 있음"); return

    for it in scraped:
        it["ix"] = it.get("ix") or it.get("url")

    now_kst = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    added_stamp = now_kst.isoformat()

    merged, new_items = merge_and_detect_new(current, scraped, added_at=added_stamp, update_existing=False)

    if new_items:
        notify_print(new_items)
        msg = f"chore: upsert {len(new_items)} new items, total {len(merged)} @ {datetime.now(timezone.utc).isoformat()}"
        try:
            resp = _put_json(merged, _get_current_strict()[0] if False else sha, msg)  # sha 재활용
            print(f"GitHub 저장 완료: {resp.get('content',{}).get('path')} sha={resp.get('content',{}).get('sha')}")
            notify_telegram(new_items)
        except requests.HTTPError as e:
            print(f"[중단] GitHub 업서트 실패: {e}")
    else:
        print("신규 항목 없음 (커밋 없음)")

if __name__ == "__main__":
    main()
