#!/usr/bin/env python3
"""Generate the importable Power Automate solution (WarrantySync_1_0_0_0.zip).

Run:  python3 warranty-sync/build/build_solution.py
Writes the unpacked source to warranty-sync/solution/ and the zip to warranty-sync/.
"""
import json, os, shutil, zipfile
from xml.sax.saxutils import escape, quoteattr

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.dirname(HERE)
SRC = os.path.join(OUT, "solution")
ZIP = os.path.join(OUT, "WarrantySync_1_0_0_0.zip")

PREFIX = "aits"
WF_ID = "7b1d3a52-4c6e-4f0a-9a37-2d5e8c1f0b64"
FLOW_NAME = "Warranty Sync - Nightly"

# ---- environment variables: key -> (display, default, required, description) ------------------
ENV = {
 "DryRun": ("Dry run", "true", False, "true = record what would change but write nothing. Set to false to go live."),
 "LaptopTypeId": ("IT Glue Laptop type ID", None, True, "GET /configuration_types"),
 "DesktopTypeId": ("IT Glue Desktop type ID", None, True, "GET /configuration_types"),
 "ActiveStatusId": ("IT Glue Active status ID", None, True, "GET /configuration_statuses"),
 "MyITPAttribute": ("myITProcess attribute name", None, True, "Exact IT Glue configuration attribute holding the myITProcess warranty date"),
 "MaxWritesPerRun": ("Max writes per run", "1500", False, "Cap on devices written per night"),
 "WriteAutotask": ("Write Autotask", "true", False, "true/false"),
 "ITGlueBaseUri": ("IT Glue base URI", "https://api.itglue.com", False, "e.g. https://api.eu.itglue.com for EU"),
 "ITGlueApiKey": ("IT Glue API key", None, True, "Needs read + write on Configurations"),
 "DattoApiUrl": ("Datto RMM API URL", None, False, "https://<platform>-api.centrastage.net"),
 "DattoApiKey": ("Datto RMM API key", None, False, ""),
 "DattoApiSecret": ("Datto RMM API secret", None, False, ""),
 "AutotaskBaseUri": ("Autotask REST base URI", None, False, "https://webservicesN.autotask.net/ATServicesRest/V1.0"),
 "AutotaskUsername": ("Autotask API username", None, False, ""),
 "AutotaskSecret": ("Autotask API secret", None, False, ""),
 "AutotaskIntegrationCode": ("Autotask integration code", None, False, ""),
 "SharePointSiteUrl": ("SharePoint site URL", None, True, "Site holding the Warranty Sync State list"),
 "SharePointListName": ("SharePoint list name", "Warranty Sync State", False, "List title (or GUID)"),
 "NotifyEmail": ("Summary email recipient", None, True, "Where the nightly summary is sent"),
}
def P(key):  # parameter expression fragment
    return "parameters('%s (%s_%s)')" % (ENV[key][0], PREFIX, key)

# ---- helpers ----------------------------------------------------------------------------------
ALL = ["Succeeded", "Failed", "Skipped", "TimedOut"]
SP_API = "/providers/Microsoft.PowerApps/apis/shared_sharepointonline"
O365_API = "/providers/Microsoft.PowerApps/apis/shared_office365"

def sp(op, params, run_after=None):
    return {"type": "OpenApiConnection",
            "inputs": {"host": {"connectionName": "shared_sharepointonline", "operationId": op, "apiId": SP_API},
                       "parameters": params, "authentication": "@parameters('$authentication')"},
            "runAfter": run_after or {}}

def http(method, uri, headers=None, body=None, run_after=None, **extra):
    i = {"method": method, "uri": uri}
    if headers: i["headers"] = headers
    if body is not None: i["body"] = body
    i.update(extra)
    return {"type": "Http", "inputs": i, "runAfter": run_after or {},
            "runtimeConfiguration": {"secureData": {"properties": ["inputs"]}}}

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
live = "equals(toLower(%s),'false')" % P("DryRun")
at_on = "equals(toLower(%s),'true')" % P("WriteAutotask")
IT = "items('Foreach_Batch')"
site, lst = P("SharePointSiteUrl"), P("SharePointListName")

page_url = ("@{%s}/configurations?filter[configuration_type_id]=@{items('Foreach_Type')}"
            "&filter[configuration_status_id]=@{%s}&filter[archived]=false"
            "&page[size]=1000&page[number]=@{variables('Page')}") % (P("ITGlueBaseUri"), P("ActiveStatusId"))
