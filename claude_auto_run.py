"""
Claude Code 自動復旧スクリプト v2.0

設計意図: Claude Code CLIがRate Limitに到達した際に、
リセット時刻を自動パースし、適切な待機後に自律的にタスクを再開する。

使用例:
    # 対話モードで起動（Rate Limit時に自動復旧）
    python3 claude_auto_run.py

    # タスクを指定して完全自律実行
    python3 claude_auto_run.py -p "CHECKPOINT.mdを読み込んで続きを実行"

    # ダミーモードでテスト（環境変数でコマンドを上書き）
    CLAUDE_CMD=./dummy_claude.sh python3 claude_auto_run.py
"""

import pexpect
import time
import sys
import os
from datetime import datetime, timedelta

# --- 設定値 ---
# リトライ回数の上限（無限ループ防止の安全弁）
MAX_RETRIES = 10
# リセット時刻をパースできなかった場合のフォールバック待機秒数
FALLBACK_WAIT_SECONDS = 300
# Exponential Backoff の初期待機秒数
BACKOFF_BASE_SECONDS = 60
# Exponential Backoff の上限秒数
BACKOFF_MAX_SECONDS = 600


def log(msg: str) -> None:
    """タイムスタンプ付きログ出力。放置運用時に時系列追跡を可能にする。"""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")


def calc_wait_seconds(reset_time_str: str) -> int:
    """
    リセット時刻の文字列から、現在時刻との差分で待機秒数を計算する。

    対応フォーマット: "12am", "3:45PM", "12 am", "3:45 pm" 等

    使用例:
        >>> calc_wait_seconds("12am")  # 深夜0時まで待機
        1200  # (現在時刻次第)

    パース失敗時は FALLBACK_WAIT_SECONDS を返す。
    """
    now = datetime.now()
    # Claude Codeが出力する様々な時刻フォーマットに対応
    formats = ['%I:%M%p', '%I%p', '%I:%M %p', '%I %p']

    for fmt in formats:
        try:
            reset_time = datetime.strptime(
                reset_time_str.strip().upper(), fmt
            ).replace(year=now.year, month=now.month, day=now.day)

            # リセット時刻が現在より過去なら翌日と解釈
            if reset_time <= now:
                reset_time += timedelta(days=1)

            diff = int((reset_time - now).total_seconds())
            # マージン60秒を追加し、最大4時間(14400秒)でキャップ
            return min(diff + 60, 14400)
        except ValueError:
            continue

    # どのフォーマットにも一致しなかった場合
    log(f"⚠️ リセット時刻 '{reset_time_str}' をパースできませんでした。フォールバック: {FALLBACK_WAIT_SECONDS}秒")
    return FALLBACK_WAIT_SECONDS


def build_command() -> list:
    """
    実行コマンドをリスト形式で構築する。
    -p オプションがある場合はそれを除去し、対話モードで起動する。
    プロンプト内容は extract_prompt() で別途取得して送信する。
    """
    env_cmd = os.environ.get('CLAUDE_CMD')
    if env_cmd:
        return env_cmd
    # -p とその引数を除去して対話モードで起動
    args = []
    skip_next = False
    for arg in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg == '-p':
            skip_next = True  # 次の引数（プロンプト本文）もスキップ
            continue
        if arg.startswith('--output-format'):
            continue
        args.append(arg)
    return ['claude', '--dangerously-skip-permissions'] + args


def extract_prompt() -> str:
    """
    sys.argv から -p オプションの引数（タスク内容）を抽出する。
    見つからなければ空文字を返す。
    """
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == '-p' and i + 1 < len(args):
            return args[i + 1]
    return ""


class RateLimitException(Exception):
    """Rate Limitが検知されたことを通知するための内部例外"""
    pass

output_buffer = ""

def watch_output(s):
    """
    child.interact() の output_filter コールバック。
    出力文字列をバッファにためてRate Limitパターンを監視する。
    """
    global output_buffer
    text = s if isinstance(s, str) else s.decode('utf-8', errors='replace')
    output_buffer += text
    # 直近の2000文字程度を保持
    if len(output_buffer) > 2000:
        output_buffer = output_buffer[-2000:]
        
    # パターン0: リセット時刻付き
    m = re.search(r'(?:Resets at|resets|Resets in)\s+(.+?)(?:\s*\(|·|\||$)', output_buffer)
    if m:
        # 見つかったら例外を投げてinteractを脱出
        raise RateLimitException(("TIME", m.group(1).strip()))
        
    # パターン1: 汎用Rate Limit
    m2 = re.search(r'(429 Too Many Requests|rate limit|hit your limit|usage limit|limit reached|5-hour limit)', output_buffer)
    if m2:
        raise RateLimitException(("GENERIC", ""))
        
    return s


