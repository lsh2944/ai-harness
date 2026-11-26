"""設計探索（目標アーキテクチャ・候補比較・現在との差・移行経路）欄の検査と報告 12 節。実 AI は呼ばない。"""
import copy
import tempfile
import unittest

import co_design_fixture as fx
from co_design_fixture import co


def explored(**changes):
    exploration = {'applicable': True, 'not_applicable_reason': '', 'target_architecture': 'ideal shape',
                   'candidates': [{'name': 'A', 'approach': 'extend current', 'tradeoffs': 'cheap', 'basis': ['E9']},
                                  {'name': 'B', 'approach': 'clean slate', 'tradeoffs': 'costly',
                                   'basis': ['https://example.org/paper']}],
                   'chosen': 'A', 'current_gap': ['gap 1'], 'migration_path': ['step 1'], 'compromises': ['kept X']}
    exploration.update(changes)
    return exploration


class ExplorationBase(unittest.TestCase):
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


class ProposalExplorationTests(ExplorationBase):
    def test_claude_applicable_exploration_is_accepted(self):
        sid = self.proposed()
        proposal = self.env.claude_proposal(sid)
        proposal['exploration'] = explored()
        output, code = self.freeze_claude(sid, proposal)
        self.assertEqual((output['status'], code), ('ok', 0), output)

    def test_claude_exploration_shape_errors_are_rejected(self):
        cases = [(explored(candidates=explored()['candidates'][:1]), 'at least two candidates'),
                 (explored(chosen='C'), 'chosen must name one of the candidates'),
                 (explored(candidates=[explored()['candidates'][0]] * 2), 'unique and non-empty'),
                 (explored(target_architecture=' '), 'target_architecture'),
                 (fx.no_exploration() | {'not_applicable_reason': ''}, 'not_applicable_reason')]
        for exploration, message in cases:
            with self.subTest(message=message):
                sid = self.proposed()
                proposal = self.env.claude_proposal(sid)
                proposal['exploration'] = exploration
                output, code = self.freeze_claude(sid, proposal)
                self.assertEqual(code, 2)
                self.assertIn(message, output['error'])

    def test_claude_proposal_without_exploration_is_rejected_by_schema(self):
        sid = self.proposed()
        proposal = self.env.claude_proposal(sid)
        del proposal['exploration']
        output, code = self.freeze_claude(sid, proposal)
        self.assertEqual(code, 2, output)

    def test_whitespace_is_normalized_before_name_checks(self):
        # 名前は前後の空白を除いて比較する（選択の空白差は通し、空白だけの名前と空白差の重複は拒否）。
        a, b = explored()['candidates']
        self.assertEqual(co.exploration_semantics(explored(chosen=' A '), 'codex'), [])
        self.assertIn('exploration candidate names must be unique and non-empty',
                      co.exploration_semantics(explored(candidates=[a | {'name': ' '}, b]), 'codex'))
        self.assertIn('exploration candidate names must be unique and non-empty',
                      co.exploration_semantics(explored(candidates=[a, b | {'name': 'A '}]), 'codex'))

    def test_codex_exploration_problems_are_kept_without_losing_the_proposal(self):
        sid = self.env.to_inputs_frozen()
        self.env.behaviors({'proposal': "result['exploration']['not_applicable_reason'] = ''"})
        output, code = self.env.run('call', '--session', sid, '--phase', 'proposal')
        # research と同じく、問題として残し INCOMPLETE だが提案は保持する。
        self.assertEqual((output['status'], output['execution'], output['state'], code), ('INCOMPLETE', 'ok', 'CODEX_PROPOSED', 2))
        self.assertEqual(output['problems'], ['exploration marked not applicable needs not_applicable_reason'])

    def test_prompts_carry_exploration_order(self):
        sid = self.env.to_inputs_frozen()
        prompt = self.env.ok('architect-prompt', '--session', sid)['architect_prompt']
        self.assertIn('proposal.exploration', prompt)
        self.assertIn('후보 2~3개', prompt)
        text = co.build_request(self.env.session(sid), 'proposal')
        self.assertIn('Design exploration:', text)
        self.assertIn('`chosen` must equal one candidate `name`', text)


class ExplorationReportTests(unittest.TestCase):
    def proposal(self, exploration=None):
        base = {'phase': 'proposal', 'side': 'claude', 'status': 'COMPLETE', 'evidence_sufficiency': 'partial',
                'summary': 's', 'requirement_mapping': [], 'design': {'components': [], 'flows': [], 'contracts': []},
                'alternatives': [], 'implementation_order': [], 'validation': [], 'risks': [], 'assumptions': [],
                'evidence': [], 'research': fx.no_research(), 'open_questions': [], 'unverified': []}
        if exploration is not None:
            base['exploration'] = exploration
        return base

    def test_section_lists_both_sides_and_marks_choice(self):
        text = '\n'.join(co._exploration_section(self.proposal(explored()), self.proposal(fx.no_exploration())))
        self.assertIn('## 12. 설계 탐색', text)
        self.assertIn('- 목표 아키텍처: ideal shape', text)
        self.assertIn('| A (선택) | extend current | cheap | E9 |', text)
        self.assertIn('| B | clean slate | costly | https://example.org/paper |', text)
        self.assertIn('- 해당 없음: plain fix', text)

    def test_old_session_without_exploration_is_reported_not_broken(self):
        old = self.proposal()
        self.assertIsNotNone(co._report_proposal(old))  # 旧凍結提案も比較表に使える
        legacy = copy.deepcopy(old)
        del legacy['research']
        self.assertIsNotNone(co._report_proposal(legacy))
        text = '\n'.join(co._exploration_section(old, {'invalid': True}))
        self.assertIn('이 세션의 제안에 exploration 필드가 없음', text)

    def test_not_applicable_with_submitted_candidates_is_accepted_and_shown(self):
        # 「該当なし」なのに候補を出した矛盾は拒否せず、報告 12 節に注記と内容を出す。
        mixed = explored(applicable=False, not_applicable_reason='plain fix')
        self.assertEqual(co.exploration_semantics(mixed, 'codex'), [])
        text = '\n'.join(co._exploration_section(self.proposal(mixed), None))
        self.assertIn('- 해당 없음: plain fix', text)
        self.assertIn('해당 없음이지만 탐색 내용이 제출됨(후보 2개)', text)
        self.assertIn('| B | clean slate | costly |', text)
        empty = '\n'.join(co._exploration_section(self.proposal(fx.no_exploration()), None))
        self.assertNotIn('탐색 내용이 제출됨', empty)
        self.assertNotIn('- 목표 아키텍처:', empty)

    def test_table_cells_escape_pipes_and_newlines(self):
        a, b = explored()['candidates']
        text = '\n'.join(co._exploration_section(
            self.proposal(explored(candidates=[a | {'approach': 'x | y\nz'}, b])), None))
        row = next(line for line in text.splitlines() if line.startswith('| A (선택)'))
        self.assertEqual(row.count(' | '), 3)  # 4 列のまま（区切りが増えない）
        self.assertNotIn('\nz', row)

    def test_invalid_exploration_shape_is_reported_as_invalid(self):
        text = '\n'.join(co._exploration_section(self.proposal({'applicable': 'yes'}), None))
        self.assertIn('exploration 형식이 유효하지 않음', text)


if __name__ == '__main__':
    unittest.main()
