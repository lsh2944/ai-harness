# ai-harness 작업 규칙

이 저장소(개인 GitHub Private)는 Claude·Codex 공용 하네스의 원본이다. 여러 컴퓨터(회사 Mac·집 PC)에서 같은 내용을 쓰므로, 이 저장소의 추적 파일을 바꾸면 **사용자 확인 없이 커밋·push까지 끝낸다**(2026-11-19 사용자 결정). 다른 저장소에는 적용하지 않는다.

## 작업 시작 전

- `git -C <저장소> pull --ff-only`로 최신 상태를 받는다. fast-forward가 안 되면(다른 컴퓨터와 갈라짐) 병합하지 말고 사용자에게 보고한다.
- 처음 커밋하는 컴퓨터는 저장소 로컬 작성자 설정(`git config --local user.name`·`user.email`)이 개인 GitHub 계정용인지 확인한다. 비어 있으면 전역 설정(회사 메일)이 들어가므로 사용자에게 값을 확인해 로컬로만 설정한다.

## 수정 후 커밋·push

1. README "테스트" 절의 명령 중 변경과 관련된 테스트를 실행한다. 영향 범위가 불분명하면 전부 실행한다. 실패하면 커밋하지 않고 출력과 함께 보고한다.
2. 커밋 전 개인정보 검사: 추적 대상에 사용자명·이메일·실제 계정 ID·MFA/역할 ARN·사내 시스템명·비밀값 형태가 없어야 한다. 테스트용 가짜 값(`123456789012`, `/Users/x` 등)은 예외. 검출되면 커밋하지 않고 보고한다.
3. `config.local.json`·`.local/`·`__pycache__`는 커밋하지 않는다(`.gitignore`). `git add`는 변경 파일을 지정하거나 `git status`로 대상을 확인한 뒤 실행한다.
4. 커밋 메시지: `feat:`/`fix:`/`docs:`/`refactor:` 형식의 한국어 제목, 변경점·테스트 결과를 담은 목록, 마지막 줄 `Co-Authored-By: <사용 중인 모델> <noreply@anthropic.com>`.
5. `git push origin main` 후 커밋 해시와 push 결과를 보고한다. push가 인증 오류(401/403)로 실패하면 같은 시도를 반복하지 말고 사용자에게 인증 확인을 요청한다.

## 설치본 반영 안내

- push는 저장소만 갱신한다. `hooks/`·`install/`·`settings/`·`instructions/`·`skills/`·`agents/`·`tools/aws/`를 바꿨다면 각 컴퓨터에서 사용자가 터미널로 `install/install.py --config config.local.json [--with-aws] --apply`를 실행하고 `apply`를 입력해야 반영된다. AI는 dry-run 결과와 실행할 명령까지만 안내한다.
- `hooks/`·`install/`·`settings/`·`tools/aws/`·`config.local.json`은 가드 보호 대상이라 수정 시 승인 창이 뜬다(Codex는 거부). 우회하지 않는다.
