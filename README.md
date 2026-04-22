# steamgametime

每整点拉取 Steam 公开 XML，比较最近两周游戏时长。如果发现某款游戏的最近两周时长增长，就通过 Bark 推送一条通知，并把最新快照提交回仓库，供下一次 GitHub Actions 继续比较。

## 当前监控对象

- 默认监控资料页 XML: https://steamcommunity.com/profiles/76561198839776064/?xml=1
- 默认脚本: steam_playtime_monitor.py
- 状态文件: data/steam_recent_playtime_state.json

## 推送内容

通知会说明：

- 哪些游戏时长增加了
- 每款游戏增加了多久
- 增长前后的最近两周时长

示例格式：

Steam 时长增长 24分钟
百日萌新 检测到以下增长:
Counter-Strike 2 +24分钟 (近2周 17小时0分钟 -> 17小时24分钟)
数据源来自公开 XML，粒度为 0.1 小时，约等于 6 分钟。

## GitHub Actions 配置

工作流文件在 .github/workflows/steam-playtime-monitor.yml，默认配置：

- 每整点运行一次
- 支持手动触发
- 运行成功后自动提交状态文件

请在仓库里配置以下内容：

1. Repository Secret: BARK_BASE_URL
   值示例: https://api.day.app/你的_device_key
2. Optional Repository Variable: STEAM_PROFILE_XML_URL
   如果不填，就使用脚本内置的目标账号 XML 地址。

## 本地运行

首次运行只会建立基线，不会推送：

python steam_playtime_monitor.py --print-current

如果只是查看本次会推什么内容，可以使用：

python steam_playtime_monitor.py --dry-run --print-current

## 限制

- 数据源是 Steam 公开 XML 里的 mostPlayedGames 节点，不是完整游戏库。
- hoursPlayed 公开只到 0.1 小时，所以换算成分钟后，真实粒度仍然是 6 分钟。
- 最后游玩时间的公开信息仍然只有日期级别，这个方案解决的是“时长增长监控”，不是精确到时分的 last played 时间戳。