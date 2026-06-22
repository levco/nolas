import uuid

from app.api.payloads.messages import EmailAddress, Message
from app.controllers.notifications.bounce import detect_bounce


def _message(
    sender: str = "mailer-daemon@googlemail.com",
    subject: str = "Delivery Status Notification (Failure)",
    body: str = "<p>Your message to <b>missing@nowhere.com</b> couldn't be delivered. 550 5.1.1 user unknown</p>",
) -> Message:
    return Message(
        id="msg-1",
        grant_id=str(uuid.uuid4()),
        object="message",
        thread_id="thread-1",
        subject=subject,
        body=body,
        snippet="",
        from_=[EmailAddress(name=sender, email=sender)],
        to=[EmailAddress(name="me", email="me@lev.co")],
        date=1717920000,
        unread=True,
        starred=False,
        folders=["INBOX"],
    )


class TestDetectBounce:
    def test_detects_mailer_daemon_dsn(self) -> None:
        bounce = detect_bounce(_message())
        assert bounce is not None
        assert "missing@nowhere.com" in bounce["bounced_addresses"]
        assert bounce["code"] == "550"
        assert bounce["origin"]["id"] == "msg-1"

    def test_ignores_regular_email(self) -> None:
        message = _message(sender="jane@example.com", subject="Deal update", body="<p>All good</p>")
        assert detect_bounce(message) is None

    def test_ignores_own_address(self) -> None:
        message = _message(body="<p>me@lev.co could not deliver</p>")
        assert detect_bounce(message) is None

    def test_undeliverable_subject_from_postmaster(self) -> None:
        message = _message(
            sender="postmaster@outlook.com",
            subject="Undeliverable: Deal update",
            body="<p>Recipient bad@dead.com rejected. 554 mailbox unavailable</p>",
        )
        bounce = detect_bounce(message)
        assert bounce is not None
        assert bounce["bounced_addresses"] == "bad@dead.com"
        assert bounce["code"] == "554"
