"""Phase 4 头像图库：库函数与路由共用本文件的 fixture（对齐 test_bot_routes.py 风格）。"""
import database as db


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
