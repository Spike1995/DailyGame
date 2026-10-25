"""边狱巴士活动超时策略解析（T-003 §3；纯标准库、无 I/O）。

本模块只判断 ``games.limbus.timeouts`` 中的 ``completion_idle_s`` /
``completion_hard_s`` 是否启用「按活动判断超时」模式，并补齐默认值：
任一键出现即启用活动模式，另一键补默认（idle 2700s / hard 21600s）；
两键均不存在时返回 legacy 策略，完全保持旧的固定 ``completion_s`` 行为
（旧键的解析仍归编排器 ``_game_timeout``，本模块不重复实现）。

错误约定（本模块采用「明确异常」而非结构化返回值）：
所有无效配置一律抛出 :class:`CompletionPolicyError`，携带稳定字符串
``code``（供预检与测试比较）与可直接展示的中文 ``message``；调用方不得
静默回落到 legacy。错误码：

- ``unsupported_location``：新键出现在全局 ``timeouts`` 或其他游戏的
  ``timeouts``（仅 limbus 游戏级支持）。
- ``timeouts_not_mapping``：``games.limbus.timeouts`` 存在但不是对象
  （null 视为缺失）。
- ``invalid_value``：值不是有限正数，或为 bool/null/字符串/NaN/Infinity/0/负数。
- ``idle_exceeds_hard``：``completion_idle_s > completion_hard_s``。

本模块不读取文件、不修改配置、不导入第三方库；供 ConfigManager.preflight
与后续 AALC 集成共用，避免两处默认值漂移。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

COMPLETION_IDLE_KEY = "completion_idle_s"
COMPLETION_HARD_KEY = "completion_hard_s"

DEFAULT_COMPLETION_IDLE_S = 2700.0
DEFAULT_COMPLETION_HARD_S = 21600.0

_LIMBUS_GAME_ID = "limbus"

CODE_UNSUPPORTED_LOCATION = "unsupported_location"
CODE_TIMEOUTS_NOT_MAPPING = "timeouts_not_mapping"
CODE_INVALID_VALUE = "invalid_value"
CODE_IDLE_EXCEEDS_HARD = "idle_exceeds_hard"


class CompletionPolicyError(Exception):
    """活动超时配置无效；``code`` 为稳定字符串错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LegacyPolicy:
    """旧固定超时模式：两个新键均不存在，行为与现有实现完全一致。"""

    mode: str = "legacy"


@dataclass(frozen=True)
class ActivityPolicy:
    """按活动判断超时模式。"""

    idle_s: float
    hard_s: float
    mode: str = "activity"


LimbusTimeoutPolicy = LegacyPolicy | ActivityPolicy

__all__ = [
    "COMPLETION_HARD_KEY",
    "COMPLETION_IDLE_KEY",
    "CODE_IDLE_EXCEEDS_HARD",
    "CODE_INVALID_VALUE",
    "CODE_TIMEOUTS_NOT_MAPPING",
    "CODE_UNSUPPORTED_LOCATION",
    "DEFAULT_COMPLETION_HARD_S",
    "DEFAULT_COMPLETION_IDLE_S",
    "ActivityPolicy",
    "CompletionPolicyError",
    "LegacyPolicy",
    "LimbusTimeoutPolicy",
    "resolve_limbus_activity_policy",
]


def _timeouts_mapping(config: Mapping[str, Any]) -> Mapping[str, Any]:
    timeouts = config.get("timeouts") if isinstance(config, Mapping) else None
    return timeouts if isinstance(timeouts, Mapping) else {}


def _games_mapping(config: Mapping[str, Any]) -> Mapping[str, Any]:
    games = config.get("games") if isinstance(config, Mapping) else None
    return games if isinstance(games, Mapping) else {}


def _game_timeouts(game_cfg: Any) -> Any:
    if not isinstance(game_cfg, Mapping):
        return None
    return game_cfg.get("timeouts")


