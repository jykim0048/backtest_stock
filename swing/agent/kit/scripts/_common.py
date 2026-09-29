"""공통 유틸 — 경로, .env 로드, 실행 디렉터리, JSON I/O, 콘솔 인코딩.

이 스킬은 독립 실행된다. 수집 함수는 scripts/sources_kr.py·social_kr.py, 정적 마스터는
assets/ 에 들어 있어 다른 레포가 필요 없다.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

# Windows 콘솔(cp949)에서 한글·특수문자 출력이 깨지거나 예외를 내지 않도록 UTF-8 로 고정
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

KST = _dt.timezone(_dt.timedelta(hours=9))
SKILL_ROOT = Path(__file__).resolve().parent.parent          # trading_agent/
ASSETS_DIR = SKILL_ROOT / "assets"
RUNS_DIR = SKILL_ROOT / "runs"
MEMORY_DIR = SKILL_ROOT / "memory"
DECISION_LOG = MEMORY_DIR / "decision_log.md"


def today_kst() -> str:
    return _dt.datetime.now(KST).strftime("%Y-%m-%d")


def warn(msg: str) -> None:
    print(f"[trading_agent] {msg}", file=sys.stderr, flush=True)


def _load_dotenv_file(p: Path) -> None:
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ and v:
                os.environ[k] = v
    except Exception as e:  # noqa: BLE001
        warn(f".env 로드 실패(무시): {e}")


def load_env() -> None:
    """<skill>/.env 를 읽고, BACKTEST_STOCK_ROOT 가 지정돼 있으면 그쪽 .env 도 보조로 읽는다.
    이미 설정된 환경변수는 덮어쓰지 않는다."""
    _load_dotenv_file(SKILL_ROOT / ".env")
    extra = os.environ.get("BACKTEST_STOCK_ROOT")
    if extra:
        _load_dotenv_file(Path(extra) / ".env")


def run_dir(code: str, date: str | None = None) -> Path:
    d = RUNS_DIR / code / (date or today_kst())
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    return path


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
