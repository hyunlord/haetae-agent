---
name: simulation-behavior
triggers: [simulation, sim, agents, agent, crowd, crowd-sim, pathfinding, navigation, flocking, collision, avoidance, steering, movement, flow, flow-field, rvo, orca, queue, congestion, spawn]
---

# 시뮬레이션 행동 패턴 (캡스톤 거친-동선 교훈 인코딩)

연속공간 다수 에이전트 시뮬레이션에서 *행동의 질*이 핵심이다. 빌드/렌더 성공만으론
콩나물 뭉침·데드락·정지를 못 잡는다. 아래는 *알고리즘 접근/원리*다 — **그대로 베끼지 말고**
맥락에 맞게 직접 구현하라(완성 코드 아님).

## 충돌 회피 (그리드락 방지 — 최우선)
**순진한 위치-점유 거부(naive position-blocking)를 쓰지 마라.** "다음 칸이 점유됐으면 멈춤"은
서로가 서로를 막아 즉시 교착(gridlock)한다 — 캡스톤서 스윕충돌 폭증·이동률 8%·90%+ blocked로 실측됨.
대신 아래 둘 중 하나(또는 결합)를 써라:

### A) 속도기반 상호 회피 (RVO / ORCA *개념*)
위치가 아니라 *속도*를 협상한다. 매 tick, 각 에이전트가:
1. 목표로 향하는 **desired velocity**(v_pref)를 구한다.
2. 시간지평 τ 안에서 이웃들의 (상대위치·상대속도)로 *충돌하는 속도 집합*(velocity obstacle)을 추정한다.
3. 그 집합 **밖**에서 v_pref에 가장 가까운 후보 속도를 고른다(샘플링 또는 선형제약 최적화).
4. **상호성(reciprocal)**: 두 에이전트가 회피 책임을 *절반씩* 나눠 진다 → 떨림(oscillation) 없이
   서로 비켜 흐른다. 한쪽만 피하게 하면 진동·교착이 남는다.

```
v_pref     = normalize(goal - pos) * max_speed
candidates = sample(v_pref)                       # v_pref 주변 + 감속/좌우 우회
safe       = [v for v in candidates
              if collision_free_within(tau, v, neighbors, share=0.5)]  # 0.5 = 상호 분담
v_new      = argmin(safe, key=lambda v: dist(v, v_pref))  # 충돌없는 것 중 의도에 가장 가까운
pos       += v_new * dt
```

### B) 대안 — flow-field(벡터장) 길찾기
목적지별로 그리드 위에 **벡터장**을 *미리* 계산(목표서 BFS/Dijkstra 역전파 → 거리장 → 그래디언트).
각 에이전트는 자기 셀의 벡터를 따라 흐르고 *지역 분리(separation)*만 얹는다. 쌍별 충돌검사 없이
다수가 부드럽게 합류·우회한다(대규모·공유 목적지에 유리).

**전역+지역 결합이 핵심**: 전역 경로(field 또는 A*)가 벽-쌓임을 막고, 지역 회피(RVO/separation)가
에이전트-에이전트 교착을 막는다. 하나만으론 부족하다.

## 길찾기 / 이동
- 연속공간 이동은 **steering**(seek/arrive + separation/alignment/cohesion) 또는 flow-field로.
- **한 줄 뭉침(single-file clumping) 금지**: 모두 같은 직선으로 몰려 벽처럼 쌓이면 실패 — 분산시켜라.

## 스폰 (연속 유입)
- **연속 스폰(steady inflow)**: 시간에 분산해 꾸준히 유입. **단발 버스트 금지**(한 틱에 떼로 쏟아 즉시 정체 만들지 마라).
- **입구 압력(backpressure)**: 입구 셀이 점유/혼잡이면 스폰을 미뤄라(겹쳐 스폰 → 즉시 충돌더미).

## 큐 / 서비스
- 서비스 지점(계산대·문·자원)에서 **줄(queue)이 형성**돼야 한다 — 무질서한 덩어리가 아니라.
- 패턴: **점유 슬롯**(서버당 1명) + **대기열(FIFO)** + **순차 진행**(슬롯 비면 다음 입장). 줄 길이를 trace로 노출.

## 로직-렌더 분리 (검증 가능성)
- **시뮬 엔진(상태·전이)을 canvas/DOM과 분리**하라. 엔진이 순수 데이터→데이터(좌표·속도·큐 상태)여야
  렌더 없이 **node에서 헤드리스로 구동·트레이스**된다(#84/#86 정합 — 실브라우저 E2E 불필요).
- 렌더는 엔진 상태를 그리는 *얇은 뷰*일 뿐. 엔진이 렌더에 의존하면 게이트(오프라인 clean-install)서 못 돈다.

## 헤드리스 트레이스 (run-judge가 행동을 판정할 수 있게)
- 트레이스는 **카운트만 찍지 마라.** tick별 **행동 상태**를 구조화 JSON으로 방출:
  에이전트 **좌표**(연속)·**목표 도달 분포**(얼마나·언제)·**큐 길이**(지점별)·혼잡/정체/충돌 지표.
- **자가채점 금지**: 엔진/하니스가 "자연스러움 OK" 같은 *판정*을 스스로 내리지 마라 — *원시 증거*만 내고
  판정은 독립 run-judge에 맡겨라(검증 독립성). "N개 처리됨" 카운트만으론 거친 동선이 가짜 done으로 덮인다.

## 원본성 (IP)
- **원본 고품질 작품**을 만들어라. 특정 상용 게임/IP를 클론하지 마라(에셋·고유 메커닉·이름 복제 금지).
