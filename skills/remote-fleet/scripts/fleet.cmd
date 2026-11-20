@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%..\..\..\src\remote_fleet" (
  set "SRC_DIR=%SCRIPT_DIR%..\..\..\src"
) else if exist "%SCRIPT_DIR%.repo_src" (
  set /p SRC_DIR=<"%SCRIPT_DIR%.repo_src"
) else if exist "%USERPROFILE%\.local\bin\fleet.cmd" (
  call "%USERPROFILE%\.local\bin\fleet.cmd" %*
  exit /b %ERRORLEVEL%
) else (
  echo Error: Could not locate remote_fleet package source. 1>&2
  exit /b 1
)
set "PYTHONPATH=%SRC_DIR%;%PYTHONPATH%"
python -m remote_fleet %*
