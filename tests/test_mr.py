"""MR/PR 提交关键分支：GitLab push option 拼装、平台探测、GitHub PR 创建、URL 提取。"""

from __future__ import annotations

import pytest

from cc_fleet.core import mr as mr_module
from cc_fleet.core.mr import (
    MR_URL_PATTERN,
    PR_URL_PATTERN,
    MrCreateError,
    build_push_cmd,
    create_pr_via_api,
    create_review_request,
    extract_mr_url_from_text,
    github_api_base,
    github_compare_url,
    parse_github_owner_repo,
    platform_from_remote_url,
)


def test_build_push_cmd_contains_all_options():
    cmd = build_push_cmd(
        source_branch="claude/add-readme",
        target_branch="main",
        title="加一行 readme",
        description="详细描述\n多行也支持",
    )
    assert cmd[:5] == ["git", "push", "-u", "origin", "claude/add-readme"]
    assert "merge_request.create" in cmd
    assert "merge_request.target=main" in cmd
    assert "merge_request.title=加一行 readme" in cmd
    # 换行被转义为字面量 \n（GitLab push option 不允许真换行）
    assert "merge_request.description=详细描述\\n多行也支持" in cmd
    assert "merge_request.remove_source_branch" in cmd


def test_build_push_cmd_custom_remote():
    cmd = build_push_cmd(
        source_branch="x", target_branch="develop", title="t", description="d", remote="upstream"
    )
    assert cmd[3] == "upstream"
    assert "merge_request.target=develop" in cmd


def test_build_push_cmd_escapes_multiple_newlines_in_description():
    """GitLab 服务端拒绝含真换行的 push option；多行 markdown 描述必须转义为字面量 \\n。"""
    md_desc = "## Summary\n- 第一点\n- 第二点\n\n## Test plan\n- 跑 pytest"
    cmd = build_push_cmd(
        source_branch="x", target_branch="main", title="t", description=md_desc
    )
    expected = "merge_request.description=## Summary\\n- 第一点\\n- 第二点\\n\\n## Test plan\\n- 跑 pytest"
    assert expected in cmd
    # 关键不变量：值串里不能含真换行字符
    desc_opt = next(o for o in cmd if o.startswith("merge_request.description="))
    assert "\n" not in desc_opt


def test_build_push_cmd_single_line_description_unchanged():
    cmd = build_push_cmd(
        source_branch="x", target_branch="main", title="t", description="纯单行描述"
    )
    assert "merge_request.description=纯单行描述" in cmd


def test_url_pattern_extracts_from_remote_stderr():
    stderr = (
        "remote: View merge request for claude/add-readme:\n"
        "remote:   https://gitlab.example.com/foo/bar/-/merge_requests/42\n"
        "To gitlab.example.com:foo/bar.git\n"
    )
    m = MR_URL_PATTERN.search(stderr)
    assert m is not None
    assert m.group(0) == "https://gitlab.example.com/foo/bar/-/merge_requests/42"


def test_url_pattern_no_match():
    assert MR_URL_PATTERN.search("just some random output") is None


# ---- extract_mr_url_from_text（mode=remote 协议） ----

def test_extract_mr_url_prefers_protocol_line():
    text = (
        "完成报告：改了 README\n"
        "顺便看到一个其他 MR https://gitlab/foo/bar/-/merge_requests/1\n"
        "\n"
        "MR_URL: https://gitlab.example.com/group/repo/-/merge_requests/777\n"
    )
    assert extract_mr_url_from_text(text) == "https://gitlab.example.com/group/repo/-/merge_requests/777"


def test_extract_mr_url_falls_back_to_any_url_when_protocol_missing():
    text = (
        "claude 忘了加协议行，但 push 输出里有 URL：\n"
        "  https://gitlab.example.com/g/r/-/merge_requests/42\n"
    )
    assert extract_mr_url_from_text(text) == "https://gitlab.example.com/g/r/-/merge_requests/42"


def test_extract_mr_url_returns_none_when_nothing():
    assert extract_mr_url_from_text("纯文本，没有任何 URL") is None
    assert extract_mr_url_from_text("") is None
    assert extract_mr_url_from_text(None) is None  # type: ignore[arg-type]


# ---- 平台探测（纯函数） ----

def test_platform_from_remote_url_github():
    assert platform_from_remote_url("git@github.com:owner/repo.git") == "github"
    assert platform_from_remote_url("https://github.com/owner/repo.git") == "github"
    assert platform_from_remote_url("https://GITHUB.com/Owner/Repo") == "github"


