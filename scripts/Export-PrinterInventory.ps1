<#
.SYNOPSIS
Report the local print queues and drivers on a device as ready-to-paste Update-PrinterDriver
component variable values.

.DESCRIPTION
Read-only companion to Update-PrinterDriver.ps1. It answers the question you have to answer
before you can schedule that component: what exactly do I put in DriverMatchString, DriverName
and MinimumDriverVersion for the printers this fleet actually has?

Built to run as a Datto RMM component. Inputs are read from environment variables (component
variables), so it can be pushed fleet-wide and the job output read back per device.

Flow:
  1) Enumerate every local print queue with Get-Printer and every registered driver with
     Get-PrinterDriver, then narrow the set with the same optional filters Update-PrinterDriver
     uses (DriverMatchString / PrinterNameMatch / PrinterNameExclude).
  2) Group the queues by driver name - one Update-PrinterDriver job targets one driver.
  3) For each driver, emit a [SUGGEST] block carrying the exact values to paste into the
     Update-PrinterDriver component variables, plus the checks that say whether those values
     are safe (how many queues a match string catches, whether a queue name would break the
     comma-separated filter format, whether the driver is shared across several queues).
  4) Optionally write the same data to CSV and/or JSON, and a compact summary to a Datto RMM
     user-defined field.
  5) Emit a greppable result line inside Datto's result markers.

Nothing is installed and no queue, driver or spooler setting is changed.

Environment variables (Datto RMM component variables):

  DriverMatchString   (optional) Only report queues whose driver name contains this substring,
                                 e.g. "Brother". Case-insensitive. Unset reports every queue,
                                 which is the usual first pass on an unknown fleet.
  PrinterNameMatch    (optional) Comma/semicolon-separated queue names to limit the report to,
                                 e.g. "Reception MFP,Warehouse*". A pattern with * or ? is a
                                 wildcard; anything else matches as a substring. Same format and
                                 semantics as Update-PrinterDriver.
  PrinterNameExclude  (optional) Same format; matching queues are left out. Applied after
                                 PrinterNameMatch.
  IncludeConnections  (optional) "false" to leave out queues that are connections to a print
                                 server. Default "true": they are reported and flagged, because
                                 Update-PrinterDriver skips them and you want to know they exist.
  CsvPath             (optional) Write the inventory to this CSV. A path that names an existing
                                 directory (or ends in a separator) gets "<COMPUTERNAME>-printers.csv"
                                 written inside it, so a fleet job can collect one file per device
                                 into a share without devices overwriting each other.
  JsonPath            (optional) Write the same inventory as JSON. Same directory handling.
  UdfNumber           (optional) 1-30. Writes a compact driver summary to that Datto RMM
                                 user-defined field so devices can be filtered by printer driver
                                 in the console. Requires elevation. UDFs hold 255 characters;
                                 a longer summary is truncated and the run logs that it was.
  LogPath             (optional) Full path to the log file. Defaults to a Datto RMM-visible path
                                 (ProgramData\CentraStage when present, otherwise ProgramData).
  TreatNoMatchAsError (optional) "true" to exit non-zero when no queue is found. Default "false",
                                 which is the fleet-safe behaviour (no RMM alert on a device that
                                 simply has no printers).

Exit codes (for Datto RMM alerting):
   0  Success - the inventory was produced, or there was nothing to report
   1  Unexpected error
   2  Configuration error (bad UdfNumber, UDF write requested without elevation)
   3  Failed to write CsvPath / JsonPath / the UDF
   6  No queues found and TreatNoMatchAsError was true

.EXAMPLE
.\scripts\Export-PrinterInventory.ps1

Reports every print queue on the device.

.EXAMPLE
$env:DriverMatchString = "Brother"
$env:CsvPath = "\\fileserver\rmm$\printers\"
.\scripts\Export-PrinterInventory.ps1

Reports only Brother queues and drops a per-device CSV on a share.

.NOTES
Read-only, so it does not need elevation unless UdfNumber is set. Safe to schedule.

The MinimumDriverVersion value it reports is the raw string Get-PrinterDriver returns, because
that is the form Update-PrinterDriver can compare against. Windows usually reports a packed
64-bit integer rather than a dotted version; the decoded dotted form is logged alongside it for
reading, but it is the raw value that belongs in the component variable.
#>

