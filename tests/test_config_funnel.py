"""Tests for funnel-optimization config additions."""

def test_defaults_has_new_keys():
    from applypilot import config
    assert config.DEFAULTS["min_score"] == 8
    assert config.DEFAULTS["max_job_age_days"] == 14
    assert config.DEFAULTS["max_in_flight_per_company"] == 3
    assert config.DEFAULTS["in_flight_window_days"] == 30


def test_get_company_limit_uses_defaults_when_no_yaml(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    config._company_limits_cache = None
    cap, window = config.get_company_limit("anycorp")
    assert cap == 3
    assert window == 30


def test_get_company_limit_honors_defaults_override(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "company_limits.yaml").write_text("""
defaults:
  max_in_flight: 5
  window_days: 7
""".strip(), encoding="utf-8")
    config._company_limits_cache = None
    cap, window = config.get_company_limit("anycorp")
    assert cap == 5
    assert window == 7


def test_get_company_limit_honors_per_company_override(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "company_limits.yaml").write_text("""
defaults:
  max_in_flight: 3
  window_days: 30
overrides:
  netflix:
    max_in_flight: 1
  stripe:
    max_in_flight: 5
    window_days: 14
""".strip(), encoding="utf-8")
    config._company_limits_cache = None
    assert config.get_company_limit("netflix") == (1, 30)
    assert config.get_company_limit("NETFLIX") == (1, 30)
    assert config.get_company_limit("stripe") == (5, 14)
    assert config.get_company_limit("unlisted") == (3, 30)


def test_get_company_limit_unlimited_cap(tmp_path, monkeypatch):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "company_limits.yaml").write_text("""
overrides:
  openai:
    max_in_flight: -1
""".strip(), encoding="utf-8")
    config._company_limits_cache = None
    cap, _ = config.get_company_limit("openai")
    assert cap == -1


def test_get_company_limit_malformed_yaml_falls_back(tmp_path, monkeypatch, caplog):
    import logging
    from applypilot import config
    caplog.set_level(logging.WARNING, logger="applypilot.config")
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "company_limits.yaml").write_text("{this: is: not: valid]", encoding="utf-8")
    config._company_limits_cache = None
    cap, window = config.get_company_limit("anycorp")
    assert cap == 3
    assert window == 30
    assert "Failed to parse" in caplog.text
