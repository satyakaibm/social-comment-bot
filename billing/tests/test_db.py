from billing import db


def test_create_and_get_tenant(isolated_db):
    with db.connect() as conn:
        port = db.next_free_port(conn)
        db.create_tenant(conn, tenant_key="acme", name="Acme", email="a@example.com", port=port)
    with db.connect() as conn:
        tenant = db.get_tenant(conn, "acme")
    assert tenant["status"] == "created"
    assert tenant["provisioned"] == 0
    assert tenant["port"] == port


def test_next_free_port_skips_taken_ports(isolated_db):
    with db.connect() as conn:
        first = db.next_free_port(conn)
        db.create_tenant(conn, tenant_key="acme", name="Acme", email="a@example.com", port=first)
        second = db.next_free_port(conn)
    assert second != first


def test_set_status_transitions(isolated_db):
    with db.connect() as conn:
        db.create_tenant(conn, tenant_key="acme", name="Acme", email="a@example.com", port=9101)
        db.set_status(conn, "acme", "active")
    with db.connect() as conn:
        assert db.get_tenant(conn, "acme")["status"] == "active"


def test_get_tenant_by_subscription(isolated_db):
    with db.connect() as conn:
        db.create_tenant(
            conn, tenant_key="acme", name="Acme", email="a@example.com", port=9101,
            razorpay_subscription_id="sub_123",
        )
    with db.connect() as conn:
        tenant = db.get_tenant_by_subscription(conn, "sub_123")
    assert tenant["tenant_key"] == "acme"


def test_set_provisioned(isolated_db):
    with db.connect() as conn:
        db.create_tenant(conn, tenant_key="acme", name="Acme", email="a@example.com", port=9101)
        db.set_provisioned(conn, "acme")
    with db.connect() as conn:
        assert db.get_tenant(conn, "acme")["provisioned"] == 1
