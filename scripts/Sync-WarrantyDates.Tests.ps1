# Plain-PowerShell checks for the date rules (no Pester needed): pwsh ./scripts/Sync-WarrantyDates.Tests.ps1
. "$PSScriptRoot/Sync-WarrantyDates.ps1" -LoadFunctionsOnly
$fail = 0
function Check($name, $got, $want) {
  if ("$got" -ne "$want") { Write-Host "FAIL $name : got '$got' want '$want'"; $script:fail++ } else { Write-Host "ok   $name" }
}
$r = Get-WarrantyDecision -Glue "2026-01-01" -MyITP "";           Check "mitp blank keeps glue" $r.Target "2026-01-01"
$r = Get-WarrantyDecision -Glue "" -MyITP "2027-03-04T00:00:00Z"; Check "glue blank uses mitp (trimmed)" $r.Target "2027-03-04"
$r = Get-WarrantyDecision -Glue "2026-01-01" -MyITP "2027-01-01"; Check "glue earlier uses mitp" $r.Target "2027-01-01"; Check "glue earlier not later" $r.Later $false
$r = Get-WarrantyDecision -Glue "2028-01-01" -MyITP "2027-01-01"; Check "glue later keeps glue" $r.Target "2028-01-01"; Check "glue later flagged" $r.Later $true
$r = Get-WarrantyDecision -Glue "2027-01-01" -MyITP "2027-01-01"; Check "equal not later" $r.Later $false
$r = Get-WarrantyDecision -Glue "" -MyITP "";                     Check "both blank" $r.Target ""
Check "status error"   (Get-DeviceStatus -Later $false -MissingId $false -HadError $true  -DryRun $false) "Error"
Check "status dry"     (Get-DeviceStatus -Later $false -MissingId $false -HadError $false -DryRun $true)  "Dry run"
Check "status missing" (Get-DeviceStatus -Later $true  -MissingId $true  -HadError $false -DryRun $false) "Missing ID"
Check "status later"   (Get-DeviceStatus -Later $true  -MissingId $false -HadError $false -DryRun $false) "Glue later - review"
Check "status ok"      (Get-DeviceStatus -Later $false -MissingId $false -HadError $false -DryRun $false) "OK"
if ($fail) { exit 1 }
