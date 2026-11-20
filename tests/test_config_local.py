"""config/local.yaml: this machine's settings laid over global.yaml."""

import pytest

from clipwatcher import config


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("CLIPWATCHER_LOCAL", "1")
    (tmp_path / "global.yaml").write_text(
        "detector:\n  keep_q: 0.99\n  weights: {U: 0.3, M: 0.25}\nrender:\n  captions: word\n"
    )
    config.global_config.cache_clear()
    yield tmp_path
    config.global_config.cache_clear()


def test_local_settings_lie_over_the_global_ones(cfgdir):
    assert config.global_config()["detector"]["keep_q"] == 0.99
    p = config.save_local(
        {"detector": {"keep_q": 0.985, "weights": {"U": 0.4}}, "render": {"captions": "pop"}}
    )
    got = config.global_config()  # at once: saving clears what was cached
    assert (
        got["detector"] == {"keep_q": 0.985, "weights": {"U": 0.4, "M": 0.25}}
        and got["render"]["captions"] == "pop"
    )
    assert p.read_text().startswith("# Settings of this machine") and config.local_overrides()["render"] == {
        "captions": "pop"
    }
    config.save_local({})  # nothing left to override: the file goes, global.yaml stands alone
    assert not p.exists() and config.global_config()["detector"]["keep_q"] == 0.99


def test_tests_and_services_can_ignore_the_local_file(cfgdir, monkeypatch):
    config.save_local({"detector": {"keep_q": 0.5}})
    monkeypatch.setenv("CLIPWATCHER_LOCAL", "0")
    config.global_config.cache_clear()
    assert config.global_config()["detector"]["keep_q"] == 0.99 and config.local_overrides() == {}
