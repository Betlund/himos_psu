# himos-psu — 鑫谷 / 昆仑九重系列电源开源 Linux 上位机

基于对 `HiMOS 1.6.1` 与实机 `昆仑九重 KE-1300P`（CH340 `1A86:7523`, `115200` bps）逆向与抓包验证开发的轻量开源 Linux 监控及调速上位机，完全本地离线运行，无任何云端遥测或硬编码密钥。

## 特性

- **零额外串口依赖**：Linux 下通过标准库 `termios` / `select` + `/sys/class/tty` 自动发现 `1A86:7523` 串口（跨平台亦可选用 `pyserial`）。
- **协议 1:1 精准实现并修正实机差异**：
  - 自动顺序下发 `POWER_INFO(sub=0)`（型号/规格/代工 `segotep`/SN）与 `POWER_INFO(sub=53)`（固件日期 `V4.24.09.20`）并合并。
  - 同时兼容 PC 发送端 `crc8(commandContent)` 与 `KE-1300P` 固件接收端 `crc8(header + commandContent)` 双口径校验。
  - 完整解析 `+3.3V / +5V / +12V / +5VSB`、`输入功率 / 输入电压 / 频率`、高压区 / 低压区 / 环境三路温度、风扇转速与故障码。
  - 严格复刻 `ControlChart` 与 `x9` 分段线性重采样生成 27 字节自定义温控风扇曲线。
- **CLI + PySide6 原生桌面 GUI**：支持命令行实时监控、JSON 输出、原始十六进制嗅探及 5 点可拖拽风扇曲线编辑器。

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

## 权限说明

若普通用户无权打开 `/dev/ttyUSB0`，可将用户加入串口所属用户组（如 `uucp` 或 `dialout`），或通过 `./bin/himos-psu udev-rule` 生成标准 `1a86:7523` udev 规则。
