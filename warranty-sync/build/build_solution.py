#!/usr/bin/env python3
"""Generate the importable Power Automate solution (WarrantySync_1_0_0_20.zip).

Run:  python3 warranty-sync/build/build_solution.py
Writes the unpacked source to warranty-sync/solution/ and the zip to warranty-sync/.
"""
import json, os, shutil, zipfile
from xml.sax.saxutils import escape, quoteattr

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.dirname(HERE)
SRC = os.path.join(OUT, "solution")
ZIP = os.path.join(OUT, "WarrantySync_1_0_0_20.zip")

PREFIX = "new"  # matches the environment default publisher (see PUBLISHER below)
WF_ID = "7b1d3a52-4c6e-4f0a-9a37-2d5e8c1f0b64"
FLOW_NAME = "Warranty Sync - Nightly"

# ---- environment variables: key -> (display, default, required, description) ------------------
ENV = {
 "DryRun": ("Dry run", "yes", False, "Yes = record what would change but write nothing. Set to No to go live."),
 "LaptopTypeId": ("IT Glue Laptop type ID", None, True, "GET /configuration_types"),
 "DesktopTypeId": ("IT Glue Desktop type ID", None, True, "GET /configuration_types"),
 "ActiveStatusId": ("IT Glue Active status ID", None, True, "GET /configuration_statuses"),
 "MyITPAttribute": ("myITProcess attribute name", None, True, "Exact IT Glue configuration attribute holding the myITProcess warranty date"),
 "MaxWritesPerRun": ("Max writes per run", "1500", False, "Cap on devices written per night"),
 "WriteAutotask": ("Write Autotask", "yes", False, "Yes = the nightly flow also writes Autotask."),
 # defaults point at the test list (affinitylab / warrantysynctest, list ID from its settings URL); the flow
 # designer validates SharePoint columns against these when the flow is saved
 "SharePointSiteUrl": ("SharePoint site URL", "https://affinitylab.sharepoint.com/sites/warrantysynctest", True,
                       "Site holding the Warranty Sync State list, e.g. https://<tenant>.sharepoint.com/sites/<site>"),
 "SharePointListName": ("SharePoint list name", "0b1cb3ad-1ff9-4454-ba75-df5f5fdc9522", False,
                        "List ID (GUID from the list settings URL) or exact list title"),
 "NotifyEmail": ("Summary email recipient", None, True, "Where the nightly summary is sent"),
 # Autotask sends these two as headers on every call. Connector policies can't read connection fields in this
 # tenant, so they come from here; the API secret itself stays in the Autotask connection.
 "AutotaskUsername": ("Autotask API username", None, True, "Username of the Autotask API user (an email address)"),
 "AutotaskIntegrationCode": ("Autotask integration code", None, True, "Tracking identifier of the Autotask API user"),
}
BOOL_ENV = {"DryRun", "WriteAutotask"}   # Yes/No (Boolean) environment variables

def env_param(k):
    disp, dflt = ENV[k][0], ENV[k][1]
    if k in BOOL_ENV:
        return {"defaultValue": dflt == "yes", "type": "Bool", "metadata": {"schemaName": "%s_%s" % (PREFIX, k)}}
    return {"defaultValue": dflt or "", "type": "String", "metadata": {"schemaName": "%s_%s" % (PREFIX, k)}}

def P(key):  # parameter expression fragment
    return "parameters('%s (%s_%s)')" % (ENV[key][0], PREFIX, key)

# ---- custom connectors: credentials live in the connection, never in the flow ---------------------
ENV_SUFFIX = "5f7e14506b88214820"   # api-name suffix used by custom connectors in this environment

def conn_param(name, disp, desc, secure, tab):
    return {name: {"type": "securestring" if secure else "string", "uiDefinition": {
        "displayName": disp, "description": desc, "tooltip": desc,
        "constraints": {"tabIndex": tab, "clearText": not secure, "required": "true"}}}}

def setheader(title, name, value, ops):
    return {"TemplateId": "setheader", "Title": title, "Parameters": {
        "x-ms-apimTemplateParameter.name": name, "x-ms-apimTemplateParameter.value": value,
        "x-ms-apimTemplateParameter.existsAction": "override", "x-ms-apimTemplate-policySection": "Request",
        "x-ms-apimTemplate-operationName": ops}}

def setquery(title, name, value, ops):
    return {"TemplateId": "setqueryparameter", "Title": title, "Parameters": {
        "x-ms-apimTemplateParameter.name": name, "x-ms-apimTemplateParameter.value": value,
        "x-ms-apimTemplateParameter.existsAction": "override", "x-ms-apimTemplate-operationName": ops}}

def hosturl(template, ops):
    return {"TemplateId": "dynamichosturl", "Title": "Set host URL", "Parameters": {
        "x-ms-apimTemplateParameter.urlTemplate": template, "x-ms-apimTemplate-operationName": ops}}

ANY = {"default": {"description": "default", "schema": {}}}

def swagger(title, desc, host, base, paths, security=None):
    d = {"swagger": "2.0", "info": {"title": title, "description": desc, "version": "1.0"}, "host": host,
         "basePath": base, "schemes": ["https"], "consumes": [], "produces": [], "paths": paths,
         "definitions": {}, "parameters": {}, "responses": {}, "tags": []}
    if security:
        d["securityDefinitions"] = {"api_key": security}; d["security"] = [{"api_key": []}]
    return d

# IT Glue region is fixed in the connector: EU instance (api.itglue.com = US, api.au.itglue.com = AU)
ITGLUE_HOST = "api.eu.itglue.com"
# Datto RMM platform is fixed in the connector: merlot
DATTO_HOST = "merlot-api.centrastage.net"

AT_HDRS = [{"name": "UserName", "in": "header", "required": True, "type": "string", "description": "Autotask API username"},
          {"name": "ApiIntegrationcode", "in": "header", "required": True, "type": "string", "description": "Autotask integration code"}]

