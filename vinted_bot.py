import os
import sys
import re
import json
import time
import html
import random
import logging
import threading
import requests

# Configurazione dell'encoding stdout per Windows
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Configurazione del Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Tenta l'importazione di curl_cffi per impersonare un browser reale
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
    logging.info("curl_cffi disponibile: le richieste utilizzeranno l'impersonazione del browser.")
except ImportError:
    HAS_CURL_CFFI = False
    logging.warning("curl_cffi non trovato: utilizzo di requests standard (potrebbero verificarsi blocchi 403).")

# ==========================================
# 1. CONFIGURAZIONE E CREDENZIALI
# ==========================================
def load_env_file(filepath=".env"):
    """Carica le variabili d'ambiente da un file .env locale se presente."""
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        except Exception as e:
            logging.warning(f"Impossibile leggere il file '{filepath}': {e}")

load_env_file()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SEEN_FILE = "vinted_seen.json"
SEARCHES_FILE = "vinted_searches.json"

DELAY_BETWEEN_REQUESTS_MIN = 3
DELAY_BETWEEN_REQUESTS_MAX = 5
CYCLE_SLEEP_TIME = 30

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"

COMMON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Ricerche predefinite
DEFAULT_SEARCHES = [
    {
        "id": "1",
        "name": "Scarpe Nike (20-70€)",
        "url": "https://www.vinted.it/api/v2/catalog/items?search_text=scarpe+uomo+nike&price_from=20&price_to=70&size_ids[]=119&order=newest_first",
        "active": False
    },
    {
        "id": "2",
        "name": "Multi-Brand (fino a 80€)",
        "url": "https://www.vinted.it/api/v2/catalog/items?search_text=&catalog[]=1231&price_to=80.00&currency=EUR&brand_ids[]=53&brand_ids[]=5977&brand_ids[]=283953&brand_ids[]=687713&brand_ids[]=5774166&size_ids[]=788&order=newest_first",
        "active": False
    }
]

# Stato globale del bot e delle ricerche
searches_lock = threading.Lock()
searches_data = []
bot_is_running = True
current_scanning_search = ""
initialized_searches = set()  # Traccia quali ricerche hanno già popolato la baseline iniziale degli ID visti


# ==========================================
# 2. GESTIONE CONVERSIONE URL E SALVATAGGIO RICERCHE
# ==========================================
def convert_to_vinted_api_url(raw_url):
    """
    Normalizza un URL di ricerca Vinted verso la pagina web del catalogo
    (https://www.vinted.it/catalog?...), dato che l'API /api/v2/catalog/items
    non e' piu' disponibile (404). Accetta sia URL web che vecchi URL API
    e assicura l'ordinamento per i piu' recenti.
    """
    url = raw_url.strip()
    if "/api/v2/catalog/items" in url:
        url = url.replace("/api/v2/catalog/items", "/catalog")

    if "order=" not in url:
        delimiter = "&" if "?" in url else "?"
        url += f"{delimiter}order=newest_first"

    return url


def load_searches(filepath=SEARCHES_FILE):
    """Carica le ricerche salvate da file JSON locale."""
    global searches_data
    with searches_lock:
        if not os.path.exists(filepath):
            searches_data = DEFAULT_SEARCHES
            save_searches(filepath)
            return
        
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    searches_data = data
                else:
                    searches_data = DEFAULT_SEARCHES
                    save_searches(filepath)
        except Exception as e:
            logging.error(f"Errore nel caricamento di '{filepath}': {e}. Uso ricerche di default.")
            searches_data = DEFAULT_SEARCHES


