---
name: benchling-tools
description: "Use the benchling-tools CLI and its local MCP server to list, read, and export a Benchling notebook, and to preview a change before applying it. Use when the user asks to list, find, or read entries, containers, schemas, or assay results; read sections or document parts; export a table or results to JSON or CSV; create an entry from a template; or plan and then apply a document, table, field, result, or container change. Credentials come from benchling-tools auth login --env NAME and stay in the OS keychain. The MCP server is read-only unless it was started with --allow-writes ENV. The toolkit does not drive a browser; pair it with one only for viewing entries and accepting pending results. Do not use this when no environment has been saved with auth login."
license: Apache-2.0
compatibility: "Requires the benchling-tools command from the benchling-tools package (Python 3.11+), an OS keychain, and network access to the tenant saved for that environment. The MCP server is local stdio."
metadata:
  summary: "Safe reads and previewed writes for a Benchling notebook. Lists, reads, and exports entries, containers, tables, and assay results. Applies a change only after a local plan. The MCP server is read-only unless started with --allow-writes ENV."
  version: "0.7.0"
  docs_url: "https://github.com/jmamodeo/benchling-tools"
---

# benchling-tools

A CLI for coding agents and humans, directly mirrored as an MCP server for chat agents.

Unofficial. Not affiliated with or endorsed by Benchling, Inc.

