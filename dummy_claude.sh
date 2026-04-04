#!/bin/bash
# テスト用ダミースクリプト: Claude Codeの各種Rate Limitメッセージを再現する

echo "Claude (Dummy) is starting..."
sleep 1

# テストケース1: リセット時刻付きサブスクリプション制限
echo "Processing your request..."
sleep 1
echo "You've hit your limit · resets 12am (Asia/Tokyo)"

# 自動復旧スクリプトからの入力を待つ
read input_line
echo "Received: $input_line"

# テストケース2: 汎用 429 Rate Limit
echo "Processing continued..."
sleep 1
echo "API Error: 429 Too Many Requests"

read input_line2
echo "Received: $input_line2"

# テストケース3: 5-hour limitパターン
echo "Processing continued..."
sleep 1
echo "⎿ 5-hour limit reached ∙ resets 3:45PM"

read input_line3
echo "Received: $input_line3"

# 最終: 成功
echo "Task completed successfully."
exit 0
