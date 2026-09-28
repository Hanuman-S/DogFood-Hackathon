# Plan: Stages A, B, C1, C2, C3, D on main, in 12 phases (revision 2)

## Context
T1 and T2 are claimed and verified. T3 voting is built; comments were cut. This round covers:
- A: fix the stale README.
- B: comments on gallery projects, which completes T3.
- C1: a portable event bundle (export and import).
- C2: Ed25519-signed certificates.
- C3: an embeddable gallery widget.
- D: a final pass.

The claim stays `["T1","T2"]`. C4 is not started.

## Execution rules
- **One phase at a time.** After each phase:
  1. Run `MSYS_NO_PATHCONV=1 ./scripts/test.sh` as its own command.
  2. Report `N passed / M failed / E errors`.
  3. Commit, as a separate command, only if there are 0 failed and 0 errors.
  4. **Stop and wait for "go".**
- **Before any push:** `git pull --rebase origin main`.
- **Each report lists:** files changed, migrations added, the test-count delta, decisions made, and any deviation from the plan.
- If a phase invalidates a later phase, I stop and say so rather than work around it.
- Every stage that adds `AuditAction`s gets **its own** core `AlterField` migration (fix 14).

## Agreed decisions
1. **"Public" means exactly what `/projects` shows an anonymous visitor.** That is the viewer-independent `projects.gallery.visible_projects()`, including whatever it does with duplicates. Comments and the embed both use only this set, and no new flags are added. Tests check that:
   - a team member cannot comment on their own draft;
   - a draft never appears in the embed or in the comments API.
2. **Snapshot ids are remapped.** The export rewrites the known id paths to bundle ids, and the import rewrites them to the new PKs inside the INSERT. The export **fails** if an id-like key sits outside the known paths. `input_hash` stays the source's value and is documented that way. The round-trip test checks that the imported results page shows the same projects, order, ranks and tie groups.
3. **An import is refused with 400 `voting_in_progress` only when the bundle has ballots and its vote has not closed.** The message says: "End voting now" on the source install, then export again. A vote that has not opened, or an open vote with zero ballots, imports normally.

## Decisions I made (open to amendment)
- **Audit rows keep their ids as labelled source history.** Ids in `detail` are not remapped. The `subject` and `detail.event` slugs are rewritten to the new slug. Each imported row gets `detail.source_history = true`, and the trails show "(source id)". This is documented in DATA-MODEL.md.
- **Voter pseudonyms cover every copy.** The pseudonym is `"v_" + HMAC(per-export random salt, voter identity)[:16]`, and the salt is discarded after the export. It replaces the voter in ballots and in voting audit rows (`actor`, `actor_email`, `detail.voter`). `user_agent` and every `ip_hash` are dropped from all exported audit rows.
- **User matching on import (fix 2): the importer's role decides.**
  - A platform admin gets users matched by email.
  - A `can_create_events` importer who is not an admin gets **placeholder users**: new accounts with `imported-<n>-<8hex>@import.invalid`, the display name, and an unusable password. So a non-admin can never attach real existing accounts to an event.
  - Issued records and signing keys are imported **only for platform admins**. For other importers they are skipped, and the summary counts them.
- **`BundleError` is always 400** (fix 9). This includes `importer_is_competitor`, which can only happen for an admin who is a competitor in the bundle. Permission refusals are 403 and are not `BundleError`s.

---

