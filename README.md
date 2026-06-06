# QUANT ANTIGRAVITY — 자동매매 대시보드

한국 주식(KOSPI/KOSDAQ) 모멘텀 종목을 추적하고, 매일 자동으로 리포트를 생성하며,
실시간 시세 기반 자동매매 시뮬레이션을 보여주는 웹 대시보드입니다.

🔗 **Live Demo:** https://backtest-stock.vercel.app

---

## 주요 기능

- **장전 종목 자동 선정** — 매일 장 시작 전, 전일 미국시장 + 국내 뉴스/공시를 분석해 코스피200·코스닥150에서 '오늘 급등 예상 종목'을 선별
- **실시간 시세 피드** — Yahoo Finance(yfinance)에서 관심 종목 현재가를 5초마다 갱신
- **자동매매 시뮬레이션** — 시초가 갭 필터, 분할 매수, 익절/손절 로직을 대시보드에서 시각화
- **딥리서치 분석** — 선정 종목별 peer 시세·뉴스·커뮤니티·DART 공시를 LLM이 요약
- **일일 리포트 자동 생성** — 매일 GitHub Actions가 종목별 진입가·목표가·손절가를 산출
- **일자별 리포트 누적** — 날짜 선택 드롭다운으로 과거 리포트 조회 가능
- **데모 모드** — 장외 시간에도 시뮬레이션 틱으로 로직 테스트 가능

---

## 프로젝트 구조

```
backtest_stock/
├── api/
│   └── index.py              # Vercel 서버리스 함수 (/api/prices 실시간 시세)
├── local_server/
│   ├── server.py             # 로컬 개발용 통합 서버 (정적 파일 + API)
│   └── news_mcp.py           # 종목 뉴스 MCP 서버
├── public/                   # Vercel 정적 서빙 디렉토리
│   ├── index.html            # 대시보드 UI (단일 파일)
│   ├── daily_market_report.json  # 최신 리포트 (대시보드가 로드)
│   ├── assets/
│   │   └── krx_companies.json
│   └── reports/
│       ├── index.json        # 사용 가능한 리포트 날짜 목록
│       ├── selection/        # 일자별 종목 선정 근거 (장전 스크리닝 결과)
│       └── YYYY-MM-DD.json   # 일자별 리포트 아카이브
├── .github/workflows/
│   └── daily_report.yml      # 매일 08:00 KST 스크리닝 → 리포트 자동 생성
├── analysis/                 # 딥리서치용 RAW 데이터 수집기 (뉴스·DART·peer)
├── data/
│   └── index_constituents.json  # (선택) 코스피200·코스닥150 정확 멤버십 코드
├── screener.py               # 장전 종목 선정 스크립트 (1차 스크리닝)
├── generate_report.py        # 리포트 생성 스크립트
├── generate_analysis.py      # 딥리서치 분석 생성 스크립트
├── ticker_utils.py           # 종목코드 → Yahoo 티커 변환 (공유 모듈)
├── watchlist.json            # 선정 종목 (screener.py 가 매일 자동 갱신)
├── requirements.txt
└── vercel.json               # Vercel 라우팅 설정
```

---

## 로컬 실행

### 1. 의존성 설치

```bash
pip install -r requirements.txt
```

### 2. 로컬 서버 실행

```bash
python local_server/server.py
```

브라우저에서 http://localhost:8000 접속하면 대시보드가 열립니다.
실시간 시세는 `/api/prices` 엔드포인트가 제공합니다.

---

## 일일 자동화 파이프라인

매 거래일 **08:00 KST(월–금)**에 `.github/workflows/daily_report.yml`이 아래 4단계를 순서대로 실행하고, 결과를 커밋·푸시합니다. Actions 탭의 **Run workflow**로 수동 트리거도 가능합니다.

```
① screener.py        →  ② generate_report.py  →  ③ generate_analysis.py  →  커밋·푸시
   장전 종목 선정          가격 레벨 산출            딥리서치 분석
```

### ① 장전 종목 선정 — `screener.py`

장 시작 전, '오늘 급등 예상 종목'을 선별해 `watchlist.json`을 **자동 갱신**합니다.

1. **전일 미국시장 분석** — yfinance로 미국 지수(S&P500·나스닥·다우·필라델피아 반도체·VIX)와 섹터 ETF(기술·반도체·헬스케어·바이오·에너지·금융 등)의 전일 등락을 수집해 강세 섹터를 파악.
2. **유니버스 + 기계적 1차 필터** — FinanceDataReader KRX 스냅샷에서 **코스피200 + 코스닥150**(시가총액 상위 200/150 근사)을 유니버스로 잡고, 전일 **거래대금회전율·등락률·거래대금**을 종합 점수화해 상위 후보(기본 30종목)를 추림.
3. **뉴스/공시 보강** — 후보별 **전일 장 마감(15:30 KST) 이후 ~ 실행 시각**의 네이버 뉴스 헤드라인과 DART 최근(7일) 공시를 수집(`analysis/sources.py` 재사용). 주말은 직전 거래일(금요일) 마감 기준.
4. **LLM 최종 선정** — Claude가 미국시장 동향 + 후보의 뉴스·공시·모멘텀을 종합해 최종 종목(기본 6개)을 선정하고 사유를 작성.

