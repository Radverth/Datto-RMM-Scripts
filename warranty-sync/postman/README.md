# Warranty Sync – Postman setup lookups

This Postman collection finds the values the **Warranty Sync** solution asks for. It also checks the three things
the project plan says to confirm before building.

| File | What it is |
|---|---|
| `WarrantySync.postman_collection.json` | The requests, grouped by system. Each request has tests that save the result for the next request. |
| `WarrantySync.postman_environment.json` | Your keys and the values the requests find. |

Folders 1–3 only read data. Folder 4 **changes data** and is optional.

## 1. Import into Postman

1. Postman → **Import** → drag in both JSON files.
2. In the top-right environment picker, choose **Warranty Sync**.
3. Open **Environments → Warranty Sync** and fill in the rows below. The rows marked *secret* are masked on screen.
   Postman only stores them locally unless you share the environment.

| Variable | What to enter | Where to get it |
|---|---|---|
| `itglue_host` | `https://api.eu.itglue.com` (EU), `https://api.itglue.com` (US) or `https://api.au.itglue.com` (AU) | Use the same region as your IT Glue login URL |
| `itglue_api_key` *(secret)* | IT Glue API key | IT Glue → Account → Settings → API Keys |
| `datto_api_url` | e.g. `https://merlot-api.centrastage.net` | Datto RMM → Setup → Global Settings → Access Control → API URL |
| `datto_api_key` *(secret)* | API key | Datto RMM → Setup → Users → your API user → API keys |
| `datto_api_secret` *(secret)* | API secret | Shown with the key, only when it is generated |
| `autotask_username` | API user's username (an email address) | Autotask → Admin → Resources/Users → API user |
| `autotask_secret` *(secret)* | API user's secret | Same place |
| `autotask_integration_code` *(secret)* | Tracking identifier | Same place, under *API Tracking Identifier* |
| `test_warranty_date` | e.g. `2027-06-30` | Only needed for folder 4 |

The requests fill in all the other rows themselves (`laptop_type_id`, `rmm_id`, `datto_token` and so on).

## 2. Run it

Run the requests **in order**: later requests use values saved by earlier ones. You can run them one by one, or right-click
**folder 1**, **2** or **3** → **Run folder** to run a whole folder in Collection Runner. Don't run folder 4 this way.

After each request, check:
- **Test Results**: shows each value found, for example `Laptop type found: Laptop = 1234`.
- **Visualize**: request 1.1, 1.2 and 1.3 results show as tables.
- **Console** (bottom bar): shows the full lists.

### Requests and URLs

| # | Request | URL | Finds / proves |
|---|---|---|---|
| 1.1 | Configuration types | `GET {itglue_host}/configuration_types?page[size]=1000` | `laptop_type_id`, `desktop_type_id` |
| 1.2 | Configuration statuses | `GET {itglue_host}/configuration_statuses?page[size]=1000` | `active_status_id` |
| 1.3 | Sample laptops | `GET {itglue_host}/configurations?filter[configuration_type_id]={laptop_type_id}&filter[configuration_status_id]={active_status_id}&filter[archived]=false&page[size]=25` | Lists the date/warranty attributes so you can pick the myITProcess one, shows whether `rmm-id` is a GUID, and saves one device's `configuration_id`, `rmm_id` and `psa_id` |
| 1.4 | One configuration | `GET {itglue_host}/configurations/{configuration_id}` | Full attribute list for one device |
| 2.1 | Datto token (standard) | `POST {datto_api_url}/auth/oauth/token` (Basic `public-client:public`, form body) | `datto_token` |
| 2.2 | Datto token (connector method) | `POST {datto_api_url}/auth/oauth/token?grant_type=password&username=…&password=…` | **Whether the custom connector's token method works** |
| 2.3 | Datto account | `GET {datto_api_url}/api/v2/account` | Whether the URL is right, and the platform name for the connector |
| 2.4 | Device by UID | `GET {datto_api_url}/api/v2/device/{rmm_id}` | Whether `rmm-id` is the Datto device UID |
| 2.5 | Device by numeric ID | `GET {datto_api_url}/api/v2/device/id/{rmm_id}` | Run only if 2.4 returns 404 |
| 3.1 | Autotask zone | `GET https://webservices.autotask.net/atservicesrest/v1.0/zoneInformation?user={autotask_username}` | Your zone; sets `autotask_base` |
| 3.2 | Configuration item | `GET {autotask_base}/ConfigurationItems/{psa_id}` | Whether `psa-id` is the Autotask configuration item ID; shows the current `warrantyExpirationDate` |
| 4.1 | ⚠ IT Glue write | `PATCH {itglue_host}/configurations/{configuration_id}` | Plan step 1: whether the date survives the next Autotask → IT Glue sync |
| 4.2 | ⚠ Datto write | `POST {datto_api_url}/api/v2/device/{rmm_id}/warranty` | Whether the warranty write works |
| 4.3 | ⚠ Autotask write | `PATCH {autotask_base}/ConfigurationItems` | Whether the warranty write works |

## 3. Copy the results into the solution

Go to Power Apps → **Solutions → Warranty Sync → Environment variables** and enter:

| Environment variable | Value from |
|---|---|
| IT Glue Laptop type ID | `laptop_type_id` (1.1) |
| IT Glue Desktop type ID | `desktop_type_id` (1.1) |
| IT Glue Active status ID | `active_status_id` (1.2) |
| myITProcess attribute name | The attribute in 1.3's **Visualize** table that holds the myITProcess date, typed exactly as shown |

Then enter these when the solution asks for connections:

| Connection | Field | Value |
|---|---|---|
| IT Glue (Warranty Sync) | API host | `itglue_host` without `https://`, e.g. `api.eu.itglue.com` |
| Datto RMM (Warranty Sync) | Platform | The name 2.3 reports, e.g. `merlot` |
| Autotask (Warranty Sync) | — | Same username, secret and integration code as in Postman |

## 4. What the checks mean

| Check | Pass | If it fails |
|---|---|---|
| 1.3 rmm-id format | Most devices show a GUID | rmm-id is numeric: the flow needs an extra lookup step (confirm with 2.5) |
| 2.2 connector token | 200 with an `access_token` | Datto doesn't accept query parameters, so the connector's token step must change |
| 2.4 device by UID | 200 | See 2.5 |
| 3.1 zone | Contains `webservices4` | The Autotask connector's host needs changing to your zone |
| 3.2 configuration item | 200 | psa-id isn't an Autotask configuration item ID |
| 4.1 then 1.4 after the next PSA sync | The date is unchanged | Autotask is overwriting the field (the field-lock risk in the plan) |

Send any failures to Claude with the request's response body, and it can adjust the solution.

## Regenerating

The two JSON files are generated by `../build/make_postman.py`:

```bash
python3 warranty-sync/build/make_postman.py warranty-sync/postman
```
