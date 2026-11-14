"""Демо-режим «только просмотр»: запись отклоняется, данные не меняются."""

from __future__ import annotations


def _stock(client):
    return client.get("/api/stock").json()


def _cell(data, sku, ff_name):
    ff_id = next(w["id"] for w in data["warehouses"] if w["name"] == ff_name)
    row = next(r for r in data["rows"] if r["sku"] == sku)
    return row["cells"][str(ff_id)] if str(ff_id) in row["cells"] else row["cells"][ff_id]


def test_pages_show_read_only_banner(client, shop, monkeypatch):
    monkeypatch.setenv("DEMO_READ_ONLY", "1")
    for path in ("/", "/orders", "/warehouses", "/history"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Демо-режим" in response.text, path


def test_form_post_rejected_and_data_unchanged(client, shop, monkeypatch):
    monkeypatch.setenv("DEMO_READ_ONLY", "1")
    before = _stock(client)
    response = client.post(
        "/receipts",
        data={
            "sku_id": shop["products"]["SKU-101"].id,
            "ff_id": shop["north"].id,
            "qty": 5,
            "idem": "readonly-form-1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "msg=" in response.headers["location"]
    assert "info" in response.headers["location"]
    assert _stock(client) == before


def test_api_post_rejected_and_data_unchanged(client, shop, monkeypatch):
    monkeypatch.setenv("DEMO_READ_ONLY", "1")
    before = _cell(_stock(client), "SKU-101", "Север")
    response = client.post(
        "/api/fbs-orders",
        json={
            "sku_id": shop["products"]["SKU-101"].id,
            "ff_id": shop["north"].id,
            "qty": 4,
            "idempotency_key": "readonly-api-1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _cell(_stock(client), "SKU-101", "Север") == before


def test_writes_still_work_without_flag(client, shop, monkeypatch):
    monkeypatch.delenv("DEMO_READ_ONLY", raising=False)
    before = _cell(_stock(client), "SKU-101", "Север")
    response = client.post(
        "/receipts",
        data={
            "sku_id": shop["products"]["SKU-101"].id,
            "ff_id": shop["north"].id,
            "qty": 2,
            "idem": "readonly-off-1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _cell(_stock(client), "SKU-101", "Север")["physical"] == before["physical"] + 2
