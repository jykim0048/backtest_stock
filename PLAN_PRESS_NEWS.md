# 한경·매경 종목 뉴스 수집 (RSS 실시간 + 네이버 API 백필) 구현 계획

대상: 코스피200 + 코스닥150 구성종목에 관련된 한국경제(hankyung.com)·매일경제(mk.co.kr) 기사.
저장: Railway Postgres(endpoint 프로젝트) → 기존 `stock_news` VIEW 에 합류해 DataGrip 에서 종목·일자별 조회.

## 사전 실측 (2026-10-19)

| 항목 | 결과 |
|---|---|
| RSS 피드 1회 | 피드당 최신 50건. 한경 증권 ≈10h, 매경 증권 ≈27h, 한경/매경 전체·헤드라인 ≈3~4h 분량 |
| Railway IP → RSS | 한경 `User-Agent: Mozilla/5.0` 200 / 크롬 UA 403(Cloudflare). 매경은 반대(크롬 UA 200, 단순 UA 403) → 사이트별 UA 고정, 403 이면 그 회차 스킵(우회 시도 안 함) |
| 네이버 `"{종목명}"` 검색 | 1000건 상한에 한경·매경 비율 2~4% → 삼성전자는 반나절치만 닿음. 백필 부적합 |
| 네이버 `"{종목명} 한경"` / `"{종목명} 매경"` | 한경/매경 정확도 77~97%, 깊이: 삼성전자 한경 1년·매경 2년, 중소형은 전 기간(2005~) |
| 네이버 비용 | 종목·언론사당 최대 10콜(100건×10). 일 한도 25,000콜(기존 파이프라인과 공유) |

## 데이터 소스 3갈래

1. **RSS 실시간** — 한경 `finance`·`economy`, 매경 `50200011`(증권)·`30000001`(헤드라인). 30분 주기.
   피드 기사 중 유니버스 종목명이 제목/요약에 등장하는 것만 저장.
2. **네이버 일일 보충** — 매일 밤 종목별 `"{name} 한경"`, `"{name} 매경"` 1페이지(100건). RSS 섹션 밖
   (산업·IT 면) 기사와 RSS 다운 구간을 메운다. ≈ 350종목 × 2 = 700콜/일.
3. **네이버 1회 백필** — 같은 쿼리로 start=1..1000 끝까지. ≈ 350 × 2 × ≤10 = 최대 7,000콜.
   한도 공유를 고려해 종목 단위 체크포인트로 이어달리기(중단·재개 가능), 1일 상한 `BACKFILL_MAX_CALLS`.

세 갈래 모두 originallink 도메인이 hankyung.com / mk.co.kr 인 것만 남기고 URL 로 dedup.
본문 크롤링은 하지 않는다 — 제목·요약·URL·발행시각만 저장(저작권).

## 유니버스 / 종목 매칭

- 유니버스 = `data/index_constituents.json` 의 KOSPI200 ∪ KOSDAQ150, 이름은 `public/assets/krx_companies.json`.
- **선행 수정 필요**: 현재 파일이 KOSPI200 199건(중복 2), KOSDAQ150 **280건**(중복 1)으로 비정상.
  `index_constituents.yml` 재실행(또는 수동 검증) 후 197+150 근처로 맞춘다.
- 매칭 규칙 (RSS·보충·백필 공통):
  - 제목+요약에서 종목명 탐색, **긴 이름 우선**(카카오뱅크가 카카오를 가림), 앞뒤가 한글/영숫자면 불일치.
  - 오탐 많은 짧은/일반명사 이름은 `analysis/news_aliases.json` 로 관리: 제외어(예: 'LG' 단독)·별칭(삼전→삼성전자, 하닉→SK하이닉스).
  - 네이버 쿼리로 들어왔지만 제목·요약에 이름이 없으면 `matched_in='query'` 로 저장(본문에만 등장) — VIEW 기본 필터에서 제외.

## 스키마 (press_news.py `init_schema`, idempotent)

```sql
CREATE TABLE IF NOT EXISTS press_news (
  url          text PRIMARY KEY,          -- 정규화(쿼리스트링·모바일 m. 제거)
  press        text NOT NULL,             -- 'hankyung' | 'mk'
  title        text NOT NULL,
  description  text,
  published_at timestamptz NOT NULL,
  channel      text NOT NULL,             -- 처음 잡힌 경로: 'rss:finance' | 'naver:daily' | 'naver:backfill' ...
  collected_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS press_news_stock (
  url        text REFERENCES press_news(url) ON DELETE CASCADE,
  code       text NOT NULL,
  matched_in text NOT NULL,               -- 'title' | 'description' | 'query'
  PRIMARY KEY (url, code)
);
CREATE INDEX IF NOT EXISTS press_news_stock_code ON press_news_stock(code);
CREATE INDEX IF NOT EXISTS press_news_published ON press_news(published_at);
CREATE TABLE IF NOT EXISTS press_news_backfill (   -- 백필 체크포인트
  code text, press text, next_start int, done bool DEFAULT false, updated_at timestamptz DEFAULT now(),
  PRIMARY KEY (code, press)
);
```

`stock_news` VIEW 에 `kind = 'press:hankyung' | 'press:mk'` 로 UNION
(news_date = published_at 의 KST 일자, report_date = 같은 값, sentiment/insight 는 NULL).

## 실행 위치

- **RSS·일일 보충: railway_server `_scheduler` 스레드** — DB 가 내부망 전용(TCP Proxy 꺼짐)이라
  컨테이너 안에서 바로 쓰는 게 가장 단순하고, GH cron 드롭 문제도 피한다. `/healthz` 에 마지막 수집 시각·건수 노출.
- **백필: CLI** `python press_news.py backfill [--max-calls N]` 를 `railway ssh` 로 컨테이너에서 실행.
  체크포인트 덕에 여러 날에 나눠 돌려도 된다.

## 작업 순서

1. 유니버스 파일 정상화(KQ150 280건 문제) — 선행.
2. `press_news.py`: RSS 파서(정규식 기반, 한경 피드는 XML 이 엄격하지 않음), 네이버 쿼리, 매칭, URL 정규화, 업서트, CLI(`rss` / `daily` / `backfill` / `stats`).
3. `analysis/news_aliases.json` 초안 — 유니버스 이름 중 2글자·영문 약칭·다른 이름의 부분문자열인 것 자동 추출 후 수동 검토.
4. `railway_server.py` 스케줄러 연결(30분 RSS, 22:30 일일 보충) + healthz.
5. `sql/stock_news_view.sql` 에 press UNION 추가 후 재생성.
6. 검증: RSS 1회 → 매칭 샘플 50건 눈으로 정밀도 확인 → 백필 10종목 시범 → 전체 백필(이어달리기).
7. 배포 후 하루 지켜보며 RSS·보충 중복률(=RSS 커버리지) 확인, 낮으면 피드 추가.

## 미결정

- 백필 기간: 기본은 "네이버가 주는 만큼"(대형주 ≈1~2년, 중소형 전 기간). 컷오프(예: 2024-01-01)를 둘지.
- 구성종목 변경(6·12월) 시 편출 종목의 기존 기사는 유지, 신규 편입 종목은 자동 백필 대상에 추가.
