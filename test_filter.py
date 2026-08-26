# -*- coding: utf-8 -*-
"""필터를 실제 기사 제목 형태에 돌려보는 회귀 테스트.

  python test_filter.py            → 현재 config.py 평가
  python test_filter.py config_old → 비교 대상 평가
"""
import importlib
import sys

import filters

# TRUE  = 반드시 받아야 하는 사고
# FALSE = 오면 안 되는 노이즈
SAMPLES = [
    # ── 진짜 받아야 하는 것 ──────────────────────────────
    ("TRUE",  "아파트 신축현장서 거푸집 붕괴…근로자 2명 매몰"),
    ("TRUE",  "물류센터 공사장 크레인 전도, 1명 사망 3명 부상"),
    ("TRUE",  "[속보] 화성 공사현장 타워크레인 붕괴…인명피해 확인 중"),
    ("TRUE",  "지하철 공사장 토사 붕괴로 작업자 1명 매몰"),
    ("TRUE",  "건설현장 철골 낙하 사고…60대 근로자 숨져"),
    ("TRUE",  "울산 석유화학 플랜트 건설현장 폭발, 4명 부상"),
    ("TRUE",  "인천 재개발 철거현장 외벽 붕괴…행인 1명 부상"),
    ("TRUE",  "제조업 사업장서 프레스 끼임 사고…30대 노동자 사망"),
    ("TRUE",  "고양 오피스텔 공사현장 지하 굴착 중 흙막이 붕괴"),
    ("TRUE",  "김해 물류창고 신축현장 화재…작업자 2명 연기 흡입"),

    # ── 오면 안 되는 노이즈 ──────────────────────────────
    ("FALSE", "고속도로 8중 추돌 사고 현장…2명 사망 12명 부상"),
    ("FALSE", "새벽 주택 화재 현장서 60대 부부 숨진 채 발견"),
    ("FALSE", "경찰, 살인 사건 현장 감식 착수…용의자 추적"),
    ("FALSE", "소방당국 화재 현장 도착 5분 만에 진화"),
    ("FALSE", "드라마 촬영 현장서 배우 낙하 장면 촬영 중 부상"),
    ("FALSE", "산불 현장 헬기 투입…진화율 70%"),
    ("FALSE", "중대재해처벌법 시행 3년, 사업장 안전점검 강화 방침"),
    ("FALSE", "고용부, 취약 사업장 특별점검 실시…추락 예방 집중"),
    ("FALSE", "건설사 주가 급락, 사망사고 여파에 투자심리 위축"),
    ("FALSE", "지난해 산재 사망 598명…건설업 사업장 비중 최다"),
    ("FALSE", "무단횡단 보행자 차량 충돌…현장서 사망"),
    ("FALSE", "축제 현장 부스 전도 사고, 관람객 2명 경상"),
    ("FALSE", "건설현장 추락 예방 캠페인 전개…안전모 착용 결의"),
    ("FALSE", "대형 건설사 사망사고 원청 대표 구속 기소"),
    ("FALSE", "건설현장 중대재해 대응 모의훈련 실시"),
    ("FALSE", "공사장 붕괴 사고 유족, 손해배상 소송 제기"),
]

# 같은 사고를 서로 다른 매체가 쓴 제목 — 1건으로 묶여야 함
DUP_GROUP = [
    "화성 공사현장 타워크레인 붕괴…2명 사망",
    "[속보] 화성 타워크레인 붕괴 사고, 공사현장 2명 사망",
    "화성 공사현장서 타워크레인 무너져 2명 숨져",
]


def evaluate(cfg, label):
    tp = fp = fn = 0
    problems = []
    for expect, title in SAMPLES:
        got = filters.match(cfg, title)
        fired = got is not None
        if expect == "TRUE":
            if fired:
                tp += 1
            else:
                fn += 1
                problems.append(("놓침", title))
        else:
            if fired:
                fp += 1
                problems.append((f"오탐:{got[0]}", title))

    n_true = sum(1 for e, _ in SAMPLES if e == "TRUE")
    n_false = len(SAMPLES) - n_true
    noise_rate = round(fp / (tp + fp) * 100) if (tp + fp) else 0

    print(f"\n{'=' * 68}\n{label}\n{'=' * 68}")
    print(f"  받아야 할 사고 {n_true}건 중 감지  : {tp}건  (놓침 {fn}건)")
    print(f"  걸러야 할 노이즈 {n_false}건 중 오탐: {fp}건")
    print(f"  → 알림이 100건 오면 그중 약 {noise_rate}%가 헛알림")
    if problems:
        print("  문제 사례:")
        for kind, t in problems:
            print(f"    [{kind:11}] {t}")
    else:
        print("  문제 사례 없음")
    return noise_rate


def check_dedup(cfg):
    print(f"\n{'=' * 68}\n같은 사고 중복 제거 (매체 3곳이 같은 사고를 보도)\n{'=' * 68}")
    kept, token_sets = [], []
    for title in DUP_GROUP:
        toks = filters.tokenize(title)
        if any(filters.similarity(toks, prev) >= cfg.DUP_SIMILARITY for prev in token_sets):
            print(f"  [중복차단] {title}")
            continue
        token_sets.append(toks)
        kept.append(title)
        print(f"  [발송   ] {title}")
    print(f"  → 기사 {len(DUP_GROUP)}건 → 알림 {len(kept)}건")
    return len(kept)


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "config"
    cfg = importlib.import_module(name)
    label = "개선안 (config.py)" if name == "config" else f"원본 ({name}.py)"
    evaluate(cfg, label)
    if name == "config":
        check_dedup(cfg)
