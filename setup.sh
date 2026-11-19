#!/bin/sh
# ai-harness の初期設定（Mac/Linux 用。Windows は未対応）。
# clone 直後に ./setup.sh を実行する。再実行しても安全（済んだ段階は飛ばす）。
#   1) このリポジトリ専用のコミット作成者（--local のみ。グローバル設定は変えない）
#   2) core.hooksPath=.githooks（コミット前の個人情報検査・pull 後の反映通知）
#   3) config.local.json がなければ対話で作成
#   4) install.py の dry-run 要約
#   5) 続行を選んだときだけ install.py --apply（install.py 自身の "apply" 確認を経る）
# --no-apply: 4) までで終了する（テスト・確認用）。
set -u
# 作成するファイル（config.local.json）を本人以外が読めないようにする
umask 077

usage() {
    echo "usage: ./setup.sh [--no-apply]"
}

no_apply=0
for arg in "$@"; do
    case $arg in
        --no-apply) no_apply=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

case $(uname -s 2>/dev/null) in
    MINGW*|MSYS*|CYGWIN*)
        echo "Windows는 아직 지원하지 않습니다(Mac/Linux 전용). README의 설치 절을 참고해 수동으로 설정하세요." >&2
        exit 1 ;;
esac

if [ ! -t 0 ]; then
    echo "setup.sh는 사용자가 터미널에서 직접 실행해야 합니다(stdin이 터미널이 아님). 아무것도 바꾸지 않았습니다." >&2
    exit 1
fi

# スクリプトのある場所（リポジトリ直下）で動く
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
cd "$here" || exit 1
top=$(git rev-parse --show-toplevel 2>/dev/null) || top=''
if [ -z "$top" ] || [ "$(CDPATH='' cd -- "$top" && pwd -P)" != "$here" ]; then
    echo "setup.sh는 ai-harness 저장소(git clone한 폴더) 바로 아래에서만 동작합니다." >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3를 찾을 수 없습니다. Python 3.11 이상을 설치한 뒤 다시 실행하세요." >&2
    exit 1
fi

helper() {
    python3 tools/harness_setup.py "$@"
}

# 1 行入力。EOF（Ctrl-D）なら中断する。結果は answer に入る。
ask() {
    printf '%s' "$1"
    if ! IFS= read -r answer; then
        echo
        echo "입력이 끝나 중단합니다." >&2
        exit 1
    fi
}

# y/N 質問。y・yes のときだけ真。
confirm() {
    ask "$1 [y/N]: "
    case $answer in
        y|Y|yes|YES|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

# --- 1) コミット作成者（このリポジトリだけ） ---
name=$(git config --local --get user.name 2>/dev/null) || name=''
email=$(git config --local --get user.email 2>/dev/null) || email=''
if [ -n "$name" ] && [ -n "$email" ]; then
    echo "[1/5] 이 저장소의 커밋 작성자 설정이 있습니다. 건너뜁니다."
else
    echo "[1/5] 이 저장소에만 쓰는 커밋 작성자(개인 GitHub 계정용)를 설정합니다. 전역 git 설정은 바꾸지 않습니다."
    echo "      메일은 GitHub에 등록한 개인 메일 또는 GitHub noreply 메일(<ID>+<사용자명>@users.noreply.github.com)을 권장합니다."
    while [ -z "$name" ]; do
        ask "      user.name: "
        name=$answer
    done
    git config --local user.name "$name" || exit 1
    while :; do
        case $email in
            *?@?*) break ;;
        esac
        ask "      user.email: "
        email=$answer
    done
    git config --local user.email "$email" || exit 1
fi

# --- 2) git hook の場所 ---
hooks=$(git config --local --get core.hooksPath 2>/dev/null) || hooks=''
if [ "$hooks" = ".githooks" ]; then
    echo "[2/5] core.hooksPath=.githooks 가 설정되어 있습니다. 건너뜁니다."
elif [ -z "$hooks" ] || confirm "[2/5] core.hooksPath가 이미 '$hooks'로 설정되어 있습니다. .githooks로 바꿀까요?"; then
    git config --local core.hooksPath .githooks || exit 1
    echo "[2/5] core.hooksPath=.githooks 를 설정했습니다(커밋 전 개인정보 검사·pull 후 반영 알림)."
else
    echo "[2/5] core.hooksPath를 바꾸지 않았습니다. 이 저장소의 hook은 동작하지 않습니다."
fi
for hook in .githooks/pre-commit .githooks/commit-msg .githooks/post-merge .githooks/post-commit; do
    if [ ! -x "$hook" ]; then
        echo "      주의: $hook 에 실행 권한이 없어 git이 실행하지 않습니다(chmod +x 필요)." >&2
    fi
