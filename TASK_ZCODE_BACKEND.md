# ZCode 任务书：后端可靠性实现与优化

本任务可与 GPT Luna 编排、Kimi Code 前端同步开始。你负责配置管理、进程监控与三款游戏适配器。工作重点是安全、可测试、可恢复；不要修改编排器或图形界面。

## 权威输入与工作位置

开始前完整阅读：

- [PLAN.md](./PLAN.md)
- [AGENTS.md](./AGENTS.md)
- 本任务书

实现目标目录是 `D:\Daily`。Python 版本为 3.14，第一版不得增加执行期第三方依赖。

`PLAN.md` 记录的第三方工具路径当前存在，但部分启动方式、完成标志与配置键仍未经真机验证。对未验证信息使用可诊断的失败结果，不要猜测接口、日志字符串或窗口坐标。

## 你的文件所有权

你可以创建或修改：

```text
D:\Daily\daily_games\config_manager.py
D:\Daily\daily_games\process_monitor.py
D:\Daily\daily_games\adapters\__init__.py
D:\Daily\daily_games\adapters\base.py
D:\Daily\daily_games\adapters\maa.py
D:\Daily\daily_games\adapters\maaend.py
D:\Daily\daily_games\adapters\aalc.py
D:\Daily\config\user.json
D:\Daily\tests\test_config_manager.py
D:\Daily\tests\test_process_monitor.py
D:\Daily\tests\test_adapters.py
```

不要修改 `daily_games.pyw`、`orchestrator.py`、`ui.py`、`ui_components.py`、启动脚本或其他 Agent 的测试。若需要变更共用接口，在交付报告中提出，不要越界修改文件。

假设另外两个 Agent 正在同一目录工作。不要回退、搬移或格式化整个项目中的其他变更；只在自己的测试内创建替身，不要创建会遮蔽正式模块的临时文件。

## 已冻结的输出接口

### 配置管理

在 `config_manager.py` 提供：

```python
@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    code: str
    message: str
    severity: str
    details: Mapping[str, object]

@dataclass(frozen=True)
class BackupInfo:
    backup_id: str
    created_at: str
    manifest_path: Path
    source_count: int

@dataclass(frozen=True)
class ConfigChange:
    change_id: str
    tool: str
    field: str
    current_display: str
    proposed_display: str
    verified: bool
    warning: str

class ConfigManager:
    def __init__(self, config_path, backup_root): ...
    def load(self) -> dict: ...
    def preflight(self) -> list[CheckResult]: ...
    def plan_required_changes(self) -> list[ConfigChange]: ...
    def apply_required_changes(self, change_ids) -> list[CheckResult]: ...
    def backup_files(self, paths) -> BackupInfo: ...
    def patch_json(self, path, updates) -> list[CheckResult]: ...
    def patch_yaml_scalars(self, path, updates) -> list[CheckResult]: ...
    def list_backups(self) -> list[BackupInfo]: ...
    def restore_backup(self, backup_id) -> list[CheckResult]: ...
```

方法可接受 `str` 或 `pathlib.Path`。`created_at` 使用带时区的 ISO 8601 字符串，`severity` 只使用 `info`、`warning`、`error`。`apply_required_changes()` 必须拒绝 `verified=False`、不存在或重复的变更 ID，并在任何写入前统一创建备份。失败时返回可显示的稳定错误码或抛出具有明确说明的自定义异常，不得以模糊布尔值隐藏原因。

### 进程监控

在 `process_monitor.py` 提供 `ProcessMonitor` 与进程记录类型，至少支持：

```python
find_by_executable(name)
launch(command, cwd=None)
wait_for_process(name, timeout_s, stop_event)
wait_for_window(pid, timeout_s, stop_event)
wait_for_exit(pid, timeout_s, stop_event)
terminate_if_owned(pid)
```

记录必须区分 `owned=True` 与现有进程。只允许 `terminate_if_owned()` 结束本次由程序启动或明确接管的挂机工具。轮询需尊重停止事件与超时，不可永久阻塞。

### 游戏适配器

在 `adapters/base.py` 定义只读的 `StepResult`，冻结字段为 `ok: bool`、`code: str`、`message: str`、`details: Mapping[str, object]`，并定义 `GameAdapter` 协议。各适配器公开：

```python
check_environment() -> StepResult
start_game() -> StepResult
wait_game_ready(timeout_s, stop_event) -> StepResult
start_helper() -> StepResult
wait_completion(timeout_s, stop_event) -> StepResult
cleanup() -> StepResult
```

