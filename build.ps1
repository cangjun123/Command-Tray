$ErrorActionPreference = "Stop"

python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --icon icon.ico `
  --add-data "icon.ico;." `
  --name CommandTray `
  main.pyw

Write-Host "Built dist\CommandTray.exe"
