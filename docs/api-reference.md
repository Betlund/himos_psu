# himos-psu 只读 API 参考（面向自动化程序 / AI 代理）

本文是给**程序或 AI 代理**读的接口说明：只有端点、字段、类型与语义，没有营销文字。
人类向的说明（含语言示例、FAQ）见 [api.md](api.md)。

- 服务方：`himos-psu daemon`（独占串口、24/7 采集的那个进程）
- 默认地址：`http://127.0.0.1:8790`
- 传输：HTTP/1.1，请求与响应都是 UTF-8 JSON（`/metrics` 例外，是 Prometheus 文本）
- 方向：**只读**。没有任何端点会修改电源、数据库或采集状态
- 版本：`schema: 1`，路径前缀 `/v1`

## 1. 启动与鉴权

```bash
# 守护进程带 --http-port 才会开 API；0（默认）= 关闭
./bin/himos-psu daemon --http-port 8790

# 可选：要求 Bearer token
./bin/himos-psu daemon --http-port 8790 --http-token <TOKEN>
```

| 项 | 值 |
|---|---|
| 绑定地址 | 固定 `127.0.0.1`（不对外网开放） |
| 鉴权 | 可选。启用后每个请求必须带 `Authorization: Bearer <TOKEN>` |
| 不接受 | URL 参数里的 token、Cookie、HTTP 以外的协议 |

鉴权失败返回 `401`，正文 `{"error": "...", "status": 401}`。

## 2. 通用约定

| 约定 | 说明 |
|---|---|
| 时间 | 所有 `ts` 是 **Unix epoch 秒**（`float`，带毫秒）；`start`/`end`/`first_sample_at` 是本地时区 ISO-8601 字符串 |
| 单位 | 功率 `W`、电能 `kWh`、电费 `CNY`（元）、温度 `°C`、电压 `V`、频率 `Hz`、转速 `RPM` |
| `null` | 表示**没有数据**，不是 0。任何数值字段都可能为 `null`（服务刚启动、断档、该分钟没采集） |
| `stale` | `true` 表示最近一帧超过 **5 秒**没更新；此时数值仍是最后一次的，不能当作实时值 |
| 缓存 | `/v1/status` 里的 `energy` 块有 **2 秒**缓存；其余字段实时 |
| 频率建议 | 控制回路 1 Hz；看板 5–15 s。不要高于 2 Hz，没有更多信息 |
| 精度 | JSON 数字保留实现给出的精度；需要 4 位小数请自行取整显示 |

## 3. 端点总览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/status` | 瞬时状态 + 今日/本月/累计电量 |
| GET | `/v1/history?hours=24&step=300` | 最近 N 小时的功率序列 |
| GET | `/v1/energy?range=today[&bands=1]` | 某区间的电量、电费、峰值、覆盖率 |
| GET | `/v1/meta` | 型号/固件/额定功率；SN 需 `?sn=1` |
| GET | `/v1/health` | 存活与数据新鲜度（探活用） |
| GET | `/metrics` | Prometheus 文本指标 |
| GET | `/` | 端点索引与 schema 版本 |

## 4. `GET /v1/status`

无参数。示例：

