# Deep Research 기능 — 작업 프로세스 & 스펙

> 대시보드 종목 드로어에 **심층 리서치(Deep Research)** 분석을 붙이는 작업의 설계·진행 기록.
> 재로그인/세션이 바뀌어도 이 문서를 읽으면 맥락과 다음 단계를 이어갈 수 있다.
> 최종 갱신: 2026-06-03

---

## 1. 목표

종목 클릭 시 드로어에 기존 **Account** 정보(실시간 시세·보유현황·catalyst·scenario)에 더해,
다음 **4개 분석 컬럼**을 탭으로 제공한다.

| # | 컬럼 | 데이터 소스 |
|---|------|------|
| 1 | 🌐 해외 Peer 종목 가격 **+ Peer Reddit 여론** | yfinance + **Reddit**(해외 peer/섹터 검색) |
| 2 | 📰 해외·국내 뉴스 분석 | Naver(국내) + Tavily(해외) |
| 3 | 💬 네이버 종목토론방 (국내 개인투자자) | Naver(cafe/web) |
| 4 | 📑 DART 분석 | DART OpenAPI |

> **Reddit 위치 결정(2026-06-04):** Reddit은 한국 종목토론방(컬럼 3)이 아니라 **해외 Peer 그룹 분석(컬럼 1)** 에 쓴다. 한국 중소형주는 Reddit에 거의 안 잡히지만, 해외 peer(Eli Lilly·GE Vernova·NGK 등)는 활발히 논의되기 때문. 따라서 community(컬럼 3)는 **네이버만**, peers(컬럼 1)에 `reddit` 배열 추가.

MCP 서버 설정(로컬 인터랙티브용)은 레포 루트 `.mcp.json` 참조. 단 **배치는 MCP 미사용**(§8) — Reddit도 공개 JSON REST로 수집.

---

## 2. 드로어 UI 구조 (확정 스킴)

드로어 최상단에 **대분류 탭 2개**:

```
헤더 (종목명 / 코드 / 시장 / ✕)
─────────────────────────────
[ 📊 Account ]   [ 🔬 Deep Research ]   ← 대분류 탭 (.drawer-toptab)
─────────────────────────────
선택된 페인
```

- **📊 Account** (`#drawer-account-pane`) = LIVE 시세 + 실시간 보유 현황 + Market Moving Catalysts + Automated Trading Scenario. **기존 구조 그대로.**
- **🔬 Deep Research** (`#drawer-research-pane`) = 4-탭 분석(`.analysis-tab`: peers/news/community/dart).
- 드로어를 열면 항상 **Account가 기본 선택**.
- 전환 함수: `selectDrawerPane('account'|'research')`, 4-탭 전환: `selectAnalysisTab('peers'|'news'|'community'|'dart')`.

관련 코드 위치: `public/index.html`
- CSS: `.drawer-toptabs`, `.drawer-toptab`, `.analysis-tabs`, `.analysis-tab`, `.analysis-*`
- HTML: `#drawer-account-pane`, `#drawer-research-pane`, `#drawer-analysis-sec`
- JS: `selectDrawerPane()`, `selectAnalysisTab()`, `renderAnalysisContent()`, `sentimentBadge()`, `linkTitle()`, `escapeHtml()`
- 데이터 바인딩: `loadDynamicReportData()`가 `item.analysis`를 `stockData[].analysis`로 전달, `openDetailDrawer()`가 페인/탭 초기화.

---

## 3. 데이터 스키마 (A/B 호환의 핵심 — 절대 임의 변경 금지)

각 종목 객체(`daily_market_report.json` / `reports/YYYY-MM-DD.json`)에 `analysis` 필드 추가:

