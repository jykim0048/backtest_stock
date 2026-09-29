"""일봉 시세 소스 — 체결·청산 '도달 여부' 판정과 평가용. 형식: {code: {open, high, low, close}}.

- DictPrices   : 고정 데이터(테스트·백필 재현)
- MockPrices   : 코드별 결정적 랜덤워크(로컬 프리뷰)
- YFinancePrices: 참고용. 2026-09-29 P0 실측에서 종가·고저 오차와 코스닥 누락 → Railway 는 DbHubPrices 사용.
- DbHubPrices  : DB증권 CHARTDAY 확정 일봉(2026-09-29). 같은 Railway 프로젝트의 DB 허브가 Redis 에 둔
                 토큰(`db:token`)을 **읽기만** 한다(발급하지 않음 — 토큰 1분 1건 제한·허브 단일 발급 원칙).
"""
import datetime
import hashlib
import json
import os
import random
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DictPrices:
    def __init__(self, data):
        self.data = data                      # {date: {code: bar}}

    def daily_bars(self, codes, date):
        day = self.data.get(date) or {}
        return {c: day[c] for c in codes if c in day}


class MockPrices:
    """기준가 1만~10만, 일 변동 ±3% 랜덤워크. 같은 (code, date) 는 항상 같은 값."""

    def _bar(self, code, date):
        seed = int(hashlib.md5(code.encode()).hexdigest()[:8], 16)
        base = 10_000 + seed % 90_000
        d0 = datetime.date(2026, 1, 1)
        n = (datetime.date.fromisoformat(date) - d0).days
        rng = random.Random(seed)
        px = base
        for _ in range(max(n, 0)):
            px *= 1 + rng.uniform(-0.03, 0.03)
        o = px * (1 + rng.uniform(-0.01, 0.01))
        c = px * (1 + rng.uniform(-0.02, 0.02))
        h = max(o, c) * (1 + rng.uniform(0, 0.02))
        lo = min(o, c) * (1 - rng.uniform(0, 0.02))
        return {"open": round(o), "high": round(h), "low": round(lo), "close": round(c)}

    def daily_bars(self, codes, date):
        return {c: self._bar(c, date) for c in codes}


class YFinancePrices:
    """yfinance .KS/.KQ 일봉. 시장은 public/assets/krx_companies.json(보통주) 기준,
    우선주는 보통주 코드의 시장 사용. 당일 봉은 장 마감 후 반영 지연 가능(미검증)."""

    def __init__(self):
        self._mkt = None

    def _market(self, code):
        if self._mkt is None:
            self._mkt = {}
            try:
                with open(os.path.join(_ROOT, "public", "assets", "krx_companies.json"),
                          encoding="utf-8") as f:
                    for e in json.load(f):
                        self._mkt[str(e.get("code")).zfill(6)] = e.get("market")
            except Exception:
                pass
        m = self._mkt.get(code) or self._mkt.get(code[:5] + "0")
        return "KQ" if str(m or "").upper().startswith("KOSDAQ") else "KS"

    def daily_bars(self, codes, date):
        import yfinance as yf
        d = datetime.date.fromisoformat(date)
        out = {}
        for c in codes:
            try:
                df = yf.download(f"{c}.{self._market(c)}", start=d.isoformat(),
                                 end=(d + datetime.timedelta(days=1)).isoformat(),
                                 progress=False, auto_adjust=False)
                if df is None or df.empty:
                    continue
                r = df.iloc[-1]
                if str(df.index[-1].date()) != date:
                    continue

                def v(k):
                    x = r[k]
                    return float(x.iloc[0] if hasattr(x, "iloc") else x)
                out[c] = {"open": v("Open"), "high": v("High"), "low": v("Low"),
                          "close": v("Close")}
            except Exception as ex:
                print(f"[swing] yfinance {c} {date}: {ex}")
        return out


class DbHubPriceError(RuntimeError):
    """DB 허브 시세 사용 불가(토큰 없음·만료·Redis 장애). 하루 런을 멈춰 원장을 저장하지 않게 한다 —
    시세 없이 정산하면 대기 주문이 '시세 없음' 으로 전부 취소되기 때문."""


