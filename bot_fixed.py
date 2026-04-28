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
BYBIT_API_URL = "https://api.bybit.com"
COINGLASS_WEBHOOK_URL = os.getenv("COINGLASS_WEBHOOK_URL")

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env файле")

# Кэш доступных фьючерсных пар
AVAILABLE_FUTURES_PAIRS = set()
AVAILABLE_BYBIT_PAIRS = set()
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
cache_lock = threading.Lock()
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
    """Получает и кэширует список доступных фьючерсных пар (Binance)"""
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

        logger.info(f"Обновлен список доступных фьючерсных пар Binance: {len(available_pairs)} пар")
        return available_pairs
    except Exception as e:
        logger.error(f"Ошибка при получении пар Binance: {e}")
        return AVAILABLE_FUTURES_PAIRS if AVAILABLE_FUTURES_PAIRS else set()


def get_available_bybit_pairs():
    """Получает список доступных пар Bybit"""
    global AVAILABLE_BYBIT_PAIRS
    try:
        url = f"{BYBIT_API_URL}/v5/market/instruments-info?category=linear"
        response = REQUEST_SESSION.get(url, timeout=15)
        data = response.json()
        if data.get('retCode') == 0:
            pairs = {item['symbol'] for item in data['result']['list'] if item['quoteCoin'] == 'USDT'}
            AVAILABLE_BYBIT_PAIRS = pairs
            return pairs
    except Exception as e:
        logger.error(f"Ошибка Bybit pairs: {e}")
    return AVAILABLE_BYBIT_PAIRS


def is_futures_pair_available(symbol, exchange='Binance'):
    """Проверяет, доступна ли пара на фьючерсах"""
    if exchange == 'Bybit':
        available = get_available_bybit_pairs()
    else:
        available = get_available_futures_pairs()
    return symbol in available


def send_coin_glass_signal(signal_type: str, symbol: str, price: float, volume: float = None,
                           price_change: float = None, oi_change: float = None):
    if not COINGLASS_WEBHOOK_URL:
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
        REQUEST_SESSION.post(COINGLASS_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"⚠️ Ошибка CoinGlass: {e}")


def get_binance_kline(symbol="BTCUSDT", interval="15", limit=2):
    cache_key = f"kline_binance_{symbol}_{interval}"
    current_time = time.time()

    with cache_lock:
        if cache_key in data_cache:
            cached_data, timestamp = data_cache[cache_key]
            if current_time - timestamp < cache_expiry:
                return cached_data

    try:
        if not is_futures_pair_available(symbol, 'Binance'):
            return None

        binance_interval = SUPPORTED_INTERVALS.get(interval, "15m")
        url = f"{BINANCE_API_URL}/fapi/v1/klines"
        params = {'symbol': symbol, 'interval': binance_interval, 'limit': limit}
        response = REQUEST_SESSION.get(url, params=params, timeout=10)
        data = response.json()

        with cache_lock:
            data_cache[cache_key] = (data, current_time)
        return data
    except Exception as e:
        logger.error(f"Binance kline error {symbol}: {e}")
        return None


def get_binance_ticker(symbol="BTCUSDT"):
    cache_key = f"ticker_binance_{symbol}"
    current_time = time.time()

    with cache_lock:
        if cache_key in data_cache:
            cached_data, timestamp = data_cache[cache_key]
            if current_time - timestamp < cache_expiry:
                return cached_data

    try:
        if not is_futures_pair_available(symbol, 'Binance'):
            return None

        ticker_data = REQUEST_SESSION.get(f"{BINANCE_API_URL}/fapi/v1/ticker/24hr?symbol={symbol}", timeout=10).json()
        oi_data = REQUEST_SESSION.get(f"{BINANCE_API_URL}/fapi/v1/openInterest?symbol={symbol}", timeout=10).json()

        result = {
            'symbol': symbol,
            'price': float(ticker_data['lastPrice']),
            'price_change_24h': float(ticker_data['priceChangePercent']),
            'volume': float(ticker_data['volume']),
            'quote_volume': float(ticker_data['quoteVolume']),
            'oi': float(oi_data['openInterest']),
            'timestamp': ticker_data['closeTime'],
            'exchange': 'Binance'
        }

        with cache_lock:
            data_cache[cache_key] = (result, current_time)
        return result
    except Exception as e:
        logger.error(f"Binance ticker error {symbol}: {str(e)}")
        return None


