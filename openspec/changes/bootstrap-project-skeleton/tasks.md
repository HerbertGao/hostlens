## 1. 打包基础

- [x] 1.1 创建 `src/hostlens/` 完整目录结构 + 每个子目录的 `__init__.py`（agent / inspectors / targets / scheduler / notifiers / remediation / reporting / mcp_server / cli / core），与 CLAUDE.md §3 完全一致；同时创建顶层 `inspectors/` / `schedules/` / `tests/` / `docs/` 目录
- [x] 1.2 写 `pyproject.toml`：`[build-system] hatchling` + `[project]` 元数据（name=hostlens, version=0.1.0, python>=3.11, license=Apache-2.0）+ `[project.scripts] hostlens = "hostlens.cli:app"`；**PEP 621 标准**：`[project]` 表中用 `dependencies = [...]` 数组列 runtime 必须依赖（**不是** `[project.dependencies]` 子表，那不是合法 TOML 表名 —— 会让 `pip install -e .` 失败 / 元数据无效），含 `typer>=0.12` / `rich>=13.7` / `pydantic>=2.6` / `pydantic-settings>=2.2` / `structlog>=24.1`（M0 `hostlens doctor` 运行时真用到的库；裸 `pip install -e .` 必须能跑通 `hostlens --help`）；**`[project.optional-dependencies]` 表分 `dev` / `mcp` / `docs` 三组**：`dev` 含 `pytest>=8` / `pytest-asyncio>=0.23` / `pytest-cov>=5` / `ruff>=0.6` / `mypy>=1.10` / `pre-commit>=3.7`；`mcp` 与 `docs` M0 留空（仅声明 key 占位，M7 / M10 填）
- [x] 1.3 验证并补全 `.gitignore`：必须含 (a) Python: `__pycache__/` / `.venv/` / `*.egg-info/` (b) 工具缓存: `.mypy_cache/` / `.ruff_cache/` / `.pytest_cache/` / `.coverage` (c) IDE: `.vscode/` / `.idea/` (d) macOS: `.DS_Store` (e) **secrets 防御**: `.env` / `.env.*` / `.env.local` / `*.pem` / `*.key` / `credentials.json` 并白名单 `!.env.example` (f) Hostlens runtime（M1+ 可能写入仓内的）: `hostlens.log` / `*.db*` / `.hostlens/`。注意 `~/.config/hostlens/` 不放仓内不需要写进 .gitignore（home-directory 路径在 .gitignore 中无效）
- [x] 1.4 添加 `LICENSE` 文件（Apache-2.0 全文）
- [x] 1.5 添加 `.editorconfig`（统一缩进 4 spaces / LF / utf-8 / `*.md` 与 `*.yml` 用 2 spaces / 末尾换行；防止 IDE 风格漂移影响 ruff format）
- [x] 1.6 **验收**：在干净的 Python 3.11 venv 执行 `pip install -e ".[dev]"`，必须 exit 0；执行 `python -c "import hostlens; print(hostlens.__file__)"` 必须输出 `src/hostlens/__init__.py` 路径；执行 `which hostlens` 必须返回 venv 内 `bin/hostlens`；执行 `test -f .editorconfig` exit 0；执行 `grep -E "^(indent_style|indent_size|end_of_line|charset|insert_final_newline)" .editorconfig | wc -l` 必须返回 ≥5（五项 EditorConfig 核心键全部存在）

## 2. 代码质量工具链

- [x] 2.1 在 `pyproject.toml` 写 `[tool.ruff]` 配置：line-length=100；select 启用 E/F/W/I/N/UP/B/SIM/RUF；ignore 与 CLAUDE.md §6 一致（如禁用 D100 "Missing docstring"，因 §6 约定"不写无意义注释"）；exclude `tests/cassettes/`
- [x] 2.2 在 `pyproject.toml` 写 `[tool.mypy]` 配置：`strict = true` + `plugins = ["pydantic.mypy"]` + `python_version = "3.11"` + `disallow_any_generics = true` + `exclude = ['tests/']`（M0 阶段 tests 不 strict 的 tradeoff 详见 design.md D-5；未来 test helpers >500 行时单独提案给 tests 加 lighter typing）
- [x] 2.3 写 `.pre-commit-config.yaml`：包含 `ruff` (lint + format) / `mypy` / 标准 hooks (trailing-whitespace / end-of-file-fixer / check-yaml / check-toml / check-merge-conflict)；**`rev:` 用 version tag**（如 `v0.6.0`、`v1.8.0`），不要用 commit hash（影响可读性 + 不利于 Dependabot 自动更新）；由 8.4 引入的 Dependabot 配置自动跟踪 pre-commit 仓库升级
- [x] 2.4 **验收**：`pre-commit install && pre-commit run --all-files` exit 0；`ruff check .` exit 0；`ruff format --check .` exit 0；`mypy --strict src/` exit 0 输出 "Success: no issues found"

