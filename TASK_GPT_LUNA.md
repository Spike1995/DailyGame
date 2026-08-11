# Luna Max 任务书：实现细化、流程编排与集成举证

本任务可与 ZCode 后端、Kimi Code 前端同步开始。Sol Max 负责方向规划，以及需求、范围、架构、冻结接口、兼容性与风险决策和最终验收。本文后续的“你”均指 Luna Max，负责在 Sol 已批准的范围内细化并实现可执行入口、顺序状态机、测试与集成证据；不要代写其他 Agent 已分配的模块。

## Sol / Luna 工作边界

Sol Max 先提供执行简报，并处理执行期间需要升级的决策；Luna Max 按简报细化步骤并执行。

若遇到与 `PLAN.md` 冲突、需要修改冻结接口、跨越文件所有权、添加生产依赖，或涉及安全与兼容性取舍，停止受影响的部分并向 Sol 报告，获得批准后再继续。

## 权威输入与工作位置

开始前完整阅读下列位于项目根目录的控制文档：

- [PLAN.md](./PLAN.md)
- [AGENTS.md](./AGENTS.md)
- 本任务书

上述相对链接以本任务书所在的项目根目录解析，禁止换成任何端点、checkout 或 NAS 的绝对地址。

实现目标目录是 `D:\Daily`。当前该目录为空，Python 版本为 3.14，第一版只使用标准库与 Tkinter。

若本任务书与 `PLAN.md` 冲突，停止受影响的实现并向 Sol 报告，由 Sol 决定后续。不要修改 `PLAN.md`、其他 Agent 的任务书、游戏文件或第三方工具文件。

## 你的文件所有权

你可以创建或修改以下文件：

```text
D:\Daily\daily_games.pyw
D:\Daily\daily_games\__init__.py
D:\Daily\daily_games\orchestrator.py
D:\Daily\启动一键日常.cmd
D:\Daily\tests\test_orchestrator.py
D:\Daily\tests\test_entrypoint.py
```

不要修改下列文件，它们分别由 ZCode 与 Kimi Code 负责：

```text
D:\Daily\daily_games\config_manager.py
D:\Daily\daily_games\process_monitor.py
D:\Daily\daily_games\adapters\**
D:\Daily\config\user.json
D:\Daily\daily_games\ui.py
D:\Daily\daily_games\ui_components.py
D:\Daily\tests\test_config_manager.py
D:\Daily\tests\test_process_monitor.py
D:\Daily\tests\test_adapters.py
D:\Daily\tests\test_ui.py
```

若共用接口需要调整，先向 Sol 提出具体兼容性变更建议；未获批准前，不得修改冻结接口或其他 Agent 拥有的文件。

假设另外两个 Agent 正在同一目录工作。不要回复、搬移或整个项目格式化他们的变更；同步阶段只执行你拥有的测试，集成阶段才执行完整测试。

## 已冻结的跨 Agent 接口

### ZCode 提供的后端

你的代码按以下接口集成，不要创建同名替代实现：

```python
from daily_games.config_manager import BackupInfo, CheckResult, ConfigChange, ConfigManager
from daily_games.process_monitor import ProcessMonitor
from daily_games.adapters import build_adapters
```

接口约定如下：

```python
ConfigManager(config_path, backup_root)
.load() -> dict
.preflight() -> list[CheckResult]
.plan_required_changes() -> list[ConfigChange]
.apply_required_changes(change_ids) -> list[CheckResult]
.backup_files(paths) -> BackupInfo
.patch_json(path, updates) -> list[CheckResult]
.patch_yaml_scalars(path, updates) -> list[CheckResult]
.list_backups() -> list[BackupInfo]
.restore_backup(backup_id) -> list[CheckResult]

build_adapters(config, process_monitor, logger) -> dict[str, GameAdapter]
```

`build_adapters` 的键固定为 `arknights`、`endfield`、`limbus`。每个 `GameAdapter` 提供：

```python
check_environment() -> StepResult
start_game() -> StepResult
wait_game_ready(timeout_s, stop_event) -> StepResult
start_helper() -> StepResult
wait_completion(timeout_s, stop_event) -> StepResult
cleanup() -> StepResult
```

`StepResult` 至少有 `ok`、`code`、`message`、`details` 四个只读字段。适配器自行保存该局的进程归属信息；`cleanup()` 不得结束本程序未启动或未明确接管的进程。