benchling-tools has no public Python API. Its Python modules are internal and may change in any release, so do not import them in scripts. A person who wants to script Benchling in Python should use the official Benchling SDK (https://docs.benchling.com/docs/getting-started-with-the-sdk). A public Python API may be added if there is enough interest; they can open a GitHub issue on https://github.com/jmamodeo/benchling-tools to ask for one.

Use this when an agent should work in a Benchling tenant through the `benchling-tools` command or the local MCP server. The toolkit itself uses only the public HTTP API and never drives a browser. An agent that also has a browser signed in to the same tenant may use it alongside the toolkit, as described in "Working alongside the browser".

## When to use it

Use this skill when the person asks to:

- list entries, containers, container schemas, schemas, or assay results
- bulk-get entries by ID or display ID, read a folder, or inspect an entry template
- read an entry, a section, a document part, an unstructured table, entry fields, or results-table rows
- get a container by ID or find containers by barcode (many at once) or name
- export a table or assay results to a local JSON or CSV file
- create a notebook entry from a template in a folder, after previewing the creation plan
- preview a document, table, field, result, or container change, and apply it only after they accept that plan

Stop and do not improvise when:

- no environment is saved yet
- the person has not accepted a specific plan id, and the next step would write to Benchling
- the task needs the Benchling website and you have no browser signed in to that tenant, or the person has not asked you to act in it
- the toolkit cannot do what was asked: follow "When the toolkit cannot do it" below

## Safety defaults

The MCP server is read-only by default. `benchling-tools mcp serve` does not enable writes. `changes_apply` is refused until that process was started with `--allow-writes ENV` for the environment in the call. Repeat the flag once per environment.

`changes plan` and the MCP tool `changes_plan` only read Benchling. They write an immutable local plan. They do not need `--allow-writes`.

`changes apply PLAN_ID` is the remote write. Run it only after the person accepts that plan id, and only with the same `--env` that created the plan. Show them the plan id, the family, and the preview in the envelope before you apply. A plan is single-use. A stale plan exits 5. Do not retry a write that exits 8 (partial or unknown).

A protected environment refuses `changes apply` (exit 2) unless `--env NAME` is on the command line and `--confirm-env NAME` repeats it. Add `--confirm-env` only after the person has confirmed that environment name for this plan. Never guess it, and never run `auth unprotect` to get around the guard. `doctor` shows `protected` and a `protection` line. On MCP, a protected environment's `changes_apply` is refused unless the server was started with `--allow-writes` for it. Reads and previews are unaffected.

`export` writes a local file and does not change Benchling. It does not need `--allow-writes`. Leave an existing file in place unless the person asked to replace it with `--overwrite`.

There is no default environment. Pass `--env NAME` on every API command. `-e` is the short form. `NAME` uses lowercase letters and digits, starts with a letter, and is at most 32 characters. Use a read environment for reads. Use a write environment only when the person asked for a write.

## Credentials

The person saves an environment in their own terminal:

```console
benchling-tools auth login --env NAME
```

The client secret is read from a hidden prompt, or from stdin when they pass `--secret-stdin`. It is never a command argument. Login saves nothing until a client-credentials token request succeeds. The tenant URL and client ID go in `environments.json`. The client secret goes in the OS keychain under the service name `benchling-tools`, with the environment name as the keychain username.

Run `auth login` only when the person has given you the credentials for that environment and asked you to save them; pass the secret with `--secret-stdin`, never as an argument. Otherwise do not run it, and never invent a tenant URL, client ID, or secret. Do not ask for the client secret, read it from the environment, or print it. If a command fails because the environment is missing or the keychain has no secret, stop and tell the person to run `benchling-tools auth login --env NAME` in their own terminal.

These commands print no secret:

```console
benchling-tools auth status
benchling-tools auth status --check --env NAME
benchling-tools doctor --env NAME
```

The person can mark an environment protected in their own terminal:

```console
benchling-tools auth login --env NAME --protected
benchling-tools auth protect --env NAME
```

`auth status` lists saved environments, whether a keychain secret is present, and whether each is `protected`. `--check` requests a token and does not print or save it. `doctor` makes read-only API checks. A failed doctor check exits 3.

If `benchling-tools` is not on `PATH`, run `uv run benchling-tools` from a checkout of this repository. The same subcommands apply.

## How to read a response

Put `--env NAME` on the command, after its name: `benchling-tools entry get REF --env NAME`. Stdout is one JSON object with `ok`, `data`, `error`, and `meta`. Stderr is diagnostics. A nonzero exit is a failure.

| Exit | Meaning |
|---:|---|
| 0 | Success |
| 2 | Input or validation error |
| 3 | Authentication error |
| 4 | Access denied or not found |
| 5 | Concurrent modification, or a plan already used |
| 6 | Unsupported capability |
| 7 | Upstream or transport failure |
| 8 | Partial or unknown write |
| 9 | Usage error: the command line did not parse (`error.code` is `usage_error`; `details.valid_options` and `details.hint` name the options to use) |

Identifiers in the examples (`etr_example`, `docpart_example`, `docpart_table1`, `docpart_rt1`, `docpart_text1`, `docpart_section1`, `strtbl_example`, `src_example`, `lib_example`, `assaysch_example`, `EXP-0001`, `RESULT_ID`, `PLAN_ID`, `NEXT_TOKEN`) are placeholders. Substitute ids from the command you just ran. Do not invent a tenant URL or an organization name.

An entry reference may be a canonical id, a display id, or a URL on the selected tenant.

## Read workflow

### List

Each list call returns one page. Pass `next_token` back as `--cursor`. A short page is not the whole result.

```console
benchling-tools entry list --project src_example --limit 50 --env NAME
benchling-tools entry list --folder lib_example --name "Example entry" --env NAME
benchling-tools users get HANDLE --env NAME
benchling-tools entry list --creator USER_ID --folder lib_example --env NAME
benchling-tools entry list --creator USER_ID --sort created-desc --limit 1 --env NAME
benchling-tools entry list --modified-after 2026-01-01 --cursor NEXT_TOKEN --env NAME
benchling-tools schema list --env NAME
benchling-tools schema list --kind result --limit 50 --env NAME
benchling-tools schema find "qPCR assay" --env NAME
benchling-tools container get con_example --env NAME
benchling-tools container find --barcode VIAL-001 --env NAME
benchling-tools container find --barcode VIAL-001 --barcode VIAL-002 --env NAME
benchling-tools container find --name "Sample vial" --env NAME
benchling-tools container schema list --limit 50 --env NAME
benchling-tools results list --entry etr_example --page-size 100 --env NAME
benchling-tools results list --entry etr_example --include-pending --env NAME
```

`entry list` filters are applied by Benchling. `--name` is an exact match, not a substring. `--folder` uses the V2 entry list. `--sort` is `created-desc`, `created-asc`, `modified-desc`, `modified-asc`, `name-asc`, or `name-desc`; it uses the V2 entry list, sends the matching [V2 `sort`](https://docs.benchling.com/docs/api/v2/benchling-api/entries/list-entries) value, and the reply echoes it as `sort` (`null` when omitted, which keeps Benchling's default of most recently modified first). Do not reuse a `next_token` from one sort with another. `container find` requires exact `--barcode` or `--name`, not both. Repeat `--barcode` to look up many barcodes in one call (one barcode per value, no commas; duplicates are dropped and order is kept). They are sent to V2 `GET /containers` 100 per request, every page is read, and the reply is `requested`, `found` (each with `barcode`, `container_id`, `schema_id`, `contents` of `entity_id`, `entity_registry_id`, and `batch_id`, and `archived`), `invalid_barcodes`, `missing_barcodes` (valid, but no container matched), and `empty_containers` (found with no contents). Barcodes Benchling names in `invalidBarcodes` are reported instead of failing the call. With one barcode the reply is still one page (`items`, `next_token`) and adds `invalid_barcodes`. The rule is the same for one barcode or many: a barcode Benchling names in `invalidBarcodes` is reported in `invalid_barcodes` (one barcode: `items: []` and a `hint`) and the call exits 0; only bad input, such as an empty value or a comma inside one value, is a `validation_error`. See [List containers](https://docs.benchling.com/docs/api/v2/benchling-api/containers/list-containers). `container find --contents ENTITY_ID` (repeatable), `--registry-id REG-ID` (repeatable, with `--registry src_…` unless the tenant has one registry) and `--in STORAGE_ID` (`loc_…`, `box_…` or `plt_…`) answer "which tubes hold this sample?" and "which tubes are in this box?". Give exactly one of `--barcode`, `--name`, `--contents` or `--registry-id`, or `--in` on its own; `--in` combines with `--name`, `--contents` and `--registry-id`. They send `storageContentsIds` (100 per request) and `ancestorStorageId` to V2 List containers, read every page up to `--limit` containers (default 50, at most 1000), and return `found` (the same items, with `parent_storage_id`) and `truncated`. `--registry-id` adds `registry`, `entities` and `unregistered_ids`; with several registries and no `--registry`, the error lists them. See [Bulk get registered entities](https://docs.benchling.com/docs/api/v2/benchling-api/registry/bulk-get-registered-entities). `container get CON_ID --location` answers "where is this tube?": `location_path` lists `id`, `kind` (`well`, `position`, `plate`, `box` or `location`), `name`, `barcode` and `schema_id` from the parent up to the top, one V2 location, box or plate read per level, stopping after 10 levels with `location_truncated` true. `container find` with `--name` or one `--barcode`, and `container schema list`, return one page, default 50 and maximum 100; pass `next_token` back with `--cursor`. Container schema results include their field definitions. `--modified-after` and `--modified-before` take `YYYY-MM-DD` or an RFC3339 timestamp that includes a time zone. `--limit` is 1 to 100 and defaults to 50 on `entry list` and `schema list`. `schema list --kind` is `entry`, `entity`, `result`, or `run`. Omit `--kind` to list all four. `schema find NAME` requires an exact, case-sensitive name and returns one schema with field definitions, types, required flags, and dropdown options. If the name is absent or ambiguous, the command returns an error; ambiguity candidates include only IDs and kinds. `results list` uses `--page-size` (default 100, maximum 100), plus optional `--schema` and `--cursor`.

`users list` searches visible users by repeatable `--handle`, `--email`, `--id`, or `--member-of`, and by `--name` or `--name-includes`. It returns one V2 page and `next_token`. `users get HANDLE` resolves an exact handle to a user ID; use that ID in `entry list --creator USER_ID --folder lib_example`, or pass a handle directly. Repeat `--creator` and `--author` to match any specified user within each filter. Both filters are server-side, and the response echoes IDs in `resolved_users`. The caller must have permission to view users; an app token does not identify a current user.

`entry bulk-get` uses either repeated `--id` or repeated `--display-id` options, not both, and chunks requests at the V2 limit of 100 IDs. `template list` returns one page; pass `next_token` back with `--cursor` and optionally filter by exact `--name`. `template get` summarizes note types in order, results-table schemas, table names, section titles, and text previews. It reads V2 `GET /entry-templates/{id}` because V3 `EntryTemplate.Get` returns metadata only. `folder get` reads one folder by ID.

`entry resolve`, `entry get`, `entry list` and `entry bulk-get` return `web_url`, the entry's link in the Benchling web app (`null` when Benchling returns none). Give the person that link when they need to open the entry.

### Read entries, tables, and results

Start with `inspect` for structure. Use `entry get --full` when you need the content. For a large table, export it instead of pulling every row into context.

```console
benchling-tools entry resolve EXP-0001 --env NAME
benchling-tools entry inspect etr_example --env NAME
benchling-tools entry get etr_example --full --env NAME
benchling-tools entry bulk-get --id etr_example --id etr_other --env NAME
benchling-tools entry bulk-get --display-id EXP-0001 --display-id EXP-0002 --env NAME
benchling-tools template get etpl_example --env NAME
benchling-tools template list --name "Example template" --env NAME
benchling-tools folder get lib_example --env NAME
benchling-tools entry fields get etr_example --env NAME
benchling-tools section get etr_example docpart_section1 --env NAME
benchling-tools part get etr_example docpart_text1 --env NAME
benchling-tools part get etr_example docpart_textbox1 --env NAME
benchling-tools table get docpart_table1 --env NAME
benchling-tools schema get assaysch_example --env NAME
benchling-tools results get RESULT_ID --env NAME
benchling-tools blob get BLOB_ID --env NAME
benchling-tools results tables etr_example --env NAME
benchling-tools results capacity etr_example --spec results.json --env NAME
benchling-tools results rows docpart_example --limit 50 --cursor NEXT_TOKEN --env NAME
benchling-tools results tables etr_example --name-prefix "Panel Testing" --where "Processing run=RUN_ID" --env NAME
benchling-tools results rows strtbl_example --entry etr_example --where "Processing run=RUN_ID" --env NAME   # this page only (where_scope: page)
benchling-tools results pending --plan PLAN_ID_OR_FILE --env NAME
benchling-tools results pending --entry ENTRY --env NAME
benchling-tools results verify PLAN_ID_OR_FILE --env NAME
benchling-tools results verify --payload body.json --created-after 2026-11-19T00:00:00Z --env NAME
benchling-tools results diff --left etr_example:research --right etr_other:regulatory --run-field "Run type" --key Well --resolve-barcodes --env NAME
```

`entry inspect` lists fill-in text boxes under `text_boxes` (`part_id`, `name`, full `text`, `submission_status`, `is_submittable`, `section`) and lookup tables under `lookup_tables` (`part_id`, `name`, `row_count`); `section get` returns both for its parts. `part get ENTRY docpart_…` reads only that part (V3 parts list with `id.anyOf`, falling back to a full snapshot if Benchling rejects the filter): a text box adds `text_box` with `name`, `text`, `formats`, `links`, `mentions`, `submission_status` and `is_submittable`; a table, including a read-only lookup table, has its columns and a page of rows in `detail`. `table get` reads an unstructured table. Its argument is a `docpart_…` id from `entry inspect`. `results tables` returns `table_id` values that are `strtbl_…` ids. `results rows` reads one page of a results-table part (`docpart_…`) from that part's rows URL; a `strtbl_…` id works only with `--entry ENTRY`, which maps it to that entry's part. `results tables` also gives each table its `part_id` (`docpart_…`), `row_count`, `submission_status` and `placeholder_rows` from one parts listing; `--no-counts` skips that. To check where one run's rows landed, use `results tables ENTRY --name-prefix TEXT --where FIELD=VALUE`: each table gets `matching_rows` (null when it has no such column) and the reply gives `rows_scanned`. `results rows ... --where FIELD=VALUE` keeps only matching rows of the page and adds `scanned` and `matched`. FIELD is a display name, system name or field-definition id; a link cell matches on the linked id. The filter runs client-side because Benchling has no text-value filter, and it counts accepted table rows only: use `results pending` for pending results. `results get`, `results list --id` and `results pending --id` take either a V3 result id (`result_…`) or a V2 assay result UUID, and each record they return, like each `results verify` row, has both as `v3_id` and `v2_id` (`v2_id` is null when Benchling does not return it). When an id cannot be read, the error `details` give `id_namespace` (what was passed) and `lookup` (what was tried). `schema get` includes each dropdown field's options. `blob get` reads metadata without downloading, using either a blob UUID or an `attachment_…` id. `blob download ID --output PATH` writes a local file and reports its size, SHA-256 and MD5. Supply `--expect-size`, `--expect-sha256`, `--expect-md5`, or `--compare LOCAL_FILE` to verify it; without any expectation the summary says `verified: false`. Existing files are kept unless `--force` is given.

`results capacity ENTRY` reports, per results table, `row_count` (V3 `rowCount`), `placeholder_rows`, `used_rows`, `pending_known` (still-pending records from this home's applied upload plans), `planned`, `remaining_without_pending`, `remaining_with_pending`, and `fits`, against Benchling's 1,000-row table limit. Pass `--plan PLAN_ID_OR_FILE`, `--spec FILE`, or `--spec-json JSON` to count planned rows. It is the same check `changes plan --family results` runs, which refuses an over-limit plan with `table_capacity_exceeded` before staging any file. Benchling does not document whether pending results count toward the limit, so `fits` is `unknown_pending` when rows fit only if they do not; relay that warning. It exits 0 even when a table does not fit: relay `next_step` (use a new entry or another table).

`results diff --left ENTRY[:RUN_VALUE] --right ENTRY[:RUN_VALUE]` compares the accepted results-table rows of two entries, or two runs on one entry (`--run-field` names the field that holds the run value), to show what changed between a first push and a rerun. Tables pair by exact name (`--table NAME` restricts them); rows pair by `--key FIELD[,FIELD]`, or by position with a warning. Each table reports `counts` and samples of `only_left`, `only_right`, and `changed` fields, plus `duplicate_keys` (every key used more than once on either side, with the row count on each side, e.g. `{key, left: 1, right: 2, …}`; such keys are not paired but are never dropped silently); `tables_only_left`, `tables_only_right`, and `schema_mismatch` cover tables that cannot be compared. `--resolve-barcodes` shows each container's barcode next to its `con_…` id and lets a tube key on its barcode. `--compare-blobs` compares attachment cells by SHA-256, downloading to a temporary file that is deleted. `--output FILE --format json|csv` writes every difference, like `export`. Pending results are not table rows; `--include-pending` adds the still-pending results of this home's applied upload plans for each entry as rows of the table they were uploaded to, marked `pending: true`, with `counts.pending_left` and `counts.pending_right`. `results list --entry ENTRY --include-pending` likewise adds a `pending` block. Plans applied from another home are not known; use `results pending` and `results verify` for those. For results pushed outside this home (for example by a lab app using the SDK), add `--pending-created-after TIME`: it also reads pending results of each table's result schema created since `TIME` ([V2 List assay results](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/list-assay-results) filters by schema and time but not entry; a pending result has no entry, see [V3 List results](https://docs.benchling.com/docs/api/benchling-api/result/list)). Benchling does not tie those records to an entry or table, so say so when reporting them: each item has `source` `this_home` or `schema_window`, and in `results diff` a record whose schema is shared by two or more tables on a side is listed in `pending_unplaced`. It only reads.

`results pending --plan` and `results verify` accept either a plan ID in the current home or a path to a plan JSON file. To read results from another home, copy the plan and its applied journal; name the journal `<plan-id>.journal.json` and place it beside the plan. This transfers only the two needed files, not the whole config home. The active environment must still match the plan.

### Export

Export reads every page and writes a local file. The envelope is a summary only: `row_count`, `columns`, `path`, and `checksum`. The rows stay in the file.

```console
benchling-tools export --kind table --part docpart_table1 --format csv --output measurements.csv --env NAME
benchling-tools export --kind results-table --part docpart_rt1 --format json --output results-table.json --env NAME
benchling-tools export --kind results-table --part strtbl_example --entry etr_example --format csv --output listed-table.csv --env NAME
benchling-tools export --kind assay-results --entry etr_example --schema assaysch_example --format csv --output assay.csv --env NAME
```

`--kind` is `table`, `results-table`, or `assay-results`. `--format` is `json` or `csv`. `--output` is required (`-o` is the short form). `table` and `results-table` need `--part`. A `docpart_…` id comes from `entry inspect`. A `strtbl_…` id from `results tables` needs `--kind results-table` and `--entry` for that same entry. `--kind table` rejects a `strtbl_…` id. `assay-results` takes `--entry` and `--schema`. Add `--overwrite` only when the person asked to replace that file.

## Preview, then apply

One plan covers one existing-entry mutation, one entry creation, or one container operation. The families are `entry`, `document`, `table`, `fields`, `results`, and `containers`. For `entry`, pass `entry_template_id`, `folder_id`, and `name`; optional `schema_fields` are checked against the template's schema. Omit `--entry` for entry and container plans. Write the JSON spec to a file, then plan. `--spec -` reads that JSON from stdin, and `--spec-json TEXT` takes it inline. Pass only one of `--spec`, `--spec-json`, and `--csv`.

```console
benchling-tools changes plan --entry etr_example --family table --spec set-cells.json --env NAME
benchling-tools changes plan --family containers --spec create-container.json --env NAME
benchling-tools changes plan --family containers --spec transfer-container.json --env NAME
benchling-tools changes apply PLAN_ID --env NAME
benchling-tools changes apply PLAN_ID --confirm-env NAME --env NAME
benchling-tools changes export PLAN_ID --output PLAN.json --env NAME
benchling-tools changes status PLAN_ID_OR_FILE --env NAME
benchling-tools changes status PLAN_ID_OR_FILE --wait --timeout 300 --env NAME
benchling-tools changes status --task TASK_ID --wait --timeout 300 --env NAME
```

After applying an entry plan, use the returned `entry_id` with `entry inspect` or `results tables`. Entry creates use the V3 `POST /entry` route and are never automatically retried; a plan cannot be replayed after an attempt.

`changes plan` does not write to Benchling. `changes apply` re-checks the plan's preconditions, then writes. `changes export PLAN_ID --output FILE` writes a self-contained JSON bundle with the plan, checksum, and journal; it does not include credentials or the state-directory path. It refuses to overwrite an existing file unless `--force` is passed. Use the bundle with `changes status FILE`, `results pending --plan FILE`, and `results verify FILE` in another home, provided the active environment matches the plan. A plain plan JSON file can also be used if its applied journal is beside it as `<plan-id>.journal.json`. `changes status` reads each recorded result task once by default and stores returned task status locally. Pass `--wait --timeout SECONDS` to poll until all tasks are complete or the timeout expires.

`changes status --task TASK_ID` (MCP `changes_status` `task_id`) reads any Benchling background task by ID, for example one an app started itself with V2 `POST /assay-results:bulk-create`, when no plan of this toolkit exists. Pass exactly one of a plan ID or `--task`; both or neither is a `validation_error`, and so is a task ID that is empty or holds `/` or whitespace. It reads V2 [Get a task](https://docs.benchling.com/docs/api/v2/benchling-api/tasks/get-task) (`GET /tasks/{task_id}`) with the same poller and backoff as a plan, and `--wait` and `--timeout` work the same way ([long-running tasks](https://docs.benchling.com/docs/long-running-tasks)). The reply has `task_id`, `task_status`, `result_state` (`submission_running`, `submission_failed`, or `pending_integration`), `timed_out`, `remote_task` in full, and for a failed task Benchling's `message` and `errors`. For a succeeded [bulk-create](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/bulk-create-assay-results) task, whose response "contains the full list of results that were created", `record_ids` lists the V2 result IDs and `record_count` counts them. `response_kind` is `assay_results`, `other` (another kind of task: `response_keys` lists its keys and its contents are not interpreted), or `none`. Relay `next_step`: `results pending --id …` with up to 20 of the IDs, check `errors`, or run it again with `--wait`. Benchling keeps a task only for a while: an expired or unknown task ID is a 404 ("Task not found. It may have expired"), reported under `remote_task_error` with `result_state` `unknown` and a `next_step` that says running it again will not help. It reads only, so MCP needs no `--allow-writes`.

Container create plans name a `schema_id`; transfer plans name a `source_container_id`, a `destination_container_id`, and `destination_contents` (using `entity_id`). Review the preview before `changes apply`. Create re-checks the schema and transfer re-checks both container modification timestamps. They are single-use plans, and uncertain writes are not retried.

`examples/results-plate-reader.json`, `examples/results-multi-schema.json`, and `examples/results-invalid.json` are canned results specs with synthetic IDs. They are for smoke tests against the offline fake API (`uv run pytest tests/test_examples.py`), not for a real tenant.

Table, set cells. `examples/table-set-cells.json` is this shape. `table_part_id` is a `docpart_…` id.

```json
{
  "operation": "set_cells",
  "table_part_id": "docpart_example",
  "updates": [{ "row": 0, "column": 1, "value": "1.25" }]
}
```

Table, append rows:

```json
{
  "operation": "append_rows",
  "table_part_id": "docpart_example",
  "rows": [["D", "4"]]
}
```

Document. `position` is `beginning_of_document`, `end_of_document`, or a part id. A part id inserts or moves immediately before that part. Each inserted part needs `contentType`.

```json
{
  "operation": "insert",
  "position": "end_of_document",
  "parts": [{ "contentType": "text", "text": "Note" }]
}
```

```json
{ "operation": "delete", "from_part_id": "docpart_text1" }
```

```json
{
  "operation": "move",
  "from_part_id": "docpart_text1",
  "position": "end_of_document"
}
```

```json
{ "operation": "replace_text", "part_id": "docpart_text1", "text": "Edited" }
```

A move that would not leave the entry starting with a section fails in preview. No plan is saved.

Fields. Each key is a V3 system name or a field-definition id. Each value is an object with `value`.

```json
{ "schema_fields": { "temperature": { "value": 21 } } }
```

Result upload. `table_id` is an `strtbl_…` id from `results tables`; each batch may have up to 500 records (this toolkit's batch size; Benchling documents 4,000 per bulk-create request) and can use a different schema. A plan that would push a table past 1,000 rows is refused; check first with `results capacity`. Fields accept system names, field-definition IDs, or display names. Display names are translated to system names in the saved plan and wire payload; the preview shows each row's `field_name_map`, and ambiguous names are rejected. The results land as pending (see "Results land as pending" below). Read each task with `changes status PLAN_ID`, or wait with `changes status PLAN_ID --wait --timeout SECONDS`.
Preview validates supplied dropdown options and linked-object schemas using read-only calls. Invalid rows are reported with their plan-wide index and included in a validation summary; no result upload occurs.
For each record without `projectId`, preview uses the entry's project (or its folder's project) and reports the default in `after.project_defaults`. If no project can be read, set `projectId` on each record (`src_…`) or explicitly pass `null` for creator-only results. Some tenants reject null with `projectId is a required field`. Apply checks all batches and local files before uploading any blob; old plans missing `projectId` need a new preview. If result creation fails after upload, the error lists unlinked blob IDs. The V2 Blobs API has no delete operation; a new plan uploads the files again.

```json
{
  "table_id": "strtbl_example",
  "assay_results": [
    { "schemaId": "assaysch_example", "fields": { "ct": { "value": 12 } } }
  ]
}
```

Several tables can be submitted in one plan. Each batch has its own table, schema, and record list:

```json
{
  "batches": [
    { "table_id": "strtbl_example", "assay_results": [{ "schemaId": "assaysch_example", "fields": { "ct": { "value": 12 } } }] },
    { "table_id": "strtbl_other", "assay_results": [{ "schemaId": "assaysch_other", "fields": { "ct": { "value": 8 } } }] }
  ]
}
```

To attach a local file (an instrument file or a plot) to a result attachment field, give that field `{"file": "PATH"}`. Optional keys: `blob_type` (`RAW_FILE`, the default, or `VISUALIZATION` for an image preview) and `mime_type`. A relative path is resolved against the current directory. Only attachment fields accept a file. The preview's `after.uploads` lists each file's base name, size, SHA-256 and MD5; show them to the person, because apply uploads exactly that file. Apply refuses the plan if the file changed since the preview, uploads it as a blob once with no retry, then writes the result with the blob ID. Files over 100 MiB are refused. Do not plan a file the person did not name.

```json
{
  "table_id": "strtbl_example",
  "assay_results": [
    { "schemaId": "assaysch_example", "fields": { "ct": { "value": 12 }, "plot": { "value": { "file": "plots/run-01.png", "blob_type": "VISUALIZATION" } } } }
  ]
}
```

Apply submits each table separately. If a batch fails, other batches are still attempted and the journal reports successful, failed, or unconfirmed submissions; do not retry an uncertain batch automatically. For a failed task, errors with a valid `index` are linked to the zero-based input row and local correlation ID. Errors without a usable index are reported as unmatched with their raw value.

CSV update of existing results. `--id-column` names the result id. Each `--map` is `CSV_COLUMN=FIELD`. `FIELD` is the export display name, the system name, or the field-definition id. Unmapped columns are not written. In an attachment column, a cell `file:PATH` uploads that local file (relative to the CSV's folder) at apply; any other text is an existing blob ID.

```console
benchling-tools changes plan --entry etr_example --family results --csv assay.csv --id-column id --map Ct=Ct --map Note=Note --env NAME
```

## Results land as pending

Results created through the API do not go straight into a notebook results table. Benchling marks them pending. They stay out of the table, and have no entry (`entry` is null), until a person reviews and accepts (integrates) them in Benchling ([Integrating the Results into the Table](https://docs.benchling.com/docs/example-creating-results)). This is the expected behaviour in practice. Benchling documents it for V3 results created with `documentPartResultsTableId` and for V2 `assay-results:bulk-create` with a `tableId`, so do not assume V3 changes it. Results created without a table id are not attached to a table at all. The public V2 and V3 specs have no call that accepts pending results, so the toolkit cannot do it.

After a results create, tell the user, naming the entry and every submitted table, that the results are pending: Benchling has not added them to results table Y on entry X (LINK) yet, a person has to review and accept them in Benchling before they appear in the table, and until then they cannot be updated, because a results update needs a result that is on the entry. The apply response carries the same text in `next_step`, with the entry's `web_url` as the link, including partial or unconfirmed submissions where acceptance may be needed. Relay it every time; it states the state and is not a request for you to accept anything (Uploading Results to Benchling through the API, ["Integrating the Results into the Table"](https://docs.benchling.com/docs/example-creating-results)).

Check, do not assume. `results pending --plan PLAN_ID_OR_FILE` reads the plan's records back, including pending ones, and marks each `pending` or `accepted`; `needs_accept` lists the rows that are still pending review. `results verify PLAN_ID_OR_FILE` diffs the pushed records against the plan field by field and reports mismatched, missing, and extra rows. `results pending --entry ENTRY` reads back every applied result upload plan for that entry in this home, newest first, and lists them under `plans`; Benchling cannot list pending results by entry, so plans applied from another home are not found (use their exported bundle with `--plan`). Without a plan, use `results pending --id RESULT_ID` or `results pending --schema SCHEMA_ID --created-after TIME` (V2 has no entry filter, so that can include other entries' results). `results list --entry ENTRY` and `results rows PART_ID` do not show pending results. After acceptance they appear on the entry. Only then plan a results update. A result that is not on the entry is refused at plan time with "Result is not on the selected entry", and nothing is written.

When an app pushed results itself with the SDK (`AssayResultService.bulk_create`) and the push looks wrong, ask for the JSON body it sent and run `results verify --payload FILE` (MCP `results_verify` with `payload` as the JSON object, never a path) with `--created-after T` (optionally `--created-before T`) or `--id ID` for each created result. The body is what V2 [bulk create](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/bulk-create-assay-results) takes: `assayResults` with `schemaId`, `fields` and an optional client `id`, and an optional `tableId`; a list of results or a list of bodies also works, at most 4,000 results per body. The report is the same as `results verify PLAN_ID`, with `source.mode` `payload`; `plan_row_index` counts payload rows from 0 across bodies. `--entry ENTRY` only narrows accepted records: pending ones have no entry ([V3 result list](https://docs.benchling.com/docs/api/benchling-api/result/list)), so they are kept. The time window is per schema and V2 `GET /assay-results` has no entry filter ([list](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/list-assay-results)), so another push of the same schema in the window shows as `extra`; narrow the window or use `--id`. It reads only and saves nothing.

For table rows, the [Benchling V3 API reference](https://docs.benchling.com/reference) defines `submissionStatus` values including `SUBMITTED`, `HAS_UNSUBMITTED_CHANGES`, and `UNEVALUATED`; the latter means the table has not been evaluated since its last write. `HAS_UNSUBMITTED_CHANGES` means the row has changes not yet submitted. `isSubmittable` separately says whether the row can be submitted in its current state. These API fields are not substitutes for checking whether an uploaded assay result is pending acceptance: the toolkit uses the result's entry association for its `pending`/`accepted` read-back state. In particular, do not infer the UI's "Submitted" label from `isSubmittable`, or treat `UNEVALUATED` as a submitted result.

The accept control may insert every pending result that matches the table's schema, not only the ones just pushed. Do not create stray or test results against a table you care about.

## Working alongside the browser

Use the toolkit for every read, write, export and check. Use a browser only to see what a person sees. Accepting pending results is a review step for a person in Benchling; the public API has no call for it, and you do not do it. The toolkit writes as the app; the browser acts as the signed-in user, so the signed-in user needs access to the entry and the app needs access to its folder.

1. Get the entry's link. `benchling-tools entry get ENTRY --env NAME` returns a stable `web_url` field, taken from Benchling's `webUrl` ("the absolute URL of the entry in the Benchling web app", Get Entry by ID on docs.benchling.com). Check that its host is the tenant saved for NAME before you open it. If there is no `web_url`, search the tenant for the display ID.
2. Before you push results, open the entry and confirm its title, sections and results tables match `entry get` and `entry inspect`.
3. After `changes apply` for results, run `results pending --plan PLAN_ID`. Every row is `pending`.
4. Relay `next_step`: the results are pending, and a person reviews and accepts them in Benchling. Tell them how many rows you pushed to each table, so they can compare that with the pending count Benchling shows before accepting, because accepting may bring in every pending result for that table's schema. A page opened before the push does not show the new pending rows until it is refreshed. Accepted rows are appended to the bottom of the table, replacing blank trailing rows. If accepting would take the table past the notebook row maximum, Benchling refuses; split the results across tables. (Uploading Results to Benchling through the API, "Integrating the Results into the Table", and Creating a new Notebook Entry containing Results, on docs.benchling.com.)
5. Confirm with the toolkit, not just the page:
   - `results pending --plan PLAN_ID` shows every row `accepted`;
   - `results verify PLAN_ID` is ok;
   - `results list --entry ENTRY` and `results rows PART_ID` now return the rows.
   Only then plan a results update.
6. After a results update, the result record changes, but its notebook results-table cell can keep showing the old value. Do not ask for an accept for this: accepting applies only to pending results after a create (steps 4 and 5). Run `results get RESULT_ID` and read `table_check`, or run `results rows PART_ID` and read each row's `stale_cells`. If `table_check.status` is `stale`, or the read carries `warnings`, the cell is stale: ask the person to open that table in Benchling and use **Re-sync table**. Re-syncing is UI-only, because the public API has no re-sync call. Run the read again afterwards and confirm `table_check.status` is `in_sync` and there are no `warnings`.
7. After document or table writes, open the entry to confirm they render. Table formulas are re-evaluated only when the document is edited in the web app (Working with Tables, on docs.benchling.com), so a computed cell may show its input until then.

In the browser, never sign, archive, delete, share or send for review. Never edit templates, schemas or settings, and never accept results in an entry you were not asked to work in.

## When the toolkit cannot do it

This rule is mandatory. When the person asks for something this toolkit cannot do, ALWAYS:

1. Explain the limitation in plain words: what they asked for, what the toolkit does today, and why it stops (a missing command, an unsupported part type, a Benchling API limit, or a bug).
2. Ask whether they want it logged as a bug or a feature request on the open-source repository, jmamodeo/benchling-tools. Do not file anything yet.
3. Only after an explicit yes, build the issue with `benchling-tools issue draft` or the MCP tool `issue_draft`. Give it:
   - `kind`: `bug` (label `bug`) or `feature` (label `enhancement`)
   - `summary`: a one-line title
   - `attempted_action`, `expected`, and `actual`: what they tried and what happened
   - `interface` (`cli` or `mcp`) and `command_or_tool`; the toolkit version is filled in
   - `description`: a detailed description of the limitation and its impact
   - `proposed_solution`: a concrete way the toolkit could do it
   - `citations`: at least one official Benchling docs page (docs.benchling.com, help.benchling.com, or the API reference under benchling.com/api/) with the quoted line that proves the solution is possible, for example the endpoint or behaviour it relies on. Read the page before you quote it. If no Benchling doc supports a solution, say so and do not invent one.
4. Show the person the full title and body. File it only when they confirm that draft. The draft returns `new_issue_url`, a prefilled GitHub new-issue link they can open. `--file` runs `gh issue create`; use it only after that confirmation.

Never put tenant names or URLs, entry or record IDs from a private tenant, display IDs, secrets, tokens, or personal data in an issue. Use placeholders such as `your-tenant.benchling.com` and `etr_XXXX`. The draft is refused when a field looks like any of these.

```console
benchling-tools issue draft --spec issue.json
benchling-tools issue draft --spec issue.json --file
```

`issue.json` holds the fields above. `--cite-url` and `--cite-quote` can be repeated instead of `citations`, and every other field has a flag; run `benchling-tools issue draft --help` for the list. The MCP tool `issue_draft` takes the same fields as those flags (`attempted`, `solution`, and matching `cite_url` and `cite_quote` lists), is draft-only, and never files.

## Reading this skill from an install

`uv tool install benchling-tools` (or `uvx benchling-tools`) installs this file as `share/benchling-tools/SKILL.md` under the tool environment's prefix. These commands find it, or the repository copy in a source checkout, and never contact Benchling. `skill path` prints the absolute path and `skill show` prints the Markdown. If the file is missing, they print a `not_found` envelope:

```console
benchling-tools skill path
benchling-tools skill show
```

The MCP server serves the same file as the read-only resource `skill://benchling-tools/SKILL.md`.

## MCP server

If the benchling-tools MCP tools are already connected, call them with the same environment name. Do not put a client secret in the client config. The server reads the OS keychain.

The MCP tools are the CLI commands, one to one, generated from the same declarations. `benchling-tools entry resolve REF --env ENV` is the tool call `entry_resolve {env: ENV, reference: REF}`, and both reply with the same envelope. The naming rule:

- The tool name is the command path joined with `_`, with `-` also turned into `_`: `entry bulk-get` is `entry_bulk_get`, `changes apply` is `changes_apply`.
- Each argument or option is an argument with the parameter name from `--help`: `--part-offset` is `part_offset`, `--confirm-env` is `confirm_env`, and `--env` is `env`.
- Defaults, limits, and help text are the CLI's. Each tool description ends with its write class: reads only, writes a local file or plan, or writes to Benchling.
- `auth login`, `auth logout`, `auth protect`, `auth unprotect`, `skill path`, `skill show`, and `mcp serve` have no tool. Options that only make sense in a terminal have no argument: `changes plan --spec` and `--spec-json` (the tool's `spec` is the JSON object itself) and `issue draft --spec` and `--file`.

Reads, `export`, and `changes_plan` work when the server was started as:

```console
benchling-tools mcp serve
```

`changes_apply` works only for an environment named at startup. A protected environment that was not named is refused with a message that says so. A protected environment that was named also needs `confirm_env` equal to `env`; set it only after the person has confirmed that environment name for this plan, never by copying `env`:

```console
benchling-tools mcp serve --allow-writes ENV
```

Do not add `--allow-writes` unless the person asked to allow writes for that environment. `export` takes `kind`, `output`, `format`, and optional `part_id`, `entry`, and `schema_id`. It returns the same summary as the CLI and writes a local file only. `changes_export` takes `plan_id`, `output`, and optional `force`, and writes a self-contained portable plan bundle locally. `changes_apply` for entry creation returns the new entry ID and `web_url`; for results uploads it returns `web_url` and `next_step`: relay it to the user so they can accept pending results. `changes_plan` takes `env`, an optional `entry`, `family` (`entry`, `document`, `table`, `fields`, `results`, or `containers`), and `spec` (a JSON object with the same shapes as the `--spec` file, including table-specific result `batches`). Omit `entry` for entry and container plans. `changes_status` accepts a plan ID or portable plan file/bundle, plus `wait` and `timeout` to poll result tasks, and does not need `--allow-writes`. `results_pending` takes one of `entry`, `plan_id`, `result_ids`, or `schema_id` with `created_after`; `results_pending` and `results_verify` accept an applied result plan ID or portable plan file/bundle, read pushed results back including pending ones, and do not need `--allow-writes`.

Tools for this workflow: `auth_status`, `doctor`, `entry_list`, `schema_list`, `schema_find`, `entry_resolve`, `entry_inspect`, `entry_get`, `entry_bulk_get`, `folder_get`, `users_list`, `users_get`, `template_get`, `template_list`, `entry_fields_get`, `section_get`, `part_get`, `table_get`, `schema_get`, `container_get`, `container_find`, `container_schema_list`, `results_list`, `results_get`, `blob_get`, `blob_download`, `results_tables`, `results_capacity`, `results_rows`, `results_pending`, `results_verify`, `results_diff`, `export`, `changes_plan`, `changes_apply`, `changes_export`, `changes_status`, `issue_draft`. `auth_status` and `issue_draft` take no environment.
