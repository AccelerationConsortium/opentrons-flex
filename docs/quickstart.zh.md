# Opentrons Flex SiLA 连接器：从零到可用

适合第一次接触这个连接器的人。全程大约 10 分钟，其中真正需要你动手的只有几步。

## 1. 准备

- 一台电脑（macOS、Linux 或 Windows），和 Flex 在同一个网络里。
- Windows 需要先装 [Git for Windows](https://git-scm.com/download/win)（部署脚本要用到其中的 `sh`）。仅用模拟器可以不装。
- 不需要提前装 Python 或其他工具，下面的脚本会自动安装。

## 2. 一条命令安装并开始引导

```sh
git clone https://github.com/AccelerationConsortium/opentrons-flex.git
cd opentrons-flex
./bootstrap.sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

脚本会安装 uv、Python 3.12、连接器和 `flexctl`，然后问你：**先试模拟器，还是连真机？**

## 3a. 先试模拟器（不需要机器人）

选 `s`。模拟器会在本机启动（SiLA 端口 50051）。另开一个终端检查：

```sh
uv run flexctl verify 127.0.0.1 --allow-simulator
```

想同时要 Opentrons HTTP API：`uv run flexctl sim --with-http`（第一次会从 GitHub 安装 robot-server）。

## 3b. 连接真机

选 `r`，引导会依次完成：

1. **找机器人**：自动在网络里搜索 Flex；搜不到时输入 IP（触摸屏：Settings > Network）。
2. **SSH 钥匙**：Flex 只接受两种方式添加钥匙：
   - 用网线直连电脑和机器人，并使用机器人的 `169.254.x.x` 地址；或者
   - 只有 Wi-Fi 时用 U 盘：请用**空的** U 盘（Flex 会把 U 盘上所有 .pub 都加为 root 登录密钥），引导会把公钥拷进去，你把 U 盘插到 Flex 上再按回车。
3. **机器人软件包**：自动从 GitHub Actions 下载和当前代码版本一致的包；没有 GitHub 权限时用 Docker 本地构建。
4. **部署并启动**：输入你的名字（用于审计记录），自动部署、切换模式、预检。
5. **冒烟测试**：只读检查，不会动机器人。

## 4. 真机硬件测试（站在机器人旁边）

```sh
uv run flexctl verify <机器人IP> --level motion   # 约 2 分钟：归位 + 急停
uv run flexctl verify <机器人IP> --level all      # 再加：tip 核对、重启保持、模块拔插
```

每一步都会告诉你要做什么、期待什么结果。按回车继续，按 `s` 跳过。结束后会生成一个 JSON 报告。各步骤含义见 [hardware_verification.md](hardware_verification.md)。

## 5. 日常命令

| 做什么 | 命令 |
|---|---|
| 看连接器状态 | `uv run flexctl status <IP>` |
| 看日志 | `uv run flexctl logs <IP> --follow` |
| 换回 Opentrons 原厂模式 | `uv run flexctl mode <IP> opentrons` |
| 切回连接器 | `uv run flexctl mode <IP> pe`（或 `unit`） |
| 更新代码后重新部署 | `uv run flexctl up <IP>` |
| 回退到上一个版本 | `uv run flexctl rollback <IP>` |
| 清理缓存和旧软件包 | `uv run flexctl clean --yes` |

## 常见问题

- **SSH 钥匙装不上（bad-interface）**：说明你不是通过网线直连的 `169.254.x.x` 地址访问。改用网线，或按提示用 U 盘。
- **找不到软件包**：运行 `uv run flexctl build`。它会说明是缺 GitHub 权限（`gh auth login`）还是缺 Docker。
- **模块拔掉后又插回（同一台，按序列号识别）**：不需要任何操作，连接器会自动恢复。换成同类型的另一台（不同序列号）不会被自动接管，需重启连接器。插入一种启动时没有的新模块类型时，连接器会在完全空闲（READY、无 run、无进行中的命令或温控/PCR 程序）时自动重启并注册它。
