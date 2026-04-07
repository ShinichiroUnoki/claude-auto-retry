"""
Claude Code 自動復旧スクリプト v3.4
select() ベースの自前I/Oループで、TUI対話とRate Limit自動復旧を両立する。
Claudeが承認を求めて停止した場合も自動応答して作業を完遂させる。

アーキテクチャ:
  interactive_loop() → select([child_fd, stdin_fd], timeout=0.5)
    ├─ child_fd ready  → read → check_rate_limit → write to stdout
    │                    → detect ❯ → inject pending prompt
    │                    → detect ❯ (再出現) → auto-approve
    ├─ stdin_fd ready  → read → write to child_fd
    └─ timeout         → if rate_limit_detected: return
"""

import pexpect
import time
import sys
import os
import re
import errno
import signal
import shutil
import tty
import termios
import select
from datetime import datetime, timedelta

# ==========================================
# 定数
# ==========================================

MAX_RETRIES = 10
FALLBACK_WAIT_SECONDS = 300
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 600

# WHY: 短すぎるとCPU消費が増え、長すぎるとRate Limit検知の反応が遅れる
SELECT_TIMEOUT = 0.5

# WHY: プロンプト注入後、Claudeが承認を求めて ❯ を再表示した場合に自動応答する。
# 連続自動応答の上限を設けて無限ループを防止する。
MAX_AUTO_APPROVALS = 5
AUTO_APPROVE_MSG = "はい、承認します。全て自動判断で最後まで進めてください。確認は不要です。"

ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
RATE_LIMIT_TIME_RE = re.compile(
    r'(?:Resets at|resets|Resets in)\s+(.+?)(?:\s*\(|·|\||$)',
    re.IGNORECASE
)
RATE_LIMIT_GENERIC_RE = re.compile(
    r'(429 Too Many Requests|rate limit|hit your limit|usage limit|limit reached|5-hour limit)',
    re.IGNORECASE
)
# WHY: ステータスバーの「You've used 90% of your session limit · resets 8pm」は
# rate limitではなく使用率の表示。これを誤検知しないように除外する。
# "resets Xpm" 部分まで含めて除去しないと、残った "resets 8pm" がTIMEパターンに誤マッチする。
USAGE_PERCENT_RE = re.compile(
    r"you've used \d+%\s+of your session limit[^\n]*",
    re.IGNORECASE
)

_RATE_LIMIT_KEYWORDS = ('rate', 'limit', '429', 'resets', 'hit your')

# パーミッションダイアログ検知用キーワード
_PERMISSION_KEYWORDS = ('do you want to proceed', 'requested permissions')

# 自律実行指示（全プロンプトの末尾に付加）
AUTONOMOUS_SUFFIX = " 承認確認は不要です。全て自動判断で最後まで進めてください。"

# ==========================================
# Rate Limit 検知状態
# ==========================================

_rate_limit_detected = False
_rate_limit_type = None
_rate_limit_value = ""
_output_buffer = ""


def reset_rate_limit_state():
    global _rate_limit_detected, _rate_limit_type, _rate_limit_value, _output_buffer
    _rate_limit_detected = False
    _rate_limit_type = None
    _rate_limit_value = ""
    _output_buffer = ""


# ==========================================
# ユーティリティ
# ==========================================

def log(msg: str) -> None:
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")


def calc_wait_seconds(reset_time_str: str) -> int:
    """リセット時刻文字列から待機秒数を計算する。パース失敗時はフォールバック値を返す。"""
    now = datetime.now()
    formats = ['%I:%M%p', '%I%p', '%I:%M %p', '%I %p']

    for fmt in formats:
        try:
            reset_time = datetime.strptime(
                reset_time_str.strip().upper(), fmt
            ).replace(year=now.year, month=now.month, day=now.day)

            if reset_time <= now:
                elapsed = (now - reset_time).total_seconds()
                if elapsed < 3600:
                    log(f"🕐 リセット時刻 {reset_time_str} は{int(elapsed)}秒前に通過済み。短時間待機で再試行。")
                    return 60
                reset_time += timedelta(days=1)

            diff = int((reset_time - now).total_seconds())
            return min(diff + 60, 14400)
        except ValueError:
            continue

    log(f"⚠️ リセット時刻 '{reset_time_str}' のパースに失敗。フォールバック: {FALLBACK_WAIT_SECONDS}秒待機")
    return FALLBACK_WAIT_SECONDS


