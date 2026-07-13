import os
import sys
import io
import json
import asyncio
import logging
import requests
from html import escape as esc
from pathlib import Path
from datetime import datetime, timezone

import matplotlib
matplotlib.use('Agg')  # без графического дисплея (сервер/Railway)
import matplotlib.pyplot as plt

from telegram import (
    Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters,
)

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

# Фирменный стиль
DIVIDER = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
ACCENT_HEX = '#D97706'

# Популярные монеты для быстрого выбора кнопками: (coingecko_id, тикер, эмодзи)
POPULAR_COINS = [
    ('bitcoin', 'BTC', '₿'),
    ('ethereum', 'ETH', 'Ξ'),
    ('solana', 'SOL', '◎'),
    ('binancecoin', 'BNB', '🔶'),
    ('ripple', 'XRP', '✕'),
    ('the-open-network', 'TON', '💎'),
    ('dogecoin', 'DOGE', '🐶'),
    ('cardano', 'ADA', '🔷'),
]

PREMIUM_ACTIONS = {'ta', 'exchanges', 'screener', 'news'}

PICK_PROMPTS = {
    'analyze': "📊 <b>Анализ монеты</b>\nВыбери из популярных или введи свою:",
    'price': "💰 <b>Быстрая цена</b>\nВыбери из популярных или введи свою:",
    'ta': "📐 <b>Технический анализ</b>\nВыбери из популярных или введи свою:",
    'exchanges': "🏦 <b>Цена на биржах</b>\nВыбери из популярных или введи свою:",
    'news': "📰 <b>Новости по монете</b>\nВыбери из популярных или введи свою:",
}


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

        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
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
        if price is None:
            return "н/д"
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

        if market_cap is None or market_cap < 10_000_000:
            risk_score += 3
            reasons.append("Маленький Market Cap")
        elif market_cap < 100_000_000:
            risk_score += 2
            reasons.append("Средний Market Cap")

        if price_change_24h and abs(price_change_24h) > 20:
            risk_score += 2
            reasons.append("Высокая волатильность")

        risk_level = min(risk_score, 10)
        return risk_level, reasons

    @staticmethod
    def format_compact(value: float) -> str:
        """$1.28T / $32.4M и т.п."""
        if not value:
            return "N/A"
        if value >= 1_000_000_000_000:
            return f"${value / 1_000_000_000_000:.2f}T"
        if value >= 1_000_000_000:
            return f"${value / 1_000_000_000:.2f}B"
        if value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        return f"${value:,.0f}"


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
    def sma_series(values: list, period: int) -> list:
        if len(values) < period:
            return []
        return [sum(values[i - period + 1:i + 1]) / period for i in range(period - 1, len(values))]

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
    def bollinger_series(values: list, period: int = 20, num_std: float = 2):
        if len(values) < period:
            return [], [], []
        uppers, mids, lowers = [], [], []
        for i in range(period - 1, len(values)):
            window = values[i - period + 1:i + 1]
            mean = sum(window) / period
            variance = sum((x - mean) ** 2 for x in window) / period
            std = variance ** 0.5
            uppers.append(mean + num_std * std)
            mids.append(mean)
            lowers.append(mean - num_std * std)
        return uppers, mids, lowers

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


def generate_ta_chart(prices: list, label: str) -> bytes:
    """PNG-график: цена + SMA20 + Bollinger Bands, тёмная тема бренда"""
    sma20 = TechnicalAnalyzer.sma_series(prices, 20)
    upper, _mid, lower = TechnicalAnalyzer.bollinger_series(prices, 20)

    bg = '#0f1115'
    fig, ax = plt.subplots(figsize=(9, 5), dpi=150)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)

    x_full = list(range(len(prices)))
    ax.plot(x_full, prices, color='#f5f5f5', linewidth=1.6, label='Цена', zorder=3)

    if upper and lower:
        x_bb = list(range(len(prices) - len(upper), len(prices)))
        ax.plot(x_bb, upper, color=ACCENT_HEX, linewidth=0.9, alpha=0.8, linestyle='--')
        ax.plot(x_bb, lower, color=ACCENT_HEX, linewidth=0.9, alpha=0.8, linestyle='--')
        ax.fill_between(x_bb, lower, upper, color=ACCENT_HEX, alpha=0.08)

    if sma20:
        x_sma = list(range(len(prices) - len(sma20), len(prices)))
        ax.plot(x_sma, sma20, color='#3B82F6', linewidth=1.3, label='SMA20')

    ax.set_title(f"{label} — цена, SMA20, Bollinger Bands", color='#f5f5f5', fontsize=13, pad=12)
    ax.tick_params(colors='#9CA3AF', labelsize=8)
    for spine in ax.spines.values():
        spine.set_color('#2b2f38')
    ax.grid(color='#232733', linewidth=0.5, alpha=0.6)
    ax.set_xticks([])
    ax.legend(loc='upper left', facecolor=bg, edgecolor='#2b2f38', labelcolor='#f5f5f5', fontsize=8)

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format='png', facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


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


