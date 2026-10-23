# Запуск MyHealth одной командой: .\start.ps1 (или двойной клик по start.bat)
# Создаёт .venv, ставит зависимости при изменении requirements.txt,
# проверяет .env, запускает сервер и открывает браузер.
param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"

function Fail($msg) {
    Write-Host $msg -ForegroundColor Red
    exit 1
}

# --- 1. Виртуальное окружение ---
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host "Создаю виртуальное окружение .venv..." -ForegroundColor Cyan
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        Fail "Python не найден. Установите Python 3 с https://www.python.org/downloads/ (галочка 'Add python.exe to PATH')."
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPy)) { Fail "Не удалось создать .venv" }
}

# --- 2. Зависимости (только если requirements.txt изменился) ---
$stamp = Join-Path $PSScriptRoot ".venv\.requirements.sha256"
$hash = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot "requirements.txt") -Algorithm SHA256).Hash
$oldHash = if (Test-Path -LiteralPath $stamp) { (Get-Content -LiteralPath $stamp -Raw).Trim() } else { "" }
if ($hash -ne $oldHash) {
    Write-Host "Устанавливаю зависимости из requirements.txt..." -ForegroundColor Cyan
    & $venvPy -m pip install --disable-pip-version-check -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Fail "pip install завершился с ошибкой" }
    Set-Content -LiteralPath $stamp -Value $hash -Encoding ASCII
}

# --- 3. Файл .env ---
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot ".env.example") -Destination $envFile
    Write-Host "Создан .env из .env.example. Впишите ключи, сохраните файл и запустите снова." -ForegroundColor Yellow
    Start-Process notepad.exe -ArgumentList "`"$envFile`""
    exit 1
}
$vars = @{}
foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
        $vars[$Matches[1]] = ($Matches[2] -replace '\s+#.*$', '').Trim('"', "'")
    }
}
if (-not $vars["GEMINI_API_KEY"]) {
    Write-Host "Внимание: в .env не задан GEMINI_API_KEY, чат с AI работать не будет." -ForegroundColor Yellow
}
if (-not $vars["POLAR_CLIENT_ID"] -or -not $vars["POLAR_CLIENT_SECRET"]) {
    Write-Host "Внимание: в .env не заданы POLAR_CLIENT_ID / POLAR_CLIENT_SECRET, синхронизация с Polar недоступна." -ForegroundColor Yellow
}

# --- 4. Сервер и браузер ---
$port = if ($vars["PORT"]) { [int]$vars["PORT"] } else { 5000 }
$url = "http://localhost:$port"

$probe = New-Object System.Net.Sockets.TcpClient
try { $probe.Connect("127.0.0.1", $port); $busy = $true } catch { $busy = $false } finally { $probe.Close() }
if ($busy) {
    Write-Host "Порт $port уже занят (возможно, MyHealth уже запущен). Открываю $url" -ForegroundColor Yellow
    if (-not $NoBrowser) { Start-Process $url }
    exit 0
}

$opener = $null
if (-not $NoBrowser) {
    # Ждём, пока сервер начнёт принимать соединения, и открываем браузер
    $opener = Start-Job -ArgumentList $port, $url -ScriptBlock {
        param($port, $url)
        for ($i = 0; $i -lt 60; $i++) {
            $c = New-Object System.Net.Sockets.TcpClient
            try { $c.Connect("127.0.0.1", $port); $c.Close(); Start-Process $url; return } catch { Start-Sleep -Milliseconds 500 } finally { $c.Close() }
        }
    }
}

Write-Host "MyHealth: $url  (остановить: Ctrl+C)" -ForegroundColor Green
try {
    & $venvPy run.py
} finally {
    if ($opener) { Remove-Job $opener -Force -ErrorAction SilentlyContinue }
}
