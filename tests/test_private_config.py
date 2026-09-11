import json
from pathlib import Path

import pytest
import yaml

from job_monitor.applications import load_notion
from job_monitor.config import Settings, load_preferences, load_profile, load_sources


def sample_bundle():
    return {
        key: yaml.safe_load(Path(f"config/{key}.yaml").read_text())
        for key in ("preferences", "candidate", "sources", "notion")
    }


def test_private_json_used_by_all_loaders():
    bundle = sample_bundle()
    bundle["preferences"]["compensation_czk"]["high_min"] = 77777
    bundle["candidate"]["canonical_document"] = "Private synthetic profile"
    bundle["sources"]["sources"] = [{"id": "test", "kind": "ashby", "board": "example"}]
    bundle["notion"]["data_source_id"] = "11111111-1111-4111-8111-111111111111"
    settings = Settings(private_config_json=json.dumps(bundle))
    assert load_preferences(settings).compensation_czk["high_min"] == 77777
    assert load_profile(settings)["canonical_document"] == "Private synthetic profile"
    assert load_sources(settings)[0].id == "test"
    assert load_notion(settings).data_source_id == bundle["notion"]["data_source_id"]
    assert "Private synthetic profile" not in repr(settings)


def test_environment_bundle_overrides_local_file(tmp_path, monkeypatch):
    bundle = sample_bundle()
    bundle["candidate"]["canonical_document"] = "File profile"
    path = tmp_path / "private.json"
    path.write_text(json.dumps(bundle))
    assert load_profile(Settings(private_config_path=str(path)))["canonical_document"] == "File profile"
    bundle["candidate"]["canonical_document"] = "Environment profile"
    monkeypatch.setenv("PRIVATE_CONFIG_JSON", json.dumps(bundle))
    monkeypatch.setenv("PRIVATE_CONFIG_PATH", str(path))
    assert load_profile(Settings.from_env())["canonical_document"] == "Environment profile"


@pytest.mark.parametrize("value", ["private-malformed-input", "{}", "[]", "null", '{"candidate": {}}'])
def test_invalid_bundle_does_not_fall_back_or_echo_contents(value):
    with pytest.raises(ValueError) as caught:
        load_profile(Settings(private_config_json=value))
    assert str(caught.value) == "Private configuration is missing or invalid; contents omitted"


def test_missing_private_file_does_not_fall_back(tmp_path):
    with pytest.raises(ValueError, match="contents omitted"):
        load_profile(Settings(private_config_path=str(tmp_path / "missing.json")))


def test_large_split_bundle_from_environment(monkeypatch):
    bundle = sample_bundle()
    bundle["candidate"]["canonical_document"] = "Private synthetic profile " * 2500
    raw = json.dumps(bundle, ensure_ascii=True)
    parts = [raw[i:i + 24000] for i in range(0, len(raw), 24000)]
    monkeypatch.setenv("PRIVATE_CONFIG_JSON", "")
    monkeypatch.setenv("PRIVATE_CONFIG_PATH", "/missing/local/config.json")
    for i in range(1, 5):
        monkeypatch.setenv(f"PRIVATE_CONFIG_JSON_{i}", parts[i - 1] if i <= len(parts) else "")
    settings = Settings.from_env()
    assert settings.has_private_config
    assert load_profile(settings) == bundle["candidate"]
    assert load_preferences(settings) == load_preferences(Settings(private_config_json=raw))
    assert load_sources(settings) == []
    assert load_notion(settings) == load_notion(Settings(private_config_json=raw))
    assert "Private synthetic profile" not in repr(settings)


@pytest.mark.parametrize("parts", [
    {"private_config_json_2": "secret"},
    {"private_config_json_1": "secret", "private_config_json_3": "secret"},
    {"private_config_json_1": '{"candidate":'},
    {"private_config_json_1": "secret", "private_config_json": "{}"},
])
def test_invalid_parts_do_not_fall_back_or_echo_contents(parts):
    with pytest.raises(ValueError) as caught:
        load_profile(Settings(**parts))
    assert str(caught.value) == "Private configuration is missing or invalid; contents omitted"


def test_cli_private_error_is_actionable_without_exposing_input(monkeypatch, capsys):
    from job_monitor import cli

    def fail():
        load_profile(Settings(private_config_json_1="private-sensitive-marker", private_config_json="{}"))

    monkeypatch.setattr(cli, "main", fail)
    with pytest.raises(SystemExit) as caught:
        cli.run()
    output = capsys.readouterr().err
    assert caught.value.code == 1
    assert "Remove PRIVATE_CONFIG_JSON" in output
    assert "private-sensitive-marker" not in output


def test_cli_unexpected_error_remains_redacted(monkeypatch, capsys):
    from job_monitor import cli

    def fail():
        raise ValueError("private-sensitive-marker")

    monkeypatch.setattr(cli, "main", fail)
    with pytest.raises(SystemExit):
        cli.run()
    output = capsys.readouterr().err
    assert "ValueError" in output
    assert "private-sensitive-marker" not in output


def test_public_examples_are_synthetic_and_have_no_sources():
    settings = Settings()
    assert load_profile(settings)["canonical_document"] == "Fictional example profile"
    assert load_sources(settings) == []
    assert load_notion(settings).data_source_id == "00000000-0000-0000-0000-000000000000"
    assert "DEMONSTRATION ONLY" in load_preferences(settings).policy_text
