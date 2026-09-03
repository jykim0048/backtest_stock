"""일회성 진단: Gemini 체인 모델별 429 쿼터 메트릭 확인 (확인 후 워크플로와 함께 제거).

배경(2026-09-03): gemini-3.5-flash 가 장중 회차에서 상시 429 RESOURCE_EXHAUSTED
(하루 1~2회만 성공, 성공 시각 산발적 — RPM 경합 심증). llm.py 는 429 원문을
code+status 로 축약해 어느 한도(분당 RPM / 일일 RPD / 토큰 TPM)인지 로그로 알 수 없다.

모델별 소형 generate_content 를 3회(20초 간격) 호출해 성공/에러 '원문'을 출력한다.
429 원문의 QuotaFailure details(quotaMetric·quotaValue)와 RetryInfo(retryDelay)로 판별:
  - *_per_day  메트릭 → 일일 한도 소진(리셋까지 지속)
  - *_per_minute / retryDelay 수십초 → 분당 한도 경합(간헐)
"""
import os
import sys
import time
import json

from google import genai
from google.genai import errors as gerr

MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview"]

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

for m in MODELS:
    for i in range(3):
        t0 = time.time()
        try:
            r = client.models.generate_content(model=m, contents="1+1=? 숫자만.")
            print(f"[{m}] try{i + 1}: OK {time.time() - t0:.1f}s -> {(r.text or '')[:40]!r}",
                  flush=True)
        except gerr.APIError as e:
            print(f"[{m}] try{i + 1}: code={getattr(e, 'code', None)} "
                  f"status={getattr(e, 'status', '')}", flush=True)
            print("   message:", str(getattr(e, "message", e))[:2500], flush=True)
            det = getattr(e, "details", None)
            if det:
                try:
                    print("   details:", json.dumps(det, ensure_ascii=False)[:2500],
                          flush=True)
                except Exception:
                    print("   details(raw):", str(det)[:2500], flush=True)
        except Exception as e:
            print(f"[{m}] try{i + 1}: unexpected {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
        time.sleep(20)
