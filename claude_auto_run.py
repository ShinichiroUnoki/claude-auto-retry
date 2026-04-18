"""
Claude Code 自動復旧スクリプト v3.6
select() ベースの自前I/Oループで、TUI対話とRate Limit自動復旧を両立する。
Claudeが承認を求めて停止した場合も自動応答して作業を完遂させる。

アーキテクチャ:
  interactive_loop() → select([child_fd, stdin_fd], timeout=0.5)
    ├─ child_fd ready  → read → RateLimitDetector.feed()
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
# 上限なしで完全自動応答する（rate limit検知で自動停止するため無限ループにはならない）。
AUTO_APPROVE_MSG = "はい、承認します。全て自動判断で最後まで進めてください。確認は不要です。"

ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# 自律実行指示（全プロンプトの末尾に付加）
AUTONOMOUS_SUFFIX = "承認確認は不要です。全て自動判断で最後まで進めてください。"

# パーミッションダイアログ検知用キーワード
_PERMISSION_KEYWORDS = ('do you want to proceed', 'requested permissions')

DEFAULT_RECOVERY_PROMPT = "/session-recover /assemble-team"


# ==========================================
# RateLimitDetector: Rate Limit検知をカプセル化
# ==========================================

class RateLimitDetector:
    """
    子プロセス出力をバッファに蓄積し、Rate Limitパターンを検知する。

    WHY: グローバル変数による状態管理を排除し、テスト容易性と
    再利用性を向上させる。reset()で状態をクリアしてリトライに備える。
    """

    _TIME_RE = re.compile(
        r'(?:Resets at|resets|Resets in)\s+(.+?)(?:\s*\(|·|\||$)',
        re.IGNORECASE,
    )
    _GENERIC_RE = re.compile(
        r'(429 Too Many Requests|rate limit|hit your limit|usage limit|limit reached|5-hour limit)',
        re.IGNORECASE,
    )
    # WHY: ステータスバーの「You've used 90% of your session limit · resets 8pm」は
    # rate limitではなく使用率の表示。これを誤検知しないように除外する。
    _USAGE_PERCENT_RE = re.compile(
        r"you've used \d+%\s+of your session limit[^\n]*",
        re.IGNORECASE,
    )
    _KEYWORDS = ('rate', 'limit', '429', 'resets', 'hit your')
    _BUFFER_MAX = 2000

    def __init__(self):
        self.detected = False
        self.type = None       # "TIME" | "GENERIC" | None
        self.value = ""        # TIMEの場合のリセット時刻文字列
        self._buffer = ""

    def reset(self):
        self.detected = False
        self.type = None
        self.value = ""
        self._buffer = ""

    def feed(self, data_bytes: bytes) -> None:
        """出力データを受け取り、Rate Limitパターンを検査する。"""
        text = data_bytes.decode('utf-8', errors='replace')

        self._buffer += text
        if len(self._buffer) > self._BUFFER_MAX:
            self._buffer = self._buffer[-self._BUFFER_MAX:]

        # WHY: 全出力に正規表現を適用するのはコストが高いため、
        # まずキーワードで高速フィルタリングする。
        if not any(kw in text.lower() for kw in self._KEYWORDS):
            return

        clean = ANSI_ESCAPE_RE.sub('', self._buffer)
        # 使用率表示行を除外してからパターンマッチする
        clean = self._USAGE_PERCENT_RE.sub('', clean)

        m = self._TIME_RE.search(clean)
        if m:
            self.type = "TIME"
            self.value = m.group(1).strip()
            self.detected = True
            return

        if self._GENERIC_RE.search(clean):
            self.type = "GENERIC"
            self.value = ""
            self.detected = True


# モジュールレベルのdetectorインスタンス（interactive_loop / run_claude_with_auto_retryが共有）
_detector = RateLimitDetector()


# ==========================================
# ユーティリティ
# ==========================================

def log(msg: str) -> None:
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")


def log_raw(fd: int, msg: str) -> None:
    """
    raw modeのターミナル内からログを出力する。

    WHY: tty.setraw() 中は行末の \n が \r\n に変換されないため、
    通常の print() を使うと出力がスタイルしてしまう（staircase）。
    os.write() で \r\n を明示することで正常に改行する。
    """
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}\r\n"
    os.write(fd, line.encode('utf-8', errors='replace'))


def send_input(fd: int, message: bytes) -> None:
    """
    子プロセスにメッセージ＋Enter×2を送信する。

    WHY: プロンプト注入・自動応答・パーミッション承認で同じ送信パターンが
    繰り返されていたため、単一関数に統合して重複を排除する。
    """
    os.write(fd, message)
    time.sleep(0.3)
    os.write(fd, b'\r')
    time.sleep(0.3)
    os.write(fd, b'\r')


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


def _drain_child_fd(fd: int, timeout: float = 2.0) -> None:
    """
    ptyバッファに蓄積された古い出力を読み捨てる。

    WHY: Rate Limit待機中にClaude CodeのTUIが溜めたANSIエスケープシーケンスを
    次のinteractive_loop開始前に破棄し、表示崩れとプロンプト検知失敗を防ぐ。
    0.1秒間出力がなければバッファが空と判断して終了する。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r, _, _ = select.select([fd], [], [], 0.1)
            if fd not in r:
                break
            data = os.read(fd, 4096)
            if not data:
                break
        except OSError:
            break


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

    auto_approve_count = 0
    # WHY: ❯ 検知後すぐに応答せず、Claudeが完全に出力を終えるまで待つ。
    # 処理中の中間出力に ❯ が含まれる場合の誤検知を防ぐ。
    auto_approve_idle_cycles = 0
    IDLE_THRESHOLD = 6  # 3秒間(0.5秒×6)出力がなければ「入力待ち」と判断

    old_settings = termios.tcgetattr(stdin_fd)
    try:
        tty.setraw(stdin_fd)

        while child.isalive():
            if _detector.detected:
                return "RATE_LIMIT"

            # --- Phase 1: 初回プロンプト注入 ---
            if pending_prompt and not prompt_injected and prompt_ready_counter >= 0:
                prompt_ready_counter += 1
                if prompt_ready_counter >= 3:
                    full_prompt = pending_prompt + " " + AUTONOMOUS_SUFFIX
                    send_input(child_fd, full_prompt.encode('utf-8'))
                    prompt_injected = True
                    log_raw(stdout_fd, "📤 プロンプトを送信しました。")

            # --- Phase 2: 自動応答（Claudeが承認待ちで停止した場合） ---
            if prompt_injected and auto_approve_idle_cycles > 0:
                auto_approve_idle_cycles += 1
                if auto_approve_idle_cycles >= IDLE_THRESHOLD:
                    auto_approve_count += 1
                    log_raw(stdout_fd, f"🤖 自動応答 #{auto_approve_count}: Claudeが入力待ちのため承認を送信")
                    send_input(child_fd, AUTO_APPROVE_MSG.encode('utf-8'))
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

                _detector.feed(data)

                clean = ANSI_ESCAPE_RE.sub('', data.decode('utf-8', errors='replace'))
                clean_lower = clean.lower()

                # --- パーミッションダイアログの自動承認 ---
                # WHY: --dangerously-skip-permissions でも "sensitive file" 系の
                # パーミッション確認は表示される。Enterキーでデフォルト選択(Yes)を実行する。
                if any(kw in clean_lower for kw in _PERMISSION_KEYWORDS):
                    log_raw(stdout_fd, "🔓 パーミッション確認を自動承認")
                    time.sleep(0.5)
                    os.write(child_fd, b'\r')

                # Phase 1: 初回 ❯ 検知 → プロンプト注入準備
                if pending_prompt and not prompt_injected and prompt_ready_counter < 0:
                    if '❯' in clean:
                        prompt_ready_counter = 0

                # Phase 2: プロンプト注入済みで ❯ 再出現 → 自動応答カウント開始
                if prompt_injected and '❯' in clean:
                    auto_approve_idle_cycles = 1

                # 出力があったらアイドルカウントをリセット
                if auto_approve_idle_cycles > 0 and '❯' not in clean and len(clean.strip()) > 0:
                    auto_approve_idle_cycles = 0

                os.write(stdout_fd, data)

            if stdin_fd in r:
                try:
                    data = os.read(stdin_fd, 4096)
                except OSError:
                    return "EXIT"
                if not data:
                    return "EXIT"

                auto_approve_idle_cycles = 0
                os.write(child_fd, data)

        return "EXIT"

    finally:
        termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_settings)


