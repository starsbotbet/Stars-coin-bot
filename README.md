# Stars Coin Bot (Fly.io, GitHub Actions)

## Шаги запуска
1. Создай repo и загрузи *содержимое* этой папки в корень (не zip).
2. В файле `fly.toml` замени `CHANGE_ME_APP_NAME` на уникальное имя (латиницей).
3. В GitHub: **Settings → Secrets and variables → Actions** добавь:
   - `FLY_API_TOKEN` — токен из Fly.io (Dashboard → Account).
   - `BOT_TOKEN` — токен бота из BotFather.
4. Сделай коммит в ветку `main` — деплой запустится автоматически (вкладка **Actions**).
5. В Fly.io → приожение → **Volumes** создай volume `data` (1GB).

Бот:
- Aiogram v3, ставки 100–5000 XTR, коэффициенты: Орёл/Решка 1.75×, Ребро 8×.
- Баланс и история ставок в SQLite `/data/bank.sqlite`.