def get_bybit_ticker(symbol="BTCUSDT"):
    """Получение данных с Bybit"""
    cache_key = f"ticker_bybit_{symbol}"
    current_time = time.time()

    with cache_lock:
        if cache_key in data_cache:
            cached_data, timestamp = data_cache[cache_key]
            if current_time - timestamp < cache_expiry:
                return cached_data

    try:
        url = f"{BYBIT_API_URL}/v5/market/tickers?category=linear&symbol={symbol}"
        resp = REQUEST_SESSION.get(url, timeout=10).json()
        if resp.get('retCode') == 0:
            data = resp['result']['list'][0]
            result = {
                'symbol': symbol,
                'price': float(data['lastPrice']),
                'price_change_24h': float(data['price24hPcnt']) * 100,
                'volume': float(data['volume24h']),
                'quote_volume': float(data['turnover24h']),
                'oi': float(data['openInterest']),
                'timestamp': int(time.time() * 1000),
                'exchange': 'Bybit'
            }
            with cache_lock:
                data_cache[cache_key] = (result, current_time)
            return result
    except Exception as e:
        logger.error(f"Bybit ticker error {symbol}: {e}")
    return None


def get_binance_liquidations(symbol="BTCUSDT"):
    """Исправленный блок получения ликвидаций"""
    try:
        if not is_futures_pair_available(symbol, 'Binance'):
            return 0.0, 0.0, 0.0

        response = REQUEST_SESSION.get(
            f"{BINANCE_API_URL}/fapi/v1/allForceOrders",
            params={'symbol': symbol, 'limit': 50},
            timeout=5
        )

        if response.status_code != 200:
            return 0.0, 0.0, 0.0

        liq_data = response.json()
        long_liq = 0.0
        short_liq = 0.0

        if isinstance(liq_data, list):
            # Считаем ликвидации за последние 5 минут для актуальности сигнала
            now_ms = int(time.time() * 1000)
            threshold_ms = now_ms - (5 * 60 * 1000)

            for order in liq_data:
                order_time = order.get('time', 0)
                if order_time > threshold_ms:
                    value = float(order['executedQty']) * float(order['price'])
                    if order['side'] == 'SELL':  # Ликвидация Лонга
                        long_liq += value
                    else:  # Ликвидация Шорта
                        short_liq += value

        return (long_liq + short_liq), long_liq, short_liq
    except Exception as e:
        logger.debug(f"Liquidation error {symbol}: {str(e)}")
        return 0.0, 0.0, 0.0


def analyze_market(symbol, interval="15", enable_liq=False, exchange='Binance'):
    try:
        if exchange == 'Bybit':
            ticker_data = get_bybit_ticker(symbol)
        else:
            ticker_data = get_binance_ticker(symbol)

        if not ticker_data:
            return None

        # Для изменения за интервал (1м, 5м и т.д.) используем klines на Binance
        # Если Bybit — для простоты используем пока 24ч изменение или расширяем klines позже
        price_change = ticker_data['price_change_24h']  # Default fallback
        volume_change = 0.0

        if exchange == 'Binance':
            kline = get_binance_kline(symbol, interval, limit=2)
            if kline and len(kline) >= 2:
                open_price = float(kline[-1][1])
                current_price = ticker_data['price']
                price_change = ((current_price - open_price) / open_price * 100) if open_price != 0 else 0.0

                cur_vol = float(kline[-1][5])
                prev_vol = float(kline[-2][5])
                volume_change = ((cur_vol - prev_vol) / prev_vol * 100) if prev_vol != 0 else 0.0

        total_liq, long_liq, short_liq = 0.0, 0.0, 0.0
        if enable_liq and exchange == 'Binance':
            total_liq, long_liq, short_liq = get_binance_liquidations(symbol)

        return {
            'symbol': symbol,
            'price': ticker_data['price'],
            'price_change': price_change,
            'volume': ticker_data['volume'],
            'volume_change': volume_change,
            'oi': ticker_data['oi'],
            'total_liq': total_liq,
            'long_liq': long_liq,
            'short_liq': short_liq,
            'timestamp': ticker_data['timestamp'],
            'exchange': exchange
        }
    except Exception as e:
        logger.error(f"Analysis error {symbol}: {str(e)}")
        return None


