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

  같은 내용 다른 매체        → dup    (안 보냄)
  사상자 수 변경, 회사명 공개  → update (🔄 표시로 보냄)
  전혀 다른 사고             → new    (보냄)

비용
----
후보가 있을 때만 호출하고 여러 건을 묶어 묻습니다. 월 2,000원 안팎입니다.

실패했을 때
-----------
AI 호출이 실패하면 **전부 새 사고로 보고 발송**합니다(fail-open).
헛알림 하나보다 사고 하나를 놓치는 게 훨씬 위험하기 때문입니다.
"""
import json
import os
import re
import sys

import requests

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

SYSTEM_PROMPT = """너는 건설회사 안전보건 담당자를 돕는 판정기다.
뉴스 기사를 받아 두 가지를 판단한다.

━━ 판단 1 · 알려야 할 사고인가 ━━

[알려야 하는 것]
- 국내 건설현장·공사장·산업 사업장에서 실제로 발생한 사고 보도
- 인명피해가 났거나, 확인 중이거나, 매몰·고립 등 발생 가능성이 있는 경우
- 사고 당일 보도라면 "경찰 수사 착수" 같은 문구가 붙어 있어도 알려야 한다
- 해외 사고라도 국내 건설사가 시공 중인 현장이면 알려야 한다

[알리면 안 되는 것]
- 통계·연간 집계·순위 ("지난해 사망 598명", "역대 최저", "최다 사망사고 어디")
- 기획·해설·사설·칼럼·인터뷰·판례 해설
- 캠페인·안전점검·협약·교육·수상·시스템 도입 등 행사나 보도자료
- 사고로부터 시간이 지난 뒤의 법적 절차 (압수수색·기소·판결·손해배상 소송)
- 주가·수주·분양·실적 등 기업 경영 뉴스
- 건설·산업 현장과 무관한 사고 (교통사고, 주택 화재, 산불, 범죄, 자연재해)
- 국내 건설사와 무관한 해외 사고
- 백과사전·위키 문서, 과거 사고를 되짚는 회고 기사

━━ 판단 2 · 이미 보낸 사고인가 ━━

'이미 보낸 사고' 목록이 함께 주어진다. 각 기사가 그중 하나와 같은 사건인지 본다.
지역·사고유형·시점이 비슷하면 같은 사건일 수 있다. 표현이 완전히 달라도
같은 사건인 경우가 많다.
  예: "천안 콘크리트 공장서 근로자 1명 숨져…거푸집 부재 전도"
      "까뮤이앤씨, 천안 사업장서 하청 노동자 사망"  → 같은 사건

같은 사건이라면, **새로운 사실이 있는지**로 갈라라.

  update — 담당자가 알아야 할 새 사실이 있다
     · 사상자 수 변경 (1명 → 3명, 부상 → 사망, 실종자 발견)
     · 시공사·원청 이름이 처음 밝혀짐
     · 사고 원인 규명, 중대재해처벌법 수사 착수, 작업중지 명령
     · 매몰자 구조 완료 등 상황 종결

  dup — 새 사실이 없다
     · 같은 내용을 다른 매체가 다시 쓴 것
     · 논평·사설·유가족 인터뷰·업계 반응
     · 주가 하락 등 파생 기사

━━ 해외 기사 걸러내기 ━━
제목·요약에 한국 행정구역 이름(서울·부산·경기·충남·천안·진천 같은 시·군·구·도)이
하나도 없고 낯선 외국식 지명이 있으면 해외 기사로 본다. 코드의 나라 이름 목록에는
없지만 네가 아는 지명이 많다.
  예: '득토 종합병원'(베트남 하띤성 Đức Thọ), '빈즈엉 공단', '앙헬레스 9층 건물'
매체 이름이 함께 주어지면 참고하라. 외국 신문의 한국어판이 섞여 들어온다.
단 국내 건설사(현대건설·삼성물산 등)가 시공 중인 현장이면 해외라도 알려야 한다.

━━ 한국어 주의 ━━
회사·지명 이름 안에 사고 단어가 우연히 들어 있는 경우에 속지 마라.
'대전도시공사'는 기관 이름이지 '전도'(사고)가 아니다.
'구미국가산단'은 경북 구미이지 '미국'이 아니다.

