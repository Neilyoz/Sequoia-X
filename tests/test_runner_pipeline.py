"""跑批编排测试：推送路由、push 开关、单策略异常容错（全 mock，不碰网络与真实库）。"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from sequoia_x.core.config import Settings
from sequoia_x.runner import pipeline


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "pipeline.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )


def _fake_strategy(
    name: str,
    key: str,
    selected: list[str] | None = None,
    error: Exception | None = None,
) -> Callable[..., Any]:
    """构造输出可控的策略类。run_daily 按真策略的构造签名 (engine=, settings=) 实例化。"""

    class _Fake:
        webhook_key = key

        def __init__(self, engine: Any, settings: Any) -> None:
            self.engine = engine
            self.settings = settings

        def run(self) -> list[str]:
            if error is not None:
                raise error
            return list(selected or [])

    _Fake.__name__ = name
    return _Fake


def _run_daily(
    settings: Settings,
    classes: list,
    **kwargs: Any,
) -> tuple[pipeline.DailyReport, MagicMock, MagicMock]:
    """在 DataEngine / FeishuNotifier / resolve 三个 patch 点下执行 run_daily。

    patch 的是 pipeline 模块里的名字，与 run_daily 内部实际查找的命名空间一致。
    """
    engine = MagicMock()
    engine.sync_today_bulk.return_value = 42
    notifier = MagicMock()
    with (
        patch.object(pipeline, "DataEngine", MagicMock(return_value=engine)),
        patch.object(pipeline, "FeishuNotifier", MagicMock(return_value=notifier)),
        patch.object(pipeline, "resolve", return_value=classes),
    ):
        report = pipeline.run_daily(settings, **kwargs)
    return report, engine, notifier


def test_run_daily_pushes_only_nonempty_and_routes_webkey(tmp_path: Path) -> None:
    """只有非空结果的策略触发 send()，webhook_key 与 pushed 标志逐一对应。"""
    settings = _settings(tmp_path)
    classes = [
        _fake_strategy("StrategyA", "ma_volume", ["000001", "600519"]),
        _fake_strategy("StrategyB", "turtle", ["300750"]),
        _fake_strategy("StrategyC", "flag", []),
    ]
    report, _, notifier = _run_daily(settings, classes)

    assert notifier.send.call_count == 2
    first, second = notifier.send.call_args_list
    assert first.kwargs == {"symbols": ["000001", "600519"], "strategy_name": "StrategyA",
                            "webhook_key": "ma_volume"}
    assert second.kwargs["webhook_key"] == "turtle"

    assert report.synced_rows == 42
    assert report.push_enabled is True
    assert report.total_signals == 3
    assert [o.pushed for o in report.outcomes] == [True, True, False]
    assert [o.error for o in report.outcomes] == [None, None, None]


def test_run_daily_push_false_keeps_symbols_without_send(tmp_path: Path) -> None:
    """push=False 只跑不推：send 零调用，但 outcomes.symbols 完整可查（API 调试语义）。"""
    settings = _settings(tmp_path)
    classes = [
        _fake_strategy("StrategyA", "ma_volume", ["000001"]),
        _fake_strategy("StrategyB", "turtle", []),
    ]
    report, _, notifier = _run_daily(settings, classes, push=False)

    notifier.send.assert_not_called()
    assert report.push_enabled is False
    assert [o.symbols for o in report.outcomes] == [["000001"], []]
    assert [o.pushed for o in report.outcomes] == [False, False]


def test_run_daily_continues_after_single_strategy_error(tmp_path: Path) -> None:
    """单策略异常只跳过自己：原 main.py 一挂全挂，此处是 T1 唯一刻意行为变更。"""
    settings = _settings(tmp_path)
    classes = [
        _fake_strategy("StrategyA", "ma_volume", ["000001"]),
        _fake_strategy("StrategyB", "turtle", error=ValueError("boom")),
        _fake_strategy("StrategyC", "flag", ["600000"]),
    ]
    report, _, notifier = _run_daily(settings, classes)

    assert len(report.outcomes) == 3
    a, b, c = report.outcomes
    assert b.error == "ValueError: boom"
    assert (b.symbols, b.pushed) == ([], False)
    assert a.error is None and c.error is None
    # 坏策略前后的策略都正常推送
    assert notifier.send.call_count == 2


def test_run_daily_resolves_strategies_argument(tmp_path: Path) -> None:
    """strategies 参数原样透传给 resolve，由注册表决定解析语义。"""
    settings = _settings(tmp_path)
    engine = MagicMock()
    # 必须给非 0：T4 §4.4 起 synced_rows==0 会提前返回（休市日保护），不再走到 resolve
    engine.sync_today_bulk.return_value = 7
    with (
        patch.object(pipeline, "DataEngine", MagicMock(return_value=engine)),
        patch.object(pipeline, "FeishuNotifier", MagicMock()),
        patch.object(pipeline, "resolve", return_value=[]) as mock_resolve,
    ):
        report = pipeline.run_daily(settings, strategies=["turtle", "ma_volume"])
    mock_resolve.assert_called_once_with(["turtle", "ma_volume"])
    assert report.outcomes == []


def test_run_daily_skips_strategies_when_no_new_data(tmp_path: Path) -> None:
    """synced_rows==0（休市日）：resolve/notifier 零构造、outcomes 空、send 零调用。

    T4 §4.4 的节假日保护：若没有这道提前返回，调度在法定节假日到点会在旧数据上
    重跑并再次推送飞书（约束 §6，推送不可撤回）。
    """
    engine = MagicMock()
    engine.sync_today_bulk.return_value = 0
    notifier_cls = MagicMock()
    resolve_mock = MagicMock(return_value=[])
    with (
        patch.object(pipeline, "DataEngine", MagicMock(return_value=engine)),
        patch.object(pipeline, "FeishuNotifier", notifier_cls),
        patch.object(pipeline, "resolve", resolve_mock),
    ):
        report = pipeline.run_daily(_settings(tmp_path))
    resolve_mock.assert_not_called()
    notifier_cls.assert_not_called()
    assert report.synced_rows == 0
    assert report.outcomes == []
    assert report.total_signals == 0
    assert report.push_enabled is True


def test_run_backfill_syncs_basic_then_backfills(tmp_path: Path) -> None:
    """回填两步顺序：sync_stock_basic 的清单原样传给 backfill，返回清单规模。"""
    settings = _settings(tmp_path)
    engine = MagicMock()
    engine.sync_stock_basic.return_value = ["600000", "000001"]
    with patch.object(pipeline, "DataEngine", MagicMock(return_value=engine)):
        result = pipeline.run_backfill(settings)
    engine.backfill.assert_called_once_with(["600000", "000001"])
    assert result == {"symbols_synced": 2}


def test_daily_report_to_dict_is_json_serializable() -> None:
    """T2 要把 to_dict() 落库为 TaskRecord.result，必须可 JSON 序列化且字段名一致。"""
    report = pipeline.DailyReport(
        trade_date="2026-09-23",
        synced_rows=42,
        outcomes=[pipeline.StrategyOutcome("S", "turtle", ["600519"], True)],
        push_enabled=True,
    )
    data = json.loads(json.dumps(report.to_dict()))
    assert data["trade_date"] == "2026-09-23"
    assert data["synced_rows"] == 42
    assert data["push_enabled"] is True
    assert data["outcomes"][0] == {
        "strategy_name": "S",
        "webhook_key": "turtle",
        "symbols": ["600519"],
        "pushed": True,
        "error": None,
    }
