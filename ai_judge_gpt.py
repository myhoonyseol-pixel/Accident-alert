# -*- coding: utf-8 -*-
"""OpenAI GPT 독립 비교 판정기.

기존 Claude 운영 판정(ai_judge.py)은 그대로 유지합니다.
같은 후보 기사(본문 포함)를 GPT에도 독립적으로 보내고 결과만 비교합니다.

이번 버전의 핵심
----------------
- Claude 최신 SYSTEM_PROMPT를 그대로 공유합니다.
- GPT는 Claude와 별도의 최근 사고(gpt_events)를 기준으로 NEW/UPDATE/DUP를 판단합니다.
- 최근에 GPT가 NO_ALERT로 본 후보도 짧게 기억해, 같은 단순부상 사고가
  제목만 바뀌어 NEW로 되살아나는 일을 줄입니다.
- 회사명은 '기사에 언급된 회사'가 아니라 실제 사고현장의 시공사/원청만 co에 적습니다.
- DL건설 현장 기사에 DL이앤씨 공시 문장이 있어도 co는 DL건설입니다.
- what/v 칸 혼동을 구제합니다.
- GPT 오류는 ERROR로 반환하며 Claude 운영 흐름에는 영향을 주지 않습니다.
- main.py가 NEW/UPDATE만 GPT 전용 Telegram 방으로 보냅니다.

필수 GitHub Secret
------------------
OPENAI_API_KEY

선택 환경변수
--------------
OPENAI_MODEL  (기본값: gpt-5.6-luna)
"""

import json
import os
import sys
import time

import requests

import ai_judge

API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.6-luna"
VALID = ("skip", "dup", "update", "new")

# Responses API Structured Outputs는 최상위 schema를 object로 둡니다.
# 실제 판정 목록은 results 배열 안에 넣습니다.
RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "i": {"type": "integer"},
                    "what": {"type": "string"},
                    "v": {"type": "string", "enum": ["new", "update", "skip", "dup"]},
                    "e": {"type": "integer"},
                    "occurred": {"type": "string"},
                    "co": {"type": "string"},
                    "chg": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["i", "what", "v", "e", "occurred", "co", "chg", "why"],
            },
        },
    },
    "required": ["results"],
}

GPT_OUTPUT_INSTRUCTION = r"""

━━ GPT 구조화 출력 형식 ━━
OpenAI Structured Outputs 제약 때문에 최상위는 배열이 아니라 반드시 객체다.
위의 'JSON 배열만 출력' 지시보다 아래 형식을 우선한다. 다른 말은 쓰지 마라.
{"results":[{"i":0,"what":"기사의 핵심 사건","v":"new","e":-1,"occurred":"","co":"","chg":"","why":"20자 이내"}]}

모든 입력 기사에 대해 results에 정확히 한 건씩 결과를 넣는다.

━━ 회사명 co 판단 보강 ━━
co에는 기사에 등장한 아무 회사나 적지 말고 **실제 사고현장의 시공사·원청**만 적어라.
- '○○건설 현장', '시공사 ○○', '원청 ○○', '○○이 시공'처럼 현장과 연결된 근거가 있어야 한다.
- 모회사·계열사·공시 주체·논평 주체가 본문에 등장했다는 이유만으로 co에 적지 마라.
- 실제 예: 'DL건설 현장서 작업자 사망' 기사 본문에 'DL이앤씨는 공시를 통해…'가 있어도
  사고현장 시공사는 DL건설이므로 co="DL건설"이다. DL이앤씨로 바꾸지 마라.

━━ 최근 NO_ALERT 기억 사용법 ━━
'[최근 GPT가 검토했지만 알림하지 않은 기사]'는 이미 발송한 사고 목록이 아니다.
다만 같은 사고가 제목만 바뀌어 다시 들어오는지 판단할 때 참고한다.
- 앞서 단순부상으로 NO_ALERT였던 같은 사고가 다시 왔는데 새 기사 제목에서 피해가 빠졌다고
  '피해규모 미상' NEW로 되살리지 마라. 이전에 확인한 단순부상 정보를 함께 사용한다.
- 이후 사망·중상·위독 등 알림 문턱을 새로 넘은 사실이 확인되면, 이전에 발송한 적은 없으므로 NEW다.
- 회사명만 새로 밝혀져도 피해가 여전히 알림 문턱 아래라면 ALERT로 올리지 마라.
"""


