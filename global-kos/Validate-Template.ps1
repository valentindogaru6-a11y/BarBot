$ErrorActionPreference = "Stop"

$kosRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).ProviderPath
$sharedMemoryRoot = Join-Path $PSScriptRoot "templates\_shared-local-memory\memory"
$templatesRoot = Join-Path $PSScriptRoot "templates"
$agentsSource = Join-Path $kosRoot "AGENTS.md"
$senderSource = Join-Path $PSScriptRoot "tools\Send-MemoryUtilityReport.ps1"
$envExampleSource = Join-Path $kosRoot ".env.example"
$gitignoreSource = Join-Path $kosRoot ".gitignore"

foreach ($path in @($sharedMemoryRoot, $agentsSource, $senderSource, $envExampleSource, $gitignoreSource)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required template path is missing: $path"
    }
}

$domainTemplates = @(Get-ChildItem -LiteralPath $templatesRoot -Directory | Where-Object {
    $_.Name -ne "_shared-local-memory"
})
if ($domainTemplates.Count -eq 0) {
    throw "No domain templates were found in $templatesRoot"
}

foreach ($template in $domainTemplates) {
    foreach ($relative in @("README.md", "AGENTS.md", "memory\seed\STARTER_NOTES.md")) {
        $path = Join-Path $template.FullName $relative
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Template '$($template.Name)' is missing required guidance: $relative"
        }
    }
}

$targetRoot = Join-Path ([IO.Path]::GetTempPath()) ("global-kos-template-" + [guid]::NewGuid().ToString("N"))
$targetMemory = Join-Path $targetRoot "memory"

