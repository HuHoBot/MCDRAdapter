import json
import uuid
import asyncio
import ssl
import threading

import websockets
import websockets.exceptions

from huhobot.config import WS_URL, PLATFORM, PLUGIN_VERSION


def _ws_closed(ws):
    """兼容 websockets 新旧版本判断连接是否已关闭"""
    if ws is None:
        return True
    if hasattr(ws, "closed"):
        return ws.closed
    return ws.close_code is not None


class HuHoBotWsClient:
    def __init__(self, config, event_handler, logger):
        self.config = config
        self.event_handler = event_handler
        self.logger = logger

        self.ws = None
        self.pending_requests = {}
        self._listen_task = None
        self._heartbeat_task = None
        self._reconnecting = False
        self._heartbeat_fail_count = 0
        self._max_heartbeat_fails = 3
        self._shook_hands = False
        self._should_reconnect = True

        self._loop = None
        self._thread = None
        self._started = False
        self._stop_event = None  # asyncio.Event，用于优雅退出

    # ======================== 线程管理 ========================

    def start(self):
        if self._started:
            return
        self._started = True
        self._should_reconnect = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="HuHoBot-WS")
        self._thread.start()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._start_and_run())
        except Exception as e:
            self.logger.error(f"事件循环异常: {e}")
        finally:
            try:
                self._loop.run_until_complete(self._cleanup())
            except Exception:
                pass
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()
            self._loop = None
            self._stop_event = None
            self._started = False

    async def _start_and_run(self):
        await self.connect()
        # 等待 stop_event 被设置，优雅退出
        await self._stop_event.wait()

    def stop(self):
        self._should_reconnect = False
        self._started = False
        if self._loop and self._loop.is_running() and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    # ======================== 连接管理 ========================

    async def connect(self):
        try:
            await self._cleanup()

            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            if not WS_URL.startswith("wss://"):
                ssl_context = None

            self.ws = await asyncio.wait_for(
                websockets.connect(
                    WS_URL,
                    ssl=ssl_context,
                    ping_interval=20,
                    ping_timeout=10
                ),
                timeout=10
            )

            self.logger.info("WebSocket 连接成功")
            self._shook_hands = False
            self._heartbeat_fail_count = 0
            await self._send_shake_hand()
            self._listen_task = asyncio.create_task(self._listen())
            self._heartbeat_task = asyncio.create_task(self._send_heartbeat())

        except Exception as e:
            self.logger.error(f"连接失败: {e}")
            await self._cleanup()
            await self.reconnect()

    async def _cleanup(self):
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except (asyncio.CancelledError, Exception):
                pass
        self._listen_task = None

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
        self._heartbeat_task = None

        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        for uid, future in self.pending_requests.items():
            if not future.done():
                future.set_result({})
        self.pending_requests.clear()

    async def reconnect(self):
        if self._reconnecting or not self._should_reconnect:
            return
        self._reconnecting = True
        try:
            self.logger.info("3秒后尝试重连...")
            await asyncio.sleep(3)
            if self._should_reconnect:
                await self.connect()
        finally:
            self._reconnecting = False

    async def trigger_reconnect(self):
        """安全地触发重连（从事件处理器内部调用）"""
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            # listen 循环检测到关闭后会自动触发 reconnect

    # ======================== 消息收发 ========================

    async def _listen(self):
        if self.ws is None:
            return
        try:
            async for message in self.ws:
                await self._process_message(message)
        except websockets.exceptions.ConnectionClosed as e:
            self.logger.warning(f"连接已关闭: {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            self.logger.error(f"监听异常: {e}")
        finally:
            await self.reconnect()

    async def _process_message(self, message):
        try:
            json_msg = json.loads(message)
        except json.JSONDecodeError:
            self.logger.error(f"收到非 JSON 消息: {message[:200]}")
            return

        header = json_msg.get("header", {})
        body = json_msg.get("body", {})
        type_ = header.get("type")
        pack_id = header.get("id")

        if pack_id in self.pending_requests:
            future = self.pending_requests.pop(pack_id)
            if not future.done():
                future.set_result(body)
        else:
            try:
                await self.event_handler.handle(type_, pack_id, body)
            except Exception as e:
                self.logger.error(f"处理事件异常: type={type_}, error={e}")

    async def _send_msg(self, type_, body, pack_id=None):
        if pack_id is None:
            pack_id = str(uuid.uuid4())
        if _ws_closed(self.ws):
            return False
        message = json.dumps({"header": {"type": type_, "id": pack_id}, "body": body})
        try:
            await self.ws.send(message)
            return True
        except Exception as e:
            self.logger.error(f"发送消息失败: {e}")
            return False

    async def send_and_wait(self, type_, body, pack_id=None, timeout=10.0):
        if not self.is_active():
            self.logger.warning("连接未就绪，无法发送消息")
            return {}
        if pack_id is None:
            pack_id = str(uuid.uuid4())
        pack_id = str(pack_id)
        future = asyncio.Future()
        self.pending_requests[pack_id] = future

        sent = await self._send_msg(type_, body, pack_id)
        if not sent:
            self.pending_requests.pop(pack_id, None)
            if not future.done():
                future.set_result({})
            return {}

        try:
            response = await asyncio.wait_for(future, timeout)
            return response
        except asyncio.TimeoutError:
            self.logger.warning(f"等待响应超时: UUID={pack_id}")
            self.pending_requests.pop(pack_id, None)
        return {}

    # ======================== 协议方法 ========================

    async def _send_shake_hand(self):
        await self._send_msg("shakeHand", {
            "serverId": self.config.serverId,
            "hashKey": self.config.hashKey,
            "name": self.config.serverName,
            "version": PLUGIN_VERSION,
            "platform": PLATFORM
        })

    async def _send_heartbeat(self):
        try:
            while True:
                await asyncio.sleep(5)
                if _ws_closed(self.ws):
                    self.logger.warning("心跳检测到连接已断开")
                    break

                try:
                    heart_uuid = str(uuid.uuid4())
                    future = asyncio.Future()
                    self.pending_requests[heart_uuid] = future
                    await self._send_msg("heart", {}, heart_uuid)

                    try:
                        await asyncio.wait_for(future, timeout=5)
                        self._heartbeat_fail_count = 0
                    except asyncio.TimeoutError:
                        self.pending_requests.pop(heart_uuid, None)
                        self._heartbeat_fail_count += 1
                        self.logger.warning(
                            f"心跳超时 ({self._heartbeat_fail_count}/{self._max_heartbeat_fails})"
                        )
                        if self._heartbeat_fail_count >= self._max_heartbeat_fails:
                            self.logger.error("连续心跳超时，触发重连")
                            break
                except websockets.exceptions.ConnectionClosed:
                    self.logger.warning("心跳时连接已关闭")
                    break
                except Exception as e:
                    self.logger.error(f"心跳异常: {e}")
                    break
        except asyncio.CancelledError:
            return
        await self.reconnect()

    # ======================== 外部调用接口 ========================

    def send_response(self, msg, type_, pack_id, callback_convert=0):
        """向服务端发送回报（从 MCDR 线程调用）"""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._send_msg(type_, {"msg": msg, "callbackConvert": callback_convert}, pack_id),
                self._loop
            )

    def send_message(self, type_, body, pack_id):
        """向服务端发送自定义消息（从 MCDR 线程或事件处理器调用）"""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._send_msg(type_, body, pack_id),
                self._loop
            )

    def send_chat(self, player, message):
        """发送游戏聊天消息到 HuHoBot（从 MCDR 线程调用）"""
        if not (self._loop and self._loop.is_running()):
            return
        chat_format = self.config.chatFormat
        prefix = chat_format.post_prefix
        is_post_chat = chat_format.post_chat

        if is_post_chat and message.startswith(prefix):
            msg_content = message[len(prefix):] if prefix else message
            formatted = chat_format.game.replace("{name}", player).replace("{msg}", msg_content)
            body = {
                "serverId": self.config.serverId,
                "msg": formatted
            }
            asyncio.run_coroutine_threadsafe(
                self._send_msg("chat", body),
                self._loop
            )


    def is_active(self):
        return self.ws is not None and not _ws_closed(self.ws) and self._shook_hands

    def set_shook_hands(self, value):
        self._shook_hands = value

    def set_should_reconnect(self, value):
        self._should_reconnect = value
