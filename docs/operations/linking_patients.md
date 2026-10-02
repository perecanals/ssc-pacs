# Patient identity: datasets, enrollments and linking patients

How the stack tells patients apart, and how to declare that patients of two
datasets are the same person. Schema: [`../reference/data_stores.md`](../reference/data_stores.md)
(`dataset`, `patient`); access rules: [`../reference/architecture.md`](../reference/architecture.md) §5.4.

## The model (Alembic `0026_patient_identity`)

- **Dataset** — a registry row: an immutable `slug` (e.g. `crisp2-lvo`) and a
  display `name` (e.g. `CRISP2/LVO`). Ingestion, grants, filters and exports use
  the name; keys use the slug. Manage with `scripts/admin/manage_datasets.py`.
- **Enrollment** — a `patient` row: one patient id *in one dataset*, keyed
  `patient_key = <slug>__<patient_id>` (e.g. `crisp2-lvo__11-001`). A patient id
  is only unique within its dataset: `crisp2-lvo__11-001` and `precise__11-001`
  are two patients, and here they are in fact two different people.
- **Subject** — `subject_id`, shared by the enrollments of one person. An
  enrollment starts as its own subject (`subject_id = patient_key`); linking
  makes several enrollments one subject.
- **Ownership** — every study/series is owned by exactly one enrollment
  (`image_study.patient_key`), the one that ingested it first, and carries its
  subject. Imaging is stored once, in the owner's tree; the patient_id on
  imaging rows is the DICOM PatientID / folder name.

What linking changes — and what it does not:

| | Shared across a subject's enrollments | Separate per enrollment |
|---|---|---|
| Imaging (studies, series, OHIF) | yes — visible from every enrollment | |
| Study / series labels | yes (they describe the imaging) | |
| Timepoints, episodes, series ranks (`NCCT_1`) | yes — computed per subject | |
| Patient labels | | yes — each dataset keeps its own |
| Patient rows, ids | | yes — one row per identifier |
| Who sees what | a user granted dataset A sees A's enrollments, A's patient labels, and **all imaging of those people** (including imaging another dataset ingested) — never B's identifiers or patient labels | |

In the web app a person enrolled in two datasets you can see appears as two
patient rows; their studies list both ids (`11-001, OL-0001`) and both datasets
in the Patient ID / Dataset columns. Patient labels shown on a study/series row
are editable only when exactly one enrollment of that person is in view;
otherwise the cell is read-only (hover for whose value is whose) — edit it from
the patient row. Data exports produce one row per enrollment when a patient
table is joined to imaging.

## Linking patients (`scripts/admin/link_patients.py`)

The linkage route is table-driven. Prepare a CSV (or Excel) table:

```csv
dataset,patient_id,link_dataset,link_patient_id
OUTERLIMITS,OL-0001,CRISP2/LVO,2-076
OUTERLIMITS,OL-0002,CRISP2/LVO,2-118
```

Each row reads "`OUTERLIMITS/OL-0001` is the same person as the already-ingested
`CRISP2/LVO/2-076`". Then:

```bash
python scripts/admin/manage_datasets.py add --slug outerlimits --name OUTERLIMITS   # once
python scripts/admin/link_patients.py link outerlimits_links.csv            # dry-run: per-row plan
python scripts/admin/link_patients.py link outerlimits_links.csv --apply
```

- The `link_*` enrollment must exist (ingested). The other is **created** when
  missing — an enrollment with no imaging of its own, `import_label =
  link:<file>` — so a dataset that only *references* existing imaging needs no
  ingestion at all.
- If it already exists, its whole subject — including any imaging it owns —
  joins the other's; nothing moves on disk.
- The run validates every row first (registered datasets, existing target, no
  self-links, no source listed twice) and writes nothing if any row fails. With
  `--apply` everything is one transaction, followed by re-deriving what follows
  from a person's imaging: stroke dates, series ranks, timepoints, the labelled
  mirrors.
- Grant the new dataset to its users (`manage_users.py set-datasets` or the
  `/admin` page) to make the linked imaging visible to them.

**Link before ingesting shared imaging.** If dataset B's export contains the
same studies (same StudyInstanceUIDs) that dataset A already ingested,
ingestion refuses them unless the two enrollments are linked: one study cannot
belong to two people, so an unlinked clash means either a missing link or a UID
clash in the source data. Once linked, re-sent studies keep their owner and
the owner's files; B's genuinely new studies are owned by B's enrollment.

### Undoing a link

```bash
# unlink.csv: dataset,patient_id
python scripts/admin/link_patients.py unlink unlink.csv            # dry-run
python scripts/admin/link_patients.py unlink unlink.csv --apply
```

The enrollment becomes its own person again, taking the imaging it owns; the
rest of the subject keeps the rest. Then **restart the web app**
(`sudo systemctl restart ssc-web-app`): its DICOMweb access cache otherwise keeps
the old cross-dataset access for up to 5 minutes.

## Separating patients merged before `0026`

Before `0026`, ingesting the same patient id into a second dataset merged it into
the existing patient (both datasets on one row). The migration turned each such
row into linked enrollments — the faithful reading of the old data. Where the
shared id was a coincidence (production: the 12 CRISP2/LVO + PRECISE ids
`11-001`…`11-010`, `12-001`, `12-002`), split them by the import label of the
studies each dataset brought:

```bash
python scripts/migration/split_merged_patients.py --dataset PRECISE --import-label-like 'precise\_%'
python scripts/migration/split_merged_patients.py --dataset PRECISE --import-label-like 'precise\_%' --apply
python scripts/data_integrity/reconcile.py          # ownership mismatches must be 0
```

Patient labels stay with the enrollment they were recorded on (there, the
CRISP2/LVO one). Restart the web app afterwards.

## Clinical data

`clinical_data.study_id` holds one dataset's patient ids but has no dataset
column. Set config.toml `[web-app] clinical_data_dataset` to that dataset's name
(production: `CRISP2/LVO`): the patient tab's episode date, the timepoint
anchors and ingestion's clinical match then only use clinical rows for that
dataset's enrollments, never for a same-id patient elsewhere. A person linked
to such an enrollment gets its anchor for all of their imaging.

## Checking

```bash
python scripts/admin/manage_datasets.py list                 # datasets, enrollments, grants
python scripts/data_integrity/reconcile.py                   # incl. ownership mismatches
```

Linked people, from SQL:

```sql
SELECT subject_id, array_agg(patient_key ORDER BY patient_key) AS enrollments
FROM patient GROUP BY subject_id HAVING count(*) > 1;
```

Scripts' `--patient` options take a `patient_key`, or a bare patient id when it
names one person; a bare id shared by different people is refused with the keys
to choose from.
