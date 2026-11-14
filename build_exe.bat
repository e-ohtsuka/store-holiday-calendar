@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe was not found.
  echo Open the project folder containing the existing virtual environment.
  echo Install dependencies into that environment if needed.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "build_windows.py"
if errorlevel 1 (
  echo.
  echo EXE build failed. Please copy the error details above.
) else (
  echo.
  echo The output ZIP is in the dist folder.
)
pause
