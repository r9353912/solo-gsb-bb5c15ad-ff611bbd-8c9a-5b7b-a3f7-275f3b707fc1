"""测试隔离：每个测试模块使用独立的临时 SQLite 数据库。

各测试模块在导入时把期望的数据库文件名写入 ``DB_BASENAME``（通过模块全局
变量），conftest 在模块级夹具建立前设置 TRACE_DB_PATH、清掉旧文件并重置
配置缓存，避免多模块共库导致数据串扰。
"""
from __future__ import annotations

import os
import pathlib

import pytest


@pytest.fixture(scope="module")
def client(request):
    basename = getattr(request.module, "DB_BASENAME", None) or (
        "trace_test_%s" % request.module.__name__
    )
    db_path = "/tmp/%s.db" % basename
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(db_path + suffix)
        if p.exists():
            p.unlink()
    os.environ["TRACE_DB_PATH"] = db_path

    from fastapi.testclient import TestClient

    from app.config import reset_settings_cache
    from app.main import app

    reset_settings_cache()
    with TestClient(app) as c:
        yield c
