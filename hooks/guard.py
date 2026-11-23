#!/usr/bin/env python3
"""Claude/Codex PreToolUse ガード。補助防御であり、シェルの完全な解析器ではない。

Claude 経由の操作だけを制限する（ユーザー自身のターミナル操作には影響しない）。
  1. ファイル削除 → ユーザー承認（一時領域・検証済み Python キャッシュは除外）
  2. AWS → 設定した read-only プロファイル以外の認証情報を禁止（aws.enabled=false ならプロファイル固定のみ無効）
  3. AWS → 参照系以外の操作（作成・変更・削除・publish・deploy）を禁止（aws.enabled に関係なく常に有効）
     例外: bedrock-runtime の推論呼び出し 4 種（BEDROCK_INFER_OPS）は tools/aws/bedrock_run.py（Bedrock API キー）経由のみ許可
  4. ガード設定・認証情報ファイル自体の改変 → ユーザー承認、認証情報の読み取り → 禁止

設定は同じディレクトリの harness_config.json（install.py が生成）から読む。
欠落・破損・不正値の場合は全操作を拒否する（fail-closed）。

Windows では PowerShell 道具も判定し（check_powershell）、パスは判定用の正規形
（区切り /・小文字・Git Bash の /c/... はドライブ形式）に揃えて POSIX と同じ判定を使う。
"""
import json
import sys


def _crash_deny(*_args):
    """捕捉されない例外（読み込み時を含む）は拒否として出力する。
    Claude は exit 1 を非ブロッキングのエラーとして扱い未判定で実行するため、異常終了させない。"""
    try:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            # 出力の文字コードが未設定（cp932 等）でも失敗しないよう ASCII でエスケープする
            "permissionDecisionReason": "가드 검사에 실패했습니다. 보호 규칙을 확인하세요."}}))
        sys.stdout.flush()
    finally:
        import os as _os
        _os._exit(0)


if __name__ == "__main__":  # テスト等で読み込んだときは呼び出し元の例外処理を変えない
    sys.excepthook = _crash_deny

import glob  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import signal  # noqa: E402
import stat  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

WINDOWS = os.name == "nt"

if WINDOWS:
    import ntpath

    _MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(?=/|$)")

    def _win_input(value):
        """Git Bash 形式（/c/...）のドライブを Windows 形式（c:/...）にする。"""
        return _MSYS_DRIVE_RE.sub(lambda m: m.group(1) + ":", str(value).replace("\\", "/"))

    def _win_canon(value):
        """判定用の正規形: 区切りは /、大文字小文字は区別しない（小文字）。"""
        return _win_input(value).replace("\\", "/").lower()

    class _WinPath:
        """os.path の代わりに使う Windows 用の薄いラッパー。結果はすべて正規形にする。
        POSIX では P = os.path そのもので、従来の動作を変えない。"""
        sep = "/"

        def __getattr__(self, name):
            return getattr(ntpath, name)

        def normpath(self, p):
            return _win_canon(ntpath.normpath(_win_input(p)))

        def abspath(self, p):
            return _win_canon(ntpath.abspath(_win_input(p)))

        def realpath(self, p):
            return _win_canon(ntpath.realpath(_win_input(p)))

        def join(self, *parts):
            return _win_canon(ntpath.join(*(_win_input(p) for p in parts)))

        def expanduser(self, p):
            return _win_canon(ntpath.expanduser(_win_input(p)))

        def expandvars(self, p):
            return _win_canon(ntpath.expandvars(_win_input(p)))

        def basename(self, p):
            return _win_canon(ntpath.basename(_win_input(p)))

        def dirname(self, p):
            return _win_canon(ntpath.dirname(_win_input(p)))

        def split(self, p):
            head, tail = ntpath.split(_win_input(p))
            return _win_canon(head), _win_canon(tail)

        def isabs(self, p):
            s = _win_input(p)
            # Python 3.13 以降の ntpath.isabs は /tmp 等のドライブなしを相対とするが、判定上は絶対パスとして扱う
            return ntpath.isabs(s) or s.startswith("/")

    P = _WinPath()
    _PATH_SPLIT_RE = re.compile(r"[\\/]")
else:
    P = os.path
    _PATH_SPLIT_RE = re.compile("/")


def _has_dotdot(path):
    """パス要素に .. を含むか（Windows では \\ も区切りとみなす）。"""
    return ".." in _PATH_SPLIT_RE.split(path)


def _norm_text(text):
    """コマンド文字列中の保護語を探すための正規化（Windows のみ区切りと大文字小文字を揃える）。"""
    return text.replace("\\", "/").lower() if WINDOWS else text


HOME = P.normpath(P.expanduser("~")) if WINDOWS else P.expanduser("~")
RUNTIME = "codex" if "--codex" in sys.argv else "claude"
# Codex 起動側（cross_check.py の TOOL_POLICY_ENV と同名）が渡す道具方針。hook matcher 対象の道具について
# none=全拒否、read=Bash・Read のみ検査付きで許可（matcher 外の道具は起動側の実行監視が止める）
TOOL_POLICY_ENV = "HARNESS_CODEX_TOOL_POLICY"
# hook の timeout（10 秒）超過は未判定のまま実行されるため、その前に拒否する
GUARD_SECONDS = 8
CONFIG_NAME = "harness_config.json"
CONFIG_PATH = P.join(P.dirname(P.abspath(__file__)), CONFIG_NAME)
PROFILE_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,128}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ConfigError(ValueError):
    """ガード設定の欠落・破損。保護を弱めないよう全操作を拒否する。"""


def _abs_path(value, name):
    if not isinstance(value, str) or not P.isabs(value) or any(c in value for c in "\n\r\0"):
        raise ConfigError(f"{name} は絶対パスの文字列が必要")
    return P.normpath(value)


