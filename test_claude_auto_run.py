"""claude_auto_run.py のユニットテスト"""

import sys
import os
import time
import pytest
from unittest.mock import patch
from datetime import datetime, timedelta
from freezegun import freeze_time

# テスト対象モジュールをインポート
sys.path.insert(0, os.path.dirname(__file__))
import claude_auto_run as car


# ==========================================
# calc_wait_seconds: リセット時刻計算
# ==========================================

class TestCalcWaitSeconds:
    """calc_wait_seconds のテスト"""

    @freeze_time("2026-04-07 14:00:00")
    def test_future_reset_time(self):
        """リセット時刻が未来 → 差分+60秒"""
        result = car.calc_wait_seconds("3pm")
        assert result == 3660  # 1時間 + 60秒マージン

    @freeze_time("2026-04-07 14:00:00")
    def test_future_reset_with_minutes(self):
        """リセット時刻が未来（分指定）"""
        result = car.calc_wait_seconds("3:30PM")
        assert result == 5460  # 1.5時間 + 60秒

    @freeze_time("2026-04-07 15:01:00")
    def test_recently_passed_reset_time(self):
        """リセット時刻が直近の過去（1時間以内）→ 60秒の短時間待機"""
        result = car.calc_wait_seconds("3pm")
        assert result == 60

    @freeze_time("2026-04-07 15:59:00")
    def test_passed_within_one_hour(self):
        """リセット時刻がちょうど59分前 → まだ1時間以内なので60秒"""
        result = car.calc_wait_seconds("3pm")
        assert result == 60

    @freeze_time("2026-04-07 16:01:00")
    def test_passed_over_one_hour(self):
        """リセット時刻が1時間以上前 → 翌日として計算"""
        result = car.calc_wait_seconds("3pm")
        # 翌日3pm = 約23時間後 → min(82740+60, 14400) = 14400
        assert result == 14400

    @freeze_time("2026-04-07 23:00:00")
    def test_reset_after_midnight(self):
        """現在23時、リセットが1am → 翌日1amまで待機"""
        result = car.calc_wait_seconds("1am")
        assert result == 7260  # 2時間 + 60秒

    @freeze_time("2026-04-07 14:00:00")
    def test_max_cap_14400(self):
        """待機時間が14400秒（4時間）でキャップされる"""
        result = car.calc_wait_seconds("12am")
        # 14:00→翌0:00 = 10時間 → min(36060, 14400) = 14400
        assert result == 14400

    def test_unparseable_format(self):
        """パース不能な文字列 → フォールバック値"""
        result = car.calc_wait_seconds("invalid")
        assert result == car.FALLBACK_WAIT_SECONDS

    def test_empty_string(self):
        """空文字列 → フォールバック値"""
        result = car.calc_wait_seconds("")
        assert result == car.FALLBACK_WAIT_SECONDS

    @freeze_time("2026-04-07 14:00:00")
    def test_format_with_space(self):
        """スペース付きフォーマット "3 pm" """
        result = car.calc_wait_seconds("3 pm")
        assert result == 3660

    @freeze_time("2026-04-07 14:00:00")
    def test_format_12am(self):
        """12amフォーマット"""
        result = car.calc_wait_seconds("12am")
        assert result == 14400  # キャップ

    @freeze_time("2026-04-07 14:00:00")
    def test_lowercase_input(self):
        """小文字入力 "3pm" """
        result = car.calc_wait_seconds("3pm")
        assert result == 3660


# ==========================================
# calc_backoff: Exponential Backoff
# ==========================================

class TestCalcBackoff:
    """calc_backoff のテスト"""

    def test_first_retry(self):
        assert car.calc_backoff(0) == 60

    def test_second_retry(self):
        assert car.calc_backoff(1) == 120

    def test_third_retry(self):
        assert car.calc_backoff(2) == 240

    def test_capped_at_max(self):
        """上限600秒でキャップ"""
        assert car.calc_backoff(10) == 600

    def test_exact_cap_boundary(self):
        """600秒ちょうどに到達するリトライ回数"""
        # 60 * 2^3 = 480, 60 * 2^4 = 960 → min(960, 600) = 600
        assert car.calc_backoff(3) == 480
        assert car.calc_backoff(4) == 600


# ==========================================
# RateLimitDetector: Rate Limit 検知
# ==========================================