itg_headers = {"x-api-key": "@%s" % P("ITGlueApiKey"), "Accept": "application/vnd.api+json"}

def attr(name): return "item()?['attributes']?['%s']" % name

until_actions = chain({
    "Get_IT_Glue_page": http("GET", page_url, itg_headers),
    "Select_Clean": {"type": "Select", "inputs": {
        "from": "@body('Get_IT_Glue_page')?['data']",
        "select": {
            "id": "@item()?['id']",
            "org": "@" + attr("organization-name"),
            "name": "@" + attr("name"),
            "serial": "@" + attr("serial-number"),
            "rmm": "@string(coalesce(%s, ''))" % attr("rmm-id"),
            "psa": "@string(coalesce(%s, ''))" % attr("psa-id"),
            "glue": "@take(string(coalesce(%s, '')), 10)" % attr("warranty-expires-at"),
            "mitp": "@take(string(coalesce(item()?['attributes']?[%s], '')), 10)" % P("MyITPAttribute"),
        }}},
    "Set_PageCount": {"type": "SetVariable", "inputs": {"name": "PageCount", "value": "@length(body('Get_IT_Glue_page')?['data'])"}},
    "Merge_Devices": {"type": "SetVariable", "inputs": {"name": "Devices", "value": "@union(variables('Devices'), body('Select_Clean'))"}},
    "Next_page": {"type": "IncrementVariable", "inputs": {"name": "Page", "value": 1}},
}, ["Get_IT_Glue_page", "Select_Clean", "Set_PageCount", "Merge_Devices", "Next_page"])
for k in ("Select_Clean", "Set_PageCount", "Merge_Devices", "Next_page"):
    until_actions[k]["runAfter"] = {list(until_actions)[list(until_actions).index(k)-1]: ["Succeeded"]}

foreach_type_actions = {
    "Reset_Page": {"type": "SetVariable", "inputs": {"name": "Page", "value": 1}, "runAfter": {}},
    "Page_until_short": {"type": "Until", "expression": "@less(variables('PageCount'), 1000)",
                         "limit": {"count": 100, "timeout": "PT3H"}, "actions": until_actions,
                         "runAfter": {"Reset_Page": ["Succeeded"]}},
}

decided = {"id": "@item()?['id']", "org": "@item()?['org']", "name": "@item()?['name']", "serial": "@item()?['serial']",
           "rmm": "@item()?['rmm']", "psa": "@item()?['psa']", "glue": "@item()?['glue']", "mitp": "@item()?['mitp']",
           "target": ("@if(empty(item()?['mitp']), item()?['glue'], if(or(empty(item()?['glue']), "
                      "less(item()?['glue'], item()?['mitp'])), item()?['mitp'], item()?['glue']))"),
           "later": "@and(not(empty(item()?['mitp'])), greater(item()?['glue'], item()?['mitp']))"}

# per-device actions
sc_glue = {"type": "Scope", "actions": {"If_glue_changed": cond(
    "and(%s, not(equals(%s?['target'], %s?['glue'])))" % (live, IT, IT),
    {"ITGlue_write": http("PATCH", "@{%s}/configurations/@{%s?['id']}" % (P("ITGlueBaseUri"), IT),
        dict(itg_headers, **{"Content-Type": "application/vnd.api+json"}),
        {"data": {"type": "configurations", "attributes": {"warranty-expires-at": "@%s?['target']" % IT}}})})}}
sc_datto = {"type": "Scope", "actions": {"If_rmm_present": cond(
    "and(%s, not(empty(%s?['rmm'])))" % (live, IT),
    {"Datto_write": http("POST", "@{%s}/api/v2/device/@{%s?['rmm']}/warranty" % (P("DattoApiUrl"), IT),
        {"Authorization": "Bearer @{variables('DattoToken')}", "Content-Type": "application/json"},
        {"warrantyDate": "@%s?['target']" % IT})})}}
sc_at = {"type": "Scope", "actions": {"If_psa_present": cond(
    "and(%s, %s, not(empty(%s?['psa'])))" % (live, at_on, IT),
    {"Autotask_write": http("PATCH", "@{%s}/ConfigurationItems" % P("AutotaskBaseUri"),
        {"UserName": "@%s" % P("AutotaskUsername"), "Secret": "@%s" % P("AutotaskSecret"),
         "ApiIntegrationCode": "@%s" % P("AutotaskIntegrationCode"), "Content-Type": "application/json"},
        {"id": "@int(%s?['psa'])" % IT, "warrantyExpirationDate": "@%s?['target']" % IT})})}}

