# 注册 Windows 任务计划 — 每天早上 9:00 自动运行每日投资日报
# 以管理员身份运行 PowerShell，然后执行: .\setup_task.ps1

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$scriptPath = Join-Path $scriptDir "daily_investment_report.py"
$taskName   = "DailyInvestmentReport"

# 移除旧任务（如果存在）
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action  = New-ScheduledTaskAction -Execute $pythonPath -Argument "`"$scriptPath`"" -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -Daily -At "09:00AM"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable

Register-ScheduledTask `
    -TaskName   $taskName `
    -Action     $action `
    -Trigger    $trigger `
    -Settings   $settings `
    -Description "每天早上9点发送每日投资日报"

Write-Host ""
Write-Host "✅ 任务已注册: $taskName" -ForegroundColor Green
Write-Host "   执行时间: 每天 09:00"
Write-Host "   脚本路径: $scriptPath"
Write-Host ""
Write-Host "手动测试运行:"
Write-Host "   Start-ScheduledTask -TaskName '$taskName'"
Write-Host ""
Write-Host "查看任务状态:"
Write-Host "   Get-ScheduledTask -TaskName '$taskName'"
