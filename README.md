# ai-harness

Claude Code와 Codex에 공통으로 쓰는 작업 지침·가드 hook·설정 조각·스킬·에이전트를 한 저장소에서 관리하고, `install/install.py`로 사용자 홈에 배치한다. 특정 프로젝트에 묶인 값은 `config.local.json`(Git 제외)에만 둔다.

## 구성

| 경로 | 내용 |
|---|---|
| `instructions/` | `~/.claude/CLAUDE.md`·`~/.codex/AGENTS.md`에 마커 블록으로 넣는 지침. `language.md`(응답·주석 언어), `core.md`(판단 전 확인·토큰 효율·하네스 안전), `conduct.md`(정직성·기획 먼저·지식 3층·안정성 비교·광역 pkill 금지·cd 복합 명령 금지·MCP 우선), `aws-readonly.md`(선택 AWS 모듈) |
| `instructions/project-template/CLAUDE.md` | 프로젝트 루트용 CLAUDE.md 견본(역할 흐름·위임·리뷰·문서 상태·실환경 경계). 설치 대상 아님, 복사해서 채운다 |
| `hooks/` | `guard.py`(PreToolUse 가드), `usage_alert.py`(사용량 경고), `tests/` |
| `install/install.py` | 배치 도구. 기본 dry-run |
| `settings/` | 병합 조각. `claude.settings*.json`(권한·자동 모드 규칙·출력 상한 등), `codex.config*.toml`(Codex 기본값·덮어쓸 값·환경변수 제외 목록), `mcp*.json`(MCP 버전 고정·aws-api env) |
| `agents/` | 공용 서브에이전트 `code-reviewer`·`architect`·`troubleshooter`·`script-feature`와 영역별 구현 에이전트 견본 `feature-template.md` |
| `skills/` | `handoff`, `cross-check`, `efficient-investigation`, `task-metrics`, `prune-memory`, `assume-dev`(AWS 모듈) |
| `tools/` | `repo_context.py`, `investigation/`, `task_metrics/`, `aws/`(MFA 인증 갱신 스크립트, AWS 모듈) |

## 설정 작성

```bash
cp config.example.json config.local.json
```

| 키 | 템플릿 변수 | 설명 |
|---|---|---|
| `project_root` | `{{PROJECT_ROOT}}` | 주 작업 프로젝트 절대 경로(`~` 허용). 가드의 보호 경로(`<project>/.claude/settings*`)와, cwd를 확정할 수 없을 때의 cross-check 작업 루트 대체값 |
| (자동) | `{{HARNESS_HOME}}` | 이 저장소 위치 |
| `memory_dir` | `{{MEMORY_DIR}}` | prune-memory 기본 대상. `null`이면 `~/.claude/projects/<project_root의 영숫자 외 문자를 -로>/memory` |
| `python` | `{{PYTHON}}` | hook을 실행할 인터프리터. `null`이면 install.py를 실행한 인터프리터. venv·이 저장소 안의 인터프리터는 거부한다 |
| `response_language` | `{{RESPONSE_LANGUAGE}}` | 응답 언어(예: Korean) |
| `comment_language` | `{{COMMENT_LANGUAGE}}` | 코드 주석 언어(예: Japanese) |
| `extra_protected_paths` | — | 가드가 수정 시 승인을 요구할 추가 절대 경로 |
| `aws.profile` | `{{AWS_PROFILE}}` | AI가 쓰는 읽기 전용 프로필(`--with-aws`일 때만 사용) |
| `aws.assume_role_cmd` | `{{ASSUME_ROLE_CMD}}` | 인증 갱신 스크립트 절대 경로. 예: `tools/aws/assume_role.sh` |
| `aws.assume_role_envs` | `{{AWS_ENVS}}`(쉼표 목록) | 가드가 `--awsEnv <이름>` 실행을 허용할 환경 이름 |
| `aws.roles`, `aws.mfa_serial` | — | `tools/aws/assume_role.py`가 쓰는 환경별 역할 ARN과 MFA 장치 ARN |

install.py가 내부에서 계산하는 변수: `{{SKILL_DIR}}`(설치된 각 스킬 폴더), `{{AWS_MCP_TOOLS}}`(`--with-aws`면 에이전트 tools에 aws-api MCP 도구를 붙이고 아니면 빈 문자열). 마크다운의 `<!-- BEGIN aws -->`~`<!-- END aws -->` 블록은 `--with-aws`일 때만 남는다. 치환되지 않은 `{{...}}`가 남으면 설치를 중단한다.

## 설치

