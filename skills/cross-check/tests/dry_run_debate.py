#!/usr/bin/env python3
"""A案・B案の全流れと停止シナリオを偽 codex と一時ディレクトリで実行する dry run（実 Codex・AWS は使わない）。"""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import co_design_fixture as fx  # noqa: E402

ARTIFACTS = ('inputs/brief.md', 'inputs/scope.json', 'inputs/evidence_index.md', 'claude/proposal.json',
             'codex/proposal.json', 'reveal/diff.json', 'ledger.json', 'synthesis.json', 'synthesis.md', 'debate.md',
             'session.json', 'session_summary.json', 'report.md')


def scenario(name, body):
    with tempfile.TemporaryDirectory(prefix='co-design-dry-run-') as temp:
        env = fx.Env(temp)
        try:
            return body(env)
        finally:
            env.close()


def full(env, plan):
    if plan == 'A':
        sid, validated, final = fx.run_plan_a(env, fidelity=True)
        extra = ('codex/debate.json', 'codex/fidelity.json', 'ledger.pre-debate.json')
    else:
        sid, validated, final = fx.run_plan_b(env, round2=True)
        extra = ('codex/critique.json', 'claude/critique.json', 'codex/rebuttal.json', 'codex/round2.json')
    root = env.artifacts / sid / 'artifacts'
    missing = [a for a in ARTIFACTS + extra if not (root / a).is_file()]
    logs = sorted(p.name for p in (env.tmp / f'co-design-{sid}/logs/calls').iterdir())
    session = env.session(sid)
    summary = json.loads((root / 'session_summary.json').read_text())
    required = fx.co.PLANS[plan]['required']
    phases = env.calls()
    return {'plan': plan, 'state': session['state'], 'codex_budget_seconds': session['codex_budget_seconds'],
            'max_calls': session['max_calls'], 'calls': phases,
            'base_calls': sum(1 for p in phases if p in required),
            'conditional_calls': sum(1 for p in phases if p in fx.co.CONDITIONAL),
            'conditional_reason': validated['reasons'][:1], 'logs': logs, 'missing_artifacts': missing,
            'violations': summary['violations'], 'over_budget': summary['over_budget'],
            'call_summaries': len(final['call_summaries'])}


def rejected(env, *argv):
    output, code = env.run(*argv)
    return code == 2 and output['status'] == 'INVALID'


