# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### 新增

- 「累计用电」副标题改为 `自 2026-11-15 起 · 已记录 3 天` 并加悬停说明：累计区间从首个有数据的分钟
  算起，但中间没采集的日子不计入，避免把「区间起点」误读成「一直在统计」。
- 用电日历**可点击**：点某天把上方曲线切到该日 24 小时功率，注脚给出当日电量/电费/峰值，
  该日加描边，再点一次取消；悬停显示日期与电量。
- GUI 支持**深色/浅色两套主题**：新增 `theme.py`，启动时按桌面配色选择，所有自绘控件（功率曲线、分时段环图、
  日历热力图、风扇曲线）与样式表在绘制时读取当前调色板；`HIMOS_PSU_THEME=dark|light` 可强制指定。
- 「用电日历」补上**月份标题与翻月控件**（`2026 年 11 月` + ◀ / ▶ / 今天），可回溯 12 个月，未来月份禁用。
- 电价保留 **4 位小数**（`0.5469` 这类居民一档价不再被截成 `0.547`）：GUI 的固定单价输入框与时段表、
  `tariff list` 输出、方案描述统一按 4 位显示，存储本来就不丢精度。
- 电价方案支持**采暖期/非采暖期两套时段**：`TariffPlan.season`（跨年窗口，如 `11-01..03-31`），
  窗口内按 `--heating-bands` 计价、其余日期按 `--bands`，`price_at()`/`recalc_costs()` 自动分季。
  CLI 新增 `tariff set --heating MM-DD:MM-DD --heating-bands ...` 与 `--preset seasonal`；
  GUI 对话框新增「区分采暖期」勾选、日期输入与「正在编辑」切换。
- `tariff list` 改为按季节分行列出，长方案不再挤成一行。
- 固定电价方案切到峰谷分时时，表格不再空白：自动填入四段示例，直接改数字即可。
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

- 本月分时段环图的图例列宽是写死的：`统一电价` 四个字会压到 kWh 数字上（截断成「统一电价6.80」）。
  改为按列偏移排布，名字列加宽到 56px。
- 「今天」在当月时会被禁用，看起来像坏了；现在始终可点，点它回到本月并取消选中的日期。
- 选中的日期描边与「今天」太像，改成更粗的强调色描边 + 加粗日期数字。
- 保存「固定电价」后，本月分时段环图仍按旧的峰谷时段切分（注脚已经显示「统一电价」，两者矛盾）。
  原因是 `band_breakdown()` 用了连接内缓存的电价方案，而对话框是另一个连接写库的；现在每次查询都读
  当前生效方案，关闭对话框后还会显式让本连接的缓存失效。
- 电价对话框选择「固定电价」时仍显示一张空时段表与「正在编辑」下拉，看起来像坏了；现在整块编辑器
  直接隐藏，只保留单价与说明。
- 数字输入框的上下箭头被样式表压成两条细线；改为显式绘制按钮与箭头图标（随主题配色）。
- 日历的翻月按钮原先用透明背景 + 细边框 + 暗灰字形，在深色主题下基本看不见；改为 `navBtn` 样式
  （面板色背景、主题边框、悬停高亮），浅色主题同样可见。
- 关闭主窗口时先停掉能耗页的刷新定时器并关闭 sqlite 连接，避免进程退出时 PySide 在销毁对象阶段段错误。
- 打包元数据：去掉 `License :: OSI Approved :: MIT License` 分类器（PEP 639 下与
  `license = "MIT"` 冲突，setuptools ≥ 77 直接抛 `InvalidConfigError`，`pip install .` 会失败），
  补 `keywords` / `authors`，并为 `assets/*.svg` 声明 package-data，安装后窗口图标不再丢失。
- `bin/himos-psu-gui` 改为委托 `bin/himos-psu gui`，不再重复一遍 PYTHONPATH 解析。
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

- 统一 `ExecStart` 的路径处理为 `_systemd_exec()`（原先三处各写一遍 `$HOME` → `%h` 的转换），
  清理未使用的导入，并把 GUI 冒烟测试从「固定睡眠 6 秒」改为等待就绪条件。
- 新增 GitHub Actions（`.github/workflows/ci.yml`）：Python 3.10/3.12 矩阵，
  跑语法检查、47 个单元测试与全历史隐私扫描。

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
