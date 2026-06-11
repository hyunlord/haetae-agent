"""라이브 호출 트랜스크립트 사이드카 (WO#67) — 모델 입출력을 *bounded*로 표면화.

#54가 codex `--json` 이벤트를 줄 단위로 다 읽고, #55가 사이드카 패턴(heartbeat.json)을 깔았다.
#67은 그 스트림에서 각 호출의 **받은 입력**(order/프롬프트 + 컨텍스트)과 **실시간 출력 tail**
(스트리밍 텍스트)을 transcripts.json으로 흘린다 — 대시보드가 유닛/단계별로 드릴다운한다.

설계(불변):
  - **bounded**: 입력은 head cap, 출력은 *rolling tail* cap, 런당 보관 호출 수 cap. 전체 무제한
    트랜스크립트 저장 금지. 호출당·런당 크기 상한을 강제한다.
  - **순수 텔레메트리·best-effort**: 캡처/쓰기 실패가 *절대* run을 죽이지 않는다(#55/#43 패턴).
    엔진 판정/codex 동작에 전혀 영향 없음 — 이벤트를 읽어 흘릴 뿐.
  - **사이드카**: state.yaml *옆*(같은 run 디렉터리)에 transcripts.json. atomic write(tmp→replace)로
    대시보드가 부분 읽기를 안 하게(#55 패턴). state/heartbeat 스키마 무변경 — 별 파일.
  - **throttle**: 출력 beat는 ~1초 1회로 합치고, 시작/종료(transition)는 즉시 flush(force).

call_kind/unit은 *heartbeat 컨텍스트*(루프가 set_context로 깔아둔 스레드별 값)에서 읽어 온다 —
트랜스크립트는 그걸 그대로 따라가므로 별도 배선이 필요 없다(observe_call이 codex.py에서 묶는다).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# bounded 상한(호출당·런당). 무제한 저장 금지 — 사이드카는 *현재 보이게* 하는 게 목적이지
# 전체 기록 보관소가 아니다. 풀-트랜스크립트는 후속(호출별 비용/전문 리포트)에서 다룬다.
_INPUT_CAP = 4000       # 입력(프롬프트)은 head cap — work order는 앞에 있으므로 머리를 남긴다.
_OUTPUT_CAP = 8000      # 출력은 rolling *tail* — 최근 K자만(지금 뭘 뱉나). 넘치면 앞부분 버림.
_RUN_CAP = 30           # 런당 보관 호출 수 — 초과 시 가장 오래된 *완료* 호출부터 드롭(활성은 보존).


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _head(text: str | None, cap: int) -> tuple[str, bool]:
    """머리에서 cap자만 — (capped_text, truncated). 입력(work order는 앞)에 적합."""
    s = "" if text is None else str(text)
    if len(s) <= cap:
        return s, False
    return s[:cap], True


class TranscriptWriter:
    """진행 중/완료 codex 호출의 입력+출력 tail을 transcripts.json으로 흘리는 best-effort writer.

    path:       transcripts.json 경로(None이면 디스크 미기록 — 인메모리 추적만, 테스트/무사이드카).
    throttle:   같은 호출의 output beat 사이 최소 쓰기 간격 초(기본 1.0). 시작/종료는 즉시.
    input_cap/output_cap/run_cap: bounded 상한(위 모듈 기본). 호출당 입력 head·출력 tail·런당 보관.
    clock:      현재 시각 생성기(테스트 주입). 기본 UTC now.
    writer:     payload(dict)를 받는 쓰기 콜백(테스트 주입/실패 시뮬). 기본 파일 atomic write.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        throttle: float = 1.0,
        input_cap: int = _INPUT_CAP,
        output_cap: int = _OUTPUT_CAP,
        run_cap: int = _RUN_CAP,
        clock: Callable[[], datetime] | None = None,
        writer: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.path = Path(path) if path else None
        self.throttle = throttle
        self.input_cap = input_cap
        self.output_cap = output_cap
        self.run_cap = run_cap
        self._clock = clock or _utcnow
        self._writer = writer  # None → 기본 파일 쓰기
        self._lock = threading.Lock()
        # call_id → 레코드. dict는 삽입 순서 보존(런당 보관/드롭 결정론).
        self._calls: dict[int, dict[str, Any]] = {}
        self._next = 0
        self._last_flush: datetime | None = None

    # ── 라이프사이클(observe_call이 호출) ───────────────────────────────────
    def start(
        self, *, kind: str | None, unit: str | None, input_text: str | None
    ) -> int:
        """호출 시작 → 입력(head cap) 기록 + 활성 등록. handle(call_id) 반환. 즉시 flush."""
        now = self._clock()
        inp, inp_trunc = _head(input_text, self.input_cap)
        with self._lock:
            cid = self._next
            self._next += 1
            self._calls[cid] = {
                "kind": kind,
                "unit": unit,
                # director-side(유닛 없는) 호출의 단계 라벨 = kind(합성/스캐폴드/replan/통합).
                "phase": kind if unit is None else None,
                "input": inp,
                "input_truncated": inp_trunc,
                "output": "",            # rolling tail 버퍼
                "output_truncated": False,
                "output_chars": 0,        # 본 전체 출력 글자 수(tail만 노출됨을 정직히)
                "started_at": now,
                "last_event_at": now,
                "status": "active",
            }
        self._flush(force=True)
        return cid

    def output(self, call_id: int, text: str | None) -> None:
        """출력 한 조각을 rolling tail에 누적(cap 초과 시 앞부분 버림). throttle된 flush."""
        if not text:
            return
        with self._lock:
            c = self._calls.get(call_id)
            if c is None:
                return
            piece = str(text)
            c["output_chars"] += len(piece)
            buf = c["output"] + ("\n" if c["output"] else "") + piece
            if len(buf) > self.output_cap:
                buf = buf[-self.output_cap:]   # rolling: 최근 cap자만(앞부분 truncate)
                c["output_truncated"] = True
            c["output"] = buf
            c["last_event_at"] = self._clock()
        self._flush(force=False)

    def finish(self, call_id: int, status: str = "done") -> None:
        """호출 종료 → status 기록(done/error). capped 기록으로 collapse(이미 capped). 즉시 flush.

        런당 보관 한도(run_cap) 초과 시 가장 오래된 *완료* 호출부터 드롭(활성은 절대 안 버림).
        """
        with self._lock:
            c = self._calls.get(call_id)
            if c is not None:
                c["status"] = status
                c["last_event_at"] = self._clock()
            self._evict_locked()
        self._flush(force=True)

    # ── 보관 한도(런당) ────────────────────────────────────────────────────
    def _evict_locked(self) -> None:
        """run_cap 초과분을 가장 오래된 *완료* 호출부터 드롭. 활성 호출은 보존(지금 보임)."""
        while len(self._calls) > self.run_cap:
            victim = None
            for cid, c in self._calls.items():  # 삽입 순(오래된 것 먼저)
                if c["status"] != "active":
                    victim = cid
                    break
            if victim is None:
                break  # 전부 활성 — 드롭 안 함(보존)
            self._calls.pop(victim, None)

    # ── 직렬화/쓰기(전부 best-effort — run을 절대 안 죽인다) ──────────────────
    def _payload(self, now: datetime) -> dict[str, Any]:
        calls: list[dict[str, Any]] = []
        with self._lock:
            for cid, c in self._calls.items():
                started: datetime = c["started_at"]
                last: datetime = c["last_event_at"]
                calls.append(
                    {
                        "call_id": cid,
                        "kind": c["kind"],
                        "unit": c["unit"],
                        "phase": c["phase"],
                        "input": c["input"],
                        "input_truncated": c["input_truncated"],
                        "output_tail": c["output"],
                        "output_truncated": c["output_truncated"],
                        "output_chars": c["output_chars"],
                        "started_at": _iso(started),
                        "last_event_at": _iso(last),
                        "elapsed_s": round((now - started).total_seconds(), 1),
                        "idle_seconds": round((now - last).total_seconds(), 1),
                        "status": c["status"],
                    }
                )
        return {"updated_at": _iso(now), "calls": calls}

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
        """transcripts.json을 atomic(tmp→replace)하게 쓴다 — 대시보드가 부분 읽기 안 하게."""
        assert self.path is not None
        text = json.dumps(payload, ensure_ascii=False)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
