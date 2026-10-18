# 장중 포트폴리오 매니저(Live PM) — 설계·구현 계획

2026-10-18 작성(설계 초안 + GitHub 레퍼런스 조사 통합). **M0 완료, M1~ 미구현.** §12 의 기본 가정은 사용자가 바꾸면
해당 단계만 고친다.

> **2026-10-18 역할 분리**: 장중 시스템을 **Live Trader(집행)** 와 **Live PM(판단)** 으로 나눈다 —
> [PLAN_LIVE_TRADER.md](PLAN_LIVE_TRADER.md) §0. Live Trader(구 VWAP 실행기, `swing/vwap`, 서비스 swing-vwap)가 이미
> 시세 수신·분할 체결·**손절 감시와 집행**·`intraday/<date>` 기록을 맡고 있다. 따라서 Live PM 은 **판단만** 하고,
> 집행은 Live Trader 에 지시(§8·PLAN_LIVE_TRADER §5)로 넘긴다. 아래 층 표의 "담당" 열이 이 분리를 반영한다.

## 0. 목적과 이름
- swing 은 하루 한 번(16:30 KST 장 마감 후)만 판단하므로, 장중에 일어난 일에는 다음 날 시가에야 대응한다.
  Live PM 은 **장중에 보유 포트폴리오 전체를 지켜보다가 이벤트가 생기면 판단**하는 상위 에이전트다.
- 이름 충돌 주의: trading_agent 안의 `13 Portfolio Manager` 는 **종목 하나의 등급**을 낸다.
  Live PM 은 **포트폴리오 단위**로 판단하며, 코드와 문서에서는 `livepm` / "Live PM" 으로 부른다.
- 신규 진입은 계속 하루 배치(trading_agent)가 맡고, Live PM 은 **보유 관리(위험 축소)부터** 맡는다.

## 1. 층 구조 — LLM 은 틱 경로에 두지 않는다
| 층 | 주기 | 하는 일 | LLM | 담당(2026-10-18) |
|---|---|---|---|---|
| L0 시세 공급 `MarketFeed` | 실시간 / 폴링 | 보유 종목 현재가·장중 고저·VI·섹터 | 없음 | **Live Trader** — `swing/intraday/feed.py`(/prices). Live PM 은 Live Trader 가 공개한 `intraday/<date>.vwap` 을 읽는다. /status(VI·업종)는 Live PM 이 직접 |
| L1 상태 엔진 `LiveBook` | 틱마다 | 원장(ledger) + 현재가 → 평가손익·노출·손절 거리·롤링 수익률 | 없음 | Live PM (VWAP·1분봉은 Live Trader `vwap/state.py`) |
| L2 반사 규칙 `Reflex` + 거래 상태 | 틱마다 | 하드 리밋 감시, 거래 상태 전환(§4) | 없음 | X1 손절 = **Live Trader 가 집행**. 거래 상태·X2~X4 = Live PM |
| L3 트리거 `Trigger` | 틱마다, 디바운스 | 이벤트 감지 → Live PM 호출 여부 결정 | 없음 | Live PM |
| L4 판단 `LivePM` | 이벤트 + 정기 | 스냅샷 → 구조화된 결정 | **있음** | Live PM |
| L5 검증 `RiskGate` | 결정마다 | 결정을 축소·거부만 가능(키울 수 없음) | 없음 | Live PM |
| L6 실행 `Executor` | 결정마다 | shadow: 기록만 / paper: 원장 장중 반영 / real: 범위 밖 | 없음 | **Live Trader** — Live PM 은 지시만(`livepm/<date>/instructions`) |
| L7 평가·대사 `Evaluate` | 장 마감 후 | shadow 반사실 손익, paper 결정↔원장 대사 | 없음 | Live PM |

LLM 이 실패하거나 느려도 L1~L3·L5 는 독립적으로 돈다. LLM 실패·타임아웃·스키마 위반은 **hold** 로 처리한다.
(레퍼런스: kalshi-ai-trading-bot·Infinite Bagholder 모두 손절·비중 한도를 LLM 밖 코드로 강제 — §13)

## 2. 시세 공급(MarketFeed) — 폴링으로 시작, WS 는 필요할 때
| 구현 | 내용 | 단계 |
|---|---|---|
| `ReplayFeed` | 저장한 틱 기록(`livepm/<date>/ticks`) 재생 — 테스트·소급 검증 | M1 |
| `PollingFeed` | `FLOW_API_BASE/prices?codes=`(vi_limit 서비스, 이미 운용 중)를 **15초**마다 + `/status` 의 vi·sectors 를 60초마다 | M1 |
| `RedisStreamFeed` | 허브 `ws_gateway` 가 Redis Streams 에 발행하는 체결가 구독 | M4(선택) |

