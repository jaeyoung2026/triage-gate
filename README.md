# triage-gate

비정형 버그 리포트를 한 번의 LLM 호출로 분석하고, 제품 컨텍스트와 키워드 규칙으로 구성된 프로그래매틱 안전 게이트를 통과시켜 **TriagePacket** (issue_kind / severity / route)을 내놓는 버그 triage 게이트.

같은 코드베이스에서 `data/product_context.json`의 `critical_paths`와 `known_limitations`만 바꾸면 다른 제품의 triage로 작동한다.

## 빠른 시작

```bash
git clone https://github.com/jaeyoung2026/triage-gate.git
cd triage-gate

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
echo "OPENAI_API_KEY=sk-..." > .env

# 샘플 10건 실행
.venv/bin/python -m triage_gate run-all data/reports

# 대시보드
.venv/bin/streamlit run viz/app.py
```

## 문서

- **[docs/GUIDE.md](docs/GUIDE.md)** — 설치, CLI 사용법(run / run-all / streamlit / evolve), 제품 컨텍스트 편집, 다른 제품에 이식하기
- **[docs/SPEC.md](docs/SPEC.md)** — 설계 원칙, 파이프라인 다이어그램, route 결정 매트릭스, 6 pydantic 스키마, 파일 구조, 안전 invariant, 알려진 이슈

## 라이선스

MIT (또는 미정)
