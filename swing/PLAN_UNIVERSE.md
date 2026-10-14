# PLAN_UNIVERSE — 섹터별 상위 10 종목 수급 모멘텀 유니버스 확장 (2026-10-13 초안 → 같은 날 swing 관찰 모드 구현)

> 2026-10-14(저녁): **선정 기준 = 종합점수(T2)** — 시뮬(8/31~10/10, `--methods total`) A 모멘텀 3.75%·Sharpe 3.54·MDD −2.05 / T1 4.66·3.59·−2.29 / **T2 4.97·4.37·−1.59**(Short 거래당 +1.10→+1.69%, 손절 52→33). `SWING_UNI_RANK_BASIS=total`(기본, `score` 로 종전): 1차 컷 전 종목 허브 보강·채점 → 업종 Top N·후보(종합 ≥ `SWING_UNI_TOTAL_MIN`=50)·통합 순위·라운드로빈 순서 모두 종합순. 유동성 백분위 풀 = Long·Short 합산(`SWING_UNI_LIQ_POOL=all`). 매트릭스 후보 정렬은 모멘텀 그대로.
> 2026-10-14: A/B/C 시뮬(`tools/sim_universe.py`, 8/31~10/10) 결과 B(통합 순위) 가 업종 집중·변동성·MDD 최악, A≈C → **운용 선정 방식 = A 업종 라운드로빈**(`config.UNI_PICK_MODE`, `universe.pick`). 상세는 PROGRESS 🧪 절.
> 2026-10-13 진행: §3 U1~U7 + 대시보드 탭 구현(A1~A7). **후보 연결 방식 변경**: 매트릭스와 병합하지 않고 **독립 소스**로 나란히(§4 A3 의 CandidateSource 대신 daily.run_day 5) 단계), 두 소스 모두 **같은 모멘텀 점수**로 정렬(매트릭스는 칸 전부 분석·점수순, 유니버스는 점수 ≥ +2·상한 5/5·쿨다운 3일). 섹터 노출 상한(A4)·B(허브 /short-rank)·C(main) 는 보류. 실측 §5 는 첫 Railway 산출로 확인.

목표: 롱숏 후보가 주간 브리핑 `sectorScreen.matrix`(칸당 ≤5, 하루 Long 0~10 · Short 0~10)에만
의존하는 구조를 벗어나, **업종별 상위 10 종목을 주간 거래대금·공매도·투자자 수급 모멘텀으로 매일
선정**하는 두 번째 후보 소스를 붙인다. 기존 matrix 소스는 그대로 두고 병합한다.

## 0. 현재 구조 요약 (2026-10-13 레포 분석 결과)

| 레포 | 역할 | 유니버스 확장 관점의 현황 |
|---|---|---|
| `backtest_stock` main | 16:10 주간 런 `generate_weekly_briefing.py` → `weekly_briefing.json` | `build_sector_screen()`(556~737행)이 KOSPI 업종 24개 신호 × 종목 근거(외인·기관·대차·공매도 등재) 동방향 2축으로 칸당 5개 선정. 후보 풀 = netbuy_rank 상위30 등재 + 공매도/대차 랭킹 + 촉매 + 신호 섹터 시총 상위 10(`krx_sector_map`). **거래대금 수집 없음**, 공매도는 대금(`pbmn`)만 사용 |
| `backtest_stock` swing_paper | 16:30 크론 `swing/daily.py` → trading_agent 13콜/종목 → 원장 | 후보 = `signals._candidates`(matrix 칸 순서 그대로, 랭킹 없음). 섹터→종목 매핑 없음, 일봉 소스 DbHubPrices는 OHLC만(`CntgVol` 버림). 재분석 쿨다운 없음 |
| `trading_bot_HUB_KIS` | KIS REST 수급 라이브러리 + strategy_runner HTTP(`/flow`, `/flow-rank`, `/sector-flow`, `/breadth`) | 종목별 공매도(`qty`·`volRlim`·`pbmn`·`pbmnRlim`)·대차·투자자 일별 1콜씩. 업종→구성종목 매핑 없음, 전역 레이트리미터 없음(0.12s sleep), KOSPI 업종만 |
| `trading_bot_HUB_DB` | DB증권 WS 1연결 + 베이시스 페어 러너 | 수급·업종·현물 마스터 조회 **전무**. 재사용 가능 = 토큰(Redis `db:token`), `core/rest.py`(URL별 TPS·연속조회), 주식선물 매도 주문·체결 대기 |

