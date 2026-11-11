# 전종목 원본 데이터 DB 수집 계획 — DART 공시 · 일별 수급 · CA 이력 (2026-11-11 작성)

## 0. 왜
지금 DB(Railway Postgres `reports`/`snapshots`)에는 DART·수급·CA 가 **리포트 JSON 안에 가공된 형태로, 워치리스트·순매수 상위 종목만** 들어 있다.
워치리스트에 오른 적 없는 종목은 공시 이력·일별 수급 시계열이 없어 전종목 분석(로컬 LLM 등)의 입력이 "정보 부족"이 된다.
→ **전종목 원본을 행 단위 테이블에 매일 누적**한다. 리포트 파이프라인(LLM·대시보드)은 건드리지 않는 별도 수집 단계다.

## 1. 지금 있는 것(조사 결과)
| 항목 | 현황 |
|---|---|
| DB 쓰기 경로 | Actions → `push_reports_to_db.py` → `POST /api/ingest`(Bearer `INGEST_TOKEN`) → `report_db.py` 가 업서트. Postgres 는 Railway 내부망 전용이라 **외부에서 직접 접속 불가** — HTTP 경유가 유일한 길 |
| 수급 수집 선례 | `backfill_netbuy_history.py` 가 허브 `/flow` 를 **유니버스 ~520종목 × 1콜**(워커 6)로 이미 돌린다. `/flow` 한 콜 = 확정 수급 9주체(`daily`, ~20거래일, 백만원) + 공매도(`shorts`, 20일) + 대차(`loans`, 20일). 허브가 종목 10분 캐시 |
| 네이버 수급 | `sources.naver_investor_trend_full(code)` — 외국인·기관·개인 순매매량(주)·외국인 보유율·종가·등락률, 20일. 종목당 1콜 |
| DART | `sources.dart_market_disclosures(bgn, end, corp_cls)` — **corp_code 없이 시장 전체**를 날짜로 스캔(페이지당 100건). 종목별 350콜이 필요 없다. 하루 유가+코스닥 ≈ 10콜 안쪽 |
| CA | `fetch_ca_calendar.py` 가 허브 `/ca`(시장 전체, 오늘−14일~+45일) 1콜로 `public/ca_calendar.json` 을 만든다. 스냅샷이라 **매번 덮어써 과거가 사라진다** |
| 유니버스 | `public/assets/index_constituents.json` — KOSPI200(201) + KOSDAQ150(150) = 351종목, 매일 갱신 워크플로 있음 |
| 규칙 | 수집(외부 호출)은 PC 금지 → Actions/Railway. `.py`·워크플로만 바꾸는 커밋은 `[skip railway]` 가능하지만 **`railway_server.py` 를 바꾸면 재배포 필요** |

## 2. 설계 한 줄
**마감 후 하루 1회 Actions 워크플로**가 기존 수집 함수로 전종목 원본을 모아 **새 행 단위 ingest API**로 Railway Postgres 의 새 테이블 3개(+실행 기록 1개)에 업서트한다.
읽기는 같은 서버의 조회 API 로 연다(로컬 LLM·다른 서비스가 DB 크레덴셜 없이 읽게).

```
[Railway 스케줄러 18:10 KST 평일] ──dispatch──▶ [Actions: stock_data_collect.yml]
        collect_stock_data.py
          ├─ DART  : list.json 시장 전체(Y·K) 최근 3일     ≈ 10콜
          ├─ 수급  : 허브 /flow × 유니버스(워커 6)          ≈ 351~520콜 (허브가 KIS 4~5 TR)
          ├─ 네이버: trend × 유니버스                       ≈ 351~520콜
          └─ CA    : 허브 /ca 시장 전체                     1콜
                │  행 묶음(JSON, 3MB 배치)
                ▼
        POST /api/ingest/rows (Bearer INGEST_TOKEN) ──▶ stock_db.py ──▶ Postgres
                                                        GET /api/stockdata/… ◀── 로컬 LLM · 대시보드
```

### 왜 Actions + ingest API 인가(대안 비교)
| 안 | 장점 | 단점 | 판단 |
|---|---|---|---|
| **A. Actions 수집 → ingest API** | 기존 12개 워크플로와 같은 방식, 로그·재실행·타임아웃(6h) 확보, DB 크레덴셜이 Actions 에 없음 | `railway_server` 에 엔드포인트 추가·재배포 1회 | **권장** |
| B. Railway 웹 서버 안 스레드 | 배포 하나로 끝 | 10분짜리 수집이 대시보드 서버와 자원을 나눠 씀, 실패 로그가 묻힘 | 비권장 |
| C. Railway 크론 서비스 신설 | DB 직접 접속 | 서비스·변수 관리가 하나 더 늘고, Config as Code 종료(2026-12-01) 이슈와 겹침 | 보류 |

