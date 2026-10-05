# Patient identity: datasets, enrollments and linking patients

How the stack tells patients apart, and how to declare that patients of two
datasets are the same person. Schema: [`../reference/data_stores.md`](../reference/data_stores.md)
(`dataset`, `patient`); access rules: [`../reference/architecture.md`](../reference/architecture.md) §5.4.

## The model (Alembic `0026_patient_identity`, `0028_study_membership`)

- **Dataset** — a registry row: an immutable `slug` (e.g. `crisp2-lvo`) and a
  display `name` (e.g. `CRISP2/LVO`). Ingestion, grants, filters and exports use
  the name; keys use the slug. Manage with `scripts/admin/manage_datasets.py`.
- **Enrollment** — a `patient` row: one patient id *in one dataset*, keyed
  `patient_key = <slug>__<patient_id>` (e.g. `crisp2-lvo__11-001`). A patient id
  is only unique within its dataset: `crisp2-lvo__11-001` and `precise__11-001`
  are two patients, and here they are in fact two different people.
- **Subject** — `subject_id`, shared by the enrollments of one person. An
  enrollment starts as its own subject (`subject_id = patient_key`); linking
  makes several enrollments one subject. It records **identity only**: linking
  does not share imaging.
- **Ownership** — every study/series is owned by exactly one enrollment
  (`image_study.patient_key`), the one that ingested it first, and carries its
  subject. Imaging is stored once, in the owner's tree —
  `<root>/<owner's slug>/<patient_id>/<StudyUID>` ([dataset layout](dataset_layout.md));
  the patient_id on imaging rows is the DICOM PatientID / folder name.
- **Membership** — when a linked enrollment ingests a study whose
  StudyInstanceUID another enrollment of the same person already owns (the very
  same object), it becomes a *member* of that study (`study_member`). The study
  keeps its owner and files.
- **Visibility** — an enrollment sees the studies it owns plus those it is a
  member of: the `study_visibility` view (owners ∪ members), the one definition
  every reader uses.

So a dataset re-exported with **new UIDs** (e.g. OUTER LIMITS copies of
CRISP2/LVO studies) keeps its own copies: each enrollment shows only its own
studies, and the copies are stored twice. Only exact same-UID studies are shared.

What linking changes — and what it does not:

| | Shared between linked enrollments | Separate per enrollment |
|---|---|---|
| Identity (`subject_id`) | yes | |
| Imaging (studies, series, OHIF) | only same-UID studies (membership) | yes — each sees what it owns + its memberships |
| Study / series labels | yes, on a shared study (they describe the imaging) | |
| Timepoints, episodes, series ranks (`NCCT_1`) | | yes — computed per owning enrollment; a shared study keeps its owner's |
| Clinical timepoint anchor (CRISP2 puncture time) | yes — a fact about the person | |
| `patient.stroke_date` | | yes — earliest study the enrollment sees (NULL when none) |
| Patient labels | | yes — each dataset keeps its own |
| Patient rows, ids | | yes — one row per identifier |
| Who sees what | | a user granted dataset A sees A's enrollments, A's patient labels and **the imaging A's enrollments see** — never B's identifiers, patient labels or B-only imaging |

In the web app a person enrolled in two datasets you can see appears as two
patient rows, each expanding to its own studies. A shared (same-UID) study lists
both ids (`11-001, OL-0001`) and both datasets in the Patient ID / Dataset
columns; patient labels shown on its rows are editable only when exactly one of
those enrollments is in view; otherwise the cell is read-only (hover for whose
value is whose) — edit it from the patient row. Data exports join a patient
table to the imaging each enrollment sees, so a shared study yields one row per
enrollment that sees it.

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

The same links as a JSON file — one dataset linked to one reference dataset —
which `link_patients.py link` accepts too:

```json
{
  "dataset": "OUTERLIMITS",
  "link_dataset": "CRISP2/LVO",
  "links": {"OL-0001": "2-076", "OL-0002": "2-118"}
}
```

**Linking together with ingestion.** When the new dataset's imaging is being
ingested anyway, put the JSON file's path in the ingestion YAML as
`patient_links`: the run applies the links once, before the first case, then
ingests. The keys must be the DICOM `PatientID`s the new files carry. Details:
[`../reference/image_ingestion_protocol.md`](../reference/image_ingestion_protocol.md).
At the end of the run the linked view is rebuilt, so same-UID studies the batch
re-sent appear under the re-sending enrollment too.

- The `link_*` enrollment must exist (ingested). The other is **created** when
  missing — an enrollment with no imaging of its own, `import_label =
  link:<file>`; it sees nothing until it ingests (its same-UID studies then
  become memberships).
- If it already exists, its whole subject joins the other's; each keeps the
  imaging it owns, nothing moves on disk, and no imaging is shared by the link
  itself.
