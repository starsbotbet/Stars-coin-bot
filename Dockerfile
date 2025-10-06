
# Используем стабильный Python
FROM python:3.11-slim

# Настройки среды
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем исходники
COPY . .

# Открываем порт (для Flask)
EXPOSE 8080

# Запускаем бота
CMD ["python", "bot.py"]
