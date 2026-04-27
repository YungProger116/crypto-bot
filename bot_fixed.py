import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import os
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
import telebot
from telebot import types
import threading
import time
import random
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3

# Отключение предупреждений о небезопасных запросах
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка .env
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BINANCE_API_URL = "https://fapi.binance.com"
COINGLASS_WEBHOOK_URL = os.getenv("COINGLASS_WEBHOOK_URL")

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env файле")

# Кэш доступных фьючерсных пар
AVAILABLE_FUTURES_PAIRS = set()
LAST_FUTURES_UPDATE = 0
FUTURES_UPDATE_INTERVAL = 3600  # 1 час


# Создание сессии с повторными попытками
def create_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=['GET']
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=100,
        pool_maxsize=100
    )
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    })
    return session


# Глобальная сессия для всех запросов
REQUEST_SESSION = create_session()

bot = telebot.TeleBot(TOKEN)

user_states = {}
user_settings = defaultdict(dict)
signal_counters = {}
lock = threading.Lock()
# ИСПРАВЛЕНИЕ: отдельный лок для кэша данных
cache_lock = threading.Lock()
# ИСПРАВЛЕНИЕ: отдельный лок для OI данных
oi_lock = threading.Lock()
active_monitors = {}
data_cache = {}
cache_expiry = 2  # Кэширование данных на 2 секунды
daily_counters_reset = defaultdict(float)
monitoring_progress = defaultdict(dict)
prev_oi_data = defaultdict(dict)
last_analysis_time = defaultdict(dict)

# Поддерживаемые интервалы Binance
SUPPORTED_INTERVALS = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "2h",
    "240": "4h",
    "360": "6h",
    "480": "8h",
    "720": "12h",
    "1440": "1d",
    "10080": "1w"
}

# Читаемые названия интервалов
INTERVAL_READABLE = {
    "1m": "1 минута",
    "3m": "3 минуты",
    "5m": "5 минут",
    "15m": "15 минут",
    "30m": "30 минут",
    "1h": "1 час",
    "2h": "2 часа",
    "4h": "4 часа",
    "6h": "6 часов",
    "8h": "8 часов",
    "12h": "12 часов",
    "1d": "1 день",
    "1w": "1 неделя"
}


def get_available_futures_pairs(force_update=False):
    """Получает и кэширует список доступных фьючерсных пар"""
    global AVAILABLE_FUTURES_PAIRS, LAST_FUTURES_UPDATE

    current_time = time.time()

    if not force_update and current_time - LAST_FUTURES_UPDATE < FUTURES_UPDATE_INTERVAL and AVAILABLE_FUTURES_PAIRS:
        return AVAILABLE_FUTURES_PAIRS

    try:
        url = f"{BINANCE_API_URL}/fapi/v1/exchangeInfo"
        response = REQUEST_SESSION.get(url, timeout=15, verify=True)
        response.raise_for_status()
        data = response.json()

        available_pairs = set()
        for symbol_data in data['symbols']:
            if (symbol_data['status'] == 'TRADING' and
                    symbol_data['contractType'] == 'PERPETUAL' and
                    symbol_data['quoteAsset'] in ['USDT', 'USDC']):
                available_pairs.add(symbol_data['symbol'])

        AVAILABLE_FUTURES_PAIRS = available_pairs
        LAST_FUTURES_UPDATE = current_time

        logger.info(f"Обновлен список доступных фьючерсных пар: {len(available_pairs)} пар")
        return available_pairs
    except Exception as e:
        logger.error(f"Ошибка при получении доступных фьючерсных пар: {e}")
        return AVAILABLE_FUTURES_PAIRS if AVAILABLE_FUTURES_PAIRS else set()


def is_futures_pair_available(symbol):
    """Проверяет, доступна ли пара на фьючерсах"""
    available_pairs = get_available_futures_pairs()
    return symbol in available_pairs


