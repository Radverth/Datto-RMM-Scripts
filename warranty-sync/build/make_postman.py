#!/usr/bin/env python3
"""Generate the Postman collection + environment for the warranty sync setup lookups.
Run: python3 warranty-sync/build/make_postman.py warranty-sync/postman"""
import json, os, sys
OUT = sys.argv[1]

def js(src): return [l for l in src.strip("\n").split("\n")]
def test(src): return [{"listen": "test", "script": {"type": "text/javascript", "exec": js(src)}}]

def url(raw):
    # Postman parses raw itself on import; keep raw + host/path minimal
    base, _, q = raw.partition("?")
    u = {"raw": raw, "host": [base.split("/")[0]], "path": [p for p in base.split("/")[1:] if p]}
    if q:
        u["query"] = [{"key": kv.split("=", 1)[0], "value": kv.split("=", 1)[1] if "=" in kv else ""} for kv in q.split("&")]
    return u

def req(name, method, raw, desc, tests=None, headers=None, body=None, auth=None):
    r = {"method": method, "header": headers or [], "url": url(raw), "description": desc}
    if body: r["body"] = body
    if auth: r["auth"] = auth
    item = {"name": name, "request": r}
    if tests: item["event"] = test(tests)
    return item

itglue_auth = {"type": "apikey", "apikey": [{"key": "key", "value": "x-api-key", "type": "string"},
               {"key": "value", "value": "{{itglue_api_key}}", "type": "string"}, {"key": "in", "value": "header", "type": "string"}]}
datto_auth = {"type": "bearer", "bearer": [{"key": "token", "value": "{{datto_token}}", "type": "string"}]}
at_headers = [{"key": "ApiIntegrationcode", "value": "{{autotask_integration_code}}"},
              {"key": "UserName", "value": "{{autotask_username}}"},
              {"key": "Secret", "value": "{{autotask_secret}}"},
              {"key": "Content-Type", "value": "application/json"}]
jsonapi = [{"key": "Accept", "value": "application/vnd.api+json"}]

