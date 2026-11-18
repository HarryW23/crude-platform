@echo off
rem 雙擊更新 crudelab 到 GitHub 上的最新版本（Windows），然後啟動。
rem 不會動到：數據（data\）、密鑰（%USERPROFILE%\.crudelab\secrets.env）、本機設定（%USERPROFILE%\.crudelab\local.env）。
chcp 65001 >nul
cd /d "%~dp0"

where git >nul 2>&1
if errorlevel 1 (
  echo 找不到 git。請到 https://git-scm.com/download/win 下載安裝後再試。
  goto :failed
)
if not exist ".git" (
  echo 這個文件夾不是用 git 下載的（可能是下載的 ZIP），沒法自動更新。
  echo 請改用 git clone 重新下載一份，把 data 文件夾複製過去。
  goto :failed
)

rem 倉庫裡的文件被改過時不自動更新，免得把你的修改覆蓋掉或卡在衝突裡
set "CHANGED="
for /f "delims=" %%l in ('git status --porcelain --untracked-files=no') do set "CHANGED=1"
if defined CHANGED (
  echo 下面這些文件在本機被改過：
  git status --short --untracked-files=no
  echo.
  echo 這台機器自己的設定（備份放哪等）請用 crudelab local set 保存，大模型在後台選，不要直接改倉庫裡的文件。
  echo 確定不需要這些修改的話，運行 git restore 文件名 還原後再更新。
  echo 為了不覆蓋你的修改，這次沒有更新。
  goto :failed
)

for /f %%h in ('git rev-parse --short HEAD') do set "BEFORE=%%h"
echo 正在從 GitHub 下載最新版本...
git pull --ff-only
if errorlevel 1 (
  echo 更新失敗。請檢查網絡；把上面的錯誤訊息保存下來。
  goto :failed
)
for /f %%h in ('git rev-parse --short HEAD') do set "AFTER=%%h"
if "%BEFORE%"=="%AFTER%" (
  echo 已經是最新版本（%AFTER%）。
) else (
  echo 已從 %BEFORE% 更新到 %AFTER%。這次的更新內容：
  git log --oneline %BEFORE%..%AFTER%
)
echo.
call start-windows.bat
exit /b %errorlevel%

:failed
echo.
pause
exit /b 1