`CheckResult` 的冻结字段为 `name`、`ok`、`code`、`message`、`severity`、`details`。`ConfigChange` 的冻结字段为 `change_id`、`tool`、`field`、`current_display`、`proposed_display`、`verified`、`warning`。`BackupInfo` 的冻结字段为 `backup_id`、`created_at`、`manifest_path`、`source_count`。时间使用带时区的 ISO 8601 字符串，`severity` 只使用 `info`、`warning`、`error`。

### 你提供给 Kimi Code 的编排接口

在 `orchestrator.py` 实现并维持以下公开接口：

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

`run_all()` 与 `run_one()` 是阻塞调用，前端会在背景线程执行。控制方法必须可由 Tkinter 主线程安全调用。`add_listener()` 接收 `StatusEvent`，返回可取消订阅的函数。监听器可能在工作线程被调用，文档注释要明确标记。

#### 耐久行为契约

- `stopped` 事件表示适配器 `cleanup()` 已完成，可能先于 `finally` 将 `is_running` 设为 `False`。关闭界面可判断 `not is_running or current_state == "stopped"`，新任务防重入只可判断 `is_running`。
- `request_stop()` 是合作式请求。内置等待必须接收 `stop_event` 与 timeout；未合作的第三方调用可延迟结束。前端安全停止后最多自动等待 30_000 ms，超时后不得强制结束进程或关闭窗口，必须保留继续等待、仅退出界面与返回主窗口的选择。
- 从根目录一键入口自动启动的完整队列成功结束后，界面延迟 3 秒自动关闭并释放单实例锁；失败、暂停、停止及用户手动启动的任务不得自动关闭界面。
- `check_configuration()` 仅委派 `preflight()`；前端只依 `CheckResult.ok` 判断。`severity == "info"` 时 `ok` 必为 `True`，`ok == False` 时只能是 `warning` 或 `error`。`details` 的类型为 `Mapping[str, object]`，`ConfigChange.change_id` 为可哈希的 `str`。
- 监听器在触发状态转移的调用线程同步执行。取消订阅可重复调用；若转移已获取监听器快照，并行取消后仍可收到一次在途事件。`current_game` 的公开值域只可为 `arknights`、`endfield`、`limbus` 或 `None`。
- 执行前编排器必须依次只读调用 `ConfigManager.preflight()` 与 `plan_required_changes()`：前者失败时不得再规划、建构适配器或启动进程；前者通过但仍有未解决变更时，也不得建构或启动。这两个 gate 与 dry-run 均不得写入第三方配置。
- MAA 必须读取实际 `gui.new.json` 当前配置，确认 `RunDirectly == true` 且 `PostActions` 同时含 `ExitArknights` 与 `ExitSelf`。PC 窗口绑定模式不得把 `ExitEmulator` 当成游戏退出信号；等待时必须同时捕获游戏与 MAA，并等双方全部 PID 退出。
- MaaEnd 必须从 mxu 的实际 `lastActiveInstanceId` 解析具名且至少含一个启用任务的 Win32 活动实例，以官方 `--autostart --instance <name> --quit-after-run` 参数启动，不发送热键、不连接 12701，也不依赖仅适用于安卓控制器的 `CloseGame`。MaaEnd 自退本身不能作为成功信号：必须从该次 MaaEnd PID 的 MaaFramework 顶层事件核对全部启用任务均出现开始事件并进入成功/失败终态；`__SceneCheckAbortPipeline` 的实际节点事件、只启动部分任务、缺少终态或没有任何成功任务都必须判为失败，资源解析器仅加载同名节点定义不算中止。长任务会轮转 `debug/maafw.log`，校验必须从启动前字节边界合并本轮新建的全部 `maafw.bak.*.log` 分片。个别任务失败但其余任务成功时记录警告后继续。MaaEnd 自退后只关闭本轮明确接管的 Endfield。真机已确认 Windows HDR 会触发颜色识别中止，因此运行 MaaEnd 前必须关闭 HDR 与显卡/游戏滤镜。
- AALC 真机启动已确认需要管理员权限（普通进程直接启动返回 `WinError 740`），必须通过精确匹配可执行文件、工作目录、参数 `start --exit 6` 且仅含一个动作的 `DailyGames-AALC-Admin` 最高权限计划任务启动。此官方一次性命令由 V1.5.1 随附文档确认会自动开始任务，并在当前版本同时退出游戏与 AALC；不得再只启动裸程序并依赖会被界面重置的 `autostart` 或完成动作。等待开始时两类进程都必须存在，并等待所有已捕获的游戏与 helper PID 都退出才可成功。任一侧缺失或单侧消失均不可视为完成。
- 每轮开始前必须确认三款游戏和三个挂机程序均未运行；每轮清理后必须再次确认当前游戏与挂机程序都已退出。任一屏障失败时不得启动下一款游戏。
- 启动前屏障按进程名保守拒绝任何同名进程；进入启动流程后，游戏就绪、管理员助手出现与完成判断都必须同时匹配配置中的绝对可执行文件路径。路径不可读、仅返回文件名或同名但路径不同的进程不得成为成功证据。
- Windows 进程与窗口检查必须声明 64 位安全的 Win32 API 函数签名并使用 Unicode 快照。快照失败、进程句柄存取遭拒或强制终止后无法确认退出时必须失败关闭，不得按“进程不存在”继续队列。
- `ProcessMonitor.launch()` 只接受以绝对可执行文件路径开头的参数数组；字符串命令列、相对程序路径与 PATH 搜索必须在创建子进程前拒绝。
- 鹰角 Launcher 1.5.0 的 `--game=<真实 UUID> --reason=4` 只负责请求选择游戏页面，且启动器已运行时真机观察到请求可能不切页，不能作为页面已选择或游戏已启动的信号。明日方舟与终末地的管理员计划任务各使用一个包装动作；包装动作内部先请求启动器，再执行受限的两步按钮自动化：先按游戏名或已验证侧栏位置选择目标游戏，再按“开始游戏”。不能拆成两个任务计划程序动作，因为启动器单实例进程可能返回非零代码并让后续动作被跳过；也不能把完整脚本塞入长 `-EncodedCommand`，因为真机任务计划程序会以 `0xFFFFFFFF` 结束且不进入脚本。安装器必须在 `%ProgramData%\DailyGames\workers` 生成短 `-File` 包装脚本，移除继承权限并只允许 `SYSTEM` 与 `Administrators` 写入，同时让任务参数、脚本 SHA-256 和脚本内版本化元数据三者严格匹配。worker 必须先核对 `Launcher.exe` 的非重解析路径、1.5.0.1504 文件版本和有效签名指纹，再请求启动器；Qt Web 未暴露控件时，只有 `Games.exe` 路径、同一文件版本、签名指纹、窗口标题、前台状态与尺寸全部核对通过，才可按本机已验证相对位置各点击一次。每次坐标输入前还要重新核对前景、窗口矩形与命中窗口，最终仍以对应目标游戏进程出现作为成功判定，并写入受保护的脱敏专用调试日志。
- 未获取用户明确授权前，不启动真机游戏或第三方挂机进程、不发送热键，也不应用或写入真实第三方配置。

