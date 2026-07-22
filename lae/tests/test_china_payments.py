from __future__ import annotations

import hashlib
import hmac
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "packages/python/lae-store/src"))

from lae_store.billing import BillingConfigurationError  # noqa: E402
from lae_store.china_payments import (  # noqa: E402
    AlipayProvider,
    ChinaPaymentHub,
    WeChatPayProvider,
    china_payment_hub_from_env,
)
from lae_store.ids import new_id  # noqa: E402

PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7
-----END PRIVATE KEY-----"""

PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAu
-----END PUBLIC KEY-----"""


class ChinaPaymentTests(unittest.TestCase):
    def test_wechat_checkout_url_and_hmac_verify(self) -> None:
        key = b"0123456789abcdef0123456789abcdef"
        provider = WeChatPayProvider(
            merchant_id="1900000001",
            api_v3_key=key,
            mch_serial_no="SERIAL1",
            private_key_pem=PRIVATE_KEY,
            app_id="wx1234567890",
            notify_url="https://lae.example.com/v1/billing/webhooks/wechat",
            public_pay_base_url="https://lae.example.com",
        )
        order_id = new_id("ord")
        checkout = provider.create_checkout(
            order_id=order_id, now=datetime.now(timezone.utc)
        )
        self.assertIn(f"/billing/pay/wechat/{order_id}", checkout.url)
        payload = {"out_trade_no": order_id, "transaction_id": "wx_evt_1"}
        digest = hmac.new(
            key,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertTrue(provider.verify_callback(payload, f"v3={digest}"))
        self.assertFalse(provider.verify_callback(payload, "v3=deadbeef"))

    def test_alipay_checkout_and_hub_select(self) -> None:
        wechat = WeChatPayProvider(
            merchant_id="1900000001",
            api_v3_key=b"0123456789abcdef0123456789abcdef",
            mch_serial_no="SERIAL1",
            private_key_pem=PRIVATE_KEY,
            app_id="wx1234567890",
            notify_url="https://lae.example.com/v1/billing/webhooks/wechat",
            public_pay_base_url="https://lae.example.com",
        )
        alipay = AlipayProvider(
            merchant_id="2088000001",
            app_id="2021000001",
            app_private_key_pem=PRIVATE_KEY,
            alipay_public_key_pem=PUBLIC_KEY,
            notify_url="https://lae.example.com/v1/billing/webhooks/alipay",
            return_url="https://lae.example.com/account",
            public_pay_base_url="https://lae.example.com",
        )
        hub = ChinaPaymentHub(wechat=wechat, alipay=alipay, default_channel="wechat_pay")
        self.assertEqual(hub.code, "wechat_pay")
        selected = hub.select("alipay")
        self.assertEqual(selected.code, "alipay")
        order_id = new_id("ord")
        checkout = selected.create_checkout(
            order_id=order_id, now=datetime.now(timezone.utc)
        )
        self.assertIn("/billing/pay/alipay/", checkout.url)
        with self.assertRaises(ValueError):
            hub.select("paypal")

    def test_hub_from_env_requires_channel(self) -> None:
        with self.assertRaises(BillingConfigurationError):
            china_payment_hub_from_env({})

    def test_hub_from_env_wechat(self) -> None:
        hub = china_payment_hub_from_env(
            {
                "LAE_PUBLIC_WEB_BASE_URL": "https://lae.example.com",
                "LAE_WECHAT_MCH_ID": "1900000001",
                "LAE_WECHAT_API_V3_KEY": "0123456789abcdef0123456789abcdef",
                "LAE_WECHAT_MCH_SERIAL": "SERIAL1",
                "LAE_WECHAT_MCH_PRIVATE_KEY": PRIVATE_KEY.replace("\n", "\\n"),
                "LAE_WECHAT_APP_ID": "wx1234567890",
            }
        )
        self.assertEqual(hub.code, "wechat_pay")
        self.assertIsNotNone(hub.wechat)
        self.assertIsNone(hub.alipay)


if __name__ == "__main__":
    unittest.main()
