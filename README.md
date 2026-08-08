# Raid Loot Share Bot

A Discord bot that tracks raid loot, lets you mark items as sold, and
automatically calculates each player's share (with stamp bonuses) using
this formula:

```
Net Pool   = Gold + Total Sold Price − (Total Stamps × Stamp Price)
Base Share = Net Pool / Number of Players
Stamper's Share = Base Share + (Their Stamps × Stamp Price)
```

---

## 1. Create the Discord Application & Bot

1. Go to https://discord.com/developers/applications → **New Application**.
2. Give it a name (e.g. "Loot Share Bot") → Create.
3. In the left sidebar, click **Bot** → **Add Bot** (or it may already exist).
4. Click **Reset Token** / **Copy** to get your bot token. **Keep this secret** —
   never share it or commit it to GitHub.
5. No privileged intents are required for this bot (it does not read
   message content or need the members intent) — you can leave those toggles off.

## 2. Invite the Bot to Your Server

1. In the Developer Portal, go to **OAuth2 → URL Generator**.
2. Under **Scopes**, check:
   - `bot`
   - `applications.commands`
3. Under **Bot Permissions**, check:
   - `Send Messages`
   - `Create Public Threads`
   - `Send Messages in Threads`
   - `Read Message History`
   - `Manage Threads` (needed to auto-archive/close a raid thread once everyone confirms)
   - `Use Slash Commands` (implied by `applications.commands`)
4. Copy the generated URL at the bottom, open it in your browser, and
   invite the bot to your server.

## 3. Install & Run

```bash
pip install -r requirements.txt
```

Create a file named `.env` in the same folder as `bot.py` (copy
`.env.example` and rename it, or create it fresh) containing:

```
DISCORD_BOT_TOKEN=your-token-here
```

Then just run:

```bash
python bot.py
```

The bot automatically loads `DISCORD_BOT_TOKEN` from `.env` on startup —
no need to set it manually as an environment variable each time. If you'd
rather set it as a real environment variable instead (e.g. for a hosting
platform that injects env vars directly), that still works too — the `.env`
file is optional, just convenient for local use.

**Keep `.env` private** — never commit it to GitHub or share it. If you use
git, add `.env` to your `.gitignore`.

### Optional: pin raid threads to one specific channel

By default, `/raid start` creates its thread in whichever channel you ran
the command from. If you want people to *trigger* the command from a
general channel (e.g. `#bot-command`) but have the thread always show up
under a different channel (e.g. `#salary-loot`), set this before running
the bot:

```bash
export RAID_THREAD_CHANNEL_ID="123456789012345678"   # the target channel's ID
```

To get a channel ID: enable Developer Mode in Discord (User Settings →
Advanced → Developer Mode), then right-click the channel → **Copy Channel
ID**.

With this set, `/raid start` can be run from any channel the bot can see,
but the thread — and every `/sold`, `/raid status`, `/raid cancel` command
after it (since those run *inside* the thread) — will live under the
target channel, keeping `#bot-command` clutter-free.

**Permissions needed for this to work:** the bot needs "Create Public
Threads" and "Send Messages in Threads" specifically on the *target*
channel (e.g. `#salary-loot`), and just "Use Application Commands" /
"Send Messages" on the trigger channel (e.g. `#bot-command`).

If it connects successfully you'll see something like:
```
Logged in as LootShareBot#1234. Synced 2 command(s).
```

Slash commands can take up to an hour to appear globally the first time;
if you don't see them right away, try kicking and re-inviting the bot, or
wait a bit. Restarting the bot re-syncs commands each time it starts.

The bot stores raid data as JSON files under `data/raids/` next to
`bot.py`, so raids survive a bot restart/crash.

---

## How to Use

### 1. Start a raid — `/raid start`

This is a two-step flow (Discord's popup forms can't resolve `@mentions`
typed as plain text, so player selection uses a real user picker instead):

**Step 1 — Select players.** Run `/raid start` in a normal text channel
(not inside a thread). You'll get an ephemeral message with a dropdown —
click it and select every player who was in the raid (up to 25), the same
way you'd pick people for a Discord role.

**Step 2 — Fill in loot details.** As soon as you finish selecting
players, a popup form opens with two fields:

- **Thread title** — pre-filled with a default like
  `Raid Loot - 2026-08-04 06:10 UTC` (the time you opened the form). Leave
  it as-is to use that default, or type your own title (e.g.
  `Weekly Dungeon - Team A`). Max 100 characters (Discord's thread name
  limit) — anything longer gets cut off.
- **Gold / items / stamps / stamp price** — the loot data template, edited
  the same as before:

```
gold: 258
items: Gdn_ring x34, Gdn_ear x34
stamps: Player1:3:Gdn_ring, Player2:1:Gdn_ear
stampprice: 4
```

Field notes for the loot data:
- `gold:` — flat gold that doesn't need selling/stamping. Use `0` if none.
- `items:` — comma-separated list of items that need to be sold later.
  **Item names must use underscores instead of spaces** (e.g. `Gdn_ring`,
  not `Gdn ring`). You can add an optional note like `x34` after the name
  — it's just cosmetic and doesn't affect the math.
