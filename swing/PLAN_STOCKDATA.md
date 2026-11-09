# 종목 판단 입력 원자료 DB 보존 + 로컬 아카이브 — 구현 계획·실행 안내 (2026-11-09)

> 새 개인 PC 에서 진행한다. 먼저 `NEW_PC_SETUP.md` 를 끝내고(레포·메모리 복원, 19:37 swing 푸시 확인) 이 문서로 시작.
> 대상 레포: `backtest_stock\backtest_stock-swing` (브랜치 `swing_paper`, main 과 병합 금지).

## 0. 확정된 결정 (2026-11-09 사용자)
| 항목 | 결정 |
|---|---|
| 범위 | 1단계(DB 보존 + DB 사본 판단 + 같은 날 재사용) **+ 로컬 매일 내려받기**를 한 번에 |
| DB 저장 실패 시 | **폴백** — 임시 폴더로 판단 계속, `inputs.saved=false` 기록·경고 (엄격 모드는 env 로 선택만 가능) |
| 클라우드 보존 | **최대 1년(365일)** — 단, 로컬이 받았다고 확인(ack)한 날짜만 삭제. (2026-11-09 개정 — Railway 엔 투자판단에 필요한 자료만, 뉴스·공시·CA 등은 최대 1년) |
| 로컬 보존 | 영구 (개인 PC sqlite + 주 1회 백업) |
| 판단 입력 | **DB 사본만 읽는다** — 수집본을 DB 에 저장한 뒤 DB 에서 복원한 폴더로 판단 |

## 1. 배경 — 지금 수집 방식 (2026-11-09 코드 확인)
판단 1건(`swing/agent/pipeline.py` `TradingAgent.decide`)마다 swing-cron(평일 17:05 KST) 프로세스 안에서:
1. `resolve.py` 종목 해석 → `peers.py` peer 확정(LLM 1콜, `peers/dynamic` 캐시)
2. `swing/agent/kit/scripts/collect.py`(homework trading_agent 스킬 **무수정 사본**)가 16개 레그 병렬 수집 → 파일 저장

| 파일 | 소스 | 읽는 역할 |
|---|---|---|
| `01_price.json` + `ohlcv.csv` | yfinance 일봉 420일 + 지표 13종 | 01 Market |
| `02_fundamentals.json` | 네이버·FnGuide 밸류에이션, OpenDART 분기재무 5기·대량보유, 리포트 컨센서스 | 04 Fundamentals |
| `03_news_community.json` | 네이버 뉴스·카페·종토방, 리포트 60일, 해외 peer 뉴스·시세, StockTwits, Reddit | 02 Sentiment · 03 News |
| `04_flow.json` | KIS 허브 `/flow`(투자자·공매도·대차 30행), 네이버 매매동향 20행 | 05 Flow |
| `05_disclosures.json` | DART 공시 120일 + **예탁원 CA**(`swing/agent/ca.py`, 수집 **뒤** 덧붙임) | 03 News |
| `manifest.json` | 레그별 status·소요초·오류 | 판단 문서 `reports.collect` 에 status 만 |

3. 저장 위치 = OS 임시폴더 `swing_runs/<code>/<date>/<purpose>/` — 크론 컨테이너 휘발성 디스크, 끝나면 사라짐.
   DB(`swing_docs`)엔 LLM 리포트·차트·수집 상태 요약만. **원자료 없음.**
- 같은 날 같은 종목이 purpose(entry/review/…-short) 가 다르면 두 번 수집.
- 수집 뒤 파일을 고치는 곳은 CA 덧붙임 하나. ohlcv 최근 60행·15만 자 자르기는 프롬프트 만들 때 읽는 쪽에서만(파일 불변).

## 2. 설계

