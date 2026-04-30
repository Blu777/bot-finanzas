"""Precarga categorias + reglas de keyword->categoria comunes para AR.

Uso:
  FIREFLY_URL=... FIREFLY_PERSONAL_TOKEN=... python seed_rules.py
"""
from __future__ import annotations

import logging
import os

from firefly_client import FireflyClient


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("seed")


# (palabra_clave, Categoria)
SEED: list[tuple[str, str]] = [
    # Supermercado
    ("carrefour", "Supermercado"),
    ("coto", "Supermercado"),
    ("disco", "Supermercado"),
    ("jumbo", "Supermercado"),
    ("vea", "Supermercado"),
    ("dia ", "Supermercado"),
    ("walmart", "Supermercado"),
    ("chango mas", "Supermercado"),
    ("changomas", "Supermercado"),
    # Transporte
    ("sube", "Transporte"),
    ("uber", "Transporte"),
    ("cabify", "Transporte"),
    ("didi", "Transporte"),
    ("ypf", "Transporte"),
    ("shell", "Transporte"),
    ("axion", "Transporte"),
    # Delivery
    ("rappi", "Delivery"),
    ("pedidos ya", "Delivery"),
    ("pedidosya", "Delivery"),
    ("glovo", "Delivery"),
    # Compras online
    ("mercado libre", "Compras online"),
    ("mercadolibre", "Compras online"),
    ("amazon", "Compras online"),
    ("aliexpress", "Compras online"),
    # Comida fuera
    ("starbucks", "Salidas"),
    ("mcdonalds", "Salidas"),
    ("burger king", "Salidas"),
    ("dean and dennys", "Salidas"),
    ("stop and coffee", "Salidas"),
    ("havanna", "Salidas"),
    # Servicios / suscripciones
    ("netflix", "Suscripciones"),
    ("spotify", "Suscripciones"),
    ("disney", "Suscripciones"),
    ("hbo", "Suscripciones"),
    ("youtube premium", "Suscripciones"),
    ("dlocal", "Suscripciones"),
    # Servicios publicos
    ("edesur", "Servicios publicos"),
    ("edenor", "Servicios publicos"),
    ("metrogas", "Servicios publicos"),
    ("aysa", "Servicios publicos"),
    ("aguas argentinas", "Servicios publicos"),
    ("personal", "Servicios publicos"),
    ("claro", "Servicios publicos"),
    ("movistar", "Servicios publicos"),
    ("telecentro", "Servicios publicos"),
    ("flow", "Servicios publicos"),
    # Inversiones
    ("rendimientos", "Inversiones"),
    ("rendimiento", "Inversiones"),
    # Transferencias (suelen ser P2P)
    ("transferencia recibida", "Transferencias"),
    ("transferencia enviada", "Transferencias"),
    # Pagos / creditos
    ("creditos de mercado pago", "Prestamos"),
    ("pago de cuota", "Prestamos"),
    # Movimientos internos MP (saldo reservado <-> disponible)
    ("dinero reservado", "Movimientos internos"),
    ("dinero retirado", "Movimientos internos"),
    ("dinero devuelto", "Movimientos internos"),
    ("dinero disponible", "Movimientos internos"),
    # Overrides de personas conocidas (deben ir DESPUES de "transferencia ..."
    # para que set_category sobreescriba al de Transferencias).
    ("paloma", "Supermercado"),  # super chino del barrio
]


def main() -> int:
    base = os.environ["FIREFLY_URL"]
    token = os.environ["FIREFLY_PERSONAL_TOKEN"]
    group_title = os.environ.get("RULE_GROUP_TITLE", "mp-bot")

    c = FireflyClient(base, token)
    log.info("Asegurando rule group '%s'", group_title)
    group = c.get_or_create_rule_group(group_title)
    gid = group["id"]

    log.info("Cargando %d reglas...", len(SEED))
    created = skipped = failed = 0
    for kw, cat in SEED:
        title = f"{kw} -> {cat}"
        try:
            c.get_or_create_category(cat)
            existing = c.find_rule_by_title(gid, title)
            if existing:
                log.info("  . exists  %s", title)
                skipped += 1
                continue
            c.create_keyword_to_category_rule(gid, kw, cat)
            log.info("  + created %s", title)
            created += 1
        except Exception as e:
            log.warning("  x error   %s : %s", title, e)
            failed += 1

    log.info("Hecho. created=%d skipped=%d failed=%d", created, skipped, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
