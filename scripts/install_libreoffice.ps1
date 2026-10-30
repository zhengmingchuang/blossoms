# Install LibreOffice into project folder: vendor\LibreOffice (for .doc conversion)
# Requires: VC++ 2015+ x64 (winget: Microsoft.VCRedist.2015+.x64)
# Run: powershell -ExecutionPolicy Bypass -File .\scripts\install_libreoffice.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VendorLo = Join-Path $ProjectRoot "vendor\LibreOffice"
$Url = "https://download.documentfoundation.org/libreoffice/stable/26.2.2/win/x86_64/LibreOffice_26.2.2_Win_x86-64.msi"
$Msi = Join-Path $env:TEMP "LibreOffice_26.2.2_Win_x86-64.msi"
$Soffice = Join-Path $VendorLo "program\soffice.exe"

if (Test-Path -LiteralPath $Soffice) {
    Write-Host "Already present: $Soffice"
    exit 0
}

$vendorParent = Split-Path -Parent $VendorLo
New-Item -ItemType Directory -Force -Path $vendorParent | Out-Null

Write-Host "Downloading LibreOffice MSI (~355MB)..."
# curl 对大文件更稳；若无 curl 再退回 Invoke-WebRequest
$curl = Get-Command curl.exe -ErrorAction SilentlyContinue
if ($curl) {
    & curl.exe -L -o $Msi $Url --retry 3 --retry-delay 2
    if ($LASTEXITCODE -ne 0) { throw "curl download failed: $LASTEXITCODE" }
} else {
    Invoke-WebRequest -Uri $Url -OutFile $Msi -UseBasicParsing
}

Write-Host "Extracting to: $VendorLo"
# Paths with spaces: property values must be quoted for msiexec
$argsA = @("/a", $Msi, "TARGETDIR=`"$VendorLo`"", "/qn")
$p = Start-Process -FilePath "msiexec.exe" -ArgumentList $argsA -Wait -PassThru

if ($p.ExitCode -ne 0) {
    Write-Host "msiexec /a exit $($p.ExitCode), trying /i INSTALLLOCATION..."
    $argsI = @(
        "/i", $Msi,
        "INSTALLLOCATION=`"$VendorLo`"",
        "/qn",
        "REGISTER_NO_MSO_TYPES=1",
        "ISCHECKFORPRODUCTUPDATES=0"
    )
    $p2 = Start-Process -FilePath "msiexec.exe" -ArgumentList $argsI -Wait -PassThru
    if ($p2.ExitCode -ne 0) {
        throw "msiexec failed with exit code $($p2.ExitCode)"
    }
}

Remove-Item -Force $Msi -ErrorAction SilentlyContinue

if (-not (Test-Path -LiteralPath $Soffice)) {
    throw "soffice.exe not found at $Soffice"
}
Write-Host "Done: $Soffice"
