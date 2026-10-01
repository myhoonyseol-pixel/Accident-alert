# -*- coding: utf-8 -*-
"""키워드 그물을 통과한 후보를 AI가 최종 판정합니다.

두 가지를 판단합니다.
  1) 진짜 알려야 할 사고인가          (통계·기획·캠페인 기사 걸러내기)
  2) 이미 보낸 사고의 후속 보도인가    (같은 사고 반복 알림 막기)

2번이 왜 필요한가
-----------------
속보는 정보가 부실합니다("천안 콘크리트 공장서 근로자 1명 숨져").
하루쯤 지나 회사명·원인이 밝혀진 기사가 나오는데, 키워드로는 이 둘이
같은 사고인 줄 모릅니다. 겹치는 고유 단어가 지역명 하나뿐이라서요.
실제로 천안 까뮤이앤씨 사망사고가 34시간 간격으로 두 번 발송됐습니다.

그렇다고 후속을 전부 막으면 안 됩니다. 대형 사고일수록 나중에 나오는
기사가 더 정확하거든요. 그래서 기준을 '새로운 사실이 있는가'로 잡습니다.

  통계·캠페인 기사           → skip   (안 보냄)
  같은 내용 다른 매체        → dup    (안 보냄)
  사상자 수 변경, 회사명 공개  → update (🔄 표시로 보냄)
  전혀 다른 사고             → new    (보냄)

skip 이 왜 따로 필요한가
-----------------------
처음에는 new/update/dup 셋뿐이었는데, 이러면 '알릴 사고가 아니다'라고
답할 칸이 없습니다. 실제로 2026-08-31 AI가 "안전점검 캠페인"이라고
정확히 알아보고도 new 로 찍어서, 도의원 현장점검 홍보 기사가 발송됐습니다.
같은 날 통계 기사는 dup 으로 찍어서 막혔고요. 칸이 없으니 매번 다르게
찍은 겁니다. AI 잘못이 아니라 답변 형식의 문제였습니다.

비용
----
후보가 있을 때만 호출하고 여러 건을 묶어 묻습니다. 월 2,000원 안팎입니다.

실패했을 때
-----------
AI 호출이 실패하면 제목 자체에 사망·중상·추락·붕괴 같은 **명백한 사고 신호가
있는 기사만** ⚠️ AI 미검증으로 발송합니다(guarded fail-open).
정책·수주·과거사고 후속처럼 제목만으로도 비사고가 분명한 기사는 발송하지 않습니다.

다만 이게 조용히 일어나면 안 됩니다. 2026-09-12 02:17 에 답을 읽다 실패해
AI 검증이 통째로 꺼진 채 "올해 하청노동자 5명 숨진 HD현대중공업…노동부
특별감독" 기사가 나갔는데, 받는 쪽에서는 AI가 승인한 건지 그냥 새어나온
건지 구분할 방법이 없었습니다. 그래서 실패하면 세 가지를 합니다.

  1) AI가 실제로 뭐라고 답했는지 로그에 남긴다 (안 남기면 원인을 못 봅니다)
  2) 한 번 더 물어본다 (실패했을 때만이라 평소 비용은 그대로입니다)
  3) 그래도 안 되면 제목 안전장치를 통과한 기사만 ai="fail" 로 보낸다
     → main.py 가 알림에 '⚠️ AI 미검증' 을 붙입니다
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import filters

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

SYSTEM_PROMPT = """너는 건설회사 안전보건 담당자다.
방금 일어난 산업현장 사고를 몇 분 안에 사내에 알리는 것이 임무다.
기사 제목과 본문 앞부분을 받는다. 본문이 없으면 제목만으로 판단한다.

━━ 먼저 · 이 기사의 핵심 사건을 한 문장으로 쓴다 ━━

what 칸에 "누가 어디서 무엇을" 을 25자로 적는다. 판정은 그 다음이다.
  "올해 하청노동자 5명 숨진 HD현대중공업…노동부 재감독"
    what: 노동부가 HD현대중공업 재감독 → 사고가 아니다. skip.
    '5명 숨진'은 회사를 꾸미는 말이고, 실제 서술어는 '재감독'이다.

한 기사에 여러 지역·여러 사고가 실린 모음 기사는 **제목이 가리키는 사건
하나만** 판단한다. 요약·본문에 같이 실린 다른 사건은 판단에 쓰지 마라.
  "영천 지방도서 트럭이 갓길 정차 SUV 추돌…1명 사망" (본문 뒤에 포항 공사장 사고)
    what: 영천 트럭·SUV 추돌 → 교통사고. skip.

━━ 판단 1 · 감시 대상 산업·작업인가 ━━