class TestRateLimitDetector:
    """RateLimitDetector のテスト"""

    def setup_method(self):
        self.detector = car.RateLimitDetector()

    def test_no_rate_limit(self):
        """通常出力 → 検知なし"""
        self.detector.feed(b"Hello, this is normal output")
        assert self.detector.detected is False

    def test_detect_resets_time(self):
        """'resets 3pm' パターンを検知"""
        self.detector.feed(b"You've hit your limit - resets 3pm (Asia/Tokyo)")
        assert self.detector.detected is True
        assert self.detector.type == "TIME"
        assert self.detector.value == "3pm"

    def test_detect_resets_at_time(self):
        """'Resets at 1am' パターンを検知"""
        self.detector.feed(b"Resets at 1am")
        assert self.detector.detected is True
        assert self.detector.type == "TIME"
        assert "1am" in self.detector.value

    def test_detect_429(self):
        """'429 Too Many Requests' を検知"""
        self.detector.feed(b"Error: 429 Too Many Requests")
        assert self.detector.detected is True
        assert self.detector.type == "GENERIC"

    def test_detect_hit_your_limit(self):
        """'hit your limit' を検知"""
        self.detector.feed(b"You've hit your limit")
        assert self.detector.detected is True
        assert self.detector.type == "GENERIC"

    def test_detect_usage_limit(self):
        """'usage limit' を検知"""
        self.detector.feed(b"You've reached the usage limit")
        assert self.detector.detected is True
        assert self.detector.type == "GENERIC"

    def test_detect_5_hour_limit(self):
        """'5-hour limit' を検知"""
        self.detector.feed(b"5-hour limit reached")
        assert self.detector.detected is True
        assert self.detector.type == "GENERIC"

    def test_ansi_escape_stripped(self):
        """ANSIエスケープシーケンスを除去して検知"""
        data = b"\x1b[31mYou've hit your limit\x1b[0m - resets 5am"
        self.detector.feed(data)
        assert self.detector.detected is True
        assert self.detector.type == "TIME"

    def test_two_stage_filter_skips_unrelated(self):
        """キーワードを含まない出力 → フルバッファ検査をスキップ"""
        self.detector.feed(b"normal output without keywords")
        assert self.detector.detected is False
        # バッファには蓄積されている
        assert "normal output" in self.detector._buffer

    def test_buffer_truncation(self):
        """バッファが2000文字でトランケートされる"""
        large_data = b"x" * 3000
        self.detector.feed(large_data)
        assert len(self.detector._buffer) == 2000

    def test_time_pattern_takes_priority(self):
        """TIMEパターンがGENERICより優先される"""
        self.detector.feed(b"hit your limit - resets 3pm")
        assert self.detector.type == "TIME"
        assert self.detector.value == "3pm"

    def test_reset(self):
        """reset() で状態がクリアされる"""
        self.detector.feed(b"429 Too Many Requests")
        assert self.detector.detected is True

        self.detector.reset()
        assert self.detector.detected is False
        assert self.detector.type is None
        assert self.detector.value == ""
        assert self.detector._buffer == ""

    def test_usage_percent_not_detected(self):
        """ステータスバーの使用率表示を誤検知しない"""
        self.detector.feed(
            b"You've used 90% of your session limit \xc2\xb7 resets 8pm (Asia/Tokyo)"
        )
        assert self.detector.detected is False

    def test_usage_percent_50_not_detected(self):
        """50%の使用率表示を誤検知しない"""
        self.detector.feed(
            b"You've used 50% of your session limit \xc2\xb7 resets 8pm"
        )
        assert self.detector.detected is False

    def test_real_rate_limit_still_detected_after_percent(self):
        """使用率表示の後に本当のrate limitが来た場合は検知する"""
        self.detector.feed(
            b"You've used 90% of your session limit \xc2\xb7 resets 8pm"
        )
        assert self.detector.detected is False
        # リセットして本物のrate limitを送信
        self.detector.reset()
        self.detector.feed(b"You've hit your limit - resets 8pm")
        assert self.detector.detected is True
        assert self.detector.type == "TIME"


# ==========================================
# parse_args: 引数パース
# ==========================================

