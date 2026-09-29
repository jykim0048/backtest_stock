"""대시보드 확인용 데모 데이터 생성 — `python swing/server.py --demo` 가 자동 호출.

swing/data/demo/ (git 제외)에 ① mock 에이전트·mock 시세로 9/9~9/28 백필(과거 신호 swing/data/wb_history)
② 테스트 Short(seed_demo_short) 를 만든다. 네트워크·API 키·패키지 설치 불필요(표준 라이브러리만).
실제 로컬 실행 결과(swing/data/local)·Postgres 와 섞이지 않는다.

  python swing/tools/demo_data.py            # 없을 때만 생성
  python swing/tools/demo_data.py --rebuild  # 지우고 다시 생성
"""
import argparse
import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DEMO_DIR = os.path.join(_ROOT, "swing", "data", "demo")
FROM, TO = "2026-09-09", "2026-09-28"


def build(rebuild=False, log=print):
    """데모 원장이 없거나 rebuild 면 생성. 반환: 데모 폴더 경로."""
    if not rebuild and os.path.exists(os.path.join(DEMO_DIR, "ledger.json")):
        log(f"[demo] 기존 데모 데이터 사용: {DEMO_DIR}")
        return DEMO_DIR
    if os.path.isdir(DEMO_DIR):
        shutil.rmtree(DEMO_DIR)
    from swing import run_daily
    from swing.tools import seed_demo_short
    log(f"[demo] 데모 데이터 생성: {FROM} ~ {TO} (mock 에이전트·mock 시세) → {DEMO_DIR}")
    dsn = os.environ.pop("DATABASE_URL", None)      # 데모는 항상 로컬 파일(DB 에 쓰지 않음)
    try:
        run_daily.main(["--from", FROM, "--to", TO, "--wb-dir", os.path.join(_ROOT, "swing", "data", "wb_history"),
                        "--agent", "mock", "--prices", "mock", "--store", "file:" + DEMO_DIR])
        seed_demo_short.main(["--store", DEMO_DIR])
    finally:
        if dsn is not None:
            os.environ["DATABASE_URL"] = dsn
    return DEMO_DIR


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    build(ap.parse_args().rebuild)
