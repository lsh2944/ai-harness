"""prune-memory: Claude 정리 결과를 Codex에 읽기 전용으로 검증시키는 연결 스크립트.

1) 백업(원본)과 현재 memory(후보)를 해시로 비교해 변경 목록을 만들고, 기계적 검사
   (미변경 파일 동일성·링크·name/파일명·인덱스 포함)를 직접 수행한다. 실패하면 Codex를 부르지 않는다.
2) 변경 파일·통합 원본·1단계 참조 파일·MEMORY.md만 임시 폴더에 고정 복사하고
   `codex exec --sandbox read-only`로 의미 검증을 시킨다. 모델·추론은 Codex 설정을 그대로 쓴다.
종료 코드: 0=PASS(또는 변경 없음), 1=FAIL, 2=INCOMPLETE·실행 실패·타임아웃.
"""
import argparse, hashlib, importlib.util, json, os, pathlib, re, shutil, stat, subprocess, sys, tempfile, time

HERE = pathlib.Path(__file__).parent
# 도구 무효화 키·실행 감시·프로세스 그룹 정지·신호 처리는 cross-check 의 정의를 그대로 쓴다.
_spec = importlib.util.spec_from_file_location(
    "cross_check", HERE.resolve().parents[1] / "cross-check/scripts/cross_check.py")
CC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CC)
# 묶음 읽기용 셸 실행만 허용하고 MCP·웹 검색·파일 변경 등 그 밖의 item 은 정지한다.
VERIFY_ITEMS = CC.ALLOWED_ITEMS | {"command_execution", "todo_list"}
DEFAULT_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
WIKI = re.compile(r"\[\[([^\]]+)\]\]")
MDLINK = re.compile(r"\]\(([^)#\s]+\.md)")


def load(d):
    # memory Markdown만 대상(.backup_* 등 하위 폴더는 제외)
    return {p.name: p.read_bytes() for p in sorted(pathlib.Path(d).glob("*.md"))}


def sha(b):
    return hashlib.sha256(b).hexdigest()


def names_of(files):
    # [[name]]은 frontmatter name 또는 파일명(확장자 제외)으로 해석한다
    out = {}
    for fn, b in files.items():
        out[fn[:-3]] = fn
        m = re.search(r"^name:\s*(\S+)", b.decode(errors="replace"), re.M)
        if m:
            out[m.group(1)] = fn
    return out


def refs(text, names):
    found = {names[n] for n in WIKI.findall(text) if n in names}
    found |= {p for p in MDLINK.findall(text)}
    return found


def mechanical_checks(cand, retained=()):
    # retained: 삭제 승인 대기 중인 통합 원본(인덱스에서 빠져도 정상)
    problems = []
    names = names_of(cand)
    for fn, b in cand.items():
        s = b.decode(errors="replace")
        for n in WIKI.findall(s):
            if n not in names:
                problems.append(f"broken_reference: {fn} -> [[{n}]]")
        for p in MDLINK.findall(s):
            if p not in cand:
                problems.append(f"broken_reference: {fn} -> ({p})")
        m = re.search(r"^name:\s*(\S+)", s, re.M)
        if fn != "MEMORY.md" and m and m.group(1) != fn[:-3]:
            problems.append(f"name_mismatch: {fn} name={m.group(1)}")
    idx = cand.get("MEMORY.md")
    if idx is None:
        problems.append("index: MEMORY.md 없음")
    else:
        listed = set(MDLINK.findall(idx.decode(errors="replace")))
        for fn in cand:
            if fn != "MEMORY.md" and fn not in listed and fn not in retained:
                problems.append(f"index: {fn} 가 MEMORY.md 에 없음")
    return problems


def make_readonly(root):
    for p in pathlib.Path(root).rglob("*"):
        p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def tree_hash(root):
    h = hashlib.sha256()
    for p in sorted(pathlib.Path(root).rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode()); h.update(p.read_bytes())
    return h.hexdigest()


