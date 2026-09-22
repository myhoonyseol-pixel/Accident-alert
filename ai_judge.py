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
AI 호출이 실패하면 **전부 새 사고로 보고 발송**합니다(fail-open).
헛알림 하나보다 사고 하나를 놓치는 게 훨씬 위험하기 때문입니다.

다만 이게 조용히 일어나면 안 됩니다. 2026-09-12 02:17 에 답을 읽다 실패해
AI 검증이 통째로 꺼진 채 "올해 하청노동자 5명 숨진 HD현대중공업…노동부
특별감독" 기사가 나갔는데, 받는 쪽에서는 AI가 승인한 건지 그냥 새어나온
건지 구분할 방법이 없었습니다. 그래서 실패하면 세 가지를 합니다.

  1) AI가 실제로 뭐라고 답했는지 로그에 남긴다 (안 남기면 원인을 못 봅니다)
  2) 한 번 더 물어본다 (실패했을 때만이라 평소 비용은 그대로입니다)
  3) 그래도 안 되면 판정에 ai="fail" 을 달아 보낸다
     → main.py 가 알림에 '⚠️ AI 미검증' 을 붙입니다
"""
import json
import os
import re
import sys
import time

import requests

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

SYSTEM_PROMPT = """너는 건설회사 안전보건 담당자다.
방금 일어난 사고를 몇 분 안에 사내에 알리는 것이 임무다.
기사 제목과 본문 앞부분을 받는다. 본문이 없으면 제목만으로 판단한다.

━━ 먼저 · 이 기사가 무엇을 말하는지 한 문장으로 쓴다 ━━

what 칸에 "누가 언제 어디서 무엇을" 을 25자로 적어라.
이걸 먼저 쓰면 나머지 판단은 저절로 따라온다.

  "올해 하청노동자 5명 숨진 HD현대중공업…노동부, 특별감독 이어 재감독까지"
    what: 노동부가 HD현대중공업 재감독
    → 사고가 아니다. skip.
    '5명 숨진'은 회사를 꾸미는 말이고, 실제 서술어는 '재감독'이다.
    (실제로 이 기사를 잘못 보낸 적이 있다)

━━ 판단 1 · 알려야 할 사고인가 ━━

⚠️ 단, 이 기사가 '이미 보낸 사고'의 후속이면 **판단 3을 먼저** 본다.
   며칠 뒤 기사라도, 수사·감독 얘기가 섞여 있어도, 새 사실이 있으면 update 다.
   판단 1의 '사고 이후의 일 → skip' 은 **새 사실이 없을 때만** 적용한다.

스스로 셋을 물어라. 아래 단어는 **예시일 뿐이다.**
목록에 없어도 성격이 같으면 똑같이 판단하라. 목록을 정답지로 쓰지 마라.

(가) 핵심 사건이 '사고'인가, '사고 이후의 일'인가
     감독·수사·재판·발표·추모·통계·집계·대책·캠페인·교육은 사고가 아니다.
     사고가 수식어로만 쓰였으면 사고 기사가 아니다.
     예) 총체적 인재 / 조사 결과 / 특별감독 / 기소 / 1주기 / 올해 ○명 숨진
         산업재해 예방 교육 실시 / 재해율 3년 연속 감소 / 안전점검 캠페인

(나) 방금 일어난 사고인가
     본문에 발생 일시가 있으면 그것을 기준으로 삼고 occurred 칸에 적어라.
     며칠 지났어도 **첫 보도라면 알린다.** 대신 날짜는 반드시 적어라.

(다) 피해가 문턱을 넘는가
     사망·중상·위독·매몰·실종·심정지·피해규모 미상 → 넘는다
     단순 부상만 있으면 몇 명이든 넘지 않는다
     예외: 건설현장 구조물 사고(붕괴·폭발·타워크레인 전도)는 인명피해가
           없어도 알린다. 단 (가)(나)를 통과한 것만.

━━ 판단 2 · 누가 다쳤나 ━━

가르는 기준은 **장소가 아니라 '일하다 다쳤는가'** 다.

  아파트에 불이 나 주민 사망           → 거주자. 안 알림
  아파트 창호 공사하던 2명 추락 사망     → 작업자. 반드시 알림
  공사장서 고철 싣던 '고물상' 사망      → 작업자다. 반드시 알림
        (실제 사례. 하청 사업주였다. '고물상'이라는 말에 속지 마라)

대상 — 건설현장 / 소규모 시공·보수(사망·중상만) / 제조업 공장·물류창고·산단 /
      해외는 국내 건설사 시공현장 또는 한국인 피해

제외 — 철도 선로·항만 하역·화물운송·차량정비·농작업·어선 / 교통사고·산불·
      범죄·자연재해 / 거주자·이용객 사고 / 주가·수주·실적 등 경영뉴스

━━ 판단 3 · 이미 보낸 사고인가 ━━

'이미 보낸 사고' 목록이 함께 온다.

**단어가 겹치는지로 보지 마라.** 매체마다 완전히 다르게 쓴다.
아래 셋은 전부 같은 사고다. 실제 사례이고, 우리는 세 번 따로 보냈다.

  집게차 작업 도중 철제 구조물에 충돌…40대 심정지
  광주 아파트 공사장서 철제 구조물에 맞은 40대 숨져
  북구 아파트 공사장서 H빔에 머리 맞아 40대 고물상 숨져

  겹치는 고유 단어가 하나도 없다. 그래도 같은 사고다.
  → 시각·지역·피해자 나이·사고 형태를 맞춰봐라.
    '광주'와 '북구'는 같은 곳일 수 있다(광주 북구).
    '철제 구조물'과 'H빔'은 같은 것이다.
    본문에 발생 일시가 있으면 그것을 대조하면 확실하다.

