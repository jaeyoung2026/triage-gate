# Spec

triage-gate 내부 구조 스펙. 파이프라인, 스키마, 프로젝트 구조.

사용법은 [GUIDE.md](GUIDE.md)를 본다.

## 설계 원칙

- **비정형 입력**: GitHub issue, 이메일, 채팅, OCR, 한국어 제보 — 아무 모양이나 받는다
- **LLM이 판단, 프로그램이 가드**: 한 LLM 호출이 모든 판단 차원을 한꺼번에 낸다. 그 뒤 프로그래매틱 gate가 키워드 rules와 `critical_path` 안전 플로어를 적용한다. severity는 **downgrade 불가 — LLM 값은 floor, rules·제품 컨텍스트가 ceiling이 되어 필요시 올린다**
- **제품 컨텍스트가 결정을 바꾼다**: 같은 코드, `product_context.json`만 바꾸면 다른 제품의 triage
- **자기 의심 → 사람 검토 신호**: LLM이 `self_concerns`를 함께 출력. 0개가 아니면 `needs_human_review=true`
- **합의 점수**: rules↔LLM 플래그 Jaccard − self_concern 패널티로 산출

## 파이프라인

```
RawReport (비정형 텍스트)
   │
   ├─► analyze  (LLM 1회, 구조화 출력)
   │     - 필드 추출 + field_sources (stated / inferred / missing)
   │     - preliminary_issue_kind (bug / feature_request / support_question / duplicate / insufficient_info)
   │     - 적대적 risk 스캔 (detected_risks)
   │     - severity_call + severity_rationale
   │     - info_sufficiency (high / medium / low)
   │     - self_concerns (모델의 자기 의심)
   │
   ├─► gate  (프로그래매틱, LLM 아님)
   │     - 비-bug fast path (feature_request → pm, support_question → support, ...)
   │     - severity 상향 1: 위험 플래그 floor (rules ∪ LLM danger flags)
   │     - severity 상향 2: critical_path severity floor (제품 컨텍스트)
   │     - route 매트릭스 (severity × danger × info_sufficiency)
   │     - 하드 invariant (S0/S1 → human_engineer, danger → auto_fix 금지)
   │     - agreement_score = Jaccard(LLM, rules) − self_concern 패널티
   │     - needs_human_review = self_concerns ∨ danger ∨ {S0/S1/unknown} ∨ agreement<0.6
   │
   ├─► TriagePacket (downstream 계약)
   │
   └─► Trace (전체 산출물 보존)
         │
         ├─► traces/<id>.json   (Streamlit viz + evolve 입력)
         └─► evolve              (rules / product_context diff 제안)
```

**LLM 호출은 1회**. 나머지는 전부 결정론적 함수다.

## route 결정 매트릭스

bug인 경우 (fast path 통과 후):

| severity | danger 플래그 | info_sufficiency | route |
|---|---|---|---|
| `unknown` | — | — | `needs_more_info` |
| `S0` | — | — | `human_engineer` |
| `S1` | — | — | `human_engineer` |
| `S2` | ✓ | — | `human_engineer` |
| `S2` | ✗ | `high` | `auto_fix` |
| `S2` | ✗ | `medium` | `human_engineer` |
| `S2` | ✗ | `low` | `needs_more_info` |
| `S3` | ✓ | — | `human_engineer` |
| `S3` | ✗ | `high` / `medium` | `auto_fix` |
| `S3` | ✗ | `low` | `needs_more_info` |

`auto_fix` 진입점은 **단 2~3칸**. 나머지는 전부 human 또는 more_info. 보수적 gate.

비-bug fast path:

| issue_kind | route |
|---|---|
| `feature_request` | `pm` |
| `support_question` | `support` |
| `duplicate` | `human_engineer` |
| `insufficient_info` | `needs_more_info` |

## 스키마

6개 pydantic 모델. 정의는 [`triage_gate/schema.py`](../triage_gate/schema.py).

1. **`RawReport`** — 비정형 입력. `raw_text`가 single source of truth
2. **`ProductContext`** — 제품 컨텍스트 (critical_paths, known_limitations, glossary, precedents)
3. **`Analysis`** — analyze 한 번의 LLM 호출 출력
   - extraction: `fields`, `field_sources`, `intake_notes`, `language`, `preliminary_issue_kind`
   - severity: `severity_call`, `severity_rationale`, `impact_summary`
   - risk: `detected_risks`, `risk_rationale`
   - completeness: `info_sufficiency`, `missing_fields`
   - self-doubt: `self_concerns`
