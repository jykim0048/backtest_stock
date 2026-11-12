"""당일 등락 요인 분해(swing/agent/price_factors.py) + 파이프라인 연결 — 오프라인."""
import json
import os
import unittest

from swing.agent import pipeline as PL
from swing.agent import price_factors as F
from swing.tests.test_agent_pipeline import FakeLLM, agent, fake_collect, req

PRICE = {"status": "ok", "latest_row": "2026-09-28", "latest_ohlcv": {"Close": 45000}, "returns_pct": {"1d": 2.27}}


def it(cat, d, st, title="t"):
    return {"cat": cat, "title": title, "dir": d, "strength": st, "evidence": ""}


class TestRules(unittest.TestCase):
    def test_basis_from_price_snapshot(self):
        b = F.basis(PRICE, "2026-09-28")
        self.assertEqual((b["date"], b["ratePct"], b["close"], b["kind"]), ("2026-09-28", 2.27, 45000, "day"))
        self.assertAlmostEqual(b["prevClose"], 45000 / 1.0227, places=1)
        self.assertEqual(F.basis(PRICE, "2026-09-29")["kind"], "last")             # 최신 행이 판단일보다 앞
        self.assertIsNone(F.basis({"latest_row": "2026-09-28"}, "2026-09-28"))     # 등락률 없음
        self.assertIsNone(F.basis(None, "2026-09-28"))

    def test_clean_drops_bad_rows_dedups_and_caps(self):
        raw = [{"title": "외국인 순매수 50억", "direction": "up", "strength": "4", "evidence": "4/5 외국인"},
               {"title": "외국인 순매수 50억", "direction": "up", "strength": "2"},    # 중복
               {"title": "", "direction": "up", "strength": "3"},                   # 제목 없음
               {"title": "방향 틀림", "direction": "sideways", "strength": "3"},
               {"title": "강도 이상", "direction": "down", "strength": "9"},
               {"title": "a", "direction": "neutral", "strength": "x"},
               {"title": "b", "direction": "up", "strength": "1"}, "문자열"]
        out = F.clean(raw, "flow")
        self.assertEqual([x["title"] for x in out], ["외국인 순매수 50억", "강도 이상", "a", "b"])
        self.assertEqual([x["strength"] for x in out], [4, 5, 2, 1])
        self.assertEqual({x["cat"] for x in out}, {"flow"})
        many = [{"title": f"t{i}", "direction": "up", "strength": str(1 + i % 5)} for i in range(9)]
        self.assertEqual(len(F.clean(many, "news")), F.MAX_PER_CAT)
        self.assertEqual(F.clean(None, "news"), [])

    def test_attribute_sums_to_rate_and_splits_by_strength(self):
        rows = F.attribute([it("flow", 1, 3), it("news", 1, 1), it("market", -1, 2)], 2.0)
        self.assertAlmostEqual(sum(x["pp"] for x in rows), 2.0, places=6)
        up = [x["pp"] for x in rows if x["dir"] > 0]
        self.assertAlmostEqual(up[0] / up[1], 3.0, delta=0.15)
        self.assertLess(next(x["pp"] for x in rows if x["dir"] < 0), 0)

    def test_attribute_missing_direction_goes_to_etc(self):
        rows = F.attribute([it("flow", 1, 3)], -1.5)                       # 내렸는데 하락 요인이 없음
        etc = [x for x in rows if x["cat"] == "etc"]
        self.assertEqual(len(etc), 1)
        self.assertLess(etc[0]["pp"], 0)
        self.assertAlmostEqual(sum(x["pp"] for x in rows), -1.5, places=6)

    def test_attribute_matches_main_rule(self):
        # main analysis/price_factors.attribute 와 같은 값(2026-11-11 구현의 대표 사례)
        rows = F.attribute([it("a", 1, 4), it("b", 1, 2), it("c", -1, 3), it("d", 0, 1)], 3.4)
        self.assertEqual([x["pp"] for x in rows], [4.5, 2.3, -3.4, 0.0])

    def test_build(self):
        b = F.basis(PRICE, "2026-09-28")
        raw = {"flow": [{"title": "기관 순매수", "direction": "up", "strength": "4", "evidence": "e"}],
               "market": [{"title": "RSI 과열", "direction": "down", "strength": "2", "evidence": "e"}]}
        pf = F.build(raw, b)
        self.assertEqual(pf["basis"]["ratePct"], 2.27)
        self.assertAlmostEqual(sum(pf["byCat"].values()), 2.3, places=6)            # 반올림 합 = 등락률(소수 1자리)
        self.assertTrue(pf["estimated"])
        self.assertIsNone(F.build({}, b))
        self.assertIsNone(F.build(raw, None))


