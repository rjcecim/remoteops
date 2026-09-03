@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "RemoteOps.spec" (
    echo RemoteOps.spec nao encontrado.
    echo Execute este arquivo na raiz do repositorio.
    exit /b 1
)

set "PYTHON="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=%cd%\.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON=%cd%\venv\Scripts\python.exe"
)

if not defined PYTHON (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Python nao encontrado no PATH nem em .venv.
        exit /b 1
    )
    set "PYTHON=python"
)

echo Python: %PYTHON%
echo Spec:   %cd%\RemoteOps.spec
echo.

"%PYTHON%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo PyInstaller nao encontrado. Instalando extras de build...
    "%PYTHON%" -m pip install -e ".[build]"
    if errorlevel 1 (
        echo Falha ao instalar dependencias de build.
        exit /b 1
    )
    echo.
)

echo Gerando RemoteOps.exe...
"%PYTHON%" -m PyInstaller --noconfirm --clean RemoteOps.spec
if errorlevel 1 (
    echo.
    echo Falha no PyInstaller.
    exit /b 1
)

if not exist "dist\RemoteOps.exe" (
    echo.
    echo O PyInstaller terminou, mas dist\RemoteOps.exe nao foi gerado.
    exit /b 1
)

echo.
echo Pronto: %cd%\dist\RemoteOps.exe
dir "dist\RemoteOps.exe"
endlocal
exit /b 0