def test_platform_from_remote_url_gitlab_and_selfhosted():
    # 非 github.com 一律按 gitlab（含自建 GitLab 的任意域名）
    assert platform_from_remote_url("git@gitlab.example.com:g/r.git") == "gitlab"
    assert platform_from_remote_url("https://git.example.com/g/r.git") == "gitlab"
    assert platform_from_remote_url("") == "gitlab"


def test_parse_github_owner_repo_ssh_and_https():
    assert parse_github_owner_repo("git@github.com:my-org/cc-fleet.git") == ("my-org", "cc-fleet")
    assert parse_github_owner_repo("https://github.com/my-org/cc-fleet.git") == ("my-org", "cc-fleet")
    assert parse_github_owner_repo("https://github.com/my-org/cc-fleet") == ("my-org", "cc-fleet")
    assert parse_github_owner_repo("ssh://git@github.com/my-org/cc-fleet.git") == ("my-org", "cc-fleet")


def test_parse_github_owner_repo_invalid_raises():
    with pytest.raises(ValueError):
        parse_github_owner_repo("not-a-url")


def test_github_api_base():
    assert github_api_base("github.com") == "https://api.github.com"
    assert github_api_base("ghe.example.com") == "https://ghe.example.com/api/v3"


def test_github_compare_url_github_com():
    assert (
        github_compare_url("github.com", "my-org", "cc-fleet", "main", "claude/x")
        == "https://github.com/my-org/cc-fleet/compare/main...claude/x?expand=1"
    )


def test_github_compare_url_enterprise_host():
    # 用真实 web host（不掺 api host），支持 GitHub Enterprise 自有域名
    assert (
        github_compare_url("ghe.example.com", "my-org", "cc-fleet", "main", "claude/x")
        == "https://ghe.example.com/my-org/cc-fleet/compare/main...claude/x?expand=1"
    )


# ---- GitHub PR URL 提取 ----

def test_pr_url_pattern_matches_github():
    m = PR_URL_PATTERN.search("see https://github.com/owner/repo/pull/42 done")
    assert m is not None and m.group(0) == "https://github.com/owner/repo/pull/42"


def test_extract_mr_url_handles_github_protocol_line():
    text = (
        "完成报告：建了 PR\n"
        "MR_URL: https://github.com/my-org/cc-fleet/pull/12\n"
    )
    assert extract_mr_url_from_text(text) == "https://github.com/my-org/cc-fleet/pull/12"


def test_extract_mr_url_falls_back_to_github_pull_url():
    text = "claude 忘了协议行，但输出里有 https://github.com/o/r/pull/7"
    assert extract_mr_url_from_text(text) == "https://github.com/o/r/pull/7"


# ---- create_pr_via_api / create_review_request ----

async def test_create_pr_via_api_missing_token_raises(monkeypatch):
    """缺 token 应立刻抛错且不触网、不 push。"""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(MrCreateError) as ei:
        await create_pr_via_api(
            worktree=mr_module.Path("/tmp"),
            source_branch="claude/x",
            target_branch="main",
            title="t",
            description="d",
        )
    assert "token" in str(ei.value).lower()


def _fake_run_factory(remote_url: str, push_rc: int = 0):
    async def _fake_run(cmd, cwd):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return 0, remote_url + "\n", ""
        if cmd[:2] == ["git", "push"]:
            return push_rc, "", ("push failed" if push_rc else "")
        return 0, "", ""
    return _fake_run


