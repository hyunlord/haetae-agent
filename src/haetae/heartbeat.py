"""라이브 하트비트 사이드카 (WO#55) — 진행 중인 codex 호출을 표면화.

hang 때 대시보드가 "0 활성"에 작업로그가 침묵하면 살아있나/뭐 하나/멈췄나 알 수 없었다.
#54가 codex `--json`을 줄 단위로 읽는 스트리밍 루프를 깔았으니, 그 이벤트를 *사이드카
heartbeat.json* 으로 흘려 라이브로 보이게 한다.

설계(확정):
  - **사이드카**: state.yaml *옆*(같은 run 디렉터리)에 heartbeat.json을 best-effort로 쓴다.
    state.yaml은 단계 경계 권위 상태 그대로 — 이건 별 파일(스키마 무변경).
  - **순수 텔레메트리**: 엔진 판정/codex 동작에 *전혀* 영향 없음. 읽기만 표면화.
  - **best-effort**: 하트비트 쓰기 실패가 *절대* run을 죽이지 않는다(#43/#33 패턴).
  - **동시성**: 병렬 모드는 여러 codex 호출이 동시 진행 → handle별 슬롯(dict)+lock.
  - **throttle**: 이벤트마다 디스크를 때리지 않게 ~1초 1회로 합치되, 시작/종료(transition)는
    즉시 flush(force) — 활성 등장/사라짐이 라이브로 보이게.

call_kind/unit은 *호출자(loop)* 가 주입한다. 루프는 codex 호출 직전 set_context로 스레드별
컨텍스트를 깔고(같은 스레드에서 호출이 실행됨), codex 클라이언트가 _run 시작 시 그걸 읽어
start() 한다. 이벤트는 명시 handle로 보고하므로(스트리밍 리더가 다른 스레드여도) 안전하다.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class HeartbeatWriter:
    """진행 중인 codex 호출을 heartbeat.json으로 라이브 표면화하는 best-effort writer.

    path:     heartbeat.json 경로(None이면 디스크 미기록 — 인메모리 추적만, 테스트/무사이드카).
    throttle: 같은 활동의 beat 사이 최소 쓰기 간격 초(기본 1.0). 시작/종료는 즉시.
    clock:    현재 시각 생성기(테스트 주입). 기본 UTC now.
    writer:   payload(dict)를 받는 쓰기 콜백(테스트 주입/실패 시뮬). 기본 파일 atomic write.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        throttle: float = 1.0,
        clock: Callable[[], datetime] | None = None,
        writer: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.path = Path(path) if path else None
        self.throttle = throttle
        self._clock = clock or _utcnow
        self._writer = writer  # None → 기본 파일 쓰기
        self._lock = threading.Lock()
        self._acts: dict[int, dict[str, Any]] = {}
        self._next = 0
        self._last_flush: datetime | None = None
        self._ctx = threading.local()

    # ── 호출자(loop)가 깔고 codex 클라이언트가 읽는 스레드별 컨텍스트 ──────────
    def set_context(self, call_kind: str | None, unit: str | None) -> None:
        self._ctx.kind = call_kind
        self._ctx.unit = unit

    def get_context(self) -> tuple[str | None, str | None]:
        return getattr(self._ctx, "kind", None), getattr(self._ctx, "unit", None)

    # ── codex 클라이언트가 호출하는 라이프사이클 ──────────────────────────────
    def start(
        self, call_kind: str | None, unit: str | None, *, idle_timeout: float | None = None
    ) -> int:
        """진행 활동 등록 → handle 반환. 즉시 flush(활성 등장이 라이브로 보이게)."""
        now = self._clock()
        with self._lock:
            h = self._next
            self._next += 1
            self._acts[h] = {
                "call_kind": call_kind,
                "unit": unit,
                "started_at": now,
                "last_event_at": now,
                "last_event_summary": None,
                "idle_timeout": idle_timeout,
            }
        self._flush(force=True)
        return h

    def beat(self, handle: int, summary: str | None) -> None:
        """이 활동의 last_event 갱신(+요약). throttle된 flush — 핫루프에서도 가볍게."""
        with self._lock:
            a = self._acts.get(handle)
            if a is None:
                return
            a["last_event_at"] = self._clock()
            if summary:
                a["last_event_summary"] = summary
        self._flush(force=False)

    def finish(self, handle: int) -> None:
        """활동 종료 → 슬롯 제거. 즉시 flush(사라짐이 라이브로; 다 비면 idle 표기)."""
        with self._lock:
            self._acts.pop(handle, None)
        self._flush(force=True)

    # ── 직렬화/쓰기 (전부 best-effort — run을 절대 안 죽인다) ────────────────
    def _payload(self, now: datetime) -> dict[str, Any]:
        acts: list[dict[str, Any]] = []
        with self._lock:
            for a in self._acts.values():
                started: datetime = a["started_at"]
                last: datetime = a["last_event_at"]
                acts.append(
                    {
                        "call_kind": a["call_kind"],
                        "unit": a["unit"],
                        "started_at": _iso(started),
                        "last_event_at": _iso(last),
                        "elapsed_s": round((now - started).total_seconds(), 1),
                        "idle_seconds": round((now - last).total_seconds(), 1),
                        "last_event_summary": a["last_event_summary"],
                        "idle_timeout": a["idle_timeout"],
                    }
                )
        acts.sort(key=lambda x: (x["unit"] or "", x["call_kind"] or ""))
        return {"updated_at": _iso(now), "activities": acts}

    def _flush(self, *, force: bool) -> None:
        now = self._clock()
        if not force and self._last_flush is not None:
            if (now - self._last_flush).total_seconds() < self.throttle:
                return  # throttle: 너무 잦은 쓰기 합치기
        self._last_flush = now
        payload = self._payload(now)
        try:
            if self._writer is not None:
                self._writer(payload)
            elif self.path is not None:
                self._default_write(payload)
        except Exception:  # noqa: BLE001 — 텔레메트리 쓰기 실패는 절대 run을 죽이지 않는다
            pass

    def _default_write(self, payload: dict[str, Any]) -> None:
        """heartbeat.json을 atomic(tmp→replace)하게 쓴다 — 대시보드가 부분 읽기 안 하게."""
        assert self.path is not None
        text = json.dumps(payload, ensure_ascii=False)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