CONNECTORS = {
 "itglue": {
  "name": "new_5Fwsitglue", "id": "3e9c6a10-52b4-4d1f-8a7e-0c2b9f4d6e81", "display": "ITGlueWarrantySync",
  "desc": "IT Glue configurations: list and update warranty date.", "color": "#1f5fa6",
  "swagger": swagger("ITGlueWarrantySync", "IT Glue configurations for the warranty sync", ITGLUE_HOST, "/", {
     "/configurations": {"get": {"responses": ANY, "summary": "List configurations", "operationId": "ListConfigurations",
        "description": "Active, non-archived configurations of one type. Inputs are sent as headers and copied into "
                       "IT Glue's filter[...] / page[...] query parameters by connector policies, because connector "
                       "parameter names must be alphanumeric.",
        "parameters": [
          {"name": "typeId", "in": "header", "required": True, "type": "string", "x-ms-summary": "Configuration type ID"},
          {"name": "statusId", "in": "header", "required": True, "type": "string", "x-ms-summary": "Configuration status ID"},
          {"name": "pageSize", "in": "header", "required": True, "type": "string", "default": "1000", "x-ms-summary": "Page size"},
          {"name": "pageNumber", "in": "header", "required": True, "type": "string", "default": "1", "x-ms-summary": "Page number"}]}},
     "/configurations/{id}": {"get": {"responses": ANY, "summary": "Get configuration", "operationId": "GetConfiguration",
        "parameters": [{"name": "id", "in": "path", "required": True, "type": "string", "description": "IT Glue configuration ID"}]},
        "patch": {"responses": ANY, "summary": "Update configuration", "operationId": "UpdateConfiguration",
        "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"},
          {"name": "body", "in": "body", "required": True, "schema": {"type": "object",
             "description": "JSON:API body, e.g. data.type = configurations, data.attributes.warranty-expires-at = yyyy-MM-dd"}}]}}},
     security={"type": "apiKey", "in": "header", "name": "x-api-key"}),
  "params": dict(**conn_param("api_key", "IT Glue API key", "IT Glue API key with read/write on Configurations", True, 2)),
  "policies": [setheader("Set accept", "Accept", "application/vnd.api+json", ["ListConfigurations", "GetConfiguration", "UpdateConfiguration"]),
               setheader("Set content type", "Content-Type", "application/vnd.api+json", ["UpdateConfiguration"]),
               setquery("Set type filter", "filter[configuration_type_id]", "@headers('typeId')", ["ListConfigurations"]),
               setquery("Set status filter", "filter[configuration_status_id]", "@headers('statusId')", ["ListConfigurations"]),
               setquery("Set archived filter", "filter[archived]", "false", ["ListConfigurations"]),
               setquery("Set page size", "page[size]", "@headers('pageSize')", ["ListConfigurations"]),
               setquery("Set page number", "page[number]", "@headers('pageNumber')", ["ListConfigurations"])],
 },
 "datto": {
  "name": "new_5Fwsdattormm", "id": "8a4f2d6b-1c3e-4b7a-9d05-6e8f1a2b3c47", "display": "DattoRMMWarrantySync",
  "desc": "Datto RMM API v2: get token and set device warranty date.", "color": "#0a6ebd",
  "swagger": swagger("DattoRMMWarrantySync", "Datto RMM API v2 for the warranty sync", DATTO_HOST, "/", {
     "/auth/oauth/token": {"post": {"summary": "Get token", "operationId": "GetToken",
        "description": "Uses the API key and secret stored in the connection.", "parameters": [],
        "responses": ANY}},
     "/api/v2/device/{deviceUid}": {"get": {"responses": ANY, "summary": "Get device", "operationId": "GetDevice",
        "parameters": [{"name": "deviceUid", "in": "path", "required": True, "type": "string", "description": "Datto device UID (IT Glue rmm-id)"},
          {"name": "Authorization", "in": "header", "required": True, "type": "string", "description": "Bearer <access_token>"}]}},
     "/api/v2/device/{deviceUid}/warranty": {"post": {"responses": ANY, "summary": "Set device warranty", "operationId": "SetDeviceWarranty",
        "parameters": [{"name": "deviceUid", "in": "path", "required": True, "type": "string", "description": "Datto device UID (IT Glue rmm-id)"},
          {"name": "Authorization", "in": "header", "required": True, "type": "string", "description": "Bearer <access_token>"},
          {"name": "body", "in": "body", "required": True, "schema": {"type": "object", "properties": {
             "warrantyDate": {"type": "string", "description": "yyyy-MM-dd"}}}}]}}}),
  "params": dict(**conn_param("apiKey", "Datto RMM API key", "API key from Setup > Users > API", True, 2),
                 **conn_param("apiSecret", "Datto RMM API secret", "API secret key", True, 3)),
  "policies": [setheader("Set token client", "Authorization", "Basic cHVibGljLWNsaWVudDpwdWJsaWM=", ["GetToken"]),
               setheader("Set token content type", "Content-Type", "application/x-www-form-urlencoded", ["GetToken"]),
               setquery("Set grant type", "grant_type", "password", ["GetToken"]),
               setquery("Set API key", "username", "@connectionParameters('apiKey')", ["GetToken"]),
               setquery("Set API secret", "password", "@connectionParameters('apiSecret')", ["GetToken"]),
               setheader("Set content type", "Content-Type", "application/json", ["SetDeviceWarranty"])],
 },
 "autotask": {
  "name": "new_5Fwsautotask", "id": "c51e7b38-9f2a-4e6d-b4c1-2d7a8e9f0b56", "display": "AutotaskWarrantySync",
  "desc": "Autotask REST API: update configuration item warranty date.", "color": "#e5622b",
  "swagger": swagger("AutotaskWarrantySync", "Autotask REST API for the warranty sync", "webservices4.autotask.net", "/atservicesrest/V1.0", {
     "/ConfigurationItems/{id}": {"get": {"responses": ANY, "summary": "Get configuration item", "operationId": "GetConfigurationItem",
        "parameters": [{"name": "id", "in": "path", "required": True, "type": "string", "description": "Autotask configuration item ID"}] + AT_HDRS}},
     "/ConfigurationItems/query": {"post": {"responses": ANY, "summary": "Query configuration items", "operationId": "QueryConfigurationItems",
        "description": "Autotask query model: MaxRecords, IncludeFields, Filter [{op, field, value}].",
        "parameters": [{"name": "body", "in": "body", "required": True, "schema": {"type": "object",
             "description": "e.g. Filter = [{op: eq, field: serialNumber, value: <serial>}]"}}] + AT_HDRS}},
     "/ConfigurationItems": {"patch": {"responses": ANY, "summary": "Update configuration item", "operationId": "UpdateConfigurationItem",
        "parameters": [{"name": "body", "in": "body", "required": True, "schema": {"type": "object", "properties": {
             "id": {"type": "integer", "format": "int64"}, "warrantyExpirationDate": {"type": "string", "description": "yyyy-MM-ddT00:00:00Z (midnight UTC)"}}}}] + AT_HDRS}}},
     security={"type": "apiKey", "in": "header", "name": "Secret"}),
  "params": dict(**conn_param("api_key", "Autotask API secret", "Secret of the Autotask API user", True, 2)),
  "policies": [setheader("Set content type", "Content-Type", "application/json", ["QueryConfigurationItems", "UpdateConfigurationItem"])],
 },
}
for c in CONNECTORS.values():
    c["api"] = "shared_" + c["name"].lower().replace("_", "-") + "-" + ENV_SUFFIX
    c["connref"] = PREFIX + "_shared" + c["name"][len(PREFIX) + 1:].lower() + ENV_SUFFIX + "_wsync"

def cc(key, op, params, secure=("outputs",), run_after=None):
    api = CONNECTORS[key]["api"]
    if key == "autotask":
        params = dict(params, UserName="@{%s}" % P("AutotaskUsername"),
                      ApiIntegrationcode="@{%s}" % P("AutotaskIntegrationCode"))
    a = {"type": "OpenApiConnection", "inputs": {"host": {"apiId": "/providers/Microsoft.PowerApps/apis/" + api,
         "operationId": op, "connectionName": api}, "parameters": params}, "runAfter": run_after or {}}
    if secure: a["runtimeConfiguration"] = {"secureData": {"properties": list(secure)}}
    return a

# ---- helpers ----------------------------------------------------------------------------------
ALL = ["Succeeded", "Failed", "Skipped", "TimedOut"]
SP_API = "/providers/Microsoft.PowerApps/apis/shared_sharepointonline"
O365_API = "/providers/Microsoft.PowerApps/apis/shared_office365"

def sp(op, params, run_after=None):
    return {"type": "OpenApiConnection",
            "inputs": {"host": {"connectionName": "shared_sharepointonline", "operationId": op, "apiId": SP_API},
                       "parameters": params, "authentication": "@parameters('$authentication')"},
            "runAfter": run_after or {}}

def cond(expr, then, run_after=None):
    return {"type": "If", "expression": {"equals": ["@" + expr, True]},
            "actions": then, "else": {"actions": {}}, "runAfter": run_after or {}}

def chain(actions, order):
    """runAfter each action on the previous one (all outcomes)."""
    prev = None
    for n in order:
        actions[n]["runAfter"] = {prev: ALL} if prev else {}
        prev = n
    return actions

# ---- the flow ---------------------------------------------------------------------------------
live = "contains(createArray('false', 'no'), toLower(string(%s)))" % P("DryRun")
at_on = "contains(createArray('true', 'yes'), toLower(string(%s)))" % P("WriteAutotask")
IT = "items('Foreach_Batch')"
site, lst = P("SharePointSiteUrl"), P("SharePointListName")

def attr(name): return "item()?['attributes']?['%s']" % name

until_actions = chain({
    "Get_IT_Glue_page": cc("itglue", "ListConfigurations", {
        "typeId": "@{items('Foreach_Type')}", "statusId": "@{%s}" % P("ActiveStatusId"),
        "pageSize": "1000", "pageNumber": "@{variables('Page')}"}, secure=None),
    "Select_Clean": {"type": "Select", "inputs": {
        "from": "@body('Get_IT_Glue_page')?['data']",
        "select": {
            "id": "@item()?['id']",
            "org": "@" + attr("organization-name"),
            "name": "@" + attr("name"),
            "serial": "@" + attr("serial-number"),
            "asset": "@string(coalesce(%s, ''))" % attr("asset-tag"),
            "glue": "@take(string(coalesce(%s, '')), 10)" % attr("warranty-expires-at"),
            "mitp": "@take(string(coalesce(item()?['attributes']?[%s], '')), 10)" % P("MyITPAttribute"),
        }}},
    "Set_PageCount": {"type": "SetVariable", "inputs": {"name": "PageCount", "value": "@length(body('Get_IT_Glue_page')?['data'])"}},
    # a Set variable step can't read the variable it sets, so merge in a Compose first
    "Compose_Merged": {"type": "Compose", "inputs": "@union(variables('Devices'), body('Select_Clean'))"},
    "Merge_Devices": {"type": "SetVariable", "inputs": {"name": "Devices", "value": "@outputs('Compose_Merged')"}},
    "Next_page": {"type": "IncrementVariable", "inputs": {"name": "Page", "value": 1}},
}, ["Get_IT_Glue_page", "Select_Clean", "Set_PageCount", "Compose_Merged", "Merge_Devices", "Next_page"])
for k in ("Select_Clean", "Set_PageCount", "Compose_Merged", "Merge_Devices", "Next_page"):
    until_actions[k]["runAfter"] = {list(until_actions)[list(until_actions).index(k)-1]: ["Succeeded"]}