## 3. 测试基线

- [x] 3.1 在 `pyproject.toml` 写 `[tool.pytest.ini_options]`：`asyncio_mode = "auto"` + `pythonpath = ["src"]` + `addopts = "-ra --strict-markers --strict-config"` + `markers = ["slow: long-running tests"]`
- [x] 3.2 写 `tests/conftest.py` 骨架（暂留空但为后续 M1+ fixtures 预留位）
- [x] 3.3 写 `tests/test_smoke.py`：1 个 sync 测试（`assert hostlens` 可 import）+ 1 个 async 测试（验证 `asyncio_mode = "auto"` 生效，写 `async def test_async_smoke()` 直接跑通）
- [x] 3.4 **验收**：`pytest -v` exit 0 且至少 2 个测试 PASS；`pytest --cov=hostlens --cov-report=term` exit 0 且输出 TOTAL 行

## 4. 核心服务：异常基类

- [x] 4.1 写 `src/hostlens/core/exceptions.py`：定义 `HostlensError(Exception)` 基类 + `ConfigError` / `TargetError` / `InspectorError` 三个子类；每个类含 docstring 说明用途；**不**预先添加未在 spec 列出的其他异常
- [x] 4.2 写 `tests/core/test_exceptions.py`：覆盖 (a) `isinstance(ConfigError("x"), HostlensError) is True` (b) 通用 `except HostlensError as e:` 能捕获三个子类 (c) `dir(hostlens.core.exceptions)` 恰好导出 4 个异常类（不多不少）
- [x] 4.3 **验收**：`mypy --strict src/hostlens/core/exceptions.py` exit 0；`pytest tests/core/test_exceptions.py -v` exit 0

## 5. 核心服务：配置加载

- [x] 5.1 写 `src/hostlens/core/config.py`：`Settings(BaseSettings)` 定义 `log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"` + `log_mode: Literal["dev", "prod"] = "prod"` + `config_dir: Path = Path("~/.config/hostlens").expanduser()`；`model_config = SettingsConfigDict(env_prefix="HOSTLENS_", env_file=".env", extra="ignore")`；定义 `_SENSITIVE_FIELD_PATTERN = re.compile(r"(?i)(key|token|secret|password|credential)")` 模块常量；写 `load_settings() -> Settings` 工厂函数：捕获 `pydantic.ValidationError`，遍历每个 error，若 `error['loc']` 字段名匹配 `_SENSITIVE_FIELD_PATTERN` 则把 `error['input']` 替换为 `"***"`，再 raise `ConfigError(formatted_message, original=ve)`；普通字段保留实际值（便于调试）
- [x] 5.2 写 `tests/core/test_config.py` 基础部分：覆盖 (a) 默认值生效 (b) `HOSTLENS_LOG_LEVEL=INFO` 通过 env 加载 (c) 非法 `HOSTLENS_LOG_LEVEL=NotALevel` raise `ConfigError`，消息**必须**含字段名 `log_level` + 期望的有效值集合 + **实际值** `"NotALevel"`（非 sensitive 字段保留实际值便于调试）
- [x] 5.3 写 `tests/core/test_config_redaction.py` **敏感字段脱敏专项**：(a) 定义临时 `class TestSettings(Settings): anthropic_api_key: SecretStr` （或直接用 `_SENSITIVE_FIELD_PATTERN` 单测）；(b) 模拟敏感字段传入非法值 `"sk-ant-realleak123"`；(c) 断言 raise `ConfigError`，消息**必须**含字段名 `anthropic_api_key`，**必须**含 `"***"`，**禁止**含 `"sk-ant-realleak123"` 或其任何子串 ≥4 chars；(d) 测试模式匹配：`api_key` / `auth_token` / `client_secret` / `db_password` / `aws_credential` 五个不同字段名都正确触发脱敏
- [x] 5.4 **验收**：`mypy --strict src/hostlens/core/config.py` exit 0；`pytest tests/core/test_config.py tests/core/test_config_redaction.py -v` exit 0

## 6. 核心服务：日志与脱敏

