"""MR_TITLE / MR_DESCRIPTION 协议解析单测。"""

from __future__ import annotations

from cc_fleet.core.mr_meta import parse_mr_metadata


def test_parse_basic():
    text = """
    完成报告：略

    MR_TITLE: feat: 在 README 顶部新增项目简介行

    MR_DESCRIPTION_BEGIN
    ## 背景
    用户希望 README 顶部有简介。

    ## 改动概要
    - README.md 顶部插入一行
    MR_DESCRIPTION_END
    """
    m = parse_mr_metadata(text)
    assert m.title == "feat: 在 README 顶部新增项目简介行"
    assert m.description is not None
    assert "## 背景" in m.description
    assert "用户希望 README 顶部有简介。" in m.description
    assert "## 改动概要" in m.description
    # description 保留块内换行
    assert "\n" in m.description


def test_parse_takes_last_match():
    # 前文示例（如 prompt 中的样例）不应污染主控解析；取最后一次命中。
    text = """
    示例（参考）：
    MR_TITLE: <旧示例标题>

    MR_DESCRIPTION_BEGIN
    示例描述
    MR_DESCRIPTION_END

    现在是实际输出：

    MR_TITLE: feat: 真正的标题

    MR_DESCRIPTION_BEGIN
    真正的描述
    MR_DESCRIPTION_END
    """
    m = parse_mr_metadata(text)
    assert m.title == "feat: 真正的标题"
    assert m.description is not None
    assert "真正的描述" in m.description
    assert "示例描述" not in m.description


def test_parse_missing_returns_none():
    text = "完成报告：略\n（claude 忘记按协议输出）"
    m = parse_mr_metadata(text)
    assert m.title is None
    assert m.description is None


def test_parse_empty_text():
    m = parse_mr_metadata("")
    assert m.title is None
    assert m.description is None


def test_parse_only_title():
    text = "MR_TITLE: fix: 修一处空指针"
    m = parse_mr_metadata(text)
    assert m.title == "fix: 修一处空指针"
    assert m.description is None


def test_parse_only_description():
    text = """
    MR_DESCRIPTION_BEGIN
    ## 背景
    略
    MR_DESCRIPTION_END
    """
    m = parse_mr_metadata(text)
    assert m.title is None
    assert m.description is not None
    assert "## 背景" in m.description


def test_parse_empty_title_treated_as_none():
    # MR_TITLE: 行存在但值为空白，应当被视作缺失
    text = "MR_TITLE:    \n"
    m = parse_mr_metadata(text)
    assert m.title is None


def test_parse_empty_description_block_treated_as_none():
    text = """
    MR_DESCRIPTION_BEGIN

    MR_DESCRIPTION_END
    """
    m = parse_mr_metadata(text)
    assert m.description is None


def test_parse_description_with_special_chars():
    # 模板里包含反引号、引号、URL、emoji 等不应破坏解析
    text = """
    MR_TITLE: chore: 升级依赖

    MR_DESCRIPTION_BEGIN
    ## 改动概要
    - 升级 `foo` 至 1.2.3
    - 见 https://example.com/changelog
    - "需要 reviewer 关注"
    MR_DESCRIPTION_END
    """
    m = parse_mr_metadata(text)
    assert m.title == "chore: 升级依赖"
    assert m.description is not None
    assert "`foo`" in m.description
    assert "https://example.com/changelog" in m.description
