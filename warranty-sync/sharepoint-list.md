# SharePoint list: Warranty Sync State

One row per device. Same columns as the JSON state file used by the PowerShell script.

| Column | Type | Purpose |
|---|---|---|
| Title | Text (**indexed**) | IT Glue configuration ID |
| Organisation, Device, Serial | Text | For people reading the list |
| MyITPDate, GlueDate | Date | Inputs at last run |
| PushedDate | Date | Date last written to every system; drives "only changes" |
| Status | Choice | OK, Glue later – review, Missing ID, Error, Dry run |
| Note, LastRun | Text, Date | Error detail; last processed |

Add a view **Needs attention** filtered to `Status ≠ OK`.

Environment variables (solution): DryRun, LaptopTypeId, DesktopTypeId, ActiveStatusId, MaxWritesPerRun (1500), WriteAutotask (Yes).