- 공통 출력: `Tick{code, ts, price, dayHigh, dayLow, dayOpen, vol, vi}`.
- `ret5m`·`ret30m` 은 `LiveBook` 이 틱 링버퍼(종목당 최근 40분)로 직접 계산한다 — 소스가 분봉을 주지 않아도 됨.
- PollingFeed 는 받은 틱을 `livepm/<date>/ticks` 에 1분 간격 샘플로 저장 → 그대로 ReplayFeed 입력이 된다.
- **M0 실측(2026-10-18, 10/16 장 마감 데이터)**:
  - `/prices?codes=` = 허브 `hub._fetch_prices` 가 KIS REST 현재가 조회(**FHKST01010100** inquire-price)를 종목마다
    호출한 결과(종목별 7초 캐시). 실측 당시 응답 `{price, rate, volume, open, prevClose}` 에는 장중 고저가 없었는데,
    KIS 응답(`stck_hgpr`/`stck_lwpr`)에는 있고 허브가 버리고 있었다 → **허브에 `high`/`low` 추가(2026-10-18)**.
    따라서 `dayHigh`/`dayLow` 는 **KIS 당일 누적 고가·저가**를 그대로 쓴다 — 15초 폴링 사이의 꼬리도 다음 폴링에서 잡힌다.
  - X1 도달 시각 = `dayLow ≤ 손절`(Short 는 `dayHigh ≥ 손절`)이 처음 관측된 폴링 시각(최대 15초 지연).
    Live PM 기동 시점에 이미 도달해 있으면 시각을 알 수 없으므로 "기동 전 도달"로 기록하고, 그날 모든 결정보다 앞선 것으로 본다(보수적).
  - 허브가 아직 `high`/`low` 를 주지 않는 응답(배포 전 등)이면 폴링 관측 최댓값·최솟값으로 대체하고 틱에 `hlSource="observed"` 를 남긴다.
  - `/status` → `vi`(시장 전체 최근 VI 30건: code·vi_on·kind·vi_time·cncl_time·vi_prc), `upper`/`lower`(상하한가),
    `sectors`(**KOSPI 산업별 26개**: code·name·index·changePct), 각 `*_asof`. **KOSPI·KOSDAQ 종합지수와 KOSDAQ 업종은 없음.**
  - 따라서 T4 는 `vi` 에서 보유 종목 코드·`vi_on` 으로 판정, T5 는 보유 종목 업종이 KOSPI 26개 업종명에 매칭될 때만
    적용(KOSDAQ 종목은 T5 제외, 업종명 정규화는 기존 `krx_sector_map` 방식 재사용).

## 3. 트리거 (L3) — 언제 Live PM 을 깨우는가
| # | 트리거 | 기본값(노브) | 대상 |
|---|---|---|---|
| T1 | 손절선 근접 — 진입가→손절가 거리의 X% 소진 | 70% (`LIVEPM_STOP_NEAR`) | 보유 |
| T2 | 목표가 도달 | 도달 시 | 보유 |
| T3 | 급변 — 최근 5분 변동률(불리한 방향) | 3% (`LIVEPM_JUMP_5M`) | 보유 |
| T4 | VI 발동 | 발동 시 | 보유 |
| T5 | 보유 섹터 반전 — `kis:sectors` 하락 상위 5 진입(Long) / 상승 상위 5(Short) | 상위 5 | 보유 섹터 |
| T6 | 포트폴리오 일중 손익 | −1.5% (`LIVEPM_DD_DAY`) | 전체 |
| T7 | 정기 점검 | 09:10 · 11:00 · 13:30 · 14:50 | 전체 |

- 방향은 `side` 로 뒤집는다(Short 는 가격 상승이 불리).
- 디바운스: 같은 종목 + 같은 트리거는 **15분**(`LIVEPM_DEBOUNCE_MIN`) 안에 다시 호출하지 않는다.
  여러 트리거가 겹치면 3초 모아 한 호출로 묶는다.
- 상한: 하루 **40회**(`LIVEPM_MAX_CALLS`) **그리고** 하루 **LLM 비용 3,000원**(`LIVEPM_MAX_COST_KRW`).
  하나라도 넘으면 T7 만 남기고, 둘 다 넘으면 판단 정지(모니터링·기록만).

