# dump工具 · il2cpp Dump 翻译工作台

> 一款用于**浏览 / 中文翻译 / 搜索** Il2CppDumper 生成的 `dump.cs` 的桌面软件，
> 并集成 Hook 代码生成、Frida 调试、GG 脚本、IDA 命名脚本、ADB 手机调试与 MCP 接入能力。
> 让 Unity il2cpp 游戏的逆向分析、安全研究与自有游戏调试从"对着天书查地址"变成"点一下就出模板"。

---

## ✨ 功能特性

### 浏览与翻译
- **加载 dump.cs**：支持拖拽打开，55MB 大文件首开约 10 秒，二次打开走 `.dcache` 缓存秒开
- **中文翻译**：类 / 方法 / 字段逐词翻译，附带 Unity 生命周期、基类角色等大白话解释
- **结构树**：`分组 → DLL → 用途分类 → 类` 四层可折叠浏览
- **用途分类**：19 类自动归类（界面UI / 战斗技能 / 角色移动 / 背包物品 / 网络通信…）
- **类详情**：中文释义 + 用途 + 基类角色 + 字段（含内存偏移）+ 属性 + 方法（含 RVA 地址）

### 搜索与定位
- **全局搜索**：同时搜类名 / 方法名 / 中文 / 分类名（`Ctrl + K` 快速聚焦）
- **地址反查**：输入 `0xABC1234` 这类地址 → 自动定位到 类.方法 并跳转高亮（GG/Frida/崩溃日志地址都能反查）
- **基类跳转**：详情页显示继承链面包屑，每级可点击直达；自定义类型字段也是链接
- **字符串搜索**：配合 `stringliteral.json` 搜索游戏全部文本，命中给出 metadata 偏移

### 代码生成（核心生产力）
- **Hook 代码生成**：四标签切换 —— 精简模板 / 完整示例（双平台可编译）/ Frida JS（免编译调试）/ 按名解析（游戏更新免改码），一键复制
- **Frida 批量追踪**：整类或全部收藏方法生成一个追踪脚本，运行时打印调用、参数解码、返回值（ARM64 寄存器自动映射，il2cpp 字符串自动解码）
- **Hook 工程脚手架**：生成完整 Android NDK/CMake 工程 zip（`hook_main.cpp` + `offsets.h` + `il2cpp_api.h` + `CMakeLists.txt` + `build.sh` + README），解压、克隆 Dobby、`bash build.sh` 三步出 so
- **GG 脚本生成**：一键生成 GameGuardian Lua 读写骨架，弹窗预览 + 复制 + 导出 `.lua`
- **GG 通用探测器**：生成不绑定类的通用 lua，手机上任意地址粘进去即读出对象类名、基址换算 RVA、对象头部 hexdump
- **IDA 命名脚本**：导出单类 / 全部收藏，在 IDA 里跑脚本自动重命名函数 + 加中文注释

### 跨版本维护
- **版本对比**：选新版 dump.cs，自动输出新增/删除类与方法、RVA 漂移、收藏项受影响清单，可导出全量 md 报告
- **Hook 迁移**：版本对比后，粘贴老 hook 代码 → RVA 一键重写成新版地址（只改能对应到旧 dump 方法表的 0x 常量，掩码/魔数等无关常量原样保留）
- **收藏**：类/方法点 ☆ 收藏，跨会话保存，在版本对比中优先标注变化

### 设备调试
- **ADB 手机调试**：工具栏 ADB 直接连接手机（USB/WiFi）—— 从已装游戏一键提取 il2cpp 材料、扫描 Unity 游戏、推送脚本、Frida 调试、logcat、shell 终端
- **Frida 内嵌运行**：Hook 弹窗「ADB 调试运行」一键把 Frida 版脚本丢到手机上跑（自动 spawn/attach，运行日志实时打印在弹窗内嵌日志区，可随时停止/清空）

### MCP 接入（让 AI 助手直接操作）
- 提供 30 个 MCP 工具，WorkBuddy / Claude 等 AI 助手可直接查询 dump、生成 Hook、操作手机
- 详见 [使用说明.md](使用说明.md) 第七节

