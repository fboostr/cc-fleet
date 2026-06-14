6. **按下方"MR 元数据规范"先在回复正文写好 MR_TITLE 与 MR_DESCRIPTION 协议块**，然后先普通 push、再创建 GitHub PR：
   ```
   git push -u origin claude/{display_slug}
   ```
   - ⚠️ **不要**带 `-o merge_request.*` 这类 push option——GitHub 不认这些选项，会整体拒收 push（典型报错 `remote rejected ... no voting servers succeeded`）
   - 然后**优先用 `gh` CLI** 创建 PR（远端若已装并 `gh auth login` 过）：
     ```
     gh pr create --base {default_branch} --head claude/{display_slug} --title "<协议块里 MR_TITLE 的值>" --body "<协议块里 MR_DESCRIPTION 的值，可含真换行>"
     ```
   - 远端没有 `gh` 时，改用 **GitHub REST API**：向 `https://api.github.com/repos/<owner>/<repo>/pulls` 发 POST（自建 GitHub Enterprise 用 `https://<host>/api/v3/repos/<owner>/<repo>/pulls`），请求头带 `Authorization: Bearer $GITHUB_TOKEN` 与 `Accept: application/vnd.github+json`，请求体含 title、head（=`claude/{display_slug}`）、base（=`{default_branch}`）、body 四个字段；从响应的 `html_url` 取 PR 链接
   - title 必须为单行；description（gh 的 `--body` / API 的 body）可含真换行，无需像 GitLab 那样转义
7. 从 `gh pr create`（或 REST 响应的 `html_url`）拿到 PR 链接，**在最终回复末尾另起一行严格按以下格式输出**（这是主控解析 PR URL 的唯一锚点，GitHub 也沿用 `MR_URL:` 前缀）：
   ```
   MR_URL: https://github.com/owner/repo/pull/123
   ```