## 3. 테이블(새 모듈 `stock_db.py` — `report_db.py` 와 분리, 부팅 시 `CREATE TABLE IF NOT EXISTS`)
```sql
-- ① 공시 원본: 접수번호가 자연키. 시장 전체(유니버스 밖 종목 포함 — 비용이 같다)
CREATE TABLE stock_disclosures (
  rcept_no    text PRIMARY KEY,
  stock_code  text NOT NULL,           -- 6자리(비상장·코드 없는 건은 저장 안 함)
  filed_date  date NOT NULL,
  corp_name   text, title text NOT NULL, filer text,
  corp_cls    text,                    -- Y 유가 / K 코스닥
  rule_tag    text,                    -- sources.dart_rule_direction(title) 결과(bullish/bearish/…) — 코드 판정, LLM 아님
  collected_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON stock_disclosures (stock_code, filed_date DESC);

-- ② 일별 수급: (종목, 날짜) 한 행에 KIS 확정 대금 + 네이버 수량 + 공매도 + 대차
CREATE TABLE stock_flow_daily (
  code text NOT NULL, trade_date date NOT NULL,
  close numeric, change_pct numeric,
  -- KIS FHPTJ04160001 확정(백만원): 개인·외국인·기관 + 세분 6주체
  amt_prsn numeric, amt_frgn numeric, amt_orgn numeric,
  amt_fund numeric, amt_scrt numeric, amt_insu numeric, amt_ivtr numeric, amt_pe numeric, amt_etc numeric,
  -- 네이버 trend(주)·외국인 보유율(%)
  qty_prsn bigint, qty_frgn bigint, qty_orgn bigint, hold_ratio numeric,
  -- 공매도(주·억원·비중%)
  short_qty bigint, short_amt numeric, short_vol_pct numeric, short_amt_pct numeric,
  -- 대차(주·억원)
  loan_new bigint, loan_rdmp bigint, loan_bal_chg bigint, loan_bal_qty bigint, loan_bal_amt numeric,
  src text,                            -- 채워진 소스 표시 예: 'kis,naver,short,loan'
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (code, trade_date)
);
CREATE INDEX ON stock_flow_daily (trade_date);

-- ③ CA 이력: 스냅샷이 덮어쓰던 것을 이벤트 키로 누적 + 변경 추적
CREATE TABLE stock_ca_events (
  event_key   text PRIMARY KEY,        -- sha1(type|code|recordDate|kind|date) — 같은 일정은 같은 키
  code text NOT NULL, ca_type text NOT NULL, kind text, event_date date NOT NULL, record_date date,
  importance int, payload jsonb NOT NULL,          -- 행 원본(fields 포함)
  first_seen date NOT NULL, last_seen date NOT NULL -- last_seen 이 멈춘 행 = 일정이 취소·변경됨
);
CREATE INDEX ON stock_ca_events (code, event_date);

-- ④ 실행 기록: 무엇을 얼마나 넣었는지(모니터링·결손 탐지)
CREATE TABLE stock_collect_runs (
  run_date date NOT NULL, part text NOT NULL,      -- dart | flow | naver | ca
  universe int, ok int, failed int, rows int, note text,
  finished_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_date, part)
);
```
- **업서트 규칙**: 수급은 컬럼별 `COALESCE(EXCLUDED.x, 기존값)` — 한 소스가 그날 실패해도 다른 소스가 채운 값을 지우지 않는다. 공시는 `ON CONFLICT DO NOTHING`(정정 공시는 접수번호가 새로 나온다). CA 는 `last_seen` 만 갱신.
- **자가 복구**: 수급 소스가 매번 ~20거래일을 주므로 매일 그 창 전체를 다시 업서트한다 → 하루 이틀 수집이 빠져도 다음 실행이 메운다(20거래일 넘게 빠지면 KIS 쪽은 복구 불가).
- **용량**: 수급 351종목 × 250거래일 ≈ 9만 행/년, 공시 시장 전체 ≈ 하루 600~1,000건 → 20만 행/년, CA 수천 행. Postgres 에 부담 없는 크기.