━━ 판단이 애매하면 ━━
new 로 한다. 놓치는 것이 헛알림보다 위험하다.
특히 같은 지역에서 다른 사고가 났을 가능성을 항상 염두에 둬라.
확신이 없으면 dup 로 묶지 마라.

반드시 아래 JSON 배열만 출력한다. 다른 말은 쓰지 마라.
[{"i":0,"v":"new","e":-1,"why":"20자 이내","chg":""}]
  v   new / update / dup
  e   같은 사건인 경우 그 사건 번호, 아니면 -1
  chg v가 update일 때 무엇이 새로 밝혀졌는지 25자 이내"""


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"JSON 배열을 찾을 수 없음: {text[:120]}")
    return json.loads(text[start:end + 1])


def judge(candidates, cfg, recent_events=None):
    """candidates: [(item, place, hits, confidence), ...]
    recent_events: [{"title":..., "when":..., "updates":int}, ...] 최근 보낸 사고

    반환: [(item, place, hits, confidence, verdict), ...]
      verdict = {"v": "new"|"update", "e": 사건번호, "chg": "바뀐 점"}
      dup 로 판정된 건은 목록에서 빠집니다.
    """
    recent_events = recent_events or []

    if not getattr(cfg, "AI_ENABLED", False) or not candidates:
        return [(c[0], c[1], c[2], c[3], {"v": "new", "e": -1, "chg": ""})
                for c in candidates]

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("[ai] ANTHROPIC_API_KEY 가 없어 AI 판정을 건너뜁니다", file=sys.stderr)
        return [(c[0], c[1], c[2], c[3], {"v": "new", "e": -1, "chg": ""})
                for c in candidates]

    parts = []
    if recent_events:
        parts.append("[이미 보낸 사고]")
        for j, ev in enumerate(recent_events):
            parts.append(f'{j}. ({ev.get("when","")}) {ev.get("title","")[:90]}')
        parts.append("")
    parts.append("[판단할 기사]")
    for i, (item, place, hits, _conf) in enumerate(candidates):
        title = (item.get("title") or "")[:120]
        summary = (item.get("summary") or "")[:200]
        outlet = (item.get("outlet") or "").replace("https://", "")[:40]
        parts.append(f'{i}. 제목: {title}\n   요약: {summary}\n'
                     f'   매체: {outlet or "미상"}\n'
                     f'   걸린단어: {place} / {"·".join(hits[:4])}')
    user_msg = "\n".join(parts)

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
                "max_tokens": 1200,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
            },
            timeout=getattr(cfg, "AI_TIMEOUT", 30),
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        verdicts = _extract_json(body["content"][0]["text"])
    except Exception as e:                              # noqa: BLE001
        print(f"[ai] 판정 실패 — 거르지 않고 전부 발송합니다: {e}", file=sys.stderr)
        return [(c[0], c[1], c[2], c[3], {"v": "new", "e": -1, "chg": ""})
                for c in candidates]

    by_i = {}
    for v in verdicts:
        try:
            by_i[int(v["i"])] = v
        except (TypeError, ValueError, KeyError):
            continue

    kept = []
    for i, cand in enumerate(candidates):
        title = (cand[0].get("title") or "")[:44]
        v = by_i.get(i, {})
        verdict = str(v.get("v", "new")).lower()
        why = v.get("why", "")
        if verdict == "dup":
            print(f"[ai] 재탕 × {title} ({why})")
            continue
        if verdict == "update":
            chg = v.get("chg", "")
            print(f"[ai] 후속 ↻ {title} ({chg or why})")
            kept.append((*cand, {"v": "update", "e": v.get("e", -1), "chg": chg}))
            continue
        print(f"[ai] 발송 ○ {title} ({why})")
        kept.append((*cand, {"v": "new", "e": -1, "chg": ""}))

    usage = body.get("usage", {})
    print(f"[ai] {len(candidates)}건 판단 → {len(kept)}건 발송 "
          f"(입력 {usage.get('input_tokens','?')} / 출력 {usage.get('output_tokens','?')} 토큰)")
    return kept