class TestParseArgs:
    """parse_args のテスト"""

    def test_no_args(self):
        """引数なし → デフォルトコマンド、プロンプトなし"""
        with patch.object(sys, 'argv', ['script.py']):
            with patch.dict(os.environ, {}, clear=True):
                cmd, prompt = car.parse_args()
        assert cmd == ['claude', '--dangerously-skip-permissions']
        assert prompt == ""

    def test_with_prompt(self):
        """-p でプロンプト指定"""
        with patch.object(sys, 'argv', ['script.py', '-p', 'hello world']):
            with patch.dict(os.environ, {}, clear=True):
                cmd, prompt = car.parse_args()
        assert prompt == "hello world"
        assert '-p' not in cmd
        assert 'hello world' not in cmd

    def test_output_format_stripped(self):
        """--output-format は除外される"""
        with patch.object(sys, 'argv', ['script.py', '--output-format=json']):
            with patch.dict(os.environ, {}, clear=True):
                cmd, prompt = car.parse_args()
        assert '--output-format=json' not in cmd

    def test_env_cmd_override(self):
        """CLAUDE_CMD 環境変数でコマンドを上書き"""
        with patch.object(sys, 'argv', ['script.py']):
            with patch.dict(os.environ, {'CLAUDE_CMD': './dummy.sh'}):
                cmd, prompt = car.parse_args()
        assert cmd == ['./dummy.sh']

    def test_extra_args_passed_through(self):
        """未知の引数はclaudeコマンドに渡される"""
        with patch.object(sys, 'argv', ['script.py', '--verbose']):
            with patch.dict(os.environ, {}, clear=True):
                cmd, prompt = car.parse_args()
        assert '--verbose' in cmd


# ==========================================
# ANSI_ESCAPE_RE: 正規表現
# ==========================================

class TestAnsiEscapeRegex:
    """ANSIエスケープシーケンスの除去テスト"""

    def test_color_code(self):
        assert car.ANSI_ESCAPE_RE.sub('', '\x1b[31mred\x1b[0m') == 'red'

    def test_bold(self):
        assert car.ANSI_ESCAPE_RE.sub('', '\x1b[1mbold\x1b[0m') == 'bold'

    def test_cursor_movement(self):
        assert car.ANSI_ESCAPE_RE.sub('', '\x1b[2Amoved') == 'moved'

    def test_no_escape(self):
        assert car.ANSI_ESCAPE_RE.sub('', 'plain text') == 'plain text'

    def test_mixed(self):
        text = '\x1b[32m❯\x1b[0m hello'
        assert car.ANSI_ESCAPE_RE.sub('', text) == '❯ hello'


# ==========================================
# _drain_child_fd: PTYバッファ読み捨て
# ==========================================

class TestDrainChildFd:
    """_drain_child_fd のテスト"""

    def test_drains_available_data(self):
        """データがある場合、全て読み捨てて終了する"""
        r_fd, w_fd = os.pipe()
        try:
            os.write(w_fd, b"some stale output\r\n" * 10)
            car._drain_child_fd(r_fd, timeout=1.0)
            # drain後はバッファが空 → select で即 fd なし
            import select as sel
            ready, _, _ = sel.select([r_fd], [], [], 0)
            assert ready == []
        finally:
            os.close(r_fd)
            os.close(w_fd)

    def test_returns_immediately_when_empty(self):
        """バッファが空なら0.1秒以内に返る"""
        r_fd, w_fd = os.pipe()
        try:
            start = time.time()
            car._drain_child_fd(r_fd, timeout=1.0)
            elapsed = time.time() - start
            assert elapsed < 0.3  # 0.1秒で break するので余裕を持って 0.3s 以内
        finally:
            os.close(r_fd)
            os.close(w_fd)

    def test_handles_eof_gracefully(self):
        """write端を閉じてEOFになっても例外を出さない"""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"data")
        os.close(w_fd)
        # OSError(EIO) が発生してもクラッシュしない
        car._drain_child_fd(r_fd, timeout=1.0)
        os.close(r_fd)

    def test_respects_timeout(self):
        """連続データが来続けても timeout で抜ける"""
        r_fd, w_fd = os.pipe()
        try:
            # タイムアウト前に大量書き込み（read側をブロックはしないが継続データを模倣）
            os.write(w_fd, b"x" * 4096)
            start = time.time()
            car._drain_child_fd(r_fd, timeout=0.5)
            elapsed = time.time() - start
            assert elapsed < 1.0
        finally:
            os.close(r_fd)
            os.close(w_fd)