def failed(scope): return "equals(actions('%s')?['status'], 'Failed')" % scope
had_err = "or(%s, %s, %s)" % (failed("Scope_ITGlue"), failed("Scope_Datto"), failed("Scope_Autotask"))
missing = "or(empty(%s?['rmm']), and(%s, empty(%s?['psa'])))" % (IT, at_on, IT)
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
    "Scope_ITGlue": sc_glue, "Scope_Datto": sc_datto, "Scope_Autotask": sc_at,
    "Compose_HadError": {"type": "Compose", "inputs": "@" + had_err},
    "Compose_Missing": {"type": "Compose", "inputs": "@" + missing},
    "Compose_Status": {"type": "Compose", "inputs": (
        "@if(outputs('Compose_HadError'), 'Error', if(%s, 'Dry run', if(outputs('Compose_Missing'), 'Missing ID', "
        "if(%s?['later'], 'Glue later – review', 'OK'))))") % (dry, IT)},
    "Compose_Pushed": {"type": "Compose", "inputs": "@if(and(%s, not(outputs('Compose_HadError')), not(outputs('Compose_Missing'))), %s?['target'], %s)" % (live, IT, existing_pushed)},
    "Compose_Note": {"type": "Compose", "inputs": (
        "@concat(if(%s, 'IT Glue write failed. ', ''), if(%s, 'Datto RMM write failed. ', ''), "
        "if(%s, 'Autotask write failed. ', ''), if(empty(%s?['rmm']), 'rmm-id missing. ', ''), "
        "if(and(%s, empty(%s?['psa'])), 'psa-id missing. ', ''), if(or(%s, %s, %s), 'See flow run history.', ''))") % (
        failed("Scope_ITGlue"), failed("Scope_Datto"), failed("Scope_Autotask"), IT, at_on, IT,
        failed("Scope_ITGlue"), failed("Scope_Datto"), failed("Scope_Autotask"))},
    "If_row_exists": {"type": "If", "expression": {"equals": ["@greater(length(%s), 0)" % existing_row, True]},
        "actions": {"Update_row": sp("PatchItem", dict(row, id="@first(%s)?['ID']" % existing_row))},
        "else": {"actions": {"Create_row": sp("PostItem", row)}}, "runAfter": {}},
}, ["Get_State_row", "Scope_ITGlue", "Scope_Datto", "Scope_Autotask", "Compose_HadError", "Compose_Missing",
    "Compose_Status", "Compose_Pushed", "Compose_Note", "If_row_exists"])

state_str = "@concat(',', join(body('Select_State'), ','), ',')"
to_push = ("@and(not(empty(item()?['target'])), not(contains(outputs('StateString'), "
           "concat(',', item()?['id'], '|', item()?['target'], ','))))")

summary_body = ("<p>Warranty Sync finished (@{if(%s, 'LIVE', 'DRY RUN')}).</p><ul>"
    "<li>Devices read from IT Glue: @{length(body('Select_Decided'))}</li>"
    "<li>Changes pending: @{length(body('Filter_ToPush'))}</li>"
    "<li>Processed tonight: @{length(outputs('Compose_Batch'))}</li>"
    "<li>Errors tonight: @{length(body('Filter_Errors'))}</li></ul>"
    "<p><a href=\"@{%s}/Lists/WarrantySyncState/Needs%%20attention.aspx\">Needs attention view</a></p>") % (live, site)