foreach_type_actions = {
    "Reset_Page": {"type": "SetVariable", "inputs": {"name": "Page", "value": 1}, "runAfter": {}},
    "Page_until_short": {"type": "Until", "expression": "@less(variables('PageCount'), 1000)",
                         "limit": {"count": 100, "timeout": "PT3H"}, "actions": until_actions,
                         "runAfter": {"Reset_Page": ["Succeeded"]}},
}

CLEAN_SELECT = until_actions["Select_Clean"]["inputs"]["select"]

def scan_actions(page_filter=None, stop=None):
    """IT Glue paging loop for one configuration type.
    page_filter: keep only matching devices from each page BEFORE merging (merging thousands of
                 records with union() is slow, so a targeted run merges only the hits).
    stop:        expression that is true once the wanted device is found; ends paging early and
                 skips the remaining types."""
    ua = json.loads(json.dumps(until_actions))
    if page_filter:
        ua["Filter_Page"] = {"type": "Query", "runAfter": {"Set_PageCount": ["Succeeded"]},
                             "inputs": {"from": "@body('Select_Clean')", "where": "@" + page_filter}}
        ua["Compose_Merged"]["inputs"] = "@union(variables('Devices'), body('Filter_Page'))"
        ua["Compose_Merged"]["runAfter"] = {"Filter_Page": ["Succeeded"]}
    until = {"type": "Until", "limit": {"count": 100, "timeout": "PT3H"}, "actions": ua,
             "expression": "@or(less(variables('PageCount'), 1000), %s)" % stop if stop else "@less(variables('PageCount'), 1000)"}
    acts = {"Reset_Page": {"type": "SetVariable", "inputs": {"name": "Page", "value": 1}, "runAfter": {}}}
    if stop:
        acts["If_still_searching"] = cond("not(%s)" % stop, {"Page_until_short": dict(until, runAfter={})},
                                          run_after={"Reset_Page": ["Succeeded"]})
    else:
        acts["Page_until_short"] = dict(until, runAfter={"Reset_Page": ["Succeeded"]})
    return acts

decided = {"id": "@item()?['id']", "org": "@item()?['org']", "name": "@item()?['name']", "serial": "@item()?['serial']",
           "asset": "@item()?['asset']", "glue": "@item()?['glue']", "mitp": "@item()?['mitp']",
           "target": ("@if(empty(item()?['mitp']), item()?['glue'], if(or(empty(item()?['glue']), "
                      "less(item()?['glue'], item()?['mitp'])), item()?['mitp'], item()?['glue']))"),
           "later": "@and(not(empty(item()?['mitp'])), greater(item()?['glue'], item()?['mitp']))"}

# ---- device links: IT Glue has no psa-id / rmm-id, so devices are matched by serial number ----------
# Autotask: the ONE active configuration item with this serial -> its id (and warranty date).
# Datto RMM: that item's rmmDeviceUID (filled by the Datto RMM <-> Autotask integration); if blank, IT Glue's
# asset tag when it looks like a Datto device UID (the Datto RMM -> IT Glue sync writes it there).
def uid_shape(e):  # 36 characters in five dash-separated groups, like a Datto device UID
    return "and(equals(length(%s), 36), equals(length(split(%s, '-')), 5))" % (e, e)

def resolve_actions(X):
    serial = "trim(string(coalesce(%s?['serial'], '')))" % X
    asset = "trim(string(coalesce(%s?['asset'], '')))" % X
    is_uid = uid_shape(asset)
    ref_no = "trim(string(coalesce(outputs('Compose_CI')?['referenceNumber'], '')))"
    return chain({
        "Find_CI": cc("autotask", "QueryConfigurationItems", {"body": {
            "MaxRecords": 10,
            "IncludeFields": ["id", "serialNumber", "isActive", "referenceNumber", "rmmDeviceUID", "referenceTitle", "warrantyExpirationDate"],
            # a device with no serial must not match items with a blank serial
            "Filter": [{"op": "eq", "field": "serialNumber", "value": "@{if(empty(%s), '-no-serial-', %s)}" % (serial, serial)}]}},
            secure=None),
        "Filter_CI_active": {"type": "Query", "inputs": {"from": "@coalesce(body('Find_CI')?['items'], json('[]'))",
                             "where": "@equals(item()?['isActive'], true)"}},
        "Compose_CICount": {"type": "Compose", "inputs": "@length(body('Filter_CI_active'))"},
        "Compose_CI": {"type": "Compose", "inputs": "@if(equals(outputs('Compose_CICount'), 1), first(body('Filter_CI_active')), json('{}'))"},
        "Compose_Psa": {"type": "Compose", "inputs": "@string(coalesce(outputs('Compose_CI')?['id'], ''))"},
        # Datto device UID, first match wins: Autotask reference number (where this tenant keeps it),
        # Autotask rmmDeviceUID, then IT Glue asset tag. Reference number and asset tag must look like a UID.
        "Compose_Rmm": {"type": "Compose", "inputs": (
            "@if(%s, %s, if(not(empty(string(coalesce(outputs('Compose_CI')?['rmmDeviceUID'], '')))), "
            "string(outputs('Compose_CI')?['rmmDeviceUID']), if(%s, %s, '')))") % (
            uid_shape(ref_no), ref_no, is_uid, asset)},
    }, ["Find_CI", "Filter_CI_active", "Compose_CICount", "Compose_CI", "Compose_Psa", "Compose_Rmm"])

PSA, RMM, CICOUNT = "outputs('Compose_Psa')", "outputs('Compose_Rmm')", "outputs('Compose_CICount')"
psa_reason = ("if(greater(%s, 1), 'Several active Autotask items share this serial. ', "
              "'No active Autotask item with this serial. ')") % CICOUNT
rmm_reason = "'No Datto device UID (Autotask reference number and rmmDeviceUID are blank, asset tag is not a UID). '"

# per-device actions
sc_glue = {"type": "Scope", "actions": {"If_glue_changed": cond(
    "and(%s, not(equals(%s?['target'], %s?['glue'])))" % (live, IT, IT),
    {"ITGlue_write": cc("itglue", "UpdateConfiguration", {"id": "@{%s?['id']}" % IT,
        "body": {"data": {"type": "configurations", "attributes": {"warranty-expires-at": "@%s?['target']" % IT}}}}, secure=None)})}}
sc_datto = {"type": "Scope", "actions": {"If_rmm_present": cond(
    "and(%s, not(empty(%s)))" % (live, RMM),
    {"Datto_write": cc("datto", "SetDeviceWarranty", {"deviceUid": "@{%s}" % RMM,
        "Authorization": "Bearer @{body('Get_Datto_token')?['access_token']}", "body/warrantyDate": "@%s?['target']" % IT}, secure=("inputs",))})}}
sc_at = {"type": "Scope", "actions": {"If_psa_present": cond(
    "and(%s, %s, not(empty(%s)))" % (live, at_on, PSA),
    {"Autotask_write": cc("autotask", "UpdateConfigurationItem", {"body/id": "@int(%s)" % PSA,
        "body/warrantyExpirationDate": "@{%s?['target']}T00:00:00Z" % IT}, secure=None)})}}

def failed(scope): return "equals(actions('%s')?['status'], 'Failed')" % scope
had_err = "or(%s, %s, %s)" % (failed("Scope_ITGlue"), failed("Scope_Datto"), failed("Scope_Autotask"))
missing = "or(empty(%s), and(%s, empty(%s)))" % (RMM, at_on, PSA)
dry = "not(%s)" % live
existing_row = "body('Get_State_row')?['value']"
existing_pushed = "if(greater(length(%s), 0), coalesce(first(%s)?['PushedDate'], ''), '')" % (existing_row, existing_row)

