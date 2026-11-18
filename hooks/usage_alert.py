#!/usr/bin/env python3
"""Claude/Codex の使用量警告。モデル呼び出し・自動続行・強制停止は行わない。"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

INTERVAL = 30
SCAN_BYTES = 8 * 1024 * 1024
# キャッシュ読み取りを新規入力と区別し、推論トークンは出力に二重加算しない。
# 1M コンテキストでは 1 回の呼び出しで 10〜20 万のキャッシュ読みが通常なので、cache は高めに置く。
TURN_LIMITS = {"input": 100_000, "cache": 5_000_000, "output": 20_000}
SESSION_LIMITS = {"input": 500_000, "cache": 50_000_000, "output": 100_000}
LABELS = {"input": "새 입력", "cache": "캐시 읽기", "output": "출력(추론 포함)"}


def zeros():
    return dict.fromkeys(TURN_LIMITS, 0)


def counts(u, runtime):
    if runtime == "claude":
        return {"input": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                "cache": u.get("cache_read_input_tokens") or 0, "output": u.get("output_tokens") or 0}
    return {"input": max(0, (u.get("input_tokens") or 0) - (u.get("cached_input_tokens") or 0)),
            "cache": u.get("cached_input_tokens") or 0, "output": u.get("output_tokens") or 0}


def scan(path, saved, runtime):
    """追加分のみ読む。部分行は次回に残し、ストリーム再送は message id で置換する。"""
    stat = path.stat()
    identity = [stat.st_dev, stat.st_ino]
    if saved.get("identity") != identity or saved.get("offset", 0) > stat.st_size:
        saved.clear()
    saved.setdefault("identity", identity)
    saved.setdefault("seen", {})
    saved.setdefault("totals", zeros())
    with path.open("rb") as handle:
        handle.seek(saved.get("offset", 0))
        consumed = 0
        while consumed < SCAN_BYTES:
            start = handle.tell()
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                handle.seek(start)
                break
            consumed += len(line)
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            if runtime == "claude":
                message = row.get("message") or {}
                usage = message.get("usage")
                if row.get("type") == "assistant" and isinstance(usage, dict):
                    key = message.get("id") or str(start)
                    previous = saved["seen"].get(key, zeros())
                    current = counts(usage, runtime)
                    for k in current:
                        saved["totals"][k] += current[k] - previous[k]
                    saved["seen"][key] = current
                    saved["has_usage"] = True
                if row.get("type") == "cost-state" and isinstance(row.get("totalCostUSD"), (float, int)):
                    saved["cost"] = row["totalCostUSD"]
            else:
                payload = row.get("payload") or {}
                if row.get("type") == "event_msg" and payload.get("type") == "token_count":
                    usage = (payload.get("info") or {}).get("total_token_usage")
                elif row.get("type") == "token_usage_record":
                    usage = payload.get("thread_token_usage")
                else:
                    usage = None
                if isinstance(usage, dict):
                    # 二つのイベント形式は同じ累積値なので加算しない。
                    saved["totals"] = counts(usage, runtime)
                    saved["has_usage"] = True
        saved["offset"] = handle.tell()
    return saved["totals"]


def messages(state, totals, event, cost=None):
    notes = []
    baseline = state.setdefault("baseline", zeros())
    notified = state.setdefault("notified", {})
    for scope, values, limits in (("turn", {k: max(0, totals[k] - baseline[k]) for k in totals}, TURN_LIMITS),
                                  ("session", totals, SESSION_LIMITS)):
        for k, limit in limits.items():
            bucket = values[k] // limit
            key = scope + ":" + k
            if bucket > notified.get(key, 0):
                notified[key] = bucket
                notes.append(f"{'이번 요청' if scope == 'turn' else '세션 누적'} {LABELS[k]} {values[k]:,} 토큰 (알림 기준 {limit:,})")
    if cost is not None:
        bucket = int(cost // 10)
        if bucket > notified.get("usd", 0):
            notified["usd"] = bucket
            notes.append(f"Claude 기록 누적 ${cost:.1f} (구독 잔여량·실제 청구액 아님)")
    if event == "Stop":
        state["baseline"] = totals.copy()
        for k in TURN_LIMITS:
            notified.pop("turn:" + k, None)
    return notes


def monitor(data, runtime, directory, now=None):
    now = time.time() if now is None else now
    event = data.get("hook_event_name", "Stop")
    transcript = data.get("transcript_path")
    if not transcript or not Path(transcript).is_file():
        return {}
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    sid = hashlib.sha256((runtime + ":" + str(data.get("session_id") or transcript)).encode()).hexdigest()[:32]
    filename = directory / (sid + ".json")
    fd = os.open(directory / (sid + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {}
        try:
            state = json.loads(filename.read_text())
        except (OSError, ValueError):
            state = {}
        if event == "PreToolUse" and now - state.get("checked", 0) < INTERVAL:
            return {}
        state["checked"] = now
        paths = {str(Path(transcript))}
        if runtime == "claude":
            paths.update(str(p) for p in Path(transcript).with_suffix("").glob("subagents/*.jsonl"))
        child = data.get("agent_transcript_path")
        if child and Path(child).is_file():
            state.setdefault("children", [])
            if child not in state["children"] and child != transcript:
                state["children"].append(child)
        paths.update(state.get("children", []))
        files = state.setdefault("files", {})
        if event == "UserPromptSubmit":
            state["baseline_pending"] = {name: Path(name).stat().st_size for name in paths if Path(name).is_file()}
        for name in paths:
            try:
                scan(Path(name), files.setdefault(name, {}), runtime)
            except OSError:
                continue
        totals = {k: sum(f.get("totals", {}).get(k, 0) for f in files.values()) for k in TURN_LIMITS}
        cost = files.get(str(Path(transcript)), {}).get("cost")
        pending = state.get("baseline_pending", {})
        warming = bool(pending)
        state["baseline_pending"] = {name: size for name, size in pending.items()
                                     if files.get(name, {}).get("offset", 0) < size and Path(name).exists()}
        if event == "UserPromptSubmit" or warming:
            state["baseline"] = totals.copy()
            for k in TURN_LIMITS:
                state.setdefault("notified", {}).pop("turn:" + k, None)
            notes = []
        else:
            notes = messages(state, totals, event, cost)
        if event == "Stop" and not any(f.get("has_usage") for f in files.values()) and not state.get("unsupported"):
            notes.append("사용량 기록을 집계할 수 없어 이번 세션의 토큰 경고가 제한됩니다.")
            state["unsupported"] = True
        fd, temp = tempfile.mkstemp(dir=directory, prefix=".usage-")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle)
            os.replace(temp, filename)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        if notes:
            return {"systemMessage": "기록된 토큰 사용량 주의: " + " / ".join(notes)
                    + " — 로컬 기록 기준이며 계정 한도·잔여량·사용률을 뜻하지 않습니다. 경고가 없어도 계정 한도는 소모될 수 있습니다. 알림만 제공하며 모델·추론·필수 검증은 유지합니다."}
        return {}


def main():
    runtime = "codex" if "--codex" in sys.argv else "claude"
    data = json.load(sys.stdin)
    directory = Path.home() / (".codex" if runtime == "codex" else ".claude") / "hooks" / ".usage_state_v2"
    print(json.dumps(monitor(data, runtime, directory), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 警告の失敗でモデルを再実行しない。ユーザーには機能低下を伝える。
        print(json.dumps({"systemMessage": "사용량 집계 실패: 토큰 경고가 제한되며 계정 잔여량은 판단할 수 없습니다. 작업은 계속됩니다."}, ensure_ascii=False))