actions = {
    "Init_RunStart": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "RunStart", "type": "string", "value": "@utcNow()"}]}},
    "Init_Devices": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "Devices", "type": "array", "value": []}]}},
    "Init_Page": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "Page", "type": "integer", "value": 1}]}},
    "Init_PageCount": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "PageCount", "type": "integer", "value": 0}]}},
    "Init_DattoToken": {"type": "InitializeVariable", "inputs": {"variables": [{"name": "DattoToken", "type": "string", "value": ""}]}},
    "Compose_TypeIds": {"type": "Compose", "inputs": "@createArray(%s, %s)" % (P("LaptopTypeId"), P("DesktopTypeId"))},
    "If_live_get_token": cond(live, {
        "Get_Datto_token": http("POST", "@{%s}/auth/oauth/token" % P("DattoApiUrl"),
            {"Content-Type": "application/x-www-form-urlencoded"},
            "grant_type=password&username=@{encodeUriComponent(%s)}&password=@{encodeUriComponent(%s)}" % (P("DattoApiKey"), P("DattoApiSecret")),
            authentication={"type": "Basic", "username": "public-client", "password": "public"},
            ) | {"runtimeConfiguration": {"secureData": {"properties": ["inputs", "outputs"]}}},
        "Set_DattoToken": {"type": "SetVariable", "inputs": {"name": "DattoToken", "value": "@body('Get_Datto_token')?['access_token']"},
                           "runAfter": {"Get_Datto_token": ["Succeeded"]},
                           "runtimeConfiguration": {"secureData": {"properties": ["inputs", "outputs"]}}}}),
    "Foreach_Type": {"type": "Foreach", "foreach": "@outputs('Compose_TypeIds')", "actions": foreach_type_actions,
                     "runtimeConfiguration": {"concurrency": {"repetitions": 1}}},
    "Select_Decided": {"type": "Select", "inputs": {"from": "@variables('Devices')", "select": decided}},
    "Get_State_items": sp("GetItems", {"dataset": "@" + site, "table": "@" + lst, "$select": "Title,PushedDate", "$top": 5000}),
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
    actions[n]["runtimeConfiguration"] = {"paginationPolicy": {"minimumItemCount": 100000}}
# top-level chain. Foreach_Batch / summary run regardless of per-device failures.
order = ["Init_RunStart", "Init_Devices", "Init_Page", "Init_PageCount", "Init_DattoToken", "Compose_TypeIds",
         "If_live_get_token", "Foreach_Type", "Select_Decided", "Get_State_items", "Select_State", "StateString",
         "Filter_ToPush", "Compose_Batch", "Foreach_Batch", "Get_run_rows", "Filter_Errors", "Send_summary"]
prev = None
for n in order:
    actions[n]["runAfter"] = {prev: (ALL if n in ("Get_run_rows", "Filter_Errors", "Send_summary") else ["Succeeded"])} if prev else {}
    prev = n

params = {"$connections": {"defaultValue": {}, "type": "Object"},
          "$authentication": {"defaultValue": {}, "type": "SecureObject"}}
for k, (disp, dflt, _, _) in ENV.items():
    params["%s (%s_%s)" % (disp, PREFIX, k)] = {"defaultValue": dflt or "", "type": "String",
                                                "metadata": {"schemaName": "%s_%s" % (PREFIX, k)}}

flow = {"properties": {
    "connectionReferences": {
        "shared_sharepointonline": {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": PREFIX + "_sharedsharepointonline"},
                                    "api": {"name": "shared_sharepointonline"}},
        "shared_office365": {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": PREFIX + "_sharedoffice365"},
                             "api": {"name": "shared_office365"}}},
    "definition": {"$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
                   "contentVersion": "1.0.0.0", "parameters": params,
                   "triggers": {"Recurrence": {"type": "Recurrence", "recurrence": {
                       "frequency": "Day", "interval": 1, "timeZone": "GMT Standard Time",
                       "schedule": {"hours": ["1"], "minutes": [0]}}}},
                   "actions": actions},
    "templateName": None}, "schemaVersion": "1.0.0.0"}

# ---- solution files ---------------------------------------------------------------------------
def lbl(tag, text, extra=""):
    return '<%s default=%s><label description=%s languagecode="1033" /></%s>' % (tag, quoteattr(text), quoteattr(text), tag)

def write(path, text):
    full = os.path.join(SRC, path); os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as f: f.write(text)

shutil.rmtree(SRC, ignore_errors=True)
guid_u = WF_ID.upper()
wf_file = "/Workflows/WarrantySyncNightly-%s.json" % guid_u
write(wf_file.lstrip("/"), json.dumps(flow, indent=2, ensure_ascii=False))
write("[Content_Types].xml", '<?xml version="1.0" encoding="utf-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="xml" ContentType="application/octet-stream" /><Default Extension="json" ContentType="application/octet-stream" /></Types>')

