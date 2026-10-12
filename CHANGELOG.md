# 更新日志

本文件记录项目的所有重要变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 计划中
- 标准化风险标注数据集与人工复核结论回流机制
- 向量检索（chromadb + 本地 embedding）替换关键词检索
- OCR 接入（扫描件发票识别）
- 审查结果导出 Word / Excel 审查底稿
- 多用户与角色权限（经办人 / 审核人 / 项目负责人 / 财务）

## [0.1.0] - 2026-10-12

### 新增
- 项目初始化：目录结构、依赖声明、打包脚本
- **领域模型**：按「事项—规则—证据—风险—处置」五元关联框架定义完整数据模型
  （`core/models.py`、`core/enums.py`）
- **模型 API 统一接入层**：抽象 `BaseLLMProvider`，实现四类 provider
  - `openai_compatible`：DeepSeek / 通义千问 / Kimi / 智谱 / OpenAI / Ollama / vLLM / 任意兼容端点
  - `anthropic`：Claude 系列
  - `gemini`：Google Gemini
  - `mock`：离线确定性输出，用于无网络冒烟测试与单元测试
  - 支持多 provider 配置、运行时切换、失败自动回退
- **材料上传与解析接口层**：统一 `Material` 中间表示 + 8 种 loader
  （pdf / docx / xlsx / csv / json / txt / md / 图片），提供四类上传接口
  （路径 / 文件夹 / 字节流 / 粘贴文本），内容指纹去重、多编码探测、保形脱敏
- **规则引擎与制度知识库**：YAML 规则加载（AST 白名单沙箱求值）、
  字符二元组 BM25 条款检索（零依赖、断网可用），内置 31 条规则 + 40 条条款
- **审查主流程**：六阶段流水线
  （上下文组装 → 结构化抽取 → 规则校验 → 制度检索 → 语义推理 → 解释与证据定位），
  支持阶段级降级并在结论中明示
- **存储层**：SQLite（WAL）任务/材料/风险/结论持久化 + 哈希链审计日志（可定位篡改）
- **六维评测模块**：准确性、召回性、时延、一致性、可解释性、审核采纳度
- **桌面界面**：PySide6 主窗口 + 7 个功能页面
  （概览 / 材料上传 / 风险审查 / 规则库 / 知识库 / 能力评测 / 设置）
- **打包**：PyInstaller 单文件 exe 构建脚本
- **自动化测试**：158 项 pytest + 35 项离线冒烟 + 17 项界面冒烟测试，全程不联网

[Unreleased]: https://github.com/DongFFF/fund-risk-sentinel/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DongFFF/fund-risk-sentinel/releases/tag/v0.1.0
