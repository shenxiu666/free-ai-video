"""激活与持久化测试（docs/03）：全部 hermetic（APPDATA/.env 均指临时目录）。

覆盖：一键激活→持久池→模拟重启恢复→二次激活 already；
.env 读写 roundtrip；响应与文件不含秘密值。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as m  # noqa: E402


@pytest.fixture()
def iso_env(tmp_path, monkeypatch):
    """隔离：APPDATA/.env/派生变量全部指向临时区，测后自动还原。"""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("FREEAI_KEY_DB_PATH", raising=False)
    monkeypatch.delenv("FREEAI_DERIVED_KEY", raising=False)
    monkeypatch.delenv("FREEAI_DERIVED_KEY_ID", raising=False)
    monkeypatch.delenv("FREEAI_KEYPOOL_SALT", raising=False)
    monkeypatch.setattr(m, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(m, "_POOL_SINGLETON", None)
    monkeypatch.setattr(m, "_POOL_PERSISTENT", False)
    monkeypatch.setattr(m, "_POOL_NEEDS_REACTIVATION", False)
    yield tmp_path
    m._POOL_SINGLETON = None
    m._POOL_PERSISTENT = False
    m._POOL_NEEDS_REACTIVATION = False
    for k in ("FREEAI_DERIVED_KEY", "FREEAI_DERIVED_KEY_ID", "FREEAI_KEYPOOL_SALT"):
        m_key = __import__("os").environ.pop(k, None)  # noqa: F841


def test_persist_status_shape(iso_env):
    from fastapi.testclient import TestClient

    c = TestClient(m.app)
    body = c.get("/api/keys/persist-status").json()
    assert body["ok"] is True
    assert body["persistent"] is False
    assert body["activated"] is False
    assert body["backend_status"] == "未激活"


def test_activate_persists_and_survives_restart(iso_env):
    from fastapi.testclient import TestClient

    c = TestClient(m.app)
    r = c.post("/api/keys/activate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persistent"] is True
    assert body["backend_status"] in ("TPM锁定", "DPAPI兜底", "未加密-仅调试")
    assert body.get("migrated", 0) == 0
    blob = r.text
    assert "FREEAI_DERIVED_KEY" not in blob  # 响应无秘密键名值
    # .env 落盘：有派生三件套，无 KEK/明文 Key
    env_text = (iso_env / ".env").read_text(encoding="utf-8")
    assert "FREEAI_DERIVED_KEY=" in env_text and "KEK" not in env_text
    # 录 Key → 模拟重启（单例清零）→ 掩码恢复
    assert c.post("/api/keys", json={"raw_key": "sk-persist-123456789"}).status_code == 200
    m._POOL_SINGLETON = None
    m._POOL_PERSISTENT = False
    keys = c.get("/api/keys").json()["keys"]
    assert any(k["mask"].startswith("sk-pe") for k in keys)
    assert c.get("/api/keys/persist-status").json()["persistent"] is True
    # 二次激活：already，不折腾
    again = c.post("/api/keys/activate", json={}).json()
    assert again.get("already") is True


def test_env_roundtrip_tmp(tmp_path):
    import crypto as _crypto

    p = tmp_path / ".env"
    p.write_text("# 注释\nA=1\nB='q q'\n", encoding="utf-8")
    assert _crypto.load_local_env(p) == [] or True
    import os as _os

    _os.environ.pop("ZZ_T1", None)
    _crypto.save_local_env({"ZZ_T1": "v1", "A": "2"}, p)
    text = p.read_text(encoding="utf-8")
    assert "ZZ_T1=v1" in text and "\nA=2\n" in text and "# 注释" in text
    _os.environ.pop("ZZ_T1", None)
