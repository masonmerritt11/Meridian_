"""In-process runtime for the modular monolith: event relay, consumer groups and the scheduler.
Each loop runs in its own daemon thread. A worker that is INACTIVE in the registry is not run and its
consumer group is not read, so its events wait durably in Redis until it is re-activated."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import redis
from sqlalchemy.orm import Session

from meridian.bus.bus import EventBus, Subscription
from meridian.bus.schemas import Envelope
from meridian.core.config import Settings
from meridian.core.logging import get_logger
from meridian.db.session import SessionLocal
from meridian.marketdata.service import build_provider
from meridian.workers.base import BaseWorker, WorkerContext, execute, heartbeat, is_active
from meridian.workers.impl import ALL_WORKERS
from meridian.research.llm import LLMClient
from meridian.workers.macro_provider import FairEconomyWeeklyProvider

log = get_logger("meridian.runtime")


def make_context(settings: Settings, market: Any = None, macro: Any = None) -> WorkerContext:
    return WorkerContext(settings=settings, session_factory=SessionLocal,
                         services={"market": market or build_provider(settings), "macro": macro or FairEconomyWeeklyProvider(),
                                   "llm": LLMClient(settings)})


@dataclass
class Runtime:
    settings: Settings
    ctx: WorkerContext
    bus: EventBus
    workers: dict[str, BaseWorker] = field(default_factory=dict)
    subs: list[Subscription] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)
    last_tick: dict[str, float] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: Settings, ctx: WorkerContext | None = None, r: redis.Redis | None = None) -> Runtime:
        ctx = ctx or make_context(settings)
        r = r or redis.Redis.from_url(settings.redis_url, decode_responses=True)
        bus = EventBus(r, ctx.session_factory, settings.stream_prefix)
        rt = cls(settings=settings, ctx=ctx, bus=bus)
        for wcls in ALL_WORKERS:
            w = wcls(ctx)
            rt.workers[w.spec.id] = w
            if w.spec.subscribes:
                rt.subs.append(Subscription(group=w.spec.id, event_types=set(w.spec.subscribes),
                                            handler=rt._event_handler(w), max_attempts=w.spec.max_retries))
        return rt

    @staticmethod
    def _event_handler(w: BaseWorker):  # noqa: ANN205
        def handler(s: Session, env: Envelope) -> None:
            execute(w, trigger="EVENT", env=env, session=s)
        return handler

    # ------------------------------------------------------------ synchronous driving (demo, tests, CLI)
    def run_scheduled(self, worker_id: str) -> Any:
        return execute(self.workers[worker_id], trigger="MANUAL")

    def drain(self, max_rounds: int = 50) -> int:
        """Relay and consume until the pipeline is quiet. Returns events handled."""
        total = 0
        for _ in range(max_rounds):
            moved = self.bus.relay_once()
            handled = sum(self.bus.consume_once(sub, block_ms=1) for sub in self.subs
                          if is_active(self.ctx.session_factory, sub.group))
            total += handled
            if moved == 0 and handled == 0:
                break
        return total

    # ------------------------------------------------------------ background
    def start(self) -> None:
        self._spawn("relay", self._relay_loop)
        for sub in self.subs:
            self._spawn(f"consumer:{sub.group}", self._consumer_loop, sub)
        self._load_last_runs()
        self._spawn("scheduler", self._scheduler_loop, False)
        self._spawn("scheduler-slow", self._scheduler_loop, True)
        log.info("runtime started", extra={"consumers": [s.group for s in self.subs]})

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)

    def _spawn(self, name: str, fn: Any, *args: Any) -> None:
        t = threading.Thread(target=self._guard, args=(name, fn, *args), name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _guard(self, name: str, fn: Any, *args: Any) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                fn(*args)
                return
            except Exception:  # noqa: BLE001 — recover and keep running
                log.exception("runtime loop crashed; restarting", extra={"loop": name, "backoff_s": backoff})
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _relay_loop(self) -> None:
        last_hb = 0.0
        while not self._stop.is_set():
            n = self.bus.relay_once()
            if time.time() - last_hb > 30:
                heartbeat(self.ctx.session_factory, "data.event_relay", "HEALTHY", {"last_batch": n})
                last_hb = time.time()
            if n == 0:
                self._stop.wait(0.5)

    def _consumer_loop(self, sub: Subscription) -> None:
        last_hb = 0.0
        while not self._stop.is_set():
            if not is_active(self.ctx.session_factory, sub.group):
                self._stop.wait(5)
                continue
            self.bus.consume_once(sub, block_ms=2000)
            w = self.workers[sub.group]
            if w.spec.schedule_seconds is None and time.time() - last_hb > 30:
                heartbeat(self.ctx.session_factory, sub.group, "HEALTHY", {"consumer": "listening", **sub.stats})
                last_hb = time.time()

    def _load_last_runs(self) -> None:
        """Resume schedules from the database so a redeploy does not re-run every worker (and re-spend on LLM calls)."""
        from sqlalchemy import func, select

        from meridian.db.models import WorkerRun
        s = self.ctx.session_factory()
        try:
            rows = s.execute(select(WorkerRun.worker_id, func.max(WorkerRun.started_at))
                             .where(WorkerRun.trigger.in_(("SCHEDULE", "MANUAL"))).group_by(WorkerRun.worker_id)).all()
            for wid, ts in rows:
                if wid in self.workers and ts is not None:
                    self.last_tick[wid] = ts.timestamp()
        finally:
            s.close()

    def _scheduler_loop(self, slow: bool) -> None:
        while not self._stop.is_set():
            now = time.time()
            for wid, w in self.workers.items():
                if w.slow != slow or not w.spec.schedule_seconds or not is_active(self.ctx.session_factory, wid):
                    continue
                if now - self.last_tick.get(wid, 0) >= w.spec.schedule_seconds:
                    self.last_tick[wid] = now
                    try:
                        execute(w, trigger="SCHEDULE")
                    except Exception:  # noqa: BLE001 — already recorded + escalated by executor
                        pass
            self._stop.wait(1)
