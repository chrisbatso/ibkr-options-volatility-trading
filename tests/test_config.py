import pytest

from market_watcher.config import config_flag, config_float, config_int


class FakeContext:
    def __init__(self, **config):
        self.config = config


@pytest.fixture
def set_config(monkeypatch):
    def _set(**config):
        monkeypatch.setattr("market_watcher.config.context", FakeContext(**config))

    return _set


# --- config_flag -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "True", " TRUE ", "yes", "on", "1"])
def test_truthy_values(set_config, value):
    set_config(FLAG=value)

    assert config_flag("FLAG", default=False) is True


@pytest.mark.parametrize("value", ["false", "False", "no", "off", "0"])
def test_falsy_values(set_config, value):
    set_config(FLAG=value)

    assert config_flag("FLAG", default=True) is False


@pytest.mark.parametrize("config", [{}, {"FLAG": ""}, {"FLAG": "   "}])
def test_unset_or_blank_flag_uses_the_default(set_config, config):
    set_config(**config)

    assert config_flag("FLAG", default=True) is True
    assert config_flag("FLAG", default=False) is False


def test_non_boolean_value_raises(set_config):
    set_config(FLAG="maybe")

    with pytest.raises(ValueError, match="FLAG"):
        config_flag("FLAG", default=True)


# --- config_int ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [("30", 30), (" 0 ", 0), ("-5", -5)])
def test_integer_values(set_config, value, expected):
    set_config(MINUTES=value)

    assert config_int("MINUTES", default=99) == expected


@pytest.mark.parametrize("config", [{}, {"MINUTES": ""}, {"MINUTES": "  "}])
def test_unset_or_blank_int_uses_the_default(set_config, config):
    set_config(**config)

    assert config_int("MINUTES", default=30) == 30


@pytest.mark.parametrize("value", ["thirty", "30.5", "1e3"])
def test_non_integer_value_raises(set_config, value):
    set_config(MINUTES=value)

    with pytest.raises(ValueError, match="MINUTES"):
        config_int("MINUTES", default=30)


# --- config_float ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [("1000", 1000.0), (" 2.5 ", 2.5), ("1e3", 1000.0), ("-0.5", -0.5)])
def test_float_values(set_config, value, expected):
    set_config(CAP=value)

    assert config_float("CAP", default=99.0) == expected


@pytest.mark.parametrize("config", [{}, {"CAP": ""}, {"CAP": "  "}])
def test_unset_or_blank_float_uses_the_default(set_config, config):
    set_config(**config)

    assert config_float("CAP", default=1000.0) == 1000.0


def test_non_numeric_value_raises(set_config):
    set_config(CAP="a thousand")

    with pytest.raises(ValueError, match="CAP"):
        config_float("CAP", default=1000.0)
