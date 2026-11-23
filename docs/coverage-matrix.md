# Downstream app testing coverage matrix

This matrix maps the testing and debugging workflows of a downstream lab app to `benchling-tools` (issue #108). It is the inventory for the post-0.7.0 coverage pass. The 0.5.0 workflow set is already covered and is not repeated here.

The downstream app writes to production through the official Benchling SDK. `benchling-tools` is the fast path on the shared development tenant: explore it, check a payload before the app sends it, push a small test batch, read the result back, and find out why a push failed. Each CLI command is one MCP tool named after its command path, so one row covers both interfaces.

Status values:

- **covered**: a command does the job today.
- **partial**: a command does part of the job, or the job works with a known pain point (naming, errors, or read-back).
- **gap**: no command does the job.

Nothing here changes the safety model. Keychain auth, `--allow-writes`, protected environments, and preview-then-apply stay as they are. Every proposed command in the gap list is read-only. All IDs in this file are synthetic placeholders.

## Matrix

### Setup and connectivity

| Workflow | CLI command | MCP tool | Status | Notes |
|---|---|---|---|---|
| Check that the dev environment is saved and its credentials work | `auth status --check`, `doctor` | `auth_status`, `doctor` | covered | `doctor` exits 3 with one hint per failed check. |
| Find the user behind a record | `users get`, `users list`, `entry list --creator` | `users_get`, `users_list`, `entry_list` | covered | The app token is not a user identity, so app-created records name the app. |

### Entities

| Workflow | CLI command | MCP tool | Status | Notes |
|---|---|---|---|---|
| Read an entity that a result links to (custom entity, DNA or AA sequence) by ID | none | none | gap (G1) | The result planner already reads V3 `GET /custom-entity/{id}`, `/dna-sequence/{id}` and `/aa-sequence/{id}` to check link schemas, but no command exposes the record. |
| Find a test entity by schema, name, or registry ID | none | none | gap (G1) | Needed to pick a valid link value for a result payload without opening the web app. |
| Create or update a test entity | none | none | gap, out of scope | Entity registration stays in the downstream app through the SDK. A new write family would need its own preview and apply design. Reopen as a feature request if dev testing needs it. |

### Schemas

| Workflow | CLI command | MCP tool | Status | Notes |
|---|---|---|---|---|
| Look up an entry, entity, result, or run schema with field types, required flags, and dropdown options | `schema find`, `schema get`, `schema list --kind` | `schema_find`, `schema_get`, `schema_list` | covered | `schema find` is an exact, case-sensitive match. Dropdown values in result uploads are option IDs (`sfso_…`), not labels. |
| Map a display name to a system name for a payload | `schema get` | `schema_get` | covered | Result uploads also accept display names and show `field_name_map` in the preview. Entry field updates do not. |
| Read container schemas | `container schema list` | `container_schema_list` | partial (G3) | The list embeds field definitions. There is no get by schema ID, so finding one schema means paging. |
| Read plate, box, and location schemas | none | none | gap (G3) | `schema list --kind` has no inventory kinds. |
| Read request schemas | none | none | gap (G4) | `schema list --kind` has no request kind. |

### Plates and containers

| Workflow | CLI command | MCP tool | Status | Notes |
|---|---|---|---|---|
| Read a container with its contents, quantity, and parent storage | `container get` | `container_get` | covered | Returns the V2 container as Benchling sends it, including `contents` and `parentStorageId`. |
| Find a container by exact barcode or name | `container find` | `container_find` | covered | Exactly one filter per call. |
| Find containers by schema or by the box or plate they are in | `container find` | `container_find` | partial (G3) | Only `--barcode` and `--name` are supported. V2 list containers also takes `schemaId` and `ancestorStorageId`. |
| Read a plate and its wells by position | none | none | gap (G3) | A plate ID is not a container ID, so `container get` cannot read it. |
| Read a box or a location | none | none | gap (G3) | Needed to check where the app put a test container. |
| Create a test container, or transfer contents between containers | `changes plan --family containers`, `changes apply` | `changes_plan`, `changes_apply` | covered | Apply re-checks the schema or both containers' modification times first. |
| Check a container before and after a transfer | `container get`, `changes plan` | `container_get`, `changes_plan` | covered | The preview shows both containers. Read each again with `container get` after apply. |

### Entries and notebook tables

| Workflow | CLI command | MCP tool | Status | Notes |
|---|---|---|---|---|
| Resolve an entry from a display ID or URL | `entry resolve` | `entry_resolve` | covered | Returns `web_url`. |
| Read an entry's structure, tables, and results | `entry inspect`, `entry get --full` | `entry_inspect`, `entry_get` | covered | Large entries page parts, results, and table rows. |
| Read entry schema fields | `entry fields get` | `entry_fields_get` | covered | |
| Create a scratch entry from a template | `changes plan --family entry`, `changes apply` | `changes_plan`, `changes_apply` | covered | Apply returns the new entry's ID and `web_url`. |

### Results

| Workflow | CLI command | MCP tool | Status | Notes |
|---|---|---|---|---|
| List the results tables on an entry | `results tables` | `results_tables` | partial | Pain point: it returns `strtbl_…` IDs while `results rows`, `table get`, and `export --kind table` are keyed by `docpart_…`. Since 0.7.0, `results rows` and `export --kind results-table` map a `strtbl_…` ID when `--entry` is passed, and a wrong namespace is a `validation_error` that names both namespaces. No follow-up. |
| Read results and results-table rows | `results list`, `results get`, `results rows` | `results_list`, `results_get`, `results_rows` | covered | Accept V2 UUIDs and V3 `result_…` IDs and return both. |
| Check a result payload before the app sends it | `changes plan --family results` | `changes_plan` | covered | The spec uses the SDK's `AssayResultCreate` shape (`schemaId`, `fields`). The preview reports types, required fields, dropdown options, links, decimal bounds, and project defaults, every problem in a row at once. |
| Push a small test batch to one or more tables | `changes apply` | `changes_apply` | covered | Results land pending until a person clicks the plug icon. `next_step` names the entry and tables. |
| Update existing results from a CSV | `changes plan --family results --csv`, `changes apply` | `changes_plan`, `changes_apply` | covered | Columns are mapped with `--map`; nothing is guessed. |
| Attach a file to a result and check the uploaded file | `changes plan` with `{"file": …}`, `blob get`, `blob download` | `changes_plan`, `blob_get`, `blob_download` | covered | `blob download --compare` checks the bytes against a local file. |
| Detect a stale results-table cell after an update | `results get`, `results rows` | `results_get`, `results_rows` | covered | `table_check` and `stale_cells`. **Re-sync table** is UI-only; there is no API call to add. |

### Requests

| Workflow | CLI command | MCP tool | Status | Notes |
|---|---|---|---|---|
| Read a request the app creates or fulfills | none | none | gap (G4) | |
| List requests by schema and status | none | none | gap (G4) | Needed to find the app's test requests on the dev tenant. |
| Create or update a request | none | none | gap, out of scope | Request writes stay in the downstream app through the SDK. |

### Before and after checks

| Workflow | CLI command | MCP tool | Status | Notes |
|---|---|---|---|---|
| See what a write will change before it is sent | `changes plan` | `changes_plan` | covered | Each plan has `before` and `after`. Nothing is written until apply. |
| Snapshot a table or assay results before and after an app run | `export` | `export` | partial, out of scope | Each export writes a file and reports its SHA-256, so a changed checksum shows a change. The toolkit has no diff command; compare the two CSV or JSON files with standard diff tools. No follow-up. |
| Confirm that a toolkit push landed as planned | `results verify` | `results_verify` | covered | Field-by-field `matched`, `mismatched`, `missing`, and `extra`. |

### Diagnosing a failed push

| Workflow | CLI command | MCP tool | Status | Notes |
|---|---|---|---|---|
| Diagnose a push made with the toolkit | `changes status --wait` | `changes_status` | covered | Task errors map to the batch row and local correlation ID. Journals keep each write's `http_status`. Writes are never retried. |
| Move a toolkit push to another machine to debug it | `changes export` | `changes_export` | covered | `results pending --plan`, `results verify`, and `changes status` read the bundle. |
| Check the async task of a push made by the app's SDK | none | none | gap (G2) | `changes status` reads only toolkit plans. The app has the task ID that V2 bulk create returned, but no command reads `GET /tasks/{task_id}`. |
| Find the results the app pushed, pending or accepted | `results pending --id`, `results pending --schema --created-after` | `results_pending` | partial | `results pending --entry` knows only plans applied from this home, because Benchling has no API filter for pending results by entry. The schema mode can include results from other entries. Documented in [API limitations](api-limitations.md); no follow-up. |
| Read a structured error for any failure | every command | every tool | covered | One envelope with `error.code`, `details`, and a fixed exit code. |

## Gaps and follow-ups

Each gap is a read-only command, and so a read-only MCP tool. Citations are to the official API reference. The quoted line is the operation summary or field description in that spec. V2 calls requests "Legacy Requests".

### G1: entity reads

Proposed: `entity get ID` choosing the route by prefix (`ent_`, `seq_`, `aaseq_`), the same routes the result planner reads, and `entity find --schema SCHEMA_ID --name NAME` returning one page with `--cursor`.

- [V3 API specification](https://benchling.com/api/v3/openapi.yaml), `CustomEntity.Get` on `GET /custom-entity/{custom_entity_id}`: "Get CustomEntity by ID". The same spec has `GET /dna-sequence/{dna_sequence_id}` ("Get DnaSequence by ID") and `GET /aa-sequence/{aa_sequence_id}` ("Get AaSequence by ID").
- [V2 API specification](https://benchling.com/api/v2/openapi.yaml), `listCustomEntities` on `GET /custom-entities`: "List custom entities". Its parameters include `schemaId`, `name`, `registryId`, and `entityRegistryIds.anyOf`.

### G2: task status by ID

Proposed: `task get TASK_ID`, returning the task status, message, and errors, with the same row-index mapping `changes status` uses when the caller passes the payload length.

- [V2 API specification](https://benchling.com/api/v2/openapi.yaml), `getTask` on `GET /tasks/{task_id}`: "Get a task by id". The `AsyncTask` response has `status`, `message`, `errors`, and `response`.

### G3: plates and storage

Proposed: `plate get ID` with wells keyed by position, `box get ID`, `location get ID`, `container schema get ID`, and `--schema` and `--in STORAGE_ID` filters on `container find`.

- [V2 API specification](https://benchling.com/api/v2/openapi.yaml), `getPlate` on `GET /plates/{plate_id}`: "Get a plate". The `Plate.wells` field: "Well contents of the plate, keyed by position string (eg. "A1")."
- Same spec, `getBox` on `GET /boxes/{box_id}`: "Get a box", and `getLocation` on `GET /locations/{location_id}`: "Get a location by ID".
- Same spec, `listPlateSchemas` on `GET /plate-schemas`: "List plate schemas".
- Same spec, `listContainers` on `GET /containers` takes `schemaId` and `ancestorStorageId`.

### G4: request reads

Proposed: `request get ID`, `request list --schema SCHEMA_ID --status STATUS` with `--cursor`, and a `request` kind for `schema list`.

- [V2 API specification](https://benchling.com/api/v2/openapi.yaml), `getRequest` on `GET /requests/{request_id}`: "Get a Legacy Request by ID".
- Same spec, `listRequests` on `GET /requests`: "List Legacy Requests". Its parameters include `schemaId` and `requestStatus`.
- Same spec, `listRequestSchemas` on `GET /request-schemas`: "List request schemas".

### Out of scope

- Entity and request writes. The downstream app makes these through the SDK.
- A diff command for exports. Standard diff tools compare the files.
- Re-syncing a results table. The API has no call for it.
- Filtering pending results by entry. The API has no filter for it.

## Review

- [ ] The matrix has been reviewed by a maintainer.
- [ ] G1 to G4 are each filed as one issue, after review, with the citations above.

Pain points found in this pass are rows in the matrix, not verbal notes. A pain point with no follow-up says so in its row.
