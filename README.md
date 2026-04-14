# triage-gate

멀티 에이전트 버그 리포트 triage 게이트. 비정형 리포트를 읽고, 3명의 specialist가 병렬로 판단한 뒤, 제품 컨텍스트 기반 severity floor를 적용해서 **TriagePacket** (issue_kind / severity / route)을 내놓는다.

단순한 "버그 분류기"가 아니라, downstream 시스템이 그대로 사용할 수 있는 **triage packet 생성기**다. 라벨 하나가 아니라 판정 근거·위험 플래그·완전성·합의 점수·하드 오버라이드 내역까지 포함한 구조화 출력을 생성한다.

## 설계 원칙

- **비정형 입력**: GitHub issue, 이메일, 채팅, OCR, 한두 줄 한국어 제보 — 아무 모양이나 받는다
- **다각도 검증**: severity / risk / completeness 3명의 specialist가 각각 좁은 프롬프트로 **병렬** 판단. 불일치 자체가 신호 → `agreement_score` 하락 + `needs_human_review=true`
- **프로그래매틱 + LLM 하이브리드**: 규칙(rules.py)은 이름 붙일 수 있는 위험을 잡고, LLM은 이름 붙일 수 없는 맥락을 잡는다. 규칙이 **robust floor** 역할
- **source-role 분할**: 각 specialist가 제품 컨텍스트의 **필요한 부분만** 본다. 토큰 예산 억제 + 해석 충돌 방지
- **product context 기반 진화**: `critical_paths`·`known_limitations`를 config로 분리. 같은 프레임워크라도 제품 설정만 바꾸면 분류 결과가 달라진다
- **진화 루프**: `evolve_agent`가 trace를 읽고 config/rules diff를 제안. 자동 적용 금지, 사람이 승인

## 파이프라인 전체 흐름

```
RawReport (비정형 텍스트)
   │
   ├─► rules_on_raw  (프로그래매틱: 키워드 → risk 플래그, 결정론적 floor)
   │
   ├─► intake_agent  (LLM small)
   │     │  - 필드 추출 + field_sources (stated / inferred / missing)
   │     │  - preliminary_issue_kind (bug / feature_request / support_question / duplicate / insufficient_info)
   │     │  - ProductContext: scope + known_limitations + glossary
   │     ▼
   │   ExtractedReport
   │
   ├─► 3 specialists 병렬 실행 (LLM)
   │     ├─ severity_agent     ← critical_paths + scope
   │     ├─ risk_agent         ← critical_paths(risk_flags) + known_limitations (adversarial)
   │     └─ completeness_agent ← scope_summary만
   │
   ├─► synthesize  (프로그래매틱, LLM 아님)
   │     │  - Layer A  : 비-bug fast path (feature_request → pm 등)
   │     │  - Layer B  : severity (specialist 판단)
   │     │  - Layer B' : 위험 플래그 → severity upgrade
   │     │  - Layer B'': critical_path severity floor (product_context가 결정 바꾸는 지점)
   │     │  - Layer C  : route 매트릭스 (severity × danger × info_sufficiency)
   │     │  - Layer D  : agreement_score (3 specialist 일치도)
   │     │  - Layer E  : conflict 리스트
   │     ▼
   │   SynthesizerDecision
   │
   ├─► decide  (프로그래매틱 하드 오버라이드, defense in depth)
   │     │  - S0/S1 → human_engineer 강제
   │     │  - danger flag → auto_fix 금지
   │     │  - needs_human_review 결정
   │     ▼
   │   TriagePacket  (downstream 계약)
   │
   └─► Trace (모든 중간 산출물 보존)
         │
         ├─► traces/<id>.json  (Streamlit viz + evolve_agent 입력)
         └─► evolve_agent → rules/product_context diff 제안
```

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
  bug_confidence:     0.72
  risk_flags:         ['payment', 'outage']
  missing_fields:     ['reproduction_steps', 'expected_result']
  agreement_score:    0.8
  timings:
    intake_ms: 6646 ms
    specialists_parallel_ms: 2273 ms
    synth_decide_ms: 0 ms
```

전체 Trace는 `traces/BR-100.json`에 저장된다 (Streamlit viz와 evolve_agent의 입력).

### 2) 배치 triage

`data/reports/` 디렉토리의 모든 `*.json`을 돌린다.

```bash
.venv/bin/python -m triage_gate run-all data/reports
```

5개 샘플 리포트가 포함되어 있다:

| id | source | 기대 결과 |
|---|---|---|
| BR-001 | github_issue (ko) | `bug / S0 / human_engineer` (payment critical path) |
| BR-002 | email | `feature_request / unknown / pm` (dark mode 요청) |
| BR-003 | chat | `support_question / unknown / support` (known_limitation 매칭) |
| BR-004 | chat | `bug / S2 / needs_more_info` (tooltip, settings_save 오탐) |
| BR-005 | github_issue | `bug / S1 / human_engineer` (auth session, user_auth critical path) |

### 3) Streamlit 시각화

```bash
.venv/bin/streamlit run viz/app.py
```

브라우저에서 `http://localhost:8501` 열기.