def _build_user_msg(candidates, recent_events, silent_events=None):
    """Claude와 같은 본문/사건 정보를 주되 GPT 자체 기억을 사용합니다."""
    parts = []

    if recent_events:
        parts.append("[이미 보낸 사고 — GPT 비교방 기준]")
        for j, ev in enumerate(recent_events):
            co = f' [시공사: {ev["co"]}]' if ev.get("co") else ""
            parts.append(f'{j}. ({ev.get("when", "")}) {ev.get("title", "")[:90]}{co}')
        parts.append("")

    silent_events = silent_events or []
    if silent_events:
        parts.append("[최근 GPT가 검토했지만 알림하지 않은 기사]")
        for ev in silent_events:
            bits = []
            if ev.get("occurred"):
                bits.append(f'사고발생 {ev["occurred"]}')
            if ev.get("co"):
                bits.append(f'시공사 {ev["co"]}')
            if ev.get("why"):
                bits.append(f'이유 {ev["why"]}')
            tail = f" [{' / '.join(bits)}]" if bits else ""
            core = ev.get("what") or ev.get("title", "")
            parts.append(f'- {core[:90]}{tail}')
        parts.append("")

    parts.append("[판단할 기사]")
    for i, (item, place, hits, _conf) in enumerate(candidates):
        title = (item.get("title") or "")[:120]
        summary = (item.get("summary") or "")[:200]
        outlet = (item.get("outlet") or "").replace("https://", "")[:40]
        line = (
            f'{i}. 제목: {title}\n'
            f'   요약: {summary}\n'
            f'   매체: {outlet or "미상"}\n'
            f'   걸린단어: {place} / {"·".join(hits[:4])}'
        )

        body = (item.get("body") or "").strip()
        if body:
            line += f'\n   본문: {body[:1000]}'
        else:
            line += '\n   본문: (확보 실패 — 제목·요약만으로 판단)'

        cov = item.get("coverage", 0)
        if cov >= 2:
            line += f'\n   동시 보도: {cov}개 기사'
        hint = item.get("followup_hint")
        if hint:
            line += f'\n   ※ 이미 보낸 사고의 후속으로 보임 — {hint}'
        parts.append(line)

    return "\n".join(parts)


def _output_text(body):
    """Responses API JSON에서 모델의 텍스트 출력을 꺼냅니다."""
    texts = []
    for out in body.get("output", []) or []:
        for content in out.get("content", []) or []:
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(content["text"])
    if not texts:
        raise ValueError("OpenAI 응답에서 output_text를 찾을 수 없습니다.")
    return "\n".join(texts)


