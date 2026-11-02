# Warranty Sync – Nightly: flow build sheet

1. **Recurrence** daily, finishing ≥2h before the first Datto RMM report.
2. **Get Datto token**: Datto RMM (Warranty Sync) connector `GetToken`, secure inputs/outputs.
3. **IT Glue list**: Apply to each over [LaptopTypeId, DesktopTypeId] (concurrency 1); Do until page returns <1000.
   `GET /configurations?filter[configuration_type_id]=<id>&filter[configuration_status_id]=<ActiveStatusId>&filter[archived]=false&page[size]=1000&page[number]=<n>`; merge with `union()`.
4. **Select "Clean"**
   - id `item()?['id']`, org `item()?['attributes']?['organization-name']`, name `...['name']`, serial `...['serial-number']`
   - rmm `...['rmm-id']`, psa `...['psa-id']`
   - glue `take(coalesce(item()?['attributes']?['warranty-expires-at'], ''), 10)`
   - mitp `take(coalesce(item()?['attributes']?['<myITProcess attribute>'], ''), 10)`
5. **Select "Decided"**
   - target `if(empty(item()?['mitp']), item()?['glue'], if(or(empty(item()?['glue']), less(item()?['glue'], item()?['mitp'])), item()?['mitp'], item()?['glue']))`
   - later `and(not(empty(item()?['mitp'])), greater(item()?['glue'], item()?['mitp']))`
6. **Get items** from Warranty Sync State (pagination 10,000; Title, PushedDate). Select(text) `concat(item()?['Title'], '|', item()?['PushedDate'])`; Compose `StateString` = `concat(',', join(body('Select_state'), ','), ',')`.
7. **Filter array "ToPush"**: `and(not(empty(item()?['target'])), not(contains(outputs('StateString'), concat(',', item()?['id'], '|', item()?['target'], ','))))`
8. **Compose "Batch"**: `take(body('ToPush'), int(variables('MaxWritesPerRun')))`
9. **Apply to each Batch** (concurrency 5), when DryRun = false:
   - target ≠ glue → IT Glue `PATCH /configurations/{id}` `{"data":{"type":"configurations","attributes":{"warranty-expires-at":"<target>"}}}`
   - rmm not empty → Datto `SetDeviceWarranty` `{"warrantyDate":"<target>"}`
   - WriteAutotask and psa → Autotask `PATCH /ConfigurationItems` `{"id": <psa>, "warrantyExpirationDate":"<target>"}`
   - Upsert State row (run after success/failed/skipped): `PushedDate = target` only if all writes OK and both IDs present; Status per date rules.
   - DryRun = true → upsert with Status `Dry run`, no PushedDate.
10. **Summary** email/Teams: device count, pending, written, errors, link to *Needs attention* view.
