param(
    [switch]$InstallElevated,
    [switch]$RepairTasks,
    [string]$ExpectedScriptSha256 = '',
    [string]$ExpectedConfigSha256 = ''
)

$ErrorActionPreference = 'Stop'
$LegacyTaskName = 'DailyGames-OneClick-Admin'
$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$EntryPath = Join-Path $ProjectRoot 'daily_games.pyw'
$ConfigPath = Join-Path $ProjectRoot 'config\user.json'
$WindowsDirectory = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::Windows
)
$WindowsPowerShellRoot = Join-Path `
    $WindowsDirectory `
    'System32\WindowsPowerShell\v1.0'
$SystemPowerShellPath = Join-Path $WindowsPowerShellRoot 'powershell.exe'
$BuiltInModuleRoot = Join-Path $WindowsPowerShellRoot 'Modules'
$WorkerProductRoot = Join-Path (
    [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::CommonApplicationData
    )
) 'DailyGames'
$WorkerRoot = Join-Path $WorkerProductRoot 'workers'
$WorkerLogRoot = Join-Path $WorkerProductRoot 'logs'
$InstallerErrorLog = Join-Path $WorkerLogRoot 'installer_error.log'
$AdministratorSid = 'S-1-5-32-544'
$SystemSid = 'S-1-5-18'
$UsersSid = 'S-1-5-32-545'

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

function Confirm-AdminTaskInstall {
    $message = (
        "首次安装或安全组件更新需要一次 Windows UAC 授权。`n`n" +
        '点击“确定”后，请在下一个 Windows 提示中选择“是”。' +
        "安装完成后，本次一键启动会自动继续；任务定义不变时，后续启动不再询问。"
    )
    try {
        Add-Type -AssemblyName PresentationFramework
        $result = [System.Windows.MessageBox]::Show(
            $message,
            '一键日常：需要管理员授权',
            [System.Windows.MessageBoxButton]::OKCancel,
            [System.Windows.MessageBoxImage]::Information
        )
        return $result -eq [System.Windows.MessageBoxResult]::OK
    }
    catch {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            $fallback = [System.Windows.Forms.MessageBox]::Show(
                $message,
                '一键日常：需要管理员授权',
                [System.Windows.Forms.MessageBoxButtons]::OKCancel,
                [System.Windows.Forms.MessageBoxIcon]::Information
            )
            return $fallback -eq [System.Windows.Forms.DialogResult]::OK
        }
        catch {
            # 无法显示安装说明时失败关闭，不得直接跳到 Windows UAC。
            return $false
        }
    }
}

function Show-InstallCancelled {
    param([string]$Message)
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            $Message,
            '一键日常：首次安装未完成',
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Warning
        ) | Out-Null
    }
    catch {
        # 消息框不可用时直接返回；没有获得授权时不得继续启动 GUI。
    }
}

function Test-UacCancellation {
    param([System.Exception]$Exception)

    $current = $Exception
    while ($null -ne $current) {
        if (
            ($current -is [System.ComponentModel.Win32Exception] -and
                $current.NativeErrorCode -eq 1223) -or
            $current.HResult -eq -2147023673
        ) {
            return $true
        }
        $current = $current.InnerException
    }
    return $false
}

function Write-InstallerFailure {
    param([System.Exception]$Exception)

    if (-not $InstallElevated -or $null -eq $Exception) {
        return
    }
    try {
        if (-not (Test-Path -LiteralPath $WorkerLogRoot -PathType Container)) {
            return
        }
        $message = ([string]$Exception.Message) -replace '[\r\n]+', ' '
        $record = "{0}`t{1}`t{2}{3}" -f @(
            [DateTimeOffset]::Now.ToString('o'),
            $Exception.GetType().FullName,
            $message,
            [Environment]::NewLine
        )
        [System.IO.File]::AppendAllText(
            $InstallerErrorLog,
            $record,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    catch {
        # 诊断日志失败不得覆盖原始安装错误。
    }
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Get-FileSnapshot {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "找不到待核验文件：$Path"
    }
    [byte[]]$bytes = [System.IO.File]::ReadAllBytes($Path)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString(
            $sha256.ComputeHash($bytes)
        ).Replace('-', '')
    }
    finally {
        $sha256.Dispose()
    }
    return [pscustomobject]@{
        Bytes = $bytes
        Sha256 = $hash
    }
}

function Initialize-TrustedPowerShellEnvironment {
    if (-not (Test-Path -LiteralPath $SystemPowerShellPath -PathType Leaf)) {
        throw "找不到 Windows 系统 PowerShell：$SystemPowerShellPath"
    }
    if (-not (Test-Path -LiteralPath $BuiltInModuleRoot -PathType Container)) {
        throw "找不到 Windows PowerShell 内置模块目录：$BuiltInModuleRoot"
    }

    # 入口使用 -NoProfile，并把自动加载范围限制到 Windows 受保护目录，避免
    # 用户级 PSModulePath 抢先提供同名 ScheduledTasks 或 Security 模块。
    $env:PSModulePath = $BuiltInModuleRoot
    foreach ($relativePath in @(
        'Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1',
        'ScheduledTasks\ScheduledTasks.psd1'
    )) {
        $modulePath = Join-Path $BuiltInModuleRoot $relativePath
        if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
            throw "找不到 Windows PowerShell 内置模块：$modulePath"
        }
        Import-Module -Name $modulePath -Force -ErrorAction Stop
    }
}

