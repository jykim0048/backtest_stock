# 신규 후보 단계만 재실행(`--new-only`) — 구현 계획 (2026-11-09)

> 배경: 11/09 17:05 정산이 주간 브리핑 날짜 게이트(`wb_ok`)에 걸려 매트릭스·유니버스 신규 후보 판단을 통째로 생략
> (후보 0, 하방 촉매 Short 1건만). 브리핑은 21:33 KST 에 11/09 자로 복구됐지만, 하루 처리(`daily.run_day`)는 원장에
> "처리됨"이 찍혀 있고 `--redo` 는 "판단 실패" 행만 다시 보기 때문에 신규 후보만 골라 돌릴 입구가 없다.

## 0. 목표·범위
| 항목 | 내용 |
|---|---|
| 목표 | 그날 **생략된 신규 후보 판단·주문만** 다시 돌린다. 청산·재판별·매도 검토·재시도·비중 채우기·복기는 손대지 않는다. |
| 입구 | `python -m swing.run_daily --new-only [--dry-run] [--date YYYY-MM-DD]` |
| 소스 | 매트릭스(브리핑 4칸) + 유니버스 독립 후보. **하방 촉매 Short 은 제외**(그날 정규 런에서 이미 판단·주문됨 — 11/09 서진시스템). |
| 주문 | 정규 런과 동일: `daily._new_entries` → 다음 영업일(`run.entryDate`) 1일 유효 지정가. |
| 비범위 | 매도 검토 신호 트리거(2)①) 재실행 — 보유 종목 PM 재판별은 이미 당일 돌았고, 추가 매도 예약은 중복 위험이 커서 이번엔 안 함. |

## 1. 실행 가드(하나라도 걸리면 아무것도 쓰지 않고 `skip` 반환)
1. `run/<date>` 가 있고 그 notes 에 `주간 브리핑 asof … ≠ <date> — 신규·신호 매도 검토 생략` 이 있을 것(= 정말 생략된 날만).
2. `run.candidates` 와 `run.shortCandidates` 가 모두 비어 있을 것(이미 신규 판단이 있으면 거부 → 두 번 돌려도 중복 주문 없음).
3. 원장 `asof == date`(다음 영업일 런이 돌았으면 소급 금지 — redo 와 같은 규칙).
4. 새로 받은 브리핑 `signals.asof_date(wb) == date`(여전히 스테일이면 거부).
5. `progress.status != "running"`(정산·재분석이 돌고 있으면 거부).
6. 장중(평일 09:00~15:30 KST) 실행 거부 — Live Trader 가 원장을 같이 쓰는 시간대.

## 2. 설계
### 2-1. 후보 수집 로직 공용화 — `daily.py` 리팩터(권장) 
`run_day` 의 후보 수집 두 토막을 함수로 뽑아 run_day 와 new-only 가 **같은 코드**를 쓴다(중복 구현 금지):
- `collect_matrix_candidates(L, store, wb, date, uni, run)` → (cands, scands) : 85~109행(보유·대기 제외, 매트릭스 쿨다운, Short 선물 한정 필터·노트).
- `collect_universe_candidates(L, store, wb, date, uni, uni_doc, cands, scands, run, bars, prices)` → (ucands, uscands, skipped_uni_cap, skipped_uni_short_cap) : 191~224행(공통 채점, 쿨다운, 상한 이연, 추가 시세).
- `run_day` 본문은 이 두 함수를 호출하도록만 바꾼다. 동작 동일성은 기존 `swing/tests`(391개) 로 확인.
- 대안(리팩터 없이 new-only 안에 복제): 운영 경로를 안 건드리지만 규칙이 둘로 갈라진다 — 비권장. **사용자 결정 1.**

