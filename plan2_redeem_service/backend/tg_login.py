from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Optional

from .config import Settings
from .telegram_forwarder import (
    PlaywrightTelegramWebForwarder,
    TelegramForwardError,
    TelegramWarrantyForwarder,
    _resolve_entity,
    _safe_disconnect,
    resolve_tg_transport,
    sanitize_tg_error,
)


def _load_dotenv_file(env_path: Path) -> None:
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key not in os.environ or not os.environ[key].strip():
            os.environ[key] = value


def _verify_only(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.transport is not None:
        settings.tg_transport = args.transport.strip().lower() or "auto"
    if args.profile_dir is not None:
        settings.tg_web_profile_dir = args.profile_dir.strip()
    api_id = args.api_id if args.api_id is not None else settings.tg_api_id
    api_hash = (args.api_hash if args.api_hash is not None else settings.tg_api_hash).strip()
    session = settings.tg_session.strip()
    target = (args.target if args.target is not None else settings.tg_admin_target).strip()
    settings.tg_api_id = api_id
    settings.tg_api_hash = api_hash
    settings.tg_admin_target = target

    transport = resolve_tg_transport(settings)
    if transport == "web":
        profile_dir = settings.tg_web_profile_dir.strip()
        missing = []
        if not profile_dir:
            missing.append("PLAN2_TG_WEB_PROFILE_DIR")
        elif not Path(profile_dir).expanduser().is_dir():
            print(f"Telegram Web Profile 目录不存在: {profile_dir}", file=sys.stderr)
            return 1
        if not target:
            missing.append("PLAN2_TG_ADMIN_TARGET")
        if missing:
            print(f"配置不完整，缺少: {', '.join(missing)}", file=sys.stderr)
            return 1
        forwarder = PlaywrightTelegramWebForwarder(
            enabled=True,
            profile_dir=profile_dir,
            target=target,
            web_url=settings.tg_web_url,
            timeout_seconds=settings.tg_timeout_seconds,
        )
        try:
            result = forwarder.verify_connection()
        except TelegramForwardError as exc:
            print(f"Telegram Web 只读连接检查失败: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    missing = []
    if api_id <= 0:
        missing.append("PLAN2_TG_API_ID")
    if not api_hash:
        missing.append("PLAN2_TG_API_HASH")
    if not session:
        missing.append("PLAN2_TG_SESSION")
    if not target:
        missing.append("PLAN2_TG_ADMIN_TARGET")
    if missing:
        print(f"配置不完整，缺少: {', '.join(missing)}", file=sys.stderr)
        return 1

    forwarder = TelegramWarrantyForwarder(
        enabled=True,
        api_id=api_id,
        api_hash=api_hash,
        session=session,
        target=target,
        timeout_seconds=settings.tg_timeout_seconds,
    )
    try:
        result = forwarder.verify_connection()
    except TelegramForwardError as exc:
        print(f"Telegram 只读连接检查失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


async def _interactive_login_async(api_id: int, api_hash: str, target: str, timeout_seconds: float) -> tuple[str, dict[str, object]]:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        receive_updates=False,
        timeout=max(5, int(timeout_seconds)),
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            phone = input("请输入 Telegram 手机号（含国际区号，如 +86... / +84...）: ").strip()
            if not phone:
                raise TelegramForwardError("手机号不能为空")
            await client.send_code_request(phone)
            code = input("请输入 Telegram 登录验证码: ").strip()
            if not code:
                raise TelegramForwardError("验证码不能为空")
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                password = getpass.getpass("检测到两步验证，请输入云端密码（输入时不回显）: ")
                await client.sign_in(password=password)

        session_str = client.session.save()
        me = await client.get_me()
        summary: dict[str, object] = {
            "me_id": getattr(me, "id", None),
            "me_username": getattr(me, "username", None),
            "target": target,
            "target_resolved": False,
        }
        if target:
            entity = await _resolve_entity(client, target)
            summary.update(
                {
                    "target_resolved": True,
                    "target_id": getattr(entity, "id", None),
                    "target_type": type(entity).__name__,
                    "target_username": getattr(entity, "username", None),
                }
            )
        return session_str, summary
    finally:
        await _safe_disconnect(client)


def _interactive_login(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    api_id = args.api_id if args.api_id is not None else settings.tg_api_id
    if api_id <= 0:
        raw_id = input("请输入 Telegram API ID (my.telegram.org): ").strip()
        if not raw_id.isdigit():
            print("API ID 必须是正整数", file=sys.stderr)
            return 1
        api_id = int(raw_id)

    api_hash = (args.api_hash if args.api_hash is not None else settings.tg_api_hash).strip()
    if not api_hash:
        api_hash = getpass.getpass("请输入 Telegram API Hash（输入时不回显）: ").strip()
        if not api_hash:
            print("API Hash 不能为空", file=sys.stderr)
            return 1

    default_target = (args.target if args.target is not None else settings.tg_admin_target).strip()
    prompt_hint = f" [{default_target}]" if default_target else ""
    entered_target = input(f"请输入目标管理员 Peer ID 或 @username{prompt_hint}: ").strip()
    target = entered_target or default_target

    try:
        session_str, summary = asyncio.run(
            _interactive_login_async(api_id, api_hash, target, settings.tg_timeout_seconds)
        )
    except Exception as exc:
        print(f"Telegram 登录或校验失败: {sanitize_tg_error(exc, (api_hash,))}", file=sys.stderr)
        return 1

    print("\n=== 只读账号与目标校验结果（未发送任何消息） ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n=== 请将以下配置粘贴到生产或本地 .env（切勿提交到 Git、截屏或分享给他人） ===")
    print("PLAN2_TG_WARRANTY_ENABLED=true")
    print(f"PLAN2_TG_API_ID={api_id}")
    print(f"PLAN2_TG_API_HASH={api_hash}")
    print(f"PLAN2_TG_SESSION={session_str}")
    print(f"PLAN2_TG_ADMIN_TARGET={target}")
    print(f"PLAN2_TG_TIMEOUT_SECONDS={int(settings.tg_timeout_seconds)}")
    print("\n安全提醒：PLAN2_TG_SESSION 等同于该 Telegram 账号的登录态，请严格保密并限制 .env 权限（如 chmod 600）。")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan 2 Telegram 保修自动报备登录与只读校验工具"
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="仅使用当前环境变量 / .env 只读验证 Telegram 会话与目标实体解析，不发送任何消息",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="可选 .env 文件路径（默认读取当前目录下的 .env，不覆盖已显式设置的环境变量）",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "telethon", "web"),
        default=None,
        help="覆盖 PLAN2_TG_TRANSPORT（auto / telethon / web）",
    )
    parser.add_argument("--profile-dir", type=str, default=None, help="覆盖 PLAN2_TG_WEB_PROFILE_DIR")
    parser.add_argument("--api-id", type=int, default=None, help="覆盖 PLAN2_TG_API_ID")
    parser.add_argument("--api-hash", type=str, default=None, help="覆盖 PLAN2_TG_API_HASH")
    parser.add_argument("--target", type=str, default=None, help="覆盖 PLAN2_TG_ADMIN_TARGET")
    args = parser.parse_args(argv)

    if args.env_file:
        _load_dotenv_file(Path(args.env_file))

    if args.verify_only:
        return _verify_only(args)
    return _interactive_login(args)


if __name__ == "__main__":
    raise SystemExit(main())
