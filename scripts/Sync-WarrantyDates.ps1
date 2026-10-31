<#
.SYNOPSIS
Sync laptop/desktop warranty dates from IT Glue (fed by myITProcess) to IT Glue, Datto RMM and Autotask PSA.

.DESCRIPTION
Script implementation of the "Warranty Date Sync" plan (same logic as the Power Automate flow in
warranty-sync/). The script:
  1) Reads every active, non-archived Laptop and Desktop configuration from IT Glue (all organisations)
  2) Works out the target warranty date (see Get-WarrantyDecision)
  3) Compares it with the date last pushed (state file) and keeps only the changes
  4) Writes the date to IT Glue (if it changed), Datto RMM (rmm-id) and Autotask (psa-id)
  5) Records the result per device in the state file; PushedDate is only set when every write succeeded

Date rules:
  myITProcess blank                          -> keep IT Glue date
  IT Glue blank, or earlier than myITProcess -> use myITProcess date (written to IT Glue)
  IT Glue later than myITProcess             -> keep IT Glue date, Status "Glue later - review"
  rmm-id or psa-id missing                   -> skip that system, Status "Missing ID"

The script is DRY RUN unless -Apply is given.

.PARAMETER Apply
Actually write to IT Glue / Datto RMM / Autotask. Without it, only the state file is updated (Status "Dry run").

.PARAMETER ITGlueApiKey
IT Glue API key. Falls back to env ITGLUE_API_KEY.

.PARAMETER ITGlueBaseUri
Default https://api.itglue.com

.PARAMETER LaptopTypeId
.PARAMETER DesktopTypeId
.PARAMETER ActiveStatusId
IT Glue configuration type / status IDs (GET /configuration_types, /configuration_statuses).

.PARAMETER MyITProcessAttribute
Exact IT Glue configuration attribute holding the myITProcess warranty date (record from a test response).

.PARAMETER DattoApiUrl
Datto RMM platform API URL, e.g. https://pinotage-api.centrastage.net. Falls back to env DATTO_API_URL.

.PARAMETER DattoApiKey
.PARAMETER DattoApiSecret
Datto RMM API key/secret. Fall back to env DATTO_API_KEY / DATTO_API_SECRET.

.PARAMETER WriteAutotask
Write to Autotask PSA (default true). Use -WriteAutotask:$false to skip.

.PARAMETER AutotaskBaseUri
e.g. https://webservices5.autotask.net/ATServicesRest/V1.0. Falls back to env AUTOTASK_BASE_URI.

.PARAMETER AutotaskUsername
.PARAMETER AutotaskSecret
.PARAMETER AutotaskIntegrationCode
API user credentials. Fall back to env AUTOTASK_USERNAME / AUTOTASK_SECRET / AUTOTASK_INTEGRATION_CODE.

.PARAMETER MaxWritesPerRun
Cap on devices written per run (default 1500) so the first backfill spreads over several runs.

.PARAMETER StatePath
JSON state file (one row per IT Glue configuration ID). Default: .\WarrantySyncState.json

.PARAMETER LoadFunctionsOnly
Define the functions and exit (used by tests).

.EXAMPLE
.\scripts\Sync-WarrantyDates.ps1 -LaptopTypeId 111 -DesktopTypeId 222 -ActiveStatusId 3 -MyITProcessAttribute "myitprocess-warranty-expiry"
#>

