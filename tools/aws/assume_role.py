#!/usr/bin/env python3
"""Refresh the shared AWS profile without exposing credentials or MFA in argv."""

import argparse
try:
    import fcntl
except ImportError:  # Windows（認証情報の書き込みは未対応。save_credentials が案内して止まる）
    fcntl = None
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time


# ロール・MFA・更新先プロファイルは利用者設定（config.local.json の "aws"）から読む。
# 読むのはこのリポジトリ直下の config.local.json（ガードの保護対象）だけ。差し替え手段は設けない。
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.local.json"
ROLE_ARN = re.compile(r"arn:aws[a-z-]*:iam::[0-9]{12}:role/[\w+=,.@/-]{1,512}")
MFA_SERIAL = re.compile(r"arn:aws[a-z-]*:iam::[0-9]{12}:mfa/[\w+=,.@/-]{1,512}")
ENV_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
PROFILE_NAME = re.compile(r"[A-Za-z0-9_.+-]{1,128}")
ROLES = {}
MFA_ARN = None
PROFILE = None
SECTION = re.compile(r"^\s*\[([^\]\r\n]+)\]\s*(?:[#;].*)?$")
KEY = re.compile(r"^\s*(aws_access_key_id|aws_secret_access_key|aws_session_token)\s*=", re.I)


def load_config(path):
    """aws.profile・aws.roles・aws.mfa_serial を検証して (roles, mfa, profile) を返す。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError(f"Cannot read harness config: {path}") from None
    section = data.get("aws") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise ValueError('Harness config needs an "aws" object')
    roles, mfa, profile = section.get("roles"), section.get("mfa_serial"), section.get("profile")
    if not isinstance(roles, dict) or not roles or not all(
            isinstance(k, str) and ENV_NAME.fullmatch(k) and isinstance(v, str) and ROLE_ARN.fullmatch(v)
            for k, v in roles.items()):
        raise ValueError("aws.roles must map environment names to IAM role ARNs")
    if not isinstance(mfa, str) or not MFA_SERIAL.fullmatch(mfa):
        raise ValueError("aws.mfa_serial must be an IAM MFA device ARN")
    if not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile) or profile == "default":
        raise ValueError("aws.profile is invalid (the default profile cannot be overwritten)")
    return dict(roles), mfa, profile


def assume_role(environment, token):
    payload = {
        "RoleArn": ROLES[environment],
        "SerialNumber": MFA_ARN,
        "RoleSessionName": f"{int(time.time())}-assumed-role-via-mac",
        "TokenCode": token,
    }
    # Resolve the source through default, without inherited credentials, config
    # paths, alternate credential providers, or endpoint overrides.
    env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    env.update(AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off")
    # AWS CLI v2 rejects --cli-input-json file:///dev/stdin ("Invalid JSON received"),
    # so pass the payload through a private 0600 file that is removed right after.
    directory = tempfile.mkdtemp(prefix="assume-role-")
    path = os.path.join(directory, "input.json")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        result = subprocess.run(
            ["aws", "sts", "assume-role", "--profile", "default", "--output", "json",
             "--cli-input-json", f"file://{path}"],
            stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=60, env=env,
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if result.returncode:
        raise ValueError("AWS assume-role failed; credentials were not updated")
    try:
        credentials = json.loads(result.stdout)["Credentials"]
        rules = {
            "AccessKeyId": r"[A-Z0-9]{16,128}",
            "SecretAccessKey": r"[A-Za-z0-9/+=]{40}",
            "SessionToken": r"[A-Za-z0-9/+=]{1,16384}",
        }
        for key, pattern in rules.items():
            value = credentials[key]
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise ValueError("invalid credentials")
    except (ValueError, KeyError, TypeError):
        raise ValueError("AWS returned invalid credentials; credentials were not updated") from None
    return dict(zip(
        ("aws_access_key_id", "aws_secret_access_key", "aws_session_token"),
        (credentials["AccessKeyId"], credentials["SecretAccessKey"], credentials["SessionToken"]),
    ))


def update_profile(raw, credentials):
    """Replace credential assignments while retaining unrelated text verbatim."""
    lines = raw.splitlines(keepends=True)
    sections = [(i, match.group(1).strip()) for i, line in enumerate(lines)
                if (match := SECTION.fullmatch(line.rstrip("\r\n")))]
    matches = [i for i, name in sections if name == PROFILE]
    if len(matches) > 1:
        raise ValueError("Duplicate shared profile; credentials were not updated")
    newline = "\r\n" if "\r\n" in raw else "\n"
    if not matches:
        prefix = raw + (newline if raw and not raw.endswith(("\r", "\n")) else "")
        return prefix + f"[{PROFILE}]" + newline + "".join(
            f"{key} = {value}{newline}" for key, value in credentials.items())
    start = matches[0]
    end = next((i for i, _ in sections if i > start), len(lines))
    block = [lines[start]]
    seen = set()
    for line in lines[start + 1:end]:
        match = KEY.match(line)
        if match:
            key = match.group(1).lower()
            if key not in seen:
                block.append(f"{key} = {credentials[key]}{newline}")
                seen.add(key)
        else:
            block.append(line)
    if block and not block[-1].endswith(("\r", "\n")):
        block[-1] += newline
    block.extend(f"{key} = {value}{newline}" for key, value in credentials.items() if key not in seen)
    return "".join(lines[:start] + block + lines[end:])


def save_credentials(credentials):
    if fcntl is None:
        # dir_fd・O_NOFOLLOW・flock を前提にした書き込みのため、Windows ではまだ使えない（何も書かない）
        raise SystemExit("assume_role.py の認証情報の書き込みは Windows では未対応です（Mac/Linux で実行してください）。")
    directory = Path.home() / ".aws"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    # Directory-relative operations keep writes in the validated directory.
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    temporary = None
    try:
        os.fchmod(dir_fd, 0o700)
        lock_fd = os.open(".credentials.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                          0o600, dir_fd=dir_fd)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise ValueError("Credentials lock must be a regular file")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        raw = ""
        try:
            existing = os.stat("credentials", dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError("Credentials must be a regular file, not a symlink")
            fd = os.open("credentials", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            with os.fdopen(fd, "r", encoding="utf-8", newline="") as stream:
                raw = stream.read()
        updated = update_profile(raw, credentials)
        temporary = f".credentials-{secrets.token_hex(16)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=dir_fd)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, "credentials", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=dir_fd)
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(dir_fd)


def main():
    global ROLES, MFA_ARN, PROFILE
    os.umask(0o077)
    try:
        ROLES, MFA_ARN, PROFILE = load_config(DEFAULT_CONFIG)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--awsEnv", required=True, choices=ROLES)
    args = parser.parse_args()
    try:
        token = getpass.getpass("MFA code: ") if sys.stdin.isatty() else sys.stdin.readline().strip()
        if not re.fullmatch(r"[0-9]{6}", token):
            raise ValueError("MFA code must contain exactly six digits")
        save_credentials(assume_role(args.awsEnv, token))
    except (OSError, ValueError, subprocess.SubprocessError, EOFError) as exc:
        # AWS output and exception details may contain secrets; never echo them.
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else "Unable to refresh credentials"
        print(f"Error: {message}", file=sys.stderr)
        return 1
    print(f"✅ ~/.aws/credentials [{PROFILE}] 업데이트 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
