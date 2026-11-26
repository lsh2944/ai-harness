"""構造化メッセージの一括登録・ID 接続を、実 AI なしで検査する。"""
import copy
import json
import os
from pathlib import Path
from unittest.mock import patch

import co_design_fixture as fx
from test_partial_submission import SubmissionBase, co


class ProposalHandoffTests(SubmissionBase):
    def proposed(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        self.assertTrue(self.env.ok('investigation-ready', '--session', sid)['ready'])
        return sid

    def message(self):
        source = self.env.project / 'src/app.py'
        observation = '原文の条件 A\n代案 B と残余リスクを保持'
        proposal = {'phase': 'proposal', 'side': 'claude', 'status': 'COMPLETE', 'evidence_sufficiency': 'partial',
            'summary': '設計全文\n条件 A のときだけ採用',
            'requirement_mapping': [{'requirement': 'R1', 'approach': '代案 B の条件も確認する'}],
            'design': {'components': ['元の境界'], 'flows': ['流れと未確認条件'], 'contracts': ['送受信の両側']},
            'alternatives': [{'option': '代案 B', 'reason_rejected': '条件 C が未確認'}],
            'implementation_order': ['確認してから実装'], 'validation': [{'check': '境界ケース', 'layer': 'mock'}],
            'risks': [{'severity': 'high', 'risk': '契約の差', 'mitigation': '原文を照合'}],
            'assumptions': ['条件 A'], 'open_questions': ['C を誰が確認するか'], 'unverified': ['実環境'],
            'research': fx.no_research(), 'exploration': fx.no_exploration(),
            'evidence': [{'id': 'local:source_a', 'kind': 'file', 'ref': 'src/app.py:L1-L2', 'observation': observation},
                         {'id': 'E1', 'kind': 'brief', 'ref': 'brief:L2', 'observation': 'requirement'},
                         {'id': 'E2', 'kind': 'inference', 'ref': 'inputs/data/raw.csv', 'observation': 'unverified inference'}]}
        return {'proposal': proposal, 'evidence_manifest': [{'key': 'local:source_a', 'path': str(source), 'lines': [1, 2],
            'observation': observation, 'file_sha256': co.sha256(source.read_bytes()),
            'source_state': {'worktree': str(self.env.project), 'checked_at': '2026-11-17', 'status': 'Git unused; local sample'}}]}

    def prepare(self, sid, message, expected=True, output=None, source=None):
        source = source or self.draft(message)
        output = output or self.location('proposal.json')
        before, calls, original = self.snapshot(), self.env.calls(), source.read_bytes()
        result, code = self.env.run('proposal', 'prepare', '--session', sid, '--file', source, '--out', output)
        self.assertEqual(code, 0 if expected else 2, result)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.env.calls(), calls)
        if not expected:
            self.assertEqual(self.snapshot(), before)
        return result, output

    def single_observation_message(self):
        message = self.message()
        message['handoff_version'] = 2
        message['evidence_manifest'] = [{k: v for k, v in item.items() if k != 'observation'}
                                        for item in message['evidence_manifest']]
        return message

    def inspected_message(self, sid):
        message = self.single_observation_message()
        inspected = self.env.ok('evidence', 'inspect', '--session', sid,
                                '--file', self.env.project / 'src/app.py', '--whole-file')
        message['evidence_manifest'][0].update({key: inspected[key] for key in ('path', 'lines', 'file_sha256')})
        message['proposal']['evidence'][0]['ref'] = inspected['ref']
        return message

    def test_inspected_metadata_prepares_and_freezes_without_semantic_rewriting(self):
        sid = self.proposed()
        message = self.inspected_message(sid)
        source = self.draft(' \n' + json.dumps(message, ensure_ascii=False, indent=2) + '\n ')
        original = source.read_bytes()
        result, output = self.prepare(sid, message, source=source)
        self.assertEqual((co.store(sid) / 'architect-handoff.json').read_bytes(), original)
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', output)
        expected = copy.deepcopy(message['proposal'])
        expected['evidence'][0]['id'] = result['evidence_mapping']['local:source_a']
        self.assertEqual(co.read_json_artifact(sid, 'claude/proposal.json'), expected)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.env.calls(), ['proposal'])

    def test_file_change_after_inspect_is_rejected_without_registration(self):
        sid = self.proposed()
        message = self.inspected_message(sid)
        (self.env.project / 'src/app.py').write_text('new1\nnew2\nnew3\nnew4\n')
        result, _ = self.prepare(sid, message, False)
        self.assertIn('source hash changed', result['error'])
        self.assertEqual(self.env.session(sid)['evidence'], [])

    def test_complete_response_with_prefix_suffix_or_fence_is_rejected_not_extracted(self):
        for before, after in (('설명입니다.\n', ''), ('', '\n추가 설명'), ('```json\n', '\n```'), ('', '\n{}')):
            with self.subTest(before=before, after=after):
                sid = self.proposed()
                message = self.single_observation_message()
                source = self.draft(before + json.dumps(message, ensure_ascii=False) + after)
                result, output = self.prepare(sid, message, False, source=source)
                self.assertEqual(result['status'], 'INVALID')
                self.assertIn('preserve the complete architect response', result['error'])
                self.assertEqual(self.env.session(sid)['evidence'], [])
                self.assertNotIn('proposal_handoff', self.env.session(sid))
                self.assertFalse(output.exists())
                self.assertFalse((co.store(sid) / 'architect-handoff.json').exists())

    def test_open_fence_response_and_appended_closing_fence_are_both_rejected_unchanged(self):
        # 実際の失敗形を合成データで再現。入力を補修して成功扱いにしない。
        for prefix, suffix in (('조사 완료 안내\n```json\n', ''), ('조사 완료 안내\n```json\n', '\n```\n'), ('', '\n```\n')):
            with self.subTest(prefix=prefix, suffix=suffix):
                sid = self.proposed()
                message = self.single_observation_message()
                body = prefix + json.dumps(message, ensure_ascii=False)
                source = self.draft(body + suffix)
                original = source.read_bytes()
                result, output = self.prepare(sid, message, False, source=source)
                self.assertEqual(result['status'], 'INVALID')
                self.assertIn('preserve the complete architect response', result['error'])
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(self.env.session(sid)['evidence'], [])
                self.assertNotIn('proposal_handoff', self.env.session(sid))
                self.assertFalse(output.exists())
                self.assertFalse((co.store(sid) / 'architect-handoff.json').exists())

    def test_saved_json_bytes_and_internal_fences_are_preserved_for_unicode_and_crlf(self):
        # 保存済み入力からの保全だけを検査する。モデルの生成や最初のコピーは認証しない。
        for newline in ('none', 'lf', 'crlf'):
            with self.subTest(newline=newline):
                calls_before = self.env.calls()
                sid = self.proposed()
                message = self.single_observation_message()
                message['proposal']['summary'] = '조건 A\n```python\nx = "가나다・日本語・🙂"\n```\r\n미확인 유지'
                message['proposal']['evidence'][0]['observation'] += '\n```text\n원문 내부 펜스\n```'
                text = json.dumps(message, ensure_ascii=False, indent=2)
                raw = (text + ('\n' if newline != 'none' else '')).encode('utf-8')
                if newline == 'crlf':
                    raw = raw.replace(b'\n', b'\r\n')
                source = self.location('original-response.json')
                source.write_bytes(raw)
                result, output = self.prepare(sid, message, source=source)
                self.assertEqual(source.read_bytes(), raw)
                self.assertEqual((co.store(sid) / 'architect-handoff.json').read_bytes(), raw)
                expected = copy.deepcopy(message['proposal'])
                expected['evidence'][0]['id'] = result['evidence_mapping']['local:source_a']
                self.assertEqual(json.loads(output.read_bytes()), expected)
                self.assertEqual(self.env.session(sid)['evidence'][0]['observation'], message['proposal']['evidence'][0]['observation'])
                self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', output)
                self.assertEqual(co.read_json_artifact(sid, 'claude/proposal.json'), expected)
                self.assertEqual(source.read_bytes(), raw)
                self.assertEqual(self.env.calls(), calls_before + ['proposal'])

    def test_v2_single_observation_preserves_full_proposal_registry_and_freeze(self):
        sid = self.proposed()
        message = self.single_observation_message()
        source = self.draft(message)
        result, output = self.prepare(sid, message, source=source)
        self.assertEqual(result['handoff_version'], 2)
        expected = copy.deepcopy(message['proposal'])
        for entry in expected['evidence']:
            entry['id'] = result['evidence_mapping'].get(entry['id'], entry['id'])
        self.assertEqual(json.loads(output.read_bytes()), expected)
        registered = self.env.session(sid)['evidence'][0]
        self.assertEqual(registered['observation'], message['proposal']['evidence'][0]['observation'])
        self.assertEqual((co.store(sid) / 'architect-handoff.json').read_bytes(), source.read_bytes())
        self.assertNotIn('observation', json.loads(source.read_bytes())['evidence_manifest'][0])
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', output)
        self.assertEqual(co.read_json_artifact(sid, 'claude/proposal.json'), expected)

    def test_v2_duplicate_observation_is_rejected_even_when_equal(self):
        sid = self.proposed()
        for observation in (self.message()['proposal']['evidence'][0]['observation'], 'different wording'):
            with self.subTest(observation=observation):
                message = self.single_observation_message()
                message['evidence_manifest'][0]['observation'] = observation
                self.prepare(sid, message, False)

    def test_legacy_conflicting_observations_are_not_silently_converted(self):
        sid = self.proposed()
        message = self.message()
        message['evidence_manifest'][0]['observation'] = 'different wording of the observation'
        rejected, _ = self.prepare(sid, message, False)
        self.assertIn('ref/observation', rejected['error'])
        self.assertFalse((co.store(sid) / 'architect-handoff.json').exists())

    def test_handoff_version_must_be_explicit_supported_integer(self):
        sid = self.proposed()
        for version in (1, 3, '2', True, None):
            with self.subTest(version=version):
                message = self.single_observation_message()
                message['handoff_version'] = version
                rejected, _ = self.prepare(sid, message, False)
                self.assertIn('handoff_version', rejected['error'])
        message = self.single_observation_message()
        message = {k: v for k, v in message.items() if k != 'handoff_version'}
        self.prepare(sid, message, False)

    def test_v2_still_requires_full_observation_ref_range_and_current_source_hash(self):
        sid = self.proposed()
        variants = []
        message = self.single_observation_message()
        message['proposal']['evidence'][0] = {k: v for k, v in message['proposal']['evidence'][0].items() if k != 'observation'}
        variants.append(message)
        message = self.single_observation_message()
        message['evidence_manifest'][0]['file_sha256'] = '0' * 64
        variants.append(message)
        message = self.single_observation_message()
        message['proposal']['evidence'][0]['ref'] = 'src/app.py:L3-L4'
        variants.append(message)
        message = self.single_observation_message()
        message['evidence_manifest'][0]['lines'] = [1, 999]
        variants.append(message)
        for message in variants:
            with self.subTest(message=variants.index(message)):
                self.prepare(sid, message, False)

    def test_v2_rewriting_observation_after_prepare_is_rejected_by_freeze(self):
        sid = self.proposed()
        _, output = self.prepare(sid, self.single_observation_message())
        proposal = json.loads(output.read_bytes())
        proposal['evidence'][0]['observation'] += ' changed meaning'
        rejected = self.freeze_rejected(sid, self.draft(proposal))
        self.assertIn('semantic rewriting', rejected['error'])

    def test_full_semantic_content_and_exact_ids_are_preserved_then_legacy_freeze_runs(self):
        sid = self.proposed()
        message = self.message()
        result, output = self.prepare(sid, message)
        expected = copy.deepcopy(message['proposal'])
        expected['evidence'][0]['id'] = 'E3'
        self.assertEqual(json.loads(output.read_bytes()), expected)
        self.assertEqual(result['evidence_mapping'], {'local:source_a': 'E3'})
        self.assertEqual(result['sha256'], co.sha256(output.read_bytes()))
        entry = self.env.session(sid)['evidence'][0]
        self.assertEqual(entry['file_sha256'], message['evidence_manifest'][0]['file_sha256'])
        self.assertEqual(entry['source_state'], message['evidence_manifest'][0]['source_state'])
        self.assertEqual((co.store(sid) / 'evidence/claude/E3.txt').read_bytes(), b'line1\nline2\n')
        self.assertEqual(self.env.session(sid)['state'], 'CODEX_PROPOSED')
        frozen = self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', output)
        self.assertEqual(frozen['state'], 'CLAUDE_FROZEN')
        self.assertEqual(co.read_json_artifact(sid, 'claude/proposal.json'), expected)
        later = self.env.ok('evidence', 'add', '--session', sid, '--file', self.env.project / 'src/app.py', '--lines', '3-4')
        self.assertEqual(later['id'], 'E4')

    def test_documented_architect_example_matches_real_schema_and_prepares_without_rewriting(self):
        references = Path(__file__).resolve().parents[1] / 'references'
        text = (references / 'architect-handoff.md').read_text()
        self.assertIn('(architect-handoff.example.json)', text)
        self.assertNotIn('```', text)
        message = json.loads((references / 'architect-handoff.example.json').read_bytes())
        co.validate_doc(message['proposal'], 'proposal')
        source = self.env.project / 'src/app.py'
        manifest = message['evidence_manifest'][0]
        manifest.update(path=str(source), file_sha256=co.sha256(source.read_bytes()))
        manifest['source_state']['worktree'] = str(self.env.project)
        message['proposal']['evidence'][0]['ref'] = 'src/app.py:L1-L2'
        sid = self.proposed()
        result, output = self.prepare(sid, message)
        expected = copy.deepcopy(message['proposal'])
        expected['evidence'][0]['id'] = result['evidence_mapping']['local:source_a']
        self.assertEqual(json.loads(output.read_bytes()), expected)
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', output, '--normalize-refs')
        self.assertEqual(co.read_json_artifact(sid, 'claude/proposal.json'), expected)

    def test_init_project_root_supports_absolute_handoff_and_precise_ledger_ref_hints(self):
        init = self.env.ok('init', '--question-id', 'absolute-handoff')
        root, sid = Path(init['project_root']), init['session']
        self.assertEqual(root, self.env.project.resolve())
        self.assertFalse((root / '.local/cross-check-drafts').exists())
        brief, scope, index = self.env.inputs()
        self.env.ok('freeze-inputs', '--session', sid, '--brief', brief, '--scope', scope, '--index', index)
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        self.assertTrue(self.env.ok('investigation-ready', '--session', sid)['ready'])
        directory = root / '.local/cross-check-drafts' / sid
        directory.mkdir(parents=True)
        message = self.message()
        source, prepared = directory / 'architect_message.json', directory / 'prepared.json'
        source.write_text(json.dumps(message, ensure_ascii=False))
        result, output = self.prepare(sid, message, source=source, output=prepared)
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', output)
        self.env.ok('reveal', '--session', sid)
        frozen = co.read_artifact(sid, 'claude/proposal.json')
        base, payload = self.base(sid, stage='pre-debate')
        hints = base['hints']
        self.assertEqual(hints['proposal_ids_not_ledger_refs'], [{'id': 'E1', 'kind': 'brief'}, {'id': 'E2', 'kind': 'inference'}])
        self.assertNotIn('E1', hints['known_evidence_ids'])
        self.assertNotIn('E2', hints['known_evidence_ids'])
        self.assertIn(result['evidence_mapping']['local:source_a'], hints['known_evidence_ids'])
        self.assertIn('brief:L1', hints['reference_examples'])
        self.assertEqual(co.read_artifact(sid, 'claude/proposal.json'), frozen)

    def test_relative_handoff_paths_still_refuse_without_mutation(self):
        sid = self.proposed()
        message = self.message()
        source = self.draft(message)
        target = self.location('prepared.json')
        before, calls = self.snapshot(), self.env.calls()
        result, code = self.env.run('proposal', 'prepare', '--session', sid,
                                   '--file', source.relative_to(self.env.project), '--out', target)
        self.assertEqual(code, 2, result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.env.calls(), calls)

    def test_object_design_arrays_fail_with_precise_path_without_mutation(self):
        sid = self.proposed()
        for field in ('components', 'flows', 'contracts'):
            with self.subTest(field=field):
                message = self.message()
                message['proposal']['design'][field] = [{'name': 'preserve this detail', 'condition': 'A'}]
                result, _ = self.prepare(sid, message, False)
                self.assertIn('$.proposal.design.' + field + '[0]: expected string', result['error'])

    def test_nonfile_local_ids_fail_at_id_not_generic_prose_error_without_deletion(self):
        sid = self.proposed()
        for index, kind in ((1, 'brief'), (2, 'inference')):
            with self.subTest(kind=kind):
                message = self.message()
                message['proposal']['evidence'][index]['id'] = 'local:' + kind
                result, _ = self.prepare(sid, message, False)
                self.assertIn(f'$.proposal.evidence[{index}].id', result['error'])
                self.assertIn('do not delete or rewrite', result['error'])

    def freeze_rejected(self, sid, source):
        before, calls = self.snapshot(), self.env.calls()
        result, code = self.env.run('freeze', '--session', sid, '--side', 'claude', '--file', source, '--normalize-refs')
        self.assertEqual(code, 2, result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.env.calls(), calls)
        self.assertEqual(self.env.session(sid)['state'], 'CODEX_PROPOSED')
        return result

    def test_prepare_archives_exact_source_and_rejects_replacement_prepare(self):
        sid = self.proposed()
        message = self.message()
        source = self.draft(message)
        result, output = self.prepare(sid, message, source=source)
        self.assertEqual((co.store(sid) / 'architect-handoff.json').read_bytes(), source.read_bytes())
        receipt = self.env.session(sid)['proposal_handoff']
        self.assertEqual(receipt['source_sha256'], co.sha256(source.read_bytes()))
        self.assertEqual(receipt['prepared_sha256'], co.sha256(output.read_bytes()))
        changed = copy.deepcopy(message)
        changed['proposal']['risks'] = []
        result, _ = self.prepare(sid, changed, False)
        self.assertIn('already prepared', result['error'])

    def test_post_prepare_deleting_three_nonfile_evidence_entries_is_rejected(self):
        sid = self.proposed()
        message = self.message()
        extra = copy.deepcopy(message['evidence_manifest'][0])
        extra.update(key='local:source_b', lines=[3, 4])
        message['evidence_manifest'].append(extra)
        message['proposal']['evidence'].append({'id': 'local:source_b', 'kind': 'file',
            'ref': 'src/app.py:L3-L4', 'observation': extra['observation']})
        message['proposal']['evidence'].append({'id': 'E4', 'kind': 'brief', 'ref': 'scope:L1', 'observation': 'scope condition'})
        result, output = self.prepare(sid, message)
        prepared = json.loads(output.read_bytes())
        self.assertEqual(len(prepared['evidence']), 5)
        changed = copy.deepcopy(prepared)
        changed['evidence'] = [item for item in changed['evidence'] if item['kind'] == 'file']
        self.assertEqual(len(changed['evidence']), 2)
        rejected = self.freeze_rejected(sid, self.draft(changed))
        self.assertIn('evidence deletion', rejected['error'])
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', output)
        self.assertEqual(co.read_json_artifact(sid, 'claude/proposal.json'), prepared)

    def test_post_prepare_rewriting_meaning_conditions_and_order_is_rejected(self):
        sid = self.proposed()
        _, output = self.prepare(sid, self.message())
        original = json.loads(output.read_bytes())
        for field, value in [('assumptions', ['new assumption']), ('risks', []),
                             ('summary', original['summary'].replace('\n', ' ')),
                             ('evidence', list(reversed(original['evidence'])))]:
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = value
                result = self.freeze_rejected(sid, self.draft(changed))
                self.assertIn('semantic rewriting', result['error'])

    def test_original_message_edit_is_rejected_even_if_prepared_draft_unchanged(self):
        sid = self.proposed()
        message = self.message()
        source = self.draft(message)
        _, output = self.prepare(sid, message, source=source)
        message['proposal']['assumptions'] = ['silently revised']
        source.write_text(json.dumps(message))
        result = self.freeze_rejected(sid, output)
        self.assertIn('integrity changed', result['error'])

    def test_immutable_handoff_archive_tampering_is_rejected(self):
        sid = self.proposed()
        _, output = self.prepare(sid, self.message())
        archive = co.store(sid) / 'architect-handoff.json'
        archive.chmod(0o600)
        archive.write_text('{}')
        result = self.freeze_rejected(sid, output)
        self.assertIn('integrity changed', result['error'])

    def test_json_formatting_and_object_key_order_do_not_change_semantics(self):
        sid = self.proposed()
        message = self.message()
        _, output = self.prepare(sid, message)
        document = json.loads(output.read_bytes())
        source = self.draft(json.dumps(document, ensure_ascii=False, sort_keys=True))
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', source, '--normalize-refs')
        self.assertEqual(co.read_json_artifact(sid, 'claude/proposal.json'), document)

    def test_valid_noncanonical_brief_refs_are_preserved_with_normalize_flag(self):
        for ref in ('brief:L1-2', 'brief:L01'):
            with self.subTest(ref=ref):
                sid = self.proposed()
                message = self.message()
                message['proposal']['evidence'][1]['ref'] = ref
                _, output = self.prepare(sid, message)
                frozen = self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', output, '--normalize-refs')
                self.assertEqual(frozen['normalized_refs'], [])
                self.assertEqual(co.read_json_artifact(sid, 'claude/proposal.json')['evidence'][1]['ref'], ref)

    def test_manifest_envelope_does_not_reduce_existing_full_proposal_capacity(self):
        sid = self.proposed()
        message = self.message()
        components = ['x' * 16000 for _ in range(16)] + ['']
        message['proposal']['design']['components'] = components
        padding = co.MAX_DOC - 64 - len(co.dump(message['proposal']))
        self.assertTrue(0 < padding < co.MAX_STR)
        components[-1] = 'y' * padding
        self.assertGreater(len(json.dumps(message, ensure_ascii=False).encode()), co.MAX_DOC)
        result, output = self.prepare(sid, message)
        self.assertEqual(json.loads(output.read_bytes())['design']['components'], components)
        self.assertLessEqual(output.stat().st_size, co.MAX_DOC)

    def test_preexisting_registered_command_mcp_web_keep_their_original_kind(self):
        sid = self.proposed()
        registered = []
        for _ in range(3):
            registered.append(self.env.ok('evidence', 'add', '--session', sid, '--file', self.env.project / 'src/app.py', '--lines', '1-1')['id'])
        message = self.message()
        message['proposal']['evidence'] = [{'id': ident, 'kind': kind, 'ref': kind + ' raw observation', 'observation': 'unchanged'}
                                           for ident, kind in zip(registered, ('command', 'mcp', 'web'))]
        message['evidence_manifest'] = []
        result, output = self.prepare(sid, message)
        self.assertEqual(result['registered'], 0)
        self.assertEqual(json.loads(output.read_bytes()), message['proposal'])

    def test_unregistered_nonfile_kinds_are_not_promoted_to_registered_file_evidence(self):
        sid = self.proposed()
        for kind in ('command', 'mcp', 'web'):
            message = self.message()
            message['proposal']['evidence'][1] = {'id': 'E1', 'kind': kind, 'ref': kind + ' observation', 'observation': 'unregistered'}
            self.prepare(sid, message, False)

    def test_batch_is_not_partly_registered_when_later_source_hash_is_wrong(self):
        sid = self.proposed()
        message = self.message()
        second = copy.deepcopy(message['evidence_manifest'][0])
        second.update(key='local:source_b', lines=[3, 4], file_sha256='0' * 64)
        message['evidence_manifest'].append(second)
        message['proposal']['evidence'].append({'id': second['key'], 'kind': 'file', 'ref': 'src/app.py:L3-L4',
                                                'observation': second['observation']})
        self.prepare(sid, message, False)
        self.assertEqual(self.env.session(sid)['evidence'], [])
        self.assertFalse((co.store(sid) / 'evidence/claude').exists())

    def test_free_prose_local_key_is_rejected_instead_of_guessed_replacement(self):
        sid = self.proposed()
        message = self.message()
        message['proposal']['summary'] += ' see local:source_a for the condition'
        result, _ = self.prepare(sid, message, False)
        self.assertIn('prose', result['error'])

    def test_exploration_local_basis_and_bad_shape_are_rejected_at_prepare(self):
        # 設計探索欄も prepare で検査する: basis の local キーは本文扱いで拒否、意味違反は SchemaError。
        explored = {'applicable': True, 'not_applicable_reason': '', 'target_architecture': 't',
                    'candidates': [{'name': 'A', 'approach': 'a', 'tradeoffs': 't', 'basis': ['local:source_a']},
                                   {'name': 'B', 'approach': 'b', 'tradeoffs': 't', 'basis': ['src/app.py:L1-L2']}],
                    'chosen': 'A', 'current_gap': [], 'migration_path': [], 'compromises': []}
        for exploration, message in ((explored, 'prose'), (explored | {'candidates': explored['candidates'][1:]},
                                                           'at least two candidates')):
            with self.subTest(message=message):
                sid = self.proposed()
                handoff = self.message()
                handoff['proposal']['exploration'] = exploration
                result, _ = self.prepare(sid, handoff, False)
                self.assertIn(message, result['error'])

    def test_duplicate_missing_and_ambiguous_manifest_references_are_rejected(self):
        sid = self.proposed()
        variants = []
        message = self.message()
        message['evidence_manifest'].append(copy.deepcopy(message['evidence_manifest'][0]))
        variants.append(message)
        message = self.message()
        message['evidence_manifest'] = []
        variants.append(message)
        message = self.message()
        message['proposal']['evidence'][0]['ref'] = 'src/app.py:L2-L3'
        variants.append(message)
        message = self.message()
        message['proposal']['evidence'].append(copy.deepcopy(message['proposal']['evidence'][0]))
        variants.append(message)
        for message in variants:
            self.prepare(sid, message, False)

    def test_invalid_range_hash_schema_side_and_source_state_fail_before_registration(self):
        sid = self.proposed()
        variants = []
        for field, value in (('lines', [0, 99]), ('file_sha256', '0' * 64), ('source_state', {})):
            message = self.message()
            message['evidence_manifest'][0][field] = value
            variants.append(message)
        message = self.message()
        message['proposal'].pop('risks')
        variants.append(message)
        message = self.message()
        message['proposal']['side'] = 'codex'
        variants.append(message)
        for message in variants:
            self.prepare(sid, message, False)

    def test_changed_current_source_is_not_silently_rewritten(self):
        sid = self.proposed()
        message = self.message()
        (self.env.project / 'src/app.py').write_text('new code\nline2\nline3\nline4\n')
        self.prepare(sid, message, False)

    def test_existing_output_is_rejected_without_evidence_or_session_writes(self):
        sid = self.proposed()
        self.prepare(sid, self.message(), False, output=self.draft('existing sentinel'))

    def test_identical_message_input_and_output_is_rejected(self):
        sid = self.proposed()
        message = self.message()
        source = self.draft(message)
        self.prepare(sid, message, False, output=source, source=source)

    def test_private_session_evidence_source_is_rejected(self):
        sid = self.proposed()
        private = co.store(sid) / 'private-source.txt'
        private.write_text('private fake data\n')
        message = self.message()
        message['evidence_manifest'][0].update(path=str(private), lines=[1, 1], file_sha256=co.sha256(private.read_bytes()))
        self.prepare(sid, message, False)

    def test_other_project_evidence_source_is_rejected(self):
        sid = self.proposed()
        external = self.env.file('external.txt', 'external fake data\n')
        message = self.message()
        message['evidence_manifest'][0].update(path=str(external), lines=[1, 1], file_sha256=co.sha256(external.read_bytes()))
        self.prepare(sid, message, False)

    def test_real_subworktree_metadata_is_preserved_without_rewriting_it_to_project_root(self):
        sid = self.proposed()
        worktree = self.env.project / 'subrepo-app'
        source = worktree / 'src/app.py'
        source.parent.mkdir(parents=True)
        source.write_text('subrepo line 1\nsubrepo line 2\n')
        message = self.message()
        manifest = message['evidence_manifest'][0]
        manifest.update(path=str(source), file_sha256=co.sha256(source.read_bytes()))
        manifest['source_state'] = {'worktree': str(worktree), 'checked_at': '2026-11-17',
                                    'status': 'Git worktree; branch feature; commit abc; tracked and untracked changes'}
        message['proposal']['evidence'][0]['ref'] = 'subrepo-app/src/app.py:L1-L2'
        self.prepare(sid, message)
        self.assertEqual(self.env.session(sid)['evidence'][0]['source_state'], manifest['source_state'])

    def test_reported_worktree_must_contain_its_source_file(self):
        sid = self.proposed()
        other = self.env.project / 'other-worktree'
        other.mkdir()
        message = self.message()
        message['evidence_manifest'][0]['source_state']['worktree'] = str(other)
        self.prepare(sid, message, False)

    def test_external_reported_worktree_is_rejected(self):
        sid = self.proposed()
        message = self.message()
        message['evidence_manifest'][0]['source_state']['worktree'] = str(self.env.root)
        self.prepare(sid, message, False)

    def test_symlinked_reported_worktree_is_rejected(self):
        sid = self.proposed()
        link = self.env.project / 'linked-worktree'
        link.symlink_to(self.env.project / 'src', target_is_directory=True)
        message = self.message()
        message['evidence_manifest'][0]['source_state']['worktree'] = str(link)
        self.prepare(sid, message, False)

    def test_protected_reported_worktree_is_rejected(self):
        sid = self.proposed()
        worktree = self.env.project / 'hooks'
        worktree.mkdir()
        message = self.message()
        message['evidence_manifest'][0]['source_state']['worktree'] = str(worktree)
        self.prepare(sid, message, False)

    def test_symlink_evidence_source_is_rejected(self):
        sid = self.proposed()
        link = self.env.project / 'src/link.py'
        link.symlink_to(self.env.project / 'src/app.py')
        message = self.message()
        message['evidence_manifest'][0]['path'] = str(link)
        self.prepare(sid, message, False)

    def test_protected_output_is_rejected_before_registration(self):
        sid = self.proposed()
        self.prepare(sid, self.message(), False, output=co.art(sid) / 'prepared.json')

    def test_message_created_before_codex_end_is_rejected_even_after_rewriting_contents(self):
        sid = self.env.to_inputs_frozen()
        source = self.draft('{}')
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        message = self.message()
        source.write_text(json.dumps(message))
        self.prepare(sid, message, False, source=source)

    def test_first_policy_stop_blocks_prepare_without_registration_or_followup_model_call(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "sys.stderr.write('Command blocked by PreToolUse hook: fake denial\\n'); sys.stderr.flush()"})
        output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual((output['state'], code), ('STOPPED', 2))
        self.prepare(sid, self.message(), False)
        self.assertEqual(self.env.calls(), ['proposal'])


