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
| `tools/` | `repo_context.py`, `investigation/`, `task_metrics/`, `aws/`(MFA 인증 갱신 스크립트, AWS 모듈), `privacy_check.py`(커밋 전 개인정보 검사), `harness_setup.py`(setup.sh 보조) |
| `setup.sh` | 새 컴퓨터 초기 설정(Mac/Linux). 아래 "새 컴퓨터 설정" 참고 |
| `apply.sh` | 설치본 반영(dry-run 요약 → `install.py --apply`의 `apply` 입력 확인). 아래 "설치본 반영(apply)" 참고 |
| `.githooks/` | 저장소 git hook. `pre-commit`·`commit-msg`(개인정보 검사), `post-commit`(커밋 후 반영 알림), `post-merge`(pull 후 반영 알림) |

## 새 컴퓨터 설정(Mac/Linux)

Windows는 아직 지원하지 않는다(`setup.sh`가 MINGW/MSYS/CYGWIN을 감지하면 안내 후 종료). git은 clone할 때 저장소의 스크립트를 실행하지 않으므로 clone만으로 자동 설치되지는 않는다. clone 후 `./setup.sh`를 한 번 실행한다.

### ① Git·Python 준비

- macOS: `xcode-select --install`(Command Line Tools) 또는 Homebrew로 Git을 설치한다. Linux: 배포판 패키지 관리자로 설치한다.
- `install.py`는 Python 3.11 이상(`tomllib`)이 필요하다. hook용 인터프리터는 venv가 아닌 시스템 Python이어야 한다(`setup.sh`가 PATH와 흔한 설치 위치에서 찾아 제안한다).

### ② GitHub fine-grained token 만들기

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → **Generate new token**.
2. Token name을 정하고 **Expiration(만료일)** 을 설정한다. 만료되면 새 토큰을 만들어 아래 "토큰 교체"대로 바꾼다.
3. **Resource owner**: 본인 계정. **Repository access**: "Only select repositories"에서 이 저장소(필요하면 다른 개인 저장소도)를 고른다.
4. **Repository permissions**: **Contents → Read and write**. Metadata는 자동으로 Read-only가 붙는다.
5. 생성 직후 한 번만 보이는 토큰 값을 복사한다. 토큰은 저장소 파일·`config.local.json`·git 설정에 넣지 않는다.

### ③ clone과 인증 저장

```bash
git clone https://github.com/<GitHub 사용자명>/ai-harness.git
```

- 프롬프트의 `Username`에는 GitHub 사용자명, `Password`에는 ②의 토큰을 붙여 넣는다(입력해도 화면에 표시되지 않는다).
- macOS: Command Line Tools·Homebrew의 Git은 보통 credential helper `osxkeychain`이 설정되어 있어 토큰이 키체인에 저장되고 다음부터 묻지 않는다. `git config --get credential.helper`로 확인한다.
- Linux: credential helper가 설정되어 있지 않으면 fetch·push마다 다시 묻는다. 쓸 수 있는 helper(libsecret, Git Credential Manager, 메모리에만 잠시 두는 `cache` 등)는 배포판·데스크톱 환경에 따라 다르다. `store`는 토큰을 홈의 파일에 평문으로 저장하므로 권장하지 않는다.

### ④ `./setup.sh`

clone한 폴더에서 터미널로 직접 실행한다(파이프·AI 도구 등 비대화 실행은 거부). 다시 실행해도 안전하며 끝난 단계는 건너뛴다.

```bash
cd ai-harness
./setup.sh            # 끝까지(적용 여부는 마지막에 묻는다)
./setup.sh --no-apply # dry-run 요약까지만
```

