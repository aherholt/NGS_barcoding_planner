# Open points

| # | Topic | Needed for | Status |
|---|---|---|---|
| 1 | Real well-barcode and index sequences imported (Excel → `import_reference`) | everything | to do |
| 2 | i5 orientation expected by the provider for NovaSeq X sample sheets | sample sheet | ask provider |
| 3 | Provider's guaranteed output per flow cell / lane option, and whether they sell lanes | capacity bars | ask provider |
| 4 | Does the provider accept the BCL Convert v2 sample sheet, or a template of their own? | provider export | ask provider |
| 5 | SharePoint list changes (read-length number columns, NGS run ID as text, status values) | sync | to do |
| 6 | Internal column names in `config/sharepoint_fields.json` | sync | run `sharepoint_columns` |
| 7 | Entra app registrations (sync + login) | sync, login | M365 admin |
| 8 | Colour-channel map for XLEAP-SBS in `PLANNER["TWO_CHANNEL_MAP"]` verified against Illumina docs | colour check | verify |
| 9 | PhiX defaults (1 % normal, 10 % low complexity) and 10 % safety margin agreed with provider | capacity | decide |
| 10 | Split of requested reads across pools: currently proportional to samples per pool | read targets | confirm |
| 11 | More than 24 pools or 10 culture plates per experiment → several STAR runs; not yet split automatically | robot file | later |
| 12 | Hostname / HTTPS certificate on the lab server | go-live | IT |
| 13 | Backup job and a tested restore | audit trail | IT |
