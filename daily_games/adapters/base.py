"""游戏适配器基类：只读 StepResult 与 GameAdapter 协议。

StepResult 为冻结数据类型，字段固定为 ok / code / message / details。
GameAdapter 是各适配器（MAA、MaaEnd、AALC）共同遵循的协议（鸭子类型），
供编排器以相同阶段串行驱动三款游戏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class StepResult:
    """单一阶段的结果（只读）。

    code 使用稳定字符串，常见值定义于本模块。
    details 仅供显示与诊断，不应包含敏感配置内容。
    """

    ok: bool
    code: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)


# 稳定代码常量（适配器共用）
CODE_OK = "ok"
CODE_ENV_NOT_READY = "environment_not_ready"
CODE_START_FAILED = "start_failed"
CODE_WAIT_TIMEOUT = "wait_timeout"
CODE_HELPER_EXITED_EARLY = "helper_exited_early"
CODE_STOP_REQUESTED = "stop_requested"
CODE_COMPLETION_SIGNAL_UNSET = "completion_signal_unset"
CODE_COMPLETION_NOT_DETERMINED = "completion_not_determined"
CODE_NOT_IMPLEMENTED = "not_implemented"
CODE_MANUAL_CONFIGURATION_REQUIRED = "manual_configuration_required"


@runtime_checkable
class GameAdapter(Protocol):
    """游戏适配器协议。

    各阶段返回 StepResult。阶段失败时，编排器据此暂停队列并显示原因。
    完成判断不得仅以挂机程序消失推断成功，除非同时有已验证的完成信号。
    """

    @property
    def game_id(self) -> str:  # pragma: no cover - 协议
        ...

    @property
    def display_name(self) -> str:  # pragma: no cover - 协议
        ...

    def check_environment(self) -> StepResult:  # pragma: no cover - 协议
        ...

    def start_game(self) -> StepResult:  # pragma: no cover - 协议
        ...

    def wait_game_ready(self, timeout_s: float, stop_event: object | None = None) -> StepResult:  # pragma: no cover
        ...

    def start_helper(self) -> StepResult:  # pragma: no cover - 协议
        ...

    def wait_completion(self, timeout_s: float, stop_event: object | None = None) -> StepResult:  # pragma: no cover
        ...

    def cleanup(self) -> StepResult:  # pragma: no cover - 协议
        ...


class AdapterBase:
    """适配器共用辅助：日志、配置访问。

    子类通过 self._log(level, msg) 记录，不直接接触 logging 配置。
    logger 可为 None（测试时），此时记录被忽略。
    """

    def __init__(self, *, game_id: str, display_name: str, game_cfg: Mapping[str, object],
                 process_monitor, logger=None):
        self._game_id = game_id
        self._display_name = display_name
        self._game_cfg = game_cfg
        self._pm = process_monitor
        self._logger = logger

    @property
    def game_id(self) -> str:
        return self._game_id

    @property
    def display_name(self) -> str:
        return self._display_name

    def _log(self, level: str, msg: str) -> None:
        if self._logger is None:
            return
        method = getattr(self._logger, level, None)
        if callable(method):
            method("[%s] %s", self._game_id, msg)

    # 以下方法由子类实现；此处提供安全默认（not_implemented），
    # 避免子类遗漏时以模糊行为回应。

    def check_environment(self) -> StepResult:
        return StepResult(False, CODE_NOT_IMPLEMENTED, "check_environment 未实现", {})

    def start_game(self) -> StepResult:
        return StepResult(False, CODE_NOT_IMPLEMENTED, "start_game 未实现", {})

    def wait_game_ready(self, timeout_s: float, stop_event: object | None = None) -> StepResult:
        return StepResult(False, CODE_NOT_IMPLEMENTED, "wait_game_ready 未实现", {})

    def start_helper(self) -> StepResult:
        return StepResult(False, CODE_NOT_IMPLEMENTED, "start_helper 未实现", {})

    def wait_completion(self, timeout_s: float, stop_event: object | None = None) -> StepResult:
        return StepResult(False, CODE_NOT_IMPLEMENTED, "wait_completion 未实现", {})

    def cleanup(self) -> StepResult:
        return StepResult(False, CODE_NOT_IMPLEMENTED, "cleanup 未实现", {})


__all__ = [
    "StepResult",
    "GameAdapter",
    "AdapterBase",
    "CODE_OK",
    "CODE_ENV_NOT_READY",
    "CODE_START_FAILED",
    "CODE_WAIT_TIMEOUT",
    "CODE_HELPER_EXITED_EARLY",
    "CODE_STOP_REQUESTED",
    "CODE_COMPLETION_SIGNAL_UNSET",
    "CODE_COMPLETION_NOT_DETERMINED",
    "CODE_NOT_IMPLEMENTED",
    "CODE_MANUAL_CONFIGURATION_REQUIRED",
]
