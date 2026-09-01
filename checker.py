#!/usr/bin/env python3
"""
Checker de stock de la PS5 Pro en minoristas de EE.UU.
Corre cada hora (via GitHub Actions) y avisa por Telegram cuando encuentra stock.

Fuentes:
- Target: RedSky API (semi-oficial).
- Best Buy / Walmart / Amazon / GameStop / PlayStation Direct: scraping best-effort.
  Ojo: desde IPs de datacenter (GitHub Actions) estos suelen estar bloqueados;
  en ese caso el checker reporta UNKNOWN (❔), NO "sin stock", asi no te da
  un falso negativo ni te spamea.

Las alertas se mandan a TODOS los chats listados en TELEGRAM_CHAT_IDS
(separados por coma, ej: "123456789,-100987654321").

Uso:
  python checker.py                     # corre todos los chequeos y avisa por Telegram
  python checker.py --test-telegram     # manda un mensaje de prueba a todos los chats
  python checker.py --get-chat-id       # imprime los chat IDs que le hablaron al bot
  python checker.py --once-report       # corre y solo imprime, sin avisar (debug)
"""
import os
import sys
import json
from datetime import datetime, timezone, timedelta

import requests

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


# ---------------- Target (RedSky API) ----------------
def target_pdp(tcin):
    return f"https://www.target.com/p/-/A-{tcin}"


def check_target(cfg):
    results = []
    key = cfg.get("redsky_key", "")
    store = str(cfg.get("store_id", ""))
    zip_ = str(cfg.get("zip", "10001"))
    for tcin in cfg.get("tcins", []):
        params = {
            "key": key, "tcin": str(tcin), "is_bot": "false",
            "store_id": store, "zip": zip_, "state": "NY",
            "latitude": "40.71", "longitude": "-74.00",
            "channel": "WEB", "page": f"/p/A-{tcin}",
        }
        try:
            r = requests.get(
                "https://redsky.target.com/redsky_aggregations/v1/web/pdp_fulfillment_v1",
                params=params,
                headers={"User-Agent": UA, "Accept": "application/json"},
                timeout=25,
            )
            if r.status_code in (403, 429):
                results.append(result("Target", f"TCIN {tcin}", UNKNOWN,
                                      target_pdp(tcin), f"HTTP {r.status_code} (bloqueado)"))
                continue
            r.raise_for_status()
            data = r.json()
            product = ((data.get("data") or {}).get("product") or {})
            ship = ((product.get("fulfillment") or {}).get("shipping_options") or {})
            avail = (ship.get("availability_status") or "").upper()
            if avail in ("IN_STOCK", "LIMITED_STOCK"):
                status = IN_STOCK
            elif avail:
                status = OUT_OF_STOCK
            else:
                status = UNKNOWN
            results.append(result("Target", f"TCIN {tcin}", status,
                                  target_pdp(tcin), f"shipping={avail or 'n/a'}"))
        except Exception as e:  # noqa: BLE001
            results.append(result("Target", f"TCIN {tcin}", UNKNOWN,
                                  target_pdp(tcin), f"error: {e}"))
    return results


# ---------------- Retailers via scraping HTML (best-effort) ----------------
OUT_PATTERNS = [
    "sold out", "out of stock", "currently unavailable",
    "temporarily out of stock", "coming soon", "notify me when",
    "out-of-stock", "soldout", "get notified",
]
IN_PATTERNS = [
    "add to cart", "add to bag", "addtocart", "add-to-cart", "buy now",
]
BLOCK_PATTERNS = [
    "captcha", "are you a human", "robot or human", "press & hold",
    "press and hold", "access denied", "unusual traffic",
    "verify you are", "px-captcha", "enable javascript",
]


def check_one_html(name, url):
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=25)
        body = r.text.lower()
        if r.status_code in (403, 429, 503) or any(b in body for b in BLOCK_PATTERNS):
            return result(name, url, UNKNOWN, url, f"bloqueado (HTTP {r.status_code})")
        if r.status_code >= 400:
            return result(name, url, UNKNOWN, url, f"HTTP {r.status_code}")
        has_out = any(p in body for p in OUT_PATTERNS)
        has_in = any(p in body for p in IN_PATTERNS)
        # Priorizo "sin stock" para evitar falsas alarmas (spam).
        if has_out:
            return result(name, url, OUT_OF_STOCK, url, "detecto 'sin stock'")
        if has_in:
            return result(name, url, IN_STOCK, url, "detecto 'add to cart'")
        return result(name, url, UNKNOWN, url, "sin senales claras")
    except Exception as e:  # noqa: BLE001
        return result(name, url, UNKNOWN, url, f"error: {e}")


def make_html_checker(display_name):
    def _check(cfg):
        return [check_one_html(display_name, u) for u in cfg.get("urls", [])]
    return _check


CHECKERS = {
    "bestbuy": make_html_checker("Best Buy"),
    "target": check_target,
    "walmart": make_html_checker("Walmart"),
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