- [x] 6.1 写 `src/hostlens/core/logging.py`：`configure_logging(mode: Literal["dev", "prod"]) -> None`；dev 模式用 `structlog.dev.ConsoleRenderer` + 颜色（在 TTY 下）；prod 模式用 `structlog.processors.JSONRenderer`；processor 链顶部加自定义 `redact_sensitive` processor，**必须用 `isinstance(value, collections.abc.Mapping)` 递归遍历**（涵盖 dict / `os._Environ` 等所有 mapping 类型 / list / tuple / set，最大递归深度 8）：匹配 `(?i)(key|token|secret|password|credential)` 的字段名（含任意层级嵌套 mapping key）把值替换为 `"***"`；**禁止修改原始数据**（脱敏只发生在日志渲染阶段；用 `copy.deepcopy` 或纯函数式重建）
- [x] 6.2 写 `tests/core/test_logging.py`：(a) prod 模式输出可 `json.loads` 解析 (b) dev 模式输出在 TTY mock 下含 ANSI 颜色码 (c) **顶层脱敏**：`logger.info("test", anthropic_api_key="sk-ant-realkey")` 输出**不含** `sk-ant-realkey`；正常字段（`username="alice"`）保留原值 (d) **嵌套 dict 脱敏**：`logger.info("env", env={"ANTHROPIC_API_KEY": "sk-x", "HOME": "/u/a"})` 输出 `env.ANTHROPIC_API_KEY=***` 且 `env.HOME=/u/a` (e) **嵌套 list 脱敏**：`logger.info("targets", targets=[{"name": "p", "ssh_key": "BEGIN..."}])` 输出 `targets[0].ssh_key=***` (f) **不修改原数据**：caller 的 dict 在 log 调用后保持原值 (g) **递归深度限制**：构造 9 层嵌套 dict 不应崩溃（超过 8 层时第 9+ 层不保证脱敏，但不能 raise RecursionError） (h) **`os.environ` 整体传入也脱敏**：`monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leakkey"); logger.info("dump", env=os.environ)` 输出**不含** `sk-ant-leakkey`（断言 `os._Environ` 这种非 dict mapping 也被正确识别 + 递归）
- [x] 6.3 **验收**：`mypy --strict src/hostlens/core/logging.py` exit 0；`pytest tests/core/test_logging.py -v` exit 0；脱敏测试明确 assert "sk-ant" not in captured_log_output

## 7. CLI 骨架与 doctor

- [x] 7.1 写 `src/hostlens/cli/_doctor_schema.py`：Pydantic 模型 `CheckResult(status: Literal["ok", "present", "missing", "unreadable", "error"], detail: str | None = None, path: str | None = None)` + `DoctorReport(version: str = "0.1.0", timestamp: datetime, checks: dict[str, CheckResult], ready: bool)`；`model_config = ConfigDict(extra="forbid")` 保证 schema 严格
- [x] 7.2 写 `src/hostlens/cli/doctor.py`：实现 3 个 checker（`check_python_version()` / `check_anthropic_key()` / `check_config_dir()`）；`run_doctor(json_output: bool) -> int` 入口；human 模式用 Rich `Table` 输出；json 模式用 `report.model_dump_json(indent=2)`；返回 exit code（任一 critical check fail → 1，否则 0）
- [x] 7.3 写 `src/hostlens/cli/__init__.py`：`app = typer.Typer(no_args_is_help=True)` + `@app.command("doctor")` 注册 doctor + 暴露 `app` 给 entrypoint
- [x] 7.4 **关键安全实现**：`check_anthropic_key()` 在 `ANTHROPIC_API_KEY` 存在时返回 `CheckResult(status="present", detail=None)`；缺失时返回 `CheckResult(status="missing", detail=None)`；`detail` 字段**必须**为 `None`（**禁止任何形式的脱敏前缀 / 后缀 / 掩码值**——存在性检查不需要密钥任何形式的值）；写代码级 review checklist 防回归
- [x] 7.5 写 `tests/cli/test_doctor_schema.py`：(a) `hostlens doctor --json | jq -e .ready` exit 0 (b) JSON 输出含 `version / timestamp / checks / ready` 四 required 字段（与 specs cli-foundation §需求:`hostlens doctor --json` 输出稳定 schema 对应） (c) `checks` 必须含 `python_version / anthropic_key / config_dir` 三 key (d) 每个 check 的 `status` 在 enum `["ok", "present", "missing", "unreadable", "error"]` 内
- [x] 7.6 写 `tests/cli/test_doctor_redaction.py`：**密钥脱敏专项测试**：(a) mock `os.environ["ANTHROPIC_API_KEY"]="sk-ant-secretkey"`，doctor JSON 输出整体 string 中**不含** `secretkey` 子串 (b) `checks.anthropic_key.detail` 必须严格为 `null`（不允许任何前缀 / 掩码） (c) 即使 `HOSTLENS_LOG_LEVEL=DEBUG` 下密钥也不出现在 stderr 日志
- [x] 7.7 写 `tests/cli/test_doctor_tty.py`：**非 TTY 行为测试**：(a) `hostlens doctor | cat`（管道剥离 TTY）输出不含 ANSI 颜色码 (b) `hostlens --help` Typer 自动生成的输出可被解析 (c) 未知子命令 `hostlens nonexistent` exit 非 0
- [x] 7.8 写 `tests/cli/test_doctor_permissions.py`：**config_dir 权限测试**：(a) `~/.config/hostlens/` 不可读时（mock 文件系统或 tmp_path + chmod）exit 1 (b) stderr 含修复指引（如 `chmod 755`） (c) 目录不存在时 status=`missing`
- [x] 7.9 **验收**：`hostlens --help` exit 0 含 `doctor`；`hostlens doctor` 默认输出人类可读；`hostlens doctor --json | jq -e .` exit 0；`pytest tests/cli/ -v` exit 0（含 7.5–7.8 所有测试文件）