### 2-1. 테이블 (swing-cron·swing-web 이 같은 `DATABASE_URL` 공유)
```sql
CREATE TABLE IF NOT EXISTS swing_stock_inputs (
  date         TEXT NOT NULL,     -- 판단 기준일 YYYY-MM-DD
  code         TEXT NOT NULL,     -- 6자리
  file         TEXT NOT NULL,     -- 01_price.json | ohlcv.csv | 02_fundamentals.json | 03_news_community.json
                                  -- | 04_flow.json | 05_disclosures.json | manifest.json
  body         BYTEA NOT NULL,    -- 원문 gzip (sqlite 는 BLOB)
  bytes        INTEGER NOT NULL,  -- 압축 전 크기
  sha256       TEXT NOT NULL,     -- 압축 전 원문 해시
  collected_at TEXT NOT NULL,     -- UTC ISO
  PRIMARY KEY (date, code, file)
);
```
- 완결 표시 문서(`swing_docs`): `inputs/<date>/<code>` =
  `{files: {이름: sha256}, bytes: {이름: n}, legs: {레그: status}, collectedAt, usedBy: [purpose…]}` — **파일 행을 다 쓴 뒤 마지막에** 기록.
  이 문서가 없으면(중간 실패) 그 묶음은 없는 것으로 보고 다시 수집.
- ack 문서: `inputs/ack/<date>` = `{ackAt, files: n, sha: <그날 전체 해시>}` — 로컬이 검증 끝낸 날짜.
- purpose 는 키에 없음 → entry·review 가 같은 묶음 공유.

### 2-2. 신규 `swing/stockdata.py`
| 함수 | 역할 |
|---|---|
| `has_bundle(store, date, code) -> dict\|None` | 완결 문서 + price 레그 ok 면 문서 반환 |
| `save_bundle(store, date, code, src_dir) -> dict` | 7개 파일 gzip·sha → 행 upsert → 완결 문서 기록. 없는 파일은 건너뛰고 문서에 없음으로 |
| `load_bundle(store, date, code, dst_dir) -> dict` | 행 → 압축 해제 → sha 검증(불일치 = 예외) → `dst_dir` 에 쓰기 → 읽기 전용 속성 |
| `day_index(store, frm, to)` | 날짜별 `{code: {file: sha}}` (로컬 동기화 비교용) |
| `export_day(store, date)` | 그날 전 행(body 는 base64) + 완결 문서 |
| `ack(store, date, sha)` / `prune(store, today, keep_days=365)` | ack 기록 / ack 된 날짜 중 365일 지난 것만 행·문서 삭제, 삭제 수 반환 |

### 2-3. `swing/store.py`
- `SqlStore`: 위 테이블 생성 추가 + `put_blob/get_blobs/del_blobs/blob_index` (Postgres `%s`+`psycopg2.Binary`, sqlite `?`+bytes).
- `FileStore`(로컬·테스트): `inputs/<date>/<code>/<file>.gz` + 같은 메서드.

### 2-4. `swing/agent/pipeline.py` `decide` (peer 확정 다음, 약 30줄)
```
① has_bundle(date, code)? ─ 예 → ④ (reused=true)
② collect_fn(tmp/) → ca_fn(tmp/)                 # 지금과 동일
③ save_bundle(tmp/)                               # 실패 → 폴백(아래)
④ load_bundle → use/ (새 폴더)                    # sha 불일치 → ②부터 1회 재시도 → 그래도 실패면 폴백
⑤ 애널리스트~PM·차트는 use/ 만 읽는다
```
- 폴백: tmp/ 로 판단 계속, 판단 문서 `inputs={key, saved:false, error}` + run 로그 경고. `SWING_INPUTS_STRICT=1` 이면 대신 RoleError.
- 정상: 판단 문서 `inputs={key:"inputs/<date>/<code>", sha:{…}, reused:bool, saved:true}`.
- 가격 수집 실패는 지금처럼 판단 안 함. price 실패 묶음은 저장은 하되 재사용 안 함.
- **kit·역할 파일·Live Trader(swing/vwap) 무변경.**

### 2-5. 계측 (run 로그)
- 종목별 `inputBytes`(파일별 원문)·`inputGz`·`collectS`·`reused`·`saved`. 1주 뒤 하루 저장량·판단 1건 입력 규모 확인.

### 2-6. swing-web API (`swing/server.py`, GET 전용 서버 — 기존 `/admin/*` 패턴)
| 경로 | 내용 |
|---|---|
| `GET /admin/inputs/index?from=&to=` | `day_index` + 날짜별 ack 여부 |
| `GET /admin/inputs/day?date=` | `export_day` (JSON, body base64-gzip) |
| `GET /admin/inputs/ack?date=&sha=` | 서버가 그날 전체 해시 재계산 → 일치할 때만 ack 기록 |
- 인증 `SWING_ADMIN_TOKEN`(swing-web 에 **이미 설정됨** — 11/9 `/api/swing/config` `adminSimConfigured: true` 확인).
  새 경로는 쿼리 `token=` 대신 **헤더 `X-Admin-Token`** 으로 받기(로그에 토큰 안 남게). 종토방 닉네임 등 원문이라 반드시 보호.

