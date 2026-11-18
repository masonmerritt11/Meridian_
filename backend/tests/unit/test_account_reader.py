import base64
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meridian.brokerage.coinbase_account import CoinbaseAccountReader, KeyNotReadOnly, build_jwt, check_account_path
from meridian.brokerage.sync import parse_positions, protective_levels
from meridian.marketdata.coinbase import ForbiddenRequest

KEY = "organizations/org-1/apiKeys/key-1"


def _ec_pem() -> tuple[str, object]:
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()
    return pem, k.public_key()


def test_jwt_es256_and_ed25519():
    pem, pub = _ec_pem()
    t = build_jwt(KEY, pem.replace("\n", "\\n"), "GET", "/api/v3/brokerage/cfm/positions", now=1_700_000_000)
    h = jwt.get_unverified_header(t)
    assert h["alg"] == "ES256" and h["kid"] == KEY and len(h["nonce"]) == 32
    body = jwt.decode(t, pub, algorithms=["ES256"], options={"verify_nbf": False, "verify_exp": False})
    assert body["uri"] == "GET api.coinbase.com/api/v3/brokerage/cfm/positions" and body["iss"] == "cdp" and body["sub"] == KEY
    assert body["exp"] - body["nbf"] == 120
    ed = Ed25519PrivateKey.generate()
    raw = ed.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pubraw = ed.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    t2 = build_jwt(KEY, base64.b64encode(raw + pubraw).decode(), "GET", "/api/v3/brokerage/key_permissions")
    assert jwt.get_unverified_header(t2)["alg"] == "EdDSA"
    jwt.decode(t2, ed.public_key(), algorithms=["EdDSA"])


def test_account_reader_is_read_only():
    for bad in ("/api/v3/brokerage/orders", "/api/v3/brokerage/orders/batch_cancel", "/api/v3/brokerage/cfm/sweeps/schedule",
                "/api/v3/brokerage/accounts"):
        with pytest.raises(ForbiddenRequest):
            check_account_path("GET", bad)
    with pytest.raises(ForbiddenRequest):
        check_account_path("POST", "/api/v3/brokerage/cfm/positions")
    pem, _ = _ec_pem()

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET" and req.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"can_view": True, "can_trade": True, "can_transfer": False})
    r = CoinbaseAccountReader(KEY, pem, transport=httpx.MockTransport(handler))
    with pytest.raises(KeyNotReadOnly):
        r.assert_read_only()


def test_parse_positions_and_levels():
    pos = parse_positions([{"product_id": "GOL-25NOV26-CDE", "side": "LONG", "number_of_contracts": "2", "avg_entry_price": "4050.5"},
                           {"product_id": "BIT-27NOV26-CDE", "side": "SHORT", "number_of_contracts": "3", "avg_entry_price": "91000"},
                           {"product_id": "X", "side": "LONG", "number_of_contracts": "0", "avg_entry_price": "1"}])
    assert set(pos) == {"GOL-25NOV26-CDE", "BIT-27NOV26-CDE"} and pos["BIT-27NOV26-CDE"].direction == "SHORT"
    orders = [{"product_id": "GOL-25NOV26-CDE", "side": "SELL", "order_configuration": {"stop_limit_stop_limit_gtc": {"stop_price": "4010", "limit_price": "4005"}}},
              {"product_id": "GOL-25NOV26-CDE", "side": "SELL", "order_configuration": {"limit_limit_gtc": {"limit_price": "4120"}}},
              {"product_id": "GOL-25NOV26-CDE", "side": "BUY", "order_configuration": {"limit_limit_gtc": {"limit_price": "3900"}}}]
    stop, target = protective_levels(orders, "GOL-25NOV26-CDE", "LONG")
    assert str(stop) == "4010" and str(target) == "4120"
    br = [{"product_id": "BIT-27NOV26-CDE", "side": "BUY", "order_configuration": {"trigger_bracket_gtc": {"limit_price": "88000", "stop_trigger_price": "93000"}}}]
    assert tuple(map(str, protective_levels(br, "BIT-27NOV26-CDE", "SHORT"))) == ("93000", "88000")