## 4. API(`railway_server.py`)
| 메서드 | 경로 | 내용 |
|---|---|---|
| POST | `/api/ingest/rows` | `{"table": "flow"\|"disclosures"\|"ca"\|"runs", "rows": [...]}` — 테이블·컬럼 **화이트리스트**, 타입 검증, 배치 업서트. Bearer `INGEST_TOKEN`(기존 토큰 재사용) |
| GET | `/api/stockdata/flow?code=&from=&to=` | 종목 일별 수급 시계열 |
| GET | `/api/stockdata/disclosures?code=&from=&to=` | 종목 공시 이력 |
| GET | `/api/stockdata/ca?code=&from=&to=` | 종목 CA 이력(취소 추정 포함) |
| GET | `/api/stockdata/bundle?code=&days=120` | 위 셋을 한 번에(LLM 입력용) |
| GET | `/api/stockdata/status` | 테이블별 최신 날짜·행 수·종목 커버리지·최근 실행 기록 |
- 조회 API 는 읽기 전용 토큰(`STOCKDATA_READ_TOKEN`, 신규)으로 막는다 — 공개 대시보드 도메인에 원본 덤프를 열어 두지 않는다.

## 5. 수집 스크립트 `collect_stock_data.py`
- 인자: `--parts dart,flow,naver,ca`(기본 전부) · `--universe index|archive|all` · `--date` · `--dry-run`(전송 없이 건수만) · `--backfill-days N`.
- 유니버스 기본 = `index_constituents.json` 351종목 ∪ 워치리스트·netbuy 아카이브 등재 종목(기존 백필과 같은 ~520). DART·CA 는 유니버스와 무관하게 시장 전체.
- 수급: `ThreadPoolExecutor(6)` 로 허브 `/flow`(선례와 같은 부하), 네이버는 워커 4 + 호출 간 0.15초. 종목 실패는 건너뛰고 `failed` 로 집계 — 전체를 죽이지 않는다.
- 장 마감 가드: 휴장일이면 no-op(`krx_calendar`), 15:40 이전이면 KIS 확정이 비므로 거부(`--force` 로 무시).
- 표준 라이브러리 + `requests` 만(가벼운 워크플로 — pandas/yfinance 체인 회피). 수집 함수는 `analysis/sources.py` 에서 필요한 3개만 얇게 가져오거나 같은 호출을 재구현.
- 끝에 `stock_collect_runs` 기록 + 요약 출력. **유니버스의 80% 미만이 성공하면 exit 1**(워크플로 실패로 알림).

## 6. 워크플로 `stock_data_collect.yml`
- 트리거: `workflow_dispatch`(수동·백필 인자) + Railway 스케줄러가 **평일 18:10 KST** dispatch(`railway_server._scheduler` 에 한 줄 — 기존 방식).
  18:10 인 이유: KIS 확정(15:40+)·네이버 당일 반영 뒤이고, 16:00~16:30 마감 파이프라인·17:05 swing 정산과 허브 부하가 겹치지 않는다.
- 시크릿: `DART_API_KEY`·`REPORT_INGEST_TOKEN`(둘 다 이미 있음). `concurrency: stock-data`(중복 실행 방지). 커밋할 파일이 없으므로 git push 없음.
- 예상 시간 10분 안쪽(허브 520콜 ≈ 3~5분, 네이버 ≈ 2분, DART·CA 수 초).

## 7. 백필(1회)
| 데이터 | 가능한 깊이 | 방법 |
|---|---|---|
| 공시 | **원하는 만큼**(DART 는 과거 전체 조회 가능, corp_code 없는 조회는 한 번에 3개월 창) | 3개월씩 끊어 Y·K 반복. 1년 ≈ 20만 행, 수백 콜 — DART 일 한도 안 |
| 수급(KIS 대금·공매도·대차) | **~20거래일**(TR 한계) | 첫 실행이 곧 백필. 이미 있는 `netbuy_rank` 아카이브(64일, 상위 종목)를 테이블로 옮겨 일부 보강 |
| 수급(네이버 수량·보유율) | 페이지를 넘기면 더 과거까지 가능할 것으로 보이나 **실측 필요** | `pageSize`·`page` 로 60~120일 시도, 한도는 첫 실행 로그로 확정 |
| CA | 허브 `/ca` 에 `from`·`to` 를 넓혀 과거 조회 | 과거 1년 1회 조회(허브·KIS 응답 한도 실측 필요) |
- 더 긴 수급 이력(1년+)이 필요하면 KRX 정보데이터시스템뿐인데 클라우드 IP 가 막혀 있어 이번 범위에서 뺀다(별도 과제).

