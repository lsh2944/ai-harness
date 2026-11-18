"""ID と Claude 用 slot に限定した部分提出。汎用 JSON patch は扱わない。"""
from collections import Counter
import copy
import re
import shlex


ENVELOPE = {'session', 'kind', 'stage', 'base_id', 'base_sha256'}
LEDGER_PAYLOAD = ENVELOPE | {'issues', 'add_issues', 'states'}
SYNTHESIS_PAYLOAD = ENVELOPE | {'decisions', 'remove_decisions', 'set'}
ISSUE_OPERATION = {'issue_id', 'set', 'turns', 'add_turns'}
ISSUE_SET = {'type', 'importance', 'title', 'requirement_ref', 'claude', 'decision_method', 'verification', 'status'}
TURN_OPERATION = {'slot', 'set'}
TURN_SET = {'text', 'evidence_ids', 'would_change_if'}
DECISION_OPERATION = {'issue_id', 'set'}


def fields(co, value, allowed, required=()):
    if not isinstance(value, dict):
        raise co.SchemaError('partial object: expected object')
    unknown, missing = set(value) - set(allowed), set(required) - set(value)
    if unknown or missing:
        detail = []
        if unknown:
            detail.append('unknown: ' + ', '.join(sorted(unknown)))
        if missing:
            detail.append('missing: ' + ', '.join(sorted(missing)))
        raise co.SchemaError('partial object has missing or unknown fields (' + '; '.join(detail) + ')')


def format_hints(co, kind):
    """パーサと同じ許可集合を公開する。意味・stance・根拠は生成しない。"""
    hints = {'payload_fields': sorted(LEDGER_PAYLOAD if kind == 'ledger' else SYNTHESIS_PAYLOAD),
             'fixed_envelope_fields': sorted(ENVELOPE),
             'edit_target': 'Read the returned draft before editing. Edit only the draft; never paste the scaffold response, full_base, hints or slots into it.'}
    if kind == 'ledger':
        hints.update(issue_operation_fields=sorted(ISSUE_OPERATION), issue_set_fields=sorted(ISSUE_SET),
                     turn_operation_fields=sorted(TURN_OPERATION), turn_set_fields=sorted(TURN_SET),
                     add_issue_fields=sorted(ISSUE_SET | {'issue_id', 'turns'}),
                     issue_routing='Use issues with issue_id for an existing full_base issue, including its blank placeholder. Use add_issues only for a new, noncolliding issue_id; never invent Codex fields or turns.')
    else:
        props = co.load_schema('synthesis')['properties']
        hints.update(decision_operation_fields=sorted(DECISION_OPERATION),
                     decision_set_fields=sorted(set(props['decisions']['items']['properties']) - {'issue_id', 'codex_checked'}),
                     set_fields=sorted(set(props) - {'decisions'}))
    return hints


def checked_subset(co, value, schema):
    fields(co, value, schema['properties'])
    co.check_schema(value, dict(schema, required=[]))


def fingerprint(co, session, kind):
    sid = session['sid']
    changed = co.verify_frozen(session)
    if changed:
        raise co.Refused('partial base dependency integrity check failed')
    paths = {'ledger.json', kind + '.json', 'ledger.pre-debate.json'}
    paths.update(item['path'] for item in session['frozen'].values())
    snapshots = {}
    for relative in sorted(paths):
        path = co.art(sid) / relative
        snapshots[relative] = co.sha256(co.read_artifact(sid, relative)) if path.exists() or path.is_symlink() else None
    calls = []
    for call in session['calls']:
        observed = {}
        if call.get('execution') == 'ok':
            for name in ('result', 'links'):
                path = co.call_file(sid, call, name + '.json')
                if path is None:
                    raise co.Refused('partial base Codex dependency is missing')
                raw, _, _ = co.read_source(path, co.MAX_DOC * 4, allow_session_files=True)
                observed[name] = co.sha256(raw)
                if observed[name] != call.get(name + '_sha256'):
                    raise co.Refused('partial base Codex dependency integrity check failed')
        calls.append({'record': call, 'observed': observed})
    return co.canonical_digest({'state': session['state'], 'files': snapshots, 'calls': calls,
                                'inputs': session['inputs'], 'data': session.get('data', {}),
                                'evidence': session['evidence'], 'conditional': session.get('conditional'),
                                'conditional_used': session.get('conditional_used')})


def final_base(co, session):
    """既存の Claude 回答・判定を残し、新しい Codex 原文だけを足す。"""
    current = co._artifact_or_none(session['sid'], 'ledger.json')
    fresh, _ = co.scaffold_final_ledger(session)
    if current is None:
        return fresh
    co.validate_doc(current, 'ledger')
    current = copy.deepcopy(current)
    by_id = {issue['id']: issue for issue in current['issues']}
    for item in fresh['issues']:
        issue = by_id.get(item['id'])
        if issue is None:
            current['issues'].append(item)
            by_id[item['id']] = item
            continue
        codex_turns = [turn for turn in item['turns'] if turn['speaker'] == 'codex']
        if codex_turns:
            issue['codex'] = copy.deepcopy(item['codex'])
        existing = Counter(co.canonical_digest(turn) for turn in issue['turns'])
        seen = Counter()
        for turn in codex_turns:
            key = co.canonical_digest(turn)
            seen[key] += 1
            if seen[key] > existing[key]:
                issue['turns'].append(copy.deepcopy(turn))
                if turn['kind'] == 'critique':
                    issue['turns'].append(co.blank_turn(turn['phase'], 'claude', 'answer'))
    return current


