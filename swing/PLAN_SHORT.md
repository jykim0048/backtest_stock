# Short(공매도) 모의투자 — 규칙·구현 기록

2026-09-30 사용자 결정: 하루 분석 상한 10개(R2), 나머지 권장값 그대로. 구현·오프라인 테스트 완료,
**실제 trading_agent 로 Short 판단 실측(S4)은 네트워크 PC 몫**.

## 규칙 (R1~R13)
| # | 항목 | 확정값 | 노브 |
|---|---|---|---|
| R1 | 후보 | matrix **동반약세 → 수급이탈** 칸, 보유·주문 대기(방향 무관)·그날 Long 후보 제외 | `SHORT_SIGNALS` |
| R2 | 하루 분석 상한 | **10** (Long 과 별도) | `SWING_MAX_NEW_SHORT_PER_DAY` |
| R3 | 진입 조건 | PM **Sell/Underweight** + Trader **Sell**(= 공매도 진입) + 진입가·손절가, 손절 > 진입. 목표는 진입가 아래일 때만 | `SHORT_RATINGS` |
| R4 | 주문·체결 | 다음 영업일 1일 유효 지정가 매도. **고가 ≥ 진입가 → 진입가 체결** | — |
| R5 | 손절 | 시가 ≥ 손절 → 시가 환매, 장중 고가 ≥ 손절 → 손절가 환매, 체결일에도 적용 | — |
| R6 | 목표 | 저가 ≤ 목표 → 청산하지 않고 재판별(Long 과 같음). 손절과 동시면 손절 우선, 체결일 미적용 | — |
| R7 | 보유·재판별 | 5영업일 후 재판별, 최대 15영업일. PM **Buy/Overweight 면 다음 날 시가 환매**, Hold·Underweight·Sell 은 계속 보유. 목표 도달 후 보유는 손절 = min(새 손절, 기존, 공매도가)(본전 손절) | `COVER_RATINGS` |
| R8 | 청산 검토 트리거 | 보유 숏 섹터가 **동반강세/수급유입 전환** 또는 종목이 그 칸에 등재 | — |
| R9 | 담보·평가 | 체결 시 진입금액 100% 를 현금에서 담보로 묶고 매도대금은 현금에 넣지 않음(레버리지 없음). 평가 = 담보 + (진입가 − 현재가) × 수량 | — |
| R10 | 비용 | 진입(매도) 대금 × 0.2% 매도세, 환매 비용 없음, **대차수수료 연 4%** 를 달력일 일할 차감 | `SWING_BORROW_RATE` |
| R11 | 노출 한도 | 종목당 10%(Long 과 같음), **Short 총노출 ≤ 평가액의 30%**(보유 수량 × 현재가 + 대기 주문) — 넘으면 주문 스킵 | `SWING_SHORT_MAX_GROSS` |
| R12 | 현금 부족 | Long 과 같음 — 평가손실(숏은 가격 상승) 종목부터 시가 청산 | — |
| R13 | 제도 제약 | 업틱룰 = **진입가 ≥ 전일 종가**로 근사, 과열종목·대차 가능 여부·리콜은 미반영(대시보드에 모의 가정 표기) | — |

전체 끄기: `SWING_SHORT=0`(신규 Short 분석·주문 중단, 보유분 정산·환매 검토는 계속).

## 구현 위치
| 모듈 | 내용 |
|---|---|
| `config.py` | 위 노브 |
| `engine.py` | `side` 분기 — 체결·갭/장중 손절·목표 재판별·본전 손절(`apply_hold`)·담보/세금/대차수수료·`position_value`/`equity_now`·`short_exposure` 상한·현금 확보 |
| `signals.py` | `short_candidates`, `up_codes`, `review_triggers` 방향별 트리거(결과에 `side`) |
| `agent_iface.py` | `DecisionRequest.side`·`Decision.side`, `entry_verdict(d, last_close)` 방향별, `sell_verdict`(숏 = 환매), `effective_target` 방향별, MockAgent Short 경로 |
| `agent/pipeline.py` | `_short_context` — Sell = 공매도 진입, 가격 방향·업틱룰·대차수수료 명시(역할 파일 무수정) |
| `daily.py` | 4) 신규 Short(`_new_entries(..., "short")`), 판단 키 `decision/<date>/<code>/entry-short`·`review-short`, run 로그 `shortCandidates`·`shortCapSkipped` |
| `server.py`·`static/` | Short 탭 = Long 과 같은 표(보유·환매 검토·신규 후보→공매도 주문·체결 결과·후보 칸), 청산 사유 "환매" 라벨, 판단 원문 제목 "공매도 진입/Short 환매 검토" |
| 데모 | mock 백필이 Short 도 실제 흐름으로 생성 — 테스트 Short 주입(`seed_demo_short.py`) 삭제 |
| 테스트 | `tests/test_short.py` 18건(엔진 10·신호 2·판정 3·맥락 1·하루 흐름 2) |

## 남은 확인 (네트워크 PC — HANDOFF_P1 4절과 함께)
- **S4 실측**: 역할 프롬프트(09 Trader·13 PM)가 롱 관점이라 공매도 맥락에서 Trader 가 `Action: Sell` + Entry(≥ 전일 종가)·
  Stop(진입가 위)을 주는지 2~3종목 확인. 안 되면 `_short_context` 문구 보강.
- 호출량: 하루 최대 Long 10 + Short 10 종목 × 약 13콜 — Railway 크론 소요시간 실측 후 상한 조정.
- 데모 관찰: mock 기준 Short 주문의 상당수가 총노출 30% 상한으로 스킵 — 실제 운용에서 상한이 적절한지 확인.