def scaffolded(env):
    """L1 初稿 → そのままでは validate 拒否 → 埋めると通過、を A 案の流れで確かめ、report.md まで作る。"""
    sid = env.to_revealed()
    root = env.artifacts / sid / 'artifacts'
    steps = {}
    draft = json.loads(Path(env.ok('ledger', 'scaffold', '--session', sid, '--stage', 'pre-debate')['draft']).read_text())
    env.write_artifact(sid, 'ledger.json', draft)
    steps['pre-debate draft rejected'] = rejected(env, 'ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
    draft['issues'][0].update(title='Retry missing', claude={'position': 'Add retry', 'stance': 'proposed', 'evidence_ids': []})
    draft['issues'][0]['turns'][0]['text'] = 'Codex plan lacks retry'
    draft['issues'][0]['importance'] = 'high'
    env.write_artifact(sid, 'ledger.json', draft)
    env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
    env.ok('call', '--session', sid, '--phase', 'debate')
    draft = json.loads(Path(env.ok('ledger', 'scaffold', '--session', sid, '--stage', 'final')['draft']).read_text())
    codex = json.loads((root / 'codex/debate.json').read_text())
    steps['codex text verbatim'] = (
        {i['id']: i['title'] for i in draft['issues']}.get('C1') == codex['critique_of_claude'][0]['claim'] and
        {i['id']: i['codex']['position'] for i in draft['issues']}.get('I1') == codex['responses_to_claude_critique'][0]['argument'])
    env.write_artifact(sid, 'ledger.json', draft)
    steps['final draft rejected'] = rejected(env, 'ledger', 'validate', '--session', sid, '--stage', 'final')
    env.write_artifact(sid, 'ledger.json', fx.fill_final_ledger(draft))
    env.ok('ledger', 'validate', '--session', sid, '--stage', 'final')
    draft = json.loads(Path(env.ok('synthesis', 'scaffold', '--session', sid)['draft']).read_text())
    env.write_artifact(sid, 'synthesis.json', draft)
    steps['synthesis draft rejected'] = rejected(env, 'synthesis', 'validate', '--session', sid)
    env.write_artifact(sid, 'synthesis.json', fx.fill_synthesis(draft))
    validated = env.ok('synthesis', 'validate', '--session', sid)
    final = env.ok('finalize', '--session', sid)
    report = Path(final['report']).read_text()
    headings = [line.split('.')[0] for line in report.splitlines() if line.startswith('## ')]
    return {'steps': steps, 'next': validated['next'], 'state': env.session(sid)['state'],
            'report_sections': len(headings), 'report_in_order': headings == [f'## {n}' for n in range(1, 12)],
            'drafts': sorted(p.name for p in root.glob('*.draft.json'))}


def stopped(env):
    sid = env.to_inputs_frozen()
    pid_file = env.root / 'child.pid'
    env.behaviors({'proposal': fx.hang_with_child(pid_file) +
                   "tool('x1', 'cat ~/.' + 'claude/projects/other/session.jsonl', '')\ntime.sleep(30)\n"})
    output, code = env.run('call', '--session', sid, '--phase', 'proposal')
    refused, _ = env.run('call', '--session', sid, '--phase', 'proposal')
    final = env.ok('finalize', '--session', sid)
    summary = json.loads(Path(final['session_summary']).read_text())
    return {'state': env.session(sid)['state'], 'exit_code': code, 'stop': output.get('stop'),
            'child_killed': fx.process_gone(int(pid_file.read_text())), 'next_call': refused['status'],
            'calls': env.calls(), 'session_summary_state': summary['final_state'], 'violations': summary['violations']}


def main():
    a = scenario('A', lambda env: full(env, 'A'))
    b = scenario('B', lambda env: full(env, 'B'))
    s = scenario('STOPPED', stopped)
    d = scenario('SCAFFOLD', scaffolded)
    print(json.dumps({'plan_A': a, 'plan_B': b, 'stopped': s, 'scaffold': d}, ensure_ascii=False, indent=1))
    checks = {
        'A: 2 base + 1 conditional': (a['base_calls'], a['conditional_calls'], a['calls']) == (2, 1, ['proposal', 'debate', 'fidelity']),
        'B: 3 base + 1 conditional': (b['base_calls'], b['conditional_calls'], b['calls']) == (3, 1, ['proposal', 'critique', 'rebuttal', 'round2']),
        'codex budgets 1800/2400': (a['codex_budget_seconds'], b['codex_budget_seconds']) == (1800, 2400),
        'caps 3/4': (a['max_calls'], b['max_calls']) == (3, 4),
        'all artifacts': not a['missing_artifacts'] and not b['missing_artifacts'],
        'finalized without violations': (a['state'], b['state'], a['violations'], b['violations']) == ('FINALIZED', 'FINALIZED', 0, 0),
        'STOPPED scenario': (s['state'], s['stop']['category'], s['child_killed'], s['next_call'], s['calls'])
                            == ('STOPPED', 'INDEPENDENCE', True, 'REFUSED', ['proposal']),
        'scaffold drafts rejected until filled, Codex text verbatim': all(d['steps'].values()) and d['drafts'] == [
            'ledger.final.draft.json', 'ledger.pre-debate.draft.json', 'synthesis.draft.json'],
        'report.md generated (11 sections, A/B/scaffold)': d['report_in_order'] and d['state'] == 'FINALIZED'
                                                          and not a['missing_artifacts'] and not b['missing_artifacts'],
    }
    for name, ok in checks.items():
        print(('PASS ' if ok else 'FAIL ') + name)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