가장 먼저 본다. 대상이 아니면 사망자가 있어도 skip 한다.
아래 목록은 예시다. 목록에 없어도 성격이 같으면 똑같이 판단하라.

[대상]
- 건설·토목·건축·철거·해체·신축·증축·리모델링 현장
- 소규모 설치·시공·보수·수선 작업 (창호·지붕·간판·배관·전기 등)
- 제조업 공장·생산시설, 물류센터·물류창고, 산업단지 사업장
- 조선소·제철소·발전소·정유·석유화학·플랜트, 채석장·광산
- 해외는 국내 건설사 시공 현장이거나 한국인이 피해를 입은 경우

[대상 아님]
- 음식점·숙박·마사지·미용·판매점·병원·학교·사무실 등 일반 영업·서비스 시설
- 농업·어업, 화물운송·차량정비, 철도 운행·항만 하역
- 교통사고·범죄·산불·자연재해·군 작전 중 사고
- 거주자·손님·이용객 사고

건물 용도가 [대상 아님]이어도, 거기서 건설·설치·보수 작업을 하다 난 사고는 대상이다.
  마사지업소 직원이 영업 중 화재로 사망 → skip
  마사지업소 인테리어 공사 중 작업자 추락 → 대상
  병원 증축공사 중 근로자 추락 → 대상

━━ 판단 2 · 사고 자체를 보도한 기사인가 ━━

감독·수사·재판·조사·통계·추모·대책·교육·캠페인·국감 등 사고 이후의 일 → skip.
성명·촉구·기자회견·유가족 호소·기획·분석·칼럼처럼 지난 사고를 다시 다룬 기사도 skip.
단, 이미 보낸 사고의 후속이면 판단 5를 먼저 본다. 새 사실이 있으면 update 다.

며칠 지난 사고라도 첫 보도면 알린다. 본문에 발생 일시가 있으면 occurred 에 적는다.

━━ 판단 3 · 피해가 알림 문턱을 넘는가 ━━

사망·중상·위독·의식불명·심정지·매몰·실종·피해규모 미상 → 통과
단순 부상만 확인됨 → skip (몇 명이든)
건설현장 구조물 사고(붕괴·폭발·타워크레인 전도)는 인명피해가 없어도 통과.

━━ 판단 4 · 일하던 사람이 다쳤는가 ━━

장소 이름이 아니라 피해자가 그 일을 하던 중이었는지로 본다.
  아파트 주민 화재 사망 → skip
  아파트 창호공사 작업자 추락 → 대상
  공사장에서 고철 싣던 '고물상' 사망 → 대상 (실제로 하청 사업주였다)

━━ 판단 5 · 이미 보낸 사고인가 ━━

'이미 보낸 사고' 목록이 함께 온다. 단어가 겹치는지로 보지 말고
발생일·지역·사업장/회사·피해자·사고 형태를 맞춰본다. 이 중 여럿이 일치하면
표현이 달라도 같은 사고다. 아래 셋은 같은 사고다.
  집게차 작업 도중 철제 구조물에 충돌…40대 심정지
  광주 아파트 공사장서 철제 구조물에 맞은 40대 숨져
  북구 아파트 공사장서 H빔에 머리 맞아 40대 고물상 숨져
  ('광주'와 '북구'(광주 북구), '철제 구조물'과 'H빔'은 같은 것일 수 있다)

같은 사고라면 —
  update  사상자 수 변화 / 시공사·원청 최초 공개 / 매몰·실종 종결. 이 셋뿐이다.
          목록에 [시공사: ○○] 가 이미 있으면 그 회사는 최초 공개가 아니다.
  dup     그 외 전부. 작업중지·수사착수·압수수색·원인규명도 dup 이다.

**회사명이 처음 나온 기사는 절대 버리지 마라.** 언론이 나중에 이름을 빼기도 한다.
  "전남광주 현대엔지니어링 시공 현장에서 40대 사망 중대재해 발생" 을
  사후보도라며 skip 한 적이 있다. 광주 H빔 사고의 시공사가 처음 밝혀진 기사였다.

━━ 판단이 애매하면 ━━

[대상 아님]에 해당한다는 근거가 있으면 skip 한다.
대상이라는 근거도, 대상이 아니라는 근거도 없으면(본문 없음·초기 속보) new 로 한다.
같은 사고인지 새 사고인지 애매하면 new 로 한다. 같은 지역에서 다른 사고가 났을 수 있다.

━━ 해외 기사 ━━

한국 행정구역 이름이 하나도 없고 낯선 외국 지명이 있으면 해외로 본다.
  예: '득토 종합병원'(베트남 하띤성), '빈즈엉 공단', '앙헬레스 9층 건물'