```bash
# 설정 파일은 이 저장소 안의 일반 파일만 허용한다(바깥 경로·바깥을 가리키는 링크는 거부). 옵션 생략형(--ap 등)은 받지 않는다.
# 1) dry-run: 쓰지 않고 변경 예정 경로와 줄 수 요약만 출력한다(파일 내용·비밀값은 출력하지 않음)
python3 install/install.py --config config.local.json [--target claude|codex|both] [--with-aws]
# 2) 확인 후 적용: 사용자가 직접 터미널에서 실행하고, 확인 문구에 apply를 입력해야 쓴다.
#    stdin이 터미널이 아니면(AI 도구·파이프·CI 실행) 항상 거부하며, 이를 건너뛰는 옵션·환경변수는 없다.
python3 install/install.py --config config.local.json [--target ...] [--with-aws] --apply
```

배치 대상:

- Claude: `~/.claude/hooks/{guard.py,usage_alert.py,guard_hook.sh,harness_config.json}`, `~/.claude/settings.json`(조각 병합 + 소유 hook 교체), `~/.claude.json`(있을 때만: 등록된 MCP의 버전 고정, `--with-aws`면 aws-api env), `~/.claude/CLAUDE.md`(마커 블록), `~/.claude/skills/<이름>/`, `~/.claude/agents/<이름>.md`
- Codex: `~/.codex/hooks/…`, `~/.codex/config.toml`(조각 적용 + `# BEGIN/END shared harness hooks` 블록), `~/.codex/AGENTS.md`(마커 블록, 언어 지침 포함), `~/.agents/skills/<이름>/`
- `cross-check`·`prune-memory` 스킬 폴더에는 실행 설정 `harness_config.json`도 둔다. 스킬의 `tests/`는 배치하지 않는다.

안전장치: `settings.json`은 조각의 스칼라·키를 **값이 없을 때만** 넣고(`defaultMode` 등 사용자가 정한 값은 유지), 리스트(deny/ask/hard_deny)는 중복 없이 추가한다. 기존 값의 형식이 예상과 다르면 쓰지 않고 중단한다. aws-api MCP의 읽기 전용 env만은 강제값으로 덮어쓴다. 기존 설정의 다른 키·사용자 hook은 보존하고 소유 hook·마커 블록만 교체한다. 쓰기는 원자적(임시 파일→rename)이고 권한은 0600이다. hook과 설정 sidecar를 먼저 두고 등록 파일(settings.json·config.toml)은 마지막에 쓴다. 준비 중·적용 중 다른 프로그램이 파일을 바꾸면 중단하고, 실패하면 이번에 쓴 파일을 되돌린다. 변경 전 파일은 `~/.ai-harness/backups/<시각>/`(0700)에 보관한다. 심볼릭 링크 대상은 거부한다.

설치 후:

- Codex는 hook 정의가 바뀌면 `[hooks.state]`의 `trusted_hash`가 맞지 않아 **경고 없이 hook을 실행하지 않는다.** 대화형 `codex` 시작 화면의 "Trust all and continue" 또는 세션 내 hooks 브라우저에서 사용자가 다시 신뢰하고, 차단돼야 할 명령(예: 비밀 파일 읽기)이 실제로 거부되는지 확인한다. `trusted_hash`를 직접 계산·기입하지 않는다.
- Claude는 새 세션에서 hook이 적용된다.
- 기존 `CLAUDE.md`·`AGENTS.md`에 같은 내용을 손으로 넣어 두었다면 dry-run의 `WARN … possible duplicate`를 보고 중복 부분을 직접 정리한다(자동으로 지우지 않는다).

## 갱신

저장소를 갱신(`git pull` 등)한 뒤 같은 명령으로 dry-run → `--apply`를 다시 실행한다. 변경이 없으면 모든 항목이 `SAME`으로 나온다. 이전 버전에서 없어진 스킬 파일은 자동 삭제하지 않는다.

## 테스트

모든 테스트는 임시 디렉터리·임시 HOME에서 실행되며 실제 설정·AWS·Codex를 사용하지 않는다.

```bash
python3 -m unittest discover -s hooks/tests -t hooks/tests
python3 -m unittest discover -s install/tests -t install/tests
python3 -m unittest discover -s skills/cross-check/tests -t skills/cross-check/tests
python3 -m unittest discover -s tools/tests -t tools/tests
python3 -m unittest discover -s tools/investigation -t tools/investigation
python3 -m unittest discover -s tools/task_metrics -t tools/task_metrics
python3 -m unittest discover -s tools/aws -t tools/aws
```

`install/tests`는 venv가 아닌 기반 인터프리터로 install.py를 하위 프로세스 실행한다(Python 3.11 이상 필요, `tomllib`).

## 가드 동작 요약

