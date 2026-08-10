param(
    [switch]$InstallElevated,
    [switch]$RepairTasks
)

$ErrorActionPreference = 'Stop'
$LegacyTaskName = 'DailyGames-OneClick-Admin'
$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$EntryPath = Join-Path $ProjectRoot 'daily_games.pyw'
$ConfigPath = Join-Path $ProjectRoot 'config\user.json'

function Show-LaunchError {
    param([string]$Message)
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            $Message,
            '一键日常',
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        ) | Out-Null
    }
    catch {
        # 消息框不可用时保留静默失败；调用入口本身不应留下终端窗口。
    }
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Get-PythonwPath {
    $command = Get-Command 'pythonw.exe' -ErrorAction Stop
    return [System.IO.Path]::GetFullPath($command.Source)
}

function Invoke-SoftwarePathSelfCheck {
    param([string]$PythonwPath)

    # 必須先完成位址自查，後續排程規格才會使用更新後的執行檔與工作目錄。
    # pythonw 讓這個啟動前步驟不顯示終端機視窗，退出碼仍可供失敗關閉判斷。
    $repairArguments = (
        '"' + $EntryPath.Replace('"', '\"') + '" --repair-paths'
    )
    $repair = Start-Process `
        -FilePath $PythonwPath `
        -ArgumentList $repairArguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($repair.ExitCode -ne 0) {
        throw (
            '软件地址自查未通过。请打开程序执行 --dry-run 查看候选、' +
            '搜索根目录或多版本冲突。'
        )
    }
}

function ConvertTo-TaskArguments {
    param([object[]]$Values)
    $parts = @(
        foreach ($value in @($Values)) {
            $text = [string]$value
            if ($text -match '[\s"]') {
                '"' + $text.Replace('"', '\"') + '"'
            }
            else {
                $text
            }
        }
    )
    return ($parts -join ' ')
}

