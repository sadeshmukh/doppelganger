# Doppelganger

A selfbot that does a lot of things.

- summarizer
- zalgoifier
- bangbangifier
- more text transformers
- the worst way to CDN images
- latex rendering
- persistent autoresponses
- public reminders
- revealing those private channels
  - show those private channels
- resubscriber! and associated website

## Setup

Clone the repo and run `uv sync` to install dependencies. Then, create a `.env` with the following variables. If you don't have one, it'll work fine, but it might error on certain codepaths.

```
SLACK_XOXP=xoxp-
SLACK_BOT_TOKEN=xoxb-
SLACK_APP_TOKEN=xapp-
OPENAI_API_BASE_URL=https://ai.hackclub.com/proxy/v1
OPENAI_API_KEY=sk-hc-v1-
NOTIF_CHANNEL_ID=C0123456789
AUTORESUB (0 or 1)
WEB_PORT=
RESUB_SITE_URL=
REMIND_BOT_TOKEN=xoxb-
```

You can run the bot with Docker Compose (`docker compose up -d`) or with `uv run main.py`.
