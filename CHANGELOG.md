# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### 新增

- **后台采集守护进程（`himos-psu daemon`）**：独占串口并 24/7 采集，通过 Unix socket 对外提供
  NDJSON IPC——订阅遥测、下发风扇模式/曲线/刷新周期、清故障码。GUI 检测到守护进程时自动切换为
  IPC 跟随模式（不再打开串口），可边采集边调速；`systemd-daemon` 生成带 `Restart=always` 的用户单元。
- `systemd-daemon` 新增 `--install` / `--enable` / `--linger` / `--dry-run`：一条命令把守护进程装成
  `systemd --user` 服务并启用（不再需要手工重定向写单元文件）。检测到还有手跑的 daemon 占用串口时
  会拒绝启用并提示先停掉它；发现旧的 `himos-psu-restore.service` 会打印退役命令。
- `collect` / `daemon` 新增 `--stale-timeout`：超过 N 秒收不到遥测即退出，交由 systemd 重启，
  用于休眠唤醒后 USB 重新枚举的场景。
- 风扇配置持久化与启动自动恢复：新增 `config.py`，在 GUI 切换模式（排除一次性 `Clean`）、点击「应用自定义曲线」或调节刷新周期时将配置原子写入 `~/.config/himos-psu/settings.json`。
- GUI 重连自动恢复与设置开关：连接电源并收到首帧校验通过的 `0x02` 遥测后自动按序重推模式与曲线（存在锁存故障码时自动跳过），「设置」页新增自动恢复开关与配置摘要。
- CLI 新增 `apply-saved`（支持 `--dry-run` / `--if-needed` / `--force` / `--wait-port` / `--skip-if-busy`）、`systemd-unit` 以及 `fan-mode` / `fan-curve` / `refresh-rate` 的 `--save` 参数，方便通过 `systemd --user` 在开机时无 GUI 恢复曲线。

### 修复

- CLI 的覆盖率一行补上「记录时段」并新增 `first_sample_at` / `last_sample_at` 字段：
  覆盖率的分母是首末分钟桶而不是日历区间，不打印时段时容易被误读成「今天漏了 10%」。
- 生成的 user 单元不再给 `daemon` 加 `--quiet`：服务里 journal 是唯一的日志出口，
  否则启动失败只留下 `exited status=1` 而没有原因。
- 守护进程快照缓存改为按帧类别存放（厂商/出厂日期/序列号固定保留，状态与扩展遥测只留最新一帧）。
  原先按原始字节做 12 条 FIFO，启动握手得到的 `0x03`/`0x05` 帧几秒内就被每帧都不同的遥测流冲掉，
  后连上的 GUI 拿不到规格，厂商一栏显示 `--`。
- `PowerSupplyClient.set_fan_mode()` 改为在发送 `0x13` 切模式之前先校验并预构建 `0x1B` 曲线帧，避免非法曲线在切模式后抛出异常、发送半套指令；连续帧间隔默认设为 `0.15s`。
- GUI 冒烟测试原先按固定的「今天 10:00」写入样本，在 UTC 时区且还没到 10 点的 CI
  runner 上会被「今日」区间排除、断言失败；改为相对当前时刻生成样本。

### 工程

- 新增 GitHub Actions（`.github/workflows/ci.yml`）：Python 3.10/3.12 矩阵，
  跑语法检查、23 个单元测试与全历史隐私扫描。

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
