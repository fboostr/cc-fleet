"""REVIEW_VERDICT 协议解析 + 剥协议尾。"""

from __future__ import annotations

from cc_fleet.core.review import parse_review_output, strip_review_protocol_tail


def test_parse_approved():
    text = """
    我核对了 plan，覆盖了原始需求，没发现遗漏。

    REVIEW_VERDICT: APPROVED
    """
    v = parse_review_output(text)
    assert v.status == "APPROVED"
    assert "核对了 plan" in v.body
    assert "REVIEW_VERDICT" not in v.body


def test_parse_needs_revision():
    text = (
        "## 审查意见\n"
        "1. 漏了空值处理\n"
        "2. 没有回滚方案\n\n"
        "REVIEW_VERDICT: NEEDS_REVISION\n"
    )
    v = parse_review_output(text)
    assert v.status == "NEEDS_REVISION"
    assert v.body == "## 审查意见\n1. 漏了空值处理\n2. 没有回滚方案"


def test_parse_takes_last_occurrence():
    """前文若举了协议示例，正文末尾的真实命中应被采纳。"""
    text = (
        "示例：REVIEW_VERDICT: NEEDS_REVISION\n\n"
        "真正的结论在末尾：\n"
        "REVIEW_VERDICT: APPROVED\n"
    )
    v = parse_review_output(text)
    assert v.status == "APPROVED"


def test_parse_missing_verdict():
    v = parse_review_output("写了一堆意见但忘了输出 verdict 协议行")
    assert v.status is None
    # 没有协议尾时 body 就是原文 rstrip
    assert v.body == "写了一堆意见但忘了输出 verdict 协议行"


def test_parse_case_insensitive():
    v = parse_review_output("review_verdict: approved\n")
    assert v.status == "APPROVED"


def test_strip_tail_no_protocol_returns_rstripped():
    assert strip_review_protocol_tail("纯文本\n\n  \n") == "纯文本"


def test_strip_tail_removes_only_final_protocol():
    text = (
        "示例块：```\nREVIEW_VERDICT: APPROVED\n```\n\n"
        "真协议尾：\n"
        "REVIEW_VERDICT: NEEDS_REVISION\n"
    )
    body = strip_review_protocol_tail(text)
    # 示例块里的还在，末尾真协议被剥
    assert "示例块" in body
    assert body.endswith("真协议尾：")