- 설정 sidecar(`harness_config.json`)가 없거나 깨졌거나 값이 잘못되면(읽기 중 예기치 않은 예외 포함) **모든 도구 호출을 거부**한다(fail-closed). 이 경우 Claude도 hook을 고칠 수 없으므로 사용자가 `/hooks` 등으로 해제하거나 install.py를 다시 실행한다.
- `install.py` 적용(`--apply`, 경로 변형·인터프리터·래퍼·인라인 실행 포함)은 가드 설정을 바꾸므로 승인 요청(Codex는 거부). 이 문자열 검출에 더해 install.py의 터미널 확인(비대화 실행 거부, `apply` 입력)을 추가 방어로 둔다. 둘 다 우회 가능성이 있는 보조 방어다(알려진 한계 참고).
- `settings.json`의 보호용 강제 키(`--with-aws`의 `env.AWS_PROFILE`)는 기존 값과 달라도 덮어쓰며, dry-run에 `WARN … will be overwritten`으로 표시한다(값은 출력하지 않음).
- 인증 갱신 스크립트는 `<aws.assume_role_cmd> --awsEnv <등록 환경>`의 단독 실행(또는 `echo <6자리> |` 입력)만 허용하고, 변수 대입·`env`/`export`·래퍼·인터프리터 경유·다른 명령과의 연결은 승인 요청. `tools/aws/assume_role.py`는 저장소의 `config.local.json`만 읽는다.
- 어느 프로젝트든 `.claude/settings*.json` 수정은 승인 요청.
- 파일 삭제는 임시 디렉터리·검증된 `__pycache__` 밖이면 승인 요청, 비밀 파일(`~/.aws` 허용 목록 외, Codex·Claude 인증 파일) 읽기는 거부, 가드·설정·하네스 원본(`hooks/`·`install/`·`settings/`·`tools/aws/`·`config.local.json`)·인증 갱신 스크립트 수정은 승인 요청.
- AWS 변경·배포·비밀 출력 명령은 항상 거부한다. `--with-aws`일 때만 프로필을 `aws.profile`로 고정하고, 등록된 환경 이름의 인증 갱신 스크립트 실행을 예외로 허용한다. AWS 모듈 없이도 원시 자격증명 주입(`AWS_ACCESS_KEY_ID=` 등)·자격증명 파일 경로 변경은 거부한다.

## 이행 시 주의

기존 개인 하네스(프로젝트의 `tools/harness/`, 루트 `assume_role.sh`, 프로젝트 `.claude/skills`·`.agents/skills`)는 Git 밖에 있어 이행으로 바뀌는 내용이 이력에 남지 않는다. 이행 전에 그 파일들을 백업하고, 이후 변경은 이 저장소의 Git 이력으로 관리한다. 새 가드는 옛 경로(`tools/harness/`, 루트 `assume_role.sh`)를 보호하지 않으므로, 옛 파일을 남겨 두는 동안은 `extra_protected_paths`에 등록하거나 정리 계획을 세운다(삭제는 사용자가 결정). 기존 `~/.claude/CLAUDE.md`·`~/.codex/AGENTS.md`에 손으로 넣은 같은 절은 dry-run의 `WARN … possible duplicate`를 보고 사용자가 정리한다.

## 알려진 한계

- 가드는 **명령 문자열을 검사하는 보조 방어**이며 완전한 셸 파서가 아니다. 설정 문구나 heredoc 안의 `npm run deploy`, `aws …`로 시작하는 줄, 비밀 파일 경로 문자열 같은 무해한 텍스트도 차단될 수 있다. 차단되면 다른 도구·스크립트로 우회하지 말고 문구를 바꾸거나 파일 편집 도구를 쓴다.
- 스크립트 파일 안에 적힌 삭제·SDK 쓰기 코드는 명령 검사로 잡지 못한다(AWS는 프로필 고정과 읽기 전용 역할이 실제 방어선). `python3 -m install.install --apply`, glob(`install/inst*.py`), 저장소를 통째로 복사한 설치기처럼 이름·위치를 바꾼 실행은 문자열 검사로 막지 못한다. 이 경우에도 `--apply`는 터미널 확인을 요구하지만, `script`·`pty`·`expect` 등으로 의사 터미널을 만들어 확인 문구를 넘기면 이 확인도 우회될 수 있다.
- `HARNESS_HOME`은 Git 저장소라 `git checkout`·`reset`·`stash`·`pull` 등으로 보호 원본(`hooks/`·`install/`·`settings/`·`tools/aws/`)이 승인 없이 바뀔 수 있고, 가드의 쓰기 판정은 git 명령을 검사하지 않는다. 단 설치된 hook·설정에 반영되려면 사용자가 터미널에서 `install.py --apply`를 실행해야 한다.
- hook 인터프리터가 사라지면 Claude는 `guard_hook.sh`가 거부를 반환하지만, Codex는 신뢰 절차 때문에 python을 직접 실행하므로 인터프리터 소실·hook 타임아웃 시 가드 없이 실행될 수 있다. Python을 업그레이드하면 install.py를 다시 실행한다.
- Codex exec 모드의 일부 도구(node_repl 등 MCP 외 경로)는 hook 범위 밖이다.
- 가드 메시지·사용량 경고 문구는 한국어로 고정되어 있다.
- `skills/handoff`는 MIT 라이선스(원 저작자 표기는 `skills/handoff/LICENSE.txt`)를 따른다.
