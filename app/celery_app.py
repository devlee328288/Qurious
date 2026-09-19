"""Celery 애플리케이션 설정.

브로커/백엔드: Redis (이미 스택에 존재)
워커 실행: celery -A app.celery_app worker --loglevel=info --concurrency=2
Beat  실행: celery -A app.celery_app beat  --loglevel=info
"""
from celery import Celery
from celery.schedules import crontab
from app.config import settings

celery_app = Celery("lumina-invest")

celery_app.conf.update(
    # ── 브로커 / 백엔드 ─────────────────────────────────────────────────────────
    broker_url=settings.REDIS_URL,
    result_backend=settings.REDIS_URL,

    # ── 직렬화 ──────────────────────────────────────────────────────────────────
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # ── 시간대 ──────────────────────────────────────────────────────────────────
    timezone="Asia/Seoul",
    enable_utc=True,

    # ── 태스크 동작 ─────────────────────────────────────────────────────────────
    task_track_started=True,       # STARTED 상태 기록 (폴링용)
    task_acks_late=True,           # 완료 후 ACK → 워커 crash 시 재큐
    worker_prefetch_multiplier=1,  # LLM 태스크가 무거우므로 1:1 처리
    result_expires=3600,           # 결과 1시간 보관

    # ── Celery Beat 주기 스케줄 ────────────────────────────────────────────────
    beat_schedule={
        "sync-market-data-hourly": {
            "task": "app.tasks.sync_tasks.sync_market_data",
            "schedule": 3600.0,           # 1시간
            "options": {"expires": 3500},
        },
        "sync-candles-daily": {
            "task": "app.tasks.sync_tasks.sync_stock_candles",
            "schedule": 86400.0,          # 24시간
            "options": {"expires": 82800},
        },

        # ── 시세 수집 (collector 패키지) ─────────────────────────────────────
        # 셋을 한 시간 간격으로 벌려 둔다. 수정주가는 수집이 끝난 뒤에 돌아야 하고,
        # 매니페스트는 그 둘이 끝난 상태의 지문이어야 하기 때문이다.
        #
        # crontab 을 쓰는 이유: 위의 두 개처럼 초 단위 간격으로 두면 워커를 재시작할
        # 때마다 실행 시각이 밀려, "어제는 몇 시에 돌았나" 를 아무도 모르게 된다.
        #
        # 시각은 KST 다 (timezone="Asia/Seoul"). 당일 시세를 받는 것이 아니라 최근
        # 2주의 빈 날을 채우는 것이라, 장 마감 시각과 맞출 이유가 없다.
        "collector-portal-recent": {
            "task": "collector.portal_recent",
            "schedule": crontab(hour=18, minute=0),
            "options": {"expires": 3600 * 5},
        },
        "collector-rebuild-adjusted": {
            "task": "collector.rebuild_adjusted",
            "schedule": crontab(hour=19, minute=0),
            "options": {"expires": 3600 * 5},
        },
        "collector-write-manifest": {
            "task": "collector.write_manifest",
            "schedule": crontab(hour=20, minute=0),
            "options": {"expires": 3600 * 3},
        },
    },
)

# tasks 패키지 자동 탐색
celery_app.autodiscover_tasks(["app.tasks"])
