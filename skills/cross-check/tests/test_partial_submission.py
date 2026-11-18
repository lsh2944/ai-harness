"""型付き部分提出を実 runner と偽 Codex だけで検査する。"""
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import co_design_fixture as fx

co = fx.co


class SubmissionBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='submission-test-')
        self.env = fx.Env(self.temp.name)
        self.counter = 0

    def tearDown(self):
        self.env.close()
        self.temp.cleanup()

    def snapshot(self):
        result = {}
        for path in self.env.root.rglob('*'):
            info = path.lstat()
            content = os.readlink(path) if stat.S_ISLNK(info.st_mode) else path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            result[str(path.relative_to(self.env.root))] = (info.st_mode, info.st_mtime_ns, content)
        return result

    def location(self, name='draft.json'):
        self.counter += 1
        target = self.env.project / '.local/cross-check-drafts' / str(self.counter) / name
        target.parent.mkdir(parents=True)
        return target

    def draft(self, document):
        path = self.location()
        path.write_text(document if isinstance(document, str) else json.dumps(document, ensure_ascii=False))
        return path

    def full(self, sid, kind, document, stage=None, *extra):
        args = [kind, 'validate', '--session', sid, '--file', self.draft(document)]
        if stage:
            args += ['--stage', stage]
        return self.env.ok(*args, *extra)

    def debated(self, plan_b=False):
        sid = self.env.to_revealed(plan_b=plan_b)
        if plan_b:
            self.env.ok('call', '--session', sid, '--phase', 'critique')
            source = self.draft(fx.ledger([fx.issue('I1')]))
            self.env.ok('freeze', '--session', sid, '--side', 'claude-critique', '--file', source)
            self.env.ok('call', '--session', sid, '--phase', 'rebuttal')
        else:
            self.full(sid, 'ledger', fx.ledger([fx.issue('I1')]), 'pre-debate')
            self.env.ok('call', '--session', sid, '--phase', 'debate')
        return sid

    def base(self, sid, kind='ledger', stage='final'):
        path = self.location('partial.json')
        args = [kind, 'scaffold', '--session', sid, '--partial', '--out', path]
        if kind == 'ledger':
            args += ['--stage', stage]
        output = self.env.ok(*args)
        return output, json.loads(path.read_bytes())

    def partial(self, sid, payload, expected=True, *extra):
        source = self.draft(payload)
        before, calls = self.snapshot(), self.env.calls()
        args = [payload['kind'], 'validate', '--session', sid, '--partial-file', source]
        if payload['kind'] == 'ledger':
            args += ['--stage', payload['stage']]
        output, code = self.env.run(*args, *extra)
        self.assertEqual(code, 0 if expected else 2, output)
        self.assertEqual(source.read_text(), json.dumps(payload, ensure_ascii=False))
        self.assertEqual(self.env.calls(), calls)
        if not expected:
            self.assertEqual(self.snapshot(), before)
        return output

    def final_changes(self, base, payload):
        wanted = {item['id']: item for item in fx.final_ledger()['issues']}
        for item in base['full_base']['issues']:
            target = wanted.get(item['id'])
            if not target:
                continue
            payload['issues'].append({'issue_id': item['id'], 'set': {key: copy.deepcopy(target[key]) for key in
                ('claude', 'decision_method', 'verification', 'status')},
                'turns': [{'slot': entry['slot'], 'set': {'text': 'answer with conditions retained',
                          'would_change_if': 'only when source changes'}} for entry in base['claude_turn_slots'][item['id']]
                          if not entry['turn']['text'].strip()]})
        return payload

    def synthesis_changes(self, payload, document=None):
        document = document or fx.synthesis()
        payload['decisions'] = [{'issue_id': d['issue_id'], 'set': {k: v for k, v in d.items()
                                  if k not in ('issue_id', 'codex_checked')}} for d in document['decisions']]
        payload['set'] = {key: value for key, value in document.items() if key != 'decisions'}
        return payload


