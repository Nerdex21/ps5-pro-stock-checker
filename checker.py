#!/usr/bin/env python3
"""
Checker de stock de la PS5 Pro en minoristas de EE.UU.
Corre cada hora (via GitHub Actions) y avisa por Telegram cuando encuentra stock.

Fuentes (de mas a menos confiable):
- Walmart: availabilityStatus del JSON __NEXT_DATA__ de la pagina.
- Newegg: API interna ProductRealtime (Instock bool).
- Sam's Club / Abt: disponibilidad schema.org embebida en la pagina.
- PlayStation Direct / Amazon: deteccion por texto en el HTML.
- Best Buy / GameStop: scraping best-effort (suelen bloquear).
- Target: deshabilitado (su API interna murio y el HTML es un shell de JS).

Todo se baja con curl_cffi imitando la huella TLS de Chrome (pasa varios
anti-bots). Si una pagina viene bloqueada o sin datos, se reporta UNKNOWN (❔),
NUNCA "sin stock" — cero falsas alarmas.

Las alertas se mandan a TODOS los chats listados en TELEGRAM_CHAT_IDS
(separados por coma, ej: "123456789,-100987654321").

Uso:
  python checker.py                     # corre todos los chequeos y avisa por Telegram
  python checker.py --test-telegram     # manda un mensaje de prueba a todos los chats
  python checker.py --get-chat-id       # imprime los chat IDs que le hablaron al bot
  python checker.py --once-report       # corre y solo imprime, sin avisar (debug)
"""
import os
import re
import sys
import json
from datetime import datetime, timezone, timedelta
from html import unescape

import requests

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # fallback: sin huella de Chrome, mas facil de bloquear
    cffi_requests = None

# La consola de Windows usa cp1252 por defecto y no banca emojis.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")

IN_STOCK = "IN_STOCK"
OUT_OF_STOCK = "OUT_OF_STOCK"
UNKNOWN = "UNKNOWN"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

EMOJI = {IN_STOCK: "✅", OUT_OF_STOCK: "❌", UNKNOWN: "❔"}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:  # noqa: BLE001
        log(f"WARN no pude leer {path}: {e}")
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def result(retailer, product, status, url, detail):
    return {"retailer": retailer, "product": product, "status": status,
            "url": url, "detail": detail}


