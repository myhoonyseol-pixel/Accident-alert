# -*- coding: utf-8 -*-
"""OpenAI GPT 독립 비교 판정기.

기존 Claude 운영 판정(ai_judge.py)은 그대로 유지합니다.
같은 후보 기사(본문 포함)를 GPT에도 독립적으로 보내고 결과만 비교합니다.

- Claude와 동일한 SYSTEM_PROMPT를 사용합니다.
- NEW/UPDATE는 ALERT, SKIP/DUP은 NO_ALERT로 매핑합니다.
- GPT 오류는 ERROR로 반환하며 Claude 운영 흐름에는 영향을 주지 않습니다.
- main.py가 ALERT(NEW/UPDATE)만 GPT 전용 Telegram 방으로 보냅니다.

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
{"results":[{"i":0,"what":"기사의 핵심 사고","v":"new","e":-1,"occurred":"","co":"","chg":"","why":"20자 이내"}]}

what: 기사의 핵심 사건을 짧게 요약한다.
occurred: 본문에서 확인되는 실제 사고 발생 일시. 모르면 빈 문자열.
co: 본문에서 확인되는 시공사/원청/사업주. 모르면 빈 문자열.
모든 입력 기사에 대해 results에 정확히 한 건씩 결과를 넣는다.
"""


def _build_user_msg(candidates, recent_events):
    """Claude와 같은 정보량으로 GPT 입력을 만듭니다."""
    parts = []

    if recent_events:
        parts.append("[이미 보낸 사고]")
        for j, ev in enumerate(recent_events):
            parts.append(f'{j}. ({ev.get("when", "")}) {ev.get("title", "")[:90]}')
        parts.append("")

    parts.append("[판단할 기사]")
    for i, (item, place, hits, _conf) in enumerate(candidates):
        title = (item.get("title") or "")[:160]
        summary = (item.get("summary") or "")[:300]
        body = (item.get("body") or "")[:1200]
        outlet = (item.get("outlet") or item.get("source") or "").replace("https://", "")[:80]
        published = item.get("published") or ""

        line = (
            f"{i}. 제목: {title}\n"
            f"   매체: {outlet or '미상'}\n"
            f"   기사시각: {published}\n"
            f"   걸린단어: {place} / {'·'.join(hits[:6])}\n"
            f"   요약: {summary or '(없음)'}\n"
            f"   본문: {body or '(본문 확보 실패 — 제목과 요약만으로 판단)'}"
        )

        cov = item.get("coverage", 0)
        if cov >= 2:
            line += f"\n   동시 보도: {cov}개 기사"

        hint = item.get("followup_hint")
        if hint:
            line += f"\n   ※ 이미 보낸 사고의 후속으로 보임 — {hint}"

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
    """Structured Outputs의 {"results": [...]}를 안전하게 읽습니다.

    혹시 예전 형식(JSON 배열)이 돌아와도 비교기가 죽지 않도록
    레거시 배열 파싱을 한 번 더 지원합니다.
    """
    text = (text or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return data["results"]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # 비정상적으로 앞뒤 텍스트가 붙은 경우 첫 JSON 객체만 시도합니다.
    start = text.find("{")
    if start >= 0:
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                return data["results"]
        except json.JSONDecodeError:
            pass

    # 마지막 안전망: Claude 쪽 레거시 배열 파서
    return ai_judge._extract_json(text)


def judge(candidates, cfg, recent_events=None):
    """후보 전체의 GPT 판정을 반환합니다.

    반환 형식:
      [(item, place, hits, confidence, verdict), ...]

    verdict 주요 필드:
      v          new/update/skip/dup/error
      decision   ALERT/NO_ALERT/ERROR
      type       NEW/UPDATE/IRRELEVANT/DUP/ERROR
      what       기사의 핵심 사건
      occurred   실제 사고 발생 일시
      co         시공사/원청
      why        판단 이유
      chg        UPDATE 변경사항
    """
    recent_events = recent_events or []
    if not candidates:
        return []

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL

    if not api_key:
        print("[gpt] OPENAI_API_KEY가 없어 비교 판정을 건너뜁니다.", file=sys.stderr)
        return [
            (*c, {
                "v": "error", "e": -1, "what": "", "occurred": "", "co": "",
                "why": "OPENAI_API_KEY 없음", "chg": "",
                "decision": "ERROR", "type": "ERROR", "model": model,
            })
            for c in candidates
        ]

    user_msg = _build_user_msg(candidates, recent_events)
    response_body = None
    verdicts = None
    raw = ""
    last_err = None

    # 운영 Claude와 같이 실패 시 한 번 재시도합니다.
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
        return [
            (*c, {
                "v": "error", "e": -1, "what": "", "occurred": "", "co": "",
                "why": str(last_err)[:180], "chg": "",
                "decision": "ERROR", "type": "ERROR", "model": model,
            })
            for c in candidates
        ]

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
            result = {
                "v": "error", "e": -1, "what": "", "occurred": "", "co": "",
                "why": "해당 후보의 GPT 결과 누락", "chg": "",
                "decision": "ERROR", "type": "ERROR", "model": model,
            }
        else:
            verdict = str(v.get("v", "")).lower()
            decision, detail_type = type_map.get(verdict, ("ERROR", "ERROR"))
            result = {
                "v": verdict if verdict in type_map else "error",
                "e": v.get("e", -1),
                "what": (v.get("what") or "").strip(),
                "occurred": (v.get("occurred") or "").strip(),
                "co": (v.get("co") or "").strip(),
                "why": (v.get("why") or "").strip(),
                "chg": (v.get("chg") or "").strip(),
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
