# Deep Research 기능 — 작업 프로세스 & 스펙

> 대시보드 종목 드로어에 **심층 리서치(Deep Research)** 분석을 붙이는 작업의 설계·진행 기록.
> 재로그인/세션이 바뀌어도 이 문서를 읽으면 맥락과 다음 단계를 이어갈 수 있다.
> 최종 갱신: 2026-09-18 (peer 여론: Reddit RSS 우선 + StockTwits 추가 — §1·§3·§8·§9)

---

## 1. 목표

종목 클릭 시 드로어에 기존 **Account** 정보(실시간 시세·보유현황·catalyst·scenario)에 더해,
다음 **4개 분석 컬럼**을 탭으로 제공한다.

| # | 컬럼 | 데이터 소스 |
|---|------|------|
| 1 | 🌐 해외 Peer 종목 가격 **+ Peer Reddit·StockTwits 여론** | yfinance + **StockTwits**(미국 상장 peer 스트림, 주) + **Reddit**(RSS 예산제 → Tavily/Brave, 보조) |
| 2 | 📰 해외·국내 뉴스 분석 | Naver(국내) + Tavily(해외) |
| 3 | 💬 네이버 종목토론방 (국내 개인투자자) | Naver(cafe/web) |
| 4 | 📑 DART 분석 | DART OpenAPI |

> **Reddit 위치 결정(2026-06-04):** Reddit은 한국 종목토론방(컬럼 3)이 아니라 **해외 Peer 그룹 분석(컬럼 1)** 에 쓴다. 한국 중소형주는 Reddit에 거의 안 잡히지만, 해외 peer(Eli Lilly·GE Vernova·NGK 등)는 활발히 논의되기 때문. 따라서 community(컬럼 3)는 **네이버만**, peers(컬럼 1)에 `reddit` 배열 추가.

> **StockTwits 추가·Reddit 경로 재편(2026-09-18):** Tavily 월 한도 소진(432) + search.json 403 으로 `peers.reddit` 이 9월 들어 사흘만 채워진 것을 계기로, TradingAgents 의 수집 로직(RSS-first Reddit · StockTwits 공개 스트림)을 `analysis/social.py` 로 이식했다. X(트위터)는 API 가 유료라 쓰지 않고, '트윗' 역할의 개인투자자 단문 소스는 **StockTwits**(키 불필요, 사용자 라벨 Bullish/Bearish)다. 상세는 §9.