매체 주소가 함께 온다. 외국 신문의 한국어판이 섞여 들어온다.

━━ 한국어 주의 ━━

이름 속에 우연히 든 사고 단어에 속지 마라.
'대전도시공사'는 기관 이름이지 '전도'가 아니다. '구미국가산단'은 '미국'이 아니다.

━━ 출력 ━━
아래 JSON 배열만 출력한다. 배열 뒤에 아무것도 쓰지 마라.

[{"i":0,"cid":"받은 기사 ID","t":"제목 앞 12글자","what":"핵심 사건","v":"넷 중 하나","e":-1,"occurred":"","co":"","chg":"","why":"판단 이유"}]

  기사마다 하나씩, 빠짐없이, 받은 순서대로 답한다.

  cid       입력의 기사 ID를 한 글자도 바꾸지 말고 그대로 베낀다 (답과 기사를 짝짓는 1순위 키)
  t         제목 앞 12글자를 그대로 베낀다 (cid가 틀렸을 때의 보조 확인용)
  what      핵심 사건 25자. 판정 전에 먼저 쓴다. 항상 채운다
  v         skip · dup · update · new 넷 중 하나. 판정은 반드시 이 칸에만 쓴다
  e         dup·update면 사건 번호, 아니면 -1
  occurred  본문의 사고 발생 일시 "09-11 19:35" 또는 "09-11", 없으면 ""
            (기사 작성일이 아니라 사고가 난 때다)
  co        본문의 시공사·원청 이름, 없으면 ""
  chg       update일 때 무엇이 달라졌는지 25자
  why       판단 이유 20자. 결정한 판단 번호로 시작한다 (예: "판단1·음식점", "판단3·단순부상")"""


def _extract_json(text: str):
    """AI 답에서 **첫 번째로 완성된** JSON 배열만 꺼냅니다.

    왜 '첫 번째'이고 '완성된' 인가
    ---------------------------
    예전 코드는 첫 '[' 부터 **마지막** ']' 까지 통째로 잘라 썼습니다.

        start, end = text.find("["), text.rfind("]")

    AI가 답만 주고 끝내면 문제가 없습니다. 그런데 답 뒤에 설명을 한 줄
    덧붙이거나 배열을 한 번 더 출력하면, 그 뒷부분의 ']' 까지 끌려와서
    읽기가 통째로 실패합니다. 2026-09-12 02:17 에 실제로 이렇게 터졌습니다.

        [{"i":0,"v":"...","e":-1,"why":"...","chg":""}]   ← 55자, 정상
        (AI가 여기서 안 멈추고 한 줄 더 씀)                  ← 56번째 글자
        → Extra data: line 2 column 1 (char 56)

    첫 줄만 읽었으면 아무 문제가 없었습니다. 그런데 읽기가 실패하면
    '전부 발송'으로 넘어가기 때문에, 이 한 글자 때문에 AI 검증이 사라진 채
    기사가 나갔습니다. 그래서 괄호 짝을 세어 배열이 닫히는 순간 멈추고,
    **뒤에 무엇이 붙든 무시합니다.**

    문자열 안의 괄호에 속지 않도록 따옴표 안쪽은 세지 않습니다.
    제목에 '[속보]' 같은 대괄호가 들어오는 일이 실제로 있습니다.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)

    start = text.find("[")
    if start == -1:
        raise ValueError(f"JSON 배열을 찾을 수 없음: {text[:150]}")

    depth = 0
    in_str = False      # 지금 따옴표 안에 있는가
    escaped = False     # 바로 앞 글자가 역슬래시였는가
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:                      # 바깥 배열이 닫혔다
                return json.loads(text[start:i + 1])

    raise ValueError(f"배열이 끝나지 않음: {text[:150]}")


