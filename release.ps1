param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^v\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$')]
  [string]$Version,

  [string]$Title = $Version,
  [string]$Notes = "Windows build for $Version",
  [switch]$Draft,
  [switch]$Prerelease
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
  throw "GitHub CLI is not installed. Install it from https://cli.github.com/ and run 'gh auth login'."
}

$status = git status --porcelain
if ($status) {
  throw "Working tree is not clean. Commit or stash changes before releasing."
}

.\build.ps1

$exePath = Join-Path $PSScriptRoot "dist\CommandTray.exe"
if (-not (Test-Path $exePath)) {
  throw "Build failed: $exePath was not created."
}

$existingTag = git tag --list $Version
if (-not $existingTag) {
  git tag $Version
}

git push origin $Version

$ghArgs = @(
  "release", "create", $Version,
  $exePath,
  "--title", $Title,
  "--notes", $Notes
)

if ($Draft) {
  $ghArgs += "--draft"
}

if ($Prerelease) {
  $ghArgs += "--prerelease"
}

gh @ghArgs

Write-Host "Released $Version with $exePath"