## 1. 데이터 소스 결정 (API 조사 결과)

### 1-1. 업종 구성종목 + 시총 + 거래대금 → **DB증권 `USTOCKCONDLIST`** (핵심)
- `POST /api/v1/quote/kr-stock/inquiry/industry-components`, In `{InputBstpIscd: 업종코드, InputRankSortClsCode1: 42(거래대금)|2(시총)|13(거래량)|12(등락률), InputCondMrktDivCode: "UJ"}`
- Out 행: `Iscd, KorIsnm, Avls(시총, 100만원), AvlsRlim, Prpr, PrdyCtrt, AcmlVol, AcmlTrPbmn`. 모의 ⭕, 2 TPS.
- 업종 코드 목록은 `USTOCKCOND` `/inquiry/industry-cls` (In `InputCondMrktDivCode: K|Q|K2|KR`, `InputMrktClsCode: "U"`) → 코스피 32행.
- **코드 체계 함정**: DB증권 1001=코스피 종합, 1005=음식료·담배 … / KIS 0001=코스피, 1001=코스닥. 섹터 조인은 코드 아니라 **이름(`norm_sec`)** 으로.
- 콜 수: 업종 32(+코스닥) × 1~2콜 = 60여 콜, 2 TPS → 약 30초. 1페이지 행수 상한 미확인(예시 29행=전 구성종목으로 추정) → 첫 실측 항목.

### 1-2. 종목별 일별 거래대금 + 투자자 매수/매도 → **DB증권 `DAYSTOCKTJJ`**
- `/inquiry/daily-investor`, In `{InputCondMrktDivCode:"J", InputIscd1, InputDate1, InputDate2}`, 기간 지정 1콜.
- Out 행: `Date, Prpr, PrdyCtrt, AcmlVol, AcmlTrPbmn, Orgn/FrgnReg/Prsn × Shnu/Seln × Vol/TrPbmn`(19필드). 순매수 = Shnu − Seln 직접 계산. 기관 세부·기타법인 없음. 2 TPS.
- 주간 거래대금 = 5영업일 `AcmlTrPbmn` 합, 비교 기준 = 직전 4주 주평균. **금액 단위 미확인**(예시 스케일이 거래대금과 달라 천원 추정) → 실측.
- 대안: CHARTDAY에는 거래대금 없음(KIS 일봉 `FHKST03010100`에는 `acml_tr_pbmn` 있음, 주봉 W 1콜).

### 1-3. 공매도 수량·비중, 대차 → **KIS만 가능** (DB증권 카탈로그 170건 전수 확인: 공매도·대차·신용(종목)·프로그램 TR 없음)
- 종목별: `FHPST04830000` `/quotations/daily-short-sale` → `ssts_cntg_qty`(수량), `ssts_vol_rlim`(거래량 비중), `ssts_tr_pbmn`, 누적치. 허브 `_fetch_short_sale_daily`(hub.py:999)가 이미 `qty/volRlim/pbmn/pbmnRlim`으로 내려줌(`/flow` shorts). T+1 공표.
- 시장 랭킹(저비용): `FHPST04820000` `/ranking/short-sale`, `FID_INPUT_ISCD` 0001/1001/2001, `FID_PERIOD_DIV_CODE=D`, `FID_INPUT_CNT_1=4`(=1주 누적) → 3콜로 1주 공매도 수량·대금·비중 상위. 허브 미구현(신규).
- 대차: `HHPST074500C0`(허브 `_fetch_loan_daily`), 1콜 100행.
- 투자자 일별(기관 세부 포함): `FHPTJ04160001` — **15:40 이전 호출 차단**, 1콜 30행. 16:30 크론에선 문제 없음.

### 1-4. KIS 업종별 거래대금 순위 `FHPST01710000`(volume-rank, `FID_INPUT_ISCD`=업종코드, `FID_BLNG_CLS_CODE=3`)
- 공식 MCP 설정에 "기타: 업종코드"로 문서화됐으나 실호출 샘플 없음, 1콜 30행(커뮤니티). **DB증권 USTOCKCONDLIST가 전 구성종목+시총+거래대금을 한 번에 주므로 1차 소스로 쓰지 않음.** KIS 쪽은 공매도·대차 보강 전용.

