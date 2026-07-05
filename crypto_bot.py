import os
import sys
import requests
import logging
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
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


class CryptoBot:
    """Главный класс бота"""

    def __init__(self, token: str):
        self.token = token
        self.app = Application.builder().token(token).post_init(self._post_init).build()
        self.analyzer = CryptoAnalyzer()
        self._register_handlers()
        self.app.add_error_handler(self._error_handler)

    async def _post_init(self, app: Application):
        """Регистрация списка команд в меню Telegram"""
        await app.bot.set_my_commands([
            BotCommand("start", "Начать работу с ботом"),
            BotCommand("analyze", "Полный анализ монеты"),
            BotCommand("price", "Быстрая цена"),
            BotCommand("compare", "Сравнение двух монет"),
            BotCommand("top", "Топ 20 монет по объёму"),
            BotCommand("help", "Справка"),
        ])

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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        welcome_message = """
🚀 **Добро пожаловать в ОБНУЛЕННЫЙ анализатор!**

Я помогаю анализировать крипто-проекты и делать умные инвестиции.

**Доступные команды:**
/analyze [монета] - Полный анализ проекта
/price [монета] - Быстрая цена
/compare [coin1] [coin2] - Сравнение двух монет
/top - Топ 20 монет по объёму
/help - Справка

**Примеры:**
/analyze bitcoin
/price ethereum
/compare bitcoin ethereum

Используй бота чтобы быстро анализировать проекты! 📊
        """
        await update.message.reply_text(welcome_message, parse_mode='Markdown')

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
