[CmdletBinding()]
param(
    [string]$MemoryRoot,
    [string]$ConfigPath,
    [switch]$DryRun,
    [switch]$TestSend,
    [ValidateRange(1, 500)]
    [int]$BatchSize = 10
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if ([string]::IsNullOrWhiteSpace($MemoryRoot)) {
    $MemoryRoot = Join-Path $projectRoot "memory"
}
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $projectRoot ".env"
}

$workingRoot = Join-Path $MemoryRoot "02-working"
$eventsPath = Join-Path $workingRoot "MEMORY_UTILITY_EVENTS.jsonl"
$pilotLogPath = Join-Path $workingRoot "MEMORY_UTILITY_LOG.md"
$statePath = Join-Path $workingRoot "MEMORY_UTILITY_REPORT_STATE.json"
$reportsRoot = Join-Path $workingRoot "reports"

foreach ($path in @($eventsPath, $pilotLogPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required utility pilot file is missing: $path"
    }
}

$pilotLog = Get-Content -LiteralPath $pilotLogPath -Raw
if ($pilotLog -notmatch '(?m)^Status:\s*Active\r?$') {
    throw "Memory utility pilot is not Active. No report was sent."
}

function Read-DotEnv {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Local configuration file is missing: $Path. Copy .env.example to .env and configure it locally."
    }

    $values = @{}
    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $Path) {
        $lineNumber++
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        if ($trimmed -notmatch '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            throw ".env syntax error on line $lineNumber. Expected KEY=VALUE; line content was not shown."
        }

        $key = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[$value.Length - 1] -eq '"') -or ($value[0] -eq "'" -and $value[$value.Length - 1] -eq "'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }

    return $values
}