itglue = {"name": "1. IT Glue", "auth": itglue_auth, "description":
  "Finds the IDs for the Laptop and Desktop types and the Active status, then shows a sample device so you can read the "
  "myITProcess attribute name and the rmm-id / psa-id formats. Read-only.", "item": [
  req("1.1 Configuration types (Laptop / Desktop IDs)", "GET", "{{itglue_host}}/configuration_types?page[size]=1000",
      "Sets `laptop_type_id` and `desktop_type_id` -> environment variables **IT Glue Laptop type ID** and **IT Glue Desktop type ID**.",
      r'''
pm.test("200 OK", () => pm.response.to.have.status(200));
const rows = pm.response.json().data.map(t => ({ id: t.id, name: t.attributes.name }));
console.table(rows);
const find = re => rows.find(r => re.test(r.name));
const laptop = find(/^laptops?$/i) || find(/laptop/i);
const desktop = find(/^desktops?$/i) || find(/desktop/i);
if (laptop) pm.environment.set("laptop_type_id", laptop.id);
if (desktop) pm.environment.set("desktop_type_id", desktop.id);
pm.test("Laptop type found: " + (laptop ? laptop.name + " = " + laptop.id : "NONE - pick from the console"), () => pm.expect(laptop).to.exist);
pm.test("Desktop type found: " + (desktop ? desktop.name + " = " + desktop.id : "NONE - pick from the console"), () => pm.expect(desktop).to.exist);
pm.visualizer.set(`<table border=1 cellpadding=4><tr><th>ID</th><th>Name</th></tr>{{#each rows}}<tr><td>{{id}}</td><td>{{name}}</td></tr>{{/each}}</table>`, { rows });
''', jsonapi),
  req("1.2 Configuration statuses (Active ID)", "GET", "{{itglue_host}}/configuration_statuses?page[size]=1000",
      "Sets `active_status_id` -> environment variable **IT Glue Active status ID**.",
      r'''
pm.test("200 OK", () => pm.response.to.have.status(200));
const rows = pm.response.json().data.map(t => ({ id: t.id, name: t.attributes.name }));
console.table(rows);
const active = rows.find(r => /^active$/i.test(r.name)) || rows.find(r => /active/i.test(r.name));
if (active) pm.environment.set("active_status_id", active.id);
pm.test("Active status found: " + (active ? active.name + " = " + active.id : "NONE - pick from the console"), () => pm.expect(active).to.exist);
pm.visualizer.set(`<table border=1 cellpadding=4><tr><th>ID</th><th>Name</th></tr>{{#each rows}}<tr><td>{{id}}</td><td>{{name}}</td></tr>{{/each}}</table>`, { rows });
''', jsonapi),
  req("1.3 Sample laptops (myITProcess attribute, rmm-id, psa-id)", "GET",
      "{{itglue_host}}/configurations?filter[configuration_type_id]={{laptop_type_id}}&filter[configuration_status_id]={{active_status_id}}&filter[archived]=false&page[size]=25",
      "Lists 25 active laptops and shows every **date / warranty** attribute with sample values, so you can pick the exact "
      "myITProcess attribute name -> environment variable **myITProcess attribute name**. Also reports whether `rmm-id` "
      "looks like a Datto device UID (GUID) or a number, and saves the first device's IDs for the next requests.",
      r'''
pm.test("200 OK", () => pm.response.to.have.status(200));
const data = pm.response.json().data;
pm.test("Devices returned: " + data.length, () => pm.expect(data.length).to.be.above(0));
const guid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const dateLike = v => typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v);
// every attribute that is warranty/date-ish, with how many devices have a value and a sample
const stats = {};
data.forEach(d => Object.entries(d.attributes).forEach(([k, v]) => {
  if (/warrant|expir|myit|purchase|install/i.test(k) || dateLike(v)) {
    stats[k] = stats[k] || { attribute: k, filled: 0, sample: "" };
    if (v !== null && v !== "") { stats[k].filled++; if (!stats[k].sample) stats[k].sample = String(v); }
  }
}));
const attrs = Object.values(stats).sort((a, b) => b.filled - a.filled);
console.table(attrs);
const rows = data.map(d => ({ id: d.id, org: d.attributes["organization-name"], name: d.attributes.name,
  serial: d.attributes["serial-number"], rmm: d.attributes["rmm-id"], psa: d.attributes["psa-id"],
  rmmFormat: d.attributes["rmm-id"] == null ? "missing" : guid.test(String(d.attributes["rmm-id"])) ? "GUID (device UID)" : "number / other",
  glue: d.attributes["warranty-expires-at"] }));
console.table(rows);
const first = data.find(d => d.attributes["rmm-id"] && d.attributes["psa-id"]) || data[0];
if (first) {
  pm.environment.set("configuration_id", first.id);
  pm.environment.set("rmm_id", first.attributes["rmm-id"] || "");
  pm.environment.set("psa_id", first.attributes["psa-id"] || "");
}
const rmmGuid = rows.filter(r => r.rmmFormat.startsWith("GUID")).length;
pm.test(`rmm-id is a GUID on ${rmmGuid}/${rows.length} devices (GUID = Datto device UID, no extra lookup needed)`, () => true);
pm.visualizer.set(`
<h3>Date / warranty attributes (pick the myITProcess one)</h3>
<table border=1 cellpadding=4><tr><th>Attribute</th><th>Devices with a value</th><th>Sample</th></tr>
{{#each attrs}}<tr><td><b>{{attribute}}</b></td><td>{{filled}}</td><td>{{sample}}</td></tr>{{/each}}</table>
<h3>Devices</h3>
<table border=1 cellpadding=4><tr><th>ID</th><th>Org</th><th>Name</th><th>Serial</th><th>rmm-id</th><th>Format</th><th>psa-id</th><th>IT Glue warranty</th></tr>
{{#each rows}}<tr><td>{{id}}</td><td>{{org}}</td><td>{{name}}</td><td>{{serial}}</td><td>{{rmm}}</td><td>{{rmmFormat}}</td><td>{{psa}}</td><td>{{glue}}</td></tr>{{/each}}</table>`,
  { attrs, rows });
''', jsonapi),
  req("1.4 One configuration (full attributes)", "GET", "{{itglue_host}}/configurations/{{configuration_id}}",
      "Full record for `configuration_id` (set by 1.3, or type your own). Use it to double-check the myITProcess attribute name.",
      r'''
pm.test("200 OK", () => pm.response.to.have.status(200));
const a = pm.response.json().data.attributes;
console.log(JSON.stringify(a, null, 2));
''', jsonapi),
]}

