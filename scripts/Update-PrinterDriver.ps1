<#
.SYNOPSIS
Download a printer driver package, stage it, and rebind matching local print queues to the new driver.

.DESCRIPTION
Built to run as a Datto RMM component. All inputs are read from environment variables
(component variables) rather than script parameters, so the component can be pushed
fleet-wide with no per-device editing.

Flow:
  1) Find local print queues whose driver name matches DriverMatchString, then narrow that set
     with PrinterNameMatch / PrinterNameExclude if either is set.
     If none match, log "nothing to do" and exit 0 (expected on devices without the printer).
  2) Download the package from DriverDownloadUrl to a temp working folder and verify it.
  3) Extract (.zip), or run the vendor installer (.exe/.msi) with SilentArgs.
  4) Stage the .inf with pnputil and register the driver with Add-PrinterDriver.
  5) Rebind every matching queue with Set-Printer -DriverName. Queues already on DriverName are
     treated as an in-place version update instead: the staged package is re-registered so the
     print subsystem picks up the newer build of the same driver.
  6) Re-query Get-Printer and confirm each queue reports the new driver. For an in-place version
     update, compare the installed driver version before and after instead.
  7) Remove the temp working folder and emit a greppable result line.

Environment variables (Datto RMM component variables):

  DriverDownloadUrl   (required) Direct HTTPS link to the driver package (.zip, .exe, .msi or .inf).
  DriverMatchString   (required) Substring matched against the DriverName of existing queues,
                                 e.g. "Brother HL-L2350". Matching is case-insensitive.
  DriverName          (required) Exact driver name to bind queues to, as published by the new
                                 driver's INF, e.g. "Brother HL-L2350D series".
  PrinterNameMatch    (optional) Comma/semicolon-separated queue names to limit the run to, e.g.
                                 "Reception MFP,Warehouse*". A pattern with * or ? is a wildcard;
                                 anything else matches as a substring. Use this when several
                                 printers share one generic driver and only some should change.
  PrinterNameExclude  (optional) Same format; queues matching are left alone. Applied after
                                 PrinterNameMatch. With neither set, every queue on the matched
                                 driver is updated.
                                 NOTE: filters decide which queues are REBOUND to a different
                                 driver. They cannot scope an in-place version update, because
                                 Windows keeps one copy of a driver per name - every queue using
                                 that driver gets the new version.
  MinimumDriverVersion (optional) Version floor for an in-place update, e.g. "61.235.1.0". If the
                                 driver is already at or above it, the run exits 0 without
                                 downloading. After an update, a lower version fails with exit 8.
  RestartSpooler      (optional) "true" to restart the Print Spooler after an in-place version
                                 update so queues stop using the previously loaded binaries.
  InfPath             (optional) Path to the .inf inside the extracted package, relative to the
                                 extraction root. Set this when auto-discovery picks the wrong INF.
  SilentArgs          (optional) Arguments for a vendor .exe/.msi package, e.g. "/S" or "/quiet".
                                 Required for .exe packages that are not zip-based self-extractors.
  LogPath             (optional) Full path to the log file. Defaults to a Datto RMM-visible path
                                 (ProgramData\CentraStage when present, otherwise ProgramData).
  ExpectedSha256      (optional) SHA256 of the download. When set, a mismatch aborts before install.
  TreatNoMatchAsError (optional) "true" to exit non-zero when no queue matches. Default "false",
                                 which is the fleet-safe behaviour (no RMM alert on devices that
                                 simply do not have this printer).
  DryRun              (optional) "true" to log every decision without installing the driver or
                                 changing any queue. Useful for a first fleet-wide pass.
  KeepWorkingFiles    (optional) "true" to leave the temp download/extract folder in place for
                                 troubleshooting. Default "false".

Exit codes (for Datto RMM alerting):
   0  Success - all matched queues are on the new driver, or nothing to do
   1  Unexpected error
   2  Configuration error (missing required variable, not elevated)
   3  Download failed or failed verification
   4  Extract/stage failed (no usable .inf, vendor installer failed)
   5  Driver install failed (pnputil / Add-PrinterDriver)
   6  No matching queues found and TreatNoMatchAsError was true
   7  One or more queues failed to rebind
   8  Rebind reported success but verification did not confirm the new driver

.EXAMPLE
$env:DriverDownloadUrl = "https://download.brother.com/pub/com/hll2350dw.zip"
$env:DriverMatchString = "Brother HL-L2350"
$env:DriverName        = "Brother HL-L2350D series"
.\scripts\Update-PrinterDriver.ps1

.NOTES
Runs elevated (Datto RMM SYSTEM context). Re-running is safe: a rebind of a queue already on the
target driver is a no-op, and an in-place version update that finds nothing newer reports
VERSIONUNCHANGED and still exits 0.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$script:ExitOk               = 0
$script:ExitUnexpected       = 1
$script:ExitConfig           = 2
$script:ExitDownload         = 3
$script:ExitStage            = 4
$script:ExitInstall          = 5
$script:ExitNoMatch          = 6
$script:ExitRebind           = 7
$script:ExitVerify           = 8

$script:LogFile     = $null
$script:WorkingRoot = $null
$script:RebootRequired = $false

function Get-EnvValue {
  <# Reads a component variable, trimming whitespace and treating empty as unset. #>
  param(
    [Parameter(Mandatory = $true)][string] $Name
  )

  $value = [System.Environment]::GetEnvironmentVariable($Name)
  if ($null -eq $value) { return $null }
  $value = $value.Trim()
  if (-not $value) { return $null }
  return $value
}

function Get-EnvBool {
  <# Datto RMM passes booleans as text; accept the usual spellings. #>
  param(
    [Parameter(Mandatory = $true)][string] $Name,
    [Parameter()][bool] $Default = $false
  )

  $value = Get-EnvValue -Name $Name
  if (-not $value) { return $Default }

  switch ($value.ToLowerInvariant()) {
    "true"  { return $true }
    "yes"   { return $true }
    "1"     { return $true }
    "on"    { return $true }
    "false" { return $false }
    "no"    { return $false }
    "0"     { return $false }
    "off"   { return $false }
    default { return $Default }
  }
}

function Resolve-LogPath {
  param([Parameter()][string] $ExplicitPath)

  if ($ExplicitPath) { return $ExplicitPath }

  $programData = $env:ProgramData
  if (-not $programData) { $programData = Join-Path $env:SystemDrive "ProgramData" }

  # Datto RMM's agent folder is already collected/visible on the endpoint, so prefer it.
  $centraStage = Join-Path $programData "CentraStage"
  if (Test-Path -LiteralPath $centraStage) {
    return (Join-Path $centraStage "Update-PrinterDriver.log")
  }

  return (Join-Path $programData "Update-PrinterDriver.log")
}

