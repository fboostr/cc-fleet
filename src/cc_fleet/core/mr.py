"""MR / PR 提交：按代码托管平台分流，无需 glab / gh 之类 CLI。

- **GitLab**：`git push -o merge_request.create ...` 一气呵成（origin 指向 GitLab 实例时）。
- **GitHub**：先普通 `git push`，再调 GitHub REST API 建 PR。token 从环境变量
  `GITHUB_TOKEN` / `GH_TOKEN` 读（配在 `.env` 即可，启动时 `load_dotenv()` 会加载），
  不依赖 `gh` CLI。GitHub 不认 GitLab 的 `-o merge_request.*` push option，会整体
  拒收 push（`remote rejected ... no voting servers succeeded`），故必须分平台处理。

平台由 origin 的 remote URL 自动探测（见 `platform_from_remote_url`：含 `github.com`
→ github，其余一律 gitlab），调用方也可显式指定（见 `create_review_request`）。

实现要点：
- subprocess 拉 `git push`；GitLab 从 stderr 的 `remote: ...` 行抓 MR URL，GitHub 从
  REST 响应的 `html_url` 取 PR URL
- 失败指数退避重试 3 次（GitHub 侧仅对网络错误 / 5xx 重试，4xx 直接失败）
- 目标分支已有未关闭的 MR/PR 时：GitLab 复用并把 URL 写到 stderr（rc=0）；GitHub 返回
  422，本模块据此回查已存在的 open PR 并返回其 URL — 两侧都视为成功（幂等）
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class MrCreateError(RuntimeError):
    """git push 失败，或 push 成功但无法从输出 / API 响应提取 MR/PR URL。"""


# GitLab 在 push 完成后会在 stderr 输出形如：
#   remote: View merge request for feature/x:
#   remote:   https://gitlab.example.com/group/repo/-/merge_requests/123
MR_URL_PATTERN = re.compile(r"https?://[^\s]+/-/merge_requests/\d+")

# GitHub PR URL 形如 https://github.com/owner/repo/pull/123
PR_URL_PATTERN = re.compile(r"https?://[^\s]+/pull/\d+")

# MR(GitLab) 或 PR(GitHub) 的 URL 统称，供 mode=remote 从 claude 文本里抽取。
_REVIEW_URL_PATTERN = re.compile(r"https?://[^\s]+/(?:-/merge_requests|pull)/\d+")

# mode=remote 下 claude 在 dev 末尾按协议输出 `MR_URL: <url>`（GitHub 下同样用
# 该前缀输出 PR 链接）；优先用这条锚点行抓取，避免 dev 描述里随便提到的别的
# MR/PR URL 污染。
_REVIEW_URL_PROTOCOL_LINE = re.compile(
    r"^\s*MR_URL:\s*(https?://[^\s]+/(?:-/merge_requests|pull)/\d+)\s*$",
    re.MULTILINE,
)


def extract_mr_url_from_text(text: str) -> str | None:
    """从 claude 输出中抽 MR/PR URL：

    1. 优先匹配协议行 `MR_URL: <url>`（dev_protocol_remote.md 强制要求的格式）
    2. 兜底：在全文中找任意一条 GitLab `/-/merge_requests/N` 或 GitHub `/pull/N`
       URL，取第一条

    都没找到返回 None。
    """
    if not text:
        return None
    m = _REVIEW_URL_PROTOCOL_LINE.search(text)
    if m:
        return m.group(1)
    m2 = _REVIEW_URL_PATTERN.search(text)
    return m2.group(0) if m2 else None


# ---------- 平台探测（纯函数 + 一个 git 调用） ----------


def platform_from_remote_url(url: str) -> str:
    """从 remote URL 粗判代码托管平台。

    含 `github.com` → `"github"`；其余一律 `"gitlab"`（保持本项目 GitLab-first 默认，
    也兼容自建 GitLab 的任意域名）。注意：GitHub Enterprise 用自有域名，auto 探测识别
    不出，需在 repo 配置里显式写 `platform: github`。
    """
    return "github" if "github.com" in (url or "").lower() else "gitlab"


def _remote_host_and_path(url: str) -> tuple[str, str]:
    """把 git remote URL 拆成 `(host, "owner/repo")`，兼容两种写法：

    - scp-like SSH：`git@github.com:owner/repo(.git)`
    - URL 形式：`https://github.com/owner/repo(.git)`、`ssh://git@github.com/owner/repo`

    解析不出抛 ValueError。
    """
    s = (url or "").strip()
    if s.endswith(".git"):
        s = s[:-4]
    # scp-like（无 `://`，形如 [user@]host:path）
    if "://" not in s:
        m = re.match(r"^(?:[^@/]+@)?([^/:]+):(.+)$", s)
        if m:
            return m.group(1), m.group(2).lstrip("/")
    p = urlparse(s)
    if p.hostname and p.path.strip("/"):
        return p.hostname, p.path.lstrip("/")
    raise ValueError(f"无法解析 remote URL：{url!r}")


def parse_github_owner_repo(url: str) -> tuple[str, str]:
    """从 GitHub remote URL 解析 `(owner, repo)`，兼容 SSH / HTTPS 两种写法。"""
    _, path = _remote_host_and_path(url)
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"无法从 remote URL 解析 owner/repo：{url!r}")
    return parts[0], parts[1]


def github_api_base(host: str) -> str:
    """github.com → `https://api.github.com`；自建 GitHub Enterprise → `https://<host>/api/v3`。"""
    if host in ("github.com", "www.github.com"):
        return "https://api.github.com"
    return f"https://{host}/api/v3"


def github_compare_url(host: str, owner: str, repo: str, base: str, head: str) -> str:
    """拼 GitHub 网页版「比较并建 PR」URL，供 API 自动建 PR 失败时给用户手动兜底。

    形如 `https://<host>/<owner>/<repo>/compare/<base>...<head>?expand=1`。
    用真实 web host（github.com 或 GitHub Enterprise 自有域名），**不**走 `github_api_base`
    的 api host。分支名里的 `/`（如 `claude/xxx`）GitHub compare 路径原样接受，不做编码。
    """
    return f"https://{host}/{owner}/{repo}/compare/{base}...{head}?expand=1"


async def detect_remote_platform(repo_or_worktree: Path, remote: str = "origin") -> str:
    """跑 `git remote get-url <remote>` 并据 URL 判平台；读不到时回退 `"gitlab"`。"""
    rc, out, _ = await _run(["git", "remote", "get-url", remote], repo_or_worktree)
    if rc != 0:
        return "gitlab"
    return platform_from_remote_url(out.strip())


def build_push_cmd(
    *,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    remote: str = "origin",
) -> list[str]:
    """纯函数，便于单测。GitLab push option 文档：
    https://docs.gitlab.com/ee/user/project/push_options.html
    """
    # GitLab 服务端 push option 不接受真换行字符（rc=128, "push options must not
    # have new line characters"），但接受字面量 `\n` 序列，会在 MR description 里
    # 还原为换行。这里把 description 的真换行做转义；title 由调用方保证为单行。
    safe_description = description.replace("\n", "\\n")
    return [
        "git", "push", "-u", remote, source_branch,
        "-o", "merge_request.create",
        "-o", f"merge_request.target={target_branch}",
        "-o", f"merge_request.title={title}",
        "-o", f"merge_request.description={safe_description}",
        "-o", "merge_request.remove_source_branch",
    ]


async def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


async def create_mr_via_push(
    *,
    worktree: Path,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    remote: str = "origin",
    max_attempts: int = 3,
    backoff_base_sec: float = 2.0,
    timeout_sec: int = 300,
) -> str:
    """通过 GitLab push option 创建 MR，返回 MR URL；失败抛 MrCreateError。"""
    cmd = build_push_cmd(
        source_branch=source_branch,
        target_branch=target_branch,
        title=title,
        description=description,
        remote=remote,
    )

    last_err = ""
    for attempt in range(max_attempts):
        try:
            rc, out, err = await asyncio.wait_for(_run(cmd, worktree), timeout=timeout_sec)
        except asyncio.TimeoutError:
            last_err = f"git push 超时（{timeout_sec}s）"
            logger.warning("MR 第 %d 次尝试超时", attempt + 1)
        else:
            url_match = MR_URL_PATTERN.search(err) or MR_URL_PATTERN.search(out)
            if rc == 0 and url_match:
                return url_match.group(0)
            if rc == 0:
                last_err = (
                    "push 成功但未能从输出抽到 MR URL；可能 remote 不是 GitLab 或服务端禁用了 push option。\n"
                    f"stderr 末尾：{err[-500:]}"
                )
            else:
                last_err = err.strip() or out.strip() or f"退出码 {rc}"
                logger.warning("MR 第 %d 次尝试失败：%s", attempt + 1, last_err[:200])

        if attempt < max_attempts - 1:
            await asyncio.sleep(backoff_base_sec * (2**attempt))

    raise MrCreateError(last_err)


# ---------- GitHub PR（REST API） ----------


def _github_token(token: str | None = None) -> str | None:
    """优先用入参 token，否则读环境变量 `GITHUB_TOKEN` / `GH_TOKEN`（来自 `.env`）。"""
    return token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cc-fleet",
    }


async def _github_post_json(
    url: str, payload: dict, token: str, timeout_sec: int
) -> tuple[int, object]:
    """POST JSON，返回 `(status, 解析后的响应体)`。抽成模块级函数便于测试 monkeypatch。"""
    import aiohttp  # 延迟导入：仅 GitHub 路径需要，且 aiohttp 已是项目依赖（web server 在用）

    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=_github_headers(token)) as resp:
            return resp.status, await resp.json(content_type=None)


async def _github_get_json(url: str, token: str, timeout_sec: int) -> tuple[int, object]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=_github_headers(token)) as resp:
            return resp.status, await resp.json(content_type=None)


def _is_already_exists(data: object) -> bool:
    """识别 GitHub 422「A pull request already exists」错误体。"""
    if not isinstance(data, dict):
        return False
    if "already exist" in str(data.get("message", "")).lower():
        return True
    for e in data.get("errors") or []:
        if isinstance(e, dict) and "already exist" in str(e.get("message", "")).lower():
            return True
    return False


def _error_message(data: object) -> str:
    if isinstance(data, dict):
        msg = str(data.get("message", "")).strip()
        errs = data.get("errors")
        return f"{msg} {errs}".strip() if errs else (msg or str(data))
    return str(data)


async def _find_existing_pr_url(
    api_base: str, owner: str, repo: str, branch: str, token: str, timeout_sec: int
) -> str | None:
    """目标分支已有 open PR 时回查其 URL（对齐 GitLab「复用已有 MR」的幂等行为）。"""
    url = f"{api_base}/repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open"
    status, data = await _github_get_json(url, token, timeout_sec)
    if status == 200 and isinstance(data, list) and data:
        html_url = data[0].get("html_url") if isinstance(data[0], dict) else None
        return html_url
    return None


async def create_pr_via_api(
    *,
    worktree: Path,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    remote: str = "origin",
    token: str | None = None,
    max_attempts: int = 3,
    backoff_base_sec: float = 2.0,
    timeout_sec: int = 300,
) -> str:
    """先普通 push，再调 GitHub REST API 建 PR，返回 PR URL；失败抛 MrCreateError。"""
    tok = _github_token(token)
    if not tok:
        raise MrCreateError(
            "未找到 GitHub token：请在 .env 配置 GITHUB_TOKEN（或 GH_TOKEN）。"
            "该 token 需具备目标仓库的 PR 写权限（classic PAT 勾 repo，或 fine-grained "
            "授予 Pull requests: Read and write）。"
        )

    # 1. 先解析 owner/repo + api base（解析失败早抛，免得白 push）
    rc, out, err = await _run(["git", "remote", "get-url", remote], worktree)
    if rc != 0:
        raise MrCreateError(f"读取 remote {remote!r} URL 失败：{(err or out).strip()}")
    remote_url = out.strip()
    try:
        host, _ = _remote_host_and_path(remote_url)
        owner, repo = parse_github_owner_repo(remote_url)
    except ValueError as e:
        raise MrCreateError(str(e)) from e
    api_base = github_api_base(host)
    # push 成功后任何建 PR 失败，都给用户一个手动建 PR 的兜底入口（head=源分支、base=目标分支）
    compare_url = github_compare_url(host, owner, repo, target_branch, source_branch)

    # 2. 普通 push（**不带任何 push option**；GitHub 不认 GitLab 的 -o merge_request.*）
    prc, pout, perr = await _run(["git", "push", "-u", remote, source_branch], worktree)
    if prc != 0:
        raise MrCreateError(f"git push 失败：{(perr or pout).strip()}")

    # 3. 建 PR（网络错误 / 5xx 指数退避重试；4xx 直接失败）
    payload = {
        "title": title,
        "head": source_branch,
        "base": target_branch,
        "body": description,
    }
    url = f"{api_base}/repos/{owner}/{repo}/pulls"
    last_err = ""
    for attempt in range(max_attempts):
        try:
            status, data = await _github_post_json(url, payload, tok, timeout_sec)
        except Exception as e:  # noqa: BLE001 网络层错误，重试
            last_err = f"调用 GitHub API 失败：{e}"
            logger.warning("PR 第 %d 次尝试网络错误：%s", attempt + 1, last_err[:200])
        else:
            if status == 201 and isinstance(data, dict) and data.get("html_url"):
                return data["html_url"]
            if status == 422 and _is_already_exists(data):
                existing = await _find_existing_pr_url(
                    api_base, owner, repo, source_branch, tok, timeout_sec
                )
                if existing:
                    return existing
                last_err = "PR 已存在但无法回查其 URL"
                break  # 已存在却取不回，重试无意义
            if status in (403, 404):
                # 分支已 push 成功，PR 接口却 403/404：几乎都是 token 权限/可见性问题。
                # GitHub 对无权限仓库统一返 404（而非 403），故两者并列提示，并附手动建 PR 链接。
                last_err = (
                    f"GitHub API 返回 {status}：{_error_message(data)}。"
                    f"分支已成功 push，但自动建 PR 失败——通常是 GITHUB_TOKEN 没有 "
                    f"{owner}/{repo} 的 Pull requests:write 权限，或该仓库对当前 token "
                    f"不可见/不存在（GitHub 对无权限仓库统一返 404）。"
                    f"可用此链接手动建 PR：{compare_url}"
                )
            else:
                last_err = (
                    f"GitHub API 返回 {status}：{_error_message(data)}。"
                    f"分支已 push，可用此链接手动建 PR：{compare_url}"
                )
            logger.warning("PR 第 %d 次尝试失败：%s", attempt + 1, last_err[:200])
            if 400 <= status < 500:
                break  # 客户端错误（鉴权 / 校验失败等）重试无意义

        if attempt < max_attempts - 1:
            await asyncio.sleep(backoff_base_sec * (2**attempt))

    raise MrCreateError(last_err)


async def create_review_request(
    *,
    platform: str,
    worktree: Path,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    remote: str = "origin",
) -> str:
    """按平台分发并返回 MR/PR URL：

    - `"github"` → `create_pr_via_api`（push + REST API 建 PR）
    - 其它（默认 gitlab）→ `create_mr_via_push`（push option 建 MR）

    平台差异收敛在本模块内，session 只调本函数。
    """
    if platform == "github":
        return await create_pr_via_api(
            worktree=worktree,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=description,
            remote=remote,
        )
    return await create_mr_via_push(
        worktree=worktree,
        source_branch=source_branch,
        target_branch=target_branch,
        title=title,
        description=description,
        remote=remote,
    )
