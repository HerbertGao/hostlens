## 上下文

仓库根目录目前有 6 个设计文档 + `.gitignore` + GitHub 私有仓库已建立 + 项目级 `.claude/` 配置，**完全没有 `src/` 代码**。Python 生态有大量"项目脚手架"的选型分歧（packaging backend / dependency manager / lint 工具 / 配置库 / 日志库），M0 需要一次性把这些选型锁死并固化到 `pyproject.toml`、`.pre-commit-config.yaml`、`.github/workflows/`，**让 M1+ 所有后续工作有一致的工程基线**。

利益相关者：
- **未来的 AI 协作者** —— 打开仓库就能 `pip install -e ".[dev]"` 跑起来，不需要再做选型决策
- **面试官 / 简历读者** —— 看 `pyproject.toml` 与 CI 配置就能判断「这个项目工程基线靠谱」
- **现在的我** —— 把全部"次要决策"在 M0 一次性解决，M1+ 专心写业务

约束（继承自 CLAUDE.md + README）：
- 技术栈已锁定（Python 3.11+ async / Anthropic SDK 原生 / Pydantic v2 / Typer + Rich / pytest-asyncio + VCR / structlog + OTel）
- 不引入 LangChain / LlamaIndex / LangGraph
- mypy `--strict` 必须能过
- CLI 必须支持 `--json` 输出 schema 稳定

## 目标 / 非目标

**目标**：

- 在 M0 收尾时，`pip install -e ".[dev]"` 一行命令完成所有依赖安装
- `hostlens --help` / `hostlens doctor --json` 是 M0 唯一可运行的命令，输出 schema 稳定
- `pytest` / `pre-commit run --all-files` / `mypy --strict src/` 全部 exit 0
- GitHub Actions CI 在 Python 3.11 + 3.12 matrix 下绿
- 一份 `.pre-commit-config.yaml` 让所有 PR 自动跑 ruff + mypy
- 异常基类 / 配置 / 日志骨架已就位，M1+ 可直接 `from hostlens.core.exceptions import HostlensError`

**非目标**（M0 不做的决策推迟到对应 milestone）：

- 不引入 `uv` 替代 pip（pip + pyproject extras 在 M0 足够；`uv` 评估推迟到 M1 真依赖装多了再说）
- 不上 `commitlint` / Conventional Commits 校验工具（commit 风格目前靠人工 + Co-Authored-By；M3+ 再考虑）
- 不上 `coverage` 强制门槛（M0 只有 2 个 smoke 测试，定门槛=自欺欺人；M2 引入 Agent loop 后定 80%）
- 不上 `pre-commit-ci.com` 远程钩子托管（本地 + GH Actions 双层够用）
- 不上 ADR-001 之外的「为什么不用 X」论证（架构文档已覆盖）
- 不实现任何业务能力（Inspector / Backend / Tool Registry 等全部 M1+）

## 决策

### D-1：包布局采用 `src/` layout 而非 flat layout

**选择**：项目根 / `src/hostlens/<modules>/` / `tests/`

**替代方案**：
- (a) flat layout（`hostlens/<modules>/` 与 `tests/` 平级）—— 简单但 pytest 容易撞 import path（开发模式 `pip install -e .` 之前 import 都从源码走，CI 与开发环境行为可能不一致）
- (b) `src/` layout（推荐）—— 强制走 `pip install` 安装路径，CI 与开发环境一致；pytest 必须配置 `pythonpath = ["src"]` 才能从源码 import

**理由**：CLAUDE.md §3 已确定 `src/hostlens/` 布局；`src/` layout 是 PyPA 当前推荐做法（packaging.python.org），主流大型项目（Django / FastAPI / Pydantic v2）都用。

### D-2：打包后端选 `hatchling`

**选择**：`pyproject.toml` 顶部 `[build-system] requires = ["hatchling"]` + `build-backend = "hatchling.build"`

**替代方案**：
- (a) `setuptools`（传统）—— 老牌但需要额外配置 `find_packages`；对 `src/` layout 兼容性需要显式 `package_dir`
- (b) `hatch` / `hatchling`（推荐）—— PEP 517 现代化，原生支持 `src/` layout，单文件 `pyproject.toml`，无需任何 `setup.py` / `setup.cfg` / `MANIFEST.in`
- (c) `poetry` —— 同时管理依赖 + 打包但锁定 `poetry.lock` 格式与 PEP 621 不完全兼容（虽然 2024 后有改善）
- (d) `pdm` —— 类似 poetry 但社区更小

