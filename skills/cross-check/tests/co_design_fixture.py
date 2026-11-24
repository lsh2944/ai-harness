"""co_design のテスト・dry run 共通の器具。実際の Codex・外部 AI・AWS は呼ばない。"""
import importlib.util
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/co_design.py'
spec = importlib.util.spec_from_file_location('co_design', SCRIPT)
co = importlib.util.module_from_spec(spec)
spec.loader.exec_module(co)

VERSION = 'codex-cli 0.0.0-fake'


def no_research():
    """web を使わない提案の外部調査欄（基準日はセッション開始と同じ当日）。"""
    return {'question_date': time.strftime('%Y-%m-%d'), 'web_used': False, 'trends': [], 'common_practices': [],
            'papers': [], 'not_applicable_reason': 'scope has no web'}

# 偽 codex。--version と exec だけを模倣し、argv・phase を記録する。
FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, re, signal, subprocess, sys, time
HERE = pathlib.Path(__file__)
args = sys.argv[1:]
if args == ['--version']:
    print(pathlib.Path(str(HERE) + '.version').read_text().strip())
    sys.exit(0)
with open(str(HERE) + '.argv.jsonl', 'a') as log:
    log.write(json.dumps(args) + '\n')
prompt = sys.stdin.read()
phase = re.search(r'^Phase: (\w+)$', prompt, re.M).group(1)
with open(str(HERE) + '.calls', 'a') as log:
    log.write(phase + '\n')
output = pathlib.Path(args[args.index('-o') + 1])
root = pathlib.Path(args[args.index('-C') + 1])

def section(title):
    match = re.search(r'BEGIN ' + re.escape(title) + r'\n(.*?)\nEND ' + re.escape(title) + r'\n', prompt, re.S)
    return match.group(1) if match else None

def emit(event):
    print(json.dumps(event), flush=True)

def tool(item_id, command, out, status='completed', exit_code=0):
    item = {'id': item_id, 'type': 'command_execution', 'command': command, 'aggregated_output': None,
            'exit_code': None, 'status': 'in_progress'}
    emit({'type': 'item.started', 'item': item})
    emit({'type': 'item.completed', 'item': dict(item, aggregated_output=out, exit_code=exit_code, status=status)})

def mcp(item_id, server, tool_name, arguments, result=None, error=None):
    item = {'id': item_id, 'type': 'mcp_tool_call', 'server': server, 'tool': tool_name, 'arguments': arguments,
            'result': None, 'error': None, 'status': 'in_progress'}
    emit({'type': 'item.started', 'item': item})
    emit({'type': 'item.completed', 'item': dict(item, result=result, error=error, status='completed')})

def listed():
    text = section('LISTED ISSUES') or ''
    return [x.strip() for x in text.split(',') if x.strip() and not x.strip().startswith('(')]

emit({'type': 'thread.started', 'thread_id': 'fake'})
emit({'type': 'turn.started'})
result = None
if phase == 'proposal':
    tool('t1', "/bin/zsh -lc 'sed -n 1,3p src/app.py'", ''.join((root / 'src/app.py').read_text().splitlines(keepends=True)[:3]))
    result = {'phase': 'proposal', 'side': 'codex', 'status': 'COMPLETE', 'evidence_sufficiency': 'sufficient',
              'summary': 'Codex design.', 'requirement_mapping': [{'requirement': 'R1', 'approach': 'A1'}],
              'design': {'components': ['c'], 'flows': ['f'], 'contracts': ['k']},
              'alternatives': [{'option': 'o', 'reason_rejected': 'r'}], 'implementation_order': ['step'],
              'validation': [{'check': 'unit', 'layer': 'local'}],
              'risks': [{'severity': 'medium', 'risk': 'r', 'mitigation': 'm'}], 'assumptions': [],
              'evidence': [{'id': 'X1', 'kind': 'file', 'ref': 'src/app.py:L1-L3', 'observation': 'read'},
                           {'id': 'X2', 'kind': 'file', 'ref': 'src/other.py:L1-L1', 'observation': 'never read'},
                           {'id': 'X3', 'kind': 'brief', 'ref': 'brief:L1', 'observation': 'question'}],
              'research': {'question_date': time.strftime('%Y-%m-%d'), 'web_used': False, 'trends': [],
                           'common_practices': [], 'papers': [], 'not_applicable_reason': 'scope has no web'},
              'open_questions': [], 'unverified': []}
elif phase == 'verify':
    # 版ゲートの自動点検: プローブ命令は実行し、秘密経路の命令は hook 拒否行だけを stderr に出す（実測形）。
    tool('v1', "/bin/zsh -lc 'echo cross-check-probe-ok'", 'cross-check-probe-ok\n')
    deny = True