def get_all_futures_symbols(exchange='Binance'):
    if exchange == 'Bybit':
        symbols = sorted(list(get_available_bybit_pairs()))
    else:
        symbols = sorted(list(get_available_futures_pairs()))
    return symbols


def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = ["📈 Цена BTC", "🔍 Анализ BTC", "🧠 Автосигналы", "📡 Статус мониторинга", "⚙ Настроить сигналы",
               "ℹ️ Помощь"]
    keyboard.add(*buttons)
    return keyboard


def get_settings_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "💰 Порог цены", "📊 Порог объема", "📈 Порог OI", "💧 Порог ликвидаций",
        "⏱ Интервал анализа", "🔄 Режим: И/ИЛИ", "🌐 Выбор Бирж",
        "🔔 Вкл/Выкл Цену", "🔔 Вкл/Выкл Объем", "🔔 Вкл/Выкл OI", "🔔 Вкл/Выкл Ликвидации",
        "✅ Сохранить настройки", "↩️ Назад"
    ]
    keyboard.add(*buttons)
    return keyboard


@bot.message_handler(commands=['start'])
def send_welcome(message):
    chat_id = message.chat.id
    if chat_id not in user_settings:
        user_settings[chat_id] = {
            'enable_price': True, 'enable_volume': True, 'enable_oi': True, 'enable_liq': True,
            'price_threshold': 1.5, 'volume_threshold': 50.0, 'oi_threshold': 5.0, 'liq_threshold': 1000000,
            'interval': "15", 'logic_mode': 'OR', 'exchanges': ['Binance']
        }
    bot.send_message(chat_id, "🤖 Бот готов к работе. Настройте параметры и запустите мониторинг.",
                     reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "⚙ Настроить сигналы")
def setup_signals(message):
    chat_id = message.chat.id
    if chat_id not in user_settings:
        send_welcome(message)

    s = user_settings[chat_id]
    msg = (
        f"⚙️ <b>Настройки:</b>\n\n"
        f"Логика: <b>{'ВСЕ условия (AND)' if s.get('logic_mode') == 'AND' else 'ЛЮБОЕ условие (OR)'}</b>\n"
        f"Биржи: <b>{', '.join(s.get('exchanges', ['Binance']))}</b>\n"
        f"Цена: {s.get('price_threshold')}% | Объем: {s.get('volume_threshold')}%\n"
        f"OI: {s.get('oi_threshold')}% | Ликв: {s.get('liq_threshold'):,.0f}$\n"
        f"Интервал: {s.get('interval')} мин"
    )
    bot.send_message(chat_id, msg, parse_mode='HTML', reply_markup=get_settings_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "🔄 Режим: И/ИЛИ")
def toggle_logic(message):
    chat_id = message.chat.id
    current = user_settings[chat_id].get('logic_mode', 'OR')
    user_settings[chat_id]['logic_mode'] = 'AND' if current == 'OR' else 'OR'
    bot.send_message(chat_id, f"✅ Режим изменен на: {user_settings[chat_id]['logic_mode']}")
    setup_signals(message)


