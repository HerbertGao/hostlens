# project-skeleton 规范

## 目的

定义项目脚手架基线(M0)——项目可通过 pip 一行命令安装、代码质量工具链一键运行、测试基线可跑通、CI 在 push 与 pull request 上自动运行、包结构遵循 src/ layout。

## 需求
### 需求:项目可通过 pip 一行命令安装

仓库必须能通过 `pip install -e ".[dev]"` 单条命令完成全部 dev 环境依赖安装；用户禁止再手动 `pip install` 其他工具（ruff / mypy / pytest 等）。

#### 场景:全新环境安装

- **当** 在干净的 Python 3.11 或 3.12 venv 中执行 `pip install -e ".[dev]"`
- **那么** 安装必须 exit 0，且 `python -c "import hostlens"` 必须能成功 import

#### 场景:CLI 入口注册

- **当** 安装完成后在 shell 执行 `which hostlens`
- **那么** 必须返回 venv 内的 `hostlens` 可执行路径，且 `hostlens --help` exit 0

### 需求:代码质量工具链可一键运行

项目必须配置 ruff / mypy / pre-commit 三件套，运行命令必须为 `ruff check .` / `ruff format --check .` / `mypy --strict src/` / `pre-commit run --all-files`，且全部 exit 0 才算 M0 完成。

#### 场景:lint 检查通过

- **当** 在项目根执行 `ruff check .`
- **那么** 必须 exit 0，stderr 无 warning

#### 场景:格式检查通过

- **当** 在项目根执行 `ruff format --check .`
- **那么** 必须 exit 0，stdout 显示"All checks passed"或等价信息

#### 场景:严格类型检查通过

- **当** 在项目根执行 `mypy --strict src/`
- **那么** 必须 exit 0，输出 "Success: no issues found"

#### 场景:pre-commit hooks 跑通

- **当** 在项目根执行 `pre-commit install && pre-commit run --all-files`
- **那么** 必须 exit 0，ruff + mypy + 标准 hooks（trailing-whitespace 等）全部通过

### 需求:测试基线可跑通

项目必须配置 `pytest` + `pytest-asyncio` (asyncio_mode=auto) + `pytest-cov`，运行 `pytest -v` 必须 exit 0；**必须包含至少 2 个 smoke 测试**：1 个 sync 测试（验证 `hostlens` 可 import）+ 1 个 async 测试（验证 `asyncio_mode = "auto"` 生效，写 `async def test_xxx()` 直接被 pytest 识别）。

#### 场景:pytest 全量运行

- **当** 在项目根执行 `pytest -v`
- **那么** 必须 exit 0，且至少 2 个测试 PASS（1 sync + 1 async）

#### 场景:覆盖率报告生成

- **当** 在项目根执行 `pytest --cov=hostlens --cov-report=term`
- **那么** 必须 exit 0，stdout 包含 "TOTAL" 行与覆盖率百分比

### 需求:CI 在 push 和 pull request 上自动运行

仓库必须含 `.github/workflows/ci.yml`，触发条件为 push 到 main 与所有 pull_request；matrix 必须覆盖 Python 3.11 与 3.12；workflow 必须依次跑 lint / type / test 三步，任一失败必须导致整个 workflow 失败。

#### 场景:push 触发 CI

- **当** 任何 commit push 到 main
- **那么** GitHub Actions 必须在 5 分钟内启动 CI workflow

#### 场景:CI matrix 全绿

- **当** CI workflow 完成
- **那么** Python 3.11 与 3.12 两个 job 都必须 success

#### 场景:lint 失败阻断 CI

- **当** workflow 内 `ruff check .` 返回非 0
- **那么** 整个 workflow 必须 fail，后续 mypy 与 pytest job 也必须不跑（或被标 cancelled）

### 需求:包结构遵循 src/ layout

源码必须放在 `src/hostlens/`，测试必须放在 `tests/`；pyproject.toml 必须显式配置 `pythonpath = ["src"]` 让 pytest 能从 src 加载；禁止在仓库根直接放 `hostlens/` 目录。

#### 场景:import 从 src 解析

- **当** 在测试中执行 `from hostlens.core.exceptions import HostlensError`
- **那么** import 必须从 `src/hostlens/core/exceptions.py` 加载

#### 场景:目录结构完整

- **当** 检查 `src/hostlens/` 目录
- **那么** 必须存在以下子目录，每个含 `__init__.py`：`agent`, `inspectors`, `targets`, `scheduler`, `notifiers`, `remediation`, `reporting`, `mcp_server`, `cli`, `core`