[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$script:ExitOk         = 0
$script:ExitUnexpected = 1
$script:ExitConfig     = 2
$script:ExitExport     = 3
$script:ExitNoMatch    = 6

$script:LogFile = $null

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
    return (Join-Path $centraStage "Export-PrinterInventory.log")
  }

  return (Join-Path $programData "Export-PrinterInventory.log")
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
    Stage tags (INIT/DISCOVER/SUGGEST/EXPORT/RESULT) are the hooks a Datto RMM monitor keys off.
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

function Get-SafeProperty {
  <#
    StrictMode turns a missing property into a terminating error, and the print cmdlets expose
    different property sets across Windows builds. Ask for a property without betting on it.
  #>
  param(
    [Parameter()] $InputObject,
    [Parameter(Mandatory = $true)][string] $Name
  )

  if ($null -eq $InputObject) { return $null }
  try {
    $value = $InputObject.$Name
    if ($null -eq $value) { return $null }
    return $value
  } catch {
    return $null
  }
}

function Split-PatternList {
  <# Component variables carry lists as comma- or semicolon-separated text. #>
  param([Parameter()][string] $Value)

  if (-not $Value) { return @() }
  return @($Value -split "[;,]" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Test-NameMatchesAny {
  <#
    Same rule Update-PrinterDriver applies: a pattern containing * or ? is used as a wildcard,
    anything else is treated as a substring. Duplicated deliberately so what this script reports
    is what that script will actually do.
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

function ConvertTo-DottedDriverVersion {
  <#
    Get-PrinterDriver usually reports DriverVersion as a packed 64-bit integer, four 16-bit
    fields high to low. Decode it for reading only - the raw value is what Update-PrinterDriver
    can compare, so that is what gets suggested for MinimumDriverVersion.
    Returns $null when the value is not a packed integer (some drivers report dotted already).
  #>
  param([Parameter()] $Version)

  if ($null -eq $Version) { return $null }

  $text = "$Version".Trim()
  if (-not $text) { return $null }
  if ($text -match "\.") { return $null }

  $packed = [uint64]0
  if (-not [uint64]::TryParse($text, [ref]$packed)) { return $null }

  # Split the four 16-bit fields with BitConverter rather than -shr/-band: the shift operators
  # disagree about unsigned 64-bit operands between Windows PowerShell 5.1 and PowerShell 7,
  # and these endpoints run 5.1.
  $bytes = [System.BitConverter]::GetBytes($packed)
  if (-not [System.BitConverter]::IsLittleEndian) { [array]::Reverse($bytes) }

  $revision = [System.BitConverter]::ToUInt16($bytes, 0)
  $build    = [System.BitConverter]::ToUInt16($bytes, 2)
  $minor    = [System.BitConverter]::ToUInt16($bytes, 4)
  $major    = [System.BitConverter]::ToUInt16($bytes, 6)

  return "$major.$minor.$build.$revision"
}

function Get-SuggestedMatchString {
  <#
    Turn a driver name into a DriverMatchString candidate by dropping the parts that change
    between builds of the same driver: a trailing version parenthetical, and the generic
    trailing words vendors append.

      "Brother HL-L2350D series"          -> "Brother HL-L2350D"
      "HP Universal Printing PCL 6 (v7.0.1)" -> "HP Universal Printing PCL 6"

    Only ever removes a suffix, so the result is always a substring of the driver name and will
    match it. Whether it matches too much is checked separately and reported.
  #>
  param([Parameter(Mandatory = $true)][string] $DriverName)

  $candidate = [regex]::Replace($DriverName, '\s*\([^)]*\)\s*$', '')

  $trailingWords = @("series", "class driver", "driver", "printer")
  $changed = $true
  while ($changed) {
    $changed = $false
    foreach ($word in $trailingWords) {
      $pattern = "(?i)\s+" + [regex]::Escape($word) + "$"
      if ($candidate -match $pattern) {
        $candidate = [regex]::Replace($candidate, $pattern, "")
        $changed = $true
      }
    }
  }

  $candidate = $candidate.Trim()
  if (-not $candidate) { return $DriverName }
  return $candidate
}

function Resolve-ExportPath {
  <#
    A path naming an existing directory, or ending in a separator, gets a per-device file name
    inside it. That is what lets a fleet job point every device at one share without each
    device clobbering the last one's file.
  #>
  param(
    [Parameter(Mandatory = $true)][string] $Path,
    [Parameter(Mandatory = $true)][string] $Extension
  )

  $isDirectory = $Path.EndsWith("\") -or $Path.EndsWith("/")
  if (-not $isDirectory) {
    if (Test-Path -LiteralPath $Path -PathType Container) { $isDirectory = $true }
  }

  if ($isDirectory) {
    return (Join-Path $Path ("{0}-printers.{1}" -f $env:COMPUTERNAME, $Extension))
  }

  return $Path
}

function New-ExportDirectory {
  param([Parameter(Mandatory = $true)][string] $FilePath)

  $directory = Split-Path -Path $FilePath -Parent
  if ($directory -and -not (Test-Path -LiteralPath $directory)) {
    New-Item -Path $directory -ItemType Directory -Force | Out-Null
  }
}

function Write-DattoUdf {
  <#
    Datto RMM surfaces HKLM\SOFTWARE\CentraStage "CustomN" values as user-defined fields, so a
    driver summary written here can be filtered on in the console. The field holds 255 chars.
  #>
  param(
    [Parameter(Mandatory = $true)][int] $Number,
    [Parameter(Mandatory = $true)][string] $Value
  )

  $key = "HKLM:\SOFTWARE\CentraStage"
  if (-not (Test-Path -LiteralPath $key)) {
    New-Item -Path $key -Force | Out-Null
  }

  $payload = $Value
  if ($payload.Length -gt 255) {
    $payload = $payload.Substring(0, 252) + "..."
    Write-Log -Level WARN -Stage EXPORT -Message "UDF $Number payload was $($Value.Length) characters and has been truncated to 255."
  }

  Set-ItemProperty -LiteralPath $key -Name ("Custom" + $Number) -Value $payload -Force
  Write-Log -Level INFO -Stage EXPORT -Message "Wrote UDF $Number : $payload"
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
    [Parameter()][int] $Queues = 0,
    [Parameter()][int] $Drivers = 0,
    [Parameter()][int] $Connections = 0,
    [Parameter()][int] $Filtered = 0,
    [Parameter()][string] $CsvFile,
    [Parameter()][string] $JsonFile
  )

  $csvField  = $CsvFile;  if (-not $csvField)  { $csvField  = "none" }
  $jsonField = $JsonFile; if (-not $jsonField) { $jsonField = "none" }

  $summary = "PRINTERINVENTORY_RESULT STATUS=$Status EXIT=$ExitCode QUEUES=$Queues DRIVERS=$Drivers CONNECTIONS=$Connections FILTERED=$Filtered CSV=""$csvField"" JSON=""$jsonField"" MESSAGE=""$Message"""
  Write-Log -Level $(if ($ExitCode -eq 0) { "INFO" } else { "ERROR" }) -Stage RESULT -Message $summary

  [Console]::Out.WriteLine("<-Start Result->")
  [Console]::Out.WriteLine($summary)
  [Console]::Out.WriteLine("<-End Result->")
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

$driverMatchString   = Get-EnvValue -Name "DriverMatchString"
$printerNameMatch    = Get-EnvValue -Name "PrinterNameMatch"
$printerNameExclude  = Get-EnvValue -Name "PrinterNameExclude"
$csvPath             = Get-EnvValue -Name "CsvPath"
$jsonPath            = Get-EnvValue -Name "JsonPath"
$udfNumberRaw        = Get-EnvValue -Name "UdfNumber"
$logPath             = Get-EnvValue -Name "LogPath"
$includeConnections  = Get-EnvBool  -Name "IncludeConnections" -Default $true
$treatNoMatchAsError = Get-EnvBool  -Name "TreatNoMatchAsError" -Default $false

$queueCount      = 0
$driverCount     = 0
$connectionCount = 0
$filteredCount   = 0
$csvWritten      = $null
$jsonWritten     = $null
$exitCode        = $script:ExitOk

try {
  Initialize-Log -Path (Resolve-LogPath -ExplicitPath $logPath)
} catch {
  [Console]::Out.WriteLine("Unable to initialise log file: $($_.Exception.Message)")
}

try {
  Write-Log -Level INFO -Stage INIT -Message "Export-PrinterInventory starting on $env:COMPUTERNAME (PowerShell $($PSVersionTable.PSVersion))"

  # --- Validate the inputs that can be wrong before any work is done -------
  $udfNumber = 0
  if ($udfNumberRaw) {
    if (-not [int]::TryParse($udfNumberRaw, [ref]$udfNumber) -or $udfNumber -lt 1 -or $udfNumber -gt 30) {
      $exitCode = $script:ExitConfig
      Write-Log -Level ERROR -Stage INIT -Message "UdfNumber must be a whole number from 1 to 30; got '$udfNumberRaw'."
      Write-Result -Status "CONFIGERROR" -ExitCode $exitCode -Message "UdfNumber '$udfNumberRaw' is not in the range 1-30"
      exit $exitCode
    }
    if (-not (Test-IsAdministrator)) {
      $exitCode = $script:ExitConfig
      Write-Log -Level ERROR -Stage INIT -Message "UdfNumber is set but the script is not running elevated; writing a user-defined field needs HKLM access."
      Write-Result -Status "CONFIGERROR" -ExitCode $exitCode -Message "UDF write requires elevation"
      exit $exitCode
    }
  }

  if ($driverMatchString)   { Write-Log -Level INFO -Stage INIT -Message "DriverMatchString='$driverMatchString'" }
  if ($printerNameMatch)    { Write-Log -Level INFO -Stage INIT -Message "PrinterNameMatch='$printerNameMatch'" }
  if ($printerNameExclude)  { Write-Log -Level INFO -Stage INIT -Message "PrinterNameExclude='$printerNameExclude'" }
  if (-not $includeConnections) { Write-Log -Level INFO -Stage INIT -Message "IncludeConnections=false; print server connections will be left out." }

  if (-not (Test-PrintCmdletsAvailable)) {
    # No print subsystem means there is definitively nothing to inventory - not an alert.
    Write-Log -Level WARN -Stage DISCOVER -Message "Get-Printer is unavailable on this device (no print subsystem). Nothing to report."
    Write-Result -Status "NOTHINGTODO" -ExitCode $script:ExitOk -Message "Print cmdlets unavailable on this device"
    exit $script:ExitOk
  }

  # --- Step 1: enumerate ---------------------------------------------------
  $allPrinters = @(Get-Printer -ErrorAction Stop)
  Write-Log -Level INFO -Stage DISCOVER -Message "Device has $($allPrinters.Count) print queue(s) before filtering."

  # One lookup of every registered driver, so per-queue version reads do not re-query.
  $driverLookup = @{}
  try {
    foreach ($driver in @(Get-PrinterDriver -ErrorAction Stop)) {
      $name = Get-SafeProperty -InputObject $driver -Name "Name"
      if ($name -and -not $driverLookup.ContainsKey("$name")) {
        $driverLookup["$name"] = $driver
      }
    }
    Write-Log -Level INFO -Stage DISCOVER -Message "Device has $($driverLookup.Count) registered printer driver(s)."
  } catch {
    Write-Log -Level WARN -Stage DISCOVER -Message "Could not enumerate printer drivers, so versions will be reported as unknown: $($_.Exception.Message)"
  }

  # Port details fill in the host address for TCP/IP queues, which is what you need to tell
  # two queues on the same driver apart.
  $portLookup = @{}
  try {
    foreach ($port in @(Get-PrinterPort -ErrorAction Stop)) {
      $portName = Get-SafeProperty -InputObject $port -Name "Name"
      if ($portName -and -not $portLookup.ContainsKey("$portName")) {
        $portLookup["$portName"] = $port
      }
    }
  } catch {
    Write-Log -Level WARN -Stage DISCOVER -Message "Could not enumerate printer ports; host addresses will be blank: $($_.Exception.Message)"
  }

  # The default-printer flag is per user profile. Under SYSTEM (how Datto RMM runs components)
  # it reflects SYSTEM's profile, not the signed-in user's, so it is reported but not trusted.
  $defaultPrinterName = $null
  try {
    $defaultPrinter = @(Get-CimInstance -ClassName Win32_Printer -ErrorAction Stop | Where-Object { $_.Default }) | Select-Object -First 1
    if ($defaultPrinter) { $defaultPrinterName = "$($defaultPrinter.Name)" }
  } catch {
    Write-Log -Level WARN -Stage DISCOVER -Message "Could not read the default printer from Win32_Printer: $($_.Exception.Message)"
  }

  # --- Step 2: filter ------------------------------------------------------
  $includePatterns = @(Split-PatternList -Value $printerNameMatch)
  $excludePatterns = @(Split-PatternList -Value $printerNameExclude)

  $selected = @()
  foreach ($printer in $allPrinters) {
    $queueName  = "$(Get-SafeProperty -InputObject $printer -Name 'Name')"
    $driverName = "$(Get-SafeProperty -InputObject $printer -Name 'DriverName')"
    $queueType  = "$(Get-SafeProperty -InputObject $printer -Name 'Type')"

    if ($driverMatchString -and ($driverName -notlike "*$driverMatchString*")) {
      $filteredCount++
      continue
    }
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
    if (-not $includeConnections -and $queueType -eq "Connection") {
      $filteredCount++
      Write-Log -Level INFO -Stage DISCOVER -Message "FILTERED queue '$queueName' is a print server connection and IncludeConnections is false."
      continue
    }

    $selected += $printer
  }

  if ($selected.Count -eq 0) {
    if ($treatNoMatchAsError) {
      $exitCode = $script:ExitNoMatch
      Write-Log -Level ERROR -Stage DISCOVER -Message "No print queue to report and TreatNoMatchAsError is true."
      Write-Result -Status "NOMATCH" -ExitCode $exitCode -Message "No print queue matched the filters" -Filtered $filteredCount
      exit $exitCode
    }

    Write-Log -Level INFO -Stage DISCOVER -Message "No print queue to report. Nothing to do."
    Write-Result -Status "NOTHINGTODO" -ExitCode $script:ExitOk -Message "No print queue matched the filters" -Filtered $filteredCount
    exit $script:ExitOk
  }

  # --- Step 3: build one record per queue ----------------------------------
  $records = @()
  foreach ($printer in $selected) {
    $queueName  = "$(Get-SafeProperty -InputObject $printer -Name 'Name')"
    $driverName = "$(Get-SafeProperty -InputObject $printer -Name 'DriverName')"
    $queueType  = "$(Get-SafeProperty -InputObject $printer -Name 'Type')"
    $portName   = "$(Get-SafeProperty -InputObject $printer -Name 'PortName')"

    if ($queueType -eq "Connection") { $connectionCount++ }

    $driver = $null
    if ($driverLookup.ContainsKey($driverName)) { $driver = $driverLookup[$driverName] }

    # DriverVersion first, MajorVersion as the fallback - matching how Update-PrinterDriver
    # reads it, so the value reported here is the value that script will compare.
    $rawVersion = Get-SafeProperty -InputObject $driver -Name "DriverVersion"
    if ($null -eq $rawVersion) { $rawVersion = Get-SafeProperty -InputObject $driver -Name "MajorVersion" }
    $rawVersionText = if ($null -ne $rawVersion) { "$rawVersion" } else { $null }
    $dottedVersion = ConvertTo-DottedDriverVersion -Version $rawVersionText

    $port = $null
    if ($portName -and $portLookup.ContainsKey($portName)) { $port = $portLookup[$portName] }
    $hostAddress = Get-SafeProperty -InputObject $port -Name "PrinterHostAddress"

    $records += [pscustomobject][ordered]@{
      ComputerName        = $env:COMPUTERNAME
      PrinterName         = $queueName
      DriverName          = $driverName
      DriverVersionRaw    = $rawVersionText
      DriverVersionDotted = $dottedVersion
      DriverManufacturer  = "$(Get-SafeProperty -InputObject $driver -Name 'Manufacturer')"
      PrinterEnvironment  = "$(Get-SafeProperty -InputObject $driver -Name 'PrinterEnvironment')"
      Type                = $queueType
      PortName            = $portName
      PortHostAddress     = "$hostAddress"
      Shared              = [bool](Get-SafeProperty -InputObject $printer -Name "Shared")
      ShareName           = "$(Get-SafeProperty -InputObject $printer -Name 'ShareName')"
      Published           = [bool](Get-SafeProperty -InputObject $printer -Name "Published")
      IsDefaultForCaller  = ($defaultPrinterName -and $queueName -eq $defaultPrinterName)
      SuggestedMatchString = Get-SuggestedMatchString -DriverName $driverName
      CollectedUtc        = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }

    $versionDisplay = if ($rawVersionText) { $rawVersionText } else { "unknown" }
    if ($dottedVersion) { $versionDisplay += " ($dottedVersion)" }

    Write-Log -Level INFO -Stage DISCOVER -Message "Queue '$queueName' driver='$driverName' version=$versionDisplay type=$queueType port='$portName'"
  }

  $queueCount = $records.Count

  # --- Step 4: one suggestion block per driver -----------------------------
  # An Update-PrinterDriver job targets one driver, so the driver is the unit to report on.
  $driverGroups = @($records | Group-Object -Property DriverName | Sort-Object -Property Name)
  $driverCount = $driverGroups.Count

  Write-Log -Level INFO -Stage SUGGEST -Message "$queueCount queue(s) across $driverCount driver(s). Values below go into the Update-PrinterDriver component variables - one job per driver."

  $groupIndex = 0
  foreach ($group in $driverGroups) {
    $groupIndex++
    $groupDriverName = "$($group.Name)"
    $groupRecords = @($group.Group)
    $first = $groupRecords[0]
    $matchSuggestion = "$($first.SuggestedMatchString)"

    Write-Log -Level INFO -Stage SUGGEST -Message "--- Driver $groupIndex of ${driverCount}: '$groupDriverName' ---"
    Write-Log -Level INFO -Stage SUGGEST -Message "  DriverMatchString    = $matchSuggestion"
    Write-Log -Level INFO -Stage SUGGEST -Message "  DriverName           = $groupDriverName"

    if ($first.DriverVersionRaw) {
      $versionNote = "  MinimumDriverVersion = $($first.DriverVersionRaw)"
      if ($first.DriverVersionDotted) {
        $versionNote += "   (reads as $($first.DriverVersionDotted); paste the raw value, that is the form the update script compares)"
      }
      Write-Log -Level INFO -Stage SUGGEST -Message $versionNote
    } else {
      Write-Log -Level WARN -Stage SUGGEST -Message "  MinimumDriverVersion = unknown - this driver reports no version, so leave the variable unset."
    }

    $queueNames = @($groupRecords | ForEach-Object { $_.PrinterName })
    Write-Log -Level INFO -Stage SUGGEST -Message "  PrinterNameMatch     = $($queueNames -join ',')   (omit to target every queue on this driver)"
    Write-Log -Level INFO -Stage SUGGEST -Message "  Queues ($($queueNames.Count)): $($queueNames -join '; ')"

    # Does the suggested match string catch anything beyond this driver? Update-PrinterDriver
    # matches with -like "*string*" against the driver name, so replicate that exactly.
    $collateral = @($records | Where-Object { $_.DriverName -ne $groupDriverName -and $_.DriverName -like "*$matchSuggestion*" })
    if ($collateral.Count -gt 0) {
      $collateralDrivers = @($collateral | Select-Object -ExpandProperty DriverName -Unique)
      Write-Log -Level WARN -Stage SUGGEST -Message "  DriverMatchString '$matchSuggestion' also matches $($collateral.Count) queue(s) on other driver(s): $($collateralDrivers -join '; '). Narrow it, or scope the job with PrinterNameMatch."
    }

    # Several queues on one driver means an in-place version update reaches all of them,
    # whatever the name filter says - the same caveat Update-PrinterDriver warns about at run time.
    if ($groupRecords.Count -gt 1) {
      Write-Log -Level WARN -Stage SUGGEST -Message "  $($groupRecords.Count) queues share driver '$groupDriverName'. A newer build of the same driver updates it in place and reaches every one of them; PrinterNameMatch cannot scope that."
    }

    # A queue name carrying a list separator or a wildcard character cannot be used verbatim
    # in PrinterNameMatch, because that variable is split on , and ; and read as a wildcard.
    foreach ($record in $groupRecords) {
      $name = "$($record.PrinterName)"
      if ($name -match "[;,]") {
        Write-Log -Level WARN -Stage SUGGEST -Message "  Queue '$name' contains a comma or semicolon, so it cannot go into PrinterNameMatch verbatim - that variable is split on both. Use a distinctive fragment of the name instead."
      }
      if ($name -match "[\*\?]") {
        Write-Log -Level WARN -Stage SUGGEST -Message "  Queue '$name' contains * or ?, which PrinterNameMatch reads as a wildcard. Match on a fragment without those characters."
      }
    }

    $connectionQueues = @($groupRecords | Where-Object { $_.Type -eq "Connection" })
    if ($connectionQueues.Count -gt 0) {
      $connectionNames = @($connectionQueues | ForEach-Object { $_.PrinterName })
      Write-Log -Level WARN -Stage SUGGEST -Message "  $($connectionQueues.Count) of these are print server connections ($($connectionNames -join '; ')). Update-PrinterDriver skips them - their driver is managed on the server."
    }
  }

  # --- Step 5: optional exports -------------------------------------------
  if ($csvPath) {
    try {
      $resolvedCsv = Resolve-ExportPath -Path $csvPath -Extension "csv"
      New-ExportDirectory -FilePath $resolvedCsv
      $records | Export-Csv -LiteralPath $resolvedCsv -NoTypeInformation -Encoding UTF8
      $csvWritten = $resolvedCsv
      Write-Log -Level INFO -Stage EXPORT -Message "Wrote $queueCount row(s) to $resolvedCsv"
    } catch {
      $exitCode = $script:ExitExport
      Write-Log -Level ERROR -Stage EXPORT -Message "Could not write CSV to '$csvPath': $($_.Exception.Message)"
      Write-Result -Status "EXPORTFAILED" -ExitCode $exitCode -Message "Failed to write CSV to '$csvPath'" -Queues $queueCount -Drivers $driverCount -Connections $connectionCount -Filtered $filteredCount
      exit $exitCode
    }
  }

  if ($jsonPath) {
    try {
      $resolvedJson = Resolve-ExportPath -Path $jsonPath -Extension "json"
      New-ExportDirectory -FilePath $resolvedJson
      # -Depth 4 covers the flat records plus the array wrapper on PowerShell 5.1.
      $json = $records | ConvertTo-Json -Depth 4
      # Written via .NET rather than Set-Content because Windows PowerShell 5.1 emits a UTF-8
      # BOM, which trips strict JSON parsers on whatever collects these files.
      $absoluteJson = $resolvedJson
      if (-not [System.IO.Path]::IsPathRooted($absoluteJson)) {
        $absoluteJson = Join-Path (Get-Location).Path $absoluteJson
      }
      [System.IO.File]::WriteAllText($absoluteJson, $json, (New-Object System.Text.UTF8Encoding($false)))
      $jsonWritten = $resolvedJson
      Write-Log -Level INFO -Stage EXPORT -Message "Wrote $queueCount record(s) to $resolvedJson"
    } catch {
      $exitCode = $script:ExitExport
      Write-Log -Level ERROR -Stage EXPORT -Message "Could not write JSON to '$jsonPath': $($_.Exception.Message)"
      Write-Result -Status "EXPORTFAILED" -ExitCode $exitCode -Message "Failed to write JSON to '$jsonPath'" -Queues $queueCount -Drivers $driverCount -Connections $connectionCount -Filtered $filteredCount -CsvFile $csvWritten
      exit $exitCode
    }
  }

  if ($udfNumber -gt 0) {
    try {
      $udfParts = @($driverGroups | ForEach-Object {
        $groupFirst = @($_.Group)[0]
        $version = $groupFirst.DriverVersionRaw
        if (-not $version) { $version = "?" }
        "$($_.Name) v$version x$($_.Count)"
      })
      Write-DattoUdf -Number $udfNumber -Value ($udfParts -join " | ")
    } catch {
      $exitCode = $script:ExitExport
      Write-Log -Level ERROR -Stage EXPORT -Message "Could not write UDF ${udfNumber}: $($_.Exception.Message)"
      Write-Result -Status "EXPORTFAILED" -ExitCode $exitCode -Message "Failed to write UDF $udfNumber" -Queues $queueCount -Drivers $driverCount -Connections $connectionCount -Filtered $filteredCount -CsvFile $csvWritten -JsonFile $jsonWritten
      exit $exitCode
    }
  }

  Write-Result -Status "SUCCESS" -ExitCode $script:ExitOk -Message "Reported $queueCount queue(s) across $driverCount driver(s)" -Queues $queueCount -Drivers $driverCount -Connections $connectionCount -Filtered $filteredCount -CsvFile $csvWritten -JsonFile $jsonWritten
  $exitCode = $script:ExitOk
  exit $exitCode

} catch {
  $exitCode = $script:ExitUnexpected
  Write-Log -Level ERROR -Stage RESULT -Message "Unexpected error: $($_.Exception.Message)"
  Write-Result -Status "ERROR" -ExitCode $exitCode -Message $_.Exception.Message -Queues $queueCount -Drivers $driverCount -Connections $connectionCount -Filtered $filteredCount -CsvFile $csvWritten -JsonFile $jsonWritten
  exit $exitCode
}
