## 1. 依赖与脚手架

- [ ] 1.1 `pyproject.toml` 增加 runtime 依赖 `asyncssh>=2.18,<3`、dev 依赖 `pytest-docker>=3.1,<4` 与 `pytest-rerunfailures>=14.0,<16`（**PEP 508 语法**，与现有 `>=` 风格一致；**禁止** Poetry caret `^`）；验收：`pip install -e ".[dev]"` 成功 + `python -c "import asyncssh"` 不报错
- [ ] 1.2 创建 `src/hostlens/targets/{__init__.py, base.py, local.py, ssh.py, registry.py, config.py}` 空骨架文件；验收：`python -c "import hostlens.targets"` 成功
- [ ] 1.3 创建 `src/hostlens/cli/target.py` Typer 子命令组空骨架；在 `cli/__init__.py` 注册到 app；验收：`hostlens target --help` 列出子命令名（add/list/remove/test）但每个子命令执行 NotImplementedError

## 2. 基础类型与 Protocol

- [ ] 2.1 实现 `hostlens.targets.base.Capability` Enum，**恰好** M1 最小集 5 个成员 `{SHELL, FILE_READ, SSH, SYSTEMD, DOCKER_CLI}`（**禁止**预留 M8/M9 placeholder）；验收：`tests/targets/test_capability.py` 覆盖 spec §需求:`Capability` Enum... 全部 3 个场景（恰好集合 / 值小写 / 与 CAPABILITY_ALLOWLIST 严格相等）
- [ ] 2.2 实现 `hostlens.targets.base.ExecResult` Pydantic 模型（`exit_code: int | None`，**禁止**用 `-1` 魔数表达超时；`timed_out is True` 时模型层 validator 强制 `exit_code is None`；frozen=True extra=forbid）；验收：`tests/targets/test_exec_result.py` 覆盖 6 个场景（超时 None / 非零 exit / signal-killed 128+signum / 模型层 validator / UTF-8 容错 / frozen）
- [ ] 2.3 实现 `hostlens.targets.base.ExecutionTarget` Protocol（含 `name/type/capabilities` 属性与 `exec/read_file` async 方法）；验收：`tests/targets/test_protocol.py` 含 mypy 静态校验 + 4 个场景（Protocol 形状 / async / type 字段值域 / read_file 10MB 上限）
- [ ] 2.4 实现 `hostlens.core.exceptions.TargetError` 子类（继承自 M0 已落地的 `HostlensError`），含 `kind: str` 与 keyword-only `__init__`；更新 `hostlens.core.exceptions.__all__` 公共导出（M0 测试需同步 expected count）；验收：`tests/core/test_exceptions.py` 公共导出断言更新

## 3. LocalTarget 实现

- [ ] 3.1 实现 `hostlens.targets.local.LocalTarget` 基本 exec（`asyncio.create_subprocess_shell(cmd, env=..., start_new_session=True)` + env 合并 os.environ）；**module 顶层加 `if sys.platform == "win32": raise ImportError("LocalTarget requires POSIX host (Linux/macOS); Windows support is not in M1 scope")`**；guard 必须在所有 POSIX-only 符号（`os.killpg` / `os.getpgid`）的 import / 使用之前；验收：(a) 单测覆盖正常 exec / pipe 解析 / env 合并 / 非零 exit / signal-killed 返回 128+signum；(b) **Windows guard 分支覆盖测试**（用 `monkeypatch.setattr(sys, "platform", "win32")` + `importlib.reload` 断言 raise ImportError）—— 该测试**仅覆盖 guard 分支**，**不能**证明真实 Windows 行为正确（POSIX 宿主上 `os.killpg` 仍存在）；真实 Windows 支持属 design 非目标 #8，超出本提案；CI **不**要求 Windows runner
- [ ] 3.2 实现 LocalTarget 超时 + 进程组回收（`asyncio.wait_for` + `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` + `await proc.wait()`）；验收：单测调用 `await local.exec("sleep 60", timeout=1)`，用 `psutil.Process(...).children(recursive=True)` 断言**无** `sleep` 残留；返回 `ExecResult(timed_out=True, exit_code=None)`
- [ ] 3.3 实现 LocalTarget 运行时 capability 探测（探测 `docker --version` / `systemctl --version` 并缓存）；验收：单测覆盖有 docker / 无 docker 两种环境 mock
- [ ] 3.4 实现 LocalTarget `read_file`（`aiofiles.open` + 10MB 上限 + 路径合法性校验拒绝 NUL 字节）；验收：单测覆盖正常读 / 文件不存在 raise FileNotFoundError / 超过 10MB raise TargetError / NUL 字节 raise TargetError
- [ ] 3.5 **非 root 用户跑通验收**：在非 root shell 跑 `python -c "import asyncio; from hostlens.targets.local import LocalTarget; r = asyncio.run(LocalTarget('t').exec('whoami', timeout=5)); print(r.stdout)"` 必须输出当前用户名 + exit_code=0

