"""适配器工厂：build_adapters 返回三款游戏的适配器。

返回键固定为 arknights / endfield / limbus。此接口由编排器（GPT Luna）直接使用。
"""

from __future__ import annotations

import logging
from typing import Mapping

from .aalc import AalcAdapter
from .base import GameAdapter
from .maa import MaaAdapter
from .maaend import MaaEndAdapter

__all__ = ["build_adapters", "GameAdapter"]


def _adapter_logger(base_logger, game_id: str):
    """返回带游戏 ID 后缀的子 logger，便于日志区分来源。

    测试常注入 FakeLogger；只对真正的 logging.Logger 派生子 logger，
    其他对象原样透传，保持测试兼容。
    """
    if isinstance(base_logger, logging.Logger):
        return base_logger.getChild(game_id)
    return base_logger


def build_adapters(config: Mapping[str, object], process_monitor, logger=None) -> dict[str, GameAdapter]:
    """依配置建构三款游戏的适配器。

    config 为 user.json 加载后的 dict；读取 ``games`` 区段。
    每个适配器收到带游戏 ID 后缀的子 logger，便于日志按游戏过滤。
    全局 ``polling`` 与 ``timeouts`` 段作为 global_options 传入，使适配器
    能读取 completion_poll_interval_s / cleanup_grace_s 等可调参数。
    """
    games = config.get("games", {}) if isinstance(config, dict) else {}
    if not isinstance(games, dict):
        raise ValueError("config.games 必须是对象")

    global_options: dict[str, object] = {}
    for section in ("polling", "timeouts"):
        value = config.get(section) if isinstance(config, Mapping) else None
        if isinstance(value, Mapping):
            global_options[section] = value

    arknights_cfg = games.get("arknights", {})
    endfield_cfg = games.get("endfield", {})
    limbus_cfg = games.get("limbus", {})

    if not isinstance(arknights_cfg, dict):
        raise ValueError("config.games.arknights 必须是对象")
    if not isinstance(endfield_cfg, dict):
        raise ValueError("config.games.endfield 必须是对象")
    if not isinstance(limbus_cfg, dict):
        raise ValueError("config.games.limbus 必须是对象")

    return {
        "arknights": MaaAdapter(
            game_cfg=arknights_cfg, process_monitor=process_monitor,
            logger=_adapter_logger(logger, "arknights"),
            global_options=global_options,
        ),
        "endfield": MaaEndAdapter(
            game_cfg=endfield_cfg, process_monitor=process_monitor,
            logger=_adapter_logger(logger, "endfield"),
            global_options=global_options,
        ),
        "limbus": AalcAdapter(
            game_cfg=limbus_cfg, process_monitor=process_monitor,
            logger=_adapter_logger(logger, "limbus"),
            global_options=global_options,
        ),
    }
