"""行範囲の読み取り専用算出と既存の開始条件を、実 AI なしで検査する。"""
from contextlib import ExitStack, redirect_stderr
import copy
import io
from unittest.mock import patch

from test_partial_submission import SubmissionBase, co


class EvidenceInspectTests(SubmissionBase):
    def proposed(self):
        sid = self.env.to_inputs_frozen()
        self.env.ok('call', '--session', sid, '--phase', 'proposal')
        return sid

    def inspect(self, sid, source, *bounds, expected=True):
        before, calls = self.snapshot(), self.env.calls()
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(co, name, side_effect=AssertionError(name)))
                     for name in ('find_codex', 'run_call', 'save_session', 'write_private',
                                  'write_frozen', 'reap_stale_calls', 'next_evidence_id')]
            mocks += [stack.enter_context(patch.object(co.subprocess, 'run', side_effect=AssertionError('subprocess'))),
                      stack.enter_context(patch.object(co.os, 'killpg', side_effect=AssertionError('killpg')))]
            result, code = self.env.run('evidence', 'inspect', '--session', sid, '--file', source, *bounds)
            for mocked in mocks:
                mocked.assert_not_called()
        self.assertEqual(code, 0 if expected else 2, result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.env.calls(), calls)
        if not expected:
            self.assertNotIn('numbered_source', result)
            self.assertNotIn('file_sha256', result)
        return result

    def test_whole_file_and_explicit_range_use_one_snapshot_without_registration(self):
        sid = self.proposed()
        source = self.env.project / 'src/app.py'
        with patch.object(co, 'read_source', wraps=co.read_source) as reads:
            result = self.inspect(sid, source, '--whole-file')
        self.assertEqual(sum(str(c.args[0]) == str(source) for c in reads.call_args_list), 1)
        self.assertEqual(result['line_count'], 4)
        self.assertEqual(result['lines'], [1, 4])
        self.assertEqual(result['ref'], 'src/app.py:L1-L4')
        self.assertEqual(result['file_sha256'], co.sha256(source.read_bytes()))
        self.assertEqual(result['sha256'], result['file_sha256'])
        self.assertEqual(result['numbered_source'], '1\tline1\n2\tline2\n3\tline3\n4\tline4\n')
        partial = self.inspect(sid, source, '--lines', '2-3')
        self.assertEqual(partial['lines'], [2, 3])
        self.assertEqual(partial['ref'], 'src/app.py:L2-L3')
        self.assertEqual(partial['sha256'], co.sha256(b'line2\nline3\n'))
        self.assertEqual(partial['file_sha256'], result['file_sha256'])
        self.assertEqual(self.env.session(sid)['evidence'], [])

    def test_invalid_bounds_are_not_clamped(self):
        sid = self.proposed()
        for bounds in ('1-5', '0-1', '3-2', '1', '-1-2', '1-1x'):
            with self.subTest(bounds=bounds):
                self.inspect(sid, self.env.project / 'src/app.py', '--lines=' + bounds, expected=False)

    def test_newline_forms_unicode_empty_file_and_single_line(self):
        sid = self.proposed()
        source = self.env.project / 'src/app.py'
        for data, count in ((b'a\nb\nc\nd\n', 4), (b'a\nb\nc\nd', 4),
                            (b'a\r\nb\r\nc\r\nd\r\n', 4), (b'\n', 1),
                            ('한글\n日本語'.encode(), 2), (b'', 0)):
            with self.subTest(data=data):
                source.write_bytes(data)
                result = self.inspect(sid, source, '--whole-file')
                self.assertEqual(result['line_count'], count)
                self.assertEqual(result['lines'], [1, count] if count else None)
                self.assertEqual(result['ref'], 'src/app.py:L1' + ('-L' + str(count) if count > 1 else '') if count else None)
                self.assertEqual(result['file_sha256'], co.sha256(data))
                self.assertEqual(result['bytes'], len(data))
                expected = ''.join(str(i) + '\t' + line.decode() for i, line in enumerate(data.splitlines(keepends=True), 1))
                self.assertEqual(result['numbered_source'], expected)
        self.inspect(sid, source, '--lines', '1-1', expected=False)

    def test_no_validated_range_is_inferred_without_explicit_selector(self):
        for bounds in ([], ['--lines', '1-2', '--whole-file']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                co.parser().parse_args(['evidence', 'inspect', '--session', 's', '--file', '/sample', *bounds])

    def test_before_proposal_running_stopped_and_later_states_do_not_read_source(self):
        source = self.env.project / 'src/app.py'
        sid = self.env.to_inputs_frozen()
        self.inspect(sid, source, '--whole-file', expected=False)
        sid = self.proposed()
        original = copy.deepcopy(self.env.session(sid))
        for state, stop, running in (('CODEX_PROPOSED', None, True), ('STOPPED', {'category': 'POLICY'}, False),
                                     ('CLAUDE_FROZEN', None, False)):
            calls = copy.deepcopy(original['calls'])
            if running:
                calls[0].update(execution='running', ended_at=None)
            self.env.edit_session(sid, state=state, stop=stop, calls=calls)
            with patch.object(co, 'read_source', wraps=co.read_source) as reads:
                self.inspect(sid, source, '--whole-file', expected=False)
            self.assertNotIn(str(source), [str(c.args[0]) for c in reads.call_args_list])

    def test_frozen_input_and_private_result_tampering_block_source_read(self):
        for kind in ('input', 'result', 'links'):
            sid = self.proposed()
            call = self.env.session(sid)['calls'][0]
            path = co.art(sid) / 'inputs/brief.md' if kind == 'input' else co.call_file(sid, call, kind + '.json')
            path.chmod(0o600)
            path.write_text('changed')
            source = self.env.project / 'src/app.py'
            with patch.object(co, 'read_source', wraps=co.read_source) as reads:
                self.inspect(sid, source, '--whole-file', expected=False)
            self.assertNotIn(str(source), [str(c.args[0]) for c in reads.call_args_list])

    def test_concurrent_lock_refuses_without_mutation(self):
        sid = self.proposed()
        with co.session_lock(sid):
            self.inspect(sid, self.env.project / 'src/app.py', '--whole-file', expected=False)

    def test_source_protections_and_project_boundary_are_preserved(self):
        sid = self.proposed()
        protected = self.env.project / 'hooks/sample.py'
        protected.parent.mkdir()
        protected.write_text('mock protected data')
        settings = self.env.project / 'config.toml'
        settings.write_text('mock settings')
        outside = self.env.root / 'outside.py'
        outside.write_text('mock outside data')
        link = self.env.project / 'src/link.py'
        link.symlink_to(self.env.project / 'src/app.py')
        for source in (protected, settings, outside, link, 'src/app.py', co.art(sid) / 'inputs/brief.md'):
            with self.subTest(source=str(source)):
                self.inspect(sid, source, '--whole-file', expected=False)

    def test_existing_size_caps_and_invalid_utf8_are_not_silently_truncated(self):
        sid = self.proposed()
        source = self.env.project / 'src/app.py'
        for data in (b'x' * (co.MAX_SOURCE + 1), b'x' * (co.MAX_DOC + 1), b'\xff\n'):
            source.write_bytes(data)
            self.inspect(sid, source, '--whole-file', expected=False)

    def test_snapshot_metadata_remains_consistent_if_file_changes_after_read(self):
        sid = self.proposed()
        source = self.env.project / 'src/app.py'
        original = source.read_bytes()
        read_source = co.read_source

        def changing_read(value, *args, **kwargs):
            result = read_source(value, *args, **kwargs)
            if str(value) == str(source):
                source.write_bytes(b'changed\n')
            return result

        with patch.object(co, 'read_source', side_effect=changing_read):
            result = self.env.ok('evidence', 'inspect', '--session', sid, '--file', source, '--whole-file')
        self.assertEqual(result['file_sha256'], co.sha256(original))
        self.assertEqual(result['lines'], [1, 4])
        self.assertEqual(result['numbered_source'], '1\tline1\n2\tline2\n3\tline3\n4\tline4\n')