**理由**：hatchling 由 PyPA 维护，与 PEP 621 标准 100% 兼容，写法最干净；不锁定额外工具链（用户可继续用 pip / uv / poetry 装）。

### D-3：依赖管理：`pyproject.toml` extras 分组，**不**引入 lock 文件

**选择**：PEP 621 标准——`[project]` 表中 `dependencies = [...]` 数组列核心运行时依赖（**不是** `[project.dependencies]` 子表，那不是合法 TOML 表名）；`[project.optional-dependencies]` 表分 `dev` / `mcp` / `docs` 三组；不生成 `requirements.txt` / `uv.lock`

**替代方案**：
- (a) 现状：仅 pyproject extras —— 简单，依赖版本约束写在 extras 里；不锁版本（开发期靠 lazy update）
- (b) 加 `requirements.lock` / `uv.lock` —— 锁版本，CI 复现性更好，但维护 lock 文件本身有成本

**理由**：M0 依赖很少（核心运行时仅 typer/rich/pydantic/pydantic-settings/structlog；dev 只是 pytest 系列 + ruff + mypy + pre-commit）；lock 文件的"复现性"收益与维护成本不匹配。**M2 引入 Anthropic SDK / asyncssh 等真依赖后**重新评估 `uv lock`（届时再发独立 OpenSpec 提案）。

### D-4：lint / format 用 `ruff` 一体化

**选择**：`ruff check` + `ruff format` 替代 `black` + `isort` + `flake8` + `pyupgrade`

**替代方案**：
- (a) `black` + `isort` + `flake8` + `pyupgrade` —— 经典组合但 4 个工具，pre-commit 跑得慢
- (b) `ruff` —— Rust 实现，10x-100x 速度；一个工具覆盖 lint + format + import sort + pyupgrade；2024 已是 Pydantic / FastAPI / Anthropic 等主流项目的选择

**理由**：速度 + 单一工具 + 主流采用。配置在 `pyproject.toml` 单文件即可。

### D-5：类型检查用 `mypy --strict` 而非 `pyright`，**仅对 `src/` 跑 strict**

**选择**：`mypy --strict src/` 在 CI 强制 0 error；`tests/` 不参与 strict 检查（pyproject `[tool.mypy] exclude = ['tests/']`）

**替代方案**：
- (a) `mypy`（标准）—— Python 类型检查事实标准，与 Pydantic v2 集成成熟
- (b) `pyright`（Microsoft，Pylance 底层）—— 速度更快，但 strict 模式有时过于激进；CI 配置稍复杂；与 Pydantic v2 集成需要 `pyright-pylance-bundle`

**关于排除 tests 的 tradeoff**：
- **支持排除**：测试代码常用 `Mock()` / fixture 返回 `Any`；strict 下需要大量 `cast()` 与 `# type: ignore` 影响可读性；测试本身的"类型"是次要价值（行为正确性才是核心）。这与 Pydantic / FastAPI 等主流项目实践一致。
- **反对排除**：弱化"全栈类型安全"基线信号；test helper 可能成为 untyped dumping ground。
- **决策**：M0 阶段排除 tests；**未来 milestone 加 sub-task**：若 test helpers 增长到 >500 行时单独发提案给 tests 加 lighter typing（`disallow_untyped_defs = true` 但不开 strict 全部 flag）。

**理由**：mypy 更稳；CLAUDE.md §6 已锁定 mypy --strict；Pydantic 团队官方支持 mypy 插件（`pydantic.mypy`），strict 模式下 Pydantic v2 model 自动得到完整类型推断。

### D-6：测试栈 `pytest` + `pytest-asyncio` + `pytest-cov`

**选择**：`pytest-asyncio` 用 `asyncio_mode = "auto"`（自动识别 async 测试函数），无需 `@pytest.mark.asyncio` 装饰

**替代方案**：
- (a) `unittest` + `unittest.IsolatedAsyncioTestCase` —— stdlib 无新依赖但语法啰嗦
- (b) `pytest` + manual asyncio_mode —— 需要每个 async test 显式装饰
- (c) `pytest` + `asyncio_mode = "auto"`（推荐）—— 写 `async def test_xxx` 就跑