# ---------------- Telegram ----------------
def tg_request(method, params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/{method}"
    r = requests.post(url, data=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_chat_ids():
    """Lista de chat IDs desde TELEGRAM_CHAT_IDS (separados por coma).
    Acepta tambien TELEGRAM_CHAT_ID (singular) por compatibilidad."""
    raw = (os.environ.get("TELEGRAM_CHAT_IDS", "").strip()
           or os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    return [c.strip() for c in raw.split(",") if c.strip()]


def send_telegram(text):
    chat_ids = get_chat_ids()
    if not chat_ids:
        log("WARN Falta TELEGRAM_CHAT_IDS; no puedo enviar.")
        return False
    ok = 0
    for chat_id in chat_ids:
        try:
            tg_request("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            })
            ok += 1
        except Exception as e:  # noqa: BLE001
            log(f"ERROR enviando Telegram a {chat_id}: {e}")
    log(f"Telegram: enviado a {ok}/{len(chat_ids)} chat(s).")
    return ok > 0


# ---------------- Fetch con huella de Chrome ----------------
def fetch(url, accept="text/html,application/xhtml+xml,*/*;q=0.8", referer=None):
    """GET con curl_cffi (huella TLS de Chrome real); cae a requests si no esta."""
    headers = dict(BROWSER_HEADERS, Accept=accept)
    if referer:
        headers["Referer"] = referer
    if cffi_requests is not None:
        return cffi_requests.get(url, impersonate="chrome", headers={
            "Accept": accept, **({"Referer": referer} if referer else {}),
        }, timeout=30)
    return requests.get(url, headers=headers, timeout=25)


# ---------------- Deteccion generica sobre HTML ----------------
PRODUCT_PATTERNS = ["ps5 pro", "playstation 5 pro", "playstation5 pro", "playstation®5 pro"]
OUT_PATTERNS = [
    "sold out", "out of stock", "currently unavailable",
    "temporarily out of stock", "coming soon", "notify me when",
    "out-of-stock", "soldout", "get notified",
]
IN_PATTERNS = [
    "add to cart", "add to bag", "addtocart", "add-to-cart", "buy now",
]
SCHEMA_IN = {"instock", "limitedavailability", "onlineonly", "preorder"}
SCHEMA_OUT = {"outofstock", "soldout", "discontinued", "backorder"}
OPEN_BOX_RE = re.compile(r"\bopen[\s-]+box\b", re.I)


def is_open_box(value):
    return bool(OPEN_BOX_RE.search(unescape(str(value or ""))))


def iter_ldjson_products(text):
    for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
                            text, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:  # noqa: BLE001
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            if item_type == "Product" or (isinstance(item_type, list)
                                           and "Product" in item_type):
                yield item


def page_is_open_box(text, url=""):
    """Detect an open-box primary listing, not unrelated page recommendations."""
    if is_open_box(url):
        return True

    for tag in re.findall(r"<meta\b[^>]*>", text, re.I):
        attrs = dict(re.findall(r"([\w:-]+)\s*=\s*['\"](.*?)['\"]", tag, re.S))
        if (attrs.get("property") or attrs.get("name")) in ("og:title", "title"):
            if is_open_box(attrs.get("content")):
                return True

    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if title and is_open_box(title.group(1)):
        return True

    for item in iter_ldjson_products(text):
        product_name = item.get("name") or ""
        if (any(p in product_name.lower() for p in PRODUCT_PATTERNS)
                and is_open_box(product_name)):
            return True
    return False


def ldjson_availability(text):
    """Disponibilidad desde bloques JSON-LD tipo Product (senal estructurada).
    Devuelve (set de availabilities en minuscula, precio) solo de productos
    cuyo nombre matchea la PS5 Pro. Ignora menciones sueltas de schema.org
    en el JS de la pagina (esas dan falsos positivos)."""
    avails, price = set(), None
    for item in iter_ldjson_products(text):
        name = (item.get("name") or "").lower()
        if name and not any(p in name for p in PRODUCT_PATTERNS):
            continue
        offers = item.get("offers") or {}
        for o in (offers if isinstance(offers, list) else [offers]):
            a = (o.get("availability") or "").rsplit("/", 1)[-1].lower()
            if a:
                avails.add(a)
                price = price or o.get("price")
    return avails, price


def check_one_html(name, url, out_extra=()):
    try:
        r = fetch(url)
        if r.status_code in (403, 429, 503):
            return result(name, url, UNKNOWN, url, f"bloqueado (HTTP {r.status_code})")
        if r.status_code >= 400:
            return result(name, url, UNKNOWN, url, f"HTTP {r.status_code}")
        if page_is_open_box(r.text, getattr(r, "url", url)):
            return result(name, url, OUT_OF_STOCK, url, "ignorado: oferta Open Box")
        body = r.text.lower()

        # Si la pagina ni menciona el producto es un muro anti-bot o un shell
        # de JS: no hay senal de stock ahi.
        if not any(p in body for p in PRODUCT_PATTERNS):
            return result(name, url, UNKNOWN, url,
                          "pagina sin datos del producto (bloqueo o shell JS)")

        # 1) Senal estructurada (JSON-LD del producto) si existe: la mas confiable.
        avails, price = ldjson_availability(r.text)
        if avails:
            ins, outs = avails & SCHEMA_IN, avails & SCHEMA_OUT
            tag = "/".join(sorted(avails)) + (f", ${price}" if price else "")
            if ins and not outs:
                return result(name, url, IN_STOCK, url, f"JSON-LD: {tag}")
            if outs and not ins:
                return result(name, url, OUT_OF_STOCK, url, f"JSON-LD: {tag}")
            # mezcla (ofertas nuevas y usadas con distinto estado): sigo con texto

        # 2) Texto. Priorizo "sin stock" para evitar falsas alarmas.
        out_hit = next((p for p in list(out_extra) + OUT_PATTERNS if p in body), None)
        if out_hit:
            return result(name, url, OUT_OF_STOCK, url, f"detecto '{out_hit}'")
        in_hit = next((p for p in IN_PATTERNS if p in body), None)
        if in_hit:
            return result(name, url, IN_STOCK, url, f"detecto '{in_hit}'")
        return result(name, url, UNKNOWN, url, "sin senales claras")
    except Exception as e:  # noqa: BLE001
        return result(name, url, UNKNOWN, url, f"error: {str(e)[:120]}")


def make_html_checker(display_name):
    def _check(cfg):
        extra = cfg.get("out_extra", [])
        return [check_one_html(display_name, u, extra) for u in cfg.get("urls", [])]
    return _check


# ---------------- Walmart: availabilityStatus del __NEXT_DATA__ ----------------
def check_walmart(cfg):
    results = []
    for url in cfg.get("urls", []):
        try:
            r = fetch(url)
            if page_is_open_box(r.text, getattr(r, "url", url)):
                results.append(result("Walmart", url, OUT_OF_STOCK, url,
                                      "ignorado: oferta Open Box"))
                continue
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            if r.status_code == 200 and m:
                data = json.loads(m.group(1))
                prod = ((((data.get("props") or {}).get("pageProps") or {})
                         .get("initialData") or {}).get("data") or {}).get("product") or {}
                status_raw = (prod.get("availabilityStatus") or "").upper()
                name = prod.get("name") or url
                price = ((prod.get("priceInfo") or {}).get("currentPrice") or {}).get("priceString", "")
                if is_open_box(name):
                    results.append(result("Walmart", name, OUT_OF_STOCK, url,
                                          "ignorado: oferta Open Box"))
                    continue
                if status_raw:
                    status = IN_STOCK if status_raw == "IN_STOCK" else OUT_OF_STOCK
                    detail = f"availabilityStatus={status_raw}" + (f", {price}" if price else "")
                    results.append(result("Walmart", name, status, url, detail))
                    continue
            # sin JSON utilizable: caigo a la deteccion generica
            results.append(check_one_html("Walmart", url, cfg.get("out_extra", [])))
        except Exception as e:  # noqa: BLE001
            results.append(result("Walmart", url, UNKNOWN, url, f"error: {str(e)[:120]}"))
    return results


# ---------------- Newegg: API interna ProductRealtime ----------------
def check_newegg(cfg):
    results = []
    for item in cfg.get("items", []):
        page = f"https://www.newegg.com/p/{item}"
        try:
            r = fetch(f"https://www.newegg.com/product/api/ProductRealtime?ItemNumber={item}",
                      accept="application/json", referer=page)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                results.append(result("Newegg", item, UNKNOWN, page,
                                      f"API bloqueada (HTTP {r.status_code})"))
                continue
            main = (r.json().get("MainItem") or {})
            instock = main.get("Instock")
            if instock is None:
                results.append(result("Newegg", item, UNKNOWN, page, "API sin campo Instock"))
                continue
            title = main.get("Title") or item
            if is_open_box(title):
                results.append(result("Newegg", title, OUT_OF_STOCK, page,
                                      "ignorado: oferta Open Box"))
                continue
            results.append(result("Newegg", title, IN_STOCK if instock else OUT_OF_STOCK,
                                  page, f"Instock={instock}"))
        except Exception as e:  # noqa: BLE001
            results.append(result("Newegg", item, UNKNOWN, page, f"error: {str(e)[:120]}"))
    return results


CHECKERS = {
    "bestbuy": make_html_checker("Best Buy"),
    "walmart": check_walmart,
    "newegg": check_newegg,
    "samsclub": make_html_checker("Sam's Club"),
    "abt": make_html_checker("Abt"),
    "amazon": make_html_checker("Amazon"),
    "gamestop": make_html_checker("GameStop"),
    "playstation_direct": make_html_checker("PlayStation Direct"),
}


def run_all(config):
    results = []
    for name, ccfg in config.get("retailers", {}).items():
        if not ccfg.get("enabled", True):
            continue
        fn = CHECKERS.get(name)
        if not fn:
            log(f"WARN retailer desconocido en config: {name}")
            continue
        try:
            results.extend(fn(ccfg))
        except Exception as e:  # noqa: BLE001
            results.append(result(name, "-", UNKNOWN, "", f"error inesperado: {e}"))
    return results


# ---------------- Modos utilitarios ----------------
def cmd_get_chat_id():
    data = tg_request("getUpdates", {})
    seen = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = (chat.get("title") or chat.get("username")
                                or chat.get("first_name") or "")
    if not seen:
        print("No hay mensajes recientes. Mandale un mensaje a tu bot y volve a correr esto.")
    for cid, who in seen.items():
        print(f"chat_id={cid}  ({who})")


# ---------------- Main ----------------
def main():
    args = sys.argv[1:]
    config = load_json(CONFIG_PATH, {})

    if "--test-telegram" in args:
        ok = send_telegram("\U0001f514 Test del checker de PS5 Pro. Si ves esto, "
                           "Telegram esta bien configurado ✅")
        print("OK, mensaje enviado." if ok else "FALLO (revisa token/chat id).")
        return

    if "--get-chat-id" in args:
        cmd_get_chat_id()
        return

    report_only = "--once-report" in args

    results = run_all(config)

    log("Resumen del chequeo:")
    for r in results:
        print(f"  {EMOJI.get(r['status'], '?')} {r['retailer']:<20} "
              f"{r['status']:<13} {r['detail']}")

    if report_only:
        return

    state = load_json(STATE_PATH, {})
    now = datetime.now(timezone.utc)
    recheck_h = config.get("recheck_alert_hours", 6)
    alerts = []

    for r in results:
        key = f"{r['retailer']}::{r['product']}"
        prev = state.get(key, {})
        prev_status = prev.get("status")

        if r["status"] == IN_STOCK:
            last_alert = prev.get("last_alert")
            transitioned = prev_status != IN_STOCK
            stale = False
            if last_alert:
                try:
                    stale = (now - datetime.fromisoformat(last_alert)) > timedelta(hours=recheck_h)
                except Exception:  # noqa: BLE001
                    stale = True
            # Aviso al pasar a "con stock", o de nuevo si sigue con stock hace rato.
            if transitioned or stale or not last_alert:
                alerts.append(r)
                prev["last_alert"] = now.isoformat()

        prev["status"] = r["status"]
        prev["checked"] = now.isoformat()
        state[key] = prev

    save_json(STATE_PATH, state)

    if alerts:
        lines = ["\U0001f6a8 <b>¡PS5 Pro CON STOCK!</b> \U0001f6a8", ""]
        for r in alerts:
            link = f'<a href="{r["url"]}">{r["retailer"]}</a>' if r["url"] else r["retailer"]
            lines.append(f"✅ {link} — {r['product']}\n<i>{r['detail']}</i>")
        lines += ["", "Corre a comprarla \U0001f3c3"]
        if send_telegram("\n".join(lines)):
            log(f"Enviadas {len(alerts)} alerta(s) por Telegram.")
    else:
        log("Sin stock nuevo.")
        # Heartbeat: con always_notify=true avisa igual aunque no haya stock
        # (util para verificar que los mensajes llegan; despues poner false).
        if config.get("always_notify", False):
            lines = ["\U0001f634 No stock anywhere (por ahora)", ""]
            for r in results:
                lines.append(f"{EMOJI.get(r['status'], '?')} {r['retailer']} — {r['detail']}")
            if not results:
                lines.append("⚠️ Nada configurado para chequear (config.json sin URLs/TCINs).")
            send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