done

# --- 3) 利用者設定 ---
if [ -e config.local.json ] || [ -L config.local.json ]; then
    echo "[3/5] config.local.json 이 있습니다. 건너뜁니다."
else
    echo "[3/5] config.local.json 을 만듭니다(Git 제외 파일, 이 컴퓨터 전용)."
    while :; do
        ask "      project_root(주 작업 프로젝트 절대 경로) [$HOME/project]: "
        project=${answer:-$HOME/project}
        case $project in
            /*|'~'|'~/'*) break ;;
        esac
        echo "      절대 경로(또는 ~로 시작)로 입력하세요."
    done
    detected=$(helper system-python) || detected=''
    if [ -z "$detected" ]; then
        echo "      venv가 아닌 Python 3.11 이상을 찾지 못했습니다. 경로를 직접 입력하거나 비워 두세요(비우면 install.py를 실행한 python3 사용)."
    fi
    while :; do
        if [ -n "$detected" ]; then
            ask "      hook용 python(venv 밖 시스템 Python 3.11+, '-' 입력 시 비움) [$detected]: "
            python=${answer:-$detected}
        else
            ask "      hook용 python(비우면 null): "
            python=$answer
        fi
        [ "$python" = "-" ] && python=''
        if [ -z "$python" ]; then
            break
        fi
        case $python in
            /*) if [ -x "$python" ] && [ ! -d "$python" ]; then break; fi ;;
        esac
        echo "      실행 가능한 절대 경로를 입력하세요."
    done
    default_lang=$(helper example-field response_language) || exit 1
    default_comment=$(helper example-field comment_language) || exit 1
    lang=''
    while [ -z "$lang" ]; do
        ask "      응답 언어 [$default_lang]: "
        lang=${answer:-$default_lang}
    done
    comment=''
    while [ -z "$comment" ]; do
        ask "      코드 주석 언어 [$default_comment]: "
        comment=${answer:-$default_comment}
    done
    if confirm "      AWS 읽기 전용 모듈(--with-aws)을 사용합니까?"; then
        helper write-config --project-root "$project" --python "$python" \
            --response-language "$lang" --comment-language "$comment" --aws || exit 1
        echo
        echo "config.local.json 에 견본의 aws 절을 복사했습니다."
        echo "aws.roles(환경별 역할 ARN)·aws.mfa_serial(MFA 장치 ARN)·필요하면 profile·assume_role_envs를 편집한 뒤 ./setup.sh 를 다시 실행하세요."
        exit 0
    fi
    helper write-config --project-root "$project" --python "$python" \
        --response-language "$lang" --comment-language "$comment" || exit 1
fi

# aws 節の状態を表示してから進む（キーの誤記・null は helper がエラーにする）
aws=$(helper aws-mode) || exit 1
echo "      AWS 모듈(--with-aws): $aws"
if [ "$aws" = "placeholder" ]; then
    echo
    echo "config.local.json 의 aws.roles 또는 aws.mfa_serial 이 아직 견본 값입니다."
    echo "실제 값으로 편집한 뒤 ./setup.sh 를 다시 실행하세요(AWS를 쓰지 않으면 aws 절을 지웁니다)."
    exit 0
fi

# --- 4) dry-run ---
echo "[4/5] 설치 예정 내용을 확인합니다(쓰기 없음)."
helper dry-run
status=$?
if [ "$status" -eq 3 ]; then
    echo "[5/5] 설치본이 이미 최신입니다. 할 일이 없습니다."
    exit 0
elif [ "$status" -ne 0 ]; then
    echo "dry-run이 실패했습니다. 위 오류를 확인한 뒤 다시 실행하세요." >&2
    exit 1
fi

apply_command=$(helper apply-command) || exit 1
if [ "$no_apply" -eq 1 ]; then
    echo "[5/5] --no-apply: 적용하지 않았습니다. 적용 명령: $apply_command"
    exit 0
fi

# --- 5) 適用（install.py 自身の端末確認を経る） ---
if ! confirm "[5/5] 위 내용으로 설치를 적용할까요? (다음 확인에서 apply를 입력해야 실제로 씁니다)"; then
    echo "적용하지 않았습니다. 나중에 적용하려면: $apply_command"
    exit 0
fi
py=$(helper python-of) || exit 1
set -- "$here/install/install.py" --config "$here/config.local.json"
if [ "$aws" = "on" ]; then
    set -- "$@" --with-aws
fi
"$py" "$@" --apply
