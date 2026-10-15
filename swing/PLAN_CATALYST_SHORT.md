# 하방 촉매 → Short 후보 입구 (2026-10-15)

배경: 10/13 두산퓨얼셀 EB·BW 발행 공시처럼 하방 압력이 강한 공시를 놓쳤다. 원인은 ① main DART 키워드에 **교환사채가 없고** 최신순 40건 상한에 잘림
② 10/14 "4,000억 영구채" 뉴스를 LLM 이 "자금 확보" 로 읽어 **bullish 3점** ③ 촉매 스코어(1~5)가 롱 전용이라 악재는 1~2점 → 타임라인·섹터 시그널 풀·스윙 어디에도
하방 촉매를 소비하는 단계가 없었다(10/13 아카이브엔 유상증자 공시 4건·하방 뉴스가 bearish 로 이미 잡혀 있었음).

## 구조 (매트릭스 → 유니버스 → **촉매** 세 소스, 공통 모멘텀 채점·쿨다운·주식선물 옵션 동일)

### main (backtest_stock main)
- `analysis/sources.py` — DART 키워드 확장(교환사채·영구·신종자본증권·감자·관리종목·불성실공시·매매거래정지·횡령·배임·감사의견·상장폐지·담보제공·잠정실적),
  **규칙 방향 태그** `dart_rule_direction(title)` → (bearish|bullish|None, kind: dilution|distress|contract|buyback|approval|bonus).
  해지·철회·취소 제목과 "거래정지 + 호재(공급계약 등)" 는 None. `dart_today_disclosures` 는 우선 키워드(자금조달·악재) 30건 + 일반 40건 2단 수집(3페이지), 행에 dirRule/ruleKind.
- `generate_intraday_briefing.py` — 프롬프트에 희석성 자금조달 = bearish 규칙, 공시 촉매는 규칙 태그 우선(LLM bullish 면 `llmDirection` 보존), title·dirRule·ruleKind·code 저장,
  **백스톱**: 규칙 bearish 공시를 LLM 이 안 고르면 제목 템플릿으로 추가(`BRIEFING_CAT_RULE_EXTRA` 10).
- `score_catalysts.py` — `bearScore`(1~5, LLM + 규칙 휴리스틱 폴백 `_mock_bear`: 규칙 공시 4(정정만이면 3), 하방 뉴스 2(3회차↑ 3), −10% 이하 +1), 구 아카이브는 회차 disclosures 의 url→제목으로 규칙 복원,
  회차 `bearFourStar`.
- `generate_weekly_briefing.py` — 타임라인 종목 행에 **하방 행**(bearScore 기준 코스피 ★4↑/코스닥 ★5, direction=bearish), 병합 시 방향 보존, 섹터 시그널 풀 `catDirection`(점수 미가산).
- 대시보드 타임라인 ▼ 하방 배지, 주간 엑셀 별점 셀 ▼.

### swing_paper
- `swing/catalysts.py` — `fetch_archive`(main raw) → `collect_bearish`(회차 catalysts bearish + 회차 disclosures 규칙 bearish + DART 직접 조회분 + 스코어 회차 changePct/bearScore)
  → `strength`(bearScore 우선) → `short_candidates`(제외: 코드 미해석·보유/대기/다른 소스·쿨다운·강도<CAT_SHORT_MIN·당일 −CAT_MAX_DROP 이하 급락·+CAT_RALLY_SKIP 이상 급등·리츠/ETF/우선주·주식선물 한정).
  `dart_today_bearish`(DART_API_KEY 있을 때 16:30 당일 공시 직접 — 15:30 이후 접수분). `CatalystRunner` 는 store `catalyst/<date>` 에 entries·skipped·candidates.
- `daily.run_day(cat=)` 6) 단계: 유니버스 뒤, `attach_scores(source="catalyst")`, 상한 `CAT_NEW_SHORT_PER_DAY`, run `shortCandidates`(source=catalyst, catalyst{strength…})·`catShortCapSkipped`.
- `agent_iface.DecisionRequest.catalyst`, `pipeline._catalyst_line`(공시 요약·강도·당일 등락·마감후 접수 → 추격 리스크 판단 지시).
- 대시보드 후보 표 태그 `하방촉매 ★n(· 마감후)`, 상태 `촉매`.
- 리플레이 `swing/tools/replay_catalyst.py` + `/admin/catrep?token&from&to[&min&drop]` → `sim/catalyst_replay/<from>_<to>`: 후보 행 + 다음 영업일 종가 진입 1/3/5일 수익률, 강도·종류별 하락비율·Short 평균.

노브: `SWING_CAT_SHORT`(1) · `SWING_CAT_SHORT_MIN`(4) · `SWING_CAT_NEW_SHORT_PER_DAY`(5) · `SWING_CAT_MAX_DROP`(20) · `SWING_CAT_RALLY_SKIP`(10) · `SWING_CAT_DART_DIRECT`(1, DART_API_KEY 필요).

## 검증
- 오프라인: swing 125 테스트(촉매 7·리플레이 2), main `tests/test_dart_rule_direction.py`.
- 로컬 아카이브 리플레이(10/12~10/15, 수익률 없음): 규칙 ★4 후보 — 유상증자·CB 발행결정 다수, 정정만 있는 재등장(STX·MSDI)은 ★3 으로 내려 제외, 거래정지(단일판매공급계약) 제외, 리츠 제외.
- 운영 확인 순서: ① main 푸시 → 다음 장중 라운드부터 dirRule·백스톱 ② swing 푸시 + swing-cron `DART_API_KEY` ③ `/admin/catrep` 로 9/1~10/14 수익률 집계 ④ 16:30 런의 촉매 후보·PM 판단 확인.

## 남은 결정
- 희석률(발행 규모/시총) 계산은 DART 문서 본문 파싱이 필요해 미구현(강도 가점 후보).
- 하방 촉매를 섹터 시그널 부정 축으로 인정할지(현재 표시만).
- 시뮬(sim_universe)에 촉매 Short 소스를 체결 규칙까지 붙일지 — 리플레이 결과 본 뒤.
