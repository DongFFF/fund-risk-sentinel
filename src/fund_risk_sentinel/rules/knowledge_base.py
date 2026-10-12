"""制度知识库与检索（对应课题"检索增强生成（RAG）"路径）。

职责：把分散的制度条款（国家办法、负面清单、校内细则）整理成可检索的条目，
在审查时按材料内容召回最相关的条款，注入提示模板，使模型的每一条风险提示
都能挂上"制度依据"，而不是自由发挥。

检索实现刻意不依赖向量库：中文用**字符二元组 + 倒排索引 + BM25 打分**。
理由是部署成本——exe 用户不需要额外装 torch、不需要下载 embedding 模型、
断网也能用。若后续要换成向量检索，只需替换 ``KnowledgeBase._score``，
接口不变（``search`` 的签名与返回类型保持不变）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..core.enums import DocumentRole
from ..core.exceptions import KnowledgeBaseError
from ..core.models import ClauseRef, Material
from ..core.models import Base

logger = logging.getLogger(__name__)

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_WORD = re.compile(r"[a-zA-Z0-9_]{2,}")
_ARTICLE_HEAD = re.compile(r"^\s*(第[一二三四五六七八九十百零〇\d]+条|第[一二三四五六七八九十百零〇\d]+章)")

#: BM25 参数
_K1 = 1.5
_B = 0.75


class Clause(Base):
    """知识库内部条款条目（比 ``ClauseRef`` 多出检索用字段）。"""

    clause_id: str
    doc_title: str = ""
    doc_no: str = ""
    article: str = ""
    text: str = ""
    effective_date: str = ""
    url: str = ""
    tags: list[str] = []
    keywords: list[str] = []

    def to_ref(self) -> ClauseRef:
        return ClauseRef(
            doc_title=self.doc_title,
            doc_no=self.doc_no,
            article=self.article,
            text=self.text,
            effective_date=self.effective_date,
            url=self.url,
            clause_id=self.clause_id,
        )


def tokenize(text: str) -> list[str]:
    """中英混排分词：英文按词，中文按字符二元组（不依赖分词库）。"""
    if not text:
        return []
    lowered = text.lower()
    tokens: list[str] = _LATIN_WORD.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) <= 4:
            tokens.append(run)
    return tokens


class KnowledgeBase:
    """制度条款知识库。"""

    def __init__(self, clauses: Iterable[Clause] | None = None) -> None:
        self._clauses: list[Clause] = list(clauses or [])
        self._index: dict[str, dict[int, int]] = {}
        self._doc_len: list[int] = []
        self._avg_len: float = 0.0
        self._dirty: bool = True
        if self._clauses:
            self._build_index()

    # ============================================================ 构建

    def _build_index(self) -> None:
        index: dict[str, dict[int, int]] = {}
        lengths: list[int] = []
        for idx, clause in enumerate(self._clauses):
            tokens = tokenize(f"{clause.doc_title} {clause.article} {clause.text} {' '.join(clause.tags)} {' '.join(clause.keywords)}")
            counter = Counter(tokens)
            lengths.append(len(tokens) or 1)
            for token, freq in counter.items():
                index.setdefault(token, {})[idx] = freq
        self._index = index
        self._doc_len = lengths
        self._avg_len = (sum(lengths) / len(lengths)) if lengths else 0.0
        self._dirty = False

    def _ensure_index(self) -> None:
        if self._dirty:
            self._build_index()

    # ============================================================ 增删

    def add_clause(
        self,
        text: str,
        *,
        doc_title: str = "",
        doc_no: str = "",
        article: str = "",
        effective_date: str = "",
        url: str = "",
        tags: Iterable[str] | None = None,
        keywords: Iterable[str] | None = None,
        clause_id: str = "",
    ) -> Clause:
        clause = Clause(
            clause_id=clause_id or f"clause-{len(self._clauses) + 1:04d}",
            doc_title=doc_title,
            doc_no=doc_no,
            article=article,
            text=text.strip(),
            effective_date=effective_date,
            url=url,
            tags=list(tags or []),
            keywords=list(keywords or []),
        )
        self._clauses.append(clause)
        self._dirty = True
        return clause

    def add_clauses(self, clauses: Iterable[Clause]) -> int:
        count = 0
        for clause in clauses:
            self._clauses.append(clause)
            count += 1
        self._dirty = True
        return count

    def clear(self) -> None:
        self._clauses.clear()
        self._index.clear()
        self._doc_len.clear()
        self._avg_len = 0.0
        self._dirty = True

    @property
    def clauses(self) -> list[Clause]:
        return list(self._clauses)

    def __len__(self) -> int:
        return len(self._clauses)

    # ============================================================ 载入

    def add_from_yaml(self, path: Path | str) -> int:
        """从 YAML 载入条款。

        格式（顶层 ``clauses`` 列表）::

            doc_title: 国家自然科学基金资助项目资金管理办法
            doc_no: 财教〔2021〕177 号
            effective_date: "2021-09-30"
            clauses:
              - article: 第十二条
                text: 直接费用中……
                tags: [额度, 用途]
        """
        p = Path(path)
        try:
            with p.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise KnowledgeBaseError(f"知识库文件 {p} 读取失败：{exc}") from exc

        if isinstance(data, list):
            raw_clauses, meta = data, {}
        elif isinstance(data, dict):
            raw_clauses = data.get("clauses") or []
            meta = data
        else:
            raise KnowledgeBaseError(f"知识库文件 {p} 顶层结构不合法")

        added = 0
        for index, item in enumerate(raw_clauses):
            if not isinstance(item, dict):
                continue
            self.add_clause(
                str(item.get("text") or ""),
                doc_title=str(item.get("doc_title") or meta.get("doc_title") or p.stem),
                doc_no=str(item.get("doc_no") or meta.get("doc_no") or ""),
                article=str(item.get("article") or ""),
                effective_date=str(item.get("effective_date") or meta.get("effective_date") or ""),
                url=str(item.get("url") or meta.get("url") or ""),
                tags=item.get("tags") or meta.get("tags") or [],
                keywords=item.get("keywords") or [],
                clause_id=str(item.get("clause_id") or f"{p.stem}-{index + 1:03d}"),
            )
            added += 1
        return added

    def add_from_markdown(
        self,
        path: Path | str,
        *,
        doc_title: str = "",
        doc_no: str = "",
        effective_date: str = "",
        url: str = "",
    ) -> int:
        """从制度全文（Markdown / 纯文本）按"第X条"切分条款。

        这是把一份制度文件变成知识库最常用的方式：财务处直接把办法原文丢进来即可。
        """
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise KnowledgeBaseError(f"制度文件 {p} 读取失败：{exc}") from exc

        title = doc_title or _guess_title(text) or p.stem
        chapters: list[tuple[str, str]] = []
        current_article = ""
        current_chapter = ""
        buffer: list[str] = []

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            head = _ARTICLE_HEAD.match(stripped)
            if head:
                token = head.group(1)
                if "章" in token:
                    current_chapter = token
                    continue
                if current_article and buffer:
                    chapters.append((current_article, "\n".join(buffer)))
                current_article = token
                buffer = [stripped]
            elif current_article:
                buffer.append(stripped)
        if current_article and buffer:
            chapters.append((current_article, "\n".join(buffer)))

        if not chapters:
            # 没有条文结构：整篇作为一条，避免知识库为空
            self.add_clause(
                text,
                doc_title=title,
                doc_no=doc_no,
                article="全文",
                effective_date=effective_date,
                url=url,
                tags=[p.stem],
            )
            return 1

        for article, body in chapters:
            self.add_clause(
                body,
                doc_title=title,
                doc_no=doc_no,
                article=article,
                effective_date=effective_date,
                url=url,
                tags=[current_chapter] if current_chapter else [],
            )
        return len(chapters)

    def add_from_materials(self, materials: Iterable[Material]) -> int:
        """把标记为"制度文本"角色的上传材料自动纳入知识库。"""
        added = 0
        for material in materials:
            if material.role != DocumentRole.POLICY:
                continue
            added += self.add_from_markdown_text(material.text, doc_title=material.filename)
        return added

    def add_from_markdown_text(self, text: str, *, doc_title: str = "", doc_no: str = "") -> int:
        """同 ``add_from_markdown``，但输入是文本而非文件路径。"""
        lines = text.splitlines()
        chapters: list[tuple[str, str]] = []
        current_article = ""
        buffer: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            head = _ARTICLE_HEAD.match(stripped)
            if head and "章" not in head.group(1):
                if current_article and buffer:
                    chapters.append((current_article, "\n".join(buffer)))
                current_article = head.group(1)
                buffer = [stripped]
            elif current_article:
                buffer.append(stripped)
        if current_article and buffer:
            chapters.append((current_article, "\n".join(buffer)))

        title = doc_title or _guess_title(text) or "制度文本"
        if not chapters:
            self.add_clause(text, doc_title=title, doc_no=doc_no, article="全文")
            return 1
        for article, body in chapters:
            self.add_clause(body, doc_title=title, doc_no=doc_no, article=article)
        return len(chapters)

    # ============================================================ 检索

    def search(
        self,
        query: str,
        *,
        top_k: int = 6,
        min_score: float = 0.0,
        doc_title_filter: str = "",
    ) -> list[ClauseRef]:
        """按查询文本召回最相关的制度条款。"""
        self._ensure_index()
        if not self._clauses:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scores: dict[int, float] = {}
        total_docs = len(self._clauses)
        for token in set(query_tokens):
            postings = self._index.get(token)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            for idx, freq in postings.items():
                doc_len = self._doc_len[idx] or 1
                denom = freq + _K1 * (1 - _B + _B * doc_len / (self._avg_len or 1))
                scores[idx] = scores.get(idx, 0.0) + idf * (freq * (_K1 + 1)) / denom

        if not scores:
            return []

        # 标签/关键词命中加权，让"额度""用途"这类制度维度词更精准
        lowered_query = query.lower()
        for idx, clause in enumerate(self._clauses):
            if idx not in scores:
                continue
            for keyword in list(clause.keywords) + list(clause.tags):
                if keyword and keyword.lower() in lowered_query:
                    scores[idx] += 1.5

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        results: list[ClauseRef] = []
        for idx, score in ranked:
            if score <= min_score:
                continue
            clause = self._clauses[idx]
            if doc_title_filter and doc_title_filter not in clause.doc_title:
                continue
            results.append(clause.to_ref())
            if len(results) >= top_k:
                break
        return results

    def search_multi(
        self,
        queries: Iterable[str],
        *,
        top_k: int = 6,
        per_query_k: int = 3,
    ) -> list[ClauseRef]:
        """多路查询合并去重——按材料角色分别检索再汇总，召回更稳。"""
        seen: set[str] = set()
        merged: list[ClauseRef] = []
        for query in queries:
            for ref in self.search(query, top_k=per_query_k):
                key = ref.clause_id or f"{ref.doc_title}{ref.article}"
                if key in seen:
                    continue
                seen.add(key)
                merged.append(ref)
                if len(merged) >= top_k:
                    return merged
        return merged

    # ============================================================ 持久化

    def save(self, path: Path | str) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": "1.0", "clauses": [c.model_dump(mode="json") for c in self._clauses]}
        if p.suffix.lower() == ".json":
            p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            with p.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False, width=1000)
        return p

    def load(self, path: Path | str) -> int:
        p = Path(path)
        if p.suffix.lower() == ".json":
            data = json.loads(p.read_text(encoding="utf-8"))
            return self.add_clauses(Clause.model_validate(c) for c in data.get("clauses", []))
        return self.add_from_yaml(p)

    # ============================================================ 元信息

    def stats(self) -> dict[str, Any]:
        docs = Counter(c.doc_title for c in self._clauses)
        return {
            "clause_count": len(self._clauses),
            "document_count": len(docs),
            "documents": [{"title": t, "clauses": n} for t, n in docs.most_common(20)],
            "token_count": len(self._index),
        }

    def format_for_prompt(self, refs: Iterable[ClauseRef], *, max_chars: int = 4000) -> str:
        """把召回条款格式化为提示词片段。"""
        lines: list[str] = []
        used = 0
        for index, ref in enumerate(refs, start=1):
            header = f"[制度依据 {index}] {ref.display()}"
            body = ref.text.strip().replace("\n", " ")
            block = f"{header}\n{body}\n"
            if used + len(block) > max_chars:
                break
            lines.append(block)
            used += len(block)
        return "\n".join(lines)


def _guess_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped and ("办法" in stripped or "规定" in stripped or "通知" in stripped or "意见" in stripped):
            return stripped[:80]
    return ""


# ================================================================ 载入工具

def load_knowledge_base(directory: Path | str) -> KnowledgeBase:
    """从目录载入知识库：支持 ``*.yaml`` / ``*.yml`` / ``*.md`` / ``*.txt``。"""
    root = Path(directory)
    kb = KnowledgeBase()
    if not root.is_dir():
        return kb
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith((".", "_")):
            continue
        suffix = path.suffix.lower()
        try:
            if suffix in (".yaml", ".yml", ".json"):
                kb.add_from_yaml(path)
            elif suffix in (".md", ".txt"):
                kb.add_from_markdown(path)
        except KnowledgeBaseError as exc:
            logger.warning("知识库文件 %s 载入失败：%s", path, exc)
    return kb


__all__ = ["Clause", "KnowledgeBase", "tokenize", "load_knowledge_base"]