## 4. 거래 상태와 반사 규칙 (L2) — LLM 과 무관하게 항상 적용
거래 상태 3단계(레퍼런스: NautilusTrader RiskEngine 의 ACTIVE/REDUCING/HALTED):
| 상태 | 허용 | 진입 조건 |
|---|---|---|
| `active` | 모든 1차 액션 | 기본 |
| `reducing` | 포지션을 줄이는 액션만(trim·exit·tighten_stop·flag_review). 2차의 add/enter 차단 | X2 발동, 또는 수동 |
| `halted` | 판단·실행 정지, 모니터링·틱 기록·평가만 | X3(킬스위치), 비용 상한 초과, 시세 공급 5분 끊김 |

1차 액션은 원래 위험 축소뿐이라 `active`·`reducing` 차이는 2차부터 의미가 있지만, 상태 구조는 처음부터 둔다.

| # | 반사 규칙 | 기본값 |
|---|---|---|
| X1 | 장중 손절가 도달 감지 | 엔진과 같은 판정(Long 저가 ≤ 손절, Short 고가 ≥ 손절). **도달 시각을 기록** — 이후 그 종목의 exit/trim 결정은 G8 로 거부(이미 손절된 것으로 간주) |
| X2 | 포트폴리오 일중 손실 −3% | `reducing` 전환 + 경고 |
| X3 | 킬스위치 | 저장소 키 `livepm/state` = `halted` (수동으로 되돌릴 때까지 유지) |
| X4 | 시세 끊김 | 5분 이상 틱 없음 → `halted`, 복구 후 자동 `active`(X2·X3 사유가 없을 때만) |

## 5. 입력 스냅샷 (L4 입력) — 압축된 JSON 한 덩어리
틱 원본은 넣지 않는다. 한 번 호출에 대략 3~6k 토큰을 목표로 한다.
```json
{
  "asof": "2026-10-19T10:42:05+09:00",
  "state": "active",
  "trigger": [{"id": "T1", "code": "005930", "detail": "손절 거리 78% 소진"}],
  "portfolio": {"equity": 512340000, "cash": 201000000, "dayPnlPct": -0.8,
                 "longGross": 0.52, "shortGross": 0.21, "sectorWeights": {"전기·전자": 0.18}},
  "positions": [{
    "code": "005930", "name": "삼성전자", "side": "long", "sector": "전기·전자",
    "qty": 300, "entry": 61200, "stop": 59400, "target": 66000, "heldDays": 3, "weight": 0.036,
    "px": 59800, "pnlPct": -2.3, "stopDistUsedPct": 78,
    "ret5m": -1.2, "ret30m": -2.0, "retDay": -2.6, "vi": false,
    "sectorState": "하락 상위 3위",
    "thesis": "PM Overweight — HBM 수주 기대, 외국인 순매수 전환(진입 판단 요약 200자)",
    "invalidation": "59,400 이탈 또는 외국인 순매도 재전환",
    "lastLivePm": {"ts": "09:10", "action": "hold", "reason": "..."}
  }],
  "market": {"sectorsUp": ["..."], "sectorsDown": ["..."]},
  "today": {"callsUsed": 6, "costKrw": 410, "decisions": ["09:10 hold all", "10:05 trim 000660 50%"]}
}
```
- `thesis` = 진입 판단 `Decision.summary`(PM Executive Summary), `invalidation` = `trader_reason(d)`(Trader 손절 근거).
  **M0 에서 `daily.py` 가 주문 생성 시 주문에 붙이고, 체결 시 포지션으로 넘긴다.** 기존 보유분은 저장된
  `decision/<date>/<code>/entry` 에서 소급 추출한다.
- 지수: `/status` 에 종합지수가 없어(M0 실측) 1차에선 `market` 에 `sectorsUp`/`sectorsDown`(KOSPI 업종 상·하위 5)만 넣는다.
  종합지수가 필요하면 허브에 업종 0001(KOSPI)·1001(KOSDAQ) 발행을 추가하는 작업을 M4 와 함께 검토.
- `lastLivePm`·`today.decisions` 는 결정이 오락가락하지 않게 하는 단기 기억이다.

