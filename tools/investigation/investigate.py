#!/usr/bin/env python3
"""Offline evidence aggregation. No shell execution, network, or model calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid

try:
    import pwd
except ImportError:  # Windows
    pwd = None
WINDOWS = os.name == "nt"

# 調査対象のプロジェクトルートは実行時の cwd（プロジェクトルートで実行する）。環境変数での差し替えは設けない。
try:
    ROOT = Path(os.getcwd()).resolve()
except OSError:
    ROOT = Path("/")
# ハーネス本体（hook・インストーラ・設定断片・認証更新スクリプト・利用者設定）は入力にしない
HARNESS_HOME = Path(__file__).resolve().parents[2]
HARNESS_PROTECTED = tuple(HARNESS_HOME / name for name in ("hooks", "install", "settings", "tools/aws", "config.local.json"))


def too_broad_root(path):
    """/・ホームやその上位・一時領域の根のように、調査ルートとして広すぎるか。ホーム配下の隠しディレクトリ内も対象。"""
    path = Path(path).resolve()
    homes = {Path.home().resolve()}
    if pwd is not None:
        try:
            homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve())
        except (KeyError, OSError):
            pass
    broad = {Path(p).resolve() for p in ("/", "/tmp", "/private", "/private/tmp", "/var", "/private/var",
                                         "/var/folders", "/Users", "/home", "/Volumes")}
    if WINDOWS:
        # ドライブの根・C:\Users・一時領域の根も広すぎる
        temp = Path(tempfile.gettempdir()).resolve()
        broad |= {Path(path.anchor), Path(Path.home().anchor) / "Users", temp, temp.parent}
    if path in broad or any(home == path or home.is_relative_to(path) for home in homes):
        return True
    # ホーム配下の隠しディレクトリ（~/.aws・~/.codex・~/.claude・~/.agents 等の設定・認証領域）を含む場所も拒否する
    return any(path.is_relative_to(home) and any(part.startswith(".") for part in path.relative_to(home).parts)
               for home in homes)
MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 20
MAX_JOB_BYTES = 64 * 1024 * 1024
MAX_DISPLAY_CHARS = 8000
CACHE_DIR = ".investigation-cache"
PATTERNS = {
    "error": r"\b(?:error|fatal|exception|traceback|panic)\b",
    "warning": r"\bwarn(?:ing)?\b",
    "failure": r"\b(?:fail(?:ed|ure|ures)?|timeout|timed out|disconnect(?:ed)?)\b",
    "mqtt": r"\bmqtt\b",
    "transport": r"\b(?:srt|gstreamer|gst|packet|retransmit|relay)\b",
    "switch": r"\b(?:failover|switch(?:ed|ing)?)\b",
}
COMPILED = {k: re.compile(v, re.I) for k, v in PATTERNS.items()}
VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


def relative_path(value):
    if not isinstance(value, str) or not value:
        raise ValueError("input paths must be nonempty strings")
    path = Path(value)
    if path.is_absolute():
        path = path.relative_to(ROOT)
    if not path.parts or any(p in ("..", ".") or p.startswith(".") for p in path.parts):
        raise ValueError("hidden paths and parent traversal are not allowed")
    target = (ROOT / path).resolve()
    if path.parts[:2] == ("tools", "harness") or any(
            target == p or p in target.parents for p in HARNESS_PROTECTED) or any(
        re.search(r"credential|secret|token|settings|^id_(rsa|ed25519)|\.(pem|key|p12|pfx)$", p, re.I)
        for p in path.parts
    ):
        raise ValueError("protected input path")
    return path


# ---- ディレクトリ単位のファイル操作 ----
# POSIX は dir_fd と O_NOFOLLOW で要素ごとにリンクを辿らず開く。Windows には dir_fd・O_NOFOLLOW がないため、
# ディレクトリを Path で持ち、要素ごとに lstat でリンク・ジャンクションでないことを確かめる（検査と open の間の
# 差し替えは防げない補助的な検査）。OneDrive のクラウドファイルの再解析ポイントはリンクとみなさない。
def _no_link(path, directory=False):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or getattr(os.path, "isjunction", lambda _p: False)(path):
        raise OSError(f"refusing link: {path.name}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"not a directory: {path.name}")
    return info


def _open_dir(path):
    if WINDOWS:
        _no_link(Path(path), directory=True)
        return Path(path)
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _child_dir(directory, part):
    if WINDOWS:
        return _open_dir(directory / part)
    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    os.close(directory)
    return child


def _close_dir(directory):
    if not WINDOWS:
        os.close(directory)


def _open_in(directory, name, flags, mode=0o777):
    if WINDOWS:
        target = directory / name
        if os.path.lexists(target):
            _no_link(target)
        return os.open(target, flags | os.O_BINARY, mode)
    return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=directory)


def _stat_in(directory, name):
    if WINDOWS:
        return os.stat(directory / name, follow_symlinks=False)
    return os.stat(name, dir_fd=directory, follow_symlinks=False)


def _replace_in(directory, source, target):
    if WINDOWS:
        os.replace(directory / source, directory / target)
    else:
        os.replace(source, target, src_dir_fd=directory, dst_dir_fd=directory)


def _unlink_in(directory, name):
    if WINDOWS:
        os.unlink(directory / name)
    else:
        os.unlink(name, dir_fd=directory)


def read_input(value):
    """Open every path component without following symlinks; bounded regular files only."""
    path = relative_path(value)
    directory = _open_dir(ROOT)
    try:
        for part in path.parts[:-1]:
            directory = _child_dir(directory, part)
        fd = _open_in(directory, path.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
                raise ValueError("input must be a regular file no larger than 32 MiB")
            data = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            # Windows の st_ctime は fstat と stat で意味が揃わない（作成時刻・変更時刻）ため比較に使わない
            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, None if WINDOWS else s.st_ctime_ns)
            current = _stat_in(directory, path.name)
            if len(data) > MAX_BYTES or signature(before) != signature(after) or signature(after) != signature(current):
                raise ValueError("input changed during read or exceeds 32 MiB; retry after it stabilizes")
    finally:
        _close_dir(directory)
    return {"path": str(ROOT / path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}, data


def aggregate(evidence, data):
    content = data.decode("utf-8")  # Invalid text is an error, never silently dropped.
    if "\x00" in content:
        raise ValueError("binary input is not supported")
    groups = {name: {"matching_lines": 0, "sample_lines": []} for name in PATTERNS}
    samples = {}
    lines = content.splitlines()
    for number, line in enumerate(lines, 1):
        for name, pattern in COMPILED.items():
            if pattern.search(line):
                group = groups[name]
                group["matching_lines"] += 1
                if len(group["sample_lines"]) < 3:
                    group["sample_lines"].append(number)
                    samples[number] = {"line": number, "text": line[:280], "text_truncated": len(line) > 280}
    for group in groups.values():
        group["omitted_matching_lines"] = group["matching_lines"] - len(group["sample_lines"])
    return {**evidence, "lines": len(lines), "groups": {k: v for k, v in groups.items() if v["matching_lines"]}, "samples": list(samples.values())}


def cache_directory():
    path = ROOT / CACHE_DIR
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if WINDOWS:
        # 所有者・権限ビットの検査は POSIX のみ（Windows はリンクでないディレクトリであることだけ確かめる）
        return _open_dir(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ValueError("cache directory must be owned by you with mode 700")
    return fd


def write_json(fd, name, value):
    temporary = ".tmp-" + uuid.uuid4().hex
    dest = _open_in(fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(dest, "wb") as stream:
            stream.write(encode(value))
        _replace_in(fd, temporary, name)
    finally:
        try:
            _unlink_in(fd, temporary)
        except FileNotFoundError:
            pass


def cached_result(key, build, enabled):
    if not enabled:
        return build(), "disabled"
    fd = cache_directory()
    name = key + ".json"
    try:
        try:
            source = _open_in(fd, name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(source, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("cache entry must be regular")
                entry = json.loads(stream.read(1024 * 1024))
            payload = entry["payload"]
            digest = hashlib.sha256(encode(payload)).hexdigest()
            if (entry["key"] == key and entry["digest"] == digest
                    and isinstance(payload, dict) and set(payload) == {"files", "dependencies"}
                    and isinstance(payload["files"], list) and isinstance(payload["dependencies"], list)):
                return payload, "hit"
        except (OSError, ValueError, KeyError, TypeError):
            pass  # A broken cache is not evidence; rebuild from verified input bytes.
        result = build()
        entry = {"key": key, "payload": result, "digest": hashlib.sha256(encode(result)).hexdigest()}
        write_json(fd, name, entry)
        return result, "miss"
    finally:
        _close_dir(fd)


def run_job(job, use_cache=True):
    exit_code = job.get("exit_code") if isinstance(job, dict) else None
    valid_exit = type(exit_code) is int and 0 <= exit_code <= 255
    status = {"reported_exit_code": exit_code if valid_exit else None,
              "command_status": ("success" if exit_code == 0 else "failure") if valid_exit else "unknown"}
    try:
        if not isinstance(job, dict) or set(job) - {"name", "files", "dependencies", "exit_code", "known_key"}:
            raise ValueError("job accepts only name, files, dependencies, exit_code, known_key")
        if "name" in job and (not isinstance(job["name"], str) or len(job["name"]) > 128):
            raise ValueError("job name must be a string of at most 128 characters")
        files, dependencies = job.get("files"), job.get("dependencies", [])
        if not isinstance(files, list) or not files or not isinstance(dependencies, list) or len(files) + len(dependencies) > MAX_FILES:
            raise ValueError("provide 1–20 explicit input/dependency paths per job; globs are not expanded")
        exit_code = job.get("exit_code")
        if exit_code is not None and (type(exit_code) is not int or not 0 <= exit_code <= 255):
            raise ValueError("exit_code must be null or an integer from 0 to 255")
        known_key = job.get("known_key")
        if known_key is not None and (not isinstance(known_key, str) or not re.fullmatch(r"[0-9a-f]{64}", known_key)):
            raise ValueError("known_key must be an evidence_key from a previously inspected result")
        sources, dep_evidence, total_bytes = [], [], 0
        for index, path in enumerate(files + dependencies):
            info, data = read_input(path)
            total_bytes += len(data)
            if total_bytes > MAX_JOB_BYTES:
                raise ValueError("job inputs/dependencies exceed 64 MiB; narrow the explicit file set")
            if index < len(files):
                sources.append((info, data))
            else:
                dep_evidence.append(info)
        identity = {"version": VERSION, "files": [s[0] for s in sources], "dependencies": dep_evidence, "exit_code": exit_code}
        key = hashlib.sha256(encode(identity)).hexdigest()
        if known_key == key:
            return {"name": job.get("name", "summary"), "ok": True, "unchanged": True,
                    "evidence_key": key, "reuse_previous_evidence": True, **status}
        result, cache = cached_result(key, lambda: {
            "files": [aggregate(info, data) for info, data in sources],
            "dependencies": dep_evidence,
        }, use_cache)
        # Exit status belongs to this invocation, never to cached evidence.
        return {**result, "name": job.get("name", "summary"), "ok": True,
                "cache": cache, "evidence_key": key, **status}
    except (OSError, ValueError, TypeError) as exc:
        name = job.get("name", "summary") if isinstance(job, dict) else "invalid"
        return {"name": name[:128] if isinstance(name, str) else "invalid", "ok": False, "error": str(exc), **status}


def display_report(results):
    report = {"notice": "Keyword counts are evidence, not root-cause or test-pass conclusions. Groups overlap. Samples are incomplete; inspect original files using the supplied lines/hash when needed. Exit codes are caller-reported. Cache covers only explicit inputs/dependencies.", "jobs": results}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if len(output) <= MAX_DISPLAY_CHARS:
        return output, True
    name = "report-" + hashlib.sha256(encode(report)).hexdigest() + ".json"
    storage_error = None
    try:
        fd = cache_directory()
        try:
            write_json(fd, name, report)
        finally:
            _close_dir(fd)
    except (OSError, ValueError) as exc:
        storage_error = str(exc)[:600]
    compact = []
    for index, result in enumerate(results):
        row = {k: result[k] for k in ("name", "ok", "reported_exit_code", "command_status", "evidence_key", "unchanged") if k in result}
        row["report_job_index"] = index
        if "error" in result:
            row.update(error=result["error"][:300], error_truncated=len(result["error"]) > 300)
        compact.append(row)
    response = {"output_truncated": True, "full_report": str(ROOT / CACHE_DIR / name) if storage_error is None else None,
                "notice": "Inspect required job details in full_report before drawing conclusions. Original logs remain at the paths in that report.",
                "jobs": compact}
    if storage_error is not None:
        response.update(report_error=storage_error, omitted_evidence_unavailable=True,
                        notice="Could not preserve full report. Fix report storage and rerun before drawing conclusions; all job statuses are retained below.")
    return json.dumps(response, ensure_ascii=False, indent=2), storage_error is None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-cache", action="store_true")
    sub = parser.add_subparsers(dest="mode", required=True)
    summary = sub.add_parser("summary")
    summary.add_argument("files", nargs="+")
    summary.add_argument("--dependency", action="append", default=[])
    summary.add_argument("--exit-code", type=int)
    summary.add_argument("--known-key", help="evidence_key of a result already inspected in this investigation")
    batch = sub.add_parser("batch")
    batch.add_argument("manifest", help="project-local JSON list of independent jobs (maximum 8)")
    args = parser.parse_args()
    try:
        if too_broad_root(ROOT):
            raise ValueError("working root is too broad; run from a project directory")
        if args.mode == "summary":
            jobs = [{"files": args.files, "dependencies": args.dependency, "exit_code": args.exit_code, "known_key": args.known_key}]
        else:
            jobs = json.loads(read_input(args.manifest)[1])
            if not isinstance(jobs, list) or not 1 <= len(jobs) <= 8:
                raise ValueError("batch manifest must contain 1–8 jobs")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda job: run_job(job, not args.no_cache), jobs))
        display, preserved = display_report(results)
        print(display)
        return 1 if not preserved or any(not r["ok"] or r.get("command_status") == "failure" for r in results) else 0
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if WINDOWS:
        # ログの日本語・韓国語を既定コードページ（cp932 等）で出力すると失敗するため UTF-8 に固定する
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
