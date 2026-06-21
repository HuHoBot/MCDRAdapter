import uuid as uuid_lib

from mcdreforged.api.all import *

from huhobot.config import HuHoBotConfig, WS_URL
from huhobot.ws_client import HuHoBotWsClient
from huhobot.event_handler import EventHandler

# 全局变量
ws_client: HuHoBotWsClient = None
config: HuHoBotConfig = None
event_handler: EventHandler = None
server_interface: PluginServerInterface = None

PLUGIN_ID = "huhobot"


def _ensure_server_id(server: PluginServerInterface):
    global config
    if not config.serverId:
        config.serverId = uuid_lib.uuid4().hex
        server.save_config_simple(config, "config.json")
        server.logger.info(f"已自动生成 serverId: {config.serverId}")


def _save_config(server: PluginServerInterface):
    server.save_config_simple(config, "config.json")


def _upgrade_config(server: PluginServerInterface):
    global config
    if config.version < 2:
        config.version = 2
        server.save_config_simple(config, "config.json")
        server.logger.info("配置已自动升级到 version 2，新增 postEvent / motd.markdown / motd.customMarkdown 字段")


def on_load(server: PluginServerInterface, old):
    global ws_client, config, event_handler, server_interface
    server_interface = server

    config = server.load_config_simple(
        "config.json",
        target_class=HuHoBotConfig
    )

    _ensure_server_id(server)

    _upgrade_config(server)

    ws_client_ref = [None]
    event_handler = EventHandler(server, config, lambda: ws_client_ref[0], _save_config, server.logger)

    ws_client = HuHoBotWsClient(config, event_handler, server.logger)
    ws_client_ref[0] = ws_client

    ws_client.start()
    server.logger.info("插件已加载，WebSocket 客户端已启动")

    _register_commands(server)


def on_unload(server: PluginServerInterface):
    global ws_client
    if ws_client:
        server.logger.info("正在停止 WebSocket 客户端...")
        ws_client.stop()
        ws_client = None


def on_info(server: PluginServerInterface, info: Info):
    if event_handler and not info.is_user:
        event_handler.on_server_info(info)


def on_user_info(server: PluginServerInterface, info: Info):
    if ws_client and info.is_player and info.content:
        ws_client.send_chat(info.player, info.content)



def on_player_joined(server: PluginServerInterface, player: str, info: Info):
    if event_handler:
        event_handler.on_player_joined(player)


def on_player_left(server: PluginServerInterface, player: str):
    if event_handler:
        event_handler.on_player_left(player)


def on_server_stop(server: PluginServerInterface, server_return_code: int):
    pass


# ======================== 命令注册 ========================

def _register_commands(server: PluginServerInterface):
    server.register_command(
        Literal("!!huhobot")
        .runs(lambda src: _cmd_help(src))
        .then(
            Literal("status").runs(lambda src: _cmd_status(src))
        )
        .then(
            Literal("reload")
            .requires(lambda src: src.has_permission(3), lambda: "需要 Admin 权限")
            .runs(lambda src: _cmd_reload(src))
        )
        .then(
            Literal("bind")
            .requires(lambda src: src.has_permission(3), lambda: "需要 Admin 权限")
            .then(
                Text("code").runs(lambda src, ctx: _cmd_bind(src, ctx["code"]))
            )
        )
    )


def _cmd_help(source: CommandSource):
    source.reply("=== HuHoBot MCDR Adapter ===")
    source.reply("!!huhobot          - 显示帮助")
    source.reply("!!huhobot status   - 显示连接状态")
    source.reply("!!huhobot reload   - 重载配置并重连")
    source.reply("!!huhobot bind <code> - 确认绑定请求")


def _cmd_status(source: CommandSource):
    if ws_client:
        connected = ws_client.is_active()
        status = "已连接" if connected else "未连接"
        source.reply(f"[HuHoBot] 连接状态: {status}")
        source.reply(f"[HuHoBot] 服务器 ID: {config.serverId}")
    else:
        source.reply("[HuHoBot] WebSocket 客户端未初始化")


def _cmd_reload(source: CommandSource):
    global ws_client, config, event_handler, server_interface

    server = source.get_server().as_plugin_server_interface()
    server_interface = server

    if ws_client:
        ws_client.stop()

    config = server.load_config_simple(
        "config.json",
        target_class=HuHoBotConfig
    )

    _ensure_server_id(server)

    _upgrade_config(server)

    ws_client_ref = [None]
    event_handler = EventHandler(server, config, lambda: ws_client_ref[0], _save_config, server.logger)
    ws_client = HuHoBotWsClient(config, event_handler, server.logger)
    ws_client_ref[0] = ws_client

    ws_client.start()
    source.reply("[HuHoBot] 配置已重载，WebSocket 已重新连接")


def _cmd_bind(source: CommandSource, code: str):
    if event_handler and event_handler.confirm_bind(code):
        source.reply(f"[HuHoBot] 绑定确认已发送 (code: {code})")
    else:
        source.reply(f"[HuHoBot] 未找到绑定请求 (code: {code})")
