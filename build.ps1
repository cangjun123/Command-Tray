$ErrorActionPreference = "Stop"

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name CommandTray `
  main.pyw

Write-Host "Built dist\CommandTray.exe"
