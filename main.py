import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

@register("whisper_box", "刘师傅", "碎碎念收集器 - 倾听你的每一句碎碎念", "1.0.0")
class ImListeningPlugin(Star):
    # 引入 config 注入。AstrBot 会自动把配置通过 KV 字典的形式传进来
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config # 这就是一个 KV 存储字典，可以直接读写
        self.user_sessions = {}

    @filter.command("等待")
    async def set_wait_time(self, event: AstrMessageEvent, time_str: str):
        """设置接收消息的等待时间（秒）"""
        try:
            new_time = float(time_str)
            if new_time <= 0:
                yield event.plain_result("时间必须大于0秒哦。")
                return
            
            # 【KV 存储写入】：直接修改字典键值，并调用官方的保存方法落盘
            self.config["wait_time"] = new_time
            self.config.save_config() 
            yield event.plain_result(f"设置成功！现在的等待时间是 {self.config['wait_time']} 秒。")
            
        except ValueError:
            yield event.plain_result("参数错误，请发送正确的数字，例如：/等待 5")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE)
    async def handle_message(self, event: AstrMessageEvent):
        # === 核心修复 1：使用绝对唯一 ID 隔离会话，防止重名/群聊串线 ===
        # 放弃使用极易重复的昵称，改用底层适配器提供的唯一数字或字符串标识
        sender_id = getattr(event.message_obj, "sender_id", event.get_sender_name())
        group_id = getattr(event.message_obj, "group_id", "")
        session_key = f"{group_id}_{sender_id}"
        
        message_str = event.message_str
        user_name = event.get_sender_name()
        
        # 指令直通车判断（无需修改）
        if message_str.startswith("等待 ") or message_str == "等待":
            return
            
        message_chain = event.get_messages()
        raw_text = ""
        for msg in message_chain:
            if hasattr(msg, "text"):
                raw_text += msg.text
                
        text_to_check = raw_text.strip() if raw_text else message_str.strip()
        
        if not text_to_check or text_to_check.startswith("/"):
            if session_key in self.user_sessions:
                session = self.user_sessions[session_key]
                if session["future"] and not session["future"].done():
                    session["future"].set_result(True) 
            return
            
        # 生效范围判断
        is_group = bool(group_id)
        chat_scope = self.config.get("chat_scope", "全部")
        
        if chat_scope == "仅私聊" and is_group:
            return
        if chat_scope == "仅群聊" and not is_group:
            return
            
        # === 连续输入合并逻辑 ===
        if session_key not in self.user_sessions:
            self.user_sessions[session_key] = {
                "messages": [],
                "future": None
            }
            
        session = self.user_sessions[session_key]
        
        # === 核心修复 2：引入“时间戳”强制锚定顺序 ===
        # AstrMessageEvent 的 message_obj 包含底层下发的整数型 timestamp
        # 我们将时间和文本打包成一个元组存进去，而不是只存文本
        timestamp = getattr(event.message_obj, "timestamp", 0)
        session["messages"].append((timestamp, message_str))
        
        if session["future"] and not session["future"].done():
            session["future"].cancel()
            
        current_future = asyncio.Future()
        session["future"] = current_future
        
        wait_time = self.config.get("wait_time", 5.0)
        
        try:
            await asyncio.wait_for(current_future, timeout=wait_time)
        except asyncio.TimeoutError:
            pass 
        except asyncio.CancelledError:
            event.message_str = "" 
            return
            
        # === 核心修复 3：最终重排，粉碎一切乱序 ===
        # 在合并之前，强制按照 tuple 的第一个元素（即时间戳 timestamp）从小到大升序排序
        session["messages"].sort(key=lambda x: x[0])
        combined_text = "\n".join([msg[1] for msg in session["messages"]])
        
        logger.info(f"用户 {user_name} ({session_key}) 的合并消息已提交: \n{combined_text}")
        
        event.message_str = combined_text
        if session_key in self.user_sessions:
            del self.user_sessions[session_key]