# Studio — OpenAI plugin (ChatGPT / Codex)

A plugin package for the Studio MCP server: the listing details ChatGPT shows (name, version, category, developer,
logo, suggested prompts) and six skills — workflows that teach the client AI how to use Studio's tools well.

**Status: not submitted, not public.** For testing in developer mode or a private workspace only (Kommandant,
2026-10-18: „még ne legyen nyilvános, még tesztelni kell, még nem küldjük be”).

```
openai-plugin/
├── .codex-plugin/plugin.json   name, version, description, interface (displayName, category, logo, defaultPrompt…)
├── .mcp.json                   the MCP server: https://ai-conversioneditor-production.up.railway.app/mcp (OAuth)
├── .app.json                   the ChatGPT connector this plugin maps to — put the real id in (see below)
├── assets/                     logo.png (512 px), composer-icon.png (192 px) — the same icon the server sends
└── skills/                     studio-presentation, studio-video, studio-graphics-on-video, studio-documents,
                                studio-check-and-fix, studio-scan-book
```

## Before testing

1. `.app.json`: replace `REPLACE_WITH_THE_CONNECTOR_ID_FROM_CHATGPT` with the id ChatGPT gives the connector
   (Plugins → Add custom MCP server, or the existing Studio connector's details).
2. Install it locally (Codex): add an entry to `~/.agents/plugins/marketplace.json` (personal) with
   `"source": {"source": "local", "path": "./…/openai-plugin"}`, a `"policy"` with `"installation": "AVAILABLE"` and
   an `"authentication"` value (see the docs), and `"category": "Creativity"`; or publish it to a private workspace
   from the Plugins page (workspace admins) — that does not make it public.

## Before a public submission (later — not now)

- The final product name (`name`, `displayName`) and domain → `websiteURL`; the server's `PUBLIC_BASE_URL` follows.
- Public **privacy policy** and **terms of service** pages (`privacyPolicyURL`, `termsOfServiceURL`) — the app has
  none yet; a `supportURL`.
- Screenshots (`interface.screenshots`), OpenAI business verification, the submission portal review.

Format: the same layout as OpenAI's published plugins (e.g. `openai/plugins` → `plugins/canva`).
Docs: https://developers.openai.com/plugins/build/plugins
