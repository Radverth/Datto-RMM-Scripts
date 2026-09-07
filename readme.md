# Datto RMM Automation Scripts

This repo is a small collection of PowerShell scripts written to automate common tasks for devices managed by **Datto RMM**, including syncing credentials into **IT Glue**.

## Scripts

### Sync Entra LAPS to IT Glue

`scripts/Sync-EntraLapsToITGlue.ps1` retrieves a Windows LAPS password stored in Entra ID (Azure AD) via Microsoft Graph and upserts it into an IT Glue **Password** record.

`scripts/Set-LocalAdminAndSyncToITGlue.ps1` creates/updates a local admin account on a Windows PC and upserts that credential into IT Glue, associating it to the device Configuration by serial number.

### Reset AD user password

`scripts/Reset-ADPassword.ps1` runs on a Domain Controller, resets a single Active Directory user password, and unlocks the account.

#### Purpose & behavior
- Imports the `ActiveDirectory` module (requires Domain Admin or equivalent context) and ensures the target user exists in AD.
- Stops if the user is a member of **Domain Admins** or the built-in **Administrators** group to prevent high-privilege resets.
- Converts the provided `-Password` to a secure string, resets the password, and unlocks the account if it was locked.

#### Requirements
- Must run on a writable Domain Controller with the Active Directory PowerShell module available.
- Caller must have sufficient rights to query group membership and reset the specified account (typically Domain Admin or Account Operator privileges).

#### Usage
```powershell
.\scripts\Reset-ADPassword.ps1 `
  -Username "jdoe" `
  -Password "N3wP@ssword123!"
```

#### Parameters
- `-Username` (required): sAMAccountName of the AD user to reset.
- `-Password` (required): new password in plain text; the script converts it to a secure string before the reset.

#### Notes
- Intended for automation runbooks or scheduled maintenance where a known service account manages user passwords.
- Because the command accepts plaintext passwords, supply the value through secure pipelines or vaults whenever possible.
- The command emits an error and exits with code 2 or 3 when the user is part of privileged groups, keeping your admin role intact.

### Update printer driver

`scripts/Update-PrinterDriver.ps1` downloads a printer driver package, stages it into the Windows
driver store, and rebinds every local print queue that is still on the old driver — without
recreating the queues. Built to be pushed fleet-wide from Datto RMM, where most devices have the
printer and some do not.

#### Purpose & behavior
- Reads **all** of its inputs from environment variables (Datto RMM component variables). It takes
  no CLI parameters, so one component definition covers every client and site.
- Finds queues by driver-name substring (`DriverMatchString`), not by queue name, because queue
  names vary per client. Matching is case-insensitive.
- Checks for matching queues **before** downloading anything, so devices without the printer skip
  the transfer entirely instead of pulling a driver package they will never use.
- Downloads the package, verifies it is non-empty, logs its size and SHA256 (and compares it to
  `ExpectedSha256` when set), then extracts a `.zip`, runs a vendor `.exe`/`.msi` with `SilentArgs`,
  or uses a bare `.inf` directly.
- Picks the INF automatically — preferring one whose contents name `DriverName` — or uses `InfPath`
  when the package ships several. Stages it with `pnputil /add-driver /install` and registers it
  with `Add-PrinterDriver`.
- Rebinds each matching queue with `Set-Printer -DriverName`, then re-queries `Get-Printer` and
  logs pass/fail per queue.
- Cleans up the temp working folder and exits with a code Datto RMM can alert on.

#### Environment variables