```json
{
  "schema": 1,
  "ts": 1795756533.707,
  "age_s": 0.189,
  "stale": false,
  "state": "采集中",
  "power_w": 213.75,
  "input_voltage_v": 228.5,
  "frequency_hz": 50.1,
  "rails_v": {"12": 12.13, "5": 5.01, "3.3": 3.31, "5vsb": 5.01},
  "temps_c": {"hv": 62.7, "lv": 52.0, "env": 31.3},
  "fan_rpm": 1140,
  "fan_mode": "custom",
  "fan_mode_id": 3,
  "fault_code": 0,
  "unit": {"model": "KE-1300P", "firmware": "V4.24.09.20", "vendor": "segotep", "rated_power_w": 1300},
  "energy": {"today_kwh": 1.5615, "today_cost_yuan": 0.867, "month_kwh": 8.79,
             "total_kwh": 8.78, "recorded_days": 3}
}
```

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| `schema` | int | 否 | 固定 `1` |
| `ts` | float | 是 | 最近一帧遥测的接收时间；从未收到过为 `null` |
| `age_s` | float | 是 | `ts` 距今秒数 |
| `stale` | bool | 否 | `age_s > 5` 或无数据 |
| `state` | str | 否 | 采集器状态：`采集中` / `正在连接电源…` / `未启动` 等 |
| `power_w` | float | 是 | 电源**输入端**功率（墙插侧） |
| `input_voltage_v` | float | 是 | 输入电压 |
| `frequency_hz` | float | 是 | 输入频率 |
| `rails_v.12` / `.5` / `.3.3` / `.5vsb` | float | 是 | 直流输出各路线电压 |
| `temps_c.hv` | float | 是 | 高压侧（一次侧）温度 |
| `temps_c.lv` | float | 是 | 低压侧（二次侧）温度 |
| `temps_c.env` | float | 是 | 环境温度 |
| `fan_rpm` | int | 是 | 风扇转速 |
| `fan_mode` | str | 是 | `auto` / `silent` / `performance` / `custom` / `clean` / `unknown`（小写） |
| `fan_mode_id` | int | 是 | 同上，协议原始编号 0–5 |
| `fault_code` | int | 是 | `0` 表示正常 |
| `unit.model` | str | 是 | 型号 |
| `unit.firmware` | str | 是 | 固件显示版本 |
| `unit.vendor` | str | 是 | 代工厂 |
| `unit.rated_power_w` | int | 是 | 额定功率 |
| `energy.today_kwh` | float | 是 | 今日（本地 0 点起）电量 |
| `energy.today_cost_yuan` | float | 是 | 今日电费 |
| `energy.month_kwh` | float | 是 | 本月电量 |
| `energy.total_kwh` | float | 是 | 自**开始记录**起的累计电量（不是电源出厂以来） |
| `energy.recorded_days` | int | 是 | 有数据的天数（当天 ≥ 10 分钟才算） |

未连接电源时只返回 `schema`/`ts`/`age_s`/`stale`/`state`/`power_w`，其中 `power_w` 为 `null`。

## 5. `GET /v1/history`

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `hours` | float | `24` | `0 < hours ≤ 336` |
| `step` | int | `300` | 只能是 `60` / `300` / `900` / `3600` |

```json
{
  "schema": 1, "step_s": 300, "unit": "W",
  "start": 1795752900, "end": 1795756533,
  "points": [
    {"ts": 1795752900, "avg": 145.2, "min": 130.0, "max": 260.1},
    {"ts": 1795753200, "avg": null, "min": null, "max": null}
  ]
}
```

- `points` 按时间升序，`start` 对 `step` 向下对齐，因此 1 小时可能得到 12 或 13 个点
- `avg/min/max` 是该格内**每分钟平均功率**的平均/最小/最大值；**不是 1 秒尖峰**
- 该格没有数据时三者全为 `null`（画图请留断点，不要补 0）
- 数据来源是最细 1 分钟的历史档，所以 `step` 不能小于 60

## 6. `GET /v1/energy`

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `range` | str | `today` | `today` / `yesterday` / `week` / `month` / `year` |
| `bands` | 0/1 | `0` | `1` 时返回分时段明细 |