## 8. GitHub Actions CI

- [x] 8.1 写 `.github/workflows/ci.yml`：触发器 `on: [push, pull_request]`（仅 main 分支 push）；jobs.ci 用 `strategy.matrix.python-version: ["3.11", "3.12"]` + `runs-on: ubuntu-latest`；steps：(a) checkout (b) setup-python (c) `pip install -e ".[dev]"` (d) `ruff check .` (e) `ruff format --check .` (f) `mypy --strict src/` (g) `pytest --cov=hostlens --cov-report=term`；任一 step 失败必须导致整个 job fail；**禁止** workflow 步骤需要任何 secrets（M0 doctor 检查不依赖真实 ANTHROPIC_API_KEY，CI 必须能在零密钥环境跑通）
- [x] 8.2 **本地验证 workflow 语法**：commit 前**优先用 `actionlint`** 校验 `.github/workflows/ci.yml`（捕获 GitHub Actions 语义错误，如未知 action 版本 / 无效 expression / step 顺序错误）；`actionlint` 不可用时可降级 `act --list`；**`yamllint` 不算等价方案**（只校验 yaml 格式无法捕获 Actions 语义），仅作为最后的 best-effort 标记并在 task 注释明示限制。安装 actionlint：`brew install actionlint` (macOS) 或 `go install github.com/rhysd/actionlint/cmd/actionlint@latest`
- [x] 8.3 **本地命令镜像验证**：所有 workflow steps 必须能在本地原样执行（验证开发-CI 行为一致），即 `pip install -e ".[dev]" && ruff check . && ruff format --check . && mypy --strict src/ && pytest --cov=hostlens --cov-report=term` 五条命令在本地 venv 内必须全 exit 0；workflow yaml 与本地命令文本必须完全一致（不允许 CI 偷偷加额外参数）
- [x] 8.4 写 `.github/dependabot.yml`：监控 `pip`（pyproject.toml）/ `github-actions`（.github/workflows/）/ `pre-commit` 三种 ecosystem；schedule weekly；commit message prefix `chore(deps):`
- [ ] 8.5 **远端验收**（最终而非阻塞）：commit + push 后在 GitHub Actions UI 验证 workflow 自动触发；matrix 两个 Python 版本都必须绿；`gh run list --workflow=ci.yml --limit 1 --json status,conclusion` 输出 status=completed + conclusion=success

## 9. 最终集成验收（M0 退出条件）

- [x] 9.1 在干净的 venv（删除现有 `.venv/` 后重建）跑完整 demo path 五步：`pip install -e ".[dev]"` / `hostlens --help` / `hostlens doctor --json | jq -e '.ready != null'` / `pytest -v` / `pre-commit run --all-files`；全部 exit 0
- [x] 9.2 在 README.md 顶部 badges 区添加 CI status badge：`[![CI](https://github.com/HerbertGao/hostlens/actions/workflows/ci.yml/badge.svg)](https://github.com/HerbertGao/hostlens/actions/workflows/ci.yml)`
- [ ] 9.3 commit 全部 M0 改动 + push 到 GitHub；等待 CI 绿
- [x] 9.4 **schema 稳定性验证（与 design.md D-9 "schema 演进政策" 一致）**：snapshot 测试**只锁 required 字段**（顶层 `version` / `timestamp` / `checks` / `ready`，每个 check 的 `status`），**允许** optional 字段（`detail` / `path` / 新增 check）的 add-only 演进而不需要每次更新 snapshot；required 字段任何变更必须 bump `version` 字段并显式更新 spec 才能通过测试
- [x] 9.5 **README 加 coverage policy 透明声明**：在 quickstart 或 testing 节加一句"M0 阶段 coverage 报告但不设强制门槛；M2 引入 Agent loop 后设 80% 门槛"，避免 reviewer 误以为"忘了配置 coverage"
- [x] 9.6 准备 archive：跑 `openspec-cn validate --change bootstrap-project-skeleton`（如果该子命令存在）确认变更可归档；标记本变更可推进到 `/opsx:archive`
