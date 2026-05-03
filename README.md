# Rem — Discord AI Bot

Rem is an AI-powered Discord bot that knows everything about your server in real time — members, roles, channels, online status, recent activity, and server info. It answers questions via chat or slash commands and has built-in content moderation.

---

## Requirements

- Python 3.10+
- [MongoDB Atlas](https://www.mongodb.com/atlas) (free tier works)
- Discord bot token — [Discord Developer Portal](https://discord.com/developers/applications)
- Groq API key — [console.groq.com](https://console.groq.com)

---

## Step 1 — Install dependencies

```bash
pip install -r requirements.txt
```

---

## Step 2 — Fill in your config

Open `bot.py` and fill in the 4 values at the top:

```python
DISCORD_TOKEN = "your-discord-bot-token"
MONGO_URI     = "your-mongodb-connection-string"
DATABASE_NAME = "auramc"
GROQ_API_KEY  = "your-groq-api-key"
```

> ⚠️ Never share or commit these values publicly. If deploying to Railway, use environment variables instead (see Railway section below).

---

## Step 3 — Enable Discord bot intents

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Select your bot → click **Bot** on the left
3. Scroll to **Privileged Gateway Intents** and enable all three:
   - ✅ Presence Intent
   - ✅ Server Members Intent
   - ✅ Message Content Intent
4. Save changes

---

## Step 4 — Run the bot

```bash
python bot.py
```

On first startup the bot will automatically:
- Sync all members, roles, and channels to MongoDB
- Fetch the last 25 messages from every channel
- Seed default server info
- Start the 10-minute auto-sync background task

---

## Slash Commands

| Command        | Description                                      | Who can use   |
|----------------|--------------------------------------------------|---------------|
| `/rem`         | Ask Rem anything about the server                | Everyone      |
| `/serverinfo`  | Show live server stats (members, IP, version...) | Everyone      |
| `/roles`       | List all roles and who has them                  | Everyone      |
| `/members`     | Show recently joined members and their roles     | Everyone      |
| `/activity`    | Show the 10 most recent messages across channels | Everyone      |
| `/editinfo`    | Update a server info field manually              | Admins only   |

---

## Chat Commands

You can also talk to Rem directly in any channel:

| Method             | Example                          |
|--------------------|----------------------------------|
| `!rem <question>`  | `!rem who is online right now?`  |
| `@Rem <question>`  | `@Rem what's the server IP?`     |

---

## Updating Server Info with /editinfo

Admins can update any field at any time:

```
/editinfo key:server_ip value:play.yourserver.com
/editinfo key:version value:1.21.4
/editinfo key:store value:store.yourserver.com
/editinfo key:discord_invite value:https://discord.gg/yourcode
```

---

## How Auto-Sync Works

Rem keeps its data fresh automatically:

- **Real-time** — when a member joins/leaves, roles change, channels are created/deleted, or a message is sent, Rem updates MongoDB immediately
- **Every 10 minutes** — a full sync runs in the background to catch anything missed
- **On startup** — full sync + message history fetch runs for every server the bot is in
- **Presence** — who is online/idle/DND is tracked live in memory and updates instantly

---

## Content Moderation

Every message goes through a 4-stage filter before reaching the AI:

| Stage | What it does | API call? |
|-------|-------------|-----------|
| 1 | Instantly blocks known slurs and threats via regex | ❌ No |
| 2 | Instantly blocks jailbreak attempts via regex | ❌ No |
| 3 | Short messages (under 200 chars) with no red flags → auto safe | ❌ No |
| 4 | Long or ambiguous messages → asks AI to judge | ✅ Yes |

This means 90%+ of normal server questions never use an API call for moderation, saving your Groq quota.

---

## Deploying 24/7 on Railway

**1. Push to GitHub**
- Create a new repo on github.com
- Upload `bot.py`, `requirements.txt`, and a `Procfile`

**2. Create a Procfile**

Create a file named exactly `Procfile` (no extension) containing:
```
worker: python bot.py
```

**3. Update bot.py config to use environment variables**

Replace the hardcoded values at the top with:
```python
import os

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
MONGO_URI     = os.environ["MONGO_URI"]
DATABASE_NAME = os.environ.get("DATABASE_NAME", "auramc")
GROQ_API_KEY  = os.environ["GROQ_API_KEY"]
```

**4. Deploy on Railway**
- Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
- Select your repo
- Go to the **Variables** tab and add:

```
DISCORD_TOKEN = your-token
MONGO_URI     = your-mongodb-uri
GROQ_API_KEY  = your-groq-key
DATABASE_NAME = auramc
```

**5. Done** — Railway will deploy automatically and keep it running 24/7.

---

## MongoDB Collections

| Collection     | What it stores                              |
|----------------|---------------------------------------------|
| `server_state` | Per-guild key-value config (IP, version...) |
| `messages`     | Last 500 messages per channel               |
| `members`      | All non-bot members and their roles         |
| `roles`        | All roles and their member lists            |
| `channels`     | All channels with type and category         |

Every document has a `guild_id` field so multiple servers are fully isolated from each other.

---

## Recommended Groq Models

| Purpose      | Model                    | Daily Limit  |
|--------------|--------------------------|--------------|
| Main AI      | `llama-3.1-8b-instant`   | 14,400 req   |
| Moderation   | `llama-3.1-8b-instant`   | shared       |

To change models, update these lines in `bot.py`:
```python
AI_MODEL         = "llama-3.1-8b-instant"
MODERATION_MODEL = "llama-3.1-8b-instant"
```