- `stamps:` — comma-separated `PlayerName:count:item_name` entries for who
  stamped what. `PlayerName` must match the **display name** (server
  nickname) or username of one of the players you selected in Step 1 —
  no `@` needed (one is tolerated if you type it out of habit). Leave it
  as `stamps:` (empty) or `stamps: none` if nothing needed stamping.
- `stampprice:` — gold cost per stamp at the time of the raid.

Submitting the form creates a new thread (named `Raid Loot - <date/time>`)
and posts a summary of the raid + a reminder of the `/sold` command to use.

> Note: if two selected players happen to share the exact same display
> name, the bot can't tell them apart by name in `stamps:` and will ask
> you to resolve it (e.g. temporarily use their username instead, or
> rename one of them in-server).

If anything is formatted wrong (bad item name, unknown player in a stamp
entry, etc.), the bot replies with a specific error message plus the
template example — nothing gets created until the data is valid.

### 2. Mark items as sold — `/sold`

Inside the raid thread, once the party lead sells an item, run:

```
/sold item:Gdn_ring price:1000
```

- `item` must match a name from the original `items:` list exactly
  (underscores, not spaces). If you mistype it, the bot tells you the
  remaining unsold items so you can retry.
- Once **every** item has been marked sold, the bot automatically
  calculates and posts the full payout breakdown in the thread, mentioning
  every player, e.g.:

```
⚔️ Raid Loot Share | 8 Players
━━━━━━━━━━━━━━━━━━━━━
💎 Gold  →  258G
💎 Gdn_ring (x34)  →  522G
💎 Gdn_ear (x34)  →  2,637G
━━━━━━━━━━━━━━━━━━━━━
🔖 Stamp Deduction  →  −16G  (4 stamps × 4G)
💰 Net Pool         →  3,401G
👥 Base Share       →  425.13G each
━━━━━━━━━━━━━━━━━━━━━
📋 Payout
🏅 @Player1  →  437.13G  (+12G stamp bonus)
🏅 @Player2  →  429.13G  (+4G stamp bonus)
🏅 @Player3  →  425.13G
...
━━━━━━━━━━━━━━━━━━━━━
```

### 3. Confirm you've been paid — `/confirm`

Once the payout has been calculated and posted, each player runs
`/confirm` in the thread to acknowledge they received their share:

- Only counts for players who were on the original roster — anyone else
  running it is told it doesn't count, nothing is tracked.
- Each confirmation posts a public `✅ @Player confirmed receipt. (3/8)`
  message so everyone can see progress.
- Running it twice just tells you that you've already confirmed.
- Once **everyone** on the roster has confirmed, the bot posts a closing
  message and **archives** the thread. Archived threads disappear from the
  active thread list but aren't locked — anyone can still post in them
  later, which automatically reopens/unarchives it (e.g. for a late
  correction).

### 5. Force-confirm a player — `/raid forceconfirm`

Sometimes a player receives their share but never runs `/confirm` (they
forget, go offline, etc). The **raid creator or a server admin** can run
`/raid forceconfirm player:@PlayerName` inside the thread to manually mark
that player as confirmed on their behalf. This counts the same as if the
player had run `/confirm` themselves, including triggering the automatic
close if they were the last one needed.

### 6. Check progress — `/raid status`

Run inside a raid thread anytime to see which items are sold/unsold
(private/ephemeral reply, only visible to you). Once loot is calculated,
this also shows who has and hasn't confirmed yet.

### 7. Cancel a raid — `/raid cancel`

Run inside a raid thread to delete its data (only the raid creator or a
server admin can do this). Useful if the data was entered wrong and you
want to start over with `/raid start`.

---

## Restricting where `/raid start` can be used

This is native Discord functionality, no code needed: **Server Settings →
Integrations → find this bot → Command Permissions**. From there an admin
can allow/deny specific slash commands in specific channels or for
specific roles. If `/raid start` isn't showing up in a channel you
expect, check here first, then check the bot's channel permission
overwrites for "Use Application Commands".

## Notes & Limitations

- Each thread holds exactly one raid; running `/raid start` again just
  creates a new, separate thread/raid.
- A "gold only" raid (no `items:`) calculates and posts the payout
  immediately after `/raid start`, since there's nothing left to sell.
- All numbers support decimals (e.g. `stampprice: 4.5`) if your game ever
  needs it, though gold is normally whole numbers.
- Timestamps (raid start, loot calculated, raid closed) are posted using
  Discord's dynamic timestamp format, so every player automatically sees
  them converted to their own local time zone — there's no single "server
  time" concept in Discord, so this is the accurate equivalent. Only the
  thread's *title* stays in plain UTC text, since Discord doesn't support
  live-rendered timestamps in channel/thread names, only in message text.
- If you ever need to reset everything, stop the bot and delete the
  `data/raids/` folder — this wipes all raid history.