## 8. 단계와 완료 기준
| 단계 | 작업 | 끝났다고 보는 기준 |
|---|---|---|
| P1 | `stock_db.py`(스키마·업서트·조회) + `/api/ingest/rows`·`/api/stockdata/*` + 단위 테스트(화이트리스트·COALESCE 업서트·키 생성) | 테스트 통과, 배포 후 `/api/stockdata/status` 가 빈 테이블 4개를 보고 |
| P2 | `collect_stock_data.py` + 오프라인 테스트(허브·네이버·DART 응답 픽스처 → 행 변환) + 워크플로(수동) | `--dry-run` 이 Actions 에서 종목 수·행 수를 출력 |
| P3 | 첫 실제 실행 + 백필(공시 1년, 수급 20일, CA 1년, netbuy 아카이브 이관) | status 에서 수급 커버리지 ≥ 유니버스의 95%, 공시·CA 행 수가 0 이 아님, 표본 3종목을 대시보드 수급 탭 수치와 대조해 일치 |
| P4 | 스케줄러 18:10 dispatch + 실패 알림(워크플로 실패) + `docs/` 문서 | 평일 3일 연속 자동 실행 성공, 휴장일 no-op 확인 |
| P5 | 소비: 로컬 LLM 입력을 `/api/stockdata/bundle` 로 전환. (선택) 딥리서치가 공시·수급을 DB 에서 먼저 읽게 해 외부 호출 절감 | 전종목 분석에서 "정보 부족" 종목 비율이 눈에 띄게 줄어듦 |
- 예상 작업량: P1·P2 가 대부분(코드 400~500줄 + 테스트), P3 는 실행·검증 위주. 배포는 P1 에서 Railway 1회(`[skip railway]` 금지), 이후는 워크플로만.

## 9. 먼저 정해야 할 것(권장값 표시)
1. **수급 유니버스**: ① 지수 구성 351종목 ② **+워치리스트·아카이브 등재 종목 ≈ 520(권장 — 기존 백필과 동일, 부하 검증됨)** ③ 전 상장 ≈ 2,600(허브·KIS 콜이 5배 → 30분+, 허브 부하 실측 뒤 판단). 공시·CA 는 어느 쪽이든 시장 전체.
2. **실행 시각**: **18:10 권장**. 더 이르게(16:30) 하면 마감 파이프라인과 허브가 겹친다.
3. **공시 백필 깊이**: **1년 권장**(전종목 분석의 반복 유증·CB 이력 확인용). 3년도 가능(행 60만).
4. **조회 API 보호**: **읽기 토큰 권장**. 대시보드에서 직접 쓰려면 프록시 한 겹 추가.
5. **재무(분기 실적)** 를 같이 넣을지: 이번 범위는 DART 공시·수급·CA. 재무는 `dart_financials` 가 종목당 1콜·분기 1회라 **P6 로 분리 권장**.

## 10. 리스크
- **허브 부하**: `/flow` 는 KIS 4~5 TR 을 부른다. 520종목 × 5 = 2,600 TR — 선례(백필)에서 문제없었지만 장 마감 직후 다른 워크플로와 겹치지 않게 18:10 로 띄운다. 429·타임아웃은 재시도 2회 후 건너뜀.
- **네이버 차단**: Actions IP 에서 trend API 는 지금도 쓰고 있다(딥리서치). 호출이 20배로 늘므로 간격을 두고, 실패율이 높으면 KIS 대금만으로도 행은 남는다(COALESCE).
- **KIS 20거래일 한계**: 수집이 한 달 멈추면 그 구간 대금·공매도·대차는 영구 결손. `status` 의 최신 날짜를 주 1회 확인하거나 결손 시 워크플로 실패로 드러나게 한다.
- **CA 필드**: 허브 `/ca` 의 `fields.*` 는 KIS 문서 기준·실서비스 미검증이라 `payload` 를 통째로 jsonb 에 둔다(스키마 변경에 안전).
- **이 PC 규칙**: 수집·백필은 전부 Actions 에서 돈다. 로컬에서는 코드·오프라인 테스트만.

## 11. 구현 기록(2026-11-11 — P1·P2)
- `stock_db.py`(스키마·행 검증·업서트·조회) · `railway_server.py`(`POST /api/ingest/rows`, `GET /api/stockdata/{status,flow,disclosures,ca,bundle}`,
  부팅 시 스키마 생성, 스케줄러 평일 18:10 `stock_data_collect.yml` dispatch) · `collect_stock_data.py` · `.github/workflows/stock_data_collect.yml` ·
  `tests/test_stock_data_collect.py`(9건, 오프라인).
