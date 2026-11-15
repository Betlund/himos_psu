# himos-psu — 鑫谷 / 昆仑九重系列电源开源 Linux 上位机

基于对 `HiMOS 1.6.1` 与实机 `昆仑九重 KE-1300P`（CH340 `1A86:7523`, `115200` bps）逆向与抓包验证开发的轻量开源 Linux 监控、调速与能耗统计上位机，完全本地离线运行，无任何云端遥测或硬编码密钥。

## 特性

- **零额外串口依赖**：Linux 下通过标准库 `termios` / `select` + `/sys/class/tty` 自动发现 `1A86:7523` 串口（跨平台亦可选用 `pyserial`）。
- **协议 1:1 精准实现并修正实机差异**：
  - 自动顺序下发 `POWER_INFO(sub=0)`（型号/规格/代工 `segotep`/SN）与 `POWER_INFO(sub=53)`（固件日期 `V4.24.09.20`）并合并。
  - 同时兼容 PC 发送端 `crc8(commandContent)` 与 `KE-1300P` 固件接收端 `crc8(header + commandContent)` 双口径校验。
  - 完整解析 `+3.3V / +5V / +12V / +5VSB`、`输入功率 / 输入电压 / 频率`、高压区 / 低压区 / 环境三路温度、风扇转速与故障码。
  - 严格复刻 `ControlChart` 与 `x9` 分段线性重采样生成 27 字节自定义温控风扇曲线。
- **CLI + PySide6 原生桌面 GUI**：命令行实时监控、JSON 输出、原始十六进制嗅探；GUI 分「实时监控 / 能耗统计 / 设置」三页，含 5 点可拖拽风扇曲线编辑器。
- **能耗统计（v0.2 新增）**：后台采集并把输入功率积分成 kWh，支持固定电价与峰谷（尖/峰/平/谷）电价、今日/本周/本月/今年报表、月历热力图、负载率分布、供电会话、CSV/JSON 导出与改价重算历史费用。

## 快速上手

在本仓库目录下无需安装包即可直接使用（显式调用系统 `/usr/bin/python3`）：

```bash
# 查看检测到的电源串口
./bin/himos-psu list

# 读取 KE-1300P 规格、版本号、序列号与徽章状态
./bin/himos-psu info

# 持续刷新监控遥测数据（Ctrl+C 退出）
./bin/himos-psu watch

# 切换风扇模式 (auto / silent / performance / custom / clean)
./bin/himos-psu fan-mode silent

# 下发自定义 5 点温控曲线 (温度%,占空比%)
./bin/himos-psu fan-curve 0,0 40,20 60,30 80,70 100,100

# 启动 PySide6 桌面控制面板（离线体验可追加 --mock）
./bin/himos-psu-gui
```

## 能耗统计

### 开始采集

```bash
# 前台采集（Ctrl+C 停止）；GUI 打开时也会自动采集
./bin/himos-psu collect

# 查看今日 / 本月 / 自定义区间
./bin/himos-psu energy today
./bin/himos-psu energy month --json
./bin/himos-psu energy report --from 2026-11-01 --to 2026-11-15
```

GUI 的「能耗统计」页会随采集自动刷新；采集进程与 GUI **互斥**（串口被 `TIOCEXCL` 独占），
同时启动时第二个进程会明确提示，而不会静默失败。

### 统计口径与边界

- 统计的是**电源输入端功率**，不含显示器、音箱等外设。
- 固件只提供输入功率与输入电压，**不提供输出电流**，因此本工具不计算转换效率、不做能效曲线。
- 采样间隔由 PSU 自身推送决定（默认 1 s，可用 `refresh-rate` 调到 1–3 s）；能量用**梯形积分**按实测时间戳累积，不使用名义周期。
- 断档（相邻样本间隔 > 5 s）只记录为缺口，**不按 0 W 补算**，并计入覆盖率。
- 「运行时长」指功率高于 20 W 的时长，与单纯的采集覆盖时长区分。

### 电价方案

内置「示例：居民峰谷」方案（尖/峰/平/谷四段），**价格为示例值，请按当地实际电价修改**：

```bash
# 查看当前方案
./bin/himos-psu tariff list

# 固定电价
./bin/himos-psu tariff set --name 家用电 --price 0.55 --recalc

# 自定义峰谷（时段必须从 00:00 连续覆盖到 24:00）
./bin/himos-psu tariff set --name 峰谷 --bands "0-7:谷:0.32,7-10:平:0.58,10-24:峰:0.92" --recalc

# 按当前方案重算全部历史费用
./bin/himos-psu tariff recalc
```

改价不需要重建数据库：分钟桶保留瓦秒，费用由当前方案重新计算。

### 数据与保留

- 数据库：`~/.local/share/himos-psu/energy.db`（可用 `--db` 或 `HIMOS_PSU_DB` 覆盖），SQLite WAL。
- 原始样本保留 14 天，分钟桶与日/月汇总永久保留，长时间运行体积可控（1 s 采样约 2.5 MB/天）。
- 导出：

```bash
./bin/himos-psu energy export --kind minute --out -        # 分钟明细
./bin/himos-psu energy export --kind daily  --out daily.csv
./bin/himos-psu energy export --kind sessions --out -       # 供电会话
```

### GUI

`./bin/himos-psu-gui` 打开后切到「能耗统计」页：KPI 卡、今日/近 7 天/近 30 天曲线、
本月分时段占比、月历热力图、负载率分布、最近供电会话，以及导出与电价设置入口。

![能耗统计页](docs/energy-dashboard.png)

## 权限说明

若普通用户无权打开 `/dev/ttyUSB0`，可将用户加入串口所属用户组（如 `uucp` 或 `dialout`），或通过 `./bin/himos-psu udev-rule` 生成标准 `1a86:7523` udev 规则。

## 开发与测试

```bash
# 单元测试（协议、能耗积分、电价、GUI 离屏渲染）
/usr/bin/python3 -m unittest discover -s tests

# 无硬件联调：先用 mock 电源采集一段，再查看报表
./bin/himos-psu --mock collect --db /tmp/demo.db
./bin/himos-psu energy --db /tmp/demo.db today
```

测试覆盖真实抓包帧解码、CRC 双口径、分包重同步、梯形积分与断档、峰谷/阶梯边界、
改价重算、CSV 导出、采集锁互斥，以及 GUI 三个页签的离屏渲染。
