"""
Claude Code 自動復旧スクリプト v2.0
（対話型 TUI & 自動待機 ハイブリッド版）

【設計意図】
Claude Code CLIが Rate Limit (429エラー) やサブスクリプション上限に達した際、
自動的にリセット時刻をパースし、適切な時間だけ待機した後に自律的にタスクを再開します。
TUI (対話画面) のUXを損なわず、制限に到達した "瞬間だけ" スクリプトが介入するよう設計されています。

【主な機能】
- TUIプロキシ機能: 普段は人間がそのまま対話できる透過的な動作環境を提供
- 正規表現による自動検知: 出力バッファを監視し、Rate Limitやリセット時刻をリアルタイムに検知
- ANSI制御文字の除去: ターミナルの文字色装飾による文字化け（検知漏れ）を防止
- セキュアな再開処理: レート制限メニューへの意図せぬキー入力を防ぐためのキャンセルロジック実装
"""

import pexpect
import time
import sys
import os
import re
from datetime import datetime, timedelta

# ==========================================
# 定数・設定値
# ==========================================

# リトライ回数の上限（無限ループ防止のための安全弁）
MAX_RETRIES = 10

# リセット時刻をパースできなかった場合（汎用Rate Limit時など）のデフォルト待機秒数
FALLBACK_WAIT_SECONDS = 300

# Exponential Backoff（指数関数的待機）の初期待機秒数と上限値
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 600

# バッファ監視機能と対話TUIの橋渡しに使用するグローバル変数
output_buffer = ""

# ==========================================
# ユーティリティ関数
# ==========================================

def log(msg: str) -> None:
    """
    タイムスタンプ付きのログ出力関数。
    長時間の放置運用時に、いつ何が起きたかの時系列追跡を可能にします。
    """
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")


def calc_wait_seconds(reset_time_str: str) -> int:
    """
    Claudeが出力した「リセット時刻の文字列」から、現在時刻との差分（待機すべき秒数）を計算します。

    対応フォーマット例: "12am", "3:45PM", "12 am", "3:45 pm"

    パースに失敗した場合は、安全のため FALLBACK_WAIT_SECONDS を返します。
    """
    now = datetime.now()
    # Claude Codeが出力する様々な時刻フォーマットに対応
    formats = ['%I:%M%p', '%I%p', '%I:%M %p', '%I %p']

    for fmt in formats:
        try:
            reset_time = datetime.strptime(
                reset_time_str.strip().upper(), fmt
            ).replace(year=now.year, month=now.month, day=now.day)

            # 抽出したリセット時刻が現在より過去（例: 現在23時でリセットが1am）の場合、翌日として扱う
            if reset_time <= now:
                reset_time += timedelta(days=1)

            diff = int((reset_time - now).total_seconds())
            
            # API側の時刻ズレを考慮し、マージンとして60秒を追加。最大4時間(14400秒)でキャップする。
            return min(diff + 60, 14400)
        except ValueError:
            continue

    # どのフォーマットにも一致しなかった時のフォールバック処理
    log(f"⚠️ リセット時刻 '{reset_time_str}' のパースに失敗。フォールバック: {FALLBACK_WAIT_SECONDS}秒待機")
    return FALLBACK_WAIT_SECONDS


# ==========================================
# プロセス管理・コマンド構築
# ==========================================

def build_command() -> list:
    """
    pexpectで起動するための実行コマンドリストを構築します。
    
    仕様:
    - `-p` オプション（スクリプトへのプロンプト指示）が含まれている場合は、
      対話モード起動のためにコマンドライン引数から除去します。
    - 環境変数 `CLAUDE_CMD` が設定されている場合は、それ（テスト用のダミースクリプト等）を優先します。
    """
    env_cmd = os.environ.get('CLAUDE_CMD')
    if env_cmd:
        return env_cmd

    args = []
    skip_next = False
    
    for arg in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        
        # -p は初回プロンプトとして扱うため除外
        if arg == '-p':
            skip_next = True
            continue
            
        # TUIモードを維持するため出力フォーマットの指定を排除
        if arg.startswith('--output-format'):
            continue
            
        args.append(arg)
        
    return ['claude', '--dangerously-skip-permissions'] + args


def extract_prompt() -> str:
    """
    実行時の引数 (sys.argv) から `-p` オプションに指定されたタスク内容を抽出します。
    自律実行（放置用）の最初の指示として使用されます。
    """
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == '-p' and i + 1 < len(args):
            return args[i + 1]
    return ""


# ==========================================
# 出力監視・例外制御
# ==========================================

class RateLimitException(Exception):
    """
    Rate Limit が検知されたことを通知するためのカスタム例外。
    child.interact() は無限ループで対話入出力を処理するため、
    検知時にこの例外を投げることで対話モードから一時的に脱出します。
    """
    pass


