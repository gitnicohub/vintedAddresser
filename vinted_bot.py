import os
import sys
import json
import time
import html
import random
import logging
import requests

# Configurazione dell'encoding stdout per Windows
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Configurazione del Logging per visualizzare i dettagli di esecuzione
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Tenta l'importazione di curl_cffi per impersonare un browser reale e superare i blocchi TLS/Cloudflare di Vinted.
# Se non disponibile, effettua il fallback sul modulo standard 'requests'.
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


# File locale per salvare gli ID degli articoli già notificati
SEEN_FILE = "vinted_seen.json"

# Intervallo di tempo (in secondi)
DELAY_BETWEEN_REQUESTS_MIN = 3  # Pausa tra una richiesta e l'altra
DELAY_BETWEEN_REQUESTS_MAX = 5
CYCLE_SLEEP_TIME = 120          # Pausa di 120 secondi tra i cicli completi

# ==========================================
# 2. RICERCHE DA MONITORARE (URL API VINTED)
# ==========================================
SEARCH_URLS = [
    # Ricerca 1 (Nike 20-70€, taglia 119, ordina per più recenti)
    "https://www.vinted.it/api/v2/catalog/items?search_text=scarpe+uomo+nike&price_from=20&price_to=70&size_ids[]=119&order=newest_first",
    
    # Ricerca 2 (Filtri Custom e Multi-Brand fino a 80€, ordina per più recenti)
    "https://www.vinted.it/api/v2/catalog/items?search_text=&catalog[]=1231&price_to=80.00&currency=EUR&brand_ids[]=53&brand_ids[]=5977&brand_ids[]=283953&brand_ids[]=687713&brand_ids[]=5774166&size_ids[]=788&order=newest_first"
]

# User-Agent moderno ed intestazioni HTTP per simulare un browser reale
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"

COMMON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ==========================================
# 3. GESTIONE SESSIONE ED ANTI-SCRAPING
# ==========================================
def create_vinted_session():
    """
    Inizializza una sessione HTTP e recupera i cookie necessari da Vinted.
    Per evitare errori 401/403, effettua prima una GET ad una pagina principale di Vinted.
    """
    logging.info("Inizializzazione nuova sessione Vinted...")
    
    if HAS_CURL_CFFI:
        session = curl_requests.Session(impersonate="chrome124")
    else:
        session = requests.Session()
    
    session.headers.update(COMMON_HEADERS)

    # Inizializza i cookie visitando la pagina 'how_it_works' o la homepage
    try:
        response = session.get("https://www.vinted.it/how_it_works", timeout=12)
        if response.status_code != 200:
            logging.warning(f"Chiamata how_it_works fallita con stato {response.status_code}. Tentativo homepage...")
            response = session.get("https://www.vinted.it", timeout=12)
        
        logging.info(f"Sessione inizializzata con successo. Codice stato: {response.status_code}")
    except Exception as e:
        logging.error(f"Errore durante l'inizializzazione della sessione Vinted: {e}")

    # Imposta gli header per le successive chiamate API JSON
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.vinted.it/catalog",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    })
    
    return session


# ==========================================
# 4. GESTIONE FILE DEGLI ARTICOLI VISTI
# ==========================================
def load_seen_items(filepath=SEEN_FILE):
    """
    Carica dal file locale gli ID degli articoli già notificati.
    Se il file non esiste, lo crea inizializzandolo come lista vuota.
    """
    if not os.path.exists(filepath):
        logging.info(f"File '{filepath}' non trovato. Creazione nuovo file...")
        save_seen_items(filepath, set())
        return set()
    
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(str(item_id) for item_id in data)
    except Exception as e:
        logging.error(f"Errore nel caricamento del file degli articoli visti '{filepath}': {e}")
    
    return set()


def save_seen_items(filepath, seen_ids):
    """
    Salva la lista aggiornata di ID visti nel file JSON locale.
    Limita la dimensione massima della cronologia (es. ultimi 5000 elementi)
    per evitare che il file diventi troppo grande nel tempo.
    """
    try:
        # Mantieni al massimo 5000 articoli nella cronologia
        ids_to_save = list(seen_ids)[-5000:]
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(ids_to_save, f, indent=2)
    except Exception as e:
        logging.error(f"Errore durante il salvataggio degli ID visti in '{filepath}': {e}")


