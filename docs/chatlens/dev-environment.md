# WSL + Windows 混合开发环境使用指南

本文档规定 ChatLens（基于 Moect 的群聊分析扩展）在 **WSL2（Linux）+ Windows 原生** 混合模式下的目录布局、环境隔离、开发流程与测试分工。

---

## 1. 环境角色与职责分工

| 能力域 | WSL2（Linux） | Windows 原生（Win 10/11 x64） |
| --- | --- | --- |
| Coding Agent / 源码编辑 / Git 与 PR 管理 | **主环境** | 同步拉取验证（仅读/测试分支） |
| `analytics/` 跨平台分析核心（Adapter、预处理、L1/L2/L3 检测、聚类、Provider、报告） | **主开发与单测环境** | 集成运行与回归 |
| 合成数据（SQLite / JSONL+媒体）单元测试与 Mock Provider 测试 | **主环境**（秒级反馈） | 全量回归 |
| 前端 TypeScript 类型检查与纯前端单元测试 | 可执行（需使用 WSL 独立 `node_modules` 或跳过） | **主构建环境**（生成发行用 `ui-qce/`） |
| Moect 微信 4.x 进程接入、密钥获取、DAT/表情缓存内存恢复 | 不支持（禁止为 WSL 重构 Win32 模块） | **唯一运行与验证环境** |
| pywebview / WebView2 桌面 GUI、DPAPI 加密、Playwright Edge UI 回归 | 不支持 | **唯一运行与验证环境** |
| PyInstaller 打包（`build.py`）与 Inno Setup 安装器（`installer/build.ps1`） | 不支持 | **唯一打包环境** |
| 真实授权群聊样本覆盖度测试与端到端桌面验收 | 仅可读取已授权导出的离线 JSONL+媒体副本（绝不入库） | **完整端到端验收环境** |

---

## 2. 仓库存放与文件系统规范（严禁跨系统混用依赖目录）

### 2.1 推荐双克隆布局（最佳实践）

1. **WSL 侧开发克隆（ext4 原生文件系统）**：
   - 路径示例：`/home/<user>/PyProjects/ChatLens`（当前工作区即位于 WSL2 ext4）。
   - 优势：Git 操作、Python/Node 文件 I/O 与 pytest 速度比 `/mnt/c` 快一个数量级，且避免 NTFS 权限与符号链接问题。
2. **Windows 侧验证克隆（NTFS 原生文件系统）**：
   - 路径示例：`C:\Dev\ChatLens`。
   - 通过 Git 分支同步（`git push origin <branch>` 后在 Windows 侧 `git fetch && git checkout <branch>`，或将 WSL 裸路径仅作只读 Git remote：`git remote add wsl \\wsl$\Ubuntu\home\<user>\PyProjects\ChatLens`），**绝不在 Windows 中直接激活 WSL 目录里的 `.venv` 或 `node_modules`，也绝不在 WSL 中直接激活 Windows 目录里的 `.venv` 或 `node_modules`**。

### 2.2 行尾与 Git 规范

- 本仓库 Python/TS/Markdown 源码以 LF 为主，PowerShell（`.ps1`）与 Inno Setup（`.iss`）在 Windows 编辑时避免批量改写换行符。
- 提交前始终运行 `git diff --check`，确保无尾随空白、冲突标记或误纳入的缓存/产物。
- **PR 目标规范**：所有开发在特性分支（如 `docs/cl-00-phase0-audit`、`feat/cl-01-adapter`）完成，**PR 仅提交至 Fork 仓库 `yuanbaoerer/ChatLens` 的 `main` 分支，禁止自动合并，禁止直接向 `upstream`（`mofan6/Moect`）开 PR**。使用 `gh` 时始终显式指定 `--repo yuanbaoerer/ChatLens`。

---

## 3. 依赖隔离规范

### 3.1 Python 虚拟环境隔离（必须严格区分）

