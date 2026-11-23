---
name: task-metrics
description: 의미 있는 개발 작업의 확인된 사용량·검증·재작업을 로컬에 기록하거나 작업별 기록을 비교한다.
---

# 작업별 효율 기록

Claude·Codex가 같은 결정적 CLI를 사용한다. 별도 AI 호출·훅·계정/세션 로그 자동 검색 없이 기존 실행 결과와 사용자가 제공한 수치만 기록한다. 기록은 측정 자료이며 모델·추론·필수 검증을 바꾸는 기준이 아니다.

## 기록

프로젝트 지침이 지정한 의미 있는 작업 완료 시 **메인만 1건** 기록한다. 짧은 질의/사소한 편집은 생략한다. 교차검증은 생성된 summary를 실행별로 한 번 가져온다. 기존 검증 호출과 함께 실행할 수 있고 기록 때문에 테스트/리뷰를 반복하지 않는다.

```bash
{{PY}} "{{HARNESS_HOME}}/tools/task_metrics/task_metrics.py" record --task feature-example --run codex-main-001 --provider codex --workflow single --kind feature --verification passed
```

- task: 비교할 작업의 비밀정보 없는 짧은 식별자. 같은 작업의 Claude/Codex는 공유한다. run: provider 내에서 고유한 실행 ID(예: 날짜+작업+주 실행/교차검토 순번). 중복 오류는 새 ID로 우회하지 말고 기존 기록을 확인한다.
- `--workflow`: `single`, `review`, `debate`, `test-plan`, `code-review`. (`independent`는 폐기됐으며 기존 기록 읽기용으로만 남는다.) `--kind`: `feature`, `bugfix`, `refactor`, `config`.
- 확인한 수치만 `--input-tokens`, `--output-tokens`, `--cached-input-tokens`, `--elapsed-seconds`, `--rework-count`로 넣는다. 생략하면 null이며 미확인을 0으로 쓰지 않는다. `--source provider-report|user-report|unknown`으로 출처를 구분한다. 모델이 사용량/시간을 추정하거나 usage 비율을 토큰으로 환산하지 않는다.
- input은 **캐시를 포함한 전체 입력 토큰**, cached는 그 부분집합이다. 제공된 통계가 캐시를 별도로 집계하면 필드 의미를 확인하고 전체 입력으로 정규화한 값만 기록한다. 의미가 불명확하면 생략한다. 캐시를 전체 입력에 다시 더하지 않는다.
- rework는 확인된 결함·누락 때문에 구현을 고쳐 재검증한 횟수다. 최초 구현/검증이나 사용자 범위 추가는 포함하지 않는다. 모르면 생략한다. verification은 `passed|failed|not-run|unknown`; 교차검토 COMPLETE를 테스트 passed로 바꾸지 않는다.

교차검증의 **기존 summary.json만** 가져오기:

```bash
{{PY}} "{{HARNESS_HOME}}/tools/task_metrics/task_metrics.py" record --task feature-example --run codex-cross-001 --provider codex --workflow test-plan --kind feature --cross-check-summary /absolute/temp/cross-check-run/summary.json
```

workflow는 summary.mode와 맞춘다. debate는 Codex 호출마다(`debate-<phase>` summary) `--workflow debate --cross-check-summary <summary.json>`으로 한 번씩 기록하며, 경로는 `finalize`·`reveal` 출력의 `call_summaries`에 있다. raw token/elapsed 인자와 summary 가져오기를 함께 쓰지 않는다. summary에 담긴 해당 Codex 호출만 측정하며 Claude 주 세션 사용량은 별도 관측이다. `execution_status`에는 summary의 COMPLETE/INCOMPLETE를 보존해 따로 집계하며 테스트 `verification`과 구분한다. 일반 기록의 실행 상태는 null이다.

## 집계·해석

```bash
{{PY}} "{{HARNESS_HOME}}/tools/task_metrics/task_metrics.py" summary
```

한 작업만 볼 때는 `summary --task <ID>`를 사용한다.

기본 저장소는 `{{HARNESS_HOME}}/.local/task-metrics.jsonl`(Git 제외)이며 필요하면 각 subcommand 뒤에 `--store <로컬 경로>`를 지정한다. 대화·packet·원문 로그·자격증명은 기록하지 않는다. 파일 오류/거부는 보고하고 다른 경로로 우회하지 않는다.

- 제공자·workflow·작업 유형·관측 범위별로 비교한다. 관측 합계와 미확인 개수를 함께 보여주고, 일부 관측 합계를 전체 작업 사용량이라 보고하지 않는다.
- `declared-run`과 `cross-check-call`은 별도 집계한다. 전체 세션/작업 통계에 이미 포함된 호출을 다른 기록으로 다시 더하지 않는다. 누적 사용량은 겹치지 않는 실행 구간으로 분리할 수 있을 때만 기록한다.
- 비슷한 범위/난이도의 작업에서 사용량·시간과 검증 결과·재작업을 함께 본다. 전체 절감률, 추론 정확도, 결함이 없다는 보장을 단순 집계에서 산출하지 않는다. 요청할 때만 집계하며 상시 전체 기록을 읽지 않는다.