**理由**：CLAUDE.md §2 已锁定 pytest + pytest-asyncio；auto mode 减少样板代码，与 async-first 风格一致。

### D-7：配置库用 `pydantic-settings`（M0 仅 env + .env 两源；YAML 推到 M1+）

**选择**：`Settings(BaseSettings)` 模型，**M0 仅从 env + `.env` 文件读取**；`~/.config/hostlens/*.yaml` 加载推到 M1+（用 `pydantic_settings.YamlConfigSettingsSource` 实现，届时单独立 OpenSpec 提案）。M0 阶段 doctor 只**探测**`config_dir` 目录可读性，**不**读任何 yaml 文件内容。

**替代方案**：
- (a) `os.environ` 裸读 —— 无类型，无校验
- (b) `python-decouple` —— 简单但无 Pydantic 集成
- (c) `dynaconf` —— 功能多但学习曲线陡
- (d) `pydantic-settings`（推荐）—— Pydantic v2 官方推荐配套库，与 `BaseModel` 强类型完全对齐

**关于 YAML 来源推迟**：M0 阶段没有需要从 yaml 读的复杂配置（业务字段全部在 M1+），现在引入 yaml source 是 over-engineering。M1 第一次需要 yaml 配置（如 `~/.config/hostlens/targets.yaml`）时同步引入 yaml source 更合理。

**理由**：技术栈已锁定 Pydantic v2；pydantic-settings 是同生态首选；M2+ 配置 schema 复杂化时无需重写。

### D-8：日志库用 `structlog`，**不**用 `loguru`

**选择**：`structlog` + `dev` 模式 human-readable / `prod` 模式 JSON

**替代方案**：
- (a) stdlib `logging` —— 标准但结构化日志需要大量样板
- (b) `loguru` —— 简单好用但 OpenTelemetry 集成不如 structlog
- (c) `structlog`（推荐）—— 结构化原生 + 与 OpenTelemetry context propagation 集成最干净（M5+ 用）

**理由**：CLAUDE.md §2 已锁定 structlog + OTel；OTel 集成是关键考虑（Agent 调用链追踪是简历亮点之一）。

### D-9：CLI 用 `Typer` + `Rich`，doctor 输出 `--json` 与 human 两种模式

**选择**：Typer app 注册子命令；doctor 默认人类可读（Rich console 渲染表格 + 颜色），`--json` flag 输出机器可解析 JSON

**doctor JSON schema**（specs delta 定义稳定契约）：

```json
{
  "version": "0.1.0",
  "timestamp": "2026-05-22T12:34:56+08:00",
  "checks": {
    "python_version": {"status": "ok",      "detail": "3.11.7"},
    "anthropic_key":  {"status": "present", "detail": null},
    "config_dir":     {"status": "ok",      "detail": null, "path": "~/.config/hostlens"}
  },
  "ready": true
}
```

**Status enum 统一**：`"ok" | "present" | "missing" | "unreadable" | "error"`。约定：
- `python_version` / `config_dir` 等"健康度"检查用 `"ok"` 表示通过
- `anthropic_key` 这种"存在性"检查用 `"present"` / `"missing"`（区别于"有值是否合法"的健康度语义）
- **`anthropic_key.detail` 必须为 `null`** —— 即使密钥存在也禁止打印任何前缀 / 后缀 / 掩码值，存在性检查不需要任何值
- 其他 check 出现意外失败用 `"error"` 并在 detail 写脱敏错误描述

**Schema 演进政策（区分 required vs optional）**：
- **Required 字段**（顶层 `version` / `timestamp` / `checks` / `ready`；每个 check 的 `status`）—— snapshot 锁定，任何变更都是 **breaking** 必须 bump `version` 字段且更新 spec
- **Optional 字段**（每个 check 的 `detail` / 特定 check 的 `path` 等附加 metadata）—— 允许在不 bump version 的前提下新增；删除或语义变更视为 breaking
- 新增 check（如 M1+ 加 `target_connectivity`）—— 算 optional addition，不 bump version 但需更新 spec

**理由**：JSON 输出是 doctor 给 Agent 调用方 ping 的契约（CLAUDE.md §4.9 全局规范）；schema 早定早稳，但要给 add-only 演进留口子（snapshot 测试只锁 required 字段）。