class ArchitectCaptureTests(SubmissionBase):
    """Agent の output_file（transcript への symlink）から最終報告を原文のまま保存する。"""

    def transcript(self, entries, agent_id='a1b2c3', target_name=None, folder='subagents', raw=None):
        # 実測の配置を模す: tasks/<id>.output -> subagents/agent-<id>.jsonl
        target = self.env.root / 'transcripts' / folder / (target_name or f'agent-{agent_id}.jsonl')
        target.parent.mkdir(parents=True, exist_ok=True)
        text = raw if raw is not None else ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in entries)
        target.write_text(text, encoding='utf-8')
        link = self.env.root / 'tasks' / f'{agent_id}.output'
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(target)
        return link

    def handback(self, message='{}'):
        return self.assistant({'type': 'tool_use', 'name': 'SubagentHandback', 'input': {'message': message}})

    @staticmethod
    def assistant(*content):
        return {'type': 'assistant', 'message': {'role': 'assistant', 'content': list(content)}}

    def proposed(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        return sid

    def capture(self, sid, link, expected=True):
        out = self.location('architect-message.json')
        before = self.snapshot()
        result, code = self.env.run('proposal', 'capture', '--session', sid, '--agent-output', link, '--out', out)
        self.assertEqual(code, 0 if expected else 2, result)
        if not expected:
            self.assertFalse(out.exists())
            self.assertEqual(self.snapshot(), before)
        return result, out

    def test_handback_is_saved_verbatim_and_prepares_and_freezes(self):
        sid = self.proposed()
        message = ProposalHandoffTests.message(self)
        message['handoff_version'] = 2
        message['proposal']['evidence'] = [e for e in message['proposal']['evidence'] if e['kind'] == 'brief']
        message['evidence_manifest'] = []
        body = ' ' + json.dumps(message, ensure_ascii=False, indent=2).replace('\n', '\r\n')  # 文字を足さず保存されること
        link = self.transcript([
            self.assistant({'type': 'text', 'text': '調査中のメモ'}),
            self.assistant({'type': 'text', 'text': '最終報告を渡します'},
                           {'type': 'tool_use', 'id': 't1', 'name': 'SubagentHandback', 'input': {'message': body}}),
        ])
        result, out = self.capture(sid, link)
        self.assertEqual(out.read_bytes(), body.encode('utf-8'))
        self.assertEqual((result['source'], result['bytes'], result['sha256']),
                         ('SubagentHandback', len(body.encode('utf-8')), co.sha256(body.encode('utf-8'))))
        prepared, code = self.env.run('proposal', 'prepare', '--session', sid, '--file', out,
                                      '--out', self.location('proposal.json'))
        self.assertEqual(code, 0, prepared)
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', prepared['path'])
        self.assertEqual(co.read_json_artifact(sid, 'claude/proposal.json'), message['proposal'])

    def test_last_text_is_used_only_without_handback(self):
        sid = self.proposed()
        link = self.transcript([self.assistant({'type': 'text', 'text': 'first'}),
                                {'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'ignored user text'}]}},
                                self.assistant({'type': 'text', 'text': '{"final": true}'})])
        result, out = self.capture(sid, link)
        self.assertEqual((result['source'], out.read_text()), ('text', '{"final": true}'))

    def test_handback_in_earlier_entry_beats_later_text(self):
        sid = self.proposed()
        link = self.transcript([self.handback('{"final": "handback"}'),
                                self.assistant({'type': 'text', 'text': '後から出た説明文'})])
        result, out = self.capture(sid, link)
        self.assertEqual((result['source'], out.read_text()), ('SubagentHandback', '{"final": "handback"}'))

    def test_refusals_leave_no_file_and_no_session_change(self):
        sid = self.proposed()
        good = [self.handback()]
        cases = {
            'no message': self.transcript([self.assistant({'type': 'tool_use', 'name': 'Read', 'input': {}})], 'none'),
            'wrong target name': self.transcript(good, 'wrongname', target_name='notes.jsonl'),
            'not in subagents': self.transcript(good, 'outside', folder='other'),
            'id mismatch': self.transcript(good, 'mismatch', target_name='agent-other.jsonl'),
            'broken last line': self.transcript([], 'broken', raw=json.dumps(good[0]) + '\n{"type": "assistant", "mess'),
            'broken middle line': self.transcript([], 'middle', raw='not json\n' + json.dumps(good[0]) + '\n'),
        }
        direct = self.env.root / 'transcripts' / 'subagents' / 'agent-direct.jsonl'
        direct.write_text(json.dumps(good[0]) + '\n')
        cases['not an .output path'] = direct
        cases['missing'] = self.env.root / 'tasks' / 'missing.output'
        fifo_target = self.env.root / 'transcripts' / 'subagents' / 'agent-fifo.jsonl'
        os.mkfifo(fifo_target)
        fifo = self.env.root / 'tasks' / 'fifo.output'
        fifo.symlink_to(fifo_target)
        cases['fifo'] = fifo
        for label, path in cases.items():
            with self.subTest(label=label):
                self.capture(sid, path, expected=False)
        with self.subTest(label='relative path'):
            result, code = self.env.run('proposal', 'capture', '--session', sid, '--agent-output', 'tasks/x.output',
                                        '--out', self.location('m.json'))
            self.assertEqual(code, 2, result)
        with self.subTest(label='oversized'):
            link = self.transcript(good, 'big')
            with patch.object(co.HANDOFF, 'AGENT_OUTPUT_MAX', 10):
                self.capture(sid, link, expected=False)
        with self.subTest(label='existing output'):
            link = self.transcript(good, 'exists')
            out = self.location('exists.json')
            out.write_text('keep')
            result, code = self.env.run('proposal', 'capture', '--session', sid, '--agent-output', link, '--out', out)
            self.assertEqual((code, out.read_text()), (2, 'keep'), result)
        self.assertNotIn('proposal_handoff', self.env.session(sid))

    def test_capture_requires_completed_proposal_and_runs_once_before_prepare(self):
        link = self.transcript([self.handback()])
        self.capture(self.env.to_inputs_frozen(), link, expected=False)
        stopped = self.proposed()
        self.env.edit_session(stopped, state='STOPPED', stop={'category': 'POLICY'})
        self.capture(stopped, link, expected=False)
        sid = self.proposed()
        self.env.edit_session(sid, proposal_handoff={'source_sha256': 'x'})
        self.capture(sid, link, expected=False)


class ArchitectEventCaptureTests(SubmissionBase):
    """永続 transcript がない呼出しでも既存完了通知を機械的に保存する。"""

    proposed = ArchitectCaptureTests.proposed

    def events(self, body='{}'):
        common = {'type': 'system', 'task_id': 'a1', 'session_id': 'caller1',
                  'tool_use_id': 'tool1', 'run_id': 'run1'}
        return [dict(common, subtype='task_started', task_type='local_agent', subagent_type='architect'),
                dict(common, subtype='task_notification', status='completed', handback='send',
                     handback_report={'text': body})]

    def event_capture(self, sid, entries, expected=True, raw=None, extra=()):
        stream = self.location('events.jsonl')
        stream.write_bytes(raw if raw is not None else ''.join(json.dumps(e, ensure_ascii=False) + '\n'
                           for e in entries).encode('utf-8'))
        out = self.location('captured.json')
        before, calls, session_before = self.snapshot(), self.env.calls(), self.env.session(sid)
        result, code = self.env.run('proposal', 'capture', '--session', sid, '--agent-events', stream,
                                   '--agent-id', 'a1', '--caller-session-id', 'caller1', '--out', out, *extra)
        self.assertEqual(code, 0 if expected else 2, result)
        self.assertEqual(self.env.session(sid), session_before)
        self.assertEqual(self.env.calls(), calls)
        if not expected:
            self.assertFalse(out.exists())
            self.assertEqual(self.snapshot(), before)
        return result, out

    def test_events_preserve_full_handoff_through_prepare_and_freeze(self):
        sid = self.proposed()
        message = ProposalHandoffTests.single_observation_message(self)
        body = ' \r\n' + json.dumps(message, ensure_ascii=False, indent=2).replace('\n', '\r\n')
        entries = self.events(body)
        other = self.events('wrong agent')
        for entry in other:
            entry['task_id'] = 'other'
        result, out = self.event_capture(sid, other + entries)
        self.assertEqual(out.read_bytes(), body.encode('utf-8'))
        self.assertEqual(result['sha256'], co.sha256(body.encode('utf-8')))
        prepared = self.env.ok('proposal', 'prepare', '--session', sid, '--file', out,
                               '--out', self.location('proposal.json'))
        self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', prepared['path'])
        frozen = co.read_json_artifact(sid, 'claude/proposal.json')
        expected = copy.deepcopy(message['proposal'])
        expected['evidence'][0]['id'] = prepared['evidence_mapping']['local:source_a']
        self.assertEqual(frozen, expected)

    # ProposalHandoffTests の生成処理だけ再利用し、追加 AI や実行系は使わない。
    message = ProposalHandoffTests.message

    def test_malformed_body_is_preserved_not_repaired(self):
        sid = self.proposed()
        for body in ['説明\r\n```json\n{}', '{"text":"한글 ``` agentId: x"}', '{}\n']:
            with self.subTest(body=body):
                _, out = self.event_capture(sid, self.events(body))
                self.assertEqual(out.read_bytes(), body.encode('utf-8'))
                result, code = self.env.run('proposal', 'prepare', '--session', sid, '--file', out,
                                            '--out', self.location('proposal.json'))
                self.assertEqual(code, 2, result)
                self.assertEqual(out.read_bytes(), body.encode('utf-8'))

    def test_wrong_incomplete_ambiguous_and_broken_events_are_refused(self):
        sid = self.proposed()
        cases = [[], self.events()[:1], self.events()[1:], self.events() + self.events()[1:]]
        for index, key, value in [(0, 'subagent_type', 'other'), (1, 'status', 'failed'),
                                  (1, 'tool_use_id', 'other'), (1, 'run_id', 'other'),
                                  (1, 'session_id', 'other'), (1, 'task_id', 'other'),
                                  (1, 'handback', 'none'), (1, 'handback_report', {}),
                                  (1, 'handback_report', {'text': 1})]:
            entries = self.events()
            entries[index][key] = value
            cases.append(entries)
        for index, entries in enumerate(cases):
            with self.subTest(case=index):
                self.event_capture(sid, entries, expected=False)
        good = ''.join(json.dumps(e) + '\n' for e in self.events()).encode()
        for raw in [good + b'{', b'broken\n' + good, good + b'{"type":1,"type":2}\n']:
            with self.subTest(raw=raw[-30:]):
                self.event_capture(sid, [], expected=False, raw=raw)

    def test_event_mode_preserves_existing_state_and_source_limits(self):
        self.event_capture(self.env.to_inputs_frozen(), self.events(), expected=False)
        sid = self.proposed()
        with patch.object(co.HANDOFF, 'AGENT_OUTPUT_MAX', 10):
            self.event_capture(sid, self.events(), expected=False)
        self.env.edit_session(sid, proposal_handoff={'source_sha256': 'x'})
        self.event_capture(sid, self.events(), expected=False)

    def test_event_cli_paths_and_existing_output_are_not_bypassed(self):
        sid = self.proposed()
        stream = self.location('events.jsonl')
        stream.write_text(''.join(json.dumps(e) + '\n' for e in self.events()))
        out = self.location('output.json')
        base = ['proposal', 'capture', '--session', sid, '--out', out]
        valid = ['--agent-id', 'a1', '--caller-session-id', 'caller1']
        for args in [base + ['--agent-events', stream],
                     base + ['--agent-events', 'relative.jsonl'] + valid]:
            result, code = self.env.run(*args)
            self.assertEqual(code, 2, result)
            self.assertFalse(out.exists())
        with self.assertRaises(SystemExit) as error:
            self.env.run(*base, '--agent-events', stream, '--agent-output', '/tmp/unused.output', *valid)
        self.assertEqual(error.exception.code, 2)
        linked = self.location('linked.jsonl')
        linked.symlink_to(stream)
        outside = self.env.root / 'outside.jsonl'
        outside.write_bytes(stream.read_bytes())
        for source in [linked, outside]:
            result, code = self.env.run(*base, '--agent-events', source, *valid)
            self.assertEqual(code, 2, result)
            self.assertFalse(out.exists())
        out.write_text('keep')
        result, code = self.env.run(*base, '--agent-events', stream, *valid)
        self.assertEqual((code, out.read_text()), (2, 'keep'), result)


if __name__ == '__main__':
    import unittest
    unittest.main()