### 1-5. 숏 다리 현실
- KIS·DB증권 모두 **개인 공매도/대주 신규 주문 API 없음**(DB `CSPAT00600 MgntrnCode`는 상환 코드만). swing의 Short는 현재 모의 시뮬레이션이라 당장 제약 없음.
- 실주문(모의투자 계좌)으로 갈 경우 숏 = **주식선물 매도**(`CFOAT00100 BnsTpCode=1`, DB 허브 `Broker._order_fut` 재사용) → 숏 유니버스는 주식선물 상장 종목(`FCODES JF`의 `UnasShrnIscd`)으로 한정. 후보에 `hasStockFuture` 플래그를 지금부터 붙여 둔다.

## 2. 아키텍처 — 어디서 계산하나

**결정(권장): swing-cron 안의 `swing/universe/` 모듈이 DB증권 REST(공유 토큰) + KIS 허브 HTTP로 직접 산출**, 결과를 store `universe/<date>`에 저장하고 후보 소스로 소비.

근거:
- swing-cron은 DB 허브와 같은 Railway 프로젝트라 `db:token` 읽기 패턴(`prices.DbHubPrices`)이 이미 검증됨. 토큰 발급은 허브만(1분 1건) — swing은 읽기만.
- main 주간 런(Actions)은 이미 ~3~4분/타임아웃 10분이고 Actions 해외 IP에서 DB증권 REST 가능 여부 미확인. main에 넣으면 wb_history 백필과 섹터명 일치는 쉬우나 실행 위험이 큼.
- KIS 허브에 넣으면(`/sector-leaders`) 섹터×종목 매핑을 KIS 마스터파일 파서부터 새로 만들어야 함(core/master.py는 선물 전용 파이프 구분자 파서). 보강 TR 1개(`/short-rank`)만 추가하는 선에서 그친다.

대안으로 남겨 두는 것: 유니버스 스냅샷을 main `weekly_briefing.json`의 `sectorTop` 키로도 내보내면 대시보드(main)와 백필이 편해진다 — 2단계 이후 선택.

## 3. 산출 파이프라인 (16:30 크론, 정산 뒤·후보 산출 전)

```
U1 업종 목록      USTOCKCOND K(+Q)                 → 32(+N) 업종, 이름 norm_sec 로 sectorFlow 24행과 조인
U2 구성종목 스냅샷 USTOCKCONDLIST sort=42 업종마다   → {code, name, sector, avls, acmlVol, acmlTrPbmn, prdyCtrt}
                   store universe/snap/<date> (매일 1장, 주간 합산 재료)
U3 1차 컷         업종별 당일 거래대금 상위 K(기본 15) ∪ 시총 상위 5 — 유동성 하한(20일 평균 거래대금 ≥ 20억, 스냅샷 누적 전엔 당일 ≥ 20억)
                   → 약 32×15 ≈ 480종목
U4 종목 일별      DAYSTOCKTJJ(기간 = 최근 25영업일) 종목당 1콜 @2TPS ≈ 4분
                   → 주간 거래대금(tv1w), 직전4주 평균(tv4w), 외인·기관 순매수 주간 합(억), 1W/1M 등락
U5 공매도·대차    KIS 허브 신규 /short-rank(FHPST04820000 1주 누적, 3콜) 로 1주 공매도 상위 집합 확보 →
                   U3 교집합 + 각 업종 상위 10 후보에만 /flow?code=&rows=10 (공매도 qty·volRlim 추이, 대차 rmndChg)
U6 스코어·방향    섹터 신호(sectorFlow.signal) 와 결합해 Long/Short 점수 → 업종별 Top10 → store universe/<date>
U7 후보 병합      CandidateSource(matrix) + CandidateSource(sectorTop) → 중복 제거·쿨다운·섹터 노출 상한 → 일 상한
```

### 3-1. 모멘텀 스코어(초안, 노브는 env)
- `tvMom` = tv1w / tv4w − 1 (주간 거래대금 가속). 상위 분위 가점(+2/+1), 급감 −1.
- `flowMom` = (외인+기관 주간 순매수) / 시총 (bp). 부호·크기로 ±2/±1.
- `shortMom` = 1주 평균 `volRlim` − 직전 4주 평균 (공매도 비중 변화, pp). Long엔 감소가 +, Short엔 증가가 +. 공매도 상위(`/short-rank`) 등재는 Short +1.
- `loanMom` = 대차잔고 주간 증감 부호(Long: 감소 +, Short: 증가 +).
- `pxMom` = 1W 등락 부호가 방향과 일치 +1.
- **방향 규칙**: 섹터 신호 동반강세·수급유입 → Long 후보만, 동반약세·수급이탈 → Short 후보만, 혼조 → flowMom·shortMom 부호 일치 시 그 방향(없으면 제외). 같은 종목이 양방향에 들면 Long 우선(기존 daily.py:85 규칙 유지).
- 업종별 Top10 = 방향별 점수 내림차순, 동점은 tv1w 큰 순.

