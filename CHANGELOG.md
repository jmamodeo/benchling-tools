# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Read-only `results diff --left ENTRY[:RUN_VALUE] --right ENTRY[:RUN_VALUE]` (MCP `results_diff`) compares the results-table rows of two entries, or two runs on one entry, to show what changed between a first push and a rerun. Tables pair by exact name, with `tables_only_left`, `tables_only_right`, and `schema_mismatch` for tables it cannot compare. Rows pair by `--key` fields (or by position, with a warning), and `duplicate_keys` lists every key used more than once on either side with the count on each side (`{key, left: 1, right: 2, left_row_indexes, right_row_indexes}`, also counted in `counts.duplicate_keys`), so no row drops out of the diff unreported. Each table reports counts and samples of `only_left`, `only_right`, and `changed` fields. `--run-field` selects a run's rows locally, because Benchling has no server-side text-value filter. `--resolve-barcodes` reads container barcodes with V2 [Bulk get containers](https://docs.benchling.com/docs/api/v2/benchling-api/containers/bulk-get-containers), 100 ids per call. `--compare-blobs` compares attachment cells by SHA-256 ([Get blob file](https://docs.benchling.com/docs/api/v2/benchling-api/blobs/get-blob-file)). `--output FILE --format json|csv` writes every difference, like `export` (CLI only). Rows come from [List rows](https://docs.benchling.com/docs/api/benchling-api/document-part-results-table/rows-list); see [Working with tables](https://docs.benchling.com/docs/working-with-tables). A run value selects rows with the same match as `--where`, and archived and empty placeholder rows are not compared.
- Read-only `users list` and `users get` (MCP `users_list` and `users_get`) and server-side `entry list --creator` and `--author` filters accepting user IDs or handles.
- `entry list --sort` (MCP `entry_list` `sort`) orders entries by `created-desc`, `created-asc`, `modified-desc`, `modified-asc`, `name-asc`, or `name-desc`, sent as the V2 [List entries](https://docs.benchling.com/docs/api/v2/benchling-api/entries/list-entries) `sort` value (`createdAt:desc`, `createdAt:asc`, `modifiedAt:desc`, `modifiedAt:asc`, `name:asc`, `name:desc`). A sort uses V2 `GET /entries`, and the reply echoes the applied `sort` (or `null`). Without `--sort` nothing changes and no `sort` is sent. An unknown value is a `usage_error` listing the choices. A `next_token` from one sort must not be reused with another. `benchling-tools entry list --creator USER_ID --sort created-desc --limit 1` returns the newest entry that user created.
- `results pending --entry REF` (MCP `results_pending` `entry`) reads back the records of every applied result upload plan for that entry in this home, newest first, each marked pending, accepted, or archived with `v2_id` and `v3_id`. The entry may be an ID, display ID, or URL. `plans` lists each plan's `plan_id`, `applied_at`, and `record_count`. Benchling has no API filter for pending results by entry, so this reads local plans and journals; result updates and unapplied or failed plans are left out. With no matching plan the reply is empty and `next_step` points to `results pending --plan BUNDLE.json` or `--schema … --created-after …`.
- `results tables ENTRY --where FIELD=VALUE` (repeatable) adds `matching_rows` and `rows_scanned` per table, and `--name-prefix TEXT` keeps tables whose name starts with TEXT. `results rows TABLE --where FIELD=VALUE` returns only the page's matching rows with `scanned` and `matched`. FIELD is a display name, system name or field-definition id; link cells match on the linked id. The filter is client-side because Benchling has no text-value filter, and pending results are not counted. MCP `results_tables` takes `counts`, `name_prefix` and `where`, and `results_rows` takes `where`.
- Read-only `results capacity ENTRY [--plan PLAN_ID_OR_FILE | --spec FILE | --spec-json JSON] [--include-schema-pending --created-after T]` (MCP `results_capacity`) reports, per results table, `row_count` (V3 `rowCount`), `placeholder_rows`, `used_rows`, `pending_known`, `planned`, `remaining_without_pending`, `remaining_with_pending`, and `fits` against Benchling's 1,000-row table limit ([Capture data with Results](https://help.benchling.com/hc/en-us/articles/39954660101773-Capture-data-with-Results): "Result tables cannot have more than 1000 rows."). An empty placeholder row counts as 0 used. Benchling does not document whether pending results count, so `fits` is `unknown_pending` when rows fit only without them. It exits 0 even when a table does not fit, and `next_step` names the table.
- `container find` takes `--barcode` more than once (MCP `container_find` `barcodes` list) and looks the barcodes up 100 per V2 [List containers](https://docs.benchling.com/docs/api/v2/benchling-api/containers/list-containers) request, following `nextToken`. Several barcodes return `requested`, `found` (`barcode`, `container_id`, `schema_id`, `contents` with `entity_id`, `entity_registry_id`, `batch_id`, and `archived`), `invalid_barcodes`, `missing_barcodes`, and `empty_containers`. Barcodes Benchling lists in `invalidBarcodes` are reported, and the rest of that chunk is read again once, instead of failing the call. Duplicates are dropped and order is kept.

### Changed

- **Breaking:** the MCP `container_find` argument `barcode` (a string) is now `barcodes` (a list of strings). One barcode keeps the one-page `items` and `next_token` reply.
- A 400 from V2 `GET /containers` that lists `invalidBarcodes` now puts them in `error.details.invalid_barcodes`, with the hint "Benchling did not recognise these barcodes: …". It is still a `validation_error` with exit code 2. A comma inside one `--barcode` value is still rejected, and the message says to repeat `--barcode`.
- `results tables` adds `part_id` (`docpart_…`), `row_count`, `submission_status` and `placeholder_rows` to each table from one V3 parts listing; `--no-counts` keeps the single V2 read. The counts come from the same capacity helper `results capacity` and `changes plan` use, so a table has one row count everywhere.
- `changes plan --family results` checks every planned table's capacity with the same helper as `results capacity`, before any file is staged. A plan that would push a table past 1,000 rows is a `validation_error` with `details.reason` `table_capacity_exceeded` and per-table numbers in `details.tables`; a plan that fits only when pending results are ignored is saved with an `unknown_pending` warning.
- The 500-records-per-table error no longer calls 500 a Benchling limit. It is this toolkit's batch size; Benchling documents 4,000 results per bulk-create request ([bulk create](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/bulk-create-assay-results)). The 500 cap is unchanged.
- A `usage_error` for an unknown option lists that command's long options in `details.valid_options` and, where known, the right alternative in `details.hint`: `results pending` names its four sources, and `results list --plan` points to `results pending --plan` or `results verify`. An extra positional argument that looks like an entry reference (`results pending EXP-1`, `results list etr_…`) gets the hint `Pass the entry with --entry REF`, and a plan ID gets `--plan`.
- Result upload plans accept schema display names as field keys, translate them to system names, and show the mapping in the preview. An ambiguous name is reported as an `ambiguous_field` row problem alongside the row's other problems, and unknown-field errors list `available_display_names` only where display names are accepted (not entry field updates).

### Fixed

- Result upload plans validate unconstrained links and decimal bounds correctly and report multiple field constraint problems per row, including invalid or unavailable dropdown options.
- Result uploads default missing `projectId` values to the entry's project at preview, reject unresolved projects, check all batches before uploading blobs, and report unlinked blobs after failed creation. Explicit null remains creator-only and may be rejected by some tenants.

## [0.7.0] - 2026-11-22

### Breaking

- To upgrade from 0.6.0: install the `benchling-tools` package and command (there is no `benchlingtk` command or `import benchlingtk` alias), pass `--env` after the command name (`benchling-tools entry get REF --env NAME`, not `benchling-tools --env NAME entry get REF`), and move the state directory from `benchlingtk` to `benchling-tools` under the same platform config root, or set `BENCHLING_TOOLS_HOME` to the existing path. Existing keychain logins carry over; the keychain service name remains `benchlingtk`.
- Renamed the distribution and command from `benchlingtk` to `benchling-tools` and the Python
  module to `benchling_tools`; there is no old command or import alias. Update scripts and MCP
  client launch configurations. The MCP skill resource is now `skill://benchling-tools/SKILL.md`.
  MCP tool names are unchanged.
- `BENCHLING_TOOLS_HOME` replaces `BENCHLINGTK_HOME`, and the default state directory changes
  from `benchlingtk` to `benchling-tools` under the same platform config root. Nothing is moved
  automatically: move the old state directory to the new location, or set
  `BENCHLING_TOOLS_HOME` to its existing path. This preserves environments, plans, and journals.
  The keychain service name remains `benchlingtk`, so existing credentials remain usable.

### Added

- `blob download` (MCP `blob_download`) streams a blob UUID or `attachment_…` ID to a local file and reports size, SHA-256 and MD5. Optional expected hashes, size, or a reference file verify the download before it replaces the destination; without expectations the summary says `verified: false`. Existing files require `--force` to replace.
- `changes export PLAN_ID --output FILE` writes a self-contained JSON bundle of a plan, its checksum, and journal. `results pending --plan`, `results verify`, and `changes status` accept that bundle; a plain plan file still works with its applied sibling journal. The active environment must match.
- CI exercises real macOS and Windows keyring set/get/delete operations.
- `results list --id` (MCP `results_list` `result_ids`) lists results by id, up to 100, as V3 `id.anyOf`. Each id may be a V3 result id (`result_…`) or a V2 assay result UUID; a UUID that cannot be mapped is listed in `not_found` with `id_namespace` and `lookup`.

- CLI paging options that only the MCP tools had: `entry get --full` takes `--part-offset`, `--part-limit`, `--result-offset`, `--result-limit`, `--table-row-offset` and `--table-row-limit`; `section get` takes the part and table-row options; `part get` and `table get` take `--row-offset` and `--row-limit`; `schema get` takes `--field-offset` and `--field-limit`; and `results tables` takes `--offset` and `--limit`. Paged output has a `pagination` object.
- `changes plan --spec-json TEXT` takes the change spec as inline JSON. Only one of `--spec`, `--spec-json`, and `--csv` may be passed.
- `results rows` (MCP `results_rows`) accepts a `v2_structured_table` id (`strtbl_…`) from `results tables` with the new `--entry ENTRY` (MCP `entry`). It is mapped to the entry's matching `v3_document_part` (`docpart_…`) results table, the same match `export --kind results-table` uses, and the page adds `table_id`, `entry_id`, `id_namespace` and `listed_id_namespace`. A `strtbl_…` id with no entry, not on the entry, or with no single matching part is a `validation_error` whose `details` carry `id_namespace` and `expected_id_namespace`; it is no longer sent to Benchling and returned as `not_found`.
- Every Benchling-id argument and option names its id namespace in its help, and so in the generated MCP tool descriptions: V3 result ids (`result_…`) and V2 assay result UUIDs, both accepted, for `results list`, `results get` and `results pending --id`; `strtbl_…` from `results tables` versus `docpart_…` for `results rows`, `table get`, `part get` and `section get`; blob UUIDs and `attachment_…` for `blob get` and `blob download`; and the entry (`etr_…`), folder (`lib_…`), project (`src_…`), template (`tmpl_…`), container (`con_…`) and result schema (`assaysch_…`) prefixes. `schema get` says which prefix selects which schema kind.
- Every MCP tool description ends with a generated write class: reads only; writes a local file or plan (`blob_download`, `export`, `changes_plan`); or writes to Benchling (`changes_apply`), with its gating rule.
- A command line that does not parse (an unknown command or option, a missing argument or subcommand, or an invalid or out-of-range value) prints a JSON envelope on stdout with `error.code` `usage_error` and `command`, `usage`, and `help` in `error.details`, and exits 9 (`ExitCode.USAGE`).
- `results rows` (MCP `results_rows`) detects stale results-table cells. After `PATCH /result/{id}`, a cell can keep the old value. Each row that links a result is compared with that record when its `submissionStatus` is `SUBMITTED`, `UNEVALUATED` or absent. Each column is matched to its field through `fieldDefinitionApiId`. A row that disagrees carries `warning` and `stale_cells` (column, field-definition id, `table_value`, `result_value`). The page carries `stale_check` (`compared_rows`, `stale_rows`, `unreadable_results`), plus `warnings` telling the person to use **Re-sync table** in the Benchling UI. Each linked result is read once with `GET /result/{id}`. Archived rows and `UNSUBMITTED` or `HAS_UNSUBMITTED_CHANGES` rows are not compared.
- `results get` (MCP `results_get`) adds `table_check`. It finds the results-table row on the record's entry that links the result, and reports `in_sync`, `stale`, `not_compared`, `no_table_row`, `not_on_entry`, or `unavailable`. A stale row adds `warnings` with the same **Re-sync table** advice. This reads the entry's parts and its results tables of the record's schema.
- Re-syncing a results table is documented as UI-only. The recorded V2 and V3 specs have no re-sync call, so no re-sync command is added.

### Changed

- **Breaking:** the Typer dependency is limited to versions before 0.29, whose CLI parameter interface breaks the MCP help-text parity check.
- **Breaking:** `blob get` accepts V3 `attachment_…` IDs as well as V2 blob UUIDs, and its metadata includes `id_namespace`. Attachment downloads use V3 directly; no undocumented ID conversion is assumed.
- **Breaking:** new result-upload journal events use `create_results` instead of `submit_results`. Existing journals with `submit_results` remain readable for status and result read-back.
- **Breaking:** the MCP dependency is limited to versions before 2.0.
- **Breaking:** `results get`, `results list`, `results pending --id` and `results verify` (and their MCP tools) accept either a V2 assay result UUID or a V3 result id (`result_…`), and return both. `results get` maps a UUID to its V3 record with V2 `GET /assay-results:bulk-get` and its `v3Id`; `results pending --id` reads a `result_…` id with V3 `GET /result/{id}` and finds its V2 record by schema, creation time and `v3Id`, so a V3 id is never sent to `assayResultIds`. `results get` and each `results list` item add `v2_id` and `v3_id` (`v2_id` is null when Benchling does not return it); each `results pending` item adds `v2_id` beside `id` and `v3_id`; `results verify` rows and `needs_accept` add `v2_id` and `v3_id` beside `result_id`. A plan whose task returned only V3 ids is read back too.
- **Breaking:** an id that cannot be read is reported with its namespace. `results get` returns a `not_found` envelope whose message and `details` give `id_namespace` (the namespace passed: `v3_result`, `v2_assay_result`, or `unknown`), `expected_id_namespace`, `lookup` (the API path used) and `accepted_id_namespaces`. A V2 bulk-get 400 is a `validation_error` with the same fields, and `results pending` adds `not_found_details` with them for each id in `not_found`.
- **Breaking:** `--env` (`-e`) is an option of each command that needs an environment, and the root `--env` is removed. Write `benchling-tools entry get REF --env NAME` (the option may come anywhere after the command name), not `benchling-tools --env NAME entry get REF`; the old form is a `usage_error` whose `details.hint` says where `--env` goes. `changes apply` on a protected environment still needs `--env` typed on the command line and `--confirm-env` repeating it. MCP tools keep their required `env` argument.
- **Breaking:** usage errors exit 9, not 2, and print a `usage_error` envelope, not a text help box. Exit 2 now means only `validation_error`. CLI-enforced limits and choices (for example `--limit 0` or `--family magic`) are usage errors; the same values passed to an MCP tool stay a `validation_error`. A bare `benchling-tools` still prints the help, and exits 9.
- **Breaking:** the `--install-completion` and `--show-completion` options are removed.
- Docs repositioned: README.md and SKILL.md describe benchling-tools as a CLI for coding agents and humans, directly mirrored as an MCP server for chat agents. Both state that there is no public Python API, that the Python modules are internal (the package docstring says so too), and point Python scripting to the official Benchling SDK; a public API may be added if enough people ask in a GitHub issue. The README is shorter: the full CLI and MCP reference moved to `docs/reference.md`, the demo transcript to `docs/demo.md`, the module map to `ARCHITECTURE.md`, and the test details and sandbox exercise to `CONTRIBUTING.md`. No command, tool, argument, or behavior changed.
- **Breaking:** MCP `changes_apply` takes `confirm_env`. A protected environment needs `confirm_env` equal to `env` even when it is listed in `--allow-writes`, the same rule as `changes apply --confirm-env`, and a `confirm_env` naming another environment is refused for any environment. Agents set it only after the person confirms the environment name.
- **Breaking:** the MCP server is generated from the CLI. Each command declared in `cli.py` is one tool named after its command path, with the same arguments, defaults, limits, and help text, so no tool is written by hand and the two cannot drift. Commands that need a terminal or change local credentials (`auth login`, `auth logout`, `auth protect`, `auth unprotect`, `skill path`, `skill show`, `mcp serve`) have no tool. Tool renames: `list_environments` → `auth_status`, `get_capabilities` → `doctor`, `resolve_entry` → `entry_resolve`, `get_entry` → `entry_get`, `inspect_entry` → `entry_inspect`, `get_entries` → `entry_bulk_get`, `list_entries` → `entry_list`, `get_entry_fields` → `entry_fields_get`, `get_folder` → `folder_get`, `get_template` → `template_get`, `list_templates` → `template_list`, `get_section` → `section_get`, `get_part` → `part_get`, `get_table` → `table_get`, `get_schema` → `schema_get`, `list_schemas` → `schema_list`, `find_schema` → `schema_find`, `list_results` → `results_list`, `get_result` → `results_get`, `list_result_tables` → `results_tables`, `list_results_table_rows` → `results_rows`, `read_back_results` → `results_pending`, `verify_results` → `results_verify`, `get_blob` → `blob_get`, `get_container` → `container_get`, `find_containers` → `container_find`, `list_container_schemas` → `container_schema_list`, `get_change_status` → `changes_status`, `plan_changes` → `changes_plan`, `apply_changes` → `changes_apply`. `export` and `issue_draft` keep their names.
- **Breaking:** tool arguments follow the CLI flags. `changes_plan` takes `spec` (was `specification`), `export` takes `output` (was `path`), `issue_draft` takes `attempted`, `solution`, and matching `cite_url` and `cite_quote` lists (were `attempted_action`, `proposed_solution`, and `citations`), and `results_list` takes `page_size`. `auth_status` returns the same rows as `benchling-tools auth status`, plus `writes_allowed`.
- `doctor` and the `doctor` tool return an `authentication_error` envelope, with the checks in `details`, when a check fails. CLI failures after the environment was opened carry `env` and `tenant_url` in `meta`, as MCP failures do. An out-of-range paging argument on a tool is a `validation_error` envelope.
- Every CLI argument and option has help text.
- **Breaking:** the package no longer ships `py.typed` and drops the `Typing :: Typed` classifier, because benchling-tools has no public Python API. Type checkers no longer read its inline types; script Benchling from Python with the official Benchling SDK.
- The docs offer one install route, `uv tool install benchling-tools` (or `uvx benchling-tools` without installing); the `pip install` instructions are removed. A checkout still installs with `uv tool install .`. The 0.6.0 notes no longer present `import benchlingtk` as a supported API.

### Fixed

- CI explicitly enforces 96% coverage, uses locked dependencies and pinned actions and tooling, and checks the skill name against package metadata rather than the checkout directory.
- SKILL.md step 6 no longer says to accept an updated result with the plug icon. After a results update, the agent checks `results get` `table_check` or `results rows` `stale_cells`. If a cell is stale, it asks the person to use **Re-sync table** in the Benchling UI. The plug stays documented only for accepting pending results after a create.

## [0.6.0] - 2026-11-21

### Changed

- **Breaking:** the distribution is renamed from `benchling-toolkit` to `benchlingtk`, the internal import package from `benchling_toolkit` to `benchlingtk`, and the command from `benchling` to `benchlingtk`. Install with `uv tool install benchlingtk` and run `benchlingtk` and `benchlingtk mcp serve`; update MCP client configs to `uvx benchlingtk mcp serve`. The old install name and `benchling` command are not provided. `benchlingtk --version` prints `benchlingtk 0.6.0`.
- **Breaking:** the installed data files are under `share/benchlingtk/` (`SKILL.md`, `schemas/`, `contracts/`), the MCP skill resource is `skill://benchlingtk/SKILL.md`, the `SKILL.md` skill name is `benchlingtk`, and schema `$id` values use the new name.
- Repository links point at `https://github.com/jmamodeo/benchlingtk`, and `benchlingtk issue draft` and MCP `issue_draft` file issues there. Older changelog text is unchanged.
- **Breaking:** saved state moves to the new name and is not migrated. The OS keychain service name is now `benchlingtk` (was `benchling-toolkit`), the state directory is now the platform config directory plus `benchlingtk` (was `benchling-toolkit`), and the override variable is now `BENCHLINGTK_HOME` (was `BENCHLING_TOOLKIT_HOME`). Existing users must `uv tool install benchlingtk` and run `benchlingtk auth login` again for each environment. Plans and journals under the old state directory are not read; finish or discard pending plans before upgrading. The old keychain item and directory can be removed by hand.

## [0.5.3] - 2026-11-20

### Fixed

- Results tables whose rows have been accepted no longer fail `entry get --full`, `entry inspect`, `results rows` and `export --kind results-table` with `Table response contained invalid rows`. The API reference types a row's `result` as `ResultRef`, but Benchling sends the concrete `__typename` (for example `Result`). A row's `result` is now any object with a non-empty string `id`; its `__typename` (`Result`, `ResultRef`, any other string, or absent) is informational and kept in the normalized row. A `result` that is not an object, has no string `id`, or has a non-string `__typename` is still rejected, as is every other row problem. A rejected row now names the failing check in the error `details` (`check`: `row`, `cells`, `id`, `rowId`, `rowIndex`, `isArchived`, `isSubmittable`, `submissionStatus` or `result`) alongside `part_id` and `row_index`, never values.

## [0.5.2] - 2026-11-20

### Added

- The pip package ships `SKILL.md`. It is installed as `share/benchling-toolkit/SKILL.md` under the environment prefix, next to the JSON schemas and contracts, so an agent can read the skill without a clone of the repository.
- `benchling skill path` prints the absolute path of the installed `SKILL.md`, and `benchling skill show` prints its contents. They look under `sys.prefix`, `sys.base_prefix`, and the user install scheme, then fall back to the repository's `SKILL.md` for editable and development installs. A missing file is a `not_found` envelope. Both are read-only and never contact Benchling.
- The MCP server serves `SKILL.md` as the read-only resource `skill://benchling-toolkit/SKILL.md` (`text/markdown`).
- CI builds the wheel and checks that it contains `SKILL.md` with the same bytes as the repository copy (`pytest -m wheel`). It also installs the wheel and checks that `benchling skill path` and `benchling skill show` return that file.

### Fixed

- Results tables evaluated in the Benchling web app no longer fail `entry get --full`, `entry inspect`, `results rows` and `export --kind results-table` with `Table response contained invalid rows`. A cell's `evaluationValue` is now read leniently: known BenchlingValue types map as before, a `*CellEvaluationValue` such as `ObjectLinkCellEvaluationValue` becomes `{type: "<typename>", id, name, ...}`, and any other typename becomes `{type: "unknown", typename}`. A BenchlingValue with no `value` reads as `null`, and a cell's `errors` list (`errorType`, `message`, `type`) is passed through. `export --kind results-table` uses `inputValue` when the evaluation has no `value`, the fallback the Benchling "Working with Tables" guide describes. `inputValue` stays typed, and the row checks (cells list, `rowIndex`, `submissionStatus`, `ResultRef`, cell count against `columnDefinitions`) still apply; a rejected row or cell now puts `part_id`, `row_index`, `cell_index` and `typename` in the error `details`, never cell values.
- `benchling entry get --help` and `benchling entry list --help` now mention the `web_url` field, the entry's link in the Benchling web app, as `entry resolve` and `entry bulk-get` already did. The MCP tools that return an entry already described it.

## [0.5.1] - 2026-11-20

### Added

- Entry output has a `web_url` field, the entry's link in the Benchling web app, taken from V3 `webUrl` or V2 `webURL` (`null` when Benchling returns none). It is in `entry resolve`, `entry get` (with or without `--full`), `entry list` and `entry bulk-get`, and in the MCP tools `resolve_entry`, `get_entry`, `list_entries` and `get_entries`. `changes apply` and MCP `apply_changes` return `web_url` for an entry created from a template and for a results upload. The pending-results `next_step` from `changes apply`, `results pending --plan` and `results verify` now includes the link, not only the entry ID.

### Changed

- `SKILL.md` no longer forbids an agent from using a browser. The toolkit still uses only the public HTTP API and never drives a browser, but an agent with a browser signed in to the same tenant may use it to view entries and accept pending results. A new "Working alongside the browser" section says when to use each: every read, write, export and check goes through the toolkit, and the browser views, confirms, and clicks the plug icon. It also says the toolkit writes as the app and the browser acts as the signed-in user. It opens the entry from `web_url`, checks pending and accepted rows with `results pending` and `results verify`, and lists what never to do in the browser. Benchling docs are named by title.
- `SKILL.md` allows `auth login` when the person has given the agent the credentials for that environment, and still forbids it otherwise or with invented credentials.

### Fixed

- The MCP tool list in `SKILL.md` names every tool the server registers. It no longer lists `inspect_entry_template`, which does not exist, and now includes `list_environments`, `get_capabilities`, `find_schema`, `get_template`, `list_templates`, `get_container`, `find_containers`, and `list_container_schemas`.

## [0.5.0] - 2026-11-20

### Added

- Result attachment fields can take a local file. In a results upload, an attachment field value of `{"file": PATH}` (optional `blob_type` `RAW_FILE` or `VISUALIZATION`, optional `mime_type`) is hashed at `changes plan`; in a CSV update, an attachment cell `file:PATH` does the same, relative to the CSV's folder. The preview lists each file's base name, size, SHA-256, MD5, MIME type and blob type, never its path or contents. `changes apply` reads each file again, refuses it with a conflict if its size or SHA-256 changed, uploads it as a V2 blob (`POST /blobs` up to 10,000,000 bytes, otherwise multipart in 10 MiB parts with an abort if any step fails), journals the blob ID, and then sends the result write with the blob ID. Uploads are never retried. Files over 100 MiB, more than 1 GiB in one plan, empty files, non-regular files, and files inside the toolkit state directory are refused.
- `benchling blob get BLOB_ID` and the read-only MCP tool `get_blob` read a blob's metadata (name, type, MIME type, upload status) with V2 `GET /blobs/{blob_id}`. Nothing is downloaded.
- `benchling results pending` and the MCP tool `read_back_results` read results back, including pending rows that are not yet in the entry's table, and mark each `pending`, `accepted`, or `archived`. The source is an applied result upload plan (`--plan`, using the journal's record IDs), explicit V2 result IDs (`--id`, V2 `GET /assay-results:bulk-get` in chunks of 200), or a result schema with a created-after time (`--schema` and `--created-after`, one page of V2 `GET /assay-results`). Output is bounded by `--limit`, lists pending rows first with `needs_accept`, and `next_step` says where to click the plug icon.
- `benchling results verify PLAN_ID` and the MCP tool `verify_results` diff an applied result upload plan against the records Benchling created, field by field by system name. They report matched, mismatched, missing and extra rows with counts and at most `--sample` rows each, plus the rows that still need the plug-icon accept. Both commands only read.
- Protected environments. `auth login --protected` (or `auth protect --env NAME` later, and `auth unprotect` to clear it) marks an environment protected in `environments.json`; a later login without `--protected` or `--unprotected` keeps the saved setting. `changes apply` on a protected environment is refused unless `--env` names it on the command line and `--confirm-env NAME` repeats the name. A `--confirm-env` that names a different environment is refused for any environment. MCP `apply_changes` still needs the environment in `--allow-writes`, and its refusal says the environment is protected. `doctor` reports `protected` and a `protection` line, and `auth status` and MCP `list_environments` report `protected`. Reads and previews are unaffected.
- Canned results fixtures in `examples/`: a plate-reader style table, a multi-schema push, and an invalid payload for the validation error path, all with synthetic IDs. `tests/test_examples.py` runs them through `changes plan` and `changes apply` against the offline fake API.
- Added read-only bulk entry lookup by IDs or display IDs (chunked at 100 IDs), folder retrieval, paged template listing, and V2 entry-template structure summaries in the CLI and MCP server.
- Results plans can upload several table-specific batches, including different result schemas, while preserving the 500-record cap per table. `changes status --wait --timeout SECONDS` and MCP `get_change_status` can poll each task; failed-task errors with an in-range `index` are linked to that table's input row and local correlation ID, and other error shapes remain visible as unmatched.
- `changes plan --family entry` previews creation from an entry template in a folder. It reads and validates the V3 template and folder, supports optional schema fields, and applies once with V3 `POST /entry`. The create response's entry ID is returned for inspection; the plan is not reusable after any attempt.
- `schema list` includes entity schemas, and `schema find NAME` returns exactly one matching schema's typed field definitions, required flags, and dropdown options. Result-plan previews validate dropdown and linked-object values without writing; errors identify every invalid row and summarize problem counts.

### Changed

- Relicensed under the Apache License 2.0, with a `NOTICE` file. Versions up to and including 0.4.1 remain available under the MIT License.

## [0.4.1] - 2026-11-19

### Added

- Agents follow a mandatory rule, written into `SKILL.md`, the MCP server instructions, `AGENTS.md` and the README: when the toolkit cannot do what the person asked, the agent explains the limitation and asks whether to log a bug or a feature request on the repository. Only after an explicit yes does it draft the issue, show it, and file it once the person confirms.
- `benchling issue draft` and the MCP tool `issue_draft` build that issue from structured fields: kind, summary, attempted action, expected and actual behaviour, interface, command or tool, description, proposed solution, and Benchling docs citations. The toolkit version, Python version and OS are filled in. Each citation must be an https URL on docs.benchling.com, help.benchling.com, or the API reference under benchling.com/api/, with a quoted line. A draft is refused when any field looks like a tenant URL, a Benchling record or display ID, a token or secret, or an email address. The output is a title, a body, the `bug` or `enhancement` label, and a prefilled GitHub new-issue URL. Nothing is filed by default; the CLI's `--file` runs `gh issue create`, and the MCP tool never files.
- Bug and feature issue forms in `.github/ISSUE_TEMPLATE` ask for the same fields and require a Benchling documentation citation. They replace the Markdown templates.
- After a successful results upload, `changes apply` and the MCP tool `apply_changes` return a `next_step` that names the entry and results table and tells the user to click the plug icon to accept the pending results.
- `SKILL.md` has a "Results land as pending" section. Results created through the API are not put in a notebook results table directly. They are pending, with no entry, until a person accepts them in the table. Benchling documents this for V3 (`documentPartResultsTableId`) and for V2 bulk create. The accept control may insert every pending result that matches the table's schema, so stray results should be avoided.

### Changed

- A results update that names a result not on the entry now says that pending results must be accepted in the table first. The README, `docs/api-limitations.md`, `ARCHITECTURE.md`, the MCP tool descriptions and `agensi/listing.yaml` use the same wording.
- The README and setup guide say `benchling-toolkit` is on PyPI and show `uv tool install benchling-toolkit`, `pip install benchling-toolkit` and `uvx --from benchling-toolkit benchling mcp serve`.

## [0.4.0] - 2026-11-19

### Added

- `.github/workflows/publish.yml` publishes to PyPI when a GitHub release is published. It builds the sdist and wheel, runs `twine check --strict`, requires the release tag to equal `v` plus the package version, and uploads with PyPI trusted publishing (OIDC, GitHub environment `pypi`). No PyPI token is stored in the repository.
- `SKILL.md` tells an agent when to use benchling-toolkit. It covers listing, reading entries, tables, and results, exporting to a local file, and previewing a change with `changes plan` before `changes apply`. The MCP server is read-only unless started with `--allow-writes ENV`. Credentials come from `benchling auth login --env NAME` and stay in the OS keychain. Frontmatter uses the Agent Skills fields only. `summary`, `version`, and `docs_url` are strings under `metadata`. `tags`, `keywords`, and `use_cases` are lists in `agensi/listing.yaml`.
- A weekly GitHub Actions workflow, `.github/workflows/openapi-drift.yml`, downloads the public V3 OpenAPI spec from the URL in `contracts/openapi-manifest.json` and fails when the SHA-256 differs. The failure names each `routes_used` path whose operation or referenced definition changed. The workflow also runs on `workflow_dispatch`. The public spec needs no tenant and no credentials. `scripts/check_openapi_drift.py` performs the comparison. Its tests use fixture specs and stay offline.
- `ARCHITECTURE.md` explains the plan, apply, and journal flow, with a sequence diagram.
- `docs/threat-model.md` covers credentials, stale or replayed plans, write scoping, and agent misuse. It names the code for each control and the cases that code does not close.
- The README embeds a GIF of the offline fixture demo and links an asciinema cast built from the same `scripts/demo.py` output. The session uses the in-process fake API. No live tenant is contacted.
- `docs/setup.md` explains how to create a Benchling app, grant it project access, log in with `benchling auth login --env NAME` (OS keychain, including headless login), and keep separate read and write environments.

### Changed

- The README install section shows `uv tool install benchling-toolkit` and `uvx --from benchling-toolkit benchling mcp serve`, and states that those commands work once the package is published to PyPI. Until then, the checkout forms are `uv tool install .` and `uvx --from . benchling mcp serve`.
- MCP `plan_changes` takes a typed `specification` for document, table, field, inline result, and CSV result-update changes. A value that does not match is a `validation_error` in the standard envelope, and nothing is planned. Every MCP tool parameter has a description. The CLI still accepts the same JSON specs.

### Added

- `benchling changes plan --family results` accepts a CSV file. `--id-column` names the result id and each `--map CSV_COLUMN=FIELD` maps a column to a field display name, system name, or field-definition id. The plan resolves those names to field-definition ids, previews only the values that change, and writes nothing until apply. Apply sends `PATCH /result/{id}` after re-checking each result's `modifiedAt`. Unknown columns and bad rows are reported at plan time. An assay-result export CSV round-trips when the mapped columns are that export's headers.
- `benchling export` and the MCP tool `export` write an unstructured table, a results table, or assay results to a JSON or CSV file. The response is a summary only: row count, column names, file path, and the SHA-256 checksum of the file. Pages are read internally. An existing file is left unchanged unless overwrite is set. Benchling is only read.
- `benchling results rows PART_ID` and the MCP tool `list_results_table_rows` read one page of a results table from `GET /document-part-results-table/{id}/rows/items`. The page size defaults to 50 and stops at 100. The response uses the standard JSON envelope and includes `next_token`.

### Fixed

- `export --kind assay-results` writes one column per result-schema field. V3 results keep those values in `schemaFields`, and the column name is the field's display name. Links and entity references are written as ids.
- An unset assay field stays empty. `linkedEntityId` is written only for a link field when it names a different entity. CSV writes `""` and JSON writes `null`.
- `export --kind results-table` and `--kind table` flatten link and entity cells to ids, the same values assay-result export writes.
- `export --part` accepts a `strtbl_…` id from `results tables` when `--entry` is that entry and `--kind` is `results-table`. The id is resolved to the entry's V3 results-table part, or the rows are read as assay results for that entry and schema. A `strtbl_…` id without `--entry`, or with `--kind table`, is a validation error that names the `v3_document_part` id from `entry inspect`.
- `entry list --name` accepts a name that contains a comma. `name.anyOf` and `names.anyOf` are comma-separated and document no escape, so that name is sent as the V2 `name` parameter together with the other filters.
- A modified timestamp with no time zone is a validation error. `YYYY-MM-DD` is still the start or end of that UTC day.
- Entry and schema lists return `modified_at` as a UTC timestamp with a `Z` suffix. V2 `+00:00` values and other offsets are converted.

### Fixed

- During `auth login`, the keychain error's `dbus-run-session` command fills in the environment name, the normalized tenant URL, and the client ID that were passed or already saved. Each value is shell-quoted. `$KEYRING_PASSWORD` and `$BENCHLING_CLIENT_SECRET` stay variable references. Omitted values, and errors from other commands, keep the placeholders.
- The keychain error is the cause, one fix command, and a pointer to the README Headless Linux section. `dbus-run-session` also runs `benchling auth login` in that same shell. It says `DBUS_SESSION_BUS_ADDRESS` is unset only when it is unset, and says the bus has no Secret Service when the variable is set but the bus has no keyring. The MCP client `env` block and cron note is included only when `benchling mcp serve` raises the error.
- The README Headless Linux section no longer says `gnome-keyring-daemon --unlock` prints `DBUS_SESSION_BUS_ADDRESS`. It shows a long-lived `dbus-daemon --session --fork` bus that MCP, later shells, and cron can share, notes a systemd user bus, and says a wrong `KEYRING_PASSWORD` for an existing login keyring hangs the first keyring call.
- Closing MCP stdin after a tool call exits 0 and does not print `MCP server failed to start` / `Event loop is closed`.
- A token request that cannot reach the host says to check the tenant URL. The raw transport error, including SSL text, stays in `details.reason`. `details` also includes `env` and `tenant_url`.
- A 401 from token exchange says `Benchling at https://<tenant> rejected the client ID or secret.` `details` includes `env`, `tenant_url`, and `status`. `--secret-stdin` with an empty stdin says `No client secret was received on stdin`.

### Added

- `benchling entry list` and the MCP tool `list_entries` list entries by project, folder, schema, modified date range, and exact name. Filters are server-side. A folder filter uses V2 `GET /entries`, because V3 `GET /entry/items` ignores `folderId`. The other filters use V3 unless a folder is set, in which case the whole query stays on V2. Pages default to 50, cap at 100, and return `next_token`.
- `benchling schema list` and the MCP tool `list_schemas` list entry, result, and run schemas (`/entry-schema/items`, `/result-schema/items`, `/run-schema/items`), following every upstream page up to the caller's limit.
- `schema get` and `get_schema` include each dropdown field's options from `/dropdown/{id}` and `/dropdown/{id}/options/items`.
- `contracts/list-parameters.json` records the query parameters entry and schema lists may send. Tests reject a list call that adds a parameter outside that list. Write bodies are checked against the vendored OpenAPI snapshots.

### Fixed

- A document move that would not leave the entry starting with a section fails in preview with `Entry must start with a section`, before a plan is saved or anything is written.
- Apply journal events record the raw HTTP status of each write (`http_status`).

### Added

- `benchling auth login` accepts `--tenant-url` and `--client-id`. Together with `--secret-stdin`, login runs with no terminal and does not call `getpass`, so there is no echo warning. A missing value with no terminal is one error that names the flags.
- Keychain failures name the cause (no Secret Service backend, D-Bus session not reachable, or a locked collection) and the headless Linux commands (`dbus-run-session` and `gnome-keyring-daemon --unlock --components=secrets`). README has a Headless Linux section. There is still no plaintext fallback.
- `benchling mcp serve --verbose` turns request logging on. It is off by default.

### Fixed

- Unstructured table reads follow the current V3 shape: `rows` is a URL, pages come from `.../rows/items` at `pageSize` 100, cells use `inputValue.value`, and the version precondition is the entry version.
- Unstructured table writes use `update_table_cells` and `insert_table_rows` on `PATCH /entry/{id}`. A stale entry version (HTTP 409) is the same conflict as a stale plan. The full-table replace operation is removed.
- Document insert, delete, move, and text replacement send `position`, `sourceRange`, and `destinationPosition`.
- Entry field writes send field-definition IDs. Plans still accept a system name or an ID and show both.
- `entry get --full` and `part get` return unstructured-table and results-table rows. A failure to read those rows fails the request. `DocumentPartResultsTable` and `DocumentPartUnavailable` are modeled.
- HTTP 405 is reported as `api_drift`: the endpoint no longer supports the method.

### Changed

- Repository links now point at `https://github.com/jmamodeo/benchling-toolkit` after the GitHub repository rename. Older changelog text is unchanged.
- **Breaking:** `BENCHLING_<ENV>_API_ENDPOINT`, `BENCHLING_<ENV>_CLIENT_ID`, and `BENCHLING_<ENV>_CLIENT_SECRET` are no longer read. There is no fallback or alias. Configure an environment with `benchling auth login --env NAME`.
- Tenant URLs accept a bare host and a trailing `/api/v2` or `/api/v3`. A trailing slash is not shown. `http://` is rejected on its own. The tenant URL and the client ID are validated separately.
- A 401 from the token endpoint names the configured tenant URL and includes `env` and `tenant_url` in `details`.
- `benchling doctor` exits 3 with `ok: false` when a check fails. Each failed check has a hint. `auth status --check` puts the specific cause in the top-level message.
- An invalid environment name explains the lowercase-letters-and-digits rule. Logging out an environment that is not configured says so.
- MCP startup errors, including a missing keychain, are a JSON envelope on stderr and a non-zero exit. Only that `mcp serve` path adds that the MCP SDK launches stdio servers with only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER`, so `DBUS_SESSION_BUS_ADDRESS` has to be set in the client `env` block. Cron needs the same long-lived bus.
- `--secret-stdin` reads the controlling terminal with `os.open` and `FileIO`. Opening `/dev/tty` in text mode `r+` is rejected on Linux and macOS, which made that flag fail even when a terminal existed.

### Removed

- **Breaking:** `benchling envs list`. `benchling auth status` is the only list of environments. There is no alias.
- The `early_access` field on saved environment metadata. V3 requests still send `EARLY-ACCESS: true`.

### Added

- `benchling auth login --env NAME` prompts for the tenant URL and client ID, reads the client secret from a hidden prompt or `--secret-stdin` (never from an argument), and saves only after a client-credentials token request succeeds. The tenant URL and client ID go in `environments.json` (mode 0600). The client secret goes in the OS keychain (service `benchling-toolkit`).
- `benchling auth logout --env NAME` removes that `environments.json` record and its keychain secret.
- `benchling auth status` lists saved environments and whether a keychain secret is present, without printing the secret. `--check` requests a token and reports the result without printing or saving it. The previous single-environment `access_token_usable` field is now `access_token_cached` on each environment (in-memory only). `credential_source` is `keyring`.

### Removed

- **Breaking:** environment-variable credentials. A missing OS keychain is an error. Client secrets are not written to a plaintext file.

## [0.3.0] - 2026-11-19

### Changed

- **Breaking:** the distribution is renamed from `benchling-agent` to `benchling-toolkit`, and the import package from `benchling_agent` to `benchling_toolkit`. Install and import the new name. The old install name and import path are not provided.
- **Breaking:** per-user state now lives in a `benchling-toolkit` directory. Override it with `BENCHLING_TOOLKIT_HOME` (previously `BENCHLING_AGENT_HOME`). Plans and journals written by 0.2.x are not read.
- **Breaking:** schema `$id` values and the installed data files under `share/benchling-toolkit/` use the new name.
- The `benchling` command is unchanged. It still runs the CLI and `benchling mcp serve`. `benchling --version` prints `benchling-toolkit 0.3.0`.
- Docs describe this project as a toolkit of safe, auditable tools — a JSON-first CLI and a read-only-by-default MCP server — that AI agents use to work with Benchling.

## [0.2.0] - 2026-11-18

### Changed

- **Breaking:** the concept formerly called a "profile" is now consistently an "environment". The JSON envelope key `meta.profile` is now `meta.env`, the `auth status` and `doctor` data key `profile` is now `env`, and saved change plans store `env` instead of `profile`. Plans created by 0.1.x are not readable by this version; preview them again.
- The package version is defined once, in `benchling_agent.__version__`; `pyproject.toml` reads it.
- The test suite was audited for redundancy: duplicated interface, CLI and MCP-serve tests, hand-rolled planner doubles that the fake-backed plan tests already covered, and overlapping service-read tests were removed or merged, and same-branch parametrizations were collapsed. Unique assertions were kept. The suite is now 199 test functions (357 collected cases) at 98% branch coverage; the CI floor stays at 96%.
- Long functions (`build_mcp_server`, snapshot assembly, the table, document and result planners, and plan apply) were split into small helpers, and plan and apply dispatch by change family through a mapping. Behavior is unchanged.
- The code base is formatted with `ruff format`, enforced in CI, and public modules, classes and functions have docstrings.

### Added

- `benchling --version`.
- `scripts/demo.py`, which runs the real CLI and MCP server against the offline fake API and produces the transcript shown in the README. A test keeps it runnable.
- A `build` job in CI that builds the sdist and wheel, runs `twine check --strict`, and smoke-tests the installed wheel.
- README sections for why the project exists, an architecture diagram, the safety model, design decisions and tradeoffs, and known limitations.

### Removed

- **Breaking:** the hidden `--profile`/`-p` option and the hidden `profiles` command group. `--env`/`-e` and `envs` are the only way to select and list environments.
- **Breaking:** the `login` and `logout` commands, which only printed an unsupported-capability error. Authentication is environment-variable only and is documented in the README.
- **Breaking:** `entry get --api-version`, a raw Benchling V2 diagnostic passthrough. `entry get` always returns the typed V3 snapshot.

## [0.1.2] - 2026-11-18

### Added

- Offline test coverage for the MCP server (every tool, per-environment write guard, error envelopes), document and table planning and applying (validation, version preconditions, partial completion, cell-state preservation), field validation, result planning, the CLI commands, the API client (retries, timeouts, pagination) and service read paths, all against a synthetic in-process fake of the Benchling API.
- Total branch coverage rose from 66% to 98%; the enforced CI floor is now 96%. The suite has 220 test functions (381 collected cases).

### Fixed

- `replace_text` now accepts the flattened V3 text parts (`DocumentPartText`) that current API responses return, and preserves their formatting. Previously only the legacy nested `TextDocumentContent` shape was recognized. This fix is verified against offline fixtures only; it has not been checked against a live Benchling tenant.

## [0.1.1] - 2026-11-18

### Changed

- Split the change-planning module into a `benchling_agent.plans` package with one module per change family (document, table, fields, results) plus a shared core and repository. Imports from `benchling_agent.plans` are unchanged and behavior is identical.
- `AuthManager` now takes a plain mapping of environment name to client secret instead of a credential-store object.
- Environment-only configuration is now the single authentication path in the code, matching the documentation.

### Removed

- Unused browser-login server, OS keychain storage, API-key authentication, delegated (authorization-code) flow and saved-profile persistence. None of these were reachable from the `benchling` CLI or the MCP server. The `keyring` dependency and its transitive dependencies were dropped.

### Added

- Static type checking with mypy (strict-ish configuration) in CI and the dev extra; the package now ships `py.typed`.
- Test coverage reporting with pytest-cov (branch coverage, enforced floor) in CI, and a coverage badge in the README with a script to refresh it.

### Fixed

- Type annotations throughout the package, found by the new mypy check.

## [0.1.0] - 2026-11-18

### Added

- Initial release.
- `benchling` JSON-first command-line interface and local MCP server (stdio) sharing one typed service layer.
- Read commands for entries, sections, parts, tables, schemas, entry fields, and results.
- Preview-then-apply change plans for document, table, fields, and assay-result families, with version preconditions and a local execution journal.
- Multi-environment configuration via `BENCHLING_<ENV>_API_ENDPOINT`, `_CLIENT_ID`, and `_CLIENT_SECRET`; OAuth client-credentials auth with in-memory token caching.
- Read-only MCP server by default, with per-environment `--allow-writes`.
- Versioned JSON envelope, stable exit codes, and published JSON schemas.
- Operating-system trust-store TLS verification.

[Unreleased]: https://github.com/jmamodeo/benchling-tools/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/jmamodeo/benchling-tools/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/jmamodeo/benchling-tools/compare/v0.5.3...v0.6.0
[0.5.3]: https://github.com/jmamodeo/benchling-tools/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/jmamodeo/benchling-tools/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/jmamodeo/benchling-tools/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/jmamodeo/benchling-tools/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/jmamodeo/benchling-tools/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/jmamodeo/benchling-tools/compare/v0.2.0...v0.4.0
[0.2.0]: https://github.com/jmamodeo/benchling-tools/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/jmamodeo/benchling-tools/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jmamodeo/benchling-tools/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jmamodeo/benchling-tools/releases/tag/v0.1.0