## 4. TargetRegistry 与配置加载

- [ ] 4.1 实现 `hostlens.targets.registry.TargetRegistry`（API：`register(target, entry)` / `get(name)` / `get_entry(name)` / `names()` / `list()` / `list_entries()`；name 冲突 raise）；同时持有 `ExecutionTarget` 实例与 `TargetEntry` 元数据双索引；验收：单测覆盖 spec 全部 4 个场景（含 get_entry / list_entries）
- [ ] 4.2 实现 `hostlens.targets.config.TargetsConfig` Pydantic 模型 + `TargetEntry`（LocalEntry / SSHEntry 通过 `type` discriminator；SSH 字段集**恰好** `{host, user, port, key_path, password, passphrase}` 不多不少；extra=forbid）；验收：单测覆盖 schema 验证 / unknown type raise / `agent_forwarding` raise / 字段集恰好
- [ ] 4.3 实现 `hostlens.targets.config.load_targets_config(path)` —— 含 `${ENV_VAR}` 展开 / 占位仅 `password`/`passphrase` 字段允许 / 文件不存在返回空 registry + INFO log；验收：单测覆盖 spec §需求:`TargetsConfig` 必须从 yaml 加载且环境变量占位展开 全部场景
- [ ] 4.4 实现 yaml → TargetRegistry 装配工厂 `build_registry_from_config(config)`（按 type 实例化 LocalTarget / SSHTarget）；验收：集成测试用 fixture yaml 装配 2 个 target 并验证 names()
- [ ] 4.5 **密钥脱敏测试**：构造含 `password: literal-test-secret-do-not-leak` 的 TargetEntry，跑一次 load → registry build → repr/str；验收：log 输出与 `repr(entry)` 都**不**含 `literal-test-secret-do-not-leak` 子串

## 5. SSHTarget 实现（含 per-target connection pool）