def turn_slots(co, document):
    return {issue['id']: {'claude-' + str(n) + '-' + co.canonical_digest(turn)[:16]: n
                          for n, turn in enumerate(issue['turns']) if turn['speaker'] == 'claude'}
            for issue in document['issues']}


def create(co, session, kind, stage, document, output):
    sid = session['sid']
    if kind == 'ledger' and stage == 'pre-debate' and session['plan'] != 'A':
        raise co.Refused('plan B independent critique uses the existing full freeze path')
    if kind == 'ledger' and stage == 'final':
        document = final_base(co, session)
    if kind == 'synthesis':
        current = co._artifact_or_none(sid, 'synthesis.json')
        if current is not None:
            co.validate_doc(current, 'synthesis')
            document = current
    ident = kind + '-' + stage + '-' + co.secrets.token_hex(8)
    slots = turn_slots(co, document) if kind == 'ledger' else {}
    base = {'session': sid, 'kind': kind, 'stage': stage, 'base_id': ident, 'document': document,
            'dependencies': fingerprint(co, session, kind), 'slots': slots}
    raw = co.dump(base)
    digest = co.sha256(raw)
    payload = {k: base[k] for k in ('session', 'kind', 'stage', 'base_id')}
    payload['base_sha256'] = digest
    payload.update({'issues': [], 'add_issues': [], 'states': {}} if kind == 'ledger' else
                   {'decisions': [], 'remove_decisions': [], 'set': {}})
    hints = dict(co.scaffold_hints(session, kind, document), partial=format_hints(co, kind))
    # 出力先の拒否を、base・session の保存より先に確定する。
    co.submission_draft_target(output)
    target = co.write_draft(output, payload)
    co.write_frozen(co.store(sid) / 'partial-bases' / (ident + '.json'), raw)
    session.setdefault('partial_bases', {})[ident] = digest
    co.save_session(session)
    public_slots = {issue['id']: [{'slot': slot, 'turn': issue['turns'][n]} for slot, n in slots[issue['id']].items()]
                    for issue in document['issues']} if kind == 'ledger' else {}
    return {'draft': str(target), 'base_id': ident, 'base_sha256': digest, 'full_base': document,
            'claude_turn_slots': public_slots,
            'hints': hints,
            'next': next_command(co, session, kind, stage, target, submit=True)}


def next_command(co, session, kind, stage, source=None, submit=False):
    prefix = ['python3', str(co.HERE / 'co_design.py'), kind]
    stage_args = ['--stage', stage] if kind == 'ledger' else []
    if submit:
        return 'Read the returned draft before editing; preserve its fixed envelope, fill only typed changes, then submit: ' + shlex.join(prefix + ['validate', '--session', session['sid'],
                                                                          *stage_args, '--partial-file', str(source)])
    example = co.PROJECT / '.local/cross-check-drafts/<unique>' / (kind + '.partial.json')
    return ('Before further changes, get a fresh base and inspect changed evidence; never auto-rebase or replay an old patch: '
            + shlex.join(prefix + ['scaffold', '--session', session['sid'], *stage_args, '--partial', '--out', str(example)])
            + '. Create the unique directory first. A stale conflict is not permission/hook authorization to retry.')


