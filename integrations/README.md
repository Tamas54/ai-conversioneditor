# Integrations

All three talk to the same REST API (`/v1`). The base URL is part of the connection, so they work with any domain.

| Platform | Folder | What it offers |
|---|---|---|
| Zapier | `zapier/` | Action **Convert file** (URL → any format), action **Run job** (JSON pipeline), trigger **Job finished** (REST hook) |
| Make | `make/` | Modules **Convert a file**, **Run a job**, **Get a job**, instant trigger **Watch finished jobs** (webhook) |
| n8n | `n8n/` | Node **AICE** (Convert, Run job, Get job, Unit conversion), trigger node **AICE Trigger** (webhook) |

Build and publish:

- **Zapier**: `cd zapier && npm install && npx zapier-platform-cli push` (needs a Zapier developer account).
- **Make**: import the folder with the *Make Apps Editor* VS Code extension (or paste each JSON into the
  corresponding tab of a new custom app at make.com → Custom apps).
- **n8n**: `cd n8n && npm pack` and install the tarball under *Settings → Community nodes*, or publish to npm.

Tests (no network, mocked platform objects): `node --test zapier/test/*.test.js n8n/test/*.test.js`.
