import pytest

import send_email


@pytest.fixture(autouse=True)
def isolate_preview(monkeypatch, tmp_path):
    monkeypatch.setattr(send_email, "PREVIEW_PATH", tmp_path / "preview.html")


def test_send_brief_email_raises_when_not_configured(monkeypatch):
    monkeypatch.setattr(send_email, "SMTP_USERNAME", None)
    monkeypatch.setattr(send_email, "SMTP_APP_PASSWORD", None)
    monkeypatch.setattr(send_email, "EMAIL_TO", None)
    with pytest.raises(RuntimeError):
        send_email.send_brief_email("subject", "<p>body</p>")


def test_connection_reset_reports_pre_login_failure_without_retry(monkeypatch):
    monkeypatch.setattr(send_email, "SMTP_USERNAME", "me@gmail.com")
    monkeypatch.setattr(send_email, "SMTP_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(send_email, "EMAIL_TO", "family@gmail.com")
    attempts = []
    def reset(host, port, **kwargs):
        attempts.append((host, port))
        raise ConnectionResetError(54, "Connection reset by peer")
    monkeypatch.setattr(send_email.smtplib, "SMTP_SSL", reset)
    with pytest.raises(RuntimeError, match="Gmail login was not reached") as caught:
        send_email.send_brief_email("subject", "<p>body</p>")
    assert len(attempts) == 1
    assert isinstance(caught.value.__cause__, ConnectionResetError)
    assert "app-pass" not in str(caught.value)
    assert "retry python send_email.py" in str(caught.value)


@pytest.mark.parametrize("phase", ["login", "send"])
def test_authentication_and_send_errors_are_not_mislabeled_or_retried(monkeypatch, phase):
    monkeypatch.setattr(send_email, "SMTP_USERNAME", "me@gmail.com")
    monkeypatch.setattr(send_email, "SMTP_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(send_email, "EMAIL_TO", "family@gmail.com")
    error = (
        send_email.smtplib.SMTPAuthenticationError(535, b"authentication failed")
        if phase == "login" else send_email.smtplib.SMTPServerDisconnected("connection lost")
    )
    attempts = []
    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            attempts.append("connect")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def login(self, *args):
            attempts.append("login")
            if phase == "login":
                raise error
        def sendmail(self, *args):
            attempts.append("send")
            raise error
    monkeypatch.setattr(send_email.smtplib, "SMTP_SSL", FakeSMTP)
    with pytest.raises(type(error)) as caught:
        send_email.send_brief_email("subject", "<p>body</p>")
    assert caught.value is error
    assert attempts == (["connect", "login"] if phase == "login" else ["connect", "login", "send"])


def test_run_preserves_preview_when_delivery_fails(monkeypatch, tmp_path):
    content_path = tmp_path / "content.json"
    content_path.write_text('{"date": "Thursday", "homework": ["Read page 12"]}')
    monkeypatch.setattr(send_email, "CONTENT_PATH", content_path)
    def fail(*args):
        raise RuntimeError("SMTP connection failed")
    monkeypatch.setattr(send_email, "send_brief_email", fail)
    with pytest.raises(RuntimeError, match="SMTP connection failed"):
        send_email.run()
    assert "Read page 12" in send_email.PREVIEW_PATH.read_text()


def test_send_brief_email_sends_via_smtp(monkeypatch):
    monkeypatch.setattr(send_email, "SMTP_USERNAME", "me@gmail.com")
    monkeypatch.setattr(send_email, "SMTP_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(send_email, "EMAIL_TO", "family@gmail.com")

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, **kwargs):
            calls["host"] = host
            calls["port"] = port
            calls["timeout"] = kwargs["timeout"]
            calls["verify_hostname"] = kwargs["context"].check_hostname

        def __enter__(self):
            calls.setdefault("sequence", []).append("enter")
            return self

        def __exit__(self, *args):
            calls.setdefault("sequence", []).append("exit")
            return False

        def login(self, username, password):
            calls.setdefault("sequence", []).append("login")
            calls["login"] = (username, password)

        def sendmail(self, from_addr, to_addrs, msg):
            calls.setdefault("sequence", []).append("sendmail")
            calls["sendmail"] = (from_addr, to_addrs)
            calls["msg_contains_html"] = "<p>body</p>" in msg

    monkeypatch.setattr(send_email.smtplib, "SMTP_SSL", FakeSMTP)

    send_email.send_brief_email("Test Subject", "<p>body</p>")

    assert calls["host"] == "smtp.gmail.com"
    assert calls["port"] == 465
    assert calls["timeout"] == 30
    assert calls["verify_hostname"] is True
    assert calls["login"] == ("me@gmail.com", "app-pass")
    assert calls["sendmail"] == ("me@gmail.com", ["family@gmail.com"])
    assert calls["msg_contains_html"] is True
    assert calls["sequence"] == ["enter", "login", "sendmail", "exit"]


def test_send_brief_email_splits_comma_separated_recipients(monkeypatch):
    monkeypatch.setattr(send_email, "SMTP_USERNAME", "me@gmail.com")
    monkeypatch.setattr(send_email, "SMTP_APP_PASSWORD", "app-pass")
    monkeypatch.setattr(send_email, "EMAIL_TO", "family@gmail.com, other@gmail.com")

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, username, password):
            pass

        def sendmail(self, from_addr, to_addrs, msg):
            calls["sendmail"] = (from_addr, to_addrs)

    monkeypatch.setattr(send_email.smtplib, "SMTP_SSL", FakeSMTP)

    send_email.send_brief_email("Test Subject", "<p>body</p>")

    assert calls["sendmail"] == (
        "me@gmail.com",
        ["family@gmail.com", "other@gmail.com"],
    )


def test_run_reads_content_renders_and_sends(tmp_path, monkeypatch):
    monkeypatch.setattr(send_email, "CONTENT_PATH", tmp_path / "daily_brief_content.json")
    (tmp_path / "daily_brief_content.json").write_text(
        '{"date": "2026-09-05", "warnings": [], "aviraj_highlight": null, '
        '"homework": [], "agenda": [], "dress_code": null, "reminders": []}'
    )

    calls = {}
    monkeypatch.setattr(send_email, "render_brief_html", lambda data: "<html>fake</html>")
    monkeypatch.setattr(
        send_email,
        "send_brief_email",
        lambda subject, html: calls.update({"subject": subject, "html": html}),
    )

    send_email.run()

    assert calls["subject"] == "Daily School Brief - 2026-09-05"
    assert calls["html"] == "<html>fake</html>"