## 6. 출력 스키마 (L4 출력) — `llm.generate_json(schema=...)` 로 강제
```json
{
  "type": "object",
  "required": ["assessment", "decisions"],
  "properties": {
    "assessment": {"type": "string", "description": "포트폴리오 상황 2~3문장"},
    "decisions": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["code", "action", "reason", "confidence", "thesisValid"],
        "properties": {
          "code": {"type": "string"},
          "action": {"enum": ["hold", "tighten_stop", "trim", "exit", "flag_review"]},
          "trimPct": {"type": "number", "minimum": 0.1, "maximum": 0.9},
          "newStop": {"type": "number"},
          "reason": {"type": "string", "description": "근거 1~2문장, 스냅샷 수치 인용"},
          "thesisValid": {"type": "boolean"},
          "confidence": {"type": "number", "minimum": 0, "maximum": 1},
          "invalidation": {"type": "string", "description": "이 결정을 뒤집을 조건"}
        }
      }
    }
  }
}
```
| action | 의미 | paper 단계 적용 |
|---|---|---|
| hold | 유지 | 없음 |
| tighten_stop | 손절가를 유리한 쪽으로만 이동(Long ↑ / Short ↓) | 원장 손절가 갱신(§8 손절 시각 규칙) |
| trim | 일부 청산(10~90%) | 결정 시점 현재가로 일부 청산 |
| exit | 전량 청산 | 결정 시점 현재가로 청산 |
| flag_review | 오늘 장 마감 후 trading_agent 재판별 요청 | 기존 `reviewFlag` 경로 재사용(`livepm` 플래그) |

`add`(추가 매수)·`enter`(신규 진입)은 2차(M5)에서 `active` 상태 전용으로 검토한다.

## 7. RiskGate (L5) — 축소·거부만 가능
| # | 검사 | 실패 시 |
|---|---|---|
| G1 | 스냅샷에 없는 종목 코드 / 스키마 위반 | 거부 |
| G2 | `tighten_stop` 이 손절을 불리하게 옮기거나 현재가를 넘어섬 | 거부 |
| G3 | `confidence` < 0.6 인 `exit`/`trim` | `flag_review` 로 강등 |
| G4 | 같은 종목에 대해 30분 안에 반대 방향 결정 | 거부(플립플롭 방지) |
| G5 | 체결 당일 포지션의 `exit` | 손절가 도달 전이면 `flag_review` 로 강등(체결일 판단 존중) |
| G6 | 한 번의 호출에서 `exit`+`trim` 합계가 평가액의 30% 초과 | 큰 것부터 30% 까지만 허용 |
| G7 | 장 시간 밖(09:00~15:20 외) | 거부 |
| G8 | X1 로 이미 손절가 도달이 기록된 종목의 exit/trim/tighten_stop | 거부(엔진 손절이 우선) |
| G9 | 거래 상태가 허용하지 않는 액션(§4) | 거부 |

거부·강등은 원래 결정과 함께 사유를 기록한다 — 평가 때 "게이트가 막은 결정"의 반사실 손익도 본다.

## 8. 실행 모드 (L6) — `LIVEPM_MODE`

> 2026-10-18 역할 분리 이후: paper 집행은 Live PM 이 원장을 고치는 것이 아니라 **Live Trader 에 지시**를 남기고,
> Live Trader 가 `intraday/<date>` 에 기록 → 16:30 `settle_day` 가 확정한다(PLAN_LIVE_TRADER §5). 아래 `engine.close_intraday`
> 신설 계획은 엔진 일부 청산(`_close_position(qty=)`)·`_apply_exit` 로 이미 대체됐다. shadow 는 지시를 남기지 않고 기록만 한다.
| 모드 | 동작 | 단계 |
|---|---|---|
| `shadow` | 결정·당시 가격만 기록, 원장 변경 없음 | **M2 (2~4주)** |
| `paper` | 원장에 장중 청산·손절 갱신 반영 | M3 (승급 기준 통과 후) |
| `real` | 국내판 order_guard 경유 실주문 | 범위 밖 — 별도 설계 |

### paper 모드와 하루 엔진(settle_day)의 충돌 처리
일봉 엔진은 장중 순서를 모르므로 다음 규칙으로 맞춘다.
1. **exit/trim**: `engine.close_intraday(L, code, ts, price, pct, reason="livepm_exit")` — 청산된 수량은 그날
   `settle_day` 대상에서 빠진다. 체결가 = 결정 시점 현재가(슬리피지 노브 `LIVEPM_SLIPPAGE`, 기본 0.1%).