def run_claude_with_auto_retry():
    """
    Claude Codeを対話モード(interact)で起動し、ユーザーと対話可能な状態にする。
    裏で出力データを監視し、Rate Limit検知時は対話を一時中断して自動復旧ロジックに移行する。
    """
    global output_buffer
    import re  # watch_outputで使用
    globals()['re'] = re # 簡易的なスコープ対応
    
    cmd = build_command()
    initial_prompt = extract_prompt()
    log(f"🚀 [Claude Auto-Recovery v2.0] Starting: {cmd}")
    if initial_prompt:
        log(f"📝 初回プロンプト: {initial_prompt[:80]}...")

    # コマンド構築
    if isinstance(cmd, list):
        child = pexpect.spawn(cmd[0], cmd[1:], encoding='utf-8', timeout=None)
    else:
        child = pexpect.spawn(cmd, encoding='utf-8', timeout=None)

    retry_count = 0

    # 初回プロンプト送信
    if initial_prompt:
        try:
            child.expect(r'❯', timeout=30)
            time.sleep(1.0)
            child.send(initial_prompt)
            time.sleep(0.5)
            child.send(chr(13))
            time.sleep(0.5)
            child.send(chr(13))
            log("📤 初回プロンプトを送信しました。")
        except pexpect.TIMEOUT:
            log("⚠️ 起動待機中にタイムアウト。プロンプトを直接送信します。")
            child.send(initial_prompt + chr(13))

    try:
        while retry_count < MAX_RETRIES:
            output_buffer = ""  # バッファリセット
            try:
                # ユーザーが自由にキー操作できる状態（対話モード）に入る
                # 出力は watch_output 経由で stdout に表示される
                child.interact(output_filter=watch_output)
                
                # interactが正常に終了(EOF)した場合
                log("✅ プロセスが正常終了またはユーザーによって切断されました。")
                break
                
            except RateLimitException as e:
                # Rate Limit検知による強制中断
                err_type, val = e.args[0]
                
                if err_type == "TIME":
                    wait_time = calc_wait_seconds(val)
                    log(f"\n⚠️ [検知] サブスクリプション制限に達しました。リセット予定: {val}")
                    log(f"⏳ {wait_time}秒（約{wait_time // 60}分）待機して再試行します...（リトライ #{retry_count + 1}/{MAX_RETRIES}）")
                    time.sleep(wait_time)
                else:
                    # 汎用エラー（時刻未パース）
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
                            log(f"⏳ {wait_time}秒（約{wait_time // 60}分）待機して再試行します...（リトライ #{retry_count + 1}/{MAX_RETRIES}）")
                            time.sleep(wait_time)
                        else:
                            backoff = min(BACKOFF_BASE_SECONDS * (2 ** retry_count), BACKOFF_MAX_SECONDS)
                            log(f"⏳ リセット時刻不明。{backoff}秒間の Exponential Backoff...（リトライ #{retry_count + 1}/{MAX_RETRIES}）")
                            time.sleep(backoff)
                    except Exception:
                        backoff = min(BACKOFF_BASE_SECONDS * (2 ** retry_count), BACKOFF_MAX_SECONDS)
                        log(f"⏳ {backoff}秒間の Exponential Backoff...（リトライ #{retry_count + 1}/{MAX_RETRIES}）")
                        time.sleep(backoff)

                if not child.isalive():
                    log("✅ 待機中にプロセスが終了しました。")
                    break
                
                # 待機後、Claudeを再開させる
                child.send("Continue the task." + chr(13))
                retry_count += 1

            except KeyboardInterrupt:
                log("\n🛑 ユーザーによって停止されました。")
                break
        else:
            log(f"🔴 最大リトライ回数（{MAX_RETRIES}回）に達しました。終了します。")

    finally:
        child.close()
        log("🔒 プロセスをクリーンアップしました。")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    run_claude_with_auto_retry()