datto = {"name": "2. Datto RMM", "auth": datto_auth, "description":
  "Gets a token, confirms the connector's token method works, and checks that IT Glue's rmm-id is a Datto device UID. Read-only.", "item": [
  req("2.1 Get token (form body - Datto standard)", "POST", "{{datto_api_url}}/auth/oauth/token",
      "Standard Datto RMM token request. Sets `datto_token` for the other requests.",
      r'''
pm.test("200 OK", () => pm.response.to.have.status(200));
const t = pm.response.json().access_token;
pm.test("access_token returned", () => pm.expect(t).to.be.a("string"));
pm.environment.set("datto_token", t);
''', body={"mode": "urlencoded", "urlencoded": [
          {"key": "grant_type", "value": "password"}, {"key": "username", "value": "{{datto_api_key}}"},
          {"key": "password", "value": "{{datto_api_secret}}"}]},
      auth={"type": "basic", "basic": [{"key": "username", "value": "public-client", "type": "string"},
                                       {"key": "password", "value": "public", "type": "string"}]}),
  req("2.2 Get token (query parameters - as the custom connector does)", "POST",
      "{{datto_api_url}}/auth/oauth/token?grant_type=password&username={{datto_api_key}}&password={{datto_api_secret}}",
      "**Important.** The Datto RMM (Warranty Sync) connector sends the key and secret as query parameters, because a custom "
      "connector can't build a form body from its connection. If this request returns a token, the connector will work. "
      "If 2.1 works but this fails, tell Claude so the connector can be changed.",
      r'''
pm.test("Connector token method works (200 + access_token)", () => {
  pm.response.to.have.status(200);
  pm.expect(pm.response.json().access_token).to.be.a("string");
});
''', headers=[{"key": "Content-Type", "value": "application/x-www-form-urlencoded"}],
      auth={"type": "basic", "basic": [{"key": "username", "value": "public-client", "type": "string"},
                                       {"key": "password", "value": "public", "type": "string"}]}),
  req("2.3 Account (confirms platform URL)", "GET", "{{datto_api_url}}/api/v2/account",
      "If this works, `datto_api_url` is right. The platform name for the connector connection is the part before `-api`, "
      "e.g. `https://merlot-api.centrastage.net` -> `merlot`.",
      r'''
pm.test("200 OK", () => pm.response.to.have.status(200));
const host = pm.environment.replaceIn("{{datto_api_url}}").match(/https?:\/\/([^.-]+)-api/);
pm.test("Connector platform value: " + (host ? host[1] : "?"), () => pm.expect(host).to.not.be.null);
'''),
  req("2.4 Device by UID (is rmm-id a device UID?)", "GET", "{{datto_api_url}}/api/v2/device/{{rmm_id}}",
      "Uses `rmm_id` from 1.3. **200** means rmm-id is the Datto device UID, so the flow can write warranties directly. "
      "**404** means it isn't; run 2.5.",
      r'''
pm.test("rmm-id is a Datto device UID (200)", () => pm.response.to.have.status(200));
if (pm.response.code === 200) {
  const d = pm.response.json();
  console.log({ hostname: d.hostname, uid: d.uid, id: d.id, warrantyDate: d.warrantyDate, site: d.siteName });
}
'''),
  req("2.5 Device by numeric ID (only if 2.4 fails)", "GET", "{{datto_api_url}}/api/v2/device/id/{{rmm_id}}",
      "If rmm-id is numeric, this finds the device and shows its `uid`. In that case the flow needs an extra lookup step; tell Claude.",
      r'''
if (pm.response.code === 200) console.log("Numeric ID resolves to uid:", pm.response.json().uid);
pm.test("Status " + pm.response.code, () => true);
'''),
  req("2.6 Request rate (API limit headroom)", "GET", "{{datto_api_url}}/api/v2/system/request_rate",
      "Shows how much of Datto RMM's API request limit your account is using. Check it before the first big backfill: "
      "the Datto RMM Only flow makes up to two calls per device (read, then write if different). If usage is already "
      "high, lower **Max writes per run**.",
      r'''
pm.test("200 OK", () => pm.response.to.have.status(200));
const r = pm.response.json();
console.log(r);
const rows = Object.entries(r).map(([k, v]) => ({ field: k, value: typeof v === "object" ? JSON.stringify(v) : String(v) }));
pm.visualizer.set(`<table border=1 cellpadding=4><tr><th>Field</th><th>Value</th></tr>{{#each rows}}<tr><td>{{field}}</td><td>{{value}}</td></tr>{{/each}}</table>`, { rows });
'''),
]}