`.gitignore` 已忽略 `.venv*/`（`.gitignore:3`），建议按平台显式命名以便一眼区分：

| 环境 | 推荐路径 | Python 版本 | 安装依赖范围 |
| --- | --- | --- | --- |
| **WSL 分析核心环境** | `.venv-wsl/`（在 WSL 克隆内） | Python `3.11.x`（推荐 `uv venv --python 3.11 .venv-wsl`） | **仅安装跨平台依赖**：`Pillow==12.0.0`、`zstandard==0.25.0`、`pycryptodomex==3.24.1`、`psutil==7.2.2`、`XlsxWriter==3.2.9`、`pyzipper==0.3.6`，以及后续 `analytics/` 引入的跨平台库与测试工具（如 `pytest`）；**不安装** `pywebview`、`pythonnet`、`PySide6`、`pyinstaller`、`pysilk-mod`、`pilk` |
| **Windows 桌面全量环境** | `.venv-win/`（在 Windows 克隆内） | Windows Python `3.11.x` x64 | 完整安装 `pip install -r requirements.txt`（及可选 `playwright` 用于 Edge UI 回归） |

> **为什么不在 WSL 运行 `pip install -r requirements.txt`？**
> `requirements.txt` 包含 `pythonnet==3.2.1`、`pywebview==6.1`、`PySide6==6.12.0`、`pyinstaller==6.22.3` 与 C 扩展 `pysilk-mod`/`pilk`，在 Linux 既不需要（WSL 不跑桌面宿主/打包），又会引入庞大的桌面二进制或平台编译失败。

### 3.2 Node.js / 前端依赖隔离与 `ui-qce/` 产物纪律

- **禁止跨系统共用 `frontend/node_modules/`**：esbuild、Rollup、Tailwind CSS v4（Lightning CSS / Oxide）均包含平台原生二进制插件，Windows 与 Linux 的 `node_modules` 不可互换。
- **独立安装**：
  - Windows 克隆内执行 `npm ci --prefix frontend` 安装 Windows 原生包并执行 `npm run build --prefix frontend`；
  - 若在 WSL 克隆内执行 `npm ci --prefix frontend` 进行 `tsc --noEmit` 类型检查，仅使用 WSL 克隆自己的 `frontend/node_modules/`。
- **`ui-qce/` 提交纪律**：`frontend/vite.config.ts:19` 将构建结果直接输出至仓库跟踪目录 `ui-qce/`。**仅在前端代码实际变更并完成构建验证的 PR 中提交 `ui-qce/` 改动**；纯后端或纯文档 PR 严禁附带 `ui-qce/` diff。

---

## 4. 日常开发与验证命令清单

### 4.1 WSL 侧常用命令

```bash
# 1. 创建并激活 WSL 独立 Python 3.11 环境（仅需一次）
uv venv --python 3.11 .venv-wsl
source .venv-wsl/bin/activate
uv pip install Pillow==12.0.0 zstandard==0.25.0 pycryptodomex==3.24.1 psutil==7.2.2

# 2. 运行 WSL 当前可直接执行的 Moect 跨平台基线单测
.venv-wsl/bin/python -m unittest -v tests/test_windows_version.py tests/test_publisher_startup.py

# 3. 验证跨平台数据读取层导入正常（不触发 wx4/media.py Win32 阻塞）
.venv-wsl/bin/python -c "import core.data, wx4.reader, wx4.forward; print('OK')"

# 4. 运行 Analytics Core 跨平台测试（离线、无微信）
.venv-wsl/bin/python -m unittest discover -s tests/analytics -p "test_*.py" -v

# 5. 提交前差异与格式检查
git diff --check
git status -sb
```

分析适配器的 `page_messages` 按时间和消息 ID 分页。从同一来源和时间范围重建快照时，内容变动会使旧游标失效。需要连续浏览多页时，可先调用 `build_snapshot`，再在返回的快照上调用 `Snapshot.page_messages`，避免重复读取源文件和检查媒体。

