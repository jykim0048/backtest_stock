"""report.txt → 공개 주석(::notice::) — Actions 로그·요약은 로그인해야 보이지만 주석은 공개(krx_companies.yml).

주석 1개는 약 4KB 에서 잘리고 단계당 notice 10개 제한 → 이스케이프 후 3.6KB 조각으로 나눠
인자 offset 부터 10개를 출력한다(워크플로가 offset 0·10·20 세 단계로 호출)."""
import sys

off = int(sys.argv[1]) if len(sys.argv) > 1 else 0
try:
    s = open("report.txt", encoding="utf-8", errors="replace").read()
except FileNotFoundError:
    s = "(report.txt 없음)"


def esc(x):
    return x.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


chunks, cur = [], ""
for ln in s.splitlines():
    if cur and len(esc(cur + ln + "\n").encode("utf-8")) > 3600:
        chunks.append(cur)
        cur = ""
    cur += ln + "\n"
if cur:
    chunks.append(cur)
for i, c in enumerate(chunks[off:off + 10], off + 1):
    print(f"::notice title=krx_companies report {i}/{len(chunks)}::{esc(c)}")
