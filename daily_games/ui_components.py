"""前端纯逻辑层：状态显示、控件规则、事件归并、关闭与配置向导决策。

本模块刻意不导入 tkinter，所有函数与 UiController 都可以在没有
Tk 根窗口的环境下单元测试。Tk 界面壳层在 daily_games.ui。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 游戏与状态常数
# ---------------------------------------------------------------------------

GAME_ORDER = ("arknights", "endfield", "limbus")

GAME_NAMES = {
    "arknights": "明日方舟",
    "endfield": "明日方舟：终末地",
    "limbus": "边狱巴士",
}

STATE_LABELS = {
    "idle": "待命",
    "checking": "检查环境",
    "starting_game": "启动游戏",
    "waiting_game": "等待游戏窗口",
    "starting_helper": "启动挂机程序",
    "running": "执行日常",
    "cleaning": "清理进程",
    "completed": "已完成",
    "failed": "失败",
    "paused": "已暂停",
    "skipped": "已跳过",
    "stopping": "停止中",
    "stopped": "已停止",
}

#: 需要用户介入（重试 / 跳过 / 停止）的状态。
ATTENTION_STATES = frozenset({"failed", "paused"})

#: 仍属进行中、不应允许再次启动的状态。
ACTIVE_STATES = frozenset({
    "checking", "starting_game", "waiting_game", "starting_helper",
    "running", "cleaning", "stopping",
})

#: 单款游戏本轮尝试结束的状态；计时在此冻结。
FINISHED_STATES = frozenset({"completed", "failed", "skipped", "stopped"})

#: 关闭窗口的三种分支。
CLOSE_KEEP = "keep"
CLOSE_STOP_THEN_EXIT = "stop"
CLOSE_CANCEL = "cancel"

#: 前端自行生成错误事件时使用的错误码。
FRONTEND_ERROR_CODE = "FRONTEND_ERROR"


def state_label(state: str) -> str:
    """返回状态的中文显示文本；未知状态以文本明示，不静默略过。"""
    return STATE_LABELS.get(state, f"未知状态（{state}）")


def game_name(game_id: str | None) -> str:
    if not game_id:
        return "系统"
    return GAME_NAMES.get(game_id, game_id)


# ---------------------------------------------------------------------------
# 控件启用规则
# ---------------------------------------------------------------------------

def control_states(is_running: bool, current_state: str) -> dict[str, bool]:
    """依执行状态计算各控件是否启用。

    - 执行中（含失败 / 暂停，队列尚未停止）禁止再次启动。
    - 失败或暂停时才启用重试与跳过；执行中一律可要求安全停止。
    - 配置检查与备份恢复只在闲置时开放，避免执行中修改第三方配置。
    """
    attention = current_state in ATTENTION_STATES
    return {
        "start_all": not is_running,
        "run_one": not is_running,
        "retry": bool(is_running and attention),
        "skip": bool(is_running and attention),
        "stop": bool(is_running),
        "check_config": not is_running,
        "restore_backup": not is_running,
    }


# ---------------------------------------------------------------------------
# 显示格式化
# ---------------------------------------------------------------------------

def format_event_time(timestamp: str) -> str:
    """把带时区的 ISO 时间戳转为本地 HH:MM:SS；无法解析时原样返回。"""
    try:
        moment = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return str(timestamp)
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%H:%M:%S")


def format_event(event) -> str:
    """把 StatusEvent 格式化为事件记录区的一行文本。"""
    line = (
        f"[{format_event_time(event.timestamp)}] {game_name(event.game_id)}"
        f"／{state_label(event.state)}：{event.message}"
    )
    if event.error_code:
        line += f"（错误码 {event.error_code}）"
    return line


def format_duration(seconds: float) -> str:
    """把秒数格式化为 H:MM:SS；负数按 0 处理。"""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def card_elapsed_text(card, now: float) -> str:
    """卡片计时文本：进行中为「已运行」，结束后为「用时」，未开始为空。"""
    if card.started_at is None:
        return ""
    if card.finished_at is not None:
        return f"用时 {format_duration(card.finished_at - card.started_at)}"
    return f"已运行 {format_duration(now - card.started_at)}"


def format_check_result(result) -> str:
    mark = "通过" if result.ok else "失败"
    line = f"[{mark}] {result.name}（{result.severity}／{result.code}）：{result.message}"
    if result.details:
        line += f" — {result.details}"
    return line


def format_change(change) -> str:
    verified = "已验证" if change.verified else "未验证"
    line = (
        f"{change.tool}／{change.field}：{change.current_display}"
        f" → {change.proposed_display}（{verified}）"
    )
    if change.warning:
        line += f" 警告：{change.warning}"
    return line


def format_backup(backup) -> str:
    return (
        f"{backup.backup_id}｜创建时间 {backup.created_at}"
        f"｜{backup.source_count} 个来源｜{backup.manifest_path}"
    )


# ---------------------------------------------------------------------------
# 配置向导决策
# ---------------------------------------------------------------------------

def selectable_change_ids(changes) -> set:
    """只有 verified=True 的变更允许勾选。"""
    return {c.change_id for c in changes if c.verified}


def validate_change_selection(changes, selected_ids) -> list:
    """返回不允许勾选的 change_id 列表；空列表代表选择合法。"""
    selectable = selectable_change_ids(changes)
    return [cid for cid in selected_ids if cid not in selectable]


# ---------------------------------------------------------------------------
# 卡片状态与事件归并
# ---------------------------------------------------------------------------

@dataclass
class CardState:
    game_id: str
    state: str = "idle"
    message: str = ""
    error_code: str | None = None
    #: time.monotonic() 时刻；本轮尝试从进入 checking 起算。
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class LocalEvent:
    """前端自行生成的事件（例如背景工作异常），字段与 StatusEvent 对齐。"""

    game_id: str | None
    state: str
    message: str
    error_code: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def error_event(exc: BaseException) -> LocalEvent:
    return LocalEvent(
        game_id=None,
        state="failed",
        message=f"后台任务发生意外错误：{exc!r}",
        error_code=FRONTEND_ERROR_CODE,
    )


# ---------------------------------------------------------------------------
# 控制器：编排器与 Tk 接口之间的衔接层
# ---------------------------------------------------------------------------

class UiController:
    """持有编排器引用、事件队列与卡片状态。

    - listener() 由编排器在任意线程调用，只负责放入队列。
    - drain() 由 Tk 主线程通过 root.after 定期取出。
    - 阻塞型调用（run_all、run_one、配置检查、恢复）一律走 run_blocking，
      异常会转成可见的错误事件，不会静默失败。

    bg 可在测试中注入同步替身（例如 lambda fn, cb: cb(fn())），
    避免真的开线程。
    """

    def __init__(self, orchestrator, bg=None):
        self.orchestrator = orchestrator
        self.events: queue.Queue = queue.Queue()
        self.cards = {gid: CardState(game_id=gid) for gid in GAME_ORDER}
        self.last_state = "idle"
        self._bg = bg
        self._run_submitted = False

    # -- 事件通道 ---------------------------------------------------------

    def listener(self, event) -> None:
        """编排器事件监听器；可在背景线程安全调用。"""
        self.events.put(("event", event))

    def drain(self, limit: int = 500) -> list:
        """取出当前队列中的所有项目，不阻塞等待新项目。"""
        items = []
        while len(items) < limit:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        return items

    def apply_event(self, event, now: float | None = None) -> None:
        """把事件归并到对应游戏卡片；系统事件只更新整体状态。

        从非进行中状态进入 checking 视为新一轮尝试（含重试），重新计时；
        进入 FINISHED_STATES 时冻结计时。
        """
        now = time.monotonic() if now is None else now
        card = self.cards.get(event.game_id)
        if card is not None:
            if event.state == "checking" and card.state not in ACTIVE_STATES:
                card.started_at = now
                card.finished_at = None
            elif (event.state in FINISHED_STATES
                  and card.started_at is not None and card.finished_at is None):
                card.finished_at = now
            card.state = event.state
            card.message = event.message
            card.error_code = event.error_code
        self.last_state = event.state

    # -- 背景执行 ---------------------------------------------------------

    def run_blocking(self, fn, on_done=None):
        """在背景线程执行 fn；结果或异常都经由队列回到主线程。"""
        if self._bg is not None:
            return self._bg(fn, on_done)

        def worker():
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - 异常必须转为可见错误
                self.events.put(("event", error_event(exc)))
            else:
                if on_done is not None:
                    self.events.put(("callback", on_done, result))

        thread = threading.Thread(
            target=worker, name="daily-games-ui-bg", daemon=True
        )
        thread.start()
        return thread

    # -- 启动入口 ---------------------------------------------------------

    def _submit_run(self, fn) -> bool:
        """提交一次运行；已提交但工作线程尚未进入编排器时同样拒绝。

        仅由 Tk 主线程调用；标志在工作结束（含异常）时于背景线程清除。
        """
        if self._run_submitted or self.orchestrator.is_running:
            return False
        self._run_submitted = True

        def job():
            try:
                return fn()
            finally:
                self._run_submitted = False

        self.run_blocking(job)
        return True

    def request_start_all(self) -> bool:
        """执行中或已提交未开始时拒绝第二次启动，返回 False。"""
        return self._submit_run(self.orchestrator.run_all)

    def request_run_one(self, game_id: str) -> bool:
        if game_id not in GAME_ORDER:
            raise ValueError(f"未知的游戏 ID：{game_id}")
        return self._submit_run(lambda: self.orchestrator.run_one(game_id))

    # -- 执行中控制（主线程可安全调用） ---------------------------------

    def retry_current(self) -> None:
        self.orchestrator.retry_current()

    def skip_current(self) -> None:
        self.orchestrator.skip_current()

    def request_stop(self) -> None:
        self.orchestrator.request_stop()

    # -- 关闭窗口决策 -----------------------------------------------------

    def close_choices(self) -> tuple:
        """闲置时返回空 tuple（直接关闭）；执行中返回三种明确分支。"""
        if not self.orchestrator.is_running:
            return ()
        return (CLOSE_KEEP, CLOSE_STOP_THEN_EXIT, CLOSE_CANCEL)

    def handle_close_choice(self, choice: str) -> str:
        """返回后续动作：exit / wait-stopped / stay。

        只有 stop 分支会调用 request_stop()；keep 与 cancel 不触碰任何
        进程，前端本身也绝不 kill 游戏或挂机程序。
        """
        if choice == CLOSE_STOP_THEN_EXIT:
            self.orchestrator.request_stop()
            return "wait-stopped"
        if choice == CLOSE_KEEP:
            return "exit"
        if choice == CLOSE_CANCEL:
            return "stay"
        raise ValueError(f"未知的关闭选项：{choice}")

    # -- 配置与恢复 ---------------------------------------------------------

    def check_configuration(self, on_done=None):
        return self.run_blocking(self.orchestrator.check_configuration, on_done)

    def plan_configuration_changes(self, on_done=None):
        return self.run_blocking(
            self.orchestrator.plan_configuration_changes, on_done
        )

    def apply_selected_changes(self, changes, selected_ids, on_done=None):
        """应用前再次验证：verified=False 的变更一律拒绝。"""
        rejected = validate_change_selection(changes, selected_ids)
        if rejected:
            raise ValueError(f"包含未验证的变更，已拒绝应用：{rejected}")
        return self.run_blocking(
            lambda: self.orchestrator.apply_configuration_changes(
                list(selected_ids)
            ),
            on_done,
        )

    def list_backups(self, on_done=None):
        return self.run_blocking(self.orchestrator.list_backups, on_done)

    def restore_backup(self, backup_id: str, confirmed: bool = False, on_done=None):
        """未经二次确认（confirmed=False）时不调用编排器。"""
        if not confirmed:
            return None
        return self.run_blocking(
            lambda: self.orchestrator.restore_backup(backup_id), on_done
        )
