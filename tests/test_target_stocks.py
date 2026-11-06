from pathlib import Path

import pytest
import yaml

from market_watcher.common import get_target_stocks

RESEARCH_DIR = Path(__file__).resolve().parents[1] / "src" / "market_watcher" / "research"


def write(tmp_path, text):
    path = tmp_path / "stocks.yaml"
    path.write_text(text)
    return path


def test_loads_tickers_and_strategies(tmp_path):
    path = write(tmp_path, "AAPL:\n  strategy: short straddle\nTSLA:\n  strategy: long straddle\n")

    assert get_target_stocks(path) == {
        "AAPL": {"strategy": "short straddle"},
        "TSLA": {"strategy": "long straddle"},
    }


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        get_target_stocks(tmp_path / "missing.yaml")


def test_invalid_yaml_raises(tmp_path):
    with pytest.raises(yaml.YAMLError):
        get_target_stocks(write(tmp_path, "AAPL: [unclosed"))


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty file
        "- AAPL\n- TSLA\n",  # a list instead of a mapping
        "AAPL:\n  strategy: iron condor\n",  # unknown strategy
        "AAPL: long straddle\n",  # missing the `strategy` key
    ],
)
def test_unexpected_structure_raises(tmp_path, text):
    with pytest.raises(ValueError):
        get_target_stocks(write(tmp_path, text))


@pytest.mark.parametrize("name", ["target_stocks.yaml", "target_stocks_short.yaml"])
def test_shipped_target_stock_files_are_valid(name):
    assert get_target_stocks(RESEARCH_DIR / name)
