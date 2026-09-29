# swing — 섹터 시그널 스윙 모의투자 (`swing_paper` 브랜치 전용)

main 주간 브리핑의 **섹터 시그널 종목 관찰**(동반강세·수급유입) 종목을 매일 누적해
trading_agent 가 판단하고, 5영업일 보유 롱 모의투자를 한다(`SWING_HOLD_DAYS`). 2026-09-29 부터 동반약세·수급이탈
종목의 **Short(공매도)** 도 대칭 규칙으로 운용한다 — 규칙 R1~R13 은 [PLAN_SHORT.md](PLAN_SHORT.md). **main 에 병합하지 않는다.**

## 규칙 요약
| 항목 | 값 |
|---|---|
| 후보 | 그날 `sectorScreen.matrix` 동반강세 → 수급유입(보유·주문 대기 제외, 하루 ≤ `SWING_MAX_NEW_PER_DAY`=10) |
| 진입 조건 | PM Rating Buy/Overweight + Trader Action Buy + 진입가·손절가(손절 < 진입) |
| 가격 | 진입·손절 = Trader, 목표 = PM Price Target(없으면 손절·만기만) |
| 주문 | 다음 영업일 1일 유효 지정가. 저가 ≤ 진입가 → **진입가** 체결 |
| 보유 | 5영업일(체결일 = 1일째, `SWING_HOLD_DAYS`) → 만기일 장 마감 후 **trading_agent 재판별**: Sell/Underweight 면 다음 영업일 시가 매도, 아니면 연장(이후 매일 재판별), 최대 15영업일(`SWING_MAX_HOLD_DAYS`) 도달 시 판별 없이 다음 날 시가 매도 |
| 손절 | 저가 ≤ 손절가 → 손절가. 시가가 이미 손절가 아래면 **시가** |
| 목표 | 고가 ≥ 목표가 → 즉시 청산하지 않고 **재판별**: Sell 이면 다음 날 시가 매도, 보유면 새 목표가(PM)·손절가 = max(Trader 새 손절, 기존, 매수가)(본전 손절). 같은 날 손절도 닿으면 손절 우선. 체결일엔 목표 미적용. 재판별 실패 시 원 규칙대로 다음 날 시가 매도 |
| 조기 매도 | 보유 종목 섹터가 수급이탈/동반약세 전환 **또는** 종목이 그 칸에 등재 → trading_agent 재판단 → PM Sell/Underweight 면 다음 영업일 시가 |
| 자본·비용 | 5억, 매도세 0.2%, 슬리피지 없음, 비중 = Trader %(생략 5%, 상한 10%) |
| 현금 부족 | 체결일 시가에 평가손실 종목을 손실률 큰 순으로 청산(합쳐도 부족하면 청산 없이 스킵) |
| **Short** | 동반약세 → 수급이탈(하루 ≤ `SWING_MAX_NEW_SHORT_PER_DAY`=10), PM Sell/Underweight + Trader Sell, 고가 ≥ 진입가 체결(진입가 ≥ 전일 종가), 손절 진입가 위·목표 아래, 환매 = PM Buy/Overweight, 담보 100%·매도세 0.2%·대차 연 4%·총노출 ≤ 30% — [PLAN_SHORT.md](PLAN_SHORT.md) |

## 구성
```
config.py        설정(env 노브)             engine.py      원장·체결·청산(순수 함수)
agent_iface.py   판단 요청/결과·파서·Mock   signals.py     주간 브리핑 → 후보·매도 트리거
agent/           trading_agent 헤드리스(kit=원본 복사, peers.py, pipeline.py)
daily.py         하루 처리                  run_daily.py   swing-cron 진입점(백필 포함)
store.py         File / Postgres(swing_docs) prices.py     일봉(Dict·Mock·yfinance·DbHub=DB증권 CHARTDAY)
server.py        swing-web(API + static/)   tools/extract_wb_history.py  과거 신호 복원
data/wb_history/ 9/9~ 일별 주간 브리핑 축약본(git 이력에서 추출)
```

## 브랜치 구성 (2026-09-29 정리)
- 이 브랜치에는 `swing/` 와 의존 파일만 있다: `llm.py`(P1 LLM 호출), `krx_calendar.py`·`data/krx_holidays.json`
  (휴장일), `public/assets/krx_companies.json`(일봉 시장 구분), `requirements.txt`, `.python-version`.
- main 의 대시보드·파이프라인·워크플로·리포트는 삭제됨 → **`git merge main` 금지**(삭제 파일마다 충돌).
  main 쪽 개선을 가져올 땐 남긴 파일만: `git checkout origin/main -- llm.py krx_calendar.py data/krx_holidays.json`.
- 주간 브리핑 입력은 병합이 아니라 main raw 에서 매일 읽는다(`SWING_MAIN_RAW_BASE`). 과거 신호 복원 도구는
  같은 레포의 main 이력을 읽으므로 `git fetch origin main` 후 `--ref origin/main` 으로 실행.

