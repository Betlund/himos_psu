# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

暂无。

## [0.2.0] - 2026-11-16

首个公开发布。在「实时监控 + 风扇调速」之上加入完全本地的能耗统计。

### 新增

- 能耗采集：后台线程读取 PSU 上报的输入功率，按**实测时间戳**做梯形积分累积为 kWh。
- 断档处理：相邻样本间隔 > 5 s 记为缺口并计入覆盖率，**不按 0 W 补算**。
- 报表：今日 / 昨日 / 本周 / 本月 / 今年 / 自定义区间，含电量、电费、平均与峰值功率、覆盖率、运行时长。
- 可视化：今日功率曲线、近 7/30 天电量曲线、分时段占比环图、月历热力图、负载率分布、最近供电会话。
- 电价引擎：固定电价与峰谷分时（尖/峰/平/谷，须从 00:00 连续覆盖到 24:00）；改价后可重算全部历史费用。
- CLI：`collect`、`energy {today,yesterday,week,month,year,report,export,prune}`、`tariff {list,set,recalc}`。
- GUI：主窗口改为「实时监控 / 能耗统计 / 设置」三页签；设置页含电价编辑、碳排因子、数据管理与导出。
- 存储：SQLite（WAL）；原始样本保留 14 天，分钟桶与日汇总永久保留。
- 单采集者锁：串口被 `TIOCEXCL` 独占，GUI 与 `collect` 同时只能运行一个，冲突时明确报错。

### 变更

- `bin/himos-psu`、`bin/himos-psu-gui` 改为按脚本自身位置解析 `src/`，不再依赖硬编码路径，可在任意工作目录与软链接下调用。
- `gui` 子命令接受 `-p/--port`、`--mock`、`--no-crc-check`，`himos-psu-gui --mock` 现在可用。

### 工程

- 新增本地隐私流水线 `tools/pipeline.sh`：语法检查 + 单元测试 + 隐私扫描（工作区/暂存区/全历史/提交信息）。
- `pre-commit` 与 `pre-push` 钩子，安装脚本 `tools/install-hooks.sh`。
- `tools/privacy_scan.py`：按形状匹配序列号、限量编号、本机路径、内网地址、邮箱与常见密钥，命中值遮蔽输出。
- 测试从 6 个增加到 23 个，覆盖积分精度、断档语义、峰谷边界、改价重算、CSV 导出、采集锁互斥与 GUI 三页签渲染。

### 文档

- README 重构：徽章、首屏截图、三行快速上手、实时监控与能耗统计双截图表、兼容性说明。
- 新增 MIT `LICENSE`。

## [0.1.0] - 2026-11-15

内部初始版本，未单独发布。

- HiMOS 协议实现：帧编解码、双 CRC 口径校验、流式重同步、遥测与规格解析。
- 串口层：Linux `termios`/`select` 零依赖实现 + CH340 自动发现，另含 pyserial 与 mock 传输。
- 风扇控制：5 点自定义曲线，复刻 `ControlChart` 拖拽约束与 `x9` 重采样为 27 字节下发。
- CLI 与 PySide6 GUI 初版；6 个单元测试，含真实抓包帧解码与 CRC 校验。

[Unreleased]: https://github.com/Betlund/himos_psu/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Betlund/himos_psu/releases/tag/v0.2.0
