# swing — 섹터 시그널 스윙 모의투자 (`swing_paper` 브랜치 전용)

main 주간 브리핑의 **섹터 시그널 종목 관찰**(동반강세·수급유입) 종목을 매일 누적해
trading_agent 가 판단하고, 3영업일 보유 롱 모의투자를 한다. **main 에 병합하지 않는다.**

## 규칙 요약
| 항목 | 값 |
|---|---|
| 후보 | 그날 `sectorScreen.matrix` 동반강세 → 수급유입(보유·주문 대기 제외, 하루 ≤ `SWING_MAX_NEW_PER_DAY`=10) |
| 진입 조건 | PM Rating Buy/Overweight + Trader Action Buy + 진입가·손절가(손절 < 진입) |
| 가격 | 진입·손절 = Trader, 목표 = PM Price Target(없으면 손절·만기만) |
| 주문 | 다음 영업일 1일 유효 지정가. 저가 ≤ 진입가 → **진입가** 체결 |
| 보유 | 3영업일(체결일 = 1일째), 3일째 종가 청산 |
| 손절 | 저가 ≤ 손절가 → 손절가. 시가가 이미 손절가 아래면 **시가** |
| 목표 | 고가 ≥ 목표가 → 목표가. 같은 날 둘 다 → 손절 우선. 체결일엔 목표 미적용(보수적) |
| 조기 매도 | 보유 종목 섹터가 수급이탈/동반약세 전환 **또는** 종목이 그 칸에 등재 → trading_agent 재판단 → PM Sell/Underweight 면 다음 영업일 시가 |
| 자본·비용 | 5억, 매도세 0.2%, 슬리피지 없음, 비중 = Trader %(생략 5%, 상한 10%) |
| 현금 부족 | 체결일 시가에 평가손실 종목을 손실률 큰 순으로 청산(합쳐도 부족하면 청산 없이 스킵) |

## 구성
```
config.py        설정(env 노브)             engine.py      원장·체결·청산(순수 함수)
agent_iface.py   판단 요청/결과·파서·Mock   signals.py     주간 브리핑 → 후보·매도 트리거
daily.py         하루 처리                  run_daily.py   swing-cron 진입점(백필 포함)
store.py         File / Postgres(swing_docs) prices.py     일봉(Dict·Mock·yfinance)
server.py        swing-web(API + static/)   tools/extract_wb_history.py  과거 신호 복원
data/wb_history/ 9/9~ 일별 주간 브리핑 축약본(git 이력에서 추출)
```

## 로컬
```bash
python -m unittest discover -s swing/tests -t .
python -m swing.run_daily --from 2026-09-09 --to 2026-09-28 --wb-dir swing/data/wb_history \
    --agent mock --prices mock --store file:swing/data/local
python swing/server.py        # http://localhost:8124 (DATABASE_URL 없으면 swing/data/local)
```

## Railway 설정 (사용자 작업, 1회)
기존 대시보드 서비스는 main 브랜치라 이 브랜치 푸시에 영향받지 않는다. 같은 프로젝트에 서비스 2개를 추가한다.

1. **New Service → GitHub Repo `jykim0048/backtest_stock`** → Settings → Source → Branch = `swing_paper`
2. Settings → **Config-as-code 파일 경로**
   - `swing-web`: `swing/railway.web.json` (시작 `python swing/server.py`, 헬스체크 `/healthz`)
   - `swing-cron`: `swing/railway.cron.json` (시작 `python -m swing.run_daily`, 크론 `30 7 * * 1-5` = 평일 16:30 KST)
   - config 파일을 못 쓰면 같은 값을 Settings 의 Start Command·Cron Schedule 에 직접 입력.
     루트 `Procfile`(main 대시보드용)이 기본값으로 잡히지 않게 반드시 시작 명령을 지정.
3. **변수** (둘 다): `DATABASE_URL`(기존 Postgres 참조 변수 — 테이블 `swing_docs` 자동 생성)
   - `swing-cron` 추가: `GEMINI_API_KEY`, `DART_API_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`,
     `FLOW_API_BASE`(KIS 허브), 선택 `GH_RAW_TOKEN`(레포 private 전환 시), `LLM_CHAIN`
   - `swing-web` 선택: `SWING_PRICES_PROXY`(main 대시보드 URL — 장중 시세 중계)
   - 노브(선택): `SWING_MAX_NEW_PER_DAY`, `SWING_INITIAL_CAPITAL`, `SWING_SELL_TAX`, `SWING_AGENT`, `SWING_PRICES`
4. `swing-web` → Settings → Networking → **Generate Domain**

## 남은 작업
- P0: Railway 에서 수집 경로·일봉 소스(yfinance vs KIS 허브) 실측
- P1: trading_agent 역할 프롬프트 헤드리스 이식(`--agent trading_agent`) — 절차는 [HANDOFF_P1.md](HANDOFF_P1.md)
- P5: `--from 2026-09-09 --to 2026-09-28 --wb-dir swing/data/wb_history` 소급 검증(뉴스·여론은 현재값 — 룩어헤드 명시)