def nullable(f): return "@if(empty(%s?['%s']), null, %s?['%s'])" % (IT, f, IT, f)
row = {"dataset": "@" + site, "table": "@" + lst,
       "item/Title": "@%s?['id']" % IT, "item/Organisation": "@coalesce(%s?['org'], '')" % IT,
       "item/Device": "@coalesce(%s?['name'], '')" % IT, "item/Serial": "@coalesce(%s?['serial'], '')" % IT,
       "item/MyITPDate": nullable("mitp"), "item/GlueDate": nullable("glue"),
       "item/PushedDate": "@outputs('Compose_Pushed')", "item/Status/Value": "@outputs('Compose_Status')",
       "item/Note": "@outputs('Compose_Note')", "item/LastRun": "@utcNow()"}

batch_actions = chain({
    "Get_State_row": sp("GetItems", {"dataset": "@" + site, "table": "@" + lst,
        "$filter": "Title eq '@{%s?['id']}'" % IT, "$top": 1}),
    **resolve_actions(IT),
    "Scope_ITGlue": sc_glue, "Scope_Datto": sc_datto, "Scope_Autotask": sc_at,
    "Compose_HadError": {"type": "Compose", "inputs": "@" + had_err},
    "Compose_Missing": {"type": "Compose", "inputs": "@" + missing},
    "Compose_Status": {"type": "Compose", "inputs": (
        "@if(outputs('Compose_HadError'), 'Error', if(%s, 'Dry run', if(outputs('Compose_Missing'), 'Missing ID', "
        "if(%s?['later'], 'Glue later – review', 'OK'))))") % (dry, IT)},
    "Compose_Pushed": {"type": "Compose", "inputs": "@if(and(%s, not(outputs('Compose_HadError')), not(outputs('Compose_Missing'))), %s?['target'], %s)" % (live, IT, existing_pushed)},
    "Compose_Note": {"type": "Compose", "inputs": (
        "@concat(if(%s, 'IT Glue write failed. ', ''), if(%s, 'Datto RMM write failed. ', ''), "
        "if(%s, 'Autotask write failed. ', ''), if(empty(%s), %s, ''), "
        "if(and(%s, empty(%s)), %s, ''), if(or(%s, %s, %s), 'See flow run history.', ''))") % (
        failed("Scope_ITGlue"), failed("Scope_Datto"), failed("Scope_Autotask"), RMM, rmm_reason, at_on, PSA, psa_reason,
        failed("Scope_ITGlue"), failed("Scope_Datto"), failed("Scope_Autotask"))},
    "If_row_exists": {"type": "If", "expression": {"equals": ["@greater(length(%s), 0)" % existing_row, True]},
        "actions": {"Update_row": sp("PatchItem", dict(row, id="@first(%s)?['ID']" % existing_row))},
        "else": {"actions": {"Create_row": sp("PostItem", row)}}, "runAfter": {}},
}, ["Get_State_row", "Find_CI", "Filter_CI_active", "Compose_CICount", "Compose_CI", "Compose_Psa", "Compose_Rmm",
    "Scope_ITGlue", "Scope_Datto", "Scope_Autotask", "Compose_HadError", "Compose_Missing",
    "Compose_Status", "Compose_Pushed", "Compose_Note", "If_row_exists"])

state_str = "@concat(',', join(body('Select_State'), ','), ',')"
to_push = ("@and(not(empty(item()?['target'])), not(contains(outputs('StateString'), "
           "concat(',', item()?['id'], '|', item()?['target'], ','))))")

summary_body = ("<p>Warranty Sync finished (@{if(%s, 'LIVE', 'DRY RUN')}).</p><ul>"
    "<li>Devices read from IT Glue: @{length(body('Select_Decided'))}</li>"
    "<li>Changes pending: @{length(body('Filter_ToPush'))}</li>"
    "<li>Processed tonight: @{length(outputs('Compose_Batch'))}</li>"
    "<li>Errors tonight: @{length(body('Filter_Errors'))}</li></ul>"
    "@{if(greaterOrEquals(length(body('Get_State_items')?['value']), 5000), "
    "'<p><b>Warning: the Warranty Sync State list has 5,000 or more rows, the most this flow can read. "
    "Devices beyond that are treated as never pushed and are written again every night. Ask for the state "
    "lookup to be split before the list grows further.</b></p>', '')}"
    "<p><a href=\"@{%s}/Lists/WarrantySyncState/Needs%%20attention.aspx\">Needs attention view</a></p>") % (live, site)

actions = {
    "Init_RunStart": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "RunStart", "type": "string", "value": "@utcNow()"}]}},
    "Init_Devices": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "Devices", "type": "array", "value": []}]}},
    "Init_Page": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "Page", "type": "integer", "value": 1}]}},
    "Init_PageCount": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "PageCount", "type": "integer", "value": 0}]}},
    "Compose_TypeIds": {"type": "Compose", "inputs": "@createArray(%s, %s)" % (P("LaptopTypeId"), P("DesktopTypeId"))},
    "If_live_get_token": cond(live, {
        # the token is read straight from this action (secure outputs) by the Datto actions, which have
        # secure inputs; it is never put in a variable, as variable actions can't hide their values
        "Get_Datto_token": cc("datto", "GetToken", {}, secure=("inputs", "outputs"))}),
    "Foreach_Type": {"type": "Foreach", "foreach": "@outputs('Compose_TypeIds')", "actions": foreach_type_actions,
                     "runtimeConfiguration": {"concurrency": {"repetitions": 1}}},
    "Select_Decided": {"type": "Select", "inputs": {"from": "@variables('Devices')", "select": decided}},
    "Get_State_items": sp("GetItems", {"dataset": "@" + site, "table": "@" + lst, "$top": 5000}),
    "Select_State": {"type": "Select", "inputs": {"from": "@body('Get_State_items')?['value']",
                     "select": "@concat(item()?['Title'], '|', take(coalesce(item()?['PushedDate'], ''), 10))"}},
    "StateString": {"type": "Compose", "inputs": state_str},
    "Filter_ToPush": {"type": "Query", "inputs": {"from": "@body('Select_Decided')", "where": to_push}},
    "Compose_Batch": {"type": "Compose", "inputs": "@take(body('Filter_ToPush'), int(%s))" % P("MaxWritesPerRun")},
    "Foreach_Batch": {"type": "Foreach", "foreach": "@outputs('Compose_Batch')", "actions": batch_actions,
                      "runtimeConfiguration": {"concurrency": {"repetitions": 5}}},
    "Get_run_rows": sp("GetItems", {"dataset": "@" + site, "table": "@" + lst,
                       "$filter": "LastRun ge '@{variables('RunStart')}'", "$top": 5000}),
    "Filter_Errors": {"type": "Query", "inputs": {"from": "@body('Get_run_rows')?['value']",
                      "where": "@equals(item()?['Status']?['Value'], 'Error')"}},
    "Send_summary": {"type": "OpenApiConnection", "inputs": {
        "host": {"connectionName": "shared_office365", "operationId": "SendEmailV2", "apiId": O365_API},
        "parameters": {"emailMessage/To": "@" + P("NotifyEmail"), "emailMessage/Subject": "Warranty Sync summary",
                       "emailMessage/Body": summary_body, "emailMessage/Importance": "Normal"},
        "authentication": "@parameters('$authentication')"}, "runAfter": {}},
}
for n in ("Get_State_items", "Get_run_rows"):
    # 5,000 is the most SharePoint "Get items" will page to on this licence
    actions[n]["runtimeConfiguration"] = {"paginationPolicy": {"minimumItemCount": 5000}}
# top-level chain. Foreach_Batch / summary run regardless of per-device failures.
order = ["Init_RunStart", "Init_Devices", "Init_Page", "Init_PageCount", "Compose_TypeIds",
         "If_live_get_token", "Foreach_Type", "Select_Decided", "Get_State_items", "Select_State", "StateString",
         "Filter_ToPush", "Compose_Batch", "Foreach_Batch", "Get_run_rows", "Filter_Errors", "Send_summary"]
prev = None
for n in order:
    actions[n]["runAfter"] = {prev: (ALL if n in ("Get_run_rows", "Filter_Errors", "Send_summary") else ["Succeeded"])} if prev else {}
    prev = n

params = {"$connections": {"defaultValue": {}, "type": "Object"},
          "$authentication": {"defaultValue": {}, "type": "SecureObject"}}