class DbHubPrices:
    """DB증권 CHARTDAY(일차트) — KRX 정규장 확정 일봉(시장 J = KRX, 수정주가 미사용 = 실제 체결가).

    **조회 전용.** DB증권 토큰은 계좌를 식별해 주문 TR 도 통과하므로, 이 클래스는 CHARTDAY 경로 하나만
    호출하고 허브 패키지(core.broker 등)를 가져오지 않는다. 토큰은 DB 허브가 발급해 Redis `db:token`
    ({"token", "expires_at"})에 둔 것을 읽기만 한다.

    환경변수: DBHUB_REDIS_URL(없으면 REDIS_URL) — Railway 에선 `${{Redis.REDIS_URL}}`(DB 허브 Redis),
    DB_REST_BASE(기본 https://openapi.dbsec.co.kr:8443). CHARTDAY 4 TPS → 호출 간격 0.26초.
    종목 하나가 실패(응답 오류·그날 봉 없음)하면 그 종목만 빠지고, 토큰 문제는 DbHubPriceError.
    """
    PATH = "/api/v1/quote/kr-chart/day"          # CHARTDAY — 이 경로만 허용
    TOKEN_KEY = "db:token"
    MIN_GAP = 0.26                               # 4 TPS

    def __init__(self, redis_url=None, rest_base=None, *, redis_client=None, post=None,
                 sleep=time.sleep, now=time.time, log=print):
        self.redis_url = redis_url or os.environ.get("DBHUB_REDIS_URL") or os.environ.get("REDIS_URL")
        self.rest_base = (rest_base or os.environ.get("DB_REST_BASE") or "https://openapi.dbsec.co.kr:8443").rstrip("/")
        self._redis, self._post = redis_client, post
        self.sleep, self.now, self.log = sleep, now, log
        self._last = 0.0

    def _token(self):
        r = self._redis
        if r is None:
            if not self.redis_url:
                raise DbHubPriceError("DBHUB_REDIS_URL/REDIS_URL 없음 — DB 허브 Redis 를 참조해야 함")
            import redis                                  # 루트 requirements(# swing)
            r = self._redis = redis.Redis.from_url(self.redis_url, socket_timeout=5)
        try:
            raw = r.get(self.TOKEN_KEY)
        except Exception as ex:
            raise DbHubPriceError(f"DB 허브 Redis 읽기 실패: {type(ex).__name__}: {ex}") from ex
        if not raw:
            raise DbHubPriceError(f"Redis `{self.TOKEN_KEY}` 없음 — DB 허브가 토큰을 발급했는지 확인")
        try:
            d = json.loads(raw)
            tok, exp = str(d["token"]), float(d["expires_at"])
        except (ValueError, KeyError, TypeError) as ex:
            raise DbHubPriceError(f"`{self.TOKEN_KEY}` 형식 오류: {ex}") from ex
        if exp <= self.now() + 60:
            raise DbHubPriceError("DB 허브 토큰 만료(또는 1분 이내 만료) — 허브 재발급 대기")
        return tok

    def _call(self, token, code, ymd):
        gap = self.MIN_GAP - (self.now() - self._last)
        if gap > 0:
            self.sleep(gap)
        self._last = self.now()
        body = {"In": {"InputOrgAdjPrc": "0", "InputCondMrktDivCode": "J", "InputIscd1": code,
                       "InputDate1": ymd, "InputDate2": ymd}}
        headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token}",
                   "cont_yn": "N", "cont_key": ""}
        post = self._post
        if post is None:
            import requests
            post = requests.post
        r = post(self.rest_base + self.PATH, headers=headers, json=body, timeout=10)
        if getattr(r, "status_code", 200) != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        data = r.json()
        if str(data.get("rsp_cd")) != "00000":
            raise RuntimeError(f"{data.get('rsp_cd')} {data.get('rsp_msg')}")
        out = data.get("Out") or []
        return out if isinstance(out, list) else [out]

    @staticmethod
    def _num(v):
        try:
            x = float(str(v).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
        return abs(x) if x else None                     # 부호 붙은 값 방어, 0 = 없음

    def daily_bars(self, codes, date):
        if not codes:
            return {}
        token = self._token()
        ymd = date.replace("-", "")
        out = {}
        for c in codes:
            try:
                rows = self._call(token, c, ymd)
            except Exception as ex:
                self.log(f"[swing] dbhub {c} {date}: {ex}")
                continue
            row = next((x for x in rows if str(x.get("Date", "")).strip() == ymd), None)
            if not row:
                continue
            bar = {"open": self._num(row.get("Oprc")), "high": self._num(row.get("Hprc")),
                   "low": self._num(row.get("Lprc")), "close": self._num(row.get("Prpr"))}
            if all(bar.values()):
                out[c] = bar
        return out