def finish(out, summary):
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return {"PASS": 0, "NO_CHANGES": 0, "FAIL": 1}.get(summary["verdict"], 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory", required=True, help="정리 후 대상 memory 폴더(후보)")
    ap.add_argument("--backup", required=True, help="이번 정리 직전 백업 폴더(원본)")
    ap.add_argument("--mapping", help="Claude가 쓴 통합 원본→결과 매핑 Markdown(통합이 있을 때)")
    ap.add_argument("--out", help="결과 폴더(기본: 새 임시 폴더)")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--dry-run", action="store_true", help="묶음만 만들고 Codex는 호출하지 않는다")
    a = ap.parse_args()

    out = pathlib.Path(a.out) if a.out else pathlib.Path(tempfile.mkdtemp(prefix="prune-verify-"))
    out.mkdir(parents=True, exist_ok=True)
    codex = os.environ.get("CODEX_BIN") or (DEFAULT_CODEX if os.path.exists(DEFAULT_CODEX) else shutil.which("codex"))
    for need, label in ((codex, "codex CLI"), (a.memory, "memory"), (a.backup, "backup"), (a.mapping or a.memory, "mapping")):
        if not need or not os.path.exists(need):
            return finish(out, {"verdict": "INCOMPLETE", "error": f"{label} not found: {need}"})

    base, cand = load(a.backup), load(a.memory)
    if "MEMORY.md" not in base:
        return finish(out, {"verdict": "INCOMPLETE", "error": "백업에 MEMORY.md 없음"})
    changed = sorted(f for f in base.keys() & cand.keys() if sha(base[f]) != sha(cand[f]))
    added = sorted(cand.keys() - base.keys())
    removed = sorted(base.keys() - cand.keys())
    unchanged = sorted(f for f in base.keys() & cand.keys() if f not in changed)
    lists = {"changed": changed, "added": added, "removed": removed, "unchanged_count": len(unchanged)}
    if not (changed or added or removed):
        return finish(out, {"verdict": "NO_CHANGES", **lists})

    mapping = pathlib.Path(a.mapping).read_text() if a.mapping else ""
    sources = {f for f in re.findall(r"[\w.-]+\.md", mapping) if f in base or f in cand}

    # 1) 기계적 검사: 실패 시 Codex를 부르지 않는다
    problems = mechanical_checks(cand, retained=sources & set(unchanged))
    if problems:
        return finish(out, {"verdict": "FAIL", "stage": "mechanical", "problems": problems, **lists})

    # 2) Codex 입력 범위: 변경·추가·삭제 + 통합 원본(매핑에 나온 파일) + 1단계 참조 + MEMORY.md
    core = set(changed) | set(added) | set(removed) | sources | {"MEMORY.md"}
    bnames, cnames = names_of(base), names_of(cand)
    neighbors = set()
    # MEMORY.md는 전 파일을 가리키므로 바뀐 포인터 줄의 링크만 따라간다
    bl = set(base["MEMORY.md"].decode(errors="replace").splitlines())
    cl = set(cand.get("MEMORY.md", b"").decode(errors="replace").splitlines())
    for line in bl ^ cl:
        neighbors |= set(MDLINK.findall(line))
    for f in core - {"MEMORY.md"}:
        if f in cand:
            neighbors |= refs(cand[f].decode(errors="replace"), cnames)
        if f in base:
            neighbors |= refs(base[f].decode(errors="replace"), bnames)
    core_names = {n for n, fn in {**bnames, **cnames}.items() if fn in core}
    for f, b in cand.items():
        if set(WIKI.findall(b.decode(errors="replace"))) & core_names:
            neighbors.add(f)
    neighbors = {f for f in neighbors if f in cand or f in base} - core

    bundle = out / "bundle"
    for side, files in (("baseline", base), ("candidate", cand)):
        d = bundle / side / "memory"; d.mkdir(parents=True)
        for f in sorted(core | neighbors):
            if f in files:
                (d / f).write_bytes(files[f])
    role = lambda f: "변경" if f in changed else "추가" if f in added else "삭제" if f in removed else \
        "통합 원본" if f in sources else "인덱스" if f == "MEMORY.md" else "참조 관계(변경 없음)"
    rows = "\n".join(f"| `{f}` | {role(f)} |" for f in sorted(core | neighbors))
    (bundle / "changes.md").write_text(
        "# 변경 목록 (스크립트가 백업과 해시 비교로 생성)\n\n"
        f"- 변경 {len(changed)} / 추가 {len(added)} / 삭제 {len(removed)} / 변경 없음 {len(unchanged)}\n"
        "- 스크립트 사전 검사 통과: 묶음 밖 미변경 파일의 해시 동일, 전체 `[[링크]]`·Markdown 링크 유효, "
        "frontmatter name=파일명, 전 파일 MEMORY.md 포함.\n"
        "- 묶음에는 아래 파일만 있다. 묶음 밖 파일은 위 사전 검사로 확인됐으므로 검증 범위에서 제외한다.\n\n"
        f"| 파일 | 역할 |\n| --- | --- |\n{rows}\n\n"
        "## Claude 매핑(통합 원본→결과)\n\n" + (mapping or "통합 없음\n"))
    make_readonly(bundle)
    if a.dry_run:
        return finish(out, {"verdict": "DRY_RUN", "bundle": sorted(core | neighbors), **lists})
    before = tree_hash(bundle)

    last = out / "last_message.json"
    # cross-check と同じ道具無効化キー（唯一の定義）。묶음 읽기에 셸만 필요하므로 shell_tool 은 남긴다.
    cmd = [codex, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "-C", str(bundle),
           "--json", "--output-schema", str(HERE / "verify_schema.json"), "-o", str(last)]
    for key in CC.DISABLE_CONFIG:
        cmd += ["-c", key]
    cmd.append((HERE / "verify_prompt.md").read_text())
    event_path, error_path = out / "events.jsonl", out / "stderr.txt"
    t0 = time.time()
    rc, stopped = None, None
    with CC.signal_guard(), open(event_path, "wb") as so, open(error_path, "wb") as se:
        # guard hook(policy=read)이 셸·Read 이외 도구를 실행 전에 거부하고, 감시가 그 밖의 도구 item 을 잡아 그룹째 정지한다.
        proc = subprocess.Popen(cmd, stdout=so, stderr=se, stdin=subprocess.DEVNULL,
                                start_new_session=True, env=CC.codex_env("read"))
        watch = CC.LiveWatch(event_path, error_path, VERIFY_ITEMS)
        try:
            while proc.poll() is None:
                CC.check_signal()
                stopped = watch.poll()
                if not stopped and time.time() - t0 >= a.timeout:
                    stopped = "TIMEOUT"
                if not stopped and event_path.stat().st_size + error_path.stat().st_size > CC.MAX_LOGS:
                    stopped = "execution logs exceeded 8 MiB"
                if stopped:
                    break
                time.sleep(0.1)
            CC.stop_group(proc)
            rc = proc.returncode
            CC.check_signal()
        except CC.RunnerSignal as sig:
            CC.stop_group(proc)
            stopped = f"runner received signal {sig.args[0]}"
        except BaseException:
            CC.stop_group(proc)
            raise
    secs = round(time.time() - t0, 1)

    problems = [stopped] if stopped else []
    if not stopped and (p := watch.poll(to_eof=True)):
        problems.append(p)
    usage, event_problems = CC.inspect_events(event_path, VERIFY_ITEMS)
    problems += event_problems
    thread = None
    for line in event_path.read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == "thread.started":
            thread = e.get("thread_id")

    summary = {"exit": rc, "seconds": secs, "thread_id": thread, "usage": usage, "out": str(out),
               "problems": problems,
               "bundle_files": len(core | neighbors), "bundle_unchanged": tree_hash(bundle) == before, **lists}
    try:
        res = json.loads(last.read_text())
        ok = res.get("verdict") in ("PASS", "FAIL", "INCOMPLETE") and res.get("checked_files")
    except (OSError, ValueError):
        res, ok = None, False
    # 종료 코드 0만으로 합격 처리하지 않는다
    if rc != 0 or problems or not ok or not summary["bundle_unchanged"]:
        summary["verdict"] = "INCOMPLETE"
        summary["error"] = "codex 실행 실패·타임아웃·도구 정책 위반/hook 거부·결과 형식 불량·입력 변경 중 하나"
    else:
        summary.update(verdict=res["verdict"], stage="codex", findings=res["findings"],
                       unverified=res["unverified"], checked_files=len(res["checked_files"]))
    return finish(out, summary)


if __name__ == "__main__":
    try:
        code = main()
    except (Exception, KeyboardInterrupt) as error:
        # 예기치 않은 실패를 FAIL(1)로 오인하지 않도록 INCOMPLETE(2)로 통일한다.
        print(json.dumps({"verdict": "INCOMPLETE", "error": f"{type(error).__name__}: {error}"[:500]},
                         ensure_ascii=False, indent=2))
        code = 2
    sys.exit(code)
