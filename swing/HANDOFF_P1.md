# HANDOFF P1 — trading_agent 헤드리스 이식 (다른 PC 의 Claude Code 용)

> **2026-09-29 갱신: P1 구현(3절)·오프라인 테스트는 회사 PC 에서 완료**(`swing/agent/`, 테스트 43건).
> 개인 PC 에 남은 일은 **4절 실동작 검증**(네트워크 필요)뿐이다 — 6절 프롬프트 참조.
>
> 이 문서는 **개인 PC(가정용 회선, 네트워크 가능)** 에서 Claude Code 가 읽고 P1 을 진행하기 위한
> 인수인계서다. 회사 PC 에만 있는 로컬 문서(CLAUDE.md·PROGRESS.md)는 이 PC 에 없으므로,
> 필요한 규칙·맥락은 전부 여기에 적는다. 작성: 2026-09-29(회사 PC), 기준 커밋 `2ec53d93`.

## 0. 맥락 (3줄)
- `backtest_stock` 레포 `swing_paper` 브랜치의 `swing/` = 섹터 시그널 스윙 모의투자. main 주간 브리핑의
  동반강세·수급유입 종목을 매일 판단 → 다음 영업일 지정가 → 3영업일 보유(롱). 규칙 전체는 `swing/README.md`.
- 판단 엔진 자리는 `swing/agent_iface.py` 의 `decide(DecisionRequest) -> Decision` 형식으로 비워 두었고,
  지금은 `MockAgent` 만 있다. **P1 = homework 레포 `trading_agent` 스킬(13역할)을 이 형식으로 무인 실행 이식.**
- Railway 크론(`swing-cron`, 평일 16:30 KST)에서 사람 없이 돈다 → 서브에이전트·대화 없이 LLM 호출만으로 재현.

## 1. 규칙 (반드시)
- 작업 브랜치는 `swing_paper` 만. **main 에 병합·푸시 금지.** 푸시는 `git push origin swing_paper`.
- **이 브랜치는 swing 전용으로 정리됨(2026-09-29)** — 루트에는 `swing/` 와 의존 파일 6개(`llm.py`,
  `krx_calendar.py`, `data/krx_holidays.json`, `public/assets/krx_companies.json`, `requirements.txt`,
  `.python-version`)만 있다. 따라서 **`git merge main` 도 금지**(삭제 파일마다 충돌). main 의 수정이 필요하면
  남긴 파일만 개별로: `git checkout origin/main -- llm.py`.
- 코드 수정은 `swing/` 안에서만. 예외: 루트 `requirements.txt` 에 패키지 추가가 필요하면 이 브랜치에서만
  `# swing` 주석 블록으로 추가.
- 남긴 main 파일(`llm.py`, `krx_calendar.py`)은 **import 만** 하고 고치지 않는다. main 의 `analysis/` 는
  이 브랜치에 없다 — 수집은 homework 스킬 코드(kit)만 쓴다.
- homework 레포(`jykim0048/homework`, 브랜치 `trading_agent`)는 **원본**. 이식은 복사로 하고 출처 커밋을 기록.
- 커밋 메시지 끝: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. 커밋·푸시는 사용자 요청 시.
- 키(.env)는 커밋 금지, 채팅에 남기지 말 것.

## 2. 사전 준비 (사용자 + Claude)
1. **homework `trading_agent` 브랜치의 로컬 변경을 먼저 푸시**(dart_fin 보정·실측 메모 등). 원격 최신이 9/22
   `c5a90de` 이면 이 PC 작업분이 아직 안 올라간 것.
2. backtest_stock 준비 — 이미 클론이 있으면 worktree, 없으면 브랜치 클론:
   ```bash
   git -C <backtest_stock 클론> fetch origin
   git -C <backtest_stock 클론> worktree add ../backtest_stock-swing swing_paper
   # 또는
   git clone -b swing_paper https://github.com/jykim0048/backtest_stock.git backtest_stock-swing
   ```
3. `pip install -r requirements.txt` (브랜치 루트) + homework `trading_agent/requirements.txt`.
   Railway 는 루트 requirements.txt 만 설치하므로 kit 에 필요한 패키지는 루트에 합쳐 넣는다.
