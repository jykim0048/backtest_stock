# QUANT ANTIGRAVITY — 자동매매 대시보드

한국 주식(KOSPI/KOSDAQ) 모멘텀 종목을 추적하고, 매일 자동으로 리포트를 생성하며,
실시간 시세 기반 자동매매 시뮬레이션을 보여주는 웹 대시보드입니다.

🔗 **Live Demo:** https://backtest-stock.vercel.app

---

## 주요 기능

- **실시간 시세 피드** — Yahoo Finance(yfinance)에서 관심 종목 현재가를 5초마다 갱신
- **자동매매 시뮬레이션** — 시초가 갭 필터, 분할 매수, 익절/손절 로직을 대시보드에서 시각화
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
│       └── YYYY-MM-DD.json   # 일자별 리포트 아카이브
├── .github/workflows/
│   └── daily_report.yml      # 매일 08:00 KST 리포트 자동 생성
├── generate_report.py        # 리포트 생성 스크립트
├── ticker_utils.py           # 종목코드 → Yahoo 티커 변환 (공유 모듈)
├── watchlist.json            # 관심 종목 설정
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

## 리포트 생성

### 수동 실행

```bash
python generate_report.py
```

`watchlist.json`에 등록된 종목들의 데이터를 가져와 다음 파일을 생성/갱신합니다.

- `public/daily_market_report.json` — 대시보드가 읽는 최신 리포트
- `public/reports/YYYY-MM-DD.json` — 일자별 아카이브
- `public/reports/index.json` — 날짜 목록

### 자동 실행 (GitHub Actions)

`.github/workflows/daily_report.yml`이 **매일 08:00 KST(월–금)**에 자동 실행되어
리포트를 생성하고 커밋·푸시합니다. Actions 탭에서 **Run workflow**로 수동 트리거도 가능합니다.

---

## 관심 종목 변경

`watchlist.json`만 수정하면 리포트 대상 종목이 바뀝니다.

```json
[
  { "code": "066570", "name": "LG전자",   "market": "KOSPI"  },
  { "code": "035420", "name": "네이버",   "market": "KOSPI"  },
  { "code": "090360", "name": "로보스타", "market": "KOSDAQ" }
]
```

- `code` — 6자리 종목코드
- `market` — `KOSPI`(→ `.KS`) 또는 `KOSDAQ`(→ `.KQ`)

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
- **Data:** [yfinance](https://github.com/ranaroussi/yfinance) (Yahoo Finance)
- **자동화:** GitHub Actions (cron)
- **배포:** Vercel

---

## ⚠️ 면책 조항

본 프로젝트는 **학습 및 시뮬레이션 목적**으로 제작되었습니다.
표시되는 매매 신호·리포트는 자동 생성된 기술적 지표이며 **투자 권유가 아닙니다.**
실제 투자 판단과 그에 따른 책임은 전적으로 사용자 본인에게 있습니다.
