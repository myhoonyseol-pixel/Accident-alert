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

import ai_judge
import config
import filters


class _Disabled:
    """선택 기능 모듈이 없을 때 자리를 대신합니다.

    이메일이나 텔레그램을 안 쓰는 사람이 해당 파일을 안 올려도
    시스템 전체가 죽지 않도록 하기 위한 장치입니다.
    (실제로 mailer.py 를 안 올려 ModuleNotFoundError 로 전체가 멈춘 적 있음)
    """

    def __init__(self, name):
        self._name = name

    def enabled(self, cfg):
        return False

    def send(self, *a, **kw):
        return 0


try:
    import mailer
except ImportError:
    print("[안내] mailer.py 가 없어 이메일 발송을 건너뜁니다.", file=sys.stderr)
    mailer = _Disabled("mailer")

try:
    import telegram
except ImportError:
    print("[안내] telegram.py 가 없어 텔레그램 발송을 건너뜁니다.", file=sys.stderr)
    telegram = _Disabled("telegram")

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


def load_recipients():
    """받는 사람 목록을 만듭니다.

    '나에게 보내기'는 각자가 앱에 동의하면 그 사람 토큰으로 그 사람에게
    보낼 수 있습니다(카카오 공식: 일반 사용자도 사용 가능, 검수 불필요).
    동의한 본인에게만 가므로 정당한 방식입니다.

    KAKAO_REFRESH_TOKEN        본인 (필수)
    KAKAO_REFRESH_TOKENS       추가 인원 (선택). 줄바꿈이나 콤마로 구분.
                               "이름=토큰" 형식이면 로그에 이름이 찍힙니다.
                                 김과장=q3lj8n...
                                 박차장=a8fk2p...
    """
    people = [("본인", KAKAO_REFRESH_TOKEN)]
    extra = os.environ.get("KAKAO_REFRESH_TOKENS", "").strip()
    for chunk in re.split(r"[,\n]+", extra):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            label, tok = chunk.split("=", 1)
            label, tok = label.strip(), tok.strip()
        else:
            label, tok = f"수신자{len(people)}", chunk
        if tok:
            people.append((label, tok))
    return people


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
    state.setdefault("events", [])
    # 생존신호 이후 몇 건을 보냈는지. "새 속보 없음"이 거짓말이 되지 않게 셉니다.
    state.setdefault("sent_since_heartbeat", 0)
    return state


