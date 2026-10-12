# 代码审查记录与修复

审查对象：`main` @ `55b4225`（2026-10-11，悉尼时间）。共 8 个可复现问题：2 个 P1、6 个 P2，全部已修复。每个修复都带一个回归测试：修复前失败、修复后通过；测试只用临时目录和 mock，不联网。

| # | 级别 | 问题 | 修复提交 | 回归测试 |
|---|---|---|---|---|
| 1 | P1 | bash 选项参数绕过检查，可写工作区外 | `86b44be` | `tests/test_tools.py::test_bash_option_smuggling_blocked`、`test_bash_safe_options_allowed` |
| 2 | P1 | 简报 HTML 未清理，昵称造成存储型 XSS | `1dddccb` | `tests/test_web.py::test_nickname_xss_is_sanitized`、`test_render_markdown_allowlist` |
| 3 | P2 | 同日旧文件让没写简报的运行被判成功、重复发邮件 | `471e2d1` | `tests/test_web.py::test_stale_same_day_file_does_not_mark_run_ok` |
| 4 | P2 | 同日多次运行共用一个文件，历史正文与 trace 对不上 | `471e2d1` | `tests/test_web.py::test_same_day_runs_keep_their_own_body` |
| 5 | P2 | 非法推送时间先入库，下次启动整个服务失败 | `36d0033` | `tests/test_web.py::test_invalid_send_time_rejected_and_startup_survives` |
| 6 | P2 | bash basename 与 search_content 遍历不检查软链 | `9089d58` | `tests/test_tools.py::test_symlink_basename_and_search_blocked` |
| 7 | P2 | 假模型模块级 `_cache`，并发用户串用搜索结果 | `3979362` | `tests/test_agent.py::test_demo_script_no_cross_user_cache` |
| 8 | P2 | `days` 只对 HN 生效，RSS/arXiv 返回旧文，Tavily 写死 2 天 | `73f15fa` | `tests/test_tools.py::test_days_window_applies_to_rss_arxiv_tavily` |

## 1. [P1] bash 选项参数绕过检查
- **位置**：`app/tools/fs_tools.py` 的 `bash`。
- **问题**：以 `-` 开头的参数直接跳过检查，路径可以藏在选项里；白名单里的 `sort` 有写文件能力（`-o`）。
- **复现**：`sort -o../outside.txt input.txt` 返回 `[exit 0]`，在工作区父目录生成了文件。
- **建议**：每个命令单独设允许的选项；禁用输出/执行类选项；对所有文件参数做边界检查。
- **修复**：新增 `BASH_SPECS`，每个命令有无参选项字母白名单和带值选项表（值按类型校验，比如 `-n` 必须是数字）；长选项一律拒绝；`find` 单独解析，只允许 `-name/-type/-maxdepth/-newer` 等测试项，`-exec/-delete/-fprint` 全拒；`date` 只接受 `+FORMAT`；移除能写文件的 `sort`、`uniq`。

## 2. [P1] 存储型 XSS
- **位置**：`app/main.py` 的简报页，`app/templates/brief.html` 的 `html|safe`。
- **问题**：`markdown.markdown()` 保留原始 HTML，模板又用 `|safe`；假模型把昵称直接拼进标题。
- **复现**：昵称设为带 `onerror` 的 `<img>`，简报页响应正文里出现未转义的标签。
- **建议**：Markdown 转 HTML 之后再用严格白名单清理；只转义昵称不够。
- **修复**：新增 `render_markdown()`：`markdown` → `nh3.clean()`，只留基本排版标签，`a` 只留 `href/title`，URL 只允许 http/https/mailto，加 `rel="noopener noreferrer nofollow"`。依赖加到 `requirements.txt`。

## 3 + 4. [P2] 同日运行：成功判定不可靠、正文共用
- **位置**：`app/service.py` 的 `run_for_user`。
- **问题**：成功条件只看“文件存在 + 模型正常结束”，旧文件也算；同一天所有记录指向同一个 `briefs/YYYY-MM-DD.md`，后一次会覆盖前一次的正文。
- **复现**：第一次写入简报，第二次模型直接结束：状态仍是 `ok`，邮件发了两次。两次成功运行的 `path` 相同，读第一条得到第二次的正文。
- **建议**：每次运行用独立的输出位置，确认本次写入成功后再提交。
- **修复**：每次运行生成 `run_id`，Agent 的目标文件是 `runs/<日期>/<run_id>.md`（通过 `Agent.run(output=...)` 注入到 system prompt）。只有同时满足以下条件才算成功：模型正常结束、trace 里有成功写这个路径的 `write_file`、文件非空、修改时间在本次开始之后。成功后才发邮件，并复制到 `briefs/<日期>.md` 给后续去重。`Brief.path` 指向不可变的运行文件。

## 5. [P2] 非法推送时间导致服务起不来
- **位置**：`app/main.py` 的 `save_prefs`，`app/scheduler.py` 的 `start`。
- **问题**：只截取 `send_time[:5]` 就提交，之后才交给 APScheduler 验证。
- **复现**：POST `send_time=25:00` 返回 500，但值已经入库；下次 `scheduler.start()` 抛 `ValueError`，lifespan 失败。
- **修复**：写库前用正则校验 `HH:MM`（00–23 / 00–59），不合法返回 422；`start()` 里逐个用户 try，坏配置跳过并记日志。

## 6. [P2] 软链检查遗漏
- **位置**：`app/tools/fs_tools.py` 的 `bash`、`search_content`。
- **问题**：bash 只解析带 `/` 或 `..` 的参数，`cat link.txt` 会跟着软链读外部文件；`search_content` 不检查遍历到的每个文件。
- **复现**：工作区放一个指向外部文件的 `link.txt`，`cat link.txt` 和 `search_content` 都能读到外部内容。
- **修复**：每个文件操作数都 `sb.resolve()`（会展开软链）；`grep -R`（跟随软链）移出白名单；`search_content` 对每个遍历到的文件检查真实路径，跳过指向工作区外的。

## 7. [P2] 假模型全局缓存
- **位置**：`app/llm/demo.py`。
- **问题**：模块级 `_cache` 被所有实例共享，A 查重期间 B 的搜索会覆盖它。
- **复现**：按 A 搜索 → B 搜索 → A 查重的顺序交错，A 的简报里出现了 B 的链接。
- **修复**：删掉全局状态，写简报时从本次运行自己的消息历史里取 `web_search` 的结果。

## 8. [P2] 回溯窗口只对 HN 生效
- **位置**：`app/tools/news_tools.py`。
- **问题**：RSS、arXiv 不接收 `days`，Tavily 写死 `days: 2`。
- **复现**：mock 一篇 2001 年的文章，`days=1` 时 RSS 和 arXiv 仍然返回它。
- **修复**：`days` 传给所有来源；RSS/arXiv 用 feedparser 的 `published_parsed/updated_parsed` 过滤，Tavily 请求带调用方的 `days`，并按 `published_date` 再过滤一遍。**发布日期未知的条目保留**，标成 `date=unknown`，由模型判断（宁可多给，不要静默丢掉）。工具 schema 的描述也同步改了。

## 仍未完全解决
- bash 仍然是应用层沙箱：检查和执行之间如果有别的进程替换软链（TOCTOU），只有 OS 级隔离（容器、nsjail、只读挂载）能根治。
- 文档里已经写明的无鉴权、SSRF、单进程调度等局限，这次没有处理。
