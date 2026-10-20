-- 종목별·일자별 뉴스 뷰 (DataGrip 조회용). reports.payload(jsonb) 를 펼친 읽기 전용 VIEW 라
-- 별도 적재가 필요 없고, 리포트가 업서트되면 즉시 반영된다.
--
-- 출처:
--   daily / intraday / down / intraday_down : [].analysis.news.items  (LLM 요약·감성·URL 포함)
--   intraday_briefing                       : rounds[].sectorsUp|sectorsDown[].stocks[].news (제목만, 회차간 중복 제거)
--   press:hankyung / press:mk               : press_news + press_news_stock (press_news.py 수집, 한경·매경 RSS/네이버)
--                                             matched_in='query'(본문에만 종목명 등장)는 제외.
--                                             match 컬럼 = 'title'(제목에 종목명) | 'description'(요약에만) —
--                                             관련도 높은 것만 보려면 WHERE match = 'title'
--
-- news_date: 기사 날짜 원문(news_date_raw)이 형식이 제각각이라(YYYY-MM-DD, RFC822, YYYY.MM.DD 등)
--            파싱 가능한 것만 date 로 변환, 나머지는 NULL. 브리핑 뉴스는 원문 날짜가 없어 report_date 사용.
CREATE OR REPLACE VIEW stock_news AS
WITH analysis_news AS (
  SELECT r.report_date,
         r.kind,
         s->>'code'   AS code,
         s->>'name'   AS name,
         s->>'market' AS market,
         n->>'date'   AS news_date_raw,
         n->>'sentiment' AS sentiment,
         n->>'source' AS source,
         n->>'title'  AS title,
         COALESCE(n->>'insight', n->>'summary') AS insight,
         n->>'url'    AS url,
         NULL::text   AS match
  FROM reports r
  CROSS JOIN LATERAL jsonb_array_elements(
         CASE WHEN jsonb_typeof(r.payload) = 'array' THEN r.payload ELSE '[]'::jsonb END) s
  CROSS JOIN LATERAL jsonb_array_elements(
         CASE WHEN jsonb_typeof(s->'analysis'->'news'->'items') = 'array'
              THEN s->'analysis'->'news'->'items' ELSE '[]'::jsonb END) n
  WHERE r.kind IN ('daily', 'intraday', 'down', 'intraday_down')
),
briefing_news AS (
  SELECT DISTINCT
         r.report_date,
         r.kind,
         st->>'code' AS code,
         st->>'name' AS name,
         NULL::text  AS market,
         NULL::text  AS news_date_raw,
         NULL::text  AS sentiment,
         NULL::text  AS source,
         t #>> '{}'  AS title,
         NULL::text  AS insight,
         NULL::text  AS url,
         NULL::text  AS match
  FROM reports r
  CROSS JOIN LATERAL jsonb_array_elements(
         CASE WHEN jsonb_typeof(r.payload->'rounds') = 'array' THEN r.payload->'rounds' ELSE '[]'::jsonb END) rd
  CROSS JOIN LATERAL jsonb_array_elements(
         CASE WHEN jsonb_typeof(rd->'sectorsUp') = 'array' THEN rd->'sectorsUp' ELSE '[]'::jsonb END
      || CASE WHEN jsonb_typeof(rd->'sectorsDown') = 'array' THEN rd->'sectorsDown' ELSE '[]'::jsonb END) sec
  CROSS JOIN LATERAL jsonb_array_elements(
         CASE WHEN jsonb_typeof(sec->'stocks') = 'array' THEN sec->'stocks' ELSE '[]'::jsonb END) st
  CROSS JOIN LATERAL jsonb_array_elements(
         CASE WHEN jsonb_typeof(st->'news') = 'array' THEN st->'news' ELSE '[]'::jsonb END) t
  WHERE r.kind = 'intraday_briefing'
),
press AS (
  SELECT (p.published_at AT TIME ZONE 'Asia/Seoul')::date AS report_date,
         'press:' || p.press AS kind,
         s.code,
         s.name,
         NULL::text  AS market,
         to_char(p.published_at AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD HH24:MI') AS news_date_raw,
         NULL::text  AS sentiment,
         CASE p.press WHEN 'hankyung' THEN '한국경제' WHEN 'mk' THEN '매일경제' END AS source,
         p.title,
         p.description AS insight,
         p.url,
         s.matched_in AS match
  FROM press_news p
  JOIN press_news_stock s ON s.url = p.url
  WHERE s.matched_in <> 'query'
),
unioned AS (
  SELECT * FROM analysis_news
  UNION ALL
  SELECT * FROM briefing_news
  UNION ALL
  SELECT * FROM press
)
SELECT u.report_date,
       CASE
         WHEN u.kind = 'intraday_briefing' OR u.kind LIKE 'press:%' THEN u.report_date
         WHEN u.news_date_raw ~ '^\d{4}-\d{2}-\d{2}'
           THEN to_date(left(u.news_date_raw, 10), 'YYYY-MM-DD')
         WHEN u.news_date_raw ~ '^[A-Za-z]{3}, \d{2} [A-Za-z]{3} \d{4}'
           THEN to_date(substr(u.news_date_raw, 6, 11), 'DD Mon YYYY')
         WHEN u.news_date_raw ~ '^\d{4}\.\d{2}\.\d{2}'
           THEN to_date(left(u.news_date_raw, 10), 'YYYY.MM.DD')
       END AS news_date,
       u.code,
       u.name,
       u.market,
       u.kind,
       u.sentiment,
       u.source,
       u.title,
       u.insight,
       u.url,
       u.news_date_raw,
       u.match
FROM unioned u
WHERE u.title IS NOT NULL AND u.title <> '';

COMMENT ON VIEW stock_news IS
  '종목별·일자별 뉴스 (reports.payload 펼침). 출처: sql/stock_news_view.sql (backtest_stock)';