def save_searches(filepath=SEARCHES_FILE):
    """Salva le ricerche nel file JSON locale."""
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(searches_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Errore nel salvataggio delle ricerche in '{filepath}': {e}")


# ==========================================
# 3. GESTIONE SESSIONE VINTED & VISTI
# ==========================================
def create_vinted_session():
    """Inizializza una sessione HTTP per Vinted."""
    logging.info("Inizializzazione nuova sessione Vinted...")
    if HAS_CURL_CFFI:
        session = curl_requests.Session(impersonate="chrome124")
    else:
        session = requests.Session()
    
    session.headers.update(COMMON_HEADERS)

    try:
        response = session.get("https://www.vinted.it/how_it_works", timeout=12)
        if response.status_code != 200:
            logging.warning(f"Chiamata how_it_works fallita ({response.status_code}). Tentativo homepage...")
            response = session.get("https://www.vinted.it", timeout=12)
        logging.info(f"Sessione Vinted inizializzata (Stato: {response.status_code}).")
    except Exception as e:
        logging.error(f"Errore durante l'inizializzazione sessione Vinted: {e}")

    return session


def load_seen_items(filepath=SEEN_FILE):
    """Carica gli ID degli articoli già visti."""
    if not os.path.exists(filepath):
        save_seen_items(filepath, set())
        return set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(str(item_id) for item_id in data)
    except Exception as e:
        logging.error(f"Errore caricamento articoli visti '{filepath}': {e}")
    return set()


def save_seen_items(filepath, seen_ids):
    """Salva gli ID visti."""
    try:
        ids_to_save = list(seen_ids)[-5000:]
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(ids_to_save, f, indent=2)
    except Exception as e:
        logging.error(f"Errore salvataggio ID visti in '{filepath}': {e}")


# ==========================================
# 4. TELEGRAM API HELPERS & MENU INTERATTIVO
# ==========================================
def send_telegram_message(bot_token, chat_id, text, reply_markup=None):
    """Invia un messaggio Telegram."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        res = requests.post(url, data=payload, timeout=10)
        return res.json()
    except Exception as e:
        logging.error(f"Errore invio messaggio Telegram: {e}")
        return None


def edit_message_text(bot_token, chat_id, message_id, text, reply_markup=None):
    """Aggiorna un messaggio Telegram esistente."""
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        logging.error(f"Errore modifica messaggio Telegram: {e}")


def answer_callback_query(bot_token, callback_query_id, text=None):
    """Risponde ad una Callback Query di un pulsante inline."""
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(url, data=payload, timeout=5)
    except Exception:
        pass


def build_menu_text_and_keyboard():
    """Genera il testo e l'Inline Keyboard per la selezione delle ricerche."""
    global bot_is_running, current_scanning_search
    with searches_lock:
        searches = list(searches_data)
    
    status_str = "▶️ <b>IN ESECUZIONE</b>" if bot_is_running else "⏹️ <b>FERMATO</b>"
    active_searches = [s for s in searches if bool(s.get("active", False))]
    inactive_searches = [s for s in searches if not bool(s.get("active", False))]
    
    active_count = len(active_searches)
    
    # Costruisci l'elenco testuale dettagliato delle ricerche ATTIVE
    if active_searches:
        active_list_str = "\n".join([f"  ✅ <b>[ID {s.get('id')}]</b> {html.escape(s.get('name', ''))}" for s in active_searches])
    else:
        active_list_str = "  <i>(Nessuna ricerca attiva)</i>"

    # Costruisci l'elenco testuale delle ricerche DISATTIVATE
    if inactive_searches:
        inactive_list_str = "\n".join([f"  ❌ <b>[ID {s.get('id')}]</b> {html.escape(s.get('name', ''))}" for s in inactive_searches])
    else:
        inactive_list_str = "  <i>(Nessuna ricerca disattivata)</i>"
        
    scanning_str = f"🔎 <b>In scansione ora:</b> <i>{html.escape(current_scanning_search)}</i>\n" if (bot_is_running and current_scanning_search) else ""
    
    text = (
        f"⚙️ <b>GESTIONE RICERCHE VINTED BOT</b>\n\n"
        f"📊 <b>Stato Bot:</b> {status_str}\n"
        f"🎯 <b>Ricerche attive:</b> {active_count}/{len(searches)}\n"
        f"{scanning_str}\n"
        f"🟢 <b>RICERCHE ATTIVATE:</b>\n{active_list_str}\n\n"
        f"🔴 <b>RICERCHE DISATTIVATE:</b>\n{inactive_list_str}\n\n"
        f"<i>Tocca i pulsanti in basso per attivare (✅) o disattivare (❌) le singole ricerche:</i>"
    )
    
    inline_keyboard = []
    # Genera un pulsante per ciascuna ricerca
    for s in searches:
        s_id = str(s.get("id"))
        s_name = s.get("name", f"Ricerca {s_id}")
        is_active = bool(s.get("active", False))
        icon = "✅ ATTIVA" if is_active else "❌ DISATTIVATA"
        
        button_text = f"[{icon}] {s_name}"
        inline_keyboard.append([{"text": button_text, "callback_data": f"toggle:{s_id}"}])
    
    # Riga di controllo bot
    ctrl_row = []
    if bot_is_running:
        ctrl_row.append({"text": "⏹️ Ferma Bot", "callback_data": "action:stop"})
    else:
        ctrl_row.append({"text": "▶️ Avvia Bot", "callback_data": "action:start"})
    ctrl_row.append({"text": "🔄 Aggiorna Menu", "callback_data": "action:refresh"})
    inline_keyboard.append(ctrl_row)
    
    # Pulsante per guida su aggiunta/rimozione ricerche
    inline_keyboard.append([{"text": "➕ Come Aggiungere/Rimuovere Ricerche", "callback_data": "action:help_add"}])
    
    return text, {"inline_keyboard": inline_keyboard}


# ==========================================
# 5. TELEGRAM BOT POLLING & COMANDI
# ==========================================
def process_telegram_update(bot_token, authorized_chat_id, update):
    """Elabora ogni aggiornamento (messaggi e callback query) ricevuto da Telegram."""
    global bot_is_running, searches_data

    # 1. GESTIONE CALLBACK QUERY (PULSANTI INLINE)
    if "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb["id"]
        from_chat_id = str(cb["message"]["chat"]["id"])
        message_id = cb["message"]["message_id"]
        data = cb.get("data", "")

        if authorized_chat_id and from_chat_id != str(authorized_chat_id):
            answer_callback_query(bot_token, cb_id, "Non sei autorizzato ad usare questo bot.")
            return

        if data.startswith("toggle:"):
            target_id = data.split(":", 1)[1]
            status_msg = "Ricerca non trovata"
            with searches_lock:
                for s in searches_data:
                    if str(s.get("id")) == target_id:
                        s["active"] = not bool(s.get("active", False))
                        status_msg = f"Ricerca '{s.get('name')}' {'attivata ✅' if s['active'] else 'disattivata ❌'}"
                        break
                save_searches()
            
            answer_callback_query(bot_token, cb_id, status_msg)
            menu_text, reply_markup = build_menu_text_and_keyboard()
            edit_message_text(bot_token, from_chat_id, message_id, menu_text, reply_markup)

        elif data == "action:start":
            bot_is_running = True
            answer_callback_query(bot_token, cb_id, "▶️ Bot Avviato!")
            menu_text, reply_markup = build_menu_text_and_keyboard()
            edit_message_text(bot_token, from_chat_id, message_id, menu_text, reply_markup)

        elif data == "action:stop":
            bot_is_running = False
            answer_callback_query(bot_token, cb_id, "⏹️ Bot Fermato!")
            menu_text, reply_markup = build_menu_text_and_keyboard()
            edit_message_text(bot_token, from_chat_id, message_id, menu_text, reply_markup)

        elif data == "action:refresh":
            answer_callback_query(bot_token, cb_id, "🔄 Menu Aggiornato")
            menu_text, reply_markup = build_menu_text_and_keyboard()
            edit_message_text(bot_token, from_chat_id, message_id, menu_text, reply_markup)

        elif data == "action:help_add":
            help_text = (
                "💡 <b>COME AGGIUNGERE O RIMUOVERE RICERCHE:</b>\n\n"
                "🔹 <b>Aggiungere una ricerca:</b>\n"
                "Invia un messaggio in chat col comando <code>/aggiungi</code>:\n"
                "<code>/aggiungi Nome Ricerca | https://www.vinted.it/catalog?...</code>\n\n"
                "<i>Esempio:</i>\n"
                "<code>/aggiungi Felpe Adidas | https://www.vinted.it/catalog?search_text=adidas&price_to=30</code>\n\n"
                "🔹 <b>Rimuovere una ricerca:</b>\n"
                "Invia <code>/rimuovi &lt;ID&gt;</code> (es. <code>/rimuovi 1</code>)\n"
                "Oppure digita <code>/ricerche</code> per vedere tutti gli ID."
            )
            answer_callback_query(bot_token, cb_id)
            send_telegram_message(bot_token, from_chat_id, help_text)

        return

    # 2. GESTIONE MESSAGGI DI TESTO E COMANDI
    if "message" in update:
        msg = update["message"]
        from_chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()

        if authorized_chat_id and from_chat_id != str(authorized_chat_id):
            return

        if text.startswith("/start") or text.startswith("/menu") or text.startswith("/ricerche"):
            menu_text, reply_markup = build_menu_text_and_keyboard()
            send_telegram_message(bot_token, from_chat_id, menu_text, reply_markup)

        elif text.startswith("/avvia"):
            bot_is_running = True
            menu_text, reply_markup = build_menu_text_and_keyboard()
            send_telegram_message(bot_token, from_chat_id, "▶️ <b>Bot avviato con successo!</b>\n\n" + menu_text, reply_markup)

        elif text.startswith("/ferma") or text.startswith("/stop"):
            bot_is_running = False
            menu_text, reply_markup = build_menu_text_and_keyboard()
            send_telegram_message(bot_token, from_chat_id, "⏹️ <b>Bot fermato. Notifiche messe in pausa.</b>\n\n" + menu_text, reply_markup)

        elif text.startswith("/aggiungi") or text.startswith("/add"):
            parts = text.split(" ", 1)
            if len(parts) < 2:
                send_telegram_message(
                    bot_token, from_chat_id,
                    "⚠️ <b>Sintassi errata!</b> Usa:\n"
                    "<code>/aggiungi Nome Ricerca | URL_VINTED</code>"
                )
                return
            
            content = parts[1].strip()
            if "|" in content:
                name_part, url_part = content.split("|", 1)
                name = name_part.strip()
                raw_url = url_part.strip()
            else:
                raw_url = content
                name = f"Ricerca {len(searches_data) + 1}"
            
            api_url = convert_to_vinted_api_url(raw_url)
            
            with searches_lock:
                new_id = str(max([int(s.get("id", 0)) for s in searches_data] + [0]) + 1)
                new_search = {
                    "id": new_id,
                    "name": name,
                    "url": api_url,
                    "active": True
                }
                searches_data.append(new_search)
                save_searches()
            
            reply = f"✅ <b>Ricerca aggiunta ed attivata!</b>\n\n📌 <b>ID:</b> {new_id}\n🏷️ <b>Nome:</b> {html.escape(name)}\n🔗 <b>URL API:</b> <code>{html.escape(api_url)}</code>"
            send_telegram_message(bot_token, from_chat_id, reply)
            menu_text, reply_markup = build_menu_text_and_keyboard()
            send_telegram_message(bot_token, from_chat_id, menu_text, reply_markup)

        elif text.startswith("/rimuovi") or text.startswith("/del"):
            parts = text.split(" ", 1)
            if len(parts) < 2:
                send_telegram_message(bot_token, from_chat_id, "⚠️ Specifica l'ID della ricerca da rimuovere (es: <code>/rimuovi 1</code>)")
                return
            
            target_id = parts[1].strip()
            removed = False
            removed_name = ""
            
            with searches_lock:
                new_list = []
                for s in searches_data:
                    if str(s.get("id")) == target_id:
                        removed = True
                        removed_name = s.get("name")
                    else:
                        new_list.append(s)
                if removed:
                    searches_data = new_list
                    save_searches()
            
            if removed:
                send_telegram_message(bot_token, from_chat_id, f"🗑️ Ricerca <b>'{html.escape(removed_name)}'</b> (ID {target_id}) rimossa.")
                menu_text, reply_markup = build_menu_text_and_keyboard()
                send_telegram_message(bot_token, from_chat_id, menu_text, reply_markup)
            else:
                send_telegram_message(bot_token, from_chat_id, f"❌ Nessuna ricerca trovata con ID {target_id}.")

        elif text.startswith("/status"):
            with searches_lock:
                searches = list(searches_data)
            status_str = "▶️ IN ESECUZIONE" if bot_is_running else "⏹️ FERMATO"
            msg = f"📊 <b>STATO VINTED BOT</b>: {status_str}\n\n<b>Ricerche configurate ({len(searches)}):</b>\n"
            for s in searches:
                icon = "✅" if s.get("active", True) else "❌"
                msg += f"• [{icon}] ID {s.get('id')}: <b>{html.escape(s.get('name'))}</b>\n"
            send_telegram_message(bot_token, from_chat_id, msg)


def telegram_polling_thread(bot_token, authorized_chat_id):
    """Thread in background per il polling continuo degli aggiornamenti Telegram."""
    offset = 0
    logging.info("Thread Polling Telegram avviato.")
    while True:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            params = {"offset": offset, "timeout": 20}
            res = requests.get(url, params=params, timeout=25)
            if res.status_code == 200:
                data = res.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        process_telegram_update(bot_token, authorized_chat_id, update)
        except Exception as e:
            logging.error(f"Errore nel polling Telegram: {e}")
            time.sleep(3)


# ==========================================
# 6. INVIO NOTIFICHE TELEGRAM
# ==========================================
def send_telegram_notification(bot_token, chat_id, item, search_name=None):
    """Invio notifica Telegram per un nuovo articolo trovato."""
    title = item.get("title", "Titolo non disponibile")
    brand = item.get("brand_title", "Non specificato")
    size = item.get("size_title", "Non specificata")
    status = item.get("status", "Non specificato")
    
    price_obj = item.get("price")
    if isinstance(price_obj, dict):
        price_val = price_obj.get("amount", "N/D")
        currency = price_obj.get("currency_code", "EUR")
        price_str = f"{price_val} {currency}"
    elif price_obj is not None:
        price_str = f"{price_obj} EUR"
    else:
        price_str = "N/D"

    user_info = item.get("user", {})
    country = user_info.get("country_title") or user_info.get("country_code") or item.get("country")
    country_line = f"🌍 <b>Paese:</b> {html.escape(str(country))}\n" if country else ""

    item_url = item.get("url", "")
    if item_url and not item_url.startswith("http"):
        item_url = f"https://www.vinted.it{item_url}"

    photo_url = None
    if item.get("photo") and isinstance(item["photo"], dict):
        photo_url = item["photo"].get("url")
    elif item.get("photos") and isinstance(item["photos"], list) and len(item["photos"]) > 0:
        photo_url = item["photos"][0].get("url")

    search_info = f"\n🔍 <b>Ricerca:</b> {html.escape(str(search_name))}\n" if search_name else ""

    caption = (
        f"🛍️ <b>NUOVO ARTICOLO SU VINTED!</b>\n"
        f"{search_info}\n"
        f"📌 <b>Titolo:</b> {html.escape(str(title))}\n"
        f"💶 <b>Prezzo:</b> {html.escape(str(price_str))}\n"
        f"🏷️ <b>Brand:</b> {html.escape(str(brand))}\n"
        f"📏 <b>Taglia:</b> {html.escape(str(size))}\n"
        f"✨ <b>Stato:</b> {html.escape(str(status))}\n"
        f"{country_line}\n"
        f"🔗 <a href='{item_url}'>Visualizza su Vinted</a>"
    )

    reply_markup = {
        "inline_keyboard": [
            [{"text": "🛒 Apri su Vinted", "url": item_url}]
        ]
    }

    if photo_url:
        telegram_api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(reply_markup)
        }
        try:
            res = requests.post(telegram_api_url, data=payload, timeout=10)
            if res.status_code == 200:
                logging.info(f"Notifica foto inviata per l'articolo ID {item.get('id')}")
                return True
        except Exception as e:
            logging.error(f"Errore nell'invio della foto a Telegram: {e}")

    # Fallback testo
    telegram_api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": caption,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(reply_markup),
        "disable_web_page_preview": False
    }
    try:
        res = requests.post(telegram_api_url, data=payload, timeout=10)
        if res.status_code == 200:
            logging.info(f"Notifica testo inviata per l'articolo ID {item.get('id')}")
            return True
        else:
            logging.error(f"Errore invio messaggio Telegram: Stato {res.status_code} - {res.text}")
    except Exception as e:
        logging.error(f"Errore durante la chiamata API a Telegram: {e}")

    return False