def calc_backoff(retry_count: int) -> int:
    return min(BACKOFF_BASE_SECONDS * (2 ** retry_count), BACKOFF_MAX_SECONDS)


def wait_and_log(seconds: int, retry_count: int) -> None:
    log(f"⏳ {seconds}秒（約{seconds // 60}分）待機して再試行します...（リトライ #{retry_count + 1}/{MAX_RETRIES}）")
    time.sleep(seconds)


# ==========================================
# SIGWINCH転送
# ==========================================

def _sync_terminal_size(child):
    try:
        size = shutil.get_terminal_size()
        child.setwinsize(size.lines, size.columns)
    except (OSError, ValueError):
        pass


def setup_sigwinch_handler(child):
    """
    WHY: Claude CodeのTUIは端末サイズに依存して描画するため、
    リサイズが伝わらないと表示が崩壊する。
    """
    signal.signal(signal.SIGWINCH, lambda sig, frame: _sync_terminal_size(child))
    _sync_terminal_size(child)


# ==========================================
# コマンド構築・引数パース
# ==========================================

def parse_args() -> tuple:
    """
    sys.argvをパースし、(command: list, prompt: str) を返す。
    -p オプションは初回プロンプトとして抽出し、claude起動コマンドからは除外する。
    """
    env_cmd = os.environ.get('CLAUDE_CMD')

    prompt = ""
    cmd_args = []
    skip_next = False

    for i, arg in enumerate(sys.argv[1:]):
        if skip_next:
            skip_next = False
            continue

        if arg == '-p':
            skip_next = True
            if i + 1 < len(sys.argv[1:]):
                prompt = sys.argv[1:][i + 1]
            continue

        if arg.startswith('--output-format'):
            continue

        cmd_args.append(arg)

    if env_cmd:
        cmd = [env_cmd]
    else:
        cmd = ['claude', '--dangerously-skip-permissions'] + cmd_args

    return cmd, prompt


# ==========================================
# 出力監視
# ==========================================

def check_rate_limit(data_bytes):
    """
    子プロセス出力をバッファに蓄積し、Rate Limitパターンを検知する。
    WHY: 例外を投げずフラグを立てるだけにすることで、
    I/Oループのtry/finallyによるTTY復元を妨げない。
    """
    global _output_buffer, _rate_limit_detected, _rate_limit_type, _rate_limit_value

    text = data_bytes.decode('utf-8', errors='replace')

    text_lower = text.lower()
    needs_full_check = any(kw in text_lower for kw in _RATE_LIMIT_KEYWORDS)

    _output_buffer += text
    if len(_output_buffer) > 2000:
        _output_buffer = _output_buffer[-2000:]

    if not needs_full_check:
        return

    clean_buf = ANSI_ESCAPE_RE.sub('', _output_buffer)

    # WHY: ステータスバーの使用率表示（例: "You've used 90% of your session limit · resets 8pm"）
    # に含まれる "resets 8pm" を誤検知しないよう、使用率表示行を除外してからパターンマッチする。
    clean_buf_filtered = USAGE_PERCENT_RE.sub('', clean_buf)

    m = RATE_LIMIT_TIME_RE.search(clean_buf_filtered)
    if m:
        _rate_limit_type = "TIME"
        _rate_limit_value = m.group(1).strip()
        _rate_limit_detected = True
        return

    m2 = RATE_LIMIT_GENERIC_RE.search(clean_buf_filtered)
    if m2:
        _rate_limit_type = "GENERIC"
        _rate_limit_value = ""
        _rate_limit_detected = True


# ==========================================
# 自前 I/O ループ（プロンプト注入 + 自動応答機能付き）
# ==========================================

