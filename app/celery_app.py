"""Celery 애플리케이션 설정.

브로커/백엔드: Redis (이미 스택에 존재)
워커 실행: celery -A app.celery_app worker --loglevel=info --concurrency=2
Beat  실행: celery -A app.celery_app beat  --loglevel=info
"""
from celery import Celery
from celery.schedules import crontab
from app.config import settings

# ⚠️ 태스크 모듈을 **명시적으로** 싣는다.
#
# 원래는 맨 아래 autodiscover_tasks(["app.tasks"]) 하나로 끝내려 했는데, 그 함수는
# 기본적으로 각 패키지 아래의 `tasks` 라는 이름의 모듈(= app/tasks/tasks.py)을 찾는다.
# 그런 파일이 없어서 **어떤 태스크도 등록되지 않았다.** 워커를 띄우면 [tasks] 목록이 빈
# 채로 뜨고, 태스크를 보내면 NotRegistered 가 난다 (2026-09-19 Redis 컨테이너로 실측).
#
# 조용히 실패하는 종류라, 워커가 "ready" 라고 찍고 나서도 아무 일도 안 일어난다.
# 모듈을 직접 적으면 그런 일이 없고, 새 태스크 파일을 만들 때 한 줄 추가하면 된다.
celery_app = Celery(
    "lumina-invest",
    include=[
        "app.tasks.agent_tasks",
        "app.tasks.ingest_tasks",
        "app.tasks.sync_tasks",
        "app.tasks.collector_tasks",
    ],
)

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
        # ⚠️ 부르는 이름은 **등록 이름**이어야 한다. 모듈 경로가 아니다.
        #
        # 원래 "app.tasks.sync_tasks.sync_market_data" 였는데, 그 태스크의 실제 등록
        # 이름은 @celery_app.task(name="sync.market_data") 로 지정된 "sync.market_data"
        # 다. 이름이 어긋나면 Beat 가 쏜 메시지를 워커가 모르는 태스크로 보고 버린다.
        # A4(#23)가 "Beat 태스크 2개가 한 번도 실행된 적 없음" 으로 지적한 그 결함이다.
        #
        # 위쪽 include 문제(태스크가 아예 등록조차 안 되던 것)와는 **별개의 버그**라,
        # 하나만 고쳐서는 여전히 안 돈다. 둘 다 고쳐야 한다.
        "sync-market-data-hourly": {
            "task": "sync.market_data",
            "schedule": 3600.0,           # 1시간
            "options": {"expires": 3500},
        },
        "sync-candles-daily": {
            "task": "sync.stock_candles",
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

# 위 include 가 실제 등록을 맡는다. autodiscover 는 앞으로 app/tasks/tasks.py 를 만들
# 경우를 대비해 남겨 두지만, **여기에 의존하지 않는다** (위 주석 참고).
celery_app.autodiscover_tasks(["app.tasks"])