function Initialize-Log {
  param([Parameter(Mandatory = $true)][string] $Path)

  $directory = Split-Path -Path $Path -Parent
  if ($directory -and -not (Test-Path -LiteralPath $directory)) {
    New-Item -Path $directory -ItemType Directory -Force | Out-Null
  }

  # Keep the log bounded so a fleet-wide schedule cannot fill the disk.
  if (Test-Path -LiteralPath $Path) {
    $existing = Get-Item -LiteralPath $Path
    if ($existing.Length -gt 5MB) {
      Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
    }
  }

  $script:LogFile = $Path
}

function Write-Log {
  <#
    One line per step: UTC timestamp, level, stage tag, message.
    Stage tags (INIT/DISCOVER/DOWNLOAD/STAGE/INSTALL/REBIND/VERIFY/CLEANUP/RESULT) are the
    hooks a Datto RMM monitor keys off.
  #>
  param(
    [Parameter(Mandatory = $true)][ValidateSet("INFO", "WARN", "ERROR")][string] $Level,
    [Parameter(Mandatory = $true)][string] $Stage,
    [Parameter(Mandatory = $true)][string] $Message
  )

  $timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
  $line = "{0} {1} [{2}] {3}" -f $timestamp, $Level.PadRight(5), $Stage, ($Message -replace "\r?\n", " | ")

  # Console, not Write-Output: this must reach the Datto RMM stdout capture without
  # landing in the pipeline of whichever function is logging.
  [Console]::Out.WriteLine($line)

  if ($script:LogFile) {
    try {
      Add-Content -LiteralPath $script:LogFile -Value $line -Encoding UTF8
    } catch {
      # Never let logging failures take down the run.
    }
  }
}