4. 키: `GEMINI_API_KEY`(또는 `LLM_CHAIN` 에 anthropic 추가 시 `ANTHROPIC_API_KEY`), `DART_API_KEY`,
   `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, 선택 `FLOW_API_BASE`·`TAVILY_API_KEY`. 환경변수로 설정.
5. 확인: `python -m unittest discover -s swing/tests -t .` → 32건 OK.

## 3. 구현 사양
### 3-1. 배치
```
swing/agent/
  __init__.py
  SOURCE.md          원본 homework 커밋 sha·복사 파일 목록·수정점
  roles/             homework trading_agent/references/roles/01~13 + kr_market_rules.md + output_schemas.md 복사
  kit/               homework trading_agent/scripts 의 collect·sources_kr·social_kr·dart_fin·indicators·
                     resolve·peers_resolve·_common + assets/(peers.json·industry_peers.json 포함) 복사
                     (import 경로만 수정, 로직 불변). 출처 = homework `951c796` 이후 최신
  peers.py           해외 peer 무인 확정(3-6 B안) — LLM 제안 + yfinance 검증 + 저장소 캐시
  pipeline.py        TradingAgent — decide(req) -> Decision
swing/tests/test_agent_pipeline.py
```
- 수집은 **스킬의 collect.py 를 그대로 재사용**(스킬과 결과 동일성 우선). `.env` 로딩 대신 환경변수.
  출력은 임시 폴더(`SWING_AGENT_RUNS`, 기본 `/tmp/swing_runs/<code>/<date>/`).
- 14_reflector·decision_log·outcome(반성 루프)은 **P1 범위 밖**. PM 의 과거 맥락 입력은 "과거 맥락 없음".
- 해외 peer 동적 확정(스킬의 `peers_resolve.py status/propose` + Claude 제안)은 **P1 범위 안** — 3-6 절.

### 3-2. 파이프라인 (SKILL.md 0~8단계의 무인 버전)
| 순서 | 역할 파일 | 입력 | reports 키 |
|---|---|---|---|
| 0a | — | **해외 peer 확정(3-6)**: 캐시 → 없으면 LLM 제안 1콜 + yfinance 검증 → 저장소 캐시 → kit 의 `memory/peers_dynamic.json` 으로 내려쓰기 | peers |
| 0b | — | `collect.py <code> --date <req.date>` → 01~05 JSON·manifest(0a 의 동적 peer 를 `select_peers` 가 사용). **01_price unavailable 이면 중단 → `Decision.error`** | collect |
| 1~5 | 01~05 애널리스트 | 각자 자기 JSON 만 | market, sentiment, news, fundamentals, flow |
| 6 | 06 Bull → 07 Bear | 리포트 5개 + 이력(라운드 1, `SWING_AGENT_ROUNDS`) | debate |
| 7 | 08 Research Manager | 토론 전문 | research_manager |
| 8 | 09 Trader | ResearchPlan + market 리포트 | trader |
| 9 | 10 공격 → 11 보수 → 12 중립 | 트레이더 제안 + 리포트 5개 + 이력 | risk_debate |
| 10 | 13 PM | Plan + Trader + 리스크 이력 + "과거 맥락 없음" | pm |

- LLM 은 main `llm.py` 의 `generate_json(system, user, max_tokens=, schema=)` 만 사용(Gemini 폴백 체인).
  역할 마크다운 출력 형식(output_schemas.md)은 그대로 두고 JSON 한 겹으로 감싼다:
  `schema = {"type":"object","properties":{"markdown":{"type":"string"}},"required":["markdown"]}`.
  system = `kr_market_rules.md` + 역할 파일, user = 입력 파일 내용(원문 JSON) + 아래 전략 맥락.
- 마지막에 `agent_iface.decision_from_markdown(req, trader_md, pm_md, reports, agent="trading_agent@<sha>")`.
  **파서가 읽는 라벨(`**Action**:`, `**Entry Price**:`, `**Stop Loss**:`, `**Position Sizing**:`,
  `**Rating**:`, `**Price Target**:`)을 바꾸지 말 것.** 수집 실패 레그는 reports 에 `collect` 키로 요약.
- 역할 호출 실패(LLMError 등)는 예외를 올리지 말고 `Decision.error` 로 — 하루 런 전체가 멈추면 안 된다
  (`daily._safe_decide` 가 한 번 더 감싸지만 부분 리포트를 남기는 게 목적).

### 3-3. 전략 맥락 주입 (Trader·PM·Research Manager user 프롬프트 앞에 붙임)
- **entry**: "스윙 모의투자 신규 진입 검토. 주문은 다음 영업일 1일 유효 지정가(Entry Price 도달 시 그 가격
  체결), 보유 최대 3영업일 후 종가 청산. Stop Loss 필수(없으면 주문 안 됨). Price Target 은 3영업일 안에 현실적인
  수준으로. 섹터 신호: {signal} {sector}, 스크리닝 근거: {reason}."
- **review**: "보유 중 종목 매도 검토. 진입 {entry}·손절 {stop}·목표 {target}·보유 {holdDay}/3일.
  트리거: {reason}. PM 이 Sell/Underweight 면 다음 영업일 시가 매도, 그 외 보유 유지."
  review 일 때 Trader Action 은 Sell/Hold 중심, 가격 줄은 생략 가능.
- 역할 파일 자체는 수정하지 않는다(원본 동일성). 맥락은 user 쪽에만 추가.

### 3-4. 연결·설정
- `swing/run_daily.py` `make_agent("trading_agent")` 가 `swing.agent.pipeline.TradingAgent(store=...)` 를 반환하게
  (peer 캐시용 저장소 — `make_store` 결과를 넘기도록 `main()` 순서 조정).
- env 노브: `SWING_AGENT_ROUNDS`(1), `SWING_AGENT_NO_SOCIAL`(1이면 `--no-social` — Reddit 429 회피 기본 권장),
  `SWING_AGENT_RUNS`(수집 임시 폴더), `SWING_PEER_TTL_DAYS`(동적 peer 유효기간, 180),
  `SWING_PEER_RETRY_DAYS`(제안 실패 후 재시도 대기, 30).
  종목당 LLM 약 13콜(라운드 1) + **처음 보는 종목만 peer 제안 1~2콜** — `SWING_MAX_NEW_PER_DAY` 로 총량 제어.
- 필요한 패키지가 루트 requirements.txt 에 없으면 이 브랜치에서만 추가.

### 3-5. 테스트 (오프라인)
`swing/tests/test_agent_pipeline.py` — `llm.generate_json` 과 수집 함수를 가짜로 바꿔:
- 호출 순서·횟수(라운드 1 = 13콜), 각 역할 user 에 **자기 입력만** 들어가는지
- 가짜 Trader/PM 마크다운 → Decision 값(entry/stop/target/rating/weight) 정확
- 01_price unavailable → error, 중간 역할 LLMError → error + 부분 reports
- review 맥락 문구 주입 여부
- peer(3-6): 큐레이션 종목은 LLM 미호출 / 캐시 적중 시 미호출 / 제안에 한국 상장·가짜 티커 섞이면 제거 /
  유효 2개 미만이면 1회 재제안 → 그래도 실패면 업종 기본표 + `failedAt` 기록 → 재시도 대기 중 미호출 /
  TTL 경과 시 재제안 / 미국 벨웨더 2개가 앞으로 정렬 / peer 단계 실패가 판단 전체를 막지 않음
  (yfinance 는 가짜 함수로 대체)
기존 32건 포함 전부 통과해야 함.

### 3-6. 해외 peer 무인 확정 (사용자 결정: B안, 2026-09-29)
스킬은 `peers_resolve.py status` 결과 `needs_proposal`(source 가 industry-default·none)이면 **Claude 가 대화 중
peer 를 제안**하고 `propose` 로 검증·저장한다. 무인 실행에서는 이 제안을 **LLM 1콜로 대체**한다(원본
backtest_stock `generate_analysis.resolve_peers` 가 Gemini 로 하던 방식과 같음).
1. **판정**: `sources_kr.select_peers(code, industryKey, peers.json, industry_peers.json, dynamic_cfg=캐시)`.
   `curated`·`dynamic`(TTL 이내)이면 그대로. `industry-default`·`none` 이면 2로.
   단 캐시에 `failedAt` 이 있고 `SWING_PEER_RETRY_DAYS` 이내면 제안 생략(업종 기본표로 진행).
2. **제안**: `llm.generate_json` 1콜. system = `peers_resolve.py` 독스트링의 제안 규칙 그대로(사업이 가장 유사한
   해외 상장 4~5개, 한국 상장 제외, Yahoo 심볼, **앞 2개는 미국 상장 벨웨더**, note 는 한국어 한 줄).
   user = 종목명·코드·yfinance 프로필(sector·industry·industryKey·영문 사업 요약이 있으면 포함).
   schema = `{"peers":[{"name","ticker","note"}]}`.
3. **검증**: `peers_resolve.validate_proposal` → `check_yfinance`(5일 시세) → `order_bellwethers_first`.
   **유효 2개 미만이면 거절 사유를 user 에 붙여 1회 재제안**. 그래도 미달이면 업종 기본표로 진행하고
   캐시에 `{"failedAt": 날짜, "rejected": [...]}` 기록.
4. **저장**: swing 저장소 키 `peers/dynamic` 한 문서(`{code: {name, resolved_at, source:"llm-proposed", model,
   peers, failedAt?}}`) — Railway 파일시스템은 휘발성이라 **저장소가 진실 원천**. 판단 직전에 이 문서를
   kit 의 `memory/peers_dynamic.json` 형식으로 내려써 `collect.py` 가 그대로 읽게 한다(collect 로직 불변).
5. **기록**: `reports["peers"]` 에 source(curated/dynamic/llm-proposed/industry-default/none)·peer 목록·
   거절 목록·LLM 호출 수. 대시보드 판단 원문에 그대로 보인다.
6. **실패 격리**: peer 단계의 어떤 예외도 판단을 막지 않는다 — 업종 기본표(또는 peer 없음)로 수집 진행.
7. **선택(사용자 승인 시)**: 개인 PC 스킬의 `memory/peers_dynamic.json`(커밋 안 된 기존 검증분)을
   `peers/dynamic` 초기값으로 넣는 1회성 스크립트 `swing/agent/seed_peers.py`. 넣은 항목은 source 를 유지.
- 백필(과거 날짜)에서도 peer 는 **현재 시점 정보로 확정**된다(뉴스·여론과 같은 룩어헤드 — 결과에 명시).

## 4. 실동작 검증 (이 PC 는 네트워크 가능)
1. 수집 단독: `python swing/agent/kit/scripts/collect.py 005930 --no-social` → manifest legs 표로 보고.
2. 판단 단독: 2~3종목(대형·코스닥·금융)으로 `TradingAgent().decide(...)` → Rating·가격 줄 파싱 확인.
3. 하루 런(파일 저장소):
   ```bash
   python -m swing.run_daily --date 2026-09-28 --wb-dir swing/data/wb_history \
       --agent trading_agent --prices yfinance --store file:swing/data/local --force
   ```
   → `swing/data/local/run/2026-09-28.json` 의 candidates 에 rating·entry·stop·ordered 확인,
   `python swing/server.py` 로 대시보드 판단 원문 대화상자에 역할별 리포트가 보이는지.
4. **P0 겸 확인**: yfinance 일봉(`swing/prices.py YFinancePrices`)이 당일 16:30 기준 OHLC 를 주는지,
   KIS 허브/네이버 값과 2~3종목 대조. 틀리면 소스 교체 제안(구현은 사용자 확인 후).
5. 호출량·소요시간 기록: 종목당 LLM 콜 수·초, 10종목 하루 런 총 시간(Railway 크론 타임아웃 판단용).
6. **peer B안 실측**: `peers.json` 에 없는 중소형 2종목(예: 9/28 후보 중 CJ CGV 079160, 실리콘투 257720)으로
   제안 → 검증 결과(유효·거절 티커·사유)와 캐시 적중(두 번째 실행 LLM 0콜)을 보고.

## 5. 완료 기준 · 보고
- [x] homework trading_agent 최신 푸시 확인, `swing/agent/SOURCE.md` 에 출처 sha(`951c796`)
- [x] swing/agent 구현(peer B안 포함) + 오프라인 테스트 전부 통과(43건, 회사 PC)
- [ ] 실동작 1~6 결과(수집 legs 표, 판단 샘플, 하루 런, 일봉 대조, 콜 수·시간, peer 제안·캐시)
- [ ] 이 문서 하단 "진행 기록" 에 결과·함정 추가 후 `swing_paper` 에 커밋·푸시(사용자 승인 후)
- 사용자에게: 샘플 종목 PM 등급·진입/손절/목표, 실패 레그, 하루 런 소요시간, 남은 리스크를 한국어 존댓말로 보고.

## 6. 붙여넣을 프롬프트 (개인 PC Claude Code)
```
backtest_stock 레포의 swing_paper 브랜치를 최신으로 받아줘(없으면 클론/worktree).
swing/HANDOFF_P1.md, swing/README.md, swing/agent/SOURCE.md 를 먼저 읽어.
P1 구현은 회사 PC 에서 끝났고, 이 PC 에서는 HANDOFF_P1.md 4절 "실동작 검증" 1~6 만 하면 돼.