def judge(candidates, cfg, recent_events=None):
    """candidates: [(item, place, hits, confidence), ...]
    recent_events: [{"title":..., "when":..., "updates":int}, ...] 최근 보낸 사고

    반환: [(item, place, hits, confidence, verdict), ...]
      verdict = {"v": "new"|"update", "e": 사건번호, "chg": "바뀐 점"}
      skip·dup 으로 판정된 건은 목록에서 빠집니다.
      알 수 없는 값이 오면 new 로 봅니다(놓치는 것보다 헛알림이 낫다).
    """
    recent_events = recent_events or []

    if not getattr(cfg, "AI_ENABLED", False) or not candidates:
        return [(c[0], c[1], c[2], c[3], {"v": "new", "e": -1, "chg": ""})
                for c in candidates]

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        # 열쇠가 없으면 AI는 못 쓰지만, 잡기사 전체를 속보로 보내지는 않습니다.
        print("[ai] ANTHROPIC_API_KEY 가 없어 제목 안전장치로 판단합니다", file=sys.stderr)
        out = []
        for cand in candidates:
            safe = _safe_fail_open(cand, cfg, "ANTHROPIC_API_KEY 없음")
            if safe is not None:
                out.append(safe)
        return out

    # 나눠서 묻습니다. (2026-09-22)
    # 한 번에 20건을 본문째 물었더니 AI가 번호를 헷갈려 답이 한 칸씩 밀렸고,
    # '경북 화재' 기사에 대한 발송 판정이 '서문시장 재건축' 기사에 붙어 나갔습니다.
    # 호출이 늘어도 지시문(약 3천 자)을 다시 보내는 값이라 차이는 몇 원입니다.
    size = max(1, int(getattr(cfg, "AI_BATCH_SIZE", 8)))
    kept = []
    # 앞 묶음에서 NEW로 판정한 사고를 다음 묶음의 '이미 보낸 사고'에 넣어서 묻습니다.
    # (2026-09-30 현대건설 서초 현장 사고가 서로 다른 묶음에서 NEW 3건으로 나왔음)
    context = list(recent_events)
    run_new = []
    for k in range(0, len(candidates), size):
        rows = _judge_batch(candidates[k:k + size], cfg, context, api_key)
        rows = merge_same_run(rows, context, len(recent_events), run_new, "ai")
        kept += [r for r in rows if r[4].get("v") != "dup"]
    return kept


_KST = timezone(timedelta(hours=9))


def _run_event(row):
    """이번 회차에 NEW로 판정된 기사를 '이미 보낸 사고' 목록 형식으로 바꿉니다."""
    item, v = row[0], row[4]
    title = item.get("title") or ""
    pub = item.get("published")
    when = v.get("occurred") or (pub.astimezone(_KST).strftime("%m/%d %H:%M") if pub else "")
    return {"title": title[:90], "when": when, "co": v.get("co") or "",
            "tok": sorted(filters.tokenize(title)), "same_run": True}


def merge_same_run(rows, context, base, run_new, log="ai"):
    """한 묶음의 판정을 같은 회차 앞 묶음들의 NEW와 합칩니다. Claude·GPT 공용.

    context  : AI에게 보여준 '이미 보낸 사고' (실제 기억 + 이번 회차 NEW). 여기에 추가합니다.
    base     : 실제 사건 기억의 길이. 이 번호 이상은 이번 회차에 새로 붙인 사고입니다.
    run_new  : 이번 회차 NEW 행. context[base + i] 가 run_new[i] 입니다.

    같은 회차의 앞 NEW를 가리키는 update는 dup으로 바꿉니다. 아직 보내기 전인 사고에
    '후속'을 따로 보낼 이유가 없고, 새로 밝혀진 회사명은 앞 NEW 알림에 합칩니다.
    """
    for row in rows:
        v = row[4]
        try:
            e = int(v.get("e", -1))
        except (TypeError, ValueError):
            e = -1
        if v.get("v") in ("update", "dup") and base <= e < base + len(run_new):
            first = run_new[e - base][4]
            if v.get("co") and not first.get("co"):
                first["co"] = v["co"]
            if v.get("v") == "update":
                print(f"[{log}] 같은 회차 동일사고 × {row[0].get('title', '')[:44]} "
                      f"— 앞 묶음 NEW와 합침", file=sys.stderr)
            v["v"], v["decision"], v["type"] = "dup", "NO_ALERT", "DUP"
        elif v.get("v") == "new":
            run_new.append(row)
            context.append(_run_event(row))
    return rows


def _norm(s) -> str:
    """제목 비교용 — 글자·숫자만 남깁니다. (따옴표·괄호·띄어쓰기 차이 무시)"""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(s or ""))


def _candidate_id(item) -> str:
    """AI 답을 원 기사에 붙이는 안정적인 짧은 ID.

    제목 앞글자는 같은 보도자료 묶음에서 쉽게 겹칩니다. 제목+링크로 만든 ID를
    입력에 같이 주고 그대로 돌려받으면 한 답이 빠져도 뒤 기사로 밀리지 않습니다.
    """
    raw = f"{item.get('title','')}\n{item.get('link','')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _date_key(value: str):
    """'09-11 19:35', '09/11 19:35' 등에서 (월, 일)만 꺼냅니다."""
    m = re.search(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})(?!\d)", str(value or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