def merge_ledger(co, base, payload):
    document = copy.deepcopy(base['document'])
    schema = co.load_schema('ledger')
    issue_schema = schema['properties']['issues']['items']
    issue_props = issue_schema['properties']
    allowed = ISSUE_SET
    fields(co, payload, LEDGER_PAYLOAD)
    by_id = {issue['id']: issue for issue in document['issues']}
    touched = set()
    for operation in payload.get('issues', []):
        fields(co, operation, ISSUE_OPERATION, {'issue_id'})
        ident = operation['issue_id']
        if ident not in by_id or ident in touched:
            raise co.SchemaError('unknown or duplicate partial issue id')
        touched.add(ident)
        issue = by_id[ident]
        updates = operation.get('set', {})
        fields(co, updates, allowed)
        for key, value in updates.items():
            if key in ('claude', 'verification'):
                checked_subset(co, value, issue_props[key])
                issue[key].update(value)
            else:
                co.check_schema(value, issue_props[key])
                issue[key] = value
        used_slots = set()
        for change in operation.get('turns', []):
            fields(co, change, TURN_OPERATION, {'slot', 'set'})
            slot = change['slot']
            if slot not in base['slots'].get(ident, {}) or slot in used_slots:
                raise co.SchemaError('unknown or duplicate Claude turn slot')
            used_slots.add(slot)
            updates = change['set']
            fields(co, updates, TURN_SET)
            turn_schema = issue_props['turns']['items']
            checked_subset(co, updates, turn_schema)
            issue['turns'][base['slots'][ident][slot]].update(updates)
        for turn in operation.get('add_turns', []):
            co.check_schema(turn, issue_props['turns']['items'])
            if turn['speaker'] != 'claude':
                raise co.SchemaError('only new Claude turns may be submitted')
            issue['turns'].append(copy.deepcopy(turn))
    for addition in payload.get('add_issues', []):
        fields(co, addition, allowed | {'issue_id', 'turns'}, {'issue_id'})
        ident = addition['issue_id']
        if not isinstance(ident, str) or not co.ISSUE_ID.fullmatch(ident) or ident in by_id:
            raise co.SchemaError('unknown or duplicate added issue id')
        issue = co.blank_issue(ident, 'claude')
        for key, value in addition.items():
            if key != 'issue_id':
                issue[key] = copy.deepcopy(value)
        if any(turn.get('speaker') != 'claude' for turn in issue['turns']):
            raise co.SchemaError('new issues cannot invent Codex turns')
        co.check_schema(issue, issue_schema)
        by_id[ident] = issue
        document['issues'].append(issue)
    if 'states' in payload:
        checked_subset(co, payload['states'], schema['properties']['states'])
        document['states'].update(payload['states'])
    return document


def merge_synthesis(co, base, payload):
    fields(co, payload, SYNTHESIS_PAYLOAD)
    document = copy.deepcopy(base['document'])
    props = co.load_schema('synthesis')['properties']
    decision_schema = props['decisions']['items']
    existing = {decision['issue_id']: decision for decision in document['decisions']}
    known = {issue['id'] for issue in co.read_json_artifact(base['session'], 'ledger.json')['issues']}
    touched = set()
    for operation in payload.get('decisions', []):
        fields(co, operation, DECISION_OPERATION, {'issue_id', 'set'})
        ident = operation['issue_id']
        if ident not in known or ident in touched:
            raise co.SchemaError('unknown or duplicate partial decision id')
        touched.add(ident)
        updates = operation['set']
        fields(co, updates, set(decision_schema['properties']) - {'issue_id', 'codex_checked'})
        checked_subset(co, updates, decision_schema)
        if ident not in existing:
            decision = {'issue_id': ident, 'codex_checked': False}
            document['decisions'].append(decision)
            existing[ident] = decision
        existing[ident].update(copy.deepcopy(updates))
    removed = payload.get('remove_decisions', [])
    if not isinstance(removed, list) or any(not isinstance(i, str) for i in removed) or len(removed) != len(set(removed)):
        raise co.SchemaError('remove_decisions must contain unique issue ids')
    if any(i not in existing or i in touched for i in removed):
        raise co.SchemaError('unknown or conflicting removed decision id')
    document['decisions'] = [decision for decision in document['decisions'] if decision['issue_id'] not in removed]
    updates = payload.get('set', {})
    fields(co, updates, set(props) - {'decisions'})
    for key, value in updates.items():
        co.check_schema(value, props[key])
        document[key] = copy.deepcopy(value)
    return document


def candidate(co, session, kind, stage, source):
    payload = co.decode_json(co.validation_candidate(session['sid'], '', source))
    fields(co, payload, set(payload) if isinstance(payload, dict) else (),
           ENVELOPE)
    ident = payload['base_id']
    if not isinstance(ident, str) or not re.fullmatch(r'(ledger|synthesis)-[a-z-]+-[0-9a-f]{16}', ident):
        raise co.SchemaError('invalid partial base id')
    expected = session.get('partial_bases', {}).get(ident)
    raw, _, _ = co.read_source(co.store(session['sid']) / 'partial-bases' / (ident + '.json'),
                               co.MAX_DOC * 4, allow_session_files=True)
    if expected is None or expected != payload['base_sha256'] or co.sha256(raw) != expected:
        raise co.Refused('partial base integrity check failed')
    base = co.decode_json(raw)
    if any(payload.get(key) != value or base.get(key) != value for key, value in
           (('session', session['sid']), ('kind', kind), ('stage', stage), ('base_id', ident))):
        raise co.SchemaError('partial base session/kind/stage mismatch')
    if base['dependencies'] != fingerprint(co, session, kind):
        raise co.SchemaError('stale partial base: obtain a fresh scaffold, inspect changes, and resubmit; do not auto-rebase')
    co.check_lengths(payload)
    for key in ('issues', 'add_issues', 'decisions'):
        if key in payload and not isinstance(payload[key], list):
            raise co.SchemaError('partial operations must be arrays')
    merged = merge_ledger(co, base, payload) if kind == 'ledger' else merge_synthesis(co, base, payload)
    result = co.dump(merged)
    if len(result) > co.MAX_DOC:
        raise co.SchemaError('merged partial candidate exceeds the full document size limit')
    return result