for k, (disp, dflt, _, _) in ENV.items():
    params["%s (%s_%s)" % (disp, PREFIX, k)] = env_param(k)

flow = {"properties": {
    "connectionReferences": {
        "shared_sharepointonline": {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": PREFIX + "_sharedsharepointonline_wsync"},
                                    "api": {"name": "shared_sharepointonline"}},
        "shared_office365": {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": PREFIX + "_sharedoffice365_wsync"},
                             "api": {"name": "shared_office365"}},
        **{c["api"]: {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": c["connref"]},
                      "api": {"name": c["api"], "logicalName": c["name"]}} for c in CONNECTORS.values()}},
    "definition": {"$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
                   "contentVersion": "1.0.0.0", "parameters": params,
                   "triggers": {"Recurrence": {"type": "Recurrence", "recurrence": {
                       "frequency": "Day", "interval": 1, "timeZone": "GMT Standard Time",
                       "schedule": {"hours": ["1"], "minutes": [0]}}}},
                   "actions": actions},
    "templateName": None}, "schemaVersion": "1.0.0.0"}

# ---- single-system flows: Autotask Only, Datto RMM Only -----------------------------------------
# Each reads IT Glue with the same date rules, reads the device in ONE system and writes the warranty date
# only where that system differs. Nothing else is touched (no other system, no state list), so they can be
# run on demand at any time; the target system itself is the "only changes" check.
# Run-button inputs: Device (blank = all) and WriteNow (write even while Dry run is on).
ONE = "items('Foreach_Device')"
one_current = "outputs('Compose_Current')"
one_dev = "outputs('Compose_Device')"
one_live = "or(%s, equals(triggerBody()?['WriteNow'], true))" % live

def ref(c): return {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": c["connref"]},
                    "api": {"name": c["api"], "logicalName": c["name"]}}

def single_system_flow(label, id_compose, idlabel, conn, get_action, current_expr, write_action, pre_actions, no_id_reason):
    """label e.g. 'Autotask'; id_compose: the Compose holding this system's ID ('Compose_Psa' / 'Compose_Rmm');
    get_action: optional read of the device (None when the Autotask query already has the date)."""
    sys_id = "outputs('%s')" % id_compose
    match = ("or(empty({d}), equals(toLower(string(item()?['id'])), {d}), "
             "equals(toLower(trim(string(coalesce(item()?['serial'], '')))), {d}), "
             "equals(toLower(trim(string(coalesce(item()?['asset'], '')))), {d}), "
             "equals(toLower(trim(string(coalesce(item()?['name'], '')))), {d}))").format(d=one_dev)
    has_serial = "not(empty(trim(string(coalesce(item()?['serial'], '')))))"

    def result_row(current, result):
        return {"org": "@%s?['org']" % ONE, "device": "@%s?['name']" % ONE, "serial": "@%s?['serial']" % ONE,
                idlabel: "@" + sys_id, label.replace(" ", ""): current, "target": "@%s?['target']" % ONE, "result": result}

    found = {}
    if get_action:
        found["Get_Current"] = dict(get_action, runAfter={})
    found["Compose_Current"] = {"type": "Compose", "runAfter": {"Get_Current": ["Succeeded"]} if get_action else {},
                                "inputs": "@" + current_expr}
    found["If_differs"] = {"type": "If", "runAfter": {"Compose_Current": ["Succeeded"]},
        "expression": {"equals": ["@not(equals(%s, %s?['target']))" % (one_current, ONE), True]},
        "actions": {
            "Scope_Write": {"type": "Scope", "runAfter": {}, "actions": {"If_live_write": cond(one_live, {
                "Write_warranty": dict(write_action, runAfter={})})}},
            "Record_change": {"type": "AppendToArrayVariable", "runAfter": {"Scope_Write": ALL},
                "inputs": {"name": "Results", "value": result_row("@" + one_current,
                    "@if(%s, 'Error', if(%s, 'Updated', 'Would update'))" % (failed("Scope_Write"), one_live))}}},
        # a targeted device is always reported, including when it is already correct
        "else": {"actions": {"If_targeted": cond("not(empty(%s))" % one_dev, {
            "Record_unchanged": {"type": "AppendToArrayVariable", "runAfter": {},
                "inputs": {"name": "Results", "value": result_row("@" + one_current, "Already correct")}}})}}}
    if get_action:
        found["Record_lookup_error"] = {"type": "AppendToArrayVariable", "runAfter": {"Get_Current": ["Failed", "TimedOut"]},
            "inputs": {"name": "Results", "value": result_row("", "%s lookup failed" % label)}}

    device_actions = dict(resolve_actions(ONE))
    device_actions["If_has_id"] = {"type": "If", "runAfter": {"Compose_Rmm": ["Succeeded"]},
        "expression": {"equals": ["@not(empty(%s))" % sys_id, True]},
        "actions": found,
        "else": {"actions": {"Record_no_match": {"type": "AppendToArrayVariable", "runAfter": {},
            "inputs": {"name": "Results", "value": result_row("", "@trim(%s)" % no_id_reason)}}}}}

    summary = ("<p>Warranty Sync - {L} Only finished (@{{if({live}, 'LIVE', 'DRY RUN')}}).</p><ul>"
        "<li>Target: @{{if(empty(outputs('Compose_Device')), 'all devices', concat('<b>', outputs('Compose_Device'), '</b>'))}}</li>"
        "<li>Devices read from IT Glue: @{{length(body('Select_Decided'))}}</li>"
        "<li>Matching the target: @{{length(body('Filter_Target'))}}"
        "@{{if(and(not(empty(outputs('Compose_Device'))), equals(length(body('Filter_Target')), 0)), "
        "' <b>- no active laptop/desktop in IT Glue matches this ID, serial, asset tag or name</b>', '')}}</li>"
        "<li>With a serial number and a warranty date: @{{length(body('Filter_System'))}}"
        "@{{if(and(greater(length(body('Filter_Target')), 0), equals(length(body('Filter_System')), 0)), "
        "' <b>- the matching device has no serial number or no warranty date</b>', '')}}</li>"
        "<li>Checked this run: @{{length(outputs('Compose_Batch'))}}</li>"
        "<li>@{{if({live}, 'Updated', 'Would update')}}: @{{length(body('Filter_Changed'))}}</li>"
        "<li>Errors: @{{length(body('Filter_Failed'))}}</li></ul>"
        "<p>Devices whose {L} date differs are listed (a targeted device is always listed).</p>@{{body('Results_table')}}"
        ).format(L=label, I=idlabel, live=one_live)

    actions = {
        "Init_Devices": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "Devices", "type": "array", "value": []}]}},
        "Init_Page": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "Page", "type": "integer", "value": 1}]}},
        "Init_PageCount": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "PageCount", "type": "integer", "value": 0}]}},
        "Init_Results": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "Results", "type": "array", "value": []}]}},
        **json.loads(json.dumps(pre_actions)),
        "Compose_Device": {"type": "Compose", "inputs": "@toLower(trim(string(coalesce(triggerBody()?['Device'], ''))))"},
        "Compose_TypeIds": {"type": "Compose", "inputs": "@createArray(%s, %s)" % (P("LaptopTypeId"), P("DesktopTypeId"))},
        # fast path: the Device input is an IT Glue configuration ID -> fetch that one record
        "If_device_entered": cond("not(empty(%s))" % one_dev, {
            "Get_by_id": cc("itglue", "GetConfiguration", {"id": "@{trim(string(triggerBody()?['Device']))}"}, secure=None),
            "Keep_if_laptop_desktop": {"type": "Query", "runAfter": {"Get_by_id": ["Succeeded"]}, "inputs": {
                "from": "@createArray(body('Get_by_id')?['data'])",
                "where": ("@and(contains(createArray(string(%s), string(%s)), string(item()?['attributes']?['configuration-type-id'])), "
                          "equals(string(item()?['attributes']?['configuration-status-id']), string(%s)), "
                          "not(equals(item()?['attributes']?['archived'], true)))") % (P("LaptopTypeId"), P("DesktopTypeId"), P("ActiveStatusId"))}},
            "Select_Found": {"type": "Select", "runAfter": {"Keep_if_laptop_desktop": ["Succeeded"]},
                             "inputs": {"from": "@body('Keep_if_laptop_desktop')", "select": json.loads(json.dumps(CLEAN_SELECT))}},
            "Set_Found": {"type": "SetVariable", "runAfter": {"Select_Found": ["Succeeded"]},
                          "inputs": {"name": "Devices", "value": "@body('Select_Found')"}}}),
        # otherwise (or if the ID lookup found nothing) scan IT Glue, keeping only matching devices per page
        "Foreach_Type": {"type": "Foreach", "foreach": "@outputs('Compose_TypeIds')",
                         "actions": scan_actions(page_filter="and(%s, %s)" % (match, has_serial),
                                                 stop="and(not(empty(%s)), greater(length(variables('Devices')), 0))" % one_dev),
                         "runtimeConfiguration": {"concurrency": {"repetitions": 1}}},
        "Select_Decided": {"type": "Select", "inputs": {"from": "@variables('Devices')", "select": decided}},
        "Filter_Target": {"type": "Query", "inputs": {"from": "@body('Select_Decided')", "where": "@" + match}},
        "Filter_System": {"type": "Query", "inputs": {"from": "@body('Filter_Target')",
                          "where": "@and(not(empty(item()?['target'])), %s)" % has_serial}},
        "Compose_Batch": {"type": "Compose", "inputs": "@take(body('Filter_System'), int(%s))" % P("MaxWritesPerRun")},
        # concurrency 1: Results is appended to from inside the loop
        "Foreach_Device": {"type": "Foreach", "foreach": "@outputs('Compose_Batch')", "actions": device_actions,
                           "runtimeConfiguration": {"concurrency": {"repetitions": 1}}},
        "Filter_Changed": {"type": "Query", "inputs": {"from": "@variables('Results')",
                           "where": "@contains(createArray('Updated', 'Would update'), item()?['result'])"}},
        "Filter_Failed": {"type": "Query", "inputs": {"from": "@variables('Results')",
                          "where": "@not(contains(createArray('Updated', 'Would update', 'Already correct'), item()?['result']))"}},
        "Results_table": {"type": "Table", "inputs": {"from": "@variables('Results')", "format": "HTML"}},
        "Send_summary": {"type": "OpenApiConnection", "inputs": {
            "host": {"connectionName": "shared_office365", "operationId": "SendEmailV2", "apiId": O365_API},
            "parameters": {"emailMessage/To": "@" + P("NotifyEmail"), "emailMessage/Subject": "Warranty Sync - %s Only summary" % label,
                           "emailMessage/Body": summary, "emailMessage/Importance": "Normal"},
            "authentication": "@parameters('$authentication')"}},
    }
    order = (["Init_Devices", "Init_Page", "Init_PageCount", "Init_Results"] + list(pre_actions) +
             ["Compose_Device", "Compose_TypeIds", "If_device_entered", "Foreach_Type", "Select_Decided", "Filter_Target",
              "Filter_System", "Compose_Batch", "Foreach_Device", "Filter_Changed", "Filter_Failed", "Results_table", "Send_summary"])
    prev = None
    for n in order:
        # Foreach_Type runs even when the ID lookup fails (input wasn't an IT Glue ID)
        lenient = n == "Foreach_Type" or n in order[order.index("Filter_Changed"):]
        actions[n]["runAfter"] = {prev: (ALL if lenient else ["Succeeded"])} if prev else {}
        prev = n

    params = {"$connections": {"defaultValue": {}, "type": "Object"},
              "$authentication": {"defaultValue": {}, "type": "SecureObject"}}
    for k in ["DryRun", "LaptopTypeId", "DesktopTypeId", "ActiveStatusId", "MyITPAttribute", "MaxWritesPerRun", "NotifyEmail",
              "AutotaskUsername", "AutotaskIntegrationCode"]:
        params["%s (%s_%s)" % (ENV[k][0], PREFIX, k)] = env_param(k)

    return {"properties": {
        "connectionReferences": {
            "shared_office365": {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": PREFIX + "_sharedoffice365_wsync"},
                                 "api": {"name": "shared_office365"}},
            CONNECTORS["itglue"]["api"]: ref(CONNECTORS["itglue"]),
            # Autotask is always needed: devices are found in Autotask by serial number
            CONNECTORS["autotask"]["api"]: ref(CONNECTORS["autotask"]),
            CONNECTORS[conn]["api"]: ref(CONNECTORS[conn])},
        "definition": {"$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
                       "contentVersion": "1.0.0.0", "parameters": params,
                       "triggers": {"manual": {"type": "Request", "kind": "Button", "inputs": {"schema": {
                           "type": "object", "required": [], "properties": {
                               "Device": {"title": "Device (blank = all)", "type": "string", "x-ms-dynamically-added": True,
                                          "description": "IT Glue configuration ID, serial number, asset tag or device name. Leave blank for all devices.",
                                          "x-ms-content-hint": "TEXT"},
                               "WriteNow": {"title": "Write now (ignore Dry run)", "type": "boolean", "x-ms-dynamically-added": True,
                                            "description": "Yes = write to %s on this run even while the Dry run variable is Yes." % label,
                                            "x-ms-content-hint": "BOOLEAN"}}}}}},
                       "actions": actions},
        "templateName": None}, "schemaVersion": "1.0.0.0"}