function New-WorkerDirectorySecurity {
    $security = [Security.AccessControl.DirectorySecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    $administrator = [Security.Principal.SecurityIdentifier]::new(
        $AdministratorSid
    )
    $system = [Security.Principal.SecurityIdentifier]::new($SystemSid)
    $users = [Security.Principal.SecurityIdentifier]::new($UsersSid)
    $inheritance = (
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $security.SetOwner($administrator)
    [void]$security.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            $system,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            $propagation,
            $allow
        )
    )
    [void]$security.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            $administrator,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            $propagation,
            $allow
        )
    )
    [void]$security.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            $users,
            [Security.AccessControl.FileSystemRights]::ReadAndExecute,
            $inheritance,
            $propagation,
            $allow
        )
    )
    return $security
}

function New-WorkerFileSecurity {
    $security = [Security.AccessControl.FileSecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    $administrator = [Security.Principal.SecurityIdentifier]::new(
        $AdministratorSid
    )
    $system = [Security.Principal.SecurityIdentifier]::new($SystemSid)
    $users = [Security.Principal.SecurityIdentifier]::new($UsersSid)
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $security.SetOwner($administrator)
    foreach ($entry in @(
        [pscustomobject]@{
            Sid = $system
            Rights = [Security.AccessControl.FileSystemRights]::FullControl
        }
        [pscustomobject]@{
            Sid = $administrator
            Rights = [Security.AccessControl.FileSystemRights]::FullControl
        }
        [pscustomobject]@{
            Sid = $users
            Rights = [Security.AccessControl.FileSystemRights]::ReadAndExecute
        }
    )) {
        [void]$security.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                $entry.Sid,
                $entry.Rights,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
    }
    return $security
}

function Test-WorkerPathLocation {
    param([string]$Path)

    try {
        $expectedRoot = [System.IO.Path]::GetFullPath($WorkerRoot)
        $candidate = [System.IO.Path]::GetFullPath($Path)
        return (
            [System.IO.Path]::GetDirectoryName($candidate).Equals(
                $expectedRoot,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -and
            [System.IO.Path]::GetFileName($candidate) -match
                '^launcher_automation_(Arknights|Endfield)\.ps1$'
        )
    }
    catch {
        return $false
    }
}

function Test-ProtectedWorkerItemAcl {
    param([string]$Path)

    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if (
            ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne
            0
        ) {
            return $false
        }
        $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
        if (-not $acl.AreAccessRulesProtected) {
            return $false
        }
        $ownerSid = $acl.GetOwner(
            [Security.Principal.SecurityIdentifier]
        ).Value
        if ($ownerSid -notin @($AdministratorSid, $SystemSid)) {
            return $false
        }
        $trustedWriters = @($AdministratorSid, $SystemSid)
        # 不可用 Modify 這類複合值判斷：它同時包含讀取位元，會把合法的
        # ReadAndExecute 規則誤判為可寫。只合併實際能改變內容或 ACL 的位元。
        $dangerousRights = (
            [Security.AccessControl.FileSystemRights]::WriteData -bor
            [Security.AccessControl.FileSystemRights]::AppendData -bor
            [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
            [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
            [Security.AccessControl.FileSystemRights]::Delete -bor
            [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
            [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
            [Security.AccessControl.FileSystemRights]::TakeOwnership
        )
        foreach ($rule in @($acl.Access)) {
            if (
                $rule.AccessControlType -ne
                [Security.AccessControl.AccessControlType]::Allow
            ) {
                continue
            }
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
            if ($sid -in $trustedWriters) {
                continue
            }
            if (
                ([int64]$rule.FileSystemRights -band
                    [int64]$dangerousRights) -ne 0
            ) {
                return $false
            }
        }
        return $true
    }
    catch {
        return $false
    }
}

function Test-ProtectedWorkerPath {
    param([string]$Path)

    if (-not (Test-WorkerPathLocation -Path $Path)) {
        return $false
    }
    foreach ($candidate in @(
        $WorkerProductRoot,
        $WorkerRoot,
        $WorkerLogRoot,
        $Path
    )) {
        if (-not (Test-ProtectedWorkerItemAcl -Path $candidate)) {
            return $false
        }
    }
    return $true
}

function Initialize-ProtectedWorkerDirectory {
    if (-not (Test-IsAdministrator)) {
        throw '建立受保护启动器脚本目录时未获得管理员权限。'
    }
    foreach ($directory in @($WorkerProductRoot, $WorkerRoot, $WorkerLogRoot)) {
        if (
            (Test-Path -LiteralPath $directory) -and
            -not (Test-Path -LiteralPath $directory -PathType Container)
        ) {
            throw "受保护启动器脚本路径不是目录：$directory"
        }
        [System.IO.Directory]::CreateDirectory($directory) | Out-Null
        $item = Get-Item -LiteralPath $directory -Force -ErrorAction Stop
        if (
            ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne
            0
        ) {
            throw "受保护启动器脚本目录不得是重解析点：$directory"
        }
        Set-Acl `
            -LiteralPath $directory `
            -AclObject (New-WorkerDirectorySecurity)
    }
    foreach ($directory in @($WorkerProductRoot, $WorkerRoot, $WorkerLogRoot)) {
        if (-not (Test-ProtectedWorkerItemAcl -Path $directory)) {
            throw "受保护启动器脚本目录 ACL 校验失败：$directory"
        }
    }
}

function Get-PythonwPath {
    $command = Get-Command 'pythonw.exe' -ErrorAction Stop
    return [System.IO.Path]::GetFullPath($command.Source)
}

function Invoke-SoftwarePathSelfCheck {
    param([string]$PythonwPath)

    # 必须先完成地址自查，后续计划任务规格才会使用更新后的程序与工作目录。
    # 外层 PowerShell 已以隐藏窗口启动；这里使用同一安装目录的 python.exe
    # 并逐项传参，确保 --repair-paths 不会在 Start-Process 引号处理中丢失。
    $pythonPath = Join-Path `
        ([System.IO.Path]::GetDirectoryName($PythonwPath)) `
        'python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "找不到与 pythonw.exe 配套的 python.exe：$pythonPath"
    }
    & $pythonPath $EntryPath '--repair-paths'
    $repairExitCode = $LASTEXITCODE
    if ($repairExitCode -ne 0) {
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
        [string]$GameExecutable,
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
    $unexpectedProcessName = switch ($processName) {
        'Arknights' { 'Endfield' }
        'Endfield' { 'Arknights' }
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
    $launcherItem = Get-Item `
        -LiteralPath $resolvedLauncherExecutable `
        -Force `
        -ErrorAction Stop
    $launcherDirectoryItem = Get-Item `
        -LiteralPath ([System.IO.Path]::GetDirectoryName(
            $resolvedLauncherExecutable
        )) `
        -Force `
        -ErrorAction Stop
    if (
        ($launcherItem.Attributes -band
            [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ($launcherDirectoryItem.Attributes -band
            [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $launcherItem.VersionInfo.FileVersion -ne $expectedWindowVersion
    ) {
        throw '启动器主程序路径或版本与已核验身份不一致。'
    }
    $launcherSignature = Get-AuthenticodeSignature `
        -LiteralPath $resolvedLauncherExecutable
    if (
        $launcherSignature.Status -ne 'Valid' -or
        $null -eq $launcherSignature.SignerCertificate -or
        $launcherSignature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne
        $expectedSignerThumbprint
    ) {
        throw '启动器主程序签名与已核验签名不一致。'
    }
    $resolvedGameExecutable = [System.IO.Path]::GetFullPath($GameExecutable)
    $gameItem = Get-Item `
        -LiteralPath $resolvedGameExecutable `
        -Force `
        -ErrorAction Stop
    if (
        $gameItem.PSIsContainer -or
        ($gameItem.Attributes -band
            [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        -not [System.IO.Path]::GetFileName($resolvedGameExecutable).Equals(
            $GameProcessName,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw '目标游戏程序路径与进程名不一致。'
    }
    $gameSignature = Get-AuthenticodeSignature `
        -LiteralPath $resolvedGameExecutable
    if (
        $gameSignature.Status -ne 'Valid' -or
        $null -eq $gameSignature.SignerCertificate -or
        $gameSignature.SignerCertificate.Thumbprint.ToUpperInvariant() -ne
        $expectedSignerThumbprint
    ) {
        throw '目标游戏程序签名与已核验签名不一致。'
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
    if (
        -not [System.IO.Path]::GetDirectoryName($resolvedDebugLog).Equals(
            [System.IO.Path]::GetFullPath($WorkerLogRoot),
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or
        [System.IO.Path]::GetFileName($resolvedDebugLog) -ne
            'launcher_button_automation.log'
    ) {
        throw '启动器按钮自动化日志必须位于受保护的 ProgramData logs 目录。'
    }

    $metadata = [ordered]@{
        version = 4
        launcher_executable = $resolvedLauncherExecutable
        working_directory = $WorkingDirectory
        arguments = ConvertTo-TaskArguments $launcherArgumentValues
        target_process = $GameProcessName
        target_executable = $resolvedGameExecutable
    }
    $metadataJson = $metadata | ConvertTo-Json -Compress
    $metadataEncoded = [Convert]::ToBase64String(
        [System.Text.Encoding]::UTF8.GetBytes($metadataJson)
    )

    $source = @'
# DAILY_GAMES_LAUNCHER_WORKER_V4=__WRAPPER_METADATA__
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
        private static extern IntPtr WindowFromPoint(LauncherPoint point);
        [DllImport("user32.dll")]
        private static extern IntPtr GetAncestor(IntPtr window, uint flags);
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
            if (!GetCursorPos(out oldPoint)) return false;
            if (!SetCursorPos(x, y)) return false;
            try {
                Thread.Sleep(25);
                LauncherRect currentRect;
                if (
                    GetForegroundWindow() != window ||
                    !GetWindowRect(window, out currentRect) ||
                    currentRect.Left != rect.Left ||
                    currentRect.Top != rect.Top ||
                    currentRect.Right != rect.Right ||
                    currentRect.Bottom != rect.Bottom
                ) return false;

                LauncherPoint clickPoint = new LauncherPoint { X = x, Y = y };
                IntPtr hitWindow = WindowFromPoint(clickPoint);
                if (
                    hitWindow == IntPtr.Zero ||
                    GetAncestor(hitWindow, 2) != window ||
                    GetForegroundWindow() != window
                ) return false;

                Input[] inputs = new Input[2];
                inputs[0].Type = 0;
                inputs[0].Mouse.Flags = 0x0002;
                inputs[1].Type = 0;
                inputs[1].Mouse.Flags = 0x0004;
                uint sent = SendInput(
                    (uint)inputs.Length,
                    inputs,
                    Marshal.SizeOf(typeof(Input))
                );
                Thread.Sleep(80);
                return sent == inputs.Length;
            }
            finally {
                SetCursorPos(oldPoint.X, oldPoint.Y);
            }
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

function Get-AccessibleSelectionState {
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
            return 'unknown'
        }
        $pattern = $null
        if ($button.TryGetCurrentPattern(
            [System.Windows.Automation.SelectionItemPattern]::Pattern,
            [ref]$pattern
        )) {
            if ((
                [System.Windows.Automation.SelectionItemPattern]$pattern
            ).Current.IsSelected) {
                return 'selected'
            }
            return 'not_selected'
        }
        if ($button.TryGetCurrentPattern(
            [System.Windows.Automation.TogglePattern]::Pattern,
            [ref]$pattern
        )) {
            if ((
                [System.Windows.Automation.TogglePattern]$pattern
            ).Current.ToggleState -eq
                [System.Windows.Automation.ToggleState]::On
            ) {
                return 'selected'
            }
            return 'not_selected'
        }
        return 'unknown'
    }
    catch {
        return 'unknown'
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

function Test-LauncherExecutableIdentity {
    try {
        $launcherPath = [System.IO.Path]::GetFullPath(
            '__LAUNCHER_EXECUTABLE__'
        )
        $launcher = Get-Item `
            -LiteralPath $launcherPath `
            -Force `
            -ErrorAction Stop
        $launcherDirectory = Get-Item `
            -LiteralPath ([System.IO.Path]::GetDirectoryName($launcherPath)) `
            -Force `
            -ErrorAction Stop
        if (
            $launcher.PSIsContainer -or
            ($launcher.Attributes -band
                [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            ($launcherDirectory.Attributes -band
                [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $launcher.VersionInfo.FileVersion -ne '__WINDOW_VERSION__'
        ) {
            return $false
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $launcherPath
        return (
            $signature.Status -eq 'Valid' -and
            $null -ne $signature.SignerCertificate -and
            $signature.SignerCertificate.Thumbprint.ToUpperInvariant() -eq
                '__WINDOW_SIGNER_THUMBPRINT__'
        )
    }
    catch {
        return $false
    }
}

function Test-VerifiedTargetGameProcess {
    $currentSessionId = (
        [System.Diagnostics.Process]::GetCurrentProcess()
    ).SessionId
    foreach ($candidate in @(
        Get-Process -Name '__PROCESS_NAME__' -ErrorAction SilentlyContinue
    )) {
        try {
            if ($candidate.SessionId -ne $currentSessionId) {
                continue
            }
            $candidatePath = [System.IO.Path]::GetFullPath(
                $candidate.MainModule.FileName
            )
            if (-not $candidatePath.Equals(
                '__TARGET_EXECUTABLE__',
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                continue
            }
            $candidateItem = Get-Item `
                -LiteralPath $candidatePath `
                -Force `
                -ErrorAction Stop
            if (
                $candidateItem.PSIsContainer -or
                ($candidateItem.Attributes -band
                    [System.IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                continue
            }
            $signature = Get-AuthenticodeSignature `
                -LiteralPath $candidatePath
            if (
                $signature.Status -eq 'Valid' -and
                $null -ne $signature.SignerCertificate -and
                $signature.SignerCertificate.Thumbprint.ToUpperInvariant() -eq
                    '__WINDOW_SIGNER_THUMBPRINT__'
            ) {
                return $true
            }
        }
        catch {
            continue
        }
    }
    return $false
}

$script:lastAutomationStatus = ''
$script:windowVerificationStatus = 'verified_window_not_checked'
if (-not $nativeTypesReady) {
    Write-AutomationStatus -Code 'native_type_load_failed'
    exit 7
}
Write-AutomationStatus -Code 'started'
if (-not (Test-LauncherExecutableIdentity)) {
    Write-AutomationStatus -Code 'launcher_identity_mismatch'
    exit 9
}
if (Get-Process -Name '__UNEXPECTED_PROCESS_NAME__' -ErrorAction SilentlyContinue) {
    Write-AutomationStatus -Code 'unexpected_game_already_running'
    exit 8
}
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
    if (Test-VerifiedTargetGameProcess) {
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
            Write-AutomationStatus -Code 'game_selection_clicked_by_verified_position'
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
    $selectionState = Get-AccessibleSelectionState `
        -Window $window `
        -Name '__GAME_BUTTON_NAME__'
    if ($selectionState -eq 'not_selected') {
        Write-AutomationStatus -Code 'game_selection_not_confirmed'
        exit 3
    }
    if ($selectionState -eq 'selected') {
        Write-AutomationStatus -Code 'game_selection_confirmed_by_accessibility'
    }
    else {
        Write-AutomationStatus -Code 'game_selection_not_observable'
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
        if (
            Get-Process `
                -Name '__UNEXPECTED_PROCESS_NAME__' `
                -ErrorAction SilentlyContinue
        ) {
            Write-AutomationStatus -Code 'unexpected_game_started'
            exit 8
        }
        if (Test-VerifiedTargetGameProcess) {
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
        '__UNEXPECTED_PROCESS_NAME__',
        $unexpectedProcessName
    )
    $source = $source.Replace(
        '__TARGET_EXECUTABLE__',
        $resolvedGameExecutable.Replace("'", "''")
    )
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
    $workerDirectory = $WorkerRoot
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
        Executable = $SystemPowerShellPath
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
        [string]$GameProcessName = '',
        [string]$GameExecutable = ''
    )
    $fullPath = [System.IO.Path]::GetFullPath($Executable)
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "找不到计划任务程序：$fullPath"
    }
    $workingDirectory = [System.IO.Path]::GetDirectoryName($fullPath)
    $wrapperAction = New-StartButtonActionSpec `
        -Automation $StartButtonAutomation `
        -GameProcessName $GameProcessName `
        -GameExecutable $GameExecutable `
        -LauncherExecutable $fullPath `
        -LauncherArguments $Arguments `
        -WorkingDirectory $workingDirectory `
        -DebugLogPath (Join-Path $WorkerLogRoot 'launcher_button_automation.log')
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
    param([byte[]]$ConfigBytes)

    if ($null -eq $ConfigBytes -or $ConfigBytes.Length -eq 0) {
        throw '管理员任务配置快照为空。'
    }
    $configText = [System.Text.Encoding]::UTF8.GetString($ConfigBytes)
    if ($configText.Length -gt 0 -and $configText[0] -eq [char]0xFEFF) {
        $configText = $configText.Substring(1)
    }
    $config = $configText | ConvertFrom-Json
    return @(
        New-AdminTaskSpec `
            -Name 'DailyGames-Arknights-Admin' `
            -Executable ([string]$config.games.arknights.launcher.executable) `
            -Arguments @($config.games.arknights.launcher.args) `
            -StartButtonAutomation $config.games.arknights.launcher.start_button_automation `
            -GameProcessName ([string]$config.games.arknights.game_process_name) `
            -GameExecutable ([string]$config.games.arknights.game_executable)
        New-AdminTaskSpec `
            -Name 'DailyGames-MAA-Admin' `
            -Executable ([string]$config.games.arknights.helper.executable) `
            -Arguments @()
        New-AdminTaskSpec `
            -Name 'DailyGames-Endfield-Admin' `
            -Executable ([string]$config.games.endfield.launcher.executable) `
            -Arguments @($config.games.endfield.launcher.args) `
            -StartButtonAutomation $config.games.endfield.launcher.start_button_automation `
            -GameProcessName ([string]$config.games.endfield.game_process_name) `
            -GameExecutable ([string]$config.games.endfield.game_executable)
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
        $workerPath = [System.IO.Path]::GetFullPath([string]$Spec.WorkerPath)
        if (-not (Test-ProtectedWorkerPath -Path $workerPath)) {
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
    $workerPath = [System.IO.Path]::GetFullPath([string]$Spec.WorkerPath)
    if (-not (Test-WorkerPathLocation -Path $workerPath)) {
        throw '启动器包装脚本必须位于受保护的 ProgramData workers 目录。'
    }
    Initialize-ProtectedWorkerDirectory
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
        Set-Acl `
            -LiteralPath $workerPath `
            -AclObject (New-WorkerFileSecurity)
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

function Test-TaskSecurityContract {
    param([object]$Task)

    try {
        $triggerCount = @(
            $Task.Triggers | Where-Object { $null -ne $_ }
        ).Count
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principalUser = [string]$Task.Principal.UserId
        if ($principalUser -match '^S-\d+(?:-\d+)+$') {
            $principalSid = [Security.Principal.SecurityIdentifier]::new(
                $principalUser
            )
        }
        else {
            $principalSid = [Security.Principal.NTAccount]::new(
                $principalUser
            ).Translate([Security.Principal.SecurityIdentifier])
        }
        $userMatches = $principalSid.Value.Equals(
            $identity.User.Value,
            [System.StringComparison]::OrdinalIgnoreCase
        )
        return (
            $userMatches -and
            ([string]$Task.Principal.LogonType) -eq 'Interactive' -and
            ([string]$Task.Principal.RunLevel) -eq 'Highest' -and
            [bool]$Task.Settings.Enabled -and
            [bool]$Task.Settings.AllowDemandStart -and
            ([string]$Task.Settings.MultipleInstances) -eq 'IgnoreNew' -and
            ([string]$Task.Settings.ExecutionTimeLimit) -eq 'PT12H' -and
            -not [bool]$Task.Settings.DisallowStartIfOnBatteries -and
            -not [bool]$Task.Settings.StopIfGoingOnBatteries -and
            $triggerCount -eq 0
        )
    }
    catch {
        return $false
    }
}

function Test-TaskMatchesProject {
    param(
        [object]$Task,
        [object]$Spec
    )

    return (
        (Test-TaskDefinitionMatchesSpec -Task $Task -Spec $Spec) -and
        (Test-WorkerFileMatchesSpec -Spec $Spec)
    )
}

function Test-TaskDefinitionMatchesSpec {
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
    return (Test-TaskSecurityContract -Task $Task)
}

function New-AdminInstallState {
    param(
        [object]$Spec,
        [object]$ExistingTask
    )

    $taskExisted = $null -ne $ExistingTask
    $taskXml = ''
    if ($taskExisted) {
        $taskXml = [string](Export-ScheduledTask `
            -InputObject $ExistingTask `
            -ErrorAction Stop)
        if ([string]::IsNullOrWhiteSpace($taskXml)) {
            throw "无法导出计划任务以供回滚：$($Spec.TaskName)"
        }
    }

    $workerPath = [string]$Spec.WorkerPath
    $workerExisted = $false
    [byte[]]$workerBytes = @()
    $workerHash = ''
    $workerAcl = $null
    if (-not [string]::IsNullOrWhiteSpace($workerPath)) {
        $workerPath = [System.IO.Path]::GetFullPath($workerPath)
        if (-not (Test-WorkerPathLocation -Path $workerPath)) {
            throw '启动器包装脚本快照路径超出受保护 workers 目录。'
        }
        $workerItem = Get-Item `
            -LiteralPath $workerPath `
            -Force `
            -ErrorAction SilentlyContinue
        if ($null -ne $workerItem) {
            if (
                [bool]$workerItem.PSIsContainer -or
                ($workerItem.Attributes -band
                    [System.IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                throw "启动器包装脚本快照目标不是普通文件：$workerPath"
            }
            $workerSnapshot = Get-FileSnapshot -Path $workerPath
            $workerExisted = $true
            $workerBytes = [byte[]]$workerSnapshot.Bytes
            $workerHash = [string]$workerSnapshot.Sha256
            $workerAcl = Get-Acl -LiteralPath $workerPath -ErrorAction Stop
        }
    }

    return [pscustomobject]@{
        Spec = $Spec
        TaskExisted = $taskExisted
        TaskXml = $taskXml
        TaskTouched = $false
        WorkerPath = $workerPath
        WorkerExisted = $workerExisted
        WorkerBytes = $workerBytes
        WorkerHash = $workerHash
        WorkerAcl = $workerAcl
        WorkerTouched = $false
    }
}

function Restore-LauncherWorkerState {
    param([object]$State)

    if (
        -not [bool]$State.WorkerTouched -or
        [string]::IsNullOrWhiteSpace([string]$State.WorkerPath)
    ) {
        return
    }
    $workerPath = [System.IO.Path]::GetFullPath([string]$State.WorkerPath)
    if (-not (Test-WorkerPathLocation -Path $workerPath)) {
        throw '拒绝回滚 workers 目录以外的包装脚本。'
    }
    $currentItem = Get-Item `
        -LiteralPath $workerPath `
        -Force `
        -ErrorAction SilentlyContinue
    if (
        $null -ne $currentItem -and
        (
            [bool]$currentItem.PSIsContainer -or
            ($currentItem.Attributes -band
                [System.IO.FileAttributes]::ReparsePoint) -ne 0
        )
    ) {
        throw "包装脚本在安装期间变成非普通文件，拒绝覆盖：$workerPath"
    }

    if ([bool]$State.WorkerExisted) {
        if ($null -ne $currentItem) {
            $currentHash = [string](
                Get-FileSnapshot -Path $workerPath
            ).Sha256
            $matchesOld = $currentHash.Equals(
                [string]$State.WorkerHash,
                [System.StringComparison]::OrdinalIgnoreCase
            )
            $matchesInstalled = $currentHash.Equals(
                [string]$State.Spec.WorkerSha256,
                [System.StringComparison]::OrdinalIgnoreCase
            )
            if (-not $matchesOld -and -not $matchesInstalled) {
                throw "包装脚本在安装期间被外部修改，拒绝覆盖：$workerPath"
            }
        }
        $temporaryPath = "$workerPath.$([Guid]::NewGuid().ToString('N')).rollback"
        try {
            [System.IO.File]::WriteAllBytes(
                $temporaryPath,
                [byte[]]$State.WorkerBytes
            )
            $temporaryHash = [string](
                Get-FileSnapshot -Path $temporaryPath
            ).Sha256
            if (-not $temporaryHash.Equals(
                [string]$State.WorkerHash,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                throw '包装脚本回滚暂存文件哈希校验失败。'
            }
            Move-Item `
                -LiteralPath $temporaryPath `
                -Destination $workerPath `
                -Force
            Set-Acl `
                -LiteralPath $workerPath `
                -AclObject $State.WorkerAcl
        }
        finally {
            if (Test-Path -LiteralPath $temporaryPath -PathType Leaf) {
                Remove-Item -LiteralPath $temporaryPath -Force
            }
        }
        $restoredHash = [string](
            Get-FileSnapshot -Path $workerPath
        ).Sha256
        if (-not $restoredHash.Equals(
            [string]$State.WorkerHash,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw '包装脚本回滚后的哈希校验失败。'
        }
        return
    }

    if ($null -eq $currentItem) {
        return
    }
    $currentHash = [string](Get-FileSnapshot -Path $workerPath).Sha256
    if (-not $currentHash.Equals(
        [string]$State.Spec.WorkerSha256,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "本轮新建包装脚本被外部修改，拒绝删除：$workerPath"
    }
    Remove-Item -LiteralPath $workerPath -Force -ErrorAction Stop
}

function Restore-AdminInstallStates {
    param([object[]]$States)

    $rollbackErrors = [Collections.Generic.List[string]]::new()
    for ($index = $States.Count - 1; $index -ge 0; $index--) {
        $state = $States[$index]
        if ([bool]$state.TaskTouched) {
            try {
                if ([bool]$state.TaskExisted) {
                    Register-ScheduledTask `
                        -TaskName $state.Spec.TaskName `
                        -Xml ([string]$state.TaskXml) `
                        -Force `
                        -ErrorAction Stop | Out-Null
                }
                else {
                    $currentTask = Get-ScheduledTask `
                        -TaskName $state.Spec.TaskName `
                        -ErrorAction SilentlyContinue
                    if ($null -ne $currentTask) {
                        if (-not (Test-TaskDefinitionMatchesSpec `
                            -Task $currentTask `
                            -Spec $state.Spec)) {
                            throw '本轮新建任务已被外部修改，拒绝删除。'
                        }
                        Unregister-ScheduledTask `
                            -TaskName $state.Spec.TaskName `
                            -Confirm:$false `
                            -ErrorAction Stop
                    }
                }
            }
            catch {
                $rollbackErrors.Add(
                    "任务 $($state.Spec.TaskName) 回滚失败：$($_.Exception.Message)"
                )
            }
        }
        try {
            Restore-LauncherWorkerState -State $state
        }
        catch {
            $rollbackErrors.Add(
                "任务 $($state.Spec.TaskName) 的包装脚本回滚失败：" +
                $_.Exception.Message
            )
        }
    }
    return @($rollbackErrors)
}

function Install-AdminTask {
    param([string]$ExpectedConfigHash)

    if (-not (Test-IsAdministrator)) {
        throw '安装管理员启动任务时未获得管理员权限。'
    }
    if (-not (Test-Path -LiteralPath $EntryPath -PathType Leaf)) {
        throw "找不到程序入口：$EntryPath"
    }
    if ($ExpectedConfigHash -notmatch '^[0-9A-Fa-f]{64}$') {
        throw '管理员安装缺少有效的配置快照 SHA-256。'
    }
    $configSnapshot = Get-FileSnapshot -Path $ConfigPath
    if (-not $configSnapshot.Sha256.Equals(
        $ExpectedConfigHash,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw '配置在请求 UAC 前后发生变化，已中止管理员任务安装。'
    }
    $installSpecs = @(
        Get-AdminTaskSpecs -ConfigBytes ([byte[]]$configSnapshot.Bytes)
    )

    $principal = New-ScheduledTaskPrincipal `
        -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive `
        -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
        -MultipleInstances IgnoreNew
    $installStates = @()
    foreach ($spec in $installSpecs) {
        $existing = Get-ScheduledTask `
            -TaskName $spec.TaskName `
            -ErrorAction SilentlyContinue
        if (
            $null -ne $existing -and
            (Test-TaskMatchesProject -Task $existing -Spec $spec)
        ) {
            continue
        }
        $installStates += New-AdminInstallState `
            -Spec $spec `
            -ExistingTask $existing
    }

    try {
        Initialize-ProtectedWorkerDirectory
        foreach ($state in $installStates) {
            $spec = $state.Spec
            if (-not [string]::IsNullOrWhiteSpace([string]$spec.WorkerPath)) {
                $state.WorkerTouched = $true
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

            # Register-ScheduledTask 可能已完成替换后才抛错，因此调用前标记。
            $state.TaskTouched = $true
            Register-ScheduledTask `
                -TaskName $spec.TaskName `
                -InputObject $definition `
                -Force `
                -ErrorAction Stop | Out-Null
            $installedTask = Get-ScheduledTask `
                -TaskName $spec.TaskName `
                -ErrorAction Stop
            if (-not (Test-TaskMatchesProject `
                -Task $installedTask `
                -Spec $spec)) {
                throw "管理员任务安装后校验失败：$($spec.TaskName)"
            }
        }
        foreach ($spec in $installSpecs) {
            $installedTask = Get-ScheduledTask `
                -TaskName $spec.TaskName `
                -ErrorAction Stop
            if (-not (Test-TaskMatchesProject `
                -Task $installedTask `
                -Spec $spec)) {
                throw "管理员任务整批校验失败：$($spec.TaskName)"
            }
        }
    }
    catch {
        $installFailure = $_.Exception
        $rollbackErrors = @(Restore-AdminInstallStates -States $installStates)
        if ($rollbackErrors.Count -gt 0) {
            throw [InvalidOperationException]::new(
                (
                    "管理员任务安装失败，且回滚未完整完成：" +
                    ($rollbackErrors -join '；')
                ),
                $installFailure
            )
        }
        throw [InvalidOperationException]::new(
            "管理员任务安装失败，已回滚本轮任务与包装脚本：" +
            $installFailure.Message,
            $installFailure
        )
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
    $scriptSnapshot = Get-FileSnapshot -Path $PSCommandPath
    Initialize-TrustedPowerShellEnvironment
    if ($InstallElevated) {
        if (
            $ExpectedScriptSha256 -notmatch '^[0-9A-Fa-f]{64}$' -or
            -not $scriptSnapshot.Sha256.Equals(
                $ExpectedScriptSha256,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            throw '启动脚本在请求 UAC 前后发生变化，已中止管理员任务安装。'
        }
        Install-AdminTask -ExpectedConfigHash $ExpectedConfigSha256
        exit 0
    }

    if (-not (Test-Path -LiteralPath $EntryPath -PathType Leaf)) {
        throw "找不到程序入口：$EntryPath"
    }
    $pythonwPath = Get-PythonwPath
    Invoke-SoftwarePathSelfCheck -PythonwPath $pythonwPath
    $configSnapshot = Get-FileSnapshot -Path $ConfigPath
    $specs = @(
        Get-AdminTaskSpecs -ConfigBytes ([byte[]]$configSnapshot.Bytes)
    )
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
        if (-not (Confirm-AdminTaskInstall)) {
            Show-InstallCancelled (
                '已取消首次安装说明，尚未请求 Windows UAC；' +
                '管理员任务未安装，也没有启动游戏。重新运行一键入口即可继续。'
            )
            exit 2
        }
        $quotedScript = '"' + $PSCommandPath.Replace('"', '\"') + '"'
        $elevatedArguments = @(
            '-NoProfile',
            '-NonInteractive',
            '-ExecutionPolicy', 'Bypass',
            '-File', $quotedScript,
            '-InstallElevated',
            '-ExpectedScriptSha256', $scriptSnapshot.Sha256,
            '-ExpectedConfigSha256', $configSnapshot.Sha256
        ) -join ' '
        try {
            $installer = Start-Process `
                -FilePath $SystemPowerShellPath `
                -ArgumentList $elevatedArguments `
                -Verb RunAs `
                -WindowStyle Hidden `
                -Wait `
                -PassThru
        }
        catch {
            if (Test-UacCancellation -Exception $_.Exception) {
                Show-InstallCancelled (
                    '已取消 Windows UAC 授权；首次安装尚未完成，' +
                    '管理员任务未安装，也没有启动游戏。重新运行一键入口即可继续。'
                )
                exit 2
            }
            throw
        }
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
    $failure = $_.Exception
    Write-InstallerFailure -Exception $failure
    $hint = if ($InstallElevated) {
        '本次管理员任务安装未完成；请按以上错误修复后重新安装。'
    }
    else {
        '首次安装或任务定义变化时会请求一次 Windows UAC；若已经授权，以上错误就是安装失败原因。'
    }
    Show-LaunchError (
        "一键启动失败：$($failure.Message)`n" + $hint
    )
    exit 1
}
