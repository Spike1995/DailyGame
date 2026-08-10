# Kimi Code 任务书：Tkinter 前端实现

本任务可与 GPT Luna 编排、ZCode 后端同步开始。你只负责图形界面与前端互动，通过已冻结的 `DailyOrchestrator` 接口驱动流程；不要在 UI 内重写状态机、进程监控或第三方配置逻辑。

## 权威输入与工作位置

开始前完整阅读：

- [PLAN.md](./PLAN.md)
- [AGENTS.md](./AGENTS.md)
- 本任务书

实现目标目录是 `D:\Daily`。使用 Python 3.14 内置 Tkinter 与 `ttk`，不得增加执行期第三方依赖。

## 你的文件所有权

你可以创建或修改：

```text
D:\Daily\daily_games\ui.py
D:\Daily\daily_games\ui_components.py
D:\Daily\tests\test_ui.py
```

若单一 `ui.py` 已足够，可不创建 `ui_components.py`。不要修改 `daily_games.pyw`、`orchestrator.py`、后端模块、适配器、`config\user.json`、启动脚本或其他 Agent 的测试。

遇到接口缺口时，在交付报告提出具体方法名称、参数与使用情境，不要越界修改其他 Agent 的文件。

假设另外两个 Agent 正在同一目录工作。不要回复、搬移或整个项目格式化他们的变更；同步阶段只执行 `test_ui.py`，完整测试留给 GPT Luna 的集成阶段。

## 已冻结的编排接口

前端从 GPT Luna 的模块导入：

```python
from daily_games.orchestrator import DailyOrchestrator, StatusEvent
```

可使用的公开接口如下：

```python
DailyOrchestrator(config_path)
.add_listener(listener) -> callable
.dry_run() -> list[CheckResult]
.run_all() -> RunReport
.run_one(game_id) -> RunReport
.retry_current() -> None
.skip_current() -> None
.request_stop() -> None
.check_configuration() -> list[CheckResult]
.plan_configuration_changes() -> list[ConfigChange]
.apply_configuration_changes(change_ids) -> list[CheckResult]
.list_backups() -> list[BackupInfo]
.restore_backup(backup_id) -> list[CheckResult]
.is_running -> bool
.current_game -> str | None
.current_state -> str
```

`run_all()`、`run_one()`、配置检查与恢复可能阻塞，必须放在背景线程。`retry_current()`、`skip_current()`、`request_stop()` 可由主线程安全调用。

`StatusEvent` 的冻结字段为 `game_id: str | None`、`state: str`、`message: str`、`error_code: str | None`、`timestamp: str`。时间使用带时区的 ISO 8601 字符串。状态值固定为：

```text
idle, checking, starting_game, waiting_game, starting_helper,
running, cleaning, completed, failed, paused, skipped,
stopping, stopped
```

`CheckResult` 的字段为 `name`、`ok`、`code`、`message`、`severity`、`details`。`ConfigChange` 的字段为 `change_id`、`tool`、`field`、`current_display`、`proposed_display`、`verified`、`warning`。`BackupInfo` 的字段为 `backup_id`、`created_at`、`manifest_path`、`source_count`。`GameResult` 的字段为 `game_id`、`state`、`ok`、`code`、`message`、`started_at`、`ended_at`；`RunReport` 的字段为 `ok`、`stopped`、`results`。前端只读取这些公开字段。

你的模块对 GPT Luna 提供：

```python
run_gui(config_path=r"D:\Daily\config\user.json") -> None
```

不要在模块导入时创建 Tk 根窗口或自动进入 `mainloop()`。

## 接口功能

主窗口至少包含：

- 三款游戏状态卡片，固定 ID 为 `arknights`、`endfield`、`limbus`，显示中文名称、当前阶段、最近讯息与错误。
- “开始全部”与三个“单独执行”入口。
- “重试当前任务”“跳过当前任务”“安全停止队列”。
- “检查第三方配置”“恢复备份”“打开日志目录”。
- 总状态、当前游戏与可卷动的事件记录区。

视觉层级以状态辨识和可操作性为主。使用 `ttk`，支持 Windows 常见 DPI 与窗口缩放；不加入图片、动画或外部主题包。状态不能只靠颜色表达，需同时显示文本。

## 互动与线程规则

- Tkinter 控件只在主线程更新。监听器收到事件后放入 `queue.Queue`，再由 `root.after(...)` 定期取出并更新画面。
- 执行中停用“开始全部”与所有单款启动按钮，避免重复启动；停止完成后再恢复。
- `failed` 或 `paused` 时突出显示错误码与原因，并启用重试、跳过、停止。正常执行时不要启用不适用的控制。
- 背景工作中的异常要转为可见错误，不可让 Tkinter 回调静默失败。
- 前端不以解析日志文本推算状态，也不直接操作进程、热键、游戏窗口或第三方配置文件。

## 关闭窗口

未执行任务时可正常退出。任务执行中按下关闭按钮时，显示三项明确选择：

1. 保留当前游戏与挂机进程并退出界面。
2. 调用 `request_stop()`，等待流程进入 `stopped` 后退出。
3. 取消关闭并返回主窗口。

不得直接 `kill` 进程。选择安全停止时，画面要显示停止中，避免用户以为程序无响应；等待动作不能阻塞 Tkinter 主线程。

## 配置与恢复画面

首次启动时显示配置向导，先调用 `check_configuration()`，再以 `plan_configuration_changes()` 列出每项变更的工具、字段、当前值、建议值、验证状态与警告。只有 `verified=True` 的项目可勾选；送出前再次确认，并把勾选的 `change_id` 传给 `apply_configuration_changes()`。

“检查第三方配置”逐项显示检查结果。“恢复备份”先调用 `list_backups()`，显示时间与来源摘要，要求二次确认后才调用 `restore_backup()`。前端不得自行读写 MAA、MaaEnd 或 AALC 配置。

若没有备份、检查失败或恢复部分失败，保留详细结果供用户复制，不以单一“操作失败”取代原因。

## 可测试性

把状态到显示文本、按钮启用规则与事件归并写成不依赖 Tk 根窗口的函数或小型模型。`test_ui.py` 至少覆盖：

- 所有状态的中文显示与控件启用规则。
- 执行中拒绝第二次启动。
- 事件从背景线程送达时经队列转交。
- 失败后重试、跳过、停止按钮调用正确方法。
- 关闭窗口三种分支不直接终止进程。
- 配置检查与备份恢复使用编排器公开接口。
- 首次配置向导不能勾选或应用 `verified=False` 的变更。

使用假的编排器完成测试。同步开发期间不要创建假的 `orchestrator.py` 覆盖 GPT Luna 的文件；在测试内以 mock 或测试替身注入。

## 验收与报告

至少执行并报告每个命令的退出码与关键结果：

```powershell
py -3.14 -m compileall -q D:\Daily\daily_games\ui.py D:\Daily\daily_games\ui_components.py
py -3.14 -m unittest discover -s D:\Daily\tests -p "test_ui.py" -v
```

若未创建 `ui_components.py`，语法检查命令移除该路径。可用编排器替身手动打开 UI 检查布局，但不得启动真实游戏、发送热键或修改第三方配置。

最终报告需列出已修改文件、窗口与互动功能、测试命令及退出码、尚未完成的真实集成验证、需要 GPT Luna 配合的接口事项。
