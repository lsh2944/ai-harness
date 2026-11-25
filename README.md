# ai-harness

Shared working instructions, guard hooks, settings fragments, skills and agents for Claude Code and Codex, managed in one repository and deployed to the user's home directory by `install/install.py`. Values tied to a specific project live only in `config.local.json` (excluded from Git).

## Layout

| Path | Contents |
|---|---|
| `instructions/` | Instructions inserted as marker blocks into `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`: `language.md` (response/comment language, root README language), `core.md` (ask before judging, token efficiency, harness safety), `conduct.md` (honesty, plan first, three knowledge layers, stability comparison, no broad pkill, no `cd` compound commands, MCP first), `aws-readonly.md` (optional AWS module) |
| `instructions/project-template/CLAUDE.md` | Template CLAUDE.md for a project root (role flow, delegation, review, document state, live-environment boundary). Not installed; copy and fill in |
| `hooks/` | `guard.py` (PreToolUse guard), `usage_alert.py` (usage warning), `tests/` |
| `install/install.py` | Deployment tool. Dry-run by default |
| `settings/` | Merge fragments. `claude.settings*.json` (permissions, auto-mode rules, output limits, etc.), `codex.config*.toml` (Codex defaults, override values, environment-variable exclusion list), `mcp*.json` (MCP version pins, aws-api env) |
| `agents/` | Shared subagents `code-reviewer`, `architect`, `troubleshooter`, `script-feature`, and the per-area implementation agent template `feature-template.md` |
| `skills/` | `handoff`, `cross-check`, `efficient-investigation`, `task-metrics`, `prune-memory`, `assume-dev` (AWS module) |
| `tools/` | `repo_context.py`, `investigation/`, `task_metrics/`, `aws/` (MFA credential refresh scripts, AWS module), `privacy_check.py` (pre-commit privacy check), `harness_setup.py` (helper for setup.sh) |
| `setup.sh` / `setup.cmd` | Initial setup of a new computer (macOS/Linux / Windows). See "New computer setup" below |
| `apply.sh` / `apply.cmd` | Apply to the installed copy (dry-run summary → `install.py --apply` confirmed by typing `apply`). See "Applying to the installed copy (apply)" below |
| `.githooks/` | Repository git hooks. `pre-commit` and `commit-msg` (privacy check), `post-commit` (apply reminder after a commit), `post-merge` (apply reminder after a pull) |

## New computer setup (macOS/Linux)

On Windows follow "New computer setup (Windows)" below (`setup.sh` detects MINGW/MSYS/CYGWIN, points to `setup.cmd` and exits). Git does not run repository scripts on clone, so cloning alone installs nothing. Run `./setup.sh` once after cloning.

### 1. Prepare Git and Python

- macOS: install Git with `xcode-select --install` (Command Line Tools) or Homebrew. Linux: use the distribution's package manager.
- `install.py` needs Python 3.11 or later (`tomllib`). The hook interpreter must be a system Python, not a venv (`setup.sh` looks in PATH and common install locations and suggests one).

### 2. Create a GitHub fine-grained token

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → **Generate new token**.
2. Choose a token name and set an **Expiration**. When it expires, create a new token and replace it as described in "Rotating the token" below.
3. **Resource owner**: your account. **Repository access**: under "Only select repositories", pick this repository (and other personal repositories if needed).
4. **Repository permissions**: **Contents → Read and write**. Metadata is added automatically as Read-only.
5. Copy the token value, which is shown only once right after creation. Never put the token in repository files, `config.local.json` or git config.

### 3. Clone and store credentials

```bash
git clone https://github.com/<GitHub username>/ai-harness.git
```

- At the prompt, enter your GitHub username for `Username` and paste the token from step 2 for `Password` (nothing is shown while typing).
- macOS: Git from Command Line Tools or Homebrew usually has the `osxkeychain` credential helper configured, so the token is stored in the keychain and not asked again. Check with `git config --get credential.helper`.
- Linux: without a credential helper, every fetch and push asks again. Available helpers (libsecret, Git Credential Manager, the in-memory `cache`, etc.) depend on the distribution and desktop environment. `store` keeps the token in plain text in a file in your home directory and is not recommended.

### 4. `./setup.sh`

Run it yourself in a terminal from the cloned folder (non-interactive runs such as pipes or AI tools are refused). It is safe to re-run; completed steps are skipped.

```bash
cd ai-harness
./setup.sh            # all steps (asks whether to apply at the end)
./setup.sh --no-apply # stop after the dry-run summary
```