```jsonc
"analysis": {
  "peers": {
    "summary": "한국어 2~3문장",
    "items": [
      { "name": "Eli Lilly", "ticker": "LLY", "price": "$1,064.15", "changePct": -1.67, "note": "한 줄" }
    ],
    "reddit": [ { "title": "...", "url": "...", "subreddit": "r/...", "sentiment": "긍정|부정|중립", "summary": "한 줄(해외 peer/섹터 여론)" } ]
  },
  "news": {
    "summary": "한국어 3~4문장",
    "items": [
      { "title": "...", "source": "...", "date": "YYYY-MM-DD",
        "sentiment": "긍정|부정|중립", "url": "...", "insight": "한 줄" }
    ]
  },
  "community": {
    "summary": "한국어 3~4문장 (국내 개인투자자 여론만)",
    "sentimentLabel": "예: 전반적으로 중립~긍정",
    "naver":  [ { "title": "...", "url": "...", "sentiment": "긍정|부정|중립", "summary": "한 줄" } ]
  },
  "dart": {
    "summary": "한국어 3~4문장",
    "highlights":    [ { "label": "매출액(2025)", "value": "1조5,475억원", "note": "한 줄" } ],
    "recentFilings": [ { "date": "YYYY-MM-DD", "title": "...", "type": "...", "insight": "한 줄" } ]
  }
}
```

- `analysis`가 없는 종목은 UI에서 "데이터 없음" 으로 표시(정상 동작).
- 색상 규칙: peer 등락은 앱 관례(빨강=상승/파랑=하락), 감성 배지(긍정=초록/부정=빨강/중립=회색).

---

## 4. 데이터 수집 프로세스 — 멀티에이전트 (A 방식)

**supervisor(메인 Claude) + 컬럼별 서브에이전트** 패턴으로 수집한다.

1. 종목 1개당 **4개 `general-purpose` 서브에이전트를 병렬**로 띄운다 (Agent 툴, 한 메시지에 4개 호출).
2. 각 에이전트에 ① 종목 컨텍스트, ② 위 스키마의 해당 조각, ③ 사용할 MCP 도구를 지정한다.
   - 서브에이전트는 deferred MCP 도구를 `ToolSearch`로 로드한 뒤 호출한다.
   - "결과 JSON만 반환, 파일 쓰지 말 것" 지시.
3. supervisor가 4개 JSON을 모아 종목 객체의 `analysis`에 주입한다.
4. JSON 검증: `node -e "JSON.parse(require('fs').readFileSync('public/daily_market_report.json','utf8'))"`.
   (※ Windows PowerShell의 `ConvertFrom-Json`은 UTF-8을 깨뜨려 표시하므로 검증은 node로.)

> 서브에이전트 프롬프트 예시는 git 히스토리(이 기능 최초 커밋의 대화) 참조. 핵심은 **컬럼별 1에이전트 + 스키마 고정 + JSON-only 반환**.

---

## 5. 로드맵 — A → B

- **A (현재 단계, dev-time/배치)**: 분석을 미리 생성해 JSON으로 저장 → 정적 서빙. 현재 cron+Vercel 구조에 그대로 얹힘.
- **B (런타임)**: 종목 클릭 시 백엔드가 라이브로 4개 분석 생성. 프론트/스키마는 A와 동일 → 전환 비용은 주로 **백엔드 통합 + 캐싱 + API 키 관리**.

> ⚠️ `.mcp.json`의 MCP 서버는 **로컬/Claude Code 환경에서만** 동작. Vercel 배포본엔 없음.
> 그래서 A는 "미리 생성", B는 "DART/Naver/Tavily/Claude API를 백엔드에서 직접 통합" 이 필요.

---

## 6. 진행 현황 (2026-06-03)

- [x] 4개 컬럼 ↔ MCP 매핑 확정
- [x] `analysis` JSON 스키마 확정
- [x] **한미약품(128940)** 4개 분석 수집(멀티에이전트) → `daily_market_report.json` 주입
- [x] 드로어 UI: Deep Research 4-탭 구현
- [x] 드로어 UI: **Account / Deep Research 대분류 탭** 분리 (스킴 확정·승인됨)
- [x] 로컬 검증(서버 200, JSON 유효, JS 문법 0 에러)
- [x] 나머지 5종목 분석 수집·주입 (자화전자·HD현대중공업·올릭스·비에이치아이·미코) — **6종목 전체 완료**
- [x] ② 일일 배치 자동화 **구현** (`generate_analysis.py` + 워크플로) — §8 참조
- [ ] ② 첫 CI 실행(`workflow_dispatch`)으로 라이브 검증 (Naver/Tavily/DART/LLM)
- [ ] (선택) B 런타임 전환

