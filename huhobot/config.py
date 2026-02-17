from mcdreforged.api.utils.serializer import Serializable

import base64

# 硬编码常量，不暴露到配置文件
_WS_URL_ENC = "d3M6Ly9tYy54Znl3ei5jbjoyNTY3MQ=="
WS_URL = base64.b64decode(_WS_URL_ENC).decode()
PLATFORM = "MCDR"
PLUGIN_VERSION = "1.0.0"


class ChatFormat(Serializable):
    game: str = "<{name}> {msg}"
    group: str = "群:<{nick}> {msg}"
    post_chat: bool = True
    post_prefix: str = ""


class MotdConfig(Serializable):
    server_ip: str = "play.hypixel.net"
    server_port: int = 25565
    api: str = "https://motdbe.blackbe.work/status_img/java?host={server_ip}:{server_port}"
    text: str = "共{online}人在线"
    output_online_list: bool = True
    post_img: bool = True


class WhiteListConfig(Serializable):
    addCommand: str = "whitelist add {id}"
    delCommand: str = "whitelist remove {id}"


class HuHoBotConfig(Serializable):
    serverId: str = ""
    hashKey: str = ""
    serverName: str = "MCDR Server"
    chatFormat: ChatFormat = ChatFormat.get_default()
    motd: MotdConfig = MotdConfig.get_default()
    whitelist: WhiteListConfig = WhiteListConfig.get_default()
    customCommand: list = []
    callbackConvertImg: int = 1
    version: int = 1