- After `--apply` the [linked view](dataset_layout.md#linked-view) is rebuilt
  (when `[storage].linked_view_root` is set): it holds one symlink per membership.
- The run validates every row first (registered datasets, existing target, no
  self-links, no source listed twice) and writes nothing if any row fails. With
  `--apply` everything is one transaction, followed by re-deriving what follows
  from a person's imaging: stroke dates, series ranks, timepoints, the labelled
  mirrors.
- Grant the new dataset to its users (`manage_users.py set-datasets` or the
  `/admin` page) to make its imaging visible to them.

**Link before ingesting shared imaging.** If dataset B's export contains the
same studies (same StudyInstanceUIDs) that dataset A already ingested,
ingestion refuses them unless the two enrollments are linked: one study cannot
belong to two people, so an unlinked clash means either a missing link or a UID
clash in the source data. Once linked, re-sent studies keep their owner and
the owner's files, and B's enrollment becomes a member of them; B's studies with
new UIDs are owned by B's enrollment (and seen only by it). Linking *after*
ingestion is fine too: link, then re-run the ingestion for the refused cases.

### Undoing a link

```bash
# unlink.csv: dataset,patient_id
python scripts/admin/link_patients.py unlink unlink.csv            # dry-run
python scripts/admin/link_patients.py unlink unlink.csv --apply
```

The enrollment becomes its own person again, taking the imaging it owns; the
rest of the subject keeps the rest. Memberships between it and the others are
dropped (they were only valid for one person). Then **restart the web app**
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

**Files are not moved** by a split (or by any change of owner): the moved
studies keep working from their stored paths, under the previous owner's
folder. The script reports how many studies are now filed under another
enrollment's folder; relocate them in a maintenance window with
`scripts/migration/move_to_dataset_layout.py` ([dataset layout](dataset_layout.md)).

## Clinical data

Clinical data is per dataset (Alembic `0027`): each `dataset` row may register
one clinical table (`<slug with - as _>_clinical_data`, e.g.
`precise_clinical_data`), its patient-id column, its episode-date column and an
optional `timepoint_strategy`. A clinical row only ever matches enrollments of
its own dataset — the patient tab's episode date and ingestion's clinical match
never use it for a same-id patient elsewhere. The timepoint anchors (the CRISP2
puncture columns) are read only from the dataset with `timepoint_strategy =
crisp2_puncture` (production: `CRISP2/LVO`); a person linked to such an
enrollment gets its anchor for all of their imaging. Other datasets anchor on
each episode's thrombectomy study.

```bash
# Upload a CSV/Excel (dry-run: validates ids, dates, coverage; then --execute)
python scripts/admin/manage_datasets.py import-clinical --dataset PRECISE \
    --file precise.xlsx --id-column 'Patient ID' --date-column 'Stroke Date'
python scripts/admin/manage_datasets.py import-clinical --dataset PRECISE \
    --file precise.xlsx --id-column 'Patient ID' --date-column 'Stroke Date' --execute  # --replace to overwrite
python scripts/admin/manage_datasets.py clear-clinical --dataset PRECISE --execute  # unregister, keep table
```

The dry run reports missing/duplicate ids, unparseable dates, coverage ("N of M
enrolled patients have a row") and file ids not enrolled. Headers are
normalized to snake_case (`Patient ID` → `patient_id`) and the id column is
stored as text. An unknown dataset is offered for creation (same y/N prompt as
ingestion). `--execute` writes the table, a unique index on the id column,
SELECT grants copied from `patient`, and the registration in one transaction.

**Production cutover** (the former global table): adopt it, then remove the
retired `[web-app] clinical_episode_date_column` / `clinical_data_dataset` keys
from config.toml (they are ignored with a startup WARN) and restart the web app.

```bash
python scripts/admin/manage_datasets.py import-clinical --dataset CRISP2/LVO \
    --from-table clinical_data --id-column study_id --date-column stroke_date \
    --timepoint-strategy crisp2_puncture --execute
```

`--from-table` renames the table to the convention name
(`crisp2_lvo_clinical_data`). Because it was named `clinical_data`, a
compatibility **view** `clinical_data` (`SELECT *`, same SELECT grants) is left
so researchers' existing queries keep working — deprecated, to be dropped in a
later release; `--replace` rebuilds it.

## Checking

```bash
python scripts/admin/manage_datasets.py list                 # datasets, enrollments, grants, clinical table
python scripts/data_integrity/reconcile.py                   # incl. ownership mismatches
```

Linked people, from SQL:

```sql
SELECT subject_id, array_agg(patient_key ORDER BY patient_key) AS enrollments
FROM patient GROUP BY subject_id HAVING count(*) > 1;
-- which studies each enrollment sees (owner or member)
SELECT patient_key, count(*) FILTER (WHERE is_owner) AS owned,
       count(*) FILTER (WHERE NOT is_owner) AS shared
FROM study_visibility GROUP BY patient_key ORDER BY patient_key;
```

Scripts' `--patient` options take a `patient_key`, or a bare patient id when it
names one person; a bare id shared by different people is refused with the keys
to choose from.
