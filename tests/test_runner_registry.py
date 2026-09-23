"""策略注册表测试：清单完整性、顺序契约与别名解析。"""

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from sequoia_x.runner.registry import (
    STRATEGY_CLASSES,
    UnknownStrategyError,
    get_all_strategies,
    resolve,
)

# 注册表顺序 = 原 main.py 策略列表顺序，是跨任务契约（架构 §3.2），防有人重排。
_EXPECTED_ORDER = [
    "MaVolumeStrategy",
    "TurtleTradeStrategy",
    "HighTightFlagStrategy",
    "LimitUpShakeoutStrategy",
    "UptrendLimitDownStrategy",
    "RpsBreakoutStrategy",
    "PrivatePlacementStrategy",
]

# 与 .env.example 的 STRATEGY_WEBHOOK_* 键集合一一对应（按 T1 §5 要求只比对类属性）。
_EXPECTED_WEBHOOK_KEYS = {
    "ma_volume",
    "turtle",
    "flag",
    "shakeout",
    "limit_down",
    "rps",
    "private_placement",
}


def test_registry_has_seven_strategies_in_contract_order() -> None:
    """顺序变动会改变跑批日志与结果序列，这里逐一断言类名与顺序。"""
    assert [cls.__name__ for cls in get_all_strategies()] == _EXPECTED_ORDER


def test_get_all_strategies_returns_fresh_list() -> None:
    """不得把内部容器的引用漏给调用方。"""
    first = get_all_strategies()
    first.append(first[0])
    assert len(get_all_strategies()) == len(_EXPECTED_ORDER)


def test_webhook_keys_match_env_example_contract() -> None:
    """webhook_key 集合决定飞书路由，必须与 STRATEGY_WEBHOOK_* 配置约定一致。"""
    assert {cls.webhook_key for cls in STRATEGY_CLASSES} == _EXPECTED_WEBHOOK_KEYS


# Feature: sequoia-x-v2, Property 14: 策略别名解析等价且去重
@given(cls=st.sampled_from(list(STRATEGY_CLASSES)))
@h_settings(max_examples=30, deadline=None)
def test_resolve_alias_equals_class_name_and_dedupes(cls: type) -> None:
    """属性 14：任意策略用 webhook_key 别名与精确类名解析结果相同，两种写法混用只出一个。"""
    by_key = resolve([cls.webhook_key])
    by_name = resolve([cls.__name__])
    assert by_key == by_name == [cls]
    assert resolve([cls.webhook_key, cls.__name__]) == [cls]


def test_resolve_keeps_registry_order_not_input_order() -> None:
    """输出顺序必须是注册表顺序，保证跑批结果可与历史对比。"""
    resolved = resolve(["turtle", "ma_volume"])
    assert [cls.__name__ for cls in resolved] == ["MaVolumeStrategy", "TurtleTradeStrategy"]


def test_resolve_unknown_name_raises_with_valid_list() -> None:
    """报错消息要列出全部合法名称，否则调用方只能翻源码排查。"""
    with pytest.raises(UnknownStrategyError) as exc_info:
        resolve(["不存在的名字"])
    message = str(exc_info.value)
    assert "不存在的名字" in message
    for key in _EXPECTED_WEBHOOK_KEYS:
        assert key in message


def test_resolve_none_means_all_strategies() -> None:
    assert resolve(None) == get_all_strategies()
