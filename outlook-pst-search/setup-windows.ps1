# 새 PC에서 앱 실행에 필요한 구성 요소를 한 번에 준비합니다.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Install-Winget($id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    winget install --id $id -e --accept-package-agreements --accept-source-agreements
    return ($LASTEXITCODE -eq 0)
}

Write-Host "== 1/3 파이썬 확인"
$launcher = $null
foreach ($name in @("py", "python")) {
    if (Get-Command $name -ErrorAction SilentlyContinue) { $launcher = $name; break }
}
if (-not $launcher) {
    Write-Host "파이썬이 없습니다. winget으로 Python 3.13을 설치합니다."
    if (-not (Install-Winget "Python.Python.3.13")) {
        Write-Host "자동 설치에 실패했습니다. https://www.python.org 에서 Python 3.12 이상을 설치한 뒤 다시 실행하세요."
        exit 1
    }
    $launcher = "py"
    $env:Path = "$env:LOCALAPPDATA\Programs\Python\Launcher;$env:LOCALAPPDATA\Programs\Python\Python313;$env:Path"
}
if ($launcher -eq "py") { $sqlite = py -3 -c "import sqlite3; print(sqlite3.sqlite_version)" }
else { $sqlite = python -c "import sqlite3; print(sqlite3.sqlite_version)" }
if (-not $sqlite -or [version]$sqlite -lt [version]"3.42.0") {
    Write-Host "SQLite $sqlite 는 3.42 미만입니다. Python 3.12 이상을 다시 설치하세요."
    exit 1
}
Write-Host "Python 및 SQLite $sqlite 확인."

Write-Host "== 2/3 PST 변환 도구 확인"
if (Test-Path "$PSScriptRoot\vendor\readpst-win64\readpst.exe") {
    Write-Host "저장소에 포함된 readpst를 사용합니다. 별도 설치가 필요 없습니다."
} else {
    Write-Host "vendor\readpst-win64 가 없습니다. 저장소를 다시 내려받으세요."
}

Write-Host "== 3/3 의미 검색 모델(선택)"
$answer = Read-Host "임베딩 모델 준비 (Enter=이 PC에 내려받기, n=건너뛰기, 또는 NAS 모델 경로 예: \\nas\ollama\models)"
if ($answer -eq "n") {
    Write-Host "건너뛰었습니다. 일반(키워드) 검색과 외부 API 답변은 그대로 사용할 수 있습니다."
} else {
    $ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (-not (Test-Path $ollamaExe)) {
        Write-Host "Ollama를 설치합니다."
        if (-not (Install-Winget "Ollama.Ollama")) {
            Write-Host "자동 설치에 실패했습니다. https://ollama.com 에서 직접 설치한 뒤 다시 실행하세요."
        }
    }
    $env:Path = "$env:LOCALAPPDATA\Programs\Ollama;$env:Path"
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Host "ollama 명령을 찾지 못했습니다. 설치 후 다시 실행하세요."
    } elseif ($answer) {
        # 모델 파일은 NAS에 한 벌만 두고 이 PC의 Ollama가 읽어서 임베딩한다.
        setx OLLAMA_MODELS "$answer" | Out-Null
        $env:OLLAMA_MODELS = "$answer"
        Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Start-Process -WindowStyle Hidden "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" -ArgumentList "serve"
        Start-Sleep -Seconds 3
        Write-Host "모델 저장소를 $answer 로 지정했습니다. 현재 목록:"
        ollama list
        Write-Host "목록이 비어 있으면 아무 PC 한 대에서 'ollama pull qwen3-embedding:0.6b'를 한 번만 실행하세요."
    } else {
        $running = $false
        try { Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null; $running = $true } catch {}
        if (-not $running) {
            Start-Process -WindowStyle Hidden (Get-Command ollama).Source -ArgumentList "serve"
            Start-Sleep -Seconds 3
        }
        ollama pull qwen3-embedding:0.6b
        if ($LASTEXITCODE -ne 0) { Write-Host "모델 내려받기에 실패했습니다. 네트워크 상태를 확인하고 'ollama pull qwen3-embedding:0.6b'를 다시 실행하세요." }
    }
}

Write-Host ""
Write-Host "준비 완료. start.cmd 를 두 번 클릭해 실행하세요."
Write-Host "외부 AI(z.ai 등)를 쓰려면 앱 오른쪽 위의 API 설정에서 주소·모델·키를 입력합니다."
