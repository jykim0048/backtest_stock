"""trading_agent 헤드리스 이식(P1). 원본 = homework 레포 `trading_agent` 브랜치(SOURCE.md).

kit/ 는 원본 스킬 폴더 구조 그대로(scripts·assets·references, 코드 무수정) — 원본의 bare import
(`from _common import …`)를 살리려고 kit/scripts 를 sys.path 에 올려 쓴다.
"""
import os
import sys

SOURCE_SHA = "951c796"
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
KIT_DIR = os.path.join(AGENT_DIR, "kit")
KIT_SCRIPTS = os.path.join(KIT_DIR, "scripts")


def ensure_kit_path():
    if KIT_SCRIPTS not in sys.path:
        sys.path.insert(0, KIT_SCRIPTS)
