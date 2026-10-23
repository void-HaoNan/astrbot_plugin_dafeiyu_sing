# astrbot_plugin_dafeiyu_sing

给 AstrBot 用的本地唱歌/翻唱插件：点一首歌，先把人声和伴奏分开，用 RVC 把人声换成你选的音色，再混回伴奏，最后发一条语音。

不依赖云端服务，不按次计费。重活在本机跑，有 N 卡会快一些，没有也能慢慢跑。

## 功能

- `/唱歌 歌名`：点一首歌，默认唱 30 秒，可以配置时长和升降调
- `/唱歌音色`：看当前音色\n- `/唱歌状态`：看正在唱什么、排了几首
- 模型工具 `sing_song`：直接对机器人说「唱一首 起风了」也能触发
- 自动按语种挑音色：读 RVC 目录下的 `model_map.json`，中文/日语分别用不同模型；也可以固定一个
- 全程后台：接单后立刻回话，前台聊天不受影响；排队时会告诉你前面还有几首
- 子进程降优先级，分离/换声不抢聊天的 CPU
- 同一会话冷却、超时保护、过期成品自动清理

## 安装

1. 把插件放到 AstrBot 的 `data/plugins/astrbot_plugin_dafeiyu_sing`
2. 在 AstrBot 后台重载插件
3. 打开插件配置，填 `rvc_root`，指向你的 RVC 整合包目录（里面要有 `pkg/<整合包>/runtime/python.exe`）

依赖：Python 侧只用标准库；另外需要 RVC 整合包自带的 runtime 和 ffmpeg。

## 配置

| 键 | 说明 | 默认 |
|---|---|---|
| `model` | 音色名，或 `auto` 按语种自动挑 | `auto` |
| `pitch` | 升降调半音 | `0` |
| `max_seconds` | 每次唱多长 | `30` |
| `timeout_seconds` | 整首超时 | `900` |
| `cooldown_seconds` | 同会话冷却 | `30` |
| `out_keep_hours` | 成品保留小时 | `24` |

RVC 目录、整合包和 ffmpeg 全自动找：从插件位置往上认 AstrBot 目录，找它旁边的 `rvc/pkg/*/runtime/python.exe`，ffmpeg 先找 `bin/ffmpeg.exe` 再用 PATH 里的。放别处可以设环境变量 `RVC_ROOT` 或 `FFMPEG` 覆盖。\n\n音色就是 RVC 整合包里 `assets/weights/<名字>.pth` 的名字，索引在 `assets/indices/<名字>.index`。

## 工作原理

```
点歌 → 网易云/B站找音频 → demucs 分离人声和伴奏 → 截取人声最响的一段
     → RVC 换音色 → ffmpeg 混音 → 发语音
```

## 说明

- 只唱一小段（默认 30 秒），完整翻唱会很久。
- 找不到音频、分离不出人声、RVC 报错时会给一句短提示，不会把日志甩到群里。
- 歌词/歌曲版权归原作者，插件只做本机处理，不提供曲库下载服务。

## 参考

设计和交互参考了 AstrBot 社区里的翻唱/点歌插件（如 matsuko_cover、astrbot_plugin_music）和 RVC-Project 的用法，代码为本项目自己实现。

## License

MIT