1. 이 저장소에만 쓰는 커밋 작성자(`git config --local user.name`·`user.email`)가 없으면 입력받는다. 전역 git 설정은 바꾸지 않는다. 메일은 `<개인 메일>` 또는 GitHub noreply 메일(`<ID>+<GitHub 사용자명>@users.noreply.github.com`)을 쓴다.
2. `git config --local core.hooksPath .githooks`를 설정한다(다른 값이 이미 있으면 바꿀지 묻는다).
3. `config.local.json`이 없으면 대화형으로 만든다(권한 0600). project_root(기본 `$HOME/project`), hook용 python(venv 밖 Python 3.11+ 제안, `-`로 비우면 `null`), 응답·주석 언어(견본 기본값), AWS 모듈 사용 여부. `extra_protected_paths`에는 이 clone의 `.githooks`·`tools/privacy_check.py`·`tools/harness_setup.py`·`setup.sh`·`apply.sh` 절대 경로가 들어간다. AWS를 쓰면 견본의 `aws` 절을 복사하고(`assume_role_cmd`는 이 clone의 경로로 바꿈) 종료하므로, `aws.roles`·`aws.mfa_serial`(필요하면 `profile`·`assume_role_envs`)을 실제 값으로 편집한 뒤 다시 실행한다. 견본 값 그대로면 다음 단계로 가지 않는다.
4. `AWS 모듈(--with-aws): on/off/placeholder`를 표시한 뒤 `install.py` dry-run 요약(`SAME`은 생략)을 보여 준다. 변경이 없으면 여기서 끝난다. `--with-aws`는 `config.local.json`에 `aws` 절(object)이 있으면 붙인다. `aws`가 `null` 등 object가 아니거나 `AWS`처럼 대소문자만 다른 키가 있으면 오류로 멈춘다(AWS를 쓰지 않으려면 키를 지운다).
5. 적용할지 묻고, `y`면 `install.py --apply`를 실행한다. 이어서 install.py 자신의 확인 문구에 `apply`를 입력해야 실제로 쓴다.

### ⑤ 이후 커밋·push

- 커밋하면 pre-commit·commit-msg hook이 파일과 커밋 메시지의 개인정보 검사를 실행한다(아래 "git hook").
- `git push origin main`은 저장된 토큰으로 인증된다.

### 문제 해결(인증)

| 증상 | 원인 | 대처 |
|---|---|---|
| 403 `Write access to repository not granted` | 토큰에 이 저장소 또는 Contents 쓰기 권한이 없음, 또는 키체인에 다른 토큰이 저장되어 있음 | ②의 Repository access·Contents 권한을 확인하거나 새 토큰을 만들고 아래대로 교체 |
| 401 / `Invalid username or token` | 토큰 만료·오입력 | 새 토큰을 만들어 교체 |

토큰 교체(macOS 키체인): 저장된 항목을 지운 뒤 다시 fetch·push하면 Username·Password(새 토큰)를 다시 묻는다.

```bash
printf 'protocol=https\nhost=github.com\n\n' | git credential-osxkeychain erase
git push origin main
```

Linux는 설정한 credential helper의 방법을 따른다(환경에 따라 다름).

## git hook(`.githooks/`)

`setup.sh`가 `core.hooksPath`를 설정한 저장소에서만 동작한다. 수동 설정: `git config --local core.hooksPath .githooks`.

- **pre-commit**: `python3 tools/privacy_check.py --staged`로 index의 삭제 외 모든 변경 파일(추가·수정·이름 변경·타입 변경, 심볼릭 링크는 대상 문자열)을 검사한다. 12자리 AWS 계정 ID·IAM ARN·이메일·AWS 액세스 키·GitHub 토큰·`sk-` 키·PRIVATE KEY 헤더·`/Users/<이름>`·`/home/<이름>` 경로·현재 사용자의 HOME 경로와 `privacy_words`를 찾으면 `파일:행: 종류(값 일부 마스킹)`를 출력하고 커밋을 막는다.
  - 크기 상한은 없다(1MiB 단위로 읽어 줄 단위로 검사). BOM 있는 UTF-8/16/32는 디코드해 검사하고, BOM만 붙인 위장에 대비해 원본 바이트에서 NUL을 뺀 UTF-8·latin-1로도 한 번 더 검사한다(검출은 합집합). 앞부분에 NUL이 있는 바이너리(BOM 없는 UTF-16 포함)는 검사할 수 없으므로 `SKIP`으로 표시하고 **커밋을 막는다**. 내용을 확인한 뒤 `--no-verify` 사용 여부는 사용자가 결정한다.
  - `privacy_words`가 없으면 "고유명사 검사는 privacy_words 설정 시에만"이라는 알림이 한 줄 나온다(차단하지 않음).
  - 오탐이면 **사용자 확인을 받은 뒤** `tools/privacy_check.py`의 패턴·허용 목록을 고친다(허용 목록은 최소한으로 두고 근거를 주석으로 남긴다). 테스트용 값은 소스에 그대로 쓰지 말고 실행 시 문자열을 이어 붙여 만든다.
  - `git commit --no-verify`로 건너뛸 수 있지만 우회 여부는 사용자가 판단한다.
  - 전체 점검: `python3 tools/privacy_check.py --all`(추적 파일 전체, index 내용. 바이너리가 있으면 종료 코드 1).
