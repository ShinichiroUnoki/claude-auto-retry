# Claude Auto-Recovery v3.1 🚀

Claude Code CLI を利用する際、APIのRate Limit（429エラー）や、サブスクリプションの上限制限（"resets 12am" 等）に達した際に、**自動で待機し、制限解除後に自律的に機能再開**を行う強力なラッパースクリプトです。

## ✨ 特徴 (Features)

- 🕐 **リセット時刻の自動パース**: "resets 3:45PM" などの文字列から現在時刻との差分を計算し、必要な時間だけ正確に待機します。
- ⏳ **Exponential Backoff**: 汎用的なRate Limitエラーに対しては、指数関数的（1分→2分→4分...）に待機時間を増やして再試行します。
- 💻 **対話×自動のハイブリッド**: Claude CodeのTUI（対話型UI）を完全に利用可能。人間がチャットしている最中でも、制限にかかった瞬間だけスクリプトが割り込んで自動待機に入ります。待機明けは自動再開します。
- 🔒 **セキュアな設計**: プロセス起動時のシェルインジェクションリスクを排除し、プロセス終了時も確実にクリーンアップします。
- 📐 **ターミナルリサイズ対応**: SIGWINCHを子プロセスに転送し、TUI描画の崩壊を防止します。

## 🆕 v3.1 変更点

| 変更 | Before (v2.0) | After (v3.1) | 理由 |
|------|---------------|--------------|------|
| I/Oループ | `pexpect.interact()` | 自前 `select()` ループ | interact()は外部から安全に中断できない構造的制約がある |
| Rate Limit検知通知 | `output_filter`内で例外送出 | `threading.Event` フラグ | 例外送出がTTY復元を破壊する可能性がある |
| ループ脱出方式 | 例外 / escape_char | `select()` タイムアウト(0.5秒)でイベント確認 | watchdogスレッド不要。シングルスレッドでシンプル |
| ターミナルリサイズ | 未対応 | SIGWINCH転送 | TUI描画崩壊の防止 |
| 正規表現コンパイル | 毎回 `re.compile()` | モジュールレベルで1回 | パフォーマンス改善 |
| TTY復元 | pexpectのtry/finally依存 | 自前try/finallyで完全管理 | 例外パスに関わらず確実に復元 |
| 復旧フロー | ESC→即Continue | ESC→1秒待機→ESC→0.5秒待機→Continue | メニューキャンセルの完了を待つ |
| pexpectバッファ | 未考慮 | expect()後にフラッシュ | 自前ループ切替時のデータロスト防止 |

### アーキテクチャ図

```
┌──────────────────────────────────────────────────┐
│ interactive_loop()                                │
│                                                    │
│  select([child_fd, stdin_fd], timeout=0.5)         │
│    ├─ child_fd ready  → read → check_rate_limit() │
│    │                    → write to stdout           │
│    ├─ stdin_fd ready   → read → write to child_fd  │
│    └─ timeout          → check rate_limit_event    │
│                          → if set: return           │
│                                                    │
│  try/finally で TTY 状態を確実に復元                │
└──────────────────────────────────────────────────┘
```

### v3.0 のバグ修正

v3.0では `rate_limit_watchdog` スレッドが `os.write(child.child_fd, escape_char)` でinteract()を脱出させようとしていましたが、これは**子プロセスへのデータ送信**であり、`interact()` の `escape_character` 検出は**stdinからの入力のみ**を監視するため、実際にはinteract()を脱出できませんでした。v3.1では自前I/Oループでこの問題を根本的に解消しています。

## 📦 前提環境 (Prerequisites)

- Python 3.x
- `pexpect` ライブラリ
- Claude Code CLI (`claude` コマンドへのパスが通っていること)

```bash
# 依存関係のインストール
pip install pexpect
```

## 🚀 使い方 (Usage)

### 1. 対話モード（普段使い向け）

Claude Codeを通常通り対話的に使用できます。もし途中で利用制限に達した場合、自動的に待機モードへ移行します。

```bash
python3 claude_auto_run.py
```

### 2. タスク指定による完全自律実行（放置向け）

一晩放置したい重いバッチ処理や、ファイル生成タスクなどに最適です。最初のプロンプトを自動送信した後、完了するまでリトライを繰り返します。

```bash
python3 claude_auto_run.py -p "CHECKPOINT.md を読み込んで続きを最後まで自律的に完了させてください。"
```

### 3. テスト用ダミーモード

`CLAUDE_CMD` 環境変数を設定することで、実際の Claude API を叩かずに（テスト用スクリプトを利用して）Rate Limit時の動作確認が可能です。

```bash
CLAUDE_CMD=./dummy_claude.sh python3 claude_auto_run.py
```

## 🧠 動作の仕組み

本スクリプトは `select()` ベースの自前I/Oループを使用しています。

1. `claude` プロセスを `pexpect.spawn()` で子プロセスとして起動します。
2. SIGWINCHハンドラを設定し、ターミナルリサイズを子プロセスに転送します。
3. ユーザーのキーボード入力（stdin）と画面出力（stdout）を `select()` + `os.read()`/`os.write()` で直結し、普段使いのUXを損ないません。
4. 子プロセスからの出力を受信するたびに `check_rate_limit()` でバッファに蓄積し、Rate Limitパターンを正規表現で検知します。
5. 検知時は `threading.Event` でフラグを立て、次の `select()` タイムアウト（0.5秒以内）でI/Oループを安全に脱出します。
6. 所定時間のスリープを入れた後に `Continue the task.\r` を送信して安全に戦線復帰します。
7. TTYのraw mode設定/復元は `try/finally` で完全に管理され、異常終了時もターミナルが壊れません。
