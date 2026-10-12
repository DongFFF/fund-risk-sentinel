# fund-risk-sentinel

**English** | [简体中文](README.zh-CN.md)

> **Research Fund Risk Sentinel** — Desktop risk identification for university research funding
> 高校科研经费风险智能识别系统（桌面端）

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey)
![Tests](https://img.shields.io/badge/checks-210%20passing-brightgreen)
![Data](https://img.shields.io/badge/data-stays%20local-orange)

A desktop application for **pre-emptive warning and in-process interception** of risk in university research funding. Upload unstructured materials — reimbursement claims, contracts, invoices, approval notes — and a **freely configurable LLM** reviews them through an *Event → Rule → Evidence → Risk → Disposition* framework, producing **verifiable risk leads** (severity, matched basis, related materials, suggested review points) instead of making compliance rulings on anyone's behalf.

Ships as a single-file Windows `exe`. Model APIs are configured, never hard-coded — **no vendor lock-in**.

---

## Table of contents

- [1. What problem does this solve](#1-what-problem-does-this-solve)
- [2. Capability framework](#2-capability-framework)
- [3. Quick start](#3-quick-start)
- [4. Project layout](#4-project-layout)
- [5. Data and security](#5-data-and-security)
- [6. Development status](#6-development-status)
- [7. Documentation](#7-documentation)
- [8. License](#8-license)

---

## 1. What problem does this solve

University research funding management has long relied on **post-hoc audit plus manual sampling**. Three pain points follow:

| Pain point | How it shows up | How this system responds |
| --- | --- | --- |
| **Late discovery** | Violations surface only at project-closing audit | Moves review upstream of reimbursement, contract and budget checkpoints |
| **Fragmented rules** | National measures, negative lists and in-house rules disagree | A policy knowledge base plus a rule engine unify the wording; every hit traces back to a clause |
| **Costly verification** | Unstructured materials are compared by hand, one by one | LLM-based structured extraction plus cross-material consistency checking |

**Scope and boundaries.** The model only surfaces suspicions, links clauses and generates explainable prompts. Review judgement, authorised disposition and exception approval always remain with a human. The system output is *always* a **lead pending review**; high-risk items or those with insufficient evidence are flagged explicitly.

---

## 2. Capability framework

The system implements the research project's **three core capabilities** as separate, independently replaceable layers:

```
(1) Material understanding & structured extraction     ingest/ + extract/
    Identify project, account, amount, date, vendor, approving role;
    check attachment completeness and field consistency
         |
         v
(2) Policy semantic matching & risk reasoning          rules/ + review/
    Link materials to policy clauses and in-house rules; detect account
    mismatch, vague purpose, amount or timing anomalies, contract-vs-claim
    inconsistency, and cross-material semantic contradictions
         |
         v
(3) Result explanation & evidence localisation         explain/
    Output severity, matched basis, related materials and suggested review
    points, all traceable to a specific clause and material
```

**The five-element framework** (threaded through the data model):

```
Event ────── Rule ──────── Evidence ─────── Risk ────────── Disposition
Budget/      Amount/        Claim/          Missing/       Supplement/
Contract/    Purpose/       Contract/       Conflict/      Return/
Reimburse/   Procedure/     Invoice/        Deviation/     Restrict/
Settlement   Timing/        Approval        Excess         Consult/
             Authority      notes           authority      Release
                                                        with trace
```

**Four adaptation paths** (technical options under real-world constraints):

| Path | Fits when | Where it lives |
| --- | --- | --- |
| Structured rules + knowledge retrieval | Policy wording is clear and stable | `rules/engine.py` + `rules/knowledge_base.py` |
| Retrieval-augmented generation (RAG) | Decisions need policy grounding | `rules/knowledge_base.py` retrieval → injected into prompt templates |
| Prompt templates & instruction engineering | Capability boundaries need fast validation | `prompts/` |
| Parameter-efficient fine-tuning (LoRA/QLoRA) | Domain terminology and context need adaptation | OpenAI-compatible endpoint in `llm/` pointing at a locally fine-tuned model |

---

## 3. Quick start

### 3.1 Requirements

- Windows 10/11 (macOS and Linux also run; the packaging script targets Windows)
- Python 3.11+

```bash
git clone https://github.com/DongFFF/fund-risk-sentinel.git
cd fund-risk-sentinel

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

### 3.2 Configure a model

Copy the templates and fill in your own API key (**config files are excluded by `.gitignore` and never uploaded**):

```bash
cp config/settings.example.yaml config/settings.yaml
cp config/providers.example.yaml config/providers.yaml
```

Edit `config/providers.yaml` — here, wiring up DeepSeek:

```yaml
active: deepseek
providers:
  deepseek:
    type: openai_compatible
    base_url: https://api.deepseek.com/v1
    api_key: ${DEEPSEEK_API_KEY}
    model: deepseek-chat
```

Environment variables are read directly, and Ollama / vLLM on the local machine gives you a fully offline setup (see `docs/03-接口规范.md`).

> No API key at all? The bundled `mock` provider runs the entire pipeline deterministically offline, so you can try the system before configuring anything.

### 3.3 Run it

```bash
# Graphical interface
python run.py

# Command-line smoke test (offline, uses the built-in mock model, exercises the whole chain)
python scripts/smoke_test.py

# Headless UI smoke test (no window; checks all 7 pages construct and refresh)
python scripts/ui_smoke_test.py

# Run a sample material through a real model
python examples/run_review_demo.py --file examples/样例-差旅报销单.txt
```

### 3.4 Build the exe

```bash
pip install -r requirements-dev.txt
python scripts/build_exe.py
# Output: dist/FundRiskSentinel.exe
```

---

## 4. Project layout

```
fund-risk-sentinel/
├── run.py                       # Desktop entry point
├── config/                      # Configuration (examples committed, real config never)
│   ├── settings.example.yaml
│   ├── providers.example.yaml
│   ├── rules/                   # Risk rule base (YAML)
│   └── knowledge/               # Policy knowledge base corpus
├── docs/                        # Requirements / architecture / interfaces / data model / evaluation / roadmap
├── src/fund_risk_sentinel/
│   ├── core/                    # Domain model: Event · Rule · Evidence · Risk · Disposition
│   ├── config/                  # Config loading and validation
│   ├── llm/                     # Unified, pluggable model API layer
│   ├── ingest/                  # Material upload and parsing (pdf/docx/xlsx/csv/img/txt)
│   ├── extract/                 # Capability (1): structured extraction
│   ├── rules/                   # Rule engine + policy knowledge base
│   ├── review/                  # Capability (2): review pipeline orchestration
│   ├── explain/                 # Capability (3): explanation and evidence localisation
│   ├── evaluate/                # Six-dimension evaluation metrics
│   ├── storage/                 # SQLite storage + audit log
│   └── ui/                      # PySide6 interface
├── tests/                       # pytest
├── examples/                    # Sample materials and demo scripts
└── scripts/                     # Smoke tests / database init / packaging
```

---

## 5. Data and security

- **Data never leaves the machine.** All materials, review results and audit logs are written only to the local `data/` directory (SQLite plus files).
- **Minimum necessary.** Data provenance, field definitions and redaction status are recorded at upload time; `ingest/` exposes redaction hooks.
- **Traceable.** Every risk lead retains the full chain: material → clause → model version → output → human adjustment.
- **No secrets in the repo.** `.gitignore` excludes `config/settings.yaml`, `config/providers.yaml`, `.env`, `data/` and `*.log`.

The audit log uses a **hash chain** (`entry_hash = sha256(prev_hash + …)`), so `verify_chain()` pinpoints exactly which record was tampered with rather than merely reporting that tampering occurred.

---

## 6. Development status

| Module | Status |
| --- | --- |
| Domain model (five-element framework) | ✅ Done |
| Unified model API layer | ✅ Done (OpenAI-compatible / Anthropic / Gemini / Mock) |
| Material upload and parsing interfaces | ✅ Done (8 formats) |
| Rule engine and policy knowledge base | ✅ Done (31 built-in rules + 40 policy clauses) |
| Review pipeline orchestration | ✅ Done (6 stages, per-stage degradation) |
| Storage layer and audit log | ✅ Done (tamper-evident hash chain) |
| Six-dimension evaluation module | ✅ Done |
| Desktop interface (PySide6) | ✅ Done (7 pages) |
| Automated tests | ✅ 158 pytest + 35 offline smoke + 17 UI smoke, all passing |
| exe packaging | 🔧 Packaging script ready, pending on-machine verification |
| Standardised dataset | ⏳ To be built (all data currently passes through LLM review first, accumulating into annotated corpora) |

See `docs/07-开发路线图.md` for the detailed roadmap.

---

## 7. Documentation

> The design documents are currently written in Chinese. English translations are on the roadmap — contributions welcome.

| Document | Contents |
| --- | --- |
| [01-需求与课题背景](docs/01-需求与课题背景.md) | Pain points, three core capabilities, five-element framework, scope boundaries |
| [02-系统架构](docs/02-系统架构.md) | Layering, six-stage pipeline, pluggable model layer, evaluation sandbox |
| [03-接口规范](docs/03-接口规范.md) | Model integration, upload interfaces, rule and knowledge base formats, Python API |
| [04-数据模型](docs/04-数据模型.md) | Domain model fields and design trade-offs |
| [05-评测方案](docs/05-评测方案.md) | Six-dimension metric definitions, thresholds, sample-size disclosure |
| [06-安全与合规](docs/06-安全与合规.md) | Responsibility boundaries, redaction, audit chain, key management |
| [07-开发路线图](docs/07-开发路线图.md) | Completed / near-term / mid-term / long-term plans |

---

## 8. License

Released under the MIT License — see [LICENSE](LICENSE).

Research project background is documented in `docs/01-需求与课题背景.md`.
