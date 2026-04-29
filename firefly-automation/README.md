# firefly-automation / mp-sync

Servicio Python que cada hora trae los pagos aprobados de Mercado Pago de las
ultimas 24hs y los registra en Firefly III + Google Sheets.

## Componentes

- **Firefly III**: ya instalado como app de TrueNAS (puerto 30105).
- **mp-sync**: este servicio, desplegado como Custom App de TrueNAS.

## Estructura

```
firefly-automation/
├── docker-compose.yml      # YAML para pegar en TrueNAS Custom App
└── mp-sync/
    ├── Dockerfile
    ├── requirements.txt
    ├── sync.py
    └── credentials.json    # service account de Google (lo subis vos)
```

## Despliegue

### 1. Datasets ya creados

```
/mnt/HMS/appdata/firefly/stack/mp-sync/   # codigo + credentials.json
/mnt/HMS/appdata/firefly/mp-sync/logs/    # logs persistentes
```

### 2. Imagen ya construida en el host

```bash
docker build -t mp-sync:local /mnt/HMS/appdata/firefly/stack/mp-sync
```

(Re-ejecutar cuando se modifique `sync.py` o `requirements.txt`.)

### 3. Subir credentials.json

Desde la PC:

```powershell
scp .\credentials.json tiago@192.168.1.2:/mnt/HMS/appdata/firefly/stack/mp-sync/credentials.json
```

Compartir la planilla con el email del service account (rol Editor) y habilitar
**Google Sheets API** + **Drive API** en el proyecto de Google Cloud.

### 4. Tokens necesarios

- **MP_ACCESS_TOKEN**: https://www.mercadopago.com.ar/developers/panel/app -> tu app -> Credenciales.
- **FIREFLY_PERSONAL_TOKEN**: en Firefly UI -> Options -> Profile -> OAuth -> "Create new token".
- **FIREFLY_ASSET_ACCOUNT_ID**: ID de la cuenta asset (URL `/accounts/show/<ID>`).
- **GOOGLE_SHEET_ID**: ID de la planilla (`docs.google.com/spreadsheets/d/<ID>/edit`).

### 5. Crear la Custom App en TrueNAS

1. **Apps** -> **Discover Apps** -> boton **Custom App** (arriba a la derecha).
2. Modo **Install via YAML** -> pegar el contenido de `docker-compose.yml`.
3. Reemplazar los valores `REPLACE_ME_xxx` con los tokens reales.
4. Guardar / Install. La app aparece como `mp-sync` en el listado.

### 6. Verificar logs

```bash
docker logs -f mp_sync
# o desde TrueNAS UI: Apps -> mp-sync -> Logs
```

## Actualizar el codigo

```bash
# 1) editar sync.py o requirements.txt en /mnt/HMS/appdata/firefly/stack/mp-sync/
# 2) rebuild imagen
docker build -t mp-sync:local /mnt/HMS/appdata/firefly/stack/mp-sync
# 3) reiniciar la app desde TrueNAS UI (o: docker restart mp_sync)
```

## Notas

- `mp-sync` se conecta a Firefly III por la URL del host (`192.168.1.2:30105`),
  asi sobrevive a updates/reinicios de la app de Firefly sin depender de la red
  interna `ix-internal-firefly-iii-firefly-net`.
- Idempotencia: el script consulta `external_id:mp-<id>` en Firefly y la columna
  A de la Google Sheet antes de insertar, asi el loop horario no duplica gastos
  aunque la ventana sea de 24h.
- Logs rotan en `/mnt/HMS/appdata/firefly/mp-sync/logs/sync.log` (5 x 2MB).
