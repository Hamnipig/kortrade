"""관세청 수출입무역통계 OpenAPI 클라이언트.

공공데이터포털(data.go.kr) 게이트웨이를 통해 관세청 무역통계를 조회한다.
- 응답은 XML. 표준 응답 구조는 <response><header/><body><items><item/>...</items></body></response>
- 일부 엔드포인트는 header 없이 <items> 만 반환하기도 하므로 양쪽을 모두 처리한다.
- 인증키(serviceKey)는 URL 인코딩된 형태로 발급되므로 requests 의 자동 인코딩을 우회한다.
"""

from __future__ import annotations

import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlencode

import requests
import yaml

log = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# 게이트웨이가 명시적으로 돌려주는 오류 코드 중 재시도가 무의미한 것들
FATAL_CODES = {
    "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
    "SERVICE_ACCESS_DENIED_ERROR",
    "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR",
    "UNREGISTERED_IP_ERROR",
    "DEADLINE_HAS_EXPIRED_ERROR",
    "30",  # SERVICE KEY IS NOT REGISTERED
    "22",  # LIMITED NUMBER OF SERVICE REQUESTS EXCEEDS
}

# 잘못된 파라미터를 지적하는 응답. 같은 요청을 다시 보내도 답이 바뀌지 않으므로
# **재시도하면 안 된다**. (fatal 과 다르다: 전체 실행을 중단시키지는 않는다.)
#   실측 사례 — sidoCd 를 00~99 로 훑을 때 없는 코드마다 resultCode 99
#   '존재하지 않는 시도코드입니다' 가 돌아온다. 이걸 재시도하면 코드 하나당
#   1.5+3+6+12 = 22.5초를 버리고, 100개 훑으면 37분이 그냥 날아간다.
_PERMANENT_HINTS = ("존재하지 않", "유효하지 않", "잘못된", "NOT_EXIST", "INVALID")


def _is_permanent(code: str, msg: str) -> bool:
    return code == "99" and any(h in msg for h in _PERMANENT_HINTS)


class CustomsAPIError(RuntimeError):
    def __init__(self, message: str, code: str | None = None, fatal: bool = False,
                 permanent: bool = False):
        super().__init__(message)
        self.code = code
        self.fatal = fatal
        # 재시도 무의미 (잘못된 파라미터). fatal 이면 자동으로 permanent 이기도 하다.
        self.permanent = permanent or fatal


@dataclass
class EndpointSpec:
    name: str
    path: str
    doc: str
    params: dict[str, dict]
    item_fields: dict[str, str]

    @property
    def required(self) -> list[str]:
        return [k for k, v in self.params.items() if v.get("required")]


@dataclass
class CustomsClient:
    service_key: str | None = None
    config_path: Path = CONFIG_DIR / "api.yaml"
    # (연결, 응답) 분리. 연결이 안 되는 건 40초를 기다려도 결과가 같으므로 짧게 끊고
    # 재시도로 넘긴다. 응답은 대용량 구간에서 느릴 수 있어 넉넉히 준다.
    timeout: tuple[int, int] = (15, 90)
    max_retries: int = 4
    min_interval: float = 0.35          # 초당 ~3콜. 게이트웨이 부하 방지
    daily_call_budget: int = 9_000      # 개발계정 10,000 대비 안전 마진

    host: str = field(init=False)
    prefix: str = field(init=False)
    max_months_per_call: int = field(init=False)
    endpoints: dict[str, EndpointSpec] = field(init=False)
    calls_made: int = field(init=False, default=0)
    _last_call: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.host = cfg["host"]
        self.prefix = cfg["prefix"]
        self.max_months_per_call = int(cfg.get("max_months_per_call", 12))
        self.endpoints = {
            name: EndpointSpec(name=name, **spec) for name, spec in cfg["endpoints"].items()
        }
        if self.service_key is None:
            self.service_key = os.environ.get("DATA_GO_KR_SERVICE_KEY")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "kortrade/1.0 (+customs trade proxy pipeline)"

    # ------------------------------------------------------------------ 요청

    def _throttle(self) -> None:
        delta = time.monotonic() - self._last_call
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last_call = time.monotonic()

    def _build_url(self, spec: EndpointSpec, params: dict[str, Any]) -> str:
        # serviceKey 는 발급 시점에 이미 퍼센트 인코딩된 문자열이므로 재인코딩하면 안 된다.
        rest = {k: v for k, v in params.items() if k != "serviceKey" and v is not None}
        qs = urlencode(rest, quote_via=quote)
        key = params["serviceKey"]
        return f"{self.host}{self.prefix}{spec.path}?serviceKey={key}&{qs}"

    def call(self, endpoint: str, **params: Any) -> list[dict[str, str]]:
        """엔드포인트를 1회 호출하고 정규화된 레코드 리스트를 돌려준다."""
        spec = self.endpoints[endpoint]
        if not self.service_key:
            raise CustomsAPIError(
                "인증키가 없습니다. 환경변수 DATA_GO_KR_SERVICE_KEY 를 설정하거나 "
                "CustomsClient(service_key=...) 로 전달하세요.",
                fatal=True,
            )
        if self.calls_made >= self.daily_call_budget:
            raise CustomsAPIError(
                f"일일 호출 예산({self.daily_call_budget}) 소진. 내일 이어서 실행하세요.", fatal=True
            )

        missing = [p for p in spec.required if p != "serviceKey" and params.get(p) is None]
        if missing:
            raise ValueError(f"{endpoint}: 필수 파라미터 누락 {missing}")

        full = {"serviceKey": self.service_key, **params}
        url = self._build_url(spec, full)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
                self.calls_made += 1
                resp.raise_for_status()
                return parse_response(resp.content, spec.item_fields)
            except CustomsAPIError as exc:
                # 잘못된 파라미터는 재시도해도 같은 답이 온다. 즉시 올린다.
                if exc.permanent:
                    raise
                last_exc = exc
            except (requests.RequestException, ET.ParseError) as exc:
                last_exc = exc
            backoff = 1.5 * (2**attempt)
            log.warning("%s 호출 실패(%s/%s): %s — %.1fs 후 재시도",
                        endpoint, attempt + 1, self.max_retries, last_exc, backoff)
            time.sleep(backoff)

        raise CustomsAPIError(f"{endpoint} 호출이 {self.max_retries}회 모두 실패: {last_exc}")


