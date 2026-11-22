---
name: benchlingtk
description: "Use the benchlingtk CLI and its local MCP server to list, read, and export a Benchling notebook, and to preview a change before applying it. Use when the user asks to list, find, or read entries, containers, schemas, or assay results; read sections or document parts; export a table or results to JSON or CSV; create an entry from a template; or plan and then apply a document, table, field, result, or container change. Credentials come from benchlingtk auth login --env NAME and stay in the OS keychain. The MCP server is read-only unless it was started with --allow-writes ENV. The toolkit does not drive a browser; pair it with one only for viewing entries and accepting pending results. Do not use this when no environment has been saved with auth login."
license: Apache-2.0
compatibility: "Requires the benchlingtk command from the benchlingtk package (Python 3.11+), an OS keychain, and network access to the tenant saved for that environment. The MCP server is local stdio."
metadata:
  summary: "Safe reads and previewed writes for a Benchling notebook. Lists, reads, and exports entries, containers, tables, and assay results. Applies a change only after a local plan. The MCP server is read-only unless started with --allow-writes ENV."
  version: "0.6.0"
  docs_url: "https://github.com/jmamodeo/benchlingtk"
---

# benchlingtk

A CLI for coding agents and humans, directly mirrored as an MCP server for chat agents.

Unofficial. Not affiliated with or endorsed by Benchling, Inc.

