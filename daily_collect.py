# -*- coding: utf-8 -*-
"""매일 1회 재검증용 데이터 수집기. 판단은 하지 않습니다.

main.py(15분 루프)와 별개로 하루 1회만 실행됩니다. 여기서는 스킵·발송
판정을 하지 않고 **후보만 모아 state/daily_review.json에 저장**합니다.
실제 판단(진짜 놓친 사고인가, 회사명이 정말 빠졌는가)은 이 파일을 읽어서
Claude가 직접 합니다 — 자동 판정 스크립트를 하나 더 만들면, 그 스크립트가
또 틀렸을 때 아무도 모르는 문제가 반복되기 때문입니다.

main.py를 import하지 않는 이유
------------------------------
main.py는 모듈 최상단에서 KAKAO_REST_API_KEY 등을 필수 환경변수로 읽고,
없으면 그 자리에서 종료합니다. 이 스크립트는 카카오와 무관하게 돌아야
하므로, 필요한 최소 로직(fetch_body 등)만 아래에 그대로 복사해 씁니다.
main.py 쪽 로직이 바뀌면 이 파일도 손으로 맞춰야 한다는 뜻이니, 나중에
main.py의 필수 환경변수 검사를 함수 안으로 옮기게 되면 여기도 import로
바꿔 정리하는 게 좋습니다.

하는 일
-------
1) 느슨한 재검색 — filters.match()의 "장소 단어" 조건 없이, 제목에 사고
   단어(ACCIDENT_WORDS)만 있으면 통과시켜 최근 뉴스를 다시 모읍니다.
   그중 state/seen.json의 seen에 한 번도 안 걸린 링크만 추립니다.
   (검색어 자체는 main.py와 같은 GOOGLE_NEWS_QUERIES를 씁니다 — 검색어가
   놓친 것까지는 이 스크립트도 못 잡습니다. 후속 과제로 남겨둡니다.)
2) 회사명 변경 재확인 — 최근 3일 발송 기록(state/seen.json의 events)의
   원문을 다시 열어, 저장된 co 문자열이 지금 본문에도 있는지 대조합니다.
   이건 확정 판단이 아니라 "확인 필요" 신호만 남깁니다.
3) 결과를 state/daily_review.json에 저장합니다.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

try:
    import feedparser
except ImportError:
    feedparser = None

import config
import filters

KST = timezone(timedelta(hours=9))
STATE_PATH = os.path.join(os.path.dirname(__file__), "state", "seen.json")
REVIEW_PATH = os.path.join(os.path.dirname(__file__), "state", "daily_review.json")

LOOSE_HOURS = 24              # 재검색 범위
COMPANY_RECHECK_DAYS = 3      # 회사명 재확인 범위

_TAG_RE = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def now_utc():
    return datetime.now(timezone.utc)


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "events": []}


def article_key(title: str, link: str) -> str:
    norm = re.sub(r"[^가-힣a-zA-Z0-9]", "", title)[:40]
    return hashlib.sha1((norm or link).encode("utf-8")).hexdigest()[:16]


def fresh(published, hours) -> bool:
    if not published:
        return True
    return published > now_utc() - timedelta(hours=hours)


# ── 본문 가져오기 (main.py의 fetch_body를 그대로 복사. 위 docstring 참고) ──
_BODY_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_ANYTAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def fetch_body(link: str, limit: int = 3000) -> str:
    if not link:
        return ""
    try:
        r = requests.get(
            link,
            headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0 Safari/537.36"),
                "Accept-Language": "ko-KR,ko;q=0.9",
            },
            timeout=getattr(config, "BODY_TIMEOUT", 4),
            allow_redirects=True,
            stream=True,
        )
        if r.status_code != 200:
            r.close()
            return ""
        ctype = r.headers.get("Content-Type", "").lower()
        if ctype and "html" not in ctype and "text" not in ctype:
            r.close()
            return ""
        raw = b""
        for chunk in r.iter_content(16384):
            raw += chunk
            if len(raw) > 200_000:
                break
        r.close()
        enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else None
        if not enc:
            m = re.search(rb'charset=["\']?([\w-]+)', raw[:5000], re.I)
            enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
        try:
            page = raw.decode(enc, errors="replace")
        except LookupError:
            page = raw.decode("utf-8", errors="replace")
        text = _BODY_TAG_RE.sub(" ", page)
        text = _ANYTAG_RE.sub(" ", text)
        text = html.unescape(text)
        text = _SPACE_RE.sub(" ", text).strip()
        m = re.search(r"(지난\s*\d{1,2}일|\d{1,2}일\s*(오전|오후|낮|새벽))", text)
        if m and m.start() > 200:
            text = text[max(0, m.start() - 120):]
        return text[:limit]
    except Exception:                                       # noqa: BLE001
        return ""


# ── 느슨한 재검색 ────────────────────────────────────────────────
def fetch_rss(url: str, label: str):
    if feedparser is None:
        return []
    out = []
    try:
        feed = feedparser.parse(url)
        for e in feed.entries:
            pub = None
            if getattr(e, "published_parsed", None):
                pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            raw_title = clean(getattr(e, "title", ""))
            out.append({
                "title": (filters.strip_source_tail(raw_title)
                          if label == "구글뉴스" else raw_title),
                "summary": ("" if label == "구글뉴스"
                            else clean(getattr(e, "summary", ""))[:300]),
                "link": getattr(e, "link", ""),
                "published": pub,
                "source": label,
            })
    except Exception as exc:                                # noqa: BLE001
        print(f"[daily] {url}: {exc}", file=sys.stderr)
    return out


def collect_loose():
    items = []
    if getattr(config, "GOOGLE_NEWS_ENABLED", True):
        for q in config.GOOGLE_NEWS_QUERIES:
            url = ("https://news.google.com/rss/search?q="
                   + urllib.parse.quote(f"{q} when:1d")
                   + "&hl=ko&gl=KR&ceid=KR:ko")
            items += fetch_rss(url, "구글뉴스")
            time.sleep(0.1)
    for url in config.EXTRA_RSS_FEEDS:
        items += fetch_rss(url, "언론사RSS")
    print(f"[daily] 재검색 {len(items)}건")
    return items


def loose_match(title, summary=""):
    """filters.match()와 달리 장소 단어 조건 없이 제목의 사고 단어만 본다.

    일부러 넓게 잡아서, 원래 시스템(장소+사고 단어 동시 요구)이 놓쳤을 만한
    표현을 잡으려는 것입니다. 노이즈가 늘어나는 건 의도된 트레이드오프이고,
    최종 판단은 Claude가 seen.json과 대조해서 합니다.
    """
    title = title or ""
    summary = summary or ""
    for trap in getattr(config, "TRAP_WORDS", ()):
        title = title.replace(trap, " ")
        summary = summary.replace(trap, " ")
    if any(w in title for w in config.EXCLUDE_WORDS):
        return None
    hits = [w for w in config.ACCIDENT_WORDS if w in title]
    if not hits:
        return None
    text = f"{title} {summary}"
    if filters.is_foreign(config, text) and not filters.has_company(config, text):
        return None
    return hits


def find_missed(state):
    """seen에 한 번도 안 걸린, 사고로 보이는 최근 기사를 찾는다."""
    seen = state.get("seen", {})
    out = []
    for item in collect_loose():
        if not item["title"] or not item["link"]:
            continue
        if not fresh(item["published"], LOOSE_HOURS):
            continue
        hits = loose_match(item["title"], item["summary"])
        if not hits:
            continue
        key = article_key(item["title"], item["link"])
        if key in seen:
            continue          # 원래 시스템이 이미 봤던 기사(발송 여부 무관) — 대상 아님
        out.append({
            "title": item["title"],
            "link": item["link"],
            "source": item["source"],
            "hits": hits,
            "published": (item["published"].astimezone(KST).strftime("%m/%d %H:%M")
                          if item["published"] else ""),
        })
    return out


# ── 회사명 변경 재확인 ────────────────────────────────────────────
def recheck_companies(state):
    """최근 3일 발송 기록의 원문을 다시 열어 회사명 표기가 바뀌었는지 본다.

    확정 판단이 아니라 "확인 필요" 신호만 만듭니다 — 문자열 대조는 표현이
    바뀌면("○○건설" → "○○건설㈜") 오탐이 날 수 있어서, 최종 판단은 Claude가
    본문(body)을 직접 읽고 합니다.
    """
    cutoff = (now_utc() - timedelta(days=COMPANY_RECHECK_DAYS)).timestamp()
    events = [e for e in state.get("events", []) if e.get("ts", 0) > cutoff]
    out = []
    for ev in events:
        link = ev.get("link", "")
        if not link:
            continue
        body = fetch_body(link)
        if not body:
            continue
        co = (ev.get("co") or "").strip()
        if co:
            if co not in body:
                out.append({"title": ev.get("title", ""), "link": link,
                            "co": co, "flag": "회사명이 본문에서 안 보임 — 빠졌을 가능성",
                            "body": body[:800]})
        elif any(w in body for w in ("시공사", "원청", "시공은", "㈜")):
            out.append({"title": ev.get("title", ""), "link": link,
                        "co": "", "flag": "회사명 언급 가능성 — 재판단 필요",
                        "body": body[:800]})
    return out


def main():
    state = load_state()
    review = {
        "generated_at": now_utc().astimezone(KST).strftime("%Y-%m-%d %H:%M"),
        "missed_candidates": find_missed(state),
        "company_recheck": recheck_companies(state),
    }
    os.makedirs(os.path.dirname(REVIEW_PATH), exist_ok=True)
    with open(REVIEW_PATH, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=1)
    print(f"[daily] 놓친 후보 {len(review['missed_candidates'])}건 / "
          f"회사명 재확인 {len(review['company_recheck'])}건 → {REVIEW_PATH}")


if __name__ == "__main__":
    main()