### 2-7. 클라우드 정리 (swing-cron)
- `run_daily` 끝(복기 뒤)에 `stockdata.prune(store, today, 365)` — ack 된 날짜 중 365일 지난 것만 삭제, run 로그에 `inputsPruned`.
- 저장 총량이 2GB 를 넘으면 run 로그 경고(대시보드 표시는 선택). 1년치 용량은 1단계 1주 실측(`inputGz`)으로 다시 추정.

### 2-8. 로컬 동기화 `swing/inputs_sync.py` (`python -m swing.inputs_sync`)
```
--archive  D:\swing_archive\inputs.sqlite   (로컬 sqlite, 2-1 과 같은 스키마 + swing_docs)
--base     https://swing-web-production.up.railway.app
--from     (기본: 아카이브 마지막 날짜 다음날, 첫 실행이면 서버에 있는 가장 오래된 날짜부터)
--materialize <date> <code> <dir>   (선택: 아카이브 → 폴더 복원, 로컬 Claude 분석·리플레이용)
```
1. `/admin/inputs/index` 로 서버 목록과 로컬 비교 → 없는 날짜·sha 다른 파일만 대상
2. `/admin/inputs/day` 받기 → 압축 해제·sha 검증 → 로컬 sqlite upsert(트랜잭션)
3. 그날 전체 해시 계산 → `/admin/inputs/ack` → 서버 일치 응답 확인
- 재실행 안전(증분). PC 가 며칠 꺼져 있어도 켜지면 밀린 날짜를 한 번에.
- 토큰은 환경변수 `SWING_ADMIN_TOKEN` 에서만 읽는다(코드·채팅·파일에 값 금지).
- 의존성: 표준 라이브러리만(urllib·sqlite3·gzip·hashlib) — 로컬에 패키지 설치 불필요.

## 3. 테스트 (`swing/tests/test_stockdata.py`, `python -m unittest discover -s swing/tests -t .`)
1. save→load 바이트 동일(7개 파일, 한글 UTF-8)
2. 같은 날 같은 종목 두 번째 판단(review) 은 collect_fn 미호출·`reused=true`
3. DB 사본만 읽음 — ③ 뒤 tmp/ 삭제해도 판단 성공
4. 저장 실패(가짜 store 예외) → 폴백 판단 성공·`saved=false` / STRICT=1 이면 RoleError
5. sha 불일치 → 재수집 1회
6. 완결 문서 없는 묶음 → 재수집
7. price 실패 묶음 재사용 안 함
8. prune: ack 없는 날짜는 365일 지나도 남음, ack 된 날짜만 삭제
9. inputs_sync: 가짜 서버(FileStore 백엔드)로 증분·sha 검증·ack 흐름, 두 번 실행해도 중복 없음
10. 기존 전체(391개) 통과 유지

## 4. 새 PC 에서 진행 순서

### 4-1. 시작
Claude 앱 → Code → 작업 폴더 `바탕화면\github` → 첫 메시지 예:
> `backtest_stock-swing` 의 `swing/PLAN_STOCKDATA.md` 대로 구현해줘.
> (이 파일 = Google Drive migration 폴더 STOCKDATA_IMPL_GUIDE.md 와 같은 내용, 11/9 미푸시 패치 push_20261109 로 레포에 포함)

- 시작 전 확인: `git -C backtest_stock\backtest_stock-swing log --oneline -4` 에 11/9 19:37 커밋 3개가 있는지
  (없으면 `NEW_PC_SETUP.md` 7번으로 먼저 복원).
- 파이썬: `cd backtest_stock\backtest_stock-swing` → `python -m venv .venv` → `.venv\Scripts\pip install -r requirements.txt`.

### 4-2. 구현 → 테스트
- 2-2 → 2-3 → 2-4 → 2-5 → 2-6 → 2-7 → 2-8 순. 각 단계 테스트 먼저 붙이고 진행.
- 전체 테스트: `.venv\Scripts\python -m unittest discover -s swing/tests -t .`
- 로컬 리허설(네트워크 없이): FileStore + 가짜 collect_fn 으로 `python -m swing.run_daily --agent mock` 류 데모가 있으면 그걸로 판단 1회 → `inputs/` 생성 확인.