---

## 🚀 快速开始

提供三种使用方式，按需选择：

| 方式 | 适合人群 | 说明 |
|------|---------|------|
| **下载 exe** | 普通用户 | 从 [Releases](../../releases) 下载 `dump工具.exe`（GUI）和/或 `dump工具-mcp.exe`（MCP 服务），双击即可运行，无需 Python |
| **源码运行** | 开发者 / 贡献者 | 克隆本仓库，用本机 Python 环境运行（见下方） |
| **自行打包** | 需要定制构建 | 用 PyInstaller 自行打包成 exe（见"构建"一节） |

### 源码运行

```bash
git clone https://github.com/tcmmm/dump-tool.git
cd dump-tool

# 依赖（按需安装，核心 GUI 只需以下库）
pip install PySide6   # 或 PySide2 / PyQt5/6，任选其一

# 启动
python dump_gui.py
# 或双击 启动工具.bat
```

### frida-server（使用 Frida 相关功能时需要）

本仓库**不含** frida-server 二进制（体积较大）。如需使用 Frida 调试 / 批量追踪功能：

1. 前往 [frida releases](https://github.com/frida/frida/releases) 下载对应版本（认准 `android-arm64` 或你设备的架构）
2. 重命名为 `frida-server-<version>-arm64` 放到本程序目录，或自行通过 ADB 推送到手机
3. 详见 [手游Hook详细教程.md](手游Hook详细教程.md) 6.2 节

---

## 🛠 构建（自行打包 exe）

使用 PyInstaller 打包，配置文件见 `dump工具.spec`（GUI 版）和 `dump工具-mcp.spec`（MCP 版）：

```bash
pip install pyinstaller
pyinstaller dump工具.spec          # 产出 dist/dump工具.exe
pyinstaller dump工具-mcp.spec      # 产出 dist/dump工具-mcp.exe
```

> 注：`dump_gui.py` 体积较大（单文件集成全部 GUI 逻辑），首次打包耗时属正常现象。

---

## 📁 项目结构

```
dump-tool/
├── dump_gui.py                 # GUI 主程序（核心，集成全部界面逻辑）
├── adb_kit.py                  # ADB 连接 / 设备操作工具集
├── mcp_entry.py                # MCP 服务入口
├── frontend/                    # 内嵌 Web 前端资源（ADB 调试面板等）
├── 启动工具.bat                 # Windows 源码运行启动脚本
├── dump工具.spec                # PyInstaller GUI 打包配置
├── dump工具-mcp.spec            # PyInstaller MCP 打包配置
├── example_hook_pause_get_base.c # Hook 示例代码（含基址获取完整实现）
├── adb_config.example.json      # ADB 配置示例模板（复制为 _adb_config.json 使用）
├── 使用说明.md                  # 完整功能使用说明（22 项功能详解）
├── 手游Hook详细教程.md          # 从 dump.cs 到 hook 跑起来的完整教程
├── README.md
├── LICENSE
├── CONTRIBUTING.md
└── .gitignore
```

---

## 🤝 贡献

欢迎提交 Pull Request！无论是修 bug、加功能、改翻译、补文档都欢迎。

- 贡献流程与规范见 [CONTRIBUTING.md](CONTRIBUTING.md)
- 新功能请先开 Issue 讨论设计方向，避免重复劳动

---

## 📜 许可证

本项目基于 [MIT License](LICENSE) 开源，可自由使用、修改、分发。

---

## ⚖️ 合规与免责声明

本工具面向 **il2cpp / Unity 运行机制学习、自有游戏调试、单机游戏安全研究**。

- 修改**联网竞技游戏**可能违反游戏用户协议，轻则封号，重则触及法律红线（破坏计算机信息系统）
- 商业级反外挂系统（如 TP/ACE 等）不在本工具对抗范围，也不应被用于对抗
- 传播外挂、代做外挂在国内有明确入罪案例，请勿触碰
- 作者不对工具被用于任何违规用途承担责任，使用者需自行承担相应后果

详见 [手游Hook详细教程.md](手游Hook详细教程.md) 第十节"合规与边界"。