def interactive_loop(child, pending_prompt=None):
    """
    pexpect.interact() の代替。select() + タイムアウトで対話とRate Limit検知を両立する。

    pending_prompt が指定された場合、TUI出力内で ❯ を検知した時点で
    プロンプトを自動注入する。注入後に ❯ が再出現した場合は、
    Claudeが承認を求めて停止したと判断し、自動応答を送信する。

    Returns:
        "RATE_LIMIT" or "EXIT"
    """
    stdin_fd = sys.stdin.fileno()
    child_fd = child.child_fd
    stdout_fd = sys.stdout.fileno()

    prompt_injected = False
    prompt_ready_counter = -1  # -1 = ❯ 未検知

    # 自動応答: プロンプト注入後に ❯ が再出現した場合の処理
    auto_approve_count = 0
    # WHY: ❯ 検知後すぐに応答せず、Claudeが完全に出力を終えるまで待つ。
    # 処理中の中間出力に ❯ が含まれる場合の誤検知を防ぐ。
    auto_approve_idle_cycles = 0
    IDLE_THRESHOLD = 6  # 3秒間(0.5秒×6)出力がなければ「入力待ち」と判断

    old_settings = termios.tcgetattr(stdin_fd)
    try:
        tty.setraw(stdin_fd)

        while child.isalive():
            if _rate_limit_detected:
                return "RATE_LIMIT"

            # --- Phase 1: 初回プロンプト注入 ---
            if pending_prompt and not prompt_injected and prompt_ready_counter >= 0:
                prompt_ready_counter += 1
                if prompt_ready_counter >= 3:
                    full_prompt = pending_prompt + AUTONOMOUS_SUFFIX
                    os.write(child_fd, full_prompt.encode('utf-8'))
                    time.sleep(0.3)
                    os.write(child_fd, b'\r')
                    time.sleep(0.3)
                    os.write(child_fd, b'\r')
                    prompt_injected = True
                    log("📤 プロンプトを送信しました。")

            # --- Phase 2: 自動応答（Claudeが承認待ちで停止した場合） ---
            if prompt_injected and auto_approve_idle_cycles > 0:
                auto_approve_idle_cycles += 1
                if auto_approve_idle_cycles >= IDLE_THRESHOLD:
                    if auto_approve_count < MAX_AUTO_APPROVALS:
                        auto_approve_count += 1
                        log(f"🤖 自動応答 #{auto_approve_count}: Claudeが入力待ちのため承認を送信")
                        os.write(child_fd, AUTO_APPROVE_MSG.encode('utf-8'))
                        time.sleep(0.3)
                        os.write(child_fd, b'\r')
                        time.sleep(0.3)
                        os.write(child_fd, b'\r')
                    else:
                        log(f"⚠️ 自動応答上限({MAX_AUTO_APPROVALS}回)に達しました。手動入力をお待ちします。")
                    auto_approve_idle_cycles = 0

            try:
                r, _, _ = select.select([child_fd, stdin_fd], [], [], SELECT_TIMEOUT)
            except (select.error, OSError) as e:
                err_code = e.args[0] if hasattr(e, 'args') else e.errno
                if err_code == errno.EINTR:
                    continue
                raise

            if child_fd in r:
                try:
                    data = os.read(child_fd, 4096)
                except OSError:
                    return "EXIT"
                if not data:
                    return "EXIT"

                check_rate_limit(data)

                clean = ANSI_ESCAPE_RE.sub('', data.decode('utf-8', errors='replace'))
                clean_lower = clean.lower()

                # --- パーミッションダイアログの自動承認 ---
                # WHY: --dangerously-skip-permissions でも "sensitive file" 系の
                # パーミッション確認は表示される。メニュー形式（1. Yes / 2. ... / 3. No）
                # なのでEnterキーのみ送信してデフォルト選択（Yes）を実行する。
                if any(kw in clean_lower for kw in _PERMISSION_KEYWORDS):
                    log("🔓 パーミッション確認を自動承認")
                    time.sleep(0.5)
                    os.write(child_fd, b'\r')

                # Phase 1: 初回 ❯ 検知 → プロンプト注入準備
                if pending_prompt and not prompt_injected and prompt_ready_counter < 0:
                    if '❯' in clean:
                        prompt_ready_counter = 0

                # Phase 2: プロンプト注入済みで ❯ 再出現 → 自動応答カウント開始
                if prompt_injected and '❯' in clean:
                    auto_approve_idle_cycles = 1  # カウント開始

                # 出力があったらアイドルカウントをリセット
                if auto_approve_idle_cycles > 0 and '❯' not in clean and len(clean.strip()) > 0:
                    auto_approve_idle_cycles = 0

                os.write(stdout_fd, data)

            else:
                # child_fd からの出力がない（タイムアウト）→ アイドルサイクル加算
                # WHY: ❯ 検知後に出力が止まった = Claudeがユーザー入力を待っている
                pass

            if stdin_fd in r:
                try:
                    data = os.read(stdin_fd, 4096)
                except OSError:
                    return "EXIT"
                if not data:
                    return "EXIT"

                # ユーザーが手動入力したら自動応答カウントをリセット
                auto_approve_idle_cycles = 0
                os.write(child_fd, data)

        return "EXIT"

    finally:
        termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_settings)


