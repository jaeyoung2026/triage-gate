# triage-gate

버그 리포트 triage 게이트. 비정형 리포트를 한 번의 LLM 호출로 5차원(추출 / issue_kind / severity / risk / completeness)으로 분석하고, 제품 컨텍스트와 키워드 규칙으로 구성된 **프로그래매틱 안전 게이트**를 통과시켜 **TriagePacket** (issue_kind / severity / route)을 내놓는다.

단순한 "버그 분류기"가 아니라, downstream 시스템이 그대로 사용할 수 있는 **triage packet 생성기**다. 라벨 하나가 아니라 판정 근거·위험 플래그·완전성·severity 상향 내역·self_concerns까지 포함한 구조화 출력을 생성한다.

## 설계 원칙

- **비정형 입력**: GitHub issue, 이메일, 채팅, OCR, 한두 줄 한국어 제보 — 아무 모양이나 받는다
- **LLM이 판단, 프로그램이 가드**: 한 LLM 호출이 모든 판단 차원을 한꺼번에 낸다. 그 뒤 프로그래매틱 gate가 안전 플로어(키워드 rules + critical_path)를 적용한다. severity는 **절대 downgrade 안 됨 — LLM이 내놓은 값은 floor, 규칙·제품 컨텍스트가 ceiling이 되어 필요시 올린다**
- **제품 컨텍스트가 결정을 바꾼다**: 같은 LLM, 같은 프레임워크라도 `product_context.json`의 `critical_paths`·`known_limitations`만 바꾸면 다른 제품의 triage로 작동
- **자기 의심 → 사람 검토 신호**: LLM이 자기 답변에 대한 `self_concerns`를 함께 출력하고, 이것이 0개가 아니면 `needs_human_review=true`. 합의 점수는 rules↔LLM 불일치에서 파생
- **evolve 루프**: `evolve_agent`가 trace를 읽고 config/rules diff를 제안. 자동 적용 금지, 사람이 승인

## 파이프라인

```
RawReport (비정형 텍스트)
   │
   ├─► analyze  (LLM 1회 호출, 구조화 출력)
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
   │     - 하드 invariant (S0/S1 → human_engineer 강제, danger → auto_fix 금지)
   │     - agreement_score = Jaccard(LLM, rules) − self_concern 패널티
   │     - needs_human_review = self_concerns ∨ danger ∨ {S0/S1/unknown} ∨ agreement<0.6
   │
   ├─► TriagePacket (downstream 계약)
   │
   └─► Trace (전체 산출물 보존)
         │
         ├─► traces/<id>.json   (Streamlit viz + evolve_agent 입력)
         └─► evolve_agent       (rules / product_context diff 제안)
```

**LLM 호출은 1회**. 나머지는 전부 결정론적 함수다. 이것이 이 버전(simplified)의 핵심 특징.

## 요구사항

- Python 3.11+
- `OPENAI_API_KEY` 환경 변수 또는 `.env` 파일
- 의존성: `pydantic>=2.0`, `openai>=1.40`, `streamlit>=1.40`, `pandas>=2.0`

## 설치

```bash
git clone https://github.com/jaeyoung2026/triage-gate.git
cd triage-gate

# 가상환경
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# API 키 설정 (둘 중 하나)
echo "OPENAI_API_KEY=sk-..." > .env     # 방법 A: .env 파일 (권장, gitignored)
export OPENAI_API_KEY=sk-...             # 방법 B: 쉘 환경 변수
```

`.env` 파일은 `llm.py`가 자동으로 로드한다. 쉘 환경 변수가 있으면 그게 우선한다.

## 사용법

### 1) 단건 triage

리포트를 JSON으로 저장하고 CLI로 실행한다.

```bash
cat > data/reports/BR-100.json <<'EOF'
{
  "report_id": "BR-100",
  "source_kind": "github_issue",
  "raw_text": "결제 페이지에서 카드 입력하고 완료 누르면 500 에러 떠요. 매번 그래요."
}
EOF

.venv/bin/python -m triage_gate run data/reports/BR-100.json
```

`source_kind`는 다음 중 하나: `github_issue`, `email`, `chat`, `ocr`, `slack`, `unknown`.

출력 예시:

```
── BR-100 ──
  issue_kind:         bug
  severity:           S0
  route:              human_engineer
  needs_human_review: True
  bug_confidence:     0.0
  risk_flags:         ['outage', 'payment']
  missing_fields:     ['title', 'expected_result', 'stack_trace']
  agreement_score:    0.0
  severity upgrades:
    • critical path ['payment_checkout'] matched (severity already ≥ floor)
  conflicts:
    • LLM raised flags rules did not: ['outage']
    • Rules raised flags LLM did not: ['payment']
    • self_concern: severity may be too low — impact scope unclear from raw text
  timings:
    analyze_ms: 6319 ms
    gate_ms: 0 ms
```

전체 Trace는 `traces/BR-100.json`에 저장된다 (Streamlit viz와 evolve_agent의 입력).

### 2) 배치 triage

```bash
.venv/bin/python -m triage_gate run-all data/reports
```

10개 샘플 리포트가 포함되어 있다:

| id | source | 기대 결과 |
|---|---|---|
| BR-001 | github_issue (ko) | `bug / S0 / human_engineer` (payment critical path) |
| BR-002 | email | `feature_request / unknown / pm` (dark mode 요청) |
| BR-003 | chat | `support_question / unknown / support` (known_limitation 매칭) |
| BR-004 | chat | `bug / S2 / needs_more_info` (tooltip, settings_save 오탐) |
| BR-005 | github_issue | `bug / S1 / human_engineer` (auth session, user_auth critical path) |
| BR-006 | github_issue | `bug / S1 / human_engineer` (CSV export 0바이트, data_export floor) |
| BR-007 | slack (ko) | `bug / S1 / human_engineer` (팀 5명 로그인 불가, user_auth floor) |
| BR-008 | email | `bug / S0 / human_engineer` (cross-workspace 멤버 leak, security) |
| BR-009 | chat (ko) | `insufficient_info / unknown / needs_more_info` ("안 돼요ㅠㅠ") |
| BR-010 | email (ko) | `support_question / unknown / support` (결제 후 영수증 미도착) |

### 3) Streamlit 시각화

```bash
.venv/bin/streamlit run viz/app.py
```

브라우저에서 `http://localhost:8501` 열기.

**두 가지 뷰**:

- **single trace**: raw 원문 → intake 추출 (field_sources 포함) → analyze의 4-차원 출력(severity/risk/completeness/self_concerns) → gate의 severity 상향 내역 → 최종 packet 까지를 한 화면에서 본다. `agreement_score` 게이지, severity upgrade 칩, conflict 칩, self_concerns 경고가 왜 이 route가 선택됐는지 역추적 가능하게 한다
- **bucket overview**: 모든 trace를 route/issue_kind/severity 버킷으로 분포 표시. `auto_fix` 비율이 30%를 넘으면 경고. severity upgrade나 conflict가 있는 trace를 별도 섹션으로 모아서 evolve_agent가 학습할 패턴을 강조

### 4) evolve_agent — 진화 루프

```bash
.venv/bin/python -m triage_gate evolve
```

`traces/` 전체를 읽고 4가지 패턴을 감지해서 markdown 리포트를 출력한다:

- **critical_path 키워드 false positive**: LLM이 `severity_call=S3`이라고 부르고 danger flag도 없는데 critical_path floor가 발동한 케이스 → `product_context.json` 키워드 좁히라고 제안
- **낮은 agreement_score**: `agreement_score < 0.7`인 케이스 → analyze.py 프롬프트 재검토 또는 rules.py 키워드 조정 제안
- **self_concerns 표면화**: 모델이 스스로 의심한 항목들 → 반복 패턴이면 프롬프트의 해당 STEP 보강 제안
- **rules ↔ LLM 불일치**: 키워드 규칙과 LLM의 `detected_risks`가 다른 경우 → `rules.py` `RISK_KEYWORDS` 확장/수축 제안

## 제품 컨텍스트 설정

`data/product_context.json`이 이 프레임워크를 "특정 제품에 맞는" triage 시스템으로 만드는 **단일 config 파일**이다. 다른 제품에 이식할 때 편집할 것은 여기 하나뿐.

```json
{
  "product_name": "Mirror",
  "version": "2026.04.14-seed",
  "scope_summary": "무엇을 제공하고 무엇은 안 하는가",
  "critical_paths": [
    {
      "name": "payment_checkout",
      "keywords": ["checkout", "payment", "billing", "결제"],
      "description": "Subscription checkout and invoice flow.",
      "default_severity_floor": "S0",
      "default_risk_flags": ["payment"]
    }
  ],
  "known_limitations": [
    "Free plan is limited to 1000 rows per dashboard. This is by design, not a bug."
  ],
  "domain_glossary": {
    "workspace": "Top-level tenant container"
  },
  "precedent_cases": [...]
}
```