2. **결정 전에 이미 손절가 도달**(X1 기록)이면 G8 로 막혀 엔진의 기존 손절 처리가 그대로 적용된다.
3. **tighten_stop**: 그날의 일봉 저가에는 결정 전 가격이 섞여 있으므로 엔진은 **그날엔 기존 손절가**로 판정
   (`stopPending={stop, from: ts, baseLow, baseHigh}`), 결정 이후 새 손절 도달은 Live PM 이 틱으로 감지해 `close_intraday(reason="livepm_stop")`.
   KIS `dayLow` 는 당일 누적이라 결정 전 저가가 섞이므로, 결정 시점 `dayLow` 를 `baseLow` 로 저장하고
   **이후 `dayLow` 가 `baseLow` 보다 내려가면서 새 손절 이하**가 되거나 현재가가 새 손절 이하일 때만 도달로 본다(Short 는 `dayHigh`·`baseHigh` 대칭).
   다음 영업일부터 새 손절가가 원장 손절가가 된다.
4. 모든 장중 청산은 `trades` 에 `source="livepm"` 으로 남아 대시보드·통계에서 구분된다.

### 평가·대사 (L7, 장 마감 후 `run_daily` 끝에서 호출)
- **shadow**: 결정마다 "실행됐다면"의 반사실 결과 → `livepm/eval/<date>`.
  exit/trim = (결정가 − 실제 원장 청산가 또는 그날 종가 평가) × 수량 = 회피 손실 / 놓친 이익.
  tighten_stop = 새 손절 발동 여부와 발동 후 회복 여부. hold = 그날 엔진 손절 발동(놓친 경고).
- **paper**: 결정 기록 ↔ 원장 `trades` 대사(레퍼런스: kalshi 봇 `fills`) — 기록됐는데 원장에 없거나 수량이 다르면 경고.
- 누적 지표: 회피손익(원), 적중률(exit 후 종가가 더 불리했던 비율), 강등·거부 건수, 호출당 비용.

### shadow → paper 승급 기준(제안)
- 최소 10영업일·exit/trim 결정 15건 이상
- 누적 회피손익 > 0, exit 적중률 ≥ 55%
- 하루 비용 상한 초과 0회, 스키마 위반율 < 5%

## 9. 실행 위치와 모델
- 프로세스: **새 Railway 서비스 `swing-livepm`**(같은 레포·`swing_paper` 브랜치, 시작 `python -m swing.livepm`,
  config `swing/railway.livepm.json`). 평일 08:55~15:30 루프, 그 외 시간엔 다음 개장까지 대기. 휴장일은 `krx_calendar`.
  swing-web 스레드에 넣으면 웹 재시작 때 같이 죽고, 크론은 장중 상시 실행이 맞지 않는다.
- 변수: `DATABASE_URL`, `REDIS_URL`(시세 버스), `FLOW_API_BASE`(/status), LLM API 키. 규칙값은 코드 상수(2026-10-18 결정 반영 예정, M1-8).
- 저장: 기존 `store.from_env()`(Postgres `swing_docs`).
  | 키 | 내용 |
  |---|---|
  | `livepm/state` | 거래 상태와 사유·변경 시각 |
  | `livepm/<date>/instructions` | Live Trader 에 넘기는 지시(추가만) |
  | `livepm/<date>/events` | 트리거·반사 규칙 이벤트 |
  | `livepm/<date>/calls/<n>` | 스냅샷·원 결정·게이트 결과·모델·토큰·비용 |
  | `livepm/eval/<date>` | 평가·대사 결과 |
- 원장 동시 쓰기: shadow 는 원장을 읽기만. paper 에선 Live PM(장중)과 swing-cron(16:30)이 시간대로 나뉘지만,
  안전하게 원장 문서에 `rev` 를 두고 낙관적 잠금(쓰기 직전 rev 비교)을 건다.
- 모델: `LIVEPM_LLM_CHAIN`(기본 `anthropic:<최신 Sonnet>,gemini:gemini-3.5-flash`). `llm.generate_json` 에
  `chain=` 인자를 추가한다(현재 `_chain()` 은 `LLM_CHAIN` 환경변수만 읽음). 비용은 응답 토큰 수 × 단가표로 계산.

