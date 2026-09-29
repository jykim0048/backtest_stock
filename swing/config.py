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
SHORT_MAX_GROSS = _f("SWING_SHORT_MAX_GROSS", 0.30)          # Short 총노출 상한(평가액 대비)
BORROW_RATE = _f("SWING_BORROW_RATE", 0.04)                  # 대차수수료 연율(진입금액 × 일수/365)

# main 브랜치 산출물(읽기 전용) — 주간 브리핑은 16:10 런이 ~16:14 커밋
MAIN_RAW_BASE = os.environ.get(
    "SWING_MAIN_RAW_BASE",
    "https://raw.githubusercontent.com/jykim0048/backtest_stock/main")
