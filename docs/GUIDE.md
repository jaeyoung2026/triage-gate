# Guide

triage-gate 사용 가이드. 설치부터 다른 제품 이식까지.

설계·스키마·파이프라인 같은 내부 구조는 [SPEC.md](SPEC.md)를 본다.

## 요구사항

- Python 3.11+
- `OPENAI_API_KEY`
- 의존성: `pydantic>=2.0`, `openai>=1.40`, `streamlit>=1.40`, `pandas>=2.0`

## 설치

```bash
git clone https://github.com/jaeyoung2026/triage-gate.git
cd triage-gate

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

API 키 설정 (둘 중 하나):

```bash
echo "OPENAI_API_KEY=sk-..." > .env     # 권장 — gitignored, llm.py가 자동 로드
export OPENAI_API_KEY=sk-...              # 쉘 환경 변수, 있으면 .env보다 우선
```

## 사용법

### 1) 단건 triage

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

`source_kind`: `github_issue` / `email` / `chat` / `ocr` / `slack` / `unknown`.

출력:

```
── BR-100 ──
  issue_kind:         bug
  severity:           S0
  route:              human_engineer
  needs_human_review: True
  risk_flags:         ['outage', 'payment']
  agreement_score:    0.0
  severity upgrades:
    • critical path ['payment_checkout'] matched (severity already ≥ floor)
```

Trace 전체는 `traces/BR-100.json`에 저장된다 (Streamlit viz + evolve_agent 입력). Trace에는 LLM이 원문 언어로 쓴 2-3 문장 `narration`도 포함되어 대시보드에서 중심 표시된다. 예:

> 보고서는 결제 페이지에서 카드 정보를 입력하고 완료를 누르면 500 에러가 발생한다는 것입니다. 결제 프로세스가 차단되므로 이 문제는 심각하며 S0로 분류됩니다. 추가 정보 없이 기대 결과는 알 수 없습니다.

### 2) 배치 triage

```bash
.venv/bin/python -m triage_gate run-all data/reports
```

`data/reports/` 디렉토리의 모든 `*.json`을 돌린다. 샘플 10개가 포함되어 있다:

| id | source | 시나리오 |
|---|---|---|
| BR-001 | github_issue (ko) | 결제 500 — payment critical path |
| BR-002 | email | dark mode 요청 — feature_request |
| BR-003 | chat | 1000 rows — known_limitation 매칭 |
| BR-004 | chat | tooltip cosmetic — settings_save 오탐 |
| BR-005 | github_issue | auth session expiry — user_auth critical path |
| BR-006 | github_issue | CSV export 0바이트 — data_export critical path |
| BR-007 | slack (ko) | 팀 5명 로그인 전원 불가 — multi-user outage |
| BR-008 | email | cross-workspace 멤버 leak — security |
| BR-009 | chat (ko) | "안 돼요ㅠㅠ" — insufficient_info |
| BR-010 | email (ko) | 결제 후 영수증 미도착 — payment + support |

### 3) Streamlit 대시보드

```bash
.venv/bin/streamlit run viz/app.py
```

브라우저 `http://localhost:8501`.

두 가지 뷰:

- **single trace** — raw 원문, 히어로 판정 카드 (issue_kind/severity/route), **LLM 해설 (narration)**, 게이트 조정 메모, self_concerns 경고, 사람 검토 필요 여부, 위험 플래그/누락 정보 칩을 한 화면에서 본다. 4-차원 세부 분석·rationale·field provenance·conflicts·timings는 "자세히" 익스팬더 안쪽 (기본 접힘)
- **bucket overview** — 모든 trace를 route/issue_kind/severity 버킷으로 분포 표시. `auto_fix` 비율이 30%를 넘으면 경고. upgrade나 conflict가 있는 trace를 별도 섹션으로 모아서 evolve 학습 대상 강조

### 4) evolve — 진화 루프

```bash
.venv/bin/python -m triage_gate evolve
```

`traces/` 전체를 읽고 4가지 패턴 감지해서 markdown 리포트 출력:

- **critical_path 키워드 false positive** → `product_context.json` 튜닝
- **낮은 agreement_score** → `analyze.py` 프롬프트 보강 또는 `rules.py` 키워드 조정
- **self_concerns 반복 패턴** → 프롬프트의 해당 STEP 보강
- **rules ↔ LLM 플래그 불일치** → `rules.py` `RISK_KEYWORDS` 수정

각 제안은 건드릴 파일을 명시한다.

## 제품 컨텍스트 설정

`data/product_context.json`이 이 프레임워크를 "특정 제품에 맞는" triage 시스템으로 만드는 **단일 config**. 다른 제품에 이식할 때 편집할 것은 여기 하나다.

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
  "precedent_cases": []
}
```

### 주요 설정 항목

- **`critical_paths[].keywords`** — 원문 매칭 키워드. 너무 넓으면 false positive (evolve가 잡아낸다)
- **`critical_paths[].default_severity_floor`** — 이 경로 매칭 시 severity가 이 값 아래로 못 내려간다. `S0` / `S1` / `S2` / `S3` / `unknown`
- **`known_limitations`** — 자연어 문장. analyze가 읽고 매칭 리포트는 `support_question`으로 분류 (bug 아님)
- **`domain_glossary`** — 도메인 용어, analyze에 함께 주입

## 다른 제품에 적용하기

1. `data/product_context.json`을 새 제품에 맞춰 편집
   - `critical_paths`의 `name`/`keywords`/`default_severity_floor` 교체
   - `known_limitations` 목록 교체
   - `domain_glossary`에 도메인 용어 추가
2. 필요시 `triage_gate/rules.py`의 `RISK_KEYWORDS`에 제품별 도메인 키워드 추가
3. 첫 배치를 `run-all`로 돌림
4. `evolve`로 튜닝 제안 확인
5. 제안대로 `product_context.json`/`rules.py` 수정 → 재실행 → 결과 변화 관찰

**같은 코드베이스, config만 바꾸면 다른 제품의 triage로 작동**한다. 이게 설계의 핵심 가치다.
