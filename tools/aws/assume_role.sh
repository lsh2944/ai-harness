#!/bin/sh
# 共用の読み取り専用 AWS プロファイルを MFA で更新する（設定は config.local.json の "aws"）。
# config の aws.assume_role_cmd にこのファイルの絶対パスを指定すると、ガードは登録済みの環境名に限り実行を許可する。
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/assume_role.py" "$@"