**두 가지 뷰**:

- **single trace**: 하나의 리포트에 대해 raw 원문 → intake 추출 (field_sources 포함) → 3-specialist 병렬 판단 → synthesizer 결정 trail → 최종 packet 까지를 한 화면에서 본다. `agreement_score` 게이지, `conflicts` 칩, decision trail이 왜 이 route가 선택됐는지 역추적 가능하게 한다.
- **bucket overview**: 모든 trace를 route/issue_kind/severity 버킷으로 분포 표시. `auto_fix` 비율이 30%를 넘으면 경고 (gate가 너무 느슨). conflict가 있는 trace를 별도 섹션으로 모아서 evolve_agent가 학습할 패턴을 강조.

### 4) evolve_agent — 진화 루프

```bash
.venv/bin/python -m triage_gate evolve
```

`traces/` 전체를 읽고 4가지 패턴을 감지해서 markdown 리포트를 출력한다:

- **critical_path 키워드 false positive**: severity_agent가 S3이라고 부르고 danger flag도 없는데 critical_path floor가 발동한 케이스 → `product_context.json` 키워드 좁히라고 제안
- **낮은 specialist 합의**: `agreement_score < 0.7`인 케이스 → specialist 프롬프트 재검토 제안
- **하드 오버라이드 활동**: `decide.py` 오버라이드가 자주 발동하면 → synthesize 매트릭스로 승격 제안
- **rules ↔ LLM 불일치**: 키워드 규칙과 risk_agent의 flag가 다른 경우 → `rules.py` `RISK_KEYWORDS` 확장/수축 제안

실제 작동 예시 (5 trace 분석 결과):

```
### ⚠ critical_path keyword false-positive candidates (1)
- **BR-004**: critical path ['settings_save'] forces floor S2 (was S3)
**Target file**: data/product_context.json — narrow the `keywords` list

### rules ↔ LLM risk-flag disagreement (2)
- **BR-001**: LLM-only=['outage'], rules-only=∅
- **BR-005**: LLM-only=['data_loss'], rules-only=∅
**Target**: triage_gate/rules.py RISK_KEYWORDS dict
```

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

### source-role 분할 (누가 무엇을 보는가)

| 소비자 | 보는 필드 | 보지 않는 필드 | 이유 |
|---|---|---|---|
| intake_agent | scope_summary, known_limitations, domain_glossary | critical_paths, precedent | 경계/용어/제외항목 알아야 feature_request·support 분별 |
| severity_agent | critical_paths, scope_summary | glossary, precedent, limitations | 핵심 경로 floor만 있으면 된다 |
| risk_agent | critical_paths(risk_flags), known_limitations | scope, glossary | 위험 플래그만 보면 된다 |
| completeness_agent | scope_summary (짧게) | 나머지 전부 | 완전성 판정에 product 맥락은 거의 불필요 |
| evolve_agent | **전부** + rules.py + trace 전체 | — | 오직 여기만 전면 조망 |

토큰 예산이 specialist별로 분리되어 있어서 전체 프롬프트 크기가 선형 증가가 아니라 1.3~1.5배 수준에 머문다.

### 주요 설정 항목

- **`critical_paths[].keywords`** — 원문(raw_text)과 매칭되는 키워드. 너무 넓으면 false positive (evolve_agent가 잡아낸다)
- **`critical_paths[].default_severity_floor`** — 이 경로가 매칭되면 severity가 이 값 아래로 못 내려간다. `S0`, `S1`, `S2`, `S3`, `unknown` 중 하나
- **`known_limitations`** — 자연어 문장. intake_agent가 읽고 매칭되는 리포트는 `support_question`으로 분류 (bug 아님)

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
│   ├── schema.py                   # 7 pydantic 모델 (팀 contract)
│   ├── llm.py                      # OpenAI client + 모델 선택 + .env 로더
│   ├── rules.py                    # 키워드 기반 risk flag + critical path 매칭
│   ├── intake.py                   # LLM 레이어 #1 — 비정형 → ExtractedReport
│   ├── specialists/
│   │   ├── __init__.py
│   │   ├── severity.py             # LLM #2 — 영향 크기 판단
│   │   ├── risk.py                 # LLM #3 — adversarial 위험 스캔
│   │   └── completeness.py         # LLM #4 — 정보 충분성 판정
│   ├── synthesize.py               # 프로그래매틱 결정 매트릭스
│   ├── decide.py                   # 프로그래매틱 하드 오버라이드
│   ├── evolve.py                   # 진화 루프 v1 (패턴 감지)
│   └── cli.py                      # run / run-all / evolve 서브커맨드
└── viz/
    └── app.py                      # Streamlit 앱
