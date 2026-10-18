"""장중 시세 공급 — 허브 /prices 폴링(PollingFeed)·저장된 틱 재생(ReplayFeed). 표준 라이브러리만 사용.

허브 /prices = hub._fetch_prices → KIS inquire-price(FHKST01010100) 종목별 조회(7초 캐시).
응답 {code: {price, rate, volume, value, open, high, low, prevClose}} — value(누적거래대금)·high/low 는 2026-10-18 추가,
구 허브면 없을 수 있다(None).
"""
import datetime
import json
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Optional

from .. import config as swing_config

KST = datetime.timezone(datetime.timedelta(hours=9))


def now_kst():
    return datetime.datetime.now(KST)


@dataclass
class Tick:
    code: str
    ts: str                         # KST ISO(초 단위) "2026-10-19T09:31:15+09:00"
    price: float
    volume: float                   # 당일 누적거래량
    value: Optional[float] = None   # 당일 누적거래대금(원) — 없으면 VWAP 근사
    high: Optional[float] = None    # 당일 고가
    low: Optional[float] = None     # 당일 저가
    open: Optional[float] = None
    prev_close: Optional[float] = None

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Tick(**{k: d.get(k) for k in Tick.__dataclass_fields__})

    @property
    def dt(self):
        return datetime.datetime.fromisoformat(self.ts)


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def parse_prices(body, ts):
    """/prices 응답 → [Tick]. 가격 0(거래정지·장전) 종목은 버린다."""
    out = []
    for code, s in ((body or {}).get("stocks") or {}).items():
        px = _num(s.get("price"))
        if px is None:
            continue
        out.append(Tick(code=code, ts=ts, price=px, volume=float(s.get("volume") or 0), value=_num(s.get("value")),
                        high=_num(s.get("high")), low=_num(s.get("low")), open=_num(s.get("open")),
                        prev_close=_num(s.get("prevClose"))))
    return out


class PollingFeed:
    """codes 를 chunk 개씩 나눠 /prices 조회. get(url) → dict 를 주입하면 테스트에서 네트워크 없이 돈다."""

    def __init__(self, base=None, *, get=None, chunk=30, timeout=15, clock=now_kst, log=print):
        self.base = (base or swing_config.FLOW_API_BASE).rstrip("/")
        self._get, self.chunk, self.timeout, self.clock, self.log = get, chunk, timeout, clock, log
        self.errors = 0

    def _fetch(self, url):
        if self._get:
            return self._get(url)
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def poll(self, codes):
        ts = self.clock().replace(microsecond=0).isoformat()
        codes = sorted(set(codes))
        out = []
        for i in range(0, len(codes), self.chunk):
            part = codes[i:i + self.chunk]
            url = f"{self.base}/prices?" + urllib.parse.urlencode({"codes": ",".join(part)})
            try:
                out += parse_prices(self._fetch(url), ts)
            except Exception as ex:
                self.errors += 1
                self.log(f"[feed] /prices 실패({type(ex).__name__}: {str(ex)[:80]})")
        return out


class ReplayFeed:
    """저장된 틱 → 시각별 묶음으로 재생. batches() = [(ts, [Tick…])] 시각 순."""

    def __init__(self, ticks):
        by = {}
        for t in ticks:
            t = t if isinstance(t, Tick) else Tick.from_dict(t)
            by.setdefault(t.ts, []).append(t)
        self._batches = sorted(by.items())

    def batches(self):
        return list(self._batches)
