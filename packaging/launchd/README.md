# research OOS 外部调度 (launchd 模板)

这套模板把一个 **一次性** 调度入口挂到 macOS launchd，每个交易日盘后调用同一条 CLI：
`backend/scripts/run_research_scheduler.py`。它只编排既有命令，不常驻、不下单、不复制
observer/ST/退市逻辑。

> **安装前提 (Phase 11 门槛, 缺一不可)**
> 1. 2026-10-08 首次影子成交已人工复核通过；
> 2. 已手动执行本入口 ≥1 次成功、并连续观察 ≥2 个交易日的 **成功 / 失败 / 重复运行** 行为；
> 3. 上游日线、instruments、ST、退市覆盖已原子对齐（否则 preflight 会 fail-closed）。
> 在满足以上三条之前，**不要** `launchctl bootstrap`。

## 组成

| 文件 | 作用 |
|---|---|
| `com.tsp.research-oos.plist.template` | launchd 任务定义（周一~周五 18:30，宿主机本地时区） |
| `run_research_oos.sh.template` | 包装脚本：固定环境 → 算 `--as-of` → 取当日 generation → 调调度入口（`--skip-sync`） |

`--skip-sync` 的前提是**后端自身的盘后管道已先完成**日线与 instruments 同步；外部任务只负责
研究侧（ST/退市覆盖 + preflight + observer）。若要让本入口自己做同步，改为传
`--sync-daily-command` / `--sync-instruments-command`（见调度入口 `--help`），此时
generation 请在同步**之后**再取。

## 占位符（安装前全部替换为绝对路径）

| 占位符 | 含义 | 示例 |
|---|---|---|
| `__REPO__` | 仓库根绝对路径 | `/Users/you/Projects/apps/tsp` |
| `__DATA_DIR__` | 生产数据目录 | `/Users/you/Projects/apps/tsp/data` |
| `__UV_BIN__` / `__UV_BIN_DIR__` | `uv` 可执行文件 / 其所在目录 | `/Users/you/.local/bin/uv` |
| `__WRAPPER__` | 包装脚本部署后绝对路径 | `/Users/you/.local/bin/run_research_oos.sh` |
| `__TRADING_CALENDAR__` | 冻结的换行分隔交易日历（须跨过 `--as-of` 当月） | `.../research/a_share_universe/trading_calendar.txt` |
| `__EXCLUSION_HISTORY__` | PIT 退市整理期事件 parquet | `.../20260930T-oos-exclusion-history-v1/exclusion_history.parquet` |
| `__LOG_DIR__` | launchd 输出日志目录 | `/Users/you/Library/Logs/tsp-research-oos` |

## 安装

```sh
# 1. 部署包装脚本 (替换占位符 + chmod)
sed -e 's#__REPO__#/ABS/REPO#g' ... packaging/launchd/run_research_oos.sh.template > ~/.local/bin/run_research_oos.sh
chmod +x ~/.local/bin/run_research_oos.sh

# 2. 部署 plist (替换占位符)
sed -e 's#__REPO__#/ABS/REPO#g' -e 's#__WRAPPER__#/Users/you/.local/bin/run_research_oos.sh#g' \
    ... packaging/launchd/com.tsp.research-oos.plist.template \
    > ~/Library/LaunchAgents/com.tsp.research-oos.plist
plutil -lint ~/Library/LaunchAgents/com.tsp.research-oos.plist   # 语法校验

# 3. 先手动跑包装脚本一次 (不经 launchd), 确认 observation 正常生成
~/.local/bin/run_research_oos.sh

# 4. 通过后再加载
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tsp.research-oos.plist
launchctl enable  gui/$(id -u)/com.tsp.research-oos
```

## 状态检查

```sh
launchctl print gui/$(id -u)/com.tsp.research-oos | sed -n '1,40p'   # state/last exit/stderr path
launchctl list | grep com.tsp.research-oos                          # PID + 上次退出码
tail -f "$LOG_DIR/launchd.err.log"                                   # 任务级 stderr
cat "$DATA_DIR/research/a_share_oos/_scheduler_logs/as_of=<date>/scheduler.jsonl"  # 逐 run 记录
```
`launchctl list` 第二列是上次退出码：`0` = 成功（含幂等短路）；非 0 见 `scheduler.jsonl`
与 `as_of=<date>/*.log` 定位是哪一步 / 哪个 preflight 检查失败。

## 手动触发一次（不改安装）

```sh
launchctl kickstart -k gui/$(id -u)/com.tsp.research-oos
```

## 卸载 / 回滚

```sh
# 卸载
launchctl bootout gui/$(id -u)/com.tsp.research-oos || true
launchctl disable gui/$(id -u)/com.tsp.research-oos || true
rm -f ~/Library/LaunchAgents/com.tsp.research-oos.plist

# 回滚到旧配置: 保留安装前的 plist 备份, bootout → 还原备份 → 重新 bootstrap
cp ~/Library/LaunchAgents/com.tsp.research-oos.plist ~/Library/LaunchAgents/com.tsp.research-oos.plist.bak
# ... 需要回滚时 ...
launchctl bootout gui/$(id -u)/com.tsp.research-oos || true
mv ~/Library/LaunchAgents/com.tsp.research-oos.plist.bak ~/Library/LaunchAgents/com.tsp.research-oos.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tsp.research-oos.plist
```

## 记录项（安装时填写，便于日后运维）

- 任务标签：`com.tsp.research-oos`
- 运行时间：周一~周五 18:30（**宿主机本地时区**；非 Asia/Shanghai 需换算 `Hour` 或统一机器时区）
- 工作目录 / 环境：`__REPO__`，`DATA_DIR`，`TZ=Asia/Shanghai`（仅影响 Python 日期逻辑，不影响 launchd 触发时刻）
- 状态检查：`launchctl print` / `launchctl list` / `_scheduler_logs`
- 卸载/回滚：见上

## 幂等与安全

- 同日 `as_of=` observation 已存在 → 调度入口视为**幂等成功**（退出 0），不覆盖旧产物。
- 任一步失败 → 立即非零退出，observer 不运行、不留半成品。
- 本机制**不下真实订单**；observer 仅产出 paper-only 观察包。