```

## 스키마 (7 pydantic 모델)

1. **`RawReport`** — 비정형 입력. `raw_text`가 single source of truth
2. **`ExtractedReport`** — intake_agent 출력. 필드 + `field_sources` (stated/inferred/missing) + raw 원문 보존
3. **`ProductContext`** — 제품 컨텍스트 (critical_paths, known_limitations, glossary, precedents)
4. **`SpecialistOpinion`** — discriminated union of `SeverityOpinion` / `RiskOpinion` / `CompletenessOpinion`
5. **`TriagePacket`** — downstream에 넘기는 최종 계약 (issue_kind, severity, route, risk_flags, rationale, needs_human_review)
6. **`Trace`** — 하나의 리포트에 대한 모든 중간 산출물 (raw, extracted, specialist_opinions, synthesizer_decision, overrides, final_packet, timings)
7. **`OutcomeRecord`** — downstream 피드백 (나중에 evolve_agent가 실제 label로 학습할 때 사용; 현재 v1은 conflict 기반)

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | — | **필수**. OpenAI API 키 |
| `TRIAGE_INTAKE_MODEL` | `gpt-4o-mini` | intake_agent 모델 (추출만, 작은 모델로 충분) |
| `TRIAGE_SEVERITY_MODEL` | `gpt-4o` | severity_agent 모델 (판단, 큰 모델) |
| `TRIAGE_RISK_MODEL` | `gpt-4o` | risk_agent 모델 (adversarial, 큰 모델) |
| `TRIAGE_COMPLETENESS_MODEL` | `gpt-4o-mini` | completeness_agent 모델 (구조적, 작은 모델) |

## 다른 제품에 적용하기

1. `data/product_context.json`을 새 제품에 맞춰 편집
   - `critical_paths`의 `name`/`keywords`/`default_severity_floor` 바꾸기
   - `known_limitations`를 해당 제품의 "알려진 제약" 목록으로 교체
   - `domain_glossary`에 도메인 용어 추가
2. 필요시 `triage_gate/rules.py`의 `RISK_KEYWORDS`에 제품별 도메인 키워드 추가
3. 첫 배치를 `run-all`로 돌린 뒤 `evolve`로 튜닝 제안 확인
4. 제안대로 `product_context.json`/`rules.py` 수정 → 다시 실행 → 결과 변화 관찰

**같은 코드베이스, config만 바꾸면 다른 제품의 triage로 작동**한다. 이게 이 설계의 핵심 가치.

## 아키텍처 결정 기록

- **왜 multi-agent인가**: 한 모델에게 "severity + risk + completeness 모두 판단"시키면 한 필드 오류가 다른 필드에 전파된다. 좁은 프롬프트로 분리하면 정확도가 올라가고, 불일치가 검출 가능해진다
- **왜 synthesizer가 LLM 아닌가**: 결정 매트릭스는 재현 가능해야 한다. 같은 입력 → 같은 출력이어야 디버깅/진화가 가능. LLM이 매트릭스까지 하면 설명 가능성 상실
- **왜 decide 레이어가 분리되어 있는가**: defense in depth. synthesizer 매트릭스에 버그가 있어도 `S0 → human_engineer`, `danger flag → 절대 auto_fix 금지` 같은 **비양보 원칙**은 decide가 강제
- **왜 `FieldSourceMap`이 `dict[str, FieldSource]` 아닌가**: OpenAI structured output의 strict mode는 dict의 임의 키를 허용하지 않음. 타입된 필드로 명시해야 한다
- **왜 Streamlit인가**: 해커톤 3시간 시간 박스에서 3-컬럼 레이아웃 + 칩 + 데이터프레임 + 게이지를 모두 구현할 수 있는 유일한 선택지. 의사결정을 **바꾸는** viz에 집중 — 버킷 분포가 이상하면 바로 튜닝, conflict 칩이 빨갛게 뜨면 evolve_agent 실행

## v1 한계 / 알려진 이슈

- **진화 루프 v1은 conflict 기반**: 실제 `OutcomeRecord`(사람이 라벨한 최종 결정)를 읽는 게 아니라, 같은 배치 안의 specialist 불일치만 본다. v2에서 outcome_log 도입 예정
- **precedent retrieval 없음**: `precedent_cases`는 현재 프롬프트에 주입되지 않음. v2에서 임베딩 기반 retrieval로 유사 케이스 few-shot 주입 예정
- **중복 탐지 없음**: `duplicate` issue_kind는 정의만 있고 자동 탐지 로직 없음. 지금은 intake_agent가 강하게 편향 금지 원칙에 따라 거의 안 씀
- **라우팅만 하고 실제 수정 안 함**: `auto_fix` 라우팅은 "이건 자동으로 고쳐도 된다"는 **판정**일 뿐. 실제 코드 수정/PR 생성은 별도 시스템의 일 (해커톤에서는 팀의 Fix/PR 담당자 몫)
- **intake 지연**: `gpt-4o-mini`로도 평균 6초/건. 해커톤 50건/일 스케일에선 문제없지만 프로덕션 스케일에선 병목

## 라이선스

MIT (또는 미정)

## 크레딧

2026년 4월 13~14일 codex 해커톤 준비 세션에서 설계 및 구현. 멀티 에이전트 협력 원칙은 [mirror-mind](https://github.com/jaeyoung2026/mirror-mind)의 `AGENTS.md`에서 상속.