## 10. 시스템 프롬프트 초안
```
당신은 한국 주식 스윙 모의 포트폴리오의 장중 포트폴리오 매니저다.
보유 종목은 이미 별도 리서치(trading_agent)를 거쳐 진입했다. 당신의 역할은 장중 정보를 보고
'진입 논거(thesis)가 아직 유효한가'와 '포트폴리오 위험이 허용 범위인가'를 판단하는 것이다.

원칙:
1. 기본값은 hold 다. 근거가 스냅샷 수치로 설명될 때만 행동한다. 단기 노이즈(5분 변동)만으로 exit 하지 않는다.
2. 허용 액션은 hold, tighten_stop, trim, exit, flag_review 뿐이다. 매수·비중 확대는 할 수 없다.
3. thesis 의 invalidation 조건이 충족됐으면 thesisValid=false 로 표시하고 exit 또는 trim 을 검토한다.
4. 손절가는 유리한 방향으로만 옮길 수 있다(Long 은 올리기만, Short 는 내리기만).
5. 섹터 전체가 밀리는 날에는 개별 종목보다 섹터 집중도를 먼저 본다.
6. 직전 결정(lastLivePm, today.decisions)과 반대로 가려면 무엇이 바뀌었는지 reason 에 적는다.
7. 확신이 낮으면 exit 대신 flag_review 를 쓴다.
8. 트리거된 종목은 반드시 decisions 에 포함하고, 나머지는 바꿀 필요가 있을 때만 포함한다.
출력은 주어진 JSON 스키마만 따른다.
```

## 11. 모듈 구성
```
swing/livepm/
  __init__.py
  __main__.py   장중 루프(시간표·상태·호출/비용 상한·틱 저장)
  config.py     LIVEPM_* 노브(swing/config.py 와 같은 방식)
  feed.py       Tick · ReplayFeed · PollingFeed (+ M4 RedisStreamFeed)
  book.py       LiveBook — 원장 + 틱 → 포지션 지표(링버퍼 ret5m/30m·stopDistUsedPct·dayPnl)
  state.py      거래 상태(active/reducing/halted) + 반사 규칙 X1~X4
  triggers.py   T1~T7 + 디바운스·묶음
  snapshot.py   §5 스냅샷(thesis 소급 추출 포함)
  agent.py      §6 스키마·§10 프롬프트·llm 호출·비용 계산·실패 시 hold · MockLivePM
  gate.py       §7 RiskGate G1~G9
  executor.py   shadow / paper(engine.close_intraday 호출)
  evaluate.py   §8 반사실 평가·대사·누적 지표
swing/tests/test_livepm_*.py
swing/railway.livepm.json
```

## 12. 기본 가정 (바꾸시면 해당 단계만 수정)
| # | 항목 | 기본 가정 | 영향 단계 |
|---|---|---|---|
| D1 | 1차 액션 범위 | 위험 축소 5종만 | M1 |
| D2 | 시세 공급 | **Live Trader 가 폴링한 시세를 Redis 버스로 공유**(2026-10-18 결정·구현), WS 는 M4 선택 | M1·M4 |
| D3 | 판단 모델 | Anthropic 우선 + Gemini 폴백 | M0·M2 |
| D4 | 실행 위치 | 새 서비스 `swing-livepm` — **2026-10-18 확정**(장애 격리·장중 배포 독립) | M3 |
| D6 | 집행 방식 | **Live Trader 가 VWAP 분할로 집행**(2026-10-18 확정) | M2 |
| D7 | 도입 | **shadow 없이 바로 원장 반영**(2026-10-18 확정) | M3 |
| D5 | 방향 | Long·Short 모두(엔진이 이미 대칭이라 추가 비용 작음) | M1 |

---

# 구현 계획

규모 표기: S = 반나절 이하, M = 하루 안팎, L = 이틀 이상. 각 단계는 **완료 기준**을 통과해야 다음으로 간다.

## M0 — 기반 정리 (S~M)
| # | 작업 | 파일 |
|---|---|---|
| 0-1 | `llm.generate_json(..., chain=None)` 인자 추가, 미지정 시 기존 동작 | `llm.py`, `tests/test_llm_chain.py` |
| 0-2 | 진입 주문·포지션에 `thesis`·`invalidation` 저장(주문 생성 시 `Decision.summary`·`trader_reason`) | `daily.py`, `engine.py`(`place_entry extra` 경유) |
| 0-3 | 기존 보유분 thesis 소급 스크립트(저장된 entry 판단에서 추출) | `swing/tools/backfill_thesis.py` |
| 0-4 | `/prices`·`/status` 응답 실측 — 장중 고저·지수 포함 여부를 이 문서 §2·§5 에 기록. 고저 누락 → 허브 `_fetch_prices` 에 `high`/`low` 추가 | 문서, `trading_bot_HUB_KIS/hub.py` |
| 0-5 | `swing/livepm/` 골격 + `config.py` 노브 | 신규 |

완료 기준: 기존 테스트 전부 통과, 새 체결 포지션 JSON 에 thesis 가 보임, §2 실측 결과 기록.

