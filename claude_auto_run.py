"""
Claude Code 自動復旧スクリプト v3.2
select() ベースの自前I/Oループで、TUI対話とRate Limit自動復旧を両立する。

アーキテクチャ:
  interactive_loop() → select([child_fd, stdin_fd], timeout=0.5)
    ├─ child_fd ready  → read → check_rate_limit → write to stdout
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

ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
RATE_LIMIT_TIME_RE = re.compile(
    r'(?:Resets at|resets|Resets in)\s+(.+?)(?:\s*\(|·|\||$)',
    re.IGNORECASE
)
RATE_LIMIT_GENERIC_RE = re.compile(
    r'(429 Too Many Requests|rate limit|hit your limit|usage limit|limit reached|5-hour limit)',
    re.IGNORECASE
)

# 2段階フィルタ用: フルバッファ検査を省略するための軽量キーワード
_RATE_LIMIT_KEYWORDS = ('rate', 'limit', '429', 'resets', 'hit your')

# ==========================================
# Rate Limit 検知状態
# ==========================================

_rate_limit_detected = False
_rate_limit_type = None   # "TIME" or "GENERIC"
_rate_limit_value = ""
_output_buffer = ""


def reset_rate_limit_state():
    """Rate Limit検知の内部状態をリセットする。"""
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
    """親ターミナルのサイズを子プロセスに同期する。"""
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

    # 2段階フィルタ: 新チャンクにキーワードが含まれない場合はフルバッファ検査を省略
    # WHY: select()ループは高頻度で回るため、毎回2000文字のANSI strip + regexは無駄
    text_lower = text.lower()
    needs_full_check = any(kw in text_lower for kw in _RATE_LIMIT_KEYWORDS)

    _output_buffer += text
    if len(_output_buffer) > 2000:
        _output_buffer = _output_buffer[-2000:]

    if not needs_full_check:
        return

    clean_buf = ANSI_ESCAPE_RE.sub('', _output_buffer)

    m = RATE_LIMIT_TIME_RE.search(clean_buf)
    if m:
        _rate_limit_type = "TIME"
        _rate_limit_value = m.group(1).strip()
        _rate_limit_detected = True
        return

    m2 = RATE_LIMIT_GENERIC_RE.search(clean_buf)
    if m2:
        _rate_limit_type = "GENERIC"
        _rate_limit_value = ""
        _rate_limit_detected = True


# ==========================================
# 自前 I/O ループ
# ==========================================

def interactive_loop(child):
    """
    pexpect.interact() の代替。select() + タイムアウトで対話とRate Limit検知を両立する。

    WHY: pexpect.interact()は内部のselect()ループに外部から介入できない設計のため、
    Rate Limit検知時に安全にループを脱出する手段がない。

    Returns:
        "RATE_LIMIT" or "EXIT"
    """
    stdin_fd = sys.stdin.fileno()
    child_fd = child.child_fd
    stdout_fd = sys.stdout.fileno()

    old_settings = termios.tcgetattr(stdin_fd)
    try:
        tty.setraw(stdin_fd)

        while child.isalive():
            if _rate_limit_detected:
                return "RATE_LIMIT"

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
                os.write(stdout_fd, data)

            if stdin_fd in r:
                try:
                    data = os.read(stdin_fd, 4096)
                except OSError:
                    return "EXIT"
                if not data:
                    return "EXIT"

                os.write(child_fd, data)

        return "EXIT"

    finally:
        termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, old_settings)


# ==========================================
# メインループ
# ==========================================

def run_claude_with_auto_retry():
    cmd, initial_prompt = parse_args()

    log(f"🚀 [Claude Auto-Recovery v3.2] Starting: {cmd}")
    if initial_prompt:
        log(f"📝 初回プロンプト: {initial_prompt[:80]}...")

    child = pexpect.spawn(cmd[0], cmd[1:], encoding='utf-8', timeout=None)
    setup_sigwinch_handler(child)

    retry_count = 0

    # 初回プロンプトの自動送信
    if initial_prompt:
        try:
            child.expect(r'❯', timeout=30)
            time.sleep(1.0)
            child.send(initial_prompt)
            # WHY: ファイルパスを含むプロンプトの場合、Claude Code TUIが
            # ファイル参照として非同期描画する。描画完了前にEnterを送ると
            # 送信トリガーとして認識されない。2秒あれば描画が完了する。
            time.sleep(2.0)
            child.send(chr(13))
            time.sleep(0.5)
            child.send(chr(13))
            log("📤 初回プロンプトを送信しました。")
        except pexpect.TIMEOUT:
            log("⚠️ 起動待機中にタイムアウト。プロンプトを直接送信します。")
            child.send(initial_prompt)
            time.sleep(2.0)
            child.send(chr(13))
            time.sleep(0.5)
            child.send(chr(13))

    # WHY: expect()後にpexpect内部バッファに残ったデータを画面に出す。
    # 自前I/Oループに切り替えるとpexpectのバッファは読まれなくなるため。
    try:
        remaining = child.before or ""
        if remaining and isinstance(remaining, str):
            sys.stdout.write(remaining)
            sys.stdout.flush()
    except Exception:
        pass

    # 監視・対話・復旧ループ
    try:
        while retry_count < MAX_RETRIES:
            reset_rate_limit_state()

            try:
                result = interactive_loop(child)

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

                # WHY: Claude Code は Rate Limit 時にメニューを表示する。
                # ESCでメニュー状態から抜ける。
                child.send(chr(27))
                time.sleep(1.5)

                # WHY: ESCと"C"の間隔が短いと、ターミナルがESCシーケンス(\x1BC)として
                # 解釈し、"C"が消失する。初回プロンプトと同じ段階的送信方式を使う。
                child.send("Continue the task.")
                time.sleep(0.5)
                child.send(chr(13))
                time.sleep(0.5)
                child.send(chr(13))
                retry_count += 1
                log(f"🔄 復旧指示を送信しました。（リトライ #{retry_count}/{MAX_RETRIES}）")

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