| Variable | Description | Required |
|---|---|---|
| `DriverDownloadUrl` | Direct HTTPS link to the driver package (`.zip`, `.exe`, `.msi` or `.inf`). | Yes |
| `DriverMatchString` | Substring matched against the driver name of existing queues, e.g. `Brother HL-L2350`. | Yes |
| `DriverName` | Exact driver name to bind queues to, as published by the new driver's INF, e.g. `Brother HL-L2350D series`. | Yes |
| `InfPath` | Path to the `.inf` inside the extracted package, relative to the extraction root. Set this when a package ships multiple INFs. | No |
| `SilentArgs` | Silent install or extract switches for a vendor `.exe`/`.msi`, e.g. `/S` or `/qn /norestart`. | No |
| `LogPath` | Log file location. Defaults to `ProgramData\CentraStage\Update-PrinterDriver.log` when the Datto agent folder exists, otherwise `ProgramData`. | No |
| `ExpectedSha256` | SHA256 of the download. A mismatch aborts before anything is installed. | No |
| `TreatNoMatchAsError` | `true` to exit non-zero when no queue matches. Defaults to `false` — the fleet-safe setting. | No |
| `DryRun` | `true` to log every decision without installing the driver or touching a queue. | No |
| `KeepWorkingFiles` | `true` to leave the temp download/extract folder behind for troubleshooting. | No |

#### Exit codes

| Code | Meaning |
|---|---|
| 0 | All matched queues are on the new driver, or there was nothing to do |
| 1 | Unexpected error |
| 2 | Configuration error (missing required variable, not elevated) |
| 3 | Download failed or failed verification (bad URL, 404, zero bytes, hash mismatch) |
| 4 | Extract/stage failed (no usable INF, vendor installer failed) |
| 5 | Driver install failed (`pnputil` / `Add-PrinterDriver`) |
| 6 | No matching queues found, and `TreatNoMatchAsError` was `true` |
| 7 | One or more queues failed to rebind |
| 8 | Rebind reported success but verification did not confirm the new driver |

**A device with no matching printer exits 0 by default.** That is the expected outcome on much of a
fleet and must not raise an RMM alert.

#### Datto RMM component setup

1. Create a **Script** component, category *Scripts*, target **Windows**, and set the script type to
   **PowerShell**. Paste in `Update-PrinterDriver.ps1` (or attach it and call it from the command
   line `powershell.exe -ExecutionPolicy Bypass -File .\Update-PrinterDriver.ps1`).
2. Add the component variables under **Variables**, matching the names in the table above exactly —
   the script reads them with `$env:`, so the variable name *is* the contract:
   - `DriverDownloadUrl` — Value (String), required
   - `DriverMatchString` — Value (String), required
   - `DriverName` — Value (String), required
   - `InfPath`, `SilentArgs`, `LogPath`, `ExpectedSha256` — Value (String), optional
   - `TreatNoMatchAsError`, `DryRun`, `KeepWorkingFiles` — Boolean (or String `true`/`false`), optional
3. Components run as SYSTEM, which satisfies the elevation requirement.
4. Fill the variable values in at the **job** level, so one component can serve several printer
   models by scheduling a job per model.
5. Schedule the job against a site or device group. Re-running is safe: queues already on the target
   driver are logged as current and left alone.

Local test outside Datto RMM:

```powershell
$env:DriverDownloadUrl = "https://example.vendor.com/hll2350dw-driver.zip"
$env:DriverMatchString = "Brother HL-L2350"
$env:DriverName        = "Brother HL-L2350D series"
$env:DryRun            = "true"   # drop this once the values are confirmed

.\scripts\Update-PrinterDriver.ps1
```

#### Logging & monitoring

Every step writes one timestamped line to both stdout and the log file, tagged with a stage:
`INIT`, `DISCOVER`, `DOWNLOAD`, `STAGE`, `INSTALL`, `REBIND`, `VERIFY`, `CLEANUP`, `RESULT`.

```
2026-02-11T09:14:22.108Z INFO  [DISCOVER] Matched queue 'Front Desk' driver='Brother HL-L2350 Series' type=Local port='USB001'
2026-02-11T09:14:24.501Z INFO  [DOWNLOAD] Downloaded hll2350dw.zip bytes=24117248 sha256=368DC888...
2026-02-11T09:14:31.887Z INFO  [REBIND] OK queue 'Front Desk' rebound from 'Brother HL-L2350 Series' to 'Brother HL-L2350D series'
2026-02-11T09:14:32.004Z INFO  [VERIFY] PASS queue 'Front Desk' reports driver 'Brother HL-L2350D series'
2026-02-11T09:14:32.011Z INFO  [RESULT] PRINTERDRIVERUPDATE_RESULT STATUS=SUCCESS EXIT=0 MATCHED=1 UPDATED=1 CURRENT=0 SKIPPED=0 FAILED=0 REBOOTREQUIRED=false MESSAGE="Updated 1 queue(s) to 'Brother HL-L2350D series'"
```

