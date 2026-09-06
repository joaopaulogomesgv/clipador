@echo off
echo ========================================
echo   Clipador Web - Cortador de Videos
echo ========================================
echo.

echo Verificando Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERRO: Python nao encontrado!
    echo Por favor, instale Python 3.8+ de https://python.org
    pause
    exit /b 1
)

echo Verificando FFmpeg...
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo AVISO: FFmpeg nao encontrado!
    echo Baixe em: https://ffmpeg.org/download.html
    echo Adicione ao PATH do sistema.
    echo.
)

echo Instalando dependencias...
pip install -r requirements.txt

echo.
echo ========================================
echo   Iniciando Clipador Web...
echo   Acesse: http://localhost:5000
echo ========================================
echo.
python app.py

pause