def recent_events(state):
    """최근 보낸 사고 목록. AI에게 '이거 후속 아니야?'를 묻기 위해 넘깁니다."""
    cutoff = (now_utc() - timedelta(days=getattr(config, "EVENT_MEMORY_DAYS", 5))).timestamp()
    fresh = [e for e in state.get("events", []) if e.get("ts", 0) > cutoff]
    state["events"] = fresh[-getattr(config, "EVENT_MEMORY_MAX", 15):]
    return state["events"]


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
            # 구글 뉴스는 항목마다 <source url="..."> 로 어느 매체가 썼는지 알려줍니다.
            # 이걸로 외국 매체(ko.laodong.vn 등)를 걸러냅니다.
            src = getattr(e, "source", None) or {}
            outlet = ""
            try:
                outlet = src.get("href", "") or ""
            except AttributeError:
                outlet = getattr(src, "href", "") or ""
            out.append({
                # 구글 뉴스 제목의 "- 매체명" 꼬리를 여기서 뗍니다.
                "title": (filters.strip_source_tail(raw_title)
                          if label == "구글뉴스" else raw_title),
                "summary": clean(getattr(e, "summary", "")),
                "link": getattr(e, "link", ""),
                "published": pub,
                "source": label,
                "outlet": outlet,
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
def get_access_token(refresh_token=None, label="본인", critical=True):
    payload = {
        "grant_type": "refresh_token",
        "client_id": KAKAO_REST_KEY,
        "refresh_token": refresh_token or KAKAO_REFRESH_TOKEN,
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
            if critical:
                sys.exit(1)
            return None
        r.raise_for_status()
    except SystemExit:
        raise
    except Exception as e:                              # noqa: BLE001
        print("!" * 64, file=sys.stderr)
        print(f"[{label}] 카카오 토큰 갱신 실패 — 이 사람에게는 알림이 안 갑니다.",
              file=sys.stderr)
        print("refresh_token 만료(약 60일)가 가장 흔한 원인입니다.", file=sys.stderr)
        print("get_token.py 로 새 토큰을 받아 해당 Secret을 교체하세요.", file=sys.stderr)
        print(f"원인: {e}", file=sys.stderr)
        print("!" * 64, file=sys.stderr)
        # 본인 토큰이 죽으면 알릴 방법이 없으므로 워크플로를 실패시켜
        # GitHub이 메일을 보내게 합니다. 다른 사람 토큰은 경고만 하고 넘어갑니다.
        if critical:
            sys.exit(1)
        return None

    data = r.json()
    if data.get("refresh_token"):
        print("=" * 64)
        print(f"[중요] {label} 의 새 refresh_token 발급됨. Secret을 교체하세요:")
        print(data["refresh_token"])
        print("=" * 64)
    return data["access_token"], int(data.get("refresh_token_expires_in", 0))


def broadcast(text: str, link: str = "", subject: str = "") -> int:
    """받는 사람 전원에게 카카오톡 + 이메일로 보냅니다.

    한 사람의 토큰이 죽어도 나머지에게는 정상 발송합니다.
    이메일은 카카오톡 '나에게 보내기'에 푸시 알림이 안 뜨는 문제를 메웁니다.
    """
    # 알림이 확실한 통로부터 내보냅니다.
    # 카카오톡 '나에게 보내기'는 푸시가 안 뜨므로 마지막입니다.
    if telegram.enabled(config):
        telegram.send(text, link, config)

    if mailer.enabled(config):
        body = text.replace("↓ 아래 [기사 보기] 를 누르세요", "")
        if link:
            body = body.rstrip() + f"\n\n▼ 원문 기사\n{link}"
        mailer.send(subject or "[안전속보] 건설현장 사고 감지", body.strip(), config)

    sent = 0
    for label, refresh in load_recipients():
        got = get_access_token(refresh, label, critical=(label == "본인"))
        if not got:
            continue
        token, ttl = got
        if send_kakao(token, text, link):
            sent += 1
        else:
            print(f"[kakao] {label} 발송 실패", file=sys.stderr)
        # 만료가 임박하면 그 사람에게 직접 알립니다.
        if 0 < ttl < 14 * 86400:
            send_kakao(token, f"🔑 카카오 토큰 만료 {ttl // 86400}일 남음 ({label})\n"
                              f"만료되면 이 알림이 끊깁니다. 담당자에게 알려주세요.")
        time.sleep(0.3)
    return sent


def send_kakao(token: str, text: str, link: str = "", button: str = "기사 보기"):
    # text 는 190자까지만 들어갑니다. 링크는 여기 넣지 말고 web_url 로 보내세요.
    # (web_url 은 길이 제한이 없어 긴 구글 뉴스 주소도 온전히 전달됩니다)
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
    # 기본은 끕니다. 버튼은 앱에 등록한 도메인으로만 동작하므로,
    # 항상 news.google.com 을 유지해야 모든 기사에서 버튼이 뜹니다.
    if not getattr(config, "RESOLVE_LINKS", False):
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
        if "google.com" not in final:
            return final
        # 구글 안에서만 맴돌면 페이지에서 실제 기사 주소를 찾아봅니다.
        for pat in (r'data-n-au="(https?://[^"]+)"',
                    r'<c-wiz[^>]*data-p="[^"]*?(https?://[^"&\\]+)',
                    r'rel="canonical"\s+href="(https?://(?!news\.google)[^"]+)"',
                    r'url=(https?://(?!news\.google)[^"\'&<]+)'):
            m = re.search(pat, r.text)
            if m:
                found = html.unescape(m.group(1))
                if "google.com" not in found:
                    return found
        # 못 찾아도 구글 주소를 그대로 씁니다. 눌러보면 기사로 넘어갑니다.
        return url
    except Exception as e:                              # noqa: BLE001
        print(f"[link] 원문 주소 확인 실패, 구글 링크 사용: {e}", file=sys.stderr)
        return url


def casualty_level(item):
    """인명피해 표현이 있으면 1, 없으면 0.

    초기 속보는 "인명피해 확인 중"처럼 사상자가 아직 안 적힌 경우가 많아
    이걸 발송 조건으로 걸지는 않습니다. 여러 건이 동시에 잡혔을 때
    사람이 다친 건을 먼저 보내기 위한 순서용입니다.
    """
    text = f"{item.get('title','')} {item.get('summary','')}"
    return 1 if any(w in text for w in getattr(config, "CASUALTY_WORDS", ())) else 0


def format_alert(item, place, hits, confidence, link, verdict=None):
    when = (item["published"].astimezone(KST).strftime("%m/%d %H:%M")
            if item["published"] else "시각미상")
    verdict = verdict or {}
    if verdict.get("v") == "update":
        # 이미 알린 사고인데 새 사실이 밝혀진 경우.
        # 같은 내용의 재탕 기사는 여기까지 오지 않고 AI가 걸러냅니다.
        chg = verdict.get("chg", "").strip()
        head = f"🔄 속보 업데이트\n[{chg}]" if chg else "🔄 속보 업데이트"
        title = filters.strip_source_tail(item["title"])[:80]
        return (f"{head}\n\n{title}\n\n"
                f"{when} · {item['source']}\n"
                f"↓ 아래 [기사 보기] 를 누르세요")

    if confidence == "company":
        mark, tag = "🔴", f"{place} · 주요건설사"
    elif confidence == "strong":
        mark, tag = "🚨", place
    else:
        mark, tag = "⚠️", f"{place}(추정)"
    if not casualty_level(item):
        tag += " · 피해규모 미확인"
    # 제목에 "- 매체명" 꼬리가 남아 있으면 카카오톡이 그 도메인을 링크로 만들어
    # 기사가 아니라 언론사 홈페이지로 가버립니다. 아래 [수집] 단계에서 이미 뗐지만
    # 혹시 남아 있으면 여기서 한 번 더 정리합니다.
    title = filters.strip_source_tail(item["title"])[:80]
    # ⚠️ 본문에 링크를 넣지 마세요.
    #    카카오 텍스트 메시지는 190자 제한인데 구글 뉴스 주소는 250자가 넘습니다.
    #    실제로 링크가 88자 잘려 눌러도 400 오류가 났습니다.
    #    링크는 아래 send_kakao 의 '기사 보기' 버튼에 실립니다(길이 제한 없음).
    return (f"{mark} 사고 속보 감지\n"
            f"[{tag} · {'/'.join(hits[:3])}]\n\n"
            f"{title}\n\n"
            f"{when} · {item['source']}\n"
            f"↓ 아래 [기사 보기] 를 누르세요")


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

        m = filters.match(config, item["title"], item["summary"],
                          item.get("outlet", ""))
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


def maybe_heartbeat(state):
    """하루 한 번 '살아있음'을 수신자 전원에게 알립니다.

    이게 없으면 알림이 없는 게 '사고가 없어서'인지
    '시스템이 죽어서'인지 구분할 수 없습니다.
    """
    if not config.HEARTBEAT_ENABLED:
        return False
    now_kst = datetime.now(KST)
    today = now_kst.strftime("%Y-%m-%d")
    if state.get("last_heartbeat") == today:
        return False
    # 지정 시각 '이후 첫 회차'에 보냅니다.
    #
    # 예전에는 `hour != 지정시각` 이면 건너뛰었는데, 그러면 그 한 시간 동안
    # 실행이 한 번도 없었을 때(루프 재시작·지연) 그날 생존 신호가 아예 안 갑니다.
    # 사용자는 시스템이 죽은 줄 알게 되죠. 그래서 '이후'로 바꿨습니다.
    if now_kst.hour < config.HEARTBEAT_HOUR_KST:
        return False
    # ⚠️ "새 속보 없음"은 사실일 때만 써야 합니다.
    #    이 회차에 보낼 게 없다고 해서 오늘 아무 일 없었던 게 아닙니다.
    #    (07:00에 사고 알림을 보내고 08:10에 "없음"이라고 보낸 적 있음)
    n = state.get("sent_since_heartbeat", 0)
    if n:
        line = f"지난 24시간 동안 {n}건 발송됨."
    else:
        line = "지난 24시간 동안 새 속보 없음."

    sent = broadcast(f"✅ 사고속보 감시 정상 작동 중\n{now_kst:%Y-%m-%d %H:%M} 기준\n{line}",
                     subject="[안전속보] 감시 시스템 정상 작동 중")
    if sent:
        state["last_heartbeat"] = today
        state["sent_since_heartbeat"] = 0
    return bool(sent)


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

    # AI 최종 판정 — 키워드가 걸러낸 후보만 넘깁니다.
    # 후보가 없으면 호출 자체가 없으므로 조용한 날은 비용 0원입니다.
    if picked:
        cap = getattr(config, "AI_MAX_CANDIDATES", 20)
        if len(picked) > cap:
            print(f"후보 {len(picked)}건 중 상한 {cap}건만 AI 판단", file=sys.stderr)
            picked = picked[:cap]

        events = recent_events(state)
        picked = ai_judge.judge(picked, config, events)

        # 같은 사고의 후속 알림은 상한을 둡니다.
        # 대형 사고는 후속 기사가 수십 건 쏟아지는데, 새 사실이 있는 건만
        # AI가 걸러주더라도 그것만으로 여러 번일 수 있기 때문입니다.
        limit = getattr(config, "EVENT_MAX_UPDATES", 2)
        filtered = []
        for item, place, hits, conf, verdict in picked:
            if verdict.get("v") != "update":
                filtered.append((item, place, hits, conf, verdict))
                continue
            idx = verdict.get("e", -1)
            ev = events[idx] if isinstance(idx, int) and 0 <= idx < len(events) else None
            if ev is None:
                filtered.append((item, place, hits, conf, verdict))
            elif ev.get("updates", 0) < limit:
                ev["updates"] = ev.get("updates", 0) + 1
                filtered.append((item, place, hits, conf, verdict))
            else:
                print(f"[후속상한] {item['title'][:40]} — 이 사고는 이미 "
                      f"{limit}회 후속 발송", file=sys.stderr)
        picked = filtered
        print(f"AI 판정 후 {len(picked)}건")

    need_token = bool(picked) or config.HEARTBEAT_ENABLED
    if not need_token:
        save_state(state)
        return

    people = load_recipients()
    print(f"수신자 {len(people)}명: {', '.join(n for n, _ in people)}")

    if picked:
        # 인명피해가 확인된 건을 먼저, 그다음 최신순.
        # 상한(MAX_SEND_PER_RUN)에 걸려 잘릴 때 사람이 다친 건이 밀리지 않도록.
        picked.sort(
            key=lambda c: (
                casualty_level(c[0]),
                c[0]["published"] or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        for item, place, hits, confidence, verdict in picked[:config.MAX_SEND_PER_RUN]:
            link = resolve_link(item["link"])
            title = filters.strip_source_tail(item["title"])[:70]
            n = broadcast(format_alert(item, place, hits, confidence, link, verdict),
                          link, subject=f"[안전속보] {title}")
            print(f"발송 {n}/{len(people)}명 — {item['title'][:40]}")
            state["sent_since_heartbeat"] = state.get("sent_since_heartbeat", 0) + 1
            # 새 사고면 '이미 보낸 사고' 목록에 올립니다.
            if verdict.get("v") == "new":
                state["events"].append({
                    "ts": now_utc().timestamp(),
                    "when": (item["published"] or now_utc()).astimezone(KST)
                            .strftime("%m/%d %H:%M"),
                    "title": filters.strip_source_tail(item["title"])[:90],
                    "updates": 0,
                })
        extra = len(picked) - config.MAX_SEND_PER_RUN
        if extra > 0:
            print(f"{extra}건은 상한으로 미발송(다음 실행에서 중복 제외됨)")
    else:
        maybe_heartbeat(state)

    save_state(state)


if __name__ == "__main__":
    main()
