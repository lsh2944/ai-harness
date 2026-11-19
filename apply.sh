#!/bin/sh
# 設置本（~/.claude・~/.codex・~/.agents）への反映を簡単にする（Mac/Linux 用。Windows は未対応）。
# 利用者が端末で ./apply.sh を実行する。完全自動の適用はしない:
#   1) config.local.json の python・aws 節の状態を tools/harness_setup.py で判定
#   2) install.py の dry-run 要約を表示（変更なしなら終了）
#   3) install.py --apply を実行し、install.py 自身の "apply" 入力確認をそのまま経る
# Claude Code・Codex の強制終了・プロセス検出はしない（案内のみ）。
set -u
umask 077

usage() {
    echo "usage: ./apply.sh"
}

for arg in "$@"; do
    case $arg in
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

case $(uname -s 2>/dev/null) in
    MINGW*|MSYS*|CYGWIN*)
        echo "Windows는 아직 지원하지 않습니다(Mac/Linux 전용). README의 설치 절을 참고해 수동으로 실행하세요." >&2
        exit 1 ;;
esac

if [ ! -t 0 ]; then
    echo "apply.sh는 사용자가 터미널에서 직접 실행해야 합니다(stdin이 터미널이 아님). 아무것도 바꾸지 않았습니다." >&2
    exit 1
fi

# スクリプトのある場所（リポジトリ直下）で動く
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
cd "$here" || exit 1

if [ ! -f config.local.json ]; then
    echo "config.local.json 이 없습니다. 이 컴퓨터에서 처음이면 터미널에서 ./setup.sh 를 먼저 실행하세요." >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3를 찾을 수 없습니다. Python 3.11 이상을 설치한 뒤 다시 실행하세요." >&2
    exit 1
fi

helper() {
    python3 tools/harness_setup.py "$@"
}

# aws 節の状態（キーの誤記・null は helper がエラーにする）
aws=$(helper aws-mode) || exit 1
echo "AWS 모듈(--with-aws): $aws"
if [ "$aws" = "placeholder" ]; then
    echo "config.local.json 의 aws.roles 또는 aws.mfa_serial 이 아직 견본 값입니다." >&2
    echo "실제 값으로 편집한 뒤 ./apply.sh 를 다시 실행하세요(AWS를 쓰지 않으면 aws 절을 지웁니다)." >&2
    exit 1
fi
py=$(helper python-of) || exit 1

echo "설치 예정 내용을 확인합니다(쓰기 없음)."
helper dry-run
status=$?
if [ "$status" -eq 3 ]; then
    echo "설치본이 이미 최신입니다. 할 일이 없습니다."
    exit 0
elif [ "$status" -ne 0 ]; then
    echo "dry-run이 실패했습니다. 위 오류를 확인한 뒤 다시 실행하세요." >&2
    exit 1
fi

echo
echo "주의: Claude Code·Codex가 실행 중이면 ~/.claude.json 등을 동시에 써서 적용이 중단될 수 있습니다. 모두 종료한 뒤 적용하세요."
echo "적용하려면 다음 확인에서 apply 를 입력하세요(그 밖의 입력은 취소)."
set -- "$here/install/install.py" --config "$here/config.local.json"
if [ "$aws" = "on" ]; then
    set -- "$@" --with-aws
fi
exec "$py" "$@" --apply