class PartialSubmissionTests(SubmissionBase):
    def test_partial_ledger_hints_match_payload_and_keep_semantic_fields_blank(self):
        sid = self.env.to_revealed()
        result, payload = self.base(sid, stage='pre-debate')
        hints = result['hints']
        self.assertEqual(set(hints['partial']['payload_fields']), set(payload))
        self.assertEqual(set(hints['partial']['fixed_envelope_fields']),
                         {'session', 'kind', 'stage', 'base_id', 'base_sha256'})
        self.assertEqual(hints['known_issue_ids'], ['I1'])
        self.assertNotIn(hints['new_issue_id_example'], hints['known_issue_ids'])
        self.assertIn('issue_id', hints['partial']['issue_operation_fields'])
        self.assertNotIn('codex', hints['partial']['issue_set_fields'])
        self.assertNotIn('origin', hints['partial']['add_issue_fields'])
        self.assertEqual(result['full_base']['issues'][0]['claude']['stance'], 'pending')
        self.assertEqual(result['full_base']['issues'][0]['claude']['position'], '')
        self.assertEqual(payload['issues'], [])
        self.assertTrue(Path(result['draft']).is_absolute())

    def test_hint_guided_existing_and_new_issue_operations_pass_without_id_collision(self):
        sid = self.env.to_revealed()
        result, payload = self.base(sid, stage='pre-debate')
        hint = result['hints']
        existing_id = hint['known_issue_ids'][0]
        wanted = fx.issue(existing_id)
        payload['issues'] = [{'issue_id': existing_id, 'set': {k: wanted[k] for k in hint['partial']['issue_set_fields']},
            'turns': [{'slot': result['claude_turn_slots'][existing_id][0]['slot'],
                       'set': {'text': 'Critique with original conditions retained'}}]}]
        fresh_id = hint['new_issue_id_example']
        addition = fx.issue(fresh_id, importance='low')
        payload['add_issues'] = [{'issue_id': fresh_id, **{k: addition[k] for k in hint['partial']['add_issue_fields'] if k != 'issue_id'}}]
        self.partial(sid, payload)
        ledger = co.read_json_artifact(sid, 'ledger.json')
        self.assertEqual([i['id'] for i in ledger['issues']], [existing_id, fresh_id])
        self.assertEqual(ledger['issues'][0]['turns'][0]['text'], 'Critique with original conditions retained')

    def test_partial_synthesis_hints_match_schema_and_do_not_fill_judgments(self):
        sid = self.debated()
        self.full(sid, 'ledger', fx.final_ledger(), 'final')
        result, payload = self.base(sid, 'synthesis')
        hints = result['hints']
        self.assertEqual(set(hints['partial']['payload_fields']), set(payload))
        self.assertEqual(hints['known_issue_ids'], ['I1', 'C1'])
        self.assertNotIn('codex_checked', hints['partial']['decision_set_fields'])
        self.assertIn('adopted_from', hints['partial']['decision_set_fields'])
        self.assertIn('metrics', hints['partial']['set_fields'])
        self.assertTrue(hints['enum_choices'])
        self.assertEqual(result['full_base']['decisions'][0]['decision'], '')
        self.assertEqual(result['full_base']['metrics']['missing_required'], -1)
        self.partial(sid, self.synthesis_changes(payload))
        self.assertEqual(co.read_json_artifact(sid, 'synthesis.json'), fx.synthesis())

    def test_proposal_inference_hint_does_not_authorize_ledger_evidence_or_rewrite_source(self):
        sid = self.env.to_revealed()
        frozen = co.read_artifact(sid, 'claude/proposal.json')
        result, payload = self.base(sid, stage='pre-debate')
        self.assertIn({'id': 'E9', 'kind': 'inference'}, result['hints']['proposal_ids_not_ledger_refs'])
        self.assertNotIn('E9', result['hints']['known_evidence_ids'])
        wanted = fx.issue('I1', evidence=['E9'])
        payload['issues'] = [{'issue_id': 'I1', 'set': {k: wanted[k] for k in result['hints']['partial']['issue_set_fields']},
                             'turns': [{'slot': result['claude_turn_slots']['I1'][0]['slot'], 'set': {'text': 'Unverified inference'}}]}]
        rejected = self.partial(sid, payload, False)
        self.assertIn('unknown evidence id E9', rejected['error'])
        self.assertEqual(co.read_artifact(sid, 'claude/proposal.json'), frozen)

    def test_partial_metadata_and_missing_selectors_report_specific_fields_without_writes(self):
        sid = self.env.to_revealed()
        result, payload = self.base(sid, stage='pre-debate')
        variants = []
        wrong = copy.deepcopy(payload)
        wrong['hints'] = result['hints']
        variants.append((wrong, 'unknown: hints'))
        wrong = copy.deepcopy(payload)
        wrong['issues'] = [{'set': {'title': 'missing selector'}}]
        variants.append((wrong, 'missing: issue_id'))
        wrong = copy.deepcopy(payload)
        wrong['issues'] = [{'issue_id': 'I1', 'set': {'codex': {'position': 'forged'}}}]
        variants.append((wrong, 'unknown: codex'))
        for wrong, error in variants:
            with self.subTest(error=error):
                rejected = self.partial(sid, wrong, False)
                self.assertIn(error, rejected['error'])

    def test_initial_pre_debate_placeholder_is_completed_without_codex_fields(self):
        sid = self.env.to_revealed()
        base, payload = self.base(sid, stage='pre-debate')
        original = copy.deepcopy(base['full_base']['issues'][0]['codex'])
        wanted = fx.issue('I1')
        payload['issues'] = [{'issue_id': 'I1', 'set': {k: wanted[k] for k in
            ('title', 'requirement_ref', 'type', 'importance', 'claude', 'decision_method', 'verification', 'status')},
            'turns': [{'slot': base['claude_turn_slots']['I1'][0]['slot'], 'set': {'text': 'Initial critique\nCondition A remains'}}]}]
        self.partial(sid, payload)
        stored = co.read_json_artifact(sid, 'ledger.json')
        self.assertEqual(stored['issues'][0]['codex'], original)
        self.assertIn('Condition A remains', stored['issues'][0]['turns'][0]['text'])
        self.assertEqual(co.read_artifact(sid, 'ledger.json'), co.read_artifact(sid, 'ledger.pre-debate.json'))

    def test_final_multi_answer_new_issue_and_omitted_codex_text_are_preserved(self):
        sid = self.debated()
        base, payload = self.base(sid)
        self.final_changes(base, payload)
        addition = fx.issue('I2', importance='low', claude='conditional', method='undecidable')
        payload['add_issues'] = [{'issue_id': 'I2', **{k: v for k, v in addition.items() if k not in ('id', 'origin', 'codex')}}]
        self.partial(sid, payload)
        stored = co.read_json_artifact(sid, 'ledger.json')
        for old in base['full_base']['issues']:
            new = next(item for item in stored['issues'] if item['id'] == old['id'])
            self.assertEqual(new['codex'], old['codex'])
            self.assertEqual([t for t in new['turns'] if t['speaker'] == 'codex'],
                             [t for t in old['turns'] if t['speaker'] == 'codex'])
        self.assertEqual(stored['issues'][-1]['codex']['stance'], 'pending')
        fresh, omitted = self.base(sid)
        before = co.read_json_artifact(sid, 'ledger.json')
        self.partial(sid, omitted)
        self.assertEqual(co.read_json_artifact(sid, 'ledger.json'), before)
        self.assertEqual(fresh['full_base'], before)

    def test_synthesis_initial_blank_decisions_and_metrics_are_completed(self):
        sid = self.debated()
        self.full(sid, 'ledger', fx.final_ledger(), 'final')
        base, payload = self.base(sid, 'synthesis')
        self.assertEqual(base['full_base']['metrics']['missing_required'], -1)
        self.synthesis_changes(payload)
        output = self.partial(sid, payload)
        self.assertEqual(co.read_json_artifact(sid, 'synthesis.json'), fx.synthesis())
        self.assertEqual(self.env.session(sid)['conditional']['synthesis_sha256'], output['sha256'])

    def test_synthesis_empty_decision_array_can_receive_explicit_new_decisions(self):
        sid = self.debated()
        self.full(sid, 'ledger', fx.final_ledger(), 'final')
        empty = fx.synthesis()
        empty['decisions'] = []
        self.full(sid, 'synthesis', empty)
        base, payload = self.base(sid, 'synthesis')
        self.assertEqual(base['full_base']['decisions'], [])
        self.partial(sid, self.synthesis_changes(payload))
        self.assertEqual(co.read_json_artifact(sid, 'synthesis.json'), fx.synthesis())

    def test_merged_candidate_keeps_existing_full_document_size_limit(self):
        sid = self.debated()
        base, payload = self.base(sid)
        self.partial(sid, self.final_changes(base, payload))
        base, payload = self.base(sid)
        with patch.object(co, 'MAX_DOC', len(co.dump(base['full_base'])) - 1):
            output = self.partial(sid, payload, False)
        self.assertIn('full document size limit', output['error'])

    def test_conditions_can_be_replaced_and_decision_can_become_unresolved(self):
        sid = self.debated()
        self.full(sid, 'ledger', fx.final_ledger(), 'final')
        self.full(sid, 'synthesis', fx.synthesis(conditions=['old condition']))
        _, payload = self.base(sid, 'synthesis')
        payload['decisions'] = [{'issue_id': 'I1', 'set': {'conditions': ['replacement condition']}}]
        payload['remove_decisions'] = ['C1']
        payload['set'] = {'unresolved': [{'issue_id': 'C1', 'impact': 'unknown', 'needed_evidence': 'source', 'blocking': False}]}
        self.partial(sid, payload)
        saved = co.read_json_artifact(sid, 'synthesis.json')
        self.assertEqual(saved['decisions'][0]['conditions'], ['replacement condition'])
        self.assertEqual(len(saved['decisions']), 1)
        self.assertEqual(saved['unresolved'][0]['issue_id'], 'C1')
        _, payload = self.base(sid, 'synthesis')
        new = fx.synthesis()['decisions'][1]
        payload['decisions'] = [{'issue_id': 'C1', 'set': {k: v for k, v in new.items() if k not in ('issue_id', 'codex_checked')}}]
        payload['set'] = {'unresolved': []}
        self.partial(sid, payload)
        self.assertEqual(len(co.read_json_artifact(sid, 'synthesis.json')['decisions']), 2)

    def test_stale_canonical_requires_fresh_base_without_rebase_or_mutation(self):
        sid = self.debated()
        initial, initial_payload = self.base(sid)
        self.partial(sid, self.final_changes(initial, initial_payload))
        _, payload = self.base(sid)
        changed = co.read_json_artifact(sid, 'ledger.json')
        changed['issues'][0]['title'] = 'new source decision'
        self.full(sid, 'ledger', changed, 'final')
        output = self.partial(sid, payload, False)
        self.assertEqual(output['status'], 'INVALID')
        self.assertIn('stale', output['error'])
        _, fresh = self.base(sid)
        self.partial(sid, fresh)

    def test_new_codex_result_stales_base_and_fresh_derive_uses_actual_fidelity(self):
        sid = self.debated()
        self.full(sid, 'ledger', fx.final_ledger(), 'final')
        self.full(sid, 'synthesis', fx.synthesis())
        _, old = self.base(sid, 'synthesis')
        self.env.ok('call', '--session', sid, '--phase', 'fidelity')
        self.partial(sid, old, False)
        _, fresh = self.base(sid, 'synthesis')
        output = self.partial(sid, fresh)
        self.assertEqual(output['derived_checked'], ['I1'])
        _, changed = self.base(sid, 'synthesis')
        changed['decisions'] = [{'issue_id': 'I1', 'set': {'conditions': ['explicitly changed']}}]
        output = self.partial(sid, changed)
        self.assertEqual(output['derived_checked'], [])
        self.assertFalse(any(d['codex_checked'] for d in co.read_json_artifact(sid, 'synthesis.json')['decisions']))
        self.env.ok('finalize', '--session', sid)

    def test_invalid_typed_operations_refs_and_semantics_leave_everything_unchanged(self):
        sid = self.debated()
        self.full(sid, 'ledger', fx.final_ledger(), 'final')
        base, original = self.base(sid)
        canonical_before = co.read_json_artifact(sid, 'ledger.json')
        variants = []
        for update in ({'codex': {'position': 'forged'}}, {'title': ''}, {'claude': {'evidence_ids': ['E9999']}},
                       {'importance': 'unknown'}):
            value = copy.deepcopy(original)
            value['issues'] = [{'issue_id': 'I1', 'set': update}]
            variants.append(value)
        for op in ({'issue_id': 'UNKNOWN', 'set': {}}, {'issue_id': 'I1', 'path': '/issues/0'},
                   {'issue_id': 'I1', 'turns': [{'slot': 'codex-forged', 'set': {'text': 'forged'}}]}):
            value = copy.deepcopy(original)
            value['issues'] = [op]
            variants.append(value)
        duplicate = copy.deepcopy(original)
        duplicate['issues'] = [{'issue_id': 'I1', 'set': {}}, {'issue_id': 'I1', 'set': {}}]
        variants.append(duplicate)
        for field in ('base_file', 'path', 'index'):
            value = copy.deepcopy(original)
            value[field] = '/arbitrary/path'
            variants.append(value)
        for value in variants:
            with self.subTest(value=value):
                self.partial(sid, value, False)
        self.assertEqual(canonical_before, co.read_json_artifact(sid, 'ledger.json'))

    def test_cross_session_kind_stage_and_base_hash_cannot_override_binding(self):
        sid = self.debated()
        self.full(sid, 'ledger', fx.final_ledger(), 'final')
        _, payload = self.base(sid)
        for field, value in (('session', '20261117T000000Z-aaaaaa'), ('base_sha256', '0' * 64),
                             ('stage', 'pre-debate')):
            altered = copy.deepcopy(payload)
            altered[field] = value
            self.partial(sid, altered, False)
        altered = copy.deepcopy(payload)
        altered['kind'] = 'synthesis'
        self.partial(sid, altered, False)

    def test_tampered_base_refuses_before_commit(self):
        sid = self.debated()
        self.full(sid, 'ledger', fx.final_ledger(), 'final')
        _, payload = self.base(sid)
        target = co.store(sid) / 'partial-bases' / (payload['base_id'] + '.json')
        target.chmod(0o600)
        target.write_text('{}')
        self.partial(sid, payload, False)

    def test_partial_cannot_submit_fake_checked_unknown_or_duplicate_decisions(self):
        sid = self.debated()
        self.full(sid, 'ledger', fx.final_ledger(), 'final')
        self.full(sid, 'synthesis', fx.synthesis())
        _, payload = self.base(sid, 'synthesis')
        for operations in ([{'issue_id': 'I1', 'set': {'codex_checked': True}}],
                           [{'issue_id': 'I999', 'set': {'decision': 'invented'}}],
                           [{'issue_id': 'I1', 'set': {}}, {'issue_id': 'I1', 'set': {}}]):
            altered = copy.deepcopy(payload)
            altered['decisions'] = operations
            self.partial(sid, altered, False)

    def test_partial_out_omitted_protected_or_existing_is_rejected_before_base_registration(self):
        sid = self.env.to_revealed()
        before = self.snapshot()
        result, code = self.env.run('ledger', 'scaffold', '--session', sid, '--stage', 'pre-debate', '--partial')
        self.assertEqual(code, 2, result)
        self.assertIn('--out', result['error'])
        self.assertEqual(self.snapshot(), before)

    def test_partial_protected_out_has_no_base_draft_or_session_side_effect(self):
        sid = self.env.to_revealed()
        before = self.snapshot()
        result, code = self.env.run('ledger', 'scaffold', '--session', sid, '--stage', 'pre-debate', '--partial',
                                    '--out', co.art(sid) / 'partial.json')
        self.assertEqual(code, 2, result)
        self.assertEqual(self.snapshot(), before)

    def test_partial_existing_out_has_no_base_or_session_side_effect(self):
        sid = self.env.to_revealed()
        target = self.draft('existing sentinel')
        before = self.snapshot()
        result, code = self.env.run('ledger', 'scaffold', '--session', sid, '--stage', 'pre-debate', '--partial', '--out', target)
        self.assertEqual(code, 2, result)
        self.assertEqual(self.snapshot(), before)

    def test_b_independent_critique_stays_full_but_final_and_synthesis_support_partial(self):
        sid = self.debated(plan_b=True)
        base, payload = self.base(sid)
        self.partial(sid, self.final_changes(base, payload))
        _, synthesis = self.base(sid, 'synthesis')
        self.partial(sid, self.synthesis_changes(synthesis, fx.synthesis(adopt_i1='codex')))
        self.env.ok('finalize', '--session', sid)

    def test_b_pre_partial_is_refused_without_revealing_codex_critique(self):
        sid = self.env.to_revealed(plan_b=True)
        self.env.ok('call', '--session', sid, '--phase', 'critique')
        target = self.location()
        before = self.snapshot()
        result, code = self.env.run('ledger', 'scaffold', '--session', sid, '--stage', 'pre-debate', '--partial', '--out', target)
        self.assertEqual(code, 2, result)
        self.assertNotIn('full_base', result)
        self.assertEqual(self.snapshot(), before)

    def test_new_same_issue_critique_gets_new_answer_slot_without_resetting_old_answer(self):
        sid = self.debated()
        initial, initial_payload = self.base(sid)
        self.partial(sid, self.final_changes(initial, initial_payload))
        old_issue = next(i for i in co.read_json_artifact(sid, 'ledger.json')['issues'] if i['id'] == 'C1')
        old_answers = [t['text'] for t in old_issue['turns'] if t['speaker'] == 'claude']
        self.full(sid, 'synthesis', fx.synthesis(adopt_i1='codex'), None, '--user-requested')
        # 同文でも新しい P4 の occurrence なので、既存回答を流用して完了扱いしない。
        self.env.behaviors({'round2': "result['critique_of_claude'] = [{'issue_id':'C1','type':'design-tradeoff','importance':'high','claim':'Claude misses X','evidence_refs':['src/app.py:L1-L3'],'alternative':'Do Y','would_change_if':'Z is shown'}]"})
        self.env.ok('call', '--session', sid, '--phase', 'round2')
        base, payload = self.base(sid)
        issue = next(i for i in base['full_base']['issues'] if i['id'] == 'C1')
        for answer in old_answers:
            self.assertIn(answer, [t['text'] for t in issue['turns'] if t['speaker'] == 'claude'])
        blanks = [s for s in base['claude_turn_slots']['C1'] if not s['turn']['text']]
        self.assertEqual(len(blanks), 1)
        self.partial(sid, payload, False)
        payload['issues'] = [{'issue_id': 'C1', 'turns': [{'slot': blanks[0]['slot'], 'set': {'text': 'new answer'}}]}]
        self.partial(sid, payload)
        again, _ = self.base(sid)
        self.assertFalse(any(not s['turn']['text'] for s in again['claude_turn_slots']['C1']))

    def test_legacy_missing_codex_turns_are_enriched_without_assuming_old_answer_covers_them(self):
        sid = self.debated()
        self.full(sid, 'ledger', fx.final_ledger(), 'final')
        base, payload = self.base(sid)
        issue = next(i for i in base['full_base']['issues'] if i['id'] == 'C1')
        self.assertIn('answered', [t['text'] for t in issue['turns'] if t['speaker'] == 'claude'])
        self.assertTrue(any(t['speaker'] == 'codex' and t['text'] == 'Claude misses X' for t in issue['turns']))
        blanks = [slot for slot in base['claude_turn_slots']['C1'] if not slot['turn']['text']]
        self.assertEqual(len(blanks), 1)
        self.partial(sid, payload, False)
        payload['issues'] = [{'issue_id': 'C1', 'turns': [{'slot': blanks[0]['slot'], 'set': {'text': 'explicit answer to enriched original'}}]}]
        self.partial(sid, payload)
        again, _ = self.base(sid)
        self.assertFalse(any(not slot['turn']['text'] for slot in again['claude_turn_slots']['C1']))


if __name__ == '__main__':
    unittest.main()
