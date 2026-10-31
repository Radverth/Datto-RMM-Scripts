<#
.SYNOPSIS
Create the "Warranty Sync State" SharePoint list (and "Needs attention" view) the flow reads and writes.
Requires: Install-Module PnP.PowerShell -Scope CurrentUser
.EXAMPLE
.\New-WarrantySyncStateList.ps1 -SiteUrl https://contoso.sharepoint.com/sites/IT
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string] $SiteUrl, [string] $ListTitle = "Warranty Sync State")
$ErrorActionPreference = "Stop"
Connect-PnPOnline -Url $SiteUrl -Interactive
New-PnPList -Title $ListTitle -Url "Lists/WarrantySyncState" -Template GenericList -OnQuickLaunch | Out-Null
Set-PnPField -List $ListTitle -Identity Title -Values @{ Indexed = $true; Title = "Configuration ID" } | Out-Null
foreach ($f in "Organisation", "Device", "Serial", "PushedDate") { Add-PnPField -List $ListTitle -InternalName $f -DisplayName $f -Type Text -AddToDefaultView | Out-Null }
foreach ($f in "MyITPDate", "GlueDate") { Add-PnPField -List $ListTitle -InternalName $f -DisplayName $f -Type DateTime -AddToDefaultView | Out-Null }
Add-PnPField -List $ListTitle -InternalName Status -DisplayName Status -Type Choice -AddToDefaultView `
  -Choices @("OK", "Glue later $([char]0x2013) review", "Missing ID", "Error", "Dry run") | Out-Null
Add-PnPField -List $ListTitle -InternalName Note -DisplayName Note -Type Note -AddToDefaultView | Out-Null
Add-PnPField -List $ListTitle -InternalName LastRun -DisplayName LastRun -Type DateTime -AddToDefaultView | Out-Null
Set-PnPField -List $ListTitle -Identity LastRun -Values @{ Indexed = $true } | Out-Null
Add-PnPView -List $ListTitle -Title "Needs attention" -Fields "Title","Organisation","Device","Serial","Status","Note","LastRun" `
  -Query "<Where><Neq><FieldRef Name='Status'/><Value Type='Choice'>OK</Value></Neq></Where>" | Out-Null
Write-Host "Created list '$ListTitle'. PushedDate is deliberately a text column (yyyy-MM-dd) to avoid time-zone shifts."
