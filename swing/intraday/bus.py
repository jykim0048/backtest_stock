"""장중 시세 버스(Redis) — Live Trader 가 받은 시세·VWAP 을 발행하고 Live PM 이 구독한다(2026-10-18, PLAN_LIVE_TRADER §5).

KIS 허브를 두 서비스가 따로 폴링하지 않고, Live Trader(swing-vwap)가 폴링한 결과를 그대로 나눠 쓴다.
Redis = 같은 Railway 프로젝트의 `Redis`(swing-cron 이 db:token 을 읽는 그 Redis). 키는 모두 `livetrader:` 아래.

  livetrader:ticks      Stream — 폴링 1회 = 항목 1개 {date, ts, data: JSON[{Tick 필드…, vwap, sigma, src}]}, 약 STREAM_MAXLEN 개 유지
  livetrader:last       Hash   — code → 최신 항목 JSON
  livetrader:heartbeat  String — {ts, date, codes} JSON, TTL HEARTBEAT_TTL 초(만료 = Live Trader 멈춤)

발행(PriceBus)은 실패해도 예외를 내지 않는다 — 집행·손절 루프를 절대 막지 않는다. 연결은 REDIS_URL(없으면 꺼짐).
"""
import json
import os
import time

from .feed import Tick

STREAM = "livetrader:ticks"
LAST = "livetrader:last"
HEARTBEAT = "livetrader:heartbeat"
STREAM_MAXLEN = 4000          # 하루 약 1,560회(15초) → 2일 남짓
HEARTBEAT_TTL = 120


def _connect(url):
    import redis                                   # 루트 requirements(# swing)
    return redis.Redis.from_url(url, socket_timeout=5, decode_responses=True)


def _row(t, st):
    d = t.to_dict()
    if st is not None:
        d.update(vwap=st.vwap, sigma=st.sigma, src=st.vwap_source)
    return d


class PriceBus:
    """Live Trader 쪽 발행기. client 를 주입하면 테스트에서 네트워크 없이 돈다."""

    def __init__(self, url=None, *, client=None, log=print, retry_s=60):
        self.url = url
        self._r, self.log, self.retry_s = client, log, retry_s
        self.enabled = bool(client is not None or url)
        self.errors = 0
        self._down_until = 0.0
        if not self.enabled:
            log("[bus] REDIS_URL 없음 — 시세 버스 발행 끔")

    @classmethod
    def from_env(cls, log=print):
        return cls(os.environ.get("LIVETRADER_REDIS_URL") or os.environ.get("REDIS_URL"), log=log)

    def _client(self):
        if self._r is None:
            self._r = _connect(self.url)
        return self._r

    def _safe(self, fn):
        if not self.enabled or time.monotonic() < self._down_until:
            return False
        try:
            fn(self._client())
            return True
        except Exception as ex:
            self.errors += 1
            self._down_until = time.monotonic() + self.retry_s
            self._r = None
            self.log(f"[bus] Redis 발행 실패({type(ex).__name__}: {str(ex)[:80]}) — {self.retry_s:.0f}초 뒤 재시도, 집행은 계속")
            return False

    def publish(self, date, ts, ticks, states=None):
        """폴링 1회분 발행. states = {code: VwapState}."""
        if not ticks:
            return False
        rows = [_row(t, (states or {}).get(t.code)) for t in ticks]

        def go(r):
            r.xadd(STREAM, {"date": date, "ts": ts, "data": json.dumps(rows, ensure_ascii=False)},
                   maxlen=STREAM_MAXLEN, approximate=True)
            r.hset(LAST, mapping={x["code"]: json.dumps(x, ensure_ascii=False) for x in rows})
        return self._safe(go)

    def heartbeat(self, date, ts, codes):
        return self._safe(lambda r: r.set(HEARTBEAT, json.dumps({"date": date, "ts": ts, "codes": list(codes)}),
                                          ex=HEARTBEAT_TTL))


def _decode(entry_id, fields):
    rows = json.loads(fields.get("data") or "[]")
    ticks = [Tick.from_dict(x) for x in rows]
    extra = {x["code"]: {"vwap": x.get("vwap"), "sigma": x.get("sigma"), "src": x.get("src")} for x in rows}
    return {"id": entry_id, "date": fields.get("date"), "ts": fields.get("ts"), "ticks": ticks, "vwap": extra}


class BusFeed:
    """Live PM 쪽 구독기 — 오늘 기록 따라잡기(history) + 새 항목 대기(poll)."""

    def __init__(self, url=None, *, client=None):
        self._r = client if client is not None else _connect(url or os.environ.get("LIVETRADER_REDIS_URL")
                                                             or os.environ["REDIS_URL"])
        self.last_id = "0-0"

    def history(self, date):
        """오늘(date) 항목 전부를 시간 순으로. 읽은 마지막 id 부터 poll 이 이어진다."""
        out = []
        for entry_id, fields in self._r.xrange(STREAM, "-", "+"):
            self.last_id = entry_id
            if fields.get("date") == date:
                out.append(_decode(entry_id, fields))
        return out

    def poll(self, block_ms=20000, count=100):
        """last_id 이후 새 항목(최대 block_ms 대기). 반환 [항목]."""
        res = self._r.xread({STREAM: self.last_id}, count=count, block=block_ms) or []
        out = []
        for _name, entries in res:
            for entry_id, fields in entries:
                self.last_id = entry_id
                out.append(_decode(entry_id, fields))
        return out

    def last(self):
        return {k: json.loads(v) for k, v in (self._r.hgetall(LAST) or {}).items()}

    def alive(self):
        """Live Trader 생존 신호 — 없으면 None(만료 = 멈춤으로 본다, Live PM X4)."""
        v = self._r.get(HEARTBEAT)
        return json.loads(v) if v else None
