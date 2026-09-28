import hashlib
import hmac

import razorpay

from billing import config

# Event names as documented at https://razorpay.com/docs/webhooks/subscriptions
EVENT_ACTIVATED = "subscription.activated"
EVENT_CHARGED = "subscription.charged"
EVENT_HALTED = "subscription.halted"
EVENT_CANCELLED = "subscription.cancelled"

ACTIVE_EVENTS = frozenset({EVENT_ACTIVATED, EVENT_CHARGED})
INACTIVE_EVENTS = frozenset({EVENT_HALTED, EVENT_CANCELLED})


def get_client() -> razorpay.Client:
    return razorpay.Client(auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))


def create_subscription() -> dict:
    """Create a Subscription against the one configured Plan.

    Razorpay does not take customer details here -- they're captured when the
    customer completes the authorization payment at Checkout using the
    returned subscription id (see billing.app's /signup route). The response
    includes `id` (store as razorpay_subscription_id) and `short_url` if you
    ever want to email a payment link instead of using inline Checkout.
    """
    client = get_client()
    return client.subscription.create(
        {
            "plan_id": config.RAZORPAY_PLAN_ID,
            "total_count": config.RAZORPAY_SUBSCRIPTION_TOTAL_COUNT,
            "customer_notify": 1,
        }
    )


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """HMAC-SHA256 of the raw body, hex-encoded, per Razorpay's webhook docs.

    Mirrors app/webhook.py's _signature_is_valid for the Meta webhook -- same
    construction (hmac.new(secret, raw_body, sha256).hexdigest() compared with
    hmac.compare_digest), just a different header/secret.
    """
    if not config.RAZORPAY_WEBHOOK_SECRET or not signature:
        return False
    expected = hmac.new(
        config.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)
