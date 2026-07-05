# 🚀 ИНСТРУКЦИЯ РАЗВЕРТЫВАНИЯ БОТА НА RAILWAY

## ✅ ШАГ 1: Подготовка (5 минут)

### 1.1 Создать GitHub репозиторий

1. Зайди на https://github.com/new
2. Назови репозиторий: `obnylennyy-crypto-bot`
3. Создай (Create repository)
4. Скопируй команды для загрузки

### 1.2 Загрузить код на GitHub

```bash
# В папке с ботом:
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/[твой-аккаунт]/obnylennyy-crypto-bot.git
git push -u origin main
```

**Файлы в репозитории должны быть:**
- `crypto_bot.py` — основной код
- `requirements.txt` — зависимости
- `Procfile` — инструкция для Railway
- `.env` — переменные окружения (НЕ коммитить на GitHub!)

---

## ✅ ШАГ 2: Развертывание на Railway (3 минуты)

### 2.1 Регистрация на Railway

1. Зайди на https://railway.app
2. Нажми "Sign Up"
3. Выбери "GitHub" для регистрации
4. Авторизуй Railway для доступа к GitHub

### 2.2 Создание проекта

1. На Railway нажми "New Project"
2. Выбери "Deploy from GitHub"
3. Выбери репозиторий `obnylennyy-crypto-bot`
4. Railway автоматически создаст деплой

### 2.3 Добавление переменных окружения

1. В Railway перейди в Project Settings
2. Нажми на "Environment"
3. Добавь переменную:
   ```
   TELEGRAM_TOKEN=8911297572:AAGrYoJ4LsNifECKyDpQVvJ2nPqoXJtSFfQ
   ```
4. Нажми "Deploy" или бот автоматически перезагрузится

---

## ✅ ШАГ 3: Проверка и тестирование (2 минуты)

### 3.1 Проверить статус

1. На Railway открой Project
2. Нажми на сервис (обычно "web")
3. Смотри логи — должно быть:
   ```
   🚀 Бот запущен!
   ```

### 3.2 Протестировать бота

Откройй Telegram и найди своего бота (@NEWCrypteleide_bot)

Отправь команды:
```
/start
/analyze bitcoin
/price ethereum
/compare bitcoin ethereum
/top
```

**Если работает** ✅ — бот в боевом режиме!

---

## 📝 ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ

| Переменная | Значение | Обязательна |
|-----------|---------|-----------|
| TELEGRAM_TOKEN | Твой токен бота | ✅ Да |

---

## 🔧 ОБНОВЛЕНИЕ КОДА

Когда захочешь обновить код:

```bash
# Изменяешь файлы локально
git add .
git commit -m "Обновление: добавил новую функцию"
git push origin main
```

Railway автоматически перезапустит бота с новым кодом!

---

## ❌ ВОЗМОЖНЫЕ ПРОБЛЕМЫ

### Бот не отвечает
- Проверь логи в Railway (красные ошибки?)
- Убедись, что TELEGRAM_TOKEN правильный
- Попробуй перезагрузить (Redeploy)

### "ModuleNotFoundError"
- Проверь `requirements.txt` содержит все библиотеки
- Убедись файл назвал правильно (case-sensitive)

### Бот медленный
- Это нормально для бесплатного Railway
- Обнови на платный тариф если нужна скорость

---

## 💾 ЛОКАЛЬНОЕ ТЕСТИРОВАНИЕ

Если хочешь тестировать локально перед загрузкой:

```bash
# Установи зависимости
pip install -r requirements.txt

# Создай .env файл в корне проекта:
echo "TELEGRAM_TOKEN=твой_токен" > .env

# Запусти бота
python crypto_bot.py
```

---

## 🎉 ВСЁ ГОТОВО!

Твой бот работает 24/7 на Railway!

**Следующие шаги:**
1. Добавь бота в свой Telegram канал (@NEWCrypteleide_bot)
2. Тестируй команды
3. Добавь функции по мере развития
4. Монетизируй! 💰

Вопросы? Напиши в логи Railway или в Telegram группу ОБНУЛЕННЫЙ! 🚀