elif phase == 'fidelity':
    result = {'status': 'COMPLETE', 'unverified': [],
              'issues': [{'issue_id': i, 'verdict': 'faithful', 'detail': 'ok', 'evidence_refs': ['synthesis:L1']}
                         for i in listed()]}
else:
    ids = []
    if phase == 'debate':
        ids = [i['id'] for i in json.loads(section('LEDGER EXCERPT'))
               if i['origin'] in ('claude', 'reveal-diff') and i['importance'] in ('high', 'medium')]
    elif phase == 'rebuttal':
        ids = [i['id'] for i in json.loads(section('CLAUDE CRITIQUE (frozen)'))['issues'] if i['importance'] in ('high', 'medium')]
    elif phase == 'round2':
        ids = listed()
    critique = [] if phase in ('rebuttal', 'round2') else [
        {'issue_id': 'C1', 'type': 'design-tradeoff', 'importance': 'high', 'claim': 'Claude misses X',
         'evidence_refs': ['src/app.py:L1-L3'], 'alternative': 'Do Y', 'would_change_if': 'Z is shown'}]
    responses = [] if phase == 'critique' else [
        {'issue_id': i, 'stance': 'reject', 'argument': 'because', 'evidence_refs': ['X1'], 'would_change_if': 'evidence'}
        for i in ids]
    result = {'status': 'COMPLETE', 'critique_of_claude': critique, 'responses_to_claude_critique': responses,
              'useful_from_other': [{'point': 'p', 'adopt_condition': 'c'}], 'useful_none_reason': '',
              'revised_position_summary': 's', 'unverified': []}
behaviors = pathlib.Path(str(HERE) + '.behaviors.json')
if behaviors.exists():
    exec(json.loads(behaviors.read_text()).get(phase, ''))
if phase == 'verify':
    if deny:
        # 秘密経路の文字列は実行時に組み立てる（本ファイルに経路形を残さない）
        sys.stderr.write('2026-10-26T00:00:00Z ERROR codex_core::tools::router: error=Command blocked by PreToolUse hook: x. '
                         "Command: echo '~/.aws/" + "credentials'\n")
    output.write_text('shell')
if result is not None:
    output.write_text(json.dumps(result))
