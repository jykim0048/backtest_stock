"""과거 신호 복원(4단계) — main git 이력의 public/weekly_briefing.json 커밋들에서 영업일별
장 마감 후 스냅샷(asof ≥ 16:00 KST 중 가장 이른 것 — 16:30 크론이 봤을 값)을 뽑아
swing/data/wb_history/<date>.json(signals.compact 축약본)으로 저장. 로컬 git 만 사용(네트워크 없음).

  python swing/tools/extract_wb_history.py --since 2026-09-06 [--ref main]
"""
import argparse
import json
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
from swing import signals, tradedays  # noqa: E402

PATH = "public/weekly_briefing.json"


def git(*args):
    return subprocess.run(["git", *args], cwd=_ROOT, capture_output=True, check=True).stdout


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-06")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--out", default=os.path.join(_ROOT, "swing", "data", "wb_history"))
    a = ap.parse_args(argv)

    shas = git("log", a.ref, f"--since={a.since}", "--format=%H", "--", PATH).decode().split()
    best = {}                                   # date -> (asof, sha, compact)
    for sha in reversed(shas):                  # 오래된 것부터
        try:
            wb = json.loads(git("show", f"{sha}:{PATH}").decode("utf-8"))
        except Exception as ex:
            print(f"skip {sha[:8]}: {ex}", file=sys.stderr)
            continue
        asof = str(wb.get("asof") or "")
        m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})", asof)
        if not m or not tradedays.is_trading_day(m.group(1)) or int(m.group(2)) < 16:
            continue
        if not (wb.get("sectorScreen") or {}).get("matrix"):
            continue
        d = m.group(1)
        if d not in best or asof < best[d][0]:     # 16:00 이후 가장 이른 스냅샷 = 16:30 크론이 봤을 값
            best[d] = (asof, sha, signals.compact(wb))

    os.makedirs(a.out, exist_ok=True)
    idx = []
    for d in sorted(best):
        asof, sha, comp = best[d]
        comp["sourceCommit"] = sha[:10]
        with open(os.path.join(a.out, f"{d}.json"), "w", encoding="utf-8") as f:
            json.dump(comp, f, ensure_ascii=False, indent=1)
        n_buy = len(signals.buy_candidates(comp))
        n_down = len(signals.down_codes(comp))
        idx.append({"date": d, "asof": asof, "commit": sha[:10], "buy": n_buy, "down": n_down})
        print(f"{d}  asof {asof}  {sha[:8]}  매수후보 {n_buy}  하방 {n_down}")
    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    miss = [d for d in tradedays.trading_days(a.since, max(best) if best else a.since) if d not in best]
    if miss:
        print("스냅샷 없는 영업일:", ", ".join(miss))


if __name__ == "__main__":
    main()