**M0 완료(2026-10-18)** — `llm.generate_json(chain=)`·`configured(chain=)`, `agent_iface.thesis_fields`(주문 `extra` →
체결 시 포지션으로 복사, 미도달 재주문에도 유지), `swing/tools/backfill_thesis.py`(기본 미리보기, `--apply` 로 저장),
`swing/livepm/config.py` 노브 + `python -m swing.livepm`(설정 출력만), 테스트 `test_livepm_m0.py` 7건·`test_llm_chain` 1건 추가,
전체 171건 통과. 운영 원장 소급(`backfill_thesis.py --apply`)은 배포 후 사용자 확인을 받아 실행.

> 2026-10-18 결정 반영으로 M1~M3 를 다시 썼다 — 별도 서비스 + Redis 시세 버스, 집행은 Live Trader 가 VWAP 분할로,
> shadow 없이 바로 원장 반영. 기존 "shadow 운영(M2)·paper 반영(M3)·engine.close_intraday" 계획은 폐기.

## M1 — Live PM 판단 코어, 오프라인 (L)
| # | 작업 | 테스트 |
|---|---|---|
| 1-1 | 입력 = Redis 시세 버스 `BusFeed`(오늘 기록 따라잡기 + 새 항목) — **구현됨**(`swing/intraday/bus.py`). 재생은 버스 기록·`intraday/<date>/ticks` | (구현됨) test_bus |
| 1-2 | `book.py` LiveBook — 원장 + 버스 시세 → 손익·노출·손절 거리 소진율·5/30분 수익률(Long/Short) | 링버퍼, 부호 |
| 1-3 | `state.py` 3상태 + X2(일중 손실 → reducing)·X3(킬스위치)·X4(버스 heartbeat 만료·시세 끊김 → halted). X1 손절은 Live Trader 몫 | 상태 전환 |
| 1-4 | `triggers.py` T1~T7·디바운스·묶음·호출/비용 상한. T4·T5 용 `/status`(VI·업종)는 Live PM 이 60초마다 직접 조회 | 디바운스·상한 |
| 1-5 | `snapshot.py` — Live Trader 체결·손절 현황(`intraday/<date>`) 포함, thesis 누락 처리 | 토큰 상한 |
| 1-6 | `agent.py` §6 스키마·§10 프롬프트 + `MockLivePM`, 실패·스키마 위반 → hold | |
| 1-7 | `gate.py` G1~G9 (G8 = Live Trader 가 이미 손절 집행 중인 포지션은 거부) | 규칙별 |
| 1-8 | `livepm/config.py` 노브를 **코드 상수로 전환**(Live Trader 와 같은 원칙 — 환경변수 덮어쓰기 없음) | |
| 1-9 | 통합 재생: 버스 기록 → 트리거 → Mock 판단 → 게이트 → 지시 목록 | `test_livepm_day.py` |

완료 기준: 전체 테스트 통과, 합성 하루 재생으로 이벤트·결정·지시 문서가 생성됨(네트워크·API 키 없이).

## M2 — 지시 인터페이스(Live PM → Live Trader → 원장) (M~L)
| # | 작업 |
|---|---|
| 2-1 | Live PM 이 `livepm/<date>/instructions` 에 지시를 **추가만** 한다(id·시각·포지션·action·pct·newStop·사유) |
| 2-2 | Live Trader 가 매 폴링마다 새 지시를 읽어 집행·결과를 `intraday/<date>.instructions` 에 기록(같은 id 재집행 없음) |
| 2-3 | exit·trim → 청산 슬라이스(trigger `livepm`) — **VWAP 분할 집행**: 첫 10% 즉시(현재가) + 나머지 VWAP 60분할·5분 + 기다리면 손해인 방향 5% 전량 |
| 2-4 | tighten_stop → Live Trader 가 그 시각부터 새 손절로 감시(유리한 쪽으로만), 정산 때 원장 손절가 갱신(그날 일봉 판정은 기존 손절가) |
| 2-5 | flag_review → 정산 후 run_day 가 trading_agent 재판별 대상에 추가 |
| 2-6 | `engine.settle_day`: 청산 사유 `livepm_exit`/`livepm_trim`, 손절 갱신 반영, 남은 수량 이월(기존 `_apply_exit` 재사용) |

완료 기준: 엔진·실행기 테스트에 지시 경계 사례(손절 중 지시, 같은 지시 중복, 체결일 포지션, 장 마감 직전 지시) 통과.