async def test_create_pr_via_api_success(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(mr_module, "_run", _fake_run_factory("git@github.com:o/r.git"))

    seen = {}

    async def fake_post(url, payload, token, timeout_sec):
        seen["url"] = url
        seen["payload"] = payload
        return 201, {"html_url": "https://github.com/o/r/pull/99"}

    monkeypatch.setattr(mr_module, "_github_post_json", fake_post)

    url = await create_pr_via_api(
        worktree=mr_module.Path("/tmp"),
        source_branch="claude/x",
        target_branch="main",
        title="加个功能",
        description="多行\n描述",
    )
    assert url == "https://github.com/o/r/pull/99"
    assert seen["url"] == "https://api.github.com/repos/o/r/pulls"
    # body 直接传原文，不像 GitLab 那样转义换行
    assert seen["payload"] == {"title": "加个功能", "head": "claude/x", "base": "main", "body": "多行\n描述"}


async def test_create_pr_via_api_reuses_existing_pr_on_422(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(mr_module, "_run", _fake_run_factory("https://github.com/o/r.git"))

    async def fake_post(url, payload, token, timeout_sec):
        return 422, {"message": "Validation Failed", "errors": [{"message": "A pull request already exists for o:claude/x."}]}

    async def fake_get(url, token, timeout_sec):
        assert "head=o:claude/x" in url
        return 200, [{"html_url": "https://github.com/o/r/pull/5"}]

    monkeypatch.setattr(mr_module, "_github_post_json", fake_post)
    monkeypatch.setattr(mr_module, "_github_get_json", fake_get)

    url = await create_pr_via_api(
        worktree=mr_module.Path("/tmp"),
        source_branch="claude/x",
        target_branch="main",
        title="t",
        description="d",
    )
    assert url == "https://github.com/o/r/pull/5"


async def test_create_pr_via_api_push_failure_raises(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(mr_module, "_run", _fake_run_factory("git@github.com:o/r.git", push_rc=1))
    with pytest.raises(MrCreateError) as ei:
        await create_pr_via_api(
            worktree=mr_module.Path("/tmp"),
            source_branch="claude/x",
            target_branch="main",
            title="t",
            description="d",
        )
    assert "push" in str(ei.value).lower()


async def test_create_pr_via_api_404_message_has_compare_url_and_permission_hint(monkeypatch):
    """push 成功后 PR 接口 404：错误文案要可操作——含权限提示 + compare URL + 原始状态码。"""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(mr_module, "_run", _fake_run_factory("git@github.com:o/r.git"))

    async def fake_post(url, payload, token, timeout_sec):
        return 404, {"message": "Not Found"}

    monkeypatch.setattr(mr_module, "_github_post_json", fake_post)

    with pytest.raises(MrCreateError) as ei:
        await create_pr_via_api(
            worktree=mr_module.Path("/tmp"),
            source_branch="claude/x",
            target_branch="main",
            title="t",
            description="d",
        )
    msg = str(ei.value)
    assert "https://github.com/o/r/compare/main...claude/x?expand=1" in msg
    assert "Pull requests" in msg  # 权限提示
    assert "404" in msg  # 保留原始状态码便于排查


async def test_create_pr_via_api_403_message_has_permission_hint(monkeypatch):
    """403 与 404 走同一友好分支（GitHub 对无权限仓库多返 404，少数返 403）。"""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(mr_module, "_run", _fake_run_factory("git@github.com:o/r.git"))

    async def fake_post(url, payload, token, timeout_sec):
        return 403, {"message": "Forbidden"}

    monkeypatch.setattr(mr_module, "_github_post_json", fake_post)

    with pytest.raises(MrCreateError) as ei:
        await create_pr_via_api(
            worktree=mr_module.Path("/tmp"),
            source_branch="claude/x",
            target_branch="main",
            title="t",
            description="d",
        )
    msg = str(ei.value)
    assert "Pull requests" in msg
    assert "https://github.com/o/r/compare/main...claude/x?expand=1" in msg
    assert "403" in msg


async def test_create_pr_via_api_404_compare_url_uses_enterprise_host(monkeypatch):
    """compare URL 用真实 host：GitHub Enterprise 自有域名要透传，不写死 github.com。"""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(mr_module, "_run", _fake_run_factory("git@ghe.example.com:o/r.git"))

    async def fake_post(url, payload, token, timeout_sec):
        return 404, {"message": "Not Found"}

    monkeypatch.setattr(mr_module, "_github_post_json", fake_post)

    with pytest.raises(MrCreateError) as ei:
        await create_pr_via_api(
            worktree=mr_module.Path("/tmp"),
            source_branch="claude/x",
            target_branch="main",
            title="t",
            description="d",
        )
    msg = str(ei.value)
    assert "https://ghe.example.com/o/r/compare/main...claude/x?expand=1" in msg
    assert "github.com" not in msg  # 不掺 github.com，纯企业域名


async def test_create_review_request_dispatches_by_platform(monkeypatch):
    calls = []

    async def fake_mr(**kw):
        calls.append("gitlab")
        return "https://gitlab/x/-/merge_requests/1"

    async def fake_pr(**kw):
        calls.append("github")
        return "https://github.com/x/y/pull/1"

    monkeypatch.setattr(mr_module, "create_mr_via_push", fake_mr)
    monkeypatch.setattr(mr_module, "create_pr_via_api", fake_pr)

    common = dict(
        worktree=mr_module.Path("/tmp"),
        source_branch="b",
        target_branch="main",
        title="t",
        description="d",
    )
    assert (await create_review_request(platform="github", **common)).endswith("/pull/1")
    assert (await create_review_request(platform="gitlab", **common)).endswith("/merge_requests/1")
    # 未知平台回退 gitlab
    assert (await create_review_request(platform="bitbucket", **common)).endswith("/merge_requests/1")
    assert calls == ["github", "gitlab", "gitlab"]
