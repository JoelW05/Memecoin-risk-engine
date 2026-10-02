# Supervisor for paperbot.py: keeps it running across days/weeks by restarting
# it automatically if the process ever exits for any reason (crash, network
# outage, etc.) instead of silently staying dead. Ctrl+C here stops the
# supervisor itself (and the bot with it) - that's the deliberate "shut down"
# path; anything else is an unwanted exit and gets restarted.
#
# Usage:
#   .\run_forever.ps1                          (default settings)
#   .\run_forever.ps1 --max-risk-score 25      (any paperbot.py flag passes through)
#
# Logs supervisor-level events (restarts, crash timestamps) to supervisor.log
# alongside the bot's own output, which still prints to this console.

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BotArgs
)

Set-Location $PSScriptRoot
$logPath = Join-Path $PSScriptRoot "supervisor.log"

function Write-SupervisorLog($message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Write-Host $line
    Add-Content -Path $logPath -Value $line
}

Write-SupervisorLog "Supervisor starting. Args: $($BotArgs -join ' ')"

$restartCount = 0
while ($true) {
    $restartCount++
    Write-SupervisorLog "Launching paperbot.py (run #$restartCount)"

    # Direct foreground invocation (not Start-Process) so Ctrl+C in this
    # console is delivered to the python process normally - Start-Process
    # -Wait has flaky Ctrl+C propagation on Windows and can leave an orphaned
    # python.exe running in the background after the wrapper itself dies.
    & python paperbot.py @BotArgs
    $exitCode = $LASTEXITCODE

    Write-SupervisorLog "paperbot.py exited with code $exitCode"

    if ($exitCode -eq 0) {
        # Only happens via --once/--status/--report, which this wrapper isn't
        # meant for - treat a clean exit as "nothing to supervise" and stop.
        Write-SupervisorLog "Clean exit (code 0) - this looks like a one-shot command, not the continuous loop. Supervisor stopping."
        break
    }

    Write-SupervisorLog "Unexpected exit - restarting in 15 seconds. Press Ctrl+C now to stop for good instead."
    Start-Sleep -Seconds 15
}
