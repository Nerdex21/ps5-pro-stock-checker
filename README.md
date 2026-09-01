# 🎮 PS5 Pro Stock Checker

Chequea cada 1 hora el stock de la PS5 Pro en minoristas de EE.UU. y te manda un
mensaje por **Telegram** cuando aparece stock. Corre gratis en **GitHub Actions**
(sin servidor, sin tu PC prendida).

## Cómo funciona

| Minorista | Método | Confiabilidad |
|---|---|---|
| **Best Buy** | API oficial de desarrolladores | ⭐ Alta |
| **Target** | RedSky API (semi-oficial) | Media |
| **Walmart / Amazon / GameStop / PS Direct** | Scraping best-effort | Baja desde la nube* |

\* Estos sitios bloquean IPs de datacenter. Cuando el chequeo es bloqueado, el
checker lo marca `UNKNOWN` (❔) en el log — **nunca** lo reporta como "sin stock"
ni manda falsas alarmas.

Solo avisa cuando un retailer **pasa a tener stock** (y re-avisa cada 6 h si
sigue disponible, configurable con `recheck_alert_hours`). El estado entre
corridas se guarda en `state.json` (el workflow lo commitea solo cuando cambia).

## Setup (una sola vez, ~10 min)

### 1. Telegram

Ya tenés un bot → necesitás su **token** y tu **chat ID**:

1. El token te lo dio @BotFather (formato `123456:ABC-...`).
2. Mandale cualquier mensaje a tu bot desde tu cuenta.
3. Conseguí tu chat ID:
   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "TU_TOKEN"
   python checker.py --get-chat-id
   ```
4. Probá que funcione:
   ```powershell
   $env:TELEGRAM_CHAT_ID = "TU_CHAT_ID"
   python checker.py --test-telegram
   ```

### 2. Best Buy API key (gratis)

1. Registrate en <https://developer.bestbuy.com> y pedí una API key (llega por mail, es al toque).
2. (Opcional pero recomendado) Encontrá el SKU exacto de la consola:
   ```powershell
   $env:BESTBUY_API_KEY = "TU_KEY"
   python checker.py --find-bestbuy-sku
   ```
   Pegá el SKU en `config.json → retailers.bestbuy.skus`. Si lo dejás vacío,
   busca "PlayStation 5 Pro console" automáticamente (funciona, pero el SKU es más preciso).

### 3. IDs de los demás minoristas (opcional)

En `config.json`:

- **Target**: buscá el producto en target.com; el TCIN es el número al final de la URL (`/p/-/A-XXXXXXXX`) → pegalo en `tcins`.
- **Walmart / Amazon / GameStop / PS Direct**: pegá la URL completa del producto en `urls`.

Los que queden vacíos simplemente no se chequean. Para desactivar uno: `"enabled": false`.

### 4. Secrets en GitHub

En el repo: **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token de tu bot |
| `TELEGRAM_CHAT_ID` | tu chat ID |
| `BESTBUY_API_KEY` | tu key de Best Buy |

### 5. Probar

Pestaña **Actions → PS5 Pro stock check → Run workflow**. Mirá el log: vas a ver
el resumen ✅/❌/❔ por retailer. Después queda corriendo solo cada hora.

## Correr local (debug)

```powershell
pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN = "..."; $env:TELEGRAM_CHAT_ID = "..."; $env:BESTBUY_API_KEY = "..."
python checker.py --once-report   # solo imprime, no manda alertas
python checker.py                 # corrida real
```

## Apagarlo

Cuando ya compraste la consola 🎉: **Actions → PS5 Pro stock check → ⋯ → Disable workflow**
(o borrá el repo).