def watch_output(s):
    """
    pexpect の child.interact() に渡す output_filter コールバック関数。
    
    Claudeからの標準出力を逐次受け取り、内部バッファ（output_buffer）に蓄積します。
    蓄積したデータの中に Rate Limit を示す特定の文字列がないか常時監視します。
    """
    global output_buffer
    text = s if isinstance(s, str) else s.decode('utf-8', errors='replace')
    output_buffer += text
    
    # メモリ節約のため、バッファは直近の2000文字のみ保持する
    if len(output_buffer) > 2000:
        output_buffer = output_buffer[-2000:]
        
    # -------------------------------------------------------------
    # [保守ポイント] ANSIエスケープシーケンスの除去
    # Claudeからの出力テキストには文字を赤色にするなどの制御コード
    # (例: \x1b[31m ) が含まれることがあります。
    # この制御コードが混じると正規表現の文字列マッチが失敗して検知漏れに繋がるため、
    # 判定前に必ずプレーンテキスト（クリーンな状態）に変換します。
    # -------------------------------------------------------------
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean_buf = ansi_escape.sub('', output_buffer)
        
    # パターン0: 明確にリセット時刻が含まれている場合（例: "You've hit your limit · resets 5am"）
    m = re.search(r'(?:Resets at|resets|Resets in)\s+(.+?)(?:\s*\(|·|\||$)', clean_buf, re.IGNORECASE)
    if m:
        # 見つかったら例外を投げて interact のループを脱出する
        raise RateLimitException(("TIME", m.group(1).strip()))
        
    # パターン1: 汎用的なRate Limitメッセージの場合（リセット時刻が不明な場合）
    m2 = re.search(r'(429 Too Many Requests|rate limit|hit your limit|usage limit|limit reached|5-hour limit)', clean_buf, re.IGNORECASE)
    if m2:
        raise RateLimitException(("GENERIC", ""))
        
    return s


# ==========================================
# メインの実行ループ
# ==========================================

def run_claude_with_auto_retry():
    """
    メインのプロセス管理関数です。
    Claude Codeプロセスを起動し、UIを通じたインタラクティブな操作と
    裏側のRate Limit監視を並行して実行します。
    """
    global output_buffer
    
    cmd = build_command()
    initial_prompt = extract_prompt()
    
    log(f"🚀 [Claude Auto-Recovery v2.0] Starting: {cmd}")
    if initial_prompt:
        log(f"📝 初回プロンプト: {initial_prompt[:80]}...")

    # コマンドの型（リスト形式=本番環境、文字列形式=テスト用の環境変数指定）に応じてspawn
    if isinstance(cmd, list):
        child = pexpect.spawn(cmd[0], cmd[1:], encoding='utf-8', timeout=None)
    else:
        child = pexpect.spawn(cmd, encoding='utf-8', timeout=None)

    retry_count = 0

    # -------------------------------------------------------------
    # 1. 初回プロンプトの自動送信フェーズ
    # -------------------------------------------------------------
    if initial_prompt:
        try:
            # プロンプト入力欄 `❯` が表示されるまで待つ（UIの表示完了待ち）
            child.expect(r'❯', timeout=30)
            time.sleep(1.0)
            child.send(initial_prompt)
            time.sleep(0.5)
            # 送信（エンターキーを2回押すことで、複数行モード入力状態でも確実に送信実行する）
            child.send(chr(13))
            time.sleep(0.5)
            child.send(chr(13))
            log("📤 初回プロンプトを送信しました。")
        except pexpect.TIMEOUT:
            log("⚠️ 起動待機中にタイムアウト。プロンプトを直接送信します。")
            child.send(initial_prompt + chr(13))

    # -------------------------------------------------------------
    # 2. 監視・対話・復旧ループフェーズ
    # -------------------------------------------------------------
    try:
        while retry_count < MAX_RETRIES:
            output_buffer = ""  # 監視用バッファをリセット
            try:
                # ユーザーが自由にキー操作できる TUI (対話) モードを起動。
                # 同時に watch_output コールバックを挟むことで入出力を常時監視。
                child.interact(output_filter=watch_output)
                
                # ユーザーによる正常終了(Ctrl+Dなど)で interact が終了した場合
                log("✅ プロセスが正常終了またはユーザーによって切断されました。")
                break
                
            except RateLimitException as e:
                # 監視機構が Rate Limit を検知したため、対話モードから強制脱出したルート
                err_type, val = e.args[0]
                
                # 待機時間の決定
                if err_type == "TIME":
                    wait_time = calc_wait_seconds(val)
                    log(f"\n⚠️ [検知] サブスクリプション制限に達しました。リセット予定: {val}")
                    log(f"⏳ {wait_time}秒（約{wait_time // 60}分）待機して再試行します...（リトライ #{retry_count + 1}/{MAX_RETRIES}）")
                    time.sleep(wait_time)
                else:
                    # 時刻が文字列内に見当たらなかった場合は、少し待ってから探すかBackoffを行う
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

                # -------------------------------------------------------------
                # 3. 待機明けの復旧アクションフェーズ
                # -------------------------------------------------------------
                if not child.isalive():
                    log("✅ 待機中にプロセスが終了しました。")
                    break
                
                # [保守ポイント] TUIオプション表示状態のキャンセル
                # Claude Code は Usage Limit 到達時に `/rate-limit-options` コマンドを自動発動し、
                # 画面に "What do you want to do? 1. Upgrade... 2. Stop..." というメニューを表示します。
                # このメニューが表示されている状態で「Continue the task.」という文字を入力すると
                # 予期せぬ挙動を引き起こすため、必ず一度 Esc キーを送信してメニュー状態から抜けさせます。
                child.send(chr(27)) # 27 = ESCキー
                time.sleep(0.5)
                
                # 待機明けのアクション再開を指示
                child.send("Continue the task." + chr(13))
                retry_count += 1

            except KeyboardInterrupt:
                log("\n🛑 ユーザーによって手動で停止されました(Ctrl+C)。")
                break
        else:
            # MAX_RETRIES 到達時
            log(f"🔴 最大リトライ回数（{MAX_RETRIES}回）に達しました。安全のためループを終了します。")

    finally:
        # プロセスが残らないように確実にクリーンアップ処理を行う
        child.close()
        log("🔒 プロセスをクリーンアップしました。")


if __name__ == "__main__":
    # 出力がリアルタイムにフラッシュされるよう設定
    sys.stdout.reconfigure(line_buffering=True)
    run_claude_with_auto_retry()
