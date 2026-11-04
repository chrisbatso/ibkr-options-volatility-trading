import pytest

from market_watcher.common import STRATEGIES
from market_watcher.notifier import EmailNotifier, NotificationError, SlackNotifier, parse_recipients

LONG = STRATEGIES.LONG_STRADDLE.value
SHORT = STRATEGIES.SHORT_STRADDLE.value
EMPTY = {LONG: {}, SHORT: {}}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice@example.com;bob@example.com", ["alice@example.com", "bob@example.com"]),
        ("alice@example.com, bob@example.com ;", ["alice@example.com", "bob@example.com"]),
        ("alice@example.com", ["alice@example.com"]),
        (["alice@example.com", " bob@example.com "], ["alice@example.com", "bob@example.com"]),
        ("", []),
    ],
)
def test_parse_recipients(raw, expected):
    assert parse_recipients(raw) == expected


# --- Email -----------------------------------------------------------------------------------------------------------

EMAIL_CONFIG = {
    "hostname": "smtp.example.com",
    "port": "465",
    "username": "user",
    "password": "secret",
    "sender": "bot@example.com",
    "recipients": "alice@example.com;bob@example.com",
}


class FakeSMTP:
    instances = []

    def __init__(self, hostname, port, context=None):
        self.hostname = hostname
        self.port = port
        self.logins = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def login(self, username, password):
        self.logins.append((username, password))

    def sendmail(self, sender, recipients, message):
        self.sent.append((sender, recipients, message))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr("market_watcher.notifier.smtplib.SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_email_is_sent_to_every_recipient(smtp):
    EmailNotifier(EMAIL_CONFIG).notify({LONG: {"ABEO": 0.0759}, SHORT: {"TPCO": -0.001}})

    [server] = smtp.instances
    assert (server.hostname, server.port) == ("smtp.example.com", 465)
    assert server.logins == [("user", "secret")]

    [(sender, recipients, message)] = server.sent
    assert sender == "bot@example.com"
    assert recipients == ["alice@example.com", "bob@example.com"]
    assert "From: bot@example.com" in message
    assert "To: alice@example.com, bob@example.com" in message
    assert "Long straddle opportunities:\nABEO: 7.59% (UP)" in message
    assert "Short straddle opportunities:\nTPCO: 0.1% (DOWN)" in message


def test_email_is_not_sent_when_there_is_nothing_to_report(smtp):
    EmailNotifier(EMAIL_CONFIG).notify(EMPTY)

    assert smtp.instances == []


def test_email_requires_at_least_one_recipient():
    with pytest.raises(ValueError, match="EMAIL_RECIPIENTS"):
        EmailNotifier({**EMAIL_CONFIG, "recipients": " ; "})


# --- Slack -----------------------------------------------------------------------------------------------------------

SLACK_CONFIG = {"long url": "https://hooks.example/long", "short url": "https://hooks.example/short"}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body


class FakeWebhookClient:
    calls = []
    responses = {}

    def __init__(self, url):
        self.url = url

    def send(self, text):
        FakeWebhookClient.calls.append((self.url, text))
        return FakeWebhookClient.responses.get(self.url, FakeResponse(200, "ok"))


@pytest.fixture
def webhook(monkeypatch):
    FakeWebhookClient.calls = []
    FakeWebhookClient.responses = {}
    monkeypatch.setattr("market_watcher.notifier.WebhookClient", FakeWebhookClient)
    return FakeWebhookClient


def test_slack_posts_only_to_channels_with_opportunities(webhook):
    SlackNotifier(SLACK_CONFIG).notify({LONG: {"ABEO": 0.0759, "ACRX": -0.12}, SHORT: {}})

    assert webhook.calls == [
        ("https://hooks.example/long", "Long straddle opportunities:\nACRX: 12.0% (DOWN)\nABEO: 7.59% (UP)"),
    ]


def test_slack_posts_nothing_when_there_is_nothing_to_report(webhook):
    SlackNotifier(SLACK_CONFIG).notify(EMPTY)

    assert webhook.calls == []


def test_slack_failure_raises_a_descriptive_error(webhook):
    webhook.responses["https://hooks.example/short"] = FakeResponse(404, "no_service")

    with pytest.raises(NotificationError, match="404.*no_service"):
        SlackNotifier(SLACK_CONFIG).notify({LONG: {}, SHORT: {"TPCO": 0.0}})


# --- Formatting ------------------------------------------------------------------------------------------------------


def test_short_straddle_message_lists_smallest_moves_first_and_omits_direction_for_zero():
    message = SlackNotifier(SLACK_CONFIG)._short_straddle_message({"TTMI": 0.004, "TPCO": 0.0, "KO": -0.002})

    assert message == "Short straddle opportunities:\nTPCO: 0.0%\nKO: 0.2% (DOWN)\nTTMI: 0.4% (UP)"