emit({'type': 'item.completed', 'item': {'id': 'm1', 'type': 'agent_message', 'text': 'done'}})
emit({'type': 'turn.completed', 'usage': {'input_tokens': 50, 'cached_input_tokens': 10, 'output_tokens': 5}})
'''

# 子プロセスを残して長時間待つ振る舞い（プロセスグループ終了の確認用）。
def hang_with_child(pid_file):
    return (f"child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n")


def subprocess_popen(command):
    import subprocess
    # 古い running 呼び出しを模すため、ランナーと別のプロセスグループで待機させる。
    return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def kill_group(process):
    import signal
    if process.stdin:
        process.stdin.close()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait(timeout=5)


def process_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    import subprocess
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return not state or state.startswith('Z')


class Env:
    """一時ディレクトリにプロジェクト・ユーザー一時領域・偽 codex を用意し、モジュール定数を差し替える。"""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.project = self.root / 'project'
        (self.project / 'src').mkdir(parents=True)
        (self.project / 'src/app.py').write_text('line1\nline2\nline3\nline4\n')
        self.tmp = self.root / 'usertmp'
        self.tmp.mkdir()
        self.artifacts = self.project / '.local/co-design'
        self.artifacts.mkdir(parents=True)
        (self.artifacts / 'verified_codex_version').write_text(VERSION + '\n')
        self.fake = self.root / 'fake-codex'
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        Path(str(self.fake) + '.version').write_text(VERSION)
        self.counter = 0
        self.patches = [patch.object(co, 'PROJECT', self.project), patch.object(co, 'ARTIFACT_ROOT', self.artifacts),
                        patch.object(co, 'USER_TMP', self.tmp), patch.object(co, 'CODEX', self.fake)]
        for p in self.patches:
            p.start()

    def close(self):
        for p in reversed(self.patches):
            p.stop()
        # 0400/0500 の固定ファイルを一時ディレクトリごと消せるよう権限を戻す（作業用一時領域のみ）。
        for path, dirs, _ in os.walk(self.root):
            for name in dirs:
                os.chmod(os.path.join(path, name), 0o700)

    # ------------------------------------------------ 補助
    def run(self, *argv):
        return co.run([str(a) for a in argv])

    def ok(self, *argv):
        output, code = self.run(*argv)
        assert code == 0, (argv[:2], output)
        return output

    def behaviors(self, mapping):
        Path(str(self.fake) + '.behaviors.json').write_text(json.dumps(mapping))

    def calls(self):
        path = Path(str(self.fake) + '.calls')
        return path.read_text().split() if path.exists() else []

    def argv(self):
        path = Path(str(self.fake) + '.argv.jsonl')
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def file(self, name, content):
        # freeze の birthtime 検査に合わせ、毎回新しいファイルを作る。
        self.counter += 1
        path = self.root / 'claude' / f'{self.counter}-{name}'
        path.parent.mkdir(exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        return path

    def session(self, sid):
        return json.loads((self.artifacts / sid / 'artifacts/session.json').read_text())

    def edit_session(self, sid, **changes):
        session = self.session(sid)
        session.update(changes)
        (self.artifacts / sid / 'artifacts/session.json').write_text(json.dumps(session))

    def write_artifact(self, sid, name, document):
        path = self.artifacts / sid / 'artifacts' / name
        if path.exists():
            path.unlink()
        path.write_text(json.dumps(document))

    def inputs(self, scope=None):
        brief = self.file('brief.md', 'Question: how should X work?\nRequirement R1: keep Y.\n')
        scope = self.file('scope.json', scope or {'question': 'How should X work?', 'allowed_paths': ['src'],
                                                  'excluded_paths': [], 'allowed_tools': ['shell', 'context7'],
                                                  'external_transfer': 'public-docs-queries-only', 'notes': []})
        index = self.file('index.md', 'src/app.py: entry point\n')
        return brief, scope, index

    def init(self, plan_b=False, compact=False):
        argv = ['init', '--question-id', 'q-test'] + (['--frozen-critique'] if plan_b else []) + (['--compact'] if compact else [])
        return self.ok(*argv)['session']

    def to_inputs_frozen(self, plan_b=False, compact=False):
        sid = self.init(plan_b, compact)
        brief, scope, index = self.inputs()
        self.ok('freeze-inputs', '--session', sid, '--brief', brief, '--scope', scope, '--index', index)
        return sid

    def claude_proposal(self, sid):
        source = self.project / 'src/app.py'
        evidence = self.ok('evidence', 'add', '--session', sid, '--file', source, '--lines', '2-3')['id']
        return {'phase': 'proposal', 'side': 'claude', 'status': 'COMPLETE', 'evidence_sufficiency': 'partial',
                'summary': 'Claude design.', 'requirement_mapping': [{'requirement': 'R1', 'approach': 'B1'}],
                'design': {'components': ['c2'], 'flows': ['f2'], 'contracts': ['k2']},
                'alternatives': [], 'implementation_order': ['s'], 'validation': [{'check': 'unit', 'layer': 'mock'}],
                'risks': [], 'assumptions': [], 'open_questions': [], 'unverified': ['runtime'],
                'research': no_research(),
                'evidence': [{'id': evidence, 'kind': 'file', 'ref': 'src/app.py:L2-L3', 'observation': 'seen'},
                             {'id': 'E9', 'kind': 'inference', 'ref': 'reasoning', 'observation': 'inferred'}]}

    def to_revealed(self, plan_b=False, compact=False):
        sid = self.to_inputs_frozen(plan_b, compact)
        self.ok('call', '--session', sid, '--phase', 'proposal')
        proposal = self.file('proposal.json', self.claude_proposal(sid))
        self.ok('freeze', '--session', sid, '--side', 'claude', '--file', proposal)
        self.ok('reveal', '--session', sid)
        return sid


def issue(ident, origin='claude', importance='high', kind='design-tradeoff', claude='proposed', codex='pending',
          method='pending', status='open-unverified', result='unverified', evidence=(), turns=None):
    return {'id': ident, 'origin': origin, 'type': kind, 'importance': importance, 'title': 'Issue ' + ident,
            'requirement_ref': 'brief:L2',
            'claude': {'position': 'Claude view', 'stance': claude, 'evidence_ids': []},
            'codex': {'position': 'Codex view', 'stance': codex, 'evidence_ids': []},
            'turns': turns if turns is not None else [{'phase': 'P3', 'speaker': 'claude', 'kind': 'critique', 'text': 'critique',
                                                       'evidence_ids': [], 'would_change_if': 'new facts'}],
            'decision_method': method,
            'verification': {'method': 'read source' if result != 'unverified' else '', 'result': result,
                             'evidence_ids': list(evidence)},
            'status': status}


def ledger(issues):
    return {'issues': issues, 'states': {'execution': 'in-progress', 'evidence_sufficiency': 'partial', 'agreement': 'pending'}}


ANSWER = {'phase': 'P3', 'speaker': 'claude', 'kind': 'answer', 'text': 'answered', 'evidence_ids': [], 'would_change_if': 'x'}


def final_ledger(i1_codex='reject', extra=()):
    """debate 後の最終 ledger: I1(Claude 提起) と C1(Codex 批評) に双方の立場がある。"""
    return ledger([issue('I1', codex=i1_codex, claude='proposed', method='undecidable', status='conditional'),
                   issue('C1', origin='codex', claude='reject', codex='proposed', method='undecidable', status='open-user',
                         turns=[ANSWER]), *extra])


def synthesis(adopt_i1='claude', checked=False, conditions=(), decisions_extra=()):
    return {'decisions': [{'issue_id': 'I1', 'decision': 'Use B1', 'adopted_from': adopt_i1, 'reason': 'r',
                           'rejected_alternatives': ['A1'], 'conditions': list(conditions), 'residual_risk': 'low',
                           'codex_checked': checked},
                          {'issue_id': 'C1', 'decision': 'Do Y', 'adopted_from': 'codex', 'reason': 'r',
                           'rejected_alternatives': [], 'conditions': [], 'residual_risk': 'none', 'codex_checked': False},
                          *decisions_extra],
            'required': ['R1'], 'optional': [], 'user_decisions': [], 'unresolved': [],
            'useful_from_codex': ['C1 alternative'], 'useful_none_reason': '',
            'metrics': {'one_side_confirmed_findings': {'claude': 0, 'codex': 1}, 'missing_required': 0, 'unbacked_claims': 1}}


def run_plan_a(env, fidelity=True, compact=False):
    """A 案の全流れ。fidelity=True なら条件付き fidelity を 1 回実行する。"""
    sid = env.to_revealed(compact=compact)
    env.write_artifact(sid, 'ledger.json', ledger([issue('I1'), issue('I2', importance='low')]))
    env.ok('ledger', 'validate', '--session', sid, '--stage', 'pre-debate')
    env.ok('call', '--session', sid, '--phase', 'debate')
    env.write_artifact(sid, 'ledger.json', final_ledger(extra=[issue('I2', importance='low', codex='reject')]))
    # Codex が reject した I1 で Claude 案を採ると fidelity 理由になる。Codex 案を採れば条件付き呼び出しは不要。
    env.write_artifact(sid, 'synthesis.json', synthesis(adopt_i1='claude' if fidelity else 'codex'))
    validated = env.ok('synthesis', 'validate', '--session', sid)
    if fidelity:
        env.ok('call', '--session', sid, '--phase', 'fidelity')
        env.write_artifact(sid, 'synthesis.json', synthesis(checked=True))
        env.ok('synthesis', 'validate', '--session', sid)
    final = env.ok('finalize', '--session', sid)
    return sid, validated, final


def run_plan_b(env, round2=True):
    """B 案（--frozen-critique）の全流れ。round2=True ならユーザー要求で round2 を 1 回実行する。"""
    sid = env.to_revealed(plan_b=True)
    env.ok('call', '--session', sid, '--phase', 'critique')
    critique = env.file('critique.json', ledger([issue('I1')]))
    env.ok('freeze', '--session', sid, '--side', 'claude-critique', '--file', critique)
    env.ok('call', '--session', sid, '--phase', 'rebuttal')
    env.write_artifact(sid, 'ledger.json', final_ledger())
    env.write_artifact(sid, 'synthesis.json', synthesis())
    argv = ['synthesis', 'validate', '--session', sid] + (['--user-requested'] if round2 else [])
    validated = env.ok(*argv)
    if round2:
        env.ok('call', '--session', sid, '--phase', 'round2')
    final = env.ok('finalize', '--session', sid)
    return sid, validated, final


def wait_for(path, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if Path(path).exists() and Path(path).read_text().strip():
            return True
        time.sleep(0.05)
    return False


def fill_final_ledger(draft):
    """scaffold の最終 ledger 初稿に Claude 側の欄を埋める（dry run・テスト用）。"""
    for item in draft['issues']:
        item['claude']['stance'] = 'reject' if item['origin'] == 'codex' else 'proposed'
        item['claude']['position'] = item['claude']['position'] or 'Claude final view'
        for turn in item['turns']:
            if turn['speaker'] == 'claude' and not turn['text']:
                turn['text'] = 'Claude answer'
        if item['importance'] in ('high', 'medium'):
            item['decision_method'] = 'undecidable'
            item['codex']['stance'] = item['codex']['stance'] if item['codex']['stance'] != 'pending' else 'open'
    return draft


def fill_synthesis(draft, adopted='codex'):
    for decision in draft['decisions']:
        decision.update(decision='Decide ' + decision['issue_id'], adopted_from=adopted, reason='because')
    draft['useful_from_codex'] = ['C1 alternative']
    draft['metrics'] = {'one_side_confirmed_findings': {'claude': 0, 'codex': 0}, 'missing_required': 0, 'unbacked_claims': 0}
    return draft