4. **`TriagePacket`** — downstream 계약
   - `report_id`, `issue_kind`, `bug_confidence`, `severity`, `route`
   - `rationale`, `missing_fields`, `risk_flags`, `needs_human_review`
5. **`Trace`** — 하나의 triage run에 대한 전체 산출물
   - `raw`, `analysis`, `rule_flags_raw`, `severity_upgrades`, `conflicts`, `agreement_score`, `final_packet`, `timings_ms`
6. **`OutcomeRecord`** — downstream 피드백 (예약, 현재 미사용)

### `field_sources`의 3가지 status

analyze가 추출한 각 필드는 다음 중 하나:

- **`stated`**: 리포터가 원문에 명시
- **`inferred`**: analyze가 문맥으로 추론 (`quote` 필드에 근거 원문 포함)
- **`missing`**: 원문에 없음

`reproduction_steps`와 `expected_result`는 **절대 inferred 허용 안 함** — 환각 방지. 원문에 없으면 missing.

### 위험 플래그

```python
RiskFlag = Literal[
    "auth", "payment", "data_loss", "security", "outage",   # danger 플래그
    "backend_validation", "insufficient_repro", "unclear_scope",
]
```

앞 5개가 **danger**. danger는 auto_fix 금지 + severity floor S2.

## 파일 구조

```
triage-gate/
├── .env                            # OPENAI_API_KEY (gitignored)
├── requirements.txt
├── data/
│   ├── product_context.json        # 제품 컨텍스트
│   └── reports/                    # 입력 리포트 JSON
├── traces/                         # 출력 Trace JSON
├── docs/
│   ├── GUIDE.md                    # 사용 가이드
│   └── SPEC.md                     # 이 문서
├── triage_gate/
│   ├── __init__.py
│   ├── __main__.py                 # python -m triage_gate 진입점
│   ├── schema.py                   # 6 pydantic 모델
│   ├── llm.py                      # OpenAI client + .env 자동 로더
│   ├── rules.py                    # 키워드 기반 risk + critical path 매칭
│   ├── analyze.py                  # LLM 단일 호출 → Analysis
│   ├── gate.py                     # 프로그래매틱 안전 게이트 → TriagePacket
│   ├── evolve.py                   # 진화 루프 패턴 감지
│   └── cli.py                      # run / run-all / evolve 서브커맨드
└── viz/
    └── app.py                      # Streamlit 앱
```

9개 파일.

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | — | **필수** |
| `TRIAGE_ANALYZE_MODEL` | `gpt-4o` | analyze 호출 모델. 판단 작업이므로 기본값은 큰 모델 |

## 안전 invariant (양보 불가)

gate가 절대 어기지 않는 규칙:

1. 원문에 `payment`/`auth`/`data_loss`/`security`/`outage` 키워드가 있으면 → 해당 danger 플래그 반드시 발동 (rules.py, LLM 답과 무관)
2. 하나라도 danger 플래그가 있으면 → severity 최소 S2
3. critical_path이 매칭되면 → 해당 경로의 `default_severity_floor` 아래로 못 내려감
4. severity가 S0 또는 S1이면 → route 반드시 `human_engineer`
5. 하나라도 danger 플래그가 있으면 → route 절대 `auto_fix` 아님
6. LLM이 내놓은 severity는 **floor**. gate는 올릴 수만 있고 내릴 수 없음

이 6개는 결정론적 규칙이라 매번 동일 입력에 동일 출력을 보장한다.

## 알려진 이슈

- **진화 루프는 conflict 기반** — 실제 `OutcomeRecord`(사람이 라벨한 최종 결정)를 읽는 게 아니라, 같은 배치 안의 rules↔LLM 불일치와 self_concerns만 본다
- **precedent retrieval 없음** — `precedent_cases`는 현재 프롬프트에 주입되지 않음
- **중복 탐지 없음** — `duplicate` issue_kind는 정의만 있고 자동 탐지 로직 없음
- **라우팅만 하고 실제 수정 안 함** — `auto_fix`는 "자동으로 고쳐도 된다"는 판정일 뿐. 실제 수정/PR 생성은 별도 시스템
- **analyze 지연** — `gpt-4o`로 평균 5초/건