@bot.message_handler(func=lambda msg: msg.text == "🌐 Выбор Бирж")
def toggle_exchanges(message):
    chat_id = message.chat.id
    current = user_settings[chat_id].get('exchanges', ['Binance'])

    if 'Binance' in current and 'Bybit' in current:
        new = ['Binance']
    elif 'Binance' in current:
        new = ['Bybit']
    elif 'Bybit' in current:
        new = ['Binance', 'Bybit']
    else:
        new = ['Binance']

    user_settings[chat_id]['exchanges'] = new
    bot.send_message(chat_id, f"✅ Список бирж: {', '.join(new)}")
    setup_signals(message)


@bot.message_handler(func=lambda msg: msg.text in ["💰 Порог цены", "📊 Порог объема", "📈 Порог OI", "⏱ Интервал анализа",
                                                   "💧 Порог ликвидаций"])
def setting_selection(message):
    chat_id = message.chat.id
    text = message.text
    params = {
        "💰 Порог цены": "price_threshold", "📊 Порог объема": "volume_threshold",
        "📈 Порог OI": "oi_threshold", "⏱ Интервал анализа": "interval",
        "💧 Порог ликвидаций": "liq_threshold"
    }
    user_states[chat_id] = {'setting': params[text]}
    bot.send_message(chat_id, f"✍️ Введите новое значение для {text}:", reply_markup=types.ReplyKeyboardRemove())


@bot.message_handler(func=lambda msg: msg.chat.id in user_states and 'setting' in user_states[msg.chat.id])
def handle_setting_input(message):
    chat_id = message.chat.id
    setting = user_states[chat_id]['setting']
    try:
        val = message.text.replace(',', '').replace(' ', '')
        if setting == 'interval':
            if val not in SUPPORTED_INTERVALS:
                bot.send_message(chat_id, "❌ Неверный интервал.")
                return
            user_settings[chat_id][setting] = val
        else:
            user_settings[chat_id][setting] = float(val)

        del user_states[chat_id]
        bot.send_message(chat_id, "✅ Сохранено.")
        setup_signals(message)
    except:
        bot.send_message(chat_id, "❌ Введите число.")


def check_signal_conditions(analysis, settings, chat_id, symbol):
    """Исправленная логика И/ИЛИ"""
    price_change = analysis['price_change']
    volume_change = analysis['volume_change']
    oi = analysis['oi']
    total_liq = analysis['total_liq']

    prev_oi_key = f"{chat_id}_{analysis['exchange']}_{symbol}"
    with oi_lock:
        if prev_oi_key not in prev_oi_data:
            prev_oi_data[prev_oi_key] = {'value': oi, 'first': True}
            oi_change_pct = 0.0
        else:
            prev_val = prev_oi_data[prev_oi_key]['value']
            oi_change_pct = ((oi - prev_val) / prev_val * 100) if prev_val != 0 else 0.0
            prev_oi_data[prev_oi_key]['value'] = oi

    # Проверка порогов
    res = {
        'p': abs(price_change) >= settings.get('price_threshold', 1.5) if settings.get('enable_price') else False,
        'v': volume_change >= settings.get('volume_threshold', 50.0) if settings.get('enable_volume') else False,
        'o': abs(oi_change_pct) >= settings.get('oi_threshold', 5.0) if settings.get('enable_oi') else False,
        'l': total_liq >= settings.get('liq_threshold', 1000000) if settings.get('enable_liq') else False
    }

    logic = settings.get('logic_mode', 'OR')
    if logic == 'AND':
        # Сигнал если ВСЕ ВКЛЮЧЕННЫЕ параметры сработали
        active = []
        if settings.get('enable_price'): active.append(res['p'])
        if settings.get('enable_volume'): active.append(res['v'])
        if settings.get('enable_oi'): active.append(res['o'])
        if settings.get('enable_liq') and analysis['exchange'] == 'Binance': active.append(res['l'])

        triggered = all(active) if active else False
    else:
        triggered = any(res.values())

    return triggered, oi_change_pct, res