function Test-IsAdministrator {
  $current = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($current)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-PrintCmdletsAvailable {
  return [bool](Get-Command -Name "Get-Printer" -ErrorAction SilentlyContinue)
}

function Get-MatchingPrinter {
  <# Every local queue whose current driver name contains the match string. #>
  param(
    [Parameter(Mandatory = $true)][string] $MatchString,
    [Parameter(Mandatory = $true)][string] $TargetDriverName
  )

  $pattern = "*" + $MatchString + "*"
  $printers = @(Get-Printer -ErrorAction Stop)

  return @($printers | Where-Object {
    $_.DriverName -like $pattern -or $_.DriverName -eq $TargetDriverName
  })
}

function Split-PatternList {
  <# Component variables carry lists as comma- or semicolon-separated text. #>
  param([Parameter()][string] $Value)

  if (-not $Value) { return @() }
  return @($Value -split "[;,]" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Test-NameMatchesAny {
  <#
    A pattern containing * or ? is used as a wildcard; anything else is treated as a
    substring, so "Reception" matches "Reception MFP" without the caller adding stars.
  #>
  param(
    [Parameter(Mandatory = $true)][string] $Name,
    [Parameter()][string[]] $Patterns
  )

  foreach ($pattern in $Patterns) {
    $effective = $pattern
    if ($effective -notmatch "[\*\?]") { $effective = "*$effective*" }
    if ($Name -like $effective) { return $true }
  }
  return $false
}

function Get-InstalledDriverVersion {
  <#
    Version of a driver as the print subsystem currently sees it. Used to tell an in-place
    version bump apart from a no-op, since the driver name does not change in that case.
    Returns $null when the driver is absent or exposes no usable version.
  #>
  param([Parameter(Mandatory = $true)][string] $DriverName)

  try {
    $driver = Get-PrinterDriver -Name $DriverName -ErrorAction Stop | Select-Object -First 1
    if (-not $driver) { return $null }

    foreach ($property in @("DriverVersion", "MajorVersion")) {
      $value = $null
      try { $value = $driver.$property } catch { }
      if ($value) { return [string]$value }
    }
  } catch { }

  return $null
}

function Compare-DriverVersion {
  <#
    Returns -1/0/1 like CompareTo, or $null when the two values cannot be compared.
    Driver versions come back either dotted (10.0.19041.1) or as a packed integer, so
    try both rather than guessing.
  #>
  param(
    [Parameter()][string] $Left,
    [Parameter()][string] $Right
  )

  if (-not $Left -or -not $Right) { return $null }

  try { return ([version]$Left).CompareTo([version]$Right) } catch { }
  try { return ([uint64]$Left).CompareTo([uint64]$Right) } catch { }
  return $null
}

function Restart-PrintSpooler {
  <# Queues keep using the loaded driver binaries until the spooler recycles. #>
  try {
    Restart-Service -Name "Spooler" -Force -ErrorAction Stop
    Write-Log -Level INFO -Stage INSTALL -Message "Restarted the Print Spooler service."
    return $true
  } catch {
    Write-Log -Level WARN -Stage INSTALL -Message "Could not restart the Print Spooler service: $($_.Exception.Message)"
    return $false
  }
}

function Get-PackageFileName {
  <# Work out a file name (and therefore package type) from the URL, falling back to a HEAD. #>
  param(
    [Parameter(Mandatory = $true)][string] $Url
  )

  $knownExtensions = @(".zip", ".exe", ".msi", ".inf")

  $uri = [System.Uri] $Url
  $leaf = [System.IO.Path]::GetFileName($uri.AbsolutePath)
  if ($leaf) {
    $leaf = [System.Uri]::UnescapeDataString($leaf)
    $extension = [System.IO.Path]::GetExtension($leaf)
    if ($extension -and ($knownExtensions -contains $extension.ToLowerInvariant())) {
      return $leaf
    }
  }

  # No usable extension in the path - ask the server what it is about to send.
  try {
    $head = Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing -MaximumRedirection 10 -TimeoutSec 60
    $disposition = $null
    try { $disposition = $head.Headers["Content-Disposition"] } catch { }
    if ($disposition) {
      $match = [regex]::Match([string]$disposition, 'filename\*?=(?:UTF-8'''')?"?([^";]+)"?')
      if ($match.Success) {
        $candidate = [System.Uri]::UnescapeDataString($match.Groups[1].Value.Trim())
        $candidate = [System.IO.Path]::GetFileName($candidate)
        $extension = [System.IO.Path]::GetExtension($candidate)
        if ($extension -and ($knownExtensions -contains $extension.ToLowerInvariant())) {
          Write-Log -Level INFO -Stage DOWNLOAD -Message "Package file name from Content-Disposition: $candidate"
          return $candidate
        }
      }
    }

    $contentType = $null
    try { $contentType = [string]$head.Headers["Content-Type"] } catch { }
    if ($contentType) {
      Write-Log -Level INFO -Stage DOWNLOAD -Message "Content-Type reported by server: $contentType"
      if ($contentType -match "zip") { return "driverpackage.zip" }
      if ($contentType -match "msdownload|octet-stream|executable") { return "driverpackage.exe" }
    }
  } catch {
    Write-Log -Level WARN -Stage DOWNLOAD -Message "HEAD request failed, cannot confirm package type from headers: $($_.Exception.Message)"
  }

  return $null
}

function Invoke-PackageDownload {
  param(
    [Parameter(Mandatory = $true)][string] $Url,
    [Parameter(Mandatory = $true)][string] $Destination
  )

  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  } catch {
    Write-Log -Level WARN -Stage DOWNLOAD -Message "Unable to force TLS 1.2; continuing with the system default."
  }

  Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -MaximumRedirection 10 -TimeoutSec 1800

  if (-not (Test-Path -LiteralPath $Destination)) {
    throw "Download completed but '$Destination' does not exist."
  }

  $file = Get-Item -LiteralPath $Destination
  if ($file.Length -le 0) {
    throw "Downloaded file '$Destination' is zero bytes."
  }

  return $file
}

function Expand-ZipPackage {
  param(
    [Parameter(Mandatory = $true)][string] $ArchivePath,
    [Parameter(Mandatory = $true)][string] $Destination
  )

  if (-not (Test-Path -LiteralPath $Destination)) {
    New-Item -Path $Destination -ItemType Directory -Force | Out-Null
  }

  Expand-Archive -LiteralPath $ArchivePath -DestinationPath $Destination -Force
}

function Invoke-VendorInstaller {
  <# Runs a vendor .exe/.msi. The same call covers "extract only" and "silent install" args. #>
  param(
    [Parameter(Mandatory = $true)][string] $FilePath,
    [Parameter()][string] $Arguments,
    [Parameter(Mandatory = $true)][string] $WorkingDirectory
  )

  $stdoutPath = Join-Path $WorkingDirectory "installer-stdout.log"
  $stderrPath = Join-Path $WorkingDirectory "installer-stderr.log"

  $startArgs = @{
    FilePath               = $FilePath
    WorkingDirectory       = $WorkingDirectory
    Wait                   = $true
    PassThru               = $true
    NoNewWindow            = $true
    RedirectStandardOutput = $stdoutPath
    RedirectStandardError  = $stderrPath
  }
  if ($Arguments) { $startArgs.ArgumentList = $Arguments }

  $process = Start-Process @startArgs
  $exitCode = $process.ExitCode

  foreach ($outputPath in @($stdoutPath, $stderrPath)) {
    if (Test-Path -LiteralPath $outputPath) {
      $content = (Get-Content -LiteralPath $outputPath -Raw -ErrorAction SilentlyContinue)
      if ($content -and $content.Trim()) {
        Write-Log -Level INFO -Stage STAGE -Message "Installer output ($(Split-Path -Leaf $outputPath)): $($content.Trim())"
      }
    }
  }

  return $exitCode
}

function Find-DriverInf {
  <#
    Pick the INF to stage. An explicit InfPath wins; otherwise prefer an INF that actually
    names the target driver, then fall back to the only INF present.
  #>
  param(
    [Parameter(Mandatory = $true)][string] $SearchRoot,
    [Parameter()][string] $RelativeInfPath,
    [Parameter(Mandatory = $true)][string] $TargetDriverName
  )

  if ($RelativeInfPath) {
    $candidate = $RelativeInfPath
    if (-not [System.IO.Path]::IsPathRooted($candidate)) {
      $candidate = Join-Path $SearchRoot $RelativeInfPath
    }
    if (-not (Test-Path -LiteralPath $candidate)) {
      throw "InfPath '$RelativeInfPath' was supplied but '$candidate' does not exist."
    }
    Write-Log -Level INFO -Stage STAGE -Message "Using INF from InfPath: $candidate"
    return (Get-Item -LiteralPath $candidate).FullName
  }

  $infFiles = @(Get-ChildItem -LiteralPath $SearchRoot -Filter "*.inf" -Recurse -File -ErrorAction SilentlyContinue)
  if ($infFiles.Count -eq 0) { return $null }

  Write-Log -Level INFO -Stage STAGE -Message "Found $($infFiles.Count) INF file(s) under $SearchRoot"

  $namedMatches = @($infFiles | Where-Object {
    $text = Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue
    $text -and ($text -like "*$TargetDriverName*")
  })

  if ($namedMatches.Count -ge 1) {
    if ($namedMatches.Count -gt 1) {
      Write-Log -Level WARN -Stage STAGE -Message "$($namedMatches.Count) INFs name the driver '$TargetDriverName'; using $($namedMatches[0].FullName). Set InfPath to choose explicitly."
    } else {
      Write-Log -Level INFO -Stage STAGE -Message "INF naming '$TargetDriverName': $($namedMatches[0].FullName)"
    }
    return $namedMatches[0].FullName
  }

  if ($infFiles.Count -eq 1) {
    Write-Log -Level INFO -Stage STAGE -Message "Single INF in package: $($infFiles[0].FullName)"
    return $infFiles[0].FullName
  }

  throw "Package contains $($infFiles.Count) INF files and none name '$TargetDriverName'. Set InfPath to pick one."
}

function Add-DriverToStore {
  <# pnputil stages the driver package into the Windows driver store. #>
  param(
    [Parameter(Mandatory = $true)][string] $InfFullPath
  )

  $pnputilOutput = & pnputil.exe /add-driver "$InfFullPath" /install 2>&1
  $pnputilExit = $LASTEXITCODE
  $outputText = ($pnputilOutput | Out-String).Trim()

  if ($outputText) {
    Write-Log -Level INFO -Stage INSTALL -Message "pnputil output: $outputText"
  }

  # 3010 is "success, reboot required" - the driver is staged either way.
  if ($pnputilExit -ne 0 -and $pnputilExit -ne 3010) {
    throw "pnputil /add-driver failed with exit code $pnputilExit."
  }
  if ($pnputilExit -eq 3010) {
    $script:RebootRequired = $true
    Write-Log -Level WARN -Stage INSTALL -Message "pnputil reported a reboot is required (3010)."
  }

  $publishedName = $null
  $match = [regex]::Match($outputText, "(oem\d+\.inf)", "IgnoreCase")
  if ($match.Success) {
    $publishedName = $match.Groups[1].Value
    Write-Log -Level INFO -Stage INSTALL -Message "Driver staged in the driver store as $publishedName"
  }

  return $publishedName
}

function Register-PrintDriver {
  <# Make the staged driver usable by the print subsystem so Set-Printer will accept it. #>
  param(
    [Parameter(Mandatory = $true)][string] $DriverName,
    [Parameter()][string] $PublishedInfName,
    [Parameter()][string] $InfFullPath,
    [Parameter()][switch] $Force
  )

  $existing = Get-PrinterDriver -Name $DriverName -ErrorAction SilentlyContinue
  if ($existing -and -not $Force) {
    Write-Log -Level INFO -Stage INSTALL -Message "Printer driver '$DriverName' is already registered."
    return $true
  }

  if ($existing -and $Force) {
    # Same driver name, newer build: re-add from the freshly staged INF so the print
    # subsystem picks up the new version instead of keeping the loaded one.
    Write-Log -Level INFO -Stage INSTALL -Message "Printer driver '$DriverName' is registered; re-adding it from the staged package to apply the new version."
    foreach ($candidate in @((Join-Path $env:SystemRoot "INF\$PublishedInfName"), $InfFullPath)) {
      if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
      try {
        Add-PrinterDriver -Name $DriverName -InfPath $candidate -ErrorAction Stop
        Write-Log -Level INFO -Stage INSTALL -Message "Re-registered printer driver '$DriverName' from $candidate"
        return $true
      } catch {
        Write-Log -Level WARN -Stage INSTALL -Message "Re-registering '$DriverName' from '$candidate' failed: $($_.Exception.Message)"
      }
    }

    # The store already holds the new package; the queues keep working on the old build.
    Write-Log -Level WARN -Stage INSTALL -Message "Could not re-register '$DriverName'. The new package is staged in the driver store but the print subsystem may still be using the previous build."
    return $true
  }

  try {
    Add-PrinterDriver -Name $DriverName -ErrorAction Stop
    Write-Log -Level INFO -Stage INSTALL -Message "Registered printer driver '$DriverName' from the driver store."
    return $true
  } catch {
    Write-Log -Level WARN -Stage INSTALL -Message "Add-PrinterDriver by name failed: $($_.Exception.Message)"
  }

  # Fall back to pointing Add-PrinterDriver at the INF, published copy first.
  $infCandidates = @()
  if ($PublishedInfName) {
    $infCandidates += (Join-Path $env:SystemRoot "INF\$PublishedInfName")
  }
  if ($InfFullPath) { $infCandidates += $InfFullPath }

  foreach ($candidate in $infCandidates) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    try {
      Add-PrinterDriver -Name $DriverName -InfPath $candidate -ErrorAction Stop
      Write-Log -Level INFO -Stage INSTALL -Message "Registered printer driver '$DriverName' from $candidate"
      return $true
    } catch {
      Write-Log -Level WARN -Stage INSTALL -Message "Add-PrinterDriver -InfPath '$candidate' failed: $($_.Exception.Message)"
    }
  }

  return $false
}

function Remove-WorkingFolder {
  param([Parameter(Mandatory = $true)][string] $Path)

  if (-not (Test-Path -LiteralPath $Path)) { return }
  try {
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
    Write-Log -Level INFO -Stage CLEANUP -Message "Removed working folder $Path"
  } catch {
    Write-Log -Level WARN -Stage CLEANUP -Message "Could not remove working folder ${Path}: $($_.Exception.Message)"
  }
}

function Write-Result {
  <#
    Single machine-readable summary line, plus the Datto RMM result markers so a monitor
    component can alert on STATUS/EXIT without parsing the whole log.
  #>
  param(
    [Parameter(Mandatory = $true)][string] $Status,
    [Parameter(Mandatory = $true)][int] $ExitCode,
    [Parameter(Mandatory = $true)][string] $Message,
    [Parameter()][int] $Matched = 0,
    [Parameter()][int] $Updated = 0,
    [Parameter()][int] $AlreadyCurrent = 0,
    [Parameter()][int] $Skipped = 0,
    [Parameter()][int] $Failed = 0,
    [Parameter()][int] $Filtered = 0,
    [Parameter()][string] $DriverVersion
  )

  $versionField = $DriverVersion
  if (-not $versionField) { $versionField = "unknown" }

  $summary = "PRINTERDRIVERUPDATE_RESULT STATUS=$Status EXIT=$ExitCode MATCHED=$Matched UPDATED=$Updated CURRENT=$AlreadyCurrent SKIPPED=$Skipped FILTERED=$Filtered FAILED=$Failed DRIVERVERSION=$versionField REBOOTREQUIRED=$($script:RebootRequired.ToString().ToLowerInvariant()) MESSAGE=""$Message"""
  Write-Log -Level $(if ($ExitCode -eq 0) { "INFO" } else { "ERROR" }) -Stage RESULT -Message $summary

  [Console]::Out.WriteLine("<-Start Result->")
  [Console]::Out.WriteLine($summary)
  [Console]::Out.WriteLine("<-End Result->")
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$driverDownloadUrl   = Get-EnvValue -Name "DriverDownloadUrl"
$driverMatchString   = Get-EnvValue -Name "DriverMatchString"
$driverName          = Get-EnvValue -Name "DriverName"
$infPath             = Get-EnvValue -Name "InfPath"
$silentArgs          = Get-EnvValue -Name "SilentArgs"
$expectedSha256      = Get-EnvValue -Name "ExpectedSha256"
$logPath             = Get-EnvValue -Name "LogPath"
$printerNameMatch    = Get-EnvValue -Name "PrinterNameMatch"
$printerNameExclude  = Get-EnvValue -Name "PrinterNameExclude"
$minimumDriverVersion = Get-EnvValue -Name "MinimumDriverVersion"
$treatNoMatchAsError = Get-EnvBool  -Name "TreatNoMatchAsError" -Default $false
$restartSpooler      = Get-EnvBool  -Name "RestartSpooler" -Default $false
$dryRun              = Get-EnvBool  -Name "DryRun" -Default $false
$keepWorkingFiles    = Get-EnvBool  -Name "KeepWorkingFiles" -Default $false

$matchedCount    = 0
$filteredCount   = 0
$updatedCount    = 0
$currentCount    = 0
$skippedCount    = 0
$failedCount     = 0
$isVersionUpdate = $false
$versionChanged  = $false
$versionBefore   = $null
$versionAfter    = $null
$exitCode        = $script:ExitOk

try {
  Initialize-Log -Path (Resolve-LogPath -ExplicitPath $logPath)
} catch {
  [Console]::Out.WriteLine("Unable to initialise log file: $($_.Exception.Message)")
}

try {
  Write-Log -Level INFO -Stage INIT -Message "Update-PrinterDriver starting on $env:COMPUTERNAME (PowerShell $($PSVersionTable.PSVersion))"
  if ($dryRun) {
    Write-Log -Level WARN -Stage INIT -Message "DryRun is enabled: no driver will be installed and no queue will be changed."
  }

  $missing = @()
  if (-not $driverDownloadUrl) { $missing += "DriverDownloadUrl" }
  if (-not $driverMatchString) { $missing += "DriverMatchString" }
  if (-not $driverName)        { $missing += "DriverName" }
  if ($missing.Count -gt 0) {
    throw [System.Management.Automation.RuntimeException] "Missing required environment variable(s): $($missing -join ', ')"
  }

  Write-Log -Level INFO -Stage INIT -Message "DriverMatchString='$driverMatchString' DriverName='$driverName' TreatNoMatchAsError=$treatNoMatchAsError"
  if ($printerNameMatch)     { Write-Log -Level INFO -Stage INIT -Message "PrinterNameMatch='$printerNameMatch'" }
  if ($printerNameExclude)   { Write-Log -Level INFO -Stage INIT -Message "PrinterNameExclude='$printerNameExclude'" }
  if ($minimumDriverVersion) { Write-Log -Level INFO -Stage INIT -Message "MinimumDriverVersion='$minimumDriverVersion'" }

  if (-not (Test-IsAdministrator)) {
    $exitCode = $script:ExitConfig
    Write-Log -Level ERROR -Stage INIT -Message "Not running elevated. Driver staging and queue rebinding require administrator/SYSTEM context."
    Write-Result -Status "CONFIGERROR" -ExitCode $exitCode -Message "Script must run elevated"
    exit $exitCode
  }

  if (-not (Test-PrintCmdletsAvailable)) {
    # No print subsystem means there is definitively no queue to update - not an alert.
    Write-Log -Level WARN -Stage DISCOVER -Message "Get-Printer is unavailable on this device (no print subsystem). Nothing to do."
    Write-Result -Status "NOTHINGTODO" -ExitCode $script:ExitOk -Message "Print cmdlets unavailable on this device"
    exit $script:ExitOk
  }

  # --- Step 1: locate matching queues -------------------------------------
  # Done before the download so devices without this printer skip the transfer entirely,
  # which matters when the component is pushed fleet-wide.
  $matchedPrinters = @(Get-MatchingPrinter -MatchString $driverMatchString -TargetDriverName $driverName)
  $matchedCount = $matchedPrinters.Count

  if ($matchedCount -eq 0) {
    if ($treatNoMatchAsError) {
      $exitCode = $script:ExitNoMatch
      Write-Log -Level ERROR -Stage DISCOVER -Message "No print queue matches '$driverMatchString' and TreatNoMatchAsError is true."
      Write-Result -Status "NOMATCH" -ExitCode $exitCode -Message "No queue matched '$driverMatchString'"
      exit $exitCode
    }

    Write-Log -Level INFO -Stage DISCOVER -Message "No print queue matches '$driverMatchString'. Nothing to do."
    Write-Result -Status "NOTHINGTODO" -ExitCode $script:ExitOk -Message "No queue matched '$driverMatchString'"
    exit $script:ExitOk
  }

  foreach ($printer in $matchedPrinters) {
    Write-Log -Level INFO -Stage DISCOVER -Message "Matched queue '$($printer.Name)' driver='$($printer.DriverName)' type=$($printer.Type) port='$($printer.PortName)'"
  }

  # Narrow by queue name. Both filters are optional: with neither set, every queue on the
  # matched driver is updated, which is what a shared generic driver usually wants.
  $includePatterns = @(Split-PatternList -Value $printerNameMatch)
  $excludePatterns = @(Split-PatternList -Value $printerNameExclude)

  if ($includePatterns.Count -gt 0 -or $excludePatterns.Count -gt 0) {
    $selected = @()
    foreach ($printer in $matchedPrinters) {
      $queueName = "$($printer.Name)"

      if ($includePatterns.Count -gt 0 -and -not (Test-NameMatchesAny -Name $queueName -Patterns $includePatterns)) {
        $filteredCount++
        Write-Log -Level INFO -Stage DISCOVER -Message "FILTERED queue '$queueName' does not match PrinterNameMatch."
        continue
      }
      if ($excludePatterns.Count -gt 0 -and (Test-NameMatchesAny -Name $queueName -Patterns $excludePatterns)) {
        $filteredCount++
        Write-Log -Level INFO -Stage DISCOVER -Message "FILTERED queue '$queueName' matches PrinterNameExclude."
        continue
      }
      $selected += $printer
    }

    $matchedPrinters = @($selected)
    $matchedCount = $matchedPrinters.Count
    Write-Log -Level INFO -Stage DISCOVER -Message "Printer name filter kept $matchedCount queue(s), filtered out $filteredCount."

    if ($matchedCount -eq 0) {
      if ($treatNoMatchAsError) {
        $exitCode = $script:ExitNoMatch
        Write-Log -Level ERROR -Stage DISCOVER -Message "Printer name filter excluded every matching queue and TreatNoMatchAsError is true."
        Write-Result -Status "NOMATCH" -ExitCode $exitCode -Message "Printer name filter excluded every matching queue" -Filtered $filteredCount
        exit $exitCode
      }

      Write-Log -Level INFO -Stage DISCOVER -Message "Printer name filter excluded every matching queue. Nothing to do."
      Write-Result -Status "NOTHINGTODO" -ExitCode $script:ExitOk -Message "Printer name filter excluded every matching queue" -Filtered $filteredCount
      exit $script:ExitOk
    }
  }

  $needingUpdate = @($matchedPrinters | Where-Object { $_.DriverName -ne $driverName })
  $currentCount = $matchedCount - $needingUpdate.Count

  # Queues already reporting the target name are not necessarily done: pushing a newer build
  # of the same driver (common with a shared generic PCL driver) never changes the name, so
  # this is an in-place version update rather than a rebind.
  $isVersionUpdate = $currentCount -gt 0
  $versionBefore = Get-InstalledDriverVersion -DriverName $driverName
  if ($versionBefore) {
    Write-Log -Level INFO -Stage DISCOVER -Message "Driver '$driverName' is currently installed at version $versionBefore."
  }

  if ($isVersionUpdate -and $filteredCount -gt 0) {
    # Windows keeps one copy of a driver per name, so an in-place version update reaches every
    # queue bound to it. A name filter can only decide what gets REBOUND to a different driver.
    Write-Log -Level WARN -Stage DISCOVER -Message "$filteredCount queue(s) were excluded by name, but this run updates driver '$driverName' in place. Excluded queues using that same driver will also get the new version - a name filter cannot scope a shared driver."
  }

  if ($needingUpdate.Count -eq 0 -and -not $isVersionUpdate) {
    Write-Log -Level INFO -Stage DISCOVER -Message "Nothing to update for '$driverName'."
    Write-Result -Status "NOTHINGTODO" -ExitCode $script:ExitOk -Message "No queue needs updating" -Matched $matchedCount -AlreadyCurrent $currentCount -Filtered $filteredCount
    exit $script:ExitOk
  }

  # A version floor lets a scheduled job skip the download once the fleet is up to date.
  if ($minimumDriverVersion -and $needingUpdate.Count -eq 0 -and $versionBefore) {
    $comparison = Compare-DriverVersion -Left $versionBefore -Right $minimumDriverVersion
    if ($null -eq $comparison) {
      Write-Log -Level WARN -Stage DISCOVER -Message "Cannot compare installed version '$versionBefore' with MinimumDriverVersion '$minimumDriverVersion'; continuing with the update."
    } elseif ($comparison -ge 0) {
      Write-Log -Level INFO -Stage DISCOVER -Message "Installed version $versionBefore already meets MinimumDriverVersion $minimumDriverVersion. Nothing to do."
      Write-Result -Status "ALREADYCURRENT" -ExitCode $script:ExitOk -Message "Driver already at version $versionBefore" -Matched $matchedCount -AlreadyCurrent $currentCount -Filtered $filteredCount
      exit $script:ExitOk
    }
  }

  if ($needingUpdate.Count -gt 0) {
    Write-Log -Level INFO -Stage DISCOVER -Message "$($needingUpdate.Count) of $matchedCount matched queue(s) need rebinding to '$driverName'."
  }
  if ($isVersionUpdate) {
    Write-Log -Level INFO -Stage DISCOVER -Message "$currentCount matched queue(s) already use '$driverName'; treating this run as an in-place driver version update."
  }

  # --- Step 2: download ----------------------------------------------------
  $script:WorkingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("PrinterDriverUpdate_" + [Guid]::NewGuid().ToString("N"))
  New-Item -Path $script:WorkingRoot -ItemType Directory -Force | Out-Null
  Write-Log -Level INFO -Stage DOWNLOAD -Message "Working folder: $($script:WorkingRoot)"

  $packageFileName = $null
  $downloadedFile  = $null
  try {
    Write-Log -Level INFO -Stage DOWNLOAD -Message "Downloading from $driverDownloadUrl"
    $packageFileName = Get-PackageFileName -Url $driverDownloadUrl
    if (-not $packageFileName) {
      throw "Could not determine the package type from the URL or response headers. Use a direct link ending in .zip, .exe, .msi or .inf."
    }

    $downloadPath = Join-Path $script:WorkingRoot $packageFileName
    $downloadedFile = Invoke-PackageDownload -Url $driverDownloadUrl -Destination $downloadPath
  } catch {
    $exitCode = $script:ExitDownload
    Write-Log -Level ERROR -Stage DOWNLOAD -Message "Download failed: $($_.Exception.Message)"
    Write-Result -Status "DOWNLOADFAILED" -ExitCode $exitCode -Message "Download failed from $driverDownloadUrl" -Matched $matchedCount -AlreadyCurrent $currentCount -Filtered $filteredCount
    exit $exitCode
  }

  $hash = (Get-FileHash -LiteralPath $downloadedFile.FullName -Algorithm SHA256).Hash
  Write-Log -Level INFO -Stage DOWNLOAD -Message "Downloaded $($downloadedFile.Name) bytes=$($downloadedFile.Length) sha256=$hash"

  if ($expectedSha256 -and ($hash -ne $expectedSha256.Trim().ToUpperInvariant())) {
    $exitCode = $script:ExitDownload
    Write-Log -Level ERROR -Stage DOWNLOAD -Message "SHA256 mismatch. Expected $($expectedSha256.Trim().ToUpperInvariant()), got $hash."
    Write-Result -Status "DOWNLOADFAILED" -ExitCode $exitCode -Message "SHA256 mismatch on downloaded package" -Matched $matchedCount -AlreadyCurrent $currentCount -Filtered $filteredCount
    exit $exitCode
  }

  # --- Step 3: extract / stage --------------------------------------------
  $extractRoot   = Join-Path $script:WorkingRoot "extracted"
  $installerRan  = $false
  $extension     = [System.IO.Path]::GetExtension($downloadedFile.Name).ToLowerInvariant()

  try {
    switch ($extension) {
      ".zip" {
        Write-Log -Level INFO -Stage STAGE -Message "Expanding archive to $extractRoot"
        Expand-ZipPackage -ArchivePath $downloadedFile.FullName -Destination $extractRoot
      }

      ".inf" {
        New-Item -Path $extractRoot -ItemType Directory -Force | Out-Null
        Copy-Item -LiteralPath $downloadedFile.FullName -Destination $extractRoot -Force
        Write-Log -Level INFO -Stage STAGE -Message "Package is a bare INF; staging directly."
      }

      ".msi" {
        if (-not $silentArgs) {
          throw "Package is an .msi but SilentArgs was not set (for example: /qn /norestart)."
        }
        $msiArgs = "/i `"$($downloadedFile.FullName)`" $silentArgs"
        Write-Log -Level INFO -Stage STAGE -Message "Running msiexec.exe $msiArgs"
        $installerExit = Invoke-VendorInstaller -FilePath "msiexec.exe" -Arguments $msiArgs -WorkingDirectory $script:WorkingRoot
        if ($installerExit -eq 3010) { $script:RebootRequired = $true }
        if ($installerExit -ne 0 -and $installerExit -ne 3010) {
          throw "msiexec exited with code $installerExit."
        }
        Write-Log -Level INFO -Stage STAGE -Message "msiexec completed with exit code $installerExit"
        $installerRan = $true
      }

      ".exe" {
        if ($silentArgs) {
          Write-Log -Level INFO -Stage STAGE -Message "Running vendor package: $($downloadedFile.Name) $silentArgs"
          $installerExit = Invoke-VendorInstaller -FilePath $downloadedFile.FullName -Arguments $silentArgs -WorkingDirectory $script:WorkingRoot
          if ($installerExit -eq 3010) { $script:RebootRequired = $true }
          if ($installerExit -ne 0 -and $installerExit -ne 3010) {
            throw "Vendor installer exited with code $installerExit."
          }
          Write-Log -Level INFO -Stage STAGE -Message "Vendor installer completed with exit code $installerExit"
          $installerRan = $true
          # The args may have been extract-only, so still look for an INF. Search the whole
          # working folder: vendors extract to wherever they like under it.
          $extractRoot = $script:WorkingRoot
        } else {
          # Many vendor .exe packages are zip-based self-extractors; try that before giving up.
          Write-Log -Level INFO -Stage STAGE -Message "No SilentArgs set; attempting to expand the .exe as a self-extracting archive."
          $asZip = Join-Path $script:WorkingRoot ([System.IO.Path]::GetFileNameWithoutExtension($downloadedFile.Name) + ".zip")
          Copy-Item -LiteralPath $downloadedFile.FullName -Destination $asZip -Force
          try {
            Expand-ZipPackage -ArchivePath $asZip -Destination $extractRoot
            Write-Log -Level INFO -Stage STAGE -Message "Expanded self-extracting package to $extractRoot"
          } catch {
            throw "Package is an .exe that is not a zip-based self-extractor. Set SilentArgs to the vendor's silent install or extract switch. Underlying error: $($_.Exception.Message)"
          }
        }
      }

      default {
        throw "Unsupported package type '$extension'. Supported: .zip, .exe, .msi, .inf."
      }
    }
  } catch {
    $exitCode = $script:ExitStage
    Write-Log -Level ERROR -Stage STAGE -Message "Staging failed: $($_.Exception.Message)"
    Write-Result -Status "STAGEFAILED" -ExitCode $exitCode -Message "Failed to extract or run the driver package" -Matched $matchedCount -AlreadyCurrent $currentCount -Filtered $filteredCount
    exit $exitCode
  }

  # --- Step 4: stage the driver -------------------------------------------
  $infFullPath = $null
  try {
    if (Test-Path -LiteralPath $extractRoot) {
      $infFullPath = Find-DriverInf -SearchRoot $extractRoot -RelativeInfPath $infPath -TargetDriverName $driverName
    }
  } catch {
    $exitCode = $script:ExitStage
    Write-Log -Level ERROR -Stage STAGE -Message "INF discovery failed: $($_.Exception.Message)"
    Write-Result -Status "STAGEFAILED" -ExitCode $exitCode -Message "Could not determine which INF to install" -Matched $matchedCount -AlreadyCurrent $currentCount -Filtered $filteredCount
    exit $exitCode
  }

  if (-not $infFullPath -and -not $installerRan) {
    $exitCode = $script:ExitStage
    Write-Log -Level ERROR -Stage STAGE -Message "No .inf found in the package and no vendor installer was run."
    Write-Result -Status "STAGEFAILED" -ExitCode $exitCode -Message "No INF found in the driver package" -Matched $matchedCount -AlreadyCurrent $currentCount -Filtered $filteredCount
    exit $exitCode
  }

  if ($dryRun) {
    $dryRunDetail = "DryRun: would stage '$infFullPath' and rebind $($needingUpdate.Count) queue(s) to '$driverName'."
    if ($isVersionUpdate) {
      $dryRunDetail += " $currentCount queue(s) already on '$driverName' would get the new driver version in place (currently $versionBefore)."
    }
    Write-Log -Level WARN -Stage INSTALL -Message $dryRunDetail
    Write-Result -Status "DRYRUN" -ExitCode $script:ExitOk -Message "DryRun completed, no changes made" -Matched $matchedCount -AlreadyCurrent $currentCount -Filtered $filteredCount -Skipped $needingUpdate.Count -DriverVersion $versionBefore
    exit $script:ExitOk
  }

  try {
    $publishedInfName = $null
    if ($infFullPath) {
      Write-Log -Level INFO -Stage INSTALL -Message "Staging driver with pnputil: $infFullPath"
      $publishedInfName = Add-DriverToStore -InfFullPath $infFullPath
    } else {
      Write-Log -Level INFO -Stage INSTALL -Message "No INF to stage; relying on the vendor installer to have registered the driver."
    }

    if (-not (Register-PrintDriver -DriverName $driverName -PublishedInfName $publishedInfName -InfFullPath $infFullPath -Force:$isVersionUpdate)) {
      throw "Driver '$driverName' is not available to the print subsystem after install. Confirm DriverName matches the name published by the INF."
    }

    if ($isVersionUpdate -and $restartSpooler) {
      [void](Restart-PrintSpooler)
    }
  } catch {
    $exitCode = $script:ExitInstall
    Write-Log -Level ERROR -Stage INSTALL -Message "Driver install failed: $($_.Exception.Message)"
    Write-Result -Status "INSTALLFAILED" -ExitCode $exitCode -Message "Driver install failed for '$driverName'" -Matched $matchedCount -AlreadyCurrent $currentCount -Filtered $filteredCount
    exit $exitCode
  }

  # --- Step 5: rebind each matching queue ---------------------------------
  $failedQueues = @()
  foreach ($printer in $needingUpdate) {
    $queueName = $printer.Name

    # Queues connected to a print server take their driver from the server, so the endpoint
    # cannot rebind them. Log and skip rather than failing the whole run.
    if ("$($printer.Type)" -eq "Connection") {
      $skippedCount++
      Write-Log -Level WARN -Stage REBIND -Message "SKIP queue '$queueName' is a print server connection; its driver is managed on the server."
      continue
    }

    $previousDriver = "$($printer.DriverName)"
    try {
      Set-Printer -Name $queueName -DriverName $driverName -ErrorAction Stop
      Write-Log -Level INFO -Stage REBIND -Message "OK queue '$queueName' rebound from '$previousDriver' to '$driverName'"
    } catch {
      $failedQueues += $queueName
      Write-Log -Level ERROR -Stage REBIND -Message "FAIL queue '$queueName' could not be rebound: $($_.Exception.Message)"
    }
  }

  # --- Step 6: verify ------------------------------------------------------
  $verifyFailures = @()
  foreach ($printer in $needingUpdate) {
    $queueName = $printer.Name
    if ("$($printer.Type)" -eq "Connection") { continue }
    if ($failedQueues -contains $queueName) { continue }

    try {
      $verified = Get-Printer -Name $queueName -ErrorAction Stop
      if ($verified.DriverName -eq $driverName) {
        $updatedCount++
        Write-Log -Level INFO -Stage VERIFY -Message "PASS queue '$queueName' reports driver '$($verified.DriverName)'"
      } else {
        $verifyFailures += $queueName
        Write-Log -Level ERROR -Stage VERIFY -Message "FAIL queue '$queueName' reports driver '$($verified.DriverName)', expected '$driverName'"
      }
    } catch {
      $verifyFailures += $queueName
      Write-Log -Level ERROR -Stage VERIFY -Message "FAIL queue '$queueName' could not be re-queried: $($_.Exception.Message)"
    }
  }

  $failedCount = $failedQueues.Count + $verifyFailures.Count

  # For an in-place version update the queue name check proves nothing (the driver name never
  # changed), so compare the installed driver version before and after instead.
  $versionAfter = Get-InstalledDriverVersion -DriverName $driverName
  $versionChanged = $false
  if ($isVersionUpdate) {
    if ($versionAfter -and $versionBefore -and ($versionAfter -ne $versionBefore)) {
      $versionChanged = $true
      Write-Log -Level INFO -Stage VERIFY -Message "PASS driver '$driverName' version went from $versionBefore to $versionAfter"
    } elseif ($versionAfter -and $versionBefore) {
      Write-Log -Level WARN -Stage VERIFY -Message "Driver '$driverName' still reports version $versionAfter after the update. It may already have been the newest build, or the spooler is still holding the previous one (set RestartSpooler=true)."
    } else {
      Write-Log -Level WARN -Stage VERIFY -Message "Could not read a driver version for '$driverName' before/after the update, so an in-place version change cannot be confirmed."
    }

    # An explicit version floor turns "did not change" into a real, alertable failure.
    if ($minimumDriverVersion -and $versionAfter) {
      $comparison = Compare-DriverVersion -Left $versionAfter -Right $minimumDriverVersion
      if ($null -eq $comparison) {
        Write-Log -Level WARN -Stage VERIFY -Message "Cannot compare installed version '$versionAfter' with MinimumDriverVersion '$minimumDriverVersion'."
      } elseif ($comparison -lt 0) {
        $exitCode = $script:ExitVerify
        Write-Log -Level ERROR -Stage VERIFY -Message "FAIL driver '$driverName' is at version $versionAfter, below MinimumDriverVersion $minimumDriverVersion."
        Write-Result -Status "VERIFYFAILED" -ExitCode $exitCode -Message "Driver version $versionAfter is below MinimumDriverVersion $minimumDriverVersion" -Matched $matchedCount -Updated $updatedCount -AlreadyCurrent $currentCount -Skipped $skippedCount -Filtered $filteredCount -Failed $failedCount -DriverVersion $versionAfter
        exit $exitCode
      }
    }
  }

  if ($failedQueues.Count -gt 0) {
    $exitCode = $script:ExitRebind
    Write-Log -Level ERROR -Stage RESULT -Message "Rebind failed for: $($failedQueues -join ', ')"
    Write-Result -Status "REBINDFAILED" -ExitCode $exitCode -Message "Failed to rebind: $($failedQueues -join '; ')" -Matched $matchedCount -Updated $updatedCount -AlreadyCurrent $currentCount -Skipped $skippedCount -Filtered $filteredCount -Failed $failedCount -DriverVersion $versionAfter
    exit $exitCode
  }

  if ($verifyFailures.Count -gt 0) {
    $exitCode = $script:ExitVerify
    Write-Log -Level ERROR -Stage RESULT -Message "Verification failed for: $($verifyFailures -join ', ')"
    Write-Result -Status "VERIFYFAILED" -ExitCode $exitCode -Message "Verification failed: $($verifyFailures -join '; ')" -Matched $matchedCount -Updated $updatedCount -AlreadyCurrent $currentCount -Skipped $skippedCount -Filtered $filteredCount -Failed $failedCount -DriverVersion $versionAfter
    exit $exitCode
  }

  $finalStatus = "SUCCESS"
  $finalMessage = "Updated $updatedCount queue(s) to '$driverName'"
  if ($isVersionUpdate) {
    if ($versionChanged) {
      $finalMessage = "Driver '$driverName' updated from version $versionBefore to $versionAfter"
      if ($updatedCount -gt 0) { $finalMessage += "; $updatedCount queue(s) rebound" }
    } elseif ($updatedCount -eq 0) {
      # Nothing moved: the package was probably already the installed build. Non-alerting,
      # but given its own status so a monitor can single it out if it wants to.
      $finalStatus = "VERSIONUNCHANGED"
      $finalMessage = "Driver '$driverName' still reports version $versionAfter after staging the package"
    }
  }

  Write-Result -Status $finalStatus -ExitCode $script:ExitOk -Message $finalMessage -Matched $matchedCount -Updated $updatedCount -AlreadyCurrent $currentCount -Skipped $skippedCount -Filtered $filteredCount -DriverVersion $versionAfter
  $exitCode = $script:ExitOk
  exit $exitCode

} catch {
  $message = $_.Exception.Message
  if ($message -like "Missing required environment variable*") {
    $exitCode = $script:ExitConfig
    Write-Log -Level ERROR -Stage INIT -Message $message
    Write-Result -Status "CONFIGERROR" -ExitCode $exitCode -Message $message -Matched $matchedCount -AlreadyCurrent $currentCount -Filtered $filteredCount
  } else {
    $exitCode = $script:ExitUnexpected
    Write-Log -Level ERROR -Stage RESULT -Message "Unexpected error: $message"
    Write-Result -Status "ERROR" -ExitCode $exitCode -Message $message -Matched $matchedCount -Updated $updatedCount -AlreadyCurrent $currentCount -Skipped $skippedCount -Filtered $filteredCount -Failed $failedCount -DriverVersion $versionAfter
  }
  exit $exitCode

} finally {
  if ($script:WorkingRoot) {
    if ($keepWorkingFiles) {
      Write-Log -Level INFO -Stage CLEANUP -Message "KeepWorkingFiles is set; leaving $($script:WorkingRoot) in place."
    } else {
      Remove-WorkingFolder -Path $script:WorkingRoot
    }
  }
}
