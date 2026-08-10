"""适配器工厂：build_adapters 返回三款游戏的适配器。

返回键固定为 arknights / endfield / limbus。此接口由编排器（GPT Luna）直接使用。
"""

from __future__ import annotations

from typing import Mapping

from .aalc import AalcAdapter
from .base import GameAdapter
from .maa import MaaAdapter
from .maaend import MaaEndAdapter

__all__ = ["build_adapters", "GameAdapter"]


def build_adapters(config: Mapping[str, object], process_monitor, logger=None) -> dict[str, GameAdapter]:
    """依配置建构三款游戏的适配器。

    config 为 user.json 加载后的 dict；读取 ``games`` 区段。
    """
    games = config.get("games", {}) if isinstance(config, dict) else {}
    if not isinstance(games, dict):
        raise ValueError("config.games 必须是对象")

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
        "arknights": MaaAdapter(game_cfg=arknights_cfg, process_monitor=process_monitor, logger=logger),
        "endfield": MaaEndAdapter(game_cfg=endfield_cfg, process_monitor=process_monitor, logger=logger),
        "limbus": AalcAdapter(game_cfg=limbus_cfg, process_monitor=process_monitor, logger=logger),
    }
