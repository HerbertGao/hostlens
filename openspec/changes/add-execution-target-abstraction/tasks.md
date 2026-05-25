## 1. 依赖与脚手架

- [ ] 1.1 `pyproject.toml` 增加 runtime 依赖 `asyncssh ^2.18` 与 dev 依赖 `pytest-docker ^3.1` / `pytest-rerunfailures ^14.0`；验收：`pip install -e ".[dev]"` 成功 + `python -c "import asyncssh"` 不报错
- [ ] 1.2 创建 `src/hostlens/targets/{__init__.py, base.py, local.py, ssh.py, registry.py, config.py}` 空骨架文件；验收：`python -c "import hostlens.targets"` 成功
- [ ] 1.3 创建 `src/hostlens/cli/target.py` Typer 子命令组空骨架；在 `cli/__init__.py` 注册到 app；验收：`hostlens target --help` 列出子命令名（add/list/remove/test）但每个子命令执行 NotImplementedError

## 2. 基础类型与 Protocol

- [ ] 2.1 实现 `hostlens.targets.base.Capability` Enum（M1 最小集 `SHELL/FILE_READ/SSH/SYSTEMD/DOCKER_CLI`）；验收：`tests/targets/test_capability.py` 覆盖 spec §需求:`Capability` Enum 必须含 M1 最小集与扩展守则 所有 3 个场景
- [ ] 2.2 实现 `hostlens.targets.base.ExecResult` Pydantic 模型（含 `timed_out` / `exit_code` 分离字段，frozen=True extra=forbid）；验收：`tests/targets/test_exec_result.py` 覆盖 spec 的 4 个场景（超时 / 非零 / 非 UTF-8 容错 / frozen）
- [ ] 2.3 实现 `hostlens.targets.base.ExecutionTarget` Protocol（含 `name/type/capabilities` 属性与 `exec/read_file` async 方法）；验收：`tests/targets/test_protocol.py` 含 mypy 静态校验 + 4 个场景（Protocol 形状 / async / type 字段值域 / read_file 10MB 上限）
- [ ] 2.4 实现 `hostlens.core.exceptions.TargetError` 子类（继承自 M0 已落地的 `HostlensError`），含 `kind: str` 与 keyword-only `__init__`；更新 `hostlens.core.exceptions.__all__` 公共导出（M0 测试需同步 expected count）；验收：`tests/core/test_exceptions.py` 公共导出断言更新

## 3. LocalTarget 实现

- [ ] 3.1 实现 `hostlens.targets.local.LocalTarget` 基本 exec（`asyncio.create_subprocess_shell` + env 合并 os.environ）；验收：单测覆盖正常 exec / pipe 解析 / env 合并 / 非零 exit
- [ ] 3.2 实现 LocalTarget 超时（`asyncio.wait_for` + SIGKILL）；验收：单测验证超时返回 `timed_out=True` 且无 zombie 进程（用 `psutil` 检查）
- [ ] 3.3 实现 LocalTarget 运行时 capability 探测（探测 `docker --version` / `systemctl --version` 并缓存）；验收：单测覆盖有 docker / 无 docker 两种环境 mock
- [ ] 3.4 实现 LocalTarget `read_file`（同步 `aiofiles.open` + 10MB 上限）；验收：单测覆盖正常读 / 文件不存在 raise FileNotFoundError / 超过 10MB raise TargetError
- [ ] 3.5 **非 root 用户跑通验收**：在非 root shell 跑 `python -c "import asyncio; from hostlens.targets.local import LocalTarget; r = asyncio.run(LocalTarget('t').exec('whoami', timeout=5)); print(r.stdout)"` 必须输出当前用户名 + exit_code=0

## 4. TargetRegistry 与配置加载

- [ ] 4.1 实现 `hostlens.targets.registry.TargetRegistry`（register / get / names / list 接口；name 冲突 raise）；验收：单测覆盖 spec 的 3 个场景
- [ ] 4.2 实现 `hostlens.targets.config.TargetsConfig` Pydantic 模型 + `TargetEntry`（LocalEntry / SSHEntry 通过 `type` discriminator）；验收：单测覆盖 schema 验证 / unknown type raise
- [ ] 4.3 实现 `hostlens.targets.config.load_targets_config(path)` —— 含 `${ENV_VAR}` 展开 / 占位仅 secret 字段 / 文件不存在返回空 registry；验收：单测覆盖 spec §需求:`TargetsConfig` 必须从 yaml 加载且环境变量占位展开 全部 5 个场景
- [ ] 4.4 实现 yaml → TargetRegistry 装配工厂 `build_registry_from_config(config)`（按 type 实例化 LocalTarget / SSHTarget）；验收：集成测试用 fixture yaml 装配 2 个 target 并验证 names()
- [ ] 4.5 **密钥脱敏测试**：构造含 `password: literal-test-secret-do-not-leak` 的 TargetEntry，跑一次 load → registry build → repr/str；验收：log 输出与 `repr(entry)` 都**不**含 `literal-test-secret-do-not-leak` 子串