AT_WF_ID = "2d6f8b14-7a3c-4e95-b1d8-5c0e9a4f7b23"
at_flow = single_system_flow(
    "Autotask", "Compose_Psa", "AutotaskID", "autotask",
    get_action=None,   # the serial-number query already returns the warranty date
    current_expr="take(string(coalesce(outputs('Compose_CI')?['warrantyExpirationDate'], '')), 10)",
    write_action=cc("autotask", "UpdateConfigurationItem", {"body/id": "@int(%s)" % PSA,
        "body/warrantyExpirationDate": "@{%s?['target']}T00:00:00Z" % ONE}, secure=None),
    pre_actions={}, no_id_reason=psa_reason)

DT_WF_ID = "9e3a5c71-0b4d-4f28-a6e1-7d2c8b5f4a19"
dt_flow = single_system_flow(
    "Datto RMM", "Compose_Rmm", "DattoUID", "datto",
    get_action=cc("datto", "GetDevice", {"deviceUid": "@{%s}" % RMM,
        "Authorization": "Bearer @{body('Get_Datto_token')?['access_token']}"}, secure=("inputs",)),
    current_expr="take(string(coalesce(body('Get_Current')?['warrantyDate'], '')), 10)",
    write_action=cc("datto", "SetDeviceWarranty", {"deviceUid": "@{%s}" % RMM,
        "Authorization": "Bearer @{body('Get_Datto_token')?['access_token']}", "body/warrantyDate": "@%s?['target']" % ONE}, secure=("inputs",)),
    no_id_reason="concat(if(empty(%s), %s, ''), %s)" % (PSA, psa_reason, rmm_reason),
    # the token is needed to read devices, so it is fetched even in dry run
    pre_actions={
        "Get_Datto_token": cc("datto", "GetToken", {}, secure=("inputs", "outputs"))})

# ---- List Devices: read-only report to pick test devices -------------------------------------------
LS_WF_ID = "5a8c2e47-d13b-4f6a-9c70-3b1e6d8f2a95"
search = "outputs('Compose_Search')"
haystack = ("toLower(concat(coalesce(item()?['org'], ''), '|', coalesce(item()?['name'], ''), '|', "
            "coalesce(item()?['serial'], ''), '|', item()?['id'], '|', item()?['asset']))")
list_filter = ("and(or(empty({s}), contains({h}, {s})), or(not(equals(triggerBody()?['TestReadyOnly'], true)), "
               "and(not(empty(item()?['serial'])), not(empty(item()?['mitp'])))))").format(s=search, h=haystack)
