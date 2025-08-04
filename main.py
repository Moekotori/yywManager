from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Star, register
from astrbot.api import logger
from pathlib import Path
import sqlite3
import asyncio
import time
import math
from datetime import date
from typing import Any, Dict, Optional

PLUGIN_NAME = "yinyouwo"
VERSION = "v2.3.3
AUTHOR = "akkariin"

# --- 配置项 ---
HOURLY_FEE = 6.0  # 每小时 6 元
GRACE_PERIOD_SECONDS = 120  # 2分钟内的退勤为免费

ADMIN_IDS = {"2331103944", "87654321"}

FEE_PER_30_MINS = HOURLY_FEE / 2  # 每 30 分钟的费用

# 数据目录和 SQLite 文件
try:
    DATA_DIR = Path(__file__).parent / "data" / PLUGIN_NAME
except NameError:
    DATA_DIR = Path.cwd() / "data" / PLUGIN_NAME
DB_FILE = DATA_DIR / "user_data.db"

# 使用 asyncio 的锁，保护数据库并发访问
_LOCK = asyncio.Lock()


@register(PLUGIN_NAME, AUTHOR, "音游窝系统插件 " + VERSION, VERSION)
class YinyouwoPlugin(Star):
    def __init__(self, context):
        super().__init__(context)
        self.initialized_successfully = False
        try:
            logger.info(f"[{PLUGIN_NAME}] 正在初始化，数据目录: {DATA_DIR}")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
            self._init_db()
            self.initialized_successfully = True
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 插件初始化失败！请检查目录权限或路径配置。")
            logger.exception(e)

    async def initialize(self):
        if self.initialized_successfully:
            logger.info(f"🎵 [{PLUGIN_NAME}] 插件已成功初始化，版本 {VERSION}")
        else:
            logger.error(f"[{PLUGIN_NAME}] 插件未成功初始化，将无法正常工作。")

    def _check_init(self) -> Optional[MessageEventResult]:
        if not self.initialized_successfully:
            return MessageEventResult.plain_text("❌ 插件服务异常，请联系管理员。")
        return None

    def _init_db(self):
        """创建/更新表格：增加 discount 列"""
        with self.conn:
            self.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_data (
                qq TEXT PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 0,
                joined_at REAL,
                total_time REAL NOT NULL DEFAULT 0,
                today_date TEXT,
                today_consumption REAL NOT NULL DEFAULT 0,
                discount REAL NOT NULL DEFAULT 1.0
            )""")
            # 兼容旧数据库，尝试添加新列
            try:
                self.conn.execute("ALTER TABLE user_data ADD COLUMN discount REAL NOT NULL DEFAULT 1.0")
                logger.info(f"[{PLUGIN_NAME}] 数据库已更新，增加了 'discount' 字段。")
            except sqlite3.OperationalError:
                pass  # 列已存在，属于正常情况

    def _today(self) -> str:
        return date.today().isoformat()

    def _get_uid(self, evt) -> str:
        # 1) Aiocqhttp: get_sender_id()
        try:
            if hasattr(evt, "get_sender_id"):
                val = evt.get_sender_id()
                if val:
                    return str(val)
        except Exception:
            pass
        # 2) 通用 API: get_user_id()
        try:
            if hasattr(evt, "get_user_id"):
                val = evt.get_user_id()
                if val:
                    return str(val)
        except Exception:
            pass
        # 3) 直接属性: user_id
        if hasattr(evt, "user_id"):
            val = getattr(evt, "user_id")
            if val:
                return str(val)
        # 4) evt.sender.user_id / evt.sender.id
        sender = getattr(evt, "sender", None)
        if sender:
            if hasattr(sender, "user_id") and getattr(sender, "user_id"):
                return str(sender.user_id)
            if hasattr(sender, "id") and getattr(sender, "id"):
                return str(sender.id)
        # 5) evt.message.sender.user_id / id
        msg = getattr(evt, "message", None)
        if msg and hasattr(msg, "sender"):
            snd = msg.sender
            if hasattr(snd, "user_id") and getattr(snd, "user_id"):
                return str(snd.user_id)
            if hasattr(snd, "id") and getattr(snd, "id"):
                return str(snd.id)
        # 6) raw_event 字典查找
        raw = getattr(evt, "raw_event", {}) or {}
        if isinstance(raw, dict):
            # top-level
            for key in ("user_id", "userId", "sender_id"):
                if raw.get(key):
                    return str(raw.get(key))
            # nested sender
            sender2 = raw.get("sender") or raw.get("user") or {}
            if isinstance(sender2, dict):
                for key in ("user_id", "userId", "id"):
                    if sender2.get(key):
                        return str(sender2.get(key))
            # nested message.sender
            msg2 = raw.get("message", {})
            if isinstance(msg2, dict):
                snd2 = msg2.get("sender") or {}
                if isinstance(snd2, dict):
                    for key in ("user_id", "userId", "id"):
                        if snd2.get(key):
                            return str(snd2.get(key))
        # 最后失败，打印调试日志
        logger.error(f"[{PLUGIN_NAME}] _get_uid 失败，evt 属性有: {dir(evt)}; raw_event={raw}")
        return ""

    def _format_time(self, seconds: float) -> str:
        if seconds < 0: seconds = 0
        h, rem = divmod(int(seconds), 3600)
        m, s = divmod(rem, 60)
        parts = []
        if h: parts.append(f"{h}小时")
        if m: parts.append(f"{m}分钟")
        parts.append(f"{s}秒")
        return "".join(parts) if parts else "0秒"

    # 修正点 1：补全了类型提示的括号
    async def _get_user(self, qq: str) -> Optional[Dict[str, Any]]:
        async with _LOCK:
            with self.conn as conn:
                # [安全实践]：所有SQL查询都使用参数化 (?)，防止SQL注入。
                cur = conn.cursor()
                cur.execute("SELECT * FROM user_data WHERE qq=?", (qq,))
                row = cur.fetchone()

                user = None
                if row:
                    cols = [c[0] for c in cur.description]
                    user = dict(zip(cols, row))
                else:
                    today = self._today()
                    conn.execute(
                        "INSERT INTO user_data(qq, today_date, discount) VALUES(?,?,?)",
                        (qq, today, 1.0)
                    )
                    user = {
                        "qq": qq, "balance": 0.0, "joined_at": None,
                        "total_time": 0.0, "today_date": today, "today_consumption": 0.0,
                        "discount": 1.0
                    }

                today = self._today()
                if user and user["today_date"] != today:
                    user["today_date"] = today
                    user["today_consumption"] = 0.0
                    conn.execute(
                        "UPDATE user_data SET today_date=?, today_consumption=? WHERE qq=?",
                        (today, 0.0, qq)
                    )
                return user

    # 修正点 2：删除了多余的括号
    async def _update_user(self, qq: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        async with _LOCK:
            with self.conn as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM user_data WHERE qq=?", (qq,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"尝试更新一个不存在的用户: {qq}")

                cols = [c[0] for c in cur.description]
                user = dict(zip(cols, row))
                user.update(updates)

                conn.execute("""
                UPDATE user_data SET
                  balance=?, joined_at=?, total_time=?,
                  today_date=?, today_consumption=?, discount=?
                WHERE qq=?
                """, (
                    user["balance"], user["joined_at"], user["total_time"],
                    user["today_date"], user["today_consumption"], user["discount"],
                    user["qq"]
                ))
                return user

    @filter.command("出勤", only_to_me=False)
    async def cmd_attend(self, evt: AstrMessageEvent) -> MessageEventResult:
        if init_error := self._check_init(): return init_error
        qq = self._get_uid(evt)
        if not qq: return evt.plain_result("❌ 无法识别用户QQ，操作失败")

        user = await self._get_user(qq)
        if user["joined_at"] is not None: return evt.plain_result("❌ 你已出勤，无需重复操作")

        await self._update_user(qq, {"joined_at": time.time()})
        return evt.plain_result(f"✅ 出勤成功，余额 {user['balance']:.2f} 元")

    @filter.command("退勤", only_to_me=False)
    async def cmd_leave(self, evt: AstrMessageEvent) -> MessageEventResult:
        if init_error := self._check_init(): return init_error
        qq = self._get_uid(evt)
        if not qq: return evt.plain_result("❌ 无法识别用户QQ，操作失败")

        user = await self._get_user(qq)
        if user["joined_at"] is None: return evt.plain_result("❌ 你尚未出勤，请先发送“出勤”")

        now = time.time()
        duration = now - user["joined_at"]

        if duration <= GRACE_PERIOD_SECONDS:
            await self._update_user(qq, {"joined_at": None})
            return evt.plain_result(f"✅ 退勤成功，本次出勤时长小于2分钟，免于计费。")

        billing_units = math.ceil(duration / 1800)
        base_fee = billing_units * FEE_PER_30_MINS
        final_fee = base_fee * user.get("discount", 1.0)

        if user["balance"] < final_fee:
            return evt.plain_result(
                f"❌ 余额不足，无法退勤。\n"
                f"本次需支付 {final_fee:.2f} 元，当前余额 {user['balance']:.2f} 元。"
            )

        updated_user = await self._update_user(qq, {
            "balance": user["balance"] - final_fee,
            "today_consumption": user["today_consumption"] + final_fee,
            "total_time": user["total_time"] + duration,
            "joined_at": None
        })

        return evt.plain_result(
            f"✅ 退勤成功，在线 {self._format_time(duration)}，"
            f"扣费 {final_fee:.2f} 元，今日消费 {updated_user['today_consumption']:.2f} 元，"
            f"余额 {updated_user['balance']:.2f} 元"
        )

    @filter.command("查询信息", only_to_me=False)
    async def cmd_info(self, evt: AstrMessageEvent) -> MessageEventResult:
        if init_error := self._check_init(): return init_error
        qq = self._get_uid(evt)
        if not qq: return evt.plain_result("❌ 无法识别用户QQ，操作失败")

        user = await self._get_user(qq)
        status = "🎮 出勤中" if user["joined_at"] else "🏁 已退勤"
        discount_rate = user.get("discount", 1.0)
        discount_str = f" (当前享受 {discount_rate:.0%} 折扣)" if discount_rate < 1.0 else ""

        msg = [
            f"👤 QQ：{qq}",
            f"📋 状态：{status}",
            f"💰 余额：{user['balance']:.2f} 元{discount_str}",
            f"🕑 今日消费：{user['today_consumption']:.2f} 元",
            f"⏳ 总时长：{self._format_time(user['total_time'])}"
        ]
        return evt.plain_result("\n".join(msg))

    @filter.command("出勤列表", only_to_me=False)
    async def cmd_list(self, evt: AstrMessageEvent) -> MessageEventResult:
        if init_error := self._check_init(): return init_error
        now = time.time()
        async with _LOCK:
            with self.conn as conn:
                cur = conn.execute(
                    "SELECT qq, joined_at FROM user_data WHERE joined_at IS NOT NULL ORDER BY joined_at ASC"
                )
                lines = [
                    f"• {row[0]}：{self._format_time(now - row[1])}"
                    for row in cur.fetchall()
                ]

        if not lines:
            return evt.plain_result("📭 目前没有任何人出勤")
        return evt.plain_result("📋 当前出勤列表：\n" + "\n".join(lines))

    @filter.command("余额", only_to_me=False)
    async def cmd_balance(self, evt: AstrMessageEvent) -> MessageEventResult:
        if init_error := self._check_init(): return init_error
        qq = self._get_uid(evt)
        if not qq:
            return evt.plain_result("❌ 无法识别用户QQ，操作失败")
        user = await self._get_user(qq)
        return evt.plain_result(f"💰 QQ {qq} 当前余额：{user['balance']:.2f} 元")

    @filter.command("排行榜", only_to_me=False)
    async def cmd_rank(self, evt: AstrMessageEvent) -> MessageEventResult:
        if init_error := self._check_init(): return init_error
        async with _LOCK:
            with self.conn as conn:
                cur = conn.execute(
                    "SELECT qq, balance FROM user_data ORDER BY balance DESC LIMIT 10"
                )
                lines = [f"🏅 {i + 1}. {row[0]}：{row[1]:.2f} 元" for i, row in enumerate(cur.fetchall())]

        if not lines:
            return evt.plain_result("🏆 排行榜暂无数据")
        return evt.plain_result("🏆 余额排行榜：\n" + "\n".join(lines))

    async def _admin_op(self, evt: AstrMessageEvent, op_type: str) -> MessageEventResult:
        op_qq = self._get_uid(evt)
        if op_qq not in ADMIN_IDS: return evt.plain_result("❌ 权限不足")

        parts = evt.message_str.split()
        if len(parts) != 3: return evt.plain_result(f"❌ 格式错误。用法：{op_type} <QQ号> <金额>")

        target_qq, amount_str = parts[1], parts[2]

        if not target_qq.isdigit():
            return evt.plain_result("❌ QQ号格式错误，必须为纯数字。")
        try:
            amount = float(amount_str)
            if amount <= 0:
                raise ValueError("金额必须为正数")
        except ValueError:
            return evt.plain_result("❌ 金额格式错误，必须是大于0的数字。")

        user = await self._get_user(target_qq)
        new_balance = user["balance"]

        if op_type == "充值":
            new_balance += amount
            action_text = "充值"
        else:  # 扣款
            if user["balance"] < amount:
                return evt.plain_result(f"❌ 目标余额 {user['balance']:.2f} 元，不足以扣除 {amount:.2f} 元")
            new_balance -= amount
            action_text = "扣款"

        updated_user = await self._update_user(target_qq, {"balance": new_balance})
        return evt.plain_result(
            f"✅ 已为 QQ {target_qq} {action_text} {amount:.2f} 元，"
            f"其新余额为 {updated_user['balance']:.2f} 元"
        )

    @filter.command("充值", only_to_me=False)
    async def cmd_charge(self, evt: AstrMessageEvent) -> MessageEventResult:
        if init_error := self._check_init(): return init_error
        return await self._admin_op(evt, "充值")

    @filter.command("扣款", only_to_me=False)
    async def cmd_deduct(self, evt: AstrMessageEvent) -> MessageEventResult:
        if init_error := self._check_init(): return init_error
        return await self._admin_op(evt, "扣款")

    @filter.command("折扣", only_to_me=False)
    async def cmd_discount(self, evt: AstrMessageEvent) -> MessageEventResult:
        if init_error := self._check_init(): return init_error
        op_qq = self._get_uid(evt)
        if op_qq not in ADMIN_IDS: return evt.plain_result("❌ 权限不足")

        parts = evt.message_str.split()
        if len(parts) != 3: return evt.plain_result("❌ 格式错误。用法：折扣 <QQ号> <折扣率>")

        target_qq, discount_str = parts[1], parts[2]

        if not target_qq.isdigit():
            return evt.plain_result("❌ QQ号格式错误，必须为纯数字。")
        try:
            if discount_str.endswith('%'):
                rate = float(discount_str.strip('%')) / 100.0
            else:
                rate = float(discount_str)

            if not (0.0 < rate <= 1.0):
                raise ValueError("折扣率必须在 (0, 1] 区间内，即 0% 到 100%")
        except (ValueError, TypeError):
            return evt.plain_result("❌ 折扣率格式错误。请输入如 50% 或 0.5 的值。")

        await self._get_user(target_qq)
        await self._update_user(target_qq, {"discount": rate})

        return evt.plain_result(f"✅ 已为 QQ {target_qq} 设置折扣为 {rate:.0%}")

    @filter.command("帮助", aliases={"help"}, only_to_me=False)
    async def cmd_help(self, evt: AstrMessageEvent) -> MessageEventResult:
        if init_error := self._check_init(): return init_error
        return evt.plain_result(
            f"🎵 rinNet v{VERSION} 🎵\n\n"
            "【用户指令】\n"
            " • 出勤  (开始计时)\n"
            " • 退勤  (结束计时并结算)\n"
            " • 查询信息  (查看我的状态)\n"
            " • 余额  (查看我的余额)\n"
            " • 出勤列表  (查看所有在线用户)\n"
            " • 排行榜  (查看富豪榜)\n\n"
            "【管理员指令】\n"
            " • 充值 <QQ> <金额>\n"
            " • 扣款 <QQ> <金额>\n"
            " • 折扣 <QQ> <折扣率> (例: 50% 或 0.5)\n\n"
            f"计费标准：每30分钟 {FEE_PER_30_MINS:.2f} 元，不足30分钟按30分钟计。2分钟内退勤免费。"
        )

    async def terminate(self):
        if self.initialized_successfully:
            self.conn.close()
            logger.info(f"🎵 [{PLUGIN_NAME}] 已卸载，SQLite已关闭")
