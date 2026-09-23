"""baostock 熔断状态测试：全程离线，只碰临时文件。"""

from datetime import datetime, timedelta

import pytest

from sequoia_x.data import baostock_breaker as breaker


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """状态文件必须落到临时目录，绝不能污染项目 data/。"""
    monkeypatch.setenv(breaker._ENV_PATH, str(tmp_path / "breaker.json"))
    yield


def test_no_state_means_no_cooldown() -> None:
    assert breaker.load() == {}
    assert breaker.cooldown_remaining() == 0.0


def test_first_failure_sets_short_cooldown() -> None:
    state = breaker.record_failure("连接超时")
    assert state["failures"] == 1
    assert 0 < breaker.cooldown_remaining() <= breaker.backoff_seconds(1)


def test_backoff_doubles_then_caps_at_six_hours() -> None:
    assert [breaker.backoff_seconds(n) for n in (1, 2, 3)] == [600, 1200, 2400]
    assert breaker.backoff_seconds(99) == 6 * 3600
    assert breaker.backoff_seconds(0) == 0


def test_failures_accumulate_across_rounds() -> None:
    breaker.record_failure("连接超时")
    state = breaker.record_failure("连接超时")
    assert state["failures"] == 2


def test_success_clears_state() -> None:
    breaker.record_failure("连接超时")
    breaker.record_success()
    assert breaker.load() == {}
    assert breaker.cooldown_remaining() == 0.0


def test_cooldown_expires() -> None:
    """窗口过期后必须放行，否则换 IP 的用户永远等不到重试。"""
    breaker.record_failure("连接超时")
    state = breaker.load()
    state["blocked_until"] = (
        datetime.now().astimezone() - timedelta(seconds=1)
    ).isoformat(timespec="seconds")
    breaker._write(state)
    assert breaker.cooldown_remaining() == 0.0


def test_corrupted_state_is_ignored() -> None:
    """状态文件损坏不能阻断跑批，按"无失败历史"处理。"""
    path = breaker.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 json", encoding="utf-8")
    assert breaker.load() == {}
    assert breaker.cooldown_remaining() == 0.0


def test_reset_removes_state() -> None:
    breaker.record_failure("连接超时")
    breaker.reset()
    assert breaker.state_path().exists() is False
