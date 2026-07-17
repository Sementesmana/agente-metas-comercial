"""Cron diário do pipeline (padrão da casa: APScheduler, 08h BRT)."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import CONFIG
from pipeline import run_sync

log = logging.getLogger("MetasComercial.Cron")


def _job_diario():
    try:
        resumo = run_sync(force_sa=True)
        log.info(f"Cron diário OK: {resumo}")
    except Exception as e:
        log.error(f"Cron diário falhou: {e}")


def start_scheduler():
    sched = BackgroundScheduler(timezone=CONFIG["TZ"])
    sched.add_job(
        _job_diario, CronTrigger(hour=CONFIG["CRON_HOUR"], minute=0),
        id="sync_diario", replace_existing=True, coalesce=True, max_instances=1,
    )
    sched.start()
    log.info(f"Cron agendado: diário às {CONFIG['CRON_HOUR']:02d}:00 {CONFIG['TZ']}.")
    return sched
