"""수집기 설정 — 앱 스택에 기대지 않는다.

왜 따로 두나
------------
Qurious 본체는 `app/config.py` 가 pydantic-settings 로 Postgres·Redis·Qdrant·Neo4j·
Ollama 주소를 한꺼번에 읽는다. 그 중 **수집에 필요한 것은 하나도 없다** — 필요한 것은
API 키와 디스크뿐이다.

2026-09-22 가동이 목표인데 수집기를 그 스택에 묶어 두면, 스택이 안 뜨는 날은 그날 시세를
통째로 놓친다. 놓친 시세는 사람이 기억해서 메꿔야 하고, 결국 안 메꾼다. 그래서 여기서는
**표준 라이브러리 + requests** 만 쓴다.

키는 어디서 오나
----------------
저장소 루트의 `.env` 다. `.gitignore` 에 들어 있어 커밋되지 않는다. python-dotenv 를
쓰지 않는 것도 같은 이유 — 의존성을 하나라도 덜 만든다.

⚠️ `DATA_GO_KR_API_KEY` 는 공공데이터포털이 주는 **Encoding 키**라 이미 퍼센트 인코딩돼
   있다. requests 가 한 번 더 인코딩하면 `%2B` 가 `%252B` 가 되어 서명이 깨진다.
   그래서 읽는 즉시 `unquote()` 한다. 이 함정은 실제로 겪었다(#23).
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import Dict, Optional

# 저장소 루트 = 이 파일의 부모의 부모
ROOT = Path(__file__).resolve().parents[1]

# 수집 산출물이 쌓이는 곳. .gitignore 에 등록돼 있다 — 원자료는 커밋하지 않는다.
DATA_DIR = ROOT / "data" / "collector"
DB_PATH = DATA_DIR / "market.sqlite3"
MANIFEST_DIR = DATA_DIR / "manifest"
STATE_DIR = DATA_DIR / "state"


# ==================================================
# 1. .env 읽기
# ==================================================
def _load_env_file(path: Path) -> Dict[str, str]:
    """`KEY=VALUE` 만 읽는 최소 파서. 따옴표와 CR 을 벗긴다."""
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().replace("\r", "")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


_ENV_CACHE: Optional[Dict[str, str]] = None


def env(key: str, default: str = "") -> str:
    """환경변수를 먼저 보고, 없으면 `.env` 를 본다.

    환경변수가 우선인 이유는 컨테이너·CI 에서 파일 없이 주입하는 경로를 막지 않기
    위해서다.
    """
    global _ENV_CACHE
    if os.environ.get(key):
        return os.environ[key]
    if _ENV_CACHE is None:
        _ENV_CACHE = _load_env_file(ROOT / ".env")
    return _ENV_CACHE.get(key, default)


class MissingKey(RuntimeError):
    """키가 없다. **무엇을 해야 하는지까지** 알려 준다 — 막다른 길로 만들지 않는다."""


def require(key: str, what: str) -> str:
    v = env(key)
    if not v:
        raise MissingKey(
            f"{key} 가 없다 — {what}\n"
            f"  찾은 곳: 환경변수, 그리고 {ROOT / '.env'}\n"
            f"  할 일: .env 에 `{key}=...` 한 줄을 넣는다. .env 는 gitignore 돼 있어\n"
            f"         커밋되지 않는다."
        )
    return v


def portal_key() -> str:
    """공공데이터포털 서비스키. **반드시 unquote 를 거친 값**을 돌려준다."""
    return urllib.parse.unquote(require("DATA_GO_KR_API_KEY", "공공데이터포털 서비스키"))


# ==================================================
# 2. 공공데이터포털 — 주식시세
# ==================================================
PORTAL_PRICE_URL = (
    "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
)

#: 하루치 전종목이 한 번에 들어오는 크기.
#:
#: 실측 2026-09-19 (basDt=20260917): 2,870행 · 784,012 B.
#: 내역은 KOSPI 942 · KOSDAQ 1,820 · KONEX 108 — **세 시장이 모두 들어온다.**
#: KRX Open API 가 유가증권만 주는 것과 다르다. 여유를 두어 3000 으로 잡되,
#: 응답의 totalCount 가 이 값을 넘으면 수집기가 **예외로 막는다**(조용히 자르지 않는다).
PORTAL_ROWS = 3000

#: 호출 사이 최소 간격(초). 상대 서버를 배려하는 값이지 규약에 적힌 값이 아니다.
PORTAL_SLEEP = 0.35

#: 하루 호출 한도. 응답 헤더 `X-RateLimit-Limit` 실측값(2026-09-19: 10000).
#: 리셋은 고정 창이다 — 근거는 collector/README.md 의 "유량" 절.
PORTAL_DAILY_LIMIT = 10_000

#: 이만큼 남으면 그날 수집을 멈춘다. 다른 작업(대화형 조회·검증)이 쓸 몫을 남긴다.
PORTAL_RESERVE = 300


# ==================================================
# 3. 한국투자증권(KIS)
# ==================================================
KIS_REAL_URL = "https://openapi.koreainvestment.com:9443"
KIS_MOCK_URL = "https://openapivts.koreainvestment.com:29443"

#: KIS 호출 간격(초).
#:
#: 약관 제12조는 유량을 "회사가 정하여 게시한다" 고만 하고 **수치를 싣지 않는다**(#33).
#: 그래서 직접 재 보았다 — 모의 계정에서 **초당 약 2건**을 넘기면 EGW00201 이 돌아온다.
#: 게시된 값이 아니므로 여유 있게 잡는다.
KIS_SLEEP = 0.6

#: 토큰 유효기간은 24시간이다. 매번 새로 받으면 그것만으로 유량을 쓰므로 캐시한다.
KIS_TOKEN_CACHE = STATE_DIR / "kis_token.json"


def kis_credentials(paper: bool = True) -> tuple[str, str, str]:
    """(app_key, app_secret, account_no).

    ⚠️ 기본이 **모의**(`paper=True`)인 것은 실수 방지다. 실계좌 키는 주문이 실제로
       체결되므로, 부르는 쪽이 `paper=False` 를 명시적으로 적게 만든다.
    """
    p = "KIS_MOCK_" if paper else "KIS_"
    what = "KIS 모의투자 키" if paper else "KIS 실계좌 키 (⚠️ 실제 체결)"
    return (
        require(f"{p}APP_KEY", what),
        require(f"{p}APP_SECRET", what),
        require(f"{p}ACCOUNT_NO", what),
    )


def ensure_dirs() -> None:
    for d in (DATA_DIR, MANIFEST_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)
