# 使用Alun旅行规划

## 准备与安装

在支持 Skill 的 AI 助手中，将仓库文件夹命名为 `alun-travel-plan`，放入宿主指定的 Skill 目录，并按宿主方式刷新 Skill 列表。主入口为 `SKILL.md`。

如果直接在工作区使用，把整个文件夹放入工作区，让助手读取 `alun-travel-plan/SKILL.md`。联网检索与文件读写由当前 AI 助手提供；自动核查、渲染和版本管理使用 Python 3.10+ 标准库。

完整保留这四个部分：

```text
alun-travel-plan/
├── SKILL.md
├── references/
├── scripts/
└── assets/
```

## 从一次对话开始

可以说“初始化 Alun旅行规划”，也可以直接给出旅行需求。优先提供出发地、日期、同行人员、全队或人均预算、喜欢的玩法，以及已订车票和住宿。

有目的地时直接做攻略；还没决定时先比较方向。提供已有攻略时，可以继续修改或恢复其中的版本。

## 阅读与保存攻略

先查看并确认行程方案，再看一张 A–D 四列风格选择图并选定一款。选择图由宁海示例长图的头部裁切而成，只用来比较风格，不代表本次行程内容。最终交付包含 Markdown、单文件 HTML 和一张 1:5 PNG 长图。HTML 下载到本地后双击打开，支持按天查看、清单勾选和打印；Markdown 可以放入常用笔记工具。

本地存档结构为：

```text
travel-plan/<trip_id>/
├── trip.json
├── versions/
├── outputs/
├── manifest.v1.json
└── audit.v1.json
```

日常使用只需与助手对话。需要手动运行时，在仓库根目录执行：

```bash
python scripts/validate_trip.py travel-plan/<trip_id>/trip.json
python scripts/render_outputs.py travel-plan/<trip_id>/trip.json
python scripts/validate_trip.py travel-plan/<trip_id>/trip.json --check-outputs
```

`<trip_id>` 替换为自己的行程目录名。macOS/Linux 可按实际环境使用 `python3`。

## 修改与恢复

告诉助手需要更改的日期或安排，它会更新相应的交通、预算、预约清单和两个基础文件。每个版本都有独立编号。

手动操作示例：先在同一行程目录创建 `edited.json`，保留原 `trip_id` 与 `plan_version`，编辑后运行：

```bash
python scripts/state_io.py save travel-plan/<trip_id>/trip.json --from travel-plan/<trip_id>/edited.json --summary "调整第二天上午"
python scripts/state_io.py versions travel-plan/<trip_id>
python scripts/state_io.py diff travel-plan/<trip_id> 1 2
python scripts/state_io.py restore travel-plan/<trip_id> 1
```

保存或恢复后，完成相应核查，重新确认方案与风格，再生成 Markdown、HTML 和长图。旧长图不再代表当前版本。

## 小红书体验调研

TikHub 用于补充笔记与评论中的体验线索。选择使用时，在宿主安全配置中设置 `TIKHUB_API_KEY`，并与助手确定本次请求预算。凭据与行程文件分开保存。

查看命令形态可以先执行：

```bash
python scripts/tikhub_client.py search "苏州 博物馆 预约" --pages 1 --dry-run
```

实际调用方式和预算账本见 [TikHub 接入说明](../references/tikhub.md)。

## 看一个完整示例

仓库提供了一份虚构两日行程，用来浏览版式和数据结构：

- [完整 Markdown](examples/outputs/2026-10-10-example-2d_v1.md)
- [单文件 HTML](examples/outputs/2026-10-10-example-2d_v1.html)
- [同源行程数据](examples/trip.json)
- [字段说明](../assets/schemas/README.md)

截图和下载示例均来自这份数据。
