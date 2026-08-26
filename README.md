# 건설현장 사고속보 감시기

국내 건설현장 대형 사고 뉴스를 20분마다 감시해서, 처음 보는 사고만
카카오톡 '나에게 보내기'로 즉시 쏩니다.

```
뉴스 수집 → 사고 판정 → 중복 제거 → 카카오톡 발송
(네이버 API   (filters.py)  (제목 유사도)   (나에게 보내기)
 + 구글뉴스)
```

---

## 1. 준비물 4개

| 이름 | 어디서 | 필수 |
|---|---|---|
| `KAKAO_REST_API_KEY` | 카카오 디벨로퍼스 → 앱 › 일반 › 플랫폼 키 › REST API 키 | ✅ |
| `KAKAO_REFRESH_TOKEN` | `get_token.py` 실행으로 발급 | ✅ |
| `KAKAO_CLIENT_SECRET` | 같은 화면의 '클라이언트 시크릿' | 켰다면 ✅ |
| `NAVER_CLIENT_ID` | [NAVER API HUB](https://www.ncloud.com/product/applicationService/naverApiHub) → Application 등록 | 선택 |
| `NAVER_CLIENT_SECRET` | 위와 동일 | 선택 |

> **네이버 검색 API가 이사했습니다.**
> 네이버 개발자센터(developers.naver.com)의 '사용 API' 목록에는 이제 **검색이 없습니다.**
> 검색 API는 **NAVER API HUB**(네이버 클라우드 플랫폼)로 옮겨졌고, 신규 발급은 거기서만 됩니다.
> 예전에 받아둔 개발자센터 키가 있다면 `config.py`의 `NAVER_MODE = "legacy"` 로 두면
> 2027년 6월 30일까지는 그대로 동작합니다.
>
> | | 신규 (HUB) | 예전 (개발자센터) |
> |---|---|---|
> | 주소 | `naverapihub.apigw.ntruss.com/search/v1/news` | `openapi.naver.com/v1/search/news.json` |
> | 헤더 | `X-NCP-APIGW-API-KEY-ID` / `X-NCP-APIGW-API-KEY` | `X-Naver-Client-Id` / `X-Naver-Client-Secret` |
> | `NAVER_MODE` | `"hub"` | `"legacy"` |
>
> **네이버 없이도 굴러갑니다.** 키를 비워두면 구글 뉴스 RSS만으로 동작합니다
> (`NAVER_MODE = "off"`). 우선 RSS로 시작해서 놓치는 게 많으면 그때 HUB를 붙이세요.

카카오 앱 설정에서 아래 세 가지가 되어 있어야 합니다.

1. 제품 설정 › 카카오 로그인 → **활성화 ON** (안 켜면 `KOE004`)
2. **앱 › 일반 › 플랫폼 키 › REST API 키**(클릭해서 상세 진입) → 리다이렉트 URI에
   `https://example.com/oauth` (안 하면 `KOE006`)
   – 콘솔 개편으로 이 항목이 '카카오 로그인' 메뉴에서 앱 키 하위로 옮겨졌습니다
3. 카카오 로그인 › 동의항목 → **카카오톡 메시지 전송(`talk_message`)** 을 '이용 중 동의'로
   (안 하면 `KOE205`)

같은 REST API 키 화면에 **클라이언트 시크릿**이 '사용함'으로 켜져 있다면,
그 값을 `KAKAO_CLIENT_SECRET` 으로 함께 등록해야 합니다. 빠뜨리면 토큰 갱신 때
`KOE010 Bad client credentials` 로 알림이 멈춥니다.

검수 신청은 필요 없습니다. 나에게 보내기는 검수 없이 씁니다.

### 카카오 토큰 발급

```bash
pip install -r requirements.txt
python get_token.py
```

출력된 URL을 브라우저에서 열고 → 동의 → 주소창의 `?code=...` 값을 붙여넣으면
`refresh_token`이 나옵니다.

> `code`는 **10분, 1회용**입니다. 실패하면 URL을 다시 열어 새로 받으면 됩니다.

---

## 2. GitHub에 올리기

1. GitHub 계정 생성 후 **새 저장소(repository)** 생성
   - **Public으로 만드세요.** Private은 무료 실행시간(월 2,000분)을 초과합니다.
     20분 주기 × 30일이면 약 2,200분이 나옵니다. Public은 무제한 무료이고,
     키는 코드가 아니라 Secrets에 들어가므로 노출되지 않습니다.
2. 이 폴더의 파일을 전부 업로드
3. 저장소 → **Settings › Secrets and variables › Actions › New repository secret**
   에서 위 4개를 하나씩 등록
4. **Actions** 탭 → `accident-news-alert` → **Run workflow** 로 수동 실행

첫 실행은 **알림을 보내지 않습니다.** 최근 기사를 '이미 본 것'으로 기록만 합니다
(안 그러면 12시간치 기사가 한꺼번에 쏟아집니다). 두 번째 실행부터 알림이 옵니다.

---

## 3. 파일 구성

| 파일 | 역할 |
|---|---|
| `config.py` | **키워드·주기 설정. 튜닝은 여기만 고치세요.** |
| `filters.py` | 사고 판정 + 중복 판정 로직 |
| `main.py` | 수집 → 판정 → 발송 |
| `get_token.py` | 카카오 토큰 발급 (최초 1회, 만료 시 재실행) |
| `test_filter.py` | 키워드를 고친 뒤 검증용. `python test_filter.py` |
| `state/seen.json` | 이미 보낸 기사 기록. 자동 생성·자동 커밋 |

---

## 4. 오탐이 오면

`config.py`의 `EXCLUDE_WORDS`에 그 기사 제목의 단어를 추가하고,
바꾼 뒤 반드시 테스트를 돌려서 **진짜 사고를 놓치지 않는지** 확인하세요.

```bash
python test_filter.py
```

```
받아야 할 사고 10건 중 감지  : 10건  (놓침 0건)
걸러야 할 노이즈 16건 중 오탐: 0건
```

`test_filter.py`의 `SAMPLES`에 실제로 받은 오탐 제목을 `FALSE`로,
놓친 사고를 `TRUE`로 추가해 두면 다음 튜닝 때 회귀 검증이 됩니다.

### 장소 키워드가 2단인 이유

`현장`, `사업장`은 교통사고·화재·범죄 기사에도 전부 나옵니다.
그래서 이 둘은 `CONTEXT_WORDS`(근로자, 타워크레인, 거푸집 …)가
함께 있을 때만 인정합니다. `건설현장`, `공사장` 같은 단어는 단독으로 통과합니다.

---

## 5. 알아둘 것

**토큰은 약 60일마다 갱신이 필요합니다.**
만료 14일 전부터 카카오톡으로 미리 알려주고, 갱신된 토큰은 Actions 로그에 찍힙니다.
갱신에 실패하면 워크플로가 **실패 처리**되어 GitHub이 메일을 보냅니다.
(조용히 멈추지 않게 하려는 장치입니다. GitHub 알림 설정에서 메일 수신을 켜두세요.)

**매일 오전 8시대에 '정상 작동 중' 메시지가 옵니다.**
알림이 없는 게 사고가 없어서인지 시스템이 죽어서인지 구분하기 위한 생존 신호입니다.
끄려면 `config.py`의 `HEARTBEAT_ENABLED = False`.

**GitHub 무료 스케줄러는 정시에 안 돕니다.**
혼잡할 때 5~20분 지연되거나 건너뛰기도 합니다. 실제 주기는 20~40분으로 보세요.

**네이버 API 사용량은 하루 약 1,100회입니다.**
NAVER API HUB 무료 한도는 월 775,000건, 키당 50 RPS입니다. 한참 여유롭습니다.
(HUB는 현재 무료이나 "유료 전환 시 별도 공지" 예정이라고 안내되어 있습니다)
