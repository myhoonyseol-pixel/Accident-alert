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
    # 같은 곳을 가리키는 말은 한 단어로 모읍니다.
    # A사는 '공사현장', B사는 '신축현장'으로 써도 같은 사고로 인식되게.
    "공사현장": "건설현장", "신축현장": "건설현장", "공사장": "건설현장",
    "작업현장": "건설현장", "시공현장": "건설현장", "건축현장": "건설현장",
}


def _normalize(tok: str) -> str:
    for p in _PARTICLES:
        if len(tok) > len(p) + 1 and tok.endswith(p):
            tok = tok[: -len(p)]
            break
    return _SYNONYMS.get(tok, tok)


# 나라 이름을 그냥 문자열 포함으로 찾으면 한국 지명에 걸립니다.
#   구[미국]가산단(경북 구미) → '미국'
#   [중국]집 화재, [일본]식 주점 → 국내 기사인데 해외로 오인
# 그래서 앞뒤를 함께 봅니다.
#   앞: 한글이 붙어 있으면 다른 단어의 일부다 → 무시   (구미국가산단, 재미교포)
#   뒤: 국적·물건을 뜻하는 말이 오면 해외 기사가 아니다 → 무시
#       (중국인 근로자, 베트남 국적 작업자, 중국산 자재)
_FOREIGN_TAIL = r"(?!\s*(?:인|계|국적|어|산|집|제|식|풍|말|교포|동포))"
_foreign_cache = {}


def _foreign_re(cfg):
    places = tuple(getattr(cfg, "FOREIGN_PLACES", ()))
    if not places:
        return None
    if places not in _foreign_cache:
        # 긴 이름부터 맞춰야 '인도네시아'가 '인도'로 잘리지 않습니다.
        alt = "|".join(re.escape(p) for p in sorted(places, key=len, reverse=True))
        _foreign_cache[places] = re.compile(rf"(?<![가-힣])(?:{alt}){_FOREIGN_TAIL}")
    return _foreign_cache[places]


def is_foreign(cfg, text: str) -> bool:
    """해외에서 난 사고 기사인가.

    국내 건설사 이름이 함께 나오면 해외 현장이라도 우리가 알아야 하므로,
    이 판정과 별개로 호출부에서 살려둡니다.
    """
    if any(s in text for s in getattr(cfg, "FOREIGN_SIGNALS", ())):
        return True
    rx = _foreign_re(cfg)
    return bool(rx and rx.search(text))


def is_foreign_outlet(cfg, outlet: str) -> bool:
    """기사를 쓴 매체가 외국 매체인가.

    구글 뉴스 RSS는 항목마다 <source url="..."> 로 매체 주소를 함께 줍니다.
    지명은 목록으로 다 막을 수 없지만 매체 도메인은 고정이라 확실합니다.
    (ko.laodong.vn = 베트남 라오동신문 한국어판)
    """
    host = re.sub(r"^https?://", "", (outlet or "").lower()).split("/")[0]
    host = host.split(":")[0].strip().rstrip(".")
    if not host:
        return False
    for d in getattr(cfg, "FOREIGN_OUTLET_DOMAINS", ()):
        if host == d or host.endswith("." + d):
            return True
    return any(host.endswith(t) for t in getattr(cfg, "FOREIGN_OUTLET_TLDS", ()))


def has_company(cfg, text: str) -> bool:
    packed = re.sub(r"\s+", "", text)
    return any(c in packed for c in getattr(cfg, "COMPANY_WORDS", ()))