### 4-3. 커밋·푸시 (사용자가 요청할 때만)
- 브랜치 `swing_paper`, `git push origin swing_paper`. 메시지 끝 `Co-Authored-By: …`(세션 안내 따름).
- **푸시 시각**: 평일 17:05~정산 끝(보통 ~19:00) 사이 금지 — 푸시 = swing-web·swing-cron 재배포, 정산 중이면 크론이 끊김.
  장중(09:00~15:30) 푸시도 피한다(swing-web 재배포 중 화면 공백). 권장: 평일 15:40~17:00 또는 정산 끝난 뒤·주말.
- 커밋 대상: `swing/stockdata.py`·`store.py`·`agent/pipeline.py`·`server.py`·`run_daily.py`·`inputs_sync.py`·테스트·`PLAN_STOCKDATA.md`.
  `PROGRESS.md`·`CLAUDE.md` 는 커밋 금지(로컬 전용).

### 4-4. 배포 후 검증 (첫 크론 다음)
1. 그날 17:05 크론 끝난 뒤 `/api/swing/runs?date=<오늘>` 에 종목별 `inputBytes`·`saved:true` 가 있는지.
2. 판단 문서 하나(`/api/swing/decision?key=…`)에 `inputs.sha` 가 있는지, 같은 날 review 판단이 `reused:true` 인지.
3. 로컬에서 첫 동기화(4-5) → 받은 파일 수·바이트가 run 로그 합계와 같은지.
4. 1년(365일) 지난 날짜부터 run 로그 `inputsPruned` 가 ack 된 날짜만 지우는지(테스트로 먼저 확인 — 실제로는 1년 뒤).

### 4-5. 로컬 매일 내려받기 설정 (개인 PC)
1. 토큰: Railway swing-web Variables 의 `SWING_ADMIN_TOKEN` 값을 **직접 복사**해 사용자 환경변수로 등록
   (Windows 설정 → 시스템 → 고급 시스템 설정 → 환경 변수 → 새로 만들기). 채팅·파일에 붙여 넣지 않는다.
2. 아카이브 폴더 만들기: 예 `D:\swing_archive` (D 드라이브 없으면 `%USERPROFILE%\swing_archive`).
3. 수동 첫 실행(PowerShell 새 창):
   ```
   cd $env:USERPROFILE\Desktop\github\backtest_stock\backtest_stock-swing
   .venv\Scripts\python -m swing.inputs_sync --archive D:\swing_archive\inputs.sqlite
   ```
4. 작업 스케줄러 등록(매일 21:00 + 로그온 시, 놓친 실행은 켜질 때):
   ```
   schtasks /Create /TN "swing_inputs_sync" /SC DAILY /ST 21:00 /F /TR "cmd /c cd /d %USERPROFILE%\Desktop\github\backtest_stock\backtest_stock-swing && .venv\Scripts\python -m swing.inputs_sync --archive D:\swing_archive\inputs.sqlite >> D:\swing_archive\sync.log 2>&1"
   ```
   그 뒤 작업 스케줄러 앱에서 이 작업 → 속성 → 설정 → "예약된 시작 시간을 놓친 경우 가능한 대로 빨리 작업 시작" 체크,
   트리거에 "로그온할 때" 추가.
5. 백업: 주 1회 `D:\swing_archive\inputs.sqlite` 를 Google Drive 데스크톱 폴더 등 다른 저장소로 복사.
6. 확인: `sync.log` 마지막 줄에 날짜별 받은 파일 수·ack OK.

## 5. 주의
- 회사 PC 에서는 하지 않는다(네트워크 작업 금지). 이 작업 전체가 개인 PC 전용.
- 로컬에 `DATABASE_URL` 을 두지 않는다 — 내려받기는 swing-web API 로만.
- kit(`swing/agent/kit/`)은 무수정 원칙 — 저장·복원은 kit 밖(pipeline·stockdata).
- Live Trader(`swing/vwap/`) 경로는 건드리지 않는다.
- 이후 단계(이번 범위 밖): 2단계 수집·판단 분리(`judgeq/<date>`), 3단계 로컬 Claude 판단기 연결, 4단계 판단 대상 밖 정기 수집 —
  1단계 1주 실측(`inputBytes`)을 보고 결정.
