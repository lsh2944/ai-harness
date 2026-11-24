"""外部調査（最新動向・一般的な実装・論文）欄、Claude 側 web 根拠、リンク確認、報告 11 節の検査。実 AI は呼ばない。"""
import json
import tempfile
import time
import unittest

import co_design_fixture as fx
from co_design_fixture import co

CODEX_RESEARCH = ("result['research'] = {'question_date': __import__('time').strftime('%Y-%m-%d'), 'web_used': True, "
                  "'trends': [{'summary': 'trend A', 'url': 'https://example.org/trend'}], "
                  "'common_practices': [{'summary': 'practice B', 'url': 'https://example.org/shared'}], "
                  "'papers': [{'title': 'Paper C', 'url': 'https://arxiv.org/abs/1234.5678', 'year': '2025', "
                  "'relevance': 'same problem'}], 'not_applicable_reason': ''}")


def web_research(**changes):
    research = {'question_date': time.strftime('%Y-%m-%d'), 'web_used': True,
                'trends': [], 'common_practices': [{'summary': 'practice B (claude)', 'url': 'https://example.org/shared'}],
                'papers': [], 'not_applicable_reason': ''}
    research.update(changes)
    return research


class ResearchBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='co-design-test-')
        self.env = fx.Env(self.temp.name)

    def tearDown(self):
        self.env.close()
        self.temp.cleanup()

    def proposed(self, codex_behavior=None):
        sid = self.env.to_inputs_frozen()
        if codex_behavior:
            self.env.behaviors({'proposal': codex_behavior})
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        return sid

    def freeze_claude(self, sid, proposal):
        return self.env.run('freeze', '--session', sid, '--side', 'claude', '--file', self.env.file('proposal.json', proposal))

    def check_file(self, document):
        path = self.env.project / '.local/drafts' / f'check-{time.time_ns()}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
        return path


class ProposalResearchTests(ResearchBase):
    def test_claude_unregistered_web_evidence_with_url_is_accepted(self):
        sid = self.proposed()
        proposal = self.env.claude_proposal(sid)
        proposal['evidence'].append({'id': 'E8', 'kind': 'web', 'ref': 'https://example.org/page', 'observation': 'opened'})
        proposal['research'] = web_research()
        output, code = self.freeze_claude(sid, proposal)
        self.assertEqual((output['status'], code), ('ok', 0), output)

    def test_outside_project_inference_paths_are_not_one_sided_files(self):
        # 推論根拠が作業ルート外の標準ライブラリを指しても、公開差分の片側ファイル一覧に入れない。
        sid = self.proposed()
        proposal = self.env.claude_proposal(sid)
        proposal['evidence'].append({'id': 'E8', 'kind': 'inference', 'ref': '/usr/lib/python3/tempfile.py:L1-L2',
                                     'observation': 'stdlib reading'})
        self.assertEqual(self.freeze_claude(sid, proposal)[1], 0)
        self.env.ok('reveal', '--session', sid)
        diff = json.loads((self.env.artifacts / sid / 'artifacts/reveal/diff.json').read_text())
        self.assertNotIn('/usr/lib/python3/tempfile.py', diff['one_sided']['claude_only'])
        self.assertEqual(diff['one_sided']['codex_only'], ['src/other.py'])

    def test_claude_web_evidence_without_single_url_is_rejected(self):
        sid = self.proposed()
        proposal = self.env.claude_proposal(sid)
        proposal['evidence'].append({'id': 'E8', 'kind': 'web', 'ref': 'see some page', 'observation': 'opened'})
        output, code = self.freeze_claude(sid, proposal)
        self.assertEqual(code, 2)
        self.assertIn('single http(s) URL', output['error'])

    def test_claude_command_evidence_still_requires_registration(self):
        sid = self.proposed()
        proposal = self.env.claude_proposal(sid)
        proposal['evidence'].append({'id': 'E8', 'kind': 'command', 'ref': 'ls', 'observation': 'listed'})
        output, code = self.freeze_claude(sid, proposal)
        self.assertEqual(code, 2)
        self.assertIn('must be registered', output['error'])

    def test_claude_research_shape_errors_are_rejected(self):
        cases = [(web_research(papers=[{'title': 't', 'url': 'not a url', 'year': '', 'relevance': 'r'}]), 'research url'),
                 (fx.no_research() | {'not_applicable_reason': ''}, 'not_applicable_reason'),
                 (web_research(web_used=False, not_applicable_reason='x'), 'web_used is false')]
        for research, message in cases:
            with self.subTest(message=message):
                sid = self.proposed()
                proposal = self.env.claude_proposal(sid)
                proposal['research'] = research
                output, code = self.freeze_claude(sid, proposal)
                self.assertEqual(code, 2)
                self.assertIn(message, output['error'])

    def test_codex_research_problems_are_kept_without_losing_the_proposal(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "result['research']['not_applicable_reason'] = ''; result['research']['question_date'] = '1999-01-01'"})
        output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        # 既存の入力行参照不良と同じく、問題として残し INCOMPLETE だが提案は失わない。
        self.assertEqual((output['status'], output['execution'], output['state'], code), ('INCOMPLETE', 'ok', 'CODEX_PROPOSED', 2))
        self.assertEqual(output['problems'], ['research without web findings needs not_applicable_reason'])

    def test_codex_question_date_difference_alone_is_not_a_problem(self):
        sid = self.proposed("result['research']['question_date'] = '1999-01-01'")
        self.assertEqual(self.env.session(sid)['calls'][0]['status'], 'COMPLETE')

    def test_prompts_carry_question_date_and_research_instruction(self):
        sid = self.env.to_inputs_frozen()
        today = time.strftime('%Y-%m-%d')
        prompt = self.env.ok('architect-prompt', '--session', sid)['architect_prompt']
        self.assertIn(f'"question_date": "{today}"', prompt)
        self.assertIn('최신 동향', prompt)
        self.assertIn('실제로 연 페이지의 URL만', prompt)
        text = co.build_request(self.env.session(sid), 'proposal')
        self.assertIn(f'Question date (research baseline, set research.question_date to this): {today}', text)
        self.assertIn('External research:', text)