def send_coin_glass_signal(signal_type: str, symbol: str, price: float, volume: float = None,
                           price_change: float = None, oi_change: float = None):
    if not COINGLASS_WEBHOOK_URL:
        logger.warning("COINGLASS_WEBHOOK_URL не настроен в .env файле")
        return

    payload = {
        "type": signal_type,
        "symbol": symbol,
        "price": price,
        "timestamp": int(time.time())
    }

    if volume:
        payload["volume"] = volume
    if price_change is not None:
        payload["price_change"] = price_change
    if oi_change is not None:
        payload["oi_change"] = oi_change

    try:
        response = REQUEST_SESSION.post(
            COINGLASS_WEBHOOK_URL,
            json=payload,
            timeout=5,
            verify=True
        )
        if response.status_code == 200:
            logger.info(f"✅ Сигнал {signal_type} для {symbol} отправлен в CoinGlass!")
        else:
            logger.warning(f"❌ Ошибка отправки в CoinGlass: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"⚠️ Ошибка при отправке в CoinGlass: {e}")


def get_binance_kline(symbol="BTCUSDT", interval="15", limit=2):
    """Получаем свечные данные с Binance API"""
    cache_key = f"kline_{symbol}_{interval}"
    current_time = time.time()

    # ИСПРАВЛЕНИЕ: защита кэша локом
    with cache_lock:
        if cache_key in data_cache:
            cached_data, timestamp = data_cache[cache_key]
            if current_time - timestamp < cache_expiry:
                return cached_data

    try:
        if not is_futures_pair_available(symbol):
            logger.debug(f"Пара {symbol} недоступна на фьючерсах, пропускаем")
            return None

        binance_interval = SUPPORTED_INTERVALS.get(interval, "15m")

        url = f"{BINANCE_API_URL}/fapi/v1/klines"
        params = {
            'symbol': symbol,
            'interval': binance_interval,
            'limit': limit
        }
        response = REQUEST_SESSION.get(url, params=params, timeout=10, verify=True)
        response.raise_for_status()
        data = response.json()

        with cache_lock:
            data_cache[cache_key] = (data, current_time)
        return data
    except Exception as e:
        logger.error(f"Binance kline error for {symbol}: {e}")
        return None


def get_binance_ticker(symbol="BTCUSDT"):
    """Получаем полные данные с Binance API"""
    cache_key = f"ticker_{symbol}"
    current_time = time.time()

    # ИСПРАВЛЕНИЕ: защита кэша локом
    with cache_lock:
        if cache_key in data_cache:
            cached_data, timestamp = data_cache[cache_key]
            if current_time - timestamp < cache_expiry:
                return cached_data

    try:
        if not is_futures_pair_available(symbol):
            logger.debug(f"Пара {symbol} недоступна на фьючерсах, пропускаем")
            return None

        ticker_response = REQUEST_SESSION.get(
            f"{BINANCE_API_URL}/fapi/v1/ticker/24hr?symbol={symbol}", timeout=10, verify=True
        )
        ticker_response.raise_for_status()
        ticker_data = ticker_response.json()

        oi_response = REQUEST_SESSION.get(
            f"{BINANCE_API_URL}/fapi/v1/openInterest?symbol={symbol}", timeout=10, verify=True
        )
        oi_response.raise_for_status()
        oi_data = oi_response.json()

        result = {
            'symbol': symbol,
            'price': float(ticker_data['lastPrice']),
            'price_change': float(ticker_data['priceChangePercent']),
            'volume': float(ticker_data['volume']),
            'quote_volume': float(ticker_data['quoteVolume']),
            'oi': float(oi_data['openInterest']),
            'timestamp': ticker_data['closeTime']
        }

        with cache_lock:
            data_cache[cache_key] = (result, current_time)
        return result
    except Exception as e:
        logger.error(f"Binance ticker error for {symbol}: {str(e)}")
        return None


def get_binance_liquidations(symbol="BTCUSDT", interval="15"):
    """
    Получаем ликвидации за последний интервал с фильтром по времени.
    ИСПРАВЛЕНО: startTime гарантирует что считаем только ликвидации
    за последний заданный интервал, а не произвольные последние ордера.
    """
    try:
        if not is_futures_pair_available(symbol):
            logger.debug(f"Пара {symbol} недоступна для запроса ликвидаций")
            return 0.0, 0.0, 0.0

        # Считаем ликвидации строго за последний интервал
        interval_minutes = int(interval)
        start_time_ms = int((time.time() - interval_minutes * 60) * 1000)

        response = REQUEST_SESSION.get(
            f"{BINANCE_API_URL}/fapi/v1/allForceOrders",
            params={
                'symbol': symbol,
                'startTime': start_time_ms,
                'limit': 100
            },
            timeout=3,
            verify=True
        )

        if response.status_code == 400:
            logger.debug(f"Пара {symbol} не поддерживает исторические данные о ликвидациях")
            return 0.0, 0.0, 0.0

        response.raise_for_status()
        liq_data = response.json()

        total_liq = 0.0
        long_liq = 0.0
        short_liq = 0.0

        if isinstance(liq_data, list):
            for order in liq_data:
                if order.get('status') == 'FILLED':
                    quantity = float(order.get('executedQty', 0))
                    price = float(order.get('avgPrice', 0))
                    value = quantity * price
                    # BUY = ликвидация шорта, SELL = ликвидация лонга
                    if order['side'] == 'BUY':
                        short_liq += value
                    elif order['side'] == 'SELL':
                        long_liq += value

        total_liq = long_liq + short_liq
        return total_liq, long_liq, short_liq

    except requests.exceptions.RequestException as e:
        if hasattr(e, 'response') and e.response is not None and e.response.status_code == 400:
            logger.debug(f"Пара {symbol} не поддерживает запрос ликвидаций: 400 Bad Request")
        else:
            logger.debug(f"Ошибка запроса ликвидаций для {symbol}: {str(e)}")
        return 0.0, 0.0, 0.0
    except Exception as e:
        logger.debug(f"Неизвестная ошибка при запросе ликвидаций для {symbol}: {str(e)}")
        return 0.0, 0.0, 0.0


def analyze_market(symbol, interval="15", enable_liq=False):
    try:
        binance_data = get_binance_ticker(symbol)
        if not binance_data:
            logger.debug(f"Нет данных для {symbol}")
            return None

        kline = get_binance_kline(symbol, interval, limit=2)
        if not kline or len(kline) < 2:
            logger.debug(f"Недостаточно данных свечей для {symbol}")
            return None

        latest_candle = kline[-1]
        prev_candle = kline[-2]
        current_price = binance_data['price']

        # ИСПРАВЛЕНО: сравниваем текущую цену с ЗАКРЫТИЕМ предыдущей свечи
        # (не с открытием текущей — это давало почти нулевое изменение)
        prev_close = float(prev_candle[4])  # индекс 4 = close price
        if prev_close == 0:
            price_change = 0.0
        else:
            price_change = ((current_price - prev_close) / prev_close * 100)

        latest_volume = float(latest_candle[5])
        prev_volume = float(prev_candle[5])
        if prev_volume == 0:
            volume_change = 0.0
        else:
            volume_change = ((latest_volume - prev_volume) / prev_volume * 100)

        total_liq, long_liq, short_liq = 0.0, 0.0, 0.0
        if enable_liq:
            total_liq, long_liq, short_liq = get_binance_liquidations(symbol, interval)

        return {
            'symbol': symbol,
            'price': current_price,
            'price_change': price_change,
            'volume': latest_volume,
            'volume_change': volume_change,
            'oi': binance_data['oi'],
            'total_liq': total_liq,
            'long_liq': long_liq,
            'short_liq': short_liq,
            'timestamp': binance_data['timestamp']
        }
    except Exception as e:
        logger.error(f"Analysis error for {symbol}: {str(e)}")
        return None


def get_all_futures_symbols():
    """Получаем все доступные фьючерсные пары с Binance"""
    available_pairs = get_available_futures_pairs()
    symbols = sorted(list(available_pairs))

    logger.info(f"Получено {len(symbols)} доступных торговых пар")

    if not symbols:
        logger.warning("Нет доступных пар, возвращаем основные пары")
        return ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"]

    return symbols


def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "📈 Цена BTC",
        "🔍 Анализ BTC",
        "🧠 Автосигналы",
        "📡 Статус мониторинга",
        "⚙ Настроить сигналы",
        "ℹ️ Помощь"
    ]
    keyboard.add(*buttons)
    return keyboard


def get_settings_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "💰 Порог цены",
        "📊 Порог объема",
        "📈 Порог OI",
        "⏱ Интервал анализа",
        "💧 Порог ликвидаций",
        "🔔 Вкл/Выкл Цену",
        "🔔 Вкл/Выкл Объем",
        "🔔 Вкл/Выкл OI",
        "🔔 Вкл/Выкл Ликвидации",
        "✅ Сохранить настройки",
        "↩️ Назад"
    ]
    keyboard.add(*buttons)
    return keyboard


@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    welcome_msg = (
        "🤖 Добро пожаловать в <b>Crypto Market Scanner Pro</b>!\n\n"
        "📊 <b>Возможности бота:</b>\n"
        "• Мониторинг ВСЕХ фьючерсных пар Binance\n"
        "• Настраиваемые сигналы по цене, объему, OI и ликвидациям\n"
        "• Параллельная обработка данных\n"
        "• Автоматический сброс счетчиков каждые 24 часа\n\n"
        "⚡️ <b>Быстрый старт:</b>\n"
        "1. Нажмите ⚙ Настроить сигналы\n"
        "2. Установите пороги срабатывания\n"
        "3. Сохраните настройки ✅\n"
        "4. Запустите 🧠 Автосигналы\n\n"
        "📈 <b>Рекомендуемые настройки:</b>\n"
        "• Цена: 1.5%\n• Объем: 50%\n• OI: 5%\n• Ликвидации: 1,000,000$\n"
        "• Интервал: 15 минут\n\n"
        "🔧 <b>Команды:</b>\n"
        "/start - Главное меню\n"
        "/debug - Отладка\n"
        "/test_conditions - Тест настроек\n"
        "/clear_cache - Очистить кэш\n"
        "/help - Помощь"
    )
    bot.send_message(chat_id, welcome_msg, parse_mode='HTML', reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "ℹ️ Помощь")