def load_config(path=CONFIG_PATH):
    """設定を検証して返す。不正なら ConfigError（main() で全操作拒否にする）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as error:
        # 深い入れ子の RecursionError 等も含め、読めない設定はすべて設定エラーにする
        raise ConfigError(f"{CONFIG_NAME} を読めない（{type(error).__name__}）") from None
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ConfigError("version は 1 が必要")
    config = {"project_root": _abs_path(data.get("project_root"), "project_root"),
              "harness_home": _abs_path(data.get("harness_home"), "harness_home"),
              "profile": None, "assume_role_cmd": None, "assume_role_envs": ()}
    cloud = data.get("aws")
    if not isinstance(cloud, dict) or not isinstance(cloud.get("enabled"), bool):
        raise ConfigError("aws.enabled は真偽値が必要")
    config["aws_enabled"] = cloud["enabled"]
    if cloud["enabled"]:
        profile = cloud.get("profile")
        if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
            raise ConfigError("aws.profile が不正")
        command = cloud.get("assume_role_cmd") or None
        envs = cloud.get("assume_role_envs", [])
        if not isinstance(envs, list) or not all(isinstance(e, str) and ENV_NAME_RE.fullmatch(e) for e in envs):
            raise ConfigError("aws.assume_role_envs は英数字の文字列リストが必要")
        config.update(profile=profile, assume_role_envs=tuple(envs),
                      assume_role_cmd=_abs_path(command, "aws.assume_role_cmd") if command else None)
    extra = data.get("extra_protected_paths", [])
    if not isinstance(extra, list):
        raise ConfigError("extra_protected_paths はリストが必要")
    config["extra_protected_paths"] = [_abs_path(p, "extra_protected_paths") for p in extra]
    return config


try:
    CONFIG, CONFIG_ERROR = load_config(), None
except Exception as _error:
    # 判定関数は最も厳しい既定値で動くが、main() は設定エラーなら全操作を拒否する。
    # 読み込み時の例外で異常終了すると Codex の直接起動は未判定で実行されるため、例外はすべてここで止める。
    CONFIG, CONFIG_ERROR = None, (str(_error) if isinstance(_error, ConfigError) else type(_error).__name__)

PROJECT = CONFIG["project_root"] if CONFIG else None
HARNESS_HOME = CONFIG["harness_home"] if CONFIG else None
AWS_ENABLED = CONFIG["aws_enabled"] if CONFIG else True
# 設定エラー時は実在し得ないプロファイル名にして、プロファイル指定はすべて拒否側に倒す
ALLOWED_PROFILE = (CONFIG["profile"] if CONFIG else "\0invalid-config") if AWS_ENABLED else None
ASSUME_ROLE_CMD = CONFIG["assume_role_cmd"] if CONFIG else None
ASSUME_ROLE_ENVS = CONFIG["assume_role_envs"] if CONFIG else ()

# 承認なしで削除してよい一時ディレクトリ
TEMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/",
                 "$TMPDIR", "${TMPDIR")


def _protected_write():
    """改変時に承認を求める保護パスと、コマンド文字列中の保護語。"""
    paths = [P.join(HOME, rel) for rel in (
        ".claude/settings.json", ".claude/hooks/", ".claude.json", ".aws/", ".codex/config.toml", ".codex/hooks/")]
    words = [".claude/settings", ".claude/hooks", ".claude.json", ".aws/", ".codex/config", ".codex/hooks", CONFIG_NAME]
    if HARNESS_HOME:
        name = P.basename(HARNESS_HOME)
        # ハーネス本体（hook・インストーラ・設定断片・default 認証で動く認証更新スクリプト）と利用者設定
        for sub in ("hooks", "install", "settings", "tools/aws"):
            paths.append(P.join(HARNESS_HOME, sub) + "/")
            words.append(f"{name}/{sub}")
        paths.append(P.join(HARNESS_HOME, "config.local.json"))
        words.append("config.local.json")
    if PROJECT:
        paths.append(P.join(PROJECT, ".claude/settings"))
    if ASSUME_ROLE_CMD:
        paths.append(ASSUME_ROLE_CMD)
        words.append(P.basename(ASSUME_ROLE_CMD))
    for extra in (CONFIG or {}).get("extra_protected_paths", []):
        paths.append(extra)
        words.append(extra)
    return paths, words


PROTECTED_WRITE, PROTECTED_WRITE_WORDS = _protected_write()


_PROTECTED_ENTRIES_CACHE = {}


def _protected_entries():
    """保護パスを (基準パス, ディレクトリか) で字句・実体の両方について列挙する（保護パス自体がリンクの場合も含む）。
    PROTECTED_WRITE の内容ごとにキャッシュする（実体パス解決の繰り返しを避ける）。"""
    key = tuple(PROTECTED_WRITE)
    if key not in _PROTECTED_ENTRIES_CACHE:
        entries = []
        for prot in key:
            base = P.normpath(prot)
            for b in dict.fromkeys((base, P.realpath(base))):
                entries.append((b, prot.endswith("/")))
        _PROTECTED_ENTRIES_CACHE[key] = tuple(entries)
    return _PROTECTED_ENTRIES_CACHE[key]


def is_protected_write(path):
    """絶対パスが改変承認の対象（保護パス配下・任意プロジェクトの .claude/settings*）なら True。"""
    for base, is_dir in _protected_entries():
        # ディレクトリはそれ自体（mv・cp の宛先）と配下、それ以外は従来どおり前方一致
        if (path == base or path.startswith(base + "/")) if is_dir else path.startswith(base):
            return True
    return bool(re.search(r"/\.claude/settings[^/]*$", path))

# 読み取り禁止の秘密情報。~/.aws は許可リスト方式（config と MCP ログ以外は不可）。
AWS_DIR = P.join(HOME, ".aws")
AWS_READABLE_FILES = (P.join(AWS_DIR, "config"),)
AWS_READABLE_DIRS = (P.join(AWS_DIR, "aws-api-mcp"),)
SECRET_FILES = (P.join(HOME, ".codex/auth.json"), P.join(HOME, ".claude/.credentials.json"))
# 文字列中（python -c 等）に現れる秘密情報パスの補助検出
_SEP_RE = r"[\\/]+" if WINDOWS else "/"  # Windows は \ 区切り（JSON の \\ を含む）と大文字小文字の違いも検出する
SECRET_WORDS = re.compile(rf"\.aws{_SEP_RE}(credentials|\.credentials|cli{_SEP_RE}cache|sso{_SEP_RE}cache|login{_SEP_RE}cache)"
                          rf"|\.codex{_SEP_RE}auth\.json|\.claude{_SEP_RE}\.credentials", re.I if WINDOWS else 0)

# 後続コマンドを実行するラッパー: (値を取るオプション, オプション後に読み飛ばす位置引数の数)
WRAPPERS = {
    "sudo": ({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "--user", "--group",
              "--chdir", "--close-from", "--host", "--prompt", "--role", "--type", "--other-user",
              "--command-timeout"}, 0),
    "doas": ({"-u", "-C"}, 0),
    "nice": ({"-n", "--adjustment"}, 0),
    "timeout": ({"-s", "-k", "--signal", "--kill-after"}, 1),
    "caffeinate": ({"-t", "-w"}, 0),
    "stdbuf": ({"-i", "-o", "-e", "--input", "--output", "--error"}, 0),
    "arch": ({"-arch"}, 0),
    "exec": ({"-a"}, 0),
    "time": (set(), 0), "nohup": (set(), 0), "command": (set(), 0), "builtin": (set(), 0),
}

# 再帰読み取り: 値を取るオプション（パターン・出力先などパス以外の値を読み飛ばす）
SEARCH_VALUE_OPTS = {
    "grep": {"-e", "--regexp", "-f", "--file", "-m", "--max-count", "-A", "-B", "-C", "--context",
             "--after-context", "--before-context", "-d", "--directories", "-D", "--devices",
             "--include", "--exclude", "--exclude-dir", "--label"},
    "rg": {"-e", "--regexp", "-f", "--file", "-g", "--glob", "--iglob", "-t", "--type", "-T",
           "--type-not", "--type-add", "-m", "--max-count", "-A", "-B", "-C", "--context",
           "--after-context", "--before-context", "-j", "--threads", "-d", "--max-depth", "-E",
           "--encoding", "-M", "--max-columns", "-r", "--replace", "--pre", "--pre-glob",
           "--ignore-file", "--sort", "--sortr", "--max-filesize", "--engine", "--colors",
           "--color", "--path-separator", "--context-separator", "--field-match-separator",
           "--field-context-separator"},
    "ag": {"-G", "--file-search-regex", "--ignore", "--ignore-dir", "-A", "-B", "-C",
           "--context", "-m", "--max-count", "--depth", "-p", "--path-to-ignore", "-g"},
}
SEARCH_VALUE_OPTS["egrep"] = SEARCH_VALUE_OPTS["fgrep"] = SEARCH_VALUE_OPTS["grep"]
COPY_VALUE_OPTS = {
    "tar": {"-f", "--file", "-C", "--directory", "-T", "--files-from", "-X", "--exclude-from",
            "--exclude", "-b", "--block-size"},
    "rsync": {"-e", "--rsh", "--exclude", "--include", "--filter", "--exclude-from",
              "--include-from", "--files-from", "-T", "--temp-dir", "--backup-dir", "--link-dest",
              "--compare-dest", "--copy-dest", "--log-file", "--password-file", "--chmod"},
    "cp": set(), "zip": set(), "ditto": set(),
}
GLOB_LIMIT = 2000
GLOB_SECONDS = 1.0
CHDIR = "__CHDIR__"  # strip_prefix が実行ディレクトリ変更ラッパーを検出した印

# AWS 参照系オペレーションの判定
READ_OP_PREFIXES = ("describe-", "get-", "list-", "batch-get-", "head-", "lookup-",
                    "search-", "filter-", "scan", "query", "tail", "wait", "help",
                    "validate-", "estimate-", "preview-", "check-", "test-dns-answer")
READ_OP_DENY = {"get-login-password", "get-authorization-token", "get-secret-value",
               "batch-get-secret-value", "get-session-token", "get-federation-token",
               "get-role-credentials", "get-credentials", "get-password-data"}
S3_READ = {"ls", "presign"}
LOGS_READ = {"start-query", "stop-query"}
# Bedrock のモデル推論呼び出しだけを例外として許可する（2026-11-20 ユーザー決定）。
# 読み取り専用プロファイルには推論権限がないため、Bedrock API キーを渡すラッパー（tools/aws/bedrock_run.py）経由に限る。
# 非同期呼び出し（S3 へ結果を書く）・ガードレール・管理系（bedrock サービス）・エージェントは対象外。
BEDROCK_INFER_OPS = {"converse", "converse-stream", "invoke-model", "invoke-model-with-response-stream"}
BEDROCK_RUN_NAME = "bedrock_run.py"
BEDROCK_TOKEN_RE = re.compile(r"AWS_BEARER_TOKEN_BEDROCK")
PYTHON_RE = re.compile(r"^python[0-9.]*$")
# 以下はラッパー（tools/aws/bedrock_run.py）と同じ許可範囲。変更時は両方を揃える（動値テストで検証）
BEDROCK_PROGRAMS = {"aws", "python", "python3", "uv"}
BEDROCK_PYTHONS = {"python", "python3"}
BEDROCK_AWS_VALUE_OPTS = {
    "--model-id", "--messages", "--system", "--inference-config", "--tool-config", "--guardrail-config",
    "--additional-model-request-fields", "--additional-model-response-field-paths", "--prompt-variables",
    "--request-metadata", "--performance-config", "--service-tier",
    "--body", "--content-type", "--accept", "--trace", "--guardrail-identifier", "--guardrail-version",
    "--performance-config-latency",
    "--region", "--output", "--query", "--cli-binary-format", "--cli-read-timeout", "--cli-connect-timeout",
}
BEDROCK_AWS_FLAG_OPTS = {"--no-cli-pager", "--no-paginate"}
BEDROCK_OUTFILE_OPS = {"invoke-model", "invoke-model-with-response-stream"}
UV_VALUE_OPTS = {"--with", "--with-requirements", "--project", "--directory", "--index-url"}
UV_DENY_OPTS = {"--python", "-p"}
# ラッパーと同じ命令内で宛先・実行ファイル探索を変える代入（キーの持ち出し・偽の実行ファイル）を拒否する
BEDROCK_ENV_NAMES = (r"(AWS_ENDPOINT_URL\w*|\w*_PROXY|AWS_CA_BUNDLE|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE"
                     r"|SSL_CERT_FILE|SSL_CERT_DIR|AWS_CONFIG_FILE|AWS_DATA_PATH|UV_PYTHON|PATH)")
BEDROCK_ENV_DENY_RE = re.compile(rf"^{BEDROCK_ENV_NAMES}$", re.I)
# 前のセグメントの export 等も含め、ラッパーを含む命令内のこれらの代入を検出する（文字列判定の補助防御）
BEDROCK_ENV_ASSIGN_RE = re.compile(rf"(?<![\w$]){BEDROCK_ENV_NAMES}\s*=", re.I)
AWS_GLOBAL_FLAGS = {"--debug", "--no-verify-ssl", "--no-paginate", "--no-sign-request",
                    "--no-cli-pager", "--no-cli-auto-prompt", "--cli-auto-prompt"}

DEPLOY_PATTERNS = [
    r"\bcdk\s+(deploy|destroy|bootstrap|import|watch)\b",
    r"\bsam\s+(deploy|delete|sync)\b",
    r"\bterraform\s+(apply|destroy|import)\b",
    r"\bserverless\s+(deploy|remove)\b",
    r"\b(npm|yarn|pnpm)\s+(run\s+)?[\w:.-]*(deploy|destroy)\b",
    # 「A の後（改行まで）に B」は ORDERED 形式で線形時間に判定する（下の ordered_search）
    ((r"\bgreengrass-cli\b", r"\bdeployment\s+create\b"), "\n"),
]
# 認証情報の差し替え・回避
CRED_BYPASS = [
    r"\bAWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)\s*=",
    r"\bAWS_(SHARED_CREDENTIALS_FILE|CONFIG_FILE|WEB_IDENTITY_TOKEN_FILE|ROLE_ARN)\s*=",
]
# プロファイル固定（aws.enabled のときだけ判定）
PROFILE_BYPASS = [
    ((r"\bunset\s+", r"\bAWS_(PROFILE|DEFAULT_PROFILE)\b"), "[;&|]"),
    r"\bexport\s+AWS_(PROFILE|DEFAULT_PROFILE)\s*=",
]

INLINE_DELETE = [
    r"\bos\.(remove|unlink|rmdir|removedirs)\s*\(",
    r"\bshutil\.rmtree\s*\(",
    r"\.unlink\s*\(",
    r"\bfs\.(rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync)\s*\(",
    ((r"\bfind\b", r"\s-delete\b"), "[;&|]"),
    ((r"\bfind\b", r"-(exec|execdir|ok|okdir)\b", r"\b(rm|unlink|rmdir|shred)\b"), "[;&|]"),
    ((r"\bperl\b", r"\b(unlink|rmtree|remove_tree)\b"), "\n"),
    ((r"\bxargs\b", r"\b(rm|unlink)\b"), "[;&|]"),
    r"\bgit\s+(clean|rm)\b",
    ((r"\brsync\b", r"--delete"), "[;&|]"),
]
# hook の timeout は C 実装の正規表現の途中では効かない（alarm も割り込めない）ため、これを超える Bash は判定前に確認へ回す
BASH_MAX_CHARS = 64 * 1024


def ordered_search(pattern, text):
    """正規表現（文字列）か ((A, B, …), 区切り) を検索し、一致部分の文字列か None を返す。
    後者は区切りで分けた同じ区間内に A, B, … がこの順に現れるか。`A[^;&|]*B`・`A.*B` と同じ判定を、
    A が多数現れる長い入力でも二乗時間にならないよう、各区間で最初の一致から順に探して線形時間で行う。"""
    if isinstance(pattern, str):
        m = re.search(pattern, text)
        return m.group(0) if m else None
    parts, sep = pattern
    for span in re.split(sep, text):
        start, pos = None, 0
        for part in parts:
            # pos 指定の検索は直前の文字も \b の判定に使う（切り出しと違い境界が変わらない）
            m = re.compile(part).search(span, pos)
            if not m:
                break
            start = m.start() if start is None else start
            pos = m.end()
        else:
            return span[start:pos]
    return None


_OUT_LOCK = threading.Lock()  # Windows の期限切れスレッドと判定結果の出力が重ならないようにする
_TIMER = None


def _stop_timer():
    """期限切れタイマーを止める。POSIX は SIGALRM、Windows（SIGALRM なし）はスレッドのタイマー。"""
    if hasattr(signal, "alarm"):
        signal.alarm(0)
    elif _TIMER is not None:
        _TIMER.cancel()


def out(decision, reason):
    # 出力途中の期限切れで JSON が二重にならないよう、判定確定時点でタイマーを止める。
    _stop_timer()
    if _TIMER is not None and not _OUT_LOCK.acquire(blocking=False):
        # 期限切れの拒否を出力中（Windows のタイマースレッド）。そちらが終了させる
        time.sleep(GUARD_SECONDS)
        os._exit(0)
    # Codex は ask をサポートしない。失敗扱いで実行されることを防ぐ。
    if RUNTIME == "codex" and decision == "ask":
        decision = "deny"
        reason += "; Codex hook의 승인 요청은 미지원입니다. 필요한 작업은 사용자가 직접 실행하세요."
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}, ensure_ascii=False))
    sys.exit(0)


def split_segments(cmd):
    # ; && || | 改行でコマンドを分割（クォート内は簡易的に無視しない＝安全側）
    return [s.strip() for s in re.split(r"&&|\|\||[;|\n]|\$\(|`", cmd) if s.strip()]


def _lexer(seg, punctuation_chars=False):
    lexer = shlex.shlex(seg, posix=True, punctuation_chars=punctuation_chars)
    lexer.whitespace_split = True
    if WINDOWS:
        # Windows のパス（C:\work\a.txt 等）の \ を区切りとして残す（Bash では消えるが、意図したパスで判定する安全側）
        lexer.escape = ""
    return lexer


def _split_words(seg):
    """shlex.split と同じ（Windows は \\ を残す）。クォート不整合は ValueError。"""
    return list(_lexer(seg)) if WINDOWS else shlex.split(seg)


def tokens(seg):
    try:
        return _split_words(seg)
    except ValueError:
        return seg.split()


def strip_prefix(toks):
    """先頭の VAR=val / env / WRAPPERS（timeout・nice・sudo 等）を除去し、代入を返す"""
    assigns = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        name = P.basename(t)  # /usr/bin/env・/usr/bin/nice 等の絶対パス指定も同一視する
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            k, v = t.split("=", 1)
            assigns[k] = v
        elif name == "env":
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                if re.match(r"^(-C|--chdir)", toks[i]):
                    assigns[CHDIR] = True
                if toks[i] in ("-i", "--ignore-environment"):
                    assigns["AWS_PROFILE"] = None
                if toks[i].startswith("--unset="):
                    assigns[toks[i].split("=", 1)[1]] = None
                if toks[i] in ("-u", "--unset") and i + 1 < len(toks):
                    assigns[toks[i + 1]] = None  # unset
                    i += 1
                i += 1
            continue
        elif name in WRAPPERS:
            values, positional = WRAPPERS[name]
            j = i + 1
            while j < len(toks) and toks[j].startswith("-") and toks[j] != "-":
                if toks[j] == "--":
                    j += 1
                    break
                # command -v/-V は検索のみで実行しない
                if name == "command" and toks[j] in ("-v", "-V"):
                    return assigns, toks[i:]
                # sudo -D/--chdir は後続コマンドの実行ディレクトリを変える
                if name == "sudo" and re.match(r"^(-D|--chdir)", toks[j]):
                    assigns[CHDIR] = True
                j += 2 if toks[j] in values else 1
            i = j + positional
            continue
        else:
            break
        i += 1
    return assigns, toks[i:]


def is_temp(path):
    p = path.strip("'\"")
    if ".." in p:
        return False
    p = P.expandvars(P.expanduser(p))
    if "$" in p or not P.isabs(p):
        return False
    glob_at = next((i for i, c in enumerate(p) if c in "*?[]"), None)
    if glob_at is not None:
        # ワイルドカードは最終要素のみ許可（途中のリンク経由で一時領域外へ出ないよう親を検査）
        if "/" in p[glob_at:]:
            return False
        cut = p.rfind("/", 0, glob_at)
        # Windows の c:/* は親を c:/ にする（c: はドライブごとのカレントになるため）
        parent = P.realpath((p[:cut + 1] if WINDOWS and p[cut - 1:cut] == ":" else p[:cut]) or "/")
        p = parent.rstrip("/") + "/x"
    else:
        p = P.realpath(p)
    roots = ["/private/tmp", "/tmp", os.environ.get("TMPDIR", "")]
    if WINDOWS:
        roots += [os.environ.get("TEMP", ""), os.environ.get("TMP", ""), tempfile.gettempdir()]
    return any(root and p.startswith(P.realpath(root).rstrip("/") + "/")
               for root in roots)


def _secret_roots():
    """字句パスと実体パスの両方で保護対象を判定する（保護パス自体がリンクの場合も含む）"""
    aws_dir, secret_files = AWS_DIR, SECRET_FILES
    if WINDOWS:  # 比較対象のパスは正規形なので、保護パス側も正規形にそろえる
        aws_dir, secret_files = P.normpath(aws_dir), tuple(P.normpath(f) for f in secret_files)
    real_aws = P.realpath(aws_dir)
    roots = [(aws_dir, secret_files)]
    real_files = tuple(P.realpath(f) for f in secret_files)
    if real_aws != aws_dir or real_files != secret_files:
        roots.append((real_aws, real_files))
    return roots


def _is_secret_in(path, aws_dir, files):
    if path == aws_dir or path in files:
        return True
    if path.startswith(aws_dir + "/"):
        rel = path[len(aws_dir) + 1:]
        return not (rel in ("config", "aws-api-mcp") or rel.startswith("aws-api-mcp/"))
    return False


def is_secret(path):
    """パスが秘密情報、または ~/.aws 自体（再帰読み取りの起点）なら True"""
    if WINDOWS:  # 呼び出し元が正規化していない Windows のパス（\ 区切り・大文字）も同じに扱う
        path = P.normpath(path)
    return any(_is_secret_in(path, aws, files) for aws, files in _secret_roots())


def contains_secret(path):
    """秘密情報そのもの、または ~/.aws・既存の秘密ファイルを配下に含むディレクトリなら True"""
    if is_secret(path):
        return True
    if WINDOWS:
        path = P.normpath(path)
    prefix = path.rstrip("/") + "/"
    return any(t.startswith(prefix) for aws, files in _secret_roots()
               for t in [aws] + [f for f in files if P.lexists(f)])


def brace_expand(s, limit=64):
    """入れ子なしの {a,b} を展開する（シェル展開の近似）。上限超過は切り捨てず None"""
    # 先頭部分にカンマを含めない（`{,,,…` の長い入力で二乗時間の後戻りにならないよう。一致範囲は同じ）
    m = re.search(r"\{([^{},]*,[^{}]*)\}", s)
    if not m:
        return [s]
    out = []
    for part in m.group(1).split(","):
        sub = brace_expand(s[:m.start()] + part + s[m.end():], limit)
        if sub is None:
            return None
        out += sub
        if len(out) > limit:
            return None
    return out


def path_variants(value, cwd):
    """引数から字句パスと実体パスを列挙する（ブレース・ワイルドカード展開を含む）。展開数・時間超過は None"""
    variants = brace_expand(value)
    if variants is None:
        return None
    deadline = time.monotonic() + GLOB_SECONDS
    paths = []
    for s in variants:
        lexical = P.normpath(P.join(cwd, P.expandvars(P.expanduser(s))))
        candidates = [lexical]
        if any(c in lexical for c in "*?["):
            for n, match in enumerate(glob.iglob(lexical)):
                if n >= GLOB_LIMIT or time.monotonic() > deadline:
                    return None
                candidates.append(match)
        for c in candidates:
            paths += [P.normpath(c), P.realpath(c)]
    return paths


def secret_access(value, cwd, check):
    paths = path_variants(value, cwd)
    if paths is None:
        return ("ask", "ワイルドカードの展開対象が多すぎるため確認が必要")
    if any(check(p) for p in paths):
        return ("deny", "認証情報ファイルへのアクセスは禁止")
    return None


def is_recursive_reader(prog, argv):
    if prog in ("rg", "ag", "tar", "zip", "rsync", "ditto"):
        return True
    if prog in ("grep", "egrep", "fgrep") and ("--directories=recurse" in argv or any(
            argv[k] in ("-d", "--directories") and argv[k + 1] == "recurse" for k in range(1, len(argv) - 1))):
        return True
    return prog in ("grep", "egrep", "fgrep", "cp") and any(
        re.fullmatch(r"-[A-Za-z]*[rR][A-Za-z]*|--recursive|--dereference-recursive", a) for a in argv[1:])


def recursive_sources(prog, argv):
    """再帰読み取りの (読み取り元, パターンファイル)。
    パターン・出力先・オプション値を除き、読み取り元の省略時は cwd（"."）"""
    search = prog in SEARCH_VALUE_OPTS
    values = SEARCH_VALUE_OPTS.get(prog) or COPY_VALUE_OPTS.get(prog, set())
    positional, pattern_files, explicit_pattern, k = [], [], False, 1
    while k < len(argv):
        a = argv[k]
        k += 1
        if a == "--":
            positional += argv[k:]
            break
        if a.startswith("--"):
            opt, attached = (a.split("=", 1) + [None])[:2]
            value = attached
            if attached is None and opt in values:
                value = argv[k] if k < len(argv) else ""
                k += 1
            if search and opt in ("--regexp", "--file"):
                explicit_pattern = True
                if opt == "--file":
                    pattern_files.append(value or "")
            continue
        if a.startswith("-") and a != "-":
            # 短縮オプションのまとめ書き（-rnf FILE・-etoken・-f/path）
            for n, c in enumerate(a[1:], 2):
                if "-" + c not in values:
                    continue
                value = a[n:]
                if not value:
                    value = argv[k] if k < len(argv) else ""
                    k += 1
                if search and c in "ef":
                    explicit_pattern = True
                    if c == "f":
                        pattern_files.append(value)
                break
            continue
        positional.append(a)
    if search:
        # 明示パターン（-e/-f）がなければ最初の位置引数はパターン
        if not explicit_pattern:
            positional = positional[1:]
        return positional or ["."], pattern_files
    if prog in ("cp", "rsync", "ditto"):
        return positional[:-1], pattern_files
    return positional, pattern_files


def find_in_temp(cmd):
    """find を含む全セグメントについて、起点がすべて一時領域配下なら True"""
    found = False
    for seg in split_segments(cmd):
        _, argv = strip_prefix(tokens(seg))
        if not argv or P.basename(argv[0]) != "find":
            continue
        found = True
        opts = argv[1:]
        if any(o in ("-L", "-H", "-follow") for o in opts):
            return False
        roots = []
        for o in opts:
            if o.startswith("-") or o in ("(", "!", ")"):
                break
            roots.append(o)
        if not roots or not all(is_temp(r.rstrip("/") + "/x") for r in roots):
            return False
    return found



# Python キャッシュ以外を含むディレクトリやリンクは例外にしない。
CACHE_SCAN_LIMIT = 20000
CACHE_SCAN_SECONDS = 1.0


def _cache_path(path, cwd):
    if not isinstance(path, str) or not path or _has_dotdot(path) or any(c in path for c in '$*?[]`\n\r'):
        return None
    p = P.abspath(P.join(cwd or os.getcwd(), P.expanduser(path)))
    if not WINDOWS:
        p = P.sep + p.lstrip(P.sep)
    # macOS の標準一時パス別名だけを正規化する。
    for alias, target in (('/tmp', '/private/tmp'), ('/var', '/private/var')):
        if (p == alias or p.startswith(alias + '/')) and P.islink(alias) and P.realpath(alias) == target:
            p = target + p[len(alias):]
    # 起点は POSIX が "/"、Windows が "c:/"（正規形）
    anchor = p[:p.index(P.sep) + 1]
    current = anchor
    try:
        for part in [anchor] if p == anchor else p[len(anchor):].split(P.sep):
            current = P.join(current, part)
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return None
    except OSError:
        return None
    aws = P.join(HOME, '.aws')
    if p == aws or p.startswith(aws + P.sep):
        return None
    return p


def _cache_tick(budget):
    budget[0] += 1
    # >= : Windows の monotonic は分解能が粗く（約 15ms）、期限 0 秒でも > では切れないことがある
    if budget[0] > CACHE_SCAN_LIMIT or time.monotonic() >= budget[1]:
        raise OSError('cache scan limit')


def _cache_directory(path, budget):
    if P.basename(path) != '__pycache__' or not stat.S_ISDIR(os.lstat(path).st_mode):
        return False
    with os.scandir(path) as entries:
        for entry in entries:
            _cache_tick(budget)
            if not entry.name.endswith('.pyc') or not entry.is_file(follow_symlinks=False):
                return False
    return True


def is_python_cache(path, cwd=None):
    """実在する安全なキャッシュディレクトリまたは直下の pyc だけを許可。"""
    p = _cache_path(path, cwd)
    if p is None:
        return False
    budget = [0, time.monotonic() + CACHE_SCAN_SECONDS]
    try:
        mode = os.lstat(p).st_mode
        if stat.S_ISDIR(mode):
            return _cache_directory(p, budget)
        return (stat.S_ISREG(mode) and p.endswith('.pyc')
                and _cache_directory(P.dirname(p), budget))
    except OSError:
        return False


def _cache_delete_target(path, cwd):
    if is_python_cache(path, cwd):
        return True
    # ワイルドカードは検証済みキャッシュの直下に限定する。
    parent, leaf = P.split(path)
    return leaf in ('*.pyc', '*') and is_python_cache(parent, cwd)


def _safe_delete_target(path, cwd, cache_allowed=True):
    if _has_dotdot(path):
        return False
    if '$' in path:
        # 一時パスの既存変数対応は維持する。
        return is_temp(path)
    absolute = P.abspath(P.join(cwd, P.expanduser(path)))
    return is_temp(absolute) or (cache_allowed and _cache_delete_target(path, cwd))


def find_python_cache(argv, cwd):
    """find の削除式全体と全一致キャッシュを制限時間内で検査。"""
    if not argv or P.basename(argv[0]) != 'find':
        return False
    args = argv[1:]
    roots = []
    while args and not args[0].startswith('-'):
        roots.append(args[0])
        args = args[1:]
    if args[:4] not in (['-type', 'd', '-name', '__pycache__'],
                        ['-name', '__pycache__', '-type', 'd']):
        return False
    tail = args[4:]
    if tail[:1] == ['-prune']:
        tail = tail[1:]
    if tail not in (['-exec', 'rm', '-rf', '{}', '+'],
                    ['-exec', 'rm', '-r', '-f', '{}', '+']):
        return False
    if not roots:
        return False
    budget = [0, time.monotonic() + CACHE_SCAN_SECONDS]
    try:
        stack = []
        for root in roots:
            p = _cache_path(root, cwd)
            if p is None or not stat.S_ISDIR(os.lstat(p).st_mode):
                return False
            stack.append(p)
        while stack:
            p = stack.pop()
            _cache_tick(budget)
            if P.basename(p) == '__pycache__':
                if _cache_path(p, cwd) is None or not _cache_directory(p, budget):
                    return False
                continue
            with os.scandir(p) as entries:
                for entry in entries:
                    _cache_tick(budget)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        return True
    except OSError:
        return False


# ---------------- AWS ----------------
def check_aws_argv(argv, assigns, source, bedrock_wrapped=False):
    """argv[0] == 'aws'。問題があれば (decision, reason) を返す。
    bedrock_wrapped は bedrock_run.py 経由の呼び出し（Bedrock 推論 4 種の例外を有効にする）。"""
    profile = None
    pos = []
    i = 1
    while i < len(argv):
        t = argv[i]
        if t.startswith("--"):
            if "=" in t:
                k, v = t.split("=", 1)
                if k == "--profile":
                    profile = v
            elif t in AWS_GLOBAL_FLAGS:
                pass
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                if t == "--profile":
                    profile = argv[i + 1]
                if len(pos) < 2 or t == "--profile":
                    i += 1  # 値をスキップ（サービス/操作特定前のみ厳密に）
                else:
                    i += 1
        elif t.startswith("-") and len(pos) >= 2:
            pass
        else:
            pos.append(t)
        i += 1

    env_profile = assigns.get("AWS_PROFILE", assigns.get("AWS_DEFAULT_PROFILE", ""))
    # プロファイル固定は aws.enabled のときだけ。参照系以外の禁止は常に下で判定する。
    if AWS_ENABLED:
        if "AWS_PROFILE" in assigns and env_profile is None:
            return ("deny", f"[{source}] AWS_PROFILE の解除は禁止（{ALLOWED_PROFILE} 固定）")
        if profile and profile != ALLOWED_PROFILE:
            return ("deny", f"[{source}] --profile {profile} は禁止。AI は {ALLOWED_PROFILE}（read-only ロール）のみ使用可")
        if env_profile and env_profile != ALLOWED_PROFILE:
            return ("deny", f"[{source}] AWS_PROFILE={env_profile} は禁止。{ALLOWED_PROFILE} のみ使用可")

    if not pos or pos[0] in ("help", "--version"):
        return None
    svc = pos[0]
    op = pos[1] if len(pos) > 1 else ""
    if svc == "configure":
        if op == "get" and any(p in {"region", "output"} for p in pos[2:]):
            return None
        if op in ("list", "list-profiles"):
            return None
        return ("deny", f"[{source}] aws configure による認証設定の変更は禁止")
    if svc == "s3":
        if op in S3_READ:
            return None
        if op in ("cp", "sync"):
            args = [p for p in pos[2:]]
            # S3 → ローカルのダウンロードのみ許可（宛先が s3:// でない）
            if len(args) >= 2 and args[0].startswith("s3://") and not args[-1].startswith("s3://"):
                if "--delete" in argv:
                    return ("ask", "S3 sync --delete はローカルファイルを削除する")
                protected = check_edit(args[-1])
                if protected:
                    return protected
                return None
        return ("deny", f"[{source}] aws s3 {op} は AWS 側の変更になるため禁止。必要ならユーザーに実行を依頼すること")
    if svc == "logs" and op in LOGS_READ:
        return None
    if svc == "sts" and op == "get-caller-identity":
        return None
    if svc == "bedrock-runtime" and op in BEDROCK_INFER_OPS:
        if not bedrock_wrapped:
            return ("deny", f"[{source}] Bedrock 推論は Bedrock API キーのラッパー経由のみ許可"
                            f"（python3 <ハーネス>/tools/aws/{BEDROCK_RUN_NAME} aws bedrock-runtime {op} ...）")
        # invoke-model 系の位置引数は出力ファイル。保護パスへの書き込みは通常の判定に従う
        for outfile in pos[2:]:
            protected = check_edit(outfile)
            if protected:
                return protected
        return None
    if op in READ_OP_DENY:
        return ("deny", f"[{source}] aws {svc} {op} は認証情報を出力するため禁止")
    if op.startswith(READ_OP_PREFIXES):
        return None
    return ("deny", f"[{source}] aws {svc} {op} は参照系以外の操作のため禁止（Claude は read-only 運用）。"
                    "変更が必要ならコマンドを提示してユーザーに実行を依頼すること")


def _bedrock_python_args(args):
    """python に渡す引数: 先頭が .py スクリプトであること（-c・-m 等は不可）。"""
    if not args or args[0].startswith("-") or not args[0].endswith(".py"):
        return ("deny", f"{BEDROCK_RUN_NAME} 経由の python は .py スクリプトの直接実行のみ（-c・-m 等は不可）")
    return None


def _bedrock_aws_args(inner):
    """aws bedrock-runtime の推論 4 種と許可リストのオプションだけを通す（--debug・--endpoint-url 等は拒否）。"""
    if len(inner) < 3 or inner[1] != "bedrock-runtime" or inner[2] not in BEDROCK_INFER_OPS:
        return ("deny", f"{BEDROCK_RUN_NAME} 経由の aws は bedrock-runtime の推論 4 種のみ")
    positional = 0
    i = 3
    while i < len(inner):
        token = inner[i]
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            if name in BEDROCK_AWS_FLAG_OPTS and "=" not in token:
                i += 1
                continue
            if name not in BEDROCK_AWS_VALUE_OPTS:
                return ("deny", f"{BEDROCK_RUN_NAME} 経由の aws でオプション {name} は許可されていない")
            if "=" in token:
                i += 1
                continue
            # aws CLI は位置に関係なく --debug 等を先に解釈するため、値の位置の '-' 始まり（負数以外）も拒否する
            value = inner[i + 1] if i + 1 < len(inner) else None
            if value is None or (value.startswith("-") and not re.fullmatch(r"-[0-9]+(\.[0-9]+)?", value)):
                return ("deny", f"{BEDROCK_RUN_NAME} 経由の aws でオプション {name} の値がない（または - で始まる）")
            i += 2
            continue
        positional += 1
        if inner[2] not in BEDROCK_OUTFILE_OPS or positional > 1:
            return ("deny", f"{BEDROCK_RUN_NAME} 経由の aws で位置引数 {token} は許可されていない")
        i += 1
    # 出力ファイル（invoke-model 系）の保護パス判定などは通常の AWS 判定に委ねる
    return check_aws_argv(inner, {}, "Bash", bedrock_wrapped=True)


def bedrock_wrapper_path():
    return P.realpath(P.join(HARNESS_HOME, "tools/aws", BEDROCK_RUN_NAME)) if HARNESS_HOME else None


def check_bedrock_run(argv, cwd):
    """argv[0] は bedrock_run.py。登録済みラッパーで、tools/aws/bedrock_run.py と同じ許可範囲
    （aws bedrock-runtime 推論 4 種・python スクリプト、実行ファイルはパスなしの名前）のコマンドだけを通す。"""
    expected = bedrock_wrapper_path()
    if not expected or _real(argv[0], cwd) != expected:
        return ("deny", f"登録外の {BEDROCK_RUN_NAME} は実行不可（ハーネスの tools/aws/{BEDROCK_RUN_NAME} のみ）")
    inner = argv[1:]
    if not inner:
        return ("deny", f"{BEDROCK_RUN_NAME} に実行コマンドがない")
    prog = inner[0]
    if prog not in BEDROCK_PROGRAMS:
        return ("deny", f"{BEDROCK_RUN_NAME} 経由で実行できるのはパスなしの aws・python・python3・uv のみ")
    if prog == "aws":
        return _bedrock_aws_args(inner)
    if prog in BEDROCK_PYTHONS:
        return _bedrock_python_args(inner[1:])
    if len(inner) > 1 and inner[1] == "run":
        i = 2
        while i < len(inner) and inner[i].startswith("-"):
            name = inner[i].split("=", 1)[0]
            if name in UV_DENY_OPTS or inner[i].startswith("-p"):
                return ("deny", f"{BEDROCK_RUN_NAME} 経由の uv run で --python/-p は指定できない")
            i += 1 if "=" in inner[i] or name not in UV_VALUE_OPTS else 2
        if i < len(inner) and inner[i] in BEDROCK_PYTHONS:
            return _bedrock_python_args(inner[i + 1:])
    return ("deny", f"{BEDROCK_RUN_NAME} 経由で実行できるのは aws bedrock-runtime の推論と python スクリプトのみ")


def check_bedrock_segment(argv, assigns, cwd):
    """セグメント内の bedrock_run.py 参照を判定する。正規形（python <ラッパー> <コマンド>）以外の実行・
    宛先や実行ファイル探索を変える代入は拒否。該当しなければ None。"""
    expected = bedrock_wrapper_path()
    hits = [i for i, t in enumerate(argv)
            if P.basename(t) == BEDROCK_RUN_NAME
            or (expected and t.endswith(".py") and _real(t, cwd) == expected)]
    if not hits:
        return None
    if hits != [1] or not PYTHON_RE.match(P.basename(argv[0])):
        return ("deny", f"{BEDROCK_RUN_NAME} は `python3 <ハーネス>/tools/aws/{BEDROCK_RUN_NAME} <コマンド>` の形でのみ実行可"
                        "（インタプリタオプション・uv 経由・別名・入れ子は不可）")
    if any(BEDROCK_ENV_DENY_RE.match(k) for k in assigns if k != CHDIR):
        return ("deny", f"{BEDROCK_RUN_NAME} と同じ命令でエンドポイント・プロキシ・証明書・PATH 等の変更は禁止")
    return check_bedrock_run(argv[1:], cwd)


INTERPRETERS = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|sh|bash|zsh|dash|ksh|source|\.)$")


def _real(value, cwd):
    value = re.sub(r"^\d*[<>]+", "", value)
    return P.realpath(P.join(cwd, P.expandvars(P.expanduser(value))))


def _executed(argv):
    """セグメントで実行されるスクリプトのパスと、インライン実行コード（-c・-m の引数）を返す。"""
    if not argv:
        return None, None
    prog = P.basename(argv[0])
    if not INTERPRETERS.match(prog):
        return argv[0], None
    k = 1
    while k < len(argv) and argv[k].startswith("-") and argv[k] != "-":
        if argv[k] == "-m" or (prog not in ("source", ".") and re.fullmatch(r"-[A-Za-z]*c", argv[k])):
            return None, " ".join(argv[k + 1:])
        k += 1
    return (argv[k] if k < len(argv) else None), None


def _assume_role_targets():
    paths = [ASSUME_ROLE_CMD] if ASSUME_ROLE_CMD else []
    if HARNESS_HOME:
        paths += [P.join(HARNESS_HOME, "tools/aws", n) for n in ("assume_role.sh", "assume_role.py")]
    return {P.realpath(p) for p in paths}


def _assume_role_exact(cmd, cwd):
    """`<設定のスクリプト> --awsEnv <登録環境>` の単独実行（MFA を echo/printf から渡すパイプのみ可）か。
    変数代入・env/export・ラッパー・置換・リダイレクト・他コマンドとの連結は認めない。"""
    if not (AWS_ENABLED and ASSUME_ROLE_CMD) or any(c in cmd for c in ";&`\n\r$()<>"):
        return False
    parts = cmd.split("|")
    if len(parts) > 2:
        return False
    if len(parts) == 2:
        feed = tokens(parts[0])
        if not (len(feed) >= 2 and re.fullmatch(r"[0-9]{6}", feed[-1])
                and feed[:-1] in (["echo"], ["printf", "%s\\n"])):
            return False
    argv = tokens(parts[-1])
    return (len(argv) == 3 and _real(argv[0], cwd) == P.realpath(ASSUME_ROLE_CMD)
            and argv[1] == "--awsEnv" and argv[2] in ASSUME_ROLE_ENVS)


def check_harness_exec(cmd, cwd):
    """インストーラ適用と認証更新スクリプトの実行判定。"allow"（認証更新の正規形）・(decision, reason)・None。"""
    segments = [strip_prefix(tokens(seg))[1] for seg in split_segments(cmd)]
    installer = P.realpath(P.join(HARNESS_HOME, "install/install.py")) if HARNESS_HOME else None
    # 適用はガード設定（hook sidecar）を書き換えるため、経路・ラッパー・インライン実行に関係なく承認を求める。
    # argparse の省略形（--ap 等）も含め、"apply" を含む・--a で始まる引数があれば適用とみなす。
    runs_installer = "install.py" in cmd or any(
        installer and t and not t.startswith("-") and _real(t, cwd) == installer for argv in segments for t in argv)
    if runs_installer and ("apply" in cmd.lower() or any(t.startswith("--a") for argv in segments for t in argv)):
        return ("ask", "ハーネスのインストーラ適用（--apply）はガード設定を変更するため確認が必要")
    targets = _assume_role_targets()
    names = {P.basename(p) for p in targets} | {"assume_role.py"}
    hit = False
    for argv in segments:
        script, inline = _executed(argv)
        if inline is not None:
            hit = hit or any(n in inline for n in names)
        elif script:
            hit = hit or _real(script, cwd) in targets or P.basename(script) in names
    if not hit:
        return None
    if _assume_role_exact(cmd, cwd):
        return "allow"
    return ("ask", "認証更新スクリプトは登録済み環境名での単独実行のみ許可（変数代入・ラッパー・他コマンド併用は確認が必要）")


# Bash の書き込み判定。リダイレクトと、対象ファイルを書き換えるコマンド。
# /dev/null への出力捨て・fd 複製（2>/dev/null・>&2 等）。数字列の途中から始めない（長い数字列で二乗時間にしない）
DEVNULL_RE = re.compile(r"(?<!\d)\d*>>?\s*/dev/null\b|&>>?\s*/dev/null\b|(?<!\d)\d*>&\d+")
REDIRECT_RE = re.compile(r">\s*[^&\s]|>&\s*[^\d\s&-]")
WRITE_SIMPLE_RE = re.compile(r"\b(tee|cp|mv|chmod|ln|truncate|touch)\b")
WRITE_ORDERED = (((r"\bsed\b", r"\s(-[A-Za-z]*i|--in-place)"), "[;&|]"),
                 ((r"\bperl\b", r"\s-[A-Za-z]*i"), "[;&|]"),
                 ((r"\bpython3?\b", r"(open|write|dump)"), "\n"))


def is_write_text(text):
    """書き込みを伴うコマンド・リダイレクトを文字列で検出する（線形時間）。"""
    return bool(REDIRECT_RE.search(text) or legacy_write_text(text))


def legacy_write_text(text):
    """リダイレクト以外の書き込みコマンド（tee・sed -i・cp・mv・chmod・ln・perl -i・python の書き込み・truncate・touch）。"""
    return bool(WRITE_SIMPLE_RE.search(text) or any(ordered_search(p, text) for p in WRITE_ORDERED))
# hook 入力に cwd がないとき、相対パスの書き込み対象がこの名前（パス要素単位）を含めば承認対象にする
FALLBACK_PROTECTED_NAMES = ("hooks/", "install/", "settings/", "tools/aws/", "config.local.json", ".githooks",
                            "privacy_check.py", "harness_setup.py", "setup.sh", "apply.sh", "setup.cmd", "apply.cmd")
# cd 先として字句的に確定できる引数（変数・置換・クォート・ワイルドカードを含まない）
CD_TARGET_RE = re.compile(r"[\w./~+@%,:=\\-]+" if WINDOWS else r"[\w./~+@%,:=-]+")
# サブシェル・グループ・制御構文の先頭語（直後の cd を検出するため読み飛ばす）
SHELL_LEADERS = {"(", "{", "!", "if", "then", "else", "elif", "do", "while", "until"}
WRITE_CWD_LIMIT = 32


class WriteState:
    """Bash 書き込み判定用の実行ディレクトリ候補。
    サブシェル・pushd/popd を正確に追わず、cd 先を候補に加え続ける（どの候補で解釈しても保護対象なら承認）。
    nobase: hook 入力に cwd がない（相対パスは保護名で判定）。unknown: cd 先が確定できない（相対パスの書き込みは承認）。"""

    def __init__(self, dirs=(), nobase=False):
        self.dirs = list(dict.fromkeys(P.normpath(d) for d in dirs))
        self.nobase = nobase or not self.dirs
        self.unknown = False
        self.pushed = False
        self.pending_redirect = False
        self.cache = {}  # (語, 基準ディレクトリ) → 判定。複製間で共有する

    def copy(self):
        other = WriteState()
        other.__dict__.update(self.__dict__, dirs=list(self.dirs))
        return other


def _shell_words(seg):
    """リダイレクト演算子を独立した語にして分割する（クォート内の > は語の一部のまま）。失敗時は None。"""
    try:
        return list(_lexer(seg, punctuation_chars=True))
    except ValueError:
        return None


def _update_write_cwd(state, seg, dynamic_cd):
    """cd・pushd・popd を実行ディレクトリ候補に反映する（eval 内の cd は scan_writes が同じ候補で走査する）。"""
    try:
        toks = _split_words(seg)
    except ValueError:
        # クォートがセグメントをまたぐ断片（sh -c 'cd x; …' の先頭など）はクォート記号を外して読む
        toks = [t.strip("'\"") for t in seg.split()]
    while toks and (toks[0] in SHELL_LEADERS or toks[0].startswith("(")):
        if toks[0] != "(" and toks[0].startswith("("):
            toks[0] = toks[0].lstrip("(")
        else:
            toks.pop(0)
    _, argv = strip_prefix(toks)
    if not argv:
        return
    prog = P.basename(argv[0])
    if prog == "popd":
        # このコマンド内の pushd 以前の位置はすでに候補にある。それ以外はディレクトリスタック次第
        if not state.pushed:
            state.unknown = True
        return
    if prog not in ("cd", "pushd"):
        return
    args = argv[1:]
    while prog == "cd" and args and args[0] in ("-P", "-L", "-e", "-@"):
        args = args[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    # 引数なし（HOME・ディレクトリスタック）・cd -・置換を含む cd は移動先を確定できない
    if dynamic_cd or len(args) != 1 or args[0].startswith("-") or not CD_TARGET_RE.fullmatch(args[0]):
        state.unknown = True
        return
    state.pushed = state.pushed or prog == "pushd"
    target = P.expanduser(args[0])
    if target.startswith("~"):
        state.unknown = True
        return
    # 追加中のリストを走査しないよう複製を使う
    bases = [""] if P.isabs(target) else list(state.dirs)
    for base in bases:
        joined = P.join(base, target)
        for d in (P.normpath(joined), P.realpath(joined)):
            if d not in state.dirs:
                state.dirs.append(d)
    if len(state.dirs) > WRITE_CWD_LIMIT:
        state.unknown = True


LITERAL_RE = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")


def _positional(args, value_shorts="", value_longs=()):
    """オプション（値を取るものは値も）を除いた位置引数。"""
    out, k, end = [], 0, False
    while k < len(args):
        a = args[k]
        k += 1
        if end or a == "-" or not a.startswith("-"):
            out.append(a)
        elif a == "--":
            end = True
        elif a.startswith("--"):
            if "=" not in a and a in value_longs:
                k += 1
        else:
            # 短縮オプションのまとめ書き: 値を取る文字が末尾なら次の引数が値
            for n, c in enumerate(a[1:], 2):
                if c in value_shorts:
                    k += 1 if n == len(a) else 0
                    break
    return out


def _option_values(args, shorts="", longs=()):
    """指定オプションの値（-o X・-oX・まとめ書き -sSo X・--output=X・--output X）。"""
    values, k = [], 0
    while k < len(args):
        a = args[k]
        k += 1
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if name in longs:
                if not eq:
                    value = args[k] if k < len(args) else ""
                    k += 1
                values.append(value)
        elif a.startswith("-") and a != "-":
            for n, c in enumerate(a[1:], 2):
                if c in shorts:
                    value = a[n:]
                    if not value:
                        value = args[k] if k < len(args) else ""
                        k += 1
                    values.append(value)
                    break
    return values


def _cluster_has(args, letters):
    """単一ダッシュの短縮オプションに letters のいずれかを含むか。"""
    return any(a.startswith("-") and not a.startswith("--") and set(a[1:]) & set(letters) for a in args)


def _copy_destinations(target, sources, recursive):
    """cp・rsync の実際の書き込み位置。宛先がディレクトリなら `宛先/basename(元)` に書かれるため、それも候補にする
    （宛先がディレクトリでないときこの候補が保護パスになることはない）。再帰コピーはその位置の配下も上書きし得るため祖先判定し、
    元が `.`・`dir/.`・末尾 `/`（中身のコピー）なら宛先自体も祖先判定する。"""
    if not target:
        return []
    out = []
    for source in sources:
        if source in (".", "./") or source.endswith(("/.", "/./")) or source.endswith("/"):
            if recursive:
                out.append((target[0], True, True))
        name = P.basename(source.rstrip("/"))
        if name not in ("", ".", ".."):
            out.append((P.join(target[0], name), True, recursive))
    return out


GIT_TREE_SUBS = {"apply", "checkout", "restore", "stash", "reset", "pull", "merge", "switch", "rebase", "cherry-pick",
                 "revert", "am"}
# 作業開始時の同期（ai-harness/CLAUDE.md）として承認なしで許す pull の形（-C 以外のグローバルオプションなし）
GIT_PULL_ALLOWED = (["--ff-only"], ["--ff-only", "origin", "main"])


# pull の取得元・作業ツリーの位置・フックを変えられる設定キー（ハーネス内での書き込みは承認）
GIT_CONFIG_GUARDED = re.compile(r"(remote|branch|pull|core|url|include|includeif)\.", re.I)
GIT_CONFIG_READ_OPTS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color", "--get-colorbool",
                        "-l", "--list"}


def _git_config_changes(sub, rest):
    """remote・branch・config のうち、pull の取得元・上流・フック設定を書き換える操作なら True。"""
    if sub == "remote":
        return rest[:1] in (["add"], ["set-url"], ["rename"], ["remove"], ["rm"], ["set-branches"])
    if sub == "branch":
        return any(a in ("-u", "-t", "--track") or a.startswith(("--set-upstream-to", "--track=")) for a in rest)
    if sub != "config":
        return False
    pos = _positional(rest, "f", ("--file", "--blob", "--type", "--default", "--comment", "--value"))
    opts = [a for a in rest if a.startswith("-")]
    verb = pos[0] if pos and pos[0] in ("set", "unset", "rename-section", "remove-section", "edit", "get",
                                        "list") else None
    if verb in ("get", "list") or any(o in GIT_CONFIG_READ_OPTS for o in opts):
        return False
    if verb in ("rename-section", "remove-section", "edit") or any(
            o in ("-e", "--edit", "--rename-section", "--remove-section") for o in opts):
        return True
    keys = pos[1:] if verb else pos
    write = verb in ("set", "unset") or len(keys) >= 2 or any(
        o in ("--unset", "--unset-all", "--add", "--replace-all") for o in opts)
    return write and bool(keys) and bool(GIT_CONFIG_GUARDED.match(keys[0]))


def _git_writes(args, assigns=None):
    """git の作業ツリーを書き換えるサブコマンド（GIT_TREE_SUBS）・pull の取得元を変える設定変更の基準ディレクトリ。
    基準は -C（累積）の後に --work-tree（なければ環境変数 GIT_WORK_TREE）を適用した位置。"""
    assigns = assigns or {}
    k, cbase, worktree, gitdir = 0, None, None, assigns.get("GIT_DIR") or None
    # GIT_DIR・GIT_CONFIG_* 等の環境変数でも pull の取得元・動作を変えられるため、GIT_ 代入があれば例外にしない
    plain = not any(isinstance(n, str) and n.startswith("GIT_") for n in assigns)
    while k < len(args) and args[k].startswith("-"):
        a = args[k]
        if a == "-C" and k + 1 < len(args):
            cbase = P.join(cbase or "", args[k + 1])
            k += 2
            continue
        plain = False  # -c 等で pull の動作（rebase・フック）を変えられるため、-C 以外があれば例外にしない
        if a == "--work-tree" and k + 1 < len(args):
            worktree = args[k + 1]
            k += 2
            continue
        if a.startswith("--work-tree="):
            worktree = a.split("=", 1)[1]
        if a == "--git-dir" and k + 1 < len(args):
            gitdir = args[k + 1]
        elif a.startswith("--git-dir="):
            gitdir = a.split("=", 1)[1]
        k += 2 if a in ("-c", "--git-dir", "--namespace", "--exec-path", "--config-env") else 1
    if worktree is None and assigns.get("GIT_WORK_TREE"):
        worktree = assigns["GIT_WORK_TREE"]
    # --work-tree は -C 適用後の位置から解釈される（絶対パスなら置き換え）
    base = P.join(cbase or "", worktree) if worktree else cbase
    sub = args[k] if k < len(args) else ""
    rest = args[k + 1:]
    if _git_config_changes(sub, rest):
        # 設定は git ディレクトリに書かれるため、--git-dir・GIT_DIR の指定先（ハーネスの .git ならハーネス内）も判定する
        return [], [(base, True)] + ([(P.join(cbase or "", gitdir), True)] if gitdir else [])
    if sub not in GIT_TREE_SUBS:
        return [], []
    if sub == "stash" and rest[:1] in (["list"], ["show"]):
        return [], []
    if sub == "apply" and "--apply" not in rest and any(a in ("--check", "--stat", "--numstat", "--summary") for a in rest):
        return [], []
    # reset は --hard・--merge・--keep のときだけ作業ツリーを書き換える（--soft・--mixed は HEAD・インデックスのみ）
    if sub == "reset" and not any(a in ("--hard", "--merge", "--keep") for a in rest):
        return [], []
    if sub == "pull" and plain and rest in GIT_PULL_ALLOWED:
        return [], []
    return [], [(base, True)]


CURL_VALUE_SHORTS = "AbcCdDeEFHKmoPQrtTuUwxXyYz"
CURL_VALUE_LONGS = ("--output", "--output-dir", "--cookie-jar", "--dump-header", "--header", "--data", "--data-raw",
                    "--data-binary", "--data-urlencode", "--form", "--request", "--user", "--user-agent", "--referer",
                    "--cookie", "--upload-file", "--proxy", "--config", "--max-time", "--write-out", "--range")
WGET_VALUE_SHORTS = "ABDeiloOPQRtTUwa"
WGET_VALUE_LONGS = ("--output-document", "--output-file", "--append-output", "--directory-prefix", "--execute",
                    "--input-file", "--user-agent", "--tries", "--timeout", "--level", "--accept", "--reject",
                    "--domains", "--base", "--quota", "--wait", "--header", "--user", "--password", "--post-data",
                    "--referer")


def _url_basename(url):
    """URL のパス末尾のファイル名（curl -O・wget の既定の保存名）。スキームなしは最初の / までをホストとみなす。"""
    path = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", url)
    path = path.split("/", 1)[1] if "/" in path else ""
    return P.basename(re.split(r"[?#]", path)[0])


def _under(directory, value):
    """保存ディレクトリ指定（curl --output-dir・wget -P）があれば、その配下の位置を候補にする。
    絶対パスの扱い（そのまま使うか前に付けるか）は実装依存のため、両方を候補にする。"""
    if not directory:
        return [value]
    plain = value if value.startswith(("/", "~")) else P.join(directory, value)
    return list(dict.fromkeys((plain, P.join(directory, value.lstrip("/")))))


# wget の保存先にサーバー側のパスを再現し得るオプション（-nH・--cut-dirs と組み合わせると URL のパスがそのまま保存先になる）
WGET_TREE_LONGS = ("--recursive", "--mirror", "--force-directories", "--page-requisites")


def _download_writes(prog, args):
    """curl・wget の書き込み対象。保存名が URL・応答で決まる場合は保存ディレクトリも展開先として判定する。"""
    if prog == "curl":
        outdir = (_option_values(args, "", ("--output-dir",)) or [None])[-1]
        cands = [(p, True, False) for v in _option_values(args, "o", ("--output",)) for p in _under(outdir, v)]
        cands += [(v, True, False) for v in _option_values(args, "cD", ("--cookie-jar", "--dump-header"))]
        trees = [(outdir, False)] if outdir else []
        remote = _cluster_has(args, "OJ") or any(
            a in ("--remote-name", "--remote-name-all", "--remote-header-name") for a in args)
        if not remote:
            return cands, trees
        urls = _positional(args, CURL_VALUE_SHORTS, CURL_VALUE_LONGS) + _option_values(args, "", ("--url",))
        names = [_url_basename(u) for u in dict.fromkeys(urls)]
        return cands + [(p, True, False) for n in names if n for p in _under(outdir, n)], trees or [(None, False)]
    cands = [(v, True, False) for v in _option_values(args, "Ooa", ("--output-document", "--output-file",
                                                                     "--append-output"))]
    if _option_values(args, "O", ("--output-document",)):
        return cands, []
    prefix = (_option_values(args, "P", ("--directory-prefix",)) or [None])[-1]
    names = [_url_basename(u) or "index.html" for u in _positional(args, WGET_VALUE_SHORTS, WGET_VALUE_LONGS)]
    # 再帰・ディレクトリ再現では保存先が URL のパスで決まるため、保存先が保護パスの上位・ハーネス内でも承認
    tree = _cluster_has(args, "rxpm") or any(a.startswith(WGET_TREE_LONGS) for a in args)
    return cands + [(p, True, False) for n in names for p in _under(prefix, n)], [(prefix, tree)]


def _command_writes(argv, text, assigns=None):
    """コマンドごとの書き込み対象。対象コマンドでなければ None。
    戻り値は (候補 [(値, 厳密, 祖先判定)], 展開先 [(ディレクトリ値 or None=実行ディレクトリ, ハーネス内も対象)])。
    厳密: 未展開の変数を含むとき実行ディレクトリがハーネス内なら承認（スクリプト・コードの引数は対象外）。
    祖先判定: 保護パスの上位ディレクトリも承認対象（mv・ln・chmod）。"""
    prog, args = P.basename(argv[0]), argv[1:]
    if prog == "tee":
        return [(v, True, False) for v in _positional(args)], []
    if prog in ("sed", "perl"):
        # sed -i・perl -i（まとめ書き -pi・-i.bak・BSD の -i '' を含む）
        if not any(a.startswith("--in-place") or (not a.startswith("--") and re.match(r"-[A-Za-z]*i", a)) for a in args):
            return [], []
        script_letters, values = ("ef", "efl") if prog == "sed" else ("eE", "eEI")
        scripted = any(a.startswith(("--expression", "--file")) or (not a.startswith("--") and re.match(
            r"-[A-Za-z]*[" + script_letters + "]", a)) for a in args)
        files = [p for p in _positional(args, values, ("--expression", "--file", "--line-length")) if p]
        # -e・-f がなければ最初の位置引数はスクリプト
        return [(v, True, False) for v in (files if scripted else files[1:])], []
    if re.fullmatch(r"python[0-9.]*", prog):
        if not ordered_search(WRITE_ORDERED[2], text):
            return [], []
        # コード中の文字列リテラルもパスとして扱う（コードは厳密判定の対象外）
        return ([(w, False, False) for w in args] +
                [(a or b, False, False) for w in args for a, b in LITERAL_RE.findall(w)]), []
    if prog in ("cp", "ln", "mv", "install"):
        shorts, longs = ("mogSBtf", ("--mode", "--owner", "--group", "--suffix", "--target-directory")) \
            if prog == "install" else ("St", ("--suffix", "--target-directory"))
        targets = _option_values(args, "t", ("--target-directory",))
        pos = _positional(args, shorts, longs)
        if prog == "mv" or (prog == "install" and (_cluster_has(args, "d") or "--directory" in args)):
            paths = pos + targets  # mv は移動元も変更対象、install -d は全引数を作成
        elif targets:
            paths = targets
        elif prog == "ln" and len(pos) == 1:
            paths = [P.basename(pos[0].rstrip("/")) or pos[0]]  # 実行ディレクトリに同名のリンクを作る
        else:
            paths = pos[-1:] if len(pos) >= 2 else []  # cp・ln・install は宛先のみ（読み取り元は対象外）
        cands = [(v, True, prog in ("mv", "ln")) for v in paths]
        if prog == "cp":
            recursive = _cluster_has(args, "rRa") or any(a in ("--recursive", "--archive") for a in args)
            cands += _copy_destinations(targets[-1:] or pos[-1:], pos if targets else pos[:-1], recursive)
        elif prog == "install" and not (_cluster_has(args, "d") or "--directory" in args):
            # install は再帰しない。宛先がディレクトリなら `宛先/basename(元)` に書く
            cands += _copy_destinations(targets[-1:] or pos[-1:], pos if targets else pos[:-1], False)
        return cands, []
    if prog == "chmod":
        pos = [a for a in args if a != "--" and not re.fullmatch(r"-[RfvhHLPcEC]+|--[A-Za-z-]+(=.*)?", a)]
        if not any(a.startswith("--reference") for a in args):
            pos = pos[1:]  # 先頭はモード（-w 等の記号モードを含む）
        return [(v, True, True) for v in pos], []
    if prog == "truncate":
        return [(v, True, False) for v in _positional(args, "sr", ("--size", "--reference"))], []
    if prog == "dd":
        return [(a[3:], True, False) for a in args if a.startswith("of=")], []
    if prog == "rsync":
        values = COPY_VALUE_OPTS["rsync"]
        pos = _positional(args, "".join(o[1] for o in values if len(o) == 2), values)
        # 宛先がリモート（host:path・rsync://）なら対象外
        if len(pos) < 2 or re.match(r"[^/]*:", pos[-1]):
            return [], []
        recursive = _cluster_has(args, "ra") or any(a in ("--recursive", "--archive") for a in args)
        return [(pos[-1], True, False)] + _copy_destinations(pos[-1:], pos[:-1], recursive), []
    if prog in ("curl", "wget"):
        return _download_writes(prog, args)
    if prog == "touch":
        return [(v, True, False) for v in _positional(args, "Adrt", ("--date", "--reference", "--time"))], []
    if prog == "patch":
        outputs = _option_values(args, "o", ("--output",))
        pos = _positional(args, "dopiFrDBVYzgx", ("--directory", "--output", "--strip", "--input", "--fuzz",
                                                   "--reject-file", "--ifdef", "--prefix", "--version-control",
                                                   "--basename-prefix", "--suffix", "--get"))
        # パッチ内のファイル名は分からないため、適用先ディレクトリ（-d・実行ディレクトリ）で判定する
        dirs = _option_values(args, "d", ("--directory",))
        return [(v, True, False) for v in outputs + pos[:1]], [(d, True) for d in dirs] or [(None, True)]
    if prog == "git":
        return _git_writes(args, assigns)
    if prog in ("tar", "bsdtar", "gtar"):
        old_style = bool(args) and re.fullmatch(r"[A-Za-z]+", args[0]) and "x" in args[0]
        if not (old_style or "--extract" in args or "--get" in args or any(
                re.fullmatch(r"-[A-Za-z]+", a) and "x" in a for a in args)):
            return [], []
        dirs = _option_values(args, "C", ("--directory",))
        return [], [(d, False) for d in dirs] or [(None, False)]
    if prog == "unzip":
        # 一覧・検査・標準出力への展開は書き込みなし（-d の値は除いて判定）
        if any(re.fullmatch(r"-[A-Za-z]+", a) and not a.startswith("-d") and set(a[1:]) & set("ltvpcZz") for a in args):
            return [], []
        dirs = _option_values(args, "d", ())
        return [], [(d, False) for d in dirs] or [(None, False)]
    return None


def _segment_writes(seg, state):
    """セグメントの書き込み対象 (候補, 展開先)。書き込みがなければ None。"""
    clean = DEVNULL_RE.sub(" ", seg)
    pending = state.pending_redirect
    # `>|` はセグメント分割で切れるため、次のセグメントの先頭語をリダイレクト先として扱う
    state.pending_redirect = clean.rstrip().endswith(">")
    words = _shell_words(clean)
    if words is None:
        # 字句解析できない場合は書き込みらしければ演算子・クォート記号で区切ったすべての語を対象にする（安全側）
        parts = [p for p in re.split(r"[<>&|;()'\"\s]+", clean) if p]
        first = P.basename(parts[0]) if parts else ""
        if not (pending or is_write_text(clean) or first in WRITE_PROGS):
            return None
        return [(p, False, False) for p in (parts if pending else parts[1:])], []
    cands, trees = [], []
    if pending and words:
        cands.append((words[0], True, False))
        words = words[1:]
    simple, skip = [], False
    for n, w in enumerate(words):
        if skip:
            skip = False
            continue
        if w and all(c in "();<>|&" for c in w):
            # 入力リダイレクト・heredoc の語は読み取り元なので除き、出力リダイレクトの語は対象にする
            if "<" in w and ">" not in w:
                skip = True
            elif ">" in w and n + 1 < len(words):
                cands.append((words[n + 1], True, False))
                skip = True
            continue
        simple.append(w)
    while simple and simple[0] in SHELL_LEADERS:
        simple.pop(0)
    assigns, argv = strip_prefix(simple)
    if argv and not pending:
        result = _command_writes(argv, clean, assigns)
        if result is not None:
            cands += result[0]
            trees += result[1]
        elif legacy_write_text(clean):
            # find -exec cp・xargs mv など書き込みコマンドが引数側にある場合は、すべての語とリテラルを対象にする
            cands += [(w, False, False) for w in argv[1:]]
            cands += [(a or b, False, False) for w in argv[1:] for a, b in LITERAL_RE.findall(w)]
    return (cands, trees) if cands or trees else None


WRITE_PROGS = {"tee", "sed", "perl", "cp", "ln", "mv", "install", "chmod", "truncate", "touch", "dd", "rsync", "curl",
               "wget", "patch", "git", "tar", "bsdtar", "gtar", "unzip", "python", "python3"}


def _in_harness(path):
    return bool(HARNESS_HOME) and any(path == h or path.startswith(h + "/")
                                      for h in dict.fromkeys((HARNESS_HOME, P.realpath(HARNESS_HOME))))


def _protected_ancestor(path):
    """保護パスの上位ディレクトリ（移動・リンク差し替え・権限変更で保護を外せる位置）なら True。"""
    prefix = path.rstrip("/") + "/"
    return any(base.startswith(prefix) for base, _ in _protected_entries())


def _check_write_target(value, strict, ancestor, state):
    expanded = P.expandvars(P.expanduser(value))
    if strict and "$" in expanded and any(_in_harness(d) for d in state.dirs):
        return ("ask", f"未展開の変数を含む書き込み先（実行ディレクトリがハーネス内）: {value[:200]}")
    if P.isabs(expanded):
        bases = ["/"]
    else:
        if state.unknown:
            return ("ask", f"実行ディレクトリが確定できないため相対パスの書き込みは確認が必要: {value[:200]}")
        if state.nobase:
            padded = "/" + P.normpath(expanded) + "/"
            if any("/" + n.rstrip("/") + "/" in padded for n in FALLBACK_PROTECTED_NAMES):
                return ("ask", f"ガード設定/認証関連ファイルの変更を含む可能性: {value[:200]}")
        bases = state.dirs
    for base in bases:
        key = (value, base, ancestor)
        if key not in state.cache:
            # heredoc 等で同じ語が繰り返されるため、実体パス解決の結果をコマンド内で再利用する
            paths = path_variants(value, base)
            if paths is None:
                state.cache[key] = ("ask", "ワイルドカードの展開対象が多すぎるため確認が必要")
            else:
                state.cache[key] = next((("ask", f"ガード設定/認証関連ファイルの変更: {p}") for p in paths
                                         if is_protected_write(p)), None) or next(
                    (("ask", f"保護パスを含むディレクトリの移動・リンク・権限変更: {p}") for p in paths
                     if ancestor and _protected_ancestor(p)), None)
        if state.cache[key]:
            return state.cache[key]
    return None


def _check_tree(value, harness, state):
    """展開・ダウンロード保存・パッチ適用・git 作業ツリー変更の基準ディレクトリが保護パスの中（git・patch は上位・ハーネス内も）なら承認。"""
    if value is None:
        if state.unknown:
            return ("ask", "実行ディレクトリが確定できないため展開・パッチ適用は確認が必要")
        dirs = [x for d in state.dirs for x in (d, P.realpath(d))]
    else:
        expanded = P.expandvars(P.expanduser(value))
        if "$" in expanded:
            if any(_in_harness(d) for d in state.dirs):
                return ("ask", f"未展開の変数を含む展開先（実行ディレクトリがハーネス内）: {value[:200]}")
            return None
        if not P.isabs(expanded) and state.unknown:
            return ("ask", "実行ディレクトリが確定できないため展開・パッチ適用は確認が必要")
        dirs = []
        for base in (["/"] if P.isabs(expanded) else state.dirs):
            paths = path_variants(value, base)
            if paths is None:
                return ("ask", "ワイルドカードの展開対象が多すぎるため確認が必要")
            dirs += paths
    for d in dirs:
        # 展開（tar・unzip）・ダウンロード保存先（curl・wget）は保護ディレクトリ内のみ。git・patch は書き込み先が引数に現れないため上位・ハーネス内も
        if is_protected_write(d) or (harness and (_protected_ancestor(d) or _in_harness(d))):
            return ("ask", f"保護パスを含む場所での展開・パッチ適用・作業ツリー変更: {d}")
    return None


def check_protected_write(seg, state):
    """セグメントの書き込み対象を実行ディレクトリ候補で絶対パス・実体パスにし、保護パスと比較する。"""
    result = _segment_writes(seg, state)
    if not result:
        return None
    cands, trees = result
    for value, strict, ancestor in cands:
        # --opt=path・of=path 形式の値も対象にする
        for v in dict.fromkeys((value, value.split("=", 1)[-1])):
            if v and not v.startswith("-"):
                r = _check_write_target(v, strict, ancestor, state)
                if r:
                    return r
    for value, harness in trees:
        r = _check_tree(value, harness, state)
        if r:
            return r
    return None


def _logical_segments(cmd):
    """split_segments の断片を、クォートが閉じるまで連結した論理セグメントにする。
    (連結後の文字列, 構成断片) を返す。クォート内の ; | && で切れた sh -c・python -c を一体で読むため。"""
    out, buf, quote = [], [], None
    for piece in split_segments(cmd):
        buf.append(piece)
        # クォート状態を断片ごとに追跡する（連結文字列の再解析を繰り返さず線形時間に保つ）
        k = 0
        while k < len(piece):
            c = piece[k]
            if quote == "'":
                quote = None if c == "'" else quote
            elif quote == "$'":
                # ANSI-C クォートはバックスラッシュでエスケープできる
                k += 1 if c == "\\" else 0
                quote = None if c == "'" else quote
            elif c == "\\":
                k += 1
            elif quote == '"':
                quote = None if c == '"' else quote
            elif piece.startswith("$'", k):
                quote = "$'"
                k += 1
            elif c in "'\"":
                quote = c
            k += 1
        if quote is None:
            out.append((" ; ".join(buf), buf))
            buf = []
    if buf:
        out.append((" ; ".join(buf), buf))
    return out


HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:'([A-Za-z0-9_.-]+)'|\"([A-Za-z0-9_.-]+)\"|([A-Za-z0-9_.-]+))")


def _loose_words(text):
    try:
        return _split_words(text)
    except ValueError:
        return [t.strip("'\"") for t in text.split()]


def _git_message_command(words):
    """git commit・git tag なら (サブコマンド, 以降の引数)。それ以外は None。"""
    while words and words[0] in SHELL_LEADERS:
        words = words[1:]
    _, argv = strip_prefix(words)
    if not argv or P.basename(argv[0]) != "git":
        return None
    k = 1
    while k < len(argv) and argv[k].startswith("-"):
        k += 2 if argv[k] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    return (argv[k], argv[k + 1:]) if k < len(argv) and argv[k] in ("commit", "tag") else None


def _heredoc_is_data(inner, piped, enclosing):
    """heredoc 本文がデータとしてだけ使われるか。本文を読むコマンドが cat・tee・git commit/tag -F - で、
    パイプで他のコマンドへ渡さず、$(…) 内なら外側が git commit/tag（メッセージ引数）の場合に限る。"""
    if piped or len(enclosing) > 1:
        return False
    words = _loose_words(inner)
    while words and words[0] in SHELL_LEADERS:
        words = words[1:]
    _, argv = strip_prefix(words)
    if not argv:
        return False
    prog = P.basename(argv[0])
    if enclosing:
        return prog == "cat" and enclosing[0] is not None and _git_message_command(_loose_words(enclosing[0])) is not None
    if prog in ("cat", "tee"):
        return True
    git = _git_message_command(words)
    if not git:
        return False
    rest = git[1]
    return any(a in ("-F-", "--file=-") or (a in ("-F", "--file") and rest[n + 1:n + 2] == ["-"])
               for n, a in enumerate(rest))


def strip_literal_heredocs(cmd):
    """区切りをクォートした heredoc（<<'EOF'）のうち、本文がデータとしてだけ使われるもの（_heredoc_is_data）の本文を除く。
    クォート外の << だけを対象にし（$(…) 内はクォート文脈を戻す、$'…' も扱う）、単語先頭の # 以降は注釈として読まない。
    区切りが未クォートの本文や、本文を実行し得るコマンド（source /dev/stdin・sh 等）の本文は残す。"""
    lines = cmd.split("\n")
    out, i, quote, stack = [], 0, None, []
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        found, k, start = [], 0, 0
        while k < len(line):
            c = line[k]
            if quote == "'":
                quote = None if c == "'" else quote
            elif quote == "$'":
                k += 1 if c == "\\" else 0
                quote = None if c == "'" else quote
            elif c == "\\":
                k += 1
            elif line.startswith("$(", k):
                # (外側のクォート, 外側コマンドの開始位置, 外側コマンドの文字列)
                stack.append((quote, start, line[start:k]))
                quote = None
                k += 1
                start = k + 1
            elif quote == '"':
                quote = None if c == '"' else quote
            elif line.startswith("$'", k):
                quote = "$'"
                k += 1
            elif c in "'\"":
                quote = c
            elif c == "#" and (k == 0 or line[k - 1] in " \t;&|()"):
                break  # 注釈
            elif c == "(":
                stack.append((None, start, None))
                start = k + 1
            elif c == ")" and stack:
                quote, start, _ = stack.pop()
            elif c in ";&|":
                start = k + 1
            elif line.startswith("<<", k) and not line.startswith("<<<", k):
                m = HEREDOC_RE.match(line, k)
                if m:
                    tail = re.match(r"[^;&|)]*", line[m.end():]).group(0)
                    after = line[m.end() + len(tail):]
                    piped = after.startswith("|") and not after.startswith("||")
                    # プロセス置換（>(…)・<(…)）がある行は出力が別コマンドに渡り得るためデータ扱いしない
                    data = bool(m.group(2) or m.group(3)) and not re.search(r"[<>]\(", line) and _heredoc_is_data(
                        line[start:k] + " " + tail, piped, [e[2] for e in stack])
                    found.append((m.group(1) == "-", m.group(2) or m.group(3) or m.group(4), data))
                    k = m.end()
                    continue
            k += 1
        for tabs, delim, data in found:
            body = i
            while i < len(lines) and (lines[i].lstrip("\t") if tabs else lines[i]) != delim:
                i += 1
            if not data:
                out.extend(lines[body:i])
            if i < len(lines):
                out.append(lines[i])
                i += 1
    return "\n".join(out)


def scan_writes(cmd, state, depth=0):
    """コマンド全体の書き込み対象を cd・sh -c・eval を反映した実行ディレクトリ候補で判定する。最初の該当を返す。"""
    if depth > 4:
        return ("ask", "シェルの入れ子が深すぎるため確認が必要")
    cmd = strip_literal_heredocs(cmd)
    # 置換（$(…)・`…`・変数）を含む cd はセグメント分割で引数が欠けるため、コマンド内の cd 全体を確定不可にする
    dynamic_cd = bool(ordered_search(((r"(^|[\s({!])(cd|pushd)\s", r"[$`]"), r"[;&|\n]"), cmd))
    for joined, pieces in _logical_segments(cmd):
        # クォートをまたぐ連結は、断片単位（$(…) 内の cd・書き込み）と連結全体の両方で判定する
        for seg in (pieces + [joined] if len(pieces) > 1 else [joined]):
            r = check_protected_write(seg, state)
            if r:
                return r
            _update_write_cwd(state, seg, dynamic_cd)
        try:
            toks = _split_words(joined)
        except ValueError:
            continue
        while toks and toks[0] in SHELL_LEADERS:
            toks.pop(0)
        _, argv = strip_prefix(toks)
        prog = P.basename(argv[0]) if argv else ""
        if prog in ("sh", "bash", "zsh", "dash", "ksh"):
            ci = next((i for i, a in enumerate(argv[1:], 1)
                       if a.startswith("-") and "c" in a[1:] and not a.startswith("--")), None)
            if ci is not None and ci + 1 < len(argv):
                # 子シェルの cd は呼び出し側に影響しないため複製で走査する
                r = scan_writes(argv[ci + 1], state.copy(), depth + 1)
                if r:
                    return r
        elif prog == "eval":
            # eval は同じシェルで再解釈されるため、cd も含めて同じ候補で走査する
            r = scan_writes(" ".join(argv[1:]), state, depth + 1)
            if r:
                return r
    return None


def check_bash(cmd, depth=0, cwd=None, write_state=None):
    if depth > 4:
        return ("ask", "シェルの入れ子が深すぎるため確認が必要")
    r = _text_checks(cmd)
    if r:
        return r
    # 書き込み判定の実行ディレクトリ候補。cwd が渡されなければ（hook 入力に cwd なし）保護名で判定する。
    # 入れ子（sh -c・eval）は最上位の scan_writes が cd を反映して走査済み。結果は他の判定（deny を含む）の後に返す
    write_hit = None
    if depth == 0:
        write_hit = scan_writes(cmd, write_state or WriteState([cwd] if cwd else []))
    return _check_bash_segments(cmd, depth, cwd, write_hit)


def _text_checks(cmd, extra_cred=()):
    """Bash・PowerShell 共通の、入力長に対して線形時間の文字列判定（長い入力でも deny を返すため先に行う）。"""
    # 認証情報ファイルの読み取り禁止
    if SECRET_WORDS.search(cmd):
        return ("deny", "認証情報ファイルへのアクセスは禁止")
    # Bedrock API キーは bedrock_run.py の内部でだけ扱う（AI による参照・出力・別キーの指定を禁止）
    if BEDROCK_TOKEN_RE.search(cmd):
        return ("deny", f"Bedrock API キーの環境変数の参照・設定は禁止（tools/aws/{BEDROCK_RUN_NAME} 経由のみ）")
    if BEDROCK_RUN_NAME in cmd and BEDROCK_ENV_ASSIGN_RE.search(cmd):
        return ("deny", f"{BEDROCK_RUN_NAME} を含む命令でエンドポイント・プロキシ・証明書・PATH 等の変更は禁止")
    if ordered_search(((r"\baws\b", r"--with-decryption\b"), "\n"), cmd):
        return ("deny", "復号した秘密情報の取得は禁止")

    for pat in CRED_BYPASS + (PROFILE_BYPASS if AWS_ENABLED else []) + list(extra_cred):
        if ordered_search(pat, cmd):
            return ("deny", "AWS 認証情報/プロファイルの差し替えは禁止（AI は設定したプロファイル固定）")
    for pat in DEPLOY_PATTERNS:
        if ordered_search(pat, cmd):
            return ("deny", "AWS へのデプロイ/削除系コマンドは禁止。デプロイはユーザーが実行する")
    # 長すぎる入力は以降の判定前に確認へ回す（C 実装の正規表現は期限切れで止められず、hook timeout で未判定実行になる。
    # 以降の処理は入力長に比例して重い）
    if len(cmd) > BASH_MAX_CHARS:
        # 以降の deny 判定（AWS 操作・AWS_PROFILE 代入・相対パス等の秘密情報アクセス）を行えないため、
        # aws コマンド・.aws パス要素を含むものは拒否する（どちらも線形時間の検索）
        if re.search(r"\baws\b", cmd) or re.search(r"(?<![\w.-])\.aws(?![\w.-])", cmd):
            return ("deny", f"コマンドが長すぎる（{len(cmd)} 文字 > {BASH_MAX_CHARS}）うえ AWS 関連の操作を含むため拒否")
        return ("ask", f"コマンドが長すぎる（{len(cmd)} 文字 > {BASH_MAX_CHARS}）ため確認が必要")
    return None


def _check_bash_segments(cmd, depth, cwd, write_hit):
    decisions = []
    seen_find = False
    safe_finds = True
    cwd_known = True
    cwd = cwd or os.getcwd()
    r = check_harness_exec(cmd, cwd)
    if r == "allow":
        return None
    if r:
        return r

    for seg in split_segments(cmd):
        assigns, argv = strip_prefix(tokens(seg))
        if AWS_ENABLED and any(k in assigns and assigns[k] != ALLOWED_PROFILE
                               for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")):
            return ("deny", f"AWS_PROFILE の解除・差し替えは禁止（{ALLOWED_PROFILE} 固定）")
        # sudo -D・env -C で実行ディレクトリが変わると相対パス・暗黙 cwd を正しく判定できない
        if assigns.pop(CHDIR, False) and argv:
            return ("ask", "実行ディレクトリを変更するラッパーのため確認が必要")
        if not argv:
            continue
        prog = P.basename(argv[0])
        # 相対パス・./・シンボリックリンク経由の秘密ファイル読み取りも遮断する。
        recursive = is_recursive_reader(prog, argv)
        for arg in argv[1:]:
            arg = re.sub(r"^\d*[<>]+", "", arg)
            # --opt=path・file://path 形式の値も検査する
            for value in {arg, arg.split("=", 1)[-1]}:
                value = re.sub(r"^(file|fileb)://", "", value)
                if not value or value.startswith("-"):
                    continue
                expanded = P.expandvars(P.expanduser(value))
                if ".aws/" in expanded and any(c in expanded for c in "*?[]$"):
                    return ("deny", "曖昧な AWS 認証ファイルアクセスは禁止")
                # 再帰読み取りのパターン（例: grep -r /Users/x src）はパス扱いしない
                if recursive and prog in SEARCH_VALUE_OPTS:
                    continue
                r = secret_access(value, cwd, is_secret)
                if r:
                    return r
        # 再帰読み取り（grep -r・rg・tar 等）は秘密情報を含む上位ディレクトリ・暗黙の cwd も対象にする。
        if recursive:
            sources, pattern_files = recursive_sources(prog, argv)
            # パターンファイル（-f/--file）も読み取り対象
            for value, check in [(v, contains_secret) for v in sources] + [(v, is_secret) for v in pattern_files]:
                r = secret_access(value, cwd, check)
                if r:
                    return r
        if prog == "cd":
            if len(argv) == 2 and not argv[1].startswith("-") and not any(c in argv[1] for c in "$*?[]`\n\r") and not _has_dotdot(argv[1]):
                cwd = P.abspath(P.join(cwd, P.expanduser(argv[1])))
            else:
                cwd_known = False
        elif prog in ("pushd", "popd"):
            cwd_known = False

        # Bedrock API キーのラッパー（python3 <ハーネス>/tools/aws/bedrock_run.py <コマンド>）
        r = check_bedrock_segment(argv, assigns, cwd)
        if r:
            return r
        # 認証更新スクリプトの例外は check_harness_exec（単独実行の正規形のみ）で判定済み
        if prog == "aws":
            r = check_aws_argv(argv, assigns, "Bash")
            if r:
                return r
        elif prog in ("sh", "bash", "zsh", "dash", "ksh"):
            ci = next((i for i, a in enumerate(argv[1:], 1)
                       if a.startswith("-") and "c" in a[1:] and not a.startswith("--")), None)
            if ci is not None and ci + 1 < len(argv):
                if not cwd_known:
                    return ("ask", "実行ディレクトリが不明なシェル操作")
                r = check_bash(argv[ci + 1], depth + 1, cwd)
                if r:
                    return r
            else:
                return ("ask", "シェルスクリプトの実行には内容の確認が必要")
        elif prog == "eval":
            # eval の引数はシェルで再解釈されるため入れ子として検査する
            if not cwd_known:
                return ("ask", "実行ディレクトリが不明なシェル操作")
            r = check_bash(" ".join(argv[1:]), depth + 1, cwd)
            if r:
                return r
        elif AWS_ENABLED and any(k in assigns and assigns[k] not in (None, ALLOWED_PROFILE)
                                 for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")):
            return ("deny", f"AWS_PROFILE の差し替えは禁止（{ALLOWED_PROFILE} 固定）")

        # 削除は対象ごとに判定し、混在した一般ファイルは承認対象にする。
        if prog in ("rm", "rmdir", "unlink", "shred", "trash", "srm"):
            targets = []
            options = True
            cache_options = prog in ("rm", "rmdir", "unlink")
            parent_delete = False
            for arg in argv[1:]:
                if options and arg == "--":
                    options = False
                elif options and arg.startswith("-"):
                    if prog == "rm":
                        safe_option = arg in ("--force", "--recursive", "--dir", "--verbose", "--interactive") or re.fullmatch(r"-[frRiIdv]+", arg)
                    elif prog == "rmdir":
                        safe_option = arg in ("-v", "--verbose", "--ignore-fail-on-non-empty")
                        parent_delete = parent_delete or arg == "--parents" or (not arg.startswith("--") and "p" in arg[1:])
                    else:
                        safe_option = False
                    cache_options = cache_options and bool(safe_option)
                    continue
                else:
                    targets.append(arg)
            if parent_delete or not targets or not all((cwd_known or P.isabs(t)) and _safe_delete_target(t, cwd, cache_options) for t in targets):
                decisions.append(("ask", f"ファイル削除: {seg[:200]}"))
        if prog == "find":
            seen_find = True
            if not (find_in_temp(seg) or (cwd_known and find_python_cache(argv, cwd))):
                safe_finds = False
                if "__pycache__" in argv and any(a in ("-exec", "-execdir", "-ok", "-okdir", "-delete") for a in argv[1:]):
                    decisions.append(("ask", f"find 操作の確認: {seg[:200]}"))

    if decisions:
        return decisions[0]

    for pat in INLINE_DELETE:
        found = ordered_search(pat, cmd)
        if found:
            # find は全起点が一時領域配下かつリンク追跡なしの場合のみ承認不要。
            first = pat if isinstance(pat, str) else pat[0][0]
            if first.startswith(r"\bfind") and (find_in_temp(cmd) or (seen_find and safe_finds)):
                continue
            return ("ask", f"削除を伴う可能性のある操作: {found[:200]}")

    # 保護ファイルへの書き込み（リダイレクト・sed -i・tee・cp・mv など）をセグメント単位で判定
    for seg in split_segments(cmd):
        # /dev/null への出力捨て・fd 複製（2>/dev/null・>&2 等）は書き込みではない
        seg = DEVNULL_RE.sub(" ", seg)
        if any(w in _norm_text(seg) for w in PROTECTED_WRITE_WORDS) and is_write_text(seg):
            return ("ask", "ガード設定/認証関連ファイルの変更を含むコマンド")
    # 相対パス・cd 後の書き込み対象を実行ディレクトリ基準で解釈した判定
    return write_hit


# ---------------- PowerShell（Windows の PowerShell 道具） ----------------
# 文字列判定の補助防御で、PowerShell の完全な解析器ではない。判定できない構文は確認に回す。
PS_DELETE = {"remove-item", "ri", "rm", "del", "erase", "rd", "rmdir", "remove-itemproperty", "rp"}
PS_WRITE = {"set-content", "sc", "add-content", "ac", "out-file", "tee-object", "tee", "clear-content", "clc",
            "copy-item", "cpi", "cp", "copy", "move-item", "mi", "mv", "move", "rename-item", "rni", "ren",
            "new-item", "ni", "md", "mkdir", "set-item", "si", "new-itemproperty", "set-itemproperty", "sp",
            "export-csv", "epcsv", "export-clixml", "expand-archive", "compress-archive", "set-acl",
            "invoke-webrequest", "iwr", "wget", "curl", "invoke-restmethod", "irm", "start-bitstransfer"}
# 移動元も保護パスの外へ出す操作（上位ディレクトリの移動で保護を外せる）
PS_MOVE = {"move-item", "mi", "mv", "move", "rename-item", "rni", "ren", "set-acl"}
# 再帰読み取り（秘密情報を含む上位ディレクトリも対象）
PS_RECURSIVE_READ = {"copy-item", "cpi", "cp", "copy", "compress-archive", "select-string", "sls"}
# 文字列を再解釈・別プロセスで実行するため判定できないもの
PS_DYNAMIC = {"invoke-expression", "iex", "invoke-command", "icm", "start-process", "saps", "start", "start-job",
              "sajb", "add-type", "invoke-item", "ii", "new-psdrive", "ndr", "set-alias", "sal", "new-alias", "nal",
              "register-scheduledtask", "start-threadjob"}
PS_CD = {"set-location", "sl", "cd", "chdir", "push-location", "pushd", "pop-location", "popd"}
PS_SHELLS = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
POSIX_SHELLS = {"sh", "sh.exe", "bash", "bash.exe", "zsh", "dash"}
# パス以外の値を取る主な引数（値を書き込み先・削除対象とみなさない）
PS_VALUE_PARAMS = {"value", "encoding", "filter", "include", "exclude", "itemtype", "type", "name", "newname",
                   "erroraction", "ea", "warningaction", "wa", "pattern", "inputobject", "delimiter", "depth",
                   "method", "uri", "body", "headers", "contenttype", "useragent", "credential", "stream",
                   "propertytype", "width", "first", "last", "tail", "totalcount", "head", "readcount"}
PS_PATH_PARAMS = {"path", "literalpath", "lp", "pspath", "destination", "dest", "filepath", "outfile", "target",
                  "outputpath", "destinationpath"}
PS_NULL_TARGETS = {"$null", "nul", "nul:"}
# PowerShell の環境変数による認証情報・プロファイルの差し替え（Bash の CRED_BYPASS 以外の書き方）
PS_CRED_BYPASS = [
    r"(?i)\[(System\.)?Environment\]::SetEnvironmentVariable\s*\(\s*['\"]AWS_",
    r"(?i)\b(Remove-Item|ri|rm|del|Set-Item|si|New-Item|ni|Clear-Item|cli)\s+(-\w+\s+)*['\"]?env:\\?AWS_",
]
PS_PROFILE_RE = re.compile(r"(?i)\$env:AWS_(DEFAULT_)?PROFILE\s*=\s*(['\"]?)([^'\";|\s]*)\2")
PS_DOTNET_DELETE_RE = re.compile(r"(?i)\[(System\.)?IO\.(File|Directory|FileInfo|DirectoryInfo)\]::Delete\b|\.Delete\s*\(")
PS_DOTNET_WRITE_RE = re.compile(r"(?i)\[(System\.)?IO\.(File|Directory)\]::(Write|Append|Copy|Move|Replace|Create|Open|"
                                r"SetAttributes|SetAccessControl)\w*")
PS_WRITE_TEXT_RE = re.compile(r"(?i)(?<![\w-])(" + "|".join(sorted(map(re.escape, PS_WRITE | PS_DELETE), key=len,
                                                               reverse=True)) + r")(?![\w-])|>")


def _ps_tokens(text):
    """PowerShell の簡易字句解析。区切り（; | && || 改行・括弧・$(）で区間に分け、各区間の語のリストを返す。
    ' ' はそのまま、" " はバッククォートのエスケープだけ処理する（変数展開はしない）。リダイレクトは独立した語。"""
    segments, words, word, quoted = [], [], [], False
    i, n = 0, len(text)

    def flush_word():
        nonlocal word, quoted
        if word or quoted:
            words.append("".join(word))
        word, quoted = [], False

    def flush_segment():
        nonlocal words
        flush_word()
        if words:
            segments.append(words)
        words = []

    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and j + 1 < n and text[j + 1] == "'":
                    word.append("'")
                    j += 2
                    continue
                if text[j] == "'":
                    break
                word.append(text[j])
                j += 1
            quoted, i = True, j + 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "`" and j + 1 < n:
                    j += 1
                word.append(text[j])
                j += 1
            quoted, i = True, j + 1
        elif c == "`" and i + 1 < n:
            if text[i + 1] in "\r\n":  # 行継続
                i += 2
                continue
            word.append(text[i + 1])
            i += 2
        elif c in " \t":
            flush_word()
            i += 1
        elif c in ";\r\n{}()|" or text[i:i + 2] == "&&":
            flush_segment()
            i += 2 if text[i:i + 2] in ("&&", "||") else 1
        elif c == "$" and text[i:i + 2] == "$(":
            flush_segment()
            i += 2
        elif c in "<>" or (c in "0123456789*" and text[i + 1:i + 2] == ">" and not word):
            # リダイレクト（> >> 2> *> 2>&1）を独立した語にする
            flush_word()
            j = i
            while j < n and text[j] in "0123456789*<>&":
                j += 1
            words.append(text[i:j])
            i = j
        else:
            word.append(c)
            i += 1
    flush_segment()
    return segments


def _ps_expand(value):
    """$env:NAME・${env:NAME}・$HOME・~ を展開する。展開できない $ が残れば None。"""
    s = re.sub(r"(?i)\$\{env:([A-Za-z_][\w]*)\}|\$env:([A-Za-z_][\w]*)",
               lambda m: os.environ.get(m.group(1) or m.group(2), "\0"), value)
    s = re.sub(r"(?i)\$HOME\b", lambda m: HOME, s)
    if "\0" in s or "$" in s:
        return None
    return s


def _ps_params(argv):
    """コマンドの引数を (パス候補, 引数名の集合) に分ける。値を取る引数か不明な語はパス候補に含める（安全側）。"""
    paths, names, k = [], set(), 1
    while k < len(argv):
        a = argv[k]
        k += 1
        if re.match(r"^-[A-Za-z][\w-]*:?", a) and not re.match(r"^-\d", a):
            name, _, attached = a[1:].partition(":")
            name = name.lower()
            names.add(name)
            if attached:
                if name in PS_PATH_PARAMS:
                    paths.append(attached)
                continue
            if name in PS_PATH_PARAMS:
                if k < len(argv):
                    paths.append(argv[k])
                    k += 1
            elif name in PS_VALUE_PARAMS and k < len(argv):
                k += 1
            continue
        paths.append(a)
    return paths, names


def _ps_redirects(words):
    """リダイレクトの書き込み先と、リダイレクトを除いた語を返す。"""
    targets, rest, k = [], [], 0
    while k < len(words):
        w = words[k]
        if re.fullmatch(r"[0-9*]?>>?", w) or re.fullmatch(r"[0-9*]?>>?&\d", w) or w == "<":
            if not w.endswith(tuple("0123456789")) and k + 1 < len(words):
                if w != "<":
                    targets.append(words[k + 1])
                k += 2
                continue
            k += 1
            continue
        rest.append(w)
        k += 1
    return targets, rest


def _ps_as_bash(argv, cwd, cwd_known):
    """外部コマンド（git・aws・python・sh -c 等）は Bash と同じ判定（書き込み先の走査を含む）に通す。
    argv[0] の拡張子 .exe 等は除く（python.exe → python）。"""
    first = re.sub(r"(?i)\.(exe|cmd|bat|com)$", "", argv[0])
    state = WriteState([cwd])
    state.unknown = not cwd_known
    return check_bash(shlex.join([first] + argv[1:]), 0, cwd, state)


def check_powershell(cmd, depth=0, cwd=None):
    """PowerShell 道具のコマンドを判定する。問題があれば (decision, reason)。"""
    if depth > 4:
        return ("ask", "シェルの入れ子が深すぎるため確認が必要")
    extra = list(PS_CRED_BYPASS)
    r = _text_checks(cmd, extra)
    if r:
        return r
    if AWS_ENABLED:
        for m in PS_PROFILE_RE.finditer(cmd):
            if m.group(3) != ALLOWED_PROFILE:
                return ("deny", f"AWS_PROFILE の解除・差し替えは禁止（{ALLOWED_PROFILE} 固定）")
    cwd = cwd or os.getcwd()
    cwd_known = True
    decisions = []
    for seg in _ps_tokens(cmd):
        redirects, argv = _ps_redirects(seg)
        while argv and argv[0] in ("&", "."):
            if argv[0] == "." and len(argv) > 1:
                decisions.append(("ask", f"スクリプトのドットソース実行は内容の確認が必要: {' '.join(argv)[:200]}"))
            argv = argv[1:]
        # 代入（$x = ...）の右辺をコマンドとして扱う
        if len(argv) >= 2 and re.fullmatch(r"\$[\w:]+", argv[0]) and argv[1] in ("=", "+="):
            argv = argv[2:]
        for target in redirects:
            if target.lower() in PS_NULL_TARGETS:
                continue
            value = _ps_expand(target)
            if value is None:
                decisions.append(("ask", f"書き込み先に展開できない変数を含む: {target[:200]}"))
                continue
            r = check_edit(value if P.isabs(value) else P.join(cwd, value))
            if r:
                return r
        if not argv:
            continue
        name = P.basename(argv[0]) if ("/" in argv[0] or "\\" in argv[0]) else argv[0].lower()
        expanded = [_ps_expand(a) for a in argv[1:]]
        # すべての引数を秘密情報の読み取りとして検査する（Get-Content・Copy-Item・type 等）
        for raw, value in zip(argv[1:], expanded):
            candidate = value or raw
            if candidate.startswith("-"):  # -Path:<値> 形式は値を検査する
                candidate = candidate.partition(":")[2]
            if candidate:
                r = secret_access(candidate, cwd, is_secret)
                if r:
                    return r
        if name in PS_DYNAMIC:
            decisions.append(("ask", f"文字列の再解釈・別プロセス実行は判定できないため確認が必要: {' '.join(argv)[:200]}"))
            continue
        if name in PS_SHELLS:
            lowered = [a.lower() for a in argv[1:]]
            ci = next((k for k, a in enumerate(lowered, 1) if a in ("-command", "-c")), None)
            if any(a.startswith(("-enc", "-e", "-ec")) and not a.startswith(("-ex", "-err")) for a in lowered) or ci is None:
                return ("ask", "PowerShell の別プロセス実行（-EncodedCommand・-File 等）は内容の確認が必要")
            if not cwd_known:
                return ("ask", "実行ディレクトリが不明なシェル操作")
            r = check_powershell(" ".join(argv[ci + 1:]), depth + 1, cwd)
            if r:
                return r
            continue
        if name in ("cmd", "cmd.exe", "wsl", "wsl.exe"):
            return ("ask", "cmd・wsl 経由の実行は判定できないため確認が必要")
        if name in POSIX_SHELLS:
            r = _ps_as_bash(argv, cwd, cwd_known)
            if r:
                return r
            continue
        if name in PS_CD:
            paths, _ = _ps_params(argv)
            target = _ps_expand(paths[0]) if len(paths) == 1 else None
            if target and not _has_dotdot(target) and not any(c in target for c in "*?[]"):
                cwd = P.abspath(P.join(cwd, target))
            else:
                cwd_known = False
            continue
        paths, names = _ps_params(argv)
        if name in PS_DELETE:
            values = [_ps_expand(p) for p in paths]
            if not values or not all(v and (cwd_known or P.isabs(v)) and _safe_delete_target(v, cwd) for v in values):
                decisions.append(("ask", f"ファイル削除: {' '.join(argv)[:200]}"))
            continue
        if name in PS_WRITE:
            recursive = "recurse" in names or "r" in names or name == "compress-archive"
            for p in paths:
                value = _ps_expand(p)
                if value is None:
                    decisions.append(("ask", f"書き込み先に展開できない変数を含む: {p[:200]}"))
                    continue
                if not P.isabs(value) and not cwd_known:
                    return ("ask", f"実行ディレクトリが確定できないため相対パスの書き込みは確認が必要: {p[:200]}")
                absolute = value if P.isabs(value) else P.join(cwd, value)
                r = check_edit(absolute)
                if r:
                    return r
                if name in PS_MOVE and _protected_ancestor(P.realpath(absolute)):
                    return ("ask", f"保護パスを含むディレクトリの移動・権限変更: {absolute}")
                if recursive and name in PS_RECURSIVE_READ:
                    r = secret_access(value, cwd, contains_secret)
                    if r:
                        return r
            continue
        if name in PS_RECURSIVE_READ and ("recurse" in names or "r" in names):
            for p in paths:
                r = secret_access(_ps_expand(p) or p, cwd, contains_secret)
                if r:
                    return r
            continue
        # 外部コマンド（git・aws・python・install.py・認証更新スクリプト等）は Bash の判定を使う
        if not re.fullmatch(r"[a-z]+-[a-z]+", name):
            r = _ps_as_bash(argv, cwd, cwd_known)
            if r:
                return r
    if decisions:
        return decisions[0]
    # .NET の削除・書き込み（[IO.File]::Delete 等）
    if PS_DOTNET_DELETE_RE.search(cmd):
        return ("ask", "削除を伴う可能性のある操作（.NET API）")
    if PS_DOTNET_WRITE_RE.search(cmd) and any(w in _norm_text(cmd) for w in PROTECTED_WRITE_WORDS):
        return ("ask", "ガード設定/認証関連ファイルの変更を含むコマンド（.NET API）")
    # 文字列としての保護語＋書き込み（変数経由のパス等の取りこぼしを補う）
    for seg in re.split(r"[;\n|]", cmd):
        if any(w in _norm_text(seg) for w in PROTECTED_WRITE_WORDS) and PS_WRITE_TEXT_RE.search(seg):
            return ("ask", "ガード設定/認証関連ファイルの変更を含むコマンド")
    return None


def check_edit(path):
    # 相対パスはプロセスの cwd（main() で hook 入力の cwd に移動済み）基準で解釈する
    p = P.realpath(P.expanduser(path or ""))
    lexical = P.abspath(P.expanduser(path or ""))
    if p.startswith(P.join(HOME, ".aws/")):
        return ("deny", "~/.aws 配下の編集は禁止")
    # どのプロジェクトでも .claude/settings.json・settings.local.json（権限・hook 設定）は承認対象
    if any(re.search(r"/\.claude/settings[^/]*$", v) for v in (p, lexical)):
        return ("ask", f"権限設定ファイルの変更: {p}")
    # 字句パス・実体パスの両方で、保護パス（字句・実体）と比較する
    if any(is_protected_write(v) for v in (p, lexical)):
        return ("ask", f"ガード設定/認証関連ファイルの変更: {p}")
    return None


MCP_SCAN_VALUES = 500


def check_mcp_input(tool_input):
    """MCP 引数の文字列（コード・URL・パスを含む）に秘密情報パスがあれば拒否する"""
    if SECRET_WORDS.search(json.dumps(tool_input, ensure_ascii=False)):
        return ("deny", "認証情報ファイルへのアクセスは禁止")
    stack, seen = [tool_input], 0
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack += list(value.values())
        elif isinstance(value, list):
            stack += value
        elif isinstance(value, str):
            seen += 1
            if seen > MCP_SCAN_VALUES:
                return ("ask", "MCP 引数が多すぎるため確認が必要")
            # パス形式の値（file:// を含む）は実体パスでも判定する
            path = re.sub(r"^file://", "", value)
            looks_path = path.startswith(("/", "~", "$HOME")) or (WINDOWS and re.match(r"[A-Za-z]:[\\/]", path))
            if looks_path and len(path) < 4096 and not any(c.isspace() for c in path):
                r = secret_access(path, os.getcwd(), is_secret)
                if r:
                    return r
    return None


def main():
    # 設定の欠落・破損は保護範囲が不明になるため、入力に関係なく拒否する（fail-closed）。
    if CONFIG_ERROR:
        out("deny", f"가드 설정({CONFIG_PATH})이 없거나 잘못되었습니다: {CONFIG_ERROR}. "
                    "install.py로 다시 설치하거나 사용자가 설정을 확인하세요. 작업을 실행하지 않았습니다.")
    try:
        data = json.load(sys.stdin)
    except Exception:
        out("deny", "가드 입력을 해석할 수 없습니다. 작업을 실행하지 않았습니다.")
    if not isinstance(data, dict) or not isinstance(data.get("tool_input", {}), dict):
        out("deny", "가드 입력 형식이 잘못되었습니다.")
    write_state = WriteState(nobase=True)
    if data.get("cwd"):
        os.chdir(data["cwd"])
        # Bash の相対パス書き込みは hook 入力の cwd（字句パス）と移動後の実体パスの両方で解釈する
        write_state = WriteState([os.getcwd()] + ([data["cwd"]] if P.isabs(data["cwd"]) else []))
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    # cross-check・prune-memory が起動した Codex は道具方針を渡す。監視の事後停止より前に実行自体を拒否する。
    policy = os.environ.get(TOOL_POLICY_ENV) if RUNTIME == "codex" else None
    if policy is not None:
        if policy not in ("none", "read"):
            out("deny", f"알 수 없는 도구 방침입니다({TOOL_POLICY_ENV}={policy[:20]}).")
        if policy == "none" or tool not in ("Bash", "Read"):
            out("deny", f"이 Codex 실행에서는 {tool[:60]} 도구를 사용할 수 없습니다({TOOL_POLICY_ENV}={policy}).")
    r = None
    if tool == "Bash":
        r = check_bash(ti.get("command", ""), write_state=write_state)
    elif tool == "PowerShell":
        r = check_powershell(ti.get("command", ""), cwd=os.getcwd() if data.get("cwd") else None)
    elif tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        r = check_edit(ti.get("file_path") or ti.get("notebook_path"))
    elif tool == "Read":
        r = secret_access(ti.get("file_path", ""), os.getcwd(), is_secret)
    elif tool == "Grep":
        r = secret_access(ti.get("path") or os.getcwd(), os.getcwd(), contains_secret)
        if not r and SECRET_WORDS.search(f'{ti.get("path", "")} {ti.get("glob", "")}'):
            r = ("deny", "認証情報ファイルの検索は禁止")
    elif tool == "apply_patch":
        patch = ti.get("command", "")
        for operation, path in re.findall(r"^\*\*\* (Add File|Update File|Delete File|Move to): (.+)$", patch, re.M):
            # キャッシュ削除だけを例外とし、通常の追加・更新・移動は保護する。
            if operation == "Delete File" and is_python_cache(path, os.getcwd()) and P.isfile(path):
                continue
            r = check_edit(path)
            if r:
                break
            if operation == "Delete File" and not is_temp(P.abspath(path)):
                r = ("ask", "一時ディレクトリ外のファイル削除")
                break
    # Codex は MCP サーバー名の英数字・_ 以外を _ に正規化する（aws-api → aws_api）
    elif tool in ("mcp__aws-api__call_aws", "mcp__aws_api__call_aws"):
        argv = tokens(ti.get("cli_command", ""))
        if argv and argv[0] == "aws":
            r = check_aws_argv(argv, {}, "MCP")
        else:
            r = ("deny", "aws で始まらないコマンドは不可")
    elif tool.startswith("mcp__"):
        # 追加される MCP も既定は許可し、引数中の秘密情報パスだけを共通で拒否する
        r = check_mcp_input(ti)
    if r:
        out(*r)
    _stop_timer()
    sys.exit(0)


class GuardTimeout(Exception):
    """hook のタイムアウト（未判定で実行される）より前に自分で拒否するための期限切れ。"""


TIMEOUT_REASON = "가드 검사가 제한 시간을 넘었습니다. 작업을 실행하지 않았습니다."


def _expire(signum, frame):
    raise GuardTimeout()


def _expire_thread():
    """Windows 用: 判定が期限内に終わらなければ拒否を出力してプロセスごと終了する。"""
    if not _OUT_LOCK.acquire(blocking=False):
        return  # 判定結果を出力中
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": TIMEOUT_REASON}}, ensure_ascii=False))
    sys.stdout.flush()
    os._exit(0)


def _start_timer(seconds=GUARD_SECONDS):
    global _TIMER
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _expire)
        signal.alarm(seconds)
    else:
        _TIMER = threading.Timer(seconds, _expire_thread)
        _TIMER.daemon = True
        _TIMER.start()


if __name__ == "__main__":
    if WINDOWS:
        # 判定理由（韓国語・日本語）を cp932 等の既定コードページで出力すると失敗するため UTF-8 に固定する
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    _start_timer()
    try:
        main()
    except GuardTimeout:
        out("deny", "가드 검사가 제한 시간을 넘었습니다. 작업을 실행하지 않았습니다.")
    except Exception:
        out("deny", "가드 검사에 실패했습니다. 보호 규칙을 확인하세요.")