def _extract_results(text):
    """Structured Outputs의 {"results": [...]}를 안전하게 읽습니다."""
    text = (text or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return data["results"]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start >= 0:
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                return data["results"]
        except json.JSONDecodeError:
            pass

    # 마지막 안전망: Claude 쪽 배열 파서. GPT가 레거시 배열을 내도 살립니다.
    return ai_judge._extract_json(text)


def _error_result(cand, model, why):
    return (*cand, {
        "v": "error", "e": -1, "what": "", "occurred": "", "co": "",
        "why": why, "chg": "",
        "decision": "ERROR", "type": "ERROR", "model": model,
    })


def judge(candidates, cfg, recent_events=None, silent_events=None):
    """후보 전체의 GPT 판정을 반환합니다.

    recent_events는 **GPT 비교방이 실제로 보낸 사고** 목록입니다.
    silent_events는 최근 GPT가 NO_ALERT로 본 후보의 짧은 기억입니다.
    Claude 운영 이벤트와 분리해 A/B 비교가 서로 오염되지 않게 합니다.
    """
    recent_events = recent_events or []
    silent_events = silent_events or []
    if not candidates:
        return []

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL

    if not api_key:
        print("[gpt] OPENAI_API_KEY가 없어 비교 판정을 건너뜁니다.", file=sys.stderr)
        return [_error_result(c, model, "OPENAI_API_KEY 없음") for c in candidates]

    user_msg = _build_user_msg(candidates, recent_events, silent_events)
    response_body = None
    verdicts = None
    raw = ""
    last_err = None

    for attempt in (1, 2):
        try:
            r = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "store": False,
                    # 후보가 많은 날 구조화 출력이 잘리는 것을 막습니다.
                    # 상한을 높여도 실제 사용한 토큰만 과금됩니다.
                    "max_output_tokens": 4000,
                    "input": [
                        {"role": "system", "content": ai_judge.SYSTEM_PROMPT + GPT_OUTPUT_INSTRUCTION},
                        {"role": "user", "content": user_msg},
                    ],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "accident_judgments",
                            "strict": True,
                            "schema": RESULT_SCHEMA,
                        }
                    },
                },
                timeout=getattr(cfg, "AI_TIMEOUT", 30),
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")

            response_body = r.json()
            raw = _output_text(response_body)
            verdicts = _extract_results(raw)
            break

        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[gpt] {attempt}차 시도 실패: {e}", file=sys.stderr)
            if raw:
                print(f"[gpt] GPT 원문 ↓\n{raw[:500]}", file=sys.stderr)
            raw = ""
            if attempt == 1:
                time.sleep(2)

    if verdicts is None:
        print(f"[gpt] 비교 판정 최종 실패: {last_err}", file=sys.stderr)
        return [_error_result(c, model, str(last_err)[:180]) for c in candidates]

    by_i = {}
    for v in verdicts:
        try:
            by_i[int(v["i"])] = v
        except (TypeError, ValueError, KeyError):
            continue

    type_map = {
        "new": ("ALERT", "NEW"),
        "update": ("ALERT", "UPDATE"),
        "skip": ("NO_ALERT", "IRRELEVANT"),
        "dup": ("NO_ALERT", "DUP"),
    }

    out = []
    for i, cand in enumerate(candidates):
        v = by_i.get(i)
        if not v:
            out.append(_error_result(cand, model, "해당 후보의 GPT 결과 누락"))
            continue

        verdict = str(v.get("v", "")).strip().lower()
        what = str(v.get("what", "")).strip()

        # Claude에서 실제로 발생했던 칸 혼동을 GPT도 방어합니다.
        # strict schema는 '문자열/enum' 형식은 보장하지만 의미상 잘못 넣는 것까지 막지는 못합니다.
        if what.lower() in VALID:
            print(f"[gpt] 칸 혼동 구제 {cand[0].get('title','')[:44]} — "
                  f"what 칸의 '{what}' 를 판정으로 봄", file=sys.stderr)
            verdict, what = what.lower(), ""

        if verdict not in VALID:
            out.append(_error_result(cand, model, f"판정값 이상: {v.get('v')}"))
            continue

        e = v.get("e", -1)
        try:
            e = int(e)
        except (TypeError, ValueError):
            e = -1

        company = str(v.get("co", "")).strip()
        # 제목 필터가 주요건설사를 직접 잡은 경우 제목의 회사 근거를 우선합니다.
        # 특히 'DL건설 현장...' 제목인데 본문에 'DL이앤씨는 공시...'가 함께 있을 때
        # 모델이 공시 주체를 시공사로 잘못 뽑는 것을 코드가 한 번 더 막습니다.
        if cand[3] == "company":
            title_company = str(cand[1]).strip()
            if title_company and company and company != title_company:
                print(f"[gpt] 회사명 보정 {cand[0].get('title','')[:44]} — "
                      f"{company} → {title_company}", file=sys.stderr)
                company = title_company
            elif title_company and not company:
                company = title_company

        ev = recent_events[e] if 0 <= e < len(recent_events) else None

        # Claude 최신 안전망과 동일: 같은 사고라며 dup 처리했더라도 시공사가 처음 확인됐으면 UPDATE.
        if verdict == "dup":
            if company and ev is not None and not ev.get("co"):
                print(f"[gpt] 재탕→후속 ↻ {cand[0].get('title','')[:44]} — "
                      f"시공사 최초 공개({company})")
                verdict = "update"
                v["chg"] = f"시공사 공개: {company}"[:25]

        # 반대 안전망: 이미 같은 시공사를 기억하는데 모델이 '회사 최초 공개'를 이유로
        # 또 UPDATE라 한 경우는 DUP로 내립니다. 9/17 DL건설 사고의 반복 UPDATE 방지.
        if verdict == "update" and ev is not None and company and ev.get("co") == company:
            change_text = f"{v.get('chg','')} {v.get('why','')}"
            company_update_words = ("시공사", "원청", "회사", "업체", "시공 정보", "현장 정보")
            casualty_update_words = ("사망", "중상", "위독", "사상자", "매몰", "실종", "구조")
            if (any(w in change_text for w in company_update_words)
                    and not any(w in change_text for w in casualty_update_words)):
                print(f"[gpt] 중복 회사 UPDATE→DUP {cand[0].get('title','')[:44]} — "
                      f"이미 시공사 {company} 기억", file=sys.stderr)
                verdict = "dup"
                v["chg"] = ""

        decision, detail_type = type_map[verdict]
        result = {
            "v": verdict,
            "e": e,
            "what": what,
            "occurred": str(v.get("occurred", "")).strip(),
            "co": company,
            "why": str(v.get("why", "")).strip(),
            "chg": str(v.get("chg", "")).strip(),
            "decision": decision,
            "type": detail_type,
            "model": model,
        }
        out.append((*cand, result))

    usage = (response_body or {}).get("usage", {}) or {}
    print(
        f"[gpt] {len(candidates)}건 비교판정 완료 "
        f"(입력 {usage.get('input_tokens', '?')} / 출력 {usage.get('output_tokens', '?')} 토큰)"
    )
    return out
