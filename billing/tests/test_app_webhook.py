import hashlib
import hmac
import json

import pytest

from billing import app as billing_app
from billing import config, db, tenant_ops


@pytest.fixture
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(config, "BILLING_SECRET", "x" * 32)
    flask_app = billing_app.create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _signed_post(client, event: str, subscription_id: str):
    body = json.dumps(
        {"event": event, "payload": {"subscription": {"entity": {"id": subscription_id}}}}
    ).encode()
    signature = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/razorpay",
        data=body,
        content_type="application/json",
        headers={"X-Razorpay-Signature": signature},
    )


def test_invalid_signature_rejected(client):
    resp = client.post(
        "/webhooks/razorpay",
        data=b"{}",
        content_type="application/json",
        headers={"X-Razorpay-Signature": "bad"},
    )
    assert resp.status_code == 401


def test_activation_before_provisioning_marks_pending(client):
    with db.connect() as conn:
        db.create_tenant(
            conn, tenant_key="acme", name="Acme", email="a@example.com", port=9101,
            razorpay_subscription_id="sub_123",
        )
    resp = _signed_post(client, "subscription.activated", "sub_123")
    assert resp.status_code == 200
    with db.connect() as conn:
        assert db.get_tenant(conn, "acme")["status"] == "pending_provisioning"


def test_charged_after_provisioning_starts_containers(client, monkeypatch):
    started = []
    monkeypatch.setattr(tenant_ops, "start", lambda key, env: started.append(key))
    with db.connect() as conn:
        db.create_tenant(
            conn, tenant_key="acme", name="Acme", email="a@example.com", port=9101,
            razorpay_subscription_id="sub_123",
        )
        db.set_provisioned(conn, "acme")
    resp = _signed_post(client, "subscription.charged", "sub_123")
    assert resp.status_code == 200
    assert started == ["acme"]
    with db.connect() as conn:
        assert db.get_tenant(conn, "acme")["status"] == "active"


def test_halted_stops_provisioned_tenant(client, monkeypatch):
    stopped = []
    monkeypatch.setattr(tenant_ops, "stop", lambda key, env: stopped.append(key))
    with db.connect() as conn:
        db.create_tenant(
            conn, tenant_key="acme", name="Acme", email="a@example.com", port=9101,
            razorpay_subscription_id="sub_123",
        )
        db.set_provisioned(conn, "acme")
        db.set_status(conn, "acme", "active")
    resp = _signed_post(client, "subscription.halted", "sub_123")
    assert resp.status_code == 200
    assert stopped == ["acme"]
    with db.connect() as conn:
        assert db.get_tenant(conn, "acme")["status"] == "inactive"


def test_unknown_subscription_is_ignored(client):
    resp = _signed_post(client, "subscription.activated", "sub_does_not_exist")
    assert resp.status_code == 200
