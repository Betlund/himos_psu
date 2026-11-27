# 只读 HTTP API

守护进程独占串口，所以它同时提供一个**只读**的 HTTP JSON 接口，供外部程序（Python / C++ / Rust / Shell /
Prometheus）读取当前功耗、24 小时曲线和电量统计，用来做功耗优化、看板或告警。

- 只绑定 `127.0.0.1`，默认关闭，需要 `--http-port` 显式开启
- **只读**：没有任何端点会修改电源、数据库或守护进程状态
- `/v1` 视为稳定契约：字段只会新增，不会改名或删除
- 数据过期会被标记（`stale` / `age_s`），不会把旧值当成实时值糊弄你

## 开启

```bash
./bin/himos-psu daemon --http-port 8790
```

装成 systemd 用户服务时把端口写进单元：

```bash
./bin/himos-psu systemd-daemon --install --enable --http-port 8790
systemctl --user restart himos-psu-collect
```

需要鉴权时加 `--http-token <一串随机字符>`，之后请求要带
`Authorization: Bearer <token>`（只支持请求头，不接受 URL 参数）。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/status` | 当前功率、各路电压、三路温度、风扇、故障码 + 今日/本月/累计电量 |
| GET | `/v1/history?hours=24&step=300` | 功率曲线；`step` 取 `60/300/900/3600`，`hours` ≤ 336 |
| GET | `/v1/energy?range=today` | 电量与电费；`range` 取 `today/yesterday/week/month/year`，`bands=1` 带分时段 |
| GET | `/v1/meta` | 型号、固件、厂商、额定功率；序列号需 `?sn=1` |
| GET | `/v1/health` | 存活与数据新鲜度，适合探活 |
| GET | `/metrics` | Prometheus 文本格式 |
| GET | `/` | 端点和 schema 版本的简单索引 |

### `/v1/status`

```json
{
  "schema": 1,
  "ts": 1795761234.5,
  "age_s": 0.3,
  "stale": false,
  "state": "采集中",
  "power_w": 212.4,
  "input_voltage_v": 228.4,
  "frequency_hz": 50.0,
  "rails_v": {"12": 12.13, "5": 5.01, "3.3": 3.31, "5vsb": 5.01},
  "temps_c": {"hv": 34.5, "lv": 36.1, "env": 24.4},
  "fan_rpm": 480,
  "fan_mode": "custom",
  "fan_mode_id": 3,
  "fault_code": 0,
  "unit": {"model": "KE-1300P", "firmware": "V4.24.09.20", "vendor": "segotep", "rated_power_w": 1300},
  "energy": {"today_kwh": 1.56, "today_cost_yuan": 0.87, "month_kwh": 8.79,
             "total_kwh": 8.78, "recorded_days": 3}
}
```

`age_s` 是最近一帧遥测距今的秒数，`stale` 在超过 5 秒没有新帧时为 `true`。
守护进程刚启动、还没连上电源时 `power_w` 为 `null`，其余字段仍会返回。

### `/v1/history`

```json
{
  "schema": 1, "step_s": 300, "unit": "W",
  "start": 1795755600, "end": 1795762800,
  "points": [
    {"ts": 1795755600, "avg": 145.2, "min": 130.0, "max": 260.1},
    {"ts": 1795755900, "avg": null, "min": null, "max": null}
  ]
}
```

- 数据来自 1 分钟桶，`avg/min/max` 都是**分钟内均值的统计**，不是 1 秒尖峰
- 没有采集的槽位是 `null`，**不是 0 W**，画图时请按断点处理
- 起点按 `step` 向下取整，所以 1 小时可能返回 12 或 13 个点

### `/v1/energy`

```json
{
  "schema": 1, "range": "today",
  "kwh": 1.5615, "cost_yuan": 0.867, "avg_w": 145.6, "peak_w": 414.6,
  "coverage": 1.0, "gap_s": 0.0, "active_s": 38618.0,
  "start": "2026-11-27T00:00:00", "end": "2026-11-27T10:43:39",
  "first_sample_at": "2026-11-27T00:00:00"
}
```

`coverage` 是「有数据的时间 / 记录跨度」，不是「占满一天的比例」。

## 语言示例

### Shell / curl

```bash
curl -s http://127.0.0.1:8790/v1/status | jq '.power_w, .energy.today_kwh'
curl -s 'http://127.0.0.1:8790/v1/history?hours=24&step=300' | jq '.points | length'
```

### Python（标准库，无依赖）

```python
import json, time, urllib.request

def power(url="http://127.0.0.1:8790/v1/status"):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())["power_w"]

while True:
    print(f"{power():.1f} W")
    time.sleep(1)
```

仓库里带了一个更完整的例子：`examples/api_poll.py`（支持 CSV 落盘）。

### C++（libcurl）

```cpp
// g++ poll.cpp -lcurl
#include <curl/curl.h>
#include <cstdio>
static size_t sink(void* data, size_t size, size_t n, void* out) {
  ((std::string*)out)->append((char*)data, size * n);
  return size * n;
}
int main() {
  CURL* curl = curl_easy_init();
  std::string body;
  curl_easy_setopt(curl, CURLOPT_URL, "http://127.0.0.1:8790/v1/status");
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, sink);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
  curl_easy_perform(curl);
  std::puts(body.c_str());   // 用你喜欢的 JSON 库解析 power_w
  curl_easy_cleanup(curl);
}
```

### Rust（reqwest + serde）

```rust
#[derive(serde::Deserialize)]
struct Status { power_w: Option<f64> }

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let status: Status = reqwest::blocking::get("http://127.0.0.1:8790/v1/status")?.json()?;
    println!("{:?} W", status.power_w);
    Ok(())
}
```

## Prometheus

```yaml
scrape_configs:
  - job_name: himos-psu
    static_configs:
      - targets: ["127.0.0.1:8790"]
    metrics_path: /metrics
```

暴露的指标：`himos_psu_power_watts`、`himos_psu_input_voltage_volts`、`himos_psu_frequency_hertz`、
`himos_psu_fan_rpm`、`himos_psu_temp_celsius{sensor="hv|lv|env"}`、`himos_psu_fault_code`、
`himos_psu_energy_kwh{range="today|month|total"}`、`himos_psu_energy_cost_yuan{range="today"}`、
`himos_psu_stale`。

## 复用现有 Unix socket

不想开端口也可以走守护进程的 Unix socket（`$XDG_RUNTIME_DIR/himos-psu/daemon.sock`），
发送 NDJSON 请求即可拿到**同样结构**的 JSON：

```
{"cmd": "query", "what": "status"}
{"cmd": "query", "what": "meta", "sn": true}
{"cmd": "query", "what": "energy", "range": "today", "bands": true}
{"cmd": "query", "what": "history", "hours": 24, "step": 300}
```

回复形如 `{"event": "ack", "cmd": "query", "ok": true, "payload": { ... }}`。

## 常见问题

- **没开 `--http-port` 时能读历史吗？** 能，但只能走 socket 的 `query`，或者直接用 CLI/GUI。
- **轮询频率建议？** 1 秒足够（PSU 本身 1 秒一帧）；10 秒以上适合看板。`/v1/status` 里的电量块有 2 秒缓存，不会每次都查库。
- **要做写入（改风扇）吗？** v1 不提供；等读接口稳定后再开带 token 的写端点。
- **多台机器/远程访问？** 目前只绑 loopback。要跨机器请用 SSH 端口转发，而不是改 bind 地址。
