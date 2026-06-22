# Telegram Calendar Bot

A personal Telegram bot that manages your Google Calendar using natural language. Powered by Claude AI.

## Features

- **Add events** — "dentist tomorrow 3pm", "team meeting Friday 10am–11am"
- **Edit events** — "move dentist to 4pm", "rename work to standup"
- **Delete events** — asks for confirmation before deleting
- **Conflict detection** — warns if a time slot is taken, with option to replace
- **Multi-day events** — "trip to Bali July 1 to July 5"
- **Query by date** — "what do I have on Monday?"
- **Colour coding** — events are colour-coded by category via `COLOR_MAP`
- All times in Singapore time (SGT)

## Setup

### 1. Prerequisites

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An Anthropic API key from [console.anthropic.com](https://console.anthropic.com)
- A Google Cloud project with the Calendar API enabled

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Google Calendar credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Enable the **Google Calendar API**
3. Create an **OAuth 2.0 client ID** (Desktop app, External, Testing)
4. Add your Gmail as a test user
5. Download the credentials and save as `credentials.json` in this folder

### 4. Environment variables

Create a `.env` file:

```
TELEGRAM_TOKEN=your_telegram_bot_token
ANTHROPIC_API_KEY=your_anthropic_api_key
ALLOWED_CHAT_ID=your_telegram_chat_id
```

To find your `ALLOWED_CHAT_ID`, message [@userinfobot](https://t.me/userinfobot) on Telegram.

### 5. Run locally

```bash
python bot.py
```

On first run it will open a browser for Google OAuth. After approving, `token.json` is saved automatically.

## Deployment (Railway)

1. Push this repo to GitHub
2. Create a new project on [Railway](https://railway.app) from the GitHub repo
3. Set these environment variables in Railway:
   - `TELEGRAM_TOKEN`
   - `ANTHROPIC_API_KEY`
   - `ALLOWED_CHAT_ID`
   - `COLOR_MAP`
   - `GOOGLE_TOKEN_JSON` — paste the full contents of your local `token.json`
   - `WEBHOOK_URL` — your Railway app URL, e.g. `https://yourapp.up.railway.app`

Railway sets `PORT` automatically.

## Colour IDs

| ID | Colour |
|----|--------|
| 1  | Lavender |
| 2  | Sage (green) |
| 3  | Grape (purple) |
| 4  | Flamingo (pink) |
| 5  | Banana (yellow) |
| 6  | Tangerine (orange) |
| 7  | Peacock (teal) |
| 8  | Blueberry (dark blue) |
| 9  | Basil (dark green) |
| 10 | Tomato (red) |
| 11 | Flamingo (light pink) |