try {
    New-Item -ItemType Directory -Path $targetMemory -Force | Out-Null
    Copy-Item -LiteralPath $agentsSource -Destination (Join-Path $targetRoot "AGENTS.md")
    Get-ChildItem -LiteralPath $sharedMemoryRoot -Force | Copy-Item -Destination $targetMemory -Recurse -Force

    $requiredPaths = @(
        "AGENTS.md",
        "memory\MEMORY_INDEX.md",
        "memory\00-governing\WORKFLOW.md",
        "memory\01-canonical\summary\SUMMARY_INDEX.md",
        "memory\01-canonical\decisions\DECISIONS_INDEX.md",
        "memory\01-canonical\patterns\PATTERNS_INDEX.md",
        "memory\01-canonical\lessons\LESSONS_INDEX.md",
        "memory\02-working\CURRENT_CONTEXT.md",
        "memory\02-working\DISCOVERY_INBOX.md",
        "memory\02-working\MEMORY_UTILITY_LOG.md",
        "memory\02-working\MEMORY_UTILITY_EVENTS.jsonl",
        "memory\02-working\WORKING_MEMORY_INDEX.md",
        "memory\03-graphify\GRAPHIFY_WORKFLOW.md",
        "memory\03-graphify\FRESHNESS_MANIFEST.md"
    )

    $missingPaths = @($requiredPaths | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $targetRoot $_))
    })
    if ($missingPaths.Count -gt 0) {
        throw "Bootstrap is missing required paths: $($missingPaths -join ', ')"
    }

    if (Test-Path -LiteralPath (Join-Path $targetMemory "memory")) {
        throw "Bootstrap created an unexpected nested memory\memory directory."
    }

    $utilityLogPath = Join-Path $targetMemory "02-working\MEMORY_UTILITY_LOG.md"
    $utilityWorkflowPath = Join-Path $targetMemory "00-governing\WORKFLOW.md"
    $utilityLog = Get-Content -LiteralPath $utilityLogPath -Raw
    $utilityWorkflow = Get-Content -LiteralPath $utilityWorkflowPath -Raw
    if ($utilityLog -notmatch '(?m)^Status: Not started\r?$' -or $utilityLog -notmatch 'Target sample: 10 eligible Standard/Full tasks') {
        throw "Utility pilot must be present and opt-in by default with a 10-task target."
    }
    $seedEventsPath = Join-Path $targetMemory "02-working\MEMORY_UTILITY_EVENTS.jsonl"
    if ((Get-Item -LiteralPath $seedEventsPath).Length -ne 0) {
        throw "Utility event seed must be empty for a new project."
    }
    if ($utilityWorkflow -notmatch 'Status: Active' -or $utilityWorkflow -notmatch 'Skip Lightweight') {
        throw "Workflow does not describe when the utility pilot runs."
    }

    $missingLinks = [System.Collections.Generic.List[string]]::new()
    Get-ChildItem -LiteralPath $targetMemory -Recurse -File -Filter "*.md" | ForEach-Object {
        $document = $_
        $content = Get-Content -LiteralPath $document.FullName -Raw
        foreach ($match in [regex]::Matches($content, '\[[^\]]+\]\(([^)]+)\)')) {
            $target = $match.Groups[1].Value.Trim() -replace '#.*$', ''
            if ([string]::IsNullOrWhiteSpace($target) -or $target -match '^(https?:|mailto:|#)') {
                continue
            }

            $fromDocument = Join-Path $document.DirectoryName $target
            $fromProjectRoot = Join-Path $targetRoot $target
            if (-not (Test-Path -LiteralPath $fromDocument) -and -not (Test-Path -LiteralPath $fromProjectRoot)) {
                $relativeDocument = $document.FullName.Substring($targetRoot.Length + 1)
                $missingLinks.Add("${relativeDocument}: $target")
            }
        }
    }

    if ($missingLinks.Count -gt 0) {
        throw "Bootstrap has broken local Markdown links: $($missingLinks -join '; ')"
    }

    $gitignore = Get-Content -LiteralPath $gitignoreSource -Raw
    if ($gitignore -notmatch '(?m)^\.env\s*$') {
        throw "The repository .gitignore must exclude .env."
    }
    $exampleConfig = Get-Content -LiteralPath $envExampleSource -Raw
    if ($exampleConfig -notmatch '(?m)^TELEGRAM_BOT_TOKEN=\s*$' -or $exampleConfig -notmatch '(?m)^TELEGRAM_CHAT_ID=\s*$') {
        throw ".env.example must not contain Telegram credentials."
    }

    $testSenderDir = Join-Path $targetRoot "tools\kos"
    New-Item -ItemType Directory -Path $testSenderDir -Force | Out-Null
    $testSender = Join-Path $testSenderDir "Send-MemoryUtilityReport.ps1"
    Copy-Item -LiteralPath $senderSource -Destination $testSender

    $activeUtilityLog = $utilityLog -replace '(?m)^Status: Not started\r?$', 'Status: Active'
    [System.IO.File]::WriteAllText($utilityLogPath, $activeUtilityLog, (New-Object System.Text.UTF8Encoding($true)))
    $outcomes = @("helpful", "helpful", "helpful", "helpful", "helpful", "helpful", "neutral", "neutral", "misleading", "missed")
    $testEvents = for ($index = 0; $index -lt $outcomes.Count; $index++) {
        $outcome = $outcomes[$index]
        [pscustomobject]@{
            date = "2026-01-01"
            mode = "Standard"
            signal = if ($outcome -eq "missed") { "relevant note missed" } else { "used" }
            outcome = $outcome
            effort = "unknown"
            evidence = "Synthetic validator observation $($index + 1)"
        } | ConvertTo-Json -Compress
    }
    [System.IO.File]::WriteAllLines($seedEventsPath, [string[]]$testEvents, (New-Object System.Text.UTF8Encoding($false)))
    $unsafeConfigPath = Join-Path $targetRoot "OneDrive\must-not-be-read.env"
    & $testSender -MemoryRoot $targetMemory -ConfigPath $unsafeConfigPath -DryRun -BatchSize 10 | Out-Null

    $oneDriveRejected = $false
    try {
        & $testSender -MemoryRoot $targetMemory -ConfigPath $unsafeConfigPath -BatchSize 10 | Out-Null
    } catch {
        $oneDriveRejected = $_.Exception.Message -match "OneDrive sync root"
    }
    if (-not $oneDriveRejected) {
        throw "Utility sender did not reject a config path under OneDrive."
    }

    $testReportPath = Join-Path $targetMemory "02-working\reports\memory-utility-report-0001-0010.md"
    if (-not (Test-Path -LiteralPath $testReportPath)) {
        throw "Utility sender dry-run did not create its report."
    }
    $testReport = [System.IO.File]::ReadAllText($testReportPath, [System.Text.Encoding]::UTF8)
    if ($testReport -notmatch '(?m)^\| 10 \| 2026-01-01 \| Standard \|') {
        throw "Utility sender report does not contain all expected table rows."
    }
    $eventLengthBeforeTestSend = (Get-Item -LiteralPath $seedEventsPath).Length
    & $testSender -MemoryRoot $targetMemory -ConfigPath $unsafeConfigPath -DryRun -TestSend | Out-Null
    $deliveryTestPath = Join-Path $targetMemory "02-working\reports\memory-utility-delivery-test.md"
    if (-not (Test-Path -LiteralPath $deliveryTestPath)) {
        throw "Utility sender test-send dry-run did not create its test document."
    }
    $deliveryTest = [System.IO.File]::ReadAllText($deliveryTestPath, [System.Text.Encoding]::UTF8)
    if (-not $deliveryTest.Contains('KOS_UTILITY_DELIVERY_TEST; NOT PART OF SAMPLE')) {
        throw "Test-send document does not identify itself as outside the utility sample."
    }
    $eventLengthAfterTestSend = (Get-Item -LiteralPath $seedEventsPath).Length
    if ($eventLengthAfterTestSend -ne $eventLengthBeforeTestSend) {
        throw "Test-send changed the utility event log."
    }
    if (Test-Path -LiteralPath (Join-Path $targetMemory "02-working\MEMORY_UTILITY_REPORT_STATE.json")) {
        throw "Utility sender dry-run changed delivery state."
    }

    Write-Output "Template check passed: $($requiredPaths.Count) paths, opt-in utility pilot, dry-run report, $($domainTemplates.Count) domain templates, no nesting or broken links."
}
finally {
    if (Test-Path -LiteralPath $targetRoot) {
        Remove-Item -LiteralPath $targetRoot -Recurse -Force
    }
}