### D-10：CI 用 GitHub Actions matrix Python 3.11 + 3.12，**不**测 3.13

**选择**：`strategy.matrix.python-version: ["3.11", "3.12"]`

**理由**：3.13 在 2024-10 才正式发布；anthropic SDK / asyncssh 等关键依赖在 3.13 上的稳定性 / wheel 可用性可能滞后。3.13 推到 M1+ 真用上这些库时再上 matrix。

### D-11：异常基类层次

**选择**：

```python
class HostlensError(Exception):
    """Hostlens 所有自定义异常的基类。"""

class ConfigError(HostlensError): ...           # 配置加载/校验失败
class TargetError(HostlensError): ...           # ExecutionTarget 错误 (M1 后用)
class InspectorError(HostlensError): ...        # Inspector 错误 (M1 后用)
```

后续 milestone 可继续扩展（`BackendError` / `NotifierError` 等）。**M0 只定义这 4 个**，避免 over-engineering。

## 风险 / 权衡

| 风险 | 缓解措施 |
|---|---|
| `mypy --strict` 在 Pydantic v2 model 上可能误报 | 启用 `plugins = ["pydantic.mypy"]`，Pydantic 官方提供完整类型推断 |
| `ruff` 默认 rule set 太严可能与 CLAUDE.md "不写无意义注释" 冲突 | `pyproject.toml` 显式启用 / 禁用规则集，按 CLAUDE.md §6 约定调整（如禁用 `D100` "Missing docstring in public module"） |
| `pre-commit` 在新人本地未装时 commit hooks 不跑 | CI 强制 `pre-commit run --all-files`，无人能绕过 |
| Python 3.11 vs 3.12 行为差异（如 `asyncio` 改动） | matrix CI 两个都测；任一失败立刻暴露 |
| 不锁 lock 文件未来"在我机器上能跑"问题 | M0 依赖很少（≤10 个）；约束写得宽松（`>=` 而非 `==`）；M1+ 真依赖装多了立即引入 uv lock |
| doctor JSON schema 后续要加新字段会破坏向后兼容 | schema 顶层加 `version` 字段；新增字段 only，**不**改字段语义；删除字段视为 breaking 需要新 OpenSpec 提案 |
| GitHub Actions free tier 配额（2000 min/month）M0 阶段够用，M1+ 测试更多可能不够 | M0 暂不担心；CI 跑全量 ≤2min（小项目），月余 1000+ 次 OK |

## Migration Plan

M0 是首次创建，**无 migration**。但定义"如何验证 M0 已完成"：

```bash
# 任何 reviewer 可独立验证 M0 退出条件
git clone git@github.com:HerbertGao/hostlens.git && cd hostlens
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 全部 exit 0 即代表 M0 完成
hostlens --help                                     # 1
hostlens doctor --json | jq -e '.ready != null'    # 2
pytest -v                                           # 3
pre-commit run --all-files                          # 4
mypy --strict src/                                  # 5

# GitHub Actions
gh run list --workflow=ci.yml --limit 1            # 最近一次 CI 必须是 success
```

## Open Questions

| 问题 | 暂定立场 | 何时重评 |
|---|---|---|
| 引入 `uv` 替代 pip？ | 暂不（pip + extras 够用） | M2 真依赖增多后 |
| 引入 `nox` / `tox` 多版本测试编排？ | 暂不（GH Actions matrix 够用） | 1.0 后如需本地多版本测试再上 |
| pytest 覆盖率门槛设多少？ | 不设硬门槛 | M2 引入 Agent loop 后定 80% |
| 引入 `commitizen` / commitlint？ | 暂不 | M3+ 多人协作时再上 |
| `.pre-commit-config.yaml` 是否锁版本（`autoupdate_schedule`）？ | 锁版本（reproducibility） | dependabot 每月自动 PR |
| 是否引入 `dependabot.yml`？ | **M0 引入**（task 8.4）—— 监控 pip / github-actions / pre-commit 三种 ecosystem，weekly schedule，commit prefix `chore(deps):` | — (已决定) |
| 是否引入 `.editorconfig`？ | **M0 引入**（task 1.5）—— 统一缩进 / EOL / 编码，防止 IDE 风格漂移影响 ruff format | — (已决定) |