# ==========================================
# 7. LOGICA DI RICERCA ED ESECUZIONE BOT
# ==========================================
FLIGHT_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
ITEMS_MARKER = '"items":{"items":'


def parse_catalog_html(page_html):
    """Estrae gli articoli dai dati Next.js incorporati nella pagina del catalogo."""
    chunks = FLIGHT_CHUNK_RE.findall(page_html)
    data = "".join(json.loads(c) for c in chunks)
    pos = data.find(ITEMS_MARKER)
    if pos == -1:
        raise ValueError("Dati articoli non trovati nella pagina (layout Vinted cambiato?)")
    raw_items, _ = json.JSONDecoder().raw_decode(data[pos + len(ITEMS_MARKER):])

    items = []
    for entry in raw_items:
        prod = entry.get("productItem") or {}
        if not prod.get("id"):
            continue
        box = prod.get("itemBox") or {}
        second = box.get("secondLine") or ""
        size, _, status = second.partition(" · ")
        price = prod.get("price") or {}
        photos = prod.get("photos") or []
        photo_url = photos[0].get("url") if photos else prod.get("thumbnailUrl")
        items.append({
            "id": prod["id"],
            "title": prod.get("title"),
            "brand_title": box.get("firstLine") or "Non specificato",
            "size_title": size or "Non specificata",
            "status": status or "Non specificato",
            "price": {"amount": price.get("amount"), "currency_code": price.get("currencyCode", "EUR")},
            "url": prod.get("url", ""),
            "photo": {"url": photo_url} if photo_url else None,
        })
    return items


