# ai-harness 작업 규칙

이 저장소(개인 GitHub Private)는 Claude·Codex 공용 하네스의 원본이다. 여러 컴퓨터(회사 Mac·집 PC)에서 같은 내용을 쓰므로, 이 저장소의 추적 파일을 바꾸는 작업은 **사용자 확인 없이 커밋·push까지 끝낸다**(2026-11-19 사용자 결정). 작업 중에는 의미 단위의 작은 변경마다 커밋으로 나누고, push는 사용자가 요청한 작업 하나가 완료된 시점(모든 커밋과 최종 검증이 끝난 뒤)에 한 번 한다. 다른 저장소에는 적용하지 않는다.

## 작업 시작 전

- `git -C <저장소> pull --ff-only`로 최신 상태를 받는다. fast-forward가 안 되면(다른 컴퓨터와 갈라짐) 병합하지 말고 사용자에게 보고한다.
- 새 컴퓨터는 사용자가 clone 후 터미널에서 `./setup.sh`를 실행해 작성자·hook·`config.local.json`·설치를 설정한다(README "새 컴퓨터 설정", 비대화 실행은 거부되므로 AI는 안내만 한다).
- 처음 커밋하는 컴퓨터는 저장소 로컬 작성자 설정(`git config --local user.name`·`user.email`)이 개인 GitHub 계정용인지, `git config --local core.hooksPath`가 `.githooks`인지 확인한다. 작성자가 비어 있으면 전역 설정(회사 메일)이 들어가므로 사용자에게 값을 확인해 로컬로만 설정한다.

## 커밋(작은 변경 단위)과 push(작업 완료 시 1회)

1. 커밋 전에 README "테스트" 절의 명령 중 변경과 관련된 테스트를 실행한다. 영향 범위가 불분명하면 전부 실행한다. 실패하면 커밋하지 않고 출력과 함께 보고한다.
2. 커밋 전 개인정보 검사: pre-commit hook(`.githooks/pre-commit`)이 `python3 tools/privacy_check.py --staged`로 파일을, commit-msg hook이 `--message`로 커밋 메시지를 검사해 사용자명·이메일·실제 계정 ID·IAM ARN·홈 경로·비밀값 형태와 검사할 수 없는 바이너리를 막는다. hook이 꺼져 있으면(`core.hooksPath` 미설정) 커밋 전에 같은 명령(메시지는 파일로 저장해 `--message <파일>`)을 직접 실행한다. 실명·사내 시스템명·회사 도메인은 `config.local.json`의 `privacy_words`에 둔 단어만 검출되므로 그 밖의 고유명사는 diff로 확인한다. 테스트용 가짜 값(`123456789012`, `/Users/x` 등)은 허용 목록에 있다. 검출되면 커밋하지 않고 보고한다. 오탐 수정(패턴·허용 목록 변경)은 아래 예외 규칙대로 사용자 확인 후 하고, `--no-verify` 우회는 사용자가 결정한다.
   - **예외(자동 커밋·push 규칙보다 우선):** 개인정보 검사기(`tools/privacy_check.py`)의 패턴·허용 목록, `.githooks/`, `tools/harness_setup.py`, `setup.sh`의 변경과 `core.hooksPath` 변경은 커밋 전에 사용자 확인을 받는다. 검사·hook을 약하게 만드는 변경이 확인 없이 들어가지 않게 하기 위함이다.
3. `config.local.json`·`.local/`·`__pycache__`는 커밋하지 않는다(`.gitignore`). `git add`는 변경 파일을 지정하거나 `git status`로 대상을 확인한 뒤 실행한다.
4. 커밋 메시지: `feat:`/`fix:`/`docs:`/`refactor:` 형식의 한국어 제목, 변경점·테스트 결과를 담은 목록, 마지막 줄 `Co-Authored-By: <사용 중인 모델> <noreply@anthropic.com>`.
5. 작업이 완료되면 최종 검증 후 `git push origin main`을 한 번 실행하고, 이번 작업의 커밋 목록(해시·제목)과 push 결과를 보고한다. 작업 도중에는 push하지 않는다. push가 인증 오류(401/403)로 실패하면 같은 시도를 반복하지 말고 사용자에게 인증 확인을 요청한다.

## 설치본 반영 안내

- push는 저장소만 갱신한다. `hooks/`·`install/`·`settings/`·`instructions/`·`skills/`·`agents/`·`tools/aws/`를 바꿨다면 각 컴퓨터에서 사용자가 터미널로 `install/install.py --config config.local.json [--with-aws] --apply`를 실행하고 `apply`를 입력해야 반영된다. AI는 dry-run 결과와 실행할 명령까지만 안내한다.
- `hooks/`·`install/`·`settings/`·`tools/aws/`·`config.local.json`은 가드 보호 대상이라 수정 시 승인 창이 뜬다(Codex는 거부). 우회하지 않는다.