# ------------------------------------------------------------------ 응답 파싱

def _text(el: ET.Element) -> str:
    return (el.text or "").strip()


def parse_response(payload: bytes | str, item_fields: dict[str, str]) -> list[dict[str, str]]:
    """관세청 XML 응답을 {정규화필드: 값} 딕셔너리 리스트로 변환한다.

    게이트웨이 오류(header/resultCode != 00)는 CustomsAPIError 로 올린다.
    데이터가 없는 정상 응답(totalCount=0)은 빈 리스트를 돌려준다.
    """
    root = ET.fromstring(payload)

    # 게이트웨이 레벨 오류: <OpenAPI_ServiceResponse><cmmMsgHeader>...
    err_code = root.findtext(".//returnReasonCode") or root.findtext(".//errMsg")
    if root.find(".//cmmMsgHeader") is not None:
        code = (root.findtext(".//returnReasonCode") or "").strip()
        msg = (root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg") or "").strip()
        raise CustomsAPIError(f"게이트웨이 오류 [{code}] {msg}", code=code,
                              fatal=code in FATAL_CODES)

    # 서비스 레벨 오류
    result_code = root.findtext(".//header/resultCode") or root.findtext(".//resultCode")
    if result_code is not None:
        rc = result_code.strip()
        if rc not in ("", "00", "0", "INFO-000"):
            msg = (root.findtext(".//header/resultMsg")
                   or root.findtext(".//resultMsg") or "").strip()
            raise CustomsAPIError(f"서비스 오류 [{rc}] {msg}", code=rc,
                                  fatal=rc in FATAL_CODES,
                                  permanent=_is_permanent(rc, msg))

    out: list[dict[str, str]] = []
    for item in root.iter("item"):
        rec: dict[str, str] = {}
        for child in item:
            key = item_fields.get(child.tag)
            if key:
                rec[key] = _text(child)
        if not rec:
            continue
        # 관세청 응답에는 기간이 '총계'인 합계 행이 섞여 들어온다. 저장하면 데이터가 2배로 오염된다.
        if is_total_row(rec):
            continue
        out.append(rec)
    if not out and err_code:
        log.debug("빈 응답 (reason=%s)", err_code)
    return out


# ------------------------------------------------------------------ 기간 헬퍼

def month_range(start: str, end: str) -> list[str]:
    """'202401','202412' -> ['202401', ..., '202412']"""
    sy, sm = int(start[:4]), int(start[4:6])
    ey, em = int(end[:4]), int(end[4:6])
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def chunk_periods(start: str, end: str, max_months: int = 12) -> Iterable[tuple[str, str]]:
    """조회기간 제한(기본 12개월)에 맞춰 (strtYymm, endYymm) 구간으로 쪼갠다."""
    months = month_range(start, end)
    for i in range(0, len(months), max_months):
        block = months[i : i + max_months]
        yield block[0], block[-1]


TOTAL_LABELS = ("총계", "합계", "계", "total", "TOTAL", "-")


def is_total_row(rec: dict[str, str]) -> bool:
    """기간 필드가 '총계'/'합계'인 집계 행인지. 실제 응답에 섞여 오므로 반드시 걸러야 한다."""
    period = (rec.get("period") or "").strip()
    if not period:
        return False
    if period in TOTAL_LABELS:
        return True
    # '총계' 행은 HS코드가 '-' 로 오기도 한다
    if not any(ch.isdigit() for ch in period):
        return True
    return False


def normalize_period(raw: str) -> str:
    """'2016.01' / '201601' / '2016-01' -> '2016-01'. 숫자가 없으면 빈 문자열."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 6:
        return f"{digits[:4]}-{digits[4:6]}"
    return ""


def hs6(code: str) -> str:
    """HS 코드를 6단위로 자른다.

    품목별 API 는 hsSgn=330499(6단위)로 요청해도 3304991000 등 **10단위로 쪼개서** 응답한다.
    섹터 레이어와 기업 레이어를 같은 축에서 비교하려면 6단위 롤업 키가 필요하다.
    """
    d = "".join(ch for ch in (code or "") if ch.isdigit())
    return d[:6] if len(d) >= 6 else d


def split_sigungu(raw: str) -> tuple[str, str]:
    """'강원특별자치도 강릉시' -> ('강원특별자치도', '강릉시').

    시군구 API 의 sggNm 은 시도명이 앞에 붙어서 온다. 설정 파일에는 '강릉시'로만 쓰므로
    저장 시 분리해 둬야 조회가 맞는다. 공백이 없으면 전체를 시군구명으로 본다.
    """
    s = (raw or "").strip()
    if " " in s:
        head, tail = s.rsplit(" ", 1)
        return head.strip(), tail.strip()
    return "", s