def _contains_activity_keys(section: Any) -> bool:
    return (
        isinstance(section, Mapping)
        and (COMPLETION_IDLE_KEY in section or COMPLETION_HARD_KEY in section)
    )


def _describe_value(value: Any) -> str:
    """把任意值压成可展示片段；超大整数也不会让 repr 抛 ValueError。"""
    try:
        text = repr(value)
    except ValueError:
        text = f"<{type(value).__name__}>"
    return text if len(text) <= 64 else text[:61] + "..."


def _validate_positive_number(value: Any, key: str) -> float:
    """仅接受可表示为有限正数的 int/float；bool 是 int 子类，必须先排除。

    先把值转换成 ``float``：超出双精度可表示范围的整数（如 ``10**400``）在
    ``float()`` / ``math.isfinite()`` 上会抛 ``OverflowError``，这里统一转成
    ``invalid_value``，既不外泄 ``OverflowError``，也不裁剪成 ``inf``。
    """
    number: float | None = None
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        try:
            converted: float | None = float(value)
        except (OverflowError, ValueError):
            converted = None
        if converted is not None and math.isfinite(converted) and converted > 0:
            number = converted
    if number is None:
        raise CompletionPolicyError(
            CODE_INVALID_VALUE,
            f"games.limbus.timeouts.{key} 必须是有限正数"
            f"（拒绝 bool/null/字符串/NaN/Infinity/0/负数/超出可表示范围的数值），"
            f"实际为 {_describe_value(value)}",
        )
    return number


def resolve_limbus_activity_policy(config: Mapping[str, Any]) -> LimbusTimeoutPolicy:
    """按 T-003 §3 解析 limbus 活动超时策略。

    无效配置抛出 :class:`CompletionPolicyError`，不静默回落；返回值
    不可变，可安全缓存或跨线程传递。
    """
    # 1. 位置检查：新键只允许出现在 games.limbus.timeouts。
    if _contains_activity_keys(_timeouts_mapping(config)):
        raise CompletionPolicyError(
            CODE_UNSUPPORTED_LOCATION,
            "completion_idle_s / completion_hard_s 不支持放在全局 timeouts，"
            "只允许 games.limbus.timeouts",
        )
    for game_id, game_cfg in _games_mapping(config).items():
        if game_id == _LIMBUS_GAME_ID:
            continue
        if _contains_activity_keys(_game_timeouts(game_cfg)):
            raise CompletionPolicyError(
                CODE_UNSUPPORTED_LOCATION,
                f"completion_idle_s / completion_hard_s 不支持放在 "
                f"games.{game_id}.timeouts，只允许 games.limbus.timeouts",
            )

    limbus_timeouts = _game_timeouts(_games_mapping(config).get(_LIMBUS_GAME_ID))
    if limbus_timeouts is None:
        return LegacyPolicy()
    if not isinstance(limbus_timeouts, Mapping):
        raise CompletionPolicyError(
            CODE_TIMEOUTS_NOT_MAPPING,
            "games.limbus.timeouts 必须是对象",
        )

    has_idle = COMPLETION_IDLE_KEY in limbus_timeouts
    has_hard = COMPLETION_HARD_KEY in limbus_timeouts
    if not (has_idle or has_hard):
        return LegacyPolicy()

    idle_s = (
        _validate_positive_number(limbus_timeouts[COMPLETION_IDLE_KEY], COMPLETION_IDLE_KEY)
        if has_idle
        else DEFAULT_COMPLETION_IDLE_S
    )
    hard_s = (
        _validate_positive_number(limbus_timeouts[COMPLETION_HARD_KEY], COMPLETION_HARD_KEY)
        if has_hard
        else DEFAULT_COMPLETION_HARD_S
    )
    if idle_s > hard_s:
        raise CompletionPolicyError(
            CODE_IDLE_EXCEEDS_HARD,
            f"games.limbus.timeouts.completion_idle_s ({idle_s:g}) 不得大于 "
            f"completion_hard_s ({hard_s:g})",
        )
    return ActivityPolicy(idle_s=idle_s, hard_s=hard_s)