**이 JSON만 수정하면 같은 코드베이스가 다른 제품의 triage로 작동**한다. 이게 설계의 핵심 가치다.

### 주요 설정 항목

- **`critical_paths[].keywords`** — 원문(raw_text)과 매칭되는 키워드. 너무 넓으면 false positive (evolve_agent가 잡아낸다)
- **`critical_paths[].default_severity_floor`** — 이 경로가 매칭되면 severity가 이 값 아래로 못 내려간다. `S0`, `S1`, `S2`, `S3`, `unknown` 중 하나
- **`known_limitations`** — 자연어 문장. analyze가 읽고 매칭되는 리포트는 `support_question`으로 분류 (bug 아님)

## 프로젝트 구조

```
triage-gate/
├── .env                            # OPENAI_API_KEY (gitignored)
├── .gitignore
├── requirements.txt
├── README.md
├── data/
│   ├── product_context.json        # 제품 컨텍스트 (수정 대상)
│   └── reports/                    # 입력 리포트 JSON
│       └── BR-*.json
├── traces/                         # 출력 Trace JSON (각 run마다 1개)
│   └── BR-*.json
├── triage_gate/
│   ├── __init__.py
│   ├── __main__.py                 # python -m triage_gate ... 진입점
│   ├── schema.py                   # 6 pydantic 모델 (팀 contract)
│   ├── llm.py                      # OpenAI client + .env 자동 로더 + ANALYZE_MODEL
│   ├── rules.py                    # 키워드 기반 risk flag + critical path 매칭
│   ├── analyze.py                  # LLM 단일 호출 — raw → Analysis (5차원)
│   ├── gate.py                     # 프로그래매틱 안전 게이트 → TriagePacket
│   ├── evolve.py                   # 진화 루프 v1 (패턴 감지)
│   └── cli.py                      # run / run-all / evolve 서브커맨드
└── viz/
    └── app.py                      # Streamlit 앱
```

총 **9 파일** (simplification 이전 14 파일에서 축소: intake, 3 specialists, synthesize, decide 삭제 → analyze, gate 추가).

## 스키마 (6 pydantic 모델)

1. **`RawReport`** — 비정형 입력. `raw_text`가 single source of truth
2. **`ProductContext`** — 제품 컨텍스트 (critical_paths, known_limitations, glossary, precedents)
3. **`Analysis`** — **analyze() 한 번의 LLM 호출 출력**. extraction + severity + risk + completeness + self_concerns 모두 포함
4. **`TriagePacket`** — downstream에 넘기는 최종 계약 (issue_kind, severity, route, risk_flags, rationale, needs_human_review)
5. **`Trace`** — 하나의 리포트에 대한 모든 중간 산출물 (raw, analysis, rule_flags_raw, severity_upgrades, conflicts, agreement_score, final_packet, timings)
6. **`OutcomeRecord`** — downstream 피드백 (v1은 사용 안 함, v2에서 사람 라벨 학습에 사용 예정)

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | — | **필수**. OpenAI API 키 |
| `TRIAGE_ANALYZE_MODEL` | `gpt-4o` | analyze LLM 호출 모델. 판단 작업이므로 기본값은 큰 모델 |

## 다른 제품에 적용하기

1. `data/product_context.json`을 새 제품에 맞춰 편집
   - `critical_paths`의 `name`/`keywords`/`default_severity_floor` 바꾸기
   - `known_limitations`를 해당 제품의 "알려진 제약" 목록으로 교체
   - `domain_glossary`에 도메인 용어 추가
2. 필요시 `triage_gate/rules.py`의 `RISK_KEYWORDS`에 제품별 도메인 키워드 추가
3. 첫 배치를 `run-all`로 돌린 뒤 `evolve`로 튜닝 제안 확인
4. 제안대로 `product_context.json`/`rules.py` 수정 → 다시 실행 → 결과 변화 관찰

## 아키텍처 결정 기록

### Why 1 LLM call instead of multi-agent?

이전 버전은 4번의 LLM 호출(intake + severity_agent + risk_agent + completeness_agent)과 synthesize 매트릭스(7 layer) + decide 하드 오버라이드로 구성되어 있었다. 이유는 "좁은 프롬프트 × 다각도 검증 × 불일치가 신호"였다.

