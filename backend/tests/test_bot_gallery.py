"""Phase 4 头像图库：库函数与路由共用本文件的 fixture（对齐 test_bot_routes.py 风格）。"""
import os

import fastapi
import pytest
from fastapi.testclient import TestClient

import database as db


@pytest.fixture
def client(tmp_workspace):
    db._close_all_connections()
    db.init_db(os.path.join(tmp_workspace, "bot-gallery.db"))
    from routes.bot import router

    app = fastapi.FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as test_client:
        yield test_client
    db._close_all_connections()


def test_gallery_create_list_delete(base_db):
    item = db.create_bot_gallery_item("data:image/webp;base64," + "A" * 100, label="小猫")
    assert item["id"] and item["label"] == "小猫"
    assert item["data_url"].startswith("data:image/webp;base64,")
    assert item["created_at"]

    listed = db.list_bot_gallery()
    assert [i["id"] for i in listed] == [item["id"]]

    assert db.delete_bot_gallery_item(item["id"]) is True
    assert db.delete_bot_gallery_item(item["id"]) is False
    assert db.list_bot_gallery() == []


def test_gallery_label_defaults_empty(base_db):
    item = db.create_bot_gallery_item("data:image/png;base64,BBBB")
    assert item["label"] == ""


def test_gallery_table_migration_is_idempotent(base_db):
    # 二次 init 不得因表已存在而报错（幂等前提）
    db._close_all_connections()
    db.init_db(base_db)
    assert db.list_bot_gallery() == []


def test_gallery_route_crud(client):
    url = "data:image/webp;base64," + "A" * 100
    created = client.post("/api/bot/gallery", json={"label": "小猫", "data_url": url})
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]

    assert client.get("/api/bot/gallery").json()[0]["id"] == item_id
    assert client.delete(f"/api/bot/gallery/{item_id}").json() == {"deleted": True}
    assert client.get("/api/bot/gallery").json() == []
    assert client.delete(f"/api/bot/gallery/{item_id}").status_code == 404


def test_gallery_route_rejects_non_image_and_oversize(client):
    # 与头像同口径：非图片 data URL / 表情 / 超 400_000 一律 422
    for bad in ["data:text/plain;base64,AAAA", "🐟", "data:image/webp;base64," + "A" * 400_001]:
        assert client.post("/api/bot/gallery", json={"data_url": bad}).status_code == 422
    # data_url 必填
    assert client.post("/api/bot/gallery", json={"label": "无图"}).status_code == 422