### 2-2. 새 모듈 `swing/newonly.py`
```
run_new_only(date, store, agent, prices, wb, *, uni=None, dry_run=False, log=print) -> dict
```
1. 가드(1장) 검사 → 걸리면 `{"skip": 사유}`.
2. `L = store.get("ledger")`, `run = store.get(f"run/{date}")`, `nxt = run["entryDate"]`.
3. `prog = daily.Progress(store, date, agent)` 에 `phase="new-only"` — 대시보드 헤더에 "실행 중 n/N" 표시. 끝나면 `status="done"`.
4. 후보: `collect_matrix_candidates` → 매트릭스 Long/Short. `uni.build_or_load(date, wb, L)` 는 **이미 21:4x 에 재산출된 universe/<date> 를 읽기만**(force=False) → `collect_universe_candidates`.
   - 추가 제외: 정규 런 뒤 생긴 주문·포지션(재시도 o757·o758, 비중 채우기, 촉매 Short) = 현재 `L.positions`·`L.orders` 기준이므로 자동 반영.
   - 재시도로 재주문된 종목(`run.retries[ok]`)도 pend 에 들어 있어 자동 제외.
5. 상한: `MAX_NEW_PER_DAY`/`MAX_NEW_SHORT_PER_DAY`(매트릭스), 유니버스 상한은 2-1 함수 안. 총량 `budget = max(ENTRY_JUDGE_MIN, JUDGE_PER_DAY − len(run.reviews))` → `daily.judge_budget(budget, [cands, ucands], [scands, uscands])`(촉매 목록은 빈 리스트). 11/09 는 재판별 14 → 신규 28건.
6. `--dry-run`: 여기까지만. 후보 표(코드·이름·소스·신호·점수·순위·제외 사유)와 총량 배분을 stdout 에 출력하고 **store 에 아무것도 쓰지 않는다**(progress 도 안 씀).
7. 실행: `bars = prices.daily_bars(codes, date)` → `daily._new_entries(...)` Long(매트릭스→유니버스) → Short(매트릭스→유니버스, `fut=uni.fut_set(date)`).
8. 저장(한 번에): `run["candidates"]`, `run["shortCandidates"]`, `capSkipped`/`uniCapSkipped`/`shortCapSkipped`/`uniShortCapSkipped`, `run["judgeBudget"]`(있으면), `run["uniCaps"]`,
   `run["newOnly"] = {"at", "prevNotes": [생략 노트], "long": n, "short": n, "ordered": n}`, notes 에 `신규 후보 보충 판단(HH:MM) Long a/b 주문 · Short c/d 주문` 추가(생략 노트는 이력으로 남김).
   `store.put(f"signals/{date}", signals.compact(wb))`, `store.put("ledger", L)`, `store.put(f"run/{date}", run)`.
9. 판단 실패 행은 그대로 남긴다 → 이후 `--redo` 가 기존 규칙대로 재분석(redo 가 `run.candidates` 를 읽으므로 자동 호환).
10. 반환 요약 `{date, at, long, short, ordered, failed, skippedCap…}`.

### 2-3. `run_daily.py`
- `--new-only`, `--dry-run` 인자. `a.new_only` 면 `wb = load_wb(d)` → `newonly.run_new_only(d, st, ag, px, wb, uni=_runner(st, a), dry_run=a.dry_run)` 출력 후 `continue`(하루 처리·자동 redo·복기 호출 안 함).
- `_runner` 의 `force = a.force or a.universe_only` 는 그대로(new-only 는 force 아님 → 기존 유니버스 문서 재사용).

### 2-4. 대시보드·API
- 변경 없음. `/api/swing/runs?date=` 가 채워진 `candidates` 를 그대로 보여 주고, 신규 후보 표도 같은 필드를 읽는다. 노트에 보충 판단 시각이 보인다.
- (후속, 선택) `/admin/new-only?token=&date=&dry=1` — swing-web 에는 LLM 키·FLOW_API_BASE 가 없어(변수: DATABASE_URL·REDIS_URL·SWING_ADMIN_TOKEN·SWING_PRICES_PROXY 뿐) 지금은 못 돈다. 변수 참조(`${{swing-cron.GEMINI_API_KEY}}` 등)를 붙인 뒤에만 가능. 이번 범위 밖.