`StatusEvent` 的冻结字段为 `game_id: str | None`、`state: str`、`message: str`、`error_code: str | None`、`timestamp: str`。时间使用带时区的 ISO 8601 字符串。状态字符串固定使用：

```text
idle, checking, starting_game, waiting_game, starting_helper,
running, cleaning, completed, failed, paused, skipped,
stopping, stopped
```

`GameResult` 的冻结字段为 `game_id`、`state`、`ok`、`code`、`message`、`started_at`、`ended_at`。`RunReport` 的冻结字段为 `ok`、`stopped`、`results: tuple[GameResult, ...]`。不要让前端解析日志文本来判断状态。

Kimi Code 提供以下入口：

```python
from daily_games.ui import run_gui

run_gui(config_path=r"D:\Daily\config\user.json") -> None
```

## 实现内容

### 顺序状态机

依次执行 `arknights → endfield → limbus`。每款游戏严格走完启动屏障、环境检查、启动游戏、等待窗口、启动挂机工具、等待完成、检查结果、清理与退出屏障。前一款的游戏和挂机进程未全部退出前，不得启动下一款。

失败时发布 `failed` 与 `paused` 事件并暂停队列，等待用户选择重试、跳过或停止。停止是合作式取消；超时或取消时不得强制结束未知子进程。单实例锁要防止两个入口同时执行任务。

每次执行创建独立日志，记录命令、PID、状态转换、起止时间与失败代码，不记录账号、令牌或完整第三方配置内容。`config/user.json` 的 `logging.level` 设为 `DEBUG` 时，日志还会明确记录本次严格串行的游戏顺序。