```json
{
  "schema": 1, "range": "today",
  "kwh": 1.5615, "cost_yuan": 0.867, "avg_w": 145.6, "peak_w": 414.6,
  "coverage": 1.0, "gap_s": 0.0, "active_s": 38618.0,
  "start": "2026-11-27T00:00:00", "end": "2026-11-27T10:43:39",
  "first_sample_at": "2026-11-27T00:00:00"
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `kwh` | float | 区间电量（梯形积分，按实测时间戳） |
| `cost_yuan` | float | 区间电费（按当前生效电价方案） |
| `avg_w` | float | 有数据时段的平均功率 |
| `peak_w` | float | 峰值功率（供电会话峰值与分钟峰值的较大者） |
| `coverage` | float | `有数据秒数 / 记录跨度`，0–1。**分母不是整天** |
| `gap_s` | float | 采集过程中检测到的断档总秒数（>5 秒的间隔） |
| `active_s` | float | 平均功率 > 20 W 的秒数，即「机器真正在用电」的时长 |
| `start` / `end` | str | 请求的区间（本地时间） |
| `first_sample_at` | str/null | 该区间第一条有效数据的时间 |

`bands=1` 时追加：

```json
"bands": [{"name": "峰", "kwh": 1.02, "cost_yuan": 0.59, "price": 0.5769, "share": 0.67}]
```

## 7. `GET /v1/meta`

参数 `sn=1` 才会返回序列号（默认不含）。

```json
{
  "schema": 1, "state": "采集中", "stale": false,
  "http": {"host": "127.0.0.1", "port": 8790, "auth": false},
  "units": {"power": "W", "energy": "kWh", "cost": "CNY", "temperature": "°C"},
  "model": "KE-1300P", "firmware": "V4.24.09.20", "vendor": "segotep",
  "rated_power_w": 1300, "certification_80plus": "P", "fan_mode": "custom"
}
```

## 8. `GET /v1/health`

```json
{"schema": 1, "ok": true, "state": "采集中", "age_s": 0.2, "stale": false, "database": true}
```

`ok` 表示收到过遥测；`database` 表示历史库可读。连不上端口说明守护进程没起来；
`ok:false` 说明守护进程在但电源没连上。

## 9. `GET /metrics`

`text/plain; version=0.0.4`。指标清单：

```
himos_psu_stale                                                  0 / 1
himos_psu_power_watts                                            W
himos_psu_input_voltage_volts                                    V
himos_psu_frequency_hertz                                        Hz
himos_psu_fan_rpm                                                RPM
himos_psu_temp_celsius{sensor="hv"|"lv"|"env"}                   °C
himos_psu_fault_code                                             原始故障码
himos_psu_energy_kwh{range="today"|"month"|"total"}              kWh
himos_psu_energy_cost_yuan{range="today"}                        CNY
```

## 10. 错误

| HTTP | 含义 | 正文 |
|---|---|---|
| 400 | 参数不合法（如 `step=120`） | `{"error": "...", "status": 400}` |
| 401 | 需要或错误的 Bearer token | 同上 |
| 404 | 未知端点 | 同上 |
| 500 | 服务内部异常 | 同上 |
| 503 | 能耗数据库不可用（历史/电量端点） | 同上 |

所有错误都是 JSON，`error` 是可读原因。

## 11. 推荐用法

```python
# 控制回路的 1 Hz 采样（只用状态，开销最小）
import json, time, urllib.request

def poll(url="http://127.0.0.1:8790/v1/status"):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())

while True:
    snap = poll()
    if snap["power_w"] is None or snap["stale"]:
        pass                      # 数据不可用：不要用旧值做决策
    else:
        use(snap["power_w"], snap["temps_c"]["hv"], snap["fan_rpm"])
    time.sleep(1)
```

```bash
# 24 小时曲线（5 分钟一格）交给分析脚本
curl -s 'localhost:8790/v1/history?hours=24&step=300' > hist.json
# 今天的总电量与电费
curl -s 'localhost:8790/v1/energy?range=today'
```

## 12. 常见误用（不要这样做）

1. **把 `null` 当 0**：那表示没采到数，不是 0 W，否则会把「断线」算成「空载」。
2. **用 `/v1/status` 自己做积分**：采样会丢、会重复；要电量请用 `/v1/energy` 或 `/v1/history`。
3. **把 `stale: true` 的读数当实时值**：先检查 `stale` / `age_s`。
4. **请求 `step` 小于 60**：会被 400 拒绝，因为源数据最细就是 1 分钟。
5. **把 `coverage` 当「占满一天的比例」**：分母是记录跨度；判断「今天采了多少」请结合 `first_sample_at`。
6. **把 `total_kwh` 当电源终身电量**：只是本机开始记录以来的累计。
7. **轮询超过 2 Hz**：没有任何新信息，只会增加开销。

## 13. Unix socket 等价接口

不愿开端口时，用守护进程的 socket（`$XDG_RUNTIME_DIR/himos-psu/daemon.sock`，NDJSON），
发送一行 JSON，收到的 `payload` 与 HTTP 响应体**结构相同**：

```
{"cmd": "query", "what": "status"}
{"cmd": "query", "what": "history", "hours": 24, "step": 300}
{"cmd": "query", "what": "energy", "range": "today", "bands": true}
{"cmd": "query", "what": "meta", "sn": false}
```

回复：`{"event": "ack", "cmd": "query", "ok": true, "payload": {...}}`。

## 14. 兼容性策略

- 路径版本固定为 `/v1`；字段**只会新增**，不会改名、改单位或删除
- 新增字段不影响旧消费者；请忽略不认识的键
- 破坏性变更会走 `/v2`，`/v1` 至少保留一个发布周期