def help_btn(message):
    help_msg = (
        "🆘 <b>Помощь по использованию бота</b>\n\n"
        "🎯 <b>Как работает бот:</b>\n"
        "1. Собирает данные с Binance Futures\n"
        "2. Анализирует изменения цены, объема, OI и ликвидаций\n"
        "3. Сравнивает с вашими настройками\n"
        "4. Отправляет сигнал при срабатывании условий\n\n"
        "⚙️ <b>Настройки сигналов:</b>\n"
        "<b>💰 Цена:</b> Минимальное изменение цены за выбранный интервал\n"
        "<b>📊 Объем:</b> Минимальное изменение объема торгов\n"
        "<b>📈 OI:</b> Минимальное изменение открытого интереса\n"
        "<b>💧 Ликвидации:</b> Минимальная сумма ликвидаций\n"
        "<b>⏱ Интервал:</b> Период анализа данных\n\n"
        "📊 <b>Примеры настроек:</b>\n"
        "<b>Консервативные:</b> Цена 2%, Объем 80%, OI 8%, Ликв 2M$\n"
        "<b>Умеренные:</b> Цена 1.5%, Объем 50%, OI 5%, Ликв 1M$\n"
        "<b>Агрессивные:</b> Цена 1%, Объем 30%, OI 3%, Ликв 500K$\n\n"
        "🔧 <b>Команды для отладки:</b>\n"
        "/debug - Подробная информация о настройках и статусе\n"
        "/test_conditions - Проверить, как будут работать текущие настройки\n"
        "/clear_cache - Очистить кэш данных (если что-то пошло не так)\n\n"
        "⚠️ <b>Важно:</b>\n"
        "• Бот мониторит все доступные фьючерсные пары Binance\n"
        "• Сигнал не гарантирует прибыль, это лишь инструмент анализа\n"
        "• Используйте вместе с другими методами анализа\n"
        "• При первом запуске OI данные будут накапливаться 1-2 цикла"
    )
    bot.send_message(message.chat.id, help_msg, parse_mode='HTML')


@bot.message_handler(func=lambda msg: msg.text == "↩️ Назад")
def back_to_main(message):
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📈 Цена BTC")
def price_btn(message):
    data = get_binance_ticker("BTCUSDT")
    if data:
        update_time = datetime.fromtimestamp(data['timestamp'] / 1000).strftime('%H:%M:%S')
        total_liq, _, _ = get_binance_liquidations("BTCUSDT")
        liq_info = f"Ликвидации 24ч: {total_liq:,.0f}$" if total_liq > 0 else "Ликвидации: нет данных"

        msg = (
            f"💰 <b>BTC/USDT</b>\n\n"
            f"📊 <b>Текущие данные:</b>\n"
            f"• Цена: <b>{data['price']}$</b>\n"
            f"• Изменение 24ч: <b>{data['price_change']:+.2f}%</b>\n"
            f"• Объем 24ч: <b>{data['volume']:,.0f}$</b>\n"
            f"• Открытый интерес: <b>{data['oi']:,.0f}$</b>\n"
            f"• {liq_info}\n\n"
            f"🕒 Обновлено: {update_time} UTC"
        )
        bot.send_message(message.chat.id, msg, parse_mode='HTML')
    else:
        bot.send_message(message.chat.id, "❌ Не удалось получить данные для BTC")


@bot.message_handler(func=lambda msg: msg.text == "🔍 Анализ BTC")
def scan_btn(message):
    chat_id = message.chat.id
    interval = user_settings.get(chat_id, {}).get('interval', "15")
    enable_liq = user_settings.get(chat_id, {}).get('enable_liq', True)

    status_msg = bot.send_message(chat_id, "🔄 Анализирую BTC/USDT...")

    analysis = analyze_market("BTCUSDT", interval=interval, enable_liq=enable_liq)

    if analysis:
        update_time = datetime.fromtimestamp(analysis['timestamp'] / 1000).strftime('%H:%M:%S')

        if analysis['total_liq'] > 0:
            liq_info = (
                f"💧 <b>Ликвидации:</b>\n"
                f"• Всего: <b>{analysis['total_liq']:,.0f}$</b>\n"
                f"• Long: <b>{analysis['long_liq']:,.0f}$</b>\n"
                f"• Short: <b>{analysis['short_liq']:,.0f}$</b>\n"
            )
        else:
            liq_info = "💧 <b>Ликвидации:</b> Данные временно недоступны\n"

        interval_key = SUPPORTED_INTERVALS.get(interval, "15m")
        interval_name = INTERVAL_READABLE.get(interval_key, interval_key)

        price_trend = "📈" if analysis['price_change'] > 0 else "📉" if analysis['price_change'] < 0 else "➡️"
        volume_trend = "📈" if analysis['volume_change'] > 0 else "📉" if analysis['volume_change'] < 0 else "➡️"

        msg = (
            f"🔍 <b>BTC/USDT Анализ</b>\n\n"
            f"⏱ <b>Интервал:</b> {interval_name}\n\n"
            f"📊 <b>Показатели:</b>\n"
            f"{price_trend} Цена: <b>{analysis['price']:.2f}$</b> ({analysis['price_change']:+.2f}%)\n"
            f"{volume_trend} Объем: <b>{analysis['volume']:,.0f}$</b> ({analysis['volume_change']:+.2f}%)\n"
            f"📈 OI: <b>{analysis['oi']:,.0f}$</b>\n\n"
            f"{liq_info}\n"
            f"🕒 Время анализа: {update_time} UTC"
        )

        bot.edit_message_text(msg, chat_id, status_msg.message_id, parse_mode='HTML')
    else:
        bot.edit_message_text("❌ Не удалось получить данные для анализа BTC", chat_id, status_msg.message_id)


@bot.message_handler(func=lambda msg: msg.text == "⚙ Настроить сигналы")
def setup_signals(message):
    chat_id = message.chat.id
    if chat_id not in user_settings:
        user_settings[chat_id] = {
            'enable_price': True,
            'enable_volume': True,
            'enable_oi': True,
            'enable_liq': True,
            'price_threshold': 1.5,
            'volume_threshold': 50.0,
            'oi_threshold': 5.0,
            'liq_threshold': 1000000,
            'interval': "15"
        }

    settings = user_settings[chat_id]
    interval_key = SUPPORTED_INTERVALS.get(settings.get('interval', "15"), "15m")
    interval_name = INTERVAL_READABLE.get(interval_key, interval_key)

    current_settings = (
        f"⚙️ <b>Текущие настройки:</b>\n\n"
        f"🔔 Цена: {'✅ ВКЛ' if settings.get('enable_price', True) else '❌ ВЫКЛ'} | Порог: {settings.get('price_threshold', 1.5)}%\n"
        f"🔔 Объем: {'✅ ВКЛ' if settings.get('enable_volume', True) else '❌ ВЫКЛ'} | Порог: {settings.get('volume_threshold', 50.0)}%\n"
        f"🔔 OI: {'✅ ВКЛ' if settings.get('enable_oi', True) else '❌ ВЫКЛ'} | Порог: {settings.get('oi_threshold', 5.0)}%\n"
        f"🔔 Ликвидации: {'✅ ВКЛ' if settings.get('enable_liq', True) else '❌ ВЫКЛ'} | Порог: {settings.get('liq_threshold', 1000000):,.0f}$\n"
        f"⏱ Интервал: {interval_name}\n\n"
        f"Выберите параметр для изменения:"
    )

    bot.send_message(chat_id, current_settings, parse_mode='HTML', reply_markup=get_settings_keyboard())


