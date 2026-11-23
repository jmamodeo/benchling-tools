# CLI and MCP reference

The detailed reference for the `benchling-tools` CLI and its MCP server. The [README](../README.md) is the short overview; [SKILL.md](../SKILL.md) is the agent-facing guide.

## Install

The distribution and the command are both named `benchling-tools`. Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/) are required.

`benchling-tools` is on [PyPI](https://pypi.org/project/benchling-tools/). Install the CLI and start the MCP server with:

```console
uv tool install benchling-tools
benchling-tools --help

uvx benchling-tools mcp serve
```

`uv tool install benchling-tools` puts `benchling-tools` on PATH. `uvx benchling-tools mcp serve` runs the server from that package without a separate install; the MCP client config below is that same command.

To work on the toolkit itself, run the same commands against a checkout of this repository:

```console
uv tool install .
uvx --from . benchling-tools mcp serve
```

`uv sync` in a git checkout installs the package into that project's virtualenv only. It does not put `benchling-tools` on PATH. From a checkout, run `uv run benchling-tools`, or activate `.venv`. Add `--extra dev` to `uv sync` when you need the test and lint tools. Creating the Benchling app, granting it project access, and saving it with `benchling-tools auth login --env NAME` are in the [setup guide](setup.md).

## Configuration

Create the Benchling app and grant it project access first. Those steps, including separate read and write environments, are in the [setup guide](setup.md).

Run `benchling-tools auth login` in your own terminal. The client secret is read from a hidden prompt, or from stdin with `--secret-stdin`, and is never accepted as an argument, so it does not pass through an agent, a shell history entry, or an MCP client config.

```console
benchling-tools auth login --env dev
# Tenant URL: https://your-tenant.benchling.com
# Client ID: your-app-client-id
# Client secret (not echoed):
```

Agents and CI can log in with no terminal. Pass the tenant and client ID as flags, and the secret on stdin:

```console
printf '%s' "$BENCHLING_CLIENT_SECRET" | benchling-tools auth login --env dev \
  --tenant-url https://your-tenant.benchling.com \
  --client-id your-app-client-id \
  --secret-stdin
```

`--tenant-url` accepts `https://host`, a bare host, or the same URL with a trailing `/api/v2` or `/api/v3` (those suffixes and a trailing slash are removed). `http://` is rejected. If a flag is missing and there is no terminal, login exits with one error that names `--tenant-url`, `--client-id`, and `--secret-stdin`. It does not fall back to `getpass` on a pipe, so you will not see echo warnings.

Login requests an OAuth `client_credentials` token before it saves anything. On success it writes the tenant URL and client ID to `environments.json` (mode 0600) under the state directory, and stores the client secret in the OS keychain under the service name `benchling-tools`. The username in the keychain is the environment name. Access tokens stay in process memory and are renewed before expiry in a long-running MCP process. If no usable keychain is available, login fails with the cause, one fix command, and a pointer to Headless Linux. There is no plaintext file fallback. A 401 says `Benchling at https://<tenant> rejected the client ID or secret.`

The toolkit acts as the **app**, with the app's permissions and audit identity, not the signed-in browser user. Profiles saved by previous versions are never read.

Environment names use lowercase letters and digits, start with a letter, and are at most 32 characters. The environment is required on every API command; there is no default. `-e` is short for `--env`.

```console
benchling-tools auth login --env test
benchling-tools auth status
benchling-tools auth status --check --env dev
benchling-tools doctor --env test
benchling-tools auth logout --env dev
```

`auth status` is the only way to list saved environments. It shows each tenant URL without a trailing slash and whether a keychain secret is present. It never prints the secret. `--check` requests a token and puts the specific failure in the top-level message (for example `Client secret is not in the keychain; run benchling-tools auth login --env dev`). `doctor` makes read-only API checks. When a check fails, the envelope is `ok: false`, the process exits 3, and each failed check includes a hint. `auth logout` removes the `environments.json` record and the keychain secret; it does not revoke the app in Benchling. Logging out an environment that was never configured says that it is not configured.

### Protected environments

Mark a production environment protected so a write needs its name typed twice. Pass `--protected` at login, or set it later. Protection is stored as `protected` in that environment's `environments.json` record. A later login without `--protected` or `--unprotected` keeps the saved setting:

```console
benchling-tools auth login --env prod --protected
benchling-tools auth protect --env prod
benchling-tools auth unprotect --env prod
```

On a protected environment, `changes apply` is refused (exit 2) unless `--env` names it on the command line and `--confirm-env` repeats the same name. There is no default environment and no environment-variable override for either flag. A `--confirm-env` that names a different environment is refused for any environment. Reads, `changes plan` and `changes status` are unaffected. The MCP tool `changes_apply` needs a protected environment in `--allow-writes`, and its refusal says the environment is protected. Once it is listed, the call also needs `confirm_env` equal to `env`; an agent sets it only after the person confirms the name. A `confirm_env` that names a different environment is refused for any environment. `doctor`, `auth status` and the MCP tool `auth_status` report `protected`. `doctor` also prints a `protection` line naming the flags a write needs.

```console
benchling-tools changes apply PLAN_ID --confirm-env prod --env prod
```

## Headless Linux

Linux servers and agent machines often have no Secret Service on the session bus. `benchling-tools` will not write the client secret to a plaintext file. Install a provider if it is missing (`gnome-keyring` and `dbus` on Debian/Ubuntu), then unlock a keyring and run the login in that same session. Pipe the keyring password on stdin so it is not a shell argument:

```console
dbus-run-session -- sh -c '
  printf "%s\n" "$KEYRING_PASSWORD" | gnome-keyring-daemon --unlock --components=secrets
  printf "%s" "$BENCHLING_CLIENT_SECRET" | benchling-tools auth login --env dev \
    --tenant-url https://your-tenant.benchling.com \
    --client-id your-app-client-id \
    --secret-stdin
'
```

A wrong `KEYRING_PASSWORD` for an existing login keyring makes the first keyring call hang; use the right password, or remove `~/.local/share/keyrings/login.keyring` for a fresh scratch setup.

Do not capture `gnome-keyring-daemon --unlock` with `$(...)` or backticks. The daemon does not exit, and the child keeps the substitution's pipe open, so the shell waits forever:

```console
# Hangs. Do not do this.
eval "$(printf '%s\n' "$KEYRING_PASSWORD" | gnome-keyring-daemon --unlock --components=secrets)"
```

`dbus-run-session` sets `DBUS_SESSION_BUS_ADDRESS` for that shell only. When the shell exits, the bus exits with it, and a secret stored there is gone. `gnome-keyring-daemon --unlock` does not print `DBUS_SESSION_BUS_ADDRESS`. Do not look for the address in its output.

MCP, later shells, and cron need a bus that stays up. Create one with `dbus-daemon`, then unlock the keyring on it. `dbus-daemon --print-address` prints the address and exits. Command substitution is safe for that command. It is not safe for `gnome-keyring-daemon --unlock`.

```console
runtime="${XDG_RUNTIME_DIR:-/tmp/benchling-bus-$UID}"
mkdir -p "$runtime"
chmod 700 "$runtime"
export DBUS_SESSION_BUS_ADDRESS="$(dbus-daemon --session --fork --print-address --address="unix:path=${runtime}/bus")"
printf '%s\n' "$KEYRING_PASSWORD" | gnome-keyring-daemon --unlock --components=secrets
```

Put that same `DBUS_SESSION_BUS_ADDRESS` in the environment of later shells, cron, and the MCP client config `env` block.

If `systemctl --user status` works, a user bus is often already listening at `unix:path=$XDG_RUNTIME_DIR/bus`. Export `DBUS_SESSION_BUS_ADDRESS` to that socket, then unlock the keyring. SSH and cron do not receive the user session's environment on their own. `loginctl enable-linger "$USER"` keeps the systemd user session after logout so that bus can stay up.

If the collection is locked, or the session bus has no `org.freedesktop.secrets` name, the error is the cause, one `dbus-run-session` command that unlocks the keyring and logs in inside that shell, and a pointer here. During `auth login` that command uses the environment name, the normalized tenant URL, and the client ID you passed, or the ones already saved for that environment. A value you did not supply stays a placeholder (`--env dev`, `https://your-tenant.benchling.com`, `CLIENT`). `$KEYRING_PASSWORD` and `$BENCHLING_CLIENT_SECRET` stay shell variables. `auth status`, `mcp serve`, and other commands keep the placeholders. If no provider is installed, the command is `sudo apt-get install -y gnome-keyring dbus`. The note about the MCP client `env` block and cron is included only when `benchling-tools mcp serve` raises the error. macOS and Windows use the system keychain and do not need this session.

`BENCHLING_TOOLS_HOME` overrides the state directory (the platform config directory plus `benchling-tools` otherwise). It does not hold secrets. HTTPS uses the operating system's certificate trust store, with certificate-chain and hostname verification enabled. No insecure TLS fallback is available.

## Least privilege

Create two Benchling apps and save them as two environments. The [setup guide](setup.md) covers creating each app, granting it project access, and saving it with `benchling-tools auth login --env NAME`.

- A read-only app, via `benchling-tools auth login --env read`. Use this for agents and everyday MCP sessions. Do not pass `--allow-writes read`.
- A write app, via `benchling-tools auth login --env write`. Start a server that may apply changes with `benchling-tools mcp serve --allow-writes write` only.

In Benchling, grant each app access only to the specific projects it should see. Give the read app the Read access policy. Give the write app Write, or Append when it should create records and leave existing ones unchanged. `--allow-writes` is a local switch on the MCP server. It cannot widen the access the Benchling app was given.

## CLI examples

### Read entries

Every command prints a versioned JSON envelope. Diagnostics go to stderr.

```console
benchling-tools entry resolve EXP-0001 --env dev
benchling-tools entry get etr_abc123 --full --env dev
benchling-tools entry bulk-get --id etr_abc123 --id etr_other --env dev
benchling-tools entry inspect etr_abc123 --env dev
benchling-tools template get etpl_example --env dev
benchling-tools template list --name "Example template" --env dev
benchling-tools folder get lib_example --env dev
benchling-tools section get etr_abc123 docpart_section1 --env dev
benchling-tools part get etr_abc123 docpart_text1 --env dev
benchling-tools part get etr_abc123 docpart_textbox1 --env dev
benchling-tools part get etr_abc123 docpart_lookup1 --row-limit 50 --env dev
benchling-tools table get docpart_table1 --env dev
benchling-tools schema get entsch_example --env dev
benchling-tools schema list --env dev
benchling-tools schema list --kind run --limit 50 --env dev
benchling-tools schema find "qPCR assay" --env dev
benchling-tools entry fields get etr_abc123 --env dev
benchling-tools entry list --project src_example --modified-after 2026-01-01 --env dev
benchling-tools entry list --folder lib_example --name "Example entry" --env dev
benchling-tools entry list --creator USER_ID --sort created-desc --limit 1 --env dev
benchling-tools results list --entry etr_abc123 --env dev
benchling-tools results tables etr_abc123 --env dev
benchling-tools results capacity etr_abc123 --env dev
benchling-tools results capacity etr_abc123 --spec examples/results-plate-reader.json --env dev
benchling-tools results capacity etr_abc123 --plan PLAN_ID_OR_FILE --env dev
benchling-tools results rows docpart_example --limit 50 --env dev
benchling-tools results rows strtbl_example --entry etr_abc123 --env dev
benchling-tools results tables etr_abc123 --name-prefix "Panel Testing" --where "Processing run=RUN_ID" --env dev
benchling-tools results rows strtbl_example --entry etr_abc123 --where "Processing run=RUN_ID" --env dev
benchling-tools results pending --plan PLAN_ID_OR_FILE --env dev
benchling-tools results pending --entry EXP-1 --env dev
benchling-tools results pending --schema assaysch_example --created-after 2026-11-19T00:00:00Z --env dev
benchling-tools results verify PLAN_ID_OR_FILE --sample 20 --env dev
benchling-tools results verify --payload body.json --created-after 2026-11-19T00:00:00Z --entry etr_abc123 --env dev
benchling-tools results verify --payload body.json --id RESULT_UUID --id result_example --env dev
benchling-tools results diff --left etr_abc123:research --right etr_abc123:regulatory --run-field "Run type" --key Well --env dev
benchling-tools results diff --left EXP-1 --right EXP-2 --key Tube --resolve-barcodes --output diff.csv --format csv --env dev
benchling-tools container get con_example --env dev
benchling-tools container find --barcode VIAL-001 --env dev
benchling-tools container find --barcode VIAL-001 --barcode VIAL-002 --barcode VIAL-003 --env dev
benchling-tools container find --name "Sample vial" --env dev
benchling-tools container find --registry-id REG-0001 --registry src_example --env dev
benchling-tools container find --contents ent_example --in box_example --env dev
benchling-tools container find --in plt_example --env dev
benchling-tools container get con_example --location --env dev
benchling-tools container schema list --limit 50 --env dev
benchling-tools export --kind table --part docpart_table1 --format csv --output measurements.csv --env dev
benchling-tools export --kind results-table --part docpart_rt1 --format json --output results-table.json --env dev
benchling-tools export --kind results-table --part strtbl_example --entry etr_abc123 --format csv --output listed-table.csv --env dev
benchling-tools export --kind assay-results --entry etr_abc123 --schema assaysch_example --format csv --output assay.csv --env dev
```

Entry references may be canonical IDs, display IDs, or URLs for the selected tenant. Duplicate section titles must be addressed by part ID.

Text boxes and lookup tables are first-class document parts. `entry inspect` (MCP `entry_inspect`) lists each text box under `text_boxes` as `{part_id, name, text, submission_status, is_submittable, section}`, where `text` is the full text and `section` is `{part_id, title}` of the section that holds it, or `null` before the first section. It lists each lookup table under `lookup_tables` as `{part_id, name, row_count}`. `section get` (MCP `section_get`) returns the same two lists for the parts on its page. Benchling documents `DocumentPartTextBox` as "a labeled fill-in text box within a document. Values may be submitted." with `name`, `text`, `formats`, `links`, `mentions`, `isSubmittable` and `submissionStatus` ([List entry parts](https://docs.benchling.com/docs/api/benchling-api/entry/parts-list)). Lookup-table detail and rows come from [`GET /document-part-lookup-table/{id}`](https://docs.benchling.com/docs/api/benchling-api/document-part-lookup-table/get) and [`GET /document-part-lookup-table/{id}/rows/items`](https://docs.benchling.com/docs/api/benchling-api/document-part-lookup-table/rows-list) (`EARLY-ACCESS`), read like any other table: every row page at `pageSize` 100, typed cells as in `entry get --full`, and `--table-row-offset`/`--table-row-limit` or `--row-offset`/`--row-limit` page them in the reply. Lookup rows are read-only and have no result records, so they get no stale check. An entry whose only extra parts are text boxes and lookup tables is `complete`.

`part get ENTRY PART_ID` (MCP `part_get`) reads only that part with `GET /entry/{id}/parts/items?id.anyOf=PART_ID`, plus the table detail and every row page when the part is an unstructured, results, or lookup table. It does not build a full entry snapshot. `version_id` is the part's `versionMetadata.versionId`, or the table's, or the entry's when neither carries one; when the part and its table disagree, the read is retried once and then fails with `conflict`. Benchling answers an id that is no document part with HTTP 400 "The following ids are invalid: …"; `part get` (and `table get` for an edit target) reports that as `not_found` at once. Any other HTTP 400 on `id.anyOf` makes `part get` fall back to the full snapshot. A text box adds `text_box`: `part_id`, `name`, `text`, `formats`, `links`, `mentions`, `submission_status` and `is_submittable`. A table's `detail` holds its columns and a page of rows.

`entry resolve`, `entry get` (with or without `--full`), `entry list` and `entry bulk-get` include `web_url`, the entry's link in the Benchling web app. It is Benchling's V3 `webUrl` or V2 `webURL`, passed through as returned, and is `null` when Benchling returns none. `entry bulk-get` items keep the raw V2 entry and add `web_url`.

`entry bulk-get` fetches multiple entries by repeated `--id` or `--display-id` options (specify exactly one kind), chunking requests at 100 IDs. `template list` returns one page; pass `next_token` back with `--cursor`, and optionally filter by exact `--name`. `template get` summarizes the template structure, including note types, sections, result schemas, table names, and short text previews. It reads V2 `GET /entry-templates/{id}` because V3 `EntryTemplate.Get` returns metadata only. Entry creation planning still reads V3 template metadata to validate the template and its active state. `folder get` reads one folder. These lookups are read-only.

`entry list` and `schema list` are read-only. They return one page, default 50 and at most 100, plus `next_token`. Pass that token back with `--cursor`. `schema list` reads entry, entity, result, and run schemas, following every upstream page until the page is full or the lists end. Its optional `--name` is an exact, case-sensitive match. `schema find NAME` requires one exact match, optionally limited by `--kind`; no match and ambiguous matches are errors. Ambiguity candidates show only schema IDs and kinds. A successful find includes field types, required flags, and dropdown options. `schema get` adds the options of each dropdown field (`/dropdown/{id}` and `/dropdown/{id}/options/items`). There is no separate dropdown command.

`users list` reads one V2 `GET /users` page, default 50 and maximum 100, with `--cursor` for `next_token`. Repeatable `--handle`, `--email` (up to 100), `--id`, and `--member-of` map to `handles`, `email.anyOf`, `ids`, and `memberOf`; `--name` and `--name-includes` map to `name` and `nameIncludes`. Rows expose only ID, handle, name, email and suspension status. `users get USER_ID_OR_HANDLE` reads a user by ID or resolves an exact handle. A missing or ambiguous handle is an error. Users are visible only with the caller's permission; an app token is not a user identity.

To find a person's entries, first call `benchling-tools users get HANDLE --env dev`, then pass the returned ID to `benchling-tools entry list --creator USER_ID --folder lib_example --env dev`. Alternatively, pass handles directly. Repeat `--creator` or `--author` for any-of matching; both filters together restrict the same query. Handles are resolved together first and `resolved_users` echoes the IDs. Creator and author can differ because authors can be changed. V3 uses `creator.anyOf` and `authors.anyOf`; V2 uses `creatorIds` and `authorIds.anyOf`. At most 100 creators and 100 authors are accepted.

`container get` reads one inventory container. `container find` requires exact `--barcode` or `--name`, not both. With `--name` or one `--barcode` it returns one page; pass `next_token` back with `--cursor`. Repeat `--barcode` to look up many barcodes in one call (one barcode per value, no commas; duplicates are dropped and order is kept). They are sent to V2 `GET /containers` 100 per request, every page is read, and the reply is `requested`, `found` (each with `barcode`, `container_id`, `schema_id`, `contents` of `entity_id`, `entity_registry_id`, and `batch_id`, and `archived`), `invalid_barcodes`, `missing_barcodes` (valid, but no container matched), and `empty_containers` (found with no contents). Barcodes Benchling names in `invalidBarcodes` are reported instead of failing the call. With one barcode the reply is still one page, and an invalid barcode is a `validation_error` with `details.invalid_barcodes` and a hint. See [List containers](https://docs.benchling.com/docs/api/v2/benchling-api/containers/list-containers). The MCP tool `container_find` takes `barcodes` as a list. `container schema list` returns one page of container schemas with their embedded field definitions.

`container find` also takes `--contents ENTITY_ID` (repeatable; sent as `storageContentsIds`, "Comma-separated list of IDs of entities. Restricts results to those that hold containers with at least one of the specified entities."), `--registry-id REG-ID` (repeatable) with `--registry src_…`, and `--in STORAGE_ID` (a `loc_…`, `box_…` or `plt_…` ID; sent as `ancestorStorageId`, "ID of a plate, box, or location. Restricts results to those located in the specified inventory."). Give exactly one of `--barcode`, `--name`, `--contents` or `--registry-id`, or `--in` on its own; `--in` combines with `--name`, `--contents` and `--registry-id`, and any other mix is a `validation_error`. Entity IDs are sent 100 per request, every page is followed up to `--limit` containers (default 50, at most 1000), and the reply is `found` (the same items as the barcode summary, each with `parent_storage_id`) and `truncated`. A container matched by two chunks is listed once. `--cursor` applies only to `--name` and one `--barcode`. Registry IDs are resolved with [Bulk get registered entities](https://docs.benchling.com/docs/api/v2/benchling-api/registry/bulk-get-registered-entities) (`GET /registries/{registry_id}/registered-entities:bulk-get`, 100 per request); the reply adds `registry`, `entities` (`registry_id`, `entity_id`, `name`, `schema_id`) and `unregistered_ids` (IDs the registry did not return; Benchling rejects a whole bulk-get with a 400 listing `invalidIds` when any ID is unregistered, so those IDs are dropped and the rest read again), and those entities become `--contents`. Without `--registry`, [List registries](https://docs.benchling.com/docs/api/v2/benchling-api/registry/list-registries) must return exactly one registry; otherwise the `validation_error` lists each registry's `id` and `name` in `details.registries`. MCP `container_find` takes `contents`, `registry_ids`, `registry` and `in_storage`.

`container get CON_ID --location` (MCP `container_get` `location`) adds `location_path` and `location_truncated`. The path runs from the container's parent up to the top; each item has `id`, `kind`, `name`, `barcode` and `schema_id`. `parentStorageId` is followed with [Get location](https://docs.benchling.com/docs/api/v2/benchling-api/locations/get-location) for `loc_…`, [Get box](https://docs.benchling.com/docs/api/v2/benchling-api/boxes/get-box) for `box_…` and [Get plate](https://docs.benchling.com/docs/api/v2/benchling-api/plates/get-plate) for `plt_…`, one read per level. A coordinate parent such as `plt_…:A1` adds a `well` item (`box_…:A1` adds a `position`) before its plate or box, without a read. An unknown ID prefix ends the path with `kind` null. Reading stops after 10 levels, a guard against cycles, and `location_truncated` is then true. See [Inventory](https://docs.benchling.com/docs/inventory).

Every `entry list` filter is applied by Benchling. `routing.client_side` is always empty.

- Without `--folder` or `--sort`, and when the name has no comma, the query is V3 `GET /entry/items`. Server-side filters are project (`project.eq`), schema (`schema.eq`), name (`name.anyOf`), and the modified range (`modifiedAt.gte` and `modifiedAt.lte`). That name match is exact and case-insensitive. It is not a substring search. The modified bounds are inclusive. A bare `YYYY-MM-DD` is the start of that UTC day for `--modified-after` and the end of that UTC day for `--modified-before`. A timestamp must include a time zone, such as `2026-06-15T12:00:00Z`. A timestamp with no zone is rejected.
- V3 entry listing has no folder parameter, and live tenants ignore `folderId` on that route, so this client never sends it there. `--folder` sends the whole query to V2 `GET /entries`. On that route folder (`folderId`), project (`projectId`), schema (`schemaId`), name (`names.anyOf`), and `modifiedAt` are all server-side. V2 takes one `modifiedAt` string; this client sends `>=` and `<=` joined by ` AND ` so the range stays inclusive.
- `--sort` orders the page and sends the whole query to V2 `GET /entries` with its `sort` parameter: `created-desc` is `createdAt:desc`, `created-asc` is `createdAt:asc`, `modified-desc` is `modifiedAt:desc`, `modified-asc` is `modifiedAt:asc`, `name-asc` is `name:asc`, and `name-desc` is `name:desc`. [List entries](https://docs.benchling.com/docs/api/v2/benchling-api/entries/list-entries) documents that enum with the default `modifiedAt:desc`: "Optionally add :asc or :desc to specify ascending or descending order." Without `--sort` no `sort` is sent and Benchling's default applies. The reply has `sort` next to `items`: the applied value, or `null`. A `next_token` belongs to the sort that produced it; do not reuse it with another sort ([pagination](https://docs.benchling.com/docs/pagination)). Any other value is a `usage_error` that lists the choices. The MCP `entry_list` tool takes the same choices as `sort`.
- A name that contains a comma cannot be one item of `name.anyOf` or `names.anyOf`. Both parameters are comma-separated lists, and the contract documents no escape. That name is sent as V2 `GET /entries` parameter `name`, and the other filters on the same request go to V2 as well. V2 `name` is a single entry name. The contract describes it as the specified name and does not say the comparison is case-insensitive.
- `modified_at` on entry and schema lists is a UTC timestamp with a `Z` suffix. V2 values that use `+00:00`, and values with another offset, are converted.

`results pending` reads results back, including pending ones, and marks each `pending` (no entry yet, waiting for a person to review and accept it in Benchling), `accepted` (on an entry), or `archived`. Pending results have no entry, so `results list --entry` and `results rows` do not show them. Pass exactly one source: `--entry ENTRY` (an entry ID, display ID, or same-tenant URL; reads back the records of every applied result upload plan for that entry in this home, see below), `--plan PLAN_ID_OR_FILE` (an applied result upload plan ID or portable plan file/bundle; record IDs come from its journal, or the task is read once when `changes status` has not stored them), `--id RESULT_ID` (repeatable; a V2 assay result UUID is read with `GET /api/v2/assay-results:bulk-get`, 200 per request, and a V3 `result_…` id with `GET /api/v3/result/{id}`, then matched to its V2 record by schema, creation time and `v3Id`), or `--schema SCHEMA_ID --created-after TIME` (one page of `GET /api/v2/assay-results` filtered by `schemaId` and `createdAt.gte`; follow `next_token` with `--cursor`). V2 list has no entry filter, so the schema mode can include results from other entries. Counts cover every record read; at most `--limit` (default 50, maximum 100) are listed, pending first, and `needs_accept` lists the pending IDs. Each item has `id` and `v2_id` (the V2 UUID) and `v3_id` (`result_…`); each id in `not_found` is repeated in `not_found_details` with `id_namespace` and `lookup`. `next_step` says which results are pending and that a person has to review and accept them in Benchling; with `--plan` it names the entry and its link, and `source.web_url` holds it.

`results pending --entry ENTRY` is answered from this home's plans and journals, because Benchling has no API filter for pending results by entry: V3 list results by entry finds a result only after it is accepted, and V2 list assay results has no entry filter. It reads every applied result upload plan in this `BENCHLING_TOOLS_HOME` whose entry and environment match (result updates, plans never applied, and plans whose apply failed before any write are left out; partial and unconfirmed applies are included, since they may have created records), newest first, and reads their record IDs back as `--plan` does. `plans` lists each plan with `plan_id`, `applied_at`, and `record_count`; `items` and `counts` merge every plan's records. `--cursor` is refused with `--entry`. When no plan matches, the reply is empty and `next_step` says only plans applied from this home are known, and to use `results pending --plan BUNDLE.json` (a bundle from `changes export`) or `--schema SCHEMA_ID --created-after TIME`.

`results pending --plan FILE`, `results verify FILE`, and `changes status FILE` accept a self-contained bundle made by `changes export PLAN_ID --output FILE`. The bundle carries the plan, its checksum, and journal; it contains no credentials or config-home path. It can be copied to another home, but the active environment must match the plan. Export refuses to overwrite an existing file unless `--force` is passed. A plain plan JSON file also works when its applied journal is beside it as `<plan-id>.journal.json`; if that journal is missing, export the bundle from the source home.

`changes status --task TASK_ID` (MCP `changes_status` `task_id`) reads any Benchling background task by ID, for example one an app started itself with V2 `POST /assay-results:bulk-create`, when no plan of this toolkit exists. Pass exactly one of a plan ID or `--task`; both or neither is a `validation_error`, and so is a task ID that is empty or holds `/` or whitespace. It reads V2 [Get a task](https://docs.benchling.com/docs/api/v2/benchling-api/tasks/get-task) (`GET /tasks/{task_id}`) with the same poller and backoff as a plan, and `--wait` and `--timeout` work the same way ([long-running tasks](https://docs.benchling.com/docs/long-running-tasks)). The reply has `task_id`, `task_status`, `result_state` (`submission_running`, `submission_failed`, or `pending_integration`), `timed_out`, `remote_task` in full, and for a failed task Benchling's `message` and `errors`. For a succeeded [bulk-create](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/bulk-create-assay-results) task, whose response "contains the full list of results that were created", `record_ids` lists the V2 result IDs and `record_count` counts them. `response_kind` is `assay_results`, `other` (another kind of task: `response_keys` lists its keys and its contents are not interpreted), or `none`. Relay `next_step`: `results pending --id …` with up to 20 of the IDs, check `errors`, or run it again with `--wait`. Benchling keeps a task only for a while: an expired or unknown task ID is a 404 ("Task not found. It may have expired"), reported under `remote_task_error` with `result_state` `unknown` and a `next_step` that says running it again will not help. It reads only, so MCP needs no `--allow-writes`.

`results verify PLAN_ID_OR_FILE` reads back an applied result upload plan and diffs it against the plan. Planned fields are compared field by field by system name (numbers by value, lists of strings without order). Planned rows are paired with fetched records by exact field match first, then in order, so the check does not depend on the order Benchling returns them. It reports `matched`, `mismatched` (with each differing field), `missing` (with a reason such as `task_running` or `submission_failed`), and `extra` rows, counts for each, and `ok` when nothing differs. Each list shows at most `--sample` rows (default 20, maximum 200). `needs_accept` and `next_step` name the rows and tables that still need the plug-icon accept. Each row gives `result_id` with `v2_id` and `v3_id`. Both commands only read; they do not write to Benchling or to the plan journal.

`results verify --payload FILE` (MCP `results_verify` `payload`, a JSON object) checks an upload the toolkit did not make, such as one an app sent with the SDK's `AssayResultService.bulk_create` ([SDK docs](https://benchling.com/sdk-docs/1.25.0/benchling_sdk.services.v2.stable.assay_result_service.html)). Pass exactly one of `PLAN_ID` or `--payload`; both or neither is a `validation_error`, and `--id`, `--created-after`, `--created-before` and `--entry` apply only with `--payload`. `FILE` (or `-` for stdin) is the JSON body sent to V2 `POST /assay-results:bulk-create` ([bulk create](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/bulk-create-assay-results); [create](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/create-assay-results) uses the same result shape): `assayResults`, each with `schemaId`, `fields` by system name, and an optional client `id`, and an optional top-level `tableId`. A bare list of results, or a list of bodies for an app that sent several batches, also works. A body with more than 4,000 results ("Limit of 4000 results per request"), a result without `schemaId`, or `fields` that is not an object is a `validation_error` whose `details` give `body_index` and `record_index`. Records are found with `--id` (repeatable; V2 UUIDs read with [bulk-get](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/bulk-get-assay-results), 200 per request, and V3 `result_…` ids, mixed) or, for each payload `schemaId`, with V2 [`GET /assay-results`](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/list-assay-results) `createdAt.gte` from `--created-after` and `createdAt.lte` from `--created-before`, at most 100 pages of 100 per schema (`source.warnings` says when more remain). The bulk-create task response also lists the created ids ([get task](https://docs.benchling.com/docs/api/v2/benchling-api/tasks/get-task)) and can be passed with `--id`. `--entry` drops fetched records whose entry is set and differs; pending records have no entry until accepted ([V3 result list](https://docs.benchling.com/docs/api/benchling-api/result/list); [results in a new entry](https://docs.benchling.com/docs/creating-a-new-notebook-entry-containing-results)), so they are kept. Rows are grouped by `schemaId`, and each group goes through the same diff and report as plan mode, with the same keys, plus `source` (`mode` `payload`, per-schema record counts and field names, the lookup, `other_entry_dropped`, and `not_found`). `plan_row_index` counts payload rows from 0 across bodies. A row with a client `id` is paired with that record before field pairing. Records of a schema not in the payload are `extra`. Nothing is saved, and the payload is echoed only as counts and field names.

`results capacity ENTRY [--plan PLAN_ID_OR_FILE | --spec FILE | --spec-json JSON]` reports how many rows each results table on the entry can still take. It is read-only and saves nothing. Benchling limits a results table to 1,000 rows ("Result tables cannot have more than 1000 rows", [Capture data with Results](https://help.benchling.com/hc/en-us/articles/39954660101773-Capture-data-with-Results)). Each table reports `table_id`, `part_id`, `name`, `schema_id`, `row_count` (V3 `rowCount` from [`GET /document-part-results-table/{id}`](https://docs.benchling.com/docs/api/benchling-api/document-part-results-table/get)), `submission_status`, `placeholder_rows`, `used_rows`, `pending_known`, `planned`, `limit`, `remaining_without_pending` (1000 − used − planned), `remaining_with_pending` (also minus `pending_known`), and `fits`. A table whose only row is an empty `UNSUBMITTED` placeholder uses 0 rows; the first row is read only when `rowCount` is 1. `pending_known` counts still-pending records from this home's applied upload plans for the entry, read back the same way as `results pending --entry`. Benchling does not document whether pending results count toward the limit, so `fits` is `true` when the rows fit either way, `false` when they do not fit even without pending results, `unknown_pending` in between (with a warning), and `unknown` when no single V3 part matches the `strtbl_…` table. `--plan` takes a saved plan or a portable bundle; a pending plan's records are `planned`, and an applied one adds none. `--spec` and `--spec-json` take the same result upload spec as `changes plan --family results`. `--include-schema-pending --created-after T` also reports `pending_schema`: pending results of each table's schema created since `T` on any entry (V2 lists have no entry filter, at most 20 pages of 100 per schema). It is not used for `fits`. The command exits 0 even when a table does not fit; `next_step` names the table and says to use a new entry or another table.

`changes plan --family results` runs the same check, through the same helper, after the tables are resolved and before any file is staged. A plan that would exceed 1,000 rows in any table is a `validation_error` with `details.reason` `table_capacity_exceeded` and the per-table numbers in `details.tables`; no plan is saved. A plan that fits only when pending results are ignored is saved with the `unknown_pending` warning.

`results rows PART_ID` reads one page of a results table. It is read-only. `PART_ID` is a `v3_document_part` id (`docpart_…`) from `entry inspect`. A `v2_structured_table` id (`strtbl_…`) from `results tables` needs `--entry ENTRY` (MCP `entry`): it is mapped to the entry's matching results-table part, the same match `export --kind results-table` uses, and the page adds `table_id`, `entry_id`, `id_namespace` and `listed_id_namespace`. A `strtbl_…` id with no entry, not on the entry, or matching no single part is a `validation_error` whose `details` name `id_namespace` and `expected_id_namespace`. The page size defaults to 50 and stops at 100. Pass `next_token` back with `--cursor`. The call is `GET /document-part-results-table/{id}/rows/items` with `pageSize` and `nextToken`. The part is read first, and its `rows` URL must be that path on this tenant. Cells use the same shape as `entry get --full`. `row_count` is the count on the part. `entry get --full` still reads every row of the part.

`results tables ENTRY` lists the entry's V2 results-table notes (`table_id` is `strtbl_…`, with `name`, `schema_id` and `columns`). By default it also reads the entry's V3 parts once (`GET /entry/{id}/parts/items`, paged) and gives each table its `part_id` (`docpart_…`), `row_count` and `submission_status` from the matching [`DocumentPartResultsTable`](https://docs.benchling.com/docs/api/benchling-api/document-part-results-table/get), plus `placeholder_rows`: 1 when the only row is the empty `UNSUBMITTED` row of a new table (that one row is read to tell), otherwise 0, and `used_rows` (`row_count` minus `placeholder_rows`). These are the same numbers `results capacity` reports, from the same helper. Tables are matched to parts as `results rows` does. A table no single part matches gets null counts and `count_note`. `--no-counts` (MCP `counts: false`) keeps the single V2 read. `--name-prefix TEXT` keeps tables whose name starts with TEXT (exact and case-sensitive), such as the numbered tables an app writes one run into.

`--where FIELD=VALUE` (MCP `where`, a list) is repeatable, and every condition must match. FIELD is a column's display name, the result schema's system name, or the field-definition id, resolved with the same field resolver as result upload plans. A cell matches when its value (the evaluation when Benchling returned one, otherwise the input) is exactly VALUE as text: numbers as written, booleans as `true` or `false`, an empty cell as the empty string. A link cell matches on the linked id, such as a container `con_…` id. Archived rows and the placeholder row never match. On `results tables`, every row of each selected table is read, each table gets `matching_rows` and `rows_scanned`, and the reply gives the total `rows_scanned`; a table without FIELD gets `matching_rows: null` and `where_note`, and a FIELD no listed table has is a `validation_error`. On `results rows`, only the page's matching rows are returned, with `scanned` (rows read) and `matched`; `next_token` still pages the table, and the stale-cell check runs on the returned rows. An unknown FIELD is a `validation_error` whose `details` list `available_display_names`, `available_system_names` and `available_field_ids`. The filter runs client-side: Benchling has no server-side filter on text field values (see [API limitations](api-limitations.md)). Pending results are not table rows until accepted, so they are never counted; use `results pending` for them.

`results diff --left ENTRY[:RUN_VALUE] --right ENTRY[:RUN_VALUE]` (MCP `results_diff`) compares the results-table rows of two entries, or two runs inside entries, table by table and row by row. Each side is an entry ID, display ID, or same-tenant URL, optionally followed by `:RUN_VALUE`; the two sides may be one entry with different run values. A run value keeps only the rows whose `--run-field` cell matches it, with the same client-side match as `results tables --where` and `results rows --where` (exact text; a link matches its id). Archived rows and the empty `UNSUBMITTED` placeholder row are never compared. `--run-field` is required with a run value, and Benchling has no server-side text-value filter, so the rows are read and filtered locally. Tables are read from the entry's [results-table rows](https://docs.benchling.com/docs/api/benchling-api/document-part-results-table/rows-list) ([Working with tables](https://docs.benchling.com/docs/working-with-tables)) and pair by exact name, trailing number included, so "qPCR Testing 1" pairs only with "qPCR Testing 1". `--table NAME` (repeatable) restricts the comparison, and a name not found on either side is listed in `tables_not_found`. A name on one side only is in `tables_only_left` or `tables_only_right`; a name used twice on one entry is in `duplicate_table_names`; paired tables with different result schemas are in `schema_mismatch`. None of those are row-diffed. `--key FIELD[,FIELD…]`, `--fields FIELD` (repeatable, default every column except the run field) and `--run-field` accept a display name, system name, or field-definition id, resolved with the same resolver as result plans. Rows pair by key; a link cell keys on its linked id. Without `--key`, rows pair by position and `warnings` says that pairing is weak. A key used more than once on either side is not paired. It is listed once in `duplicate_keys` as `{key, left, right, left_row_indexes, right_row_indexes}`, with the row count on each side, so a key that is unique on one side and repeated on the other is still reported, and `counts.duplicate_keys` counts these keys. Values are compared as `results verify` compares them (numbers by value, lists of strings without order). Each table reports `counts` (`left`, `right`, `matched`, `only_left`, `only_right`, `changed`, where `changed` counts rows) and at most `--sample` (default 20, maximum 200) entries of `only_left` and `only_right` rows and `changed` fields (`key`, `field`, `left`, `right`). `--resolve-barcodes` reads every distinct container link with V2 [Bulk get containers](https://docs.benchling.com/docs/api/v2/benchling-api/containers/bulk-get-containers) (`containerIds`, 100 ids per call), adds `left_barcode` and `right_barcode` to changed container fields and `barcodes` to unpaired rows, keys a container column on its barcode, and reports `containers` (`requested`, `resolved`, `unresolved`). `--compare-blobs` compares attachment cells (an `Attachment` link, or a column whose field is an attachment field) by SHA-256 instead of by id: each blob is downloaded once, as `blob download` does ([Get blob](https://docs.benchling.com/docs/api/v2/benchling-api/blobs/get-blob), [Get blob file](https://docs.benchling.com/docs/api/v2/benchling-api/blobs/get-blob-file)), into a temporary directory that is always deleted. A changed blob carries `blob_changed: true` with both digests, and each table adds `blobs` (`compared`, `changed`). It is off by default because it downloads. On the CLI, `--output FILE --format json|csv` writes every difference (one row per changed field or unpaired row) with the same rules as `export`: an existing file is kept unless `--overwrite`, and the reply adds `output` with the path, row count, and SHA-256. The MCP tool returns the JSON summary only and writes no file. Pending (unaccepted) results are not table rows; use `results pending` and `results verify` for those. To find results that reference a tube or entity, V2 [List assay results](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/list-assay-results) filters by `storageIds` or `entityIds` and V3 [List results](https://docs.benchling.com/docs/api/benchling-api/result/list) by `schemaFieldLinkValue.linkedItemIds`. The command only reads Benchling.

`results rows` and `results get` also check for stale cells. After `PATCH /result/{id}`, a results-table cell can keep showing the old value. A column is compared when its `SchemaFieldColumnDefinition.fieldDefinitionApiId` names a field that the linked record returns in `schemaFields`. A row is skipped when it is archived or its `submissionStatus` is `UNSUBMITTED` or `HAS_UNSUBMITTED_CHANGES`, because such a row may hold a person's unsaved edit. `SUBMITTED`, `UNEVALUATED` (what Benchling reports on accepted rows of a table not evaluated since the last write) and a missing status are compared. Values compare by what they show: a link compares by id, numbers by value, and empty values are equal. `results rows` reads each linked result once with `GET /result/{id}`, and nothing when no column maps to a field. A stale row carries `warning` and `stale_cells`, one per cell, with `column_index`, `column_id`, `column_name`, `field_definition_id`, `table_value` and `result_value`. The page carries `stale_check` (`compared_rows`, `stale_rows`, and `unreadable_results` for results that returned `not_found` or `access_denied`), plus `warnings` when a row is stale. `results get` adds `table_check`, found by reading the results tables on the record's entry that match its schema until a row links the result. `table_check.status` is `in_sync`, `stale` (with `stale_cells` and `warning`, and the record gets `warnings`), `not_compared` (an archived, `UNSUBMITTED` or `HAS_UNSUBMITTED_CHANGES` row, or no comparable column), `no_table_row`, `not_on_entry`, or `unavailable` (the entry or a table could not be read; `reason` is the error code). The fix is **Re-sync table** in the Benchling UI. It is UI-only: the API has no re-sync call (see [API limitations](api-limitations.md)), so the toolkit has no re-sync command. The plug icon accepts pending results only; it is not the fix for a stale cell.

`benchling-tools export` writes an unstructured table (`--kind table`), a results table (`--kind results-table`), or assay results (`--kind assay-results`) to a JSON or CSV file. `--format` is `json` or `csv`. `table` and `results-table` require `--part`. `assay-results` accepts `--entry` and `--schema`. The command reads every page itself: unstructured-table and results-table rows at `pageSize` 100, and assay results from `GET /result/items` at `pageSize` 100. The printed envelope is a summary only: `row_count`, `columns`, `path`, and `checksum` (SHA-256 of the file bytes). The rows stay in the file, so a large table does not enter an agent's context. A cell is the evaluation when Benchling returned one, otherwise the input. Duplicate column names get a `__2` suffix. An existing file is left unchanged unless `--overwrite` is set. Nothing is written to Benchling. The MCP tool `export` uses the same summary.

`changes export PLAN_ID --output FILE` writes a self-contained JSON bundle of the local plan, its checksum, and journal. It does not include credentials or the state-directory path. `--force` is required to replace an existing file. The generated `changes_export` MCP tool takes `plan_id`, `output`, and optional `force`; its write class is local-file only.

`--part` takes a `v3_document_part` id (`docpart_…`) from `entry inspect` (the `tables` part ids). `results tables ENTRY` returns a different id: `table_id` is a `v2_structured_table` id (`strtbl_…`, `id_namespace` `v2_structured_table`). Pass that id with `--kind results-table --entry ENTRY` (the same entry). The export reads the entry's document parts and uses the matching results-table part. When no part matches, it reads assay results for that entry and the table's result schema. `--kind table` does not accept a `strtbl_…` id. That is a validation error naming `v3_document_part`. A `strtbl_…` id with no `--entry` is the same kind of error.

Assay-result columns are the result's scalar metadata plus one column per schema field. V3 stores those values in `schemaFields` (an array of field-definition ref plus value), not in a top-level `fields` object. The column name is the field's display name. A link or entity reference is written as its id. A multi-value field is a JSON array in CSV. An unset field is empty: CSV writes `""` and JSON writes `null`. `linkedEntityId` is used only when the field is a link and the id is a different entity. An unset field's `linkedEntityId` is often the result's own id, and that id is not written as the value.

A results-table or unstructured-table cell is flattened the same way. A link or entity reference becomes its id in both JSON and CSV, so a results table and the assay results for the same record show the same value.

### Preview and apply changes

A change spec covers one existing-entry mutation, one new-entry creation, or one container mutation. Previewing only reads Benchling and creates a local immutable plan:

```console
benchling-tools changes plan \
  --entry etr_abc123 \
  --family table \
  --spec examples/table-set-cells.json --env dev

benchling-tools changes plan \
  --entry etr_abc123 \
  --family results \
  --csv assay.csv \
  --id-column id \
  --map Ct=Ct \
  --map Note=Note --env dev

benchling-tools changes plan \
  --family entry \
  --env dev \
  --spec - <<'JSON'
{
  "entry_template_id": "etpl_abc123",
  "folder_id": "lib_abc123",
  "name": "Fresh experiment",
  "schema_fields": {
    "temperature": { "value": 21 }
  }
}
JSON

benchling-tools changes plan --entry etr_abc123 --family table \
  --spec-json '{"operation": "append_rows", "table_part_id": "docpart_table1", "rows": [["D", "4"]]}' --env dev

benchling-tools changes apply PLAN_ID --env dev
benchling-tools changes apply PLAN_ID --confirm-env prod --env prod
benchling-tools changes export PLAN_ID --output PLAN.json --env dev
benchling-tools changes status PLAN_ID --env dev
benchling-tools changes status PLAN_ID --wait --timeout 300 --env dev
benchling-tools changes status --task TASK_ID --wait --timeout 300 --env dev
```

`--confirm-env` is required only for a [protected environment](#protected-environments).

An entry spec uses `entry_template_id`, `folder_id`, and `name`, and may include `schema_fields` when the template has a schema. Preview reads V3 template metadata and folder data, refuses missing or archived resources, and shows their names and IDs. Use `template get` for the template's note structure; V3 `EntryTemplate.Get` returns metadata only. Apply creates the entry once and returns its ID; use that ID with `entry inspect` or `results tables`. The V3 `Entry.Create` route supports `entryTemplateId` to instantiate a template ([Benchling API docs](https://docs.benchling.com/docs/api/benchling-api/entry/create)); template and folder reads use [EntryTemplate.Get](https://docs.benchling.com/docs/api/benchling-api/entry-template/get) and [Folder.Get](https://docs.benchling.com/docs/api/benchling-api/folder/get).
Container creation and transfer use the same local preview and explicit apply lifecycle. Container plans do not take `--entry`:

```console
benchling-tools changes plan --family containers --spec create-container.json --env dev
benchling-tools changes plan --family containers --spec transfer-container.json --env dev
benchling-tools changes apply PLAN_ID --env dev
```

Container create specs use `operation: "create"` and `schema_id`; transfer specs use `operation: "transfer"`, `source_container_id`, `destination_container_id`, and `destination_contents` (each item has an `entity_id`). Transfer previews read both containers and apply re-checks their modification timestamps before the one-time transfer. Create previews read the schema and apply re-checks its field definitions. As with other families, an uncertain write is never retried. MCP apply remains disabled unless the server was started with `--allow-writes ENV`.

Example create spec:

```json
{
  "operation": "create",
  "schema_id": "contsch_example",
  "name": "Sample vial",
  "barcode": "VIAL-001"
}
```

Example transfer spec:

```json
{
  "operation": "transfer",
  "source_container_id": "con_source",
  "destination_container_id": "con_destination",
  "transfer_quantity": { "value": 1, "units": "mL" },
  "destination_contents": [{ "entity_id": "ent_sample" }]
}
```

Supported specs are documented in [schemas/change-spec.schema.json](../schemas/change-spec.schema.json). The six families are:

- `entry`: create an entry from a template in a folder; optional schema fields are validated against the template's schema and sent by field-definition ID;
- `document`: insert, delete, move, or replace a text part;
- `table`: set selected cells or append rows on an unstructured table;
- `fields`: update fields already present on the entry's assigned schema;
- `results`: create V2 assay result records for one or more explicit `strtbl_…` tables (each batch can use a different schema and takes at most 500 records, this toolkit's own batch size; a plan that would push a table past Benchling's 1,000-row limit is refused; results land pending until accepted), or update existing results from a CSV file; and
- `containers`: create a container from a container schema, or transfer contents from one container to another.

Field inputs must use a schema field's V3 `systemName` or its field-definition ID and have the shape `{"value": ...}`. Plans show both identifiers, send the field-definition ID, validate value types and constraints, and re-check the affected definitions and values immediately before applying. Result uploads also accept schema display names; those keys are translated to system names in the saved plan and wire payload, and the preview shows each row's `field_name_map`. An ambiguous name is reported as an `ambiguous_field` row problem with its candidate fields, alongside every other problem in that row; unknown names list available system names, field IDs, and unambiguous display names. Entry field updates do not accept display names, so their unknown-field errors omit `available_display_names`. Result uploads also verify that the table belongs to the selected entry, that every record uses the table's result schema, and that any supplied project IDs are readable.

Result upload dropdown values are option IDs (`sfso_…`), not labels. Schema-constrained links are checked against their allowed schema. Storable, any-entity, entry, request, system-category, and attachment links do not require an allowed schema; storable and entry links are checked by ID prefix, while other unconstrained links are checked as non-empty IDs. Decimal fields are checked as numbers against their configured bounds. Constraint problems are reported per field, including multiple problems in one row.

A result update reads a CSV file instead of inline records. `--id-column` is the column that holds the result id (`id` in an assay-result export). Each `--map CSV_COLUMN=FIELD` is required; columns are not guessed from the header. `FIELD` is the export display name, the system name, or the field-definition id. The preview resolves every mapped column to a field-definition id and shows only the values that would change. Unmapped columns, including `createdAt` and `modifiedAt`, are not written. A blank cell clears a value that is present. A blank cell on an empty field is not a change, so an unmodified export round-trips: export writes both null and an empty string as a blank cell. Unknown columns, ambiguous names, and bad rows are one error and nothing is written. Apply re-reads each result and sends `PATCH /result/{id}` with `schemaFields` keyed by field-definition id, and only when `modifiedAt` and the mapped values still match the plan. The CSV is not read again at apply time. The limit is 500 rows. The same spec (`operation`, `csv_path`, `id_column`, `column_map`) is what `changes_plan` accepts.

A result attachment field (a V3 `AttachmentLinkFieldDefinition`) can take a local file. In a results upload, give the field the value `{"file": "plots/run-01.png"}`, optionally with `"blob_type": "VISUALIZATION"` (default `RAW_FILE`) and `"mime_type"` (default guessed from the file name). A relative path is resolved against the current directory. A multi-value attachment field takes a list that may mix file references and existing blob IDs. In a CSV update, an attachment cell `file:plots/run-01.png` does the same, relative to the CSV's folder, and any other text is a blob ID. The preview lists each file under `after.uploads` with its base name, size, SHA-256, MD5, MIME type and blob type; the path and contents are never in the plan or its output. Apply reads each file again and refuses the plan with a conflict if its size or SHA-256 changed. It then uploads it as a V2 blob: one `POST /blobs` up to 10,000,000 bytes, otherwise `POST /blobs:start-multipart-upload`, 10 MiB parts, and `:complete-upload`, with `:abort-upload` if a step fails. The blob ID is journaled before the result write that links it. Uploads are never retried; if the result write then fails, the blob stays in Benchling unlinked. Files over 100 MiB, more than 1 GiB in one plan, empty files, and files inside the toolkit state directory are refused. `benchling-tools blob get ID` (MCP `blob_get`) reads metadata without downloading; `ID` can be a V2 blob UUID or V3 `attachment_…` ID. `benchling-tools blob download ID --output PATH` (MCP `blob_download`) writes the file and reports size, SHA-256, and MD5. `--expect-size`, `--expect-sha256`, `--expect-md5`, and `--compare LOCAL_FILE` verify against caller-supplied values; without them `verified` is false. A failed check leaves the destination unchanged, and an existing file is kept unless `--force` is given.

For result creation, a missing `projectId` defaults to the entry's project or its folder's project. The preview shows the resolved records, project name, and `after.project_defaults` (source and batch row indexes). If no project is available or the folder is unreadable, preview fails with `project_required`: set `projectId` on each record (`src_…`) or explicitly pass `"projectId": null` for creator-only access. Some tenants reject null with `projectId is a required field`. Apply checks every batch's table, schema, project presence, and local files before any upload. Saved older plans missing `projectId` need a new preview. If result creation fails after a blob upload, the error lists `orphaned_blob_ids`: those blobs are uploaded but unlinked, the V2 Blobs API has no delete operation, and a new plan uploads the files again.

Results land as pending: they do not appear in the notebook results table, and have no entry, until a person reviews and accepts (integrates) them in Benchling ([Integrating the Results into the Table](https://docs.benchling.com/docs/example-creating-results)). After a results upload, `changes apply` returns `web_url` and a `next_step` naming the entry, its link, and the result table(s); it states that the results are pending and that a person has to accept them, and gives no UI steps, because the toolkit does not accept results. Creating an entry from a template also returns the new entry's `web_url`. The accept control may insert every pending result that matches the table's schema, not only the ones just pushed. See [docs/api-limitations.md](api-limitations.md). A table write updates the named cells or appends rows, checked against the entry version, and resets table evaluation until Benchling's browser evaluates it again.
Table upload plans use local correlation UUIDs; the tenant assigns the actual result IDs. Local UUIDs are not idempotency keys. Caller-supplied result IDs are rejected by the CLI because the tested tenant rejects them on this endpoint despite the public OpenAPI declaration. Each notebook table batch is limited to 500 records. The immediate apply result is `submission_pending`; task success is reported separately as `pending_integration`.
Use `batches` in a results spec to push multiple tables in one plan:

```json
{
  "batches": [
    { "table_id": "strtbl_example", "assay_results": [{ "schemaId": "assaysch_example", "fields": { "ct": { "value": 12 } } }] },
    { "table_id": "strtbl_other", "assay_results": [{ "schemaId": "assaysch_other", "fields": { "ct": { "value": 8 } } }] }
  ]
}
```

Three canned results specs in [`examples/`](../examples/) use synthetic IDs and generic data: `results-plate-reader.json` (one plate-reader style table), `results-multi-schema.json` (two tables with different result schemas) and `results-invalid.json` (a wrong-type value and a missing required field, for the validation error path). Each record has the `schemaId` and `fields` shape of V2 [bulk create assay results](https://docs.benchling.com/docs/api/v2/benchling-api/assay-results/bulk-create-assay-results). Pass one with `--family results --spec` against an entry that has those tables. To run all three through `changes plan` and `changes apply` against the offline fake API (`tests/fake_benchling.py`, which installs the `strtbl_plate` table), with no credentials or network:

```console
uv run pytest tests/test_examples.py
```

Apply submits each table separately and journals each task. A rejected batch does not stop later batches; inspect the partial journal before making a fresh plan, and never retry automatically. `changes status` checks every task once by default. Add `--wait --timeout SECONDS` to poll until all tasks are terminal or the timeout expires. A failed task's documented error `index` is mapped to that batch's zero-based row index and local correlation ID. Errors without an in-range index remain under `unmatched_row_errors` alongside the raw error object.
Entry creation is also single-use. A plan with a successful or uncertain create attempt cannot be applied again; a new plan and apply can still create another entry.

## MCP server

One server serves every environment saved with `auth login`. The client config carries no secrets; the server reads the same `environments.json` file and OS keychain as the CLI. Log in in your own terminal before starting the server. The shell form of the config below is `uvx benchling-tools mcp serve`. That command installs the package from PyPI. From a checkout, run `uvx --from . benchling-tools mcp serve`.

```json
{
  "mcpServers": {
    "benchling-tools": {
      "command": "uvx",
      "args": ["benchling-tools", "mcp", "serve"]
    }
  }
}
```

On Linux the standard MCP SDK starts the stdio server with only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER`. `DBUS_SESSION_BUS_ADDRESS` is not among them, so the server cannot see the keyring and exits before the protocol starts. Put the long-lived session bus from Headless Linux in the client config `env` block. A `dbus-run-session` that has already exited is not a usable value:

```json
{
  "mcpServers": {
    "benchling-tools": {
      "command": "uvx",
      "args": ["benchling-tools", "mcp", "serve"],
      "env": {
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"
      }
    }
  }
}
```

Startup failures, including a missing keychain, are a JSON envelope on stderr and a non-zero exit. Stdout is reserved for the MCP protocol. The stderr text names the MCP SDK's default environment and the `env` block. Request logging is off unless you pass `--verbose`.

The tools are generated from the CLI: each command is one tool named after its command path (`benchling-tools entry get` is `entry_get`, `benchling-tools results pending` is `results_pending`), with the same arguments, defaults, limits, and help text. Each tool description ends with a generated write class: reads only, writes a local file or plan (`blob_download`, `export`, `changes_plan`, `changes_export`), or writes to Benchling (`changes_apply`, with its gating rule). Flags become arguments with underscores (`--part-offset` is `part_offset`). Commands that need a terminal or change local credentials (`auth login`, `auth logout`, `auth protect`, `auth unprotect`, `skill path`, `skill show`, and `mcp serve`) have no tool.

Every tool except `auth_status` and `issue_draft` takes a required `env` argument, and each call is authenticated with that environment's credentials. Every parameter on every tool has a description in the published input schema. The server is read-only by default. To permit `changes_apply` in specific environments, add `--allow-writes ENV` after `serve` (repeatable, e.g. `--allow-writes dev --allow-writes test`); reads remain available in all environments. A [protected environment](#protected-environments) is refused unless it is in `--allow-writes`, and the refusal says it is protected. A plan can only be applied or checked in the environment that created it. `changes_plan` remains available because it performs remote reads and writes only an immutable local preview. Its `spec` is a typed object, the same shapes as [schemas/change-spec.schema.json](../schemas/change-spec.schema.json): a document or table `operation`, `schema_fields`, inline `assay_results` or table-specific result `batches`, or a result update with `operation` `update`, `csv_path`, `id_column`, and `column_map`. A spec that does not match is a `validation_error` in the standard envelope, and nothing is planned. `changes_apply` is still gated by `--allow-writes ENV`. `changes_status` accepts a local plan ID or portable plan file/bundle, polls result tasks, and accepts `wait` and `timeout`; status checks work without `--allow-writes`. `changes_export` writes a local portable plan bundle and works without `--allow-writes`. The CLI still passes its JSON file to the planner. Large entry, section, table, result, and schema responses expose offset/limit or cursor pagination and never silently truncate data. `entry_list`, `schema_list`, and `results_rows` use `limit` (default 50, maximum 100) and return `next_token` for the following page. `results_rows` is a read, so it works without `--allow-writes`. `results_pending` (one of `entry`, `plan_id`, `result_ids`, or `schema_id` with `created_after`) and `results_verify` (`plan_id`, or `payload` as a JSON object with `result_ids` or `created_after` and optional `created_before` and `entry`; and `sample`) are the MCP forms of `results pending` and `results verify`; they are reads and work without `--allow-writes`. `results_diff` (`left`, `right`, and optional `run_field`, `table`, `key`, `fields`, `resolve_barcodes`, `compare_blobs`, `sample`) is the MCP form of `results diff`; it is a read, returns the JSON summary, and has no file output. `export` reads every page internally and writes a local JSON or CSV file. Its response is the same summary as the CLI (row count, columns, path, SHA-256) and does not include the rows. It does not write to Benchling, and it does not need `--allow-writes`. An existing file is left unchanged unless `overwrite` is true.

The CLI and MCP tools share the same versioned envelope. Static schemas for the envelope and change specs live under [`schemas/`](../schemas/). MCP publishes the generated input and output schema for each tool during discovery. Each input parameter includes a description, and `changes_plan` `spec` is the typed union of those change shapes.

## Logging what the toolkit cannot do

Agents that use this toolkit follow one rule, written into [SKILL.md](../SKILL.md) and the MCP server instructions: when the person asks for something the toolkit cannot do, the agent always explains the limitation and asks whether to log it as a bug or a feature request on this repository. Only after an explicit yes does it draft the issue, show it, and file it once the person confirms.

`benchling-tools issue draft` and the MCP tool `issue_draft` build that issue from structured fields: kind (`bug` or `feature`), summary, what was attempted, expected and actual behaviour, the interface and command or tool, a detailed description, a proposed solution, and at least one citation. The toolkit version, Python version, and OS are filled in. Each citation is a URL on [docs.benchling.com](https://docs.benchling.com), [help.benchling.com](https://help.benchling.com), or the API reference under `benchling.com/api/`, plus the quoted line that shows the solution is possible. A draft is refused when a field looks like a tenant URL, a Benchling record or display ID, a token or secret, or an email address.

```console
benchling-tools issue draft --spec issue.json          # prints title, body, labels, and a prefilled new-issue URL
benchling-tools issue draft --spec issue.json --file   # runs gh issue create; only after the user confirmed the draft
```

Drafting does not contact Benchling or GitHub. The MCP tool is draft-only: the person opens `new_issue_url` to file it. The repository's issue forms ask for the same fields and require the Benchling documentation citation.

## Safety model

- **Preview, then apply.** `changes plan` only reads Benchling and writes an immutable local plan; nothing changes remotely until `changes apply`. A document move that would leave the entry starting with anything other than a section fails in preview, before a plan is saved. Benchling rejects that shape with HTTP 400 `Entry must start with a section`.
- **Journals record the HTTP status.** Each apply step stores the raw status Benchling returned (`http_status`) next to the response body. A local failure that never reached the API has no status.
- **Plans are checksummed and scoped.** A plan stores a SHA-256 of its content, verified on load, and is bound to the environment and tenant that created it. The checksum catches accidental corruption; it is not a signature and does not defend against someone who can already write to your state directory.
- **Version preconditions.** Document edits and table writes send the entry version observed at preview time, and Benchling answers HTTP 409 if it is stale. Table plans re-check that entry version just before writing. A concurrent edit becomes a conflict instead of an overwrite. Entry field updates have no atomic server-side precondition, so the affected values are compared immediately before writing; this is best effort. CSV result updates re-check each result's `modifiedAt`, entry, schema, and mapped values before any `PATCH /result/{id}`. That PATCH body has no version field, so the check is local. A mismatch writes nothing.
- **Single use.** A plan with any prior attempt cannot be applied again, and a per-plan file lock stops a CLI and an MCP process from applying the same plan concurrently on one machine.
- **Writes are never retried.** A write that times out or fails with a server error leaves the journal in `unknown`/`partial` state for a person to reconcile; reads are retried with bounded backoff.
- **Protected environments.** `changes apply` on an environment marked protected is refused unless `--env` names it on the command line and `--confirm-env` repeats the name. MCP `changes_apply` needs it in `--allow-writes` and `confirm_env` equal to `env`. The flag is local configuration. It is not a Benchling permission.
- **Read-only MCP by default.** `changes_apply` is refused unless the server was started with `--allow-writes ENV` for that environment. `changes_plan` stays available because it only reads Benchling and writes a local preview.
- **Export writes a local file only.** `export` reads Benchling and writes JSON or CSV on this machine. The response is a summary. An existing file is not replaced unless overwrite is set.
- **Explicit environment on every call.** There is no default environment, so a command or tool call cannot reach a tenant by omission.
- **Secrets stay in the OS keychain.** The tenant URL and client ID are stored in `environments.json` with mode 0600. Access tokens stay in process memory. Error envelopes and journals are scrubbed, and TLS verification uses the OS trust store with no insecure fallback. There is no plaintext secret file.

## Design decisions and tradeoffs

- **Plan/apply instead of one-call edit tools.** An AI agent reviews a concrete diff before anything is written, and a plan that has already been attempted cannot be applied again from this state directory. The cost is an extra step and some local state to manage.
- **OS keychain, no default environment.** The client secret is stored by the operating system and the target tenant is always explicit. The cost is a keychain dependency and no headless plaintext fallback.
- **App identity, not user identity.** OAuth client credentials give the toolkit its own permissions and audit identity, separate from any person's. The app must be granted access to each project it touches.
- **One service layer, two interfaces.** The CLI and MCP server call the same typed services and return the same envelope, so behavior is specified and tested once. The cost is a more verbose, JSON-first CLI.
- **Honest handling of API limits.** Unstructured table cells are edited one cell at a time, and new rows are inserted at the end of the table; evaluation is reset until Benchling recalculates it. Replacing text is an insert followed by a delete and can end in a reported partial completion.
- **Local, single-machine state.** Plans, journals, locks, and `environments.json` are plain files under the application directory (`BENCHLING_TOOLS_HOME` overrides it). Client secrets are in the OS keychain for that user. Concurrency control does not extend across machines.
- **Offline tests against a stateful fake.** The suite runs the real client, services and planner against an in-process fake Benchling API instead of stubbing each call. This tests behavior end to end; the cost is that the fake can drift from the real API, which is why the opt-in sandbox exercise exists.

## JSON envelope and exit codes

Every command and MCP tool returns `{"schema_version": "1.0", "ok": ..., "data": ..., "error": ..., "meta": ...}`. A command line that does not parse also prints an envelope, with `error.code` `usage_error` and the command, usage line, and help command in `error.details`. For an unknown option, `details.valid_options` lists that command's long options, and `details.hint` names the right alternative where one is known (for `results pending`, the four sources; for `results list --plan`, `results pending --plan` or `results verify`). An extra positional argument that looks like an entry reference on a command with `--entry` gets the hint `Pass the entry with --entry REF`; only `--help` and `--version` print plain text. `--env NAME` is an option of each command, after the command name: `benchling-tools entry get REF --env NAME`. Exit codes:

| Code | Meaning |
|---:|---|
| 0 | Success |
| 2 | Input or validation error |
| 3 | Authentication error |
| 4 | Access denied or resource not found |
| 5 | Concurrent modification or reused plan |
| 6 | Unsupported capability |
| 7 | Upstream or transport failure |
| 8 | Partial or unknown write completion |
| 9 | Usage error: an unknown command or option, or a missing or invalid argument |

## Known limitations

- **`replace_text` on flattened V3 text parts is verified against fixtures only.** The fix in 0.1.2 that lets `replace_text` accept the flattened `DocumentPartText` shape was built and tested against synthetic fixtures. It has not been verified against a live Benchling tenant, so do not rely on it for production data until it has been checked there (`scripts/live_cli_session.py` and `scripts/sandbox_exercise.py` are provided for that).
- The V3 Document API is in beta and Benchling may change it.
- Table cell edits and row inserts reset evaluation until Benchling recalculates the table in the browser. Only unstructured tables are editable. Results tables can be read; other structured tables cannot.
- Entry field updates are protected by a best-effort value comparison, not an atomic precondition.
- Replacing text can end in a partial completion that a person must reconcile.
- Results created through the API land as pending until a person accepts them in the results table in Benchling.
- State is local to one machine.
- The offline suite uses a synthetic fake; it cannot prove compatibility with every tenant configuration.
- `entry list --name` is an exact case-insensitive match when the name has no comma (`name.anyOf` on V3, `names.anyOf` on V2 with `--folder`). It does not search substrings. A name that contains a comma is sent as V2 `name`, because those list parameters are comma-separated and document no escape. A modified timestamp must include a time zone. Folder filters use the V2 entry list because the V3 list ignores `folderId`. Listing timestamps are UTC with a `Z` suffix.

See the [setup guide](setup.md) before the first login, and [API limitations](api-limitations.md) and the [research record](research.md) before enabling writes against production data.

## The agent skill file

[`SKILL.md`](../SKILL.md) is the Agent Skills file for this repository. Its frontmatter keeps `name`, `description`, `license`, `compatibility`, and a `metadata` map of strings (`summary`, `version`, `docs_url`). Tags, keywords, and use cases are lists in [`agensi/listing.yaml`](../agensi/listing.yaml). The package installs it as `share/benchling-tools/SKILL.md` under the environment prefix (`sys.prefix`), next to the JSON schemas and contracts. Use `benchling-tools skill path` to print the installed file's absolute path and `benchling-tools skill show` to print its contents. Both look under `sys.prefix`, `sys.base_prefix`, and the user install scheme, then fall back to the repository's `SKILL.md` for editable and development installs. If the file is not found, they print a `not_found` envelope. They never contact Benchling. The MCP server also serves the file as the read-only resource `skill://benchling-tools/SKILL.md`.

```console
benchling-tools skill path
benchling-tools skill show
```
