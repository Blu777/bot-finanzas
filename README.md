# Bot Finanzas / mp-sync

Bot de Telegram para registrar movimientos financieros en Firefly III desde:

- **Mensajes en lenguaje natural**: `gasté 15k en sushi`, `uber 12 lucas`, `me entraron 300 usd`.
- **CSV de Mercado Pago**: importación a Firefly con control de duplicados.
- **Categorización asistida por Gemini**: clasificación de transacciones pendientes y fallback del parser.

Versión actual del bot: **1.4**.

## Funcionalidades

- **Parser rule-based híbrido**
  - Montos con `k`, `lucas`, `mil`, `palo`.
  - Monedas `ARS` y `USD` mediante aliases como `$`, `pesos`, `usd`, `u$s`, `dólares`.
  - Fechas relativas: `hoy`, `ayer`, `anteayer`, días de semana y fechas `dd/mm`.
  - Detección de gastos, ingresos y transferencias entre cuentas propias.
  - Detección conservadora de múltiples transacciones en un mismo mensaje.
  - Confirmación obligatoria para casos ambiguos, cuotas o múltiples movimientos.

- **Integración con Firefly III**
  - Crea `withdrawal`, `deposit` o `transfer` según el movimiento.
  - Usa cuentas asset configuradas por alias.
  - Guarda ledger local SQLite para auditoría, reintentos e idempotencia.

- **Bot de Telegram**
  - Comandos de estado, categorías, reglas, búsqueda, últimos movimientos, retry y deshacer.
  - Botones de confirmación para parseos dudosos.
  - Restricción por `TELEGRAM_ALLOWED_CHATS`.

## Estructura

```text
├── .github/
│   └── workflows/
│       └── build.yml
├── docker-compose.example.yml
├── firefly-truenas.example.yml
├── mp-sync/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── config.py
│   ├── telegram_bot.py
│   ├── firefly_client.py
│   ├── firefly_import.py
│   ├── gemini_categorizer.py
│   ├── nl_expense.py
│   ├── retry_utils.py
│   └── seed_rules.py
└── README.md
```

## Variables de entorno

Configurar estas variables en Docker/TrueNAS:

| Variable | Requerida | Descripción |
|---|---:|---|
| `TELEGRAM_BOT_TOKEN` | Sí | Token del bot de Telegram. |
| `TELEGRAM_ALLOWED_CHATS` | Recomendado | IDs de chat autorizados separados por coma. Vacío bloquea todos los comandos salvo `/id`. |
| `FIREFLY_URL` | Sí | URL base de Firefly III. |
| `FIREFLY_PERSONAL_TOKEN` | Sí | Personal Access Token de Firefly III. |
| `FIREFLY_ASSET_ACCOUNT_ID` | Sí | Cuenta asset default en Firefly. |
| `FIREFLY_ASSET_ACCOUNTS` | No | Alias de cuentas: `Efectivo:1,Banco:2,MP:3`. |
| `CURRENCY` | No | Moneda default. Default: `ARS`. |
| `RULE_GROUP_TITLE` | No | Grupo de reglas Firefly. Default: `mp-bot`. |
| `GEMINI_API_KEY` | No | API key de Gemini para fallback/categorización. |
| `GEMINI_MODEL` | No | Modelo Gemini. Default: `gemini-2.0-flash-lite`. |
| `LOCAL_LEDGER_CSV` | No | Ruta del ledger. Si termina en `.csv`, usa SQLite equivalente. Default: `/data/ledger.csv`. |

## Seguridad

- No subir archivos con secretos reales.
- `docker-compose.yml`, `.env`, DBs locales y CSVs están ignorados por `.gitignore`.
- Usar `docker-compose.example.yml` como plantilla pública.
- Antes del primer uso, enviar `/id` al bot y configurar `TELEGRAM_ALLOWED_CHATS`.

## Uso por Telegram

### Ejemplos simples

```text
gasté 15k en sushi
uber 12 lucas
me entraron 300 usd
alquiler 450000
ayer 15k nafta
```

### Transferencias

```text
pasé 20k de MP a Banco
saqué 30k del banco
transferencia a juan 20k
```

Las transferencias entre cuentas propias se guardan como `transfer`. Las transferencias a terceros se tratan como gasto/ingreso externo y requieren confirmación si hay ambigüedad.

### Múltiples transacciones

```text
nafta 15k y peaje 3k
sushi 15k, uber 8k y cafe 2k
```

El bot muestra un preview y pide confirmación antes de guardar.

### Cuotas

```text
heladera 300k en 6 cuotas
cuota 2/6 seguro 15000
```

Los casos de cuotas requieren confirmación para evitar registrar mal el gasto.

## Comandos

```text
/start /help         ayuda
/version             versión del bot
/id                  muestra chat_id
/estado              salud/configuración básica
/categorias          lista categorías Firefly
/reglas              lista reglas creadas por el bot
/aprender kw => cat  crea regla keyword -> categoría
/borrar_regla <id>   borra regla
/categorizar         categoriza pendientes con Gemini
/aplicar_reglas      reaplica reglas en Firefly
/ultimos [n]         últimos movimientos del ledger
/buscar <texto>      busca en ledger/imports
/retry               reintenta sync pendiente
/deshacer            borra última entrada local y Firefly si aplica
```

## Despliegue en TrueNAS / Docker

### Build local

```bash
docker build -t mp-sync:local ./mp-sync
```

### Custom App en TrueNAS

1. Copiar `docker-compose.example.yml` a `docker-compose.yml`.
2. Completar variables reales.
3. En TrueNAS: **Apps** -> **Discover Apps** -> **Custom App**.
4. Seleccionar **Install via YAML** y pegar el compose.
5. Instalar y revisar logs.

### Logs

```bash
docker logs -f mp_sync
```

## Importación CSV Mercado Pago

Adjuntar un CSV al bot. Soporta:

- Formato canónico: `Date`, `Description`, `Amount`, `External_ID`.
- Statement de Mercado Pago con columnas compatibles.

El importador consulta `external_id` en Firefly para evitar duplicados.

## Desarrollo local

Instalar dependencias:

```bash
python -m pip install -r mp-sync/requirements.txt
```

Ejecutar bot:

```bash
python mp-sync/telegram_bot.py
```

Configurar previamente las variables de entorno requeridas.

## Notas de GitHub

- Los tests locales están ignorados por decisión del proyecto.
- No commitear `docker-compose.yml`, `.env`, CSVs, SQLite ni logs.
- `docker-compose.example.yml` debe mantenerse sin secretos reales.
