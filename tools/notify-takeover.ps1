#Requires -Version 5.1
<#
.SYNOPSIS
    GUI 自动化接管前的倒计时提示。

.DESCRIPTION
    决策 #4：可以抢用户鼠标，但必须有弹窗提示。

    设计说明（为什么是倒计时而不是一个确认框）：
      我的"中止键"是轮询检测的，不是系统级的——它只在步骤之间生效，
      打断不了一个正在执行的操作。所以接管前这几秒不是礼貌，而是
      用户唯一真正有效的拒绝窗口。

    提示必须包含三件事：要做什么 / 怎么中止 / 大概多久。

    注意：本文件必须保存为 UTF-8 带 BOM，否则 Windows PowerShell 5.1
    会按 ANSI 读取，中文变乱码。

.PARAMETER Task
    本次任务的简短描述。

.PARAMETER Steps
    预计步骤数。

.PARAMETER Seconds
    倒计时秒数，默认 3。

.EXAMPLE
    .\notify-takeover.ps1 -Task 'VS Code 编译运行 C++' -Steps 6
#>
[CmdletBinding()]
param(
    [string]$Task    = 'GUI 自动化',
    [int]   $Steps   = 0,
    [ValidateRange(1, 60)]
    [int]   $Seconds = 3
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object Windows.Forms.Form
$form.Text            = 'DSH 即将接管鼠标键盘'
$form.ClientSize      = New-Object Drawing.Size(560, 210)
$form.StartPosition   = 'CenterScreen'
$form.TopMost         = $true
$form.FormBorderStyle = 'FixedToolWindow'
$form.BackColor       = [Drawing.Color]::FromArgb(28, 28, 32)

$label = New-Object Windows.Forms.Label
$label.Dock      = 'Fill'
$label.TextAlign = 'MiddleCenter'
$label.Font      = New-Object Drawing.Font('Microsoft YaHei', 11)
$label.ForeColor = [Drawing.Color]::White

$stepLine = if ($Steps -gt 0) { "预计 $Steps 步，约 $([int]($Steps * 4)) 秒`n" } else { '' }

$label.Text = @"
DSH 即将接管你的鼠标和键盘

任务：$Task
$stepLine
$Seconds 秒后开始 —— 现在把鼠标移开，或直接关掉这个窗口即可取消
"@

$form.Controls.Add($label)
$form.Tag = $false

$timer = New-Object Windows.Forms.Timer
$timer.Interval = $Seconds * 1000
$timer.Add_Tick({ $form.Tag = $true; $timer.Stop(); $form.Close() })
$timer.Start()

try {
    [void]$form.ShowDialog()
    $completed = [bool]$form.Tag
}
finally {
    $timer.Stop()
    $timer.Dispose()
    $form.Dispose()
}

# Callers must check this before injecting input: closing the notice cancels.
if ($completed) { exit 0 }
exit 2