- [ ] 5.1 实现 `hostlens.targets.ssh.SSHTarget` 基础结构（asyncio.Lock 保护的 control connection state machine：`_conn` / `_last_used_at` / `_lock`）；首次 exec 时按需建立 connection；验收：mock asyncssh.connect 单测验证"连续 3 次 exec 只触发 1 次 connect"
- [ ] 5.2 实现 SSH idle timeout（`ssh.idle_timeout_seconds=300` 默认；后台 task 或 lazy check：每次 exec 前判断 `time.monotonic() - _last_used_at > idle_timeout` → close + reconnect）；验收：mock 时间快进，单测验证 idle 后 connect 再次被调用
- [ ] 5.3 实现 SSH 断线重连（捕获 `ConnectionLost` / `ChannelOpenError` → 指数退避 1s/3s/9s 最多 3 次；超出 raise `TargetError("ssh_connection_lost")`）；验收：单测 mock asyncssh.connect 第 1 次抛 ConnectionLost、第 2 次成功，断言 exec 最终成功 + 用了 2 次 connect 调用
- [ ] 5.4 实现 SSHTarget exec（`conn.run(cmd, env=env)` 走 channel，**禁止**每次 exec 重新 connect；超时通过 `asyncio.wait_for` 包装；超时仅 close channel 不影响 control connection）；验收：单测覆盖 exec 接口契约（mock asyncssh）；并行 gather 3 个 exec 验证 share connection
- [ ] 5.5 实现 SSH 连接超时（`asyncssh.connect(connect_timeout=...)`）+ raise `TargetError("ssh_connect_timeout")`；asyncssh 配置 `agent_forwarding=False, x11_forwarding=False, keepalive_interval=60`；验收：单测覆盖错误 kind 与脱敏（不含凭据）
- [ ] 5.6 实现 SSH `read_file` —— 仅 SFTP（`async with conn.start_sftp_client()`）+ 10MB 上限 + SFTP 不可用 raise + NUL 字节路径 raise；**禁止** cat fallback；验收：5.10 集成测试覆盖
- [ ] 5.7 实现 SSH env 注入约束（透传 asyncssh `env=`，**禁止** export 拼接）；验收：单测断言传给 asyncssh 的 cmd string 严格等于原 cmd（用 mock asyncssh 验证调用参数）
- [ ] 5.8 实现 SSH 凭据脱敏：`TargetError("ssh_auth_failed")` 包装前先用 `hostlens.agent.tools_adapter.scrub_exception_message` 清洗 asyncssh 原始异常字符串（path / IP / 凭据 / 身份键值对 / email-at-host 5 类）；验收：单测构造含 `literal-pwd-do-not-leak-12345` + `10.0.0.5` + `admin` 的假 asyncssh 异常，断言 `TargetError.__str__` 与 structlog log 都**不**含这些子串
- [ ] 5.9 实现 SSHTarget aclose（显式 + 析构 close control connection）；验收：单测开 + 关 100 次 SSHTarget，断言无 `ResourceWarning: unclosed transport`
- [ ] 5.10 **集成测试用真实 sshd 容器**：`tests/targets/test_ssh_integration.py` 用 `pytest-docker` 起 `linuxserver/openssh-server` 容器；容器启动 fixture 必须**注入 `AcceptEnv HOSTLENS_TEST_*` 到 sshd_config**（DOCKER_MODS / post-start exec 都可）；session-scoped fixture 复用；测试覆盖：exec echo / 非零 exit / signal-killed 128+signum / 超时 + 进程组回收 / 连接失败 + auto-retry / SFTP read_file / read_file 10MB raise / SFTP 不可用 raise / `HOSTLENS_TEST_VAR` 透传成功 + `SECRET_TOKEN` 透传失败（验证 AcceptEnv 限制） / control connection 复用（ss -tn 计数） / idle timeout 关闭 + 重连 / 断线 + 自动重连成功
- [ ] 5.11 **测试隔离**：每个测试用独立 username（容器里 `useradd test_${unique}`）或临时目录，**禁止**测试间共享 SSH 用户 home（避免一个测试改 ~/.ssh 影响别的）
- [ ] 5.12 **CI retry 收紧**：`pytest-rerunfailures` 仅对显式标 `@pytest.mark.flaky_ssh_integration` 的测试 retry 1 次（**禁止**全局 retry）；retry 时必须 dump container stderr 到 CI 日志；CI 配置 `pytest --strict-markers` 防 typo
- [ ] 5.12a **在 pyproject.toml 注册 `flaky_ssh_integration` marker**：`[tool.pytest.ini_options]` 下 `markers = ["flaky_ssh_integration: SSH integration tests allowed 1 retry due to container cold-start jitter"]`；否则 `--strict-markers` 会拒该 marker；验收：CI 跑 `pytest --strict-markers tests/targets/test_ssh_integration.py --collect-only` 不报 unknown marker 错误
- [ ] 5.13 **凭据脱敏端到端**：集成测试用错误 password 连真实 sshd，断言抛出的 TargetError + structlog log 都**不**含原始 password 子串（与 5.8 配套，端到端验证 scrubber 接通）
- [ ] 5.14 **SSH 连接复用验收（OPERABILITY §2 硬约束）**：集成测试用同一 SSHTarget 实例连跑 3 次 exec；fixture 在 sshd 容器内跑 `ss -tn '( sport = :22 )'` 计数，断言整个过程**只新增 1 个** ESTABLISHED 连接
- [ ] 5.15 **非 root 用户跑通验收**：本地起 sshd 容器 + 非 root shell 跑 `hostlens target test my-ssh` 必须返回 connectivity=ok + capabilities 含 SSH+SHELL+FILE_READ

## 6. CLI 子命令实现（含 EUID==0 拒绝）