1. If this repository has no commit author of its own (`git config --local user.name` / `user.email`), it asks for one. Global git settings are not changed. Use `<personal email>` or the GitHub noreply address (`<ID>+<GitHub username>@users.noreply.github.com`).
2. Sets `git config --local core.hooksPath .githooks` (asks before changing an existing different value).
3. If `config.local.json` does not exist, creates it interactively (mode 0600): project_root (default `$HOME/project`), hook python (suggests a Python 3.11+ outside any venv; `-` leaves it `null`), response and comment languages (template defaults), and whether to use the AWS module. `extra_protected_paths` receives the absolute paths of this clone's `.githooks`, `tools/privacy_check.py`, `tools/harness_setup.py`, `setup.sh` and `apply.sh`. With AWS enabled it copies the template's `aws` section (with `assume_role_cmd` rewritten to this clone's path) and exits, so edit `aws.roles` and `aws.mfa_serial` (and `profile` / `assume_role_envs` if needed) to real values and run it again. It does not proceed while template values remain.
4. Shows `AWS module (--with-aws): on/off/placeholder`, then the `install.py` dry-run summary (`SAME` entries omitted). Stops here if nothing changes. `--with-aws` is added when `config.local.json` has an `aws` section (an object). It stops with an error when `aws` is not an object (e.g. `null`) or when a key differs only by case such as `AWS` (delete the key to go without AWS).
5. Asks whether to apply; on `y` it runs `install.py --apply`. You must then type `apply` at install.py's own confirmation prompt for files to be written.

### 5. Committing and pushing afterwards

- On commit, the pre-commit and commit-msg hooks run the privacy check on files and the commit message (see "Git hooks" below).
- `git push origin main` authenticates with the stored token.

### Troubleshooting (authentication)

| Symptom | Cause | Fix |
|---|---|---|
| 403 `Write access to repository not granted` | The token lacks this repository or Contents write permission, or a different token is stored in the keychain | Check Repository access and Contents permission from step 2, or create a new token and replace it as below |
| 401 / `Invalid username or token` | Token expired or mistyped | Create a new token and replace it |

Rotating the token (macOS keychain): erase the stored entry; the next fetch or push asks for Username and Password (the new token) again.

```bash
printf 'protocol=https\nhost=github.com\n\n' | git credential-osxkeychain erase
git push origin main
```

On Linux, follow the method of the credential helper you configured (environment-dependent).

## New computer setup (Windows)

The target is Claude Code (desktop app and CLI). Codex integration, running the cross-check skill on Windows and the AWS module are not supported yet (phase 2). Run the commands yourself in a PowerShell or cmd terminal.

1. Install Git, Python and the GitHub CLI (Python 3.11 or later, a real installation rather than the Microsoft Store alias):

   ```powershell
   winget install --id Git.Git -e
   winget install --id Python.Python.3.12 -e --scope user
   winget install --id GitHub.cli -e
   ```

2. Authenticate and clone: log in with `gh auth login --web` (credentials are stored in Windows Credential Manager), then `gh repo clone <GitHub username>/ai-harness`.
3. Run `.\setup.cmd` in the cloned folder (`--no-apply` stops after the dry-run summary). The steps match `setup.sh`: repository-local commit author → `core.hooksPath=.githooks` → interactive `config.local.json` creation (owner-only access via `icacls`, AWS module off) → dry-run summary → confirmation → `install.py --apply` (type `apply` again there).
   - Why `.cmd` and not `.ps1`: the default Windows PowerShell execution policy blocks scripts. The interactive steps are handled by `tools/harness_setup.py` (`setup-windows` / `apply-windows`).
   - Non-console stdin such as pipes, redirects and AI tools is refused (`isatty()` is also true for NUL on Windows, so `GetConsoleMode` is checked).
4. Restart Claude Code to activate the hooks. See "Windows notes" below for what to check afterwards.

### Windows notes

- Line endings: `.gitattributes` pins text files to LF (so shell scripts and git hooks are not converted to CRLF even with `core.autocrlf=true`). Only `*.cmd` and `*.ps1` use CRLF.
- Hook registration: instead of the `/bin/sh` wrapper (`guard_hook.sh`), python is run directly as `"C:/.../python.exe" "C:/Users/<name>/.claude/hooks/guard.py"` (with `/` separators the path resolves the same in Git Bash and cmd). `PowerShell` and `MultiEdit` are added to the guard matcher.
- Harness paths in permission rules are rendered as `Edit(//c/<path>/hooks/**)` (drive letter as the first element, `/` separators). Command examples in skill documents use the `& "C:/.../python.exe" "..."` form (PowerShell call operator).
- `PYTHONUTF8=1` is added to `env` in `settings.json` (so non-ASCII output is not garbled when the system code page is cp932 or similar). The hook scripts also pin stdin/stdout to UTF-8 themselves.
- The git hooks (`pre-commit`, `commit-msg`) look for a working Python in Git Bash in the order `python3` → `python` → `py -3`.

## Git hooks (`.githooks/`)

They work only in repositories where `setup.sh` set `core.hooksPath`. Manual setup: `git config --local core.hooksPath .githooks`.

- **pre-commit**: `python3 tools/privacy_check.py --staged` checks every staged non-deletion change (added, modified, renamed, type-changed; for symlinks the target string). When it finds a 12-digit AWS account ID, IAM ARN, email address, AWS access key, GitHub token, `sk-` key, PRIVATE KEY header, `/Users/<name>` or `/home/<name>` path, the current user's HOME path, or a `privacy_words` entry, it prints `file:line: kind (value partially masked)` and blocks the commit.
  - There is no size limit (files are read in 1 MiB chunks and checked line by line). UTF-8/16/32 with a BOM is decoded before checking; to catch a BOM-only disguise, the raw bytes with NULs removed are also checked once more as UTF-8 and latin-1 (findings are unioned). Binaries with an early NUL (including BOM-less UTF-16) cannot be checked, so they are shown as `SKIP` and **the commit is blocked**. After checking the content, the user decides whether to use `--no-verify`.
  - Without `privacy_words`, a one-line notice says proper-noun checks only run when privacy_words is set (not blocking).
  - For a false positive, fix the patterns or allowlist in `tools/privacy_check.py` **only after the user confirms** (keep the allowlist minimal and leave the reason in a comment). Do not write test values literally in source; build them by concatenating strings at runtime.
  - `git commit --no-verify` skips it, but the user decides whether to bypass.
  - Full scan: `python3 tools/privacy_check.py --all` (all tracked files, index content; exit code 1 if any binary is present).
- **commit-msg**: `python3 tools/privacy_check.py --message <file>` checks the whole commit message with the same patterns and blocks on findings (including `#` comment lines and text below the `commit -v` scissors line, because `-m` / `-F` commits use cleanup=whitespace and keep comment lines in the body). If a comment in the editor template (e.g. the diff of `commit -v`) contains a detected value and blocks the commit, the user decides whether to edit the message or use `--no-verify`. `noreply@anthropic.com` in `Co-Authored-By` is allowed. Commit author (user.email) information is not checked.
- **post-commit**: if the new commit (compared against the empty tree for the first commit, or the first parent for a merge commit) changed `hooks/ install/ settings/ instructions/ skills/ agents/ tools/aws/`, it **only prints** one line: `[ai-harness] 설치본 반영 필요: ./apply.sh` ("apply needed"). It never runs Python, install.py or apply.sh and always exits 0 (no effect on the commit).
- **post-merge**: after `git pull` (merge or fast-forward), if the same paths changed, it **only prints** the list and how to run `./apply.sh` (including an absolute path for use outside the repository). It never runs Python or install.py. Without `config.local.json` it points to `./setup.sh`; if the aws section still holds template values it asks you to edit it. It does not run for `git pull --rebase` or `checkout`.
- Changing these hooks, the checker, `setup.sh`, `apply.sh` or `tools/harness_setup.py`, or changing `core.hooksPath`, requires user confirmation before committing (CLAUDE.md). A `config.local.json` newly created by `setup.sh` lists these paths in `extra_protected_paths` so the guard asks for approval before they are modified. **On a computer that already has `config.local.json`**, add any missing entries among `<clone>/.githooks`, `<clone>/tools/privacy_check.py`, `<clone>/tools/harness_setup.py`, `<clone>/setup.sh` and `<clone>/apply.sh` (absolute clone paths) to `extra_protected_paths` yourself and run `./apply.sh` in a terminal (a `config.local.json` created before `apply.sh` was added on 2026-11-19 lacks `apply.sh`).

## Configuration

If `setup.sh` did not create it, copy the template and edit it.

```bash
cp config.example.json config.local.json
```

| Key | Template variable | Description |
|---|---|---|
| `project_root` | `{{PROJECT_ROOT}}` | Absolute path of the main working project (`~` allowed). Used for the guard's protected paths (`<project>/.claude/settings*`) and as the cross-check working-root fallback when the cwd cannot be determined |
| (automatic) | `{{HARNESS_HOME}}` | Location of this repository |
| `memory_dir` | `{{MEMORY_DIR}}` | Default target of prune-memory. `null` means `~/.claude/projects/<project_root with non-alphanumerics replaced by ->/memory` |
| `python` | `{{PYTHON}}` | Interpreter that runs the hooks. `null` means the interpreter that ran install.py. Interpreters in a venv or inside this repository are refused |
| `response_language` | `{{RESPONSE_LANGUAGE}}` | Response language (e.g. Korean) |
| `comment_language` | `{{COMMENT_LANGUAGE}}` | Code comment language (e.g. Japanese) |
| `extra_protected_paths` | — | Additional absolute paths for which the guard asks for approval before modification |
| `github_owners` | `{{GITHUB_OWNERS}}` | Optional list of GitHub account names. The instruction to write the root README in English is installed only when it is non-empty, and applies only to repositories owned by these accounts. Not written by `setup.sh`; add it to `config.local.json` yourself |
| `aws.profile` | `{{AWS_PROFILE}}` | Read-only profile used by the AI (only with `--with-aws`) |
| `aws.assume_role_cmd` | `{{ASSUME_ROLE_CMD}}` | Absolute path of the credential refresh script, e.g. `tools/aws/assume_role.sh` |
| `aws.assume_role_envs` | `{{AWS_ENVS}}` (comma list) | Environment names for which the guard allows `--awsEnv <name>` |
| `aws.roles`, `aws.mfa_serial` | — | Per-environment role ARNs and the MFA device ARN used by `tools/aws/assume_role.py` |
| `privacy_words` | — | Optional. Per-user words added to the pre-commit and commit-msg privacy check (real names, company domains, etc., case-insensitive). install.py does not use this key |

Variables computed inside install.py: `{{SKILL_DIR}}` (each installed skill folder) and `{{AWS_MCP_TOOLS}}` (adds the aws-api MCP tools to agent tools with `--with-aws`, otherwise an empty string). Markdown blocks between `<!-- BEGIN aws -->` and `<!-- END aws -->` remain only with `--with-aws`. Installation stops if any unreplaced `{{...}}` remains.

## Installation

```bash
# Only regular files inside this repository are accepted as the config file (outside paths and links pointing outside are refused). Abbreviated options (--ap etc.) are not accepted.
# 1) dry-run: writes nothing and prints only the paths to change and line-count summaries (no file contents or secrets)
python3 install/install.py --config config.local.json [--target claude|codex|both] [--with-aws]
# 2) apply after review: the user runs it directly in a terminal and must type apply at the confirmation prompt.
#    It is always refused when stdin is not a terminal (AI tools, pipes, CI), and there is no option or environment variable to skip this.
python3 install/install.py --config config.local.json [--target ...] [--with-aws] --apply
```

Deployment targets:

- Claude: `~/.claude/hooks/{guard.py,usage_alert.py,guard_hook.sh,harness_config.json}`, `~/.claude/settings.json` (fragment merge + owned hook replacement), `~/.claude.json` (only if present: version pins for registered MCP servers, aws-api env with `--with-aws`), `~/.claude/CLAUDE.md` (marker block), `~/.claude/skills/<name>/`, `~/.claude/agents/<name>.md`
- Codex: `~/.codex/hooks/…`, `~/.codex/config.toml` (fragments + a `# BEGIN/END shared harness hooks` block), `~/.codex/AGENTS.md` (marker block including the language instructions), `~/.agents/skills/<name>/`
- The `cross-check` and `prune-memory` skill folders also get a runtime config `harness_config.json`. Skill `tests/` folders are not deployed.

Safeguards: in `settings.json`, scalars and keys from fragments are inserted **only when absent** (values the user chose, such as `defaultMode`, are kept), and lists (deny/ask/hard_deny) are extended without duplicates. If an existing value has an unexpected shape, nothing is written and the run stops. Only the read-only env of the aws-api MCP is force-overwritten. Other keys and user hooks are preserved; only owned hooks and marker blocks are replaced. Writes are atomic (temp file → rename) with mode 0600. Hooks and the config sidecar are written first, and the registration files (settings.json, config.toml) last. If another program changes a file during preparation or apply, the run stops, and on failure the files written in this run are rolled back. Pre-change files are kept in `~/.ai-harness/backups/<timestamp>/` (0700). Symlink targets are refused.

After installing:

- When hook definitions change, Codex's `trusted_hash` in `[hooks.state]` no longer matches and Codex **silently skips the hooks.** The user must re-trust them via "Trust all and continue" on the interactive `codex` start screen or the in-session hooks browser, then confirm that commands that should be blocked (e.g. reading secret files) are actually refused. Never compute or write `trusted_hash` by hand.
- Claude applies the hooks in new sessions.
- If you had manually added the same content to an existing `CLAUDE.md` or `AGENTS.md`, check the dry-run's `WARN … possible duplicate` and clean up the duplicates yourself (they are not removed automatically).

## Applying to the installed copy (apply)

Claude and Codex read the installed copies in `~/.claude`, `~/.codex` and `~/.agents`, not this repository. The installer (`install.py`) substitutes template values (`{{PYTHON}}` etc.) and merges `settings.json` and `config.toml`, so a plain copy or link cannot replace it. Repository updates (commit, pull) and applying to the installed copy are also deliberately separated so that guard and settings changes never take effect without user confirmation.

When it is needed:

- Needed: when `hooks/`, `install/`, `settings/`, `instructions/`, `skills/`, `agents/` or `tools/aws/` changed (post-commit after a commit and post-merge after a pull will tell you), or when you changed `config.local.json`.
- Not needed: `README.md`, `CLAUDE.md`, `tools/privacy_check.py`, `.githooks/`, `setup.sh`, `apply.sh` and other `tools/` (run directly from the repository or not installed).

Usage (run it yourself in a terminal from the repository folder; from elsewhere, an absolute path works too. On Windows, `.\apply.cmd` runs the same steps):

```bash
./apply.sh
```

1. Without `config.local.json` it points to `./setup.sh` and exits. Non-terminal runs (pipes, AI tools) are refused, and on Windows it points to `apply.cmd` and exits.
2. Shows `AWS module (--with-aws): on/off/placeholder`. With template (placeholder) values it asks you to edit them and exits. With `on` it adds `--with-aws`.
3. Shows the `install.py` dry-run summary (`SAME` omitted). If nothing changes it exits with "already up to date".
4. Runs `install.py --config <absolute path> [--with-aws] --apply`. You must type `apply` at install.py's own confirmation prompt for anything to be written (any other input cancels). The interpreter is `python` from `config.local.json`, otherwise a non-venv Python 3.11 or later.

Concurrent use:

- Installations are independent per computer. A push from one computer does not change another until it runs `git pull` and then `./apply.sh`.
- On the same computer, quit Claude Code and Codex before applying (if running apps write `~/.claude.json` and similar files at the same time, the apply stops or races). `apply.sh` does not detect or quit apps; it only reminds you. Do not run `apply.sh` (or `install.py --apply`) in several terminals at once.

## Updating

After updating the repository (`git pull` etc.), run `./apply.sh` (if hooks are configured, post-merge tells you whether it is needed). If nothing changed it ends with "already up to date". Skill files removed since the previous version are not deleted automatically.

## Tests

All tests run in temporary directories with a temporary HOME and never use real settings, AWS or Codex.

```bash
python3 -m unittest discover -s hooks/tests -t hooks/tests
python3 -m unittest discover -s install/tests -t install/tests
python3 -m unittest discover -s skills/cross-check/tests -t skills/cross-check/tests
python3 -m unittest discover -s tools/tests -t tools/tests
python3 -m unittest discover -s tools/investigation -t tools/investigation
python3 -m unittest discover -s tools/task_metrics -t tools/task_metrics
python3 -m unittest discover -s tools/aws -t tools/aws
```

`install/tests` runs install.py as a subprocess with the base interpreter rather than a venv (Python 3.11 or later required, `tomllib`).

On Windows, run with `python` (or `py -3`) instead of `python3`. Checks that need symlinks where they cannot be created (no developer mode or admin rights), file names Windows cannot create (`*`, `:`, newline), a pty, a FIFO or `/bin/zsh` are skipped with a reason. The `setup.sh` / `apply.sh` (sh, pty) tests are macOS/Linux only; on Windows the `setup-windows` / `apply-windows` tests cover them. `skills/cross-check/tests` is macOS only. GitHub Actions (`.github/workflows/test.yml`) runs this suite on ubuntu, macos and windows (macOS/Linux run from a `/tmp` copy whose path contains no `work`).

`tools/tests` also contains `test_privacy_check.py` (privacy check) and `test_setup.py` (setup.sh, apply.sh, `.githooks`, `harness_setup.py`). `test_setup.py` copies the working tree (without Git-excluded files) to a temporary origin, clones it locally and runs with a temporary HOME and a pty; `install.py --apply` is checked only through a stub. To run them individually:

```bash
python3 -m unittest discover -s tools/tests -t tools/tests -p 'test_privacy_check.py'
python3 -m unittest discover -s tools/tests -t tools/tests -p 'test_setup.py'
python3 tools/privacy_check.py --all
```

## Guard behavior summary

- If the config sidecar (`harness_config.json`) is missing, broken or holds invalid values (including any unexpected exception while reading it), **every tool call is refused** (fail-closed). Claude then cannot fix the hook either, so the user disables it via `/hooks` or similar, or re-runs install.py.
- Applying `install.py` (`--apply`, including path variants, interpreters, wrappers and inline execution) changes the guard settings, so it requires approval (Codex refuses). In addition to this string detection, install.py's terminal confirmation (refusing non-interactive runs, typing `apply`) is a second line of defense. Both are auxiliary defenses that can be bypassed (see "Known limitations").
- Forced protective keys in `settings.json` (`env.AWS_PROFILE` with `--with-aws`) are overwritten even when the existing value differs, and the dry-run shows `WARN … will be overwritten` (values are not printed).
- The credential refresh script is allowed only as a standalone `<aws.assume_role_cmd> --awsEnv <registered environment>` (or with `echo <6 digits> |` input); variable assignments, `env`/`export`, wrappers, interpreters or chaining with other commands require approval. `tools/aws/assume_role.py` reads only the repository's `config.local.json`.
- AWS refuses anything other than read operations, with one exception: the **four Bedrock model inference calls** (`converse`, `converse-stream`, `invoke-model` and `invoke-model-with-response-stream` of `bedrock-runtime`). The read-only profile has no inference permission, so they are allowed only through `python3 <HARNESS_HOME>/tools/aws/bedrock_run.py <command>`. The wrapper passes the user-created `~/.aws/bedrock_api_key` (a Bedrock API key of a dedicated IAM user, mode 600) as `AWS_BEARER_TOKEN_BEDROCK` and never prints the key. It can only run the four `aws bedrock-runtime` inference commands by bare name (options from an allowlist only; `--debug`, `--endpoint-url`, `--no-verify-ssl`, `--ca-bundle`, `--profile` and the like are refused) and `.py` scripts via `python`/`python3`/`uv run … python` (no `--python`/`-p`). Endpoint, proxy, CA and `AWS_CONFIG_FILE` environment variables are not passed to the child process. `AWS_PROFILE` is not changed, so if key authentication does not apply the call fails with read-only credentials. The guard decides with the same allowance as the wrapper (pinned by equivalence tests) and refuses direct calls, MCP calls, `bedrock_run.py` at other paths, runs other than the canonical form (`python3 <wrapper> <command>`), endpoint/proxy/CA/`PATH` assignments in the same command, and any mention of `AWS_BEARER_TOKEN_BEDROCK` in the command.
- Known limitations of the Bedrock wrapper: (1) Python scripts run through the wrapper receive the environment variable holding the key, so printing environment variables, boto3 debug logs (`set_stream_logger` etc., which include auth headers) or other Bedrock API calls inside the script are invisible to the guard (forbidden by policy only). **The real line of defense is restricting the dedicated IAM user's policy to `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` (+ `bedrock:CallWithBearerToken` required for API keys).** (2) The wrapper is identified by name and real path (including symlinks) and judged the same way, but a wrapper copied under another name, or paths built with globs, variables or substitutions (e.g. `bedrock_run*.py`), cannot be judged (the same string-matching limitation as the other known limitations). (3) Endpoint, proxy, `PATH` and similar changes are detected as strings only for assignments inside the command containing the wrapper; the wrapper compensates by removing endpoint, proxy, CA, `AWS_CONFIG_FILE`, `AWS_DATA_PATH` and `UV_PYTHON` from the child environment (`PATH` is not removed).
- Modifying `.claude/settings*.json` in any project requires approval.
- The Bash write check resolves each command's write targets to absolute real paths, following the hook input cwd and any `cd`, `pushd`, `eval` and `sh -c` inside the command, and compares them with protected paths. `cp`, `ln`, `install` and `rsync` are checked on the destination only, `mv` on source and destination, `chmod` and `touch` on path arguments, and the output files of `dd of=`, `curl -o`/`-c`/`-D` and `wget -O`/`-o`/`-a` are checked too. `curl -O`/`-J`/`--remote-name-all` and `wget` without `-O` are judged by the last URL segment (`index.html` for an empty name with `wget`) relative to the save folder (`curl --output-dir`, `wget -P`, otherwise the cwd); if the save folder is inside a protected directory, approval is required even when the name is unknown. With `curl --output-dir`, `-o` paths are also judged relative to that folder (absolute paths both as-is and prefixed), and `wget -r`/`-x`/`-p`/`-m` (including long names) require approval when the save folder is an ancestor of a protected path or inside the harness, because the URL path becomes the save location. `cp`, `install` and `rsync` are also judged at the actual write location (`destination/source name` when the destination is a directory); recursive copies (`-r`, `-R`, `-a`) require approval when that location is an ancestor of a protected path, and when the source is `.`, `dir/.` or ends in `/` (copying contents), when the destination itself is an ancestor of a protected path (e.g. `cp -r /tmp/x/. .` inside the harness, `rsync -a src/ ~/`). `mv`, `ln` or `chmod` on an ancestor folder of a protected path requires approval. `patch` and `git apply/checkout/restore/stash/reset --hard·--merge·--keep/pull/merge/switch/rebase/cherry-pick/revert/am` require approval when the working location is inside or an ancestor of a protected path or inside the harness (except `git pull --ff-only` and `git pull --ff-only origin main` without global options other than `-C` and without `GIT_*` assignments, which are allowed for start-of-work sync; the working location is taken from `--work-tree` / `GIT_WORK_TREE` after `-C`). Commands that change the pull source, upstream or hooks — `git remote add/set-url/rename/remove/set-branches`, `git branch -u/--set-upstream-to/-t`, and `git config` writes to `remote.*`, `branch.*`, `pull.*`, `core.*`, `url.*` or `include*` (including section changes and `--edit`) — require approval when the working location is inside the harness or an ancestor of a protected path, and `tar -x` / `unzip` require approval inside protected directories.
- If a `cd` target cannot be determined (variables, substitutions, `cd -`, etc.), later relative-path writes require approval, and when the hook input has no cwd, decisions are made on protected names. Inside the harness repository, a write target containing an unexpanded `$variable` requires approval.
- The body of a quoted-delimiter heredoc (`<<'EOF'`) is excluded from the write check only when `cat`, `tee` or `git commit/tag -F -` (including `$(cat <<'EOF')` in git messages) read it purely as data. If the same line has process substitution (`>(…)`, `<(…)`) or a pipe to another command, the body is checked too. For Bash commands over 64 KB, only the refusal checks that are linear in input length (secret file path strings, credential injection, profile changes, deployment, decryption) run first and the rest are skipped. In that case the presence of the word `aws` or a `.aws` path element means refusal, and anything else requires approval (Codex refuses), so a regex timeout never lets a command run unjudged.
- File deletion outside temporary directories and verified `__pycache__` requires approval; reading secret files (outside the `~/.aws` allowlist, Codex and Claude auth files) is refused; modifying the guard, settings or harness sources (`hooks/`, `install/`, `settings/`, `tools/aws/`, `config.local.json`) or the credential refresh script requires approval.
- AWS change, deployment and secret-printing commands are always refused. Only with `--with-aws` is the profile pinned to `aws.profile` and running the credential refresh script for registered environment names allowed as an exception. Even without the AWS module, raw credential injection (`AWS_ACCESS_KEY_ID=` etc.) and changing credential file paths are refused.

## Migration notes

The previous personal harness (the project's `tools/harness/`, the root `assume_role.sh`, and the project's `.claude/skills` and `.agents/skills`) lives outside Git, so changes made by the migration are not recorded in history. Back those files up before migrating and manage later changes through this repository's Git history. The new guard does not protect the old paths (`tools/harness/`, the root `assume_role.sh`), so while the old files remain, register them in `extra_protected_paths` or plan their cleanup (deletion is the user's decision). If the same sections were added by hand to an existing `~/.claude/CLAUDE.md` or `~/.codex/AGENTS.md`, the user cleans them up based on the dry-run's `WARN … possible duplicate`.

