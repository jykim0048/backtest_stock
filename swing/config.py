"""스윙 모의투자 설정 — 사용자 합의값(2026-09-29). env 로 덮어쓸 수 있는 것만 노브로 둔다."""
import os


def _f(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def _i(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return int(default)


INITIAL_CAPITAL = _f("SWING_INITIAL_CAPITAL", 500_000_000)   # 초기 자본 5억
SELL_TAX = _f("SWING_SELL_TAX", 0.002)                       # 매도세 0.2% (슬리피지 없음)
HOLD_DAYS = _i("SWING_HOLD_DAYS", 5)                         # 보유 5영업일(체결일=1일째) — 2026-09-29 3→5
MAX_HOLD_DAYS = _i("SWING_MAX_HOLD_DAYS", 15)                 # 만기 후 재판별로 연장해도 이 보유일에서 다음 날 시가 청산
MAX_WEIGHT = _f("SWING_MAX_WEIGHT", 0.10)                    # 종목당 비중 상한
DEFAULT_WEIGHT = _f("SWING_DEFAULT_WEIGHT", 0.05)            # 트레이더가 비중 생략 시
MAX_NEW_PER_DAY = _i("SWING_MAX_NEW_PER_DAY", 10)            # 하루 신규 분석 종목 상한(LLM 호출량)

BUY_SIGNALS = ("동반강세", "수급유입")                        # 롱 후보 칸
SELL_SIGNALS = ("수급이탈", "동반약세")                       # 매도 검토 트리거(섹터 신호·종목 칸)
BUY_RATINGS = ("Buy", "Overweight")                          # PM 등급 — 진입 허용
SELL_RATINGS = ("Sell", "Underweight")                       # PM 등급 — 보유 종목 매도

# Short(공매도) — 2026-09-29 사용자 합의(PLAN_SHORT.md R1~R13). Long 과 대칭 규칙
SHORT_ENABLED = os.environ.get("SWING_SHORT", "1") != "0"   # 0 이면 Short 신규 분석·주문 안 함(보유분 정산은 계속)
SHORT_SIGNALS = ("동반약세", "수급이탈")                      # 숏 후보 칸(동반약세 먼저)
SHORT_RATINGS = SELL_RATINGS                                 # PM 등급 — 공매도 진입 허용
COVER_RATINGS = BUY_RATINGS                                  # PM 등급 — 보유 숏 환매(Hold 는 계속 보유)
MAX_NEW_SHORT_PER_DAY = _i("SWING_MAX_NEW_SHORT_PER_DAY", 10)  # 하루 신규 Short 분석 상한
SHORT_MAX_GROSS = _f("SWING_SHORT_MAX_GROSS", 0)             # Short 총노출 상한(평가액 대비). 0 = 상한 없음(2026-10-16 사용자 결정, 종전 0.30)
BORROW_RATE = _f("SWING_BORROW_RATE", 0.04)                  # 대차수수료 연율(진입금액 × 일수/365)
# Short 담보(2026-10-18 사용자 지시: 현금 100% → 주식 대용) — subst = 보유 Long 평가액 × SHORT_SUBST_RATE(대용가) + 현금을
# 담보로 쓰고, 요구 담보 = Short 평가액(수량 × 현재가) × SHORT_MARGIN. 공매도 대금은 현금에 넣지 않는다(레버리지 아님).
# cash = 종전(진입금액 100% 현금 담보). 기존 포지션은 진입 당시 방식(p["collateral"])으로 정산.
SHORT_COLLATERAL = os.environ.get("SWING_SHORT_COLLATERAL", "subst")   # subst | cash
SHORT_SUBST_RATE = _f("SWING_SHORT_SUBST_RATE", 0.70)        # 대용가 비율(주식 평가액 대비, KRX 사정비율 근사)
SHORT_MARGIN = _f("SWING_SHORT_MARGIN", 1.0)                 # 요구 담보 = Short 평가액 × 이 비율

# main 브랜치 산출물(읽기 전용) — 주간 브리핑은 16:10 런이 ~16:14 커밋
MAIN_RAW_BASE = os.environ.get(
    "SWING_MAIN_RAW_BASE",
    "https://raw.githubusercontent.com/jykim0048/backtest_stock/main")

# KIS 허브(vi_limit_monitor) HTTP — 수급·공매도·대차 /flow (스킬 collect 와 같은 기본값)
FLOW_API_BASE = os.environ.get("FLOW_API_BASE", "https://tradingstrategies-production-09d4.up.railway.app")

# 업종별 종목 수급 모멘텀 유니버스(2026-10-13, swing/universe.py · PLAN_UNIVERSE.md) — 매트릭스와 독립 후보 소스, 공통 모멘텀 점수
UNIVERSE_ENABLED = os.environ.get("SWING_UNIVERSE", "1") != "0"   # 0 이면 산출·후보 모두 끔
UNI_TOPK_TV = _i("SWING_UNI_TOPK_TV", 15)          # 1차 컷: 업종별 당일 거래대금 상위
UNI_TOPK_CAP = _i("SWING_UNI_TOPK_CAP", 5)         # 1차 컷: ∪ 시총 상위
UNI_TOPN = _i("SWING_UNI_TOPN", 10)                # 업종별 Top N
UNI_MIN_TV_EOK = _f("SWING_UNI_MIN_TV_EOK", 20)    # 유동성 하한: 20일 평균 거래대금(억)
UNI_FLOW_PER_SECTOR = _i("SWING_UNI_FLOW_PER_SECTOR", 12)   # KIS 허브 /flow 보강 대상(업종별 예비 상위)
UNI_CAND_MIN = _i("SWING_UNI_CAND_MIN", 2)         # '후보' 표시 최소 점수
UNI_COOLDOWN_DAYS = _i("SWING_UNI_COOLDOWN_DAYS", 3)   # 최근 N영업일 entry 판단 있으면 유니버스 후보 제외(쿨다운)·✓ 표시
UNI_NEW_PER_DAY = _i("SWING_UNI_NEW_PER_DAY", 15)     # 유니버스 소스 하루 신규 Long 분석 상한(= 통합 상위 10, 매트릭스와 별도)
UNI_NEW_SHORT_PER_DAY = _i("SWING_UNI_NEW_SHORT_PER_DAY", 15)  # 유니버스 소스 하루 신규 Short 분석 상한(= 통합 상위 10)
UNI_RANK_N = _i("SWING_UNI_RANK_N", 50)             # 전 업종 통합 롱/숏 순위 저장 길이(표시 10)
# 유니버스 후보 선정 방식(2026-10-14 A/B/C 시뮬 후 사용자 결정 = roundrobin):
#   roundrobin = 업종 라운드로빈(상방/하방 업종을 1위 점수순으로 돌며 업종당 1개씩, 자리가 남으면 2바퀴째) · rank = 통합 순위 상위 ·
#   sectorcap = 통합 순위 + 업종당 UNI_SECTOR_CAP
UNI_PICK_MODE = (os.environ.get("SWING_UNI_PICK_MODE", "roundrobin") or "roundrobin").strip().lower()
UNI_SECTOR_CAP = _i("SWING_UNI_SECTOR_CAP", 2)
# 유니버스 선정 기준 점수(2026-10-14 시뮬 A/T1/T2 후 사용자 결정 = total, T2):
#   total = 업종 1차 컷 통과 전 종목을 종합점수(0.55·주가 + 0.45·수급, 유동성 게이트)로 채점 → 업종 Top N·후보·정렬 모두 종합순,
#           후보 = 종합 ≥ UNI_TOTAL_MIN · score = 모멘텀 점수(−5~+5)로 종전 방식
UNI_RANK_BASIS = (os.environ.get("SWING_UNI_RANK_BASIS", "total") or "total").strip().lower()
UNI_TOTAL_MIN = _f("SWING_UNI_TOTAL_MIN", 50)
UNI_SHORT_FUT_ONLY = os.environ.get("SWING_UNI_SHORT_FUT_ONLY", "1") == "1"   # 2026-10-16 기본 켬 — 매트릭스·유니버스·촉매 Short 공통   # Short 후보를 주식선물 상장 종목으로 한정
# 점수 임계값 — 거래대금 가속(1W/4W−1), 수급/시총(bp), 공매도 비중 Δ(pp), 대차 Δ(bp), 공매도 비중 높음(%)
UNI_TV_HI, UNI_TV_LO = _f("SWING_UNI_TV_HI", 0.40), _f("SWING_UNI_TV_LO", 0.15)
UNI_FLOW_HI, UNI_FLOW_LO = _f("SWING_UNI_FLOW_HI", 20), _f("SWING_UNI_FLOW_LO", 6)
UNI_SHORT_DELTA = _f("SWING_UNI_SHORT_DELTA", 0.6)
UNI_LOAN_BP = _f("SWING_UNI_LOAN_BP", 12)
UNI_SHORT_HIGH = _f("SWING_UNI_SHORT_HIGH", 5.0)
# DB증권 응답 단위 → 억원 나눗셈(1차 실측 전 추정: 거래대금 원, 투자자 매수·매도 금액 천원, Avls 100만원).
# /api/swing/universe 의 diag.rawSample 로 확인 후 조정
UNI_TV_DIV = _f("SWING_UNI_TV_DIV", 1e8)
UNI_FLOW_DIV = _f("SWING_UNI_FLOW_DIV", 1e5)
UNI_CAP_DIV = _f("SWING_UNI_CAP_DIV", 100)
# 멀티기간 수익률·백분위 점수(2026-10-14, 브레인 리포트 양식 역산): 종합 = 0.55·주가 + 0.45·수급, 유동성점수 < 30 이면 종합 공란(게이트)
UNI_W_PX = _f("SWING_UNI_W_PX", 0.55)
UNI_W_FLOW = _f("SWING_UNI_W_FLOW", 0.45)
UNI_LIQ_GATE = _f("SWING_UNI_LIQ_GATE", 30)
# 유동성 반영 방식(2026-10-14 시뮬 비교용 노브, 운용 기본 gate): gate = 유동성 < UNI_LIQ_GATE 면 종합 공란 ·
#   weight = 종합 = w_px·주가 + w_flow·수급 + UNI_W_LIQ·유동성(게이트 없음) · soft = 유동성 < 게이트면 종합 × 유동성/게이트(공란 없음)
UNI_LIQ_MODE = (os.environ.get("SWING_UNI_LIQ_MODE", "gate") or "gate").strip().lower()
UNI_W_LIQ = _f("SWING_UNI_W_LIQ", 0.0)
# 유동성 백분위 풀(2026-10-14 사용자 결정 = all): all = Long·Short 합산 풀 · side = 방향별 풀(종전 — Short 풀이 대형주 20종목뿐이라
# 경계가 ~335억까지 올라 195~284억 종목까지 게이트에 걸리던 문제)
UNI_LIQ_POOL = (os.environ.get("SWING_UNI_LIQ_POOL", "all") or "all").strip().lower()
UNI_PX_KEEP = _i("SWING_UNI_PX_KEEP", 320)          # 종가 이력 캐시 보관 영업일(12M 수익률 252 + 여유)
# 하방 촉매 Short 후보 소스(2026-10-15, PLAN_CATALYST_SHORT 3단계) — main 장중 촉매 아카이브의 bearish 촉매(규칙 태그 우선)
CAT_SHORT_ENABLED = os.environ.get("SWING_CAT_SHORT", "1") != "0"      # 0 이면 촉매 소스 끔
CAT_SHORT_MIN = _i("SWING_CAT_SHORT_MIN", 4)                           # 하방 강도(1~5) 최소 — 규칙 bearish 공시 = 4
CAT_NEW_SHORT_PER_DAY = _i("SWING_CAT_NEW_SHORT_PER_DAY", 5)           # 촉매 소스 하루 신규 Short 분석 상한
CAT_MAX_DROP = _f("SWING_CAT_MAX_DROP", 20)                            # 당일 하락률(%)이 이보다 크면 추격 금지(관찰만)
CAT_RALLY_SKIP = _f("SWING_CAT_RALLY_SKIP", 10)                        # 당일 상승률(%)이 이 이상이면 제외(제3자배정 유증 등 호재성 자금조달)
CAT_DART_DIRECT = os.environ.get("SWING_CAT_DART_DIRECT", "1") != "0"  # DART_API_KEY 있으면 16:30 당일 공시 직접 조회(장 마감 후 접수분)
# 미도달 주문 재시도(2026-10-16, 방식 A): 전날 지정가가 미도달 취소되면 같은 진입·손절·목표가로 다음 영업일 1일 지정가를 다시 낸다
# (재분석 없음 → LLM 비용 0). 최대 ENTRY_RETRY_MAX 회(원주문 포함 ENTRY_RETRY_MAX+1 회 시도). 가드: 취소일 종가가 진입가 ±ENTRY_RETRY_BAND
# 안이고 손절가를 침범하지 않았을 때만(레벨이 낡으면 중단 → 쿨다운 뒤 재선정 시 재분석).
ENTRY_RETRY_MAX = _i("SWING_ENTRY_RETRY_MAX", 2)
# 판단 실패 자동 재분석(2026-10-18, swing/redo.py) — 하루 처리 뒤 LLM 판단 실패가 있으면 이 초만큼 기다렸다가 1회 재분석. 0 = 끔
REDO_WAIT_S = _i("SWING_REDO_WAIT_S", 900)
ENTRY_RETRY_BAND = _f("SWING_ENTRY_RETRY_BAND", 0.03)
# 소급 시작일(2026-10-16 사용자 지시: 10-15 데이터부터) — 이 날 이후 유효일의 미도달 취소 중 아직 재시도 판정을 안 받은 것(기능 배포 전 취소분)도
# 다음 런에서 같은 규칙으로 재주문한다. 유효일로부터 ENTRY_RETRY_MAX 영업일이 지나면 기간 경과로 종료.
ENTRY_RETRY_FROM = os.environ.get("SWING_ENTRY_RETRY_FROM", "2026-10-15")
# 매트릭스 후보에도 유니버스·하방 촉매와 같은 쿨다운(UNI_COOLDOWN_DAYS)을 적용(2026-10-16 사용자 지시). 0 이면 끔(종전: 매일 재분석)
MATRIX_COOLDOWN = os.environ.get("SWING_MATRIX_COOLDOWN", "1") != "0"
# 체결일 손절(2026-10-16 사용자 지시): 체결일(D) 일봉이 손절가에 닿으면
#   review    = 당일 청산하지 않고 D+1 손절을 유예 → D+1 종가로 trading_agent 재판별 → PM Sell/Underweight 면 D+2 시가 청산,
#               계속 보유면 새 손절·목표(유효할 때) 적용(새 손절이 없으면 기존 손절 유지 → D+2 시가가 그 아래면 갭 손절) — 기본
#   next_open = D+1 시가 청산 예약 · same_day = 종전(D 손절가로 당일 청산, 보수적 가정)
FILL_DAY_STOP = (os.environ.get("SWING_FILL_DAY_STOP", "review") or "review").strip().lower()
# Live Trader(장중 집행기)(swing/vwap, PLAN_LIVE_TRADER.md, 2026-10-18) 기록을 정산에 반영 — 0 이면 intraday/<date> 를 무시하고 종전 일봉 규칙만
INTRADAY_SETTLE = os.environ.get("SWING_INTRADAY", "1") != "0"
# 유니버스 분석 상한 이연(2026-10-16 사용자 지시: Long 15 / Short 15, 한쪽 후보가 상한보다 적으면 남는 자리를 다른 쪽에 넘김)
UNI_CAP_CARRY = os.environ.get("SWING_UNI_CAP_CARRY", "1") != "0"
# 현금 부족 시 부분 진입 + 잔여분 이월(2026-10-16 사용자 지시, A+B): 손실 종목 청산으로도 전량이 안 되면 살 수 있는 수량만 진입
# (계획 수량의 PARTIAL_ENTRY_MIN 이상일 때만, 미만이면 종전처럼 스킵) → 모자란 수량은 같은 레벨로 다음 영업일 추가 주문(최대 ENTRY_RETRY_MAX 회,
# 미도달 재주문과 같은 가격 가드), 체결되면 기존 포지션에 합쳐 평균단가.
PARTIAL_ENTRY = os.environ.get("SWING_PARTIAL_ENTRY", "1") != "0"
PARTIAL_ENTRY_MIN = _f("SWING_PARTIAL_ENTRY_MIN", 0.5)
# 주식선물 미상장 Short 정리(2026-10-16 사용자 지시: 뉴인텍·엑스플러스 종가 청산) — UNI_SHORT_FUT_ONLY 가 켜져 있으면 선물 미상장 종목의
# Short 포지션은 그날 종가로 환매(사유 fut_none), 그 종목의 미도달 재주문·잔여분 추가 주문도 내지 않는다. 0 이면 끔.
SHORT_FUT_CLOSE = os.environ.get("SWING_SHORT_FUT_CLOSE", "1") != "0"
