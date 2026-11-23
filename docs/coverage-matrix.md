# Downstream app coverage matrix

This matrix maps the testing and debugging workflows of a downstream lab app to the benchling-tools CLI. The MCP server mirrors the CLI one-for-one: the tool name is the command path joined with underscores (`entry bulk-get` is `entry_bulk_get`), so every command below is also an MCP tool.

The downstream app keeps using the official Benchling SDK for production writes. This toolkit is the fast path for exploring a tenant, diagnosing a failed push, and trying a change as a preview without running a full application write. Work stays on the shared development tenant. This file holds no tenant names, URLs, secrets, or real record IDs.

It is the inventory for the post-0.7.0 coverage pass. The 0.5.0 workflow set (result upload, read-back, verify, attachments, protected environments) stays complete and appears here as covered rows.

Status values:

- **covered**: a command does the job today.
- **partial**: a command does part of the job; the notes say what is missing.
- **gap**: no command does it.

Every partial or gap row names a follow-up (`G1` to `G5` under [Follow-ups](#follow-ups)) or says `out of scope` with the reason. The safety model does not change in any follow-up: keychain auth, MCP write guards, protected environments, and preview-then-apply stay as they are, and every proposed command is a read.

## Matrix

### Environment

| ID | Workflow | CLI command | Status | Notes |
| --- | --- | --- | --- | --- |
| W01 | Confirm the development environment, credentials, and connectivity before a test run | `doctor`, `auth status` | covered | `doctor` checks the saved environment, the token grant, and V2 and V3 reads. |
| W02 | Keep a test write off a protected environment | `auth protect`, `changes apply` | covered | A protected environment needs `--confirm-env`; MCP also needs `--allow-writes ENV`. |

### Entries

| ID | Workflow | CLI command | Status | Notes |
| --- | --- | --- | --- | --- |
| W03 | Find the entry a push targeted from an ID, display ID, or URL | `entry resolve`, `entry bulk-get` | covered | Bulk lookups are chunked at 100 identifiers. |
| W04 | List recent test entries by folder, project, schema, creator, or modified time | `entry list` | covered | Pain point P4: `--name` is an exact, case-insensitive match, not a substring search. |
| W05 | See an entry's sections, part IDs, and tables before reading content | `entry inspect`, `section get`, `part get`, `table get` | covered | Paged reads; `entry get --full` assembles everything. |
| W06 | Read an entry's schema fields | `entry fields get` | covered | Keyed by system name. |
| W07 | Create a scratch entry from a template for a test push | `template list`, `template get`, `changes plan`, `changes apply` | covered | `--family entry`. Not live-verified; see [API limitations](api-limitations.md). |

### Entities

| ID | Workflow | CLI command | Status | Notes |
| --- | --- | --- | --- | --- |
| W08 | Read a custom entity or sequence that a result links to, by ID | none | gap | G1. Result plans already resolve linked custom entities and sequences during preview, but only to validate a link; the record is not returned. |
| W09 | Find an entity by name, registry ID, or schema | none | gap | G1. |
| W10 | Read an entity schema and its field definitions | `schema list`, `schema get`, `schema find` | covered | `schema get` selects the entity schema route for `es_…` IDs. |

### Schemas

| ID | Workflow | CLI command | Status | Notes |
| --- | --- | --- | --- | --- |
| W11 | Read a result schema's fields, required flags, and dropdown options | `schema find`, `schema get` | covered | `schema find` is exact and case-sensitive, and reports ambiguity. |
| W12 | Read entry and run schemas | `schema list`, `schema get` | covered | |
| W13 | Read container schemas | `container schema list` | covered | One page with field definitions. |

### Plates and containers

| ID | Workflow | CLI command | Status | Notes |
| --- | --- | --- | --- | --- |
| W14 | Read a container and its contents by ID | `container get` | covered | The V2 container carries `contents`. |
| W15 | Find a container by barcode or name | `container find` | covered | Exact match only. |
| W16 | Create or transfer into a test container | `changes plan`, `changes apply` | covered | `--family containers`, preview then apply. |
| W17 | Read a plate and its wells by ID or barcode | none | gap | G2. A plate is not a container, so `container get` does not read it. |
| W18 | Walk boxes and locations | none | gap | out of scope: the app's debugging workflows stop at plates and containers. Reopen if a box or location read is needed. |

### Results

| ID | Workflow | CLI command | Status | Notes |
| --- | --- | --- | --- | --- |
| W19 | Discover the result tables on an entry and their schemas | `results tables` | covered | Pain point P1: returns `strtbl_…` ids, while table reads take `docpart_…`. |
| W20 | Read results-table rows and spot stale cells | `results rows` | covered | `--entry` maps a `strtbl_…` id; `stale_check` flags cells that need **Re-sync table**. |
| W21 | List or get result records by entry, schema, or id | `results list`, `results get` | covered | Either V3 `result_…` or V2 UUID; `results get` adds `table_check`. |
| W22 | Read back results the app pushed with the SDK, including pending ones | `results pending` | covered | Use `--id` or `--schema` with `--created-after`. `--entry` only knows plans applied from this home (pain point P3). |
| W23 | Check an app payload against the schema before the app writes it | `changes plan` | covered | `--family results` with a spec in the `AssayResultCreate` shape; reports every bad row. Nothing is written. |
| W24 | Correct result values after a push | `changes plan`, `changes apply` | covered | `--family results --csv` re-reads `modifiedAt` before each PATCH. |
| W25 | Export results or a table for comparison | `export` | covered | Summary has row count, columns, and SHA-256. |
| W26 | Read and download a result attachment | `blob get`, `blob download` | covered | Download verifies against caller-supplied hashes or a reference file. |

### Requests

| ID | Workflow | CLI command | Status | Notes |
| --- | --- | --- | --- | --- |
| W27 | Read a request and its status or response | none | gap | G3. |

### Before and after checks

| ID | Workflow | CLI command | Status | Notes |
| --- | --- | --- | --- | --- |
| W28 | Snapshot an entry, table, or results before a push | `entry get`, `export` | covered | `entry get --full` reports the entry version; `export` records a SHA-256. |
| W29 | Compare created records with what was intended | `results verify` | partial | G5. `results verify` needs a toolkit plan; an SDK push has none. |
| W30 | Confirm a result update reached the record, not just the table cell | `results get` | covered | `table_check` reports `in_sync` or `stale`. |

### Diagnosing a failed push

| ID | Workflow | CLI command | Status | Notes |
| --- | --- | --- | --- | --- |
| W31 | Read the task of an SDK bulk-create by task ID | none | gap | G4. `changes status` reads tasks only for plans in this home. |
| W32 | Map task errors to the input rows that caused them | `changes status` | partial | G4. Row mapping exists for toolkit plans only. |
| W33 | Reproduce the push as a preview to get per-row validation errors | `changes plan` | covered | Same as W23; problems are grouped per row with counts. |
| W34 | Get Benchling's request ID and status for an HTTP error | any command | covered | Error envelopes carry the HTTP status and `X-Request-ID`. |
| W35 | Hand a plan, journal, and result read-back to someone else | `changes export`, `changes status` | covered | The bundle is accepted by `results pending --plan`, `results verify`, and `changes status`. |

## Pain points

Each pain point is already addressed or has a follow-up.

| ID | Area | Pain point | Disposition |
| --- | --- | --- | --- |
| P1 | Naming | `results tables` returns `strtbl_…` ids, but `table get`, `part get`, and `export --kind table` take `docpart_…` ids. | Addressed in 0.7.0: `results rows` and `export --kind results-table` map a `strtbl_…` id with `--entry`, and each id argument names its namespace in help and MCP descriptions. |
| P2 | Naming | Result uploads used to require system names; the app's payloads and people use display names. | Addressed in Unreleased: result upload plans accept display names and report ambiguous ones as row problems. Entry field updates still take system names or field-definition IDs on purpose, because display names are mutable and may not be unique. |
| P3 | Read-back | Pending results have no entry until accepted, so they cannot be found by entry for an SDK push. | Benchling has no pending-by-entry filter; `results pending --id` and `--schema --created-after` read SDK pushes. G4 adds task reads, which return the created record IDs. |
| P4 | Read-back | `entry list --name` does not search substrings. | out of scope: the V3 and V2 entry lists document exact name filters only, and nothing is filtered in the client. |
| P5 | Errors | A mistyped command or option gave no hint. | Addressed in Unreleased: `usage_error` lists valid options and suggests the right command or option. |
| P6 | Read-back | A results-table cell can keep an old value after `PATCH /result/{id}`. | Addressed in 0.7.0: `results rows` and `results get` detect it. Re-sync is UI-only; see [API limitations](api-limitations.md). |

## Follow-ups

Each gap gets one issue. Following [SKILL.md](../SKILL.md), an issue is drafted with `benchling-tools issue draft`, shown to the maintainer, and filed on jmamodeo/benchling-tools only after the maintainer confirms. The quoted lines come from the public API reference snapshots in `contracts/`.

| ID | Rows | Proposed command | Benchling API reference | Status |
| --- | --- | --- | --- | --- |
| G1 | W08, W09 | `entity get ID` and `entity list --schema --name` (read-only) for custom entities, DNA sequences, and AA sequences, selected by ID prefix as result-plan link checks already do. | [V3 API](https://benchling.com/api/v3/openapi.yaml) `GET /custom-entity/{custom_entity_id}`: "Get CustomEntity by ID". [V2 API](https://benchling.com/api/v2/openapi.yaml) `GET /custom-entities` parameter `name`: "Restricts results to those with the specified name, alias, or entity registry ID." | to file after review |
| G2 | W17 | `plate get ID` and `plate find --barcode` (read-only), returning wells. | [V2 API](https://benchling.com/api/v2/openapi.yaml) `GET /plates/{plate_id}`: "Get a plate"; `Plate.wells`: "Well contents of the plate, keyed by position string (eg. "A1")." | to file after review |
| G3 | W27 | `request get ID` (read-only) with its status and response. | [V2 API](https://benchling.com/api/v2/openapi.yaml) `GET /requests/{request_id}`: "Get a Legacy Request by ID"; `GET /requests/{request_id}/response`: "Get a Legacy Request's response". [V3 API](https://benchling.com/api/v3/openapi.yaml) `GET /request-v2-submission/{request_v2_submission_id}`: "Get RequestV2Submission by ID". | to file after review |
| G4 | W31, W32 | `task get TASK_ID` (read-only) that reads any task and maps errors with an in-range `index` to input rows when an input file is given, as `changes status` does for plans. | [V2 API](https://benchling.com/api/v2/openapi.yaml) `GET /tasks/{task_id}`: "Get a task by id". | to file after review |
| G5 | W29 | `results verify --expected FILE --id …` (read-only) that diffs an `AssayResultCreate` payload against records read back by id, without a toolkit plan. | [V2 API](https://benchling.com/api/v2/openapi.yaml) `GET /assay-results:bulk-get` `assayResultIds`: "Up to 200 IDs can be specified at once." | to file after review |

## Review

- [ ] The maintainer has reviewed this matrix.
- [ ] Each follow-up is filed, or marked out of scope here with the reason.
