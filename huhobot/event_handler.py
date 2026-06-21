import re
import threading
import uuid


class EventHandler:
    def __init__(self, server_interface, config, ws_client_ref, save_config_func, logger):
        self.server = server_interface
        self.config = config
        self.logger = logger
        self._ws_client = ws_client_ref
        self._save_config = save_config_func
        self._bind_map = {}
        self._command_results = {}
        self._command_events = {}

    @property
    def ws_client(self):
        return self._ws_client()

    async def handle(self, type_, pack_id, body):
        handler_map = {
            "shaked": self._on_shaked,
            "chat": self._on_chat,
            "cmd": self._on_cmd,
            "add": self._on_add_whitelist,
            "delete": self._on_del_whitelist,
            "queryList": self._on_query_whitelist,
            "queryOnline": self._on_query_online,
            "sendConfig": self._on_send_config,
            "shutdown": self._on_shutdown,
            "bindRequest": self._on_bind_request,
            "run": self._on_run,
            "runAdmin": self._on_run_admin,
        }

        handler = handler_map.get(type_)
        if handler:
            result = handler(pack_id, body)
            if result is not None and hasattr(result, '__await__'):
                await result
        else:
            self.logger.debug(f"未知事件类型: {type_}")

    # ======================== 握手 ========================

    def _on_shaked(self, pack_id, body):
        code = body.get("code")
        if code == 1:
            self.ws_client.set_shook_hands(True)
            self.logger.info("握手成功!")
        elif code == 6:
            server_id = self.config.serverId
            self.logger.warning(f"服务器尚未在机器人进行绑定，请在群内输入\"/绑定 {server_id}\"")
        else:
            self.ws_client.set_shook_hands(False)
            msg = body.get("msg", "未知错误")
            self.logger.error(f"握手失败! 原因: {msg}")

    # ======================== 聊天转发 ========================

    def _on_chat(self, pack_id, body):
        if not self.config.chatFormat.post_chat:
            self.ws_client.send_response("", "success", pack_id)
            return
        nick = body.get("nick", "")
        msg = body.get("msg", "")
        formatted = self.config.chatFormat.group.replace("{nick}", nick).replace("{msg}", msg)
        self.server.say(formatted)
        self.ws_client.send_response("", "success", pack_id)

    # ======================== 命令执行 ========================

    def _on_cmd(self, pack_id, body):
        cmd = body.get("cmd", "")
        if not cmd:
            self.ws_client.send_response("命令为空", "error", pack_id)
            return
        self._execute_and_respond(cmd, pack_id)

    def _execute_and_respond(self, command, pack_id, success_prefix="已执行"):
        event = threading.Event()
        result_lines = []

        def on_info_callback(info_obj):
            if not info_obj.is_user and info_obj.content:
                result_lines.append(info_obj.content)

        callback_id = f"huhobot_cmd_{pack_id}"
        self._command_results[callback_id] = result_lines
        self._command_events[callback_id] = (event, on_info_callback)

        self.server.execute(command)

        def wait_and_respond():
            event.wait(timeout=3)
            self._command_events.pop(callback_id, None)
            self._command_results.pop(callback_id, None)

            if result_lines:
                result_text = "\n".join(result_lines)
                self.ws_client.send_response(
                    f"{success_prefix}\n{result_text}", "success", pack_id
                )
            else:
                self.ws_client.send_response(
                    f"{success_prefix}", "success", pack_id
                )

        threading.Thread(target=wait_and_respond, daemon=True, name="HuHoBot-CmdWait").start()

    def on_server_info(self, info):
        for callback_id, (event, callback) in list(self._command_events.items()):
            try:
                callback(info)
                event.set()
            except Exception:
                pass

    # ======================== 白名单操作 ========================

    def _on_add_whitelist(self, pack_id, body):
        xboxid = body.get("xboxid", "")
        if not xboxid:
            self.ws_client.send_response("ID 为空", "error", pack_id)
            return
        cmd = self.config.whitelist.addCommand.replace("{id}", xboxid)
        self.server.execute(cmd)
        self.ws_client.send_response(
            f"{self.config.serverName}已接受添加名为{xboxid}的白名单请求",
            "success", pack_id
        )

    def _on_del_whitelist(self, pack_id, body):
        xboxid = body.get("xboxid", "")
        if not xboxid:
            self.ws_client.send_response("ID 为空", "error", pack_id)
            return
        cmd = self.config.whitelist.delCommand.replace("{id}", xboxid)
        self.server.execute(cmd)
        self.ws_client.send_response(
            f"{self.config.serverName}已接受删除名为{xboxid}的白名单请求",
            "success", pack_id
        )

    # ======================== 白名单查询 ========================

    def _on_query_whitelist(self, pack_id, body):
        self._execute_and_respond("whitelist list", pack_id, success_prefix="白名单列表")

    # ======================== 在线玩家查询 ========================

    def _read_online_markdown(self) -> str:
        from pathlib import Path
        data_folder = self.server.get_data_folder()
        markdown_file = Path(data_folder) / "online.md"
        if not markdown_file.is_file():
            self.logger.warning("未找到 online.md 文件，请检查文件是否存在。若需自定义，请新建该文件在 config 同级目录下。")
            return ""
        try:
            return markdown_file.read_text(encoding="utf-8")
        except Exception as e:
            self.logger.warning(f"读取 online.md 失败: {e}")
            return ""

    def _on_query_online(self, pack_id, body):
        """按 SDK postMotd 格式回传在线信息（参考 Kotlin AbstractQueryOnline）"""
        event = threading.Event()
        result_lines = []

        def on_info_callback(info_obj):
            if not info_obj.is_user and info_obj.content:
                result_lines.append(info_obj.content)

        callback_id = f"huhobot_cmd_{pack_id}"
        self._command_results[callback_id] = result_lines
        self._command_events[callback_id] = (event, on_info_callback)

        self.server.execute("list")

        def wait_and_respond():
            event.wait(timeout=3)
            self._command_events.pop(callback_id, None)
            self._command_results.pop(callback_id, None)

            # 解析 list 命令输出，提取玩家数量和玩家列表
            # 格式: "There are X of a max of Y players online: player1, player2"
            player_names = []
            raw_text = "\n".join(result_lines)

            match = re.search(r'There are (\d+) of a max of \d+ players online:(.*)', raw_text, re.DOTALL)
            if match:
                online_count = int(match.group(1))
                players_str = match.group(2).strip()
                if players_str:
                    player_names = [p.strip() for p in players_str.split(",") if p.strip()]

            # 构造 msg 文本（参考 Kotlin AbstractQueryOnline）
            motd = self.config.motd
            sb = []

            if motd.output_online_list:
                if player_names:
                    for name in player_names:
                        sb.append(name)
                else:
                    sb.append("当前没有在线玩家")

            sb.append("")
            sb.append(motd.text.replace("{online}", str(len(player_names))))

            msg_text = "\n".join(sb)

            server_ip = motd.server_ip
            server_port = motd.server_port
            api = motd.api
            post_img = motd.post_img
            useMarkdown = motd.markdown

            img_url = api.replace("{server_ip}", server_ip).replace("{server_port}", str(server_port))

            list_data = {
                "msg": msg_text,
                "url": f"{server_ip}:{server_port}",
                "imgUrl": img_url,
                "post_img": post_img,
                "serverType": "java",
                "markdown": useMarkdown,
                "currentOnline": len(player_names)
            }

            if motd.customMarkdown:
                list_data["customMarkdown"] = self._read_online_markdown()

            self.ws_client.send_message("queryOnline", {"list": list_data}, pack_id)

        threading.Thread(target=wait_and_respond, daemon=True, name="HuHoBot-CmdWait").start()

    # ======================== 配置更新 ========================

    async def _on_send_config(self, pack_id, body):
        hash_key = body.get("hashKey", "")
        if hash_key:
            self.config.hashKey = hash_key
            self._save_config(self.server)
            self.logger.info("配置已接受并保存，自动断开连接以刷新...")
            await self.ws_client.trigger_reconnect()

    # ======================== 服务端命令断开 ========================

    def _on_shutdown(self, pack_id, body):
        msg = body.get("msg", "未知原因")
        self.logger.error(f"服务端命令断开连接，原因: {msg}")
        self.logger.error("此错误具有不可容错性! 请检查插件配置文件!")
        self.ws_client.set_should_reconnect(False)
        self.ws_client.stop()

    # ======================== 绑定请求 ========================

    def _on_bind_request(self, pack_id, body):
        bind_code = body.get("bindCode", "")
        if bind_code:
            self._bind_map[bind_code] = pack_id
            self.logger.info(
                f"收到绑定请求，请在游戏内或控制台输入 "
                f"!!huhobot bind {bind_code} 来确认绑定"
            )
            self.server.say(
                f"[HuHoBot] 收到绑定请求，请输入 !!huhobot bind {bind_code} 来确认"
            )

    def confirm_bind(self, bind_code):
        pack_id = self._bind_map.pop(bind_code, None)
        if pack_id:
            self.ws_client.send_response("", "bindConfirm", pack_id)
            return True
        return False

    # ======================== 自定义命令 ========================

    def _on_run(self, pack_id, body):
        cmd = body.get("cmd", "")
        self._handle_custom_run(cmd, pack_id, admin=False)

    def _on_run_admin(self, pack_id, body):
        cmd = body.get("cmd", "")
        self._handle_custom_run(cmd, pack_id, admin=True)

    def _handle_custom_run(self, cmd, pack_id, admin=False):
        if not cmd:
            self.ws_client.send_response("命令为空", "error", pack_id)
            return

        # customCommand 是列表格式 [{"key": "xxx", "command": "yyy", "permission": 0}]
        custom_cmd = None
        for item in self.config.customCommand:
            if isinstance(item, dict) and item.get("key") == cmd:
                custom_cmd = item
                break

        if custom_cmd:
            actual_cmd = custom_cmd.get("command", cmd)
            self._execute_and_respond(actual_cmd, pack_id)
        else:
            if admin:
                self._execute_and_respond(cmd, pack_id)
            else:
                self.ws_client.send_response("未找到该自定义命令", "error", pack_id)

    # ======================== 玩家事件转发 ========================

    def on_player_joined(self, player_name: str):
        cfg = self.config.postEvent.onJoin
        if cfg.enable and cfg.formatString:
            msg = cfg.formatString.replace("{playerName}", player_name)
            self._post_custom_msg(msg,"进服")

    def on_player_left(self, player_name: str):
        cfg = self.config.postEvent.onLeft
        if cfg.enable and cfg.formatString:
            msg = cfg.formatString.replace("{playerName}", player_name)
            self._post_custom_msg(msg,"退服")

    def _post_custom_msg(self, msg: str, msgType:str="聊天"):
        pack_id = uuid.uuid4().hex
        self.ws_client.send_message("chat", {
            "serverId": self.config.serverId,
            "msg": msg,
            "msgType": msgType
        }, pack_id)