@bot.message_handler(func=lambda msg: msg.text in [
    "💰 Порог цены",
    "📊 Порог объема",
    "📈 Порог OI",
    "⏱ Интервал анализа",
    "💧 Порог ликвидаций"
])
def setting_selection(message):
    chat_id = message.chat.id
    text = message.text

    settings_map = {
        "💰 Порог цены": ("price_threshold", "порог изменения цены (%)",
                         "• Пример: 1.5 - сигнал при росте ≥1.5%\n• Пример: -2.0 - сигнал при падении ≤-2.0%"),
        "📊 Порог объема": ("volume_threshold", "порог изменения объема (%)",
                           "• Пример: 50 - сигнал при росте объема ≥50%\n• Пример: -30 - сигнал при падении объема ≤-30%"),
        "📈 Порог OI": ("oi_threshold", "порог изменения открытого интереса (%)",
                       "• Пример: 5 - сигнал при росте OI ≥5%\n• Пример: -3 - сигнал при падении OI ≤-3%"),
        "⏱ Интервал анализа": ("interval", "интервал анализа",
                               "• Доступные значения: 1, 3, 5, 15, 30, 60, 120, 240, 360, 480, 720, 1440, 10080\n• Рекомендуется: 15 (15 минут)"),
        "💧 Порог ликвидаций": ("liq_threshold", "минимальную сумму ликвидаций в $",
                               "• Пример: 1000000 - сигнал при ликвидациях ≥1,000,000$\n• Рекомендуется: 1,000,000$"),
    }

    if text in settings_map:
        param, description, example = settings_map[text]
        user_states[chat_id] = {'setting': param}

        current_value = user_settings.get(chat_id, {}).get(param, "не установлен")
        if isinstance(current_value, float):
            current_value_str = f"{current_value:.2f}"
        elif isinstance(current_value, int) and param == 'liq_threshold':
            current_value_str = f"{current_value:,.0f}"
        else:
            current_value_str = str(current_value)

        msg = (
            f"⚙️ <b>Настройка параметра:</b> {text}\n\n"
            f"📋 <b>Текущее значение:</b> {current_value_str}\n\n"
            f"📝 <b>Описание:</b>\n{description}\n\n"
            f"💡 <b>Пример:</b>\n{example}\n\n"
            f"✍️ <b>Введите новое значение:</b>"
        )

        bot.send_message(chat_id, msg, parse_mode='HTML', reply_markup=types.ReplyKeyboardRemove())


@bot.message_handler(func=lambda msg: msg.text in [
    "🔔 Вкл/Выкл Цену",
    "🔔 Вкл/Выкл Объем",
    "🔔 Вкл/Выкл OI",
    "🔔 Вкл/Выкл Ликвидации"
])
def toggle_setting(message):
    chat_id = message.chat.id
    text = message.text

    toggle_map = {
        "🔔 Вкл/Выкл Цену": "enable_price",
        "🔔 Вкл/Выкл Объем": "enable_volume",
        "🔔 Вкл/Выкл OI": "enable_oi",
        "🔔 Вкл/Выкл Ликвидации": "enable_liq"
    }

    setting_key = toggle_map[text]
    setting_name = text.split()[-1].lower()

    current_state = user_settings.get(chat_id, {}).get(setting_key, True)
    user_settings[chat_id][setting_key] = not current_state

    new_state = user_settings[chat_id][setting_key]
    # ИСПРАВЛЕНИЕ: переименована переменная чтобы не конфликтовала с внешней 'status'
    toggle_status = "✅ ВКЛЮЧЕНО" if new_state else "❌ ВЫКЛЮЧЕНО"

    settings = user_settings[chat_id]
    interval_key = SUPPORTED_INTERVALS.get(settings.get('interval', "15"), "15m")
    interval_name = INTERVAL_READABLE.get(interval_key, interval_key)

    current_settings = (
        f"⚙️ <b>Текущие настройки:</b>\n\n"
        f"🔔 Цена: {'✅ ВКЛ' if settings.get('enable_price', True) else '❌ ВЫКЛ'} | Порог: {settings.get('price_threshold', 1.5)}%\n"
        f"🔔 Объем: {'✅ ВКЛ' if settings.get('enable_volume', True) else '❌ ВЫКЛ'} | Порог: {settings.get('volume_threshold', 50.0)}%\n"
        f"🔔 OI: {'✅ ВКЛ' if settings.get('enable_oi', True) else '❌ ВЫКЛ'} | Порог: {settings.get('oi_threshold', 5.0)}%\n"
        f"🔔 Ликвидации: {'✅ ВКЛ' if settings.get('enable_liq', True) else '❌ ВЫКЛ'} | Порог: {settings.get('liq_threshold', 1000000):,.0f}$\n"
        f"⏱ Интервал: {interval_name}\n\n"
        f"✅ Параметр '{setting_name}' теперь {toggle_status}\n"
        f"Выберите следующий параметр для изменения:"
    )

    bot.send_message(chat_id, current_settings, parse_mode='HTML', reply_markup=get_settings_keyboard())


@bot.message_handler(func=lambda msg: msg.chat.id in user_states and 'setting' in user_states[msg.chat.id])
def handle_setting_input(message):
    chat_id = message.chat.id
    setting = user_states[chat_id]['setting']
    value_str = message.text

    try:
        if setting == 'interval':
            if value_str not in SUPPORTED_INTERVALS:
                allowed = ", ".join(SUPPORTED_INTERVALS.keys())
                bot.send_message(chat_id, f"❌ Ошибка! Неподдерживаемый интервал. Допустимые значения: {allowed}")
                return
            value = value_str
        elif setting == 'liq_threshold':
            value_str_clean = value_str.replace(',', '').replace(' ', '')
            value = float(value_str_clean)
        else:
            value = float(value_str)

        if chat_id not in user_settings:
            user_settings[chat_id] = {}
        user_settings[chat_id][setting] = value

        del user_states[chat_id]

        settings = user_settings[chat_id]
        interval_key = SUPPORTED_INTERVALS.get(settings.get('interval', "15"), "15m")
        interval_name = INTERVAL_READABLE.get(interval_key, interval_key)

        confirmation_msg = (
            f"✅ <b>Значение сохранено!</b>\n\n"
            f"⚙️ <b>Текущие настройки:</b>\n\n"
            f"🔔 Цена: {'✅ ВКЛ' if settings.get('enable_price', True) else '❌ ВЫКЛ'} | Порог: {settings.get('price_threshold', 1.5)}%\n"
            f"🔔 Объем: {'✅ ВКЛ' if settings.get('enable_volume', True) else '❌ ВЫКЛ'} | Порог: {settings.get('volume_threshold', 50.0)}%\n"
            f"🔔 OI: {'✅ ВКЛ' if settings.get('enable_oi', True) else '❌ ВЫКЛ'} | Порог: {settings.get('oi_threshold', 5.0)}%\n"
            f"🔔 Ликвидации: {'✅ ВКЛ' if settings.get('enable_liq', True) else '❌ ВЫКЛ'} | Порог: {settings.get('liq_threshold', 1000000):,.0f}$\n"
            f"⏱ Интервал: {interval_name}\n\n"
            f"Выберите следующий параметр или нажмите ✅ Сохранить настройки:"
        )

        bot.send_message(chat_id, confirmation_msg, parse_mode='HTML', reply_markup=get_settings_keyboard())

    except ValueError:
        bot.send_message(chat_id, "❌ Ошибка! Введите корректное число. Попробуйте еще раз:")


