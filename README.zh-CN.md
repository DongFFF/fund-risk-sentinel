# fund-risk-sentinel

[English](README.md) | **简体中文**

> 高校科研经费风险智能识别系统（桌面端）
> Research Fund Risk Sentinel — Desktop Risk Identification for University Research Funding

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey)
![Tests](https://img.shields.io/badge/checks-210%20passing-brightgreen)
![Data](https://img.shields.io/badge/data-stays%20local-orange)

一套面向高校科研经费**事前预警、事中拦截**场景的桌面应用：把报销单据、合同、发票、审批意见等非结构化材料上传进来，由**可自由配置的大模型**按「事项—规则—证据—风险—处置」五元关联框架完成审查，输出**可核验的风险线索**（风险等级 + 命中依据 + 关联材料 + 建议复核点），而不是替人下合规结论。

程序可打包为 Windows 单文件 `exe`，开箱即用；模型 API 通过配置接入，**不写死任何厂商**。

---

## 目录

- [一、这个程序解决什么问题](#一这个程序解决什么问题)
- [二、能力框架](#二能力框架对应课题研究主线)
- [三、快速开始](#三快速开始)
- [四、目录结构](#四目录结构)
- [五、数据与安全](#五数据与安全)
- [六、开发状态](#六开发状态)
- [七、文档索引](#七文档索引)
- [八、许可](#八许可)

---

## 一、这个程序解决什么问题

高校科研经费管理长期依赖「事后审计 + 人工抽查」，存在三个痛点：

| 痛点 | 表现 | 本系统的应对 |
| --- | --- | --- |
| 发现滞后 | 违规行为在结题审计时才暴露 | 把审查前移到报销/合同/预算节点之前 |
| 制度规则分散 | 国家办法、负面清单、校内细则口径不一 | 制度知识库 + 规则引擎统一口径，命中即可回溯条款 |
| 材料核验成本高 | 非结构化材料靠人逐份比对 | 大模型结构化抽取 + 跨材料一致性比对 |

**定位边界**：模型只负责发现疑点、关联条款、生成可解释提示；复核判断、授权处置、例外认定始终由人承担。程序输出的永远是「待核验线索」，高风险或证据不足的事项会被明确标注。

---

## 二、能力框架（对应课题研究主线）

系统按课题提出的**三类核心能力**分层实现，三条能力链路各自独立、可单独替换：

```
①  材料理解与结构化抽取   ingest/ + extract/
    识别项目、科目、金额、时间、供应商、审批角色；判断附件齐全性、字段一致性
         ↓
②  制度语义匹配与风险推理   rules/ + review/
    材料 ↔ 制度条款/校内口径关联；识别科目不匹配、用途模糊、额度或时点异常、
    合同与申请信息不一致、跨材料语义矛盾
         ↓
③  结果解释与证据定位       explain/
    输出风险等级、命中依据、关联材料、建议复核点，可追溯到具体条款与材料
```

**五元关联框架**（贯穿数据模型）：

```
事项(Event) ── 规则(Rule) ── 证据(Evidence) ── 风险(Risk) ── 处置(Disposition)
  预算/合同/     额度/用途/     申请单/合同/     缺失/冲突/      补充/退回/
  报销/结算      程序/时点/     发票/审批意见   偏离/越权/      限制/会商/
                 授权                           异常组合        放行留痕
```

**四条适配路径**（对应现实约束下的技术配置）：

| 路径 | 适用场景 | 本系统落点 |
| --- | --- | --- |
| 结构化规则 + 知识检索 | 制度口径清晰稳定 | `rules/engine.py` + `rules/knowledge_base.py` |
| 检索增强生成（RAG） | 需制度依据支撑 | `rules/knowledge_base.py` 检索 → 注入提示模板 |
| 提示模板与指令工程 | 需快速验证能力边界 | `prompts/` |
| 参数高效微调（LoRA/QLoRA） | 需领域术语与情境适配 | `llm/` 中指向本地微调模型的 OpenAI 兼容端点 |

---

## 三、快速开始

### 3.1 环境

- Windows 10/11（macOS / Linux 亦可运行，打包脚本以 Windows 为主）
- Python 3.11+

```bash
git clone https://github.com/DongFFF/fund-risk-sentinel.git
cd fund-risk-sentinel

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

### 3.2 配置模型

复制配置模板，填入你自己的 API Key（**配置文件已被 .gitignore 排除，不会上传**）：

```bash
cp config/settings.example.yaml config/settings.yaml
cp config/providers.example.yaml config/providers.yaml
```

编辑 `config/providers.yaml`，例如接入 DeepSeek：

```yaml
active: deepseek
providers:
  deepseek:
    type: openai_compatible
    base_url: https://api.deepseek.com/v1
    api_key: ${DEEPSEEK_API_KEY}
    model: deepseek-chat
```

也支持直接读环境变量，或在本机跑 Ollama / vLLM 完全离线使用（见 `docs/03-接口规范.md`）。

> 完全不想配 API Key？内置 `mock` 模型会以确定性方式离线跑通全流程，可以先体验再配置。

### 3.3 启动

```bash
# 图形界面
python run.py

# 命令行冒烟测试（不联网，走内置 mock 模型，验证全链路）
python scripts/smoke_test.py

# 界面无头冒烟测试（不弹窗，验证 7 个页面能否构造与刷新）
python scripts/ui_smoke_test.py

# 用真实模型跑一份样例材料
python examples/run_review_demo.py --file examples/样例-差旅报销单.txt
```

### 3.4 打包为 exe

```bash
pip install -r requirements-dev.txt
python scripts/build_exe.py
# 产物：dist/FundRiskSentinel.exe
```

---

## 四、目录结构

```
fund-risk-sentinel/
├── run.py                       # 桌面端入口
├── config/                      # 配置（示例入库，真实配置不入库）
│   ├── settings.example.yaml
│   ├── providers.example.yaml
│   ├── rules/                   # 风险规则库（YAML）
│   └── knowledge/               # 制度知识库语料
├── docs/                        # 需求 / 架构 / 接口 / 数据模型 / 评测 / 路线图
├── src/fund_risk_sentinel/
│   ├── core/                    # 领域模型：事项·规则·证据·风险·处置
│   ├── config/                  # 配置加载与校验
│   ├── llm/                     # 模型 API 统一接入层（可插拔）
│   ├── ingest/                  # 材料上传与解析（pdf/docx/xlsx/csv/img/txt）
│   ├── extract/                 # 能力①：结构化抽取
│   ├── rules/                   # 规则引擎 + 制度知识库
│   ├── review/                  # 能力②：审查主流程编排
│   ├── explain/                 # 能力③：解释与证据定位
│   ├── evaluate/                # 六维评测指标
│   ├── storage/                 # SQLite 存储 + 审计日志
│   └── ui/                      # PySide6 界面
├── tests/                       # pytest
├── examples/                    # 样例材料与演示脚本
└── scripts/                     # 冒烟测试 / 建库 / 打包
```

---

## 五、数据与安全

- **数据不出本地**：所有材料、审查结果、审计日志默认只写入本机 `data/` 目录（SQLite + 文件）。
- **最小必要**：上传时即记录数据来源、字段口径与脱敏状态；`ingest/` 提供脱敏钩子。
- **可追溯**：每条风险线索都保留「材料 → 条款 → 模型版本 → 输出 → 人工调整」的完整链路。
- **密钥不入库**：`.gitignore` 已排除 `config/settings.yaml`、`config/providers.yaml`、`.env`、`data/`、`*.log`。

审计日志采用**哈希链**（`entry_hash = sha256(prev_hash + …)`），`verify_chain()` 能定位到具体是第几条记录被篡改，而不是只报告「被改过」。

---

## 六、开发状态

| 模块 | 状态 |
| --- | --- |
| 领域模型（五元框架） | ✅ 已完成 |
| 模型 API 统一接入层 | ✅ 已完成（OpenAI 兼容 / Anthropic / Gemini / Mock） |
| 材料上传与解析接口 | ✅ 已完成（8 种格式） |
| 规则引擎与制度知识库 | ✅ 已完成（31 条内置规则 + 40 条制度条款） |
| 审查主流程编排 | ✅ 已完成（6 阶段，阶段级降级） |
| 存储层与审计日志 | ✅ 已完成（哈希链防篡改） |
| 六维评测模块 | ✅ 已完成 |
| 桌面界面（PySide6） | ✅ 已完成（7 个页面） |
| 自动化测试 | ✅ 158 项 pytest + 35 项离线冒烟 + 17 项界面冒烟，全部通过 |
| exe 打包 | 🔧 打包脚本已就绪，待实机验证 |
| 标准化数据集 | ⏳ 待建（当前所有数据先经大模型审查，逐步沉淀为标注语料） |

详细路线见 `docs/07-开发路线图.md`。

---

## 七、文档索引

| 文档 | 内容 |
| --- | --- |
| [01-需求与课题背景](docs/01-需求与课题背景.md) | 痛点、三类核心能力、五元框架、定位边界 |
| [02-系统架构](docs/02-系统架构.md) | 分层、六阶段流水线、可插拔模型层、安全沙箱 |
| [03-接口规范](docs/03-接口规范.md) | 模型接入、上传接口、规则库与知识库格式、Python API |
| [04-数据模型](docs/04-数据模型.md) | 领域模型字段与设计取舍 |
| [05-评测方案](docs/05-评测方案.md) | 六维指标口径、阈值、样本量披露 |
| [06-安全与合规](docs/06-安全与合规.md) | 责任边界、脱敏、审计链、密钥管理 |
| [07-开发路线图](docs/07-开发路线图.md) | 已完成 / 近期 / 中期 / 后期规划 |

---

## 八、许可

本项目采用 MIT License，详见 [LICENSE](LICENSE)。

课题相关信息见 `docs/01-需求与课题背景.md`。
