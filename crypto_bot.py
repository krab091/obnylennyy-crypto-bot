import os
import sys
import json
import asyncio
import requests
import logging
from pathlib import Path
from datetime import datetime, timezone
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ChatAction

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    logger.error(
        "TELEGRAM_TOKEN не задан. Установи переменную окружения TELEGRAM_TOKEN "
        "(в Railway: Variables, локально: .env или export)."
    )
    sys.exit(1)

COINGECKO_API = 'https://api.coingecko.com/api/v3'

# Ссылки сообщества (можно переопределить через переменные окружения)
# Канала пока нет — COMMUNITY_CHANNEL_URL пустой, и все упоминания канала в текстах скрываются автоматически.
# Как только канал появится — задай COMMUNITY_CHANNEL_URL в .env/Railway, и он сам появится в сообщениях.
COMMUNITY_CHANNEL_URL = os.getenv('COMMUNITY_CHANNEL_URL', '')
COMMUNITY_CHAT_URL = os.getenv('COMMUNITY_CHAT_URL', 'https://t.me/+pwqH9k1-KdFiMTMy')

# Бесплатный лимит на /analyze в сутки (упор в апгрейд до Standard/Pro)
FREE_DAILY_ANALYZE_LIMIT = 3

# Бесплатный лимит на "премиум"-аналитику в сутки: /ta, /exchanges, /screener, /news
FREE_DAILY_ADVANCED_LIMIT = 2

DATA_FILE = Path(__file__).parent / 'community_data.json'


def load_data() -> dict:
    """Загрузить данные сообщества (пользователи, рефералы, лимиты)"""
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Не удалось прочитать {DATA_FILE}: {e}")
    return {'users': {}}


def save_data(data: dict):
    """Сохранить данные сообщества на диск"""
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"Не удалось сохранить {DATA_FILE}: {e}")


def today_str() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')