- 계획과 달라진 점:
  - `stock_flow_daily.src` 컬럼은 두지 않았다 — 어느 소스가 채웠는지는 컬럼의 NULL 여부로 알 수 있고, status 의 `filled{kis,naver,short,loan}` 가 건수를 보여 준다.
  - 공시의 규칙 태그는 `rule_dir`·`rule_kind` 두 컬럼. 서버가 적재 시 `analysis.sources.dart_rule_direction` 으로 채운다(수집 스크립트는 표준 라이브러리만).
  - `/api/stockdata/status` 는 공개(행 수·최신 날짜·커버리지만). 나머지 조회는 `STOCKDATA_READ_TOKEN`, 미설정이면 `INGEST_TOKEN` 으로 읽는다(둘 다 없으면 차단).
  - 허브 `/flow` 의 `rows` 는 30 을 기본으로(확정 대금 30거래일 재업서트 — 자가 복구 창이 넓어진다). 공매도·대차는 허브가 max(rows, 20).
  - 수급 파트는 거래일 15:40 KST 이후에만 돈다(`--force` 로 무시). 공시·CA 는 언제든.

## 12. 추가 기록(2026-11-11 오후)
- **유니버스**: 수급은 상장폐지·미상장을 뺀 648종목(krx_caps 기준).
- **CA 백필 실측**: 허브 시장 전체 `/ca` 는 총 120초·종류별 30콜 상한이라 1년을 한 번에 받을 수 없다 → 수집기가 36일 창·28일 간격(첫 창 −7일)으로 나눈다.
  2025-11-11 ~ 2026-11-11 = 14창, 7,407 이벤트 → 6,927행. 다만 **상장정보(listing)는 거의 매일 하루 100행 상한에 걸리고**, 창 뒤쪽은 시간 상한으로 빠졌다
  (배당 일부 구간·의무예치 조각도 시간 상한). 잘린 종류는 `--ca-types <종류> [--ca-window N]` 으로 그 종류만 다시 받는다(허브 120초를 그 종류가 다 쓴다).
  하루 100행 상한 자체는 KIS 한계라 못 넘는다.
- **소스 3종 추가**(KRX 로 받을 수 있는 수급 계열 — netbuy 아카이브 이관·네이버 수급 더 깊게 — 은 제외):
  | 파트 | 테이블(키) | 소스 | 매일 | 백필 |
  |---|---|---|---|---|
  | `financials` | `stock_financials`(code, bsns_year, reprt_code, fs_div) | DART 다중회사 주요계정 `fnlttMultiAcnt`(100개사/콜), 상장 전 종목 | 결산일이 최근 200일 안인 보고서(~55콜) | `--fin-years 2023,2024,2025` |
  | `holders` | `stock_major_holders`(rcept_no, reporter) | DART `majorstock`(종목당 1콜 = 5% 보고 전체 이력) | 최근 공시 제목에 '대량보유'가 뜬 종목만 | `--holders-all` |
  | `research` | `stock_research`(report_key = `naver:<id>`) | 네이버 리서치 종목 목록 + 상세(목표주가·의견·본문) | 유니버스 × 최근 3일 | `--research-days 365 --research-pages 20` |
  - 재무의 숫자 컬럼(매출·영업이익·세전이익·순이익·자산·부채·자본)은 당기 금액(원), 받은 계정 전체는 `payload`(jsonb) — 분기 보고서의 누적은 `thAdd`.
  - 리포트 업서트는 컬럼별 COALESCE — 상세 조회가 막혀 목록만 들어간 행도 다음 실행이 목표주가를 채운다. 상세가 40번 연달아 실패하면 상세만 그만둔다.
  - 조회: `/api/stockdata/financials|holders|research?code=`, `bundle` 에도 포함. `status` 에 세 테이블의 행 수·기간(재무는 보고서별 종목 수).
- **워크플로 줄(lane)**: `stock_data_collect.yml` 의 concurrency 그룹이 `stock-data-<lane>`. 같은 줄은 대기가 1건뿐이라(새 대기가 앞 대기를 취소)
  허브를 쓰지 않는 긴 백필(DART·네이버)은 `lane=backfill` 로 돌려 18:10 수집과 겹쳐도 서로 밀어내지 않게 한다. 허브를 쓰는 백필(CA)은 daily 줄에서.
- **백필 불가(지금부터 쌓을 수밖에 없는 것)**: 종토방·카페·뉴스 검색·Reddit/StockTwits·밸류에이션 스냅샷. KIS 대금·공매도·대차의 30거래일 이전은 KRX 뿐(클라우드 IP 차단).
