# PACS documentation context

One **Orthanc** container (`ssc-orthanc`), one **Companion** service on the host (**systemd**, port `8043`), and **two logical PostgreSQL databases** (`orthanc_db` + `stanford-stroke`).

**Give an agent only what it needs:**

| Topic | Doc |
|--------|-----|
| **End-to-end system overview (start here)** | [`reference/system_overview.md`](reference/system_overview.md) |
| Architecture / data flow | [`reference/architecture.md`](reference/architecture.md) |
| Runtime, config files, ports, scripts | [`reference/runtime_and_config.md`](reference/runtime_and_config.md) |
| Schema / tables / SQL behavior | [`reference/data_stores.md`](reference/data_stores.md) |
| Companion product + UI model | [`reference/companion.md`](reference/companion.md) |
| Companion React / `DataTable` detail | [`reference/companion_frontend.md`](reference/companion_frontend.md) |
| Image integration protocol (ingesting new data) | [`reference/image_integration_protocol.md`](reference/image_integration_protocol.md) |
| Fresh install | [`guides/installation_and_deployment.md`](guides/installation_and_deployment.md) |
| Day-2 commands | [`operations/commands.md`](operations/commands.md) |
| Backup strategy (Tier 1 active, Tier 2 dormant) | [`operations/backup_strategy.md`](operations/backup_strategy.md) |
| Restore runbook (DB recovery procedure) | [`operations/restore_runbook.md`](operations/restore_runbook.md) |
| Cold storage design | [`cold_storage/design.md`](cold_storage/design.md) |
| Cold storage operations | [`cold_storage/runbook.md`](cold_storage/runbook.md) |
| DICOM processing recipes (NIFTI, archive inspection, cleanup) | [`recipes/dicom_processing.md`](recipes/dicom_processing.md) |
| Old implementation plans | [`history/`](history/) |
| Dated changelog snapshot | [`history/current_state_changelog.md`](history/current_state_changelog.md) |
