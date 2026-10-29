# AS-MS 工作流：通过 SiLA gRPC 运行（分支 `feat/asms-grpc-workflow`）

这份文档说明怎样只用 connector 的 SiLA gRPC 命令跑 AS-MS 的两段协议：prep，以及修正版的 wash/elute。整个过程**不上传协议，也不创建 Protocol Engine run**。第 1–3 节说明做法和模拟器上的验证结果，第 4 节是连机步骤。

| 运行名 | 协议文件 | 用到的硬件 |
|---|---|---|
| `prep` | `protocols/asms/asms_single_point_plate_prep.py`（原样复制自你的文件） | 左 8 通道 50 µL、右 8 通道 1000 µL、C1 温控模块 |
| `wash-elute` | `protocols/asms/asms_single_point_wash_and_elute.py`（仓库里修正过的版本） | 右 8 通道 1000 µL、gripper、B2 磁力架、C1 温控模块 |

wash/elute 用的是仓库修正版：甲醇加进 elution 板，再从 elution 板转到 MS 板。你 Downloads 里那份是把甲醇加进 AS-MS 板，再从 AS-MS 板转出。

---

## 1. 做法

1. **离线分析**：用 Opentrons 9.0 自己的协议分析（`opentrons analyze`）生成完整的命令列表。所有绝对坐标、流速（µL/s）、tip 长度、延时和搬板动作都来自 Protocol Engine，没有任何一项是手算的。
2. **编译**：`asms_sila_workflow compile` 把命令列表翻译成 SiLA 步骤，写成一份 JSON 计划，运行前可以先检查。规则如下：
   - 遇到不认识的命令直接拒绝，不会只跑一部分。
   - 吸液前如果活塞没有准备好，和 Protocol Engine 一样，先移到孔顶，在空中准备活塞，再下降吸液。
   - 混合时的分液用新命令 `DispenseWithoutPushOut`，不做 push-out，活塞可以接着原地吸液。
   - 丢 tip 到垃圾桶时，Protocol Engine 记录的是整个 8 通道的中心位置，SiLA `DropTip` 要的是 A1 tip 的位置。编译时用 Opentrons 模拟器算出两者的差值并换算（8 通道是 y + 31.5 mm）。
   - prep 每次只取 1 根 tip（取 H1、G1、F1…）。编译时会把喷嘴配置成只用 A1 一个，并检查同一列前面的 tip 已经取走，否则其余喷嘴会压到 tip 上，这时直接拒绝。
3. **执行**：`asms_sila_workflow run` 通过 gRPC 一条一条发送 SiLA 命令。孔与孔之间的移动都是：先抬到行程顶部（取 tip 后读回的高度），水平移过去，再垂直下降。

为此在本分支上给 connector 加了三项：
- `PipetteController.ConfigureTipType`，按 tip 体积选 tip 类型；
- `PipetteController.SetFlowRates` 和 `GetFlowRates`，单位 µL/s；
- `MotionController.DispenseWithoutPushOut`。

另外放宽了一条检查：分液量低于 pipette 最小体积时也允许，和 Protocol Engine 一致；最小体积仍然限制吸液。这是因为 prep 每孔分 2.5 µL。

---

## 2. 模拟器上验证了什么

测试在 `tests/test_asms_sila_workflow.py`。它用 `create_app` 起一个真实的 connector gRPC 服务：
- OT3 模拟器，左 `p50_multi_v3.5`，右 `p1000_multi_v3.5`，加 gripper；
- 模拟温控模块；
- 仓库里的 ASMS gripper plan。

工作流通过 gRPC 通道发命令，和真机走的是同一条路。

| 项目 | 结果 |
|---|---|
| prep 完整运行 | 428 步全部完成：11 次单 tip 取放、33 次吸液、69 次分液、60 次 touch tip |
| wash/elute 完整运行 | 648 步全部完成：26 次整列取 tip（6 次放回 tip 盒）、98 次吸液（含 8 次空气隙）、66 次分液、12 次吹液、9 次搬板 |
| 搬板 | 用到全部 4 个 ASMS gripper plan；结束时 deck ledger 显示 A1 = asms-plate、B2 = elution-plate |
| 移动路径 | 每次 `MoveTo` 都在测试里检查过：要么垂直，要么在行程顶部水平移动 |
| 预检 | deck ledger 不对、pipette 型号不对时直接拒绝，机器人不动 |
| 编译拒绝 | 不认识的命令、会让喷嘴压到 tip 的取 tip 顺序，都会拒绝编译 |

**模拟器测不到的**：真实的坐标偏差、液体行为、抓板是否抓稳、温控实际降温需要的时间。

