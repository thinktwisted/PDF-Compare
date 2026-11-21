@echo off
cd /d "%~dp0"
pyinstaller --noconfirm PDFCompare.spec
if errorlevel 1 (
    echo Build FAILED
    exit /b 1
)
echo Result: dist\PDFCompare.exe
