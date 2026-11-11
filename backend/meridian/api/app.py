from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import redis
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from meridian.api.routes import router
from meridian.bootstrap import bootstrap
from meridian.core.config import code_version, get_settings
from meridian.core.logging import configure_logging, get_logger
from meridian.db.session import get_engine

log = get_logger("meridian.api")


def _otel(app: FastAPI) -> None:
    st = get_settings()
    if not st.otel_endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("OTEL endpoint set but opentelemetry extras not installed (pip install .[otel])")
        return
    tp = TracerProvider(resource=Resource.create({"service.name": "meridian"}))
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=st.otel_endpoint)))
    trace.set_tracer_provider(tp)
    FastAPIInstrumentor.instrument_app(app)


def create_app(start_runtime: bool | None = None, run_bootstrap: bool = True) -> FastAPI:
    configure_logging()
    st = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        if run_bootstrap:
            bootstrap(st)
        app.state.runtime = None
        if st.run_workers if start_runtime is None else start_runtime:
            from meridian.runtime import Runtime
            rt = Runtime.build(st)
            rt.start()
            app.state.runtime = rt
        yield
        if app.state.runtime:
            app.state.runtime.stop()

    app = FastAPI(title="MERIDIAN", version="0.1.0", lifespan=lifespan,
                  description="Advisory-only research platform. No order execution exists in this API.")
    app.include_router(router)
    _otel(app)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN202
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                   "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/health")
    def health() -> JSONResponse:
        checks = {}
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["database"] = f"error: {type(e).__name__}"
        try:
            redis.Redis.from_url(st.redis_url).ping()
            checks["redis"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["redis"] = f"error: {type(e).__name__}"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks, "version": code_version(),
                             "mode": st.market_data_mode}, status_code=200 if ok else 503)

    static = Path(st.static_dir) if st.static_dir else Path(__file__).resolve().parents[3] / "frontend" / "out"
    if static.exists():
        app.mount("/_next", StaticFiles(directory=static / "_next"), name="next")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)  # type: ignore[return-value]
            target = (static / path).resolve()
            if static.resolve() not in target.parents and target != static.resolve():
                return FileResponse(static / "index.html")
            for cand in (target, target / "index.html", Path(str(target) + ".html")):
                if cand.is_file():
                    return FileResponse(cand)
            return FileResponse(static / "404.html" if (static / "404.html").exists() else static / "index.html")
    return app


app = create_app()
