from click.testing import CliRunner

from market_watcher import market_watcher_cli


def test_help_lists_the_commands():
    result = CliRunner().invoke(market_watcher_cli.cli, ["--help"])

    assert result.exit_code == 0
    for command in ("start", "stop", "config", "test-slack"):
        assert command in result.output


def test_config_masks_secrets(monkeypatch):
    monkeypatch.setattr(
        market_watcher_cli,
        "get_slack_config",
        lambda: {
            "long url": "https://hooks.slack.com/services/T000/B000/LONGSECRET",
            "short url": "https://hooks.slack.com/services/T000/B000/SHORTSECRET",
        },
    )
    monkeypatch.setattr(
        market_watcher_cli,
        "get_email_config",
        lambda: {
            "hostname": "smtp.example.com",
            "port": "465",
            "username": "user",
            "password": "s3cret",
            "sender": "bot@example.com",
            "recipients": "alice@example.com",
        },
    )

    result = CliRunner().invoke(market_watcher_cli.cli, ["config"])

    assert result.exit_code == 0
    assert "smtp.example.com" in result.output
    assert "https://hooks.slack.com/services/..." in result.output
    for secret in ("s3cret", "LONGSECRET", "SHORTSECRET"):
        assert secret not in result.output
