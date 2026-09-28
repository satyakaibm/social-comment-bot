import hashlib
import hmac

from billing import config, razorpay_client


def test_verify_webhook_signature_accepts_correct_hmac(monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", "test-secret")
    body = b'{"event":"subscription.activated"}'
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    assert razorpay_client.verify_webhook_signature(body, signature) is True


def test_verify_webhook_signature_rejects_wrong_signature(monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", "test-secret")
    body = b'{"event":"subscription.activated"}'
    assert razorpay_client.verify_webhook_signature(body, "0" * 64) is False


def test_verify_webhook_signature_rejects_tampered_body(monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", "test-secret")
    original = b'{"event":"subscription.activated"}'
    signature = hmac.new(b"test-secret", original, hashlib.sha256).hexdigest()
    tampered = b'{"event":"subscription.cancelled"}'
    assert razorpay_client.verify_webhook_signature(tampered, signature) is False


def test_verify_webhook_signature_requires_configured_secret(monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", "")
    body = b"{}"
    signature = hmac.new(b"anything", body, hashlib.sha256).hexdigest()
    assert razorpay_client.verify_webhook_signature(body, signature) is False