class FactorLLM(FakeLLM):
    """애널리스트에게 factors 를 돌려주는 가짜 LLM — 스키마·프롬프트도 기록."""
    def __init__(self, factors=None, **kw):
        super().__init__(**kw)
        self.factors, self.schemas = factors, {}

    def __call__(self, system, user, max_tokens, schema):
        data, model = super().__call__(system, user, max_tokens, schema)
        role = self.role_of(system)
        self.schemas[role] = schema
        if "factors" in (schema.get("properties") or {}) and self.factors is not None:
            data = dict(data, factors=self.factors(role))
        return data, model


def collect_with_price(price=PRICE):
    base = fake_collect()

    def fn(stock, date, out, social_on):
        m = base(stock, date, out, social_on)
        with open(os.path.join(out, "01_price.json"), "w", encoding="utf-8") as f:
            json.dump(price, f)
        return m
    return fn


class TestPipelineFactors(unittest.TestCase):
    def setUp(self):
        self._on = F.ON
        F.ON = True

    def tearDown(self):
        F.ON = self._on

    def test_analysts_get_factor_schema_and_decision_carries_factors(self):
        llm = FactorLLM(lambda role: [{"title": f"{role} 요인", "direction": "up" if role < "03" else "down",
                                       "strength": "3", "evidence": "근거"}])
        d = agent(llm=llm, collect=collect_with_price()).decide(req())
        self.assertIsNone(d.error)
        analysts = [r for _k, r, _f in PL.ANALYSTS]
        for role in analysts:
            self.assertIs(llm.schemas[role], F.SCHEMA)
        self.assertIs(llm.schemas["09_trader.md"], PL.MD_SCHEMA)                    # 다른 역할은 그대로
        first = next(u for r, u in llm.calls if r == analysts[0])
        self.assertIn("당일 등락 요인", first)
        self.assertIn("+2.27%", first)
        pf = d.price_factors
        self.assertEqual(sorted({x["cat"] for x in pf["items"]}), sorted(k for k, _r, _f in PL.ANALYSTS))
        self.assertAlmostEqual(sum(x["pp"] for x in pf["items"]), 2.3, places=6)
        self.assertEqual(d.to_dict()["price_factors"], pf)

    def test_missing_or_broken_factors_keep_decision(self):
        plain = agent(collect=collect_with_price()).decide(req())                  # factors 안 줌
        self.assertIsNone(plain.price_factors)
        broken = agent(llm=FactorLLM(lambda role: "고장"), collect=collect_with_price()).decide(req())
        self.assertIsNone(broken.price_factors)
        for d in (plain, broken):
            self.assertIsNone(d.error)
            self.assertEqual((d.rating, d.entry), ("Overweight", 44500))

    def test_switch_off_restores_plain_schema(self):
        F.ON = False
        llm = FactorLLM(lambda role: [{"title": "x", "direction": "up", "strength": "3", "evidence": ""}])
        d = agent(llm=llm, collect=collect_with_price()).decide(req())
        self.assertTrue(all(s is PL.MD_SCHEMA for s in llm.schemas.values()))
        self.assertIsNone(d.price_factors)


if __name__ == "__main__":
    unittest.main()