@bot.message_handler(func=lambda msg: msg.text == "✅ Сохранить настройки")
def save_settings(message):
    chat_id = message.chat.id
    if chat_id not in user_settings:
        bot.send_message(chat_id, "❌ Нет настроек для сохранения. Сначала настройте параметры.")
        return

    settings = user_settings[chat_id]

    interval_key = SUPPORTED_INTERVALS.get(settings.get('interval', "15"), "15m")
    interval_name = INTERVAL_READABLE.get(interval_key, interval_key)

    settings_message = (
        "🎉 <b>Все настройки сохранены!</b>\n\n"
        "⚙️ <b>Ваши текущие настройки:</b>\n\n"
        f"🔔 Отслеживание цены: {'✅ ВКЛ' if settings.get('enable_price', True) else '❌ ВЫКЛ'}\n"
        f"🔔 Отслеживание объема: {'✅ ВКЛ' if settings.get('enable_volume', True) else '❌ ВЫКЛ'}\n"
        f"🔔 Отслеживание OI: {'✅ ВКЛ' if settings.get('enable_oi', True) else '❌ ВЫКЛ'}\n"
        f"🔔 Отслеживание ликвидаций: {'✅ ВКЛ' if settings.get('enable_liq', True) else '❌ ВЫКЛ'}\n\n"
        f"💰 Порог цены: <b>{settings.get('price_threshold', 1.5)}%</b>\n"
        f"📊 Порог объема: <b>{settings.get('volume_threshold', 50.0)}%</b>\n"
        f"📈 Порог OI: <b>{settings.get('oi_threshold', 5.0)}%</b>\n"
        f"💧 Порог ликвидаций: <b>{settings.get('liq_threshold', 1000000):,.0f}$</b>\n"
        f"⏱ Интервал анализа: <b>{interval_name}</b>\n\n"
        "🚀 <b>Что дальше:</b>\n"
        "1. Нажмите 🧠 Автосигналы для запуска мониторинга\n"
        "2. Используйте /test_conditions для проверки настроек\n"
        "3. Отслеживайте прогресс через 📡 Статус мониторинга\n"
        "4. При необходимости настройте параметры заново"
    )

    bot.send_message(chat_id, settings_message, parse_mode='HTML', reply_markup=get_main_keyboard())


def stop_monitor(chat_id):
    """Останавливает мониторинг для указанного чата"""
    if chat_id in active_monitors:
        active_monitors[chat_id] = False
        with lock:
            if chat_id in monitoring_progress:
                monitoring_progress[chat_id]['status'] = "stopping"
        logger.info(f"Мониторинг останавливается для чата {chat_id}")
        # ИСПРАВЛЕНИЕ: убран time.sleep(3) из основного потока — поток сам обновит статус
    else:
        logger.info(f"Для чата {chat_id} нет активного мониторинга")


def reset_daily_counters(chat_id):
    """Сбрасывает счетчики сигналов каждые 24 часа"""
    current_time = time.time()
    last_reset = daily_counters_reset.get(chat_id, 0)

    if current_time - last_reset >= 86400:
        with lock:
            keys_to_delete = [key for key in signal_counters.keys() if key[0] == chat_id]
            for key in keys_to_delete:
                del signal_counters[key]

        daily_counters_reset[chat_id] = current_time
        logger.info(f"Счетчики сигналов сброшены для чата {chat_id}")