## 5. SSHTarget 实现

- [ ] 5.1 实现 `hostlens.targets.ssh.SSHTarget` 基本 exec（每次 `asyncssh.connect` + `conn.run`）；验收：先用 mock 跑单测覆盖 exec 接口契约（不验证真实 SSH 行为，留给 5.6 集成测试）
- [ ] 5.2 实现 SSH 连接超时（`asyncssh.connect(connect_timeout=...)`）+ raise `TargetError("ssh_connect_timeout")`；验收：单测覆盖错误 kind 与脱敏（不含凭据）
- [ ] 5.3 实现 SSH exec 超时（`asyncio.wait_for` 包裹 `conn.run`）；验收：单测覆盖 timed_out=True + channel close
- [ ] 5.4 实现 SSH `read_file`（SFTP 优先 + cat 兜底 + 10MB 上限）；验收：5.6 集成测试一并覆盖
- [ ] 5.5 实现 SSH env 注入约束（透传 asyncssh `env=`，**禁止** export 拼接）；验收：单测断言传给 asyncssh 的 cmd string 严格等于原 cmd（用 mock asyncssh.connect 验证调用参数）
- [ ] 5.6 **集成测试用真实 sshd**：`tests/targets/test_ssh_integration.py` 用 `pytest-docker` 起 `linuxserver/openssh-server` 容器；session-scoped fixture 复用；测试覆盖：exec echo / 非零 exit / 超时 / 连接失败 / SFTP read_file / read_file 10MB raise / env 透传；验收：**禁止** mock `asyncssh.connect` / `conn.run`（grep 检查）；CI 上 `pytest-rerunfailures` 允许 retry 1 次
- [ ] 5.7 **凭据脱敏端到端**：集成测试用错误 password 连 sshd，断言抛出的 TargetError + structlog log 都**不**含原始 password 子串
- [ ] 5.8 **非 root 用户跑通验收**：本地起 sshd 容器 + 非 root shell 跑 `hostlens target test my-ssh` 必须返回 connectivity=ok + capabilities 含 SSH+SHELL+FILE_READ

## 6. CLI 子命令实现

- [ ] 6.1 实现 `hostlens target add` —— 参数解析 / TargetsConfig 加载 / 写回 yaml / name 冲突 exit 2；验收：单测覆盖 add LocalTarget + add SSHTarget + 冲突 exit 码
- [ ] 6.2 实现 `hostlens target list` —— 表格输出（Rich Table）与 `--json` 输出（含 capabilities）；验收：**`--json` 输出 schema 稳定性**测试（snapshot 测试 + JSON Schema 校验）
- [ ] 6.3 实现 `hostlens target remove` —— 交互确认 + `--yes` 跳过 + **非交互无 `--yes` 退出 1**；验收：单测用 `runner.invoke(...)` 模拟无 TTY 环境断言 exit 1
- [ ] 6.4 实现 `hostlens target test` —— 跑 `echo hostlens-probe-$$` + capability 探测 + 失败 exit 1；验收：单测含 LocalTarget 成功路径 + SSHTarget 不可达失败路径
- [ ] 6.5 **CLI 错误输出语义**：所有 target CLI 命令的错误信息走 stderr，数据走 stdout；验收：单测断言 `result.stderr_bytes` 与 `result.stdout_bytes` 分开

## 7. doctor 集成

- [ ] 7.1 扩展 `hostlens.cli.doctor` 增加 `_check_targets()` 函数 —— 对每个 target 跑连通性 + 凭据来源识别 + capability 探测；验收：单测覆盖输出结构
- [ ] 7.2 doctor `--json` 输出新增 `targets` key；验收：snapshot 测试 + JSON Schema 校验
- [ ] 7.3 明文密码 warning（不阻塞）；某 target 连通失败 → doctor 整体 exit 1；验收：单测覆盖两种条件分支
- [ ] 7.4 **保持 M0 doctor 兼容性**：M0 doctor 已有的 `python_version` / `anthropic_api_key` / `config_dir` 检查必须保留；snapshot 测试包含原有 section