MCP 서버 설정(로컬 인터랙티브용)은 레포 루트 `.mcp.json` 참조. 단 **배치는 MCP 미사용**(§8) — Reddit·StockTwits 도 공개 REST/RSS 로 수집.

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
    "reddit": [ { "title": "...", "url": "...", "subreddit": "r/...", "sentiment": "긍정|부정|중립", "summary": "한 줄(해외 peer/섹터 여론)" } ],
    // 2026-09-18~ (없으면 구버전 분석 — UI 는 섹션 자체를 생략)
    "stocktwits": [ {
      "ticker": "LLY", "name": "Eli Lilly",
      "status": "ok|empty|unavailable|skipped",        // 코드
      "bullish": 6, "bearish": 1, "unlabeled": 23, "total": 30, "labeled": 7,
      "bullPct": 86,                                    // 라벨 기준(bullish/labeled), labeled=0 이면 null — 코드
      "windowDays": 7, "newest": "YYYY-MM-DD HH:MM", "oldest": "YYYY-MM-DD HH:MM",   // KST — 코드
      "samples": [ { "createdAt": "...", "user": "...", "sentiment": "Bullish|Bearish|null", "body": "≤280자" } ],  // 상위 3건 — 코드
      "summary": "한국어 한 줄"                         // ← 이것만 LLM
    } ],
    "socialStatus": {                                   // 코드 — 수집 실패(unavailable)와 글 없음(empty) 구분
      "reddit": "ok|empty|unavailable", "redditQueried": ["LLY@stocks", "web:Eli Lilly"],
      "stocktwits": { "LLY": "ok" }
    }
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
    "summary": "한국어 3~4문장 (LLM 작성)",
    "highlights":    [ { "label": "매출액(2025)", "value": "1조5,475억원", "note": "한 줄 (LLM 작성)" } ],
    "recentFilings": [ { "date": "YYYY-MM-DD", "title": "공시 제목", "filer": "제출인", "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=..." } ]
  }
}
```

- `analysis`가 없는 종목은 UI에서 "데이터 없음" 으로 표시(정상 동작).
- 색상 규칙: peer 등락은 앱 관례(빨강=상승/파랑=하락), 감성 배지(긍정=초록/부정=빨강/중립=회색).
- **`dart.recentFilings`는 LLM이 만들지 않고 코드가 결정적으로 채운다** — DART 공시 **최신순 5건**(무필터), 제목은 DART 원문(`dsaf001`) 링크. `summary`·`highlights`만 LLM 작성.
- **`peers.stocktwits`의 수치·상태·샘플도 코드가 채운다**(`generate_analysis._merge_stocktwits`) — LLM 출력 스키마는 `{ticker, summary}` 뿐이고 티커 매칭으로 summary 만 병합한다. LLM 이 티커를 지어내거나 수치를 써도 무시된다(peers.items 가격과 같은 원칙).
- **수집 실패 ≠ 여론 부재**: `status`/`socialStatus` 의 `unavailable` 을 UI 는 "수집 실패 — 언급 없음이 아닙니다"로, `empty` 는 "언급 없음/최근 메시지 없음"으로 구분 표기한다. 프롬프트도 unavailable 을 침묵으로 서술하지 못하게 막는다.

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
| `analysis/social.py` | peer 여론 RAW 수집기(2026-09-18, TradingAgents 이식). `stocktwits_stream`/`stocktwits_for_peers`: 공개 심볼 스트림 → 라벨(Bullish/Bearish/무라벨) 결정적 집계, 미국 상장 심볼만, 7일 창, 티커별 30분 프로세스 캐시, status ok/empty/unavailable/skipped. `reddit_rss`/`reddit_for_peers`: `/r/{sub}/search.rss` — **10분 창당 예산 2콜**(`REDDIT_BUDGET`/`REDDIT_WINDOW_S`) + 첫 429 회로차단 + 호출 간격 1.5s, 실패는 `None`(글 없음 `[]` 과 구분). 식별용 User-Agent 필수(익명 UA 는 Reddit 이 차단). |
| `analysis/peers.json` | 종목코드 → 해외 peer 티커·note 맵(도메인 지식, 정적). 가격은 런타임에 yfinance로, **StockTwits·Reddit RSS 는 peer 티커로**, 웹검색 폴백은 peer 이름으로 검색. |
| `generate_analysis.py` | 오케스트레이터. 종목별 RAW 수집 → LLM(`llm.py` 폴백 체인, 기본 Gemini)이 `analysis` 스키마 JSON 생성(구조화 출력으로 유효 JSON 보장) → `daily_market_report.json` / `reports/YYYY-MM-DD.json` 병합. **peers.items 가격은 결정적**(LLM은 peers.summary·peers.reddit·peers.stocktwits[].summary·나머지 작성). `fetch_peer_reddit`이 상위 2개 peer로 ① Reddit RSS(예산 내) → ② Tavily→Brave 웹검색(reddit.com) → ③ 공개 JSON(거주지 IP 전용) 순으로 수집해 `{posts, status, queried}` 반환. StockTwits 는 미국 상장 peer 상위 2개를 다른 수집 leg 와 병렬로 가져와 `raw.peers_stocktwits`·`raw.peers_social_status` 로 LLM 에 전달, 최종 수치는 `_merge_stocktwits` 가 코드 값으로 확정. |
| `llm.py` | 제공자 무관 LLM JSON 헬퍼. `LLM_CHAIN`(우선순위 `provider:model` 목록)을 순서대로 시도하고 쿼터/billing 소진·rate limit·키 없음 시 다음 모델로 자동 폴백. 기본 체인: `gemini-3.5-flash → gemini-3.1-flash-lite → gemini-3-flash-preview → gemini-2.5-flash → gemini-2.5-flash-lite`. Anthropic 어댑터도 포함(체인에 추가하면 동작). |

### 실행 순서 (워크플로 `daily_report.yml`)
1. `generate_report.py` — 가격/레벨(기존)
2. `generate_analysis.py` — 심층 리서치(신규, `continue-on-error: true`로 best-effort)
3. commit & push(기존)

### 필요한 GitHub Actions Secrets
`GEMINI_API_KEY`, `DART_API_KEY`, `TAVILY_API_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`
(LLM 체인 변경은 repo Variable `LLM_CHAIN`로. Anthropic 폴백을 쓰려면 `ANTHROPIC_API_KEY`도 추가.
이름이 다르면 워크플로 `env:` 매핑만 수정. 로컬은 `.env.example` 참고.)

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
- **공시(recentFilings)**: LLM 큐레이션 없이 `list.json` **최신순 5건**을 코드가 그대로 노출(`generate_analysis.analyze_stock`에서 결정적 주입). 각 항목에 `dsaf001` 원문 링크. summary/highlights만 LLM.
- **Reddit**(2026-09-18 재편 — 상세 §9): 공개 JSON(`search.json`)은 **데이터센터 IP(GitHub Actions 포함)에서 403**. 종전엔 Tavily(`include_domains=["reddit.com"]`)가 CI 의 유일한 실경로였는데 **Tavily 무료 크레딧이 월초 며칠 만에 소진(432)** 되면 `peers.reddit` 이 통째로 비었다(8월 전일 0건, 9월은 2~4일만 채워짐). 지금은 **RSS 검색이 1순위**(예산 2콜·429 회로차단), Tavily/Brave 는 살아 있을 때만 보강, 공개 JSON 은 로컬(거주지 IP) 전용 폴백. 검색 대상은 해당 종목이 아니라 **상위 peer**(RSS 는 티커, 웹검색은 이름).
- **StockTwits**: Actions IP 에서 식별 UA 로 전부 200(100~260ms), 비인증 한도 시간당 200콜 → **peer 여론의 주 소스**. 저유동 종목은 최근 30건이 수개월 전까지 거슬러 가므로 7일 창으로 자른다. 라벨 5건 미만은 UI·프롬프트 모두 '표본 부족'으로 처리.
- 병렬: 6종목 동시 처리(`ANALYSIS_CONCURRENCY`, 기본 6), DART corpCode 맵은 사전 1회 로딩+락.
- 비용: 6종목 × 1콜/일(Sonnet 4.6), 시스템 프롬프트 캐싱 적용.

---

## 9. Peer 여론 — Reddit RSS + StockTwits (2026-09-18)

### 왜 바꿨나
- `peers.reddit` 수집이 Tavily 에만 실질 의존 → 무료 크레딧 월초 소진(432) 후 전부 `[]`. 아카이브 기준 8월 전일 0건, 9월은 2~4일만 채워짐. Brave 폴백은 키 미설정, 공개 `search.json` 은 Actions IP 에서 403.
- 출처: TradingAgents(`tradingagents/dataflows/reddit.py`·`stocktwits.py`, `agents/analysts/sentiment_analyst.py`)의 RSS-first Reddit · StockTwits 스트림 · "데이터를 프롬프트에 미리 넣고 도구 호출 없이 판단" 구조를 이 레포 규약(구조화 dict, graceful, 결정적 수치)으로 이식. TradingAgents 에도 X(트위터) 수집은 없다 — 유료 API.

### Actions IP 실측 (`.github/social_probe.py` → `.github/social_probe_result.json`)
| 소스 | 결과 |
|---|---|
| StockTwits `/api/2/streams/symbol/<T>.json` (식별 UA) | LLY·NVDA·INCY 전부 200, 29~30건, 94~263ms. 브라우저 UA 도 200(로컬에서 본 Cloudflare 403 은 간헐) |
| Reddit `/r/<sub>/search.rss` 순차 6콜(1.5s 간격) | 1·2번째 200, **3번째부터 전부 429**(Retry-After 없음) |
| Reddit `/search.rss`(사이트 전체) | 429 |
| Reddit `/r/stocks/search.json` | 403 |

→ **StockTwits 가 주 소스**, Reddit 은 **창당 2콜 예산제**로 '있으면 보강'. 재측정은 Actions 의 *Social Probe* 워크플로를 수동 실행.

### 동작 요약
1. `analyze_stock` 이 peer 확정 후 `fetch_peer_reddit`·`social.stocktwits_for_peers(top=2, fresh_days=7)` 를 다른 수집 leg 와 병렬 제출.
2. Reddit: RSS(미국 peer 티커 @ r/stocks, 예산 내) → Tavily→Brave(reddit.com, peer 이름) → search.json(로컬 전용). `status` = 하나라도 글이 있으면 `ok`, 성공한 조회가 있었는데 0건이면 `empty`, 전부 실패면 `unavailable`.
3. StockTwits: 미국 상장 심볼(접미사 없는 티커)만. 사용자 라벨을 코드가 집계하고 `bullPct` 는 **라벨 달린 글 기준**(무라벨은 어느 쪽에도 넣지 않음).
4. LLM 입력 `peers_stocktwits`(집계 + 메시지 상위 6건)·`peers_social_status`. 해석 규칙(프롬프트): 70/30 완만한 강세, 90/10+ 과열·역발상 경계, 50/50 불확실, `labeled<5` 는 표본 부족 명시, 메시지는 의견이지 사건이 아님, unavailable 을 침묵으로 쓰지 말 것.
5. 후처리 `_merge_stocktwits`: 수치·상태·샘플 3건은 코드 값, `summary` 만 LLM(티커 매칭). `peers.socialStatus` 기록.
6. 대시보드(`analysisHTML` peers 분기 — 드로어·온디맨드 공용): 비율 막대·건수·방향 배지(표본 부족 / 강세 과열 경계 90%+ / 강세 우위 60%+ / 약세 우위 40%- / 혼조)·요약·샘플. 구버전 분석은 섹션 생략.

### 운영 메모
- 환경변수: `REDDIT_BUDGET`(기본 2) · `REDDIT_WINDOW_S`(600) · `REDDIT_GAP_S`(1.5) · `STOCKTWITS_CACHE_TTL`(1800). 예산 창은 자동 리셋이라 Railway 상주 프로세스(온디맨드)에서도 10분마다 되살아난다.
- 장중 회차는 종목 수가 많아 Reddit 예산이 첫 1~2종목에서 소진된다 — 의도된 동작. 나머지 종목은 `reddit: unavailable`(또는 Tavily 가 살아 있으면 웹검색 결과)로 기록되고 StockTwits 는 티커 캐시로 전 종목 채워진다.
- 새 API 키·시크릿 없음. LLM 입력은 종목당 약 1~2천 토큰 증가.
- 테스트: `python tests/test_social_sources.py`(수집기) · `python tests/test_peer_social_merge.py`(연결·병합·스키마) — 둘 다 오프라인.
