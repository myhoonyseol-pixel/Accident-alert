# -*- coding: utf-8 -*-
"""건설현장 사고 뉴스 감시 → 카카오톡 '나에게 보내기'.

20분 주기로 실행되며, 처음 보는 사고만 즉시 발송합니다.
튜닝은 config.py에서, 판정 로직은 filters.py에서 합니다.
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

import config
import filters

try:
    import feedparser
except ImportError:                                     # RSS 없이도 동작하게
    feedparser = None

KST = timezone(timedelta(hours=9))
STATE_PATH = os.path.join(os.path.dirname(__file__), "state", "seen.json")
TAG_RE = re.compile(r"<[^>]+>")


def env(name, required=True):
    val = os.environ.get(name, "").strip()
    if required and not val:
        sys.exit(f"[설정오류] 환경변수 {name} 가 비어 있습니다. "
                 f"GitHub Secrets에 등록했는지 확인하세요.")
    return val


NAVER_ID = env("NAVER_CLIENT_ID", required=False)
NAVER_SECRET = env("NAVER_CLIENT_SECRET", required=False)
KAKAO_REST_KEY = env("KAKAO_REST_API_KEY")
KAKAO_REFRESH_TOKEN = env("KAKAO_REFRESH_TOKEN")
# 앱에서 Client Secret을 '사용함'으로 켰다면 토큰 갱신 때 반드시 함께 보내야 합니다.
# 빠뜨리면 KOE010(Bad client credentials)으로 갱신이 실패합니다.
KAKAO_CLIENT_SECRET = env("KAKAO_CLIENT_SECRET", required=False)


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text or "")).strip()


def now_utc():
    return datetime.now(timezone.utc)


# ── 상태 관리 ────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "initialized": False, "last_heartbeat": ""}

    # 예전 형식({key: timestamp})을 새 형식으로 옮깁니다.
    for k, v in list(state.get("seen", {}).items()):
        if not isinstance(v, dict):
            state["seen"][k] = {"ts": v, "tok": []}
    state.setdefault("last_heartbeat", "")
    return state


def save_state(state):
    cutoff = (now_utc() - timedelta(days=config.STATE_RETENTION_DAYS)).timestamp()
    state["seen"] = {k: v for k, v in state["seen"].items() if v.get("ts", 0) > cutoff}
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def article_key(title: str, link: str) -> str:
    norm = re.sub(r"[^가-힣a-zA-Z0-9]", "", title)[:40]
    return hashlib.sha1((norm or link).encode("utf-8")).hexdigest()[:16]


def fresh(published) -> bool:
    if not published:
        return True          # 발행시각 불명이면 통과시키고 중복제거에 맡김
    return published > now_utc() - timedelta(hours=config.MAX_AGE_HOURS)


# ── 수집 ─────────────────────────────────────────────────────
def parse_rfc822(value):
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


# 네이버가 검색 API를 NAVER API HUB로 옮기면서 주소와 헤더가 모두 바뀌었습니다.
# 예전 키(개발자센터)는 2027-06-30 까지만 동작합니다.
NAVER_ENDPOINTS = {
    "hub": (
        "https://naverapihub.apigw.ntruss.com/search/v1/news",
        ("X-NCP-APIGW-API-KEY-ID", "X-NCP-APIGW-API-KEY"),
    ),
    "legacy": (
        "https://openapi.naver.com/v1/search/news.json",
        ("X-Naver-Client-Id", "X-Naver-Client-Secret"),
    ),
}


def fetch_naver():
    """장소 키워드로만 최신순 조회하고, 사고 판정은 우리 코드가 합니다."""
    mode = getattr(config, "NAVER_MODE", "hub")
    if mode == "off":
        return []
    if not (NAVER_ID and NAVER_SECRET):
        print("[naver] 키가 없어 건너뜁니다 (구글뉴스 RSS만 사용)", file=sys.stderr)
        return []
    if mode not in NAVER_ENDPOINTS:
        sys.exit(f"[설정오류] NAVER_MODE 는 hub / legacy / off 중 하나여야 합니다: {mode}")

    url, (id_header, secret_header) = NAVER_ENDPOINTS[mode]
    headers = {id_header: NAVER_ID, secret_header: NAVER_SECRET}
    out = []
    for query in config.NAVER_QUERIES:
        try:
            r = requests.get(
                url,
                headers=headers,
                params={"query": query, "display": config.NAVER_DISPLAY, "sort": "date"},
                timeout=10,
            )
            if r.status_code == 429:
                print("[naver] 호출 한도 초과(429) — 잠시 쉬었다 갑니다", file=sys.stderr)
                time.sleep(2)
                continue
            if r.status_code in (401, 403):
                print(f"[naver] 인증 실패({r.status_code}). NAVER_MODE='{mode}' 와 "
                      f"발급받은 키 종류가 맞는지 확인하세요.", file=sys.stderr)
                return out
            r.raise_for_status()
            for it in r.json().get("items", []):
                out.append({
                    "title": clean(it.get("title", "")),
                    "summary": clean(it.get("description", "")),
                    "link": it.get("originallink") or it.get("link", ""),
                    "published": parse_rfc822(it.get("pubDate")),
                    "source": "네이버",
                })
        except Exception as e:                          # noqa: BLE001
            print(f"[naver] {query}: {e}", file=sys.stderr)
        time.sleep(config.NAVER_SLEEP)
    return out


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
                # 구글 뉴스 제목의 "- 매체명" 꼬리를 여기서 뗍니다.
                "title": (filters.strip_source_tail(raw_title)
                          if label == "구글뉴스" else raw_title),
                "summary": clean(getattr(e, "summary", "")),
                "link": getattr(e, "link", ""),
                "published": pub,
                "source": label,
            })
    except Exception as exc:                            # noqa: BLE001
        print(f"[rss] {url}: {exc}", file=sys.stderr)
    return out


def collect():
    items = fetch_naver()
    if config.GOOGLE_NEWS_ENABLED:
        for q in config.GOOGLE_NEWS_QUERIES:
            url = ("https://news.google.com/rss/search?q="
                   + urllib.parse.quote(f"{q} when:1d")
                   + "&hl=ko&gl=KR&ceid=KR:ko")
            items += fetch_rss(url, "구글뉴스")
            time.sleep(0.1)
    for url in config.EXTRA_RSS_FEEDS:
        items += fetch_rss(url, "언론사RSS")
    print(f"수집 {len(items)}건")
    return items


# ── 카카오 발송 ──────────────────────────────────────────────
def get_access_token():
    payload = {
        "grant_type": "refresh_token",
        "client_id": KAKAO_REST_KEY,
        "refresh_token": KAKAO_REFRESH_TOKEN,
    }
    if KAKAO_CLIENT_SECRET:
        payload["client_secret"] = KAKAO_CLIENT_SECRET

    try:
        r = requests.post(
            "https://kauth.kakao.com/oauth/token",
            data=payload,
            timeout=10,
        )
        if r.status_code == 401 and "KOE010" in r.text:
            print("!" * 64, file=sys.stderr)
            print("KOE010 — client_secret 문제입니다.", file=sys.stderr)
            print("앱에서 Client Secret을 '사용함'으로 켜 두었다면", file=sys.stderr)
            print("GitHub Secrets에 KAKAO_CLIENT_SECRET 을 등록해야 합니다.", file=sys.stderr)
            print("(앱 › 일반 › 플랫폼 키 › REST API 키 › 클라이언트 시크릿)", file=sys.stderr)
            print("!" * 64, file=sys.stderr)
            sys.exit(1)
        r.raise_for_status()
    except SystemExit:
        raise
    except Exception as e:                              # noqa: BLE001
        # 여기서 죽으면 카카오로 알릴 방법이 없습니다.
        # 워크플로를 실패시켜 GitHub이 메일을 보내게 만듭니다.
        print("!" * 64, file=sys.stderr)
        print("카카오 토큰 갱신 실패 — 알림이 중단됩니다.", file=sys.stderr)
        print("refresh_token 만료가 가장 흔한 원인입니다.", file=sys.stderr)
        print("get_token.py 를 다시 돌려 새 토큰을 발급하고", file=sys.stderr)
        print("GitHub Secrets의 KAKAO_REFRESH_TOKEN 을 교체하세요.", file=sys.stderr)
        print(f"원인: {e}", file=sys.stderr)
        print("!" * 64, file=sys.stderr)
        sys.exit(1)

    data = r.json()
    if data.get("refresh_token"):
        print("=" * 64)
        print("[중요] 새 refresh_token 발급됨. GitHub Secret을 교체하세요:")
        print(data["refresh_token"])
        print("=" * 64)
    return data["access_token"], int(data.get("refresh_token_expires_in", 0))


def send_kakao(token: str, text: str, link: str = "", button: str = "기사 보기"):
    payload = {"object_type": "text", "text": text[:190]}
    if link:
        payload["link"] = {"web_url": link, "mobile_web_url": link}
        payload["button_title"] = button
    else:
        payload["link"] = {}
    r = requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {token}"},
        data={"template_object": json.dumps(payload, ensure_ascii=False)},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"[kakao] {r.status_code} {r.text}", file=sys.stderr)
        return False
    return True


def resolve_link(url: str) -> str:
    """구글 뉴스 경유 주소를 실제 기사 주소로 풀어냅니다.

    실패하면 원래 주소를 그대로 씁니다. 구글 링크도 누르면 기사로 넘어가긴 하므로
    여기서 실패해도 알림 자체는 정상입니다.
    """
    if not getattr(config, "RESOLVE_LINKS", True):
        return url
    if "news.google.com" not in url:
        return url
    try:
        r = requests.get(
            url,
            timeout=getattr(config, "RESOLVE_TIMEOUT", 6),
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        final = r.url or url
        # 구글 안에서만 맴돌면 실패로 봅니다.
        if "google.com" in final:
            m = re.search(r'data-n-au="(https?://[^"]+)"', r.text)
            if m:
                return html.unescape(m.group(1))
            return url
        return final
    except Exception as e:                              # noqa: BLE001
        print(f"[link] 원문 주소 확인 실패, 구글 링크 사용: {e}", file=sys.stderr)
        return url


def format_alert(item, place, hits, confidence, link):
    when = (item["published"].astimezone(KST).strftime("%m/%d %H:%M")
            if item["published"] else "시각미상")
    mark = "🚨" if confidence == "strong" else "⚠️"
    tag = place if confidence == "strong" else f"{place}(추정)"
    # 제목에 "- 매체명" 꼬리가 남아 있으면 카카오톡이 그 도메인을 링크로 만들어
    # 기사가 아니라 언론사 홈페이지로 가버립니다. 아래 [수집] 단계에서 이미 뗐지만
    # 혹시 남아 있으면 여기서 한 번 더 정리합니다.
    title = filters.strip_source_tail(item["title"])[:80]
    return (f"{mark} 사고 속보 감지\n"
            f"[{tag} · {'/'.join(hits[:3])}]\n\n"
            f"{title}\n\n"
            f"{when} · {item['source']}\n"
            f"▼ 원문 기사\n{link}")


# ── 메인 ─────────────────────────────────────────────────────
def pick_candidates(state):
    """조건에 맞고 아직 안 보낸 기사만 골라냅니다."""
    seen = state["seen"]
    recent_tokens = [set(v.get("tok", [])) for v in seen.values() if v.get("tok")]
    picked = []

    for item in collect():
        if not item["title"] or not item["link"]:
            continue
        if not fresh(item["published"]):
            continue

        m = filters.match(config, item["title"], item["summary"])
        if not m:
            continue
        place, hits, confidence = m

        key = article_key(item["title"], item["link"])
        if key in seen:
            continue

        # 같은 사고를 다른 매체가 쓴 기사인지 확인
        toks = filters.tokenize(item["title"])
        if filters.is_duplicate(config, toks, recent_tokens):
            seen[key] = {"ts": now_utc().timestamp(), "tok": sorted(toks)}
            print(f"[중복] {item['title'][:50]}", file=sys.stderr)
            continue

        recent_tokens.append(toks)
        seen[key] = {"ts": now_utc().timestamp(), "tok": sorted(toks)}
        picked.append((item, place, hits, confidence))

    return picked


def maybe_heartbeat(state, token):
    """하루 한 번 '살아있음'을 알립니다.

    이게 없으면 알림이 없는 게 '사고가 없어서'인지
    '시스템이 죽어서'인지 구분할 수 없습니다.
    """
    if not config.HEARTBEAT_ENABLED:
        return False
    now_kst = datetime.now(KST)
    today = now_kst.strftime("%Y-%m-%d")
    if state.get("last_heartbeat") == today:
        return False
    if now_kst.hour != config.HEARTBEAT_HOUR_KST:
        return False
    ok = send_kakao(token, f"✅ 사고속보 감시 정상 작동 중\n{now_kst:%Y-%m-%d %H:%M} 기준\n"
                           f"어제부터 지금까지 새 속보 없음.")
    if ok:
        state["last_heartbeat"] = today
    return ok


def main():
    state = load_state()
    first_run = not state.get("initialized")

    picked = pick_candidates(state)
    print(f"신규 매칭 {len(picked)}건 (first_run={first_run})")

    if first_run:
        # 첫 실행은 과거 기사 폭탄을 막기 위해 기록만 하고 발송 생략
        state["initialized"] = True
        save_state(state)
        print("첫 실행: 시드만 저장하고 발송하지 않음")
        return

    need_token = bool(picked) or config.HEARTBEAT_ENABLED
    if not need_token:
        save_state(state)
        return

    token, refresh_ttl = get_access_token()

    if picked:
        picked.sort(key=lambda c: c[0]["published"] or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True)
        for item, place, hits, confidence in picked[:config.MAX_SEND_PER_RUN]:
            link = resolve_link(item["link"])
            send_kakao(token, format_alert(item, place, hits, confidence, link), link)
            time.sleep(0.3)
        extra = len(picked) - config.MAX_SEND_PER_RUN
        if extra > 0:
            print(f"{extra}건은 상한으로 미발송(다음 실행에서 중복 제외됨)")
    else:
        maybe_heartbeat(state, token)

    # 토큰 만료가 임박하면 카톡으로 미리 알립니다.
    if 0 < refresh_ttl < 14 * 86400:
        send_kakao(token, f"🔑 카카오 토큰 만료 {refresh_ttl // 86400}일 남음\n"
                          f"GitHub Actions 로그에서 새 refresh_token을 확인해\n"
                          f"Secrets를 교체하세요. 방치하면 알림이 끊깁니다.")

    save_state(state)


if __name__ == "__main__":
    main()