# 본문 발생일이 이보다 오래된 사고는 새 알림으로 보내지 않습니다.
# 2026-09-30 대원산업(08-28)·대불산단(09-08) 등 지난 사고의 성명·기획 기사가
# '이미 보낸 사고' 기억(5일) 밖이라 new 로 나간 일이 있었습니다.
OLD_ACCIDENT_DAYS = 7


def occurred_age_days(occurred, now=None):
    """AI가 적은 발생일('09-11 19:35')이 오늘(KST)로부터 며칠 전인지. 모르면 None."""
    md = _date_key(occurred)
    if not md:
        return None
    today = (now or datetime.now(_KST)).astimezone(_KST).date()
    try:
        d = today.replace(month=md[0], day=md[1])
    except ValueError:
        return None
    if d > today + timedelta(days=1):      # 1월에 받은 '12-30' 같은 작년 날짜
        try:
            d = d.replace(year=d.year - 1)
        except ValueError:
            return None
    return (today - d).days


def too_old(occurred, now=None) -> bool:
    age = occurred_age_days(occurred, now)
    return age is not None and age > OLD_ACCIDENT_DAYS


def _safe_fail_open(cand, cfg, reason=""):
    """AI가 끝내 답하지 못했을 때도 명백한 사고 제목만 살립니다.

    fail-open 자체는 유지하되, 수주·양형·감독·유족합의·과거사고 회고처럼
    제목만 읽어도 '방금 난 사고'가 아닌 것은 Telegram으로 흘리지 않습니다.
    """
    item, _place, _hits, _conf = cand
    title = str(item.get("title") or "")

    # 제목에 과거 연도가 박혀 있으면 현재 속보로 보지 않습니다.
    years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", title)]
    if years and min(years) < datetime.now().year:
        print(f"[ai] 실패안전 제외 × {title[:44]} — 과거 연도 기사 ({reason})", file=sys.stderr)
        return None

    post_words = (
        "양형", "징역", "처벌", "사법처리", "법위반", "특별감독", "감독결과",
        "국감", "국정감사", "영업정지", "본계약", "수주", "유족", "합의", "장례",
        "사과", "캠페인", "교육", "안전점검", "대책 발표", "재발 방지", "재발방지",
    )
    if any(w in title for w in post_words):
        print(f"[ai] 실패안전 제외 × {title[:44]} — 사후·정책·경영 기사 ({reason})", file=sys.stderr)
        return None

    strong = (
        "사망", "숨져", "숨진", "숨졌", "심정지", "중상", "위독", "의식불명",
        "매몰", "실종", "추락", "끼임", "끼여", "깔려", "협착", "붕괴", "무너져",
        "폭발", "화재", "감전", "질식", "전도", "낙하", "참변",
    )
    if not any(w in title for w in strong):
        print(f"[ai] 실패안전 제외 × {title[:44]} — 명백한 사고 신호 없음 ({reason})", file=sys.stderr)
        return None

    print(f"[ai] 실패안전 발송 ⚠ {title[:44]} — 명백한 사고 제목 ({reason})", file=sys.stderr)
    return (*cand, {"v": "new", "e": -1, "chg": "", "ai": "fail"})


def _same_event_evidence(cand, ev, occurred, cfg) -> bool:
    """UPDATE가 실제 기존 사건과 연결된다는 코드 측 근거가 있는지 확인합니다."""
    if ev is None:
        return False
    item = cand[0]
    if item.get("followup_hint"):
        return True

    # AI가 본문에서 뽑은 사고 발생일과 기존 알림 발생일이 같으면 강한 근거입니다.
    d1, d2 = _date_key(occurred), _date_key(ev.get("when", ""))
    if d1 and d2 and d1 == d2:
        return True

    now_tok = filters.tokenize(item.get("title", ""))
    old_tok = set(ev.get("tok", [])) or filters.tokenize(ev.get("title", ""))
    return filters.same_event(cfg, now_tok, old_tok) > 0


def _validate_update(cand, v, recent_events, cfg, company, occurred):
    """AI가 직접 UPDATE라고 해도 기존 사건 동일성을 코드가 한 번 더 검증합니다."""
    try:
        e = int(v.get("e", -1))
    except (TypeError, ValueError):
        e = -1
    ev = recent_events[e] if 0 <= e < len(recent_events) else None
    if ev is None:
        return False, "기존 사건 번호가 유효하지 않음", e

    if not _same_event_evidence(cand, ev, occurred, cfg):
        return False, "기존 사고와 동일하다는 날짜·제목 근거 없음", e

    change_text = f"{v.get('chg','')} {v.get('why','')}"
    company_words = ("시공사", "원청", "회사", "업체", "시공 정보", "현장 정보")
    casualty_words = ("사망", "중상", "위독", "사상자", "매몰", "실종", "구조", "종결")
    company_only = company and any(w in change_text for w in company_words) \
        and not any(w in change_text for w in casualty_words)

    # '시공사 최초 공개' 류는 본문 속 비교대상 회사로 승격시키지 않습니다.
    if company_only:
        nco = _norm(company)
        title_n = _norm(cand[0].get("title", ""))
        old_title_n = _norm(ev.get("title", ""))
        old_co_n = _norm(ev.get("co", ""))
        if not nco or nco not in title_n:
            return False, "시공사명이 현재 기사 제목에 없음", e
        if nco in old_title_n or (old_co_n and nco == old_co_n):
            return False, "기존 사고에 이미 같은 시공사 기록", e

    return True, "", e