# ==========================================
# Rate Limit 待機処理
# ==========================================

def _handle_rate_limit_wait(child, retry_count: int) -> None:
    """
    Rate Limit検知後の待機処理。待機秒数を計算して sleep する。

    WHY: run_claude_with_auto_retry() から待機ロジックを抽出して
    メインループのネストを浅くし、可読性を向上させる。
    """
    if _detector.type == "TIME":
        wait_time = calc_wait_seconds(_detector.value)
        log(f"\n⚠️ [検知] サブスクリプション制限に達しました。リセット予定: {_detector.value}")
        wait_and_log(wait_time, retry_count)
    else:
        # WHY: interactive_loop が child_fd を os.read() で直接消費済みのため、
        # child.expect() でリセット時刻を後から探索しても必ずタイムアウトになる。
        # リセット時刻は RateLimitDetector がリアルタイムに解析するため、
        # ここでは潔く Exponential Backoff にフォールバックする。
        backoff = calc_backoff(retry_count)
        log(f"\n⚠️ [検知] Rate Limit が発生しました（リセット時刻不明）。{backoff}秒の Exponential Backoff...（リトライ #{retry_count + 1}/{MAX_RETRIES}）")
        time.sleep(backoff)


# ==========================================
# メインループ
# ==========================================

def run_claude_with_auto_retry():
    cmd, initial_prompt = parse_args()

    if not initial_prompt:
        initial_prompt = DEFAULT_RECOVERY_PROMPT
        log(f"📝 -p 未指定。デフォルトプロンプト: {initial_prompt}")

    log(f"🚀 [Claude Auto-Recovery v3.6] Starting: {cmd}")
    log(f"📝 初回プロンプト: {initial_prompt[:80]}...")

    child = pexpect.spawn(cmd[0], cmd[1:], encoding='utf-8', timeout=None)
    setup_sigwinch_handler(child)

    retry_count = 0
    next_prompt = initial_prompt

    try:
        while retry_count < MAX_RETRIES:
            _detector.reset()

            try:
                result = interactive_loop(child, pending_prompt=next_prompt)
                next_prompt = None

                if result == "EXIT":
                    log("✅ プロセスが正常終了またはユーザーによって切断されました。")
                    break

                _handle_rate_limit_wait(child, retry_count)

                if not child.isalive():
                    log("✅ 待機中にプロセスが終了しました。")
                    break

                # WHY: 正しい順序は drain→ESC→即loop。
                # 1) drain: 225分間のTUI更新でPTYバッファが4KB満杯になっている可能性があり、
                #    Claudeのwrite()がブロック中かもしれない。drainでunblockしつつ古い出力を捨てる。
                # 2) ESC: Rate Limitオーバーレイを閉じる正しいキー。\r(Enter)ではなく\x1b。
                #    \rを使うと空メッセージとして送信されClaudeが不要な応答を生成してしまう。
                # 3) 即interactive_loop: sleep不要。
                #    loop内でリアルタイムにchild_fdを読むのでANSIシーケンスが噴出しない。
                _drain_child_fd(child.child_fd, timeout=2.0)
                os.write(child.child_fd, b'\x1b')

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
