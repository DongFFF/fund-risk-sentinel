"""上传与解析层：格式识别、角色判定、脱敏、四类上传接口。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fund_risk_sentinel.config.settings import SecuritySettings
from fund_risk_sentinel.core.enums import DocumentRole
from fund_risk_sentinel.ingest.base import detect_format, file_sha256
from fund_risk_sentinel.ingest.pipeline import IngestPipeline
from fund_risk_sentinel.ingest.redaction import Redactor
from fund_risk_sentinel.ingest.roles import infer_role

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture()
def ingest() -> IngestPipeline:
    return IngestPipeline()


class TestDetectFormat:
    @pytest.mark.parametrize(
        "name",
        ["a.txt", "a.md", "a.csv", "a.json", "a.docx", "a.xlsx", "a.pdf", "a.png"],
    )
    def test_known_extensions(self, name: str) -> None:
        assert detect_format(name).value != "unknown"

    def test_unknown_extension(self) -> None:
        assert detect_format("a.zzz").value == "unknown"


class TestFileHash:
    def test_same_content_same_hash(self, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("同样的内容", encoding="utf-8")
        b.write_text("同样的内容", encoding="utf-8")
        assert file_sha256(a) == file_sha256(b)

    def test_different_content_differs(self, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("内容一", encoding="utf-8")
        b.write_text("内容二", encoding="utf-8")
        assert file_sha256(a) != file_sha256(b)


class TestInferRole:
    def test_policy_by_article_structure(self) -> None:
        """条文结构（≥3 处「第X条」）应直接判为制度，不受文件名干扰。"""
        text = "\n".join(f"第{i}条 经费使用应当遵循相关规定。" for i in range(1, 6))
        assert infer_role("关于报销的通知.txt", text) is DocumentRole.POLICY

    def test_invoice_by_keyword(self) -> None:
        assert infer_role("增值税发票.txt", "发票号码：12345678") is DocumentRole.INVOICE

    def test_contract_by_filename(self) -> None:
        assert infer_role("技术服务合同.txt", "甲方乙方约定如下") is DocumentRole.CONTRACT

    def test_fallback_other(self) -> None:
        assert infer_role("随手记.txt", "今天天气不错") is DocumentRole.OTHER


class TestRedactor:
    def test_phone_masked_keeping_edges(self) -> None:
        redactor = Redactor(SecuritySettings(redact_phone=True))
        text, log = redactor.redact("联系电话：13812345678")
        assert "13812345678" not in text
        assert text.startswith("联系电话：138")
        assert log

    def test_id_card_masked(self) -> None:
        redactor = Redactor(SecuritySettings(redact_id_card=True))
        text, log = redactor.redact("身份证号：110101199003071234")
        assert "110101199003071234" not in text
        assert "身份证" in "".join(log)

    def test_email_masked(self) -> None:
        redactor = Redactor(SecuritySettings(redact_email=True))
        text, _ = redactor.redact("邮箱：zhangwei@example.edu.cn")
        assert "zhangwei@example.edu.cn" not in text

    def test_disabled_keeps_original(self) -> None:
        redactor = Redactor(
            SecuritySettings(redact_phone=False, redact_id_card=False, redact_bank_account=False)
        )
        text, log = redactor.redact("联系电话：13812345678")
        assert "13812345678" in text
        assert log == []

    def test_redact_material_records_flag(self) -> None:
        from fund_risk_sentinel.core.models import Material

        material = Material(filename="a.txt", text="手机：13900001111")
        Redactor(SecuritySettings(redact_phone=True)).redact_material(material)
        assert material.redaction_applied is True
        assert "13900001111" not in material.text


class TestIngestPipeline:
    def test_load_paths_reads_txt(self, ingest: IngestPipeline) -> None:
        result = ingest.load_paths([EXAMPLES / "样例-差旅报销单.txt"])
        assert len(result.items) == 1
        assert result.items[0].ok is True
        assert result.materials[0].text

    def test_load_text_interface(self, ingest: IngestPipeline) -> None:
        result = ingest.load_text("报销人：张伟\n金额：1000", name="粘贴.txt")
        assert len(result.items) == 1
        assert "张伟" in result.materials[0].text

    def test_load_bytes_interface(self, ingest: IngestPipeline) -> None:
        payload = "项目名称：测试项目\n金额：500".encode()
        result = ingest.load_bytes("内存.txt", payload)
        assert result.materials[0].text

    def test_load_folder_filters_extensions(self, ingest: IngestPipeline, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("内容一", encoding="utf-8")
        (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02")
        result = ingest.load_folder(tmp_path)
        names = {m.filename for m in result.materials}
        assert "a.txt" in names
        assert "b.bin" not in names

    def test_duplicate_fingerprint_deduplicated(self, ingest: IngestPipeline, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("完全一样的内容", encoding="utf-8")
        b.write_text("完全一样的内容", encoding="utf-8")
        result = ingest.load_paths([a, b])
        assert len(result.materials) == 1

    def test_missing_file_recorded_as_error(self, ingest: IngestPipeline, tmp_path: Path) -> None:
        result = ingest.load_paths([tmp_path / "不存在.txt"])
        assert result.items[0].ok is False
        assert result.items[0].error

    def test_one_bad_file_does_not_break_batch(self, ingest: IngestPipeline, tmp_path: Path) -> None:
        good = tmp_path / "good.txt"
        good.write_text("金额：1000", encoding="utf-8")
        result = ingest.load_paths([good, tmp_path / "missing.txt"])
        assert len(result.materials) == 1
        assert any(not item.ok for item in result.items)

    def test_json_loader_reads_list_of_dicts(self, ingest: IngestPipeline, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text(
            json.dumps([{"科目": "差旅费", "金额": "3800"}], ensure_ascii=False), encoding="utf-8"
        )
        result = ingest.load_paths([path])
        assert "差旅费" in result.materials[0].text

    def test_assemble_context_puts_policy_first(self, ingest: IngestPipeline) -> None:
        """制度类材料应排在业务材料之前，先给模型"规则口径"再给"事实"。"""
        from fund_risk_sentinel.core.enums import DocumentRole
        from fund_risk_sentinel.core.models import Material

        result = ingest.load_paths([EXAMPLES / "样例-差旅报销单.txt"])
        application = result.materials[0]
        policy = Material(
            filename="实施细则.md",
            role=DocumentRole.POLICY,
            text="第一条 经费使用应当遵循相关规定。",
        )
        context, _warnings = ingest.assemble_context([application, policy])
        assert "第一条" in context
        # 清单与正文小节都应把制度类排在业务材料之前
        assert context.index("1. 实施细则.md") < context.index("2. 样例-差旅报销单.txt")
        assert context.index("【材料 1】实施细则.md") < context.index("【材料 2】样例-差旅报销单.txt")
