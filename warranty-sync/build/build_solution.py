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

PREFIX = "new"  # matches the environment default publisher (see PUBLISHER below)
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
        "shared_sharepointonline": {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": PREFIX + "_sharedsharepointonline_wsync"},
                                    "api": {"name": "shared_sharepointonline"}},
        "shared_office365": {"runtimeSource": "embedded", "connection": {"connectionReferenceLogicalName": PREFIX + "_sharedoffice365_wsync"},
                             "api": {"name": "shared_office365"}}},
    "definition": {"$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
                   "contentVersion": "1.0.0.0", "parameters": params,
                   "triggers": {"Recurrence": {"type": "Recurrence", "recurrence": {
                       "frequency": "Day", "interval": 1, "timeZone": "GMT Standard Time",
                       "schedule": {"hours": ["1"], "minutes": [0]}}}},
                   "actions": actions},
    "templateName": None}, "schemaVersion": "1.0.0.0"}

# ---- solution files ---------------------------------------------------------------------------
# Layout, element order, CRLF endings and the publisher mirror a solution exported from the target
# environment (AutotaskaBillitySync_1_0_0_13.zip), which is known to import.
ORGVER = 'OrganizationVersion="9.2.26102.127" OrganizationSchemaType="Standard" CRMServerServiceabilityVersion="9.2.26102.00127"'
PUBLISHER = ("DefaultPublisherorgda80817a", "Default Publisher for orgda80817a", "Default publisher for this organization")
VERSION = "1.0.0.0"

def write(path, text, bom=False):
    full = os.path.join(SRC, path); os.makedirs(os.path.dirname(full), exist_ok=True)
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    with open(full, "wb") as f: f.write((b"\xef\xbb\xbf" if bom else b"") + data)

def label(tag, text, ind):
    return ('%s<%s default=%s>\n%s  <label description=%s languagecode="1033" />\n%s</%s>\n'
            % (ind, tag, quoteattr(text), ind, quoteattr(text), ind, tag))

shutil.rmtree(SRC, ignore_errors=True)
wf_file = "/Workflows/WarrantySync-Nightly-%s.json" % WF_ID.upper()
write(wf_file.lstrip("/"), json.dumps(flow, indent=2, ensure_ascii=False))
write("[Content_Types].xml", '<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="xml" ContentType="application/octet-stream" /><Default Extension="json" ContentType="application/octet-stream" /></Types>', bom=True)

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
 '    <RootComponents>\n      <RootComponent type="29" id="{%s}" behavior="0" />\n    </RootComponents>\n' % WF_ID +
 '    <MissingDependencies />\n  </SolutionManifest>\n</ImportExportXml>')

def connref(logical, disp, api):
    return ('    <connectionreference connectionreferencelogicalname="%s">\n'
            '      <connectionreferencedisplayname>%s</connectionreferencedisplayname>\n'
            '      <connectorid>/providers/Microsoft.PowerApps/apis/%s</connectorid>\n'
            '      <iscustomizable>1</iscustomizable>\n      <promptingbehavior>0</promptingbehavior>\n'
            '      <statecode>0</statecode>\n      <statuscode>1</statuscode>\n    </connectionreference>\n') % (logical, escape(disp), api)

write("customizations.xml",
 '<ImportExportXml xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" %s>\n' % ORGVER +
 '  <Entities></Entities>\n  <Roles></Roles>\n  <Workflows>\n'
 '    <Workflow WorkflowId="{%s}" Name=%s>\n' % (WF_ID, quoteattr(FLOW_NAME)) +
 '      <JsonFileName>%s</JsonFileName>\n' % wf_file +
 '      <Type>1</Type>\n      <Subprocess>0</Subprocess>\n      <Category>5</Category>\n      <Mode>0</Mode>\n      <Scope>4</Scope>\n'
 '      <OnDemand>0</OnDemand>\n      <TriggerOnCreate>0</TriggerOnCreate>\n      <TriggerOnDelete>0</TriggerOnDelete>\n'
 '      <AsyncAutodelete>0</AsyncAutodelete>\n      <SyncWorkflowLogOnFailure>0</SyncWorkflowLogOnFailure>\n'
 '      <StateCode>0</StateCode>\n      <StatusCode>1</StatusCode>\n      <RunAs>1</RunAs>\n      <IsTransacted>1</IsTransacted>\n'
 '      <IntroducedVersion>%s</IntroducedVersion>\n      <IsCustomizable>1</IsCustomizable>\n' % VERSION +
 '      <IsCustomProcessingStepAllowedForOtherPublishers>1</IsCustomProcessingStepAllowedForOtherPublishers>\n'
 '      <ModernFlowType>0</ModernFlowType>\n      <PrimaryEntity>none</PrimaryEntity>\n'
 '      <LocalizedNames>\n        <LocalizedName languagecode="1033" description=%s />\n      </LocalizedNames>\n' % quoteattr(FLOW_NAME) +
 '    </Workflow>\n  </Workflows>\n  <FieldSecurityProfiles></FieldSecurityProfiles>\n  <Templates />\n  <EntityMaps />\n'
 '  <EntityRelationships />\n  <OrganizationSettings />\n  <optionsets />\n  <CustomControls />\n  <EntityDataProviders />\n'
 '  <connectionreferences>\n' +
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
          '  <secretstore>0</secretstore>\n  <type>100000000</type>\n</environmentvariabledefinition>' % (VERSION, 1 if req else 0))
    write("environmentvariabledefinitions/%s_%s/environmentvariabledefinition.xml" % (PREFIX, k), x)

if os.path.exists(ZIP): os.remove(ZIP)
names = []
for root, _, files in os.walk(SRC):
    for f in files:
        names.append(os.path.relpath(os.path.join(root, f), SRC).replace(os.sep, "/"))
first = ["customizations.xml", "solution.xml"]
last = ["[Content_Types].xml"]
names = first + sorted(n for n in names if n.startswith("Workflows/")) + \
        sorted(n for n in names if n.startswith("environmentvariabledefinitions/")) + last
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
    for n in names:
        zi = zipfile.ZipInfo(n, date_time=(2026, 11, 2, 0, 0, 0)); zi.compress_type = zipfile.ZIP_DEFLATED
        with open(os.path.join(SRC, n), "rb") as fh: z.writestr(zi, fh.read())
print("wrote", ZIP)
