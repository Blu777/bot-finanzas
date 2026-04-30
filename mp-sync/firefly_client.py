"""Cliente HTTP para la API de Firefly III. Usado por el importador y el bot."""
from __future__ import annotations

import logging
from typing import Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


log = logging.getLogger(__name__)


class FireflyError(RuntimeError):
    pass


class FireflyClient:
    def __init__(self, base_url: str, token: str, timeout: int = 20, retries: int = 3):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            # Do not retry POST automatically: a timeout after Firefly accepted a
            # transaction could duplicate money movement.
            allowed_methods=frozenset({"GET", "PUT", "DELETE"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    # ---------- helpers ----------
    def _h(self, content_type: bool = False) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.api+json",
        }
        if content_type:
            h["Content-Type"] = "application/json"
        return h

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout)
        try:
            return self.session.request(method, f"{self.base}{path}", timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise FireflyError(f"{method.upper()} {path} fallo tras retries: {exc}") from exc

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self._request("GET", path, headers=self._h(), params=params)
        if r.status_code >= 300:
            raise FireflyError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        r = self._request("POST", path, headers=self._h(content_type=True), json=payload)
        if r.status_code >= 300:
            raise FireflyError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    def _delete(self, path: str) -> None:
        r = self._request("DELETE", path, headers=self._h())
        if r.status_code not in (200, 204):
            raise FireflyError(f"DELETE {path} -> {r.status_code}: {r.text[:300]}")

    def _paginate(self, path: str, params: dict | None = None) -> Iterable[dict]:
        page = 1
        params = dict(params or {})
        while True:
            params["page"] = page
            data = self._get(path, params=params)
            for item in data.get("data", []):
                yield item
            meta = data.get("meta", {}).get("pagination", {})
            if page >= meta.get("total_pages", page):
                return
            page += 1

    # ---------- transactions ----------
    def transaction_exists(self, external_id: str) -> bool:
        # /api/v1/search/transactions usa accept: application/json (no api+json)
        r = self._request(
            "GET",
            "/api/v1/search/transactions",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            params={"query": f"external_id:{external_id}"},
        )
        if r.status_code != 200:
            return False
        return len(r.json().get("data", [])) > 0

    def create_transaction(self, payload: dict) -> dict:
        return self._post("/api/v1/transactions", payload)

    def delete_transaction(self, group_id: str | int) -> None:
        self._delete(f"/api/v1/transactions/{group_id}")

    def search_transactions(self, query: str, limit: int = 200) -> list[dict]:
        """Devuelve transacciones que matcheen la query de busqueda."""
        out: list[dict] = []
        page = 1
        while True:
            r = self._request(
                "GET",
                "/api/v1/search/transactions",
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                params={"query": query, "page": page},
            )
            if r.status_code != 200:
                raise FireflyError(f"search -> {r.status_code}: {r.text[:300]}")
            data = r.json()
            out.extend(data.get("data", []))
            meta = data.get("meta", {}).get("pagination", {})
            if page >= meta.get("total_pages", page) or len(out) >= limit:
                break
            page += 1
        return out[:limit]

    def update_transaction_category(self, group_id: str | int, category_name: str) -> dict:
        """Setea category_name en TODOS los journals (splits) del grupo."""
        data = self._get(f"/api/v1/transactions/{group_id}")
        journals = data["data"]["attributes"]["transactions"]
        new_txs = []
        for j in journals:
            new_txs.append({
                "transaction_journal_id": j["transaction_journal_id"],
                "category_name": category_name,
            })
        payload = {"apply_rules": False, "fire_webhooks": False, "transactions": new_txs}
        r = self._request(
            "PUT",
            f"/api/v1/transactions/{group_id}",
            headers=self._h(content_type=True),
            json=payload,
        )
        if r.status_code >= 300:
            raise FireflyError(f"PUT tx/{group_id} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---------- categories ----------
    def list_categories(self) -> list[dict]:
        return list(self._paginate("/api/v1/categories"))

    def get_category_by_name(self, name: str) -> dict | None:
        for c in self.list_categories():
            if c["attributes"]["name"].lower() == name.lower():
                return c
        return None

    def get_or_create_category(self, name: str) -> dict:
        existing = self.get_category_by_name(name)
        if existing:
            return existing
        return self._post("/api/v1/categories", {"name": name})["data"]

    # ---------- rule groups ----------
    def list_rule_groups(self) -> list[dict]:
        return list(self._paginate("/api/v1/rule-groups"))

    def get_or_create_rule_group(self, title: str = "mp-bot") -> dict:
        for g in self.list_rule_groups():
            if g["attributes"]["title"].lower() == title.lower():
                return g
        return self._post(
            "/api/v1/rule-groups",
            {"title": title, "description": "Reglas creadas por mp-bot", "active": True},
        )["data"]

    # ---------- rules ----------
    def list_rules(self, rule_group_id: str | int | None = None) -> list[dict]:
        if rule_group_id is not None:
            return list(self._paginate(f"/api/v1/rule-groups/{rule_group_id}/rules"))
        return list(self._paginate("/api/v1/rules"))

    def delete_rule(self, rule_id: str | int) -> None:
        self._delete(f"/api/v1/rules/{rule_id}")

    def create_keyword_to_category_rule(
        self,
        rule_group_id: str | int,
        keyword: str,
        category_name: str,
        title: str | None = None,
    ) -> dict:
        """Crea una rule: si description contiene <keyword> -> set category <category_name>."""
        title = title or f"{keyword} -> {category_name}"
        payload = {
            "title": title,
            "rule_group_id": str(rule_group_id),
            "trigger": "store-journal",
            "active": True,
            "strict": False,
            "stop_processing": False,
            "triggers": [
                {"type": "description_contains", "value": keyword, "stop_processing": False, "active": True},
            ],
            "actions": [
                {"type": "set_category", "value": category_name, "stop_processing": False, "active": True},
            ],
        }
        return self._post("/api/v1/rules", payload)["data"]

    def find_rule_by_title(self, rule_group_id: str | int, title: str) -> dict | None:
        for r in self.list_rules(rule_group_id):
            if r["attributes"]["title"].lower() == title.lower():
                return r
        return None

    def trigger_rule_group(
        self,
        rule_group_id: str | int,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> None:
        """Aplica todas las reglas del grupo sobre transacciones existentes.
        Equivalente a "Execute group" en la UI de Firefly."""
        params: dict[str, str] = {}
        if start_date:
            params["start"] = start_date
        if end_date:
            params["end"] = end_date
        r = self._request(
            "POST",
            f"/api/v1/rule-groups/{rule_group_id}/trigger",
            headers=self._h(),
            params=params,
            timeout=max(self.timeout, 120),
        )
        if r.status_code >= 300:
            raise FireflyError(
                f"trigger rule group {rule_group_id} -> {r.status_code}: {r.text[:300]}"
            )
