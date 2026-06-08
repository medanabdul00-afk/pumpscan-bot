# PumpScan Bot 🤖

Scannar Solana-coins på Dexscreener och skickar Telegram-notiser vid GO/WARN coins.

## Deploy på Railway (gratis)

### Steg 1 — GitHub

1. Gå till github.com och skapa ett konto
1. Skapa ett nytt repository som heter `pumpscan-bot`
1. Ladda upp alla filer (bot.py, requirements.txt, Procfile)

### Steg 2 — Railway

1. Gå till railway.app
1. Logga in med GitHub
1. New Project → Deploy from GitHub repo → välj pumpscan-bot
1. Lägg till Environment Variables:
- TG_TOKEN = din bot token
- TG_CHAT = ditt chat ID
- SCAN_EVERY = 60

### Steg 3 — Klar!

Boten startar automatiskt och skickar ett meddelande till Telegram.

## Filter

- Min likviditet: $10K
- Min volym 1h: $2K
- Min RugCheck score: 70
- Min LP Locked: 80%
- Köp/sälj ratio: 1.3x+
- Ålder: 5 min – 24h