---

## 3. 和 Opentrons App 原生运行的差别（连机前要知道）

1. **没有 LPC 偏移**：坐标是 Protocol Engine 的标称坐标，等于在 App 里不加 labware offset 直接跑。第一次一定要不加液空跑，盯着看位置。
2. **放回 tip 不刮**：Protocol Engine 放回 tip 时会刮一下（`scrape_tips`），SiLA `DropTip` 不刮。wash/elute 会把 tip 放回 C3 的 A5–A8 列，之后再取出来复用。
3. **prep 的单 tip 取法**：原协议是 8 通道在整列配置下偏着取，物理上只装上 1 根 tip，取 tip 的力度却按 8 根算。SiLA 版本改成只用 A1 一个喷嘴，按 1 根 tip 的力度取，取的位置和顺序完全不变。
4. **prep 的 2.5 µL 分液**：低于 8 通道 50 µL pipette 标称的 5 µL 最小体积，准确度要另外确认。Opentrons App 也是这样跑的。
5. **tip 盒**：编译时假定每次运行开始时 tip 盒都是满的。wash/elute 会用完两盒 tip（192 根），一根都不能缺。
6. **temperature 等待**：`SetTemperatureAndWait` 最长等 30 分钟。降到 4 °C 的实际时间要在真机上确认。

---

## 4. 连机步骤

**① 部署本分支的 connector**（新命令必须在机器人上）

```bash
git switch feat/asms-grpc-workflow
uv run flexctl up <robot-ip> --mode unit
```

`flexctl up` 会按当前代码检查 wheel 包，缺了或过期了就去取。用 GitHub Actions 取 wheel 需要先把这个分支推上去；不想推的话，用 Docker 在本地构建（`flexctl build`）。`connector-unit` 模式自带 ASMS 的 gripper plan（`/var/sila2_flex/asms-unit-labware-movement.json`），不用改配置。

**② 生成计划并检查**（不连机器人）

```bash
uv run python -m unitelabs.opentrons_flex.asms_sila_workflow compile prep --output prep.plan.json
uv run python -m unitelabs.opentrons_flex.asms_sila_workflow compile wash-elute --output wash-elute.plan.json
```

**③ 只做预检**（不加 `--execute`，机器人不动）

```bash
uv run python -m unitelabs.opentrons_flex.asms_sila_workflow run --plan prep.plan.json --host <robot-ip> --port 50051
```

预检检查这几项：
- 机器状态：E-stop、门、是否有错误；
- 两个 pipette 的型号和安装位置；
- 两个 pipette 上都没有 tip；
- 温控模块已连接；
- wash/elute 还会检查 gripper plan 和 deck ledger。

**④ 空跑**（不加液，只放空板、空 tube、满 tip 盒）

```bash
uv run python -m unitelabs.opentrons_flex.asms_sila_workflow run --plan prep.plan.json --host <robot-ip> --port 50051 \
  --execute --confirm-deck PREP-DECK-READY --report prep-dry.json
```

运行时盯着看：
- 取 tip 是垂直下降；
- 每次去孔都是先升到顶，水平移过去，再垂直下降；
- touch tip 只碰孔壁；
- tip 落进 D1 垃圾桶里，没有落在桶边。

tube rack 上的 snap cap 如果开着会向上翘，要确认没有被 pipette 碰到。有任何异常就按 E-stop。

**⑤ wash/elute 前先记录 deck ledger**

```bash
uv run flexctl deck <robot-ip> reconcile --set A1=asms-plate --set B1=elution-plate --operator <name> --reason "asms wash start" --confirm
uv run python -m unitelabs.opentrons_flex.asms_sila_workflow run --plan wash-elute.plan.json --host <robot-ip> --port 50051 \
  --execute --confirm-deck WASH-ELUTE-DECK-READY --report wash-dry.json
```

第一次搬板时盯着 gripper，确认抓得稳、放到磁力架上是平的。仓库文档里已经提到 Axygen 板的抓取参数还需要在真机上验证。

**⑥ 空跑都没问题后，再用水跑，最后上真实样品。**

**中途出错时**：工作流在出错的那一步停下，并关掉温控模块。它不会自动续跑。先到机器旁确认 tip 状态和板子位置：
- 板子位置不对，用 `flexctl deck reconcile` 更正 deck ledger；
- tip 状态和传感器读数对不上，用 `TipController.ReconcileTipState` 处理；
- 然后换上满的 tip 盒，从头开始跑。

失败时机器人和液体处于什么状态，`--report` 不会记录，只有成功跑完才会写报告。出错的那一步和原因只打印在终端上，记得保存。