function Test-IsOneDrivePath {
    param([string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    foreach ($root in @($env:OneDrive, $env:OneDriveCommercial, $env:OneDriveConsumer)) {
        if ([string]::IsNullOrWhiteSpace($root)) {
            continue
        }

        $fullRoot = [System.IO.Path]::GetFullPath($root).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
        if ($fullPath.StartsWith($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }

    return [regex]::IsMatch($fullPath, '(?:^|[\\/])OneDrive(?:[\\/]|$)', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
}

function Get-EventValue {
    param(
        $Event,
        [string]$Name
    )

    $property = $Event.PSObject.Properties[$Name]
    if (-not $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        throw "Utility event is missing required field '$Name'."
    }
    return [string]$property.Value
}

function Format-ReportCell {
    param([string]$Value)

    $safe = $Value -replace '[\r\n]+', ' ' -replace '\|', '\|'
    $safe = $safe -replace '\b\d{5,}:[A-Za-z0-9_-]{20,}\b', '[redacted token]'
    if ($safe.Length -gt 280) {
        $safe = $safe.Substring(0, 277) + "..."
    }
    return $safe
}

function New-ReportContent {
    param(
        [object[]]$Batch,
        [int]$FirstNumber,
        [int]$LastNumber
    )

    $helpful = @($Batch | Where-Object { $_.outcome -eq "helpful" }).Count
    $neutral = @($Batch | Where-Object { $_.outcome -eq "neutral" }).Count
    $misleading = @($Batch | Where-Object { $_.outcome -eq "misleading" }).Count
    $missed = @($Batch | Where-Object { $_.outcome -eq "missed" }).Count
    $effortSaved = @($Batch | Where-Object { $_.effort -eq "saved" }).Count
    $effortEqual = @($Batch | Where-Object { $_.effort -eq "roughly equal" }).Count
    $effortExtra = @($Batch | Where-Object { $_.effort -eq "extra" }).Count
    $effortUnknown = @($Batch | Where-Object { $_.effort -eq "unknown" }).Count

    if ($misleading -gt 0 -or $missed -gt 0) {
        $conclusion = "Проверьте устаревшие/нерелевантные заметки и маршрутизацию памяти."
    } elseif ($helpful -gt $neutral) {
        $conclusion = "Есть наблюдаемые полезные эффекты; продолжайте пилот и сравните следующие партии."
    } elseif ($helpful -eq 0) {
        $conclusion = "Польза пока не наблюдается; сравните её с затратами на поддержку памяти."
    } else {
        $conclusion = "Результат смешанный; проверьте evidence и стоимость поддержки перед выводом."
    }

    $dates = @($Batch | ForEach-Object { $_.date } | Sort-Object)
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add("# Отчёт пилота памяти: задачи $FirstNumber–$LastNumber")
    $lines.Add("")
    $lines.Add("Период: $($dates[0]) — $($dates[$dates.Count - 1])")
    $lines.Add("")
    $lines.Add("## Короткий вывод")
    $lines.Add("")
    $lines.Add($conclusion)
    $lines.Add("")
    $lines.Add("## Итоги по критериям")
    $lines.Add("")
    $lines.Add("| Исход | Количество |")
    $lines.Add("|---|---:|")
    $lines.Add("| Полезно | $helpful |")
    $lines.Add("| Нет наблюдаемого эффекта | $neutral |")
    $lines.Add("| Вводило в заблуждение | $misleading |")
    $lines.Add("| Релевантная память пропущена | $missed |")
    $lines.Add("")
    $lines.Add("Затраты: сэкономлено — $effortSaved; примерно поровну — $effortEqual; дополнительные — $effortExtra; неизвестно — $effortUnknown.")
    $lines.Add("")
    $lines.Add("## Наблюдения")
    $lines.Add("")
    $lines.Add("| № | Дата | Режим | Сигнал | Исход | Затраты | Evidence |")
    $lines.Add("|---:|---|---|---|---|---|---|")

    for ($index = 0; $index -lt $Batch.Count; $index++) {
        $event = $Batch[$index]
        $evidence = Format-ReportCell $event.evidence
        $lines.Add("| $($FirstNumber + $index) | $($event.date) | $($event.mode) | $($event.signal) | $($event.outcome) | $($event.effort) | $evidence |")
    }

    $lines.Add("")
    $lines.Add("Вывод предварительный: это небольшой самоотчётный пилот, не причинное доказательство.")

    return [pscustomobject]@{
        Content = ($lines -join [Environment]::NewLine) + [Environment]::NewLine
        Caption = "Пилот памяти, задачи ${FirstNumber}–${LastNumber}: полезно $helpful; без эффекта $neutral; вводило в заблуждение $misleading; пропущено $missed. $conclusion"
    }
}

function Send-TelegramDocument {
    param(
        [string]$Token,
        [string]$ChatId,
        [string]$ReportPath,
        [string]$Caption
    )

    Add-Type -AssemblyName System.Net.Http
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12
    $client = [System.Net.Http.HttpClient]::new()
    $client.Timeout = [TimeSpan]::FromSeconds(30)
    $form = [System.Net.Http.MultipartFormDataContent]::new()
    $response = $null

    try {
        $form.Add([System.Net.Http.StringContent]::new($ChatId), "chat_id")
        $form.Add([System.Net.Http.StringContent]::new($Caption, [System.Text.Encoding]::UTF8), "caption")

        $fileBytes = [System.IO.File]::ReadAllBytes($ReportPath)
        $fileContent = [System.Net.Http.ByteArrayContent]::new($fileBytes)
        $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("text/markdown; charset=utf-8")
        $form.Add($fileContent, "document", [System.IO.Path]::GetFileName($ReportPath))

        $uri = "https://api.telegram.org/bot$Token/sendDocument"
        $response = $client.PostAsync($uri, $form).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        $apiResult = ConvertFrom-Json -InputObject $body -ErrorAction Stop
        if (-not $response.IsSuccessStatusCode -or -not $apiResult.ok) {
            $description = [string]$apiResult.description
            $description = $description.Replace($Token, "[redacted]").Replace($ChatId, "[redacted]")
            $description = $description -replace '\b\d{5,}:[A-Za-z0-9_-]{20,}\b', '[redacted token]' -replace '[\r\n]+', ' '
            if ($description.Length -gt 180) {
                $description = $description.Substring(0, 180)
            }
            throw "Telegram API error (HTTP $([int]$response.StatusCode)): $description"
        }
    } catch {
        $message = $_.Exception.Message
        if ($message.StartsWith("Telegram API error (", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw $message
        }
        if ($response) {
            throw "Telegram HTTP request failed (status $([int]$response.StatusCode), $($_.Exception.GetType().Name)); credentials and request URL suppressed."
        }
        throw "Telegram transport failed ($($_.Exception.GetType().Name)); credentials and request URL suppressed."
    } finally {
        $form.Dispose()
        $client.Dispose()
    }
}

if (-not $DryRun) {
    if (Test-IsOneDrivePath -Path $ConfigPath) {
        throw "Config path is under a OneDrive sync root. Place .env outside synced folders; the path was not shown."
    }
    $config = Read-DotEnv -Path $ConfigPath
    $configuredBatchSize = $BatchSize
    if ($config.ContainsKey("KOS_UTILITY_REPORT_EVERY") -and $config["KOS_UTILITY_REPORT_EVERY"]) {
        $parsedBatchSize = 0
        if (-not [int]::TryParse($config["KOS_UTILITY_REPORT_EVERY"], [ref]$parsedBatchSize) -or $parsedBatchSize -lt 1 -or $parsedBatchSize -gt 500) {
            throw "KOS_UTILITY_REPORT_EVERY must be an integer from 1 to 500."
        }
        $configuredBatchSize = $parsedBatchSize
    }
    $BatchSize = $configuredBatchSize
}

if ($TestSend) {
    $testReportPath = Join-Path $reportsRoot "memory-utility-delivery-test.md"
    $testId = [guid]::NewGuid().ToString("N").Substring(0, 8)
    $testContent = @(
        "# Проверка доставки Telegram",
        "",
        "Test ID: $testId",
        "",
        "Это тестовый файл. Он не является оценкой полезности и не добавлен в JSONL-выборку.",
        "",
        "| Проверка | Результат |",
        "|---|---|",
        "| Формирование Markdown-файла | пройдено |",
        "| Передача документа в Telegram | проверяется |",
        "| Изменение пилотного журнала/state | нет |",
        "",
        "<!-- KOS_UTILITY_DELIVERY_TEST; NOT PART OF SAMPLE -->",
        "",
        "Если вы получили файл и подпись, отправка Telegram Bot API работает."
    ) -join [Environment]::NewLine
    New-Item -ItemType Directory -Path $reportsRoot -Force | Out-Null
    [System.IO.File]::WriteAllText($testReportPath, $testContent + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($true)))

    if ($DryRun) {
        Write-Output "Dry run created Telegram test report: $testReportPath"
        return
    }

    $testToken = [string]$config["TELEGRAM_BOT_TOKEN"]
    $testChatId = [string]$config["TELEGRAM_CHAT_ID"]
    if ([string]::IsNullOrWhiteSpace($testToken) -or [string]::IsNullOrWhiteSpace($testChatId)) {
        throw "Telegram settings are incomplete. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the local config; values were not displayed."
    }

    $testCaption = "Проверка доставки KOS ($testId): тестовый файл, не входящий в оценку полезности памяти."
    Send-TelegramDocument -Token $testToken -ChatId $testChatId -ReportPath $testReportPath -Caption $testCaption
    Write-Output "Telegram delivery test sent. Local copy: $testReportPath"
    return
}

$events = [System.Collections.Generic.List[object]]::new()
$lineNumber = 0
foreach ($line in Get-Content -LiteralPath $eventsPath) {
    $lineNumber++
    if ([string]::IsNullOrWhiteSpace($line)) {
        continue
    }

    try {
        $event = ConvertFrom-Json -InputObject $line -ErrorAction Stop
    } catch {
        throw "Invalid JSONL event on line $lineNumber; no report was sent."
    }

    $date = Get-EventValue $event "date"
    $mode = Get-EventValue $event "mode"
    $signal = Get-EventValue $event "signal"
    $outcome = Get-EventValue $event "outcome"
    $effort = Get-EventValue $event "effort"
    $evidence = Get-EventValue $event "evidence"
    $parsedDate = [datetime]::MinValue
    if (-not [datetime]::TryParseExact($date, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$parsedDate)) {
        throw "Invalid date on utility event line $lineNumber; expected yyyy-MM-dd."
    }
    if ($mode -notin @("Standard", "Full")) { throw "Invalid task mode on utility event line $lineNumber." }
    if ($signal -notin @("used", "relevant note missed")) { throw "Invalid memory signal on utility event line $lineNumber." }
    if ($outcome -notin @("helpful", "neutral", "misleading", "missed")) { throw "Invalid outcome on utility event line $lineNumber." }
    if ($effort -notin @("saved", "roughly equal", "extra", "unknown")) { throw "Invalid effort label on utility event line $lineNumber." }

    $events.Add([pscustomobject]@{
        date = $date
        mode = $mode
        signal = $signal
        outcome = $outcome
        effort = $effort
        evidence = $evidence
    })
}

$reportedThrough = 0
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json -ErrorAction Stop
        $reportedThrough = [int]$state.reportedThrough
    } catch {
        throw "Delivery state is invalid. No report was sent; inspect $statePath."
    }
}
if ($reportedThrough -lt 0 -or $reportedThrough -gt $events.Count) {
    throw "Delivery state does not match the event log. No report was sent; check the log and state file."
}

$available = $events.Count - $reportedThrough
if ($available -lt $BatchSize) {
    Write-Output "No complete report batch: $available of $BatchSize new observations."
    return
}

$token = $null
$chatId = $null
if (-not $DryRun) {
    $token = [string]$config["TELEGRAM_BOT_TOKEN"]
    $chatId = [string]$config["TELEGRAM_CHAT_ID"]
    if ([string]::IsNullOrWhiteSpace($token) -or [string]::IsNullOrWhiteSpace($chatId)) {
        throw "Telegram settings are incomplete. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the local .env; values were not displayed."
    }
}

New-Item -ItemType Directory -Path $reportsRoot -Force | Out-Null
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

while (($events.Count - $reportedThrough) -ge $BatchSize) {
    $firstNumber = $reportedThrough + 1
    $lastNumber = $reportedThrough + $BatchSize
    $batch = [object[]]::new($BatchSize)
    for ($offset = 0; $offset -lt $BatchSize; $offset++) {
        $batch[$offset] = $events[$reportedThrough + $offset]
    }

    $report = New-ReportContent -Batch $batch -FirstNumber $firstNumber -LastNumber $lastNumber
    $reportName = "memory-utility-report-{0:D4}-{1:D4}.md" -f $firstNumber, $lastNumber
    $reportPath = Join-Path $reportsRoot $reportName
    [System.IO.File]::WriteAllText($reportPath, $report.Content, (New-Object System.Text.UTF8Encoding($true)))

    if ($DryRun) {
        Write-Output "Dry run created report: $reportPath"
        Write-Output $report.Caption
        return
    }

    Send-TelegramDocument -Token $token -ChatId $chatId -ReportPath $reportPath -Caption $report.Caption
    $newState = [pscustomobject]@{
        reportedThrough = $lastNumber
        lastSentUtc = [datetime]::UtcNow.ToString("o")
    }
    $stateTempPath = $statePath + ".tmp"
    [System.IO.File]::WriteAllText($stateTempPath, ($newState | ConvertTo-Json), $utf8NoBom)
    Move-Item -LiteralPath $stateTempPath -Destination $statePath -Force

    Write-Output "Sent Telegram report for observations $firstNumber-$lastNumber. Local copy: $reportPath"
    $reportedThrough = $lastNumber
}