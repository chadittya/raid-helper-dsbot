# Raid Loot Share Bot

A Discord bot that tracks raid loot in a thread, lets you record drops and
sales as they happen, and automatically calculates each player's share
(with stamp bonuses) once everything is sold, using this formula:

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
5. No privileged intents are required for this bot. It does not read
   general message content — reading `@mentions` from a message does **not**
   require the Message Content intent (mentions are delivered as separate
   metadata regardless), so you can leave all privileged intent toggles off.

## 2. Invite the Bot to Your Server

1. In the Developer Portal, go to **OAuth2 → URL Generator**.
2. Under **Scopes**, check:
   - `bot`
   - `applications.commands`
3. Under **Bot Permissions**, check:
   - `View Channels`
   - `Send Messages`
   - `Send Messages in Threads`
   - `Read Message History` (needed to read the thread's first message for player mentions)
   - `Manage Threads` (needed to auto-archive a raid thread once everyone confirms)
   - `Use Slash Commands` (implied by `applications.commands`)
4. Copy the generated URL at the bottom, open it in your browser, and
   invite the bot to your server.

Note: the bot no longer creates threads itself — you create the thread,
so `Create Public Threads` isn't required for the bot.

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

Slash commands can take up to an hour to appear globally the first time;
if you don't see them right away, try kicking and re-inviting the bot, or
wait a bit. Restarting the bot re-syncs commands each time it starts.

The bot stores raid data as JSON files under `data/raids/` next to
`bot.py`, so raids survive a bot restart/crash.

---

## How to Use

### 1. Create the thread yourself

Manually create a Discord thread for the raid (title it however you like),
and in the **very first message** of that thread, `@mention` every player
who participated:

```
@Player1 @Player2 @Player3 @Player4
```

This message is what the bot reads to figure out the player roster —
double check everyone is mentioned before moving on.

Both ways of creating the thread work:

- **Threads button → Create → type the message** (the message you type
  becomes the thread's first message).
- **Right-click an existing message → Create Thread** (that original
  message becomes the thread's "starter message" — the bot specifically
  checks for this case too, since Discord treats it differently from a
  normal first message).

If you use the right-click method, make sure the message you're creating
the thread _from_ is the one with the player mentions — that's the one
the bot will read.

### 2. Start tracking — `/raid new`

Inside that same thread, run:

```
/raid new stampprice:5
```

- `stampprice` — gold cost per stamp for this entire raid (set once here;
  it does not change per item).
- The bot reads the thread's first message to detect players. If it can't
  find any mentions, or the thread has no messages yet, it tells you
  exactly what's wrong so you can fix it and retry.
- On success, the bot posts a confirmation in the thread listing the
  detected players and stamp price, plus the commands you'll use next.

### 3. Record loot — `/drop`

Run once per item collected:

```
/drop item_name:Sword stamp_qty:3 stamper:@H
```

- `item_name` — free text, any spaces/characters are fine (no underscore
  requirement — this isn't parsed from a bigger block of text anymore).
- `stamp_qty` — how many stamps this item needed (default `0` if none).
- `stamper` — the player who paid for the stamp. Required if `stamp_qty`
  is greater than 0; must be someone on the raid's player list.
- **One item = one stamper.** If two different players stamp items with
  the same name, run `/drop` separately for each — they'll show up as
  separate stock entries.
- Dropping the **same item name by the same stamper again** merges into
  that stack: quantity goes up (`x2`, `x3`, ...) and stamp counts add
  together (e.g. two drops of 3 stamps each = 6 total stamps for that
  stack).
- Each successful `/drop` posts a confirmation with that drop's own stamp
  cost, plus the full current remaining (unsold) stock list.

### 4. Record extra gold — `/gold`

```
/gold amount:258
```

Sets the raid's flat gold amount. **Each call overwrites the previous
value** — it does not add up, so if you need to correct it, just run it
again with the right total.

### 5. Sell an item — `/sell`

```
/sell
```

This opens a dropdown (visible only to you) listing every stock line that
still has unsold units. Pick one, and a popup asks for two things:

- **Quantity sold** — pre-filled with the full remaining amount for that
  line; edit it down if you're only selling part of the stack (e.g. 1 out
  of 3 available).
- **Sold price (total for this batch)** — the total gold for the quantity
  you're selling in _this_ transaction, not a per-unit price. If the rest
  of the stack sells later at a different price, just run `/sell` again
  for the remaining units — each sale is tracked separately and all of
  them add up toward the final payout.

Selling fewer than the full remaining amount marks that line **PARTIALLY
SOLD** with the reduced remaining quantity; selling the last remaining
unit marks it **SOLD**. Each sale posts a confirmation with the updated
stock list in the thread.

**Once every unit across every dropped stack has been sold**, the bot
automatically calculates the full payout and posts it — no extra command
needed. (There's currently no manual "force calculate" — if a raid has
nothing to sell at all, just don't run `/raid new`/`/drop` for it; this
bot is meant for raids that do have sellable loot.)

### 6. Check current stock — `/stock`

```
/stock
```

Posts publicly in the thread (visible to everyone, not just you): every
drop recorded so far — item name, quantity, stamper + stamp count, and
status. Status is one of:

- **AVAILABLE** — nothing sold from this stack yet
- **PARTIALLY SOLD** — some units sold, some still remain (shows the
  remaining quantity, plus how much has sold so far and for how much)
- **SOLD** — fully sold (shown as `x0`)

Plus the current raid gold total.

### 7. Confirm you've been paid — `/confirm`

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

### 8. Force-confirm a player — `/raid forceconfirm`

Sometimes a player receives their share but never runs `/confirm`. The
**raid creator or a server admin** can run
`/raid forceconfirm player:@PlayerName` inside the thread to manually mark
that player as confirmed on their behalf. This counts the same as a real
`/confirm`, including triggering the automatic close if they were the
last one needed.

### 9. Check raid status — `/raid status`

Shows the raid's current phase (`in_progress` / `completed` / `closed`),
when it started, and — once loot is calculated — who has and hasn't
confirmed yet.

### 10. Cancel a raid — `/raid cancel`

Deletes this thread's raid data entirely (only the raid creator or a
server admin can do this). Useful for starting over after a mistake.

---

## Notes & Limitations

- Each thread holds exactly one raid. Every command (`/drop`, `/gold`,
  `/sell`, `/stock`, `/confirm`, and the `/raid ...` subcommands) must be
  run **inside** that raid's thread.
- `/drop` and `/gold` stop working once the raid's loot has been
  calculated (status `completed` or `closed`) — no changes after the
  payout is posted.
- Timestamps (loot calculated, raid closed) use Discord's dynamic
  timestamp format, so every player automatically sees them converted to
  their own local time zone.
- If you ever need to reset everything, stop the bot and delete the
  `data/raids/` folder — this wipes all raid history.
- **Upgrade note:** this version changed how drops are stored internally
  (to support partial sales). Any raid created with an older version of
  this bot won't be readable by this version — cancel it with
  `/raid cancel` and start fresh with `/raid new` after upgrading.