같은 사고라면 —
  update  상황 자체가 달라짐. 셋뿐이다.
          사상자 수 변화 / 시공사·원청 최초 공개 / 매몰·실종 종결
          목록에 [시공사: ○○] 가 이미 있으면 그 회사는 '최초 공개'가 아니다.

  dup     그 외 전부. 작업중지·수사착수·압수수색·원인규명은
          사망사고에 당연히 따르는 절차이지 상황 변화가 아니다.

  실제로 틀린 사례 — 9/11 광주 H빔 사고를 보낸 뒤 9/13 에
    "전남광주 현대엔지니어링 시공 현장에서 40대 사망 중대재해 발생"
  을 '사후보도'라며 skip 했다. 시공사가 처음 밝혀진 기사였다. update 였어야 한다.
  언론은 나중에 회사명을 빼기도 한다. **회사명이 처음 나온 기사는 절대 버리지 마라.**

━━ 판단이 애매하면 ━━
new 로 한다. 놓치는 것이 헛알림보다 위험하다.
특히 같은 지역에서 다른 사고가 났을 가능성을 항상 염두에 둬라.
확신이 없으면 dup 로 묶지 마라.

━━ 해외 기사 ━━
한국 행정구역 이름이 하나도 없고 낯선 외국식 지명이 있으면 해외로 본다.
  예: '득토 종합병원'(베트남 하띤성), '빈즈엉 공단', '앙헬레스 9층 건물'
매체 주소가 함께 오니 참고하라. 외국 신문의 한국어판이 섞여 들어온다.
단 국내 건설사가 시공 중인 현장이면 해외라도 알린다.

━━ 한국어 주의 ━━
회사·지명 안에 사고 단어가 우연히 든 것에 속지 마라.
'대전도시공사'는 기관 이름이지 '전도'가 아니다. '구미국가산단'은 '미국'이 아니다.

━━ 출력 ━━
아래 JSON 배열만 출력한다. 배열 뒤에 아무것도 쓰지 마라.

[{"i":0,"what":"핵심 사건","v":"넷 중 하나","e":-1,"occurred":"","co":"","chg":"","why":"판단 이유"}]

  what      이 기사의 핵심 사건 25자. **판정 전에 먼저 쓴다.** 항상 채운다
  v         skip · dup · update · new 넷 중 하나. **판정은 반드시 이 칸에.**
            what 칸에 skip·new 같은 판정을 쓰지 마라
  e         dup·update일 때 사건 번호, 아니면 -1
  occurred  본문에 사고 발생 일시가 있으면 "09-11 19:35" 또는 "09-11",
            없으면 "" (기사 작성일이 아니라 **사고가 난 때**다)
  co        본문에 시공사·원청 이름이 있으면 그대로, 없으면 ""
  chg       update일 때 무엇이 달라졌는지 25자
  why       판단 이유 20자"""


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
        # AI_ENABLED=False 는 일부러 끈 것이므로 표시하지 않지만,
        # 열쇠가 없는 건 의도한 상태가 아니므로 미검증으로 표시합니다.
        print("[ai] ANTHROPIC_API_KEY 가 없어 AI 판정을 건너뜁니다", file=sys.stderr)
        return [(c[0], c[1], c[2], c[3],
                 {"v": "new", "e": -1, "chg": "", "ai": "fail"})
                for c in candidates]

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
        line = (f'{i}. 제목: {title}\n   요약: {summary}\n'
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
        print(f"[ai] 판정 실패 — 거르지 않고 전부 발송합니다(⚠️ AI 미검증 표시): "
              f"{last_err}", file=sys.stderr)
        return [(c[0], c[1], c[2], c[3],
                 {"v": "new", "e": -1, "chg": "", "ai": "fail"})
                for c in candidates]

    by_i = {}
    for v in verdicts:
        try:
            by_i[int(v["i"])] = v
        except (TypeError, ValueError, KeyError):
            continue

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
            print(f"[ai] 답 없음 ⚠ {title} — 이 기사에 대한 답이 빠짐", file=sys.stderr)
            kept.append((*cand, {"v": "new", "e": -1, "chg": "", "ai": "fail"}))
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
            print(f"[ai] 판정값 이상 ⚠ {title} — v='{v.get('v')}'", file=sys.stderr)
            kept.append((*cand, {"v": "new", "e": -1, "chg": "", "ai": "fail"}))
            continue
        # 본문에서 뽑아낸 사실 둘. 알림에 표시합니다.
        occurred = str(v.get("occurred", "")).strip()
        company = str(v.get("co", "")).strip()
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
            if company and ev is not None and not ev.get("co"):
                print(f"[ai] 재탕→후속 ↻ {title} — 시공사 최초 공개({company})")
                kept.append((*cand, {"v": "update", "e": e,
                                     "chg": f"시공사 공개: {company}"[:25],
                                     "occurred": occurred, "co": company}))
                continue
            print(f"[ai] 재탕 × {title} ({why}){tail}")
            continue
        if verdict == "update":
            chg = v.get("chg", "")
            print(f"[ai] 후속 ↻ {title} ({chg or why}){tail}")
            kept.append((*cand, {"v": "update", "e": v.get("e", -1), "chg": chg,
                                 "occurred": occurred, "co": company}))
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
