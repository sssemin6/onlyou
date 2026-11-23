# 새 PC에서 앱 실행에 필요한 구성 요소를 한 번에 준비합니다.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Install-Winget($id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    winget install --id $id -e --accept-package-agreements --accept-source-agreements
    return ($LASTEXITCODE -eq 0)
}

Write-Host "== 1/2 파이썬 확인"
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

Write-Host "== 2/2 PST 변환 도구 확인"
if (Test-Path "$PSScriptRoot\vendor\readpst-win64\readpst.exe") {
    Write-Host "저장소에 포함된 readpst를 사용합니다. 별도 설치가 필요 없습니다."
} else {
    Write-Host "vendor\readpst-win64 가 없습니다. 저장소를 다시 내려받으세요."
}

Write-Host ""
Write-Host "준비 완료. start.cmd 를 두 번 클릭해 실행하세요."
Write-Host "외부 AI(z.ai 등)를 쓰려면 앱 오른쪽 위의 API 설정에서 주소·모델·키를 입력합니다."
Write-Host "사내 API 주소와 키는 앱의 API 설정에서 입력하세요."