- [ ] 6.1 实现 `hostlens.cli.target.add` —— 参数解析（`--key-path PATH`、`--password-env VAR`、`--passphrase-env VAR`，**禁止** `--key-env` / `--password` / `--passphrase` 等别名） / TargetsConfig 加载 / 写回 yaml / name 冲突 exit 2；**EUID==0 时 exit 1**（在参数校验前检查，输出修复建议）；验收：单测覆盖 add LocalTarget + add SSHTarget + 冲突 exit 码 + EUID==0 exit 1 且 yaml 未修改
- [ ] 6.2 实现 `hostlens.cli.target.list` —— 表格输出（Rich Table）与 `--json` 输出（含 capabilities）；只读允许 root；验收：**`--json` 输出 schema 稳定性**测试（snapshot 测试 + JSON Schema 校验）
- [ ] 6.3 实现 `hostlens.cli.target.remove` —— 交互确认 + `--yes` 跳过 + **非交互无 `--yes` 退出 1** + **EUID==0 exit 1**；验收：单测用 `runner.invoke(...)` 模拟无 TTY 环境断言 exit 1；mock EUID 断言 root 拒绝
- [ ] 6.4 实现 `hostlens.cli.target.test` —— 跑 `echo hostlens-probe-$$` + capability 探测 + 失败 exit 1（只读允许 root）；验收：单测含 LocalTarget 成功路径 + SSHTarget 不可达失败路径
- [ ] 6.5 **CLI 错误输出语义**：所有 target CLI 命令的错误信息走 stderr，数据走 stdout；验收：单测断言 `result.stderr_bytes` 与 `result.stdout_bytes` 分开
- [ ] 6.6 **CLI 参数 typo 拒绝**：`hostlens target add ... --key-env VAR` 必须 exit 2（Typer 自动处理未知参数）；验收：snapshot 测试 stderr 含 `"No such option: --key-env"`

## 7. doctor 集成

- [ ] 7.1 扩展 `hostlens.cli.doctor` 增加 `_check_targets()` 函数 —— 对每个 target 跑连通性 + 凭据来源识别（env_var vs inline_plaintext）+ capability 探测；验收：单测覆盖输出结构
- [ ] 7.2 doctor `--json` 输出新增 `targets` key；验收：snapshot 测试 + JSON Schema 校验
- [ ] 7.3 明文密码 warning（不阻塞）；某 target 连通失败 → doctor 整体 exit 1；空 registry（无 target 配置）→ doctor 显示 hint「跑 `hostlens target add` 开始」+ exit 0；验收：单测覆盖三种条件分支
- [ ] 7.4 **保持 M0 doctor 兼容性**：M0 doctor 已有的 `python_version` / `anthropic_api_key` / `config_dir` 检查必须保留；snapshot 测试包含原有 section

## 8. Tool Registry 集成（消除 stub，更新 CAPABILITY_ALLOWLIST）

- [ ] 8.1 修改 `src/hostlens/tools/base.py`：import 切换 `from hostlens.targets.registry import TargetRegistry`；**完全删除**原 stub `TargetRegistry` Protocol 定义 + 其 `list_summaries()` 方法签名；验收：mypy --strict 0 错误；`grep -rn "list_summaries" src/hostlens/tools/` 在 target 相关代码上**零结果**（InspectorRegistry stub 的 list_summaries 保留，下一提案处理）
- [ ] 8.2 **修改 `src/hostlens/tools/schemas/list_targets.py`**：`CAPABILITY_ALLOWLIST` 改为 `frozenset({c.value for c in Capability})`（import `from hostlens.targets.base import Capability`）；**删除**硬编码的 `file_write` / `docker` / `k8s_exec` 占位值；验收：单测断言 `CAPABILITY_ALLOWLIST == {"shell", "file_read", "ssh", "systemd", "docker_cli"}`；源码层面看到派生表达式（grep 验证）
- [ ] 8.3 修改 `src/hostlens/tools/default_tools.py`：`list_targets_handler` 从 `ctx.target_registry.list_summaries()` 迁移到 `ctx.target_registry.list()` + `ctx.target_registry.get_entry(target.name)`；在 handler 内做 `ExecutionTarget → TargetSummary` 投影：`name←target.name`、`kind←target.type`、`capabilities←[c.value for c in target.capabilities if c.value in CAPABILITY_ALLOWLIST]` 按字典序、`display_name/description/tags/enabled` 从 `get_entry(name)` 返回的 TargetEntry 派生；应用 `scrub_inventory_string` + 字段名 allowlist + 整 target skip 规则；验收：(a) grep 确认无 `list_summaries` 调用残留；(b) 行为测试覆盖 tool-registry spec §场景:list_targets handler 投影真实 TargetRegistry 数据且应用脱敏 + allowlist 的双 target 场景；(c) **metadata 来源契约测试**——按 tool-registry spec §场景:TargetSummary metadata 字段必须来自 TargetEntry... 的写法，用普通 class fake `ExecutionTarget` 实现注入 `display_name="FROM_TARGET_INSTANCE"` 属性 + `TargetEntry(display_name="FROM_ENTRY")`，调用 handler 断言返回 `"FROM_ENTRY"`（**禁止**用 LocalTarget——其字段集严格，无法任意 setattr）
- [ ] 8.4 修改 `tests/tools/` 下所有使用 stub TargetRegistry 的 fixture，统一改用 `build_registry_from_config(fixture_config)` 装配真实 LocalTarget；M2 现有 `tests/tools/test_list_targets.py` 测试用例 fixture 完全替换（**禁止**保留 stub fallback）；snapshot 输出含真实数据
- [ ] 8.5 验收 §需求:`ToolContext` 必须包含 M2 字段最小集 §场景:target_registry 是真实 TargetRegistry 类型 —— `assert ToolContext.__annotations__["target_registry"] is TargetRegistry`
- [ ] 8.6 验收 spec §场景:CAPABILITY_ALLOWLIST 派生自 Capability Enum —— 单测断言两者严格相等 + grep 源码确认派生形式