def match(cfg, title, summary="", outlet=""):
    """조건을 만족하면 (장소단어, 사고단어목록, 신뢰도) 반환, 아니면 None.

    신뢰도: 'strong' = 건설현장이 명시됨 / 'weak' = 맥락으로 추정
    outlet: 기사를 쓴 매체 주소 (구글 뉴스 RSS가 알려줍니다). 없으면 빈 문자열.
    """
    title = title or ""
    summary = summary or ""

    # 0) 함정 단어를 먼저 지운다.
    #    한국어는 띄어쓰기가 없어 기관명 속에 키워드가 숨는다.
    #    '대전도시공사' → 전도(사고) + 공사(맥락) 로 오인되어 홍보 기사가 발송된 적 있음.
    for trap in getattr(cfg, "TRAP_WORDS", ()):
        title = title.replace(trap, " ")
        summary = summary.replace(trap, " ")

    text = f"{title} {summary}"

    # 1) 제외어 판정은 '제목'만 본다.
    #    요약에 스치듯 나온 단어 때문에 진짜 속보를 버리지 않기 위해서.
    if any(w in title for w in cfg.EXCLUDE_WORDS):
        return None

    # 2) 사고 키워드가 없으면 사고 기사가 아니다.
    hits = [w for w in cfg.ACCIDENT_WORDS if w in text]
    if not hits:
        return None

    # 2-2) 해외 사고는 제외한다. 두 가지로 본다.
    #        · 본문에 나라 이름이나 '현지시간' 같은 표현이 있는가
    #        · 기사를 쓴 매체가 외국 매체인가  ← 지명 목록에 없는 지명을 잡는다
    #      단 국내 건설사가 시공 중인 해외 현장 사고는 우리가 알아야 하므로 남긴다.
    if (is_foreign(cfg, text) or is_foreign_outlet(cfg, outlet)) \
            and not has_company(cfg, text):
        return None

    # 3) 장소 판정 — 띄어쓰기를 없앤 문장으로 본다.
    #    매체마다 '공사현장' / '공사 현장' 이 갈려서, 그대로 두면 같은 사고를 놓칩니다.
    #    (사고 키워드는 원문 그대로 본다. 띄어쓰기를 지우면 '안전 도모' → '안전도모'
    #     안에서 '전도'가 튀어나오는 식의 새 오탐이 생기기 때문.)
    packed = re.sub(r"\s+", "", text)

    strong = next((w for w in cfg.STRONG_PLACE_WORDS if w in packed), None)
    if strong:
        return strong, hits, "strong"

    # 3-2) 속보 대응 — '공사 중', '갱폼' 같은 건설 작업 표현.
    #      사고 직후 속보에는 '현장'도 회사명도 없이 이것만 나옵니다.
    #      ("[속보] 9층 건물 공사 중 붕괴…1명 사망")
    #      띄어쓰기가 의미를 가르므로(도로공사 중부 vs 공사 중) 원문으로 봅니다.
    work = next((w for w in getattr(cfg, "WORK_WORDS", ()) if w in text), None)
    if work:
        return work, hits, "strong"

    # 3-3) 장소 표현이 없어도 주요 건설사 이름이 나오면 건설 사고로 봅니다.
    #      재해개요형 기사("갱폼 인상 작업 중 근로자 추락 사망")에는 '현장'이라는
    #      말이 아예 없는 경우가 많은데, 시공사 이름은 거의 항상 실립니다.
    corp = next((w for w in getattr(cfg, "COMPANY_WORDS", ()) if w in packed), None)
    if corp:
        return corp, hits, "company"

    # 3-4) 제조업 공장·물류창고. 건설현장은 아니지만 우리 관심 범위입니다.
    #      '화재·폭발이면 인명피해 없어도 / 그 밖엔 사망·중상일 때만' 이라는
    #      조건은 여기서 따지지 않고 AI에게 맡깁니다. 키워드로 가르면
    #      "인명피해 확인 중" 같은 초기 속보를 놓치기 때문입니다.
    plant = next((w for w in getattr(cfg, "PLANT_PLACE_WORDS", ()) if w in packed), None)
    if plant:
        return plant, hits, "plant"

    # 4) WEAK은 산업/건설 맥락이 함께 있을 때만 통과
    weak = next((w for w in cfg.WEAK_PLACE_WORDS if w in packed), None)
    if weak and any(c in text for c in cfg.CONTEXT_WORDS):
        return weak, hits, "weak"

    return None