### 3-2. 저장 스키마 `universe/<date>`
```json
{"date":"2026-10-13","asof":"16:33","basis":{"tv":"DB DAYSTOCKTJJ AcmlTrPbmn 5일합","short":"KIS FHPST04830000 volRlim"},
 "sectors":[{"name":"운송·창고","dbCode":"1021","signal":"동반강세","n":27,
   "top":[{"code":"011200","name":"HMM","rank":1,"side":"long","score":5,"cap":123456,"tv1w":9870,"tvMom":0.42,
           "frgn1w":202,"orgn1w":148,"flowBp":28,"shortRlim1w":3.1,"shortMom":-0.8,"loanChg":-120,"ret1w":4.8,
           "hasStockFuture":true,"reason":"거래대금 +42%, 외인+기관 +350억, 공매도 비중 -0.8pp"}]}],
 "diag":{"calls":{"db":512,"hub":80},"secs":310,"truncated":[]}}
```
`signals.compact` 와 같은 축약본을 `signals/<date>`에 함께 남겨 백필·대시보드가 쓰게 한다.

## 4. 레포별 작업 목록

### A. backtest_stock-swing (주 작업)
- A0 **선행 버그**: `prices.YFinancePrices._market` 이 `startswith("KOSDAQ")`인데 krx_companies market 값은 "코스닥" → 전 종목 `.KS` 판정. `SWING_PRICES` 기본값이 yfinance 라 실운용 전 수정.
- A1 `swing/universe/dbquote.py`: DB증권 REST 클라이언트(토큰 = `DbHubPrices`의 Redis 읽기 재사용, URL별 TPS 2·연속조회 `cont_yn/cont_key`). USTOCKCOND / USTOCKCONDLIST / DAYSTOCKTJJ 래퍼.
- A2 `swing/universe/build.py`: U1~U6. 노브 `SWING_UNI_TOPK_TV(15)`, `SWING_UNI_TOPN(10)`, `SWING_UNI_MIN_TV_EOK(20)`, `SWING_UNI_LOOKBACK_W(4)`, `SWING_UNI_KOSDAQ(0)`.
- A3 `swing/sources.py`: `CandidateSource` 인터페이스(`name`, `fresh(date)`, `candidates(date, ctx, exclude, side)`), `MatrixSource`(기존 `signals._candidates` 이식), `SectorTopSource`. `Candidate`에 `source, rank, metrics, hasStockFuture` 추가.
- A4 `swing/daily.py`: 소스 병합 → **쿨다운**(store `analyzed/<code>` = 마지막 entry 판단일, `SWING_REANALYZE_DAYS(3)` 안이면 생략) → **섹터 노출 상한**(`SWING_MAX_SECTOR_WEIGHT(0.25)`) → 일 상한. 소스별 `fresh` 분리(`wb_ok` 단일 게이트 해체). `capSkipped`에 source·사유.
- A5 `agent_iface.DecisionRequest` + `pipeline.strategy_context`: `source/metrics` 전달, 문구에 거래대금·공매도 추이 근거 포함(Flow 애널리스트 user 입력에도 추가).
- A6 `engine.place_entry → position → trade`에 `source` 전파(리포트에서 소스별 성과 비교).
- A7 `server.py` `/api/swing/universe?date=` + 대시보드 "유니버스" 탭(업종 × Top10 표, 방향·점수·근거), 섹터 시그널 탭 `sig()` 클래스에 새 라벨.
- A8 테스트: `tests/test_universe.py`(mock DB 응답으로 U2~U6 결정성), `test_daily` wb 헬퍼·MockAgent·demo_data 확장. 백필은 유니버스 과거 스냅샷이 없어 **sectorTop 소스는 백필 불가** — run 로그에 명시.
- A9 LLM 예산: 후보가 수십 개로 늘면 13콜×N. `SWING_MAX_NEW_PER_DAY` 를 실제 상한으로 쓰게 되므로 소스 통합 랭킹(점수 정규화) 필수. 처음엔 sectorTop 몫을 `SWING_UNI_NEW_PER_DAY(5)`로 따로 캡.