## Known limitations

- The guard is an **auxiliary defense that inspects command strings**, not a complete shell parser. Harmless text such as `npm run deploy` in a settings string or heredoc, a line starting with `aws …`, or a secret file path string can be blocked. When blocked, do not work around it with another tool or script; rephrase the text or use the file editing tools.
- Relative write targets in Bash are resolved against the hook input cwd (including `cd` inside the command) and compared with protected paths. Before 2026-11-19 the guard only matched absolute path strings, so writing with a relative path inside the repository folder, such as `sed -i … hooks/guard.py`, passed without approval. Still undetectable: paths built with variables or command substitution outside the harness, paths inside script files, and file writes in a `python - <<EOF` body. Also not checked: `awk -i inplace`, `CDPATH`, `cd` inside scripts loaded with `source`, other `curl` output options (`--trace`, `--etag-save`, `--libcurl`, etc.) and URL globbing (`{a,b}`, `[1-3]`), abbreviated long `wget` options (`--output-doc=` etc.), and wgetrc commands like `-e output_document=…` / `-e dir_prefix=…`. Extracting with `tar -x` or `unzip` into an ancestor of a protected path (e.g. `-C ~`) is not judged. The check is conservative (for example, a `cd` in a subshell stays a candidate afterwards) and can produce false positives. Therefore modify harness files only with the Edit/Write tools, not Bash (sed, redirects, heredocs).
- Deletion or SDK write code written inside script files cannot be caught by command inspection (for AWS, the pinned profile and read-only role are the real defense). Renamed or relocated runs such as `python3 -m install.install --apply`, globs (`install/inst*.py`) or a copied installer are not stopped by string checks. `--apply` still requires terminal confirmation in those cases, but a pseudo-terminal created with `script`, `pty`, `expect` and similar can bypass that confirmation too.
- `HARNESS_HOME` is a Git repository, so `git checkout`, `reset`, `stash`, `pull` and similar can change protected sources (`hooks/`, `install/`, `settings/`, `tools/aws/`). The guard requires approval for `git` subcommands that change the working tree (`apply`, `checkout`, `restore`, `stash`, `reset --hard` and the like, `pull`, `merge`, `switch`, `rebase`, `cherry-pick`, `revert`, `am`) based on the working location, but `git pull --ff-only` for start-of-work sync is allowed, so changes pushed to origin enter the working tree without approval. Other subcommands (`git worktree`, `git submodule update`, `git bisect`, etc.) and aliases are not checked. Also not judged: environment variables that are not prefix assignments, such as `export GIT_DIR=…` in an earlier segment; `core.worktree` of another git directory specified with `GIT_DIR` / `--git-dir` (the working location is judged by the cwd); `git config --global` run outside the harness (e.g. `url.*.insteadOf`); direct edits of `.git/config`; upstream changes via `git push -u` / `--set-upstream`; and `git config` with values starting with `-` (e.g. `core.pager -R`). For any of this to reach the installed hooks and settings, however, the user must run `install.py --apply` in a terminal.
- If the hook interpreter disappears, Claude's `guard_hook.sh` returns a refusal, but Codex runs python directly because of its trust procedure, so a missing interpreter or a hook timeout can let it run without the guard. Re-run install.py after upgrading Python.
- Some tools in Codex exec mode (paths other than MCP such as node_repl) are outside the hooks' scope.
- Guard messages and usage warnings are fixed in Korean.
- **The PowerShell check on Windows (`check_powershell`)** is a simpler string check than the Bash check. It only splits segments (`;`, `|`, `&&`, `||`, parentheses, `$(`) and handles simple quotes and backticks; it cannot judge paths via variables (`$x`), actions inside script blocks or the contents of `.ps1` files. Reinterpretation or separate-process execution that cannot be judged (`Invoke-Expression`, `-EncodedCommand`, `Start-Process`, `cmd /c`, `wsl`) requires approval. External commands (git, aws, python, etc.) reuse the Bash check. When a protected path string and a write cmdlet or `>` appear in the same segment, approval is required as a compensating measure.
- Hooks on Windows run python directly, so if the interpreter path disappears (Python removed or the path changed by an upgrade), the hook itself fails and Claude may run commands unjudged (there is no wrapper like POSIX's `guard_hook.sh`). Exceptions inside guard.py return deny via an excepthook. Re-run `apply.cmd` after changing Python.
- File checks on Windows (task_metrics, investigate, repo_context) have no `dir_fd` or `O_NOFOLLOW`, so they refuse symlinks and junctions with a per-element `lstat` before opening (a swap between the check and the open cannot be prevented). Reparse points of OneDrive cloud files are not treated as links. Permission bits (0600, 0700) mean only the read-only attribute on Windows, so only `config.local.json` is restricted to owner access via `icacls`.
- `skills/handoff` is under the MIT license (original author attribution in `skills/handoff/LICENSE.txt`).
