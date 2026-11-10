"""雨云游戏云 (RGS) 管理插件

API 文档: https://api.rainyun.com/
鉴权: 请求头 X-Api-Key
"""

import asyncio
import json
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 兼容不同版本
    from astrbot.api import AstrBotConfig
except ImportError:  # pragma: no cover
    from astrbot.core.config.astrbot_config import AstrBotConfig

DEFAULT_BASE_URL = "https://api.v2.rainyun.com"

STATUS_INFO = {
    "running": ("运行中", "🟢"),
    "stopped": ("已关机", "⚪"),
    "starting": ("启动中", "🟡"),
    "stopping": ("关闭中", "🟡"),
    "rebooting": ("重启中", "🟡"),
    "installing": ("安装中", "🟡"),
    "reinstalling": ("重装中", "🟡"),
    "suspended": ("已冻结", "🔴"),
    "expired": ("已过期", "🔴"),
    "unknown": ("未知", "❔"),
}

POWER_CMD = {
    "start": ("开机", "start", "running"),
    "stop": ("关机", "stop", "stopped"),
    "reboot": ("重启", "reboot", None),
}


def _fmt_ts(ts) -> str:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return "未知"
    if ts <= 0:
        return "未知"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _days_left(ts) -> str:
    try:
        days = (int(ts) - int(time.time())) / 86400
    except (TypeError, ValueError):
        return ""
    if days < 0:
        return "已过期"
    if days < 1:
        return "不足1天"
    return f"剩 {int(days)} 天"


def _remaining_days(ts):
    """返回剩余天数（float）；无法解析时返回 None。负数表示已过期。"""
    try:
        if int(ts) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    return (int(ts) - int(time.time())) / 86400


def _status_of(inst: dict):
    return STATUS_INFO.get(str(inst.get("Status", "")).lower(), STATUS_INFO["unknown"])


def _game_name(inst: dict) -> str:
    info = inst.get("GameInfo")
    if isinstance(info, dict):
        for k in ("Name", "name", "GameName", "Title"):
            if info.get(k):
                return str(info[k])
    elif isinstance(info, str) and info.strip():
        return info.strip()[:30]
    egg = inst.get("EggType")
    return str(egg) if isinstance(egg, str) and egg else ""


def _address(inst: dict) -> str:
    for key in ("MainIPv4", "NatPublicDomain", "NATSpareDomain", "NatPublicIP"):
        val = str(inst.get(key) or "").strip()
        if val:
            return val
    return ""


def _spec(inst: dict) -> str:
    cpu = inst.get("CPU")
    mem = inst.get("Memory")
    disk = inst.get("BaseDisk")
    parts = []
    if cpu:
        parts.append(f"{cpu}核")
    if mem:
        parts.append(f"{mem}G")
    if disk:
        parts.append(f"系统盘{disk}G")
    return " ".join(parts) if parts else "未知配置"


def _err_text(data) -> str:
    if isinstance(data, dict):
        msg = data.get("message") or data.get("msg") or ""
        code = data.get("code")
        if "找不到请求的对象" in str(msg):
            return "雨云返回：找不到该实例（可能 ID 错误，或实例未开机时无面板数据）"
        if msg:
            return f"雨云返回错误：{msg}（code={code}）"
    return f"雨云返回异常：{str(data)[:200]}"