### 다음에 할 일
1. GitHub Actions에서 **Run workflow**(workflow_dispatch)로 첫 실행 → 로그에서 종목별 analysis 생성 확인. DART OpenAPI가 가장 검증 필요(코드 §8).
2. (선택) 분석 주기 분리(뉴스·토론 매일 / DART·peer 주1회), 실패 시 전일 analysis 유지 등 최적화.
3. (선택) B 런타임 전환 설계.

---

## 7. 로컬 실행 / 확인

```powershell
cd backtest_stock\public
python -m http.server 8000
# http://localhost:8000 → 한미약품 클릭 → Account / Deep Research 탭 확인
```

---

## 8. 일일 배치 자동화 (② — 구현됨)

확정 방식: **Approach A — "결정적 수집(REST) → LLM 분석(Sonnet 4.6, 1콜/종목)"**.
MCP는 로컬 전용이라 CI에서는 각 서비스 **REST API를 직접 호출**한다.

### 파일
| 파일 | 역할 |
|---|---|
| `analysis/sources.py` | REST 래퍼: `get_peer_quotes`(yfinance) · `naver_search`(news/cafe) · `tavily_search`(해외뉴스) · `reddit_search`(공개 JSON, **peer 그룹 여론용**) · `dart_*`(corpCode/financials/disclosures/major_holders). 모든 함수는 실패 시 빈 결과 반환(배치 중단 방지). |
| `analysis/peers.json` | 종목코드 → 해외 peer 티커·note 맵(도메인 지식, 정적). 가격은 런타임에 yfinance로, **Reddit은 상위 peer 이름으로 검색**. |
| `generate_analysis.py` | 오케스트레이터. 종목별 RAW 수집 → Claude(`claude-sonnet-4-6`)가 `analysis` 스키마 JSON 생성(프롬프트 캐싱) → `daily_market_report.json` / `reports/YYYY-MM-DD.json` 병합. **peers.items 가격은 결정적**(LLM은 peers.summary·peers.reddit·나머지 작성). `fetch_peer_reddit`이 상위 2개 peer로 Reddit 검색(공개 JSON→Tavily 폴백). |

### 실행 순서 (워크플로 `daily_report.yml`)
1. `generate_report.py` — 가격/레벨(기존)
2. `generate_analysis.py` — 심층 리서치(신규, `continue-on-error: true`로 best-effort)
3. commit & push(기존)

### 필요한 GitHub Actions Secrets
`ANTHROPIC_API_KEY`, `DART_API_KEY`, `TAVILY_API_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`
(이름이 다르면 워크플로 `env:` 매핑만 수정. 로컬은 `.env.example` 참고.)

### 로컬 실행
```powershell
cd backtest_stock
# .env 또는 환경변수로 5개 키 설정 후
python generate_report.py
python generate_analysis.py   # daily_market_report.json 에 analysis 병합
```

### 검증 상태 / 주의
- ✅ 검증됨: Python 문법, 워크플로 YAML, peers.json, **graceful degradation**(키 없으면 빈 결과), **peer 시세 라이브**(단일·다중·nan-safe).
- ⏳ 미검증(키 필요): Naver/Tavily/DART 라이브 응답, LLM 분석 생성 → **첫 `workflow_dispatch` 실행으로 확인**.
- ⚠️ 가장 깨지기 쉬운 곳: **DART OpenAPI**. `corpCode.xml`(zip) 다운로드·파싱으로 stock_code→corp_code 매핑, `fnlttSinglAcntAll`(연결재무 11011/CFS), `list.json`(공시), `majorstock.json`(대량보유) 사용. 응답 status·필드명이 바뀌면 여기부터 점검.
- **Reddit**: 키 없는 공개 JSON(`search.json`) 사용 → cloud IP에서 403/레이트리밋 시 **Tavily(reddit.com)로 자동 폴백**. 검색어는 해당 종목이 아니라 **상위 peer 이름**(예: "Eli Lilly stock").
- 병렬: 6종목 동시 처리(`ANALYSIS_CONCURRENCY`, 기본 6), DART corpCode 맵은 사전 1회 로딩+락.
- 비용: 6종목 × 1콜/일(Sonnet 4.6), 시스템 프롬프트 캐싱 적용.