list_columns = {
    "Organisation": "@item()?['org']", "Device": "@item()?['name']", "Serial": "@item()?['serial']",
    "ITGlueID": "@item()?['id']", "AssetTag": "@item()?['asset']",
    "ITGlueDate": "@item()?['glue']", "MyITPDate": "@item()?['mitp']", "SyncDate": "@item()?['target']",
    "Notes": ("@concat(if(empty(item()?['serial']), 'no serial number; ', ''), "
              "if(empty(item()?['mitp']), 'no myITProcess date; ', ''), "
              "if(item()?['later'], 'IT Glue later than myITProcess - review; ', ''), "
              "if(and(not(empty(item()?['target'])), not(equals(item()?['target'], item()?['glue']))), 'IT Glue date will change; ', ''), "
              "if(and(not(empty(item()?['serial'])), not(empty(item()?['mitp']))), 'test-ready', ''))"),
}
list_summary = ("<p>IT Glue active laptops and desktops.</p><ul>"
    "<li>Search: @{if(empty(outputs('Compose_Search')), '(none)', outputs('Compose_Search'))}</li>"
    "<li>Test-ready only: @{if(equals(triggerBody()?['TestReadyOnly'], true), 'Yes', 'No')}</li>"
    "<li>Devices read: @{length(body('Select_Decided'))}</li>"
    "<li>Matching: @{length(body('Filter_List'))}</li></ul>"
    "<p><b>test-ready</b> = has a serial number and a myITProcess date. Autotask and Datto RMM devices are found by "
    "serial number, so check the device is also in Autotask with that serial. "
    "Use the ITGlueID (fastest), serial, asset tag or name as the <b>Device</b> input of the Autotask Only or Datto RMM Only flow.</p>"
    "<p>@{if(greater(length(body('Filter_List')), 300), 'First 300 shown; all matches are in the attached CSV.', 'All matches are also in the attached CSV.')}</p>"
    "@{body('Devices_table')}")

ls_actions = {
    "Init_Devices": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "Devices", "type": "array", "value": []}]}},
    "Init_Page": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "Page", "type": "integer", "value": 1}]}},
    "Init_PageCount": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "PageCount", "type": "integer", "value": 0}]}},
    "Compose_Search": {"type": "Compose", "inputs": "@toLower(trim(string(coalesce(triggerBody()?['Search'], ''))))"},
    "Compose_TypeIds": {"type": "Compose", "inputs": "@createArray(%s, %s)" % (P("LaptopTypeId"), P("DesktopTypeId"))},
    "Foreach_Type": {"type": "Foreach", "foreach": "@outputs('Compose_TypeIds')",
                     "actions": scan_actions(page_filter=list_filter),
                     "runtimeConfiguration": {"concurrency": {"repetitions": 1}}},
    "Select_Decided": {"type": "Select", "inputs": {"from": "@variables('Devices')", "select": decided}},
    "Filter_List": {"type": "Query", "inputs": {"from": "@body('Select_Decided')", "where": "@" + list_filter}},
    "Select_List": {"type": "Select", "inputs": {"from": "@body('Filter_List')", "select": list_columns}},
    "Devices_table": {"type": "Table", "inputs": {"from": "@take(body('Select_List'), 300)", "format": "HTML"}},
    "Devices_csv": {"type": "Table", "inputs": {"from": "@body('Select_List')", "format": "CSV"}},
    "Send_list": {"type": "OpenApiConnection", "inputs": {
        "host": {"connectionName": "shared_office365", "operationId": "SendEmailV2", "apiId": O365_API},
        "parameters": {"emailMessage/To": "@" + P("NotifyEmail"),
                       "emailMessage/Subject": "Warranty Sync - IT Glue device list (@{length(body('Filter_List'))} devices)",
                       "emailMessage/Body": list_summary,
                       "emailMessage/Attachments": [{"Name": "itglue-devices.csv", "ContentBytes": "@{base64(body('Devices_csv'))}"}],
                       "emailMessage/Importance": "Normal"},
        "authentication": "@parameters('$authentication')"}},
}
ls_order = ["Init_Devices", "Init_Page", "Init_PageCount", "Compose_Search", "Compose_TypeIds", "Foreach_Type",
            "Select_Decided", "Filter_List", "Select_List", "Devices_table", "Devices_csv", "Send_list"]
prev = None
for n in ls_order:
    ls_actions[n]["runAfter"] = {prev: ["Succeeded"]} if prev else {}
    prev = n
ls_params = {"$connections": {"defaultValue": {}, "type": "Object"},
             "$authentication": {"defaultValue": {}, "type": "SecureObject"}}
for k in ["LaptopTypeId", "DesktopTypeId", "ActiveStatusId", "MyITPAttribute", "NotifyEmail"]:
    ls_params["%s (%s_%s)" % (ENV[k][0], PREFIX, k)] = env_param(k)
ls_flow = {"properties": {
    "connectionReferences": {
        "shared_office365": {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": PREFIX + "_sharedoffice365_wsync"},
                             "api": {"name": "shared_office365"}},
        CONNECTORS["itglue"]["api"]: ref(CONNECTORS["itglue"])},
    "definition": {"$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
                   "contentVersion": "1.0.0.0", "parameters": ls_params,
                   "triggers": {"manual": {"type": "Request", "kind": "Button", "inputs": {"schema": {
                       "type": "object", "required": [], "properties": {
                           "Search": {"title": "Search (blank = all)", "type": "string", "x-ms-dynamically-added": True,
                                      "description": "Text to look for in organisation, device name, serial, IT Glue ID or asset tag.",
                                      "x-ms-content-hint": "TEXT"},
                           "TestReadyOnly": {"title": "Test-ready only", "type": "boolean", "x-ms-dynamically-added": True,
                                             "description": "Yes = only devices with a serial number and a myITProcess date.",
                                             "x-ms-content-hint": "BOOLEAN"}}}}}},
                   "actions": ls_actions},
    "templateName": None}, "schemaVersion": "1.0.0.0"}

FLOWS = [
    {"id": WF_ID, "name": FLOW_NAME, "file": "/Workflows/WarrantySync-Nightly-%s.json" % WF_ID.upper(), "flow": flow},
    {"id": AT_WF_ID, "name": "Warranty Sync - Autotask Only", "file": "/Workflows/WarrantySync-AutotaskOnly-%s.json" % AT_WF_ID.upper(), "flow": at_flow},
    {"id": DT_WF_ID, "name": "Warranty Sync - Datto RMM Only", "file": "/Workflows/WarrantySync-DattoRMMOnly-%s.json" % DT_WF_ID.upper(), "flow": dt_flow},
    {"id": LS_WF_ID, "name": "Warranty Sync - List Devices", "file": "/Workflows/WarrantySync-ListDevices-%s.json" % LS_WF_ID.upper(), "flow": ls_flow},
]

# ---- solution files ---------------------------------------------------------------------------
# Layout, element order, CRLF endings and the publisher mirror a solution exported from the target
# environment (AutotaskaBillitySync_1_0_0_13.zip), which is known to import.
ORGVER = 'OrganizationVersion="9.2.26102.127" OrganizationSchemaType="Standard" CRMServerServiceabilityVersion="9.2.26102.00127"'
PUBLISHER = ("DefaultPublisherorgda80817a", "Default Publisher for orgda80817a", "Default publisher for this organization")
VERSION = "1.0.0.20"

def write(path, text, bom=False):
    full = os.path.join(SRC, path); os.makedirs(os.path.dirname(full), exist_ok=True)
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    with open(full, "wb") as f: f.write((b"\xef\xbb\xbf" if bom else b"") + data)

def label(tag, text, ind):
    return ('%s<%s default=%s>\n%s  <label description=%s languagecode="1033" />\n%s</%s>\n'
            % (ind, tag, quoteattr(text), ind, quoteattr(text), ind, tag))

shutil.rmtree(SRC, ignore_errors=True)
for f in FLOWS:
    write(f["file"].lstrip("/"), json.dumps(f["flow"], indent=2, ensure_ascii=False))
write("[Content_Types].xml", '<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="xml" ContentType="application/octet-stream" /><Default Extension="json" ContentType="application/octet-stream" /><Default Extension="Png" ContentType="application/octet-stream" /></Types>', bom=True)

def png(hexcolor, size=100):
    import struct, zlib
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    raw = b"".join(b"\x00" + bytes((r, g, b, 255)) * size for _ in range(size))
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

for c in CONNECTORS.values():
    n = c["name"]
    write("Connector/%s_openapidefinition.json" % n, json.dumps(c["swagger"], separators=(",", ":")))
    write("Connector/%s_connectionparameters.json" % n, json.dumps(c["params"], separators=(",", ":")))
    write("Connector/%s_policytemplateinstances.json" % n, json.dumps(c["policies"], separators=(",", ":")))
    full = os.path.join(SRC, "Connector", "%s_iconblob.Png" % n)
    with open(full, "wb") as fh: fh.write(png(c["color"]))

addr_fields = ["AddressTypeCode", "City", "County", "Country", "Fax", "FreightTermsCode", "ImportSequenceNumber", "Latitude",
               "Line1", "Line2", "Line3", "Longitude", "Name", "PostalCode", "PostOfficeBox", "PrimaryContactName",
               "ShippingMethodCode", "StateOrProvince", "Telephone1", "Telephone2", "Telephone3", "TimeZoneRuleVersionNumber",
               "UPSZone", "UTCOffset", "UTCConversionTimeZoneCode"]
