# Claude Auto-Recovery v2.0 🚀

Claude Code CLI を利用する際、APIのRate Limit（429エラー）や、サブスクリプションの上限制限（"resets 12am" 等）に達した際に、**自動で待機し、制限解除後に自律的に機能再開**を行う強力なラッパースクリプトです。

## ✨ 特徴 (Features)

- 🕐 **リセット時刻の自動パース**: "resets 3:45PM" などの文字列から現在時刻との差分を計算し、必要な時間だけ正確に待機します。
- ⏳ **Exponential Backoff**: 汎用的なRate Limitエラーに対しては、指数関数的（1分→2分→4分...）に待機時間を増やして再試行します。
- 💻 **対話×自動のハイブリッド**: 単なる裏側実行だけでなく、Claude CodeのTUI（対話型UI）を完全に利用可能。人間がチャットしている最中でも、制限にかかった瞬間だけスクリプトが割り込んで自動待機に入ります。待機明けは自動再開します。
- 🔒 **セキュアな設計**: プロセス起動時のシェルインジェクションリスクを排除し、プロセス終了時も確実にクリーンアップします。

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

本スクリプトは `pexpect` の `interact()` モードを活用しています。
1. `claude` プロセスを子プロセスとしてSpawnします。
2. ユーザーのキーボード入力（stdin）と画面出力（stdout）をパイプで直結し、普段使いのUXを損ないません。
3. その裏で `output_filter` を用いて出力の文字列バッファを監視し、Rate Limitのパターンを正規表現で常時検知しています。
4. 検知時のみ `RateLimitException` をスローして対話を中断、所定時間のスリープを入れた後に `Continue the task.\r` を送信して安全に戦線復帰します。