The run ends with a single machine-readable summary line, repeated inside Datto's
`<-Start Result->` / `<-End Result->` markers so a monitor can key off it directly. Useful greps:

- `PRINTERDRIVERUPDATE_RESULT` — the one-line outcome of a run
- `STATUS=SUCCESS` / `STATUS=NOTHINGTODO` / `STATUS=ALREADYCURRENT` — non-alerting outcomes
- `[REBIND] FAIL` — the specific queues that could not be rebound
- `[VERIFY] FAIL` — queues that accepted the rebind but did not report the new driver

The log is capped at 5 MB and rolled to `<LogPath>.1`.

#### Notes / limitations
- `DriverName` must match what the new INF publishes, character for character. If it does not, the
  script fails at `INSTALL` with exit 5 rather than leaving queues half-migrated. Confirm it on a
  test device with `Get-PrinterDriver | Select-Object Name`.
- Queues that are **connections to a print server** are logged and skipped, not failed — their
  driver is controlled by the server, so the endpoint cannot rebind them. They appear as `SKIPPED`
  in the result line.
- A vendor `.exe` needs `SilentArgs`. The script will try to expand an `.exe` as a zip-based
  self-extractor when `SilentArgs` is empty, but will fail cleanly if it is not one.
- `pnputil` exit 3010 and installer exit 3010 are treated as success; the result line reports
  `REBOOTREQUIRED=true` so a job can follow up with a reboot.
- Devices with no print subsystem (`Get-Printer` unavailable) exit 0 as "nothing to do".

## What it does

- Looks up an Entra device by `displayName` (`-DeviceName`)
- Or enumerates all devices with LAPS (`-AllDevices`) for bulk sync
- Calls the Graph **beta** `deviceLocalCredentials` endpoint to retrieve LAPS credentials
- Selects the credential matching `-LocalAdminAccountName` (falls back to the first credential returned)
- Creates or updates an IT Glue Password record (matched by name) with:
  - `username`: the LAPS account name
  - `password`: the decoded LAPS password
  - `notes`: optional notes + Graph timestamps (backup/expiration) when available

## Requirements

### Microsoft Graph

- PowerShell 5.1+ or PowerShell 7+
- Microsoft Graph PowerShell SDK modules:
  - `Microsoft.Graph.Authentication`
  - `Microsoft.Graph.Devices`

Install (CurrentUser):

```powershell
Install-Module Microsoft.Graph -Scope CurrentUser
```

Permissions (scopes requested by the script):

- `DeviceLocalCredential.Read.All`
- `Device.Read.All`
- `DeviceManagementManagedDevices.Read.All` (only when auto-associating an IT Glue Configuration by serial number via Intune)

You (or an admin) may need to grant consent depending on your tenant policies.

### IT Glue

- IT Glue API key with write access to Passwords
- Your IT Glue Organization ID (`-ITGlueOrganizationId`)

## Usage

Basic sync (creates the IT Glue Password record if it doesn’t exist):

```powershell
.\scripts\Sync-EntraLapsToITGlue.ps1 `
  -DeviceName "PC-001" `
  -ITGlueOrganizationId 123456
```

Bulk sync all devices with LAPS enabled:

```powershell
.\scripts\Sync-EntraLapsToITGlue.ps1 `
  -AllDevices `
  -ITGlueOrganizationId 123456
```

If running from Datto RMM as a component, set an environment/component variable like `ITGLUE_API_KEY`
and omit `-ITGlueApiKey`.

Create/update a local admin on the endpoint and sync it to IT Glue (Datto RMM component-friendly):