def _judge_batch(candidates, cfg, recent_events, api_key, retry_missing=True):
    """기사 몇 건을 한 번에 묻습니다. judge() 가 나눠서 부릅니다."""
    parts = []
    if recent_events:
        parts.append("[이미 보낸 사고]")
        for j, ev in enumerate(recent_events):
            co = f' [시공사: {ev["co"]}]' if ev.get("co") else ""
            parts.append(f'{j}. ({ev.get("when","")}) {ev.get("title","")[:90]}{co}')
        parts.append("")
    parts.append("[판단할 기사]")
    for i, (item, place, hits, _conf) in enumerate(candidates):
        title = (item.get("title") or "")[:120]
        summary = (item.get("summary") or "")[:200]
        outlet = (item.get("outlet") or "").replace("https://", "")[:40]
        cid = _candidate_id(item)
        line = (f'{i}. ID: {cid}\n   제목: {title}\n   요약: {summary}\n'
                f'   매체: {outlet or "미상"}\n'
                f'   걸린단어: {place} / {"·".join(hits[:4])}')
        # 기사 본문 앞부분. main.py 가 가져다 넣습니다.
        #
        # 이게 없으면 AI가 보는 것이 정규식이 본 것과 똑같아집니다.
        # 제목만으로는 다음 셋을 절대 알 수 없습니다.
        #   · 사고가 언제 났는가   (제목에 날짜를 쓰는 기사는 거의 없다)
        #   · 시공사가 어디인가    (본문 두세 문장 뒤에 나온다)
        #   · 다친 사람이 작업자인가 (제목의 '고물상'이 실은 하청 사업주였다)
        body = (item.get("body") or "").strip()
        if body:
            line += f'\n   본문: {body[:1000]}'
        # 몇 개 매체가 동시에 다루는지. 실제 사고일수록 여러 곳이 함께 씁니다.
        # 다만 홍보 보도자료도 여러 매체에 뿌려지므로 절대 기준은 아닙니다.
        cov = item.get("coverage", 0)
        if cov >= 2:
            line += f'\n   동시 보도: {cov}개 기사'
        # 키워드 중복 판정에서 걸렸지만 새 사실이 보여 넘긴 기사입니다.
        hint = item.get("followup_hint")
        if hint:
            line += f'\n   ※ 이미 보낸 사고의 후속으로 보임 — {hint}'
        parts.append(line)
    user_msg = "\n".join(parts)

    # 두 번까지 시도합니다. 실패했을 때만 한 번 더 부르는 것이라
    # 평소 비용은 그대로입니다. 일시적인 통신 오류나 AI가 형식을 어긴
    # 경우는 다시 물으면 대개 해결됩니다.
    body, verdicts, raw, last_err = None, None, "", None
    for attempt in (1, 2):
        try:
            r = requests.post(
                API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": getattr(cfg, "AI_MODEL", "claude-haiku-4-5-20251001"),
                    # 칸이 7개로 늘며 답 한 줄이 약 147자가 됐습니다. 1200이면 후보 5건쯤에서
                    # 답이 중간에 잘려 판정이 통째로 실패합니다(=전부 무검증 발송).
                    # 요금은 실제로 쓴 만큼만 나가므로 한도를 올려도 비용은 같습니다.
                    "max_tokens": 4000,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_msg}],
                },
                timeout=getattr(cfg, "AI_TIMEOUT", 30),
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            body = r.json()
            raw = (body.get("content") or [{}])[0].get("text", "") or ""
            verdicts = _extract_json(raw)
            break
        except Exception as e:                          # noqa: BLE001
            last_err = e
            print(f"[ai] {attempt}차 시도 실패: {e}", file=sys.stderr)
            # AI가 실제로 뭐라고 답했는지 남깁니다. 이게 없으면 다음에
            # 같은 일이 나도 원인을 짚을 수 없습니다.
            if raw:
                print(f"[ai] AI 원문 ↓\n{raw[:500]}", file=sys.stderr)
            raw = ""
            if attempt == 1:
                time.sleep(2)

    if verdicts is None:
        print(f"[ai] 판정 실패 — 제목 안전장치로 fail-open 여부를 결정합니다: "
              f"{last_err}", file=sys.stderr)
        out = []
        for cand in candidates:
            safe = _safe_fail_open(cand, cfg, f"API/형식 실패: {last_err}")
            if safe is not None:
                out.append(safe)
        return out

    # ── 답과 기사를 **고유 ID 우선, 제목 보조**로 짝짓습니다 ──────────
    # 제목 앞글자는 보도자료 묶음에서 겹칠 수 있으므로 cid를 1순위로 씁니다.
    # cid가 없거나 모델이 잘못 옮긴 경우에만 Claude가 추가한 제목(t) 보정을 사용합니다.
    cids = [_candidate_id(c[0]) for c in candidates]
    ntitles = [_norm(c[0].get("title"))[:80] for c in candidates]
    by_i = {}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        try:
            i = int(v.get("i", -1))
        except (TypeError, ValueError):
            i = -1

        k = None
        cid = str(v.get("cid", "")).strip()
        if cid:
            matches = [x for x, known in enumerate(cids) if x not in by_i and known == cid]
            if len(matches) == 1:
                k = matches[0]
                if k != i:
                    print(f"[ai] ID 짝 보정 — {i}번으로 온 답을 cid로 찾아 {k}번 기사에 붙임",
                          file=sys.stderr)
            elif not matches:
                print(f"[ai] cid 불일치 — '{cid}', 제목 보조 매칭 시도", file=sys.stderr)

        if k is None:
            t = _norm(v.get("t", ""))[:12]
            if len(t) >= 4:
                matches = [x for x in range(len(candidates)) if x not in by_i and t in ntitles[x]]
                if matches:
                    k = i if i in matches else min(matches, key=lambda x: abs(x - i))
                    if k != i:
                        print(f"[ai] 제목 짝 보정 — {i}번으로 온 답을 제목으로 찾아 {k}번 기사에 붙임",
                              file=sys.stderr)

        if k is None and 0 <= i < len(candidates) and i not in by_i:
            # 구버전 응답 호환용 최후 수단. 새 프롬프트에서는 cid가 항상 있어야 합니다.
            k = i
            print(f"[ai] 번호 fallback — cid/t 매칭 실패, {i}번에 임시 연결", file=sys.stderr)

        if k is not None and k not in by_i:
            by_i[k] = v

    VALID = ("skip", "dup", "update", "new")
    kept = []
    for i, cand in enumerate(candidates):
        title = (cand[0].get("title") or "")[:44]

        # ── 답이 멀쩡한지부터 확인합니다 ──────────────────────
        # 예전에는 답이 없거나 이상하면 **조용히 new(발송)** 로 처리했습니다.
        # 2026-09-17 국정감사 통계 기사, 09-21 HL만도 감독결과 기사가
        # 이렇게 나갔습니다. AI는 skip 이라고 답했는데 칸을 잘못 적었고,
        # 코드는 판정 칸에 남아 있던 "new" 를 그대로 믿었습니다.
        #
        # 이제는 셋 중 하나면 발송하되 ⚠️ AI 미검증 을 붙입니다.
        # (놓치는 것보다 헛알림이 낫다는 원칙은 그대로입니다)
        v = by_i.get(i)
        if v is None:
            if retry_missing:
                print(f"[ai] 답 없음 ↻ {title} — 빠진 기사 1건만 다시 질문", file=sys.stderr)
                kept.extend(_judge_batch([cand], cfg, recent_events, api_key, retry_missing=False))
            else:
                safe = _safe_fail_open(cand, cfg, "재질문 후에도 답 없음")
                if safe is not None:
                    kept.append(safe)
            continue

        verdict = str(v.get("v", "")).strip().lower()
        why = v.get("why", "")
        # AI가 판정 전에 적은 '이 기사의 핵심 사건'. 로그에 남겨두면
        # 잘못 보냈을 때 AI가 무엇으로 읽었는지 바로 보입니다.
        what = str(v.get("what", "")).strip()

        # 칸 혼동 구제 — what 은 25자 요약이라 판정어 한 단어만 올 일이 없습니다.
        # 그런데 딱 판정어만 있다면 AI가 칸을 헷갈린 것이므로 그쪽을 믿습니다.
        # (실제로 {"what":"skip","v":"new"} 형태로 두 번 샜습니다)
        if what.lower() in VALID:
            print(f"[ai] 칸 혼동 구제 {title} — what 칸의 '{what}' 를 판정으로 봄",
                  file=sys.stderr)
            verdict, what = what.lower(), ""

        if verdict not in VALID:
            if retry_missing:
                print(f"[ai] 판정값 이상 ↻ {title} — v='{v.get('v')}', 1건만 다시 질문",
                      file=sys.stderr)
                kept.extend(_judge_batch([cand], cfg, recent_events, api_key, retry_missing=False))
            else:
                safe = _safe_fail_open(cand, cfg, f"재질문 후 판정값 이상: {v.get('v')}")
                if safe is not None:
                    kept.append(safe)
            continue
        # 본문에서 뽑아낸 사실 둘. 알림에 표시합니다.
        occurred = str(v.get("occurred", "")).strip()
        company = str(v.get("co", "")).strip()
        # 제목에 실제 주요건설사명이 있으면 본문에서 스친 다른 회사보다 우선합니다.
        if cand[3] == "company":
            title_company = str(cand[1] or "").strip()
            if title_company and _norm(title_company) in _norm(cand[0].get("title", "")):
                if company and _norm(company) != _norm(title_company):
                    print(f"[ai] 회사명 보정 {title} — {company} → {title_company}", file=sys.stderr)
                company = title_company
        tail = f' · "{what}"' if what else ""

        if verdict == "skip":
            print(f"[ai] 제외 × {title} ({why}){tail}")
            continue
        if verdict == "dup":
            # 안전망 — AI가 재탕이라 했지만 그 사고의 시공사가 **처음** 나온 기사면
            # 코드가 update 로 올립니다. 언론은 나중에 회사명을 빼기도 하므로
            # 처음 나온 순간 붙잡지 못하면 영영 잃습니다.
            # (9/11 광주 H빔 → 9/13 현대엔지니어링 공개 기사를 AI가 버린 사고)
            e = v.get("e", -1)
            ev = (recent_events[e] if isinstance(e, int) and 0 <= e < len(recent_events)
                  else None)
            # 조건 셋 (2026-09-22 강화):
            #   · 원래 사건에 회사명이 아직 없고
            #   · 회사명이 **이 기사 제목에** 있고 (본문에 비교 대상으로 스친 회사 제외)
            #   · 원래 사건 **제목에도 없던** 회사일 때 (그래야 '최초 공개')
            # 실제로 하이닉스 기사에 비교로 나온 HL만도를, 그리고 원래 제목에
            # 이미 있던 HL만도를 '시공사 공개'로 올린 적이 있습니다.
            nco = _norm(company)
            if (nco and ev is not None and not ev.get("co")
                    and nco in _norm(cand[0].get("title"))
                    and nco not in _norm(ev.get("title"))):
                print(f"[ai] 재탕→후속 ↻ {title} — 시공사 최초 공개({company})")
                kept.append((*cand, {"v": "update", "e": e,
                                     "chg": f"시공사 공개: {company}"[:25],
                                     "occurred": occurred, "co": company}))
                continue
            print(f"[ai] 재탕 × {title} ({why}){tail}")
            continue
        if verdict == "update":
            chg = v.get("chg", "")
            ok, reason, event_idx = _validate_update(
                cand, v, recent_events, cfg, company, occurred
            )
            if not ok:
                # AI는 '이미 보낸 사고'라고 답했습니다. 코드 검증이 틀어진 것뿐이라
                # 새 사고(⚠️ 미검증)로 올리지 않고 버립니다. 예전엔 NEW로 올려서
                # 2026-09-30 서초 디에이치클래스트 사고가 두 번 나갔습니다.
                print(f"[ai] UPDATE 검증실패 → 재탕 × {title} — {reason}", file=sys.stderr)
                continue
            print(f"[ai] 후속 ↻ {title} ({chg or why}){tail}")
            kept.append((*cand, {"v": "update", "e": event_idx, "chg": chg,
                                 "occurred": occurred, "co": company}))
            continue
        if too_old(occurred):
            print(f"[ai] 지난 사고 × {title} — 발생 {occurred}, "
                  f"{OLD_ACCIDENT_DAYS}일 초과{tail}")
            continue
        extra = " ".join(x for x in (f"발생 {occurred}" if occurred else "",
                                     f"시공사 {company}" if company else "") if x)
        print(f"[ai] 발송 ○ {title} ({why}){tail}{' · ' + extra if extra else ''}")
        kept.append((*cand, {"v": "new", "e": -1, "chg": "",
                             "occurred": occurred, "co": company}))

    usage = body.get("usage", {})
    print(f"[ai] {len(candidates)}건 판단 → {len(kept)}건 발송 "
          f"(입력 {usage.get('input_tokens','?')} / 출력 {usage.get('output_tokens','?')} 토큰)")
    return kept
