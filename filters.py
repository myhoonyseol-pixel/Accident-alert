# -*- coding: utf-8 -*-
"""기사 판정 로직. main.py와 test_filter.py가 같은 함수를 씁니다."""
import re

_TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}|\d+")

# 제목 토큰 비교 시 무시할 흔한 단어
_STOPWORDS = {
    "속보", "단독", "종합", "사고", "발생", "현장", "확인", "중이다",
    "밝혔다", "전해졌다", "따르면", "관계자", "오전", "오후", "지난",
    "이날", "당시", "가운데", "대해", "위해", "있다", "했다", "됐다",
}

# 뒤에 붙은 조사를 떼어냅니다. ("공사현장서" 와 "공사현장" 이 같은 말이 되도록)
_PARTICLES = ("에서는", "에서도", "으로는", "에서", "에게", "으로", "이라", "라며",
              "까지", "부터", "에는", "에도", "만에", "서는", "은", "는", "이",
              "가", "을", "를", "의", "로", "과", "와", "도", "서", "인", "엔")

# 매체마다 다른 표현을 한 단어로 통일합니다.
# (같은 사고인데 A사는 '붕괴', B사는 '무너져'로 쓰는 경우가 많습니다)
_SYNONYMS = {
    "무너져": "붕괴", "무너진": "붕괴", "무너졌": "붕괴", "붕괴돼": "붕괴",
    "붕괴된": "붕괴", "도괴": "붕괴", "주저앉": "붕괴",
    "숨져": "사망", "숨진": "사망", "숨졌": "사망", "사망자": "사망",
    "목숨": "사망", "참변": "사망", "사망사고": "사망",
    "다쳐": "부상", "다친": "부상", "중상": "부상", "경상": "부상",
    "부상자": "부상", "중경상": "부상",
    "떨어져": "추락", "떨어진": "추락", "추락사": "추락",
    "깔려": "협착", "끼여": "끼임", "협착": "끼임",
    "불이": "화재", "불길": "화재", "화염": "화재",
    "매몰돼": "매몰", "고립": "매몰",
    "근로자": "작업자", "노동자": "작업자", "인부": "작업자",
    "타워크레인": "크레인",
}


def _normalize(tok: str) -> str:
    for p in _PARTICLES:
        if len(tok) > len(p) + 1 and tok.endswith(p):
            tok = tok[: -len(p)]
            break
    return _SYNONYMS.get(tok, tok)


def match(cfg, title, summary=""):
    """조건을 만족하면 (장소단어, 사고단어목록, 신뢰도) 반환, 아니면 None.

    신뢰도: 'strong' = 건설현장이 명시됨 / 'weak' = 맥락으로 추정
    """
    title = title or ""
    summary = summary or ""
    text = f"{title} {summary}"

    # 1) 제외어 판정은 '제목'만 본다.
    #    요약에 스치듯 나온 단어 때문에 진짜 속보를 버리지 않기 위해서.
    if any(w in title for w in cfg.EXCLUDE_WORDS):
        return None

    # 2) 사고 키워드가 없으면 사고 기사가 아니다.
    hits = [w for w in cfg.ACCIDENT_WORDS if w in text]
    if not hits:
        return None

    # 3) 장소 판정 — STRONG은 단독 통과
    strong = next((w for w in cfg.STRONG_PLACE_WORDS if w in text), None)
    if strong:
        return strong, hits, "strong"

    # 4) WEAK은 산업/건설 맥락이 함께 있을 때만 통과
    weak = next((w for w in cfg.WEAK_PLACE_WORDS if w in text), None)
    if weak and any(c in text for c in cfg.CONTEXT_WORDS):
        return weak, hits, "weak"

    return None


def tokenize(title: str) -> set:
    """제목에서 의미 있는 단어만 뽑아냅니다. 매체 간 중복 판정용.

    조사를 떼고 유의어를 통일해서, 표현이 달라도 같은 사고로 인식되게 합니다.
    """
    out = set()
    for raw in _TOKEN_RE.findall(title or ""):
        tok = _normalize(raw)
        if tok and tok not in _STOPWORDS and len(tok) >= 2:
            out.add(tok)
    return out


def similarity(a: set, b: set) -> float:
    """자카드 유사도. 같은 사고를 다른 매체가 쓴 기사를 잡아냅니다."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