[CmdletBinding(PositionalBinding = $false)]
param(
  [switch] $Apply,
  [string] $ITGlueApiKey,
  [string] $ITGlueBaseUri = "https://api.itglue.com",
  [long] $LaptopTypeId,
  [long] $DesktopTypeId,
  [long] $ActiveStatusId,
  [string] $MyITProcessAttribute,
  [string] $DattoApiUrl,
  [string] $DattoApiKey,
  [string] $DattoApiSecret,
  [bool] $WriteAutotask = $true,
  [string] $AutotaskBaseUri,
  [string] $AutotaskUsername,
  [string] $AutotaskSecret,
  [string] $AutotaskIntegrationCode,
  [ValidateRange(1, 100000)]
  [int] $MaxWritesPerRun = 1500,
  [string] $StatePath = ".\WarrantySyncState.json",
  [switch] $LoadFunctionsOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-Setting([string] $Value, [string] $EnvName) {
  if (-not [string]::IsNullOrWhiteSpace($Value)) { return $Value }
  $v = [Environment]::GetEnvironmentVariable($EnvName)
  if ([string]::IsNullOrWhiteSpace($v)) { return $null }
  return $v
}

# Trim to yyyy-MM-dd; null/blank -> ''
function ConvertTo-DateKey($Value) {
  if ($null -eq $Value) { return "" }
  $s = [string]$Value
  if ($s.Length -ge 10) { return $s.Substring(0, 10) }
  return $s.Trim()
}

function Get-WarrantyDecision {
  param([string] $Glue, [string] $MyITP)
  $glue = ConvertTo-DateKey $Glue
  $mitp = ConvertTo-DateKey $MyITP
  # yyyy-MM-dd strings compare correctly with ordinal comparison
  if ($mitp -eq "") { $target = $glue }
  elseif ($glue -eq "" -or [string]::CompareOrdinal($glue, $mitp) -lt 0) { $target = $mitp }
  else { $target = $glue }
  [pscustomobject]@{
    Target = $target
    Later  = ($mitp -ne "" -and $glue -ne "" -and [string]::CompareOrdinal($glue, $mitp) -gt 0)
  }
}

# Pure: which status the device ends up with. Writes = hashtable of system -> $true/$false/$null (null = not attempted)
function Get-DeviceStatus {
  param([bool] $Later, [bool] $MissingId, [bool] $HadError, [bool] $DryRun)
  if ($HadError) { return "Error" }
  if ($DryRun) { return "Dry run" }
  if ($MissingId) { return "Missing ID" }
  if ($Later) { return "Glue later - review" }
  return "OK"
}

function Invoke-Api {
  param([string] $Method, [string] $Uri, [hashtable] $Headers, $Body, [string] $ContentType = "application/json")
  $p = @{ Method = $Method; Uri = $Uri; Headers = $Headers }
  if ($null -ne $Body) {
    $p.Body = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 10 -Compress }
    $p.ContentType = $ContentType
  }
  for ($i = 1; $i -le 4; $i++) {
    try { return Invoke-RestMethod @p }
    catch {
      $code = $null
      if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
      if ($i -lt 4 -and ($code -eq 429 -or ($code -ge 500 -and $code -lt 600))) { Start-Sleep -Seconds ([math]::Pow(2, $i)); continue }
      throw
    }
  }
}

function Get-ITGlueDevices {
  param([string] $BaseUri, [string] $ApiKey, [long[]] $TypeIds, [long] $StatusId, [string] $MyITPAttribute)
  $headers = @{ "x-api-key" = $ApiKey; "Accept" = "application/vnd.api+json" }
  $devices = New-Object System.Collections.Generic.List[object]
  foreach ($typeId in $TypeIds) {
    $page = 1
    do {
      $uri = "$($BaseUri.TrimEnd('/'))/configurations?filter[configuration_type_id]=$typeId&filter[configuration_status_id]=$StatusId&filter[archived]=false&page[size]=1000&page[number]=$page"
      $resp = Invoke-Api -Method GET -Uri $uri -Headers $headers
      $items = @($resp.data)
      foreach ($d in $items) {
        $a = $d.attributes
        $devices.Add([pscustomobject]@{
          id     = [string]$d.id
          org    = $a.'organization-name'
          name   = $a.name
          serial = $a.'serial-number'
          rmm    = $a.'rmm-id'
          psa    = $a.'psa-id'
          glue   = ConvertTo-DateKey $a.'warranty-expires-at'
          mitp   = ConvertTo-DateKey $a.$MyITPAttribute
        })
      }
      $page++
    } while ($items.Count -ge 1000)
  }
  return $devices
}