function New-StartButtonActionSpec {
    param(
        [object]$Automation,
        [string]$GameProcessName,
        [string]$LauncherExecutable,
        [object[]]$LauncherArguments,
        [string]$WorkingDirectory,
        [string]$DebugLogPath
    )
    if ($null -eq $Automation -or -not [bool]$Automation.enabled) {
        return $null
    }
    if (-not [bool]$Automation.verified) {
        throw '启动器开始游戏按钮自动化尚未验证。'
    }
    if (
        [string]$Automation.window_title -ne '鹰角启动器' -or
        [string]$Automation.window_process_name -ne 'Games' -or
        [string]$Automation.button_name -ne '开始游戏'
    ) {
        throw '启动器按钮自动化只允许已核验的鹰角启动器“开始游戏”按钮。'
    }

    $processName = [System.IO.Path]::GetFileNameWithoutExtension(
        $GameProcessName
    )
    if ($processName -notmatch '^[A-Za-z0-9_.-]+$') {
        throw "游戏进程名不适合按钮自动化：$GameProcessName"
    }
    $expectedGameButtonName = switch ($processName) {
        'Arknights' { '明日方舟' }
        'Endfield' { '明日方舟：终末地' }
        default { throw "不支持的鹰角游戏进程：$GameProcessName" }
    }
    $gameButtonName = [string]$Automation.game_button_name
    if ($gameButtonName -ne $expectedGameButtonName) {
        throw "游戏选择按钮与目标进程不匹配：$GameProcessName"
    }
    $resolvedLauncherExecutable = [System.IO.Path]::GetFullPath(
        $LauncherExecutable
    )
    $launcherArgumentValues = @(
        foreach ($value in @($LauncherArguments)) {
            [string]$value
        }
    )
    if (
        $launcherArgumentValues.Count -ne 2 -or
        $launcherArgumentValues[0] -notmatch '^--game=[0-9a-f]{32}$' -or
        $launcherArgumentValues[1] -ne '--reason=4'
    ) {
        throw '鹰角启动器包装动作只允许已核验的游戏 ID 与 --reason=4。'
    }
    if (
        -not (Test-Path -LiteralPath $resolvedLauncherExecutable -PathType Leaf) -or
        -not [System.IO.Path]::GetDirectoryName(
            $resolvedLauncherExecutable
        ).Equals(
            $WorkingDirectory,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw '鹰角启动器程序与包装动作工作目录不匹配。'
    }
    $expectedWindowProcessPath = [System.IO.Path]::GetFullPath(
        [string]$Automation.window_process_path
    )
    $expectedWindowVersion = [string]$Automation.window_process_version
    $expectedSignerThumbprint = (
        [string]$Automation.window_signer_thumbprint
    ).Replace(' ', '').ToUpperInvariant()
    if (
        -not (Test-Path -LiteralPath $expectedWindowProcessPath -PathType Leaf) -or
        -not $expectedWindowProcessPath.StartsWith(
            $WorkingDirectory + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw '启动器窗口进程路径不在已核验的安装目录内。'
    }
    if (
        (Get-Item -LiteralPath $expectedWindowProcessPath).VersionInfo.FileVersion -ne
        $expectedWindowVersion
    ) {
        throw '启动器窗口进程版本与已核验版本不一致。'
    }
    $windowSignature = Get-AuthenticodeSignature `
        -LiteralPath $expectedWindowProcessPath
    if (
        $windowSignature.Status -ne 'Valid' -or
        $null -eq $windowSignature.SignerCertificate -or
        $windowSignature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne
        $expectedSignerThumbprint
    ) {
        throw '启动器窗口进程签名与已核验签名不一致。'
    }
    $initialDelay = [int]$Automation.initial_delay_s
    $selectionDelay = [int]$Automation.selection_delay_s
    $timeout = [int]$Automation.timeout_s
    $selectorXRatio = [double]$Automation.selector_x_ratio
    $selectorYRatio = [double]$Automation.selector_y_ratio
    $xRatio = [double]$Automation.fallback_x_ratio
    $yRatio = [double]$Automation.fallback_y_ratio
    if ($initialDelay -lt 2 -or $initialDelay -gt 30) {
        throw '启动器按钮自动化初始等待必须在 2 到 30 秒之间。'
    }
    if ($timeout -lt 5 -or $timeout -gt 120) {
        throw '启动器按钮自动化超时必须在 5 到 120 秒之间。'
    }
    if ($selectionDelay -lt 1 -or $selectionDelay -gt 10) {
        throw '启动器游戏选择后的等待必须在 1 到 10 秒之间。'
    }
    if ($selectorXRatio -lt 0.02 -or $selectorXRatio -gt 0.12) {
        throw '启动器游戏选择按钮横向相对位置超出安全范围。'
    }
    if ($selectorYRatio -lt 0.15 -or $selectorYRatio -gt 0.80) {
        throw '启动器游戏选择按钮纵向相对位置超出安全范围。'
    }
    if ($xRatio -lt 0.65 -or $xRatio -gt 0.95) {
        throw '启动器按钮横向相对位置超出安全范围。'
    }
    if ($yRatio -lt 0.70 -or $yRatio -gt 0.95) {
        throw '启动器按钮纵向相对位置超出安全范围。'
    }
    $resolvedDebugLog = [System.IO.Path]::GetFullPath($DebugLogPath)
    if (-not $resolvedDebugLog.StartsWith(
        $ProjectRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw '启动器按钮自动化日志必须位于项目根目录内。'
    }

    $metadata = [ordered]@{
        version = 3
        launcher_executable = $resolvedLauncherExecutable
        working_directory = $WorkingDirectory
        arguments = ConvertTo-TaskArguments $launcherArgumentValues
        target_process = $GameProcessName
    }
    $metadataJson = $metadata | ConvertTo-Json -Compress
    $metadataEncoded = [Convert]::ToBase64String(
        [System.Text.Encoding]::UTF8.GetBytes($metadataJson)
    )

    $source = @'
# DAILY_GAMES_LAUNCHER_WORKER_V3=__WRAPPER_METADATA__
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedSha256
)

try {
    $actualWorkerHash = (
        Get-FileHash `
            -LiteralPath $PSCommandPath `
            -Algorithm SHA256 `
            -ErrorAction Stop
    ).Hash
    if (-not $actualWorkerHash.Equals(
        $ExpectedSha256,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        exit 10
    }
}
catch {
    exit 10
}

$ErrorActionPreference = 'SilentlyContinue'
$nativeSource = @"
using System;
using System.Runtime.InteropServices;
using System.Threading;

namespace DailyGames {
    [StructLayout(LayoutKind.Sequential)]
    public struct LauncherRect {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct LauncherPoint {
        public int X;
        public int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct MouseInput {
        public int Dx;
        public int Dy;
        public uint MouseData;
        public uint Flags;
        public uint Time;
        public UIntPtr ExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct Input {
        public uint Type;
        public MouseInput Mouse;
    }

    public static class LauncherNative {
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern IntPtr FindWindow(string className, string windowName);
        [DllImport("user32.dll")]
        private static extern bool GetWindowRect(IntPtr window, out LauncherRect rect);
        [DllImport("user32.dll")]
        private static extern bool ShowWindowAsync(IntPtr window, int command);
        [DllImport("user32.dll")]
        private static extern bool BringWindowToTop(IntPtr window);
        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr window);
        [DllImport("user32.dll")]
        private static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")]
        public static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
        [DllImport("kernel32.dll")]
        private static extern uint GetCurrentThreadId();
        [DllImport("user32.dll")]
        private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);
        [DllImport("user32.dll")]
        private static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")]
        private static extern bool GetCursorPos(out LauncherPoint point);
        [DllImport("user32.dll")]
        private static extern uint SendInput(uint count, Input[] inputs, int size);
        [DllImport("user32.dll")]
        private static extern bool SetProcessDPIAware();

        public static bool ActivateVerifiedWindow(IntPtr window) {
            if (window == IntPtr.Zero) return false;
            uint ignored;
            uint currentThread = GetCurrentThreadId();
            uint targetThread = GetWindowThreadProcessId(window, out ignored);
            IntPtr foreground = GetForegroundWindow();
            uint foregroundThread = foreground == IntPtr.Zero
                ? 0
                : GetWindowThreadProcessId(foreground, out ignored);
            bool targetAttached = false;
            bool foregroundAttached = false;
            try {
                if (targetThread != 0 && targetThread != currentThread) {
                    targetAttached = AttachThreadInput(currentThread, targetThread, true);
                }
                if (
                    foregroundThread != 0 &&
                    foregroundThread != currentThread &&
                    foregroundThread != targetThread
                ) {
                    foregroundAttached = AttachThreadInput(
                        currentThread,
                        foregroundThread,
                        true
                    );
                }
                ShowWindowAsync(window, 9);
                BringWindowToTop(window);
                SetForegroundWindow(window);
                Thread.Sleep(350);
                return GetForegroundWindow() == window;
            }
            finally {
                if (foregroundAttached) {
                    AttachThreadInput(currentThread, foregroundThread, false);
                }
                if (targetAttached) {
                    AttachThreadInput(currentThread, targetThread, false);
                }
            }
        }

        public static bool ClickAt(IntPtr window, double xRatio, double yRatio) {
            SetProcessDPIAware();
            LauncherRect rect;
            if (window == IntPtr.Zero || !GetWindowRect(window, out rect)) return false;
            int width = rect.Right - rect.Left;
            int height = rect.Bottom - rect.Top;
            if (width < 900 || height < 600) return false;
            if (GetForegroundWindow() != window) return false;

            int x = rect.Left + (int)Math.Round(width * xRatio);
            int y = rect.Top + (int)Math.Round(height * yRatio);
            LauncherPoint oldPoint;
            GetCursorPos(out oldPoint);
            if (!SetCursorPos(x, y)) return false;

            Input[] inputs = new Input[2];
            inputs[0].Type = 0;
            inputs[0].Mouse.Flags = 0x0002;
            inputs[1].Type = 0;
            inputs[1].Mouse.Flags = 0x0004;
            uint sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(Input)));
            Thread.Sleep(80);
            SetCursorPos(oldPoint.X, oldPoint.Y);
            return sent == inputs.Length;
        }
    }
}
"@
$nativeTypesReady = $true
try {
    Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop
    Add-Type -TypeDefinition $nativeSource -ErrorAction Stop
}
catch {
    $nativeTypesReady = $false
}

function Write-AutomationStatus {
    param([string]$Code)
    if ($script:lastAutomationStatus -eq $Code) {
        return
    }
    $script:lastAutomationStatus = $Code
    try {
        $logDirectory = Split-Path -Parent '__DEBUG_LOG__'
        if (-not (Test-Path -LiteralPath $logDirectory -PathType Container)) {
            New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        }
        Add-Content `
            -LiteralPath '__DEBUG_LOG__' `
            -Encoding UTF8 `
            -Value ('{0} target=__PROCESS_NAME__ status={1}' -f (
                [DateTimeOffset]::Now.ToString('o')
            ), $Code)
    }
    catch {
        # 诊断日志失败不能扩大为窗口操作；主流程仍按状态码安全失败。
    }
}

function Invoke-AccessibleButton {
    param(
        [IntPtr]$Window,
        [string]$Name
    )
    try {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle($Window)
        $condition = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            $Name
        )
        $button = $root.FindFirst(
            [System.Windows.Automation.TreeScope]::Descendants,
            $condition
        )
        if ($null -eq $button) {
            return $false
        }
        $invoke = $button.GetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern
        )
        $invoke.Invoke()
        return $true
    }
    catch {
        return $false
    }
}

function Find-LauncherWindowCandidate {
    $currentSessionId = (
        [System.Diagnostics.Process]::GetCurrentProcess()
    ).SessionId
    $candidates = @(
        Get-Process -Name 'Games' -ErrorAction SilentlyContinue |
            Where-Object {
                try {
                    $_.SessionId -eq $currentSessionId -and
                    $_.MainWindowHandle -ne [IntPtr]::Zero -and
                    $_.MainWindowTitle -eq '鹰角启动器'
                }
                catch {
                    $false
                }
            }
    )
    if ($candidates.Count -eq 0) {
        $script:windowVerificationStatus = 'launcher_window_candidate_missing'
        return [IntPtr]::Zero
    }
    if ($candidates.Count -ne 1) {
        $script:windowVerificationStatus = 'launcher_window_candidate_ambiguous'
        return [IntPtr]::Zero
    }
    return [IntPtr]$candidates[0].MainWindowHandle
}

function Test-VerifiedLauncherWindow {
    param([IntPtr]$Window)
    if ($Window -eq [IntPtr]::Zero) {
        $script:windowVerificationStatus = 'verified_window_handle_missing'
        return $false
    }
    try {
        [uint32]$ownerPid = 0
        [void][DailyGames.LauncherNative]::GetWindowThreadProcessId(
            $Window,
            [ref]$ownerPid
        )
        if ($ownerPid -eq 0) {
            $script:windowVerificationStatus = 'verified_window_owner_pid_missing'
            return $false
        }
        $owner = Get-Process -Id $ownerPid -ErrorAction Stop
        if ($owner.ProcessName -ne 'Games') {
            $script:windowVerificationStatus = 'verified_window_process_mismatch'
            return $false
        }
        $ownerPath = [System.IO.Path]::GetFullPath($owner.MainModule.FileName)
        if (-not $ownerPath.Equals(
            '__WINDOW_PROCESS_PATH__',
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            $script:windowVerificationStatus = 'verified_window_path_mismatch'
            return $false
        }
        if ((Get-Item -LiteralPath $ownerPath).VersionInfo.FileVersion -ne '__WINDOW_VERSION__') {
            $script:windowVerificationStatus = 'verified_window_version_mismatch'
            return $false
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $ownerPath
        $signatureMatches = (
            $signature.Status -eq 'Valid' -and
            $null -ne $signature.SignerCertificate -and
            $signature.SignerCertificate.Thumbprint.ToUpperInvariant() -eq
                '__WINDOW_SIGNER_THUMBPRINT__'
        )
        if (-not $signatureMatches) {
            $script:windowVerificationStatus = 'verified_window_signature_mismatch'
            return $false
        }
        $script:windowVerificationStatus = 'verified_window_ready'
        return $true
    }
    catch {
        $script:windowVerificationStatus = 'verified_window_check_exception'
        return $false
    }
}

$script:lastAutomationStatus = ''
$script:windowVerificationStatus = 'verified_window_not_checked'
if (-not $nativeTypesReady) {
    Write-AutomationStatus -Code 'native_type_load_failed'
    exit 7
}
Write-AutomationStatus -Code 'started'
try {
    Start-Process `
        -FilePath '__LAUNCHER_EXECUTABLE__' `
        -ArgumentList @('__LAUNCHER_ARGUMENT_1__', '__LAUNCHER_ARGUMENT_2__') `
        -WorkingDirectory '__LAUNCHER_WORKING_DIRECTORY__' `
        -ErrorAction Stop | Out-Null
    Write-AutomationStatus -Code 'launcher_dispatched'
}
catch {
    Write-AutomationStatus -Code 'launcher_dispatch_failed'
    exit 6
}
Start-Sleep -Seconds __INITIAL_DELAY__
$deadline = (Get-Date).AddSeconds(__TIMEOUT__)
do {
    if (Get-Process -Name '__PROCESS_NAME__' -ErrorAction SilentlyContinue) {
        Write-AutomationStatus -Code 'target_already_running'
        exit 0
    }
    $window = Find-LauncherWindowCandidate
    if ($window -eq [IntPtr]::Zero) {
        Write-AutomationStatus -Code $script:windowVerificationStatus
        Start-Sleep -Seconds 1
        continue
    }
    if (-not (Test-VerifiedLauncherWindow -Window $window)) {
        Write-AutomationStatus -Code $script:windowVerificationStatus
        Start-Sleep -Seconds 1
        continue
    }
    if (-not [DailyGames.LauncherNative]::ActivateVerifiedWindow($window)) {
        Write-AutomationStatus -Code 'foreground_activation_failed'
        Start-Sleep -Seconds 1
        continue
    }

    Write-AutomationStatus -Code 'verified_window_activated'
    $selected = Invoke-AccessibleButton `
        -Window $window `
        -Name '__GAME_BUTTON_NAME__'
    if ($selected) {
        Write-AutomationStatus -Code 'game_selected_by_accessibility'
    }
    else {
        $selected = [DailyGames.LauncherNative]::ClickAt(
            $window,
            __SELECTOR_X_RATIO__,
            __SELECTOR_Y_RATIO__
        )
        if ($selected) {
            Write-AutomationStatus -Code 'game_selected_by_verified_position'
        }
    }
    if (-not $selected) {
        Write-AutomationStatus -Code 'game_selection_failed'
        exit 3
    }

    Start-Sleep -Seconds __SELECTION_DELAY__
    if (
        -not (Test-VerifiedLauncherWindow -Window $window) -or
        -not [DailyGames.LauncherNative]::ActivateVerifiedWindow($window)
    ) {
        Write-AutomationStatus -Code 'window_changed_before_start'
        exit 4
    }

    $invoked = Invoke-AccessibleButton -Window $window -Name '开始游戏'
    if ($invoked) {
        Write-AutomationStatus -Code 'start_invoked_by_accessibility'
    }
    else {
        $invoked = [DailyGames.LauncherNative]::ClickAt(
            $window,
            __X_RATIO__,
            __Y_RATIO__
        )
        if ($invoked) {
            Write-AutomationStatus -Code 'start_clicked_by_verified_position'
        }
    }
    if (-not $invoked) {
        Write-AutomationStatus -Code 'start_action_failed'
        exit 5
    }

    do {
        Start-Sleep -Milliseconds 250
        if (Get-Process -Name '__PROCESS_NAME__' -ErrorAction SilentlyContinue) {
            Write-AutomationStatus -Code 'target_started'
            exit 0
        }
    } while ((Get-Date) -lt $deadline)
    Write-AutomationStatus -Code 'target_start_timeout'
    exit 2
} while ((Get-Date) -lt $deadline)
Write-AutomationStatus -Code 'verified_window_timeout'
exit 2
'@
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    $source = $source.Replace('__WRAPPER_METADATA__', $metadataEncoded)
    $source = $source.Replace('__PROCESS_NAME__', $processName)
    $source = $source.Replace(
        '__LAUNCHER_EXECUTABLE__',
        $resolvedLauncherExecutable.Replace("'", "''")
    )
    $source = $source.Replace(
        '__LAUNCHER_ARGUMENT_1__',
        $launcherArgumentValues[0].Replace("'", "''")
    )
    $source = $source.Replace(
        '__LAUNCHER_ARGUMENT_2__',
        $launcherArgumentValues[1].Replace("'", "''")
    )
    $source = $source.Replace(
        '__LAUNCHER_WORKING_DIRECTORY__',
        $WorkingDirectory.Replace("'", "''")
    )
    $source = $source.Replace(
        '__GAME_BUTTON_NAME__',
        $gameButtonName.Replace("'", "''")
    )
    $source = $source.Replace(
        '__WINDOW_PROCESS_PATH__',
        $expectedWindowProcessPath.Replace("'", "''")
    )
    $source = $source.Replace(
        '__WINDOW_VERSION__',
        $expectedWindowVersion.Replace("'", "''")
    )
    $source = $source.Replace(
        '__WINDOW_SIGNER_THUMBPRINT__',
        $expectedSignerThumbprint
    )
    $source = $source.Replace(
        '__DEBUG_LOG__',
        $resolvedDebugLog.Replace("'", "''")
    )
    $source = $source.Replace('__INITIAL_DELAY__', [string]$initialDelay)
    $source = $source.Replace('__SELECTION_DELAY__', [string]$selectionDelay)
    $source = $source.Replace('__TIMEOUT__', [string]$timeout)
    $source = $source.Replace(
        '__SELECTOR_X_RATIO__',
        $selectorXRatio.ToString('0.######', $invariant)
    )
    $source = $source.Replace(
        '__SELECTOR_Y_RATIO__',
        $selectorYRatio.ToString('0.######', $invariant)
    )
    $source = $source.Replace(
        '__X_RATIO__',
        $xRatio.ToString('0.######', $invariant)
    )
    $source = $source.Replace(
        '__Y_RATIO__',
        $yRatio.ToString('0.######', $invariant)
    )
    $workerTokens = $null
    $workerErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseInput(
        $source,
        [ref]$workerTokens,
        [ref]$workerErrors
    )
    if ($workerErrors.Count -ne 0) {
        throw (
            '启动器包装脚本生成后语法校验失败：' +
            [string]$workerErrors[0].Message
        )
    }
    $workerDirectory = Join-Path $ProjectRoot '.generated'
    $workerPath = Join-Path (
        $workerDirectory
    ) ("launcher_automation_$processName.ps1")
    # Windows PowerShell 5.1 会把无 BOM 的 .ps1 按系统代码页读取；包装脚本含
    # 简体中文窗口和按钮名，因此必须写入 UTF-8 BOM，并把 BOM 计入任务哈希。
    $utf8WithBom = [System.Text.UTF8Encoding]::new($true)
    [byte[]]$workerBytes = @(
        $utf8WithBom.GetPreamble()
    ) + @(
        $utf8WithBom.GetBytes($source)
    )
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $workerSha256 = [BitConverter]::ToString(
            $sha256.ComputeHash($workerBytes)
        ).Replace('-', '')
    }
    finally {
        $sha256.Dispose()
    }
    return [pscustomobject]@{
        Executable = Join-Path $PSHOME 'powershell.exe'
        WorkingDirectory = $WorkingDirectory
        Arguments = ConvertTo-TaskArguments @(
            '-NoProfile',
            '-NonInteractive',
            '-WindowStyle', 'Hidden',
            '-ExecutionPolicy', 'Bypass',
            '-File', $workerPath,
            '-ExpectedSha256', $workerSha256
        )
        WorkerPath = $workerPath
        WorkerContent = $source
        WorkerSha256 = $workerSha256
    }
}

function New-AdminTaskSpec {
    param(
        [string]$Name,
        [string]$Executable,
        [object[]]$Arguments,
        [object]$StartButtonAutomation = $null,
        [string]$GameProcessName = ''
    )
    $fullPath = [System.IO.Path]::GetFullPath($Executable)
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "找不到计划任务程序：$fullPath"
    }
    $workingDirectory = [System.IO.Path]::GetDirectoryName($fullPath)
    $wrapperAction = New-StartButtonActionSpec `
        -Automation $StartButtonAutomation `
        -GameProcessName $GameProcessName `
        -LauncherExecutable $fullPath `
        -LauncherArguments $Arguments `
        -WorkingDirectory $workingDirectory `
        -DebugLogPath (Join-Path $ProjectRoot 'logs\launcher_button_automation.log')
    if ($null -eq $wrapperAction) {
        $taskExecutable = $fullPath
        $taskWorkingDirectory = $workingDirectory
        $taskArguments = ConvertTo-TaskArguments $Arguments
        $wrapperTargetProcess = ''
        $workerPath = ''
        $workerContent = ''
        $workerSha256 = ''
    }
    else {
        $taskExecutable = $wrapperAction.Executable
        $taskWorkingDirectory = $wrapperAction.WorkingDirectory
        $taskArguments = $wrapperAction.Arguments
        $wrapperTargetProcess = $GameProcessName
        $workerPath = $wrapperAction.WorkerPath
        $workerContent = $wrapperAction.WorkerContent
        $workerSha256 = $wrapperAction.WorkerSha256
    }
    return [pscustomobject]@{
        TaskName = $Name
        Executable = $taskExecutable
        WorkingDirectory = $taskWorkingDirectory
        Arguments = $taskArguments
        WrapperTargetProcess = $wrapperTargetProcess
        WorkerPath = $workerPath
        WorkerContent = $workerContent
        WorkerSha256 = $workerSha256
    }
}

function Get-AdminTaskSpecs {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "找不到配置文件：$ConfigPath"
    }
    $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    return @(
        New-AdminTaskSpec `
            -Name 'DailyGames-Arknights-Admin' `
            -Executable ([string]$config.games.arknights.launcher.executable) `
            -Arguments @($config.games.arknights.launcher.args) `
            -StartButtonAutomation $config.games.arknights.launcher.start_button_automation `
            -GameProcessName ([string]$config.games.arknights.game_process_name)
        New-AdminTaskSpec `
            -Name 'DailyGames-MAA-Admin' `
            -Executable ([string]$config.games.arknights.helper.executable) `
            -Arguments @()
        New-AdminTaskSpec `
            -Name 'DailyGames-Endfield-Admin' `
            -Executable ([string]$config.games.endfield.launcher.executable) `
            -Arguments @($config.games.endfield.launcher.args) `
            -StartButtonAutomation $config.games.endfield.launcher.start_button_automation `
            -GameProcessName ([string]$config.games.endfield.game_process_name)
        New-AdminTaskSpec `
            -Name 'DailyGames-MaaEnd-Admin' `
            -Executable ([string]$config.games.endfield.helper.executable) `
            -Arguments @($config.games.endfield.helper.cli_args)
        New-AdminTaskSpec `
            -Name 'DailyGames-AALC-Admin' `
            -Executable ([string]$config.games.limbus.helper.executable) `
            -Arguments @($config.games.limbus.helper.cli_args)
    )
}

function Test-TaskActionMatches {
    param(
        [object]$Action,
        [object]$Spec
    )
    try {
        $actualExecutable = [System.IO.Path]::GetFullPath($Action.Execute)
        $actualWorkingDirectory = [System.IO.Path]::GetFullPath(
            $Action.WorkingDirectory
        )
    }
    catch {
        return $false
    }
    return (
        $actualExecutable.Equals(
            $Spec.Executable,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        $actualWorkingDirectory.Equals(
            $Spec.WorkingDirectory,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        ([string]$Action.Arguments).Trim() -eq $Spec.Arguments.Trim()
    )
}

function Test-WorkerFileMatchesSpec {
    param([object]$Spec)

    if ([string]::IsNullOrWhiteSpace([string]$Spec.WorkerPath)) {
        return $true
    }
    try {
        $workerRoot = [System.IO.Path]::GetFullPath(
            (Join-Path $ProjectRoot '.generated')
        )
        $workerPath = [System.IO.Path]::GetFullPath([string]$Spec.WorkerPath)
        if (-not $workerPath.StartsWith(
            $workerRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            return $false
        }
        if (-not (Test-Path -LiteralPath $workerPath -PathType Leaf)) {
            return $false
        }
        $actualHash = (
            Get-FileHash -LiteralPath $workerPath -Algorithm SHA256
        ).Hash
        return $actualHash.Equals(
            [string]$Spec.WorkerSha256,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

function Write-LauncherWorker {
    param([object]$Spec)

    if ([string]::IsNullOrWhiteSpace([string]$Spec.WorkerPath)) {
        return
    }
    $workerRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $ProjectRoot '.generated')
    )
    $workerPath = [System.IO.Path]::GetFullPath([string]$Spec.WorkerPath)
    if (-not $workerPath.StartsWith(
        $workerRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw '启动器包装脚本必须位于项目根目录的 .generated 子目录。'
    }
    [System.IO.Directory]::CreateDirectory($workerRoot) | Out-Null
    $temporaryPath = "$workerPath.$([Guid]::NewGuid().ToString('N')).tmp"
    $utf8WithBom = [System.Text.UTF8Encoding]::new($true)
    try {
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            [string]$Spec.WorkerContent,
            $utf8WithBom
        )
        $temporaryHash = (
            Get-FileHash -LiteralPath $temporaryPath -Algorithm SHA256
        ).Hash
        if (-not $temporaryHash.Equals(
            [string]$Spec.WorkerSha256,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw '启动器包装脚本写入后的哈希校验失败。'
        }
        Move-Item `
            -LiteralPath $temporaryPath `
            -Destination $workerPath `
            -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
    if (-not (Test-WorkerFileMatchesSpec -Spec $Spec)) {
        throw '启动器包装脚本安装后校验失败。'
    }
}

function Test-TaskMatchesProject {
    param(
        [object]$Task,
        [object]$Spec
    )

    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        return $false
    }
    if (-not (Test-TaskActionMatches -Action $actions[0] -Spec $Spec)) {
        return $false
    }
    return (
        ([string]$Task.Principal.RunLevel) -eq 'Highest' -and
        (Test-WorkerFileMatchesSpec -Spec $Spec)
    )
}

function Install-AdminTask {
    if (-not (Test-IsAdministrator)) {
        throw '安装管理员启动任务时未获得管理员权限。'
    }
    if (-not (Test-Path -LiteralPath $EntryPath -PathType Leaf)) {
        throw "找不到程序入口：$EntryPath"
    }

    $principal = New-ScheduledTaskPrincipal `
        -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive `
        -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
        -MultipleInstances IgnoreNew
    foreach ($spec in @(Get-AdminTaskSpecs)) {
        $existing = Get-ScheduledTask `
            -TaskName $spec.TaskName `
            -ErrorAction SilentlyContinue
        if (
            $null -ne $existing -and
            (Test-TaskMatchesProject -Task $existing -Spec $spec)
        ) {
            continue
        }
        Write-LauncherWorker -Spec $spec
        $actionParameters = @{
            Execute = $spec.Executable
            WorkingDirectory = $spec.WorkingDirectory
        }
        if (-not [string]::IsNullOrWhiteSpace($spec.Arguments)) {
            $actionParameters.Argument = $spec.Arguments
        }
        $actions = @(New-ScheduledTaskAction @actionParameters)
        $definition = New-ScheduledTask `
            -Action $actions `
            -Principal $principal `
            -Settings $settings

        Register-ScheduledTask `
            -TaskName $spec.TaskName `
            -InputObject $definition `
            -Force | Out-Null
    }

    # 旧版本曾把整个编排器提权，导致鹰角启动器无法正常拉起游戏。
    # 新结构只提权会请求 UAC 的启动器与助手；旧任务不再使用，安装成功后删除。
    $legacy = Get-ScheduledTask `
        -TaskName $LegacyTaskName `
        -ErrorAction SilentlyContinue
    if ($null -ne $legacy) {
        Unregister-ScheduledTask `
            -TaskName $LegacyTaskName `
            -Confirm:$false `
            -ErrorAction SilentlyContinue
    }
}

try {
    if ($InstallElevated) {
        Install-AdminTask
        exit 0
    }

    if (-not (Test-Path -LiteralPath $EntryPath -PathType Leaf)) {
        throw "找不到程序入口：$EntryPath"
    }
    $pythonwPath = Get-PythonwPath
    Invoke-SoftwarePathSelfCheck -PythonwPath $pythonwPath
    $specs = @(Get-AdminTaskSpecs)
    $tasksReady = $true
    foreach ($spec in $specs) {
        $task = Get-ScheduledTask `
            -TaskName $spec.TaskName `
            -ErrorAction SilentlyContinue
        if ($null -eq $task -or -not (Test-TaskMatchesProject $task $spec)) {
            $tasksReady = $false
            break
        }
    }
    if (-not $tasksReady) {
        # 第一次安装或路径变化时只确认一次 UAC。注册成功后不再弹出。
        $quotedScript = '"' + $PSCommandPath.Replace('"', '\"') + '"'
        $elevatedArguments = @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', $quotedScript,
            '-InstallElevated'
        ) -join ' '
        $installer = Start-Process `
            -FilePath 'powershell.exe' `
            -ArgumentList $elevatedArguments `
            -Verb RunAs `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        if ($installer.ExitCode -ne 0) {
            throw "管理员启动任务安装失败，退出码：$($installer.ExitCode)"
        }
        foreach ($spec in $specs) {
            $task = Get-ScheduledTask -TaskName $spec.TaskName -ErrorAction Stop
            if (-not (Test-TaskMatchesProject $task $spec)) {
                throw "管理员任务安装后校验失败：$($spec.TaskName)"
            }
        }
    }

    if ($RepairTasks) {
        # 仅修复管理员任务，不启动第二个 GUI；供运行中的队列安全升级使用。
        exit 0
    }

    # 编排器保持普通权限；启动器与助手通过预注册任务按需提权。
    Start-Process `
        -FilePath $pythonwPath `
        -ArgumentList ('"' + $EntryPath + '"') `
        -WorkingDirectory $ProjectRoot | Out-Null
}
catch {
    Show-LaunchError (
        "一键启动失败：$($_.Exception.Message)`n" +
        '首次安装必须确认一次 Windows UAC；以后不会再次要求。'
    )
    exit 1
}