@register(
    "astrbot_plugin_rainyun_rgs",
    "CatGirl",
    "雨云游戏云(RGS)实例查询与开关机管理，基于雨云开放 API。",
    "v1.0.0",
)
class RainyunRGSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.api_key = str(config.get("api_key", "") or "").strip()
        self.base_url = str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        # 自动积分续费运行时状态
        self._renew_lock = asyncio.Lock()
        self._renew_task: asyncio.Task | None = None
        self._last_attempt: dict[int, float] = {}
        self._last_check_at = 0.0
        self._last_result = "尚未检查"
        if self.api_key:
            masked = self.api_key[:4] + "****" + self.api_key[-4:] if len(self.api_key) > 8 else "****"
            logger.info(f"雨云游戏云插件已加载，密钥 {masked}，地址 {self.base_url}")
        else:
            logger.warning("雨云游戏云插件：未配置 API Key，功能不可用")

    # ---------------- 基础 ----------------
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        if not self.config.get("admin_only", True):
            return True
        try:
            admins = self.context.get_config().admins_id or []
        except Exception as exc:  # pragma: no cover
            logger.warning(f"读取管理员列表失败: {exc}")
            return False
        return str(event.get_sender_id()) in [str(a) for a in admins]

    async def _api(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ):
        """返回 (ok, data)：ok=True 时 data 为 data 字段内容。"""
        if not self.api_key:
            return False, {"message": "插件未配置雨云 API 密钥，请先在插件配置中填写。"}
        url = f"{self.base_url}{path}"
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json"}
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method, url, params=params, headers=headers, json=json_body
                ) as resp:
                    text = await resp.text()
                    try:
                        payload = json.loads(text)
                    except ValueError:
                        return False, {"message": f"HTTP {resp.status}: {text[:200]}"}
                    if isinstance(payload, dict) and payload.get("code") == 200:
                        return True, payload.get("data")
                    return False, payload
        except Exception as exc:
            logger.error(f"雨云 API 请求失败 {method} {path}: {exc}")
            return False, {"message": f"请求失败: {exc}"}

    async def _list_instances(self):
        params = {"options": json.dumps({"page": 1, "page_size": 100})}
        ok, data = await self._api("GET", "/product/rgs/", params)
        if not ok:
            return None, _err_text(data)
        records = (data or {}).get("Records") or []
        return records, None

    async def _get_instance(self, rgs_id: int):
        ok, data = await self._api("GET", f"/product/rgs/{int(rgs_id)}/")
        if not ok:
            return None, _err_text(data)
        inst = (data or {}).get("Data") if isinstance(data, dict) else None
        if not inst:
            return None, "未获取到实例详情喵"
        return inst, None

    async def _resolve_id(self, raw: str):
        """把用户输入解析成实例 ID。返回 (rgs_id, err)。"""
        raw = (raw or "").strip().lstrip("#").strip()
        if raw.isdigit():
            return int(raw), None

        default = str(self.config.get("default_rgs_id", "") or "").strip().lstrip("#")
        if default.isdigit():
            return int(default), None

        records, err = await self._list_instances()
        if records is None:
            return None, err
        if not records:
            return None, "账号下没有游戏云实例喵"
        if len(records) == 1:
            return int(records[0]["ID"]), None
        ids = "、".join(f"#{r['ID']}" for r in records)
        return None, f"你有 {len(records)} 个实例，请指定 ID：{ids}（示例：/雨云详情 89403）"

    # ---------------- 积分续费 ----------------
    def _cfg_num(self, key: str, default) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    @property
    def renew_threshold(self) -> float:
        """剩余天数低于等于该值时触发自动续费。"""
        return self._cfg_num("renew_threshold_days", 7)

    @property
    def renew_primary_days(self) -> int:
        return 30 if self._cfg_num("renew_primary_days", 30) >= 20 else 7

    @property
    def renew_fallback_days(self) -> int:
        return 7 if self._cfg_num("renew_fallback_days", 7) < 20 else 30

    def _renew_targets(self) -> list[int]:
        raw = str(self.config.get("renew_instance_ids", "") or "").replace("，", ",")
        ids: list[int] = []
        for part in raw.split(","):
            part = part.strip().lstrip("#")
            if part.isdigit():
                ids.append(int(part))
        if ids:
            return ids
        default = str(self.config.get("default_rgs_id", "") or "").strip().lstrip("#")
        return [int(default)] if default.isdigit() else []

    async def _renew_instance(self, rgs_id: int, duration_day: int):
        """调用雨云积分续费接口，duration_day 仅支持 7 或 30。"""
        body = {
            "duration_day": int(duration_day),
            "product_id": int(rgs_id),
            "product_type": "rgs",
        }
        ok, data = await self._api(
            "POST", f"/product/rgs/{int(rgs_id)}/renew/", json_body=body
        )
        if ok:
            return True, ""
        return False, _err_text(data)

    async def _renew_one(self, rgs_id: int, force: bool = False) -> str:
        """单个实例：先试续 30 天，失败退回 7 天。"""
        inst, err = await self._get_instance(rgs_id)
        if inst is None:
            return f"#{rgs_id} ❌ 读取实例失败：{err}"

        days = _remaining_days(inst.get("ExpDate"))
        if days is None:
            return f"#{rgs_id} ⚠️ 到期时间未知，跳过"
        if days < 0:
            return f"#{rgs_id} ⚠️ 已过期 {abs(int(days))} 天，积分续费需在有效期内，跳过"
        if not force and days > self.renew_threshold:
            return f"#{rgs_id} 剩 {int(days)} 天，未到 {self.renew_threshold:g} 天阈值，不续费"

        cooldown = self._cfg_num("renew_cooldown_hours", 6) * 3600
        waited = time.time() - self._last_attempt.get(rgs_id, 0.0)
        if not force and waited < cooldown:
            return f"#{rgs_id} 上次尝试未过 {(cooldown - waited) / 3600:.1f} 小时，冷却中"

        self._last_attempt[rgs_id] = time.time()
        primary = self.renew_primary_days
        fallback = self.renew_fallback_days

        ok, detail = await self._renew_instance(rgs_id, primary)
        if ok:
            after, _ = await self._get_instance(rgs_id)
            tail = f"，新到期 {_fmt_ts(after.get('ExpDate'))}" if after else ""
            return f"#{rgs_id} ✅ 积分续费 {primary} 天成功{tail}"

        logger.warning(
            f"雨云 #{rgs_id} 积分续费 {primary} 天失败：{detail}，退回尝试 {fallback} 天"
        )
        if fallback == primary:
            return f"#{rgs_id} ❌ 积分续费 {primary} 天失败：{detail}"

        ok2, detail2 = await self._renew_instance(rgs_id, fallback)
        if ok2:
            after, _ = await self._get_instance(rgs_id)
            tail = f"，新到期 {_fmt_ts(after.get('ExpDate'))}" if after else ""
            return (
                f"#{rgs_id} ✅ 续 {primary} 天失败（{detail}），"
                f"已退回续费 {fallback} 天成功{tail}"
            )
        return f"#{rgs_id} ❌ 续费 {primary} 天、{fallback} 天均失败：{detail2 or detail}"

    async def _check_and_renew(self, force: bool = False) -> str:
        """检查所有目标实例并续费，返回结果文本。"""
        if self._renew_lock.locked():
            return "上一次检查还没结束，稍等喵"
        async with self._renew_lock:
            self._last_check_at = time.time()
            targets = self._renew_targets()
            if not targets:
                self._last_result = (
                    "未配置监控实例（renew_instance_ids 与 default_rgs_id 都为空）"
                )
                return self._last_result
            lines = []
            for rid in targets:
                try:
                    lines.append(await self._renew_one(rid, force=force))
                except Exception as exc:  # pragma: no cover
                    logger.error(f"雨云自动续费处理 #{rid} 异常: {exc}")
                    lines.append(f"#{rid} ❌ 异常：{exc}")
            self._last_result = "\n".join(lines)
            return self._last_result

    async def _notify(self, text: str) -> None:
        umo = str(self.config.get("notify_umo", "") or "").strip()
        if not umo:
            return
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:  # pragma: no cover
            logger.warning(f"雨云续费通知发送失败: {exc}")

    async def _auto_renew_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                if self.config.get("auto_renew_enabled", True):
                    text = await self._check_and_renew()
                    logger.info(f"雨云积分续费检查结果：\n{text}")
                    if "✅" in text or "❌" in text:
                        await self._notify(f"☁️ 雨云积分续费\n{text}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                logger.error(f"雨云自动续费循环异常: {exc}")
            await asyncio.sleep(max(0.5, self._cfg_num("renew_interval_hours", 6)) * 3600)

    async def initialize(self) -> None:
        if not self.config.get("auto_renew_enabled", True):
            logger.info("雨云积分自动续费未启用")
            return
        if self._renew_task is None or self._renew_task.done():
            self._renew_task = asyncio.create_task(self._auto_renew_loop())
            logger.info("雨云积分自动续费已启动")

    async def terminate(self) -> None:
        task = self._renew_task
        self._renew_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ---------------- 排版 ----------------
    def _fmt_detail(self, inst: dict) -> str:
        label, emoji = _status_of(inst)
        lines = [f"{emoji} 实例 #{inst.get('ID')} · {label}"]

        game = _game_name(inst)
        if game:
            lines.append(f"🎮 游戏：{game}")
        lines.append(f"⚙️ 配置：{_spec(inst)}")
        zone = str(inst.get("Zone") or "").strip()
        if not zone:
            node = inst.get("Node")
            zone = str(node.get("ChineseName") or "").strip() if isinstance(node, dict) else ""
        if zone:
            lines.append(f"📍 区域：{zone}")

        addr = _address(inst)
        if addr:
            lines.append(f"🌐 地址：{addr}")

        exp = inst.get("ExpDate")
        lines.append(f"📅 到期：{_fmt_ts(exp)}（{_days_left(exp)}）")
        lines.append(f"🔄 自动续费：{'是' if inst.get('AutoRenew') else '否'}")

        mcsm_user = str(inst.get("McsmUserName") or "").strip()
        if mcsm_user:
            lines.append(f"👤 面板用户：{mcsm_user}")
        return "\n".join(lines)

    def _fmt_brief(self, rec: dict, index: int | None = None) -> str:
        label, emoji = _status_of(rec)
        head = f"{index}. " if index else ""
        return f"{head}{emoji} #{rec.get('ID')} {label}｜{_spec(rec)}｜{_days_left(rec.get('ExpDate'))}"

    # ---------------- 指令 ----------------
    @filter.command("雨云列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """列出全部雨云游戏云实例"""
        if not self._is_admin(event):
            yield event.plain_result("🚫 只有管理员可以使用雨云指令喵")
            return

        records, err = await self._list_instances()
        if records is None:
            yield event.plain_result(f"❌ 查询失败：{err}")
            return
        if not records:
            yield event.plain_result("📭 账号下没有游戏云实例喵")
            return

        body = "\n".join(self._fmt_brief(r, i + 1) for i, r in enumerate(records))
        yield event.plain_result(f"☁️ 雨云游戏云 · 共 {len(records)} 个\n{body}")

    @filter.command("雨云详情")
    async def cmd_detail(self, event: AstrMessageEvent, rgs_id: str = ""):
        """查询指定实例详情，用法：/雨云详情 [实例ID]"""
        if not self._is_admin(event):
            yield event.plain_result("🚫 只有管理员可以使用雨云指令喵")
            return

        rid, err = await self._resolve_id(rgs_id or event.message_str.replace("/雨云详情", ""))
        if rid is None:
            yield event.plain_result(f"❌ {err}")
            return

        inst, err = await self._get_instance(rid)
        if inst is None:
            yield event.plain_result(f"❌ {err}")
            return
        yield event.plain_result(self._fmt_detail(inst))

    @filter.command("雨云状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """快速查看默认实例运行状态"""
        if not self._is_admin(event):
            yield event.plain_result("🚫 只有管理员可以使用雨云指令喵")
            return

        rid, err = await self._resolve_id("")
        if rid is None:
            yield event.plain_result(f"❌ {err}")
            return

        inst, err = await self._get_instance(rid)
        if inst is None:
            yield event.plain_result(f"❌ {err}")
            return

        label, emoji = _status_of(inst)
        tail = f"（{_days_left(inst.get('ExpDate'))}）" if inst.get("ExpDate") else ""
        yield event.plain_result(
            f"{emoji} #{inst.get('ID')} 当前{label}{tail}\n{_spec(inst)}"
        )

    async def _do_power(self, event: AstrMessageEvent, action: str, rgs_id: str):
        name, endpoint, expect = POWER_CMD[action]

        if not self._is_admin(event):
            yield event.plain_result("🚫 只有管理员可以操作实例喵")
            return
        if not self.config.get("allow_power_ops", True):
            yield event.plain_result("🚫 插件配置里已关闭开关机功能喵")
            return

        rid, err = await self._resolve_id(rgs_id)
        if rid is None:
            yield event.plain_result(f"❌ {err}")
            return

        inst, err = await self._get_instance(rid)
        if inst is None:
            yield event.plain_result(f"❌ {err}")
            return

        current = str(inst.get("Status", "")).lower()
        label, _ = _status_of(inst)
        if expect and current == expect:
            yield event.plain_result(f"ℹ️ #{rid} 已经是{label}状态了喵，不用{name}")
            return

        ok, data = await self._api("POST", f"/product/rgs/{rid}/{endpoint}")
        if not ok:
            yield event.plain_result(f"❌ {name}失败：{_err_text(data)}")
            return

        # 回查一次确认
        await asyncio.sleep(2)
        after, _ = await self._get_instance(rid)
        if after:
            now_label, emoji = _status_of(after)
            yield event.plain_result(f"✅ 已下发{name}指令喵\n{emoji} #{rid} 当前{now_label}")
        else:
            yield event.plain_result(f"✅ 已下发{name}指令喵（#{rid}）")

    @filter.command("雨云开机")
    async def cmd_start(self, event: AstrMessageEvent, rgs_id: str = ""):
        """开机，用法：/雨云开机 [实例ID]"""
        async for r in self._do_power(event, "start", rgs_id or event.message_str.replace("/雨云开机", "")):
            yield r

    @filter.command("雨云关机")
    async def cmd_stop(self, event: AstrMessageEvent, rgs_id: str = ""):
        """关机，用法：/雨云关机 [实例ID]"""
        async for r in self._do_power(event, "stop", rgs_id or event.message_str.replace("/雨云关机", "")):
            yield r

    @filter.command("雨云重启")
    async def cmd_reboot(self, event: AstrMessageEvent, rgs_id: str = ""):
        """重启，用法：/雨云重启 [实例ID]"""
        async for r in self._do_power(event, "reboot", rgs_id or event.message_str.replace("/雨云重启", "")):
            yield r

    @filter.command("雨云续费")
    async def cmd_renew(self, event: AstrMessageEvent):
        """立即用积分续费（先 30 天，失败退回 7 天），用法：/雨云续费"""
        if not self._is_admin(event):
            yield event.plain_result("🚫 只有管理员可以使用雨云指令喵")
            return
        if not self.config.get("allow_renew_ops", True):
            yield event.plain_result("🚫 插件配置里已关闭积分续费功能喵")
            return

        yield event.plain_result("💠 正在检查并尝试积分续费，稍等喵…")
        text = await self._check_and_renew(force=True)
        yield event.plain_result(text)

    @filter.command("雨云自动续费")
    async def cmd_auto_renew(self, event: AstrMessageEvent):
        """查看自动积分续费配置与上次结果"""
        if not self._is_admin(event):
            yield event.plain_result("🚫 只有管理员可以使用雨云指令喵")
            return

        enabled = "开启" if self.config.get("auto_renew_enabled", True) else "关闭"
        targets = self._renew_targets()
        lines = [
            f"💠 自动积分续费：{enabled}",
            f"⚖️ 触发阈值：剩 ≤ {self.renew_threshold:g} 天",
            f"1️⃣ 首选：{self.renew_primary_days} 天｜2️⃣ 退回：{self.renew_fallback_days} 天",
            f"⏱️ 检查间隔：{self._cfg_num('renew_interval_hours', 6):g} 小时",
            f"🎯 监控实例：{'、'.join(f'#{i}' for i in targets) if targets else '未配置'}",
        ]
        if self._last_check_at:
            lines.append(f"🕒 上次检查：{_fmt_ts(self._last_check_at)}")
            lines.append(f"📋 结果：\n{self._last_result}")
        yield event.plain_result("\n".join(lines))