### 4.2 Windows 原生侧常用命令（PowerShell）

```powershell
# 1. 创建并激活 Windows 独立 Python 3.11 环境（仅需一次）
py -3.11 -m venv .venv-win
.\.venv-win\Scripts\Activate.ps1
pip install -r requirements.txt
npm ci --prefix frontend

# 2. 前端类型检查与构建（更新 ui-qce/）
npm run build --prefix frontend

# 3. 运行 Moect 全量后端单元测试（含 SQLite/资源/导出/任务/在线协议）
python -m unittest discover -s tests -p "test_*.py" -v

# 4. 启动 WebView2 桌面演示模式（不接触真实微信数据）
python edge_app.py --demo

# 5. 启动 WebView2 真实微信集成模式（连接本机已登录微信 4.x）
python edge_app.py

# 6. 构建 Windows 独立程序与一键安装包（需 Inno Setup 6.5+）
python build.py
.\installer\build.ps1 -OutputDirectory .\release
python scripts/package_release.py .\release --no-source
```

---

## 5. 测试矩阵与诚实披露原则

| 测试层级 | 执行位置 | 触发时机 | 失败/未执行时的汇报规则 |
| --- | --- | --- | --- |
| Analytics Core 单元测试 & 合成 SQLite/JSONL 夹具测试 | WSL + Windows | 每次提交前（WSL 必跑） | 必须实测通过并给出精确命令与用例数 |
| Moect 跨平台子集单测（`test_windows_version`, `test_publisher_startup`） | WSL | 每次提交前 | 必须实测通过 |
| Moect 全量 32 个 Python 单测（含 `core.service`、`core.resources`、DPAPI） | Windows 原生 | 涉及 `core/`、集成缝合或发布前 | 若仅在 WSL 完成修改，PR 中必须明确标注 **未在 Windows 运行（BLOCKED）** |
| 前端 `tsc --noEmit` & `vite build` | WSL（仅类型检查） / Windows（含构建） | 涉及 `frontend/` 改动时 | 报告实际运行的命令与退出码 |
| Playwright + Edge UI 回归、WebView2 `--demo`、真实微信 4.x 接入、打包安装 | Windows 原生 | GUI 集成、Phase 0 完整验收、发布前 | **严禁在未于 Windows 实机执行的情况下标记为通过** |

---

## 6. 隐私、凭据与本地数据卫生

1. **禁止入库项**：真实微信数据库（`*.db`、`*.db-wal`、`*.db-shm`）、密钥（`*keys*.json`、`*.pem`）、真实导出聊天记录与媒体（`exports/`、`snapshots/`）、模型 API Key（`.env*`）、个人配置（`config.json`）。这些模式已在 `.gitignore:15-31` 中忽略；后续 CL-01/CL-02 新增本地分析工作区或本地评估数据集目录时，需先核对并补充 `.gitignore` 规则再写入文件。分析工作区应位于仓库之外；`AnalysisService` 在 workspace 上持有 `.analysis-service.lock`，同一工作区只允许一个实例，避免启动/关闭恢复误中断其他实例的运行。
2. **本地授权样本隔离**：在 Windows 导出用于评估或调试的真实授权群聊 JSONL/媒体文件，应统一存放在仓库之外（如 Windows `C:\Temp\chatlens-local-eval\` 或 WSL `~/chatlens-local-eval/`），通过环境变量传参给本地评估脚本，避免误 `git add`。
3. **API 密钥管理**：开发期仅使用本地环境变量或 Windows 凭据存储，不在源码、测试夹具或日志中打印明文密钥与原始群成员 ID。
4. **日志与遥测**：不自动上传聊天记录、不静默触发远端媒体下载、不在错误信息中回显聊天正文或密钥片段（与 `core/service.py:427-430` 的脱敏约定保持一致）。