산출물:
- `watchlist.json` — 선정 종목(`code`/`name`/`market`). 이후 단계가 소비.
- `public/reports/selection/YYYY-MM-DD.json` — 선정 근거(시장관 + 종목별 사유).

> **데이터 소스 메모:** 정확한 코스피200/코스닥150 멤버십은 KRX 로그인이 필요해 기본은 **시가총액 상위 근사**를 씁니다. `data/index_constituents.json`(`{"KOSPI200":[...],"KOSDAQ150":[...]}`)에 정확한 종목코드 명단을 두면 그 명단을 우선 사용합니다. 실시간 시세는 스크리닝에 불필요하며(전일 확정 데이터 사용), 대시보드 실시간 시세는 기존 yfinance `/api/prices`가 담당합니다.

### ② 가격 레벨 산출 — `generate_report.py`

`watchlist.json` 종목의 데이터를 가져와 진입가·목표가·손절가를 계산하고 다음을 생성/갱신합니다.

- `public/daily_market_report.json` — 대시보드가 읽는 최신 리포트
- `public/reports/YYYY-MM-DD.json` — 일자별 아카이브 / `public/reports/index.json` — 날짜 목록

### ③ 딥리서치 분석 — `generate_analysis.py`

선정 종목별 peer 시세·뉴스·커뮤니티·DART 공시를 LLM이 요약해 리포트에 `analysis` 필드로 병합합니다(best-effort).

### 수동 실행

```bash
python screener.py          # 종목 선정 → watchlist.json 갱신
python generate_report.py   # 가격 레벨 리포트 생성
python generate_analysis.py # 딥리서치 분석 병합 (API 키 필요)
```

---

## 관심 종목 변경

종목은 매일 `screener.py`가 자동 선정하지만, `watchlist.json`을 직접 편집해 **수동으로 고정**할 수도 있습니다(다음 자동 실행 시 다시 갱신됨).

```json
[
  { "code": "066570", "name": "LG전자",   "market": "KOSPI"  },
  { "code": "035420", "name": "네이버",   "market": "KOSPI"  },
  { "code": "090360", "name": "로보스타", "market": "KOSDAQ" }
]
```

- `code` — 6자리 종목코드
- `market` — `KOSPI`(→ `.KS`) 또는 `KOSDAQ`(→ `.KQ`)

선정 종목 수·후보 수는 환경변수로 조정합니다: `SCREEN_N_FINAL`(기본 6), `SCREEN_N_SHORTLIST`(기본 30).

---

## 배포 (Vercel)

1. [vercel.com](https://vercel.com)에서 GitHub 로그인 후 이 레포를 **Import**
2. Framework Preset은 **Other** 선택, 나머지는 기본값으로 **Deploy**
3. `git push`할 때마다 자동 재배포

`vercel.json`이 라우팅을 처리합니다.

| 경로 | 대상 |
|------|------|
| `/` | `public/index.html` (대시보드) |
| `/api/prices` | `api/index.py` (실시간 시세) |
| 기타 | `public/*` 정적 파일 |

> 모바일에서는 브라우저로 접속 후 **홈 화면에 추가**하면 앱처럼 사용할 수 있습니다.

---

## 기술 스택

- **Frontend:** Vanilla JS, HTML, CSS (단일 `index.html`)
- **Backend:** Python `http.server` (로컬), Vercel Python Serverless (배포)
- **Data:** [yfinance](https://github.com/ranaroussi/yfinance)(미국·실시간), [FinanceDataReader](https://github.com/FinanceData/FinanceDataReader)(KRX 유니버스), 네이버 뉴스 API, DART OpenAPI
- **선정/분석:** Anthropic Claude (장전 종목 선정 + 딥리서치)
- **자동화:** GitHub Actions (cron)
- **배포:** Vercel

---

## ⚠️ 면책 조항

본 프로젝트는 **학습 및 시뮬레이션 목적**으로 제작되었습니다.
표시되는 매매 신호·리포트는 자동 생성된 기술적 지표이며 **투자 권유가 아닙니다.**
실제 투자 판단과 그에 따른 책임은 전적으로 사용자 본인에게 있습니다.
