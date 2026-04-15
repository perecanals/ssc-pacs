> **Historical document.** Early bootstrap plan; the deployed system differs (e.g. `orthancteam/orthanc`, Folder Indexer, native Companion). For current truth see [`../reference/architecture.md`](../reference/architecture.md) and [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md).

# Implementation Plan: Lightweight PACS Setup (Orthanc + OHIF + PostgreSQL)

## 1. Context & Objective
We are setting up a lightweight, developer-friendly PACS system on a remote server to manage a large, anonymized DICOM database of stroke patients. 

Currently, we have:
* A PostgreSQL server running a custom database.
* A custom SQL table that maps Patient IDs, unique stroke episodes, and absolute file paths to DICOM directories. The table is called "flowcat_images_series". You can read it to understand , but can't update it in any circumstance. You can create a new table if you want. 
* Raw DICOM files stored on the server's filesystem.

**Goal:** Deploy Orthanc (with the OHIF web viewer plugin) using Docker, configure it to use our existing PostgreSQL server for its internal indexing, and write a Python script to ingest our existing DICOM files into Orthanc based on our custom SQL table.

## 2. Target Architecture
* **PACS Server:** Orthanc (using the `osimis/orthanc` Docker image for pre-packaged plugins).
* **Database:** PostgreSQL (Orthanc will get its own dedicated schema/database alongside our custom one).
* **Web UI:** OHIF Viewer (served directly via Orthanc plugin).
* **Ingestion:** A Python script that reads our custom PostgreSQL table and pushes DICOM files to Orthanc's REST API.

---

## 3. Implementation Steps for the AI Agent

Please help me implement this system by executing the following phases in order:

### Phase 1: Docker Setup (`docker-compose.yml`)
Create a `docker-compose.yml` file to spin up Orthanc.
* Use the `osimis/orthanc:latest` image.
* Expose port `8042` (Orthanc REST API / Web UI) and `4242` (DICOM port).
* Set up the necessary environment variables to configure Orthanc:
  * Enable the PostgreSQL plugin.
  * Enable the OHIF plugin.
  * Set up database connection strings (pointing to our host's Postgres server, assume standard port `5432`).
  * Set up default credentials for the Orthanc web interface.
* Map a volume for Orthanc's internal storage (`/var/lib/orthanc/db/`).

### Phase 2: Database Initialization Guide
Provide a short SQL script or instructions on what I need to run on my existing PostgreSQL server to create the dedicated database and user for Orthanc (e.g., `orthanc_db` and `orthanc_user`).

### Phase 3: Python Ingestion Script (`ingest_dicoms.py`)
Write a Python script to migrate our existing data into Orthanc.
* **Dependencies:** Use `psycopg2` (or `sqlalchemy`) for database access and `requests` for hitting the Orthanc API.
* **Logic:**
  1. Connect to the custom PostgreSQL database.
  2. Query the custom table to get the paths to the DICOM directories.
  3. Traverse the directories to find the dicom files. Sometimes they end in '.dcm', sometimes the do not (usually then they start by 'IMG' followed by a 5-digit number)
  4. POST each DICOM file to the Orthanc REST API (`http://localhost:8042/instances`).
* **Requirements:** * Include basic error handling (e.g., if a file path is dead, log it and continue).
  * Use a session object in `requests` for performance.
  * Add a basic progress indicator or logging so we can track the upload.

### Phase 4: Verification
Provide brief instructions on how to test that the system is running, check the OHIF viewer in the browser, and verify that the Python script successfully pushed a test batch of files.

One very important aspect is that I am constraint in my server in terms of space, so I cannot afford to duplicate the data for the PACS server, it will have to rely on indexing. The PostgreSQL table contains a few interesing columns. You can check the table description in /media/Disk_B/databases/flowcat_database/management/test_pacs/flowcat_image_series_table_description.json. The most interesting columns are:
- nhc: global patient identifier
- proces_id: unique identifier for the stroke episode
- study_type: type of study (e.g. 'BASAL', 'THROMBECTOMY', 'FOLLOW_UP', 'OTHER')
- studyinstanceuid: unique identifier for the study
- seriesinstanceuid: unique identifier for the series
- seriesdescription: description of the series from the dicom tag
- seriesdescription_: description of the series from the dicom tag, with additional series that were originally anonymized, including the series description.
- dicomdir_path: absolute path to the dicom folder containing the slice files
- nifti_path: absolute path to the nifti file, if available (not in all series)
- modality: modality of the series (e.g. 'MR', 'CT', 'US'...)