**simplification v1에서 이것을 거절했다**. 이유:
- 4 LLM 호출은 product_context가 3회 중복 로드되어 토큰 낭비 + 3 RTT 오버헤드
- 3 specialist 사이의 "합의"는 실제로 대부분 동일 답변 — 견제가 이론만큼 강하지 않았다
- completeness_agent가 하는 일은 `field_sources`에 의한 결정론적 계산이라 LLM이 필요 없었다 (실제로 지금은 프롬프트 안에서 같은 규칙을 적용하게 지시)
- 7-layer 매트릭스는 머리에 안 들어오는 복잡성이고 synthesize/decide 분리는 "defense in depth" 라는 이론적 명분은 있었지만 실제 데이터에서 decide의 하드 오버라이드가 거의 발동하지 않아 중복 코드였음

**단 하나의 LLM 호출이 대안**: 한 프롬프트 안에서 모델에게 "STEP 1 추출 → STEP 2 issue_kind → STEP 3 adversarial risk 스캔 → STEP 4 severity → STEP 5 completeness → STEP 6 self_concerns" 순서로 시키면, 좁은 프롬프트의 이점 대부분을 유지하면서 호출 수만 1/4로 줄어든다. 대신 최종 안전 보장은 프로그래매틱 gate가 맡는다.

### Why programmatic gate instead of all-LLM?

"LLM이 다 하면 gate도 LLM이 하면 되지 않나?"에 대한 답:

- **안전 invariant는 결정론적이어야 한다**. "`payment` 키워드가 raw에 있으면 반드시 payment 플래그", "S0/S1은 반드시 human_engineer", "danger flag는 auto_fix 금지" 같은 규칙은 LLM의 기분에 맡길 일이 아니다.
- **제품 사실은 코드 레벨 계약**. `critical_paths[].default_severity_floor: S0`는 "이 경로는 최소 S0이다"라는 제품의 선언이다. LLM이 이걸 무시하면 안 된다.
- **재현 가능성**. 같은 입력 → 같은 출력이어야 디버깅과 evolve가 가능하다. gate는 순수 함수.
- **evolve 학습 신호**. rules vs LLM의 불일치가 `conflicts`에 기록되어 evolve_agent의 학습 신호가 된다. 둘 다 LLM이면 이 신호가 사라진다.

### Why severity can only go UP in gate?

gate의 severity 조정 규칙은 **단방향 floor만 적용**. LLM이 내놓은 severity는 floor로 취급하고, rules/critical_path는 ceiling으로 작동해서 필요시 더 올린다.

- 내리는 방향 조정은 위험하다. LLM이 S0이라고 판단한 걸 "rules는 payment 키워드 없으니 S2로 내려"라고 하면 실제 위험을 놓친다.
- 올리는 방향은 안전하다. "rules는 payment 있으니 S3도 최소 S2로", "critical_path payment는 floor S0" 같은 것들은 안전 쪽으로 편향된다.

**"rules는 이름 붙일 수 있는 위험을 잡고 LLM은 이름 붙일 수 없는 맥락을 잡는다"**는 하이브리드 원칙은 유지됐다. 다만 그 결합 방식이 "synthesizer가 재판정"에서 "LLM이 판정 + 프로그래매틱 floor"로 더 단순해졌다.

## v1 한계 / 알려진 이슈

- **진화 루프 v1은 conflict 기반**: 실제 `OutcomeRecord`(사람이 라벨한 최종 결정)를 읽는 게 아니라, 같은 배치 안의 rules↔LLM 불일치와 self_concerns만 본다. v2에서 outcome_log 도입 예정
- **precedent retrieval 없음**: `precedent_cases`는 현재 프롬프트에 주입되지 않음. v2에서 임베딩 기반 retrieval로 유사 케이스 few-shot 주입 예정
- **중복 탐지 없음**: `duplicate` issue_kind는 정의만 있고 자동 탐지 로직 없음
- **라우팅만 하고 실제 수정 안 함**: `auto_fix` 라우팅은 "이건 자동으로 고쳐도 된다"는 **판정**일 뿐. 실제 코드 수정/PR 생성은 별도 시스템의 일
- **analyze 지연**: `gpt-4o`로 평균 5초/건. 작은 모델로 바꾸면 더 빠르지만 판단 품질 trade-off 검증 필요

## 라이선스

MIT (또는 미정)

## 크레딧

2026년 4월 13~14일 codex 해커톤 준비 세션에서 설계 및 구현.
- v0: multi-agent (4 LLM calls, 14 files)
- v1: simplified (1 LLM call, 9 files) — 같은 안전 보장을 유지하면서 복잡도 40% 감소

협력 원칙은 [mirror-mind](https://github.com/jaeyoung2026/mirror-mind)의 `AGENTS.md`에서 상속.