class Keyboards:
    """Инлайн-клавиатуры бота — вся навигация кнопками, без ручного ввода команд"""

    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton("📊 Анализ", callback_data="pick:analyze"),
             InlineKeyboardButton("💰 Цена", callback_data="pick:price")],
            [InlineKeyboardButton("📐 Тех.анализ", callback_data="pick:ta"),
             InlineKeyboardButton("🏦 Биржи", callback_data="pick:exchanges")],
            [InlineKeyboardButton("🔍 Скринер", callback_data="screener"),
             InlineKeyboardButton("📰 Новости", callback_data="pick:news")],
            [InlineKeyboardButton("🔝 Топ-20", callback_data="top"),
             InlineKeyboardButton("⚖️ Сравнить", callback_data="cmp_pick")],
            [InlineKeyboardButton("🧭 Сообщество", callback_data="community"),
             InlineKeyboardButton("🎁 Пригласить", callback_data="invite")],
            [InlineKeyboardButton("📜 Правила", callback_data="rules"),
             InlineKeyboardButton("❓ Помощь", callback_data="help")],
        ]
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def coin_picker(action: str) -> InlineKeyboardMarkup:
        rows, row = [], []
        for coin_id, symbol, emoji in POPULAR_COINS:
            token = symbol if action == 'exchanges' else coin_id
            row.append(InlineKeyboardButton(f"{emoji} {symbol}", callback_data=f"go:{action}:{token}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("✏️ Своя монета", callback_data=f"custom:{action}")])
        rows.append([InlineKeyboardButton("🔙 Меню", callback_data="menu")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def compare_picker() -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton("BTC vs ETH", callback_data="cmp:bitcoin:ethereum")],
            [InlineKeyboardButton("BTC vs SOL", callback_data="cmp:bitcoin:solana")],
            [InlineKeyboardButton("ETH vs SOL", callback_data="cmp:ethereum:solana")],
            [InlineKeyboardButton("✏️ Свои монеты", callback_data="cmp_custom")],
            [InlineKeyboardButton("🔙 Меню", callback_data="menu")],
        ]
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def coin_actions(current_action: str, coin_id: str, symbol: str = None) -> InlineKeyboardMarkup:
        """Связанные действия под карточкой монеты + Обновить/Меню"""
        symbol_token = (symbol or coin_id).upper()
        options = [
            ('analyze', '📊 Анализ', coin_id),
            ('price', '💰 Цена', coin_id),
            ('ta', '📐 ТА', coin_id),
            ('exchanges', '🏦 Биржи', symbol_token),
            ('news', '📰 Новости', coin_id),
        ]
        related = [(label, f"go:{act}:{token}") for act, label, token in options if act != current_action]

        rows, row = [], []
        for label, cb in related:
            row.append(InlineKeyboardButton(label, callback_data=cb))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        refresh_token = symbol_token if current_action == 'exchanges' else coin_id
        rows.append([
            InlineKeyboardButton("🔄 Обновить", callback_data=f"go:{current_action}:{refresh_token}"),
            InlineKeyboardButton("🔙 Меню", callback_data="menu"),
        ])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def simple_nav(refresh_callback: str = None) -> InlineKeyboardMarkup:
        rows = []
        if refresh_callback:
            rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=refresh_callback)])
        rows.append([InlineKeyboardButton("🔙 Меню", callback_data="menu")])
        return InlineKeyboardMarkup(rows)


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
        """Регистрация списка команд в меню Telegram (для тех, кто предпочитает печатать)"""
        me = await app.bot.get_me()
        self.bot_username = me.username
        await app.bot.set_my_commands([
            BotCommand("start", "Открыть главное меню"),
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

    # ---------- Данные пользователей / лимиты ----------

    def _get_user_record(self, user_id: int) -> dict:
        uid = str(user_id)
        if uid not in self.data['users']:
            self.data['users'][uid] = {
                'first_seen': today_str(),
                'referred_by': None,
                'invited_count': 0,
            }
        return self.data['users'][uid]

    def _check_and_bump_limit(self, user_id: int, feature: str, daily_limit: int) -> tuple:
        """Возвращает (allowed_without_upsell: bool, used_today: int)"""
        record = self._get_user_record(user_id)
        day = today_str()
        usage = record.setdefault('usage', {})
        feature_usage = usage.setdefault(feature, {})
        used = feature_usage.get(day, 0)
        feature_usage[day] = used + 1
        usage[feature] = {day: feature_usage[day]}  # чистим старые дни
        save_data(self.data)
        return used < daily_limit, used + 1

    def _upsell_footer(self, used_today: int, limit: int) -> str:
        return (
            f"\n\n🔒 Премиум-запросов на сегодня: {used_today}/{limit} использовано.\n"
            f"Безлимит — в тарифе Standard: /community"
        )

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error("Ошибка при обработке апдейта %s: %s", update, context.error, exc_info=context.error)
        if isinstance(update, Update):
            if update.callback_query:
                try:
                    await update.callback_query.answer("❌ Ошибка, попробуй ещё раз", show_alert=True)
                except Exception:
                    pass
            elif update.effective_message:
                try:
                    await update.effective_message.reply_text("❌ Произошла ошибка, попробуй ещё раз чуть позже")
                except Exception:
                    pass

    def _register_handlers(self):
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
        self.app.add_handler(CallbackQueryHandler(self.on_callback))
        self.app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, self.welcome_new_member))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

    # ---------- Резолв монеты ----------

    async def _resolve_coin_id(self, query: str) -> str:
        loop = asyncio.get_running_loop()
        market_data = await loop.run_in_executor(None, self.analyzer.get_market_data, query)
        if market_data:
            return query
        results = await loop.run_in_executor(None, self.analyzer.search_coin, query)
        if results:
            return results[0]['id']
        return None

    # ---------- Рендер: карточки монет ----------

    async def _render_analyze(self, coin_id: str) -> tuple:
        loop = asyncio.get_running_loop()
        market_data = await loop.run_in_executor(None, self.analyzer.get_market_data, coin_id)
        full_data = await loop.run_in_executor(None, self.analyzer.get_coin_data, coin_id)

        if not market_data:
            return (f"❌ Не удалось получить данные по «{esc(coin_id)}»", Keyboards.simple_nav())

        price = market_data.get('usd', 0)
        market_cap = market_data.get('usd_market_cap')
        volume_24h = market_data.get('usd_24h_vol')
        change_24h = market_data.get('usd_24h_change', 0) or 0

        full_market_data = (full_data or {}).get('market_data', {})
        change_7d = full_market_data.get('price_change_percentage_7d') or 0
        change_30d = full_market_data.get('price_change_percentage_30d') or 0

        risk_level, risk_reasons = CryptoAnalyzer.get_risk_level(market_cap, volume_24h, change_24h)
        risk_emoji = "🟢" if risk_level <= 3 else ("🟡" if risk_level <= 6 else "🔴")
        risk_text = "Низкий" if risk_level <= 3 else ("Средний" if risk_level <= 6 else "Высокий")

        name = esc((full_data or {}).get('name') or coin_id.capitalize())
        symbol = esc(((full_data or {}).get('symbol') or coin_id)).upper()

        e24 = "📈" if change_24h >= 0 else "📉"
        e7 = "📈" if change_7d >= 0 else "📉"
        e30 = "📈" if change_30d >= 0 else "📉"

        text = (
            f"📊 <b>{name} ({symbol})</b>\n{DIVIDER}\n\n"
            f"💰 Цена: <code>{CryptoAnalyzer.format_price(price)}</code>\n\n"
            f"📈 Изменения\n"
            f"   {e24} 24ч: <code>{change_24h:+.2f}%</code>\n"
            f"   {e7} 7д: <code>{change_7d:+.2f}%</code>\n"
            f"   {e30} 30д: <code>{change_30d:+.2f}%</code>\n\n"
            f"💎 Market Cap: <code>{CryptoAnalyzer.format_compact(market_cap)}</code>\n"
            f"📊 Объём 24ч: <code>{CryptoAnalyzer.format_compact(volume_24h)}</code>\n\n"
            f"{risk_emoji} Риск: <b>{risk_level}/10</b> ({risk_text})\n"
            f"   <i>{esc(', '.join(risk_reasons)) if risk_reasons else 'Низкие риски'}</i>\n"
            f"{DIVIDER}"
        )
        keyboard = Keyboards.coin_actions('analyze', coin_id, symbol)
        return (text, keyboard)

    async def _render_price(self, coin_id: str) -> tuple:
        loop = asyncio.get_running_loop()
        market_data = await loop.run_in_executor(None, self.analyzer.get_market_data, coin_id)
        if not market_data:
            return (f"❌ Не удалось получить цену «{esc(coin_id)}»", Keyboards.simple_nav())

        price = market_data.get('usd', 0)
        change_24h = market_data.get('usd_24h_change', 0) or 0
        emoji = "📈" if change_24h >= 0 else "📉"

        text = (
            f"💰 <b>{esc(coin_id.upper())}</b>\n{DIVIDER}\n\n"
            f"Цена: <code>{CryptoAnalyzer.format_price(price)}</code>\n"
            f"{emoji} 24ч: <code>{change_24h:+.2f}%</code>\n{DIVIDER}"
        )
        keyboard = Keyboards.coin_actions('price', coin_id)
        return (text, keyboard)

    async def _render_compare(self, coin1: str, coin2: str) -> tuple:
        loop = asyncio.get_running_loop()
        data1 = await loop.run_in_executor(None, self.analyzer.get_market_data, coin1)
        data2 = await loop.run_in_executor(None, self.analyzer.get_market_data, coin2)

        if not data1 or not data2:
            return ("❌ Не удалось найти одну или обе монеты", Keyboards.simple_nav())

        price1, price2 = data1.get('usd', 0), data2.get('usd', 0)
        change1, change2 = data1.get('usd_24h_change', 0) or 0, data2.get('usd_24h_change', 0) or 0
        cap1, cap2 = data1.get('usd_market_cap'), data2.get('usd_market_cap')
        leader = coin1 if abs(change1) > abs(change2) else coin2

        text = (
            f"⚖️ <b>СРАВНЕНИЕ</b>\n{DIVIDER}\n\n"
            f"💰 <b>{esc(coin1.upper())}</b>\n"
            f"   Цена: <code>{CryptoAnalyzer.format_price(price1)}</code>\n"
            f"   24ч: <code>{change1:+.2f}%</code>\n"
            f"   Market Cap: <code>{CryptoAnalyzer.format_compact(cap1)}</code>\n\n"
            f"💰 <b>{esc(coin2.upper())}</b>\n"
            f"   Цена: <code>{CryptoAnalyzer.format_price(price2)}</code>\n"
            f"   24ч: <code>{change2:+.2f}%</code>\n"
            f"   Market Cap: <code>{CryptoAnalyzer.format_compact(cap2)}</code>\n\n"
            f"🏆 Лидер по изменению (24ч): <b>{esc(leader.upper())}</b>\n{DIVIDER}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"cmp:{coin1}:{coin2}")],
            [InlineKeyboardButton("⚖️ Другая пара", callback_data="cmp_pick")],
            [InlineKeyboardButton("🔙 Меню", callback_data="menu")],
        ])
        return (text, keyboard)

    async def _render_top(self) -> tuple:
        loop = asyncio.get_running_loop()
        try:
            def fetch():
                url = f'{COINGECKO_API}/coins/markets'
                params = {'vs_currency': 'usd', 'order': 'volume_desc', 'per_page': 20,
                          'sparkline': False, 'locale': 'en'}
                r = requests.get(url, params=params, timeout=10)
                r.raise_for_status()
                return r.json()

            coins = await loop.run_in_executor(None, fetch)
        except Exception as e:
            return (f"❌ Ошибка при получении топа: {esc(str(e))}", Keyboards.simple_nav())

        lines = [f"🔝 <b>ТОП 20 МОНЕТ ПО ОБЪЁМУ</b>", DIVIDER, ""]
        for i, coin in enumerate(coins, 1):
            name = esc(coin['name'])
            symbol = esc(coin['symbol'].upper())
            price_str = CryptoAnalyzer.format_price(coin['current_price'])
            change = coin.get('price_change_percentage_24h') or 0
            emoji = "📈" if change >= 0 else "📉"
            vol_str = CryptoAnalyzer.format_compact(coin.get('total_volume'))
            lines.append(f"{i}. {name} ({symbol})\n   {price_str} {emoji} <code>{change:+.2f}%</code> | Vol: {vol_str}")

        text = "\n".join(lines)
        keyboard = Keyboards.simple_nav(refresh_callback="top")
        return (text, keyboard)

    async def _render_ta(self, coin_id: str) -> tuple:
        loop = asyncio.get_running_loop()
        prices = await loop.run_in_executor(None, TechnicalAnalyzer.get_price_history, coin_id, 100)
        if not prices:
            return (f"❌ Недостаточно данных по «{esc(coin_id)}»", Keyboards.simple_nav(), None)

        summary = TechnicalAnalyzer.summarize(prices)
        if 'error' in summary:
            return (f"❌ {esc(summary['error'])}", Keyboards.simple_nav(), None)

        rsi_str = f"{summary['rsi']:.1f}" if summary['rsi'] is not None else "н/д"
        sma_str = CryptoAnalyzer.format_price(summary['sma20']) if summary['sma20'] else "н/д"
        if summary['macd']:
            macd_str = f"{summary['macd']['macd']:.4f} / сигнал {summary['macd']['signal']:.4f}"
        else:
            macd_str = "н/д (мало данных)"
        if summary['bollinger']:
            bb = summary['bollinger']
            bb_str = f"{CryptoAnalyzer.format_price(bb['lower'])} — {CryptoAnalyzer.format_price(bb['upper'])}"
        else:
            bb_str = "н/д"

        label = esc(coin_id.upper())
        caption = (
            f"📐 <b>{label}</b> — технический анализ\n{DIVIDER}\n\n"
            f"RSI(14): <code>{rsi_str}</code>   SMA20: <code>{sma_str}</code>\n"
            f"MACD: <code>{macd_str}</code>\n"
            f"Bollinger: <code>{bb_str}</code>\n\n"
            f"{summary['verdict']}\n"
            f"<i>{esc(', '.join(summary['reasons'])) if summary['reasons'] else 'недостаточно данных'}</i>\n"
            f"{DIVIDER}\n⚠️ Не финансовый совет — сводка индикаторов."
        )
        chart = await loop.run_in_executor(None, generate_ta_chart, prices, coin_id.upper())
        keyboard = Keyboards.coin_actions('ta', coin_id)
        return (caption, keyboard, chart)

    async def _render_exchanges(self, ticker: str, quote: str = 'USDT') -> tuple:
        symbol = f"{ticker.upper()}/{quote.upper()}"
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, ExchangeAggregator.fetch_all_sync, symbol)

        lines = [f"🏦 <b>{esc(symbol)} НА БИРЖАХ</b>", DIVIDER, ""]
        prices = {}
        for ex_id, data in results.items():
            if 'error' in data or not data.get('price'):
                lines.append(f"{ex_id.capitalize()}: н/д")
                continue
            price = data['price']
            prices[ex_id] = price
            vol = data.get('volume')
            vol_str = f", объём: {CryptoAnalyzer.format_compact(vol)}" if vol else ""
            lines.append(f"{ex_id.capitalize()}: <code>{CryptoAnalyzer.format_price(price)}</code>{vol_str}")

        if len(prices) >= 2:
            max_ex, min_ex = max(prices, key=prices.get), min(prices, key=prices.get)
            spread_pct = (prices[max_ex] - prices[min_ex]) / prices[min_ex] * 100
            lines.append("")
            lines.append(f"📊 Разброс цен: <code>{spread_pct:.2f}%</code> ({min_ex.capitalize()} → {max_ex.capitalize()})")
        elif not prices:
            lines.append("")
            lines.append("Не удалось получить данные ни с одной биржи — проверь тикер.")

        lines.append("")
        lines.append("⚠️ Не является рекомендацией к арбитражу или сделкам.")
        lines.append(DIVIDER)

        text = "\n".join(lines)
        keyboard = Keyboards.coin_actions('exchanges', ticker.lower(), ticker.upper())
        return (text, keyboard)

    async def _render_screener(self) -> tuple:
        loop = asyncio.get_running_loop()
        try:
            def fetch():
                url = f'{COINGECKO_API}/coins/markets'
                params = {'vs_currency': 'usd', 'order': 'volume_desc', 'per_page': 15,
                          'sparkline': True, 'price_change_percentage': '24h', 'locale': 'en'}
                r = requests.get(url, params=params, timeout=15)
                r.raise_for_status()
                return r.json()

            coins = await loop.run_in_executor(None, fetch)
        except Exception as e:
            return (f"❌ Ошибка скринера: {esc(str(e))}", Keyboards.simple_nav())

        oversold, overbought, neutral_count = [], [], 0
        for coin in coins:
            sparkline = (coin.get('sparkline_in_7d') or {}).get('price') or []
            if len(sparkline) < 20:
                continue
            rsi = CryptoAnalyzer.calculate_rsi(sparkline, period=14)
            if rsi is None:
                continue
            symbol = esc(coin['symbol'].upper())
            price = coin['current_price']
            if rsi < 35:
                oversold.append((symbol, rsi, price))
            elif rsi > 65:
                overbought.append((symbol, rsi, price))
            else:
                neutral_count += 1

        lines = ["🔍 <b>СКРИНЕР (ТОП-15 ПО ОБЪЁМУ)</b>", DIVIDER, "", "🟢 Перепроданные (RSI &lt; 35):"]
        if oversold:
            for symbol, rsi, price in sorted(oversold, key=lambda x: x[1]):
                lines.append(f"   {symbol}: RSI <code>{rsi:.0f}</code>, {CryptoAnalyzer.format_price(price)}")
        else:
            lines.append("   Нет совпадений")

        lines.append("")
        lines.append("🔴 Перекупленные (RSI &gt; 65):")
        if overbought:
            for symbol, rsi, price in sorted(overbought, key=lambda x: -x[1]):
                lines.append(f"   {symbol}: RSI <code>{rsi:.0f}</code>, {CryptoAnalyzer.format_price(price)}")
        else:
            lines.append("   Нет совпадений")

        lines.append("")
        lines.append(f"⚪ Нейтральных: {neutral_count}")
        lines.append(DIVIDER)
        lines.append("⚠️ RSI по 7-дневным данным — сигнал для проверки через ТА, не сигнал к сделке.")

        text = "\n".join(lines)
        keyboard = Keyboards.simple_nav(refresh_callback="screener")
        return (text, keyboard)

    async def _render_news(self, keyword: str) -> tuple:
        loop = asyncio.get_running_loop()
        items = await loop.run_in_executor(None, NewsService.get_news, keyword, 5)
        if not items:
            return (f"❌ Не удалось получить новости по «{esc(keyword)}»", Keyboards.simple_nav())

        lines = [f"📰 <b>НОВОСТИ: {esc(keyword.upper())}</b>", DIVIDER, ""]
        for item in items:
            title = esc(item.get('title', 'Без заголовка'))
            source = esc(item.get('source_info', {}).get('name') or item.get('source', ''))
            url = item.get('url', '')
            lines.append(f"• <b>{title}</b>")
            if source:
                lines.append(f"  {source}")
            if url:
                lines.append(f'  <a href="{esc(url)}">Читать →</a>')
            lines.append("")

        lines.append("⚠️ Заголовки без редактуры — проверяй первоисточник.")
        lines.append(DIVIDER)

        text = "\n".join(lines)
        keyboard = Keyboards.coin_actions('news', keyword.lower())
        return (text, keyboard)

    def _render_community(self) -> tuple:
        links_block = f"💬 Чат: {COMMUNITY_CHAT_URL}"
        if COMMUNITY_CHANNEL_URL:
            links_block += f"\n📢 Канал: {COMMUNITY_CHANNEL_URL}"

        text = (
            f"🧭 <b>ОБНУЛЕННЫЙ — сообщество трезвых крипто-инвесторов</b>\n\n"
            f"Без сигналов «купи на хаях». Только данные, риск-скоринг и живое обсуждение.\n\n"
            f"{links_block}\n\n"
            f"<b>Тарифы:</b>\n\n"
            f"🆓 <b>Free</b>\n"
            f"Чат, /price, /top, {FREE_DAILY_ANALYZE_LIMIT} разбора анализа и {FREE_DAILY_ADVANCED_LIMIT} премиум-запроса (ТА/биржи/скринер/новости) в день\n\n"
            f"⭐ <b>Standard</b> (~700-900 ₽/мес)\n"
            f"Безлимит на все функции, ежедневный дайджест, приоритет в чате\n\n"
            f"💎 <b>Pro</b> (~2000-2500 ₽/мес)\n"
            f"Всё из Standard + закрытый чат с разборами портфелей, доступ к экспертам\n\n"
            f"Оплата — через Telegram Stars прямо в боте (скоро)."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Пригласить и получить бонус", callback_data="invite")],
            [InlineKeyboardButton("🔙 Меню", callback_data="menu")],
        ])
        return (text, keyboard)

    async def _render_invite(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> tuple:
        record = self._get_user_record(user_id)
        save_data(self.data)
        username = self.bot_username or (await context.bot.get_me()).username
        link = f"https://t.me/{username}?start=ref_{user_id}"
        invited = record.get('invited_count', 0)

        text = (
            f"🎁 <b>Приглашай друзей в ОБНУЛЕННЫЙ</b>\n\n"
            f"Твоя персональная ссылка:\n<code>{esc(link)}</code>\n\n"
            f"Уже приглашено: <b>{invited}</b> чел.\n\n"
            f"Каждый друг, который запустит бота по твоей ссылке, засчитывается автоматически."
        )
        keyboard = Keyboards.simple_nav(refresh_callback="invite")
        return (text, keyboard)

    def _render_rules(self) -> tuple:
        text = (
            f"📜 <b>Правила сообщества ОБНУЛЕННЫЙ</b>\n{DIVIDER}\n\n"
            f"1️⃣ Уважение — без токсичности, оскорблений и переходов на личности\n"
            f"2️⃣ Никаких сигналов и «гарантированных иксов» — только аргументированное мнение\n"
            f"3️⃣ Реклама и рефссылки — только по согласованию с модераторами\n"
            f"4️⃣ Финансовые решения — твоя ответственность\n"
            f"5️⃣ Флуд и спам — предупреждение, потом бан\n{DIVIDER}\n"
            f"Нарушения — пишите модераторам. Всем трезвых решений! 🧊"
        )
        return (text, Keyboards.simple_nav())

    def _render_help(self) -> tuple:
        text = (
            f"📖 <b>СПРАВКА</b>\n{DIVIDER}\n\n"
            f"Всё управляется кнопками — жми /start и выбирай нужное в меню.\n"
            f"Для тех, кто любит печатать, команды тоже работают:\n\n"
            f"/analyze [монета], /price [монета], /compare [c1] [c2], /top\n"
            f"/ta [монета], /exchanges [тикер], /screener, /news [монета]\n"
            f"/community, /invite, /rules\n\n"
            f"Продвинутая аналитика (ТА/биржи/скринер/новости) — {FREE_DAILY_ADVANCED_LIMIT} бесплатных запроса в день, "
            f"дальше — тариф Standard.\n{DIVIDER}"
        )
        return (text, Keyboards.main_menu())

    # ---------- Диспетчер ----------

    async def _dispatch(self, action: str, token: str, user_id: int, quote: str = 'USDT') -> dict:
        """Единая точка рендера + лимитов для монето-ориентированных действий"""
        if action == 'analyze':
            text, keyboard = await self._render_analyze(token)
            within, used = self._check_and_bump_limit(user_id, 'analyze', FREE_DAILY_ANALYZE_LIMIT)
            if not within:
                text += self._upsell_footer(used, FREE_DAILY_ANALYZE_LIMIT)
            return {'text': text, 'keyboard': keyboard, 'photo': None}

        if action == 'price':
            text, keyboard = await self._render_price(token)
            return {'text': text, 'keyboard': keyboard, 'photo': None}

        if action == 'ta':
            text, keyboard, photo = await self._render_ta(token)
            within, used = self._check_and_bump_limit(user_id, 'advanced', FREE_DAILY_ADVANCED_LIMIT)
            if not within:
                text += self._upsell_footer(used, FREE_DAILY_ADVANCED_LIMIT)
            return {'text': text, 'keyboard': keyboard, 'photo': photo}

        if action == 'exchanges':
            text, keyboard = await self._render_exchanges(token, quote)
            within, used = self._check_and_bump_limit(user_id, 'advanced', FREE_DAILY_ADVANCED_LIMIT)
            if not within:
                text += self._upsell_footer(used, FREE_DAILY_ADVANCED_LIMIT)
            return {'text': text, 'keyboard': keyboard, 'photo': None}

        if action == 'news':
            text, keyboard = await self._render_news(token)
            within, used = self._check_and_bump_limit(user_id, 'advanced', FREE_DAILY_ADVANCED_LIMIT)
            if not within:
                text += self._upsell_footer(used, FREE_DAILY_ADVANCED_LIMIT)
            return {'text': text, 'keyboard': keyboard, 'photo': None}

        return None

    async def _send_result(self, message, result: dict):
        if result['photo']:
            await message.reply_photo(photo=result['photo'], caption=result['text'],
                                       parse_mode=ParseMode.HTML, reply_markup=result['keyboard'])
        else:
            await message.reply_text(result['text'], parse_mode=ParseMode.HTML,
                                      reply_markup=result['keyboard'], disable_web_page_preview=True)

    async def _edit_or_send(self, query, text: str, keyboard: InlineKeyboardMarkup):
        try:
            if query.message.photo:
                # Исходное сообщение — фото (например, после /ta), в текст его не превратить
                await query.message.reply_text(text, parse_mode=ParseMode.HTML,
                                                reply_markup=keyboard, disable_web_page_preview=True)
            else:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                               reply_markup=keyboard, disable_web_page_preview=True)
        except BadRequest as e:
            if 'Message is not modified' not in str(e):
                await query.message.reply_text(text, parse_mode=ParseMode.HTML,
                                                reply_markup=keyboard, disable_web_page_preview=True)

    async def _deliver(self, query, result: dict):
        keyboard, text, photo = result['keyboard'], result['text'], result['photo']
        if photo:
            media = InputMediaPhoto(media=photo, caption=text, parse_mode=ParseMode.HTML)
            try:
                if query.message.photo:
                    await query.edit_message_media(media=media, reply_markup=keyboard)
                else:
                    await query.message.reply_photo(photo=photo, caption=text,
                                                      parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except BadRequest:
                await query.message.reply_photo(photo=photo, caption=text,
                                                  parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await self._edit_or_send(query, text, keyboard)

    # ---------- Callback-роутинг (кнопки) ----------

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data or ""
        user_id = update.effective_user.id
        await query.answer()

        if data == 'menu':
            text = "🧊 <b>ОБНУЛЕННЫЙ</b>\n<i>Трезвая крипто-аналитика без хайпа</i>\n\nВыбери, что нужно 👇"
            await self._edit_or_send(query, text, Keyboards.main_menu())
            return

        if data.startswith('pick:'):
            action = data.split(':', 1)[1]
            prompt = PICK_PROMPTS.get(action, "Выбери монету:")
            await self._edit_or_send(query, prompt, Keyboards.coin_picker(action))
            return

        if data.startswith('custom:'):
            action = data.split(':', 1)[1]
            context.user_data['awaiting'] = action
            await self._edit_or_send(
                query,
                "✏️ Напиши тикер или название монеты следующим сообщением (например: <code>btc</code>).",
                Keyboards.simple_nav(),
            )
            return

        if data == 'cmp_pick':
            await self._edit_or_send(query, "⚖️ <b>Сравнение</b>\nВыбери пару или введи свои:",
                                      Keyboards.compare_picker())
            return

        if data.startswith('cmp:'):
            _, c1, c2 = data.split(':', 2)
            text, keyboard = await self._render_compare(c1, c2)
            await self._edit_or_send(query, text, keyboard)
            return

        if data == 'cmp_custom':
            context.user_data['awaiting'] = 'compare'
            await self._edit_or_send(
                query,
                "✏️ Напиши две монеты через пробел следующим сообщением (например: <code>bitcoin ethereum</code>).",
                Keyboards.simple_nav(),
            )
            return

        if data == 'top':
            text, keyboard = await self._render_top()
            await self._edit_or_send(query, text, keyboard)
            return

        if data == 'screener':
            text, keyboard = await self._render_screener()
            within, used = self._check_and_bump_limit(user_id, 'advanced', FREE_DAILY_ADVANCED_LIMIT)
            if not within:
                text += self._upsell_footer(used, FREE_DAILY_ADVANCED_LIMIT)
            await self._edit_or_send(query, text, keyboard)
            return

        if data == 'community':
            text, keyboard = self._render_community()
            await self._edit_or_send(query, text, keyboard)
            return

        if data == 'invite':
            text, keyboard = await self._render_invite(user_id, context)
            await self._edit_or_send(query, text, keyboard)
            return

        if data == 'rules':
            text, keyboard = self._render_rules()
            await self._edit_or_send(query, text, keyboard)
            return

        if data == 'help':
            text, keyboard = self._render_help()
            await self._edit_or_send(query, text, keyboard)
            return

        if data.startswith('go:'):
            _, action, token = data.split(':', 2)
            result = await self._dispatch(action, token, user_id)
            if result is None:
                await query.answer("Неизвестное действие", show_alert=True)
                return
            await self._deliver(query, result)
            return

    # ---------- Текстовый ввод произвольной монеты ----------

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        awaiting = context.user_data.get('awaiting')
        if not awaiting:
            return  # обычное сообщение вне диалога (например, в групповом чате) — не мешаем

        context.user_data['awaiting'] = None
        text_in = (update.message.text or "").strip()
        user_id = update.effective_user.id

        if awaiting == 'compare':
            parts = text_in.lower().split()
            if len(parts) < 2:
                await update.message.reply_text(
                    "❌ Нужно две монеты через пробел, например: <code>bitcoin ethereum</code>",
                    parse_mode=ParseMode.HTML, reply_markup=Keyboards.simple_nav(),
                )
                return
            c1 = await self._resolve_coin_id(parts[0]) or parts[0]
            c2 = await self._resolve_coin_id(parts[1]) or parts[1]
            text_out, keyboard = await self._render_compare(c1, c2)
            await update.message.reply_text(text_out, parse_mode=ParseMode.HTML,
                                             reply_markup=keyboard, disable_web_page_preview=True)
            return

        if awaiting == 'exchanges':
            token = text_in.upper()
        else:
            resolved = await self._resolve_coin_id(text_in.lower())
            token = resolved or text_in.lower()

        result = await self._dispatch(awaiting, token, user_id)
        if result is None:
            await update.message.reply_text("❌ Не удалось обработать запрос", reply_markup=Keyboards.simple_nav())
            return
        await self._send_result(update.message, result)

    # ---------- Команды (для тех, кто печатает руками) ----------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        text = (
            f"🧊 <b>ОБНУЛЕННЫЙ</b>\n"
            f"<i>Трезвая крипто-аналитика без хайпа</i>\n{referred_note}\n"
            f"Разборы, технический анализ, мультибиржевые цены, скринер и новости — всё в пару кликов, "
            f"без ручного набора команд.\n\n"
            f"Выбери, что нужно 👇"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, keyboard = self._render_help()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def community(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, keyboard = self._render_community()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                         disable_web_page_preview=True)

    async def invite(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, keyboard = await self._render_invite(update.effective_user.id, context)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                         disable_web_page_preview=True)

    async def rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, keyboard = self._render_rules()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def welcome_new_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for member in update.message.new_chat_members:
            if member.is_bot:
                continue
            name = esc(member.first_name or member.username or "друг")
            text = (
                f"👋 Привет, {name}! Добро пожаловать в чат ОБНУЛЕННЫЙ.\n\n"
                f"Здесь мы разбираем крипто-проекты трезво, без хайпа. Правила — /rules, "
                f"а личного бота-аналитика найдёшь тут: @{self.bot_username or ''}"
            )
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Выбери монету:", reply_markup=Keyboards.coin_picker('analyze'))
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        coin_id = await self._resolve_coin_id(' '.join(context.args).lower())
        if not coin_id:
            await update.message.reply_text("❌ Монета не найдена", reply_markup=Keyboards.simple_nav())
            return
        result = await self._dispatch('analyze', coin_id, update.effective_user.id)
        await self._send_result(update.message, result)

    async def price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Выбери монету:", reply_markup=Keyboards.coin_picker('price'))
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        coin_id = await self._resolve_coin_id(' '.join(context.args).lower())
        if not coin_id:
            await update.message.reply_text("❌ Монета не найдена", reply_markup=Keyboards.simple_nav())
            return
        result = await self._dispatch('price', coin_id, update.effective_user.id)
        await self._send_result(update.message, result)

    async def compare(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) < 2:
            await update.message.reply_text("Выбери пару для сравнения:", reply_markup=Keyboards.compare_picker())
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        c1 = await self._resolve_coin_id(context.args[0].lower()) or context.args[0].lower()
        c2 = await self._resolve_coin_id(context.args[1].lower()) or context.args[1].lower()
        text, keyboard = await self._render_compare(c1, c2)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                                         disable_web_page_preview=True)

    async def top_coins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        text, keyboard = await self._render_top()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def ta(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Выбери монету:", reply_markup=Keyboards.coin_picker('ta'))
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        coin_id = await self._resolve_coin_id(' '.join(context.args).lower())
        if not coin_id:
            await update.message.reply_text("❌ Монета не найдена", reply_markup=Keyboards.simple_nav())
            return
        result = await self._dispatch('ta', coin_id, update.effective_user.id)
        await self._send_result(update.message, result)

    async def exchanges(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Выбери монету:", reply_markup=Keyboards.coin_picker('exchanges'))
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        ticker = context.args[0].upper()
        quote = context.args[1].upper() if len(context.args) > 1 else 'USDT'
        result = await self._dispatch('exchanges', ticker, update.effective_user.id, quote=quote)
        await self._send_result(update.message, result)

    async def screener(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        text, keyboard = await self._render_screener()
        within, used = self._check_and_bump_limit(update.effective_user.id, 'advanced', FREE_DAILY_ADVANCED_LIMIT)
        if not within:
            text += self._upsell_footer(used, FREE_DAILY_ADVANCED_LIMIT)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def news(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Выбери монету:", reply_markup=Keyboards.coin_picker('news'))
            return
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        keyword = ' '.join(context.args)
        result = await self._dispatch('news', keyword, update.effective_user.id)
        await self._send_result(update.message, result)

    def run(self):
        logger.info("🚀 Бот запускается...")
        self.app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )


if __name__ == '__main__':
    bot = CryptoBot(TELEGRAM_TOKEN)
    bot.run()