- **commit-msg**: `python3 tools/privacy_check.py --message <파일>`로 커밋 메시지 전체(`#` 주석 줄·`commit -v`의 scissors 아래 포함. `-m`·`-F` 커밋은 cleanup=whitespace라 주석 줄도 본문에 남기 때문)를 같은 패턴으로 검사해 검출 시 막는다. 편집기 템플릿의 주석(예: `commit -v`의 diff)에 검출 값이 있어 막히면 메시지 수정이나 `--no-verify`는 사용자가 판단한다. `Co-Authored-By`의 `noreply@anthropic.com`은 허용. 커밋 작성자(user.email) 정보는 검사하지 않는다.
- **post-commit**: 방금 만든 커밋(최초 커밋은 빈 트리 대비, 병합 커밋은 첫 번째 부모 대비)에서 `hooks/ install/ settings/ instructions/ skills/ agents/ tools/aws/`가 바뀌었으면 `[ai-harness] 설치본 반영 필요: ./apply.sh` 한 줄을 **출력만** 한다. Python·install.py·apply.sh를 실행하지 않고 항상 종료 코드 0이다(커밋에 영향 없음).
- **post-merge**: `git pull`(merge·fast-forward) 후 같은 경로가 바뀌었으면 목록과 `./apply.sh` 실행 안내(저장소 밖에서 쓸 절대 경로 포함)를 **출력만** 한다. Python·install.py를 실행하지 않는다. `config.local.json`이 없으면 `./setup.sh`, aws 절이 견본 값이면 편집을 안내한다. `git pull --rebase`·`checkout`에서는 실행되지 않는다.
- 이 hook들과 검사기·`setup.sh`·`apply.sh`·`tools/harness_setup.py`를 바꾸거나 `core.hooksPath`를 바꾸는 것은 커밋 전에 사용자 확인을 받는다(CLAUDE.md). `setup.sh`가 새로 만드는 `config.local.json`은 이 경로들을 `extra_protected_paths`에 넣어 가드가 수정 시 승인을 요구하게 한다. **이미 `config.local.json`이 있는 컴퓨터**는 clone 절대 경로 기준 `<clone>/.githooks`, `<clone>/tools/privacy_check.py`, `<clone>/tools/harness_setup.py`, `<clone>/setup.sh`, `<clone>/apply.sh` 중 빠진 것을 `extra_protected_paths`에 직접 추가한 뒤 터미널에서 `./apply.sh`를 실행해야 반영된다(2026-11-19 `apply.sh` 추가 전에 만든 `config.local.json`에는 `apply.sh`가 없다).

## 설정 작성

`setup.sh`가 만들지 않은 경우 견본을 복사해 편집한다.

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
| `privacy_words` | — | 선택. pre-commit·commit-msg 개인정보 검사에 더할 사용자별 단어 목록(실명·회사 도메인 등, 대소문자 무시). install.py는 이 키를 쓰지 않는다 |

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

## 설치본 반영(apply)

Claude·Codex는 이 저장소가 아니라 `~/.claude`·`~/.codex`·`~/.agents`에 설치된 사본을 읽는다. 설치기(`install.py`)가 템플릿 값(`{{PYTHON}}` 등)을 치환하고 `settings.json`·`config.toml`을 병합하므로 단순 복사·링크로는 대신할 수 없다. 또 가드·설정 변경이 사용자 확인 없이 적용되지 않도록 저장소 갱신(commit·pull)과 설치본 반영을 의도적으로 분리해 두었다.

