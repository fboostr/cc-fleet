6. **按下方"MR 元数据规范"先在回复正文写好 MR_TITLE 与 MR_DESCRIPTION 协议块**，然后一次性 push 并创建 MR：
   ```
   git push -u origin claude/{display_slug} \
     -o merge_request.create \
     -o merge_request.target={default_branch} \
     -o "merge_request.title=<协议块里 MR_TITLE 的值>" \
     -o "merge_request.description=<协议块里 MR_DESCRIPTION 的值，真换行替换为字面量 \n>" \
     -o merge_request.remove_source_branch
   ```
   - **description 中的真换行必须替换为字面量 `\n`**，否则 GitLab 服务端会以 "push options must not have new line characters" 拒收（rc=128）
   - title 必须为单行
7. 从 git push 的 stderr 抓 MR URL（GitLab 会输出形如 `remote: ... /-/merge_requests/N`），**在最终回复末尾另起一行严格按以下格式输出**（这是主控解析 MR URL 的唯一锚点）：
   ```
   MR_URL: https://gitlab.example.com/group/repo/-/merge_requests/123
   ```