配置检查、变更规划、应用与恢复只委派给 `ConfigManager`。仅允许应用 `verified=True` 且由用户在前端勾选确认的变更；一次请求只要含未知、重复或未验证 ID，就必须整批拒绝，不得应用有效子集。不要在编排器中硬编码第三方配置路径或键名。备份来源必须是非重解析点的普通文件；清单只有在原子写入、结构验证及所有副本大小与 SHA-256 回读核对完成后才可发布，失败时清理未完成目录并返回结构化错误。多文件应用先从已验证备份建立同目录暂存档与回滚副本，全部写入及解析验证通过后才提交；提交前与每档提交时复核原档、暂存档和回滚副本哈希，中途失败时只倒序恢复本轮已提交目标，不覆盖未提交目标的外部变更。恢复必须先验证整份清单与所有副本，再准备同目录替换及回滚文件；提交或写后验证失败时倒序恢复已提交目标；提交后的目标若又被外部改写，不得以回滚副本覆盖。

### 应用程序入口

`daily_games.pyw` 只负责参数解析与启动：

- 无参数时调用 `run_gui()`。
- `启动一键日常.cmd` 进入 GUI 后，配置检查全部通过即自动执行完整队列，不要求用户再点击“开始全部”。
- `--dry-run` 只执行路径、配置与进程检查，不启动游戏，不修改第三方配置。
- `--dry-run` 全部通过时返回代码 `0`；检查失败时返回非零代码并输出每项检查结果。

`启动一键日常.cmd` 使用根目录相对路径启动 `.pyw`，不得显示常驻终端窗口。入口、UAC 子进程与任务查询只能使用由 Windows 系统目录解析出的绝对 `powershell.exe` / `schtasks.exe` 路径；当前 token SID 直接由 Win32 API 读取，PowerShell 模块自动加载仅允许 Windows 内置模块目录。普通权限阶段必须从同一份配置字节快照生成任务规格及 SHA-256，并把配置与启动脚本哈希传给 UAC 子进程；管理员阶段只能从哈希一致的快照安装任务。会请求 UAC 的鹰角启动器、MAA、MaaEnd 与 AALC 由五个预注册且逐项核验主体、路径、参数、工作目录、任务设置、零触发器和最高权限的计划任务启动；任务主体必须解析为 SID 后与当前 token SID 完全一致，不接受仅凭账号短名称匹配。两个游戏任务各含一个指向 `%ProgramData%\DailyGames\workers` 受保护脚本的短 `-File` 管理员包装动作，脚本 ACL 或哈希不匹配时拒绝执行，并在内部严格按“请求启动器 → 选择游戏 → 开始游戏”顺序执行；三个助手任务各含一个直接动作。普通编排器不得整体提权。首次安装或任务定义变化时，入口先说明下一步需要 UAC，再最多请求一次授权；说明对话框无法显示或等待期间脚本／配置发生变化时，不得继续安装。用户取消时应显示“安装未完成”并停止，不得归类为普通一键运行故障。任务就绪后的一键日常不得再次要求点击授权。不要在此阶段加入打包依赖；单文件 `.exe` 属于后续可选交付。

### 测试与集成

使用 `unittest` 与假的适配器测试下列情境：正常三款顺序、单款执行、已存在进程不重复启动、失败后暂停、重试、跳过、停止与超时。测试不得启动真实游戏或修改真实配置。

同步开发期间只修改自己的文件。其他两个 Agent 尚未交付时，可用测试替身验证状态机，不要创建后端或前端临时文件占位。三方成果齐备后，再执行完整导入与测试。

## 执行顺序

1. 创建你拥有的包入口与状态机数据类型。
2. 以假的适配器完成状态机与控制流程测试。
3. 接上 ZCode 的冻结接口与 Kimi Code 的 `run_gui()`。
4. 执行语法检查、所属单元测试与完整测试。
5. 执行一次 `--dry-run`。未获取用户在当前会话的明确授权前，不执行真实游戏启动验证，也不修改 MAA、MaaEnd 或 AALC 配置。

## 执行证据与交付

至少提供以下证据，必须含退出码与关键结果：

```powershell
py -3.14 -m compileall -q D:\Daily\daily_games D:\Daily\daily_games.pyw
py -3.14 -m unittest discover -s D:\Daily\tests -p "test_orchestrator.py" -v
py -3.14 -m unittest discover -s D:\Daily\tests -p "test_entrypoint.py" -v
py -3.14 D:\Daily\daily_games.pyw --dry-run
```

三方代码到齐后，再执行：

```powershell
py -3.14 -m unittest discover -s D:\Daily\tests -v
```

Luna Max 的交付报告需列出已修改文件、接口偏差、测试命令与退出码、未执行的真机验证、剩余风险。此报告仅作为 Sol 最终验收的输入，不构成最终验收；不要只报告“已完成”或“测试已开始”。
