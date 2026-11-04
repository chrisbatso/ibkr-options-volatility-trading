import pytest

from market_watcher.common import config_flag


class FakeContext:
    def __init__(self, **config):
        self.config = config


@pytest.fixture
def set_config(monkeypatch):
    def _set(**config):
        monkeypatch.setattr("market_watcher.common.context", FakeContext(**config))

    return _set


@pytest.mark.parametrize("value", ["true", "True", " TRUE ", "yes", "on", "1"])
def test_truthy_values(set_config, value):
    set_config(FLAG=value)

    assert config_flag("FLAG", default=False) is True


@pytest.mark.parametrize("value", ["false", "False", "no", "off", "0"])
def test_falsy_values(set_config, value):
    set_config(FLAG=value)

    assert config_flag("FLAG", default=True) is False


@pytest.mark.parametrize("config", [{}, {"FLAG": ""}, {"FLAG": "   "}])
def test_unset_or_blank_value_uses_the_default(set_config, config):
    set_config(**config)

    assert config_flag("FLAG", default=True) is True
    assert config_flag("FLAG", default=False) is False


def test_non_boolean_value_raises(set_config):
    set_config(FLAG="maybe")

    with pytest.raises(ValueError, match="FLAG"):
        config_flag("FLAG", default=True)
