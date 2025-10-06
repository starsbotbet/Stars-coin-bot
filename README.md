# Telegram Coin Bot (Railway)

- Aiogram v3, polling
- Telegram Stars (currency=XTR, provider_token empty)
- Probabilities: heads 49.5%, tails 49.5%, edge 1%
- Payouts: 1.75x for side, 8x for edge
- Persistent SQLite at /data/bank.sqlite (Railway Volume)

## Env vars
- BOT_TOKEN: your bot token from BotFather
- (optional) DB_PATH: defaults to /data/bank.sqlite

## Run
python bot.py