benchlingtk has no public Python API. Its Python modules are internal and may change in any release, so do not import them in scripts. A person who wants to script Benchling in Python should use the official Benchling SDK (https://docs.benchling.com/docs/getting-started-with-the-sdk). A public Python API may be added if there is enough interest; they can open a GitHub issue on https://github.com/jmamodeo/benchlingtk to ask for one.

Use this when an agent should work in a Benchling tenant through the `benchlingtk` command or the local MCP server. The toolkit itself uses only the public HTTP API and never drives a browser. An agent that also has a browser signed in to the same tenant may use it alongside the toolkit, as described in "Working alongside the browser".

## When to use it

Use this skill when the person asks to:

- list entries, containers, container schemas, schemas, or assay results
- bulk-get entries by ID or display ID, read a folder, or inspect an entry template
- read an entry, a section, a document part, an unstructured table, entry fields, or results-table rows
- get a container by ID or find containers by barcode or name
- export a table or assay results to a local JSON or CSV file
- create a notebook entry from a template in a folder, after previewing the creation plan
- preview a document, table, field, result, or container change, and apply it only after they accept that plan

Stop and do not improvise when:

- no environment is saved yet
- the person has not accepted a specific plan id, and the next step would write to Benchling
- the task needs the Benchling website and you have no browser signed in to that tenant, or the person has not asked you to act in it
- the toolkit cannot do what was asked: follow "When the toolkit cannot do it" below

## Safety defaults

The MCP server is read-only by default. `benchlingtk mcp serve` does not enable writes. `changes_apply` is refused until that process was started with `--allow-writes ENV` for the environment in the call. Repeat the flag once per environment.

`changes plan` and the MCP tool `changes_plan` only read Benchling. They write an immutable local plan. They do not need `--allow-writes`.

`changes apply PLAN_ID` is the remote write. Run it only after the person accepts that plan id, and only with the same `--env` that created the plan. Show them the plan id, the family, and the preview in the envelope before you apply. A plan is single-use. A stale plan exits 5. Do not retry a write that exits 8 (partial or unknown).

A protected environment refuses `changes apply` (exit 2) unless `--env NAME` is on the command line and `--confirm-env NAME` repeats it. Add `--confirm-env` only after the person has confirmed that environment name for this plan. Never guess it, and never run `auth unprotect` to get around the guard. `doctor` shows `protected` and a `protection` line. On MCP, a protected environment's `changes_apply` is refused unless the server was started with `--allow-writes` for it. Reads and previews are unaffected.

`export` writes a local file and does not change Benchling. It does not need `--allow-writes`. Leave an existing file in place unless the person asked to replace it with `--overwrite`.

There is no default environment. Pass `--env NAME` on every API command. `-e` is the short form. `NAME` uses lowercase letters and digits, starts with a letter, and is at most 32 characters. Use a read environment for reads. Use a write environment only when the person asked for a write.

## Credentials

The person saves an environment in their own terminal:

```console
benchlingtk auth login --env NAME
```

The client secret is read from a hidden prompt, or from stdin when they pass `--secret-stdin`. It is never a command argument. Login saves nothing until a client-credentials token request succeeds. The tenant URL and client ID go in `environments.json`. The client secret goes in the OS keychain under the service name `benchlingtk`, with the environment name as the keychain username.

Run `auth login` only when the person has given you the credentials for that environment and asked you to save them; pass the secret with `--secret-stdin`, never as an argument. Otherwise do not run it, and never invent a tenant URL, client ID, or secret. Do not ask for the client secret, read it from the environment, or print it. If a command fails because the environment is missing or the keychain has no secret, stop and tell the person to run `benchlingtk auth login --env NAME` in their own terminal.

These commands print no secret:

```console
benchlingtk auth status
benchlingtk auth status --check --env NAME
benchlingtk doctor --env NAME
```

The person can mark an environment protected in their own terminal:

```console
benchlingtk auth login --env NAME --protected
benchlingtk auth protect --env NAME
```

`auth status` lists saved environments, whether a keychain secret is present, and whether each is `protected`. `--check` requests a token and does not print or save it. `doctor` makes read-only API checks. A failed doctor check exits 3.

If `benchlingtk` is not on `PATH`, run `uv run benchlingtk` from a checkout of this repository. The same subcommands apply.

## How to read a response

Put `--env NAME` on the command, after its name: `benchlingtk entry get REF --env NAME`. Stdout is one JSON object with `ok`, `data`, `error`, and `meta`. Stderr is diagnostics. A nonzero exit is a failure.

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
| 9 | Usage error: the command line did not parse (`error.code` is `usage_error`) |

Identifiers in the examples (`etr_example`, `docpart_example`, `docpart_table1`, `docpart_rt1`, `docpart_text1`, `docpart_section1`, `strtbl_example`, `src_example`, `lib_example`, `assaysch_example`, `EXP-0001`, `RESULT_ID`, `PLAN_ID`, `NEXT_TOKEN`) are placeholders. Substitute ids from the command you just ran. Do not invent a tenant URL or an organization name.

An entry reference may be a canonical id, a display id, or a URL on the selected tenant.

## Read workflow

### List

Each list call returns one page. Pass `next_token` back as `--cursor`. A short page is not the whole result.

```console
benchlingtk entry list --project src_example --limit 50 --env NAME
benchlingtk entry list --folder lib_example --name "Example entry" --env NAME
benchlingtk entry list --modified-after 2026-01-01 --cursor NEXT_TOKEN --env NAME
benchlingtk schema list --env NAME
benchlingtk schema list --kind result --limit 50 --env NAME
benchlingtk schema find "qPCR assay" --env NAME
benchlingtk container get con_example --env NAME
benchlingtk container find --barcode VIAL-001 --env NAME
benchlingtk container find --name "Sample vial" --env NAME
benchlingtk container schema list --limit 50 --env NAME
benchlingtk results list --entry etr_example --page-size 100 --env NAME
```

`entry list` filters are applied by Benchling. `--name` is an exact match, not a substring. `--folder` uses the V2 entry list. `container find` requires exactly one exact `--barcode` or `--name`. `container find` and `container schema list` return one page, default 50 and maximum 100; pass `next_token` back with `--cursor`. Container schema results include their field definitions. `--modified-after` and `--modified-before` take `YYYY-MM-DD` or an RFC3339 timestamp that includes a time zone. `--limit` is 1 to 100 and defaults to 50 on `entry list` and `schema list`. `schema list --kind` is `entry`, `entity`, `result`, or `run`. Omit `--kind` to list all four. `schema find NAME` requires an exact, case-sensitive name and returns one schema with field definitions, types, required flags, and dropdown options. If the name is absent or ambiguous, the command returns an error; ambiguity candidates include only IDs and kinds. `results list` uses `--page-size` (default 100, maximum 100), plus optional `--schema` and `--cursor`.

`entry bulk-get` uses either repeated `--id` or repeated `--display-id` options, not both, and chunks requests at the V2 limit of 100 IDs. `template list` returns one page; pass `next_token` back with `--cursor` and optionally filter by exact `--name`. `template get` summarizes note types in order, results-table schemas, table names, section titles, and text previews. It reads V2 `GET /entry-templates/{id}` because V3 `EntryTemplate.Get` returns metadata only. `folder get` reads one folder by ID.

`entry resolve`, `entry get`, `entry list` and `entry bulk-get` return `web_url`, the entry's link in the Benchling web app (`null` when Benchling returns none). Give the person that link when they need to open the entry.

### Read entries, tables, and results

Start with `inspect` for structure. Use `entry get --full` when you need the content. For a large table, export it instead of pulling every row into context.

```console
benchlingtk entry resolve EXP-0001 --env NAME
benchlingtk entry inspect etr_example --env NAME
benchlingtk entry get etr_example --full --env NAME
benchlingtk entry bulk-get --id etr_example --id etr_other --env NAME
benchlingtk entry bulk-get --display-id EXP-0001 --display-id EXP-0002 --env NAME
benchlingtk template get etpl_example --env NAME
benchlingtk template list --name "Example template" --env NAME
benchlingtk folder get lib_example --env NAME
benchlingtk entry fields get etr_example --env NAME
benchlingtk section get etr_example docpart_section1 --env NAME
benchlingtk part get etr_example docpart_text1 --env NAME
benchlingtk table get docpart_table1 --env NAME
benchlingtk schema get assaysch_example --env NAME
benchlingtk results get RESULT_ID --env NAME
benchlingtk blob get BLOB_ID --env NAME
benchlingtk results tables etr_example --env NAME
benchlingtk results rows docpart_example --limit 50 --cursor NEXT_TOKEN --env NAME
benchlingtk results pending --plan PLAN_ID_OR_FILE --env NAME
benchlingtk results verify PLAN_ID_OR_FILE --env NAME
```

`table get` reads an unstructured table. Its argument is a `docpart_…` id from `entry inspect`. `results tables` returns `table_id` values that are `strtbl_…` ids. `results rows` reads one page of a results-table part (`docpart_…`) from that part's rows URL; a `strtbl_…` id works only with `--entry ENTRY`, which maps it to that entry's part. `results get`, `results list --id` and `results pending --id` take either a V3 result id (`result_…`) or a V2 assay result UUID, and each record they return, like each `results verify` row, has both as `v3_id` and `v2_id` (`v2_id` is null when Benchling does not return it). When an id cannot be read, the error `details` give `id_namespace` (what was passed) and `lookup` (what was tried). `schema get` includes each dropdown field's options. `blob get` reads metadata without downloading, using either a blob UUID or an `attachment_…` id. `blob download ID --output PATH` writes a local file and reports its size, SHA-256 and MD5. Supply `--expect-size`, `--expect-sha256`, `--expect-md5`, or `--compare LOCAL_FILE` to verify it; without any expectation the summary says `verified: false`. Existing files are kept unless `--force` is given.

`results pending --plan` and `results verify` accept either a plan ID in the current home or a path to a plan JSON file. To read results from another home, copy the plan and its applied journal; name the journal `<plan-id>.journal.json` and place it beside the plan. This transfers only the two needed files, not the whole config home. The active environment must still match the plan.

### Export

Export reads every page and writes a local file. The envelope is a summary only: `row_count`, `columns`, `path`, and `checksum`. The rows stay in the file.

```console
benchlingtk export --kind table --part docpart_table1 --format csv --output measurements.csv --env NAME
benchlingtk export --kind results-table --part docpart_rt1 --format json --output results-table.json --env NAME
benchlingtk export --kind results-table --part strtbl_example --entry etr_example --format csv --output listed-table.csv --env NAME
benchlingtk export --kind assay-results --entry etr_example --schema assaysch_example --format csv --output assay.csv --env NAME
```

`--kind` is `table`, `results-table`, or `assay-results`. `--format` is `json` or `csv`. `--output` is required (`-o` is the short form). `table` and `results-table` need `--part`. A `docpart_…` id comes from `entry inspect`. A `strtbl_…` id from `results tables` needs `--kind results-table` and `--entry` for that same entry. `--kind table` rejects a `strtbl_…` id. `assay-results` takes `--entry` and `--schema`. Add `--overwrite` only when the person asked to replace that file.

## Preview, then apply

One plan covers one existing-entry mutation, one entry creation, or one container operation. The families are `entry`, `document`, `table`, `fields`, `results`, and `containers`. For `entry`, pass `entry_template_id`, `folder_id`, and `name`; optional `schema_fields` are checked against the template's schema. Omit `--entry` for entry and container plans. Write the JSON spec to a file, then plan. `--spec -` reads that JSON from stdin, and `--spec-json TEXT` takes it inline. Pass only one of `--spec`, `--spec-json`, and `--csv`.

```console
benchlingtk changes plan --entry etr_example --family table --spec set-cells.json --env NAME
benchlingtk changes plan --family containers --spec create-container.json --env NAME
benchlingtk changes plan --family containers --spec transfer-container.json --env NAME
benchlingtk changes apply PLAN_ID --env NAME
benchlingtk changes apply PLAN_ID --confirm-env NAME --env NAME
benchlingtk changes status PLAN_ID --env NAME
benchlingtk changes status PLAN_ID --wait --timeout 300 --env NAME
```

After applying an entry plan, use the returned `entry_id` with `entry inspect` or `results tables`. Entry creates use the V3 `POST /entry` route and are never automatically retried; a plan cannot be replayed after an attempt.

`changes plan` does not write to Benchling. `changes apply` re-checks the plan's preconditions, then writes. `changes status` reads each recorded result task once by default and stores returned task status locally. Pass `--wait --timeout SECONDS` to poll until all tasks are complete or the timeout expires.

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

Result upload. `table_id` is an `strtbl_…` id from `results tables`; each batch may have up to 500 records and can use a different schema. Field keys stay system names. The results land as pending (see "Results land as pending" below). Read each task with `changes status PLAN_ID`, or wait with `changes status PLAN_ID --wait --timeout SECONDS`.
Preview validates supplied dropdown options and linked-object schemas using read-only calls. Invalid rows are reported with their plan-wide index and included in a validation summary; no result upload occurs.

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
benchlingtk changes plan --entry etr_example --family results --csv assay.csv --id-column id --map Ct=Ct --map Note=Note --env NAME
```

## Results land as pending

Results created through the API do not go straight into a notebook results table. Benchling marks them pending. They stay out of the table, and have no entry (`entry` is null), until a person clicks the plug (integrate) icon in that table's toolbar in the Benchling UI. This is the expected behaviour in practice. Benchling documents it for V3 results created with `documentPartResultsTableId` and for V2 `assay-results:bulk-create` with a `tableId`, so do not assume V3 changes it. Results created without a table id are not attached to a table at all. The public V2 and V3 specs have no call that accepts pending results, so the toolkit cannot do it.

After a results create, tell the user, naming the entry and every submitted table: "Open entry X at LINK, go to results table Y, and click the plug icon in its toolbar to accept the pending results. Until you do, they do not show in the table and I cannot update them, because a results update needs a result that is on the entry. Refresh the page if the entry is already open." The apply response carries the same text in `next_step`, with the entry's `web_url` as the link, including partial or unconfirmed submissions where acceptance may be needed. Say it every time.

Check, do not assume. `results pending --plan PLAN_ID_OR_FILE` reads the plan's records back, including pending ones, and marks each `pending` or `accepted`; `needs_accept` lists the rows that still need the plug icon. `results verify PLAN_ID_OR_FILE` diffs the pushed records against the plan field by field and reports mismatched, missing, and extra rows. Without a plan, use `results pending --id RESULT_ID` or `results pending --schema SCHEMA_ID --created-after TIME` (V2 has no entry filter, so that can include other entries' results). `results list --entry ENTRY` and `results rows PART_ID` do not show pending results. After acceptance they appear on the entry. Only then plan a results update. A result that is not on the entry is refused at plan time with "Result is not on the selected entry", and nothing is written.

For table rows, the [Benchling V3 API reference](https://docs.benchling.com/reference) defines `submissionStatus` values including `SUBMITTED`, `HAS_UNSUBMITTED_CHANGES`, and `UNEVALUATED`; the latter means the table has not been evaluated since its last write. `HAS_UNSUBMITTED_CHANGES` means the row has changes not yet submitted. `isSubmittable` separately says whether the row can be submitted in its current state. These API fields are not substitutes for checking whether an uploaded assay result is pending acceptance: the toolkit uses the result's entry association for its `pending`/`accepted` read-back state. In particular, do not infer the UI's "Submitted" label from `isSubmittable`, or treat `UNEVALUATED` as a submitted result.

The accept control may insert every pending result that matches the table's schema, not only the ones just pushed. Do not create stray or test results against a table you care about.

## Working alongside the browser

Use the toolkit for every read, write, export and check. Use a browser only to see what a person sees and to do what the public API cannot, chiefly accepting pending results. The toolkit writes as the app; the browser acts as the signed-in user, so the signed-in user needs access to the entry and the app needs access to its folder.

1. Get the entry's link. `benchlingtk entry get ENTRY --env NAME` returns a stable `web_url` field, taken from Benchling's `webUrl` ("the absolute URL of the entry in the Benchling web app", Get Entry by ID on docs.benchling.com). Check that its host is the tenant saved for NAME before you open it. If there is no `web_url`, search the tenant for the display ID.
2. Before you push results, open the entry and confirm its title, sections and results tables match `entry get` and `entry inspect`.
3. After `changes apply` for results, run `results pending --plan PLAN_ID`. Every row is `pending`.
4. Accept them only if the person asked you to; otherwise relay `next_step` to them. Refresh the entry: a page opened before the push does not show the new pending rows. In each submitted results table, hover the plug (integrate) icon in the table toolbar. Its tooltip shows the number of pending rows. If the count is higher than you pushed, stop and ask, because the plug appends every pending result for that table's schema. Click the plug. The rows are appended to the bottom of the table, replacing blank trailing rows. If integrating would take the table past the notebook row maximum, Benchling refuses; split the results across tables. (Uploading Results to Benchling through the API, "Integrating the Results into the Table", and Creating a new Notebook Entry containing Results, on docs.benchling.com.)
5. Confirm with the toolkit, not just the page:
   - `results pending --plan PLAN_ID` shows every row `accepted`;
   - `results verify PLAN_ID` is ok;
   - `results list --entry ENTRY` and `results rows PART_ID` now return the rows.
   Only then plan a results update.
6. After a results update, the result record changes, but its notebook results-table cell can keep showing the old value. Do not click the plug for this: the plug only accepts pending results after a create (steps 4 and 5). Run `results get RESULT_ID` and read `table_check`, or run `results rows PART_ID` and read each row's `stale_cells`. If `table_check.status` is `stale`, or the read carries `warnings`, the cell is stale: ask the person to open that table in Benchling and use **Re-sync table**. Re-syncing is UI-only, because the public API has no re-sync call. Run the read again afterwards and confirm `table_check.status` is `in_sync` and there are no `warnings`.
7. After document or table writes, open the entry to confirm they render. Table formulas are re-evaluated only when the document is edited in the web app (Working with Tables, on docs.benchling.com), so a computed cell may show its input until then.

In the browser, never sign, archive, delete, share or send for review. Never edit templates, schemas or settings, and never accept results in an entry you were not asked to work in.

## When the toolkit cannot do it

This rule is mandatory. When the person asks for something this toolkit cannot do, ALWAYS:

1. Explain the limitation in plain words: what they asked for, what the toolkit does today, and why it stops (a missing command, an unsupported part type, a Benchling API limit, or a bug).
2. Ask whether they want it logged as a bug or a feature request on the open-source repository, jmamodeo/benchlingtk. Do not file anything yet.
3. Only after an explicit yes, build the issue with `benchlingtk issue draft` or the MCP tool `issue_draft`. Give it:
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
benchlingtk issue draft --spec issue.json
benchlingtk issue draft --spec issue.json --file
```

`issue.json` holds the fields above. `--cite-url` and `--cite-quote` can be repeated instead of `citations`, and every other field has a flag; run `benchlingtk issue draft --help` for the list. The MCP tool `issue_draft` takes the same fields as those flags (`attempted`, `solution`, and matching `cite_url` and `cite_quote` lists), is draft-only, and never files.

## Reading this skill from an install

`uv tool install benchlingtk` (or `uvx benchlingtk`) installs this file as `share/benchlingtk/SKILL.md` under the tool environment's prefix. These commands find it, or the repository copy in a source checkout, and never contact Benchling. `skill path` prints the absolute path and `skill show` prints the Markdown. If the file is missing, they print a `not_found` envelope:

```console
benchlingtk skill path
benchlingtk skill show
```

The MCP server serves the same file as the read-only resource `skill://benchlingtk/SKILL.md`.

## MCP server

If the benchlingtk MCP tools are already connected, call them with the same environment name. Do not put a client secret in the client config. The server reads the OS keychain.

The MCP tools are the CLI commands, one to one, generated from the same declarations. `benchlingtk entry resolve REF --env ENV` is the tool call `entry_resolve {env: ENV, reference: REF}`, and both reply with the same envelope. The naming rule:

- The tool name is the command path joined with `_`, with `-` also turned into `_`: `entry bulk-get` is `entry_bulk_get`, `changes apply` is `changes_apply`.
- Each argument or option is an argument with the parameter name from `--help`: `--part-offset` is `part_offset`, `--confirm-env` is `confirm_env`, and `--env` is `env`.
- Defaults, limits, and help text are the CLI's. Each tool description ends with its write class: reads only, writes a local file or plan, or writes to Benchling.
- `auth login`, `auth logout`, `auth protect`, `auth unprotect`, `skill path`, `skill show`, and `mcp serve` have no tool. Options that only make sense in a terminal have no argument: `changes plan --spec` and `--spec-json` (the tool's `spec` is the JSON object itself) and `issue draft --spec` and `--file`.

Reads, `export`, and `changes_plan` work when the server was started as:

```console
benchlingtk mcp serve
```

`changes_apply` works only for an environment named at startup. A protected environment that was not named is refused with a message that says so. A protected environment that was named also needs `confirm_env` equal to `env`; set it only after the person has confirmed that environment name for this plan, never by copying `env`:

```console
benchlingtk mcp serve --allow-writes ENV
```

Do not add `--allow-writes` unless the person asked to allow writes for that environment. `export` takes `kind`, `output`, `format`, and optional `part_id`, `entry`, and `schema_id`. It returns the same summary as the CLI and writes a local file only. `changes_apply` for entry creation returns the new entry ID and `web_url`; for results uploads it returns `web_url` and `next_step`: relay it to the user so they can accept pending results. `changes_plan` takes `env`, an optional `entry`, `family` (`entry`, `document`, `table`, `fields`, `results`, or `containers`), and `spec` (a JSON object with the same shapes as the `--spec` file, including table-specific result `batches`). Omit `entry` for entry and container plans. `changes_status` accepts `wait` and `timeout` to poll result tasks and does not need `--allow-writes`. `results_pending` and `results_verify` read pushed results back, including pending ones, and do not need `--allow-writes`.

Tools for this workflow: `auth_status`, `doctor`, `entry_list`, `schema_list`, `schema_find`, `entry_resolve`, `entry_inspect`, `entry_get`, `entry_bulk_get`, `folder_get`, `template_get`, `template_list`, `entry_fields_get`, `section_get`, `part_get`, `table_get`, `schema_get`, `container_get`, `container_find`, `container_schema_list`, `results_list`, `results_get`, `blob_get`, `blob_download`, `results_tables`, `results_rows`, `results_pending`, `results_verify`, `export`, `changes_plan`, `changes_apply`, `changes_status`, `issue_draft`. `auth_status` and `issue_draft` take no environment.