# 구글 뉴스 RSS 제목은 항상 "기사제목 - 매체명" 형태로 끝납니다.
# 이 꼬리를 떼지 않으면 카카오톡이 본문 속 매체 도메인(ctnews.kr 등)을
# 자동으로 링크로 만들어, 기사가 아니라 언론사 홈페이지로 가버립니다.
_SOURCE_TAIL_RE = re.compile(r"\s+[-–—]\s+[^-–—]{1,40}$")

# '공사 현장' → '공사현장' 처럼, 띄어 쓴 장소 표현을 붙여줍니다.
_SPACED_PLACE_RE = re.compile(r"(공사|신축|건설|작업|시공|건축|철거|재개발|재건축)\s+현장")


def strip_source_tail(title: str) -> str:
    return _SOURCE_TAIL_RE.sub("", title or "").strip()


# 사고 기사라면 거의 다 들어있는 흔한 말들.
# 중복 판정에서 '겹쳤다'로 쳐주면 서로 다른 사고가 한 건으로 묶여버립니다.
_COMMON_DOMAIN = {
    "공사장", "공사현장", "건설현장", "신축현장", "작업현장", "사업장",
    "사망", "부상", "숨진", "작업자", "크레인", "붕괴", "추락", "화재",
    "매몰", "끼임", "전도", "낙하", "폭발", "중대재해", "아파트", "근처",
}


def distinctive(tokens: set) -> set:
    """그 사고를 특정해 주는 단어만 남깁니다 (지역명, 회사명, 시설명 등)."""
    return {t for t in tokens if t not in _COMMON_DOMAIN and not t.isdigit()}


def is_duplicate(cfg, tokens: set, previous: list) -> bool:
    """이미 보낸 기사와 같은 사고인지 판정합니다.

    두 가지로 봅니다.
      1) 제목 전체가 비슷하다 (자카드 유사도)
      2) 그 사고를 특정하는 고유한 단어가 여러 개 겹친다
    2번이 있어야 제목 표현이 크게 달라도 같은 사고를 잡아냅니다.
    """
    min_shared = getattr(cfg, "DUP_MIN_SHARED", 3)
    mine = distinctive(tokens)
    for prev in previous:
        if similarity(tokens, prev) >= cfg.DUP_SIMILARITY:
            return True
        if min_shared and len(mine & distinctive(prev)) >= min_shared:
            return True
    return False


def same_event(cfg, tokens: set, prev: set) -> float:
    """두 제목이 같은 사고인가. 같으면 유사도(0~1), 아니면 0.

    is_duplicate 와 판정 기준은 같지만, '예/아니오'가 아니라 **어느 사고와**
    겹쳤는지 골라내야 할 때 씁니다(보도 확산 집계).
    """
    if not prev:
        return 0.0
    s = similarity(tokens, prev)
    if s >= cfg.DUP_SIMILARITY:
        return s
    min_shared = getattr(cfg, "DUP_MIN_SHARED", 3)
    if min_shared and len(distinctive(tokens) & distinctive(prev)) >= min_shared:
        return max(s, 0.01)      # 기준은 넘었으니 0이 아닌 값을 준다
    return 0.0


def tokenize(title: str) -> set:
    """제목에서 의미 있는 단어만 뽑아냅니다. 매체 간 중복 판정용.

    조사를 떼고 유의어를 통일해서, 표현이 달라도 같은 사고로 인식되게 합니다.
    """
    # '공사 현장' 처럼 띄어 쓴 것을 '공사현장' 으로 붙여 한 단어로 셉니다.
    # 안 그러면 '현장'이 흔한 말로 버려지면서 같은 사고인 걸 못 알아봅니다.
    title = _SPACED_PLACE_RE.sub(r"\1현장", title or "")

    out = set()
    for raw in _TOKEN_RE.findall(title):
        tok = _normalize(raw)
        if tok and tok not in _STOPWORDS and len(tok) >= 2:
            out.add(tok)
    return out


def similarity(a: set, b: set) -> float:
    """자카드 유사도. 같은 사고를 다른 매체가 쓴 기사를 잡아냅니다."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