def address(n):
    return ("        <Address>\n          <AddressNumber>%d</AddressNumber>\n" % n
            + "".join('          <%s xsi:nil="true"></%s>\n' % (f, f) for f in addr_fields) + "        </Address>\n")

write("solution.xml",
 '<ImportExportXml version="9.2.26102.127" SolutionPackageVersion="9.2" languagecode="1033" generatedBy="CrmLive" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" %s>\n' % ORGVER +
 '  <SolutionManifest>\n    <UniqueName>WarrantySync</UniqueName>\n    <LocalizedNames>\n      <LocalizedName description="Warranty Sync" languagecode="1033" />\n    </LocalizedNames>\n'
 '    <Descriptions />\n    <Version>%s</Version>\n    <Managed>0</Managed>\n    <Publisher>\n      <UniqueName>%s</UniqueName>\n' % (VERSION, PUBLISHER[0]) +
 '      <LocalizedNames>\n        <LocalizedName description="%s" languagecode="1033" />\n      </LocalizedNames>\n' % PUBLISHER[1] +
 '      <Descriptions>\n        <Description description="%s" languagecode="1033" />\n      </Descriptions>\n' % PUBLISHER[2] +
 '      <EMailAddress xsi:nil="true"></EMailAddress>\n      <SupportingWebsiteUrl xsi:nil="true"></SupportingWebsiteUrl>\n'
 '      <CustomizationPrefix>%s</CustomizationPrefix>\n      <CustomizationOptionValuePrefix>10000</CustomizationOptionValuePrefix>\n' % PREFIX +
 '      <Addresses>\n' + address(1) + address(2) + '      </Addresses>\n    </Publisher>\n'
 '    <RootComponents>\n' +
 "".join('      <RootComponent type="29" id="{%s}" behavior="0" />\n' % f["id"] for f in FLOWS) +
 "".join('      <RootComponent type="372" id="{%s}" schemaName="%s" behavior="0" />\n' % (c["id"], c["name"]) for c in CONNECTORS.values()) +
 '    </RootComponents>\n' +
 '    <MissingDependencies />\n  </SolutionManifest>\n</ImportExportXml>')

def connref(logical, disp, api, custom_id=None):
    return ('    <connectionreference connectionreferencelogicalname="%s">\n'
            '      <connectionreferencedisplayname>%s</connectionreferencedisplayname>\n'
            '      <connectorid>/providers/Microsoft.PowerApps/apis/%s</connectorid>\n' % (logical, escape(disp), api) +
            ('      <customconnectorid>\n        <connectorid>%s</connectorid>\n      </customconnectorid>\n' % custom_id if custom_id else '') +
            '      <iscustomizable>1</iscustomizable>\n      <promptingbehavior>0</promptingbehavior>\n'
            '      <statecode>0</statecode>\n      <statuscode>1</statuscode>\n    </connectionreference>\n')

def workflow_xml(f):
    return ('    <Workflow WorkflowId="{%s}" Name=%s>\n' % (f["id"], quoteattr(f["name"])) +
     '      <JsonFileName>%s</JsonFileName>\n' % f["file"] +
     '      <Type>1</Type>\n      <Subprocess>0</Subprocess>\n      <Category>5</Category>\n      <Mode>0</Mode>\n      <Scope>4</Scope>\n'
     '      <OnDemand>0</OnDemand>\n      <TriggerOnCreate>0</TriggerOnCreate>\n      <TriggerOnDelete>0</TriggerOnDelete>\n'
     '      <AsyncAutodelete>0</AsyncAutodelete>\n      <SyncWorkflowLogOnFailure>0</SyncWorkflowLogOnFailure>\n'
     '      <StateCode>0</StateCode>\n      <StatusCode>1</StatusCode>\n      <RunAs>1</RunAs>\n      <IsTransacted>1</IsTransacted>\n'
     '      <IntroducedVersion>%s</IntroducedVersion>\n      <IsCustomizable>1</IsCustomizable>\n' % VERSION +
     '      <IsCustomProcessingStepAllowedForOtherPublishers>1</IsCustomProcessingStepAllowedForOtherPublishers>\n'
     '      <ModernFlowType>0</ModernFlowType>\n      <PrimaryEntity>none</PrimaryEntity>\n'
     '      <LocalizedNames>\n        <LocalizedName languagecode="1033" description=%s />\n      </LocalizedNames>\n' % quoteattr(f["name"]) +
     '    </Workflow>\n')

def connector_xml(c):
    n = c["name"]
    return ('    <Connector>\n      <connectorid>%s</connectorid>\n      <description>%s</description>\n'
            '      <displayname>%s</displayname>\n      <iconbrandcolor>%s</iconbrandcolor>\n      <name>%s</name>\n'
            '      <connectortype>1</connectortype>\n      <openapidefinition>/Connector/%s_openapidefinition.json</openapidefinition>\n'
            '      <connectionparameters>/Connector/%s_connectionparameters.json</connectionparameters>\n'
            '      <policytemplateinstances>/Connector/%s_policytemplateinstances.json</policytemplateinstances>\n'
            '      <iconblob>/Connector/%s_iconblob.Png</iconblob>\n    </Connector>\n') % (
            c["id"], escape(c["desc"]), escape(c["display"]), c["color"], n, n, n, n, n)

write("customizations.xml",
 '<ImportExportXml xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" %s>\n' % ORGVER +
 '  <Entities></Entities>\n  <Roles></Roles>\n  <Workflows>\n' +
 "".join(workflow_xml(f) for f in FLOWS) +
 '  </Workflows>\n  <FieldSecurityProfiles></FieldSecurityProfiles>\n  <Templates />\n  <EntityMaps />\n' +
 '  <EntityRelationships />\n  <OrganizationSettings />\n  <optionsets />\n  <CustomControls />\n  <EntityDataProviders />\n'
 '  <Connectors>\n' + "".join(connector_xml(c) for c in CONNECTORS.values()) + '  </Connectors>\n'
 '  <connectionreferences>\n' +
 "".join(connref(c["connref"], c["display"], c["api"], c["id"]) for c in CONNECTORS.values()) +
 connref(PREFIX + "_sharedoffice365_wsync", "Office 365 Outlook (Warranty Sync)", "shared_office365") +
 connref(PREFIX + "_sharedsharepointonline_wsync", "SharePoint (Warranty Sync State)", "shared_sharepointonline") +
 '  </connectionreferences>\n  <Languages>\n    <Language>1033</Language>\n  </Languages>\n</ImportExportXml>')

# env var definitions: children in alphabetical order, as exported
for k, (disp, dflt, req, desc) in ENV.items():
    x = '<environmentvariabledefinition schemaname="%s_%s">\n' % (PREFIX, k)
    if dflt: x += '  <defaultvalue>%s</defaultvalue>\n' % escape(dflt)
    if desc: x += label("description", desc, "  ")
    x += label("displayname", disp, "  ")
    x += ('  <introducedversion>%s</introducedversion>\n  <iscustomizable>1</iscustomizable>\n  <isrequired>%d</isrequired>\n'
          '  <secretstore>0</secretstore>\n  <type>%s</type>\n</environmentvariabledefinition>' % (
          VERSION, 1 if req else 0, "100000002" if k in BOOL_ENV else "100000000"))
    write("environmentvariabledefinitions/%s_%s/environmentvariabledefinition.xml" % (PREFIX, k), x)

if os.path.exists(ZIP): os.remove(ZIP)
names = []
for root, _, files in os.walk(SRC):
    for f in files:
        names.append(os.path.relpath(os.path.join(root, f), SRC).replace(os.sep, "/"))
first = ["customizations.xml", "solution.xml"]
last = ["[Content_Types].xml"]
names = first + sorted(n for n in names if n.startswith("Workflows/")) + \
        sorted(n for n in names if n.startswith("Connector/")) + \
        sorted(n for n in names if n.startswith("environmentvariabledefinitions/")) + last
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
    for n in names:
        zi = zipfile.ZipInfo(n, date_time=(2026, 11, 2, 0, 0, 0)); zi.compress_type = zipfile.ZIP_DEFLATED
        with open(os.path.join(SRC, n), "rb") as fh: z.writestr(zi, fh.read())
print("wrote", ZIP)