在 `adapters/__init__.py` 提供：

```python
build_adapters(config, process_monitor, logger) -> dict[str, GameAdapter]
```

返回键固定为 `arknights`、`endfield`、`limbus`。此接口由 GPT Luna 的状态机直接使用。

## 实现要求

### `config\user.json`

创建可直接加载的默认配置，至少包含固定执行顺序、已确认的可执行文件路径、日志与备份目录、各等待阶段超时、启动方式与完成检测配置。使用稳定 ID `arknights`、`endfield`、`limbus`，显示名称另设字段。

默认值需反映计划：两款鹰角游戏经 `Launcher.exe` 启动，边狱巴士优先使用 `steam://run/1973530`。所有真实配置写入默认关闭，未验证的热键、日志规则或窗口标题要标记为待验证。

### 安全配置管理

- 写入第三方配置前，创建 `D:\Daily\backups\<时间戳>\`，并生成含原始绝对路径、SHA-256、大小与修改时间的列表。
- JSON 只更新指定键并保留未知键。先写同目录临时文件，重新解析验证后再用原子替换。
- YAML 不导入 PyYAML。只对唯一且有边界的标量键执行保留格式的替换；键缺失、重复、缩进不确定或原文件疑似损坏时拒绝写入。
- 恢复前再次备份当前版本，核对清单与哈希，避免部分恢复。
- `plan_required_changes()` 只报告计划中需要的一键执行字段，对敏感值脱敏。尚未验证的配置显示给用户，但不得由 `apply_required_changes()` 应用。
- 测试只操作临时目录，不得接触真实 MAA、MaaEnd 或 AALC 配置。

### 进程与窗口

只使用 Python 标准库。可通过 `subprocess` 与 Windows `ctypes` 完成进程和主窗口检测。进程比较要规范化大小写与完整路径，避免只靠目录名称判断 MAA 版本。

启动函数要处理含空格的路径，不以手工字符串拼接 shell 命令。保留 PID、启动时间与归属。不要占用或重设 MaaEnd 使用的 `127.0.0.1:12701`。

### 三款适配器

- MAA：读取实际 `gui.new.json`，要求 `RunDirectly` 以及适合 PC 窗口绑定的 `ExitArknights, ExitSelf`；现有进程不得复用，不可使用坐标点击。
- MaaEnd：拒绝复用现有进程，以 MXU 官方 `--autostart --instance <name> --quit-after-run` 参数运行活动 Win32 实例；不使用热键、12701 或安卓专用 `CloseGame`，完成后只关闭本轮明确接管的游戏。
- AALC：以 Steam 协议启动游戏，直接可执行文件仅作为可配置的备用方案。预检要指出 `game_path`、`autostart`、退出动作与游戏语言风险；韩文界面不得静默视为兼容。

各适配器要区分“环境不符”“启动失败”“等待超时”“工具异常退出”“停止要求”与“完成信号未配置”。完成判断不得仅以挂机程序消失推断成功，除非同时有已验证的完成信号。

## 测试范围

使用 `unittest`、`tempfile` 与 mock 覆盖：

- 配置加载、缺失键、错误类型与路径不存在。
- 配置变更规划，以及未验证、未知或重复变更 ID 被拒绝。
- 备份列表、哈希、原子写入、写入后验证与完整恢复。
- YAML 唯一键更新，以及重复键、缺键、疑似损坏时拒绝写入。
- 现有进程阻挡、启动进程归属、超时、取消与禁止结束非 owned 进程。
- 三个适配器的正常路径、未验证配置、工具提前退出与完成信号不足。

同步开发期间只执行你拥有的测试文件，避免其他 Agent 尚未完成的导入影响判断。

## 验收与报告

至少执行并报告每个命令的退出码与关键结果：

```powershell
py -3.14 -m compileall -q D:\Daily\daily_games\config_manager.py D:\Daily\daily_games\process_monitor.py D:\Daily\daily_games\adapters
py -3.14 -m unittest discover -s D:\Daily\tests -p "test_config_manager.py" -v
py -3.14 -m unittest discover -s D:\Daily\tests -p "test_process_monitor.py" -v
py -3.14 -m unittest discover -s D:\Daily\tests -p "test_adapters.py" -v
```

未获取用户在当前会话的明确授权前，不启动真实游戏、不发送热键、不修改第三方真实配置。

最终报告需列出已修改文件、公开接口、测试命令与退出码、未验证的启动或完成信号、任何需要 GPT Luna 集成处理的兼容性事项。
