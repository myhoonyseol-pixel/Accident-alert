# -*- coding: utf-8 -*-
"""키워드 그물을 통과한 후보를 AI가 최종 판정합니다.

왜 필요한가
-----------
키워드는 글자만 보고 뜻을 모릅니다. 그래서 '대전도시공사'를 사고로,
'구미국가산단'을 해외로 오인했고, 그때마다 예외를 손으로 가르쳐야 했습니다.
규칙이 425개까지 늘자 서로 충돌하기 시작했습니다
(제외어 '점검'이 "점검하던 근로자가 매몰"을 버림).

그래서 역할을 나눕니다.
  키워드 = 싼 1차 그물. 하루 수백 건을 몇 건으로 줄인다.
  AI     = 그 몇 건만 읽고 "진짜 알려야 할 사고인가"를 판단한다.

비용
----
후보가 있을 때만 호출하고, 여러 건을 한 번에 묶어 묻습니다.
하루 수십 건 판단이면 월 1,000원 안팎입니다.

실패했을 때
-----------
AI 호출이 실패하면 **전부 발송**합니다(fail-open).
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
뉴스 기사 목록을 받아, 각 기사가 '담당자에게 즉시 알려야 할 사고 속보'인지 판단한다.

[알려야 하는 것]
- 국내 건설현장·공사장·산업 사업장에서 실제로 발생한 사고 보도
- 인명피해가 났거나, 아직 확인 중이거나, 매몰·고립 등 발생 가능성이 있는 경우
- 사고 당일 보도라면 "경찰 수사 착수" 같은 문구가 붙어 있어도 알려야 한다
- 해외 사고라도 국내 건설사가 시공 중인 현장이면 알려야 한다

[알리면 안 되는 것]
- 통계·연간 집계·순위 (예: "지난해 사망 598명", "역대 최저", "최다 사망사고 어디")
- 기획·해설·사설·칼럼·인터뷰·판례 해설
- 캠페인·안전점검·협약·교육·수상·시스템 도입 등 행사나 보도자료
- 사고로부터 시간이 지난 뒤의 법적 절차 (압수수색·기소·판결·손해배상 소송)
- 주가·수주·분양·실적 등 기업 경영 뉴스
- 건설·산업 현장과 무관한 사고 (교통사고, 주택 화재, 산불, 범죄, 자연재해)
- 국내 건설사와 무관한 해외 사고
- 백과사전·위키 문서, 과거 사고를 되짚는 회고 기사

[한국어 주의]
회사·지명 이름 안에 사고 단어가 우연히 들어 있는 경우에 속지 마라.
예: '대전도시공사'는 기관 이름이지 '전도'(사고)가 아니다.
    '구미국가산단'은 경북 구미이지 '미국'이 아니다.
    '경상남도'는 '경상'(부상)이 아니다.

판단이 애매하면 send=true 로 한다. 놓치는 것이 헛알림보다 위험하다.

반드시 아래 JSON 배열만 출력한다. 다른 말은 쓰지 마라.
[{"i": 0, "send": true, "why": "판단 근거 20자 이내"}, ...]"""


def _extract_json(text: str):
    """모델 응답에서 JSON 배열만 뽑아냅니다."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"JSON 배열을 찾을 수 없음: {text[:120]}")
    return json.loads(text[start:end + 1])


def judge(candidates, cfg):
    """candidates: [(item, place, hits, confidence), ...]

    반환: 같은 형식의 리스트 (AI가 '알려야 한다'고 본 것만)
    """
    if not getattr(cfg, "AI_ENABLED", False) or not candidates:
        return candidates

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("[ai] ANTHROPIC_API_KEY 가 없어 AI 판정을 건너뜁니다", file=sys.stderr)
        return candidates

    lines = []
    for i, (item, place, hits, _conf) in enumerate(candidates):
        title = (item.get("title") or "")[:120]
        summary = (item.get("summary") or "")[:200]
        lines.append(f'{i}. 제목: {title}\n   요약: {summary}\n   걸린단어: {place} / {"·".join(hits[:4])}')
    user_msg = "다음 기사들을 판단하라.\n\n" + "\n\n".join(lines)

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
                "max_tokens": 1000,
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
        # 판단을 못 했으면 거르지 않는다. 놓치는 것보다 낫다.
        print(f"[ai] 판정 실패 — 거르지 않고 전부 발송합니다: {e}", file=sys.stderr)
        return candidates

    keep_idx = set()
    for v in verdicts:
        try:
            if v.get("send"):
                keep_idx.add(int(v["i"]))
        except (TypeError, ValueError, KeyError):
            continue

    kept = []
    for i, cand in enumerate(candidates):
        title = (cand[0].get("title") or "")[:46]
        why = next((v.get("why", "") for v in verdicts
                    if str(v.get("i")) == str(i)), "")
        if i in keep_idx:
            kept.append(cand)
            print(f"[ai] 발송 ○ {title} ({why})")
        else:
            print(f"[ai] 제외 × {title} ({why})")

    usage = body.get("usage", {})
    print(f"[ai] {len(candidates)}건 판단 → {len(kept)}건 발송 "
          f"(입력 {usage.get('input_tokens', '?')} / 출력 {usage.get('output_tokens', '?')} 토큰)")
    return kept