## 3. 테스트 `swing/tests/test_newonly.py` (오프라인, test_daily 의 FixedAgent·wb·b 재사용)
1. 스테일 브리핑으로 run_day → 후보 0·생략 노트 → 당일 브리핑으로 `run_new_only` → candidates 생성, 주문 validDate = 다음 영업일, 노트 추가, 원장 저장.
2. 다시 `run_new_only` → `skip`(이미 신규 판단 있음) — 주문 수 불변.
3. 원장 asof 가 다음 날이면 `skip`.
4. 생략 노트가 없는 날(정상 런) 이면 `skip`.
5. `dry_run=True` → store 의 run·ledger·progress 바이트 동일.
6. 보유·대기·쿨다운·재시도 종목 제외, 총량 배분(judge_budget) 적용.
7. 리팩터 회귀: 기존 391개 전부 통과(`.venv\Scripts\python -m unittest discover -s swing/tests -t .`).

## 4. 실행 경로(Railway 환경 필요 — Postgres·LLM 키·FLOW_API_BASE)
| 경로 | 방법 | 장단 |
|---|---|---|
| **A. `railway run`(이번 추천)** | 이 PC 에서 `railway run -s swing-cron -- .venv\Scripts\python -m swing.run_daily --new-only --dry-run` → 검토 → `--dry-run` 빼고 1회 | 코드 푸시·배포 없이 바로. swing-cron 변수가 **프로세스 환경에만** 주입되고 파일에 남지 않음. 단 PLAN_STOCKDATA 의 "로컬에 DATABASE_URL 을 두지 않는다" 취지와 맞는지 **사용자 결정 2**. 수집(yfinance·네이버·DART·KIS 허브)은 이 PC 네트워크로 나간다. |
| B. swing-web admin 엔드포인트 | 2-4 후속 + 변수 참조 추가 후 URL 호출 | 재사용성 좋음. 오늘은 준비 시간이 더 든다. |
| C. 하루 처리 전체 `--force` | — | 청산·매도 예약·재시도 중복 위험. **하지 않는다.** |

## 5. 오늘 밤 절차(진행 승인 시)
1. 2-1 리팩터 + 2-2 모듈 + 2-3 인자 + 3 테스트 → 전체 테스트 통과. (약 60~80분)
2. 커밋 `swing(new-only): …` → `swing_paper` 푸시(지금은 허용 시간대, `.py` 변경이라 swing 3개 서비스 재배포 — GitHub 장애로 자동배포가 늦으면 `railway up`). 실행 자체는 A 경로라 배포를 기다리지 않아도 된다.
3. `railway run … --new-only --dry-run` → 후보 표·총량 확인(보유·대기 제외가 맞는지, 11/09 유니버스 재산출본 기준인지).
4. `railway run … --new-only` → 판단 28건 안팎(LLM, 오늘 15건 ≈ 50분 기준 **약 1.5시간**) → 끝나면 `/api/swing/runs?date=2026-11-09` 의 candidates·notes, 대시보드 신규 후보 표, 원장 주문 validDate=2026-11-10 확인.
5. 판단 실패가 있으면 `--redo` 1회(기존 기능).
6. 11/10 09:00 Live Trader 가 새 주문을 집행 — 08:30 전에 끝나 있어야 한다.

## 6. 리스크·메모
- 리팩터가 run_day 동작을 바꾸지 않는지는 기존 테스트가 보증. 함수 추출만, 로직 변경 없음.
- 유니버스 재산출(21:4x)은 wbAsof 11/09 기준 — new-only 는 그 문서를 그대로 쓴다(재산출 안 함).
- 이 모드는 "생략된 날 보충" 전용이다. 정상 런이 있던 날은 가드 1·2 에서 거부된다.
- 회사 PC 에서는 하지 않는다(개인 PC 전용). 토큰·키는 채팅·파일에 적지 않는다.