roots = ['<RootComponent type="29" id="{%s}" behavior="0" />' % WF_ID]
for k in ENV: roots.append('<RootComponent type="380" schemaName="%s_%s" behavior="0" />' % (PREFIX, k))
for c in ("sharedsharepointonline", "sharedoffice365"): roots.append('<RootComponent type="372" schemaName="%s_%s" behavior="0" />' % (PREFIX, c))
write("solution.xml", '<?xml version="1.0" encoding="utf-8"?>\n<ImportExportXml version="9.2.0.0" SolutionPackageVersion="9.2" languagecode="1033" generatedBy="CrmLive" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
      '<SolutionManifest><UniqueName>WarrantySync</UniqueName><LocalizedNames><LocalizedName description="Warranty Sync" languagecode="1033" /></LocalizedNames><Descriptions />'
      '<Version>1.0.0.0</Version><Managed>0</Managed><Publisher><UniqueName>affinityit</UniqueName><LocalizedNames><LocalizedName description="Affinity IT Services" languagecode="1033" /></LocalizedNames><Descriptions />'
      '<EMailAddress xsi:nil="true"></EMailAddress><SupportingWebsiteUrl xsi:nil="true"></SupportingWebsiteUrl><CustomizationPrefix>%s</CustomizationPrefix><CustomizationOptionValuePrefix>10000</CustomizationOptionValuePrefix><Addresses><Address><AddressNumber>1</AddressNumber><AddressTypeCode>1</AddressTypeCode></Address><Address><AddressNumber>2</AddressNumber><AddressTypeCode>1</AddressTypeCode></Address></Addresses></Publisher>'
      '<RootComponents>%s</RootComponents><MissingDependencies /></SolutionManifest></ImportExportXml>' % (PREFIX, "".join(roots)))

def connref(logical, disp, api):
    return ('<connectionreference connectionreferencelogicalname="%s_%s"><connectionreferencedisplayname>%s</connectionreferencedisplayname>'
            '<connectorid>/providers/Microsoft.PowerApps/apis/%s</connectorid><iscustomizable>1</iscustomizable><promptingbehavior>0</promptingbehavior>'
            '<statecode>0</statecode><statuscode>1</statuscode></connectionreference>') % (PREFIX, logical, disp, api)
write("customizations.xml", '<?xml version="1.0" encoding="utf-8"?>\n<ImportExportXml xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
      '<Entities></Entities><Roles></Roles><Workflows><Workflow WorkflowId="{%s}" Name="%s"><JsonFileName>%s</JsonFileName><Type>1</Type><Subprocess>0</Subprocess>'
      '<Category>5</Category><Mode>0</Mode><Scope>4</Scope><OnDemand>0</OnDemand><TriggerOnCreate>0</TriggerOnCreate><TriggerOnDelete>0</TriggerOnDelete>'
      '<AsyncAutodelete>0</AsyncAutodelete><SyncWorkflowLogOnFailure>0</SyncWorkflowLogOnFailure><StateCode>0</StateCode><StatusCode>1</StatusCode><RunAs>1</RunAs>'
      '<IsTransacted>1</IsTransacted><IntroducedVersion>1.0</IntroducedVersion><IsCustomizable>1</IsCustomizable><BusinessProcessType>0</BusinessProcessType>'
      '<IsCustomProcessingStepAllowedForOtherPublishers>1</IsCustomProcessingStepAllowedForOtherPublishers><PrimaryEntity>none</PrimaryEntity>'
      '<LocalizedNames><LocalizedName languagecode="1033" description="%s" /></LocalizedNames></Workflow></Workflows>'
      '<FieldSecurityProfiles></FieldSecurityProfiles><Templates /><EntityMaps /><EntityRelationships /><OrganizationSettings /><optionsets /><CustomControls /><EntityDataProviders />'
      '<connectionreferences>%s%s</connectionreferences><Languages><Language>1033</Language></Languages></ImportExportXml>' % (
      WF_ID, FLOW_NAME, wf_file, FLOW_NAME, connref("sharedsharepointonline", "SharePoint", "shared_sharepointonline"),
      connref("sharedoffice365", "Office 365 Outlook", "shared_office365")))

for k, (disp, dflt, req, desc) in ENV.items():
    write("environmentvariabledefinitions/%s_%s/environmentvariabledefinition.xml" % (PREFIX, k),
          '<?xml version="1.0" encoding="utf-8"?>\n<environmentvariabledefinition schemaname="%s_%s">%s<introducedversion>1.0</introducedversion>'
          '<iscustomizable>1</iscustomizable><isrequired>%d</isrequired>%s%s<type>100000000</type></environmentvariabledefinition>' % (
          PREFIX, k, ("<defaultvalue>%s</defaultvalue>" % escape(dflt)) if dflt else "", 1 if req else 0,
          lbl("displayname", disp), lbl("description", desc or disp)))

if os.path.exists(ZIP): os.remove(ZIP)
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(SRC):
        for f in sorted(files):
            full = os.path.join(root, f); z.write(full, os.path.relpath(full, SRC).replace(os.sep, "/"))
print("wrote", ZIP)
