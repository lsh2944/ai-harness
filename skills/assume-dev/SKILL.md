---
name: assume-dev
description: >-
  AWS 읽기 전용 자격증명을 MFA로 갱신한다(선택 AWS 모듈).
  AWS 작업 전 또는 인증 만료 시 설치 설정의 인증 갱신 스크립트를 실행하여
  Claude와 Codex 공용 [{{AWS_PROFILE}}] 프로필을 갱신한다.
---

# assume-dev — 읽기 전용 AWS 인증 갱신

- 등록된 환경: {{AWS_ENVS}} (설치 설정 `aws.assume_role_envs`. 가드는 이 이름만 예외로 허용한다)
- 환경이 지정되지 않았으면 사용자에게 확인한다. 임의로 선택하지 않는다.

1. 사용자에게 선택한 환경의 6자리 MFA 코드를 요청한다. 가능한 경우 대화형 터미널 입력을 사용한다. 명령 문자열·로그·문서에 MFA를 남기지 않는다.
2. `{{ASSUME_ROLE_CMD}} --awsEnv <환경>`을 실행하고 stdin으로 코드를 전달한다. 스크립트가 갱신에 한해 default 인증을 사용하므로 외부에서 AWS_PROFILE을 해제하지 않는다.
3. `[{{AWS_PROFILE}}] 업데이트 완료` 메시지로 성공을 확인한다. credentials 파일을 읽어서 확인하지 않는다. 실패하면 원인을 확인하고 새 MFA가 필요할 때만 요청한다. 같은 코드로 반복 재시도하지 않는다.
4. 이후 AWS CLI에는 `--profile {{AWS_PROFILE}}`를 사용한다. 양쪽 AWS MCP는 `AWS_API_MCP_PROFILE_NAME={{AWS_PROFILE}}`, `READ_OPERATIONS_ONLY=true`로 설정한다. 활성 프로세스의 설정이 다르면 재시작하고 확인하며 다른 프로필로 우회하지 않는다.

스크립트는 파일 권한을 600으로 유지하고 키를 출력하거나 클립보드로 복사하지 않는다. 일반 AWS 변경·배포는 사용자가 직접 실행한다. 로컬 설정만으로 실제 IAM 정책 검증을 대신하지 않는다.
