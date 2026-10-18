"""swing-livepm 진입점 — M1 에서 장중 루프를 구현한다. 지금은 설정만 출력하고 끝낸다.

  python -m swing.livepm
"""
from . import config


def main():
    keys = [k for k in dir(config) if k.isupper()]
    print("[livepm] M0: 장중 루프 미구현(PLAN_PM.md M1). 현재 설정:")
    for k in keys:
        print(f"  {k} = {getattr(config, k)!r}")


if __name__ == "__main__":
    main()
