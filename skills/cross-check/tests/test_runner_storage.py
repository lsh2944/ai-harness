"""runner の保存先と候補検証を偽 Codex・隔離一時領域で検査する。実 AI は呼ばない。"""
import copy
import json
import os
from pathlib import Path
import shlex
import stat
import tempfile
import unittest

import co_design_fixture as fx

co = fx.co


class RunnerStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='runner-storage-test-')
        self.env = fx.Env(self.temp.name)
        self.counter = 0

    def tearDown(self):
        self.env.close()
        self.temp.cleanup()

    def snapshot(self):
        result = {}
        for path in self.env.root.rglob('*'):
            info = path.lstat()
            value = os.readlink(path) if stat.S_ISLNK(info.st_mode) else path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            result[str(path.relative_to(self.env.root))] = (info.st_mode, info.st_mtime_ns, value)
        return result

    def draft(self, document, name='draft.json'):
        self.counter += 1
        path = self.env.project / '.local/cross-check-drafts' / f'candidate {self.counter}' / name
        path.parent.mkdir(parents=True)
        path.write_text(document if isinstance(document, str) else json.dumps(document, ensure_ascii=False, indent=2) + '\n')
        return path

    def validate(self, sid, kind, source, stage=None, *extra):
        args = [kind, 'validate', '--session', sid, '--file', source]
        if stage:
            args += ['--stage', stage]
        return self.env.run(*args, *extra)

    def submit(self, sid, kind, document, stage=None, *extra):
        source = self.draft(document)
        original, calls = source.read_bytes(), self.env.calls()
        output, code = self.validate(sid, kind, source, stage, *extra)
        self.assertEqual(code, 0, output)
        target = co.art(sid) / (kind + '.json')
        self.assertEqual(output['path'], str(target))
        self.assertEqual(output['sha256'], co.sha256(target.read_bytes()))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.env.calls(), calls)
        return output, source

    def rejected(self, sid, kind, source, stage=None, *extra):
        before, calls = self.snapshot(), self.env.calls()
        output, code = self.validate(sid, kind, source, stage, *extra)
        self.assertEqual(code, 2, output)
        self.assertIn(output['status'], ('REFUSED', 'INVALID', 'ERROR'), output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.env.calls(), calls)
        return output

    def debated(self):
        sid = self.env.to_revealed()
        self.submit(sid, 'ledger', fx.ledger([fx.issue('I1')]), 'pre-debate')
        self.env.ok('call', '--session', sid, '--phase', 'debate')
        return sid

    def final_ledger(self):
        sid = self.debated()
        self.submit(sid, 'ledger', fx.final_ledger(), 'final')
        return sid

    def test_pre_debate_storage_is_canonical_independent_of_cwd_and_preserves_bytes(self):
        sid = self.env.to_revealed()
        document = fx.ledger([fx.issue('I1')])
        document['issues'][0]['title'] = '条件 A を維持\n根拠 B と代案 C を比較'
        source = self.draft(document)
        original = source.read_bytes()
        previous = Path.cwd()
        try:
            os.chdir(self.env.tmp)
            output, code = self.validate(sid, 'ledger', source, 'pre-debate')
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, output)
        canonical = co.art(sid) / 'ledger.json'
        frozen = co.art(sid) / 'ledger.pre-debate.json'
        self.assertEqual(output['path'], str(canonical))
        self.assertEqual(output['sha256'], co.sha256(original))
        self.assertEqual(canonical.read_bytes(), original)
        self.assertEqual(frozen.read_bytes(), original)
        self.assertEqual(frozen.stat().st_mode & 0o777, 0o400)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((co.session_dir(sid) / 'ledger.json').exists())
        self.assertFalse((self.env.tmp / 'ledger.json').exists())
        self.assertEqual(self.env.session(sid)['frozen']['ledger-pre-debate']['sha256'], output['sha256'])

    def test_mutable_final_and_synthesis_updates_preserve_meaning_and_bind_saved_bytes(self):
        sid = self.final_ledger()
        frozen = (co.art(sid) / 'ledger.pre-debate.json').read_bytes()
        document = fx.final_ledger()
        document['issues'][0]['claude']['position'] = '条件 A のときだけ採用\n代案 B は未確認'
        self.submit(sid, 'ledger', document, 'final')
        self.assertEqual(co.read_json_artifact(sid, 'ledger.json'), document)
        synthesis = fx.synthesis(conditions=['条件 A', '根拠 B を照合'])
        synthesis['decisions'][0]['reason'] = '原文の条件を保存\n残余リスクは未確認'
        output, source = self.submit(sid, 'synthesis', synthesis)
        self.assertEqual(co.read_artifact(sid, 'synthesis.json'), source.read_bytes())
        self.assertEqual(co.read_json_artifact(sid, 'synthesis.json'), synthesis)
        conditional = self.env.session(sid)['conditional']
        self.assertEqual(conditional['synthesis_sha256'], output['sha256'])
        self.assertEqual(conditional['ledger_sha256'], co.sha256(co.read_artifact(sid, 'ledger.json')))
        synthesis['decisions'][0]['residual_risk'] = 'updated risk'
        self.submit(sid, 'synthesis', synthesis)
        self.assertEqual((co.art(sid) / 'ledger.pre-debate.json').read_bytes(), frozen)
        self.assertEqual(self.env.session(sid)['state'], 'SYNTHESIZED')

    def test_normalization_is_explicit_audited_and_does_not_rewrite_source_or_prose(self):
        sid = self.env.to_revealed()
        document = fx.ledger([fx.issue('I1')])
        document['issues'][0]['claude']['evidence_ids'] = ['brief.md:2']
        document['issues'][0]['title'] = 'free prose brief.md:2'
        output, source = self.submit(sid, 'ledger', document, 'pre-debate', '--normalize-refs')
        saved = co.read_json_artifact(sid, 'ledger.json')
        self.assertEqual(saved['issues'][0]['claude']['evidence_ids'], ['brief:L2'])
        self.assertEqual(saved['issues'][0]['title'], document['issues'][0]['title'])
        audit = json.loads(Path(output['format_revision']).read_bytes())
        self.assertEqual(audit['original_utf8'].encode(), source.read_bytes())
        self.assertEqual(co.read_artifact(sid, 'ledger.json'), co.read_artifact(sid, 'ledger.pre-debate.json'))

    def test_invalid_ledger_json_schema_semantics_and_refs_leave_all_artifacts_unchanged(self):
        sid = self.env.to_revealed()
        candidates = ['{', {'issues': []}, fx.ledger([fx.issue('I1'), fx.issue('I1')])]
        invalid_ref = fx.ledger([fx.issue('I1')])
        invalid_ref['issues'][0]['claude']['evidence_ids'] = ['brief.md:999']
        candidates.append(invalid_ref)
        for candidate in candidates:
            with self.subTest(candidate=repr(candidate)[:60]):
                result = self.rejected(sid, 'ledger', self.draft(candidate), 'pre-debate', '--normalize-refs')
                self.assertEqual(result['status'], 'INVALID')

    def test_invalid_synthesis_and_fake_checked_do_not_replace_existing_canonical_or_audit(self):
        sid = self.final_ledger()
        self.submit(sid, 'synthesis', fx.synthesis())
        invalid_semantic = fx.synthesis()
        invalid_semantic['decisions'][0]['reason'] = ''
        invalid_issue = fx.synthesis()
        invalid_issue['decisions'][0]['issue_id'] = 'I999'
        for candidate in ('{', {}, invalid_semantic, invalid_issue, fx.synthesis(checked=True)):
            with self.subTest(candidate=repr(candidate)[:60]):
                result = self.rejected(sid, 'synthesis', self.draft(candidate))
                self.assertEqual(result['status'], 'INVALID')

    def test_fidelity_uses_candidate_content_and_derived_flags_bind_issue_content(self):
        sid = self.final_ledger()
        self.submit(sid, 'synthesis', fx.synthesis())
        self.env.ok('call', '--session', sid, '--phase', 'fidelity')
        candidate = fx.synthesis(checked=True)
        candidate['decisions'][1]['codex_checked'] = True
        output, _ = self.submit(sid, 'synthesis', candidate, None, '--derive-checked')
        self.assertEqual(output['derived_checked'], ['I1'])
        self.assertEqual([d['codex_checked'] for d in co.read_json_artifact(sid, 'synthesis.json')['decisions']], [True, False])
        # 関連のない C1 の変更では I1 の確認は残る（争点単位の結び付け）。
        candidate['decisions'][1]['reason'] = 'changed after fidelity'
        output, _ = self.submit(sid, 'synthesis', candidate, None, '--derive-checked')
        self.assertEqual(output['derived_checked'], ['I1'])
        # I1 自体を変えれば外れる。
        candidate['decisions'][0]['reason'] = 'I1 changed after fidelity'
        output, source = self.submit(sid, 'synthesis', candidate, None, '--derive-checked')
        self.assertEqual(output['derived_checked'], [])
        saved = co.read_json_artifact(sid, 'synthesis.json')
        self.assertFalse(any(d['codex_checked'] for d in saved['decisions']))
        self.assertTrue(json.loads(source.read_bytes())['decisions'][0]['codex_checked'])
        self.assertEqual(saved['decisions'][1]['reason'], 'changed after fidelity')
        self.assertEqual(self.env.session(sid)['conditional']['synthesis_sha256'], output['sha256'])
        self.env.ok('finalize', '--session', sid)

    def test_final_ledger_change_requires_synthesis_revalidation_before_finalize(self):
        sid = self.final_ledger()
        self.submit(sid, 'synthesis', fx.synthesis(adopt_i1='codex'))
        updated = fx.final_ledger()
        updated['issues'][0]['title'] = 'changed ledger content'
        self.submit(sid, 'ledger', updated, 'final')
        before = self.snapshot()
        self.assertEqual(self.env.run('finalize', '--session', sid)[0]['status'], 'REFUSED')
        self.assertEqual(self.snapshot(), before)
        self.submit(sid, 'synthesis', fx.synthesis(adopt_i1='codex'))
        self.env.ok('finalize', '--session', sid)

    def test_existing_immutable_ledger_refuses_before_normalization_or_any_write(self):
        sid = self.env.to_revealed()
        self.submit(sid, 'ledger', fx.ledger([fx.issue('I1')]), 'pre-debate')
        candidate = fx.ledger([fx.issue('I1')])
        candidate['issues'][0]['claude']['evidence_ids'] = ['brief.md:2']
        output = self.rejected(sid, 'ledger', self.draft(candidate), 'pre-debate', '--normalize-refs')
        self.assertIn('already frozen', output['error'])

    def test_dangling_immutable_ledger_target_is_not_overwritten(self):
        sid = self.env.to_revealed()
        (co.art(sid) / 'ledger.pre-debate.json').symlink_to('missing.json')
        self.rejected(sid, 'ledger', self.draft(fx.ledger([fx.issue('I1')])), 'pre-debate')

    def test_symlink_canonical_target_is_rejected_before_audit_or_external_write(self):
        sid = self.env.to_revealed()
        untouched = self.draft('external sentinel')
        (co.art(sid) / 'ledger.json').symlink_to(untouched)
        candidate = fx.ledger([fx.issue('I1')])
        candidate['issues'][0]['claude']['evidence_ids'] = ['brief.md:2']
        self.rejected(sid, 'ledger', self.draft(candidate), 'pre-debate', '--normalize-refs')

    def test_nonregular_canonical_target_is_rejected_without_blocking_or_mutation(self):
        sid = self.env.to_revealed()
        os.mkfifo(co.art(sid) / 'ledger.json')
        self.rejected(sid, 'ledger', self.draft(fx.ledger([fx.issue('I1')])), 'pre-debate')

    def test_synthesis_nonregular_target_is_rejected_before_derived_audit(self):
        sid = self.final_ledger()
        (co.art(sid) / 'synthesis.json').mkdir()
        self.rejected(sid, 'synthesis', self.draft(fx.synthesis(checked=True)), None, '--derive-checked')

    def test_audit_directory_conflict_refuses_before_canonical_or_frozen_write(self):
        sid = self.env.to_revealed()
        (co.art(sid) / 'format-revisions').write_text('audit sentinel')
        candidate = fx.ledger([fx.issue('I1')])
        candidate['issues'][0]['claude']['evidence_ids'] = ['brief.md:2']
        self.rejected(sid, 'ledger', self.draft(candidate), 'pre-debate', '--normalize-refs')

    def test_other_project_draft_is_refused_without_reading_it(self):
        sid = self.env.to_revealed()
        self.rejected(sid, 'ledger', self.env.file('foreign.json', fx.ledger([fx.issue('I1')])), 'pre-debate')

    def test_other_session_artifact_source_is_refused(self):
        sid = self.env.to_revealed()
        other = self.env.to_revealed()
        self.env.write_artifact(other, 'ledger.json', fx.ledger([fx.issue('I1')]))
        self.rejected(sid, 'ledger', co.art(other) / 'ledger.json', 'pre-debate')

    def test_symlink_source_is_refused(self):
        sid = self.env.to_revealed()
        source = self.draft(fx.ledger([fx.issue('I1')]))
        link = source.with_name('link.json')
        link.symlink_to(source)
        self.rejected(sid, 'ledger', link, 'pre-debate')

    def test_protected_source_inside_project_is_refused(self):
        sid = self.env.to_revealed()
        source = self.env.project / 'hooks/draft.json'
        source.parent.mkdir()
        source.write_text(json.dumps(fx.ledger([fx.issue('I1')])))
        self.rejected(sid, 'ledger', source, 'pre-debate')

    def test_codex_temporary_source_inside_project_is_still_refused(self):
        sid = self.env.to_revealed()
        # USER_TMP が PROJECT 内でも既存の co-design-* 保護を通過させない。
        previous = co.USER_TMP
        try:
            co.USER_TMP = self.env.project / 'usertmp'
            source = co.USER_TMP / 'co-design-isolated/draft.json'
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps(fx.ledger([fx.issue('I1')])))
            self.rejected(sid, 'ledger', source, 'pre-debate')
        finally:
            co.USER_TMP = previous

    def test_unknown_session_and_wrong_state_do_not_create_or_modify_storage(self):
        source = self.draft(fx.ledger([fx.issue('I1')]))
        self.rejected('20261117T000000Z-aaaaaa', 'ledger', source, 'pre-debate')
        sid = self.env.to_inputs_frozen()
        self.rejected(sid, 'ledger', source, 'pre-debate')

    def test_relative_source_is_refused(self):
        sid = self.env.to_revealed()
        self.rejected(sid, 'ledger', Path('relative.json'), 'pre-debate')

    def test_no_file_legacy_validation_remains_compatible(self):
        sid = self.env.to_revealed()
        self.env.write_artifact(sid, 'ledger.json', fx.ledger([fx.issue('I1')]))
        pre = self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
        self.assertEqual(pre['path'], str(co.art(sid) / 'ledger.json'))
        self.env.ok('call', '--session', sid, '--phase', 'debate')
        self.env.write_artifact(sid, 'ledger.json', fx.final_ledger())
        self.env.ok('ledger', 'validate', '--session', sid, '--stage', 'final')
        self.env.write_artifact(sid, 'synthesis.json', fx.synthesis(adopt_i1='codex'))
        output = self.env.ok('synthesis', 'validate', '--session', sid)
        self.assertEqual(output['sha256'], co.sha256(co.read_artifact(sid, 'synthesis.json')))
        self.env.ok('finalize', '--session', sid)

    def test_scaffold_external_next_quotes_exact_path_and_default_does_not_submit_protected_draft(self):
        sid = self.env.to_revealed()
        directory = self.env.project / '.local/cross-check-drafts/space ; $() test'
        directory.mkdir(parents=True)
        target = directory / 'ledger draft.json'
        output = self.env.ok('ledger', 'scaffold', '--session', sid, '--stage', 'pre-debate', '--out', target)
        command = output['next'].split('then submit: ', 1)[1].split('. The runner', 1)[0]
        argv = shlex.split(command)
        self.assertEqual(argv[argv.index('--file') + 1], str(target))
        self.assertNotIn('copy to artifacts/', output['next'])
        default = self.env.ok('ledger', 'scaffold', '--session', sid, '--stage', 'pre-debate')
        self.assertIn('cannot be submitted with --file', default['next'])
        self.assertNotIn(default['draft'], default['next'])
        self.assertIn(str(self.env.project / '.local/cross-check-drafts'), default['next'])


if __name__ == '__main__':
    unittest.main()
