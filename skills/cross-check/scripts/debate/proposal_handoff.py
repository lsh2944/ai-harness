"""読み取り専用 architect の JSON メッセージを、再作文せず登録済み提案へ変換する。"""
import copy
import json
import os
from pathlib import Path
import re
import shlex


LOCAL = re.compile(r'local:[A-Za-z][A-Za-z0-9_-]{0,63}')
# Agent 結果の output_file は tasks/<id>.output から subagents/agent-<id>.jsonl への symlink（2026-11-20 実測）。
AGENT_TRANSCRIPT = re.compile(r'agent-[A-Za-z0-9_-]{1,128}\.jsonl')
AGENT_OUTPUT_NAME = re.compile(r'([A-Za-z0-9_-]{1,128})\.output')
AGENT_OUTPUT_MAX = 32 * 1024 * 1024


def agent_message(co, agent_output):
    """architect の transcript から最終報告を原文のまま取り出す（SubagentHandback 優先、なければ最後の text）。

    形式は公開仕様ではないので、見つからなければ推測せず拒否する（主側の手動保存は従来どおり使える）。
    """
    given = Path(agent_output)
    if not given.is_absolute():
        raise co.Refused('--agent-output must be the absolute output_file path returned by the Agent tool')
    # 渡された <id>.output と実体 agent-<id>.jsonl の id が一致するときだけ読む（別 agent の応答を保存しない）。
    match = AGENT_OUTPUT_NAME.fullmatch(given.name)
    try:
        resolved = given.resolve(strict=True)
    except (OSError, RuntimeError):
        raise co.Refused('agent output path cannot be resolved') from None
    if (match is None or resolved.parent.name != 'subagents' or not AGENT_TRANSCRIPT.fullmatch(resolved.name)
            or resolved.name != f'agent-{match.group(1)}.jsonl'):
        raise co.Refused('agent output must be <id>.output resolving to subagents/agent-<id>.jsonl with the same id')
    try:
        # FIFO 等で open が止まらないよう O_NONBLOCK（read_source と同じ）。
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise co.Refused('agent output transcript cannot be opened') from None
    try:
        info = os.fstat(fd)
        if not co.stat.S_ISREG(info.st_mode):
            raise co.Refused('agent output transcript must be a regular file')
        chunks, total = [], 0
        # fstat 後に伸びても上限 +1 バイトまでしか読まない。
        while total <= AGENT_OUTPUT_MAX:
            chunk = os.read(fd, min(1 << 20, AGENT_OUTPUT_MAX + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > AGENT_OUTPUT_MAX:
            raise co.Refused('agent output transcript is larger than 32 MiB; save the response manually and report')
    finally:
        os.close(fd)
    handback = text = None
    for number, line in enumerate(b''.join(chunks).splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            # 壊れた行を飛ばすと直前の別応答を黙って保存しうるので推測せず拒否する。
            raise co.Refused(f'agent output transcript line {number} is not valid JSON; do not guess, '
                             'save the response manually and report') from None
        if not isinstance(entry, dict) or entry.get('type') != 'assistant':
            continue
        content = (entry.get('message') or {}).get('content') if isinstance(entry.get('message'), dict) else None
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'tool_use' and item.get('name') == 'SubagentHandback':
                value = (item.get('input') or {}).get('message') if isinstance(item.get('input'), dict) else None
                if isinstance(value, str) and value.strip():
                    handback = value
            elif item.get('type') == 'text' and isinstance(item.get('text'), str) and item['text'].strip():
                text = item['text']
    if handback is not None:
        return handback, 'SubagentHandback'
    if text is not None:
        return text, 'text'
    raise co.Refused('no final architect message found in the agent output; do not guess, save the response manually and report')


def capture(co, session, agent_output, output):
    """architect の最終報告を、文字を足さず新しい別ファイルに 1 回だけ保存する。登録・凍結はしない。"""
    sid = session['sid']
    co.require_state(session, 'CODEX_PROPOSED')
    if session.get('proposal_handoff') is not None:
        raise co.Refused('architect handoff already prepared; do not capture again')
    call = co.phase_call(session, 'proposal')
    if not call or not call.get('ended_at') or call.get('execution') != 'ok':
        raise co.Refused('capture requires a successfully completed Codex proposal; wait for it before saving the architect response')
    message, source = agent_message(co, agent_output)
    try:
        data = message.encode('utf-8')
    except UnicodeEncodeError:
        raise co.Refused('architect message is not encodable as UTF-8; save the response manually and report') from None
    if len(data) > co.MAX_DOC * 4:
        raise co.Refused('architect message exceeds the handoff size limit; preserve it and report')
    target = co.submission_draft_target(output)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise co.Refused('capture output already exists; captures never overwrite files') from None
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('short write')
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return {'status': 'ok', 'path': str(target), 'bytes': len(data), 'sha256': co.sha256(data), 'source': source,
            'head': message[:20], 'tail': message[-20:],
            'next': f'python3 {co.HERE / "co_design.py"} proposal prepare --session {sid} --file {target} '
                    '--out <new separate proposal draft path>'}


def exact(co, value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise co.SchemaError('handoff object has missing or unknown fields')


def parse_message(co, message):
    """明示 v2 は proposal にだけ観察文を持つ。既存形式の推測変換はしない。"""
    if not isinstance(message, dict):
        raise co.SchemaError('handoff must be an object')
    version = 1
    if 'handoff_version' in message:
        exact(co, message, {'handoff_version', 'proposal', 'evidence_manifest'})
        if type(message['handoff_version']) is not int or message['handoff_version'] != 2:
            raise co.SchemaError('unsupported handoff_version; explicit integer 2 is required')
        version = 2
    else:
        exact(co, message, {'proposal', 'evidence_manifest'})
    co.check_lengths(message)
    proposal = copy.deepcopy(message['proposal'])
    co.check_schema(proposal, co.load_schema('proposal'), '$.proposal')
    if not isinstance(message['evidence_manifest'], list):
        raise co.SchemaError('evidence_manifest must be an array')
    return proposal, message['evidence_manifest'], version


def source_state(co, value, source):
    exact(co, value, {'worktree', 'checked_at', 'status'})
    if not all(isinstance(v, str) and v.strip() for v in value.values()):
        raise co.SchemaError('source_state needs worktree, checked_at and reported Git/local status')
    worktree = Path(value['worktree'])
    resolved = worktree.resolve(strict=True)
    if not worktree.is_absolute() or not co._within(resolved, co.PROJECT):
        raise co.Refused('reported worktree must be inside the current project')
    protected = [Path.home() / '.aws', Path.home() / '.codex', Path.home() / '.claude', *co.HARNESS_PROTECTED]
    if (any(co._within(resolved, root) for root in protected)
            or any(p.name in {'.aws', '.codex', 'hooks'} for p in [resolved, *resolved.parents])
            or any(p.is_symlink() for p in [worktree, *worktree.parents] if str(p) not in {'/tmp', '/var'})):
        raise co.Refused('protected or symlinked reported worktree refused')
    if not resolved.is_dir() or not co._within(source.resolve(strict=True), resolved):
        raise co.SchemaError('reported worktree must contain this evidence source')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T\S+)?', value['checked_at']):
        raise co.SchemaError('source_state checked_at must be a date or ISO timestamp')


def no_local_prose(co, value):
    if isinstance(value, str) and LOCAL.search(value):
        raise co.SchemaError('local evidence key remains in prose/ref; use an explicit file/input reference, not guessed replacement')
    if isinstance(value, dict):
        for item in value.values():
            no_local_prose(co, item)
    if isinstance(value, list):
        for item in value:
            no_local_prose(co, item)


def capture_preflight(co, target):
    if any(p.is_symlink() for p in [target, *target.parents] if str(p) not in {'/tmp', '/var'}):
        raise co.Refused('evidence capture target must not be reached through a symlink')
    if target.exists():
        raise co.Refused('evidence capture target already exists')
    parent = target.parent
    while not parent.exists():
        parent = parent.parent
    if not parent.is_dir() or not co.os.access(parent, co.os.W_OK | co.os.X_OK):
        raise co.Refused('evidence capture parent must be a writable directory')


def verify_prepared(co, session, document):
    """prepare 後の手作業による意味変更・根拠削除を freeze の保存前に拒否する。"""
    receipt = session.get('proposal_handoff')
    if receipt is None:  # prepare を使わない既存セッションは従来どおり。
        return
    archived, _, _ = co.read_source(co.store(session['sid']) / 'architect-handoff.json',
                                    co.MAX_DOC * 4, allow_session_files=True)
    original, _, _ = co.read_source(receipt['source_path'], co.MAX_DOC * 4)
    if co.sha256(archived) != receipt['source_sha256'] or co.sha256(original) != receipt['source_sha256']:
        raise co.Refused('architect handoff integrity changed; preserve the original and report, do not rewrite or retry')
    expected, _, _ = parse_message(co, co.decode_json(archived))
    for entry in expected['evidence']:
        entry['id'] = receipt['evidence_mapping'].get(entry['id'], entry['id'])
    if co.sha256(co.dump(expected)) != receipt['prepared_sha256'] or document != expected:
        raise co.Refused('prepared proposal differs from architect handoff; evidence deletion or semantic rewriting is not allowed')


def prepare(co, session, source, output):
    sid = session['sid']
    co.require_state(session, 'CODEX_PROPOSED')
    if session.get('proposal_handoff') is not None:
        raise co.Refused('architect handoff already prepared; freeze the unchanged draft, do not replace the original')
    call = co.phase_call(session, 'proposal')
    if not call or not call.get('ended_at') or call.get('execution') != 'ok':
        raise co.Refused('proposal handoff requires a successfully completed Codex proposal')
    # manifest の封筒で、既存の full proposal 本文上限を実質的に減らさない。
    raw = co.validation_candidate(sid, '', source, limit=co.MAX_DOC * 4)
    _, info, _ = co.read_source(source, co.MAX_DOC * 4)
    birth = getattr(info, 'st_birthtime', None)
    if birth is None or birth <= call['ended_at']:
        raise co.Refused('handoff message must be a new file created after the Codex proposal ended')
    target = co.submission_draft_target(output)
    if co.now() <= call['ended_at']:
        raise co.Refused('proposal draft cannot be created before the Codex proposal ended')
    try:
        message = co.decode_json(raw)
    except co.SchemaError as error:
        raise co.SchemaError(str(error) + '; preserve the complete architect response; do not extract JSON, strip prose/fences, rewrite or retry') from None
    proposal, manifests, version = parse_message(co, message)
    evidence = proposal['evidence']
    ids = [item['id'] for item in evidence]
    if len(ids) != len(set(ids)):
        raise co.SchemaError('handoff proposal evidence ids must be unique')
    for index, item in enumerate(evidence):
        pattern = LOCAL if item['kind'] == 'file' else re.compile(r'E\d{1,4}')
        if not pattern.fullmatch(item['id']):
            required = 'local:<key> with one manifest entry' if item['kind'] == 'file' else 'E<n>, not local:<key>'
            raise co.SchemaError(f'$.proposal.evidence[{index}].id: {item["kind"]} requires {required}; preserve the evidence and report, do not delete or rewrite it')
    file_items = {item['id']: item for item in evidence if item['kind'] == 'file'}
    pending, used_ranges = {}, set()
    for manifest in manifests:
        manifest_fields = {'key', 'path', 'lines', 'file_sha256', 'source_state'}
        if version == 1:
            manifest_fields.add('observation')
        exact(co, manifest, manifest_fields)
        key = manifest['key']
        if not isinstance(key, str) or not LOCAL.fullmatch(key) or key in pending or key not in file_items:
            raise co.SchemaError('manifest needs a unique declared local file evidence key')
        path = Path(manifest['path'])
        if not path.is_absolute() or not co._within(path.resolve(strict=True), co.PROJECT):
            raise co.Refused('handoff evidence must be inside the current project')
        bounds = manifest['lines']
        if not isinstance(bounds, list) or len(bounds) != 2 or any(type(v) is not int for v in bounds):
            raise co.SchemaError('manifest lines must be [first, last] integers')
        source_state(co, manifest['source_state'], path)
        captured, entry = co.capture_evidence(path, *bounds)
        if entry['file_sha256'] != manifest['file_sha256']:
            raise co.SchemaError('handoff source hash changed; inspect the source difference before resubmission')
        position = (entry['path'], *bounds)
        if position in used_ranges:
            raise co.SchemaError('duplicate manifest file range')
        used_ranges.add(position)
        item = file_items[key]
        observation = item['observation'] if version == 2 else manifest['observation']
        relative = str(Path(entry['path']).relative_to(co.PROJECT.resolve()))
        suffix = ':L' + str(bounds[0]) + ('-L' + str(bounds[1]) if bounds[1] != bounds[0] else '')
        if (item['ref'] not in {relative + suffix, entry['path'] + suffix}
                or not isinstance(observation, str) or item['observation'] != observation):
            raise co.SchemaError('file evidence ref/observation must match its declared manifest exactly')
        entry.update(source_state=copy.deepcopy(manifest['source_state']), observation=observation)
        pending[key] = (captured, entry)
    if set(file_items) != set(pending):
        raise co.SchemaError('every file evidence needs exactly one local manifest entry')
    # 非登録 brief/inference や既存の別 kind の ID を、新規 file 登録で奪わない。
    reserved = co.evidence_ids_reserved(session, [ident for ident in ids if ident not in pending])
    mapping, entries = {}, []
    for key, (captured, entry) in pending.items():
        ident = co.next_evidence_id(reserved)
        reserved.add(ident)
        mapping[key] = ident
        entry.update(id=ident, added_at=co.now())
        capture_preflight(co, co.store(sid) / 'evidence/claude' / (ident + '.txt'))
        entries.append((captured, entry))
    for item in evidence:
        if item['id'] in mapping:
            item['id'] = mapping[item['id']]
    no_local_prose(co, proposal)
    hypothetical = copy.deepcopy(session)
    hypothetical['evidence'].extend(entry for _, entry in entries)
    warnings = co.proposal_semantics(proposal, 'claude', hypothetical)
    co.validate_doc(proposal, 'proposal')
    if len(co.dump(proposal)) > co.MAX_DOC:
        raise co.SchemaError('prepared proposal exceeds the full proposal size limit')
    if co.verify_frozen(session):
        raise co.Refused('frozen input integrity check failed')
    archive = co.store(sid) / 'architect-handoff.json'
    capture_preflight(co, archive)
    # 全件の検査後、保存の直前にも現在の本文ハッシュを照合する。
    for captured, entry in entries:
        latest, current = co.capture_evidence(entry['path'], *entry['lines'])
        if latest != captured or current['file_sha256'] != entry['file_sha256']:
            raise co.SchemaError('handoff source changed during preparation; inspect the difference')
    latest_message, _, _ = co.read_source(source, co.MAX_DOC * 4)
    if latest_message != raw:
        raise co.Refused('architect message changed during preparation; preserve both versions and report')
    co.write_frozen(archive, raw)
    for captured, entry in entries:
        co.write_frozen(co.store(sid) / 'evidence/claude' / (entry['id'] + '.txt'), captured)
    co.write_draft(target, proposal)
    session['evidence'].extend(entry for _, entry in entries)
    session['reserved_evidence_ids'] = sorted(reserved)
    session['proposal_handoff'] = {'source_path': str(Path(source).resolve()), 'source_sha256': co.sha256(raw),
                                   'prepared_sha256': co.sha256(co.dump(proposal)), 'path': str(target),
                                   'evidence_mapping': mapping, 'handoff_version': version}
    co.save_session(session)
    return {'status': 'ok', 'path': str(target), 'sha256': co.sha256(co.dump(proposal)), 'evidence_mapping': mapping,
            'registered': len(entries), 'warnings': warnings, 'source_sha256': co.sha256(raw), 'handoff_version': version,
            'next': 'Main freezes this separate draft (architect never writes/registers/freezes): ' + shlex.join(
                ['python3', str(co.HERE / 'co_design.py'), 'freeze', '--session', sid, '--side', 'claude', '--file', str(target)])}
