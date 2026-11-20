$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = "$RepoDir\src;$env:PYTHONPATH"
python -m remote_fleet install @args
