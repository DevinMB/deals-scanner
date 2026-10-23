"""In-process scheduling with APScheduler: periodic scans plus a watchdog for stalled runs."""

import logging
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, settings as settings_mod
from .config import Config
from .runner import Notifier, iso, run_scan

log = logging.getLogger("dealwatch.scheduler")
STALL_AFTER = timedelta(hours=2)
FIRST_SCAN_DELAY = timedelta(minutes=2)


class Scheduler:
    def __init__(self, cfg: Config, notifier: Notifier | None = None):
        self.cfg = cfg
        self.notifier = notifier or Notifier()
        self.started_at = datetime.now(timezone.utc)
        self._sched = BackgroundScheduler(timezone="UTC")
        self._running = threading.Event()

    def start(self) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            interval = settings_mod.load(conn)["scan_interval_min"]
        finally:
            conn.close()
        self._sched.add_job(self.scan, "interval", id="scan", minutes=interval, jitter=120, max_instances=1,
                            coalesce=True, next_run_time=datetime.now(timezone.utc) + FIRST_SCAN_DELAY)
        self._sched.add_job(self.watchdog, "interval", id="watchdog", minutes=15)
        self._sched.start()
        log.info("scheduler started: scans every %s min, first in %s", interval, FIRST_SCAN_DELAY)

    def shutdown(self) -> None:
        self._sched.shutdown(wait=False)

    @property
    def scanning(self) -> bool:
        return self._running.is_set()

    @property
    def busy(self) -> bool:
        """Scanning, or a "Scan now" is queued and about to start."""
        return self.scanning or any(j.id not in ("scan", "watchdog") for j in self._sched.get_jobs())

    def reschedule(self, minutes: int) -> None:
        self._sched.reschedule_job("scan", trigger="interval", minutes=minutes, jitter=120)
        log.info("scan interval changed to %s min", minutes)

    def scan(self, **kw) -> None:
        self._running.set()
        conn = db.connect(self.cfg.db_path)
        try:
            run_scan(conn, self.cfg.data_dir / "scan.lock", self.notifier, **kw)
        except Exception:
            log.exception("scan crashed")
        finally:
            conn.close()
            self._running.clear()

    def scan_now(self, **kw) -> bool:
        """Start a scan in the background. False if one is already running or queued in this process."""
        if self.busy:
            return False
        self._sched.add_job(self.scan, kwargs=kw, max_instances=1)
        return True

    def watchdog(self) -> None:
        """Warn once if no scan has finished for 2 hours (e.g. a hung browser holding the lock)."""
        now = datetime.now(timezone.utc)
        if now - self.started_at < STALL_AFTER:
            return
        conn = db.connect(self.cfg.db_path)
        try:
            s = settings_mod.load(conn)
            last = (s.get("last_run") or {}).get("finished_at")
            stalled = not last or last < iso(now - STALL_AFTER)
            warned = s.get("watchdog_warned", False)
            if stalled and not warned:
                ok = self.notifier.health(f"⚠️ Deal Watch: no scan has finished since {last or 'startup'}.")
                settings_mod.put(conn, "watchdog_warned", bool(ok))
            elif not stalled and warned:
                settings_mod.put(conn, "watchdog_warned", False)
        finally:
            conn.close()