# ==========================================
# メインループ
# ==========================================

DEFAULT_RECOVERY_PROMPT = "/session-recover /assemble-team"


def run_claude_with_auto_retry():
    cmd, initial_prompt = parse_args()

    if not initial_prompt:
        initial_prompt = DEFAULT_RECOVERY_PROMPT
        log(f"📝 -p 未指定。デフォルトプロンプト: {initial_prompt}")

    log(f"🚀 [Claude Auto-Recovery v3.4] Starting: {cmd}")
    log(f"📝 初回プロンプト: {initial_prompt[:80]}...")

    child = pexpect.spawn(cmd[0], cmd[1:], encoding='utf-8', timeout=None)
    setup_sigwinch_handler(child)

    retry_count = 0
    next_prompt = initial_prompt

    try:
        while retry_count < MAX_RETRIES:
            reset_rate_limit_state()

            try:
                result = interactive_loop(child, pending_prompt=next_prompt)
                next_prompt = None

                if result == "EXIT":
                    log("✅ プロセスが正常終了またはユーザーによって切断されました。")
                    break

                # Rate Limit 検知
                if _rate_limit_type == "TIME":
                    wait_time = calc_wait_seconds(_rate_limit_value)
                    log(f"\n⚠️ [検知] サブスクリプション制限に達しました。リセット予定: {_rate_limit_value}")
                    wait_and_log(wait_time, retry_count)
                else:
                    log(f"\n⚠️ [検知] Rate Limit が発生しました。リセット時刻を探索中...")
                    try:
                        time_idx = child.expect([
                            r'resets?\s+(?:at\s+|in\s+)?(\d{1,2}(?::\d{2})?\s*[apAP][mM])',
                            r'\r?\n',
                            pexpect.TIMEOUT
                        ], timeout=3)

                        if time_idx == 0:
                            reset_time_str = child.match.group(1).strip()
                            wait_time = calc_wait_seconds(reset_time_str)
                            log(f"🕐 リセット時刻を検出: {reset_time_str}")
                            wait_and_log(wait_time, retry_count)
                        else:
                            backoff = calc_backoff(retry_count)
                            log(f"⏳ リセット時刻不明。{backoff}秒間の Exponential Backoff...（リトライ #{retry_count + 1}/{MAX_RETRIES}）")
                            time.sleep(backoff)
                    except Exception:
                        backoff = calc_backoff(retry_count)
                        log(f"⏳ {backoff}秒間の Exponential Backoff...（リトライ #{retry_count + 1}/{MAX_RETRIES}）")
                        time.sleep(backoff)

                # 復旧アクション
                if not child.isalive():
                    log("✅ 待機中にプロセスが終了しました。")
                    break

                os.write(child.child_fd, b'\x1b')
                time.sleep(2.0)

                next_prompt = DEFAULT_RECOVERY_PROMPT
                retry_count += 1
                log(f"🔄 復旧準備完了。（リトライ #{retry_count}/{MAX_RETRIES}）")

            except KeyboardInterrupt:
                log("\n🛑 ユーザーによって手動で停止されました(Ctrl+C)。")
                break
        else:
            log(f"🔴 最大リトライ回数（{MAX_RETRIES}回）に達しました。安全のためループを終了します。")

    finally:
        child.close()
        log("🔒 プロセスをクリーンアップしました。")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    run_claude_with_auto_retry()
