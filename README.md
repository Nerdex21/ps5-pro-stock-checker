# 🎮 PS5 Pro Stock Checker

Chequea cada 1 hora el stock de la PS5 Pro en minoristas de EE.UU. y manda un
mensaje por **Telegram** (a uno o varios chats) cuando aparece stock. Corre
gratis en **GitHub Actions** (sin servidor, sin tu PC prendida).

## Cómo funciona

| Minorista | Método | Confiabilidad |
|---|---|---|
| **Target** | RedSky API (semi-oficial) | Media |
| **Best Buy / Walmart / Amazon / GameStop / PS Direct** | Scraping best-effort | Baja desde la nube* |

\* Estos sitios bloquean IPs de datacenter. Cuando el chequeo es bloqueado, el
checker lo marca `UNKNOWN` (❔) en el log — **nunca** lo reporta como "sin stock"
ni manda falsas alarmas.

Solo avisa cuando un retailer **pasa a tener stock** (y re-avisa cada 6 h si
sigue disponible, configurable con `recheck_alert_hours`). El estado entre
corridas se guarda en `state.json` (el workflow lo commitea solo cuando cambia).

## Setup (una sola vez, ~10 min)

### 1. Telegram (soporta varios chats)

Necesitás el **token** de tu bot y los **chat IDs** de cada chat a avisar:

1. El token te lo dio @BotFather (formato `123456:ABC-...`).
2. Cada persona/grupo que quiera recibir avisos tiene que **mandarle al menos
   un mensaje al bot** (o agregarlo al grupo y escribir algo).
3. Listá los chat IDs que le hablaron al bot:
   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "TU_TOKEN"
   python checker.py --get-chat-id
   ```
4. Armá la lista separada por comas y probá:
   ```powershell
   $env:TELEGRAM_CHAT_IDS = "123456789,987654321"
   python checker.py --test-telegram
   ```

> Los IDs de grupos son negativos (ej: `-1001234567890`); van igual en la lista.

### 2. URLs / IDs de los productos

En `config.json`:

- **Target**: buscá el producto en target.com; el TCIN es el número al final de la URL (`/p/-/A-XXXXXXXX`) → pegalo en `tcins`.
- **Best Buy / Walmart / Amazon / GameStop / PS Direct**: pegá la URL completa del producto en `urls`.

Los que queden vacíos no se chequean. Para desactivar uno: `"enabled": false`.

### 3. Secrets en GitHub

En el repo: **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token de tu bot |
| `TELEGRAM_CHAT_IDS` | chat IDs separados por coma, ej: `123456789,987654321` |

### 4. Probar

Pestaña **Actions → PS5 Pro stock check → Run workflow**. Mirá el log: vas a ver
el resumen ✅/❌/❔ por retailer. Después queda corriendo solo cada hora.

## Correr local (debug)

```powershell
pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN = "..."; $env:TELEGRAM_CHAT_IDS = "id1,id2"
python checker.py --once-report   # solo imprime, no manda alertas
python checker.py                 # corrida real
```

## Apagarlo

Cuando ya compraste la consola 🎉: **Actions → PS5 Pro stock check → ⋯ → Disable workflow**
(o borrá el repo).
