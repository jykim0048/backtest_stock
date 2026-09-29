# 출처 — homework `trading_agent` 스킬

- 레포 `jykim0048/homework`, 브랜치 `trading_agent`, 커밋 **`951c796`**(2026-09-29 개인 PC 푸시 — 9/22 로컬 실측 반영).
- 복사일 2026-09-29(회사 PC). `swing/agent/__init__.py` 의 `SOURCE_SHA` 와 같아야 한다(판단 결과 `agent` 필드에 기록).

## 복사한 파일 (`kit/`, 원본 폴더 구조 그대로 · **바이트 동일, 무수정**)
| kit 경로 | 원본 |
|---|---|
| `scripts/_common.py` `collect.py` `dart_fin.py` `indicators.py` `peers_resolve.py` `resolve.py` `social_kr.py` `sources_kr.py` | `trading_agent/scripts/` |
| `assets/` (dart_corp_map · industry_peers · krx_companies · peers) | `trading_agent/assets/` |
| `references/` (kr_market_rules · output_schemas · roles/01~14) | `trading_agent/references/` |

- 제외: `scripts/decision_log.py`·`outcome.py`(반성 루프 — P1 범위 밖), `tests/`, `memory/`, `runs/`, `.env`.
  `roles/14_reflector.md` 는 복사만 하고 쓰지 않는다.
- 원본 bare import(`from _common import …`)를 살리려고 `kit/scripts` 를 sys.path 에 올린다(`ensure_kit_path`).
- `kit/memory/`·`kit/runs/` 는 실행 중 생성(gitignore). `memory/peers_dynamic.json` 은 `peers.py` 가 판단 직전
  저장소(`peers/dynamic`)에서 내려쓰는 사본.

## 원본과 다른 동작 (kit 밖, `pipeline.py`·`peers.py`)
- 서브에이전트·대화 대신 LLM 호출(루트 `llm.py`), 역할 출력은 JSON `{"markdown"}` 한 겹 래핑.
- 해외 peer 제안: Claude 대화 → LLM 1콜(+재제안 1회), 캐시는 swing 저장소(HANDOFF 3-6).
- 스윙 전략 맥락을 RM·Trader·PM user 프롬프트 앞에 추가(역할 파일 무수정). 과거 맥락 = "과거 맥락 없음".
- ohlcv.csv 는 최근 60거래일만 첨부, 입력 파일은 `SWING_AGENT_MAX_INPUT_CHARS`(15만 자)에서 자름.

## 재동기화
원본이 바뀌면: kit 을 원본 최신으로 통째 덮어쓰기(위 제외 목록 유지) → `SOURCE_SHA`·이 문서 sha 갱신 →
`python -m unittest discover -s swing/tests -t .`. 역할 파일 제목 줄(첫 줄)이 바뀌면 테스트의 역할 식별도 따라간다.
