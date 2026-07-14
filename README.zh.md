# TodoGC

> 一款严肃的**企业级认知内存管理中间件**。
> ……说人话：它把你的待办清单当成程序的堆内存，用垃圾回收算法冷酷地识别并回收那些"你其实这辈子都不会做"的僵尸任务。

![python](https://img.shields.io/badge/python-3.10%2B-blue)
[![CI](https://github.com/Anderson-ops-oss/TodoGC/actions/workflows/ci.yml/badge.svg)](https://github.com/Anderson-ops-oss/TodoGC/actions/workflows/ci.yml)
![deps](https://img.shields.io/badge/dependencies-0-success)
![人生覆盖率](https://img.shields.io/badge/%E4%BA%BA%E7%94%9F%E8%A6%86%E7%9B%96%E7%8E%87-12%25-red)
![GC throughput](https://img.shields.io/badge/GC%20throughput-98%25-success)

普通 todo app 帮你**添加**任务；TodoGC 帮你**诚实地删除**任务。

一个任务如果长期和你的真实目标"不可达"（从没推进、反复推迟），就会被标记为垃圾，触发一场逼你二选一的回收仪式——**复活**它，或者承认你这辈子不会做。

*([English README here](README.md))*

---

## 核心隐喻

| 内存管理 | 映射到你的待办 |
|---|---|
| 堆对象 | 一条待办 |
| GC roots | 你声明的活跃目标 |
| 可达性 | 任务还连不连得到某个目标 |
| 引用计数 | 你真推进/编辑它的次数（光看不算） |
| 循环引用 | 两个任务互相当理由、却都不连目标——最阴险的僵尸 |
| 分代 / 晋升 | 逃过多轮回收的老赖，晋升老年代并被打脸 |
| Metaspace | "变得更自律"这类关于任务的元任务，常驻不回收 |
| Stop-the-world | 回收仪式：世界暂停，逐个处置僵尸 |

## 安装

```bash
pip install -e .            # 开发模式；装完 `todogc` 就是一条真命令
pip install -e ".[color]"   # 想要彩色输出(rich 表格 + GC 日志高亮)就带上 color
pip install -e ".[web]"     # 想要 Web 端(Streamlit 堆内存图 + add/gc/restore 交互)就带上 web
```

核心**零运行时依赖**，只用标准库；`rich`(彩色)与 `streamlit`(Web 端)都是可选增强——没装 rich 自动回退纯文本，没装 streamlit 也不影响 CLI 与测试。需要 Python 3.10+。

## 用法

```bash
todogc add "这季度上线支付重构" --root     # #1 声明一个活跃目标(GC root)
todogc add "写对账模块" --root-id 1         # #2 挂到目标 #1 下
todogc progress 2 "完成核心逻辑"            # 真推进(+1 refcount)

todogc add "学 Rust"                         # #3 ┐ 互相担保的一对僵尸
todogc add "用 Rust 重写博客"                # #4 ┘
todogc dep 3 4                               # #3 的理由是 #4
todogc dep 4 3                               # #4 的理由是 #3 —— 成环

todogc ls                                    # 看看你的堆
todogc heap                                  # 堆内存图：分代分布 + -Xmx 占用 + 僵尸标红(别名 jmap)
todogc web                                   # 把这张堆图搬进浏览器 + 交互(需 [web]，见下)
todogc watch                                 # 定时体检：状态跃迁/堆占用告警时推 ntfy(需 [notify]，见下)
todogc --lang zh gc                          # 输出语言切换为中文(默认英文，见下 v0.4)
todogc gc                                    # 触发回收 + Stop-the-world 仪式
todogc gc --dry-run                          # 只出报告，不进仪式
todogc graveyard                             # 悼词墙：看看你都埋了什么
todogc restore 3                             # 手滑了？从垃圾桶捞回来
todogc show 3                                # 详情 + 泄漏指标
todogc history 3                             # append-only 事件时间线
todogc edit 3 "学完 Rust 第一章" --ref weak   # 编辑 + strong/soft/weak
todogc link 3 1                              # 重连父目标；--off 解除
todogc doctor                                # Cognitive Leak Detector
todogc stats --since 30d                     # GC 历史统计
```

### 一次 `gc --dry-run` 长这样

```
[运行 0d00h][info][gc] GC(0) Pause Young (Allocation Failure)
  活跃堆         : 4 -> 2 tasks (上限 50)
  本轮回收       : 2 zombies   →  释放认知内存 50.0%
  reference cycle: 1 处  「学 Rust」 ⇄ 「用 Rust 重写博客」
  [Times: 焦虑=0.2h  自责=0.1h  实际推进=0.0h]

━━ 回收候选清单 (2 项) ━━━━━━━━━━━━━━━━
[reference cycle #0] 互相担保，谁都不连 root：
   ⇄ #3 学 Rust
   ⇄ #4 用 Rust 重写博客
```

### 一次 `heap`（别名 `jmap`）长这样

```
━━ 堆内存快照 · jmap -heap ━━━━━━━━━━━━━━━━
  -Xmx 占用: 5 / 50 obj  [██░░░░░░░░░░░░░░░░░░] 10.0%   (Metaspace 不计)

  ┌ Eden · 新生代 (5)
  │   ★ #1 上线支付重构
  │     #2 写对账模块
  │   ☠ #3 学 Rust  (cycle #0)
  │   ☠ #4 用 Rust 重写博客  (cycle #0)
  │   ☠ #5 孤儿任务  (unreachable)
  ├ Survivor · 幸存区 (0)
  │   (空)
  ├ Old · 老年代 (0)
  │   (空)
  ├ Metaspace · 元空间 (1)
  │   ⊛ #6 变得更自律
  └──────────────
  图例: ★ root · ☠ 回收候选(僵尸) · ⊛ 元任务
```

装了 `rich` 时：僵尸行整行标红、`★` root 描金、`-Xmx` 占用条上色；`jmap` 是它的别名（致敬 JDK 那个真的会打印堆内存图的命令）。

### Web 端（Streamlit）长这样

```bash
pip install -e ".[web]"
todogc web                 # 起本地服务、浏览器打开(--port 改端口)
```

同一套内核、同一张堆图，只是从终端搬进浏览器：

- **-Xmx 占用条** + 四代卡片（Eden / Survivor / Old / Metaspace）：僵尸行**标红**、`★` root 描金、`⊛` 元任务品红——配色语义与 CLI 一致
- **Stop-the-world 仪式** 直接在页面点：逐个僵尸选「复活 / 回收 / 跳过」、填 `finalize()` 遗言，一键执行——走的还是 CLI 那套 `run_ceremony`（`finalize` 只调一次、复活会晋升分代）
- 侧栏 **add**（含 -Xmx 满则 Allocation Failure 拒绝分配）、**墓园**一键 `restore`
- **任务详情**支持 edit/progress/snooze/link/dep 与事件时间线；**Leak Inspector** 和 GC 历史与 CLI 同源

Web 端是可选的：没装 `streamlit`，`todogc web` 只会友好提示去装，绝不影响其它命令；浏览器里的自动刷新不算「查看」（不虚增 view 埋点）。

## v0.2：策略、引用强度与认知泄漏诊断

项目目录可放一个 `todogc.toml`（完整样例见 `todogc.toml.example`）：

```toml
[gc]
mode = "normal"                 # gentle / normal / aggressive
recent_days = 30
max_consecutive_skips = 5
restore_grace_days = 7

[heap]
max = 50
```

优先级为 CLI 参数 > 环境变量 > `todogc.toml` > 默认值。`strong` 可达即保留；
`soft` 使用常规活跃度规则；`weak` 必须在时间窗内有真推进。推进、编辑、复活和恢复会
清零“连续推迟”，但历史累计推迟仍保留用于审计；恢复任务默认享有 7 天宽限期。

数据库会自动从旧版迁移到 schema v3；迁移文件库前先生成同目录 `.bak`。真实执行的 GC
写入审计历史，`heap` 与 `gc --dry-run` 始终只读。

## v0.3：定时体检(ntfy 推送)

`todogc watch` 让判活不用你手动敲命令去看——按 `todogc.toml` 的 `[notify]`/`[watch]` 配置
（完整字段见 `todogc.toml.example`），轮询判活、状态跃迁时推 [ntfy](https://ntfy.sh) 通知：

```toml
[notify]
ntfy_enabled = true
ntfy_topic = "你的-topic"        # 建议自建一个不容易被猜到的 topic 名；受保护 topic 才需要 ntfy_token

[watch]
poll_seconds = 1800              # 多久判活一次
digest_seconds = 86400           # 没有新事件也定期报个平安
remind_days = 7                  # 候选状态没变时，隔多少天再提醒一次
heap_threshold_pct = 80          # 堆占用告警阈值(%)
```

推送是**事件驱动**的，不是心跳污染——参考真实 JVM 只在实际发生一次 GC 时才打日志，
`todogc watch` 只在下列"跃迁"发生时才推(候选数量不变、状态没变一律静默)：

- 任务从存活跃迁为候选(新僵尸 / 新孤儿 / 新循环引用)。
- 候选状态本身发生变化(比如从 stale 变成陷入循环引用)。
- 同一候选状态持续太久(超过 `remind_days` 没处理)，复议期到，再提醒一次。
- 堆占用**新跨过** `heap_threshold_pct` 那一刻(持续高位不会重复告警)。

复活回到存活会静默清空去抖记录(这是好消息，不推送，但下次再堕落会被当"新"跃迁重新提醒)。
另外每隔 `digest_seconds` 会发一条心跳摘要，证明 watcher 还活着，不必疑神疑鬼。

`todogc watch` 起一个单实例建议性锁——如果你已经在跑 `todogc web`(且配了
`notify.ntfy_enabled`)，它会在 web 服务进程里自动带一条同款后台线程，此时再手动开
`todogc watch` 会检测到锁被占用、自动放弃，不会对同一份跃迁重复推送两遍。

## v0.4：中英文双语(i18n)

默认输出语言是**英文**；中文作为可选语言，三种途径切换，优先级从高到低(`--lang` >
环境变量 > 配置文件 > 默认值)：

```bash
todogc --lang zh gc                  # 1) 命令行参数，只影响这一次调用
TODOGC_LANG=zh todogc gc             # 2) 环境变量
```

```toml
[i18n]
lang = "zh"                          # 3) todogc.toml 里的持久化默认值
```

`todogc --help` 本身的文案也会跟着 `--lang`/环境变量切换；但如果只在配置文件里设置了
语言、没传 `--lang` 也没设环境变量，`--help` 仍显示默认语言(读配置文件前需要先解析出
`--config` 参数，这一步做不到"先读配置再决定 help 用什么语言")，命令实际执行的输出
则会正确地跟着配置文件走。

Web 端(`todogc web`)不读上面这套——它有自己的**按浏览器 session** 的语言切换器(侧栏
顶部的 `Language / 语言` 下拉框)，默认值取自 `todogc.toml` 的 `[i18n] lang`，但切换只
影响当前 session，不会写回配置文件、也不会影响其他人同时打开的页面。后台 `todogc watch`
推送用的语言只认 `[i18n] lang`(或 `TODOGC_LANG`)，跟 Web 端 UI 显示语言是两回事——这样
多人同时开着 Web 端也不会互相改到别人的推送语言。

> 目前覆盖 CLI 输出与 `--help` 文案、Web 端界面文案、ntfy 推送文案、配置/存储层报错信息。
> GC 日志、堆内存图、`doctor`/`stats` 的具体输出内容(`report.py`)暂时保留原样，不受
> `--lang` 影响——这块还没做，欢迎 PR。

## 判活规则

一个任务判为**存活**，当且仅当：

```
alive = 关联到某个活跃 root
     && (最近 N 天有有效活动  ||  近 N 天有真推进)
     && 连续推迟次数 < max_consecutive_skips
```

`DEFER` 和 `VIEW` 不刷新有效活动时间；只看不做、只往后推都不能保活。GC root 与
Metaspace 仍默认常驻，`doctor` 会单独审计它们是否正在制造认知内存泄漏。

外加一条：互相担保、却谁都不连 root 的**引用环**，整环回收——这正是引用计数 GC 抓不到、只有可达性分析能揪出来的自欺盲区。阈值都在 `GCPolicy` 里，自己调到"确实有点心虚"。

## 架构

```
todogc/
  config.py     todogc.toml + 环境变量配置（Python 3.10 仍保持零依赖）
  i18n.py       轻量文案目录(t()/n_()) + 语言解析，零依赖替代 gettext
  locales/      en.py / zh.py 两份文案字典(key 集合一致，由测试兜底)
  heap.py       领域模型(Task / Touch / 分代枚举)
  store.py      SQLite 持久化 + 自动埋点 + 判活指标
  collector.py  可达性标记 + Tarjan 抓环 + 判活清除
  report.py     JVM 风格 GC 日志 + 堆内存图(heap_view 视图模型，CLI/Web 共用) + 候选 + 打脸
  notify.py     ntfy 推送渠道(JSON 发布，零依赖，纯 urllib)
  watcher.py    定时体检核心：事件驱动 + 去抖 + 心跳摘要 + 单实例建议性锁
  cli.py        命令行 + Stop-the-world 回收仪式 + todogc watch + --lang
  webapp.py     Streamlit Web 端(堆内存图 + add/gc/restore 交互 + 自动挂载 watcher + 语言切换，可选)
```

## 测试

```bash
pip install -e ".[dev]"
basedpyright            # Pylance/Pyright：0 errors
ruff check todogc tests
pytest
```

## 打包

核心零依赖，打成标准 wheel / sdist 直接分发（`build` 已含在 `[dev]` 里）：

```bash
pip install -e ".[dev]"
python -m build          # 产出 dist/todogc-0.4.0-py3-none-any.whl 与 .tar.gz
pip install dist/todogc-0.4.0-py3-none-any.whl   # 到哪都能装
```

`dist/`、`build/`、`*.egg-info/` 都在 `.gitignore` 里，产物不入库。想连 Web 端一起：`pip install "todogc[web]"`。

## 状态与路线图

**已实现**：全部 MVP（录入 / 触碰追踪 / 可达性 + 循环引用 / 回收仪式 / 垃圾桶恢复）、JVM 风格 GC 日志、`finalize()` 遗言（含"只能复活一次"）、分代打脸、Metaspace 元任务、`-Xmx` 堆容量约束（`add` 满则触发 Allocation Failure、绝不自动删）、`rich` 彩色输出（可选）、堆内存图（`heap` / `jmap` 分代快照 + -Xmx 占用条 + 僵尸标红）、Web 端（`web`：Streamlit 堆内存图 + add/gc/restore 交互，可选）、标准打包（`python -m build` → wheel / sdist）。

**v0.2 已实现**：数据库迁移与备份、连续推迟/恢复宽限期、strong/soft/weak、
gentle/normal/aggressive、show/edit/history/link、doctor、GC 历史 stats、Web 任务详情与
Leak Inspector、输入校验与对抗测试。

**v0.3 已实现**：`todogc watch` 定时体检、ntfy 推送(事件驱动 + 去抖复议期 + 心跳摘要 +
堆占用告警边沿触发)、单实例建议性锁、Web 端按配置自动挂载后台 watcher 线程。

**v0.4 已实现**：中英文双语，默认英文；`--lang`/环境变量/`todogc.toml` 三级优先级；Web
端按 session 独立切换语言；CLI 输出、`--help`、Web 界面、ntfy 推送、配置/存储层报错全部
覆盖(`report.py` 的 GC 日志/堆内存图暂不在内)。

**后续候选（欢迎 PR）**：GitHub Issues/PR 同步、TODO/FIXME 导入、交互式引用图、`report.py`
的 GC 日志/堆内存图文案纳入 i18n。

> 说明：任务的"年龄"目前在**回收仪式里被复活时** +1（即"你逃过它几次"），而非每轮 GC 自动 +1。

## License

MIT
