# QUANT ANTIGRAVITY — 한국 주식 자동매매 대시보드

KOSPI/KOSDAQ 종목을 장전·장중으로 스크리닝하고, LLM이 장중 시황·딥리서치·미국
공시 촉매를 종합하며, 실시간 시세 기반 자동매매 시뮬레이션을 국면(regime)에 맞춰
운용하는 웹 대시보드입니다.

🔗 **Live Demo:** https://backteststock-production.up.railway.app

> 학습·시뮬레이션용 프로젝트입니다. 표시되는 신호·리포트는 자동 생성된 것으로
> **투자 권유가 아닙니다.** 자세한 내용은 맨 아래 면책 조항을 참고하세요.

---

## 주요 기능

### 종목 스크리닝 (2트랙)
- **장전 워치리스트** — 매일 장 시작 전, 전일 미국시장 + 국내 뉴스/공시(DART)를
  분석해 코스피200·코스닥150에서 '오늘 급등 예상 종목'을 촉매 중심으로 선별.
  자동매매 시뮬레이션 대상.
- **장중 관심종목** — 장중 30분 간격으로 '지금 막 이슈가 터진 종목'을 누적 선별
  (모니터링 전용). 시황 상방 촉매를 후보 풀에 시드해 뉴스 기반 대형주 발굴 갭을 보완.
- **하락 관찰** — 급락·악재 종목을 별도 트랙으로 표시(참고용, 자동매수 제외).

### 장중 시황판 (30분 회차, LLM 종합)
- 지수·투자자 수급(1분 누적 시계열)·섹터 히트·섹터↔테마 매칭·환율/지표를 종합한
  장중 브리핑과 **종목별 촉매**(상방/중립/하방).
- **경제지표 캘린더** — 네이버 경제캘린더 + ForexFactory 컨센서스, **관세청 수출입
  통계**(월간 YoY·품목·수입 10일 잠정).
- **실적발표 캘린더** — WiseReport(일정·컨센서스) + FnGuide 실적속보 + DART 공시,
  네이버/FnGuide 종목 페이지로 컨센서스·YoY 보강.

### 국면(Regime) 연동 모의투자
- 룰베이스 자동매매 엔진(시드 5% + 분할 진입, 5단계 익절/손절, EOD 복리 이월)이
  **장중 시황 LLM의 국면 판정을 소비**해 하락 국면을 방어.
- 하락(risk_off) → 신규·추가 매수 차단, 확신 높으면(high) 보유 전량 조기 청산
  (수익=국면 익절 / 손실=방어 청산). 상승·중립은 현행 룰 그대로.
- "LLM은 판단, 엔진은 결정적 룰 집행" — 새 LLM 호출 0, 비용 0, 감사 가능.

### 딥리서치
- 선정 종목별 해외 peer 시세·뉴스·커뮤니티·DART 공시를 수집해 LLM이 요약.
  peer는 큐레이션이 없으면 LLM 제안 + yfinance 티커 검증으로 동적 해결.
- 수집 ~20콜을 futures 그래프로 병렬화(온디맨드 ~30s). 방향성 신호가 상충하는
  종목은 조건부 후속 라운드(화이트리스트 도구 질의)로 심화.

### 수급 탭 (KIS 허브 연동)
- 외국인·기관 일별 매매동향, 공매도 20일 추이, 대차거래 잔고, VI·상하한가를
  KIS 실매매 허브(별도 레포)에서 Redis 경유로 받아 표시.

### 미국 공시 촉매 (모닝브리프)
- 미국 정규장 시간대(KST 밤) 30분 간격으로 SEC 8-K/6-K 신규 공시를 스캔
  (edgartools), 한국 연관·주도주 촉매를 모닝브리프에 반영.

### 섹터 분석 · 퀀트 팩터
- 섹터 국면/센티먼트/ETF 카드, 섹터 8항목 스코어카드 + Q점수 6팩터·게이트.

---

## 아키텍처

배포는 **Railway 상주 Python 서버**(`railway_server.py`)가 담당합니다. 이 서버는
세 가지 역할을 합니다.