class CryptoAnalyzer:
    """Класс для анализа криптопроектов"""

    @staticmethod
    def get_coin_data(coin_id: str) -> dict:
        """Получить данные о монете от CoinGecko"""
        try:
            url = f'{COINGECKO_API}/coins/{coin_id}'
            params = {
                'localization': False,
                'tickers': False,
                'market_data': True,
                'community_data': False,
                'developer_data': False,
                'sparkline': False
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при получении данных: {e}")
            return None

    @staticmethod
    def get_market_data(coin_id: str) -> dict:
        """Получить рыночные данные"""
        try:
            url = f'{COINGECKO_API}/simple/price'
            params = {
                'ids': coin_id,
                'vs_currencies': 'usd',
                'include_market_cap': 'true',
                'include_24hr_vol': 'true',
                'include_24hr_change': 'true',
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json().get(coin_id, {})
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при получении рыночных данных: {e}")
            return {}

    @staticmethod
    def search_coin(query: str) -> list:
        """Поиск монеты по названию"""
        try:
            url = f'{COINGECKO_API}/search'
            params = {'query': query}
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            results = response.json().get('coins', [])[:5]
            return results
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при поиске: {e}")
            return []

    @staticmethod
    def calculate_rsi(prices: list, period: int = 14) -> float:
        """Простой расчет RSI"""
        if len(prices) < period:
            return None

        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        seed = deltas[:period]
        up = sum([x for x in seed if x > 0]) / period
        down = -sum([x for x in seed if x < 0]) / period

        if down == 0:
            return 100 if up > 0 else 50

        rs = up / down
        rsi = 100 - (100 / (1 + rs))
        return rsi

    @staticmethod
    def format_price(price: float) -> str:
        """Форматировать цену"""
        if price >= 1:
            return f"${price:,.2f}"
        elif price >= 0.01:
            return f"${price:.4f}"
        else:
            return f"${price:.8f}"

    @staticmethod
    def get_risk_level(market_cap: float, volume: float, price_change_24h: float) -> tuple:
        """Определить уровень риска"""
        risk_score = 0
        reasons = []

        # Риск на основе market cap
        if market_cap is None or market_cap < 10_000_000:
            risk_score += 3
            reasons.append("Маленький Market Cap")
        elif market_cap < 100_000_000:
            risk_score += 2
            reasons.append("Средний Market Cap")

        # Риск на основе волатильности
        if price_change_24h and abs(price_change_24h) > 20:
            risk_score += 2
            reasons.append("Высокая волатильность")

        risk_level = min(risk_score, 10)
        return risk_level, reasons

    @staticmethod
    def format_analysis(coin_name: str, coin_id: str, market_data: dict, full_data: dict = None) -> str:
        """Форматировать анализ для постинга"""
        if not market_data:
            return f"❌ Не найдена информация о {coin_name}"

        price = market_data.get('usd', 0)
        market_cap = market_data.get('usd_market_cap')
        volume_24h = market_data.get('usd_24h_vol')
        change_24h = market_data.get('usd_24h_change', 0)

        # /simple/price не отдаёт 7d/30d — берём из /coins/{id}.market_data
        full_market_data = (full_data or {}).get('market_data', {})
        change_7d = full_market_data.get('price_change_percentage_7d') or 0
        change_30d = full_market_data.get('price_change_percentage_30d') or 0

        # Определение риска
        risk_level, risk_reasons = CryptoAnalyzer.get_risk_level(market_cap, volume_24h, change_24h)

        # Форматирование
        formatted_price = CryptoAnalyzer.format_price(price)

        # Emoji для изменений
        change_24h_emoji = "📈" if change_24h >= 0 else "📉"
        change_7d_emoji = "📈" if change_7d >= 0 else "📉"
        change_30d_emoji = "📈" if change_30d >= 0 else "📉"

        # Форматирование market cap
        if market_cap:
            if market_cap >= 1_000_000_000:
                market_cap_str = f"${market_cap/1_000_000_000:.2f}B"
            elif market_cap >= 1_000_000:
                market_cap_str = f"${market_cap/1_000_000:.2f}M"
            else:
                market_cap_str = f"${market_cap:,.0f}"
        else:
            market_cap_str = "N/A"

        # Форматирование volume
        if volume_24h:
            if volume_24h >= 1_000_000_000:
                volume_str = f"${volume_24h/1_000_000_000:.2f}B"
            elif volume_24h >= 1_000_000:
                volume_str = f"${volume_24h/1_000_000:.2f}M"
            else:
                volume_str = f"${volume_24h:,.0f}"
        else:
            volume_str = "N/A"

        # Риск статус
        if risk_level <= 3:
            risk_emoji = "🟢"
            risk_text = "Низкий"
        elif risk_level <= 6:
            risk_emoji = "🟡"
            risk_text = "Средний"
        else:
            risk_emoji = "🔴"
            risk_text = "Высокий"

        analysis = f"""
📊 **{coin_name.upper()}** ({coin_id.upper()})
━━━━━━━━━━━━━━━━━━━━━━━━━━━

💰 **Цена**: {formatted_price}

📈 **Изменения**:
   {change_24h_emoji} 24h: {change_24h:+.2f}%
   {change_7d_emoji} 7d: {change_7d:+.2f}%
   {change_30d_emoji} 30d: {change_30d:+.2f}%

💎 **Market Cap**: {market_cap_str}
📊 **Volume 24h**: {volume_str}

{risk_emoji} **Риск**: {risk_level}/10 ({risk_text})
   {', '.join(risk_reasons) if risk_reasons else 'Низкие риски'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 **Статус**: Для подробного анализа → /analyze {coin_id}
Получи полный доступ в ОБНУЛЕННЫЙ 📱
"""
        return analysis.strip()


class TechnicalAnalyzer:
    """Технический анализ на исторических ценах: SMA/EMA/MACD/Bollinger/RSI"""

    @staticmethod
    def get_price_history(coin_id: str, days: int = 100) -> list:
        """Дневные цены закрытия за период (CoinGecko market_chart)"""
        try:
            url = f'{COINGECKO_API}/coins/{coin_id}/market_chart'
            params = {'vs_currency': 'usd', 'days': days}
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            prices = response.json().get('prices', [])
            return [p[1] for p in prices]
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при получении истории цен: {e}")
            return []

    @staticmethod
    def sma(values: list, period: int):
        if len(values) < period:
            return None
        return sum(values[-period:]) / period

    @staticmethod
    def _ema_series(values: list, period: int) -> list:
        if len(values) < period:
            return []
        k = 2 / (period + 1)
        ema_values = [sum(values[:period]) / period]  # seed = SMA
        for price in values[period:]:
            ema_values.append(price * k + ema_values[-1] * (1 - k))
        return ema_values

    @staticmethod
    def ema(values: list, period: int):
        series = TechnicalAnalyzer._ema_series(values, period)
        return series[-1] if series else None

    @staticmethod
    def macd(values: list, fast: int = 12, slow: int = 26, signal: int = 9):
        if len(values) < slow + signal:
            return None
        fast_series = TechnicalAnalyzer._ema_series(values, fast)
        slow_series = TechnicalAnalyzer._ema_series(values, slow)
        offset = len(fast_series) - len(slow_series)
        if offset < 0:
            return None
        macd_line = [fast_series[offset + i] - slow_series[i] for i in range(len(slow_series))]
        if len(macd_line) < signal:
            return None
        signal_series = TechnicalAnalyzer._ema_series(macd_line, signal)
        if not signal_series:
            return None
        macd_latest = macd_line[-1]
        signal_latest = signal_series[-1]
        return {'macd': macd_latest, 'signal': signal_latest, 'histogram': macd_latest - signal_latest}

    @staticmethod
    def bollinger_bands(values: list, period: int = 20, num_std: float = 2):
        if len(values) < period:
            return None
        window = values[-period:]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        return {'upper': mean + num_std * std, 'middle': mean, 'lower': mean - num_std * std}

    @staticmethod
    def summarize(prices: list, current_price: float = None) -> dict:
        """Собрать все индикаторы + простой вердикт (не финансовый совет)"""
        if not prices:
            return {'error': 'Недостаточно данных для технического анализа'}

        current_price = current_price if current_price is not None else prices[-1]
        rsi = CryptoAnalyzer.calculate_rsi(prices, period=14)
        sma20 = TechnicalAnalyzer.sma(prices, 20)
        macd_data = TechnicalAnalyzer.macd(prices)
        bb = TechnicalAnalyzer.bollinger_bands(prices, 20)

        score = 0
        reasons = []

        if rsi is not None:
            if rsi < 30:
                score += 1
                reasons.append(f"RSI перепродан ({rsi:.0f})")
            elif rsi > 70:
                score -= 1
                reasons.append(f"RSI перекуплен ({rsi:.0f})")

        if macd_data:
            if macd_data['histogram'] > 0:
                score += 1
                reasons.append("MACD выше сигнальной линии")
            else:
                score -= 1
                reasons.append("MACD ниже сигнальной линии")

        if sma20 is not None:
            if current_price > sma20:
                score += 1
                reasons.append("Цена выше SMA20")
            else:
                score -= 1
                reasons.append("Цена ниже SMA20")

        if bb:
            if current_price <= bb['lower']:
                score += 1
                reasons.append("Цена у нижней границы Bollinger")
            elif current_price >= bb['upper']:
                score -= 1
                reasons.append("Цена у верхней границы Bollinger")

        if score >= 2:
            verdict = "🟢 Бычий уклон сигналов"
        elif score <= -2:
            verdict = "🔴 Медвежий уклон сигналов"
        else:
            verdict = "🟡 Смешанные/нейтральные сигналы"

        return {
            'rsi': rsi,
            'sma20': sma20,
            'macd': macd_data,
            'bollinger': bb,
            'score': score,
            'verdict': verdict,
            'reasons': reasons,
        }


class ExchangeAggregator:
    """Сравнение цены/объёма по нескольким биржам через ccxt"""

    EXCHANGE_IDS = ['binance', 'bybit', 'kraken', 'okx']

    @staticmethod
    def fetch_all_sync(symbol: str) -> dict:
        """Синхронный опрос бирж (запускать через run_in_executor, ccxt блокирующий)"""
        import ccxt
        results = {}
        for ex_id in ExchangeAggregator.EXCHANGE_IDS:
            try:
                exchange_class = getattr(ccxt, ex_id)
                exchange = exchange_class({'timeout': 10000, 'enableRateLimit': True})
                ticker = exchange.fetch_ticker(symbol)
                results[ex_id] = {
                    'price': ticker.get('last'),
                    'volume': ticker.get('quoteVolume') or ticker.get('baseVolume'),
                }
            except Exception as e:
                results[ex_id] = {'error': str(e)[:100]}
        return results


class NewsService:
    """Заголовки новостей по ключевому слову (CryptoCompare News API, без ключа)"""

    NEWS_URL = 'https://min-api.cryptocompare.com/data/v2/news/'

    @staticmethod
    def get_news(keyword: str, limit: int = 5) -> list:
        try:
            response = requests.get(NewsService.NEWS_URL, params={'lang': 'EN'}, timeout=10)
            response.raise_for_status()
            items = response.json().get('Data', [])
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при получении новостей: {e}")
            return []

        keyword_lower = keyword.lower()
        matched = [
            n for n in items
            if keyword_lower in n.get('title', '').lower() or keyword_lower in n.get('categories', '').lower()
        ]
        return (matched or items)[:limit]


class CryptoBot:
    """Главный класс бота"""

    def __init__(self, token: str):
        self.token = token
        self.app = Application.builder().token(token).post_init(self._post_init).build()
        self.analyzer = CryptoAnalyzer()
        self.data = load_data()
        self.bot_username = None
        self._register_handlers()
        self.app.add_error_handler(self._error_handler)

    async def _post_init(self, app: Application):
        """Регистрация списка команд в меню Telegram"""
        me = await app.bot.get_me()
        self.bot_username = me.username
        await app.bot.set_my_commands([
            BotCommand("start", "Начать работу с ботом"),
            BotCommand("analyze", "Полный анализ монеты"),
            BotCommand("price", "Быстрая цена"),
            BotCommand("compare", "Сравнение двух монет"),
            BotCommand("top", "Топ 20 монет по объёму"),
            BotCommand("ta", "Технический анализ (MACD/Bollinger/RSI/SMA)"),
            BotCommand("exchanges", "Цена монеты на нескольких биржах"),
            BotCommand("screener", "Скринер: перепроданные/перекупленные монеты"),
            BotCommand("news", "Новости по монете"),
            BotCommand("community", "О сообществе ОБНУЛЕННЫЙ и тарифах"),
            BotCommand("invite", "Пригласить друзей и получить бонус"),
            BotCommand("rules", "Правила сообщества"),
            BotCommand("help", "Справка"),
        ])

    def _get_user_record(self, user_id: int) -> dict:
        """Получить (и при необходимости создать) запись пользователя"""
        uid = str(user_id)
        if uid not in self.data['users']:
            self.data['users'][uid] = {
                'first_seen': today_str(),
                'referred_by': None,
                'invited_count': 0,
                'analyze_count': {},
            }
        return self.data['users'][uid]

    def _check_and_bump_limit(self, user_id: int, feature: str, daily_limit: int) -> tuple:
        """Проверить дневной лимит free-тира по конкретной фиче.
        Возвращает (allowed_without_upsell: bool, used_today: int)"""
        record = self._get_user_record(user_id)
        day = today_str()
        usage = record.setdefault('usage', {})
        feature_usage = usage.setdefault(feature, {})
        used = feature_usage.get(day, 0)
        feature_usage[day] = used + 1
        # Чистим старые дни, чтобы файл не разрастался
        usage[feature] = {day: feature_usage[day]}
        save_data(self.data)
        return used < daily_limit, used + 1

    def _check_and_bump_free_limit(self, user_id: int) -> tuple:
        """Обратная совместимость: лимит /analyze"""
        return self._check_and_bump_limit(user_id, 'analyze', FREE_DAILY_ANALYZE_LIMIT)

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Глобальный обработчик ошибок — логирует, не роняет бота"""
        logger.error("Ошибка при обработке апдейта %s: %s", update, context.error, exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text("❌ Произошла ошибка, попробуй ещё раз чуть позже")
            except Exception:
                pass

    def _register_handlers(self):
        """Регистрация обработчиков"""
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("analyze", self.analyze))
        self.app.add_handler(CommandHandler("price", self.price))
        self.app.add_handler(CommandHandler("compare", self.compare))
        self.app.add_handler(CommandHandler("top", self.top_coins))
        self.app.add_handler(CommandHandler("ta", self.ta))
        self.app.add_handler(CommandHandler("exchanges", self.exchanges))
        self.app.add_handler(CommandHandler("screener", self.screener))
        self.app.add_handler(CommandHandler("news", self.news))
        self.app.add_handler(CommandHandler("community", self.community))
        self.app.add_handler(CommandHandler("invite", self.invite))
        self.app.add_handler(CommandHandler("rules", self.rules))
        self.app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, self.welcome_new_member))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start (в т.ч. обработка реферальных ссылок вида /start ref_12345)"""
        user_id = update.effective_user.id
        record = self._get_user_record(user_id)

        referred_note = ""
        if context.args and context.args[0].startswith('ref_') and record.get('referred_by') is None:
            try:
                referrer_id = int(context.args[0][4:])
            except ValueError:
                referrer_id = None
            if referrer_id and referrer_id != user_id:
                record['referred_by'] = referrer_id
                referrer_record = self._get_user_record(referrer_id)
                referrer_record['invited_count'] = referrer_record.get('invited_count', 0) + 1
                referred_note = "\n🎁 Ты пришёл по приглашению — добро пожаловать в семью ОБНУЛЕННЫЙ!\n"
        save_data(self.data)

        community_line = (
            f"Используй бота чтобы быстро анализировать проекты, а в чате {COMMUNITY_CHAT_URL} "
            f"и канале {COMMUNITY_CHANNEL_URL} — общайся с сообществом 📊"
            if COMMUNITY_CHANNEL_URL else
            f"Используй бота чтобы быстро анализировать проекты, а в чате {COMMUNITY_CHAT_URL} — общайся с сообществом 📊"
        )

        welcome_message = f"""
🚀 **Добро пожаловать в ОБНУЛЕННЫЙ анализатор!**
{referred_note}
Я помогаю анализировать крипто-проекты и делать умные инвестиции.

**Доступные команды:**
/analyze [монета] - Полный анализ проекта
/price [монета] - Быстрая цена
/compare [coin1] [coin2] - Сравнение двух монет
/top - Топ 20 монет по объёму
/ta [монета] - Технический анализ (MACD/Bollinger/RSI/SMA)
/exchanges [тикер] - Цена на нескольких биржах
/screener - Перепроданные/перекупленные монеты
/news [монета] - Свежие новости
/community - О сообществе и тарифах
/invite - Пригласить друзей
/rules - Правила сообщества
/help - Справка

**Примеры:**
/analyze bitcoin
/price ethereum
/compare bitcoin ethereum

{community_line}
        """
        await update.message.reply_text(welcome_message.strip(), parse_mode='Markdown', disable_web_page_preview=True)

    async def community(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /community — о сообществе и тарифах"""
        links_block = f"💬 Чат: {COMMUNITY_CHAT_URL}"
        if COMMUNITY_CHANNEL_URL:
            links_block += f"\n📢 Канал: {COMMUNITY_CHANNEL_URL}"

        text = f"""
🧭 **ОБНУЛЕННЫЙ — сообщество трезвых крипто-инвесторов**

Без сигналов "купи на хаях". Только данные, риск-скоринг и живое обсуждение.

{links_block}

**Тарифы:**

🆓 **Free**
Чат, /price, /top, {FREE_DAILY_ANALYZE_LIMIT} разбора /analyze и {FREE_DAILY_ADVANCED_LIMIT} премиум-запроса (/ta, /exchanges, /screener, /news) в день

⭐ **Standard** (~700-900 ₽/мес)
Безлимит /analyze, /compare, /ta, /exchanges, /screener, /news, ежедневный дайджест рынка, приоритет в чате

💎 **Pro** (~2000-2500 ₽/мес)
Всё из Standard + закрытый чат с разборами портфелей, доступ к экспертам, ранний доступ к обзорам

Оплата — через Telegram Stars прямо в боте (скоро).
Хочешь пригласить друзей и получить бонус? Жми /invite 🎁
        """
        await update.message.reply_text(text.strip(), parse_mode='Markdown', disable_web_page_preview=True)

    async def invite(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /invite — персональная реферальная ссылка"""
        user_id = update.effective_user.id
        record = self._get_user_record(user_id)
        save_data(self.data)

        username = self.bot_username or (await context.bot.get_me()).username
        link = f"https://t.me/{username}?start=ref_{user_id}"
        invited = record.get('invited_count', 0)

        text = f"""
🎁 **Приглашай друзей в ОБНУЛЕННЫЙ**

Твоя персональная ссылка:
{link}

Уже приглашено: **{invited}** чел.

Каждый друг, который запустит бота по твоей ссылке, засчитывается автоматически.
        """
        await update.message.reply_text(text.strip(), parse_mode='Markdown', disable_web_page_preview=True)

    async def rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /rules — правила сообщества"""
        text = """
📜 **Правила сообщества ОБНУЛЕННЫЙ**

1️⃣ Уважение — без токсичности, оскорблений и переходов на личности
2️⃣ Никаких сигналов и "гарантированных иксов" — только аргументированное мнение
3️⃣ Реклама и рефссылки — только по согласованию с модераторами
4️⃣ Финансовые решения — твоя ответственность. Мы делимся данными, а не советами "инвестировать всё"
5️⃣ Флуд и спам — предупреждение, потом бан

Нарушения — пишите модераторам. Всем удачи и трезвых решений! 🧊
        """
        await update.message.reply_text(text.strip(), parse_mode='Markdown')

    async def welcome_new_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Приветствие новых участников в чате/группе"""
        for member in update.message.new_chat_members:
            if member.is_bot:
                continue
            name = member.first_name or member.username or "друг"
            text = f"""
👋 Привет, {name}! Добро пожаловать в чат ОБНУЛЕННЫЙ.

Здесь мы разбираем крипто-проекты трезво, без хайпа. Загляни в /rules, а личного бота-аналитика найдёшь тут: @{self.bot_username or ''}
            """
            await update.message.reply_text(text.strip(), parse_mode='Markdown')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /help"""
        help_text = """
📖 **СПРАВКА**

**Основные команды:**

1️⃣ /analyze [монета]
   Полный анализ проекта с данными о цене, рисках, изменениях
   Пример: /analyze bitcoin

2️⃣ /price [монета]
   Быстрая информация о цене и рыночных данных
   Пример: /price ethereum

3️⃣ /compare [coin1] [coin2]
   Сравнение двух монет
   Пример: /compare bitcoin ethereum

4️⃣ /top
   Топ 20 монет по объёму торгов
   Пример: /top

**Продвинутая аналитика (2 бесплатных запроса в день):**

5️⃣ /ta [монета]
   Технический анализ: RSI, SMA20, MACD, Bollinger Bands + вердикт
   Пример: /ta bitcoin

6️⃣ /exchanges [тикер] [quote]
   Цена и объём монеты на Binance/Bybit/Kraken/OKX
   Пример: /exchanges BTC USDT

7️⃣ /screener
   Топ-15 монет по объёму, отфильтрованные по RSI (перепроданность/перекупленность)

8️⃣ /news [монета]
   Свежие заголовки новостей по теме
   Пример: /news ethereum

**Сообщество:**
🧭 /community — о сообществе ОБНУЛЕННЫЙ и тарифах
🎁 /invite — персональная ссылка для приглашения друзей
📜 /rules — правила сообщества

**Поиск монет:**
Название можно писать на английском:
- bitcoin, ethereum, solana, cardano
- doge, ripple, polkadot, chainlink

**Советы:**
✅ Всегда проверяй риск перед инвестицией
✅ Смотри на объём торгов (Volume)
✅ Анализируй рыночную капитализацию (Market Cap)
✅ Не покупай на пике 📈

Вопросы? Напиши в личку! 💬
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Полный анализ монеты"""
        if not context.args:
            await update.message.reply_text(
                "❌ Укажи монету\nПример: /analyze bitcoin",
                parse_mode='Markdown'
            )
            return

        coin_name = ' '.join(context.args).lower()

        # Показываем, что обрабатываем
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        # Получаем данные
        market_data = self.analyzer.get_market_data(coin_name)
        full_data = self.analyzer.get_coin_data(coin_name)

        if not market_data or not full_data:
            # Ищем монету
            search_results = self.analyzer.search_coin(coin_name)
            if search_results:
                suggestion = search_results[0]
                suggested_id = suggestion['id']
                suggested_name = suggestion['name']

                market_data = self.analyzer.get_market_data(suggested_id)
                full_data = self.analyzer.get_coin_data(suggested_id)
                coin_name = suggested_id
            else:
                await update.message.reply_text(
                    f"❌ Монета '{coin_name}' не найдена\n/help для справки",
                    parse_mode='Markdown'
                )
                return

        analysis = self.analyzer.format_analysis(coin_name, coin_name, market_data, full_data)

        within_free_limit, used_today = self._check_and_bump_free_limit(update.effective_user.id)
        if not within_free_limit:
            analysis += (
                f"\n\n🔒 Бесплатных разборов на сегодня: {FREE_DAILY_ANALYZE_LIMIT}/{FREE_DAILY_ANALYZE_LIMIT} использовано.\n"
                f"Безлимит и приоритет — в тарифе Standard: /community"
            )

        await update.message.reply_text(analysis, parse_mode='Markdown')

    async def price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Быстрая цена"""
        if not context.args:
            await update.message.reply_text(
                "❌ Укажи монету\nПример: /price bitcoin",
                parse_mode='Markdown'
            )
            return

        coin_name = ' '.join(context.args).lower()

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        market_data = self.analyzer.get_market_data(coin_name)

        if not market_data:
            search_results = self.analyzer.search_coin(coin_name)
            if search_results:
                coin_name = search_results[0]['id']
                market_data = self.analyzer.get_market_data(coin_name)
            else:
                await update.message.reply_text(f"❌ '{coin_name}' не найдена")
                return

        price = market_data.get('usd', 0)
        change_24h = market_data.get('usd_24h_change', 0)

        emoji = "📈" if change_24h >= 0 else "📉"
        formatted_price = self.analyzer.format_price(price)

        message = f"""
💰 **{coin_name.upper()}**
Цена: {formatted_price}
{emoji} 24h: {change_24h:+.2f}%
        """
        await update.message.reply_text(message.strip(), parse_mode='Markdown')

    async def compare(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сравнение двух монет"""
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Укажи две монеты\nПример: /compare bitcoin ethereum",
                parse_mode='Markdown'
            )
            return

        coin1_name = context.args[0].lower()
        coin2_name = context.args[1].lower()

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        # Получаем данные обеих монет
        data1 = self.analyzer.get_market_data(coin1_name)
        data2 = self.analyzer.get_market_data(coin2_name)

        if not data1 or not data2:
            await update.message.reply_text("❌ Не удалось найти одну или обе монеты")
            return

        price1 = data1.get('usd', 0)
        price2 = data2.get('usd', 0)
        change1 = data1.get('usd_24h_change', 0)
        change2 = data2.get('usd_24h_change', 0)
        cap1 = data1.get('usd_market_cap')
        cap2 = data2.get('usd_market_cap')

        cap1_str = f"${cap1/1_000_000_000:.2f}B" if cap1 else 'N/A'
        cap2_str = f"${cap2/1_000_000_000:.2f}B" if cap2 else 'N/A'

        message = f"""
⚖️ **СРАВНЕНИЕ**

💰 {coin1_name.upper()}
   Цена: {self.analyzer.format_price(price1)}
   24h: {change1:+.2f}%
   Market Cap: {cap1_str}

💰 {coin2_name.upper()}
   Цена: {self.analyzer.format_price(price2)}
   24h: {change2:+.2f}%
   Market Cap: {cap2_str}

🏆 Лидер по изменениям (24h): {coin1_name if abs(change1) > abs(change2) else coin2_name}
        """
        await update.message.reply_text(message.strip(), parse_mode='Markdown')

    async def top_coins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Топ 20 монет"""
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        try:
            url = f'{COINGECKO_API}/coins/markets'
            params = {
                'vs_currency': 'usd',
                'order': 'volume_desc',
                'per_page': 20,
                'sparkline': False,
                'locale': 'en'
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            coins = response.json()

            message = "🔝 **ТОП 20 МОНЕТ ПО ОБЪЁМУ**\n━━━━━━━━━━━━━━━━━━━━━\n\n"

            for i, coin in enumerate(coins, 1):
                name = coin['name']
                symbol = coin['symbol'].upper()
                price = coin['current_price']
                volume = coin['total_volume']
                change = coin['price_change_percentage_24h'] or 0

                emoji = "📈" if change >= 0 else "📉"

                price_str = self.analyzer.format_price(price)

                if volume and volume >= 1_000_000_000:
                    vol_str = f"${volume/1_000_000_000:.2f}B"
                elif volume:
                    vol_str = f"${volume/1_000_000:.2f}M"
                else:
                    vol_str = "N/A"

                message += f"{i}. {name} ({symbol})\n   {price_str} {emoji} {change:+.2f}% | Vol: {vol_str}\n\n"

            await update.message.reply_text(message.strip(), parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка при получении топа: {str(e)}")

    def _upsell_footer(self, used_today: int, limit: int) -> str:
        return (
            f"\n\n🔒 Премиум-запросов на сегодня: {used_today}/{limit} использовано.\n"
            f"Безлимит на /ta, /exchanges, /screener, /news — в тарифе Standard: /community"
        )

    async def ta(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Технический анализ монеты: SMA/EMA/MACD/Bollinger/RSI"""
        if not context.args:
            await update.message.reply_text("❌ Укажи монету\nПример: /ta bitcoin", parse_mode='Markdown')
            return

        coin_name = ' '.join(context.args).lower()
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        prices = TechnicalAnalyzer.get_price_history(coin_name, days=100)
        if not prices:
            search_results = self.analyzer.search_coin(coin_name)
            if search_results:
                coin_name = search_results[0]['id']
                prices = TechnicalAnalyzer.get_price_history(coin_name, days=100)
            if not prices:
                await update.message.reply_text(f"❌ Не удалось получить историю цен для '{coin_name}'")
                return

        summary = TechnicalAnalyzer.summarize(prices)
        if 'error' in summary:
            await update.message.reply_text(f"❌ {summary['error']}")
            return

        rsi_str = f"{summary['rsi']:.1f}" if summary['rsi'] is not None else "н/д"
        sma_str = self.analyzer.format_price(summary['sma20']) if summary['sma20'] else "н/д"
        if summary['macd']:
            macd_str = f"{summary['macd']['macd']:.4f} / сигнал {summary['macd']['signal']:.4f}"
        else:
            macd_str = "н/д (мало данных)"
        if summary['bollinger']:
            bb = summary['bollinger']
            bb_str = f"{self.analyzer.format_price(bb['lower'])} — {self.analyzer.format_price(bb['upper'])}"
        else:
            bb_str = "н/д"

        text = f"""
📐 **ТЕХНИЧЕСКИЙ АНАЛИЗ: {coin_name.upper()}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━

RSI (14): {rsi_str}
SMA (20): {sma_str}
MACD: {macd_str}
Bollinger Bands: {bb_str}

{summary['verdict']}
Сигналы: {', '.join(summary['reasons']) if summary['reasons'] else 'недостаточно данных'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Это не финансовый совет, а сводка индикаторов на исторических данных.
"""
        within_limit, used_today = self._check_and_bump_limit(update.effective_user.id, 'advanced', FREE_DAILY_ADVANCED_LIMIT)
        if not within_limit:
            text += self._upsell_footer(used_today, FREE_DAILY_ADVANCED_LIMIT)

        await update.message.reply_text(text.strip(), parse_mode='Markdown')

    async def exchanges(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сравнение цены/объёма монеты на нескольких биржах (ccxt)"""
        if not context.args:
            await update.message.reply_text("❌ Укажи тикер\nПример: /exchanges BTC", parse_mode='Markdown')
            return

        ticker = context.args[0].upper()
        quote = context.args[1].upper() if len(context.args) > 1 else 'USDT'
        symbol = f"{ticker}/{quote}"

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, ExchangeAggregator.fetch_all_sync, symbol)

        lines = [f"🏦 **{symbol} НА БИРЖАХ**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]
        prices = {}
        for ex_id, data in results.items():
            if 'error' in data or not data.get('price'):
                lines.append(f"{ex_id.capitalize()}: н/д (пара не найдена или недоступна)")
                continue
            price = data['price']
            prices[ex_id] = price
            vol = data.get('volume')
            vol_str = f", объём: {self.analyzer.format_price(vol)}" if vol else ""
            lines.append(f"{ex_id.capitalize()}: {self.analyzer.format_price(price)}{vol_str}")

        if len(prices) >= 2:
            max_ex = max(prices, key=prices.get)
            min_ex = min(prices, key=prices.get)
            spread_pct = (prices[max_ex] - prices[min_ex]) / prices[min_ex] * 100
            lines.append("")
            lines.append(f"📊 Разброс цен: {spread_pct:.2f}% ({min_ex.capitalize()} → {max_ex.capitalize()})")
        elif not prices:
            lines.append("")
            lines.append("Не удалось получить данные ни с одной биржи — проверь тикер.")

        lines.append("")
        lines.append("⚠️ Не является рекомендацией к арбитражу или сделкам.")

        within_limit, used_today = self._check_and_bump_limit(update.effective_user.id, 'advanced', FREE_DAILY_ADVANCED_LIMIT)
        text = "\n".join(lines)
        if not within_limit:
            text += self._upsell_footer(used_today, FREE_DAILY_ADVANCED_LIMIT)

        await update.message.reply_text(text, parse_mode='Markdown')

    async def screener(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Скринер топ-монет по объёму: ищем перепроданные/перекупленные по RSI + тренд к SMA20"""
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        try:
            url = f'{COINGECKO_API}/coins/markets'
            params = {
                'vs_currency': 'usd',
                'order': 'volume_desc',
                'per_page': 15,
                'sparkline': True,
                'price_change_percentage': '24h',
                'locale': 'en',
            }
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            coins = response.json()
        except requests.exceptions.RequestException as e:
            await update.message.reply_text(f"❌ Ошибка при получении данных скринера: {str(e)}")
            return

        oversold, overbought, neutral_count = [], [], 0
        for coin in coins:
            sparkline = (coin.get('sparkline_in_7d') or {}).get('price') or []
            if len(sparkline) < 20:
                continue
            rsi = CryptoAnalyzer.calculate_rsi(sparkline, period=14)
            sma20 = TechnicalAnalyzer.sma(sparkline, 20)
            if rsi is None:
                continue
            symbol = coin['symbol'].upper()
            price = coin['current_price']
            if rsi < 35:
                oversold.append((symbol, rsi, price))
            elif rsi > 65:
                overbought.append((symbol, rsi, price))
            else:
                neutral_count += 1

        lines = ["🔍 **СКРИНЕР (ТОП-15 ПО ОБЪЁМУ)**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]

        lines.append("🟢 Перепроданные (RSI < 35) — потенциальный интерес к отскоку:")
        if oversold:
            for symbol, rsi, price in sorted(oversold, key=lambda x: x[1]):
                lines.append(f"   {symbol}: RSI {rsi:.0f}, {self.analyzer.format_price(price)}")
        else:
            lines.append("   Нет совпадений")

        lines.append("")
        lines.append("🔴 Перекупленные (RSI > 65) — риск коррекции:")
        if overbought:
            for symbol, rsi, price in sorted(overbought, key=lambda x: -x[1]):
                lines.append(f"   {symbol}: RSI {rsi:.0f}, {self.analyzer.format_price(price)}")
        else:
            lines.append("   Нет совпадений")

        lines.append("")
        lines.append(f"⚪ Нейтральных: {neutral_count}")
        lines.append("")
        lines.append("⚠️ RSI считается по 7-дневным данным — сигнал для дальнейшей проверки через /ta, не сигнал к сделке.")

        within_limit, used_today = self._check_and_bump_limit(update.effective_user.id, 'advanced', FREE_DAILY_ADVANCED_LIMIT)
        text = "\n".join(lines)
        if not within_limit:
            text += self._upsell_footer(used_today, FREE_DAILY_ADVANCED_LIMIT)

        await update.message.reply_text(text, parse_mode='Markdown')

    async def news(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Свежие новости по монете/ключевому слову"""
        if not context.args:
            await update.message.reply_text("❌ Укажи монету\nПример: /news bitcoin", parse_mode='Markdown')
            return

        keyword = ' '.join(context.args)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        loop = asyncio.get_running_loop()
        items = await loop.run_in_executor(None, NewsService.get_news, keyword, 5)

        if not items:
            await update.message.reply_text(f"❌ Не удалось получить новости по '{keyword}'")
            return

        lines = [f"📰 **НОВОСТИ: {keyword.upper()}**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]
        for item in items:
            title = item.get('title', 'Без заголовка')
            source = item.get('source_info', {}).get('name') or item.get('source', '')
            url = item.get('url', '')
            lines.append(f"• {title}")
            if source:
                lines.append(f"  Источник: {source}")
            if url:
                lines.append(f"  {url}")
            lines.append("")

        lines.append("⚠️ Заголовки без редактуры — проверяй первоисточник перед выводами.")

        within_limit, used_today = self._check_and_bump_limit(update.effective_user.id, 'advanced', FREE_DAILY_ADVANCED_LIMIT)
        text = "\n".join(lines)
        if not within_limit:
            text += self._upsell_footer(used_today, FREE_DAILY_ADVANCED_LIMIT)

        await update.message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)

    def run(self):
        """Запустить бота"""
        logger.info("🚀 Бот запускается...")
        self.app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )


if __name__ == '__main__':
    bot = CryptoBot(TELEGRAM_TOKEN)
    bot.run()
