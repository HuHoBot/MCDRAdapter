# HuHoBot-MCDR-Adapter v1.0.2

### feat(core): 添加玩家进出服务器事件推送功能
- 在 ClientManager 中新增 postCustomChat 方法用于发送自定义消息
- 实现玩家加入和离开时向机器人发送自定义格式消息

### feat(motd): 添加自定义Markdown功能支持
- 在Motd配置中新增customMarkdown选项
- 读取config同目录下的online.md文件作为自定义Markdown