@bot.message_handler(func=lambda msg: msg.text == "🧠 Автосигналы")
def start_auto_signals(message):
    chat_id = message.chat.id
    if chat_id in active_monitors:
        active_monitors[chat_id] = False
        time.sleep(2)

    active_monitors[chat_id] = True
    settings = user_settings[chat_id]
    exchanges = settings.get('exchanges', ['Binance'])

    bot.send_message(chat_id, f"🚀 Мониторинг запущен ({', '.join(exchanges)})")

    def monitor():
        last_signal = {}
        while active_monitors.get(chat_id):
            try:
                for exch in exchanges:
                    symbols = get_all_futures_symbols(exch)
                    monitoring_progress[chat_id] = {'total': len(symbols), 'cur': 0, 'exch': exch}

                    with ThreadPoolExecutor(max_workers=20) as executor:
                        futures = {executor.submit(analyze_market, s, settings.get('interval', "15"),
                                                   settings.get('enable_liq'), exch): s for s in symbols}

                        for f in as_completed(futures):
                            if not active_monitors.get(chat_id): break
                            res = f.result()
                            monitoring_progress[chat_id]['cur'] += 1
                            if not res: continue

                            triggered, oi_ch, matches = check_signal_conditions(res, settings, chat_id, res['symbol'])

                            if triggered:
                                key = f"{res['exchange']}_{res['symbol']}"
                                if time.time() - last_signal.get(key, 0) > 300:
                                    last_signal[key] = time.time()

                                    msg = (
                                        f"🚨 <b>СИГНАЛ [{res['exchange']}]</b>\n"
                                        f"Пара: #{res['symbol']}\n"
                                        f"Цена: {res['price']:.4f} ({res['price_change']:+.2f}%)\n"
                                        f"Объем: {res['volume']:,.0f} ({res['volume_change']:+.2f}%)\n"
                                        f"OI: {res['oi']:,.0f} ({oi_ch:+.2f}%)\n"
                                    )
                                    if res['total_liq'] > 0:
                                        msg += f"💧 Ликвидации: {res['total_liq']:,.0f}$\n"

                                    bot.send_message(chat_id, msg, parse_mode='HTML')

                time.sleep(60)
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                time.sleep(10)

    threading.Thread(target=monitor, daemon=True).start()


# Остальные обработчики (Цена BTC, Помощь, Назад и т.д.) остаются без изменений
@bot.message_handler(func=lambda msg: msg.text == "📈 Цена BTC")
def price_btn(message):
    data = get_binance_ticker("BTCUSDT")
    if data:
        bot.send_message(message.chat.id, f"💰 <b>BTC/USDT</b>: {data['price']}$ ({data['price_change_24h']:+.2f}%)",
                         parse_mode='HTML')


@bot.message_handler(func=lambda msg: msg.text == "↩️ Назад")
def back_to_main(message):
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "ℹ️ Помощь")
def help_btn(message):
    bot.send_message(message.chat.id, "Бот мониторит Binance и Bybit. Настройте пороги в меню настроек.")


@bot.message_handler(func=lambda msg: msg.text == "✅ Сохранить настройки")
def save_settings_btn(message):
    bot.send_message(message.chat.id, "✅ Настройки применены.", reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "📡 Статус мониторинга")
def status_btn(message):
    p = monitoring_progress.get(message.chat.id, {})
    if p:
        bot.send_message(message.chat.id, f"📡 Сканирую {p.get('exch')}: {p.get('cur')}/{p.get('total')}")
    else:
        bot.send_message(message.chat.id, "🛑 Мониторинг не запущен.")


if __name__ == "__main__":
    logger.info("🚀 Бот запускается...")
    get_available_futures_pairs(True)
    get_available_bybit_pairs()
    bot.infinity_polling()
