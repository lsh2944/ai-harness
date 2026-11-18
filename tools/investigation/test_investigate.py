import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("investigate", Path(__file__).with_name("investigate.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root_patch = patch.object(app, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.write("sender.log", "ready\nERROR mqtt disconnect\nWARNING SRT timeout\nswitching relay\n")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def job(self, **extra):
        return app.run_job({"files": ["sender.log"], **extra})

    def cli(self, args):
        output = io.StringIO()
        with patch("sys.argv", ["investigate", *args]), contextlib.redirect_stdout(output):
            status = app.main()
        return status, json.loads(output.getvalue())

    def test_counts_locations_and_unknown_exit(self):
        result = self.job()
        self.assertTrue(result["ok"])
        self.assertEqual(result["command_status"], "unknown")
        evidence = result["files"][0]
        self.assertEqual(evidence["lines"], 4)
        self.assertEqual(evidence["groups"]["failure"]["matching_lines"], 2)
        self.assertEqual(evidence["groups"]["error"]["sample_lines"], [2])
        self.assertEqual(len(evidence["samples"]), 3)  # Same line across groups appears once.
        self.assertEqual(evidence["path"], str(self.root / "sender.log"))

    def test_samples_report_omission_without_changing_raw(self):
        raw = ("ERROR " + "x" * 400 + "\n") * 8
        path = self.write("sender.log", raw)
        evidence = self.job()["files"][0]
        group = evidence["groups"]["error"]
        self.assertEqual(group["matching_lines"], 8)
        self.assertEqual(group["omitted_matching_lines"], 5)
        self.assertTrue(evidence["samples"][0]["text_truncated"])
        self.assertEqual(path.read_text(), raw)

    def test_cache_hit_and_content_hash_even_if_mtime_restored(self):
        self.assertEqual(self.job()["cache"], "miss")
        self.assertEqual(self.job()["cache"], "hit")
        path = self.root / "sender.log"
        old = path.stat()
        path.write_text(path.read_text().replace("ERROR", "error"))
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(self.job()["cache"], "miss")

    def test_dependencies_change_delete_and_input_set(self):
        path = self.write("source.py", "first")
        job = {"dependencies": ["source.py"]}
        self.assertEqual(self.job(**job)["cache"], "miss")
        self.assertEqual(self.job(**job)["cache"], "hit")
        path.write_text("second")
        self.assertEqual(self.job(**job)["cache"], "miss")
        path.unlink()
        self.assertFalse(self.job(**job)["ok"])
        self.write("other.log", "ERROR extra")
        self.assertEqual(self.job(files=["sender.log", "other.log"])["cache"], "miss")

    def test_cache_corruption_rebuild_and_permissions(self):
        self.job()
        folder = self.root / app.CACHE_DIR
        entry = next(folder.glob("*.json"))
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)
        entry.write_text("broken")
        self.assertEqual(self.job()["cache"], "miss")

    def test_cached_status_cannot_override_current_exit(self):
        for exit_code, forged_code, expected in ((7, 0, "failure"), (0, 7, "success"), (None, 0, "unknown")):
            with self.subTest(exit_code=exit_code):
                first = self.job(exit_code=exit_code)
                entry_path = self.root / app.CACHE_DIR / (first["evidence_key"] + ".json")
                entry = json.loads(entry_path.read_text())
                self.assertEqual(set(entry["payload"]), {"files", "dependencies"})
                entry["payload"].update(reported_exit_code=forged_code,
                                        command_status="success" if forged_code == 0 else "failure")
                entry["digest"] = hashlib.sha256(app.encode(entry["payload"])).hexdigest()
                entry_path.write_text(json.dumps(entry))
                args = ["summary", "sender.log"]
                if exit_code is not None:
                    args.extend(["--exit-code", str(exit_code)])
                status, output = self.cli(args)
                result = output["jobs"][0]
                self.assertEqual(status, 1 if exit_code == 7 else 0)
                self.assertEqual(result["cache"], "miss")
                self.assertEqual(result["reported_exit_code"], exit_code)
                self.assertEqual(result["command_status"], expected)
                self.assertEqual(self.job(exit_code=exit_code)["cache"], "hit")

    def test_cache_symlink_refused_and_no_cache_available(self):
        (self.root / app.CACHE_DIR).symlink_to(self.root, target_is_directory=True)
        self.assertFalse(self.job()["ok"])
        result = app.run_job({"files": ["sender.log"]}, use_cache=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["cache"], "disabled")

    def test_protected_and_outside_inputs_refused(self):
        for name in ("../outside.log", "/etc/passwd", ".env", ".aws/credentials", ".claude/settings.json", "tools/harness/guard.py", "private.key", "secrets.txt"):
            with self.subTest(name=name):
                result = self.job(files=[name])
                self.assertFalse(result["ok"])

    def test_harness_source_refused_when_inside_project(self):
        # ハーネス本体がプロジェクト配下にあっても hook・インストーラ・設定は入力にしない
        harness = self.root / "ai-harness"
        self.write("ai-harness/hooks/guard.log", "x\n")
        self.write("ai-harness/docs/note.log", "x\n")
        with patch.object(app, "HARNESS_PROTECTED", (harness / "hooks", harness / "config.local.json")):
            self.assertFalse(self.job(files=["ai-harness/hooks/guard.log"])["ok"])
            self.assertTrue(self.job(files=["ai-harness/docs/note.log"])["ok"])

    def test_root_is_cwd_and_env_cannot_widen_it(self):
        import subprocess
        import sys
        script = Path(__file__).with_name("investigate.py")
        code = "import importlib.util,sys;s=importlib.util.spec_from_file_location('m',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.ROOT)"
        other = self.root / "other"
        other.mkdir()
        # 環境変数を付けても作業ルートは cwd のまま
        result = subprocess.run([sys.executable, "-c", code, str(script)], cwd=self.root,
                                env={**os.environ, "HARNESS_PROJECT_ROOT": str(other)}, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stdout.strip(), str(self.root))

    def test_too_broad_root_is_refused(self):
        import subprocess
        import sys
        home = Path.home().resolve()
        hidden = [home / name for name in (".aws", ".codex/sessions", ".claude/projects/x", ".agents/skills")] + [home / "project/.local/x"]
        for path in ["/", home, home.parent, "/tmp", "/private/var"] + hidden:
            with self.subTest(path=path):
                self.assertTrue(app.too_broad_root(path))
        self.assertFalse(app.too_broad_root(self.root))
        self.assertFalse(app.too_broad_root(home / "project"))
        # CLI は入力を読む前に拒否する（cwd=/）
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("investigate.py")), "summary", "x.log"],
                                cwd="/", capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("too broad", result.stdout)

    def test_symlink_file_and_parent_refused(self):
        (self.root / "alias.log").symlink_to(self.root / "sender.log")
        (self.root / "alias").symlink_to(self.root, target_is_directory=True)
        for name in ("alias.log", "alias/sender.log"):
            self.assertFalse(self.job(files=[name])["ok"])

    def test_binary_invalid_utf8_and_limits_fail(self):
        path = self.root / "bad.log"
        for content in (b"\xff", b"abc\x00"):
            path.write_bytes(content)
            self.assertFalse(self.job(files=["bad.log"])["ok"])
        with patch.object(app, "MAX_BYTES", 2):
            self.assertFalse(self.job()["ok"])
        self.assertFalse(self.job(files=["sender.log"] * 21)["ok"])

    def test_batch_retains_success_and_failure_and_order(self):
        self.write("jobs.json", json.dumps([
            {"name": "good", "files": ["sender.log"]},
            {"name": "missing", "files": ["missing.log"]},
            {"name": "tests", "files": ["sender.log"], "exit_code": 2},
        ]))
        status, output = self.cli(["batch", "jobs.json"])
        self.assertEqual(status, 1)
        self.assertEqual([r["name"] for r in output["jobs"]], ["good", "missing", "tests"])
        self.assertTrue(output["jobs"][0]["ok"])
        self.assertFalse(output["jobs"][1]["ok"])
        self.assertEqual(output["jobs"][2]["reported_exit_code"], 2)

    def test_cli_zero_not_test_success_and_reported_failure(self):
        status, output = self.cli(["--no-cache", "summary", "sender.log"])
        self.assertEqual(status, 0)
        self.assertEqual(output["jobs"][0]["command_status"], "unknown")
        self.assertEqual(self.cli(["summary", "sender.log", "--exit-code", "7"])[0], 1)

    def test_no_arbitrary_commands_or_invalid_status(self):
        self.assertFalse(app.run_job({"files": ["sender.log"], "command": "echo unsafe"})["ok"])
        for status in (-1, 256, "0", True):
            self.assertFalse(self.job(exit_code=status)["ok"])
        self.write("jobs.json", "[]")
        self.assertEqual(self.cli(["batch", "jobs.json"])[0], 1)

    def test_known_key_omits_unchanged_evidence_only(self):
        self.write("source.py", "original")
        first = self.job(dependencies=["source.py"], exit_code=3)
        repeat = self.job(dependencies=["source.py"], exit_code=3, known_key=first["evidence_key"])
        self.assertTrue(repeat["unchanged"])
        self.assertNotIn("files", repeat)
        self.assertEqual(repeat["reported_exit_code"], 3)
        self.write("source.py", "modified")
        changed = self.job(dependencies=["source.py"], exit_code=3, known_key=first["evidence_key"])
        self.assertIn("files", changed)
        self.assertNotEqual(changed["evidence_key"], first["evidence_key"])

    def test_read_error_retains_reported_exit_code(self):
        result = self.job(files=["missing.log"], exit_code=7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reported_exit_code"], 7)
        self.assertEqual(result["command_status"], "failure")

    def test_large_display_keeps_full_report_and_all_statuses(self):
        self.write("verbose.log", ("ERROR MQTT " + "x" * 300 + "\n") * 20)
        jobs = [{"name": str(i), "files": ["verbose.log"] * 20} for i in range(8)]
        self.write("jobs.json", json.dumps(jobs))
        status, output = self.cli(["--no-cache", "batch", "jobs.json"])
        self.assertEqual(status, 0)
        self.assertTrue(output["output_truncated"])
        self.assertLess(len(json.dumps(output, ensure_ascii=False, indent=2)), app.MAX_DISPLAY_CHARS)
        full = json.loads(Path(output["full_report"]).read_text())
        self.assertEqual(len(full["jobs"]), 8)
        self.assertEqual(len(full["jobs"][7]["files"]), 20)
        self.assertEqual(stat.S_IMODE(Path(output["full_report"]).stat().st_mode), 0o600)

    def test_total_job_byte_budget(self):
        with patch.object(app, "MAX_JOB_BYTES", 2):
            self.assertFalse(self.job()["ok"])

    def test_report_write_failure_retains_each_status_and_fails_cli(self):
        self.write("verbose.log", ("ERROR " + "x" * 300 + "\n") * 20)
        self.write("jobs.json", json.dumps([
            {"name": "readable", "files": ["verbose.log"] * 20, "exit_code": 9},
            {"name": "missing", "files": ["missing.log"], "exit_code": 7},
        ]))
        with patch.object(app, "write_json", side_effect=OSError("disk full")):
            status, output = self.cli(["--no-cache", "batch", "jobs.json"])
        self.assertEqual(status, 1)
        self.assertTrue(output["omitted_evidence_unavailable"])
        self.assertIsNone(output["full_report"])
        self.assertEqual(output["report_error"], "disk full")
        self.assertEqual([r["reported_exit_code"] for r in output["jobs"]], [9, 7])
        self.assertFalse(output["jobs"][1]["ok"])


if __name__ == "__main__":
    unittest.main()
