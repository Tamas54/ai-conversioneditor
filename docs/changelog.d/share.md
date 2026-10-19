## Phase 4 — shareable workspace (lane "share")

- **Sharing (owner / editor / viewer), behind `SHARING_ENABLED=1`** (off by default — off, everything behaves as before:
  workspaces stay physically isolated). Share a file or a video project by user or e-mail; an e-mail without an account
  becomes a pending invite that resolves on sign-up / sign-in (no e-mail is sent — the app has no mail path). Role links
  (`/share/<token>`) work only for signed-in users; signed download links stay read-only.
  A share covers the file's *family* in the owner's workspace: its versions, renders, the project's media assets (from
  every version's `assets` map) and — read-only — the footage it was made from.
- **One ACL gate for every request**: the auth middleware reads every file id in the URL, query, JSON body and MCP
  tool arguments, checks the actor's role and runs the request in the owner's workspace; the acting user stays the
  operation's owner (credits, quotas, jobs, audit). Web: 403 `{"detail", "not_shared": true}`; MCP: a tool error
  (`isError`), no job is started. New Postgres tables: `shares`, `share_links`, `share_audit`.
- **Shared with me**: `/api/files` → `shared` (owner name, role), MCP `files(action="list")` → `shared_with_me`;
  `files(action="share" | "share_link")`. Edits by invitees are new versions in the owner's workspace with
  `edited_by`; the audit log (`share_audit`) records who shared / changed what and when.
- **Retention**: shared projects and their whole family (assets included) are exempt from the 30-day cleanup while
  the share (or a pending invite) is active.
- **Versions with optimistic locking**: every edit stays a new version; if someone else saved a newer version of the
  same base meanwhile (or of the `if_version` the client started from — any MCP tool and the editor save accept it),
  the new one becomes a fork labelled "<Name>'s version", the result carries `conflict {fork, other, message}`, and
  the user picks the current one (`POST /api/share/choose`, MCP `files(action="choose")`); nothing is lost. Same
  person branching an older version is not a conflict. Advisory edit lock with a 30 s heartbeat (stale after 90 s,
  `edit_locks` table) only tells who is editing; it never blocks.
- **Web UI** (only when sharing is on): file view "More → Share…" dialog (invite by e-mail with Can edit / Can view,
  change role, remove, pending invites, link for signed-in people, "keep location" opt-out, recent activity);
  Files → "Shared with me" (owner's name on each card, "By <name>" on versions others made in your workspace); the file
  header tag "<Owner>'s · you can edit"; a "Two versions" bar that lets the user choose the current version after a
  parallel save; a "<Name> is editing this right now" bar from the edit-lock heartbeat.