function Get-DattoToken {
  param([string] $ApiUrl, [string] $Key, [string] $Secret)
  $basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("public-client:public"))
  $body = "grant_type=password&username=$([uri]::EscapeDataString($Key))&password=$([uri]::EscapeDataString($Secret))"
  $r = Invoke-Api -Method POST -Uri "$($ApiUrl.TrimEnd('/'))/auth/oauth/token" -Headers @{ Authorization = "Basic $basic" } -Body $body -ContentType "application/x-www-form-urlencoded"
  return $r.access_token
}

function Read-State([string] $Path) {
  $state = @{}
  if (Test-Path -LiteralPath $Path) {
    $raw = Get-Content -LiteralPath $Path -Raw
    if (-not [string]::IsNullOrWhiteSpace($raw)) {
      foreach ($row in @($raw | ConvertFrom-Json)) { $state[[string]$row.Id] = $row }
    }
  }
  return $state
}

function Write-State([string] $Path, $State) {
  $tmp = "$Path.tmp"
  @($State.Values) | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tmp -Encoding UTF8
  Move-Item -LiteralPath $tmp -Destination $Path -Force
}

if ($LoadFunctionsOnly) { return }

# ---- main ---------------------------------------------------------------------------------------
$ITGlueApiKey = Resolve-Setting $ITGlueApiKey "ITGLUE_API_KEY"
if (-not $ITGlueApiKey) { throw "IT Glue API key required (-ITGlueApiKey or ITGLUE_API_KEY)." }
foreach ($req in "LaptopTypeId", "DesktopTypeId", "ActiveStatusId", "MyITProcessAttribute") {
  if (-not $PSBoundParameters.ContainsKey($req)) { throw "-$req is required." }
}
$dryRun = -not $Apply

$DattoApiUrl = Resolve-Setting $DattoApiUrl "DATTO_API_URL"
$DattoApiKey = Resolve-Setting $DattoApiKey "DATTO_API_KEY"
$DattoApiSecret = Resolve-Setting $DattoApiSecret "DATTO_API_SECRET"
$AutotaskBaseUri = Resolve-Setting $AutotaskBaseUri "AUTOTASK_BASE_URI"
$AutotaskUsername = Resolve-Setting $AutotaskUsername "AUTOTASK_USERNAME"
$AutotaskSecret = Resolve-Setting $AutotaskSecret "AUTOTASK_SECRET"
$AutotaskIntegrationCode = Resolve-Setting $AutotaskIntegrationCode "AUTOTASK_INTEGRATION_CODE"

if (-not $dryRun) {
  if (-not ($DattoApiUrl -and $DattoApiKey -and $DattoApiSecret)) { throw "Datto RMM API URL/key/secret required with -Apply." }
  if ($WriteAutotask -and -not ($AutotaskBaseUri -and $AutotaskUsername -and $AutotaskSecret -and $AutotaskIntegrationCode)) {
    throw "Autotask settings required with -Apply (or use -WriteAutotask:`$false)."
  }
}

$devices = Get-ITGlueDevices -BaseUri $ITGlueBaseUri -ApiKey $ITGlueApiKey -TypeIds @($LaptopTypeId, $DesktopTypeId) -StatusId $ActiveStatusId -MyITPAttribute $MyITProcessAttribute
Write-Host "IT Glue: $($devices.Count) active laptops/desktops."

$state = Read-State $StatePath
$now = (Get-Date).ToString("yyyy-MM-dd HH:mm")

$decided = foreach ($d in $devices) {
  $dec = Get-WarrantyDecision -Glue $d.glue -MyITP $d.mitp
  $d | Add-Member -NotePropertyName target -NotePropertyValue $dec.Target -PassThru |
       Add-Member -NotePropertyName later -NotePropertyValue $dec.Later -PassThru
}