언제 필요한가:

- 필요: `hooks/`·`install/`·`settings/`·`instructions/`·`skills/`·`agents/`·`tools/aws/`가 바뀌었을 때(커밋 후 post-commit, pull 후 post-merge가 알려 준다), `config.local.json`을 바꿨을 때.
- 불필요: `README.md`·`CLAUDE.md`·`tools/privacy_check.py`·`.githooks/`·`setup.sh`·`apply.sh`·그 밖의 `tools/`(저장소에서 직접 실행하거나 설치 대상이 아님).

사용법(저장소 폴더에서 터미널로 직접 실행. 다른 위치에서는 절대 경로로 실행해도 된다):

```bash
./apply.sh
```

1. `config.local.json`이 없으면 `./setup.sh` 안내 후 종료한다. 터미널이 아닌 실행(파이프·AI 도구)·Windows는 안내 후 종료한다.
2. `AWS 모듈(--with-aws): on/off/placeholder`를 표시한다. 견본 값(placeholder)이면 편집을 안내하고 종료한다. `on`이면 `--with-aws`를 붙인다.
3. `install.py` dry-run 요약(`SAME` 생략)을 보여 준다. 변경이 없으면 "이미 최신"으로 종료한다.
4. `install.py --config <절대 경로> [--with-aws] --apply`를 실행한다. install.py 자신의 확인 문구에 `apply`를 입력해야 실제로 쓴다(그 밖의 입력은 취소). 인터프리터는 `config.local.json`의 `python`, 없으면 venv가 아닌 Python 3.11 이상이다.

동시 사용 주의:

- 설치는 컴퓨터마다 독립이다. 한 컴퓨터에서 push해도 다른 컴퓨터는 `git pull` 후 `./apply.sh`를 실행할 때까지 이전 버전으로 동작한다.
- 같은 컴퓨터에서는 Claude Code·Codex를 종료한 뒤 적용한다(실행 중인 앱이 `~/.claude.json` 등을 동시에 쓰면 적용이 중단되거나 경합한다). `apply.sh`는 앱을 감지·종료하지 않고 안내만 한다. `apply.sh`(또는 `install.py --apply`)를 여러 터미널에서 동시에 실행하지 않는다.

## 갱신

저장소를 갱신(`git pull` 등)한 뒤 `./apply.sh`를 실행한다(hook을 설정했다면 post-merge가 반영 필요 여부를 알려 준다). 변경이 없으면 "이미 최신"으로 끝난다. 이전 버전에서 없어진 스킬 파일은 자동 삭제하지 않는다.

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

`tools/tests`에는 `test_privacy_check.py`(개인정보 검사)와 `test_setup.py`(setup.sh·apply.sh·`.githooks`·`harness_setup.py`)도 들어 있다. `test_setup.py`는 작업 트리(Git 제외 파일 제외)를 임시 origin에 복제해 로컬 clone하고 임시 HOME·pty에서 실행하며, `install.py --apply`는 스텁으로만 확인한다. 개별 실행:

```bash
python3 -m unittest discover -s tools/tests -t tools/tests -p 'test_privacy_check.py'
python3 -m unittest discover -s tools/tests -t tools/tests -p 'test_setup.py'
python3 tools/privacy_check.py --all
```

## 가드 동작 요약

