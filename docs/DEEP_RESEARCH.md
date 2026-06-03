# Deep Research 기능 — 작업 프로세스 & 스펙

> 대시보드 종목 드로어에 **심층 리서치(Deep Research)** 분석을 붙이는 작업의 설계·진행 기록.
> 재로그인/세션이 바뀌어도 이 문서를 읽으면 맥락과 다음 단계를 이어갈 수 있다.
> 최종 갱신: 2026-06-03

---

## 1. 목표

종목 클릭 시 드로어에 기존 **Account** 정보(실시간 시세·보유현황·catalyst·scenario)에 더해,
다음 **4개 분석 컬럼**을 탭으로 제공한다.

| # | 컬럼 | 데이터 소스 (MCP) |
|---|------|------|
| 1 | 🌐 해외 Peer 종목 가격 | `yahoo-finance` (yfinance) |
| 2 | 📰 해외·국내 뉴스 분석 | `naver-news`(국내) + `tavily`(해외) |
| 3 | 💬 레딧 + 네이버 종목토론방 | `tavily`(Reddit) + `naver-news`(cafe/web) |
| 4 | 📑 DART 분석 | `korean-dart` |

MCP 서버 설정은 레포 루트 `.mcp.json` 참조.

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
    ]
  },
  "news": {
    "summary": "한국어 3~4문장",
    "items": [
      { "title": "...", "source": "...", "date": "YYYY-MM-DD",
        "sentiment": "긍정|부정|중립", "url": "...", "insight": "한 줄" }
    ]
  },
  "community": {
    "summary": "한국어 3~4문장",
    "sentimentLabel": "예: 전반적으로 중립~긍정",
    "reddit": [ { "title": "...", "url": "...", "sentiment": "긍정|부정|중립", "summary": "한 줄" } ],
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
- [ ] `generate_report.py`에 수집 파이프라인 통합 (② 일일 배치 자동화)
- [ ] (선택) B 런타임 전환

### 다음에 할 일
1. 수집을 `generate_report.py`에 통합 → GitHub Actions cron 자동화. 이때 MCP/API 키를 Actions에서 어떻게 쓸지 결정 필요(§5 제약).
2. (선택) B 런타임 전환 설계.

---

## 7. 로컬 실행 / 확인

```powershell
cd backtest_stock\public
python -m http.server 8000
# http://localhost:8000 → 한미약품 클릭 → Account / Deep Research 탭 확인
```