## 9. 文档与示例

- [ ] 9.1 `docs/operations/targets.md`：targets.yaml 配置示例 + SSH 凭据 best practice + 远端 sshd `AcceptEnv HOSTLENS_*` 配置示例 + 凭据脱敏（双层）说明 + connection pool 行为说明 + EUID==0 拒绝行为说明
- [ ] 9.2 更新 `docs/ARCHITECTURE.md` §5：把 M1 LocalTarget / SSHTarget 状态从"待办"改为"M1 落地（PR #<本提案 PR 号>）"；注明 SSHTarget 实现了 per-target connection pool（对齐 OPERABILITY §2）
- [ ] 9.3 `examples/m1-targets/README.md`：5 分钟 demo 路径（docker sshd + hostlens target add/test/list + doctor + root 拒绝验证 + connection 复用验证 + Tool Registry dispatch 端到端）；与 proposal.md Demo Path 严格一致
- [ ] 9.4 README "快速开始"小节增加 `hostlens target add` + `hostlens target test` 一行示例（用 `--key-path` / `--password-env` 命名）

## 10. 验证与 demo path

- [ ] 10.1 跑 `mypy --strict src/hostlens/targets/ src/hostlens/cli/target.py src/hostlens/tools/base.py src/hostlens/tools/default_tools.py src/hostlens/tools/schemas/list_targets.py` 必须 0 错误
- [ ] 10.2 跑 `ruff check src/ tests/` 必须 0 错误
- [ ] 10.3 跑 `pytest tests/targets/ tests/cli/test_target.py tests/cli/test_doctor.py tests/tools/test_list_targets.py tests/core/test_exceptions.py -v` 必须全绿
- [ ] 10.4 跑 proposal Demo Path 步骤 1-9（必须）+ 步骤 10（可选，因 InspectorRegistry 是 stub，依赖 M1 下一提案才能完整跑通）；记录每步输出截图到 `examples/m1-targets/`；步骤 10 不可用时在 `examples/m1-targets/README.md` 注明跳过原因 + 等下一提案落地后回填

## 11. Git 工作流与归档准备（按 CLAUDE.md §5.1 + §5.3）

- [ ] 11.1 完成所有上述任务后 commit 到 feature branch `feat/add-execution-target-abstraction`（已建好）
- [ ] 11.2 **commit 后、push 前**：跑 `/review-loop-codex` 对代码变更做对抗性 review，结论 APPROVE/CLEAR 才进入 11.3
- [ ] 11.3 push branch + 更新 PR #12（描述含 spec 引用与 Demo Path）
- [ ] 11.4 等 CI 全绿 + 人类 review 通过后 squash merge：`\gh pr merge 12 --squash --delete-branch`
- [ ] 11.5 准备归档：跑 `openspec-cn validate add-execution-target-abstraction` 确认变更可归档；后续运行 `/opsx:archive` 推进到 `openspec/specs/{execution-target, ssh-execution-target}/spec.md` 并同步 `openspec/specs/tool-registry-capability-layer/spec.md` 的 3 个 MODIFIED 需求块