## M3 — 서비스 운영 (M)
| # | 작업 |
|---|---|
| 3-1 | `__main__.py` 장중 루프(08:55 기동, 15:30 종료, 휴장일 대기, 버스 heartbeat 확인) |
| 3-2 | 실제 LLM 연결 + 비용 계측 + 하루 비용 상한 |
| 3-3 | Railway `swing-livepm` 생성(시작 명령은 API 로 — 신규 서비스는 config-as-code 미적용), 변수 `DATABASE_URL`·`REDIS_URL`·LLM 키 |
| 3-4 | 장 마감 후 평가·대사(`livepm/eval/<date>`): 지시별 반사실 손익, 지시↔원장 대사 |
| 3-5 | API `/api/swing/livepm` + 대시보드 "Live PM" 탭, 청산 사유 라벨 |
| 3-6 | 첫 1~2주: 매일 결정 원문을 보며 프롬프트·트리거 임계값 조정(코드 상수 변경 + 이 문서 기록) |

## M4 — 웹소켓 게이트웨이 (선택, L, 허브 레포 `trading_bot_HUB_KIS`)
폴링 15초가 부족하다고 판단될 때만(예: T3 급변을 자주 늦게 잡음, 평가에서 지연 손실이 큼).
- 허브에 `ws_gateway`: approval_key 를 토큰 전용 Redis(Redis-lVBr)에 보관, **허브만 접속하고 Redis Streams 로 fan-out**.
  허브에 이미 `kis_ws.py`(지수선물 틱 파서 `parse_index_futures_tick` 등)가 있으므로 그 위에 주식 체결가(H0STCNT0)를 얹는 방향으로 시작.
- 구독 슬롯 매니저: 보유 종목 우선 + 남는 슬롯은 관심 상위 종목 순환(슬롯 상한은 KIS 문서로 확인).
- 재접속 시 기존 구독 자동 재등록(레퍼런스: python-kis 의 복구 가능한 웹소켓 클라이언트), PINGPONG, 갭은 REST 현재가로 보정.
- 체결통보(H0STCNI0) AES 복호화(레퍼런스: open-trading-api 샘플) — real 단계 대비.
- 배포 순서는 기존 원칙대로: hub@main push → vi_limit 브랜치 push 로 소비 서비스 재빌드.
- swing 쪽은 `RedisStreamFeed` 추가만 하면 된다(§2 공통 Tick).

## M5 — 확장 (범위 밖, 검토만)
- `add`·`enter` 액션(`active` 상태 전용, 게이트에 비중·노출 상한 추가).
- real 모드: 국내판 order_guard(허용 종목·1회 금액·일일 횟수·가격 괴리, fail-closed), 전용 앱키·별도 계좌,
  사람 승인 단계(텔레그램 등) 후 제한적 자동.

## 순서 요약
M0(완료) → M1 → M2 → M3(운영, 바로 원장 반영) → [필요 시 M4 웹소켓] → M5 검토

## 13. 레퍼런스 (2026-10-18 조사)
| 레포 | 가져온 아이디어 |
|---|---|
| [ryanfrigo/kalshi-ai-trading-bot](https://github.com/ryanfrigo/kalshi-ai-trading-bot) | 모든 주문이 risk_governor 를 통과, 일일 손실·드로다운 킬스위치, 하루 LLM 비용 상한, 정지 중에도 위험 축소 청산 허용, 결정 기록↔체결 대사(`fills`) |
| [NautilusTrader RiskEngine](https://nautilustrader.io/docs/latest/api_reference/risk) | 거래 상태 ACTIVE/REDUCING/HALTED, 주문당 금액 한도 |
| [Soju06/python-kis](https://github.com/Soju06/python-kis) | KIS 웹소켓 재접속 시 구독 자동 재등록, 구독 ticket 해지 방식 |
| [koreainvestment/open-trading-api](https://github.com/koreainvestment/open-trading-api) | WS 체결가·호가·체결통보 TR 과 AES 복호화 샘플 |
| [Infinite Bagholder](https://forum.alpaca.markets/t/show-alpaca-infinite-bagholder-an-open-source-pluggable-agentic-ai-harness-for-llm-powered-trading/19313) | 하드 리밋은 로컬 코드, 보유 포지션을 프롬프트에 주입 |
| [AlpacaSentry](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon/alpacasentry) | 감시 루프 + 트리거 시에만 추론 |
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents), [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 종목 단위 판단(thesis 공급원), 포트폴리오 단위 결정 스키마 |