class ResearchCheckTests(ResearchBase):
    def revealed(self):
        sid = self.proposed(CODEX_RESEARCH)
        proposal = self.env.claude_proposal(sid)
        proposal['research'] = web_research()
        self.assertEqual(self.freeze_claude(sid, proposal)[1], 0)
        self.env.ok('reveal', '--session', sid)
        return sid

    def entry(self, url, status='verified', title='Title', note=''):
        return {'url': url, 'status': status, 'observed_title': title, 'note': note}

    def test_valid_check_is_saved_and_reported_in_section_11(self):
        sid = self.revealed()
        output = self.env.ok('research', 'check', '--session', sid, '--file', self.check_file({'checks': [
            self.entry('https://example.org/shared'), self.entry('https://arxiv.org/abs/1234.5678', 'mismatch', 'Other paper', 'title differs')]}))
        self.assertEqual(output['counts']['verified'], 1)
        self.assertEqual(output['unchecked_urls'], ['https://example.org/trend'])
        claude = json.loads((self.env.artifacts / sid / 'artifacts/claude/proposal.json').read_text())
        codex = json.loads((self.env.artifacts / sid / 'artifacts/codex/proposal.json').read_text())
        text = '\n'.join(co._research_section(sid, claude, codex))
        self.assertIn('### 최신 동향', text)
        self.assertIn('| Codex: trend A | https://example.org/trend | Codex | 미확인 |', text)
        self.assertIn('https://example.org/shared | 양측 | 확인됨 (Title) |', text)
        self.assertIn('Paper C (2025, same problem)', text)
        self.assertIn('내용 불일치 (Other paper) — title differs', text)

    def test_section_11_without_check_or_research_field(self):
        sid = self.revealed()
        codex = json.loads((self.env.artifacts / sid / 'artifacts/codex/proposal.json').read_text())
        old = dict(codex)
        old.pop('research')
        text = '\n'.join(co._research_section(sid, old, codex))
        self.assertIn('Claude: 조사 기록 없음(이 세션의 제안에 research 필드가 없음)', text)
        self.assertIn('링크 확인 기록 없음', text)
        broken = dict(codex, research={'web_used': 'yes'})
        self.assertIn('Claude: 조사 기록 없음(research 형식이 유효하지 않음)',
                      '\n'.join(co._research_section(sid, broken, codex)))

    def test_old_proposal_without_research_keeps_initial_comparison_table(self):
        # research 追加前に凍結した提案でも、報告 1 節の比較表が「유효하지 않음」にならない。
        sid = self.revealed()
        codex = json.loads((self.env.artifacts / sid / 'artifacts/codex/proposal.json').read_text())
        old = dict(codex)
        old.pop('research')
        table = '\n'.join(co._initial_proposal_table(old, codex))
        self.assertNotIn('유효하지 않음', table)
        self.assertIsNone(co._report_proposal(dict(old, summary=1)))

    def test_web_evidence_only_url_is_listed_with_its_check(self):
        sid = self.proposed(CODEX_RESEARCH + "; result['evidence'].append({'id': 'X9', 'kind': 'web', "
                            "'ref': 'https://example.org/evidence-only', 'observation': 'spec page'})")
        proposal = self.env.claude_proposal(sid)
        proposal['research'] = web_research()
        self.assertEqual(self.freeze_claude(sid, proposal)[1], 0)
        self.env.ok('reveal', '--session', sid)
        self.env.ok('research', 'check', '--session', sid, '--file', self.check_file({'checks': [
            self.entry('https://example.org/evidence-only', title='Spec')]}))
        claude = json.loads((self.env.artifacts / sid / 'artifacts/claude/proposal.json').read_text())
        codex = json.loads((self.env.artifacts / sid / 'artifacts/codex/proposal.json').read_text())
        text = '\n'.join(co._research_section(sid, claude, codex))
        self.assertIn('### 근거로만 인용된 웹 페이지', text)
        self.assertIn('| Codex X9: spec page | https://example.org/evidence-only | Codex | 확인됨 (Spec) |', text)

    def test_malformed_check_record_does_not_break_the_report(self):
        sid = self.revealed()
        claude = json.loads((self.env.artifacts / sid / 'artifacts/claude/proposal.json').read_text())
        codex = json.loads((self.env.artifacts / sid / 'artifacts/codex/proposal.json').read_text())
        for document in ([1, 2], {'checks': [{'url': 'https://example.org/trend', 'status': 'verified', 'observed_title': 3, 'note': ''}]}):
            with self.subTest(document=document):
                self.env.write_artifact(sid, 'research_check.json', document)
                text = '\n'.join(co._research_section(sid, claude, codex))
                self.assertIn('| Codex: trend A | https://example.org/trend | Codex | 미확인 |', text)

    def test_invalid_checks_are_rejected_without_saving(self):
        sid = self.revealed()
        cases = [({'checks': [self.entry('https://unknown.example/x')]}, 'not a research'),
                 ({'checks': [self.entry('https://example.org/trend'), self.entry('https://example.org/trend')]}, 'duplicated'),
                 ({'checks': [self.entry('https://example.org/trend', 'ok')]}, 'status must be'),
                 ({'checks': [self.entry('https://example.org/trend', 'verified', '')]}, 'observed_title'),
                 ({'checks': []}, 'at least one'),
                 ({'checks': [self.entry('https://example.org/trend')], 'extra': 1}, 'at least one')]
        for document, message in cases:
            with self.subTest(message=message):
                output, code = self.env.run('research', 'check', '--session', sid, '--file', self.check_file(document))
                self.assertEqual(code, 2)
                self.assertIn(message, output['error'])
        self.assertFalse((self.env.artifacts / sid / 'artifacts/research_check.json').exists())

    def test_check_before_reveal_is_refused(self):
        sid = self.proposed()
        output, code = self.env.run('research', 'check', '--session', sid, '--file',
                                    self.check_file({'checks': [self.entry('https://example.org/trend')]}))
        self.assertEqual(code, 2)
        self.assertEqual(output['status'], 'REFUSED')


if __name__ == '__main__':
    unittest.main()