## 로컬
**대시보드 확인(데모)** — 클론 직후 명령 하나. 데모 데이터(swing/data/demo, git 제외)가 없으면 자동 생성
(mock 에이전트·mock 시세로 9/9~9/28 Long·Short 백필)한 뒤 http://localhost:8124 로 서빙.
표준 라이브러리만 쓰고 네트워크·API 키·패키지 설치 불필요, DATABASE_URL 이 있어도 무시.
```bash
python swing/server.py --demo            # 없으면 생성 후 서빙
python swing/server.py --rebuild-demo    # 코드 변경 후 데모 다시 만들기
```
그 밖:
```bash
python -m unittest discover -s swing/tests -t .
python -m swing.run_daily --from 2026-09-09 --to 2026-09-28 --wb-dir swing/data/wb_history \
    --agent mock --prices mock --store file:swing/data/local
python swing/server.py        # 옵션 없음: DATABASE_URL 있으면 Postgres, 없으면 swing/data/local
```

## Railway 설정 (사용자 작업, 1회)
**배치 = `trading_bot_DB_HUB` 프로젝트(DB·KIS 하이브리드, 2026-09-29 결정, 리전 전부 EU West).**
일봉은 같은 프로젝트 DB 허브의 Redis 토큰으로 DB증권 CHARTDAY(내부망), 수급·공매도·대차는 다른 프로젝트의
KIS 허브(`FLOW_API_BASE` 공개 URL), 신호는 backtest_stock main 주간 브리핑(GitHub raw). main 대시보드 서비스는
main 브랜치라 이 브랜치 푸시에 영향받지 않는다. **Postgres 를 새로 추가**하고(이 프로젝트엔 없음) 서비스 2개를
같은 레포·브랜치로 만든다(새 레포 불필요 — 웹은 상시, 크론은 실행 후 종료라 서비스만 분리). 리전은 **eu-west**
하나(Hobby 단일 리전 — CLI 는 `eu-west=1 southeast-asia=0` 처럼 나머지를 0 으로).

1. **New Service → GitHub Repo `jykim0048/backtest_stock`** → Settings → Source → Branch = `swing_paper`
2. Settings → **Config-as-code 파일 경로**
   - `swing-web`: `swing/railway.web.json` (시작 `python swing/server.py`, 헬스체크 `/healthz`)
   - `swing-cron`: `swing/railway.cron.json` (시작 `python -m swing.run_daily`, 크론 `30 7 * * 1-5` = 평일 16:30 KST)
   - config 파일을 못 쓰면 같은 값을 Settings 의 Start Command·Cron Schedule 에 직접 입력.
     루트 `Procfile`(main 대시보드용)이 기본값으로 잡히지 않게 반드시 시작 명령을 지정.
3. **변수** (둘 다): `DATABASE_URL`=`${{Postgres.DATABASE_URL}}`(테이블 `swing_docs` 자동 생성)
   - `swing-cron` 추가: `SWING_PRICES=dbhub`, `REDIS_URL`=`${{Redis.REDIS_URL}}`(DB 허브 Redis — 토큰 읽기 전용,
     다른 Redis 를 쓰게 되면 `DBHUB_REDIS_URL` 로 따로 지정), `GEMINI_API_KEY`, `DART_API_KEY`, `NAVER_CLIENT_ID`,
     `NAVER_CLIENT_SECRET`, `FLOW_API_BASE`(KIS 허브 vi_limit 서비스 URL), 선택 `GH_RAW_TOKEN`(레포 private 전환 시),
     `LLM_CHAIN`, `DB_REST_BASE`(기본 https://openapi.dbsec.co.kr:8443)
   - `swing-web` 선택: `SWING_PRICES_PROXY`(main 대시보드 URL — 장중 시세 중계)
   - 노브(선택): `SWING_MAX_NEW_PER_DAY`, `SWING_INITIAL_CAPITAL`, `SWING_SELL_TAX`, `SWING_AGENT`, `SWING_PRICES`
4. `swing-web` → Settings → Networking → **Generate Domain**

## 남은 작업
- P0: 일봉 소스 = **DB증권 CHARTDAY(`SWING_PRICES=dbhub`) 확정**(2026-09-29 실측: 5종목×2일 네이버와 전부 일치, yfinance 는 종가·고저 오차와 코스닥 누락). 남은 것은 Railway 에서 수집 경로(trading_agent collect) 실측
- P1 실동작 검증(개인 PC, 네트워크 필요) — 구현은 완료(`swing/agent/`, 출처 [SOURCE.md](agent/SOURCE.md)), 절차는 [HANDOFF_P1.md](HANDOFF_P1.md) 4절
- Short S4: 실제 trading_agent 로 공매도 판단 실측(Trader Sell·가격 방향) — [PLAN_SHORT.md](PLAN_SHORT.md) 남은 확인
- P5: `--from 2026-09-09 --to 2026-09-28 --wb-dir swing/data/wb_history` 소급 검증(뉴스·여론은 현재값 — 룩어헤드 명시)
