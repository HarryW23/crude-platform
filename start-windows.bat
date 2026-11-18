@echo off
rem 雙擊啟動 crudelab（Windows）。
rem 第一次運行會建立 Python 環境並安裝依賴，需要聯網，約一兩分鐘；之後直接啟動。
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo 找不到 Python 3.11 或更新版本。
  echo 請到 https://www.python.org/downloads/ 下載安裝，安裝時勾選 "Add python.exe to PATH"，然後再試。
  goto :failed
)

if not exist ".venv\Scripts\python.exe" (
  echo 第一次運行：正在安裝，需要聯網，請稍候...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo 建立 Python 環境失敗。
    goto :failed
  )
)

rem 依賴清單（pyproject.toml）變了就重新安裝一次：更新代碼後新加的依賴會自動裝上
set "STAMP=.venv\.crudelab-installed"
rem 用 Windows 自帶的 certutil 算校驗碼（第二行就是），避免在批處理裡嵌套引號
set "WANT="
for /f "skip=1 delims=" %%h in ('certutil -hashfile pyproject.toml SHA256') do if not defined WANT set "WANT=%%h"
set "HAVE="
if exist "%STAMP%" set /p HAVE=<"%STAMP%"
set "NEED="
if not exist ".venv\Scripts\crudelab.exe" set "NEED=1"
if not "%HAVE%"=="%WANT%" set "NEED=1"
if defined NEED (
  if exist "%STAMP%" echo 依賴有變化，正在更新，需要聯網，請稍候...
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e .
  if errorlevel 1 (
    echo 安裝依賴失敗。請檢查網絡後再試；把上面的錯誤訊息保存下來。
    goto :failed
  )
  >"%STAMP%" echo %WANT%
  echo 安裝完成。
)

".venv\Scripts\crudelab.exe" serve --open
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
pause
exit /b 1