### B. trading_bot_HUB_KIS (보강 1건 + 정리)
- B1 `hub.py` `short_rank_payload(token)`: `FHPST04820000` 0001/1001/2001 × `FID_INPUT_CNT_1=4` → `{asof, items:[{code,name,qty1w,pbmn1w,volRlim,pbmnRlim}]}`, 캐시 3h(16시 이후). `strategy_runner._Health`에 `/short-rank` 분기. 모니터 재빌드(vi_limit_monitor 빈 커밋) 필요.
- B2 `_fetch_investor_daily` 응답에 `acml_vol/acml_tr_pbmn` 추가(KIS output2에 존재) → `/flow daily`로도 거래대금 확보 가능(DB증권 장애 시 폴백).
- B3 미커밋 `hub.py` 재시도 변경(2026-09-23, `_fetch_index_candles tries=3` + `_sector_returns` 축별 격리) 커밋 여부 결정. 장 마감 후 푸시.
- B4 (선택) `/flow` 연속 호출에 공용 스로틀(토큰 버킷) — 유니버스 보강 80콜 수준이면 당장 불필요.

### C. backtest_stock main (버그 2건, 확장과 독립)
- C1 **`_sector_top_caps`(generate_weekly_briefing.py:549) 덮어쓰기 버그**: `_SEC_ALIAS`로 정규화된 키에 `=` 대입 → 금융이 은행 4종목, 제조가 기타제조 23종목, 전기·가스가 전기·가스·수도 3종목으로 덮임(2026-10-13 krx_sector_map 실측). `extend` 후 시총 재정렬로 수정.
- C2 `shortListed`가 공매도 대금 절대값 상위 10 → 대형주 편향(SK하이닉스 3,830억 = 시총 0.03%). `pbmnRlim` 또는 `capPct` 기준으로 전환 검토.
- C3 (선택) `weekly_briefing.json`에 swing 유니버스 요약 `sectorTop` 미러링 — 2단계.

### D. trading_bot_HUB_DB (지금은 변경 없음)
- 실주문 단계에서만: 다종목 현물 매수·주식선물 매도 Broker 확장, 잔고 조회(`CSPAQ03420`) 기반 포지션 대조, 오버나이트·롤오버. 현 M4(DRY_RUN 수집) 흐름과 분리해 진행.

## 5. 실측으로 확정할 것 (구현 전 1회 호출, Railway에서)
1. USTOCKCONDLIST 1페이지 행수 상한·연속조회 여부(증권 29행 예시가 전체인지), 코스닥(Q) 업종 코드 유무.
2. DAYSTOCKTJJ 행수 상한(25영업일 1콜 가능?), `AcmlTrPbmn`·`*TrPbmn` 단위(원/천원), 당일 행 확정 시각.
3. 업종명 조인: DB 32 업종명 ↔ KIS sectorFlow 24행 `norm_sec` 매칭률(미매칭 업종은 신호 없음=혼조 취급).
4. KIS `FHPST04820000` 응답 정렬 기준·행수, `FID_INPUT_CNT_1=4` 가 실제 1주 누적인지.
5. swing-cron 총 소요(목표 ≤ 8분: DB 4~5분 + 허브 1분 + LLM) — Railway 크론 타임아웃 확인.

## 6. 단계 순서(권장)
1. C1·A0 버그 수정(독립, 작음) → 2. 실측 1~4 → 3. A1·A2 + `universe/<date>` 저장·대시보드 표만(후보 미연결, 1~2주 관찰) → 4. B1 + U5 공매도 보강 → 5. A3~A6 후보 연결(`SWING_UNI_NEW_PER_DAY=3`부터) → 6. A7~A9 리포트·테스트 → 7. 소스별 성과 비교 후 가중치 조정.

- 2026-10-14 **혼조 업종 거래대금 가속**: 혼조 업종도 1차 컷 종목 일별 조회(DAYSTOCKTJJ)를 돌려 업종 주간 거래대금·가속(`secTv1w`·`secTvMom`)을 채운다(스트립 카드·정렬에 반영). 종목 점수·후보는 여전히 방향 업종만, 대분류(제조·금융)는 계속 제외. 비용 ≈ 혼조 업종당 DB 20콜.