def fetch_catalog_items(session, url):
    """Scarica la pagina catalogo Vinted e ne estrae gli articoli."""
    response = session.get(url, timeout=15)
    if response.status_code in (401, 403):
        raise PermissionError(f"Sessione Vinted scaduta o bloccata (Codice {response.status_code})")
    if response.status_code != 200:
        logging.warning(f"Risposta non standard da Vinted (Codice {response.status_code}) per URL: {url}")
        return []
    return parse_catalog_html(response.text)


def main():
    global current_scanning_search
    logging.info("Starting Vinted Telegram Bot...")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti! Definiscili nel file .env.")
        sys.exit(1)
    
    # Carica ricerche e articoli gia' visti
    load_searches(SEARCHES_FILE)
    seen_ids = load_seen_items(SEEN_FILE)
    logging.info(f"Caricati {len(seen_ids)} articoli visti e {len(searches_data)} ricerche configurate.")

    # Avvia thread Telegram Listener per i comandi ed il menu interattivo
    t_telegram = threading.Thread(target=telegram_polling_thread, args=(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID), daemon=True)
    t_telegram.start()

    # Invia messaggio di benvenuto con menu su Telegram
    menu_text, reply_markup = build_menu_text_and_keyboard()
    send_telegram_message(
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
        "🤖 <b>Vinted Bot Avviato!</b>\n\nUtilizza il menu qui sotto per selezionare ed attivare le ricerche che desideri monitorare:",
        reply_markup
    )

    session = create_vinted_session()
    cycle_counter = 0

    while True:
        if not bot_is_running:
            current_scanning_search = "Bot in pausa ⏹️"
            time.sleep(3)
            continue

        with searches_lock:
            active_searches = [dict(s) for s in searches_data if bool(s.get("active", False))]

        if not active_searches:
            current_scanning_search = "Nessuna ricerca attiva ⚠️"
            time.sleep(5)
            continue

        cycle_counter += 1
        logging.info(f"--- Inizio Ciclo #{cycle_counter} ({len(active_searches)} ricerche attive) ---")

        for idx, search in enumerate(active_searches, start=1):
            # Controllo immediato se il bot e' stato fermato da Telegram
            if not bot_is_running:
                current_scanning_search = "Bot in pausa ⏹️"
                logging.info("Esecuzione interrotta da Telegram durante il ciclo.")
                break

            search_id = str(search.get("id"))
            search_name = search.get("name", f"Ricerca {idx}")
            search_url = convert_to_vinted_api_url(search.get("url", ""))

            current_scanning_search = f"{search_name} ({idx}/{len(active_searches)})"
            logging.info(f"Controllo ricerca {idx}/{len(active_searches)}: '{search_name}'...")

            items = None
            try:
                items = fetch_catalog_items(session, search_url)
            except (PermissionError, Exception) as err:
                logging.warning(f"Errore nella richiesta ({err}). Rigenerazione cookie/sessione...")
                try:
                    session = create_vinted_session()
                    time.sleep(2)
                    items = fetch_catalog_items(session, search_url)
                except Exception as retry_err:
                    logging.error(f"Impossibile completare la ricerca dopo rinnovo sessione: {retry_err}")
                    items = []

            if items:
                # Se è la prima volta che eseguiamo questa ricerca, popoliamo i visti senza notificare in massa i vecchi articoli
                if search_id not in initialized_searches:
                    logging.info(f"Inizializzazione baseline articoli visti per '{search_name}' ({len(items)} articoli). Niente notifiche vecchie.")
                    for item in items:
                        item_id = str(item.get("id"))
                        if item_id:
                            seen_ids.add(item_id)
                    save_seen_items(SEEN_FILE, seen_ids)
                    initialized_searches.add(search_id)
                    continue

                new_items_count = 0
                for item in items:
                    # VERIFICA IMMEDIATA: Se il bot e' stato fermato o la ricerca disattivata, stop notifiche istantaneo!
                    if not bot_is_running:
                        current_scanning_search = "Bot in pausa ⏹️"
                        logging.info("Notifiche interrotte: bot fermato da Telegram.")
                        break

                    item_id = str(item.get("id"))
                    if item_id and item_id not in seen_ids:
                        logging.info(f"🔥 Trovato nuovo articolo! ID: {item_id} - Titolo: {item.get('title')}")
                        success = send_telegram_notification(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, item, search_name=search_name)
                        if success:
                            seen_ids.add(item_id)
                            save_seen_items(SEEN_FILE, seen_ids)
                            new_items_count += 1
                        time.sleep(1)
                logging.info(f"Ricerca '{search_name}' completata. Nuovi articoli notificati: {new_items_count}")
            else:
                logging.info(f"Ricerca '{search_name}' completata. Nessun nuovo articolo trovato.")
                # Segna comunque la ricerca come inizializzata
                initialized_searches.add(search_id)

            delay = random.uniform(DELAY_BETWEEN_REQUESTS_MIN, DELAY_BETWEEN_REQUESTS_MAX)
            logging.info(f"Pausa di {delay:.2f}s prima della prossima ricerca...")
            time.sleep(delay)

        if bot_is_running:
            current_scanning_search = f"Pausa tra i cicli ({CYCLE_SLEEP_TIME}s) 💤"
            logging.info(f"--- Ciclo #{cycle_counter} completato. Pausa di {CYCLE_SLEEP_TIME}s... ---")
            time.sleep(CYCLE_SLEEP_TIME)


if __name__ == "__main__":
    main()
