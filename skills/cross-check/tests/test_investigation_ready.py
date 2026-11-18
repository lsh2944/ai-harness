"""初回調査の開始条件を偽 Codex・隔離一時領域で検査する。実 AI の行動順序は検証しない。"""
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import co_design_fixture as fx

co = fx.co
SKILL = Path(__file__).resolve().parents[1]


def tree_snapshot(root):
    # atime は読み取りでも変わり得るため、内容・mtime・権限・項目の集合を比較する。
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_mtime_ns,
                                       p.read_bytes() if p.is_file() else None)
            for p in root.rglob('*')}


class InvestigationReadyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='investigation-ready-test-')
        self.env = fx.Env(self.temp.name)

    def tearDown(self):
        self.env.close()
        self.temp.cleanup()

    def snapshot(self):
        return tree_snapshot(self.env.root)

    def ready(self, sid, expected=True, architect_prompt=False):
        before, calls = self.snapshot(), self.env.calls()
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(co, name, side_effect=AssertionError(name)))
                     for name in ('find_codex', 'run_call', 'save_session', 'write_private', 'write_frozen', 'reap_stale_calls')]
            mocks += [stack.enter_context(patch.object(co.subprocess, 'run', side_effect=AssertionError('subprocess'))),
                      stack.enter_context(patch.object(co.os, 'killpg', side_effect=AssertionError('killpg')))]
            extra = ['--architect-prompt'] if architect_prompt else []
            output, code = self.env.run('investigation-ready', '--session', sid, *extra)
            for mocked in mocks:
                mocked.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.env.calls(), calls)
        self.assertEqual(code, 0 if expected else 2, output)
        self.assertEqual(output.get('ready', False), expected, output)
        if not expected:
            self.assertNotIn('architect_prompt', output)
            self.assertNotIn('architect_prompt_sha256', output)
        return output

    def test_generated_prompt_is_readonly_neutral_and_deterministic_for_both_plans(self):
        for plan_b in (False, True):
            with self.subTest(plan_b=plan_b):
                sid = self.proposed(plan_b, "result['summary'] = 'PRIVATE-DESIGN-SENTINEL'", data=True)
                with patch.object(co, 'read_source', wraps=co.read_source) as reads:
                    result = self.ready(sid, architect_prompt=True)
                prompt = result['architect_prompt']
                self.assertEqual(result['architect_prompt_sha256'], co.sha256(prompt.encode('utf-8')))
                self.assertEqual(prompt, self.ready(sid, architect_prompt=True)['architect_prompt'])
                self.assertNotIn('PRIVATE-DESIGN-SENTINEL', prompt)
                self.assertNotIn('result.json', prompt)
                self.assertNotIn('links.json', prompt)
                self.assertNotIn('/work/', prompt)
                self.assertNotIn('/logs/', prompt)
                for item in [*result['inputs'].values(), *result['data'].values()]:
                    self.assertIn(item['path'], prompt)
                    self.assertIn(item['sha256'], prompt)
                self.assertIn(str(co.SCHEMAS / 'proposal.schema.json'), prompt)
                self.assertIn(str(co.HERE.parent / 'references/architect-handoff.md'), prompt)
                self.assertEqual(prompt, co.neutral_architect_prompt(result))
                actual_sources = {str(self.env.project / 'src/app.py'), str(self.env.project / 'src/other.py')}
                self.assertFalse(actual_sources & {str(c.args[0]) for c in reads.call_args_list})

    def test_generated_prompt_is_unavailable_before_finish_after_stop_or_on_tamper(self):
        self.ready(self.env.to_inputs_frozen(), False, architect_prompt=True)
        sid = self.proposed()
        original = copy.deepcopy(self.env.session(sid))
        running = copy.deepcopy(original['calls'])
        running[0].update(execution='running', ended_at=None)
        self.env.edit_session(sid, calls=running)
        self.ready(sid, False, architect_prompt=True)
        self.env.edit_session(sid, calls=original['calls'], state='STOPPED', stop={'category': 'EXEC'})
        self.ready(sid, False, architect_prompt=True)
        self.env.edit_session(sid, state='CODEX_PROPOSED', stop=None)
        path = co.art(sid) / 'inputs/brief.md'
        path.chmod(0o600)
        path.write_text('changed neutral input')
        self.ready(sid, False, architect_prompt=True)

    def test_generated_prompt_rejects_private_artifact_integrity_change(self):
        sid = self.proposed()
        call = self.env.session(sid)['calls'][0]
        path = co.call_file(sid, call, 'result.json')
        path.chmod(0o600)
        path.write_text('{}')
        self.ready(sid, False, architect_prompt=True)

    def test_generated_prompt_preserves_incomplete_caveat_without_private_findings(self):
        sid = self.proposed(behavior="result['status'] = 'INCOMPLETE'; result['unverified'] = ['PRIVATE-OPEN-QUESTION']")
        result = self.ready(sid, architect_prompt=True)
        self.assertIn('INCOMPLETE', result['caveat'])
        self.assertNotIn('PRIVATE-OPEN-QUESTION', result['architect_prompt'])

    def test_prompt_option_does_not_accept_parent_supplied_design_text(self):
        import io
        from contextlib import redirect_stderr
        sid = self.proposed()
        before, calls = self.snapshot(), self.env.calls()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            co.parser().parse_args(['investigation-ready', '--session', sid, '--architect-prompt', '--prompt', 'parent design'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.env.calls(), calls)

    def proposed(self, plan_b=False, behavior=None, data=False):
        if data:
            sid = self.env.init(plan_b)
            brief, scope, index = self.env.inputs()
            source = self.env.file('raw.txt', 'original raw data\n')
            self.env.ok('freeze-inputs', '--session', sid, '--brief', brief, '--scope', scope,
                        '--index', index, '--data', source)
        else:
            sid = self.env.to_inputs_frozen(plan_b)
        if behavior:
            self.env.behaviors({'proposal': behavior})
        self.env.run('call', '--session', sid, '--phase', 'proposal')
        return sid

    def test_init_without_lock_and_frozen_inputs_are_not_ready(self):
        sid = self.env.init()
        self.assertFalse((self.env.artifacts / sid / '.lock').exists())
        self.ready(sid, False)
        self.ready(self.env.to_inputs_frozen(), False)

    def test_running_record_is_refused_without_reaping_or_stopping(self):
        sid = self.proposed()
        calls = self.env.session(sid)['calls']
        calls[0].update(execution='running', ended_at=None, pgid=12345)
        self.env.edit_session(sid, calls=calls)
        self.ready(sid, False)
        self.assertEqual(self.env.session(sid)['state'], 'CODEX_PROPOSED')
        self.assertEqual(self.env.session(sid)['calls'][0]['execution'], 'running')

    def test_concurrent_command_is_refused_without_changes(self):
        sid = self.proposed()
        with co.session_lock(sid):
            self.ready(sid, False)

    def test_success_returns_only_neutral_inputs_for_both_plans(self):
        for plan_b in (False, True):
            with self.subTest(plan_b=plan_b):
                sid = self.proposed(plan_b, "result['summary'] = 'PRIVATE-PROPOSAL-SENTINEL'", data=True)
                output = self.ready(sid)
                self.assertEqual(set(output), {'status', 'ready', 'session', 'proposal_call', 'inputs', 'data'})
                self.assertEqual(set(output['proposal_call']), {'index', 'ended_at', 'model_status'})
                self.assertEqual(set(output['inputs']), set(co.INPUT_FILES))
                self.assertEqual(len(output['data']), 1)
                for entry in [*output['inputs'].values(), *output['data'].values()]:
                    self.assertEqual(set(entry), {'path', 'sha256'})
                    self.assertEqual(co.sha256(Path(entry['path']).read_bytes()), entry['sha256'])
                serialized = json.dumps(output)
                for forbidden in ('PRIVATE-PROPOSAL-SENTINEL', 'summary', 'problems', 'result.json', 'links.json',
                                  '/work/', '/logs/', 'proposal.json'):
                    self.assertNotIn(forbidden, serialized)
                self.assertFalse((self.env.artifacts / sid / 'artifacts/codex/proposal.json').exists())

    def test_model_incomplete_success_is_ready_with_generic_caveat(self):
        sid = self.proposed(behavior="result['status'] = 'INCOMPLETE'; result['unverified'] = ['PRIVATE-MISSING-CONTRACT']")
        self.assertEqual(self.env.session(sid)['calls'][0]['execution'], 'ok')
        output = self.ready(sid)
        self.assertEqual(output['proposal_call']['model_status'], 'INCOMPLETE')
        self.assertIn('INCOMPLETE', output['caveat'])
        self.assertNotIn('PRIVATE-MISSING-CONTRACT', json.dumps(output))

    def test_actual_failed_call_and_stopped_sessions_are_not_ready(self):
        sid = self.proposed(behavior="result.pop('risks')")
        self.assertEqual(self.env.session(sid)['state'], 'STOPPED')
        self.ready(sid, False)
        sid = self.proposed()
        self.env.edit_session(sid, state='STOPPED', stop={'category': 'EXEC', 'reason': 'stopped'})
        self.ready(sid, False)

    def test_other_states_and_inconsistent_stop_are_refused(self):
        sid = self.proposed()
        for state in ('INPUTS_FROZEN', 'CLAUDE_FROZEN', 'REVEALED', 'DEBATED', 'UNKNOWN'):
            with self.subTest(state=state):
                self.env.edit_session(sid, state=state)
                self.ready(sid, False)
        self.env.edit_session(sid, state='CODEX_PROPOSED', stop={'category': 'EXEC'})
        self.ready(sid, False)

    def test_incomplete_call_and_input_records_are_refused(self):
        sid = self.proposed()
        session = self.env.session(sid)
        for field in ('index', 'phase', 'started_at', 'ended_at', 'execution', 'status', 'model_status',
                      'result_sha256', 'links_sha256'):
            with self.subTest(missing=field):
                calls = copy.deepcopy(session['calls'])
                calls[0].pop(field)
                self.env.edit_session(sid, calls=calls)
                self.ready(sid, False)
        for field, value in (('ended_at', 0), ('ended_at', float('nan')), ('ended_at', True),
                             ('index', True), ('index', 2), ('execution', 'failed'), ('status', 'unknown')):
            with self.subTest(field=field, value=value):
                calls = copy.deepcopy(session['calls'])
                calls[0][field] = value
                self.env.edit_session(sid, calls=calls)
                self.ready(sid, False)
        self.env.edit_session(sid, calls=session['calls'], inputs={})
        self.ready(sid, False)
        self.env.edit_session(sid, inputs=session['inputs'], calls=[])
        self.ready(sid, False)

    def test_frozen_inputs_and_attached_data_change_or_loss_are_refused(self):
        for name in ('brief', 'scope', 'index', 'data'):
            for missing in (False, True):
                with self.subTest(name=name, missing=missing):
                    sid = self.proposed(data=True)
                    session = self.env.session(sid)
                    relative = 'inputs/data/' + next(iter(session['data'])) if name == 'data' else 'inputs/' + co.INPUT_FILES[name]
                    path = co.art(sid) / relative
                    os.chmod(path.parent, 0o700)
                    os.chmod(path, 0o600)
                    if missing:
                        path.unlink()
                    else:
                        path.write_text('changed\n')
                    self.ready(sid, False)

    def test_missing_or_inconsistent_attachment_inventory_is_refused(self):
        sid = self.proposed(data=True)
        session = self.env.session(sid)
        for omitted in (('data',), ('data_meta',), ('data', 'data_meta')):
            for remove_field in (False, True):
                with self.subTest(omitted=omitted, remove_field=remove_field):
                    changed = copy.deepcopy(session)
                    for field in omitted:
                        if remove_field:
                            changed.pop(field)
                        else:
                            changed[field] = {}
                    co.save_session(changed)
                    self.ready(sid, False)
        co.save_session(session)
        self.env.edit_session(sid, data_meta={**session['data_meta'], 'unrecorded.txt': {'bytes': 0, 'lines': 0}})
        self.ready(sid, False)
        co.save_session(session)
        data_dir = co.art(sid) / 'inputs/data'
        os.chmod(data_dir, 0o700)
        (data_dir / 'unrecorded.txt').write_text('unrecorded raw data\n')
        self.ready(sid, False)

    def test_legacy_session_without_attachment_fields_or_files_is_ready(self):
        sid = self.proposed()
        session = self.env.session(sid)
        self.assertNotIn('data', session)
        self.assertNotIn('data_meta', session)
        self.assertFalse((co.art(sid) / 'inputs/data').exists())
        self.assertEqual(self.ready(sid)['data'], {})

    def test_symlinked_input_parent_is_refused_before_inventory(self):
        sid = self.proposed(data=True)
        inputs = co.art(sid) / 'inputs'
        preserved = co.art(sid) / 'preserved-inputs'
        inputs.rename(preserved)
        inputs.symlink_to(preserved, target_is_directory=True)
        with patch.object(Path, 'iterdir', side_effect=AssertionError('symlink inventory')) as inventory:
            self.ready(sid, False)
            inventory.assert_not_called()

    def test_private_result_and_links_change_or_loss_are_refused(self):
        for name in ('result.json', 'links.json'):
            for missing in (False, True):
                with self.subTest(name=name, missing=missing):
                    sid = self.proposed()
                    call = self.env.session(sid)['calls'][0]
                    path = co.call_file(sid, call, name)
                    if missing:
                        path.unlink()
                        fallback = co.call_file(sid, call, name)
                        if fallback is not None:
                            fallback.unlink()
                    else:
                        path.write_text('changed\n')
                    self.ready(sid, False)


class MainFreezeHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='main-freeze-test-')
        self.env = fx.Env(self.temp.name)

    def tearDown(self):
        self.env.close()
        self.temp.cleanup()

    def proposed(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        self.assertTrue(self.env.ok('investigation-ready', '--session', sid)['ready'])
        return sid

    def test_main_freezes_new_external_draft_without_losing_design_or_evidence(self):
        sid = self.proposed()
        # メインが登録した実 ID を持つ設計を、別の一般ファイルに保存する。
        proposal = self.env.claude_proposal(sid)
        proposal['summary'] = '設計全文: 条件 A のとき B。\n未確認の境界も保持する。'
        proposal['requirement_mapping'] = [{'requirement': 'R1', 'approach': '条件 A を確認してから B'}]
        proposal['alternatives'] = [{'option': '代案 C', 'reason_rejected': '契約 D が未確認のため保留'}]
        proposal['risks'] = [{'severity': 'high', 'risk': '契約 D が変わる可能性', 'mitigation': '両側の原文を照合'}]
        proposal['assumptions'] = ['条件 A が成立するときだけ採用']
        proposal['validation'] = [{'check': '境界条件と両側の契約を照合', 'layer': 'mock'}]
        proposal['unverified'] = ['実環境の挙動は未確認']
        source = self.env.project / '.local/cross-check-drafts' / sid / 'proposal.json'
        source.parent.mkdir(parents=True)
        original = (json.dumps(proposal, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        source.write_bytes(original)
        output = self.env.ok('freeze', '--session', sid, '--side', 'claude', '--file', source, '--normalize-refs')
        target = co.art(sid) / 'claude/proposal.json'
        self.assertEqual(output['state'], 'CLAUDE_FROZEN')
        self.assertEqual(self.env.session(sid)['state'], 'CLAUDE_FROZEN')
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(json.loads(target.read_bytes()), proposal)
        self.assertEqual(target.stat().st_mode & 0o777, 0o400)
        registered = self.env.session(sid)['evidence'][0]['id']
        self.assertEqual(proposal['evidence'][0]['id'], registered)
        self.assertEqual(output['sha256'], co.sha256(original))
        self.assertEqual(self.env.calls(), ['proposal'])

    def assert_protected_source_refused_without_changes(self, sid, source):
        before, calls = tree_snapshot(self.env.root), self.env.calls()
        output, code = self.env.run('freeze', '--session', sid, '--side', 'claude', '--file', source)
        self.assertEqual((output['status'], code), ('REFUSED', 2), output)
        self.assertIn('not evidence sources', output['error'])
        self.assertEqual(tree_snapshot(self.env.root), before)
        self.assertEqual(self.env.calls(), calls)
        self.assertEqual(self.env.session(sid)['state'], 'CODEX_PROPOSED')
        self.assertFalse((co.art(sid) / 'claude/proposal.json').exists())

    def test_artifact_root_work_source_is_refused_without_retry_or_mutation(self):
        # 正常系とは別 fixture で一度だけ拒否を確認し、経路を変えて再試行しない。
        sid = self.proposed()
        proposal = self.env.claude_proposal(sid)
        source = co.store(sid) / 'proposal-source.json'
        source.write_text(json.dumps(proposal))
        self.assert_protected_source_refused_without_changes(sid, source)

    def test_temporary_codex_work_source_is_refused_without_retry_or_mutation(self):
        sid = self.proposed()
        proposal = self.env.claude_proposal(sid)
        source = co.work(sid) / 'proposal-source.json'
        source.write_text(json.dumps(proposal))
        self.assert_protected_source_refused_without_changes(sid, source)

    def test_first_hook_denial_leaves_workflow_stopped_and_proposal_unrevealed(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "sys.stderr.write('Command blocked by PreToolUse hook: mock denial\\n'); sys.stderr.flush()"})
        output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        self.assertEqual((output['state'], code), ('STOPPED', 2), output)
        self.assertEqual(self.env.session(sid)['stop']['category'], 'POLICY')
        before = tree_snapshot(self.env.root)
        ready, ready_code = self.env.run('investigation-ready', '--session', sid)
        self.assertEqual(ready_code, 2, ready)
        self.assertFalse(ready.get('ready', False))
        self.assertEqual(tree_snapshot(self.env.root), before)
        self.assertEqual(self.env.calls(), ['proposal'])
        # 禁止要求を注入して runner の拒否境界を検査する。利用者の再試行手順ではない。
        for command in (('call', '--session', sid, '--phase', 'proposal'),
                        ('call', '--session', sid, '--phase', 'debate'), ('reveal', '--session', sid)):
            with self.subTest(command=command[0], phase=command[-1]):
                refused, refused_code = self.env.run(*command)
                self.assertEqual((refused['status'], refused_code), ('REFUSED', 2), refused)
                self.assertEqual(self.env.calls(), ['proposal'])
                self.assertEqual(tree_snapshot(self.env.root), before)
        for name in ('claude/proposal.json', 'codex/proposal.json', 'reveal/diff.json'):
            self.assertFalse((co.art(sid) / name).exists())


class InvestigationRoutingTests(unittest.TestCase):
    def test_documents_separate_prepare_wait_check_and_independent_investigation(self):
        # 手順の脱落を検出する文書回帰テスト。実 Claude の遵守や品質同等性の証明ではない。
        skill = (SKILL / 'SKILL.md').read_text()
        debate = (SKILL / 'references/debate.md').read_text()
        entry = skill.split('**진입 직후 조사 순서:**', 1)[1].split('**사용자 확인 단계', 1)[0]
        self.assertIn('아래 단방향 공통 절차를 적용하지 않고', entry)
        self.assertIn('대상 코드·테스트·관련 diff/로그·구현 문서 본문 조사', entry)
        self.assertIn('`--review`·`--test-plan`·`--code-review`는 기존대로', entry)
        proposal = next(line for line in debate.splitlines() if line.startswith('| P1-a Codex |'))
        claude = next(line for line in debate.splitlines() if line.startswith('| P1-b Claude |'))
        main = next(line for line in debate.splitlines() if line.startswith('| P1-b 메인 |'))
        self.assertIn('background launch는 완료가 아니다', proposal)
        self.assertIn('종료 코드 0과 `ready:true`를 모두 확인한 뒤에만', proposal)
        self.assertIn('새 architect 서브에이전트 1개', claude)
        self.assertIn('메인의 사전 조사·초안·결론이나 Codex 제안/출력은 전달하지 않는다', claude)
        self.assertIn('원래의 코드 조사·계약 양쪽/근거 원문 확인', claude)
        self.assertIn('생성 시각은 실제 조사 시작 시점의 증명이 아니다', main)
        self.assertIn('Read/Bash 사전 차단', debate)

    def test_documents_assign_main_freeze_and_stop_on_first_protection_denial(self):
        # 役割と停止規則の記述漏れだけを確認し、実 Claude の遵守を証明しない。
        skill = (SKILL / 'SKILL.md').read_text()
        debate = (SKILL / 'references/debate.md').read_text()
        architect = next(line for line in debate.splitlines() if line.startswith('| P1-b Claude |'))
        main = next(line for line in debate.splitlines() if line.startswith('| P1-b 메인 |'))
        self.assertIn('읽기 전용 조사·설계 결과를 메시지로 반환', skill)
        self.assertIn('근거 등록·별도 초안 작성·동결은 메인이 맡고', skill)
        self.assertIn('**메시지로만 반환**', architect)
        self.assertIn('`evidence add`·파일 작성·`freeze`·삭제/정리·세션 상태 변경을 실행하지 않고', architect)
        self.assertIn('실제 발급 ID', main)
        self.assertIn('설계의 의미 있는 내용·조건·위험·대안·미확인을 보존', main)
        for required in ('<workspace>/.local/cross-check-drafts/<unique>/proposal.json',
                         '`ARTIFACT_ROOT` 전체', '`USER_TMP/co-design-*` 밖',
                         '최종 `artifacts/claude/proposal.json`은 runner만 생성',
                         '첫 훅·permission·보호정책 거부부터', '메인도 추가 호출·동결·정리를 실행하지 않는다',
                         '중단 규칙이 P5와 stale 회수 뒤의 `finalize` 안내보다 우선',
                         '`finalize`·새 보고서 파일 생성도 중단', '기존 기록·`status` 조회와 채팅 보고만',
                         '보호 거부가 아닌 다른 STOPPED의 기존 보고 절차',
                         '`rm`→`rm -f`', '자식이 거부당한 동작을 메인이 대신 실행',
                         '보호 거부가 아닌 로컬 초안 schema·줄 범위 입력 오류',
                         '거부 분류가 불명확하면 중단·보고', '실제 준수를 증명하지 않으며'):
            self.assertIn(required, debate)


if __name__ == '__main__':
    unittest.main()