def get_monitoring_progress(chat_id):
    """Возвращает прогресс мониторинга для чата"""
    if chat_id not in monitoring_progress:
        return "🛑 Мониторинг не активен"

    progress = monitoring_progress[chat_id]

    if progress.get('status') == "stopped":
        return "🛑 Мониторинг остановлен"
    elif progress.get('status') == "stopping":
        return "⏳ Мониторинг останавливается..."

    total_coins = progress.get('total_coins', 0)
    scanned_coins = progress.get('scanned_coins', 0)

    if total_coins == 0:
        return "🔄 Инициализация мониторинга..."

    progress_percent = (scanned_coins / total_coins) * 100

    start_time = progress.get('start_time', time.time())
    elapsed = int(time.time() - start_time)
    elapsed_min = elapsed // 60
    elapsed_sec = elapsed % 60

    if scanned_coins > 0:
        remaining = (elapsed / scanned_coins) * (total_coins - scanned_coins)
        remaining_min = int(remaining // 60)
        remaining_sec = int(remaining % 60)
    else:
        remaining_min = 0
        remaining_sec = 0

    bar_length = 10
    filled = int(progress_percent / (100 / bar_length))
    progress_bar = "🟩" * filled + "⬜️" * (bar_length - filled)

    return (
        f"📡 <b>Статус мониторинга: АКТИВЕН</b>\n\n"
        f"⏱ Время работы: {elapsed_min} мин {elapsed_sec} сек\n"
        f"🔢 Прогресс: {scanned_coins}/{total_coins} монет\n"
        f"📊 Завершено: {progress_percent:.1f}%\n"
        f"{progress_bar}\n\n"
        f"⏳ Осталось: ~{remaining_min} мин {remaining_sec} сек\n"
        f"🔄 Следующая проверка: через {int(progress.get('next_check', 0))} сек"
    )


@bot.message_handler(func=lambda msg: msg.text == "📡 Статус мониторинга")
def status_btn(message):
    chat_id = message.chat.id
    status_msg = get_monitoring_progress(chat_id)
    bot.send_message(chat_id, status_msg, parse_mode='HTML')


def check_signal_conditions(analysis, settings, chat_id, symbol):
    """Проверяет условия для сигнала"""
    price_change = analysis['price_change']
    volume_change = analysis['volume_change']
    oi = analysis['oi']
    total_liq = analysis['total_liq']

    prev_oi_key = f"{chat_id}_{symbol}"

    # ИСПРАВЛЕНИЕ: защита OI данных отдельным локом
    with oi_lock:
        if prev_oi_key not in prev_oi_data:
            prev_oi_data[prev_oi_key] = {
                'value': oi,
                'first_measurement': True
            }
            oi_change_pct = 0.0
        else:
            prev_data = prev_oi_data[prev_oi_key]
            prev_oi_value = prev_data['value']

            if prev_oi_value == 0:
                oi_change_pct = 0.0
            else:
                oi_change_pct = ((oi - prev_oi_value) / prev_oi_value) * 100

            if not prev_data['first_measurement']:
                prev_oi_data[prev_oi_key]['value'] = oi
            else:
                prev_oi_data[prev_oi_key]['first_measurement'] = False
                prev_oi_data[prev_oi_key]['value'] = oi

    enable_price = settings.get('enable_price', True)
    enable_volume = settings.get('enable_volume', True)
    enable_oi = settings.get('enable_oi', True)
    enable_liq = settings.get('enable_liq', True)

    price_threshold = settings.get('price_threshold', 1.5)
    volume_threshold = settings.get('volume_threshold', 50.0)
    oi_threshold = settings.get('oi_threshold', 5.0)
    liq_threshold = settings.get('liq_threshold', 1000000)

    price_match = False
    volume_match = False
    oi_match = False
    liq_match = False

    if enable_price:
        if price_threshold >= 0:
            price_match = price_change >= price_threshold
        else:
            price_match = price_change <= price_threshold

    if enable_volume:
        if volume_threshold >= 0:
            volume_match = volume_change >= volume_threshold
        else:
            volume_match = volume_change <= volume_threshold

    if enable_oi:
        if oi_threshold >= 0:
            oi_match = oi_change_pct >= oi_threshold
        else:
            oi_match = oi_change_pct <= oi_threshold

    if enable_liq:
        liq_match = total_liq >= liq_threshold

    return {
        'price_match': price_match,
        'volume_match': volume_match,
        'oi_match': oi_match,
        'liq_match': liq_match,
        'oi_change_pct': oi_change_pct,
        'price_change': price_change,
        'volume_change': volume_change,
        'total_liq': total_liq
    }


@bot.message_handler(func=lambda msg: msg.text == "🧠 Автосигналы")
def start_auto_signals(message):
    chat_id = message.chat.id
    settings = user_settings.get(chat_id, {})

    logger.info(f"Запуск автосигналов для чата {chat_id} с настройками: {settings}")

    if chat_id in active_monitors and active_monitors[chat_id]:
        stop_monitor(chat_id)
        # ИСПРАВЛЕНИЕ: ждем завершения потока без блокировки основного потока
        timeout = 10
        while chat_id in active_monitors and timeout > 0:
            time.sleep(1)
            timeout -= 1

    all_symbols = get_all_futures_symbols()
    if not all_symbols:
        bot.send_message(chat_id, "❌ Не удалось получить список торговых пар. Мониторинг не запущен.")
        return

    logger.info(f"Найдено {len(all_symbols)} торговых пар для мониторинга")

    priority_pairs = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
    sorted_symbols = []

    for pair in priority_pairs:
        if pair in all_symbols:
            sorted_symbols.append(pair)
            all_symbols.remove(pair)

    sorted_symbols.extend(all_symbols)

    active_monitors[chat_id] = True
    reset_daily_counters(chat_id)

    monitoring_progress[chat_id] = {
        'status': 'active',
        'start_time': time.time(),
        'scanned_coins': 0,
        'total_coins': len(sorted_symbols),
        'next_check': 0,
        'last_cycle_time': 0
    }

    status_msg = bot.send_message(chat_id, "🔄 Запуск мониторинга...")
    time.sleep(1)
    bot.edit_message_text("🔄 Запуск мониторинга... 🔄", chat_id, status_msg.message_id)
    time.sleep(1)
    bot.edit_message_text("🔄 Запуск мониторинга... 🔄 ✅", chat_id, status_msg.message_id)
    time.sleep(1)

    bot.send_message(
        chat_id,
        f"🚀 <b>Мониторинг запущен!</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Мониторится: {len(sorted_symbols)} торговых пар\n"
        f"• Интервал: {INTERVAL_READABLE.get(SUPPORTED_INTERVALS.get(settings.get('interval', '15'), '15m'), '15 минут')}\n"
        f"• Включенные параметры: {sum([settings.get('enable_price', True), settings.get('enable_volume', True), settings.get('enable_oi', True), settings.get('enable_liq', True)])}/4\n\n"
        f"📈 <b>Что делать дальше:</b>\n"
        f"1. Отслеживайте прогресс через 📡 Статус мониторинга\n"
        f"2. Вы получите уведомления при срабатывании условий\n"
        f"3. Используйте /stop для остановки мониторинга\n"
        f"4. Настройте параметры если нужно больше/меньше сигналов",
        parse_mode='HTML'
    )

    def monitor():
        last_signal_time = {}
        last_request_time = 0
        enable_liq = settings.get('enable_liq', True)

        logger.info(f"Начало мониторинга для чата {chat_id}")

        while active_monitors.get(chat_id, False):
            try:
                start_cycle_time = time.time()
                with lock:
                    monitoring_progress[chat_id]['scanned_coins'] = 0
                    monitoring_progress[chat_id]['last_cycle_time'] = start_cycle_time

                reset_daily_counters(chat_id)

                current_time = time.time()
                if current_time - last_request_time < 1:
                    sleep_time = 1 - (current_time - last_request_time)
                    time.sleep(sleep_time)

                last_request_time = time.time()
                logger.info(f"Сканирование рынка для чата {chat_id} - {len(sorted_symbols)} пар")

                interval_val = settings.get('interval', "15")
                signals_to_send = []

                num_symbols = len(sorted_symbols)
                if num_symbols < 50:
                    max_workers = 5
                elif num_symbols < 100:
                    max_workers = 10
                else:
                    max_workers = 20

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_symbol = {}

                    for symbol in sorted_symbols:
                        if not active_monitors.get(chat_id, False):
                            break

                        future = executor.submit(analyze_market, symbol, interval_val, enable_liq)
                        future_to_symbol[future] = symbol

                    for future in as_completed(future_to_symbol):
                        if not active_monitors.get(chat_id, False):
                            executor.shutdown(wait=False, cancel_futures=True)
                            break

                        symbol = future_to_symbol[future]
                        try:
                            analysis = future.result()
                            with lock:
                                monitoring_progress[chat_id]['scanned_coins'] += 1

                            if not analysis:
                                continue

                            conditions = check_signal_conditions(analysis, settings, chat_id, symbol)

                            if (conditions['price_match'] or conditions['volume_match'] or
                                    conditions['oi_match'] or conditions['liq_match']):
                                logger.info(f"Найден сигнал для {symbol}: "
                                            f"цена={conditions['price_match']}, "
                                            f"объем={conditions['volume_match']}, "
                                            f"OI={conditions['oi_match']}, "
                                            f"ликв={conditions['liq_match']}")

                                signals_to_send.append({
                                    'symbol': symbol,
                                    'analysis': analysis,
                                    'conditions': conditions,
                                    'interval_val': interval_val
                                })

                        except Exception as e:
                            logger.error(f"Ошибка при анализе {symbol}: {e}")

                for signal_data in signals_to_send:
                    symbol = signal_data['symbol']
                    analysis = signal_data['analysis']
                    conditions = signal_data['conditions']

                    price = analysis['price']
                    price_change = analysis['price_change']
                    volume = analysis['volume']
                    volume_change = analysis['volume_change']
                    oi = analysis['oi']
                    total_liq = analysis['total_liq']
                    update_time = datetime.fromtimestamp(analysis['timestamp'] / 1000).strftime('%H:%M:%S')

                    current_time = time.time()
                    symbol_key = f"{chat_id}_{symbol}"

                    if symbol_key in last_signal_time and current_time - last_signal_time[symbol_key] < 300:
                        logger.info(f"Сигнал для {symbol} уже был, пропускаем")
                        continue

                    last_signal_time[symbol_key] = current_time

                    with lock:
                        count = signal_counters.get((chat_id, symbol), 0) + 1
                        signal_counters[(chat_id, symbol)] = count

                    interval_key = SUPPORTED_INTERVALS.get(signal_data['interval_val'], "15m")
                    readable_interval = INTERVAL_READABLE.get(interval_key, interval_key)

                    msg_lines = [f"🚨 <b>Сигнал #{count} по {symbol}!</b>\n"]

                    triggers = []
                    if conditions['price_match']:
                        direction = "📈 РОСТ" if price_change > 0 else "📉 ПАДЕНИЕ"
                        triggers.append(f"💰 Цена: {direction} {abs(price_change):.2f}%")
                    if conditions['volume_match']:
                        direction = "📈 РОСТ" if volume_change > 0 else "📉 ПАДЕНИЕ"
                        triggers.append(f"📊 Объем: {direction} {abs(volume_change):.2f}%")
                    if conditions['oi_match']:
                        direction = "📈 РОСТ" if conditions['oi_change_pct'] > 0 else "📉 ПАДЕНИЕ"
                        triggers.append(f"📈 OI: {direction} {abs(conditions['oi_change_pct']):.2f}%")
                    if conditions['liq_match']:
                        triggers.append(f"💧 Ликвидации: {total_liq:,.0f}$")

                    if triggers:
                        msg_lines.append("✅ <b>Сработало:</b> " + ", ".join(triggers))

                    msg_lines.append("")

                    msg_lines.append(f"💰 <b>Цена:</b> {price:.6f}$ ({price_change:+.2f}%)")
                    msg_lines.append(f"📊 <b>Объем:</b> {volume:,.0f}$ ({volume_change:+.2f}%)")
                    msg_lines.append(f"📈 <b>OI:</b> {oi:,.0f}$ ({conditions['oi_change_pct']:+.2f}%)")

                    if total_liq > 0:
                        msg_lines.append(f"💧 <b>Ликвидации:</b> {total_liq:,.0f}$")

                    msg_lines.append(f"⏱ <b>Интервал:</b> {readable_interval}")
                    msg_lines.append(f"🕒 <b>Время:</b> {update_time} UTC")

                    coinglass_url = f"https://www.coinglass.com/tv/Binance_{symbol}"
                    msg_lines.append(f"🔗 <b>Подробнее:</b> {coinglass_url}")

                    msg = "\n".join(msg_lines)

                    logger.info(f"Отправка сигнала для {symbol} в чат {chat_id}")
                    bot.send_message(chat_id, msg, parse_mode='HTML')

                    if conditions['price_match'] and settings.get('enable_price', True):
                        send_coin_glass_signal(
                            signal_type="entry",
                            symbol=symbol,
                            price=price,
                            price_change=price_change
                        )

                    if conditions['liq_match'] and settings.get('enable_liq', True):
                        send_coin_glass_signal(
                            signal_type="liquidation",
                            symbol=symbol,
                            price=price,
                            volume=total_liq
                        )

                cycle_time = time.time() - start_cycle_time

                base_sleep = 300 if enable_liq else 180
                sleep_time = max(30, base_sleep - cycle_time)

                with lock:
                    monitoring_progress[chat_id]['next_check'] = sleep_time

                logger.info(f"Цикл завершен за {cycle_time:.1f} сек, спим {sleep_time:.1f} сек")

                sleep_interval = 5
                for _ in range(int(sleep_time / sleep_interval)):
                    if not active_monitors.get(chat_id, False):
                        break
                    time.sleep(sleep_interval)

            except Exception as e:
                logger.error(f"Ошибка в мониторинге: {e}", exc_info=True)
                if active_monitors.get(chat_id, False):
                    time.sleep(30)

        # ИСПРАВЛЕНИЕ: поток сам обновляет статус и удаляет себя из active_monitors
        with lock:
            monitoring_progress[chat_id]['status'] = "stopped"
        if chat_id in active_monitors:
            del active_monitors[chat_id]

        logger.info(f"Мониторинг остановлен для чата {chat_id}")

    t = threading.Thread(target=monitor, daemon=True)
    t.start()
    logger.info(f"Мониторинг запущен в потоке для чата {chat_id}")


@bot.message_handler(commands=['stop'])
def stop_command(message):
    chat_id = message.chat.id
    stop_monitor(chat_id)
    bot.send_message(chat_id, "🛑 Остановка мониторинга... Подождите несколько секунд.", reply_markup=get_main_keyboard())


@bot.message_handler(commands=['status'])
def status_command(message):
    chat_id = message.chat.id
    status_msg = get_monitoring_progress(chat_id)
    bot.send_message(chat_id, status_msg, parse_mode='HTML')


@bot.message_handler(commands=['debug'])
def debug_command(message):
    chat_id = message.chat.id
    # ИСПРАВЛЕНИЕ: переименована переменная чтобы не конфликтовала с внешней 'status'
    monitor_active = active_monitors.get(chat_id, False)
    monitor_status = "активен" if monitor_active else "не активен"

    settings = user_settings.get(chat_id, {})

    with lock:
        user_counters = [(symbol, count) for (cid, symbol), count in signal_counters.items() if cid == chat_id]

    if user_counters:
        counters_info = "\n".join([f"• {symbol}: {count} сигналов" for symbol, count in user_counters[:10]])
        if len(user_counters) > 10:
            counters_info += f"\n• ... и еще {len(user_counters) - 10} пар"
    else:
        counters_info = "Нет активных счетчиков"

    last_reset = daily_counters_reset.get(chat_id, 0)
    last_reset_time = datetime.fromtimestamp(last_reset).strftime('%Y-%m-%d %H:%M:%S') if last_reset else "Никогда"

    all_symbols = get_all_futures_symbols()

    with oi_lock:
        oi_keys = [k for k in prev_oi_data.keys() if k.startswith(f"{chat_id}_")]
        oi_count = len(oi_keys)

    settings_info = []
    for key, value in settings.items():
        if key == 'interval':
            interval_name = INTERVAL_READABLE.get(SUPPORTED_INTERVALS.get(value, "15m"), "15m")
            settings_info.append(f"• {key}: {value} ({interval_name})")
        elif key in ['price_threshold', 'volume_threshold', 'oi_threshold']:
            settings_info.append(f"• {key}: {value}%")
        elif key == 'liq_threshold':
            settings_info.append(f"• {key}: {value:,.0f}$")
        elif key in ['enable_price', 'enable_volume', 'enable_oi', 'enable_liq']:
            s = "ВКЛ" if value else "ВЫКЛ"
            settings_info.append(f"• {key}: {s}")
        else:
            settings_info.append(f"• {key}: {value}")

    settings_str = "\n".join(settings_info) if settings_info else "Настройки не установлены"

    msg = (
        f"🔧 <b>ДЕБАГ ИНФОРМАЦИЯ</b>\n\n"
        f"📊 <b>Основное:</b>\n"
        f"• Статус мониторинга: {monitor_status}\n"
        f"• Найдено пар на Binance: {len(all_symbols)}\n"
        f"• Активные мониторинги: {len([k for k, v in active_monitors.items() if v])}\n\n"
        f"⚙️ <b>Настройки:</b>\n{settings_str}\n\n"
        f"📈 <b>Счетчики сигналов:</b>\n{counters_info}\n\n"
        f"🔄 <b>Сбросы:</b>\n"
        f"• Последний сброс счетчиков: {last_reset_time}\n"
        f"• OI записей в кэше: {oi_count}\n\n"
        f"📡 <b>Статус мониторинга:</b>\n{get_monitoring_progress(chat_id)}"
    )
    bot.send_message(chat_id, msg, parse_mode='HTML')


@bot.message_handler(commands=['test_conditions'])
def test_conditions_command(message):
    chat_id = message.chat.id

    if chat_id not in user_settings:
        bot.send_message(chat_id, "❌ Настройки не найдены. Сначала настройте параметры.")
        return

    settings = user_settings[chat_id]

    test_scenarios = [
        {'name': '📈 Рост цены 2%', 'price_change': 2.0, 'volume_change': 20.0, 'oi_change': 2.0, 'liq_amount': 200000},
        {'name': '📉 Падение цены 2%', 'price_change': -2.0, 'volume_change': -40.0, 'oi_change': -4.0, 'liq_amount': 800000},
        {'name': '🚀 Большой объем 80%', 'price_change': 0.3, 'volume_change': 80.0, 'oi_change': 1.0, 'liq_amount': 300000},
        {'name': '💧 Ликвидации 1.2M$', 'price_change': 0.1, 'volume_change': 10.0, 'oi_change': 0.5, 'liq_amount': 1200000},
        {'name': '📈 Рост OI 6%', 'price_change': 0.2, 'volume_change': 15.0, 'oi_change': 6.0, 'liq_amount': 100000}
    ]

    results = []

    for scenario in test_scenarios:
        price_match = False
        volume_match = False
        oi_match = False
        liq_match = False

        if settings.get('enable_price', True):
            price_threshold = settings.get('price_threshold', 1.5)
            price_match = scenario['price_change'] >= price_threshold if price_threshold >= 0 else scenario['price_change'] <= price_threshold

        if settings.get('enable_volume', True):
            volume_threshold = settings.get('volume_threshold', 50.0)
            volume_match = scenario['volume_change'] >= volume_threshold if volume_threshold >= 0 else scenario['volume_change'] <= volume_threshold

        if settings.get('enable_oi', True):
            oi_threshold = settings.get('oi_threshold', 5.0)
            oi_match = scenario['oi_change'] >= oi_threshold if oi_threshold >= 0 else scenario['oi_change'] <= oi_threshold

        if settings.get('enable_liq', True):
            liq_match = scenario['liq_amount'] >= settings.get('liq_threshold', 1000000)

        will_trigger = price_match or volume_match or oi_match or liq_match
        trigger_emoji = "✅" if will_trigger else "❌"

        results.append(
            f"{trigger_emoji} <b>{scenario['name']}</b>\n"
            f"   Цена: {scenario['price_change']:+.1f}% ({'✅ Сработает' if price_match else '❌ Нет'})\n"
            f"   Объем: {scenario['volume_change']:+.1f}% ({'✅ Сработает' if volume_match else '❌ Нет'})\n"
            f"   OI: {scenario['oi_change']:+.1f}% ({'✅ Сработает' if oi_match else '❌ Нет'})\n"
            f"   Ликв.: {scenario['liq_amount']:,.0f}$ ({'✅ Сработает' if liq_match else '❌ Нет'})\n"
        )

    summary = "\n".join(results)
    current_settings = (
        f"<b>ТЕКУЩИЕ НАСТРОЙКИ:</b>\n"
        f"💰 Цена: {settings.get('price_threshold', 1.5)}% ({'✅ ВКЛ' if settings.get('enable_price', True) else '❌ ВЫКЛ'})\n"
        f"📊 Объем: {settings.get('volume_threshold', 50.0)}% ({'✅ ВКЛ' if settings.get('enable_volume', True) else '❌ ВЫКЛ'})\n"
        f"📈 OI: {settings.get('oi_threshold', 5.0)}% ({'✅ ВКЛ' if settings.get('enable_oi', True) else '❌ ВЫКЛ'})\n"
        f"💧 Ликвидации: {settings.get('liq_threshold', 1000000):,.0f}$ ({'✅ ВКЛ' if settings.get('enable_liq', True) else '❌ ВЫКЛ'})\n"
    )

    final_msg = (
        f"🧪 <b>ТЕСТ УСЛОВИЙ СИГНАЛОВ</b>\n\n"
        f"{current_settings}\n"
        f"<b>РЕЗУЛЬТАТЫ ТЕСТОВ:</b>\n"
        f"{summary}\n\n"
        f"<i>✅ - сигнал будет отправлен\n❌ - сигнал НЕ будет отправлен</i>"
    )

    bot.send_message(chat_id, final_msg, parse_mode='HTML')


@bot.message_handler(commands=['clear_cache'])
def clear_cache_command(message):
    chat_id = message.chat.id

    with oi_lock:
        keys_to_delete = [key for key in prev_oi_data.keys() if key.startswith(f"{chat_id}_")]
        for key in keys_to_delete:
            del prev_oi_data[key]

    with cache_lock:
        data_cache.clear()

    with lock:
        user_counters = [k for k in signal_counters.keys() if k[0] == chat_id]
        for key in user_counters:
            del signal_counters[key]

    count_oi = len(keys_to_delete)
    count_counters = len(user_counters)
    bot.send_message(chat_id, f"✅ Кэш очищен! Удалено {count_oi} записей OI данных и {count_counters} счетчиков.")


@bot.message_handler(commands=['help'])
def help_command(message):
    help_msg = (
        "🆘 <b>СПРАВКА ПО КОМАНДАМ</b>\n\n"
        "🎯 <b>Основные команды:</b>\n"
        "/start - Главное меню и информация\n"
        "/help - Эта справка\n\n"
        "⚙️ <b>Настройки и управление:</b>\n"
        "/stop - Остановить мониторинг\n"
        "/status - Статус мониторинга\n"
        "/debug - Подробная информация\n"
        "/test_conditions - Тест текущих настроек\n"
        "/clear_cache - Очистить кэш данных\n\n"
        "📊 <b>Основные кнопки:</b>\n"
        "📈 Цена BTC - Текущая цена BTC\n"
        "🔍 Анализ BTC - Подробный анализ BTC\n"
        "🧠 Автосигналы - Запуск мониторинга\n"
        "📡 Статус мониторинга - Прогресс сканирования\n"
        "⚙ Настроить сигналы - Настройка параметров\n"
        "ℹ️ Помощь - Информация о боте\n\n"
        "💡 <b>Советы:</b>\n"
        "1. Начните с умеренных настроек\n"
        "2. Используйте /test_conditions перед запуском\n"
        "3. Если много сигналов - увеличьте пороги\n"
        "4. Если мало сигналов - уменьшите пороги"
    )
    bot.send_message(message.chat.id, help_msg, parse_mode='HTML')


if __name__ == "__main__":
    logger.info("🚀 Бот запускается...")

    try:
        logger.info("📋 Загружаю список доступных фьючерсных пар...")
        available_pairs = get_available_futures_pairs(force_update=True)
        logger.info(f"✅ Загружено {len(available_pairs)} доступных пар")

        logger.info("🤖 Бот готов к работе!")
        logger.info("📱 Используйте /start для начала работы")

        bot.infinity_polling(timeout=60, long_polling_timeout=60)

    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем (Ctrl+C)")
        for chat_id in list(active_monitors.keys()):
            stop_monitor(chat_id)
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}", exc_info=True)
        logger.info("🔄 Попытка перезапуска через 10 секунд...")
        time.sleep(10)

        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e2:
            logger.error(f"💥 Повторная критическая ошибка: {e2}")
            logger.error("❌ Бот остановлен. Требуется ручной перезапуск.")
