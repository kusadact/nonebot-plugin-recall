from __future__ import annotations

from typing import Optional

from nonebot import get_driver, logger, on_type
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupRecallNoticeEvent,
    Message,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.plugin import PluginMetadata

from .config import Config

try:
    from nonebot import get_plugin_config
except ImportError:
    get_plugin_config = None

__plugin_meta__ = PluginMetadata(
    name="群防撤回",
    description="在白名单群里提示被撤回的文本/图片/语音内容。",
    usage=(
        "1. 在配置中设置 recall_group_whitelist\n"
        "2. 加载插件后，监听群撤回事件\n"
        "3. 输出: 用户名撤回了一条消息/语音"
    ),
    type="application",
    homepage="https://github.com/nonebot/nonebot2",
    supported_adapters={"~onebot.v11"},
)

if get_plugin_config is not None:
    plugin_config = get_plugin_config(Config)
else:
    driver_config = get_driver().config
    if hasattr(driver_config, "model_dump"):
        plugin_config = Config.parse_obj(driver_config.model_dump())
    else:
        plugin_config = Config.parse_obj(driver_config.dict())


def _in_whitelist(group_id: int) -> bool:
    return group_id in plugin_config.recall_group_whitelist


def _build_non_voice_message(message: Message) -> Message:
    """Build message content while keeping real image segments."""
    content = Message()

    for segment in message:
        segment_type = segment.type
        if segment_type in {"at", "reply", "record"}:
            continue

        if segment_type == "text":
            text = str(segment.data.get("text", ""))
            if text:
                content.append(MessageSegment.text(text))
            continue

        if segment_type == "image":
            content.append(segment)
            continue

        if segment_type == "face":
            # Unsupported extended faces (e.g. faceType=3) are rendered as text placeholder.
            if _is_unsupported_face(segment):
                content.append(MessageSegment.text(_unsupported_face_placeholder(segment)))
                continue
            content.append(segment)
        elif segment_type == "video":
            content.append(MessageSegment.text("[视频]"))
        elif segment_type == "file":
            content.append(MessageSegment.text("[文件]"))
        else:
            content.append(MessageSegment.text(f"[{segment_type}]"))

    return content


def _is_unsupported_face(segment: MessageSegment) -> bool:
    if segment.type != "face":
        return False

    raw = segment.data.get("raw")
    if not isinstance(raw, dict):
        return False

    face_type = raw.get("faceType")
    try:
        return int(face_type) == 3
    except Exception:
        return False


def _unsupported_face_placeholder(segment: MessageSegment) -> str:
    raw = segment.data.get("raw")
    if isinstance(raw, dict):
        face_text = str(raw.get("faceText", "")).strip()
        if face_text:
            return f"[表情:{face_text}]"
    face_id = segment.data.get("id")
    if face_id is not None:
        return f"[表情:{face_id}]"
    return "[表情]"


async def _get_member_name(bot: Bot, group_id: int, user_id: int) -> str:
    try:
        member = await bot.get_group_member_info(
            group_id=group_id,
            user_id=user_id,
            no_cache=True,
        )
    except ActionFailed:
        return str(user_id)

    card = str(member.get("card") or "").strip()
    nickname = str(member.get("nickname") or "").strip()
    return card or nickname or str(user_id)


async def _get_recalled_message(bot: Bot, message_id: int) -> Optional[Message]:
    try:
        data = await bot.get_msg(message_id=message_id)
    except ActionFailed:
        return None

    raw_message = data.get("message", "")
    try:
        return raw_message if isinstance(raw_message, Message) else Message(raw_message)
    except Exception:
        logger.warning(f"nonebot-plugin-recall: failed to parse recalled message: {raw_message!r}")
        return None


# 缓存群消息，用于撤回后尽量恢复内容。
_message_cache: dict[tuple[int, int], Message] = {}
_cache_max_size = 5000

cache_group_message = on_type(GroupMessageEvent, priority=999, block=False)


@cache_group_message.handle()
async def _(event: GroupMessageEvent) -> None:
    if not _in_whitelist(event.group_id):
        return

    key = (event.group_id, int(event.message_id))
    _message_cache[key] = Message(event.get_message())

    if len(_message_cache) > _cache_max_size:
        # 删除最旧的一条，避免内存无限增长。
        oldest = next(iter(_message_cache))
        _message_cache.pop(oldest, None)


group_recall = on_type(GroupRecallNoticeEvent, priority=5, block=False)


@group_recall.handle()
async def _(bot: Bot, event: GroupRecallNoticeEvent) -> None:
    if not _in_whitelist(event.group_id):
        return

    # 只处理“自己撤回自己的消息”。
    if int(event.user_id) != int(event.operator_id):
        return

    name = await _get_member_name(bot, event.group_id, event.user_id)
    cache_key = (event.group_id, int(event.message_id))
    recalled = _message_cache.pop(cache_key, None)
    if recalled is None:
        recalled = await _get_recalled_message(bot, int(event.message_id))

    if recalled is None:
        await bot.send_group_msg(
            group_id=event.group_id,
            message=f"{name}撤回了一条消息：[内容未捕获]",
        )
        return

    voice_segments = [segment for segment in recalled if segment.type == "record"]
    if voice_segments:
        await bot.send_group_msg(
            group_id=event.group_id,
            message=f"{name}撤回了一条语音",
        )
        for segment in voice_segments:
            voice_message = Message()
            voice_message.append(segment)
            await bot.send_group_msg(group_id=event.group_id, message=voice_message)
        return

    content = _build_non_voice_message(recalled)
    if not content:
        await bot.send_group_msg(
            group_id=event.group_id,
            message=f"{name}撤回了一条消息：[空消息]",
        )
        return

    out = Message(f"{name}撤回了一条消息：")
    for segment in content:
        out.append(segment)
    await bot.send_group_msg(group_id=event.group_id, message=out)
