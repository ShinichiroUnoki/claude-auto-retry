# Claude Auto-Recovery v3.3 🚀

Claude Code CLI を利用する際、APIのRate Limit（429エラー）や、サブスクリプションの上限制限（"resets 12am" 等）に達した際に、**自動で待機し、制限解除後に自律的に機能再開**を行うラッパースクリプトです。

## ✨ 特徴

- 🕐 **リセット時刻の自動パース**: "resets 3:45PM" などの文字列から待機時間を計算。リセット済み（直近1時間以内の過去）なら60秒で即リトライ。
- ⏳ **Exponential Backoff**: 汎用Rate Limitエラーに対して指数関数的（1分→2分→4分...最大10分）に待機。
- 💻 **対話×自動のハイブリッド**: Claude CodeのTUIを完全に利用可能。制限到達時のみスクリプトが介入し、待機明けに自動再開。
- 🔄 **プロンプト自動注入**: TUI出力内で `❯` を検知してからプロンプトを送信。TUI初期化完了を保証。
- 📐 **ターミナルリサイズ対応**: SIGWINCHを子プロセスに転送。
- 🔒 **TTY安全性**: `try/finally` でraw mode設定を確実に復元。異常終了時もターミナルが壊れない。

## 📦 前提環境

- Python 3.x
- `pexpect` ライブラリ
- Claude Code CLI (`claude` コマンド)

```bash
pip install pexpect
```

## 🚀 使い方

### 1. デフォルト起動（セッション復旧）

`-p` 未指定時は `/session-recover /assemble-team` をデフォルトプロンプトとして送信します。

```bash
python3 claude_auto_run.py
```

### 2. プロンプト指定（自律実行）

一晩放置したいバッチ処理に最適。初回プロンプトを自動送信し、完了までリトライを繰り返します。

```bash
python3 claude_auto_run.py -p "CHECKPOINT.md を読み込んで続きを最後まで自律的に完了させてください。"
```

### 3. テスト用ダミーモード

```bash
CLAUDE_CMD=./dummy_claude.sh python3 claude_auto_run.py
```

## 🧠 アーキテクチャ

```
interactive_loop(pending_prompt) → select([child_fd, stdin_fd], timeout=0.5)
  ├─ child_fd ready  → read → check_rate_limit()
  │                    → detect ❯ → inject pending_prompt (初回のみ)
  │                    → write to stdout
  ├─ stdin_fd ready  → read → write to child_fd (対話透過)
  └─ timeout         → if rate_limit_detected: return "RATE_LIMIT"
```

### 動作フロー

1. `claude --dangerously-skip-permissions` を `pexpect.spawn()` で起動
2. SIGWINCHハンドラを設定（`shutil.get_terminal_size()` → `child.setwinsize()`）
3. `interactive_loop()` に入り、`select()` でstdinとchild_fdを同時監視
4. TUI出力内で `❯` を検知 → 3サイクル（約1.5秒）待機 → プロンプトを `os.write()` で注入
5. 子プロセス出力を2段階フィルタ（キーワード軽量チェック → フルバッファ正規表現）でRate Limit検知
6. Rate Limit検知時 → ループ脱出 → 待機（リセット時刻計算 or Exponential Backoff）
7. ESC送信でTUIメニューを解除 → 復旧プロンプトを次のループで `❯` 検知後に注入
8. 最大10回リトライ

### リセット時刻計算ロジック

| 状況 | 動作 |
|------|------|
| リセット時刻が未来 | 差分 + 60秒マージン（最大4時間） |
| リセット時刻が直近1時間以内の過去 | 60秒で即リトライ（既にリセット済み） |
| リセット時刻が1時間以上前の過去 | 翌日として計算 |
| パース不能 | 300秒のフォールバック |

## 🧪 テスト

```bash
python3 -m pytest test_claude_auto_run.py -v
```

39件のユニットテスト:
- `TestCalcWaitSeconds` (12件): リセット時刻計算
- `TestCalcBackoff` (5件): Exponential Backoff
- `TestCheckRateLimit` (12件): Rate Limit検知
- `TestParseArgs` (5件): 引数パース
- `TestAnsiEscapeRegex` (5件): ANSI除去

## 📁 ファイル構成

```
claude-auto-retry/
├── claude_auto_run.py       # メインスクリプト (~400行)
├── test_claude_auto_run.py  # ユニットテスト (39件)
├── dummy_claude.sh          # テスト用ダミースクリプト
└── README.md
```
