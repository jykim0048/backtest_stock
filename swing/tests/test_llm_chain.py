"""llm 폴백 체인 — 1순위 5xx 인내 · 429/404 링크 건너뛰기(2026-10-18)."""
import os
import unittest
from unittest import mock

import llm


class ChainTest(unittest.TestCase):
    def setUp(self):
        llm._DEAD.clear()
        llm._UNAVAIL_UNTIL.clear()
        self.calls = []
        self.sleeps = []
        p1 = mock.patch.dict(os.environ, {"LLM_CHAIN": "gemini:a,gemini:b,gemini:c"})
        p2 = mock.patch.object(llm.time, "sleep", side_effect=self.sleeps.append)
        p3 = mock.patch.object(llm, "UNAVAIL_WAITS", [30.0, 60.0])
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    def _adapter(self, script):
        """script: model -> 응답 목록(예외 또는 텍스트), 호출마다 앞에서 하나씩 소비(마지막 값 반복)."""
        def fn(model, system, user, max_tokens, schema):
            self.calls.append(model)
            seq = script[model]
            r = seq.pop(0) if len(seq) > 1 else seq[0]
            if isinstance(r, Exception):
                raise r
            return r
        return mock.patch.dict(llm._ADAPTERS, {"gemini": fn})

    def test_primary_5xx_waits_then_recovers(self):
        s = {"a": [llm._Retryable("503", 503)] * 4 + ['{"ok": 1}'], "b": ['{"ok": 2}']}
        with self._adapter(s):
            self.assertEqual(llm.generate_json("s", "u"), {"ok": 1})
        self.assertEqual(self.calls, ["a"] * 5)          # 짧은 재시도 3 + 긴 대기 2
        self.assertEqual(self.sleeps[-2:], [30.0, 60.0])

    def test_primary_5xx_cooldown_skips_long_waits(self):
        s = {"a": [llm._Retryable("503", 503)], "b": ['{"ok": 2}']}
        with self._adapter(s):
            self.assertEqual(llm.generate_json("s", "u"), {"ok": 2})
            n = len(self.sleeps)
            self.calls.clear()
            self.assertEqual(llm.generate_json("s", "u"), {"ok": 2})
        self.assertEqual(self.calls, ["a"] * 3 + ["b"])  # 쿨다운 중이라 긴 대기 없이 폴백
        self.assertNotIn(30.0, self.sleeps[n:])

    def test_fallback_5xx_no_long_wait(self):
        s = {"a": [llm._SwitchLink("x")], "b": [llm._Retryable("503", 503)], "c": ['{"ok": 3}']}
        with self._adapter(s):
            self.assertEqual(llm.generate_json("s", "u"), {"ok": 3})
        self.assertNotIn(30.0, self.sleeps)

    def test_429_link_skipped_afterwards(self):
        s = {"a": [llm._SwitchLink("x")], "b": [llm._Retryable("429", 429)], "c": ['{"ok": 3}']}
        with self._adapter(s):
            llm.generate_json("s", "u")
            self.calls.clear()
            llm.generate_json("s", "u")
        self.assertEqual(self.calls, ["a", "c"])         # b 는 소진 표시로 건너뜀

    def test_all_fail_message_lists_skipped(self):
        llm._DEAD[("gemini", "b")] = (float("inf"), "404")
        s = {"a": [llm._SwitchLink("x")], "c": [llm._SwitchLink("y")]}
        with self._adapter(s):
            with self.assertRaises(llm.LLMError) as cm:
                llm.generate_json("s", "u")
        self.assertIn("gemini:b skipped (404", str(cm.exception))

    def test_default_chain_has_no_retired_models(self):
        with mock.patch.dict(os.environ, {"LLM_CHAIN": ""}):
            ms = [m for _, m in llm._chain()]
        self.assertNotIn("gemini-2.5-flash", ms)
        self.assertEqual(ms[0], "gemini-3.1-flash-lite")


if __name__ == "__main__":
    unittest.main()
