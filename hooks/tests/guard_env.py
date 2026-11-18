"""guard テスト共通の隔離環境。

一時ディレクトリに guard.py の複製と harness_config.json、ダミーの秘密ファイルを置いた仮 HOME を作る。
実際の HOME・認証情報には触れない（パス判定は存在確認だけで、ダミーファイルも中身は読まない）。
"""
import atexit
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile

SOURCE = Path(__file__).resolve().parents[1] / 'guard.py'
# 一時領域（TMPDIR）配下は削除承認の例外になるため、プロジェクト等は一時領域外の存在しないパスにする。
PROJECT = '/nonexistent-harness-test/project'
HARNESS_HOME = '/nonexistent-harness-test/ai-harness'
PROFILE = 'test-readonly'
ASSUME_ROLE_CMD = PROJECT + '/assume_role.sh'

_root = Path(tempfile.mkdtemp(prefix='guard-test-')).resolve()
atexit.register(shutil.rmtree, _root, True)
HOME = _root / 'home'
HOOKS = _root / 'hooks'
GUARD = HOOKS / 'guard.py'
CONFIG = HOOKS / 'harness_config.json'
for rel in ('.codex/auth.json', '.claude/.credentials.json', '.aws/config', '.aws/credentials',
            '.codex/config.toml'):
    (HOME / rel).parent.mkdir(parents=True, exist_ok=True)
    (HOME / rel).write_text('placeholder\n')
HOOKS.mkdir()
shutil.copyfile(SOURCE, GUARD)


def config(aws=True, **extra):
    data = {'version': 1, 'project_root': PROJECT, 'harness_home': HARNESS_HOME,
            'aws': ({'enabled': True, 'profile': PROFILE, 'assume_role_cmd': ASSUME_ROLE_CMD,
                     'assume_role_envs': ['DEV', 'DEV2']} if aws else {'enabled': False})}
    data.update(extra)
    return data


CONFIG.write_text(json.dumps(config()))
# guard は `~` を呼び出し時の HOME で展開するため、このテストプロセス全体を仮 HOME にする（子プロセスにも渡す）。
os.environ['HOME'] = str(HOME)
ENV = dict(os.environ)


def load(path=GUARD, name='guard'):
    """仮 HOME で guard を読み込む。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def isolated_guard(data):
    """別設定の guard を別ディレクトリに置く。data が None なら設定ファイルなし、str ならそのまま書く。"""
    directory = Path(tempfile.mkdtemp(prefix='hooks-', dir=_root))
    shutil.copyfile(SOURCE, directory / 'guard.py')
    if data is not None:
        (directory / 'harness_config.json').write_text(data if isinstance(data, str) else json.dumps(data))
    return directory / 'guard.py'
