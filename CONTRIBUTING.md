# 贡献指南

感谢你有兴趣参与维护 dump工具！无论是修 bug、加功能、改翻译质量、补文档，都非常欢迎。

## 协作模式

本仓库采用**完全开放**的协作模式：

- **主分支不设保护规则**，被加入 Collaborator 的成员可以直接 push
- **任意人**都可以 Fork → 修改 → 提交 Pull Request
- PR 合并后会成为项目历史的一部分，贡献者会被记录在提交记录中
- 欢迎在 [Issues](../../issues) 提 bug、提需求、讨论设计

> 想成为 Collaborator 获得直接 push 权限？持续贡献几个有质量的 PR 后，
> 在 Issue 里说明即可，维护者会邀请你加入。

## 贡献流程

### 1. Fork & 克隆

```bash
# 在 GitHub 上点 Fork，然后：
git clone https://github.com/<你的用户名>/dump-tool.git
cd dump-tool
git remote add upstream https://github.com/tcmmm/dump-tool.git
```

### 2. 建分支

```bash
git checkout -b feat/你的功能名      # 新功能用 feat/
git checkout -b fix/你的修复名       # bug 修复用 fix/
git checkout -b docs/你的文档名      # 文档用 docs/
```

### 3. 开发与自测

- GUI 源码运行：`python dump_gui.py` 或双击 `启动工具.bat`
- 依赖：`pip install PySide6`（或 PySide2 / PyQt5/6）
- 改完功能请本地跑一遍，确保 GUI 能正常启动、你改的部分能正常工作
- 涉及 Hook 代码生成的改动，建议用一份真实的 `dump.cs` 验证输出正确

### 4. 提交

提交信息请清晰描述改动，推荐格式：

```
<类型>: <简述>

<详细说明（可选）>
```

类型：`feat`（新功能）/ `fix`（修复）/ `docs`（文档）/ `refactor`（重构）/ `chore`（杂项）

示例：
```
feat: 地址反查支持带 0x 前缀与不带前缀两种输入
fix: 加载超大 dump.cs 时结构树展开卡顿
docs: 补充 MCP 接入示例
```

### 5. 同步上游（避免冲突）

```bash
git fetch upstream
git rebase upstream/main    # 或 merge
```

### 6. 推送 & 提 PR

```bash
git push origin feat/你的功能名
```

然后到 GitHub 上发起 Pull Request，填写改动说明。维护者会 review 并合并。

---

## 代码风格

- **Python**：遵循 PEP 8，缩进 4 空格，行宽建议 ≤ 120
- 命名清晰优先，避免过度缩写
- 新增功能尽量配中文注释说明意图
- 大段逻辑建议拆函数，单函数别超过 ~80 行

## 文档同步

- 新增 / 改动功能请同步更新 [使用说明.md](使用说明.md) 对应章节
- 涉及用户可见行为变化的，在 PR 描述里写清楚

## 敏感信息

- 提交前确认**没有**包含真实设备 IP、具体游戏包名、个人 hook 目标等隐私信息
- 配置类文件用 `*.example.json` 模板入库，真实配置已被 `.gitignore` 排除
- frida-server 等大二进制不入库，README 写明下载方式

## 讨论

- 重大改动（新增大功能、改架构）请先开 Issue 讨论方向，避免白做
- 不确定的设计可以在 Issue 里 @ 维护者

再次感谢你的贡献！
