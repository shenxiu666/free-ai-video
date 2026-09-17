"""opencode 二进制管理契约测试（docs/05 §5.8）。

只测纯逻辑与校验；版本探测仅在内置二进制存在时跑一条（缺失则 skip）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import opencode_manager as om  # noqa: E402


def test_modes_locked():
    assert set(om.MODES) == {"builtin", "system", "custom", "auto"}
    assert om.DEFAULT_MODE == "builtin"


def test_parse_version_output():
    assert om.parse_version_output("1.18.31\n") == "1.18.31"
    assert om.parse_version_output("opencode v1.18.31 (abc)") == "1.18.31"
    assert om.parse_version_output("") == ""


def test_resolve_priority_explicit_first():
    r = om.resolve_bin(mode="builtin", bin_path="C:\\custom\\opencode.exe",
                       env_bin="C:\\env\\opencode.exe",
                       builtin="C:\\builtin\\opencode.exe",
                       path_bin="C:\\path\\opencode.exe")
    assert r == {"path": "C:\\custom\\opencode.exe", "source": "custom",
                "reason": r["reason"]}


def test_resolve_env_over_mode():
    r = om.resolve_bin(mode="builtin", bin_path="",
                       env_bin="C:\\env\\opencode.exe",
                       builtin="C:\\builtin\\opencode.exe")
    assert r["source"] == "env" and r["path"] == "C:\\env\\opencode.exe"


def test_resolve_builtin_default():
    r = om.resolve_bin(mode="builtin", bin_path="", env_bin="",
                       builtin="C:\\builtin\\opencode.exe")
    assert r["source"] == "builtin"


def test_resolve_auto_prefers_builtin(tmp_path):
    exe = tmp_path / "opencode.exe"
    exe.write_bytes(b"x")
    r = om.resolve_bin(mode="auto", builtin=str(exe), path_bin="C:\\path\\opencode.exe")
    assert r["source"] == "builtin"


def test_resolve_auto_falls_back_to_path(tmp_path):
    r = om.resolve_bin(mode="auto", builtin=str(tmp_path / "missing.exe"),
                       path_bin="C:\\path\\opencode.exe")
    assert r["source"] == "path"


def test_resolve_custom_without_path():
    r = om.resolve_bin(mode="custom")
    assert r["path"] is None


def test_save_selection_rejects_bad_mode(tmp_path):
    with pytest.raises(ValueError):
        om.save_selection("nope", "", config_path=tmp_path / "opencode.yaml")


def test_save_selection_custom_requires_existing_file(tmp_path):
    with pytest.raises(ValueError):
        om.save_selection("custom", "", config_path=tmp_path / "opencode.yaml")
    with pytest.raises(ValueError):
        om.save_selection("custom", str(tmp_path / "missing.exe"),
                          config_path=tmp_path / "opencode.yaml")


def test_save_load_roundtrip(tmp_path):
    cfg = tmp_path / "opencode.yaml"
    exe = tmp_path / "opencode.exe"
    exe.write_bytes(b"x")
    om.save_selection("custom", str(exe), config_path=cfg)
    sel = om.load_selection(config_path=cfg)
    assert sel == {"mode": "custom", "bin_path": str(exe)}
    om.save_selection("builtin", "", config_path=cfg)
    assert om.load_selection(config_path=cfg)["mode"] == "builtin"


def test_load_selection_env_override(tmp_path, monkeypatch):
    cfg = tmp_path / "opencode.yaml"
    om.save_selection("builtin", "", config_path=cfg)
    monkeypatch.setenv("OPENCODE_MODE", "system")
    assert om.load_selection(config_path=cfg)["mode"] == "system"
    monkeypatch.setenv("OPENCODE_BIN", "C:\\env\\opencode.exe")
    assert om.load_selection(config_path=cfg)["bin_path"] == "C:\\env\\opencode.exe"


def test_builtin_version_matches_probe():
    if not om.builtin_path().is_file():
        pytest.skip("内置二进制未配给（跑 tools/opencode/install.ps1）")
    ver = om.get_version(str(om.builtin_path()))
    assert ver == om.pinned_version() != ""


def test_api_endpoints_present():
    from fastapi.testclient import TestClient

    import main as m

    c = TestClient(m.app)
    st = c.get("/api/opencode/status")
    assert st.status_code == 200
    body = st.json()
    assert body["ok"] is True
    assert body["selection"]["mode"] in list(om.MODES)
    assert "effective" in body and "candidates" in body
    assert any(k["source"] == "builtin" for k in body["candidates"])
    bad = c.post("/api/opencode/select", json={"mode": "nope"})
    assert bad.status_code == 400


PLAIN_SAMPLE = '''opencode/big-pickle
opencode/mimo-v2.5-free
agnes/agnes-3.0-flash
not a model line
'''

VERBOSE_SAMPLE = '''opencode/big-pickle
{
  "id": "big-pickle",
  "providerID": "opencode",
  "name": "Big Pickle",
  "api": {"url": "https://opencode.ai/zen/v1"},
  "cost": {"input": 0, "output": 0},
  "limit": {"context": 200000, "output": 32000}
}
agnes/agnes-2.5-pro-alpha
{
  "id": "agnes-2.5-pro-alpha",
  "providerID": "agnes",
  "name": "Agnes 2.5 Pro Alpha",
  "api": {"url": "https://apihub.agnes-ai.com/v1"},
  "cost": {"input": 0.45, "output": 0.9}
}
broken/broken-model
{not json}
'''


def test_parse_models_plain():
    items = om.parse_models_plain(PLAIN_SAMPLE)
    assert [(i['provider'], i['model']) for i in items] == [
        ('opencode', 'big-pickle'), ('opencode', 'mimo-v2.5-free'),
        ('agnes', 'agnes-3.0-flash')]


def test_parse_models_verbose_free_flag():
    items = om.parse_models_verbose(VERBOSE_SAMPLE)
    by_ref = {i['ref']: i for i in items}
    assert by_ref['opencode/big-pickle']['free'] is True
    assert by_ref['opencode/big-pickle']['url'] == 'https://opencode.ai/zen/v1'
    assert by_ref['opencode/big-pickle']['context'] == 200000
    assert by_ref['agnes/agnes-2.5-pro-alpha']['free'] is False
    assert by_ref['broken/broken-model']['free'] is None


def test_list_models_curated_when_no_binary(monkeypatch):
    monkeypatch.setattr(om, 'effective_binary',
                        lambda selection=None: {'path': None, 'exists': False,
                                                'reason': 'none'})
    res = om.list_models()
    assert res['source'] == 'curated'
    assert any(m['ref'] == 'opencode/mimo-v2.5-free' for m in res['models'])


def test_list_models_live_parses_verbose(monkeypatch):
    monkeypatch.setattr(om, 'effective_binary',
                        lambda selection=None: {'path': 'C:/fake/opencode.exe',
                                                'exists': True})
    monkeypatch.setattr(om, '_run_models', lambda *a, **k: VERBOSE_SAMPLE)
    res = om.list_models(provider='opencode')
    assert res['source'] == 'live'
    assert res['models'][0]['ref'] == 'opencode/big-pickle'


def test_models_api_live_and_curated(monkeypatch):
    from fastapi.testclient import TestClient

    import main as m

    c = TestClient(m.app)
    live = c.get('/api/opencode/models?provider=opencode')
    assert live.status_code == 200
    body = live.json()
    assert body['ok'] is True and body['source'] in ('live', 'curated')
    assert any(x['ref'].startswith('opencode/') for x in body['models'])
    monkeypatch.setattr(m._opencode_mgr, 'list_models',
                        lambda **k: {'source': 'curated', 'bin': None,
                                     'models': [], 'note': 'stub'})
    stubbed = c.get('/api/opencode/models')
    assert stubbed.json()['source'] == 'curated'


def test_zen_goes_via_cli_without_key(monkeypatch):
    from providers.text import generate_text  # noqa: E402
    import providers.text as _t

    seen: dict = {}

    def _fake_run_text(bin_path="", model="", prompt="", system="", timeout=60.0):
        seen.update(bin_path=bin_path, model=model, prompt=prompt, system=system)
        return {"text": "OK", "cost": {"total": 1}}

    monkeypatch.setattr(_t._om, "run_text", _fake_run_text)
    res = generate_text("hi", provider="opencode-zen", model="mimo-v2.5-free")
    assert res["text"] == "OK"
    assert seen["model"] == "opencode/mimo-v2.5-free"  # 无前缀自动补 opencode/
    assert seen["prompt"] == "hi"  # 禁工具护栏由 manager.run_text 统一追加
    assert res["generated_by"].endswith("(cli)")


def test_run_text_appends_no_tools_guard(monkeypatch):
    import subprocess as _sp

    import opencode_manager as _mgr

    seen_argv: dict = {}

    class _Proc:
        returncode = 0
        stdout = '{"type":"text","part":{"type":"text","text":"OK"}}\n'
        stderr = ""

    def _fake_run(argv, **kw):
        seen_argv["argv"] = argv
        return _Proc()

    monkeypatch.setattr(_sp, "run", _fake_run)
    _mgr.run_text(bin_path="C:/fake/opencode.exe", model="opencode/m", prompt="hi")
    assert "不要调用任何工具" in seen_argv["argv"][-1]


def test_run_text_parses_json_events(monkeypatch):
    import subprocess as _sp

    import opencode_manager as _mgr

    out = ('{"type":"step_start","part":{"type":"step-start"}}\n'
           'not json line\n'
           '{"type":"text","part":{"type":"text","text":"OK"}}\n'
           '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":5,"input":4,"output":1}}}\n')

    class _Proc:
        returncode = 0
        stdout = out
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Proc())
    res = _mgr.run_text(bin_path="C:/fake/opencode.exe", model="opencode/m",
                        prompt="hi")
    assert res == {"text": "OK", "cost": {"total": 5, "input": 4, "output": 1}}


def test_run_text_no_output_raises(monkeypatch):
    import subprocess as _sp

    import opencode_manager as _mgr

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Proc())
    with pytest.raises(RuntimeError):
        _mgr.run_text(bin_path="C:/fake/opencode.exe", model="opencode/m", prompt="hi")