- 설정 sidecar(`harness_config.json`)가 없거나 깨졌거나 값이 잘못되면(읽기 중 예기치 않은 예외 포함) **모든 도구 호출을 거부**한다(fail-closed). 이 경우 Claude도 hook을 고칠 수 없으므로 사용자가 `/hooks` 등으로 해제하거나 install.py를 다시 실행한다.
- `install.py` 적용(`--apply`, 경로 변형·인터프리터·래퍼·인라인 실행 포함)은 가드 설정을 바꾸므로 승인 요청(Codex는 거부). 이 문자열 검출에 더해 install.py의 터미널 확인(비대화 실행 거부, `apply` 입력)을 추가 방어로 둔다. 둘 다 우회 가능성이 있는 보조 방어다(알려진 한계 참고).
- `settings.json`의 보호용 강제 키(`--with-aws`의 `env.AWS_PROFILE`)는 기존 값과 달라도 덮어쓰며, dry-run에 `WARN … will be overwritten`으로 표시한다(값은 출력하지 않음).
- 인증 갱신 스크립트는 `<aws.assume_role_cmd> --awsEnv <등록 환경>`의 단독 실행(또는 `echo <6자리> |` 입력)만 허용하고, 변수 대입·`env`/`export`·래퍼·인터프리터 경유·다른 명령과의 연결은 승인 요청. `tools/aws/assume_role.py`는 저장소의 `config.local.json`만 읽는다.
- 어느 프로젝트든 `.claude/settings*.json` 수정은 승인 요청.
- Bash 쓰기 판정은 명령별 쓰기 대상을 hook 입력 cwd와 명령 안의 `cd`·`pushd`·`eval`·`sh -c`를 따라 절대·실제 경로로 바꿔 보호 경로와 비교한다. `cp`·`ln`·`install`·`rsync`는 대상만, `mv`는 원본·대상, `chmod`는 경로 인자를 보며, `dd of=`·`curl -o`·`wget -O`의 출력 파일도 판정한다. `cp`·`rsync`는 실제 기록 위치(대상이 디렉터리면 `대상/원본 이름`)도 판정하고, 재귀 복사(`-r`·`-R`·`-a`)는 그 위치가 보호 경로의 상위여도, 원본이 `.`·`dir/.`·끝 `/`(내용 복사)면 대상 자체가 보호 경로의 상위여도 승인 요청(예: `cp -r /tmp/x/. .`를 하네스 안에서, `rsync -a src/ ~/`). 보호 경로의 상위 폴더를 `mv`·`ln`·`chmod`하면 승인 요청. `patch`·`git apply/checkout/restore/stash`는 작업 위치가 보호 경로 안·상위·하네스 안이면, `tar -x`·`unzip`은 보호 디렉터리 안이면 승인 요청.
- `cd` 대상을 확정할 수 없으면(변수·치환·`cd -` 등) 이후 상대 경로 쓰기는 승인 요청, hook 입력에 cwd가 없으면 보호 이름으로 판정한다. 하네스 저장소 안에서 쓰기 대상에 확장되지 않는 `$변수`가 있으면 승인 요청.
- 따옴표 구분자 heredoc(`<<'EOF'`) 본문은 `cat`·`tee`·`git commit/tag -F -`(git 메시지의 `$(cat <<'EOF')` 포함)가 데이터로만 읽을 때만 쓰기 판정에서 뺀다. 같은 줄에 프로세스 치환(`>(…)`·`<(…)`)이나 다른 명령으로의 파이프가 있으면 본문도 판정한다. 64KB를 넘는 Bash 명령은 거부 판정(비밀 파일·자격증명 주입·프로필 변경·배포·복호화, 모두 입력 길이에 선형인 검사)을 먼저 한 뒤 나머지 판정 전에 승인 요청(Codex는 거부)해, 정규식 시간 초과로 판정 없이 실행되는 일을 막는다.
- 파일 삭제는 임시 디렉터리·검증된 `__pycache__` 밖이면 승인 요청, 비밀 파일(`~/.aws` 허용 목록 외, Codex·Claude 인증 파일) 읽기는 거부, 가드·설정·하네스 원본(`hooks/`·`install/`·`settings/`·`tools/aws/`·`config.local.json`)·인증 갱신 스크립트 수정은 승인 요청.
- AWS 변경·배포·비밀 출력 명령은 항상 거부한다. `--with-aws`일 때만 프로필을 `aws.profile`로 고정하고, 등록된 환경 이름의 인증 갱신 스크립트 실행을 예외로 허용한다. AWS 모듈 없이도 원시 자격증명 주입(`AWS_ACCESS_KEY_ID=` 등)·자격증명 파일 경로 변경은 거부한다.

## 이행 시 주의

