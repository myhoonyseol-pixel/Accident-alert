# -*- coding: utf-8 -*-
"""OpenAI GPT 비교 판정기.

목적
----
기존 Claude 운영 판정(ai_judge.py)은 그대로 둔 채, 같은 후보 기사를 GPT에도
독립적으로 보내 결과를 비교합니다.

중요
----
- 이 파일의 결과는 기존 Claude 알림 발송 여부를 바꾸지 않습니다.
- Claude와 동일한 SYSTEM_PROMPT를 사용해 첫 A/B 비교의 판단 기준을 맞춥니다.
- GPT 결과는 new/update/skip/dup을 모두 반환합니다.
- main.py가 이를 ALERT/NO_ALERT로 표시해 GPT 전용 Telegram 방에 보냅니다.
- OpenAI 호출이 실패해도 Claude 운영 흐름에는 영향이 없습니다.

필수 GitHub Secrets
-------------------
OPENAI_API_KEY

선택 환경변수
--------------
OPENAI_MODEL
  기본값: gpt-5.6-luna
"""

import json
import os
import sys
import time

import requests

import ai_judge

API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.6-luna"

# Claude와 같은 최종 분류 체계를 강제합니다.
RESULT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "i": {"type": "integer"},
            "v": {"type": "string", "enum": ["new", "update", "skip", "dup"]},
            "e": {"type": "integer"},
            "why": {"type": "string"},
            "chg": {"type": "string"},
        },
        "required": ["i", "v", "e", "why", "chg"],
    },
}


def _build_user_msg(candidates, recent_events):
    """Claude 쪽과 같은 정보량/형식으로 GPT 입력을 만듭니다."""
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
        line = (
            f'{i}. 제목: {title}\n'
            f'   요약: {summary}\n'
            f'   매체: {outlet or "미상"}\n'
            f'   걸린단어: {place} / {"·".join(hits[:4])}'
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
    """Responses API 원문 JSON에서 모델의 텍스트 출력을 꺼냅니다."""
    texts = []
    for out in body.get("output", []) or []:
        for content in out.get("content", []) or []:
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(content["text"])
    if not texts:
        raise ValueError("OpenAI 응답에서 output_text를 찾을 수 없습니다.")
    return "\n".join(texts)


def _extract_json(text):
    """응답 앞뒤에 다른 글이 붙어도 첫 번째 완성 JSON 배열만 읽습니다."""
    return ai_judge._extract_json(text)


def judge(candidates, cfg, recent_events=None):
    """모든 후보의 GPT 판정을 반환합니다.

    반환:
      [(item, place, hits, confidence, verdict), ...]

    verdict 예:
      {
        "v": "new" | "update" | "skip" | "dup" | "error",
        "e": -1,
        "why": "...",
        "chg": "...",
        "decision": "ALERT" | "NO_ALERT" | "ERROR",
        "type": "NEW" | "UPDATE" | "IRRELEVANT" | "DUP" | "ERROR",
        "model": "..."
      }

    GPT 오류 시에도 기존 Claude 운영 판정에는 영향을 주지 않습니다.
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
                "v": "error", "e": -1, "why": "OPENAI_API_KEY 없음", "chg": "",
                "decision": "ERROR", "type": "ERROR", "model": model,
            })
            for c in candidates
        ]

    user_msg = _build_user_msg(candidates, recent_events)

    body = None
    verdicts = None
    raw = ""
    last_err = None

    # 운영 Claude와 마찬가지로 실패했을 때만 한 번 재시도합니다.
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
                        {"role": "system", "content": ai_judge.SYSTEM_PROMPT},
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

            body = r.json()
            raw = _output_text(body)
            verdicts = _extract_json(raw)
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
                "v": "error", "e": -1, "why": str(last_err)[:120], "chg": "",
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
                "v": "error", "e": -1, "why": "해당 후보의 GPT 결과 누락", "chg": "",
                "decision": "ERROR", "type": "ERROR", "model": model,
            }
        else:
            verdict = str(v.get("v", "")).lower()
            decision, detail_type = type_map.get(verdict, ("ERROR", "ERROR"))
            result = {
                "v": verdict if verdict in type_map else "error",
                "e": v.get("e", -1),
                "why": (v.get("why") or "").strip(),
                "chg": (v.get("chg") or "").strip(),
                "decision": decision,
                "type": detail_type,
                "model": model,
            }
        out.append((*cand, result))

    usage = (body or {}).get("usage", {}) or {}
    print(
        f"[gpt] {len(candidates)}건 비교판정 완료 "
        f"(입력 {usage.get('input_tokens','?')} / 출력 {usage.get('output_tokens','?')} 토큰)"
    )
    return out
