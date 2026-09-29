"""DbHubPrices 오프라인 테스트 — 가짜 Redis·HTTP(네트워크 없음)."""
import json
import unittest

from swing import prices as P
from swing import run_daily


class FakeRedis:
    def __init__(self, val=None, boom=False):
        self.val, self.boom, self.keys = val, boom, []

    def get(self, k):
        self.keys.append(k)
        if self.boom:
            raise ConnectionError("down")
        return self.val


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class FakePost:
    """code → 응답 dict(또는 예외)."""
    def __init__(self, by_code):
        self.by_code, self.calls = by_code, []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, headers, json))
        r = self.by_code[json["In"]["InputIscd1"]]
        if isinstance(r, Exception):
            raise r
        return r


NOW = 1_800_000_000.0


def token(exp=NOW + 3600):
    return json.dumps({"token": "T0K", "expires_at": exp})


def ok(rows):
    return Resp({"rsp_cd": "00000", "rsp_msg": "정상", "Out": rows})


def row(date="20260929", o="10000", h="10300", l="9900", c="10200"):
    return {"Date": date, "Hour": "", "Oprc": o, "Hprc": h, "Lprc": l, "Prpr": c, "CntgVol": "1000"}


def px(redis_val, post, **kw):
    return P.DbHubPrices(redis_client=FakeRedis(redis_val, **kw), post=post, rest_base="https://x:8443",
                         sleep=lambda s: None, now=lambda: NOW, log=lambda *a: None)


class TestDbHubPrices(unittest.TestCase):
    def test_bars_and_request_shape(self):
        post = FakePost({"005930": ok([row()]), "000660": ok([row(o="200,000", h="205000", l="198000", c="-204000")])})
        bars = px(token(), post).daily_bars(["005930", "000660"], "2026-09-29")
        self.assertEqual(bars["005930"], {"open": 10000, "high": 10300, "low": 9900, "close": 10200})
        self.assertEqual(bars["000660"]["open"], 200000)           # 콤마·부호 방어
        self.assertEqual(bars["000660"]["close"], 204000)
        url, headers, body = post.calls[0]
        self.assertEqual(url, "https://x:8443/api/v1/quote/kr-chart/day")   # CHARTDAY 경로만
        self.assertEqual(headers["authorization"], "Bearer T0K")
        self.assertEqual(body["In"], {"InputOrgAdjPrc": "0", "InputCondMrktDivCode": "J", "InputIscd1": "005930",
                                      "InputDate1": "20260929", "InputDate2": "20260929"})
        self.assertTrue(all(c[0].endswith("/kr-chart/day") for c in post.calls))

    def test_skip_other_date_zero_and_errors_per_code(self):
        post = FakePost({"A": ok([row(date="20260928")]),                       # 그날 봉 없음(휴장·정지)
                         "B": ok([row(o="0")]),                                  # 값 0 → 제외
                         "C": Resp({"rsp_cd": "IGW00201", "rsp_msg": "TPS"}),     # 응답 오류
                         "D": ConnectionError("net"),                            # 네트워크
                         "E": Resp({}, status=500),
                         "F": ok(row())})                                        # Out 이 dict 단건
        bars = px(token(), post).daily_bars(list("ABCDEF"), "2026-09-29")
        self.assertEqual(set(bars), {"F"})

    def test_token_problems_raise(self):
        post = FakePost({})
        for val, kw, msg in ((None, {}, "없음"), ("{bad", {}, "형식"), (token(exp=NOW + 30), {}, "만료"),
                             (token(), {"boom": True}, "Redis 읽기 실패")):
            with self.assertRaises(P.DbHubPriceError) as cm:
                px(val, post, **kw).daily_bars(["005930"], "2026-09-29")
            self.assertIn(msg, str(cm.exception))
        self.assertEqual(post.calls, [])

    def test_no_codes_no_token_read(self):
        r = FakeRedis(None)
        p = P.DbHubPrices(redis_client=r, post=FakePost({}))
        self.assertEqual(p.daily_bars([], "2026-09-29"), {})
        self.assertEqual(r.keys, [])

    def test_rate_limit_gap(self):
        t = {"now": NOW}
        slept = []

        def sleep(s):
            slept.append(s)
            t["now"] += s
        p = P.DbHubPrices(redis_client=FakeRedis(token()), post=FakePost({"A": ok([row()]), "B": ok([row()])}),
                          sleep=sleep, now=lambda: t["now"], log=lambda *a: None)
        p.daily_bars(["A", "B"], "2026-09-29")
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], P.DbHubPrices.MIN_GAP)

    def test_missing_redis_url(self):
        p = P.DbHubPrices(redis_url="", post=FakePost({}))
        p.redis_url = None
        with self.assertRaises(P.DbHubPriceError):
            p.daily_bars(["005930"], "2026-09-29")

    def test_run_daily_factory(self):
        self.assertIsInstance(run_daily.make_prices("dbhub"), P.DbHubPrices)


if __name__ == "__main__":
    unittest.main()
