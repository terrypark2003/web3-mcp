"""Thin FastAPI shell over DashboardService. Run this on YOUR machine.

    pip install -r requirements-web.txt
    cp .env.example .env          # fill in secrets + DASHBOARD_TOKEN
    set -a; . ./.env; set +a
    python -m polymarket_arb.webapp        # serves http://127.0.0.1:8000

SECURITY
--------
This dashboard can place REAL orders in live mode, so:

* It binds to 127.0.0.1 by default. Only set WEB_HOST=0.0.0.0 if you fully
  understand the exposure, and never without DASHBOARD_TOKEN set.
* Every API call requires the ``X-Dashboard-Token`` header to match
  DASHBOARD_TOKEN. Without that env var the service refuses every request.
* Live trading still needs EXECUTION_MODE=live + full credentials and a
  per-trade stage -> confirm. Dry-run is the default.

All the logic lives in web_core.py (pure, tested); this file is just wiring.
"""

from __future__ import annotations

import os

from .detect import FeeModel
from .execution import ExecutionConfig, PolymarketExecutor
from .web_core import AuthError, DashboardError, DashboardService

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "web")


def build_service() -> DashboardService:
    config = ExecutionConfig.from_env()
    executor = PolymarketExecutor(config)
    fee = FeeModel()

    def scan_fn():
        from .client import PolymarketClient
        from .scanner import scan_live

        return scan_live(PolymarketClient(), fee)

    def cross_scan_fn():
        from .client import PolymarketClient
        from .kalshi_client import KalshiClient
        from .multivenue import scan_cross_venue_live

        return scan_cross_venue_live(KalshiClient(), PolymarketClient())

    ev_scan_fn = None
    fair_path = os.environ.get("FAIR_VALUES_FILE")
    if fair_path:
        def ev_scan_fn():  # noqa: F811 - conditional definition is intentional
            import json

            from .client import PolymarketClient
            from .ev import fair_value_from_map
            from .multivenue import scan_ev_live

            with open(fair_path, encoding="utf-8") as fh:
                data = json.load(fh)
            probs = data.get("fair_values", data)
            fair = fair_value_from_map({k: float(v) for k, v in probs.items()})
            return scan_ev_live(PolymarketClient(), fair)

    return DashboardService(
        scan_fn=scan_fn,
        executor=executor,
        exec_config=config,
        cross_scan_fn=cross_scan_fn,
        ev_scan_fn=ev_scan_fn,
        auth_token=os.environ.get("DASHBOARD_TOKEN"),
    )


def build_demo_service() -> DashboardService:
    """Offline service backed by the bundled fixtures (no network, no keys)."""
    from .crossvenue import scan_cross_venue
    from .demo import load_demo_cross_venue, load_demo_ev_sets, load_demo_sets
    from .detect import scan_sets
    from .ev import scan_ev
    from .venues import default_venue_fees

    config = ExecutionConfig.from_env({})

    def ev_scan_fn():
        sets, fair = load_demo_ev_sets()
        return scan_ev(sets, fair, default_venue_fees(), min_ev=0.02)

    return DashboardService(
        scan_fn=lambda: scan_sets(load_demo_sets()),
        executor=PolymarketExecutor(config),
        exec_config=config,
        cross_scan_fn=lambda: scan_cross_venue(load_demo_cross_venue()),
        ev_scan_fn=ev_scan_fn,
        auth_token=os.environ.get("DASHBOARD_TOKEN", "demo-token"),
    )


def build_readonly_service(live: bool) -> DashboardService:
    """A scan-only service for the public dashboard. No execution is ever wired
    in a way that places orders here (the read endpoints only call scan fns),
    but live trading still needs the full local app — this is monitoring only.

    ``live=False`` is the bundled offline demo. ``live=True`` does a *bounded*
    live scan (capped market count) so it can fit a serverless time budget.
    """
    if not live:
        return build_demo_service()

    config = ExecutionConfig.from_env()
    executor = PolymarketExecutor(config)
    fee = FeeModel()
    limit = int(os.environ.get("LIVE_SCAN_LIMIT", "120") or "120")

    def scan_fn():
        from .client import PolymarketClient
        from .scanner import scan_live

        return scan_live(PolymarketClient(), fee, limit=limit)

    def cross_scan_fn():
        from .client import PolymarketClient
        from .kalshi_client import KalshiClient
        from .multivenue import scan_cross_venue_live

        return scan_cross_venue_live(KalshiClient(), PolymarketClient())

    return DashboardService(
        scan_fn=scan_fn,
        executor=executor,
        exec_config=config,
        cross_scan_fn=cross_scan_fn,
        ev_scan_fn=None,
        auth_token="readonly",  # unused on read paths; never gates execution here
    )


def read_only_payload(live: bool = False) -> dict:
    """Opportunities for the public dashboard, with a ``meta.source`` flag.

    Always returns a renderable payload: if a live scan fails (egress blocked,
    timeout, API change) it falls back to the bundled demo data and records the
    error in ``meta`` rather than 500-ing the dashboard.
    """
    meta = {"source": "live" if live else "demo"}
    try:
        payload = build_readonly_service(live).opportunities()
    except Exception as exc:  # noqa: BLE001 - the page must always render
        payload = build_demo_service().opportunities()
        meta = {"source": "demo", "live_error": str(exc)[:200]}
    payload["meta"] = meta
    return payload


def create_app(service: DashboardService):
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException
        from fastapi.responses import FileResponse, JSONResponse
        from pydantic import BaseModel
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise SystemExit(
            "fastapi not installed (pip install -r requirements-web.txt)"
        ) from exc

    app = FastAPI(title="Prediction-market arbitrage dashboard")

    def auth(x_dashboard_token: str = Header(default="")):
        if not service.check_auth(x_dashboard_token):
            raise HTTPException(status_code=401, detail="invalid dashboard token")

    class StageBody(BaseModel):
        market_id: str
        max_stake: float | None = None

    class ConfirmBody(BaseModel):
        stage_id: str

    @app.get("/")
    def index():  # pragma: no cover - static file serving
        return FileResponse(os.path.join(_STATIC_DIR, "index.html"))

    @app.get("/api/status", dependencies=[Depends(auth)])
    def status():
        return service.status()

    @app.get("/api/opportunities", dependencies=[Depends(auth)])
    def opportunities():
        return service.opportunities()

    @app.post("/api/stage", dependencies=[Depends(auth)])
    def stage(body: StageBody):
        try:
            return service.stage(body.market_id, body.max_stake)
        except DashboardError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/confirm", dependencies=[Depends(auth)])
    def confirm(body: ConfirmBody):
        try:
            return service.confirm(body.stage_id)
        except DashboardError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/cancel", dependencies=[Depends(auth)])
    def cancel(body: ConfirmBody):
        return service.cancel(body.stage_id)

    return app


def main() -> None:  # pragma: no cover - runs a real server
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "uvicorn not installed (pip install -r requirements-web.txt)"
        ) from exc

    demo = os.environ.get("WEB_DEMO", "").lower() in ("1", "true", "yes")
    service = build_demo_service() if demo else build_service()
    if not service.auth_token:
        raise SystemExit(
            "DASHBOARD_TOKEN is required (this dashboard can place real orders)."
        )

    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "8000"))
    if host != "127.0.0.1":
        print(f"WARNING: binding to {host} exposes a trading dashboard beyond "
              "localhost. Ensure DASHBOARD_TOKEN is strong and access is trusted.")
    app = create_app(service)
    print(f"Dashboard on http://{host}:{port} (mode={service.exec_config.mode})")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
