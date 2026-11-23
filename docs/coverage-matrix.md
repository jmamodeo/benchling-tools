# Downstream app testing coverage matrix

This matrix maps the testing and debugging workflows of a downstream lab app to `benchling-tools` commands ([#108](https://github.com/jmamodeo/benchling-tools/issues/108)). Each CLI command is one MCP tool with the same arguments, so the tool name is the command path joined with `_` (`results rows` is `results_rows`).

The downstream app keeps using the official Benchling SDK for production writes. Here the toolkit is the fast path for three jobs: exploring a tenant, diagnosing a failed push, and checking new features without running a full application write. Pushes made by the SDK are not toolkit plans. So `results pending --plan`, `results pending --entry`, `results verify`, and `changes status` only help with pushes the toolkit itself made. An SDK push is read back by schema and time, by result id, or from the entry's tables.

All work runs against the shared development tenant. This file, and the issues it links, contain no tenant names, URLs, secrets, or real record IDs.

Status:

- **covered**: a command does the job today.
- **partial**: a command does part of the job, or does it with a pain point.
- **gap**: no command does the job.

Every partial or gap row has a follow-up: an open issue, a proposed issue in [Proposed gap issues](#proposed-gap-issues), or an explicit out-of-scope decision. Nothing here is left as a verbal note.

## Matrix

### Connect and explore

| Workflow | Command (MCP tool) | Status | Notes and follow-up |
| --- | --- | --- | --- |
| Confirm the dev environment, credentials, and API reachability | `auth status`, `doctor` (`auth_status`, `doctor`) | covered | `auth login` needs a terminal and has no tool. |
| Find the app's entries by project, folder, schema, name, time, or creator | `entry list` (`entry_list`) | covered | `--creator` takes the app's user ID or handle, which isolates entries the app created. `--name` is an exact match, not a substring search. |
| Resolve a display ID or pasted URL | `entry resolve`, `entry bulk-get` (`entry_resolve`, `entry_bulk_get`) | covered | |
| See an entry's layout: sections, parts, tables, schemas | `entry inspect`, `entry get --full` (`entry_inspect`, `entry_get`) | partial | Takes 1 to 3 minutes on a large entry because it reads everything. Per-table row counts without a full inspect: [#112](https://github.com/jmamodeo/benchling-tools/issues/112). |
| Read a folder, template, or user | `folder get`, `template get`, `template list`, `users get`, `users list` | covered | |
| Read one section, part, or unstructured table | `section get`, `part get`, `table get` | covered | Paged. `table get` takes a `docpart_…` id, not a `strtbl_…` id. |

### Schemas

| Workflow | Command (MCP tool) | Status | Notes and follow-up |
| --- | --- | --- | --- |
| Look up the entry, entity, result, or run schema the app writes to, with field definitions and dropdown options | `schema list`, `schema find`, `schema get` (`schema_list`, `schema_find`, `schema_get`) | covered | Field display names, system names, and field-definition ids are all listed. Dropdown values in plans are option ids (`sfso_…`), not labels. |
| Read entry schema field values | `entry fields get` (`entry_fields_get`) | covered | |
| Look up container schemas | `container schema list` (`container_schema_list`) | covered | |
| Look up plate, box, and location schemas | none | gap | Proposed issue G2 and G3. |

### Entities

| Workflow | Command (MCP tool) | Status | Notes and follow-up |
| --- | --- | --- | --- |
| Read a custom entity or sequence that a result links to (fields, schema, registry ID) | none | gap | `changes plan --family results` already reads a linked entity internally to check its schema, but no command shows it. Proposed issue G1. |
| Find an entity by name or registry ID before or after a push | none | gap | Proposed issue G1. |
| Create, register, or edit entities | none | out of scope | The app writes entities with the SDK. Entity writes would need a new plan family and are not part of this testing pass. The safety model is unchanged. |

### Plates and containers

| Workflow | Command (MCP tool) | Status | Notes and follow-up |
| --- | --- | --- | --- |
| Read a tube or container, its contents, and its parent storage | `container get` (`container_get`) | covered | Takes a `con_…` id. |
| Find a container by barcode or name | `container find` (`container_find`) | covered | Exact match, one barcode per call. |
| Resolve many tube ids to barcodes at once, for example every tube linked from a results table | `container get` once per tube | partial | One call per tube. Bulk lookup by id or barcode is part of [#113](https://github.com/jmamodeo/benchling-tools/issues/113) (`--resolve-barcodes`). |
| Read a plate and its wells by id or barcode | none | gap | `container find --barcode` does not return plates, because V2 lists plates separately. Proposed issue G2. |
| Read a box or location to check where a tube was moved | none | gap | Proposed issue G3. |
| Create a container or transfer contents in a test | `changes plan --family containers`, `changes apply` | covered | Preview first. Apply needs `--allow-writes`, and a protected environment also needs `--confirm-env`. |

### Results

| Workflow | Command (MCP tool) | Status | Notes and follow-up |
| --- | --- | --- | --- |
| List an entry's results tables with schema and columns | `results tables` (`results_tables`) | partial | No row count, `docpart_…` id, or submission status: [#112](https://github.com/jmamodeo/benchling-tools/issues/112). |
| Read results-table rows and spot stale cells | `results rows` (`results_rows`) | covered | Accepts `strtbl_…` with `--entry`. Rows that disagree with their result record carry `stale_cells`. |
| Count or filter rows that belong to one run (a text field holding the run id) | `results rows`, then count by hand | partial | Benchling has no server-side text filter. Client-side `--where`: [#112](https://github.com/jmamodeo/benchling-tools/issues/112). |
| Read one result by V2 UUID or V3 id, with table sync | `results get` (`results_get`) | covered | `table_check` reports `in_sync`, `stale`, and the other states. |
| List results by entry, schema, or id | `results list` (`results_list`) | covered | A pending result has no entry yet, so `--entry` does not find it. |
| Read back an SDK push, pending results included | `results pending --schema … --created-after …`, `results pending --id` | covered | `--entry` and `--plan` only know toolkit plans in this home, not SDK pushes. |
| Check a table has room before a push | none | gap | A bulk create failed on a full table after sibling tables were written. Also, the 500-record plan error wrongly calls 500 a Benchling limit. [#111](https://github.com/jmamodeo/benchling-tools/issues/111). |
| Export a table or results for an offline diff | `export` (`export`) | covered | Link values are ids. The reply includes SHA-256. |
| Upload test results from a plan or CSV | `changes plan --family results`, `changes apply`, `results verify` | covered | Takes display names, system names, or field-definition ids. Created results land pending until accepted with the plug icon. |
| Update existing results and confirm the table re-synced | `changes plan --family results` (CSV or spec), `results get`, `results rows` | covered | Re-syncing a table is UI-only (**Re-sync table**). |
| Download a result attachment and check its hash | `blob get`, `blob download` (`blob_get`, `blob_download`) | covered | |
| Read assay runs | none | out of scope | Runs are not in the workflow set [#108](https://github.com/jmamodeo/benchling-tools/issues/108) lists. Run schemas can already be read with `schema get`. If the sign-off ([#109](https://github.com/jmamodeo/benchling-tools/issues/109)) finds one, it gets its own issue. |

### Requests

| Workflow | Command (MCP tool) | Status | Notes and follow-up |
| --- | --- | --- | --- |
| Read a request and its status, fields, and sample groups to check what the app picked up or fulfilled | none | gap | Proposed issue G4. |
| Create or update requests | none | out of scope | Production request writes stay in the SDK. |

### Before and after checks

| Workflow | Command (MCP tool) | Status | Notes and follow-up |
| --- | --- | --- | --- |
| Snapshot an entry, table, or result before a push and compare after | `entry get --full`, `export`, `results get` | partial | Dumps compare well by SHA-256 but are diffed by hand. Table-by-table and row-by-row diff between two entries or runs: [#113](https://github.com/jmamodeo/benchling-tools/issues/113). |
| Compare a first push with a rerun (tube reassignment, dropped fields) | `results rows` or `export` on both entries, then a hand diff | partial | [#113](https://github.com/jmamodeo/benchling-tools/issues/113). |
| Compare a toolkit-made upload with what Benchling created | `results verify` (`results_verify`) | covered | Toolkit plans only. |
| Snapshot an entity, plate, or request before and after | none | gap | Comes with G1, G2, and G4. |

### Diagnosing a failed push

| Workflow | Command (MCP tool) | Status | Notes and follow-up |
| --- | --- | --- | --- |
| Read the status and per-record errors of an SDK bulk-create task by task id | none | gap | `changes status` reads only toolkit plan journals. A failed SDK task's `errors` can only be seen through the SDK. Proposed issue G5. |
| Find which tables a partial multi-table push already wrote | `results pending --schema … --created-after …`, `results rows` | partial | It works, but needs one call per schema and a hand count per table. [#112](https://github.com/jmamodeo/benchling-tools/issues/112) adds per-table counts and run filters, and [#111](https://github.com/jmamodeo/benchling-tools/issues/111) prevents the capacity case. |
| Tell "pending, not accepted" from "missing" | `results pending` | covered | Each record is marked pending, accepted, or archived. |
| Tell a stale table cell from a bad write | `results rows`, `results get` | covered | `stale_cells` and `table_check`. |
| Understand an error message from the toolkit | JSON envelope, `error.code`, `details` | covered | Usage errors list valid options and hints. The 500-record message fix is part of [#111](https://github.com/jmamodeo/benchling-tools/issues/111). |
| Report something the toolkit cannot do | `issue draft` (`issue_draft`) | covered | Drafts only. Filing needs the person's yes. |

## Proposed gap issues

These drafts are waiting for review of this matrix, as [#108](https://github.com/jmamodeo/benchling-tools/issues/108) requires. After review, each one is filed as its own `enhancement` with `benchling-tools issue draft`, and its row here is updated with the issue number. Each is read-only and leaves the safety model alone (keychain auth, write guards, preview then apply). Quotes come from the public OpenAPI specs captured in `contracts/` and listed in `contracts/openapi-manifest.json`.

### G1. Read and find entities

- **Summary:** Add read-only `entity get` and `entity find` for custom entities and sequences.
- **Why:** Results link to entities. When a push is debugged, the linked entity's schema, fields, and registry ID need checking. No command can show it today.
- **Proposed solution:** `entity get ID` (`ent_…`, `seq_…`, `aaseq_…`) routes by prefix to V3 `GET /custom-entity/{id}`, `/dna-sequence/{id}`, or `/aa-sequence/{id}`. These are the routes `get_linked_entity_schema_id` already reads. `entity find --schema --name --registry-id` lists one page of V2 `GET /custom-entities`.
- **Citations:**
  - [V3 OpenAPI](https://benchling.com/api/v3/openapi.yaml), `GET /custom-entity/{custom_entity_id}`: "Get a single CustomEntity by ID."
  - [V2 OpenAPI](https://benchling.com/api/v2/openapi.yaml), `GET /custom-entities` `entityRegistryIds.anyOf`: "Restricts results to those that match any of the specified registry IDs".

### G2. Read plates and wells

- **Summary:** Add read-only `plate get` and `plate find`, with wells keyed by position.
- **Why:** Plate barcodes are not found by `container find`. Checking which tube or entity sits in which well after a run needs the plate's `wells`.
- **Proposed solution:** `plate get ID` calls V2 `GET /plates/{plate_id}` and returns `wells` keyed by position. `plate find --barcode --name --schema` calls V2 `GET /plates`.
- **Citations:**
  - [V2 OpenAPI](https://benchling.com/api/v2/openapi.yaml), `GET /plates/{plate_id}`: "Get a plate". `Plate.wells`: "Well contents of the plate, keyed by position string (eg. "A1")."
  - [V2 OpenAPI](https://benchling.com/api/v2/openapi.yaml), `GET /plates` `barcodes`: "Comma-separated list of barcodes."

### G3. Read boxes and locations

- **Summary:** Add read-only `storage get` for boxes and locations.
- **Why:** After a tube is reassigned, the check is where the tube now sits. `container get` returns only `parentStorageId`.
- **Proposed solution:** `storage get ID` routes by prefix to V3 `GET /box/{box_id}` or `GET /location/{location_id}`, and returns the parent chain one level at a time.
- **Citations:**
  - [V3 OpenAPI](https://benchling.com/api/v3/openapi.yaml), `GET /box/{box_id}`: "Get a single Box by ID."
  - [V3 OpenAPI](https://benchling.com/api/v3/openapi.yaml), `GET /location/{location_id}`: "Get a single Location by ID."

### G4. Read requests

- **Summary:** Add read-only `request get` and `request list`.
- **Why:** The app reads and fulfills requests. Checking what a test changed needs the request's status and fields.
- **Proposed solution:** `request get ID` and `request list --schema --status --created-after` call the V2 legacy request routes. V3 `RequestV2Submission` is early access, so it is left out until the app's request model is confirmed at review.
- **Citations:**
  - [V2 OpenAPI](https://benchling.com/api/v2/openapi.yaml), `GET /requests/{request_id}`: "Get a Legacy Request by ID". `GET /requests`: "List Legacy Requests".
  - [V3 OpenAPI](https://benchling.com/api/v3/openapi.yaml), `GET /request-v2-submission/items`: "List RequestV2Submission items."

### G5. Read a task by id

- **Summary:** Add read-only `task get TASK_ID` to read any V2 long-running task.
- **Why:** An SDK bulk create returns a task id. When it fails, its `errors` say which records were rejected and why. `changes status` reads only this home's plan journals.
- **Proposed solution:** `task get TASK_ID [--wait --timeout]` calls V2 `GET /tasks/{task_id}`, the route `changes status` already polls. It returns `status`, `message`, `errors`, and `response`, redacted like other replies.
- **Citations:**
  - [V2 OpenAPI](https://benchling.com/api/v2/openapi.yaml), `GET /tasks/{task_id}`: "Get a task by id". `AsyncTask.errors`: "Present only when status is FAILED for a bulk task. Contains information about the individual errors in the bulk task."

## Review

This matrix is the inventory for the post-0.7.0 coverage pass. The 0.5.0 workflow set is done and is not reopened here. After review, G1 to G5 are filed, or marked out of scope here with a reason. Then [#109](https://github.com/jmamodeo/benchling-tools/issues/109) runs every row end to end on the shared development tenant.