기존 개인 하네스(프로젝트의 `tools/harness/`, 루트 `assume_role.sh`, 프로젝트 `.claude/skills`·`.agents/skills`)는 Git 밖에 있어 이행으로 바뀌는 내용이 이력에 남지 않는다. 이행 전에 그 파일들을 백업하고, 이후 변경은 이 저장소의 Git 이력으로 관리한다. 새 가드는 옛 경로(`tools/harness/`, 루트 `assume_role.sh`)를 보호하지 않으므로, 옛 파일을 남겨 두는 동안은 `extra_protected_paths`에 등록하거나 정리 계획을 세운다(삭제는 사용자가 결정). 기존 `~/.claude/CLAUDE.md`·`~/.codex/AGENTS.md`에 손으로 넣은 같은 절은 dry-run의 `WARN … possible duplicate`를 보고 사용자가 정리한다.

## 알려진 한계

- 가드는 **명령 문자열을 검사하는 보조 방어**이며 완전한 셸 파서가 아니다. 설정 문구나 heredoc 안의 `npm run deploy`, `aws …`로 시작하는 줄, 비밀 파일 경로 문자열 같은 무해한 텍스트도 차단될 수 있다. 차단되면 다른 도구·스크립트로 우회하지 말고 문구를 바꾸거나 파일 편집 도구를 쓴다.
- Bash 쓰기 대상의 상대 경로는 hook 입력의 cwd(명령 안의 `cd` 포함) 기준으로 해석해 보호 경로와 비교한다. 2026-11-19 이전 가드는 절대 경로 문자열만 판정해, 저장소 폴더 안에서 `sed -i … hooks/guard.py`처럼 상대 경로로 쓰면 승인 없이 통과했다. 지금도 하네스 밖에서 변수·명령 치환으로 만든 경로, 스크립트 파일 안의 경로, `python - <<EOF` 본문의 파일 쓰기는 판정할 수 없고, `wget` 기본 파일명·`-P`, `curl -O`·`--output-dir`·`-c`/`-D`, `git reset/pull/merge/switch`, `touch`, `awk -i inplace`, `CDPATH`, `source`로 읽은 스크립트 안의 `cd`도 판정하지 않는다. `tar -x`·`unzip`으로 보호 경로의 상위(예: `-C ~`)에 풀면 판정하지 않는다. 서브셸의 `cd`도 이후 후보에 남기는 등 보수적 판정이라 오탐이 날 수 있다. 그러므로 하네스 파일은 Bash(sed·리다이렉트·heredoc)가 아니라 Edit/Write 도구로만 수정한다.
- 스크립트 파일 안에 적힌 삭제·SDK 쓰기 코드는 명령 검사로 잡지 못한다(AWS는 프로필 고정과 읽기 전용 역할이 실제 방어선). `python3 -m install.install --apply`, glob(`install/inst*.py`), 저장소를 통째로 복사한 설치기처럼 이름·위치를 바꾼 실행은 문자열 검사로 막지 못한다. 이 경우에도 `--apply`는 터미널 확인을 요구하지만, `script`·`pty`·`expect` 등으로 의사 터미널을 만들어 확인 문구를 넘기면 이 확인도 우회될 수 있다.
- `HARNESS_HOME`은 Git 저장소라 `git checkout`·`reset`·`stash`·`pull` 등으로 보호 원본(`hooks/`·`install/`·`settings/`·`tools/aws/`)이 바뀔 수 있다. 가드는 `git apply/checkout/restore/stash`만 작업 위치 기준으로 승인 요청하고 `reset`·`pull`·`merge`·`switch`는 검사하지 않는다. 단 설치된 hook·설정에 반영되려면 사용자가 터미널에서 `install.py --apply`를 실행해야 한다.
- hook 인터프리터가 사라지면 Claude는 `guard_hook.sh`가 거부를 반환하지만, Codex는 신뢰 절차 때문에 python을 직접 실행하므로 인터프리터 소실·hook 타임아웃 시 가드 없이 실행될 수 있다. Python을 업그레이드하면 install.py를 다시 실행한다.
- Codex exec 모드의 일부 도구(node_repl 등 MCP 외 경로)는 hook 범위 밖이다.
- 가드 메시지·사용량 경고 문구는 한국어로 고정되어 있다.
- `skills/handoff`는 MIT 라이선스(원 저작자 표기는 `skills/handoff/LICENSE.txt`)를 따른다.