1. **대시보드 서빙** — `public/index.html` + 에셋은 컨테이너 로컬본, 데이터 JSON은
   GitHub raw 프록시로 서빙. 리포트 커밋은 `[skip railway]`라 재배포 없이도 항상
   최신 데이터가 보입니다.
2. **정확한 KST 스케줄러** — GitHub cron은 지연이 커서, 이 상주 서버가 **정확한
   KST 시각에 `workflow_dispatch`로 각 워크플로를 트리거**합니다. (워크플로의
   `schedule:` 블록은 백업일 뿐 실질 트리거는 이 서버)
3. **API 프록시** — 실시간 시세(`/api/prices`), VI·상하한가, 딥리서치 온디맨드,
   매매일지 등.

```
┌────────────────────┐   workflow_dispatch    ┌─────────────────────┐
│  Railway 상주 서버   │ ─── (정확한 KST 시각) ──▶ │   GitHub Actions     │
│  railway_server.py │                        │   파이프라인 실행      │
│  ├ 대시보드 서빙     │ ◀── GitHub raw (JSON) ── │   → public/*.json 커밋 │
│  ├ KST 스케줄러      │                        └─────────────────────┘
│  └ /api/* 프록시     │
└────────────────────┘
        ▲  KIS 허브(별도 레포) → Redis → 수급·VI·상하한가
```

LLM은 폴백 체인(`llm.py`) — 기본 Google Gemini, 쿼터 소진 시 Anthropic으로 자동 전환.

---

## 파이프라인 스케줄 (KST, 월–금)

| 시각 | 워크플로 | 산출 |
|------|----------|------|
| 07:43 | `daily_report.yml` | 장전 워치리스트 + 리포트 + 딥리서치 + 브리핑 + 섹터 |
| 07:50 | `investment_warning.yml` | 투자주의/경고 지정·해제 계산 |
| 09:07~14:37 (30분) | `intraday_screener.yml` | 장중 관심종목 + 시황판 + 수급 + 경제/실적 캘린더 |
| 15:40 | `closing_briefing.yml` | 마감 시황(당일 회차 종합) |
| 22:47~04:47 (30분) | `us_night_catalysts.yml` | 미국 SEC 공시 촉매 |
| 월 06:30 | `theme_map.yml` | 테마맵·섹터맵 재생성 |
| 매월 2일 · 반기 | `build_corp_map.yml` · `index_constituents.yml` | 기업 코드맵 · 지수 구성 |

각 워크플로는 Actions 탭의 **Run workflow**로 수동 트리거도 가능합니다.

---

## 프로젝트 구조

