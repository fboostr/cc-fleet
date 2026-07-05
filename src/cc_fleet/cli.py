"""命令行入口：

- `cc-fleet run` 启动主进程
- `cc-fleet sessions list/cancel/logs` 管理 session
- `cc-fleet wechat-login` 个人微信(ilink)扫码登录，获取 bot_token

凭据从 `.env` 读，配置从 `--config` 指定的 YAML 读（默认 `./config.yaml`）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .app import run_app
from .config.loader import load_config
from .config.schema import AppConfig
from .storage.db import Database


def _load(args: argparse.Namespace) -> AppConfig:
    return load_config(args.config)


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = _load(args)
    errs = cfg.validate_runtime()
    if errs:
        print("配置校验失败，主进程未启动：", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 2
    for r in cfg.repos:
        if r.mode == "remote":
            print(
                f"[config] repo {r.name} mode=remote → "
                f"ssh={r.remote_ssh_alias} remote_repo={r.remote_repo_path}",
                file=sys.stderr,
            )
        else:
            print(f"[config] repo {r.name} mode=local → path={r.path}", file=sys.stderr)
    run_app(cfg)
    return 0


async def _list_sessions(cfg: AppConfig) -> None:
    db = Database(cfg.db_path)
    await db.connect()
    try:
        rows = await db.list_sessions()
        if not rows:
            print("（暂无 session）")
            return
        print(f"{'slug':30} {'display':25} {'repo':12} {'state':18} {'mr_url'}")
        for r in rows:
            print(
                f"{r['slug']:30} {(r['display_slug'] or '-'):25} {r['repo']:12} "
                f"{r['state']:18} {r['mr_url'] or '-'}"
            )
    finally:
        await db.close()


def _cmd_sessions_list(args: argparse.Namespace) -> int:
    cfg = _load(args)
    asyncio.run(_list_sessions(cfg))
    return 0


async def _cancel(cfg: AppConfig, slug: str) -> bool:
    from .core.session_manager import SessionManager
    db = Database(cfg.db_path)
    await db.connect()
    try:
        async def _noop_reply(_chatid: str, _text: str) -> None:
            return None
        mgr = SessionManager(db, cfg, _noop_reply)
        return await mgr.cancel(slug)
    finally:
        await db.close()


def _cmd_sessions_cancel(args: argparse.Namespace) -> int:
    cfg = _load(args)
    ok = asyncio.run(_cancel(cfg, args.slug))
    if ok:
        print(f"已取消 {args.slug}")
        return 0
    print(f"未找到处于活跃状态的 session {args.slug}", file=sys.stderr)
    return 1


def _cmd_sessions_logs(args: argparse.Namespace) -> int:
    cfg = _load(args)
    sess_dir = (cfg.workspace_root / "sessions" / args.slug).expanduser()
    # 默认打印人类可读的 session.log（去噪 + 工具输入/返回 + 阶段流转 + 失败判决）；
    # --raw 回退看原始 stream.jsonl（含全部流式事件，供机读/深挖）。
    fname = "stream.jsonl" if args.raw else "session.log"
    p = sess_dir / fname
    if not p.exists():
        hint = "" if args.raw else "（旧 session 可能尚无 session.log，可加 --raw 看原始 stream.jsonl）"
        print(f"未找到日志：{p}{hint}", file=sys.stderr)
        return 1
    lines = Path(p).read_text(encoding="utf-8").splitlines()
    if args.tail and args.tail > 0:
        lines = lines[-args.tail :]
    for line in lines:
        print(line)
    return 0


def _dig(d: dict, *keys: str) -> str:
    """从 dict 里按多个候选 key 取第一个非空值（容忍服务端字段命名差异）。"""
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return ""


async def _wechat_login(base_url: str | None) -> int:
    """ilink 扫码登录：取二维码 → 轮询确认 → 打印 bot_token。无需 config.yaml。"""
    import base64
    import os
    import tempfile
    import webbrowser

    from .bot.wechat.ilink_client import DEFAULT_BASE_URL, IlinkClient

    base_url = base_url or DEFAULT_BASE_URL
    client = IlinkClient(base_url=base_url)
    try:
        qr = await client.get_bot_qrcode()
        qrcode = qr.get("qrcode", "") or ""
        if not qrcode:
            print("获取二维码失败，响应缺少 qrcode 字段：", qr, file=sys.stderr)
            return 1

        # qrcode_img_content 实测是一个 URL（如 https://liteapp.weixin.qq.com/q/...），
        # 打开它即可看到二维码；不是 base64 图片。兼容老格式（data:/裸 base64）做兜底。
        img = (qr.get("qrcode_img_content", "") or "").strip()
        if img.startswith("http"):
            print(f"扫码链接：{img}")
            print("→ 在手机上用「要绑定的微信」扫该二维码；或在浏览器打开此链接后扫。")
            try:
                webbrowser.open(img)
            except Exception:  # noqa: BLE001
                pass
        elif img:
            if img.startswith("data:") and "," in img:
                img = img.split(",", 1)[1]
            try:
                fd, path = tempfile.mkstemp(prefix="cc-fleet-wechat-qr-", suffix=".png")
                with os.fdopen(fd, "wb") as f:
                    f.write(base64.b64decode(img))
                print(f"二维码图片已保存：{path}（打开后用要绑定的微信扫码）")
            except Exception as e:  # noqa: BLE001
                print(f"二维码图片解码失败（忽略）：{e}", file=sys.stderr)
        print(f"二维码内容串(qrcode id)：{qrcode}")
        print("等待扫码确认中……（最长 ~5 分钟，Ctrl-C 取消）")

        last_status = ""
        for _ in range(150):  # 150 * 2s ≈ 5 分钟
            await asyncio.sleep(2.0)
            st = await client.get_qrcode_status(qrcode)
            token = _dig(st, "bot_token", "token")
            if token:
                baseurl = _dig(st, "baseurl", "base_url") or base_url
                print("\n✓ 登录成功！")
                print(f"  bot_token = {token}")
                print(f"  baseurl   = {baseurl}")
                print("\n把它写进 .env：")
                print(f"  WECHAT_BOT_TOKEN={token}")
                if baseurl.rstrip("/") != base_url.rstrip("/"):
                    print(
                        f"注意：服务端返回的 baseurl 与默认不同，"
                        f"请在 config.yaml 的 wechat.base_url 填 {baseurl}"
                    )
                return 0
            status = (st.get("status") or "").lower()
            if status and status != last_status:
                print(f"  状态：{status}")
                last_status = status
            if status in ("expired", "canceled", "cancelled"):
                print(f"二维码已失效/取消（status={status}），请重试。", file=sys.stderr)
                return 1
        print("超时未确认，请重试。", file=sys.stderr)
        return 1
    finally:
        await client.close()


def _cmd_wechat_login(args: argparse.Namespace) -> int:
    return asyncio.run(_wechat_login(args.base_url))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cc-fleet")
    p.add_argument("--config", default="config.yaml", help="配置文件路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="启动主进程")
    run.set_defaults(func=_cmd_run)

    sessions = sub.add_parser("sessions", help="session 管理")
    sessions_sub = sessions.add_subparsers(dest="subcmd", required=True)

    ls = sessions_sub.add_parser("list", help="列出所有 session")
    ls.set_defaults(func=_cmd_sessions_list)

    cancel = sessions_sub.add_parser("cancel", help="取消 session")
    cancel.add_argument("slug")
    cancel.set_defaults(func=_cmd_sessions_cancel)

    logs = sessions_sub.add_parser(
        "logs", help="查看 session 可读运行日志（--raw 看原始 stream.jsonl）"
    )
    logs.add_argument("slug")
    logs.add_argument(
        "--raw", action="store_true", help="打印原始 stream.jsonl 而非可读 session.log"
    )
    logs.add_argument("--tail", type=int, default=0, help="只打印末尾 N 行（默认全文）")
    logs.set_defaults(func=_cmd_sessions_logs)

    wechat_login = sub.add_parser(
        "wechat-login", help="个人微信(ilink)扫码登录，获取 bot_token"
    )
    wechat_login.add_argument(
        "--base-url", default=None, help="ilink 端点（默认官方 ilinkai.weixin.qq.com）"
    )
    wechat_login.set_defaults(func=_cmd_wechat_login)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    sys.exit(rc if rc is not None else 0)
