# 출력 형식 — 결정 에이전트 4종의 구조화 출력

TradingAgents 의 Pydantic 스키마를 마크다운 형식으로 옮겼다. **헤더 문구와 순서를 정확히 지킬 것.**
결정 로그와 후속 파서가 `**Rating**:` 같은 라벨을 정규식으로 읽는다.

## 5단계 등급 (Research Manager · Portfolio Manager 공통)

| 등급 | 뜻 |
|---|---|
| Buy | 강한 확신. 신규 진입 또는 비중 확대 |
| Overweight | 우호적. 점진적 비중 확대 |
| Hold | 현 포지션 유지. 근거가 균형·상충·모호·불충분할 때 |
| Underweight | 비중 축소, 일부 차익실현 |
| Sell | 청산 또는 진입 회피 |

**결단력 있어 보이려고 방향을 만들지 말 것.** 근거가 맞서면 Hold 가 정답이다. 발언 순서와 무관하게 논리로 가중한다.

## SentimentReport (Sentiment Analyst)

```
**Overall Sentiment:** **<Bullish|Mildly Bullish|Neutral|Mixed|Mildly Bearish|Bearish>** (Score: <0.0~10.0>/10)
**Confidence:** <Low|Medium|High>

<narrative: 소스별 분석 → 소스 간 괴리·일치 → 지배 테마 → 촉매·리스크 → 요약표>
```
- Score 가이드: Bullish 6.5~10, Mildly Bullish 5.5~6.4, Neutral/Mixed 4.5~5.5, Mildly Bearish 3.5~4.4, Bearish 0~3.4.
- Mixed 는 소스들이 명확히 다른 방향일 때. Neutral 은 모든 소스가 침묵·무의견일 때만.
- Confidence Low: 소스 하나라도 unavailable 이거나 표본 5건 미만.

## ResearchPlan (Research Manager)

```
**Recommendation**: <Buy|Overweight|Hold|Underweight|Sell>

**Rationale**: <강세·약세 양측 핵심 논점 요약 후 어느 논거가 결정을 이끌었는지. 동료에게 말하듯>

**Strategic Actions**: <트레이더가 실행할 구체 단계. 등급에 맞는 비중 가이드 포함>
```

## TraderProposal (Trader)

```
**Action**: <Buy|Hold|Sell>

**Reasoning**: <애널리스트 리포트와 리서치 플랜에 근거한 2~4문장>

**Entry Price**: <절대 가격, 원>          (생략 가능)
**Stop Loss**: <절대 가격, 원>            (생략 가능)
**Position Sizing**: <예: 포트폴리오의 5%>  (생략 가능)

FINAL TRANSACTION PROPOSAL: **<BUY|HOLD|SELL>**
```
- 진입가·손절가는 **원 단위 절대 가격**. %나 범위 금지. 못 정하면 줄을 생략한다.
- 손절은 ATR·지지선 등 `01_price.json` 의 가격 구조에 근거한다.

## PortfolioDecision (Portfolio Manager)

```
**Rating**: <Buy|Overweight|Hold|Underweight|Sell>

**Executive Summary**: <진입 전략·비중·핵심 리스크 레벨·기간을 담은 2~4문장>

**Investment Thesis**: <애널리스트 토론의 구체 증거에 근거한 상세 논리. 과거 교훈이 주어졌으면 반영>

**Price Target**: <원>            (생략 가능)
**Time Horizon**: <예: 1~2주>     (생략 가능)
```

## Reflection (정산 후)

정확히 2~4문장의 산문. 불릿·헤더·마크다운 금지. 순서: ① 방향이 맞았나(알파 수치 인용) ② 논지의 어느 부분이 맞고 틀렸나 ③ 다음 유사 분석에 적용할 교훈 하나. 결정 로그에 그대로 저장되어 미래 분석이 읽으므로 모든 단어가 값을 해야 한다.

## 신호 파싱 규칙

최종 신호는 PM 출력의 `**Rating**:` 줄에서 읽는다. 5단계 단어가 없으면 **REVIEW** 로 기록하고 Hold 로 뭉개지 않는다.
