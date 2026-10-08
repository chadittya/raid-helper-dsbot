# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt   # discord.py>=2.4.0, python-dotenv>=1.0.0
python bot.py                     # runs the bot; requires DISCORD_BOT_TOKEN in .env
```

There is no test suite, linter, formatter, or build step. Verification is manual: run the bot against a real Discord server and exercise the slash commands in a thread. `bot.py` raises `SystemExit` at startup if `DISCORD_BOT_TOKEN` is unset.

Windows/local dev data lives in `data/raids/` (gitignored). Deleting that folder wipes all raid history.

## Architecture

Everything lives in one ~2300-line `bot.py`. There is no package layout — read the file top-to-bottom; it is organized as a flat sequence of (1) storage helpers, (2) formatting/payout helpers, (3) bot setup, (4) one section per command with its UI classes immediately above it.

### State: one JSON file per thread

`data/raids/<thread_id>.json`. Helpers: `raid_path`, `load_raid`, `save_raid`, `delete_raid`. There is no in-memory cache and no locking — every command re-reads the file, mutates, and writes the whole dict back. Raid status is a state machine:

`in_progress` → (last unit sold) → `completed` → (all players confirmed) → `closed`

`in_progress` is the only state where `/drop`, `/gold`, `/dropedit`, and `/playeredit` are allowed.

### The drop/unit model (key invariant)

This is the part that requires reading several functions together. `raid["drops"]` is a list of _stacks_ ("groups"), each:

```python
{"id": uuid, "match_key": str, "item_name": str, "stamper_id": str|None,
 "units": [{"stamp_qty": int, "sold": bool}, ...]}
```

- `match_key = f"{item_name.strip().lower()}|{stamper_id or 'none'}"` via `compute_match_key` — the single shared definition of "same stack".
- `/drop` merges a new unit into an existing group **only if that group still has unsold units** (`group_remaining(g) > 0`); a fully sold group is a closed transaction, so a re-drop starts a fresh group with the same `match_key`. This same "active-only" conflict rule is mirrored in `has_active_match_key_conflict` (dropedit) and `find_player_swap_conflict` (playeredit) — when changing item name or stamper, all three must agree or edits will allow silent merges.
- Sales are recorded separately in `raid["sales"]` (`group_id`, `item_name`, `stamper_id`, `qty`, `price`, `ts`), and `/sell` flips `sold = True` on the first N unsold units. Per-unit `stamp_qty` exists so a partial sale carries exactly the stamps of the units sold.
- Derived helpers (`group_remaining`, `group_total_units`, `group_total_stamp_qty`, `group_status`, `group_sold_total_price`, `drop_is_editable`) are all computed on the fly — never stored.

Payout math is in `compute_payout` / `calculate_and_format`:

```
Net Pool   = gold + total_sold − (total_stamps × stampprice)
Base Share = Net Pool / len(players)
share(p)   = Base Share + (p's stamps × stampprice)
```

The two display paths — `calculate_and_format` (thread message) and `build_salary_dm_text` (per-player DM) — must be kept in sync when the formula or wording changes.

### Command flow conventions

Every command follows the same shape and should keep doing so:

1. `if not isinstance(interaction.channel, discord.Thread)` — every command except `/help` is thread-only.
2. `load_raid(thread.id)` and reject with a specific error if missing / wrong status.
3. **Acknowledge immediately** (`defer(ephemeral=True)`) before any work, wrapped in `try/except discord.HTTPException`. Discord interactions expire in ~3s.
4. Post the _real_ result as a normal thread message via `ack_and_announce`, which then best-effort sends "✅ Done." ephemerally. Errors are still delivered through `interaction.followup.send(..., ephemeral=True)`.

Permission gates are `is_creator = interaction.user.id == raid["created_by"]` OR `interaction.user.guild_permissions.administrator`, applied on `/dropedit`, `/playeredit`, `/raid forceconfirm`, `/raid cancel`. Copy this exact check rather than inventing a new one.

Multi-step flows (`/dropedit`, `/playeredit`, `/sell`) are chains of `discord.ui.View` subclasses defined right above their command. Two rules in these chains:

- Each view overrides `interaction_check` to lock the flow to `requester_id`.
- Select menus cap at 25 options (Discord's limit); the code truncates and appends a "only the first 25 are shown" note.
- Before applying a mutation at the final Confirm, the flow **re-fetches the raid and re-validates everything** (drop still exists, still unsold, no match-key conflict, raid still `in_progress`) — see `ConfirmEditView.confirm_btn` and `PlayerEditConfirmView.confirm_btn`. Keep this guard when adding fields.
- Ephemeral interaction responses are not real messages: to rewrite them you need `edit_original_response()` on the _same_ interaction that produced them (`safe_edit_origin`) — `Message.edit()` will 404.

### Player detection (`/raid new`)

`get_thread_intro_candidates` reads the thread's **starter message** first (covers right-click → Create Thread, which Discord treats differently) then the first 10 non-system messages, and uses the first candidate containing real `@mentions` (bots and duplicates filtered). Plain-text names are not detected. This is why the bot needs `Read Message History` and why it needs no privileged Message Content intent.

### Post-completion flow

When the last unit sells (`all_fully_sold`), `/sell` itself sets `completed_at_ts`, posts the payout, sets status `completed`, resets `confirmed`, then DMs every player their share with a thread link button (`send_salary_dms`); unreachable players are listed in the thread. `/confirm` appends to `raid["confirmed"]` and `finalize_if_all_confirmed` archives the thread (unlocked, so replies unarchive it) once `len(confirmed) >= len(player_ids)`. `/raid forceconfirm` is the same path with an override message.

## Gotchas

- **Legacy JSON is unreadable.** Files under `data/raids/` written by older versions use a different schema (`items` / `stamps` / `created_at`) than the current code (`drops` / `sales` / `created_at_ts`). Don't pattern-match new code on those files; raids from an old version must be cancelled and restarted.
- `README.md` is the user-facing command reference and is kept meticulously in sync with behavior — when a user-visible message or rule changes, update it too. `revamp.md` is the original feature spec for the current drop/sell/gold/stock flow and is historical, not authoritative.
- `.claude/settings.json` denies reading `.env*` and editing `data/**`; don't try to work around that.
- Discord data is untrusted input (names from users, `item_name` free text): item names are trimmed/lowercased for matching but echoed back into messages unescaped, relying on Discord's own formatting.

## Deployment (Claude does NOT deploy)

- Runs on my cloud server. I push from local, then run deploy.sh ON THE SERVER only (it is not in this repo).
- Never run ssh, scp, or git push. Never create or edit deploy scripts.
- Local data/ is test data only; server data/raids/ is live and must stay compatible.
- If a change needs a new dependency, env var, or data migration, say so explicitly so I can do it on the server.

## Rules for changes

- Plan first: for any feature or fix, list affected commands and JSON fields, and the risks, before editing.
- Changing the JSON structure breaks existing raids. Propose a migration or a version field first.
- Do not change the payout formula without asking.
- No new dependencies without asking.
- Update README.md when user-visible behavior changes.
- End every change with a manual test plan: exact slash commands in order, with expected bot replies.

## Known limitations

- No locking and no cache: every command reads and rewrites the whole JSON file, so two commands at the same moment can overwrite each other.
- All logic is in one large bot.py; no tests (manual testing in an isolated Discord test channel).