## 8. Tool Registry 集成（消除 stub）

- [ ] 8.1 修改 `src/hostlens/tools/base.py`：import 切换 `from hostlens.targets.registry import TargetRegistry`；删除原 stub Protocol 定义；验收：mypy --strict 0 错误；`grep -r "stub" src/hostlens/tools/` 在 target_registry 相关代码上无残留
- [ ] 8.2 修改 `src/hostlens/tools/default_tools.py`：`list_targets` handler 从 `ctx.target_registry.list()` 取真实数据；按 tool-registry-capability-layer spec §`TargetSummary` 输出 schema 必须脱敏 的 scrub 规则处理字段值；验收：M2 现有 `tests/tools/test_list_targets.py` 测试用例 fixture 替换为真实 TargetRegistry + LocalTarget；snapshot 输出含真实数据
- [ ] 8.3 修改 `tests/tools/` 下所有使用 stub TargetRegistry 的 fixture，统一改用 `build_registry_from_config(fixture_config)` 装配真实 LocalTarget；**禁止**保留 stub fallback（M1 落地后 stub 是死代码）
- [ ] 8.4 验收 §需求:`ToolContext` 必须包含 M2 字段最小集 §场景:target_registry 是真实 TargetRegistry 类型 —— `assert ToolContext.__annotations__["target_registry"] is TargetRegistry`

## 9. 文档与示例

- [ ] 9.1 `docs/operations/targets.md`：targets.yaml 配置示例 + SSH 凭据 best practice + 远端 sshd `AcceptEnv` 限制说明 + 凭据脱敏说明
- [ ] 9.2 更新 `docs/ARCHITECTURE.md` §5：把 M1 LocalTarget / SSHTarget 状态从"待办"改为"M1 落地（PR #<本提案 PR 号>）"
- [ ] 9.3 `examples/m1-targets/README.md`：5 分钟 demo 路径（docker sshd + hostlens target add/test/list + doctor + Tool Registry dispatch 端到端）
- [ ] 9.4 README "快速开始"小节增加 `hostlens target add` + `hostlens target test` 一行示例

## 10. 验证与 demo path

- [ ] 10.1 跑 `mypy --strict src/hostlens/targets/ src/hostlens/cli/target.py src/hostlens/tools/base.py src/hostlens/tools/default_tools.py` 必须 0 错误
- [ ] 10.2 跑 `ruff check src/ tests/` 必须 0 错误
- [ ] 10.3 跑 `pytest tests/targets/ tests/cli/test_target.py tests/cli/test_doctor.py tests/tools/test_list_targets.py tests/core/test_exceptions.py -v` 必须全绿
- [ ] 10.4 跑 proposal Demo Path 全 8 步（从干净 venv 安装到 Tool Registry dispatch 验证 list_targets 真数据）；记录每步输出截图到 `examples/m1-targets/`
- [ ] 10.5 **并发预算**：手工验证调用方在 `concurrency.max_concurrent_targets=8` 限制下并行 dispatch 12 个 target 时，semaphore 排队而非崩溃（实现在 Inspector Runner 提案，此处只验证 TargetRegistry 接口对并发无副作用 —— `registry.get(name)` 与 `target.exec()` 可被多 task 并行调用）

## 11. Git 工作流与归档准备

- [ ] 11.1 完成所有上述任务后 commit 到 feature branch `feat/add-execution-target-abstraction`；commit message 含 OpenSpec change name 引用
- [ ] 11.2 push branch + 开 PR 到 main；PR 描述含 spec 引用（`openspec/changes/add-execution-target-abstraction/`）与 Demo Path
- [ ] 11.3 等 CI 全绿 + review 通过后 squash merge：`\gh pr merge <num> --squash --delete-branch`
- [ ] 11.4 准备归档：跑 `openspec-cn validate add-execution-target-abstraction` 确认变更可归档；后续运行 `/opsx:archive` 推进到 `openspec/specs/{execution-target, ssh-execution-target}/spec.md` 并同步 `openspec/specs/tool-registry-capability-layer/spec.md` 的 MODIFIED 需求
