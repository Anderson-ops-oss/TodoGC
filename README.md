# TodoGC

> 一款严肃的**企业级认知内存管理中间件**。
> ……说人话：它把你的待办清单当成程序的堆内存，用垃圾回收算法冷酷地识别并回收那些"你其实这辈子都不会做"的僵尸任务。

![python](https://img.shields.io/badge/python-3.10%2B-blue)
![tests](https://img.shields.io/badge/tests-27%20passing-brightgreen)
![deps](https://img.shields.io/badge/dependencies-0-success)
![人生覆盖率](https://img.shields.io/badge/%E4%BA%BA%E7%94%9F%E8%A6%86%E7%9B%96%E7%8E%87-12%25-red)
![GC throughput](https://img.shields.io/badge/GC%20throughput-98%25-success)

普通 todo app 帮你**添加**任务；TodoGC 帮你**诚实地删除**任务。

一个任务如果长期和你的真实目标"不可达"（从没推进、反复推迟），就会被标记为垃圾，触发一场逼你二选一的回收仪式——**复活**它，或者承认你这辈子不会做。

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
```

核心**零运行时依赖**，只用标准库；`rich` 是可选增强，没装就自动回退纯文本。需要 Python 3.10+。

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
todogc gc                                    # 触发回收 + Stop-the-world 仪式
todogc gc --dry-run                          # 只出报告，不进仪式
todogc graveyard                             # 悼词墙：看看你都埋了什么
todogc restore 3                             # 手滑了？从垃圾桶捞回来
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

## 判活规则

一个任务判为**存活**，当且仅当：

```
alive = 关联到某个活跃 root
     && (最近 30 天内真碰过  ||  近 30 天有真推进)
     && 推迟次数 < 5
```

外加一条：互相担保、却谁都不连 root 的**引用环**，整环回收——这正是引用计数 GC 抓不到、只有可达性分析能揪出来的自欺盲区。阈值都在 `GCPolicy` 里，自己调到"确实有点心虚"。

## 架构

```
todogc/
  heap.py       领域模型(Task / Touch / 分代枚举)
  store.py      SQLite 持久化 + 自动埋点 + 判活指标
  collector.py  可达性标记 + Tarjan 抓环 + 判活清除
  report.py     JVM 风格 GC 日志 + 堆内存图 + 候选清单 + 打脸文案
  cli.py        命令行 + Stop-the-world 回收仪式
```

## 测试

```bash
pip install -e ".[dev]"
pytest
```

## 状态与路线图

**已实现**：全部 MVP（录入 / 触碰追踪 / 可达性 + 循环引用 / 回收仪式 / 垃圾桶恢复）、JVM 风格 GC 日志、`finalize()` 遗言（含"只能复活一次"）、分代打脸、Metaspace 元任务、`-Xmx` 堆容量约束（`add` 满则触发 Allocation Failure、绝不自动删）、`rich` 彩色输出（可选）、堆内存图（`heap` / `jmap` 分代快照 + -Xmx 占用条 + 僵尸标红）。

**还没做（欢迎 PR）**：

- 弱 / 软引用优先级分级（不同回收激进度）

> 说明：任务的"年龄"目前在**回收仪式里被复活时** +1（即"你逃过它几次"），而非每轮 GC 自动 +1。

## License

MIT