## Phase 0: copy the plan and answer from the code
Copy this plan to `docs/stages-plan.md` (commit only after the suite is green). Then answer from the code, with file:line:
- (a) what `visible_projects()` does with duplicates (and `possible_duplicates`);
- (b) whether anything recomputes `build_input` and compares it with `input_hash`;
- (c) whether the audit log is append-only or hash-chained (trigger? only `record()`?), and whether `trail()` orders by id or by `created_at` (it is `-created_at, -id`; I'll check for other readers);
- (d) whether `ballot_secret`, `open_link_nonce` and `Team.invite_token` are nullable.

If an answer changes a later phase (for example, a hash chain means imported audit rows can't be inserted as-is), I stop and say so.

## Phase 1: Stage A (README)
- Rewrite the top status (`README.md:6-17`): T1 and T2 are claimed and verified; T3 is built and evidenced by `t3-report.txt`, but not claimed; results pages exist.
- Add `Demo video: [VIDEO_URL]`.
- Layout (`README.md:272-294`): fix `scoring/`, add `voting/`, and list all six scripts.
- Demo seed (`README.md:62-69`):
  - the archive's project count, checked against `ARCHIVE_PROJECTS`;
  - its open quadratic vote, 80/20, with ten ballots and the cluster;
  - the fixture event's closed vote (March 2026, no ballots).
- Fix the dangling "What this does not stop" reference.

## Phase 2 (B1): comment model, errors, services, service tests
**Model and migrations**
- `projects.Comment`:
  - `project` FK CASCADE, `author` FK PROTECT;
  - `body` (CHECK `length ≤ 2000`), `created_at`;
  - `hidden_at`, `hidden_by` (PROTECT), `hide_reason` (CHECK: all three set or none), `deleted_at`;
  - index `(project, -created_at, -id)`.
- `Event.comments_enabled` (default True).
- Core migration: `COMMENT_POSTED`, `COMMENT_REFUSED`, `COMMENT_THROTTLED`, `COMMENT_DELETED`, `COMMENT_HIDDEN`, `COMMENT_RESTORED`, `COMMENT_MODERATION_REFUSED`, `COMMENTS_TOGGLED`.
- Audit rows use `subject = event.slug`, with `detail` holding `{project, comment, reason?, limit?}`.

**Errors (`projects/comment_errors.py`)**

| Code | Status |
|---|---|
| `no_project` | 404 |
| `comments_disabled` | 409 |
| `rate_limited` | 429 |
| `duplicate_comment` | 409 |
| `invalid_comment` | 400 |
| `no_comment` | 404 |
| `no_event` | 404 (turning comments on or off by someone who is not an organizer of the event) |

Hide and restore through the API (`POST /api/comments/<id>/hide|restore`) have two gates, and each answers differently:
- **403** from the organizer portal gate (`portal_required("organizer")`, audited `ACCESS_DENIED`) for anyone who can't enter the organizer portal: participants, judges, and visitors (a visitor gets 401).
- **404 `no_comment`** from the service for an organizer of *another* event: they pass the portal gate, but the comment isn't theirs to moderate. A missing comment gives the same 404.
| `invalid_moderation` | 400 |

**Services (`projects/comments.py`)**: they never take the request, and read `db_now()` once.
- `post_comment(project_id, body, *, author, origin)` checks in this order:
  1. The project is in `visible_projects()`, otherwise 404.
  2. Comments are enabled, otherwise 409.
  3. Rate limits (`core.ratelimit.exceeded`):
     - per actor `COMMENT_RATE_PER_USER` (5 per 10 minutes), per IP `COMMENT_RATE_PER_IP` (200 per 10 minutes; raised at the B1 review, per account is the primary control);
     - counted over `(COMMENT_POSTED, COMMENT_REFUSED)`. Anonymous attempts are audited as `COMMENT_ANONYMOUS_REFUSED` (core 0018), a separate bucket that the logged-in limits don't count. That bucket has its own cap, `COMMENT_ANON_RATE_PER_IP` (60 per 10 minutes). Past the cap an attempt is still a 401, but it writes at most one `COMMENT_ANONYMOUS_THROTTLED` row per window (core 0019), under a per-address advisory lock;
     - a hit is a 429 with a `COMMENT_THROTTLED` row carrying the origin's ip_hash.
  4. Validation: strip the body; empty or over 2000 characters is a 400.
  5. Inside `atomic()`: lock the author with `select_for_update`, check for a duplicate (the same stripped body from this author **on this project**, hidden or deleted included, within the last 10 minutes; the same text on another project is accepted) → 409, then insert.

  Refusals are audited outside the transaction, before the error is raised.
- `delete_own_comment` (soft delete): anyone but the author gets 404; the row is locked.
- `hide_comment` / `restore_comment`:
  - `is_organizer_of(actor, event)` (admins included), otherwise 404, audited;
  - a reason of 1-300 characters is required;
  - a wrong state is a 400;
  - the row is locked.
- `set_comments_enabled`.
- `comments_for(project, viewer, page)`:
  - organizers and admins see every comment;
  - everyone else sees only comments that are neither hidden nor deleted;
  - newest first, 20 per page, `select_related`.

**Tests** cover the service rules, the order of refusals, the rate limits (both kinds, audited with the ip_hash), the duplicate check and the 10-minute expiry, moderation permissions (another event's organizer gets 404), a draft or unpublished-event project (404), and the team member's own draft.

## Phase 3 (B2): pages, API, moderation, integrity trail, probes
- **Project page:** the paginated comment list, bodies rendered with `|markdown`, a post form for logged-in users, and a delete button on your own comments. Anonymous visitors get a login link. `public/comments.py` handles POSTs with `login_required` and CSRF.
- **Organizer page `/organizer/events/<slug>/comments`:** `portal_required("organizer")` plus `get_managed_event`. It lists every comment with its state, hide (with reason) and restore, and the comments toggle.
- **API:**
  - `GET/POST /api/projects/<id>/comments`: anonymous POST gets 401; a bad page falls back to page 1.
  - `POST /api/comments/<id>/delete|hide|restore`.
  - Refusals use `core.api.error`. No path returns 500: bad JSON is a 400 `bad_request`.
- **Integrity trail:** add the comment actions to `voting/integrity.AUDIT_ACTIONS` and a summary branch in `trail()`.
- **`scripts/t3_check.py` probes:**
  - anonymous GET 200 and POST 401;
  - participant POST 201;
  - the same body again gives 409 `duplicate_comment`;
  - the organizer hides it, and it is then absent for a visitor and for judge_a;
  - restore;
  - a comment on a draft gives 404.
- **README:** T3 is built in full, but not claimed.
- **Tests:**
  - anonymous can read but not post (page and API);
  - XSS (`<script>`, `javascript:`, `onerror`) is sanitised;
  - hidden and deleted comments are invisible to a visitor, a participant and a judge, and visible to the organizer and an admin;
  - a draft never appears in the comments API;
  - the comments of a non-public project (a draft, or a project of an unpublished event) are 404 in the API for a visitor, a participant and a judge, and readable for the event's organizer and an admin;
  - `django_assert_num_queries` gives the same count for 3 and for 20 comments;
  - comment actions appear in `trail()`.

## Phase 4 (C1a): bundle format, id guard, export
**Format** (documented in DATA-MODEL.md field by field):
- `manifest.json`: `{format:"dogfood-event-bundle", version:1, generator, created_at, files:{path:sha256}}`.
- `event.json`.
- `media/<sha256>.<ext>`.

Rows carry bundle ids `"<kind>:<n>"`, in a deterministic order (`created_at`, then pk).

**Sections:** event (incl. `comments_enabled`), tracks, prizes, questions, criteria, memberships (+ judge tracks), teams, members and extensions, projects (answers, tags by name, media refs), rounds, assignments, scores and items, scoring config, result settings, voting config, ballots and lines (pseudonymised), tally snapshots, result snapshots, publications, comments, and audit rows (`Q(subject=slug)|Q(detail__event=slug)`).

**Users section (fix 6):** `{id, email, name}` only for users referenced by something other than ballots and voting audit rows. A user who appears only as a voter is absent.

**Never exported:** password, API tokens, sessions, judge and organizer invites, `Team.invite_token`, voter links (nonce and digest), `ballot_secret`, `open_link_nonce`, every `ip_hash` and `created_ip_hash`, `user_agent`, `SECRET_KEY` and derived keys.

**`imports/bundle_ids.py` (fix 4)**
- An explicit table of id locations for each JSON kind (`ResultSnapshot.result`, `comparison`, `combined`, `diagnostics`, `engine_config`; `VoteTallySnapshot.rows`, `counts`, `voided_ballot_ids`). I'll enumerate it **from the actual builders** (`scoring/services.py` build_input, `scoring/results.py`, `scoring/engine/*` output types, `voting/services.py` tally freeze) and from the real snapshots of the seeded events.
- It covers:
  - id-valued keys;
  - **ids used as dict keys** (digit-string keys, e.g. `{"12": …}`);
  - **id lists under neutral names** (`tie_groups`, `order`, `ranking`, …);
  - the engine `event_id` (the slug).
- The walker applies the table. Anywhere else it fails if:
  - a key matches `(^|_)id$|_ids$|^ids$|^project$|^judge$|^ballot$`, or
  - any dict key is a digit string.

**Export: `imports/bundle.py::export_event(event, *, actor, origin) -> path`**
- One REPEATABLE READ consistent read, refused inside an outer transaction.
- Canonical JSON: `sort_keys`, no ASCII escaping, strings unchanged.
- Zip entries sorted, with a fixed `date_time`.
- **Streamed to a temp file** (fix 10). The export **refuses** a bundle that would exceed the import caps: 400 `bundle_too_large`, audited.
- Audited as `EVENT_EXPORTED` (a new core migration: `EVENT_EXPORTED`, `EVENT_EXPORT_REFUSED`, `EVENT_IMPORTED`, `EVENT_IMPORT_REFUSED`).
- Access: `get_managed_event`, so organizers of the event and admins. Anyone else gets 404.
- Surfaces:
  - a button on the event page, served from `/organizer/events/<slug>/bundle` as a `FileResponse` of the temp file (deleted after sending);
  - `GET /api/events/<slug>/bundle`;
  - `manage.py export_event <slug> <file>`.

**Tests**
- A forbidden-field grep over the raw zip: the password hash, the token and invite digests, `ballot_secret`, `open_link_nonce`, `invite_token`, every ip_hash value, `SECRET_KEY` and the derived keys.
- Voter emails do not appear in ballots or voting audit rows. Voter-only users are absent from `users`.
- Pseudonyms are stable within an export and differ between exports.
- An unknown `foo_id` key fails the export (audited, no file). So does a digit-string dict key injected into a snapshot fixture.
- Permissions: a judge, a participant and another event's organizer each get 404.
- The size cap refusal.

### Phase 4 amendments (from the Phase 0 findings, agreed at the Phase 1 go)
**Finding 1: `FixtureRef` in the bundle.** `build_input` (`scoring/services.py:670-690`) reads `FixtureRef` to rebuild folded duplicates (the `dup:<fixture id>` engine ids and the moved reviews).
- `FixtureRef` has **no FK** to the kept project. It has `source`, `kind`, `external_id`, `object_id` (a BigInteger), `duplicate_of` and `note`, with `fixture_ref_unique = (source, kind, external_id)` and `source` max_length 60. How an event records its refs is the **proposal in Phase 6**, pending approval.
- The export carries **only the refs whose objects belong to the event**, and only the two kinds scoring reads: `project` refs whose `object_id` is one of the event's projects, and `score` refs whose `object_id` is one of its scores. Refs are always selected by **`(kind, object_id)`, never by `object_id` alone**. `object_id` becomes the bundle id of that row.
- Test: a project and a user share the same numeric id, and the user's ref is not exported.
- Re-exporting an imported event carries its refs again. The round-trip test asserts this.

**`bundle_ids` path kinds.** Every path in the table declares one kind:
- `plain`: the value is one DB id, remapped.
- `composite`: `"<judge>:<project>"` review ids. Both halves are remapped.
- `external`: `dup:<fixture id>`. Kept verbatim, because it is a fixture id and not a DB id.

Tests: one for each kind (a plain id remaps; a composite remaps both halves; an external id survives byte for byte), plus the unknown-key and digit-key failures above.

**Finding 2: three-state "changed" flags.** Applies to both flags:
- `ResultSnapshot.diagnostics.scores_changed_since_last_final` (`scoring/services.py:846`).
- `VoteTallySnapshot.changed_since_previous` (`voting/services.py:619`). It becomes `BooleanField(null=True)`, altered in a voting migration of its own.

Each flag is `true | false | unknown`:
- **Both flags use null for "unknown"**: when the previous final or tally is an imported row, because its `input_hash` was computed from the source install's ids.
- The organizer results page and the diagnostics show "comparison unavailable: previous final was imported" (or "previous tally was imported").
- A skipped comparison never reads as "no changes". **Every reader branches on true / false / null explicitly.** The readers today (grep, at the Phase 1 go):
  - `organizer/templates/organizer/results.html:82` has `{% if s.diagnostics.vote_tally.changed_since_previous_tally %}`. This is a truthiness check, so null would read as "unchanged". It becomes explicit branches: true → "changed since the previous tally"; null → "comparison unavailable: previous tally was imported"; false → nothing.
  - `scoring/services.py:822` copies the tally flag into `diagnostics.vote_tally.changed_since_previous_tally`. It passes null through as null.
  - `scoring/services.py:846` computes `scores_changed_since_last_final`. It becomes null when the previous final is imported. **No page shows this flag today.** The results page gains the same explicit three-way display for it ("scores changed since the last final" / "comparison unavailable: previous final was imported").
  - `scoring/services.py:867` copies the tally flag into the `TALLY_FROZEN` audit detail. Null stays null; the trail prints "unknown".
  - `scoring/management/commands/score_event.py:70` prints the diagnostics generically. Null prints as `None`, which is acceptable for a CLI dump.
  - No CSV sheet and no JSON API endpoint reads either flag (`organizer/export.py` and `organizer/results_api.py` were checked). If one is added, it must branch explicitly too.

Imported snapshots and tallies are marked by a new **nullable** `imported_from` field (the bundle sha; NULL for local rows) on both models. It is set only at INSERT. **There is no data migration**, because the immutability triggers would refuse an UPDATE backfill; existing rows stay NULL, meaning local.

Tests:
- The flags compute true/false as today for local rows.
- A new final or tally after an imported one gives `null`, never `False`.
- A render test asserts that the text "comparison unavailable: previous final was imported" (and the tally variant) appears on the organizer results page.
- `test_final_score` and `test_scoring_services` keep passing, adjusted only where they assert on an imported previous.

## Phase 5 (C1b): import validation only (no write path)
`imports/bundle_validate.py::validate_bundle(path, *, importer) -> ValidatedBundle`. Every failure is a `BundleError(400, code, reason)`, audited as `EVENT_IMPORT_REFUSED`, with nothing written.
- The upload is at most `BUNDLE_MAX_BYTES` (100 MB), streamed to a temp file. The file must be a zip.
- At most 2000 entries.
- Entry names:
  - no `..`, no leading `/` or `\`, no drive letter or NUL;
  - NFC, and at most 200 characters;
  - not a symlink (`external_attr` mode `S_IFLNK`);
  - only `manifest.json`, `event.json` and `media/<64hex>.(jpg|png|webp)`.
- Declared sizes: per file (`event.json` ≤ 50 MB, media ≤ 5 MB) and in total (≤ 300 MB), checked **before extraction**. The compression ratio must be ≤ 100. Reads are chunked with a hard cap, and reading aborts if an entry exceeds its declared size.
- The format and version must be known. The manifest `files` must equal the other entries exactly, and every sha256 must match.
- `event.json` is checked against a schema (`imports/bundle_schema.py`: types, enums, references resolve, id-path table valid).
- Live-vote refusal (decision 3).
- Every image goes through `clean_image`.

**Tests**, each refused with 400, audited, and with **every table's row count unchanged**:
- a tampered sha;
- version 2 and an unknown format;
- `../x`, an absolute path and a symlink;
- an oversized entry, an oversized total and a high-ratio bomb;
- an unlisted extra file;
- a bad image;
- schema errors;
- an open vote with ballots. An open vote with zero ballots, or a vote not yet open, passes.
- Permissions: a judge and a participant get 403.

## Phase 6 (C1c): import write path and round trip
`import_event(path, *, actor, origin) -> Event` validates first, then writes in one `atomic()`:
- Enter `deadline_bypass`, `voting_bypass` and `weights_bypass`, with the reason "event import <sha>".
  - `deadline_bypass` gains an `actor=/origin=` keyword form that always audits, like the other two. The old callers keep working.
- The slug gets `-2`, `-3`, … on a clash, under an advisory lock.
- Users:
  - An admin gets them matched by email, with missing users created with an unusable password.
  - Any other importer gets placeholders.
- The importer becomes an organizer. For an admin who is a competitor in the bundle, the import is refused with 400 `importer_is_competitor`.
- **Fresh secrets are generated (fix 8):** `ballot_secret` (`token_hex(32)`), `open_link_nonce`, and every `Team.invite_token`.
- Rows go in FK order. Snapshots, tallies and publications are INSERTed with remapped ids, never updated. Ballots become cookie-voter ballots with `voter_cookie = pseudonym`. Audit rows go in as source history.
- Finally one `EVENT_IMPORTED` row.
- DB errors map to 400 `import_conflict` (with the constraint name). The transaction rolls back, and the refusal is audited after it.

Surfaces: the upload page (admin portal and organizer portal for `can_create_events`), `POST /api/events/import` (multipart), and `manage.py import_event <file> --as <email>`.

README: imported accounts cannot log in until the operator runs `changepassword`, because there is no outbound mail.

**Round-trip test** (`transaction=True`):
1. Export.
2. `flush`.
3. **`setval` every sequence to 10000 (fix 3).**
4. Import as an admin.
5. Assert that at least one imported PK differs from its source PK.
6. Export again.
7. Compare after normalising:
   - ids, and import-time timestamps;
   - the slug suffix;
   - rows the import or export itself created;
   - media bytes and names (re-encoded, so compare dimensions, format and references);
   - **pseudonyms, mapped consistently in first-seen order (fix 5).**

Then the imported results page must show the same projects, order, ranks and tie groups.

**Other tests:**
- A same-install import gets a suffixed slug.
- A non-admin import creates placeholders and attaches no existing account.

### Phase 6 amendments (Finding 1, `FixtureRef`)
**The per-import source is unique per import:** `bundle:<sha>:<new-slug>`, so importing the same bundle twice on one install cannot clash on `fixture_ref_unique`. `source` is max_length 60. `Event.slug` is `SlugField(max_length=60)` (`events/models.py`), so the string is at most 7 (`bundle:`) + 64 (sha256 hex) + 1 + 60 = **132 characters**. The Phase 6 migration widens `FixtureRef.source` to 160 characters. That is an ALTER only, with no row updates.

**How an event records its refs: `FixtureRef.event` (APPROVED at the Phase 1 go, with amendments).** `FixtureRef` has no FK. The Phase 4 export does not depend on this: it selects refs by `(kind, object_id)`.

**Every FixtureRef reader and the kinds it reads** (grep, at the Phase 1 go):

| Reader | Kind | Lookup today |
|---|---|---|
| `scoring/services.py:672` (build_input) | `project` | source + kind |
| `scoring/services.py:686` (build_input) | `score` | source + kind + `object_id__in` |
| `scoring/services.py:723` (display_labels) | `project` | source + kind + `object_id__in` |
| `organizer/views.py:93` | `project` | kind + `object_id__in` the event's projects |
| `judge/api.py:45` | `judge` | kind + external_id (install-wide) |
| `accounts/management/commands/seed_demo.py:340` | `event` | kind + external_id (install-wide) |
| `imports/fixtures.py` | all | source + kind + external_id |

**Event-scoped kinds** are `event`, `track`, `team`, `project`, `score` and `judge` (a judge ref points at an `EventMembership`, `imports/fixtures.py:237`). The only **install-wide kind** is `user`.
- `build_input` and `display_labels` switch to `event=event` for `project` and `score` and ignore `source`. This fixes today's hidden assumption that only one fixture event exists.
- The install-wide lookups keep their source-based (fixture-source) lookup: `judge/api.py:45` for `judge` by external id, and `seed_demo.py:340` for `event`. The bundle exports only `project` and `score` refs, so a bundle never adds a second `jdg_02` or `evt_01`.

**Migrations.** The `event` FK uses `on_delete=CASCADE`: a ref describes one row of that event and means nothing once the event is gone. The work takes three migrations, because Postgres refuses an ALTER on a table in the same transaction that updated its rows:
  1. `AddField` for the nullable `event`, plus the `source` widening.
  2. A `RunPython` backfill from each row's object: event → itself, track → `track.event`, team → `team.event`, project → `project.event`, score → `score.project.event`, judge → `membership.event`, user → NULL. The **reverse is a no-op** (`RunPython.noop`).
  3. `AddConstraint`: a CHECK that event-scoped kinds require `event IS NOT NULL` (`kind = 'user' OR event_id IS NOT NULL`).

The fixture importer and the bundle import both set `event`.
- Tests:
  - the backfill, run against a database that has rows (per CLAUDE.md), then the CHECK constraint refuses an event-scoped ref with no event;
  - `build_input` on an imported copy of the fixture event gives the same engine input as the source, up to the id remap (the same `dup:` ids and the same moved reviews);
  - an import of the same bundle twice on one install succeeds with two distinct sources.


## Phase 7 (C2a): keys, SigningKey, rotation, canonical, IssuedRecord
- Add `cryptography==<current release>` to requirements, pinned.
- **Key files.** `records/keys.py` writes `/app/secrets/signing/<kid>.pem`: PKCS8 without encryption, `O_EXCL`, mode 0600, directory 0700.
  - `kid = sha256(raw_pub).hexdigest()[:16]`.
  - The private key never leaves the volume, the repo or the image.
- **`records.SigningKey`**: `(kid pk, alg "Ed25519", public_key b64, created_at, retired_at)`, holding only **this install's** keys. A partial unique index allows at most one row with `retired_at IS NULL`.
- **`records.ForeignSigningKey`**: `(kid, public_key, source_event, imported_at)`, holding keys that came with a bundle. They are **never** listed with the install's own keys (fix 1).
- **How boot picks the active PEM (fix 12).** `manage.py ensure_signing_key` runs in the entrypoint after `migrate`:
  - The active key is the single `SigningKey` row with `retired_at IS NULL`, and its PEM is `signing/<kid>.pem`.
  - If there is no active row, it generates a new PEM and inserts the row. Stray PEMs with no row are never adopted.
  - If an active row exists but its PEM is missing or does not match the public key, boot continues but prints a loud error. Issuing then refuses with 503 `signing_unavailable` until the operator runs `rotate_signing_key`, and the old records still verify.
- **Rotation (fix 12).** `manage.py rotate_signing_key`:
  1. Write the new PEM.
  2. In **one** `atomic()`: `select_for_update` the active row, set `retired_at` **first**, then insert the new row.
  3. On failure, unlink the new PEM.

  Audited as `SIGNING_KEY_ROTATED`.
- **`records/canonical.py`**: sorted keys, no whitespace, UTF-8, only int, str, list and dict. It raises on float, bool or None, and callers turn missing dates into `""` (fix 11).
- **`IssuedRecord`**:
  - Fields: `id` uuid, `kind`, `slot` (a string), `event` PROTECT, `subject_user` PROTECT, `payload` JSON, `payload_text` (the exact signed bytes), `signature` b64, `kid`, `foreign` bool, `issued_by`, `issued_at`, `revoked_at`, `revoked_by`, `revoke_reason`.
  - **The uniqueness discriminator is `slot`** (fix 7): `""` for judge and participant records; `"track:<track id or 'overall'>:place:<n>"` or `"peoples_choice"` for winners. A partial unique `(event, subject_user, kind, slot) WHERE revoked_at IS NULL`.
- **Trigger** (RunPython, Postgres only):
  - An UPDATE may change only the three revoke fields, and only from unset to set.
  - **Every DELETE is refused (fix 11).**
- A core migration adds the C2 audit actions.
- **Tests:** canonical form (floats, bools and None are refused, key order, no whitespace); `kid` derivation; rotation keeps exactly one active key; retire-first on a failed insert; the PEM file mode; boot decisions (no row, missing PEM); the trigger refuses UPDATE and DELETE.

## Phase 8 (C2b): services, pages, endpoints, portal lists
**`records/services.py`** (every action audited, refusals included):
- **`issue_records(event, kind, *, actor, origin)`:**
  - Organizer or admin only, otherwise 404.
  - Judge records only when `judging_closed`, otherwise 409 `judging_open`, and only for judges with at least one submitted review.
  - Winner records only with an active publication of a final, otherwise 409 `no_published_final`. Winners come from `scoring/results.py`, plus People's Choice.
  - The event row is locked while issuing.
  - **Idempotency compares a semantic key** (fix 11): `(subject, kind, slot)` plus the semantic fields (display name, counts, team, project, place, track). An identical active record is skipped. If the fields differ, the old record is revoked with reason "reissued" and a new one is signed.
  - **Re-issuing winners revokes active winner records whose subject/slot is no longer a winner.**
- **`revoke_record(id, reason, …)`:** a reason is required.

**Payload**
- Always: `v:1`, `kind`, `record_id`, `issued_at`, `kid`, `event{slug,name,starts_at,ends_at}`, `subject{name}`.
- Judge: `{reviews_submitted, judging{starts_at,ends_at}}`. Never a score.
- Participant: `{team, project}`.
- Winner: `{place, track, peoples_choice:"yes"|"no"}`.

**Pages**
- `/records/<uuid>`: a printable certificate (`@media print` in crt.css, no JS).
  - It shows the server-checked status: valid, invalid, revoked or unknown key.
  - **For an imported record it shows "signed by another install (kid X)", not "valid" (fix 1).**
  - It shows the raw payload, the signature and the kid.
- `/verify`: a form. A POST with a payload and signature answers valid, invalid, unknown key, revoked, or signed by another install. It has a per-IP rate limit (audited, 429) and does no other write.
- `/.well-known/dogfood-signing-keys.json`: this install's keys only, retired ones included.
- `/.well-known/dogfood-foreign-signing-keys.json`: imported keys, published separately.
- `/.well-known/dogfood-revoked.json`.
- The organizer's Records page: issue per kind, list, revoke.
- "My records" in the judge and participant portals, with the page link and a JSON download.

**Tests**
- A valid record verifies.
- Any payload change or byte flip fails.
- A revoked record shows as revoked on the page, in the JSON and at `/verify`.
- After a rotation, old records still verify.
- A judge payload has no score or criterion keys, checked recursively.
- Issue and revoke permissions: a judge, a participant and another event's organizer each get 404.
- Timing refusals.
- Reissue: the same fields are skipped and changed fields revoke and reissue. A dropped winner is revoked.
- An imported-key record is labelled "another install".

## Phase 9 (C2c): offline verifier and bundle additions
- **`scripts/verify_record.py`:** a vendored pure-Python Ed25519 verifier based on RFC 8032 §6, standard library only. Usage: `verify_record.py record.json keys.json`. It exits 0 when valid and 1 with a reason otherwise.
- **Tests:**
  - RFC 8032 §7.1 test vectors 1-3 (and SHA(abc)).
  - A subprocess run on an app-issued record exits 0; on a tampered one it exits 1.
- **Bundle:** add `issued_records` and `signing_keys` (public halves of this install's keys, plus any foreign keys the records need) to the export.
  - On import, **only admins** import them: the keys go to `ForeignSigningKey` and the records are inserted with `foreign=True`. For a non-admin they are skipped and counted.
  - A record id that clashes is skipped and counted.
  - Test: no PEM or private bytes appear in the zip.
- **Docs:** what a signature proves, and what it does not (revocation). "Back up the secrets volume: losing it means this install can't sign again under the old key."

## Phase 10 (C3): embeddable gallery widget
**Route `/embed/events/<slug>/gallery`**
- It shows the anonymous `visible_projects()` for that event. An unknown or unpublished event gives 404. It never reads `request.user`.
- Parameters (a bad value falls back, never a 500): `track`, `tag`, `sort` (`newest|oldest|name`), `limit` (1-24, default 12) and `theme` (`light|dark`).

**Template**
- A standalone template with no nav, forms or `csrf_token`.
- crt.css gets a `.embed` block and a `[data-theme=light]` token override.
- Links use `target="_blank" rel="noopener"` and absolute `PORTAL_BASE_URL` URLs.
- It shows no vote counts or results.

**Framing and cookies**
- The view sets its own CSP with `frame-ancestors *` (the middleware uses `setdefault`) and is marked `@xframe_options_exempt`.
- A new `core.middleware.EmbedMiddleware`, **outermost** in the middleware list, clears `response.cookies` on `/embed/` paths. `SessionActivityMiddleware` skips `/embed/`.

**Auto-height**
- `static/js/embed-frame.js`, on the embed page, posts `{type, height}` to the parent.
- `static/js/embed.js`, on the host page, finds `iframe[data-dogfood-embed]`. It accepts a message only if `event.origin` equals the iframe src's origin and `event.source` is that iframe's window. The height is capped. Without the script, the iframe keeps its fixed height.

**Organizer snippet:** built in the view from `PORTAL_BASE_URL` and shown in a `<pre>`.

**Tests**
- The embed route sends `frame-ancestors *`, no XFO and no `Set-Cookie`, even when the request carries session and messages cookies.
- `/`, `/projects`, `/login`, `/events/<slug>` and an organizer page still deny framing.
- An unpublished event gives 404.
- No draft ever appears.
- No vote or result text appears.
- Bad parameters give 200, and `limit=999` is capped at 24.
- The `test_platform` offline and inline checks still pass.

**Manual check (fix 13):** a host page in the scratchpad, served with `python -m http.server 9000`, that iframes the embed. Check the auto-height and that no cookies are set.

## Phase 11 (D): final pass
- **Docs:** README (What works, What it does not do yet, Layout), ARCHITECTURE.md, DATA-MODEL.md, JUDGING.md, CLAUDE.md (bundle id paths, signing keys, foreign keys, the embed exemption) and the PLAN.md phase log.
- **Check that `[VIDEO_URL]` has been replaced.** If it has not, stop and ask for the link.
- **Fresh boot:** run `docker compose down -v && docker compose up --build -d`, then the suite, `acceptance.sh`, `offline-check.sh` and `t3-check.sh`. Commit the reports.
- **Push:** `git pull --rebase origin main`, then push.
- **Clean clone:** clone from GitHub into a new folder and repeat the build and the three checks there. `cmp` the reports and say plainly whether they are byte-identical, and if not, what differs.
- C4 is not started.

## Reused code
- `core.audit.record`, `Origin`, `origin_of`
- `core.ratelimit.exceeded`
- `core.deadlines.db_now`, `deadline_bypass`
- `voting.services.voting_bypass`
- `scoring.services.weights_bypass`, `judging_closed`
- `scoring/results.py` (winners, ties)
- `projects.gallery.visible_projects`, `Filters`
- `projects.images.clean_image`
- `core.markdown.render`
- `core.api.error`, `read_json`
- `events.services.get_managed_event`
- `accounts.roles.is_organizer_of`
- `organizer.export.consistent_read`
- `imports/fixtures.py` (the free-slug pattern)
- `config/secret_key.py` (the `O_EXCL` 0600 pattern)