```
backtest_stock/
├── railway_server.py          # ★ Railway 상주 서버 (대시보드 서빙 + KST 스케줄러 + API)
├── Procfile                   # web: python railway_server.py
│
├── public/
│   ├── index.html             # 대시보드 UI (단일 파일 — 워치리스트/관심종목/수급/시황 탭)
│   ├── daily_market_report.json    # 장전 워치리스트 리포트
│   ├── intraday_report.json        # 장중 관심종목 리포트
│   ├── intraday_briefing.json      # 장중 시황판 (30분 회차)
│   ├── down_market_report.json     # 하락 관찰 (장전/장중)
│   ├── sector_analysis.json        # 섹터 분석 + 퀀트 팩터
│   ├── econ_calendar.json          # 경제지표 + 관세청 수출입 통계
│   ├── earnings_calendar.json      # 실적발표 캘린더
│   ├── us_catalysts.json           # 미국 SEC 공시 촉매
│   ├── assets/                     # KRX 종목 마스터 등 정적 에셋
│   └── reports/                    # 일자별 아카이브 (워치리스트·시황·선정 근거)
│
├── 스크리닝·리포트
│   ├── screener.py            # 종목 선정 (SCREEN_MODE=pre|intraday)
│   ├── generate_report.py     # 진입가·목표가·손절가 산출
│   └── generate_analysis.py   # 딥리서치 분석 (병렬 수집 + 조건부 후속 라운드)
│
├── 시황·브리핑
│   ├── generate_briefing.py          # 모닝 브리핑 (전일 미국장 + 당일 프리뷰)
│   ├── generate_intraday_briefing.py # 장중/마감 시황 + 국면(regime) 판정
│   ├── generate_eod_close.py         # 장 마감 정산
│   ├── refresh_flow.py               # 수급만 갱신 (LLM 미사용, 회차마다)
│   └── generate_us_catalysts.py      # 미국 SEC 공시 촉매 (edgartools)
│
├── 캘린더·데이터 수집
│   ├── fetch_econ_calendar.py        # 경제지표 + 관세청 수출입 통계
│   ├── fetch_earnings_calendar.py    # 실적발표 캘린더 (WiseReport/FnGuide/DART/네이버)
│   ├── fetch_investment_warning.py   # 투자주의/경고
│   └── fetch_index_constituents.py   # 코스피200/코스닥150 구성
│
├── 섹터·테마·팩터
│   ├── generate_sector.py     # 섹터 분석 + 퀀트 스코어카드
│   ├── build_theme_map.py     # 섹터↔테마 매칭 맵
│   └── build_krx_sector_map.py / build_sector_map_auto.py
│
├── analysis/                  # 딥리서치 RAW 수집기 (sources.py, peers.json)
├── llm.py                     # LLM 폴백 체인 (Gemini → Anthropic)
├── ticker_utils.py            # 종목코드 → Yahoo 티커 변환
├── tests/                     # 파서·게이트 회귀 테스트 (픽스처 기반, 오프라인)
├── docs/DEEP_RESEARCH.md      # 딥리서치 설계 문서
├── .github/workflows/         # 파이프라인 (Railway 스케줄러가 dispatch)
└── requirements.txt
```

---

## 로컬 실행

```bash
pip install -r requirements.txt
```

대시보드만 정적으로 미리보기:

```bash
cd public && python -m http.server 8123
```

브라우저에서 http://localhost:8123 접속. (데이터 JSON은 마지막 커밋 스냅샷이 보이며,
실시간 시세·프록시 API는 Railway 서버에서만 동작합니다.)

파이프라인 스크립트 수동 실행 예:

```bash
# 장전 워치리스트
python screener.py            # 종목 선정 → watchlist.json
python generate_report.py     # 가격 레벨 리포트
python generate_analysis.py   # 딥리서치 분석 (LLM API 키 필요)

# 장중 관심종목 (모드/경로를 env로 지정)
SCREEN_MODE=intraday python screener.py
python generate_intraday_briefing.py   # 장중 시황 + 국면 판정
```

> ⚠️ 외부 API 호출(수집·LLM)은 크레덴셜과 해외 IP 접근성이 필요해 실제 데이터
> 파이프라인은 **GitHub Actions / Railway에서** 실행합니다.

---

## 기술 스택

- **Frontend:** Vanilla JS · HTML · CSS (단일 `index.html`)
- **Backend:** Python `http.server` 기반 상주 서버 (Railway)
- **Data:** yfinance(미국·실시간), FinanceDataReader(KRX), 네이버 금융/뉴스,
  DART OpenAPI, 관세청(data.go.kr), edgartools(SEC), KIS 실매매 허브(수급·VI)
- **LLM:** 폴백 체인 (`llm.py` — Google Gemini 기본, Anthropic 폴백)
- **자동화:** GitHub Actions (Railway 상주 스케줄러가 `workflow_dispatch`)
- **배포:** Railway

---

## ⚠️ 면책 조항

본 프로젝트는 **학습 및 시뮬레이션 목적**으로 제작되었습니다.
표시되는 매매 신호·리포트는 자동 생성된 기술적 지표이며 **투자 권유가 아닙니다.**
모의투자는 가상 계좌 시뮬레이션이며 실제 주문·체결과 무관합니다.
실제 투자 판단과 그에 따른 책임은 전적으로 사용자 본인에게 있습니다.
