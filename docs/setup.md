# Benchling setup

Create the Benchling apps this toolkit uses, grant each one access to specific projects, and save each app with `benchling-tools auth login --env NAME`.

This project is unofficial and not affiliated with or endorsed by Benchling, Inc. Benchling is a trademark of Benchling, Inc.

The toolkit calls the API as the app. The app has its own permissions and its own audit identity. Login uses the OAuth client-credentials grant described in Benchling's [authentication](https://docs.benchling.com/docs/authentication) guide. A personal API key is not accepted, and delegated login (the app acting as the signed-in user) is not used.

Install the `benchling-tools` command before the login step. `uv tool install benchling-tools` and `uvx benchling-tools mcp serve` install the package from PyPI. From a checkout, use `uv tool install .` and `uvx --from . benchling-tools mcp serve`. Details are in the README section Install.

## Create an app

A tenant admin grants the Developer Platform capability before anyone can open the Developer Console: avatar, Tenant Admin Console, Users, Manage application access, Developer Platform set to Full. Benchling documents that in [Access Benchling's Developer Platform](https://help.benchling.com/hc/en-us/articles/9714802977805-Access-Benchling-s-Developer-Platform-and-Documentation-Base).

Create the app from the Developer Console. The path and the credential screens are in [Getting started with Benchling apps](https://docs.benchling.com/docs/getting-started-benchling-apps).

1. Open the user menu, then Feature Settings, then Developer Console, then Apps.
2. Click Create. For an app that stays on your own tenant, choose Private. Create it from scratch: a name and a short description are enough. The Developer Console can also create an app from a manifest; this toolkit does not ship one.
3. Make two apps when you follow the least-privilege section below. Name them so the read app and the write app stay distinct, for example `toolkit-read` and `toolkit-write`.
4. On the app's Version History tab, use Install on at least one version. An app that is not installed cannot call the API.
5. On the app settings page, copy the client ID. It identifies the app. It is not a secret, and it does not change.
6. On that same page, generate the client secret. Benchling shows the secret once. Copy it into a password manager, or straight into the login prompt below, and then close the dialog. A lost secret is replaced by generating a new one, which deletes the previous secret.

Keep the secret only until `benchling-tools auth login` has stored it in the OS keychain. It is never a command-line argument, a shell history entry, a commit, a chat message, or a field in an MCP client config.

Archiving an app revokes its API access immediately. Unarchiving restores access and keeps the current secret. If a secret is exposed, generate a new one in the Developer Console and run `benchling-tools auth login --env NAME` again for that environment.

## Grant project access

A new app can see no data. Add it as a collaborator on each project it should touch, the same way you add a person. Benchling's [permissions overview](https://help.benchling.com/hc/en-us/articles/9684210957581-Permissions-overview) defines the default access policies:

| Policy | What the app can do |
| --- | --- |
| Read | View entries and other project data. It cannot create or edit. |
| Append | Create items. It cannot edit or archive existing ones. |
| Write | View, create, edit, and archive. On the default policy, editing an entry is limited to entries that the collaborator authored. |
| Admin | The Write actions, plus managing collaborators and project settings. |

Give each app the smallest policy that does the job, on only the projects it needs. An agent that edits notebook entries a person authored may find the default Write policy too narrow: its Edit entries action is granted to the author. A tenant admin can clone that policy under Feature Settings, then Access Policies, and set Edit entries to Granted. Admin can edit entries the app did not author, and it can also change who else has access. Leave Admin off an agent app when a narrower policy is enough.

Registry, schema, inventory, and the other modules have their own policies. Project access is what lets the app read and edit notebook entries. Grant another module only when the app has to touch objects that live there.

To add the app to one project:

1. Open the Projects panel (the briefcase).
2. Right-click the project, or open it and use the gear next to its name, and choose Manage access. The project creation menu and the project settings menu call the same control Manage collaborators.
3. Search for the app, choose the access policy, and add it.
4. Save. A new project uses Done.

Adding the app to an organization gives it every project that all members of that organization can access. Some tenants also open the Registry that way; others scope the Registry by project. Adding the app to a team gives it the projects that team can access. Permissions stack: Read on the app itself plus Write through a team is Write. For an agent, add the app directly to specific projects and leave the organization and the team alone.

Folders inherit the project policy and can only be more open, so a Write project stays Write in every folder under it. Put the tight policy on the project.

A 403 or a 404 on an object usually means the app is not a collaborator on that object's project. `benchling-tools doctor --env NAME` checks that the V3 and V2 entry-list routes accept the app. An empty page means the token worked. It does not prove the app was added to a particular project. Confirm one with `benchling-tools entry list --project src_example --env NAME` or `benchling-tools entry resolve EXP-0001 --env NAME`, using an id from your tenant.

## Least privilege: a read environment and a write environment

Create two apps and save them as two environments. The MCP flag `--allow-writes` is a local switch. It does not grant anything Benchling withheld, and the CLI `changes apply` does not consult it. The app's Benchling policy is what stops a read session from changing data.

- Read app. Access policy Read, on the projects agents may inspect. Save it with `benchling-tools auth login --env read`. Use this environment for agents and for everyday MCP sessions. Do not start the server with `--allow-writes read`.
- Write app. Access policy Write, or Append when the agent should create records and leave existing ones unchanged, and only on the projects that should change. Save it with `benchling-tools auth login --env write`. A server that may apply plans is `benchling-tools mcp serve --allow-writes write`.

Keep the write environment out of agent configs that only need to read. Environment names use lowercase letters and digits, start with a letter, and are at most 32 characters. `read` and `write` fit that rule. There is no default environment. Every command and every MCP tool call names one.

## Log in with `benchling-tools auth login --env NAME`

Run this in your own terminal. `NAME` is the environment name, such as `read` or `write`:

```console
benchling-tools auth login --env NAME
```

The prompt asks for three values:

- Tenant URL. Use `https://your-tenant.benchling.com`, the bare host, or that same URL with a trailing `/api/v2` or `/api/v3`. A trailing slash and those suffixes are removed. `http://` is rejected.
- Client ID. The identifier from the app settings page.
- Client secret. The prompt does not echo it. The secret is never accepted as an argument.

Login requests a client-credentials token before it saves anything. A rejected id or secret is not written. On success:

- The tenant URL and client ID go in `environments.json`, mode 0600, under the state directory. That file holds no secret. The directory is `$XDG_CONFIG_HOME/benchling-tools`, or `~/.config/benchling-tools` when `XDG_CONFIG_HOME` is unset. On macOS it is `~/Library/Application Support/benchling-tools`. On Windows it is `%APPDATA%\benchling-tools`. `BENCHLING_TOOLS_HOME` overrides the directory and still holds no secrets.
- Upgrading from benchlingtk: move the old `benchlingtk` state directory to `benchling-tools` under the same platform config root, or set `BENCHLING_TOOLS_HOME` to the old directory (including the path previously set in `BENCHLINGTK_HOME`). No files are moved automatically. The keychain service remains unchanged, so saved credentials remain available.
- The client secret goes in the OS keychain. The service name is `benchling-tools`. The keychain username is the environment name.
- Access tokens stay in process memory and are renewed before they expire. They are not written to the keychain or to `environments.json`.

macOS and Windows use the system keychain. Linux uses the Secret Service (`org.freedesktop.secrets`) on the session bus. There is no plaintext file fallback. If the keychain cannot store the secret, login fails and nothing is saved. The error is the cause, one fix command, and a pointer to the README section Headless Linux.

Confirm the save, then the token, then a read:

```console
benchling-tools auth status
benchling-tools auth status --check --env NAME
benchling-tools doctor --env NAME
```

`auth status` lists each saved environment, its tenant URL without a trailing slash, and whether a keychain secret is present. It never prints the secret. `--check` requests a token and puts a failure in the top-level message. `doctor` is read-only: one page of V3 entries and one page of V2 entries. A failed check exits 3, the envelope is `ok: false`, and each failed check includes a hint.

`benchling-tools auth logout --env NAME` removes that `environments.json` record and its keychain secret. It does not archive the app or rotate the secret in Benchling. Logging out an environment that was never configured says that it is not configured.

A 401 from the token endpoint says `Benchling at https://<tenant> rejected the client ID or secret.`

## Headless login

Agents, CI, and machines without a terminal pass the tenant and client ID as flags and the secret on stdin:

```console
printf '%s' "$BENCHLING_CLIENT_SECRET" | benchling-tools auth login --env NAME \
  --tenant-url https://your-tenant.benchling.com \
  --client-id your-app-client-id \
  --secret-stdin
```

If a value is missing and there is no terminal, login exits with one error that names `--tenant-url`, `--client-id`, and `--secret-stdin`. It does not fall back to a hidden prompt on a pipe.

On a Linux server or agent machine, the Secret Service is often missing from the session bus. Install a provider when it is missing (`gnome-keyring` and `dbus` on Debian and Ubuntu), unlock a keyring, and run login in that same session. The keyring password stays a shell variable:

```console
dbus-run-session -- sh -c '
  printf "%s\n" "$KEYRING_PASSWORD" | gnome-keyring-daemon --unlock --components=secrets
  printf "%s" "$BENCHLING_CLIENT_SECRET" | benchling-tools auth login --env NAME \
    --tenant-url https://your-tenant.benchling.com \
    --client-id your-app-client-id \
    --secret-stdin
'
```

A wrong `KEYRING_PASSWORD` for an existing login keyring makes the first keyring call hang. Use the right password, or remove `~/.local/share/keyrings/login.keyring` for a fresh scratch setup.

`dbus-run-session` sets `DBUS_SESSION_BUS_ADDRESS` for that shell only. When the shell exits, the bus exits with it, and a secret stored there is gone. MCP, later shells, and cron need a bus that stays up. The [reference section Headless Linux](reference.md#headless-linux) shows a long-lived `dbus-daemon --session --fork` bus, the systemd user bus, and the MCP client `env` block. Do not capture `gnome-keyring-daemon --unlock` with `$(...)` or backticks. The daemon does not exit, so the shell waits forever.

The standard MCP SDK starts the stdio server with only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, and `USER`. Put the long-lived `DBUS_SESSION_BUS_ADDRESS` in the client config `env` block or the server cannot see the keychain. macOS and Windows use the system keychain and do not need this session.

## After login

Reads use the read environment:

```console
benchling-tools entry list --project src_example --env read
benchling-tools doctor --env read
```

A server that can apply plans only in the write environment:

```console
benchling-tools mcp serve --allow-writes write
```

The read-only default, the client config, and `--allow-writes` are in the [reference section MCP server](reference.md#mcp-server).