- 시작 전: python -m unittest discover -s swing/tests -t . 가 전부 통과하는지 확인.
- 키는 내가 환경변수로 넣을 테니 비어 있는 키만 알려줘(GEMINI_API_KEY, DART_API_KEY, NAVER_CLIENT_ID/SECRET).
- 검증 중 버그를 찾으면 swing/ 안에서만 고치고 테스트를 추가해. kit/ 는 원본 무수정 원칙 — kit 문제면
  homework trading_agent 원본을 고치고 SOURCE.md 재동기화 절차를 따를 것.
- main 병합·푸시 금지. 끝나면 HANDOFF_P1.md "진행 기록"에 결과(수집 legs 표, 판단 샘플, 하루 런, 일봉 대조,
  종목당 LLM 콜 수·소요시간, peer 제안·캐시)를 적고, 커밋·푸시는 내 승인 후 swing_paper 로만.
  커밋 메시지 끝에 Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>.
- 답변은 한국어 존댓말로.
```

## 진행 기록
- 2026-09-29 (회사 PC) 오프라인 골격 `2ec53d93` 푸시. P1 대기 — homework trading_agent 최신 푸시 필요.
- 2026-09-29 (개인 PC) homework `trading_agent` 최신 푸시 완료 → **`951c796`**(9/22 로컬 실측 반영: 해외 peer 뉴스·
  동적 peer `peers_resolve.py`·공매도/대차 시총비중·DART YoY 수정, 오프라인 테스트 16건). 2절 1번 통과 — P1 시작 가능,
  `SOURCE.md` 출처 sha 는 `951c796`. `memory/`(decision_log·peers_dynamic)는 커밋하지 않음(P1 범위 밖).
  이 PC 는 스킬 원본을 레포 밖 `Workspace\trading_agent` 에서 관리(스킬 정션이 그 경로) — 스킬 수정 시 homework 로 복사 후 커밋.
- 2026-09-29 (회사 PC) **해외 peer 무인 확정 = B안(LLM 제안 + yfinance 검증 + 저장소 캐시)** 사용자 결정 → 3-6 절 추가,
  3-1·3-2·3-4·3-5·4·5 절 반영. 업종 기본표(`industry_peers.json`)는 951c796 에서 새로 작성된 폴백(외부 출처 아님)이라
  제안 실패 시에만 사용.
- 2026-09-29 (회사 PC) 브랜치 정리: swing 에 불필요한 main 파일 990개 삭제(대시보드·파이프라인·워크플로·리포트).
  남긴 의존 파일은 1절 참조. `git merge main` 금지 → 필요한 파일만 `git checkout origin/main -- <파일>`.
- 2026-09-29 (회사 PC) **P1 구현 완료** — `swing/agent/`: kit(homework `951c796` 바이트 동일 복사), `peers.py`(B안),
  `pipeline.py`(13역할, JSON 래핑, 전략 맥락, 실패 격리), `run_daily --agent trading_agent` 연결, 대시보드 판단 원문에
  peers·collect 추가. 오프라인 테스트 43건 통과(신규 11: 호출 순서·입력 격리·결정 파싱·라운드2·가격 실패·역할 실패·
  수집 예외·review 맥락·peer 실패 격리·peer 큐레이션/제안·검증·캐시/재시도 대기/TTL). 실동작(4절)은 미실행 —
  이 PC 네트워크 금지. 기본값: SWING_AGENT_NO_SOCIAL=0(소셜 수집 켬 — peer B안과 짝), ohlcv 최근 60행.
