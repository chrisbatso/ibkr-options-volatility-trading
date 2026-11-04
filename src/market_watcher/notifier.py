import logging
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from slack_sdk.webhook import WebhookClient

from market_watcher.common import STRATEGIES

logger = logging.getLogger(__name__)


class NotificationError(RuntimeError):
    """Raised when a notification could not be delivered."""


def parse_recipients(recipients):
    """Normalises ``EMAIL_RECIPIENTS`` (a ``;`` or ``,`` separated string, or a list) into a list of addresses."""
    if isinstance(recipients, str):
        recipients = recipients.replace(",", ";").split(";")
    return [address.strip() for address in recipients if address and address.strip()]


class Notifier:
    def notify(self, investment_data):
        raise NotImplementedError

    def _long_straddle_message(self, data):
        # Biggest moves first.
        sorted_data = sorted(data.items(), key=lambda item: abs(item[1]), reverse=True)
        return "Long straddle opportunities:\n" + self._formatted_data(sorted_data)

    def _short_straddle_message(self, data):
        # Smallest moves first.
        sorted_data = sorted(data.items(), key=lambda item: abs(item[1]))
        return "Short straddle opportunities:\n" + self._formatted_data(sorted_data)

    def _formatted_data(self, items):
        return "\n".join(f"{ticker}: {round(abs(pnl * 100), 3)}%{self._direction(pnl)}" for ticker, pnl in items)

    @staticmethod
    def _direction(number):
        if number > 0:
            return " (UP)"
        if number < 0:
            return " (DOWN)"
        return ""


class EmailNotifier(Notifier):
    def __init__(self, config):
        self._hostname = config["hostname"]
        self._port = int(config["port"])
        self._username = config["username"]
        self._password = config["password"]
        self._sender = config["sender"]
        self._recipients = parse_recipients(config["recipients"])
        if not self._recipients:
            raise ValueError("EMAIL_RECIPIENTS must contain at least one address")

    def notify(self, investment_data):
        long_data = investment_data.get(STRATEGIES.LONG_STRADDLE.value) or {}
        short_data = investment_data.get(STRATEGIES.SHORT_STRADDLE.value) or {}
        if not long_data and not short_data:
            return

        sections = []
        if long_data:
            sections.append(self._long_straddle_message(long_data))
        if short_data:
            sections.append(self._short_straddle_message(short_data))

        message = MIMEMultipart()
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        message["Subject"] = f"Investment opportunities report - {datetime.now():%Y-%m-%d %H:%M:%S}"
        message.attach(MIMEText("\n\n".join(sections), "plain"))

        self.send(message.as_string())

    def send(self, message):
        ssl_context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self._hostname, self._port, context=ssl_context) as server:
            if self._username:
                server.login(self._username, self._password)
            server.sendmail(self._sender, self._recipients, message)
        logger.info("Sent email to %s", ", ".join(self._recipients))


class SlackNotifier(Notifier):
    def __init__(self, config):
        self._long_url = config["long url"]
        self._short_url = config["short url"]

    def notify(self, investment_data):
        long_data = investment_data.get(STRATEGIES.LONG_STRADDLE.value) or {}
        short_data = investment_data.get(STRATEGIES.SHORT_STRADDLE.value) or {}

        if long_data:
            self.send_message(self._long_url, self._long_straddle_message(long_data))
        if short_data:
            self.send_message(self._short_url, self._short_straddle_message(short_data))

    def send_message(self, url, text):
        """Posts a message to a Slack channel through its incoming webhook."""
        response = WebhookClient(url).send(text=text)
        if response.status_code != 200 or response.body != "ok":
            raise NotificationError(f"Slack webhook returned HTTP {response.status_code}: {response.body}")