$toPush = @($decided | Where-Object {
  $_.target -ne "" -and -not ($state.ContainsKey($_.id) -and $state[$_.id].PushedDate -eq $_.target)
})
$batch = @($toPush | Select-Object -First $MaxWritesPerRun)
Write-Host "Changes pending: $($toPush.Count). Processing tonight: $($batch.Count). Mode: $(if ($dryRun) {'DRY RUN'} else {'APPLY'})."

$token = $null
if (-not $dryRun) { $token = Get-DattoToken -ApiUrl $DattoApiUrl -Key $DattoApiKey -Secret $DattoApiSecret }
$glueHeaders = @{ "x-api-key" = $ITGlueApiKey; "Accept" = "application/vnd.api+json" }

$written = 0; $errors = 0
foreach ($d in $batch) {
  $note = @(); $hadError = $false; $allWritten = $true
  $missing = [string]::IsNullOrWhiteSpace([string]$d.rmm) -or ($WriteAutotask -and [string]::IsNullOrWhiteSpace([string]$d.psa))

  if (-not $dryRun) {
    # a. IT Glue
    if ($d.target -ne $d.glue) {
      try {
        $body = @{ data = @{ type = "configurations"; attributes = @{ "warranty-expires-at" = $d.target } } }
        Invoke-Api -Method PATCH -Uri "$($ITGlueBaseUri.TrimEnd('/'))/configurations/$($d.id)" -Headers $glueHeaders -Body $body -ContentType "application/vnd.api+json" | Out-Null
      } catch { $hadError = $true; $allWritten = $false; $note += "ITGlue: $($_.Exception.Message)" }
    }
    # b. Datto RMM
    if (-not [string]::IsNullOrWhiteSpace([string]$d.rmm)) {
      try {
        Invoke-Api -Method POST -Uri "$($DattoApiUrl.TrimEnd('/'))/api/v2/device/$($d.rmm)/warranty" -Headers @{ Authorization = "Bearer $token" } -Body @{ warrantyDate = $d.target } | Out-Null
      } catch { $hadError = $true; $allWritten = $false; $note += "Datto: $($_.Exception.Message)" }
    } else { $allWritten = $false; $note += "rmm-id missing" }
    # c. Autotask
    if ($WriteAutotask) {
      if (-not [string]::IsNullOrWhiteSpace([string]$d.psa)) {
        try {
          $h = @{ UserName = $AutotaskUsername; Secret = $AutotaskSecret; ApiIntegrationCode = $AutotaskIntegrationCode }
          Invoke-Api -Method PATCH -Uri "$($AutotaskBaseUri.TrimEnd('/'))/ConfigurationItems" -Headers $h -Body @{ id = [long]$d.psa; warrantyExpirationDate = $d.target } | Out-Null
        } catch { $hadError = $true; $allWritten = $false; $note += "Autotask: $($_.Exception.Message)" }
      } else { $allWritten = $false; $note += "psa-id missing" }
    }
  }

  $status = Get-DeviceStatus -Later $d.later -MissingId $missing -HadError $hadError -DryRun $dryRun
  if ($hadError) { $errors++ }
  if (-not $dryRun -and $allWritten) { $written++ }

  $prev = if ($state.ContainsKey($d.id)) { $state[$d.id].PushedDate } else { "" }
  $state[$d.id] = [pscustomobject]@{
    Id = $d.id; Organisation = $d.org; Device = $d.name; Serial = $d.serial
    MyITPDate = $d.mitp; GlueDate = $d.glue
    PushedDate = if (-not $dryRun -and $allWritten) { $d.target } else { $prev }
    Status = $status; Note = ($note -join "; "); LastRun = $now
  }
}

Write-State -Path $StatePath -State $state

$attention = @($state.Values | Where-Object { $_.Status -ne "OK" })
Write-Host "Summary: devices=$($devices.Count) pending=$($toPush.Count) written=$written errors=$errors needs-attention=$($attention.Count)"
[pscustomobject]@{
  Devices = $devices.Count; ChangesPending = $toPush.Count; Processed = $batch.Count
  Written = $written; Errors = $errors; NeedsAttention = $attention.Count; DryRun = $dryRun
}
