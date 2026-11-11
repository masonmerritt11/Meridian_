"""Operator CLI.  python -m meridian.cli <command>

  bootstrap        run migrations, seed registry/risk config/capital/CIO user
  serve            start API + workers (uses PORT env, default 8000)
  demo             run the end-to-end pipeline on SIMULATED data and print the memo(s)
  discover         (coinbase mode) print which Coinbase products the universe resolves to
  check-coinbase   call each public endpoint once and report reachability (run where api.coinbase.com is reachable)
  verify-audit     verify the audit hash chain
  export-schemas   write docs/openapi.json and docs/event-schemas.json (API contract + event schemas)
  create-user      create a user: create-user <username> <CIO|VIEWER>  (password from MERIDIAN_NEW_PASSWORD)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta

from meridian.core.config import get_settings
from meridian.core.logging import configure_logging


def main(argv: list[str]) -> int:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    st = get_settings()
    cmd = argv[0] if argv else "help"
    if cmd == "bootstrap":
        from meridian.bootstrap import bootstrap
        bootstrap(st)
    elif cmd == "serve":
        import uvicorn
        uvicorn.run("meridian.api.app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), workers=1,
                    proxy_headers=True, forwarded_allow_ips="*", log_config=None)
    elif cmd == "demo":
        from meridian.bootstrap import bootstrap
        from meridian.demo import run_demo
        bootstrap(st)
        print(json.dumps(run_demo(st), indent=2, default=str))
    elif cmd in ("discover", "check-coinbase"):
        from meridian.marketdata.coinbase import CoinbasePublicProvider
        p = CoinbasePublicProvider(overrides=json.loads(st.product_overrides) if st.product_overrides else {})
        specs = p.discover()
        for sp in specs:
            print(f"{sp.asset:7} {sp.status:11} {sp.id:28} type={sp.product_type:6} size={sp.contract_size} "
                  f"tick={sp.price_increment} expiry={sp.contract_expiry} {sp.status_detail}")
        if cmd == "check-coinbase":
            now = datetime.now(UTC)
            for sp in specs:
                if sp.status != "ACTIVE":
                    continue
                c = p.candles(sp, "ONE_HOUR", now - timedelta(hours=6), now)
                b = p.book(sp)
                print(f"  {sp.id}: candles={len(c)} last={c[-1].ts if c else None} spread_bps={b.spread_bps if b else None}")
            print(f"calls={p.client.calls} rate_limited={p.client.rate_limited}")
    elif cmd == "export-schemas":
        from pathlib import Path

        from meridian.api.app import create_app
        from meridian.bus.schemas import REGISTRY, Envelope
        out = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[2] / "docs"
        out.mkdir(parents=True, exist_ok=True)
        (out / "openapi.json").write_text(json.dumps(create_app(start_runtime=False, run_bootstrap=False).openapi(), indent=1))
        (out / "event-schemas.json").write_text(json.dumps(
            {"envelope": Envelope.model_json_schema(),
             "payloads": {f"{t} v{v}": m.model_json_schema() for (t, v), m in REGISTRY.items()}}, indent=1))
        print(f"wrote {out}/openapi.json and {out}/event-schemas.json")
    elif cmd == "verify-audit":
        from meridian.core.audit import verify_chain
        from meridian.db.session import session_scope
        with session_scope() as s:
            ok, bad = verify_chain(s)
        print("audit chain OK" if ok else f"audit chain BROKEN at id {bad}")
        return 0 if ok else 1
    elif cmd == "create-user":
        from meridian.api.auth import hash_password
        from meridian.db.models import User
        from meridian.db.session import session_scope
        pw = os.environ.get("MERIDIAN_NEW_PASSWORD")
        if len(argv) < 3 or not pw or len(pw) < 12:
            print("usage: MERIDIAN_NEW_PASSWORD=<12+ chars> create-user <username> <CIO|VIEWER>")
            return 2
        with session_scope() as s:
            s.add(User(username=argv[1], password_hash=hash_password(pw), role=argv[2].upper()))
        print("created")
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
