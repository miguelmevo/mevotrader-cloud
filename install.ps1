# ============================================================
# LumyArena → MT5 Copy Trading — Instalador automático
# Ejecutar en PowerShell como Administrador en el VPS Windows
# ============================================================

$ProjectDir = "C:\trading\telegram_mt5"
$PythonVersion = "3.11.9"
$PythonInstaller = "python-$PythonVersion-amd64.exe"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/$PythonInstaller"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  LumyArena MT5 — Instalador" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 1. Crear carpeta del proyecto
Write-Host "`n[1/6] Creando directorio del proyecto..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $ProjectDir | Out-Null
Write-Host "OK: $ProjectDir" -ForegroundColor Green

# 2. Instalar Python si no existe
Write-Host "`n[2/6] Verificando Python..." -ForegroundColor Yellow
$pythonPath = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonPath) {
    Write-Host "Python no encontrado. Descargando..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri $PythonUrl -OutFile "$env:TEMP\$PythonInstaller"
    Start-Process -Wait -FilePath "$env:TEMP\$PythonInstaller" -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1"
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine")
    Write-Host "OK: Python $PythonVersion instalado" -ForegroundColor Green
} else {
    Write-Host "OK: $(python --version) ya instalado" -ForegroundColor Green
}

# 3. Instalar dependencias Python
Write-Host "`n[3/6] Instalando dependencias Python..." -ForegroundColor Yellow
pip install --quiet telethon python-telegram-bot MetaTrader5
Write-Host "OK: telethon, python-telegram-bot, MetaTrader5" -ForegroundColor Green

# 4. Copiar archivos del proyecto
Write-Host "`n[4/6] Copiando archivos del proyecto..." -ForegroundColor Yellow
$files = @("main.py", "parser.py", "mt5_handler.py", "notifier.py", "config.json")
foreach ($file in $files) {
    $src = Join-Path $PSScriptRoot $file
    if (Test-Path $src) {
        Copy-Item $src -Destination $ProjectDir -Force
        Write-Host "  Copiado: $file" -ForegroundColor Gray
    } else {
        Write-Host "  FALTA: $file — copialo manualmente a $ProjectDir" -ForegroundColor Red
    }
}

# 5. Crear servicio Windows con NSSM
Write-Host "`n[5/6] Instalando como servicio Windows..." -ForegroundColor Yellow
$nssmPath = "$env:TEMP\nssm.zip"
$nssmDir  = "$env:TEMP\nssm"
Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $nssmPath
Expand-Archive -Path $nssmPath -DestinationPath $nssmDir -Force
$nssmExe = "$nssmDir\nssm-2.24\win64\nssm.exe"

$pythonExe = (Get-Command python).Source
$mainScript = "$ProjectDir\main.py"

& $nssmExe install "LumyArenaMT5" $pythonExe $mainScript
& $nssmExe set "LumyArenaMT5" AppDirectory $ProjectDir
& $nssmExe set "LumyArenaMT5" AppStdout "$ProjectDir\logs\output.log"
& $nssmExe set "LumyArenaMT5" AppStderr "$ProjectDir\logs\error.log"
& $nssmExe set "LumyArenaMT5" AppRotateFiles 1
& $nssmExe set "LumyArenaMT5" AppRotateSeconds 86400
& $nssmExe set "LumyArenaMT5" Start SERVICE_AUTO_START

New-Item -ItemType Directory -Force -Path "$ProjectDir\logs" | Out-Null
Write-Host "OK: Servicio 'LumyArenaMT5' creado (auto-start)" -ForegroundColor Green

# 6. Resumen y próximos pasos
Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  Instalacion completada" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "SIGUIENTE PASO — edita el archivo de configuracion:" -ForegroundColor Yellow
Write-Host "  $ProjectDir\config.json" -ForegroundColor White
Write-Host ""
Write-Host "Cuando este listo, inicia el servicio con:" -ForegroundColor Yellow
Write-Host "  net start LumyArenaMT5" -ForegroundColor White
Write-Host ""
Write-Host "Para ver logs en tiempo real:" -ForegroundColor Yellow
Write-Host "  Get-Content $ProjectDir\logs\output.log -Wait" -ForegroundColor White
Write-Host ""
Write-Host "Comandos utiles del servicio:" -ForegroundColor Yellow
Write-Host "  net start LumyArenaMT5   <- iniciar" -ForegroundColor Gray
Write-Host "  net stop LumyArenaMT5    <- detener" -ForegroundColor Gray
Write-Host "  net stop LumyArenaMT5 && net start LumyArenaMT5  <- reiniciar" -ForegroundColor Gray