# ==========================================
# 5. INVIO NOTIFICHE TELEGRAM
# ==========================================
def send_telegram_notification(bot_token, chat_id, item):
    """
    Formatta le informazioni dell'articolo e le invia tramite il bot Telegram.
    Usa 'sendPhoto' se è disponibile l'immagine dell'articolo, altrimenti 'sendMessage' come fallback.
    Include anche un pulsante inline per aprire direttamente l'articolo su Vinted.
    """
    # Estrazione sicura dei campi dell'articolo
    title = item.get("title", "Titolo non disponibile")
    brand = item.get("brand_title", "Non specificato")
    size = item.get("size_title", "Non specificata")
    status = item.get("status", "Non specificato")
    
    # Gestione Prezzo
    price_obj = item.get("price")
    if isinstance(price_obj, dict):
        price_val = price_obj.get("amount", "N/D")
        currency = price_obj.get("currency_code", "EUR")
        price_str = f"{price_val} {currency}"
    elif price_obj is not None:
        price_str = f"{price_obj} EUR"
    else:
        price_str = "N/D"

    # Paese del venditore/pubblicazione
    user_info = item.get("user", {})
    country = user_info.get("country_title") or user_info.get("country_code") or item.get("country") or "Non specificato"

    # URL dell'articolo
    item_url = item.get("url", "")
    if item_url and not item_url.startswith("http"):
        item_url = f"https://www.vinted.it{item_url}"

    # URL della foto principale
    photo_url = None
    if item.get("photo") and isinstance(item["photo"], dict):
        photo_url = item["photo"].get("url")
    elif item.get("photos") and isinstance(item["photos"], list) and len(item["photos"]) > 0:
        photo_url = item["photos"][0].get("url")

    # Formattazione del testo in HTML
    caption = (
        f"🛍️ <b>NUOVO ARTICOLO SU VINTED!</b>\n\n"
        f"📌 <b>Titolo:</b> {html.escape(str(title))}\n"
        f"💶 <b>Prezzo:</b> {html.escape(str(price_str))}\n"
        f"🏷️ <b>Brand:</b> {html.escape(str(brand))}\n"
        f"📏 <b>Taglia:</b> {html.escape(str(size))}\n"
        f"✨ <b>Stato:</b> {html.escape(str(status))}\n"
        f"🌍 <b>Paese:</b> {html.escape(str(country))}\n\n"
        f"🔗 <a href='{item_url}'>Visualizza su Vinted</a>"
    )

    # Pulsante inline per accedere velocemente all'articolo
    reply_markup = {
        "inline_keyboard": [
            [{"text": "🛒 Apri su Vinted", "url": item_url}]
        ]
    }

    # Prova ad inviare la foto con la didascalia
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
            else:
                logging.warning(f"Invio foto fallito (Stato {res.status_code}). Tentativo fallback messaggio testo...")
        except Exception as e:
            logging.error(f"Errore nell'invio della foto a Telegram: {e}")

    # Fallback: invio solo messaggio di testo
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
# 6. LOGICA DI RICERCA ED ESECUZIONE BOT
# ==========================================
def fetch_catalog_items(session, url):
    """
    Effettua la richiesta HTTP GET all'endpoint catalog Vinted.
    Restituisce la lista di articoli trovati o solleva un'eccezione se la sessione scade.
    """
    response = session.get(url, timeout=10)
    
    if response.status_code in (401, 403):
        raise PermissionError(f"Sessione Vinted scaduta o bloccata (Codice {response.status_code})")
    
    if response.status_code != 200:
        logging.warning(f"Risposta non standard da Vinted (Codice {response.status_code}) per l'URL: {url}")
        return []
    
    data = response.json()
    return data.get("items", [])


def main():
    logging.info("Starting Vinted Telegram Bot...")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti! Definiscili nel file .env o come variabili d'ambiente.")
        sys.exit(1)
    
    # Carica la lista locale degli ID già visti

    seen_ids = load_seen_items(SEEN_FILE)
    logging.info(f"Caricati {len(seen_ids)} articoli già noti dalla memoria locale.")

    # Inizializza la prima sessione
    session = create_vinted_session()
    
    cycle_counter = 0

    while True:
        cycle_counter += 1
        logging.info(f"--- Inizio Ciclo #{cycle_counter} ---")

        for idx, url in enumerate(SEARCH_URLS, start=1):
            logging.info(f"Controllo ricerca {idx}/{len(SEARCH_URLS)}...")

            items = None
            try:
                items = fetch_catalog_items(session, url)
            except (PermissionError, Exception) as err:
                logging.warning(f"Errore nella richiesta ({err}). Rigenerazione cookie/sessione in corso...")
                try:
                    # Rigenera la sessione in caso di errore 401/403 o problemi di rete
                    session = create_vinted_session()
                    time.sleep(2)
                    items = fetch_catalog_items(session, url)
                except Exception as retry_err:
                    logging.error(f"Impossibile completare la ricerca dopo il rinnovo della sessione: {retry_err}")
                    items = []

            if items:
                new_items_count = 0
                for item in items:
                    item_id = str(item.get("id"))
                    
                    if item_id and item_id not in seen_ids:
                        logging.info(f"🔥 Trovato nuovo articolo! ID: {item_id} - Titolo: {item.get('title')}")
                        
                        # Invia notifica Telegram
                        success = send_telegram_notification(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, item)
                        
                        if success:
                            seen_ids.add(item_id)
                            save_seen_items(SEEN_FILE, seen_ids)
                            new_items_count += 1
                        
                        # Piccola pausa tra l'invio delle notifiche per evitare rate-limit Telegram
                        time.sleep(1)
                
                logging.info(f"Ricerca {idx} completata. Nuovi articoli notificati: {new_items_count}")
            else:
                logging.info(f"Ricerca {idx} completata. Nessun nuovo articolo trovato o risposta vuota.")

            # Pausa casuale tra 3 e 5 secondi tra le richieste a ciascuna ricerca
            delay = random.uniform(DELAY_BETWEEN_REQUESTS_MIN, DELAY_BETWEEN_REQUESTS_MAX)
            logging.info(f"Pausa di {delay:.2f} secondi prima della prossima ricerca...")
            time.sleep(delay)

        logging.info(f"--- Ciclo #{cycle_counter} completato. Pausa di {CYCLE_SLEEP_TIME} secondi prima del prossimo ciclo... ---")
        time.sleep(CYCLE_SLEEP_TIME)


if __name__ == "__main__":
    main()