```powershell
$env:LOCAL_ADMIN_PASSWORD = "<set via Datto RMM component variable>"
$env:ITGLUE_API_KEY = "<set via Datto RMM component variable>"

.\scripts\Set-LocalAdminAndSyncToITGlue.ps1 -LocalAdminUsername "RMMAdmin"
```

Preview changes without writing to IT Glue:

```powershell
.\scripts\Sync-EntraLapsToITGlue.ps1 `
  -DeviceName "PC-001" `
  -ITGlueOrganizationId 123456 `
  -WhatIf
```

Use a specific local admin account name and a custom IT Glue Password record name:

```powershell
.\scripts\Sync-EntraLapsToITGlue.ps1 `
  -DeviceName "PC-001" `
  -LocalAdminAccountName "LAPSAdmin" `
  -ITGlueApiKey (Get-Content .\itglue.key -Raw) `
  -ITGlueOrganizationId 123456 `
  -ITGluePasswordName "PC-001 / LAPS"
```

## Parameters

- `-DeviceName`: Entra device `displayName` (must be unique; required unless using `-AllDevices`).
- `-AllDevices`: Enumerate and sync all devices with LAPS credentials (bulk mode).
- `-LocalAdminAccountName`: Which credential to select (default: `Administrator`).
- `-ITGlueApiKey`: IT Glue API key (sent in the `x-api-key` header); if omitted, uses `ITGLUE_API_KEY` env var.
- `-ITGlueOrganizationId` (required): IT Glue organization that owns the Password record.
- `-ITGluePasswordName`: Password record name to upsert (single-device mode only; overrides template).
- `-ITGluePasswordNameTemplate`: Per-device password name template (default: `"{DeviceName} - LAPS"`).
- `-ITGlueBaseUri`: IT Glue API base URL (default: `https://api.itglue.com`).
- `-ITGluePasswordCategoryId`: Optional password category ID to set on create/update.
- `-ITGlueResourceType` / `-ITGlueResourceId`: Optional association (commonly `Configurations` + configuration ID).
- `-ConfigurationSerialNumber`: Optional serial number override used to find an IT Glue Configuration item.
- `-DisableConfigurationLookup`: Skip auto-association to an IT Glue Configuration item by serial number when `-ITGlueResourceId` is not provided.
- `-RequireConfigurationMatch`: Fail if the script cannot uniquely match an IT Glue Configuration by serial number.
- `-ITGlueNotes`: Optional additional notes; Graph timestamps are appended when available.
- `-TenantId`: Optional tenant ID to use when connecting to Graph.

## Output

The script returns an object including:

- `DeviceName`, `DeviceId`, `AccountName`, `PasswordExpirationDateTime`
- `ITGlueOrganizationId`, `ITGluePasswordName`, `ITGluePasswordId`
- `ITGlueResourceType`, `ITGlueResourceId` (resolved association, when available)

## Notes / limitations

- Uses a Microsoft Graph **beta** endpoint (`/beta/deviceLocalCredentials/...`), which may change.
- The device lookup is by exact `displayName`. If multiple devices share the same name, the script stops and asks for a unique name.
- IT Glue upsert is done by searching Passwords by **name** within the given organization (first match is used).
- If `-ITGlueResourceId` is not provided, the script attempts to associate the Password to an IT Glue **Configuration** by looking up the device serial number in Graph and matching `filter[serial_number]` in IT Glue.

## Local admin sync notes

- `scripts/Set-LocalAdminAndSyncToITGlue.ps1` resolves the endpoint serial number from `Win32_BIOS.SerialNumber` and finds the IT Glue Configuration via `filter[serial_number]`.
- The local admin password should be provided via a Datto RMM variable mapped to `LOCAL_ADMIN_PASSWORD` (or passed as `-LocalAdminPassword`).
- The IT Glue Password record is upserted; it prefers matching by name, and may also update an existing record associated with the same Configuration+username when supported by IT Glue filters.

## Security recommendations

- Do not hardcode API keys in scripts. Prefer environment variables or a secret manager.
- Consider restricting who can run this, since it retrieves and stores privileged local admin credentials.
- Use `-WhatIf` when validating behavior in a new tenant or IT Glue org.
