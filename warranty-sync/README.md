# Warranty Date Sync

Implements the *Warranty Date Sync – Project Plan*: read every active Laptop/Desktop from IT Glue, work out
the correct warranty date (myITProcess vs IT Glue), and write only the changes to IT Glue, Datto RMM and Autotask.

Two implementations share the same rules:

| | Where | Use |
|---|---|---|
| PowerShell | `scripts/Sync-WarrantyDates.ps1` | Run anywhere (scheduled task / Datto component). State in a JSON file. Dry run unless `-Apply`. |
| Power Automate | this folder (`flow-expressions.md`, `sharepoint-list.md`, `datto-rmm-connector.swagger.json`) | Plan's target design. State in a SharePoint list. |

## Date rules
| Situation | Result |
|---|---|
| myITProcess blank | keep IT Glue date |
| IT Glue blank or earlier than myITProcess | use myITProcess, write to IT Glue |
| IT Glue later | keep IT Glue, status `Glue later - review` |
| differs from `PushedDate` | write to Datto RMM + Autotask |
| `rmm-id`/`psa-id` missing | skip that system, status `Missing ID` |

`PushedDate` is only set when every write succeeded, so failures retry next run.

## PowerShell quick start
```powershell
# prerequisites: IT Glue type/status IDs + exact myITProcess attribute name (see plan, step 1)
$env:ITGLUE_API_KEY="..."; $env:DATTO_API_URL="https://<platform>-api.centrastage.net"
$env:DATTO_API_KEY="..."; $env:DATTO_API_SECRET="..."
$env:AUTOTASK_BASE_URI="https://webservices5.autotask.net/ATServicesRest/V1.0"
$env:AUTOTASK_USERNAME="..."; $env:AUTOTASK_SECRET="..."; $env:AUTOTASK_INTEGRATION_CODE="..."

.\scripts\Sync-WarrantyDates.ps1 -LaptopTypeId 111 -DesktopTypeId 222 -ActiveStatusId 3 -MyITProcessAttribute "<attr>"          # dry run
.\scripts\Sync-WarrantyDates.ps1 ... -Apply -MaxWritesPerRun 25      # pilot
.\scripts\Sync-WarrantyDates.ps1 ... -Apply                          # backfill (cap 1500/run)
```
Rule tests: `pwsh ./scripts/Sync-WarrantyDates.Tests.ps1`.

Rollout follows plan section 5: dry run for a week, pilot at 25, then cut over and decommission ScalePad.