autotask = {"name": "3. Autotask", "description":
  "Finds your Autotask zone (the connector is built for webservices4) and checks a configuration item's warranty field. Read-only.", "item": [
  req("3.1 Zone information", "GET",
      "https://webservices.autotask.net/atservicesrest/v1.0/zoneInformation?user={{autotask_username}}",
      "Returns your zone URL and sets `autotask_base`. The Autotask (Warranty Sync) connector is built for **webservices4**; "
      "if yours differs, tell Claude.", r'''
pm.test("200 OK", () => pm.response.to.have.status(200));
const z = pm.response.json();
const base = z.url.replace(/\/+$/, "") + "/v1.0";
pm.environment.set("autotask_base", base);
pm.test("Zone: " + z.url, () => true);
pm.test("Matches connector zone webservices4", () => pm.expect(z.url.toLowerCase()).to.include("webservices4"));
'''),
  req("3.2 Configuration item (psa-id)", "GET", "{{autotask_base}}/ConfigurationItems/{{psa_id}}",
      "Uses `psa_id` from 1.3. Confirms IT Glue's psa-id is the Autotask configuration item ID and shows the current "
      "`warrantyExpirationDate`.", r'''
pm.test("psa-id is an Autotask configuration item (200)", () => pm.response.to.have.status(200));
if (pm.response.code === 200) {
  const i = pm.response.json().item;
  console.log({ id: i.id, referenceTitle: i.referenceTitle, serialNumber: i.serialNumber, warrantyExpirationDate: i.warrantyExpirationDate });
}
''', headers=at_headers),
]}

writes = {"name": "4. Optional write tests (CHANGES DATA)", "description":
  "Plan step 1: write a warranty date to ONE test device and check it sticks. These requests change data. "
  "Set `test_warranty_date` (yyyy-MM-dd) first.", "item": [
  req("4.1 IT Glue - set warranty on configuration_id", "PATCH", "{{itglue_host}}/configurations/{{configuration_id}}",
      "After this, wait for the next Autotask -> IT Glue sync and re-run 1.4. If the date has been overwritten or the "
      "request is refused, the Autotask field lock risk in the plan applies.",
      r'''pm.test("200 OK", () => pm.response.to.have.status(200));''',
      headers=[{"key": "Content-Type", "value": "application/vnd.api+json"}] + jsonapi,
      body={"mode": "raw", "raw": json.dumps({"data": {"type": "configurations", "attributes": {"warranty-expires-at": "{{test_warranty_date}}"}}}, indent=2)},
      auth=itglue_auth),
  req("4.2 Datto RMM - set warranty on rmm_id", "POST", "{{datto_api_url}}/api/v2/device/{{rmm_id}}/warranty",
      "Then re-run 2.4 and check `warrantyDate`.", r'''pm.test("2xx", () => pm.expect(pm.response.code).to.be.within(200, 299));''',
      headers=[{"key": "Content-Type", "value": "application/json"}],
      body={"mode": "raw", "raw": json.dumps({"warrantyDate": "{{test_warranty_date}}"}, indent=2)}, auth=datto_auth),
  req("4.3 Autotask - set warranty on psa_id", "PATCH", "{{autotask_base}}/ConfigurationItems",
      "Then re-run 3.2 and check `warrantyExpirationDate`.", r'''pm.test("200 OK", () => pm.response.to.have.status(200));''',
      headers=at_headers, body={"mode": "raw", "raw": '{\n  "id": {{psa_id}},\n  "warrantyExpirationDate": "{{test_warranty_date}}"\n}'}),
]}

collection = {"info": {"name": "Warranty Sync - Setup Lookups", "_postman_id": "6f0f3d8e-2a51-4b7c-9e1d-58c4b2a7d903",
  "description": "Looks up the values the Warranty Sync Power Automate solution needs. See README.md in this folder.",
  "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
  "item": [itglue, datto, autotask, writes]}

env = {"id": "b8a1c2d3-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "name": "Warranty Sync", "_postman_variable_scope": "environment", "values": [
  {"key": k, "value": v, "type": t, "enabled": True} for k, v, t in [
    ("itglue_host", "https://api.eu.itglue.com", "default"),
    ("itglue_api_key", "", "secret"),
    ("datto_api_url", "https://merlot-api.centrastage.net", "default"),
    ("datto_api_key", "", "secret"),
    ("datto_api_secret", "", "secret"),
    ("autotask_username", "", "default"),
    ("autotask_secret", "", "secret"),
    ("autotask_integration_code", "", "secret"),
    ("test_warranty_date", "", "default"),
    ("laptop_type_id", "", "default"), ("desktop_type_id", "", "default"), ("active_status_id", "", "default"),
    ("configuration_id", "", "default"), ("rmm_id", "", "default"), ("psa_id", "", "default"),
    ("datto_token", "", "secret"), ("autotask_base", "https://webservices4.autotask.net/atservicesrest/v1.0", "default")]]}

with open(os.path.join(OUT, "WarrantySync.postman_collection.json"), "w") as f: json.dump(collection, f, indent=2)
with open(os.path.join(OUT, "WarrantySync.postman_environment.json"), "w") as f: json.dump(env, f, indent=2)
print("ok")
