## 新增需求

### 需求:CLI 启动必须把 `.env` 加载进 `os.environ` 以统一 env 配置源

`hostlens` CLI 根回调**必须**在任何子命令逻辑与 `load_settings()` 之前，调用一次 `python-dotenv` 的 `load_dotenv`，从 **cwd 的 `.env`** 把变量加载进 `os.environ`，使所有 env-based 配置（pydantic `Settings`、`${VAR}` 占位解析、inspector secrets）共享 `.env` 这一唯一来源。**必须** `override=False`（已存在的 `os.environ` / 显式 `export` 优先，`.env` 只填补缺失项）；`.env` 缺失**必须**静默跳过（不抛异常、不打印路径或缺失提示）；**禁止**向上递归查找父目录的 `.env`（与 `Settings` 的 cwd 语义保持一致）；加载过程**禁止**打印任何变量值（密钥不入日志）。

#### 场景:`.env` 中的密钥可被 `${VAR}` 解析读到
- **当** cwd 存在 `.env` 含 `TELEGRAM_BOT_TOKEN=...`，`notifiers.yaml` 的 telegram 通道写 `bot_token: ${TELEGRAM_BOT_TOKEN}`，运行加载通道的命令（如 `notify channels`），且该变量未经 `export`
- **那么** `${TELEGRAM_BOT_TOKEN}` **必须**解析为 `.env` 中的值、通道加载成功，无需额外 `export`

#### 场景:显式 export 覆盖 `.env`（override=False）
- **当** `os.environ["X"]="from_export"` 已设置，且 cwd 的 `.env` 含 `X=from_dotenv`
- **那么** 加载后 `os.environ["X"]` **必须**仍为 `"from_export"`（已存在值不被 `.env` 覆盖）

#### 场景:无 `.env` 静默零影响
- **当** cwd 不存在 `.env`，运行任一 CLI 命令
- **那么** **禁止**抛异常、**禁止**打印 `.env` 路径或缺失提示；命令照常执行（env 仅来自真实 `os.environ`）

#### 场景:Settings 取值不因加载改变
- **当** `.env` 含 `HOSTLENS_LOG_MODE=dev` 且无对应 `export`
- **那么** `load_settings()` **必须**仍得到 `log_mode="dev"`（取值结果与加载前一致，仅命中来源从 `.env file` 层前移到 `os.environ` 层）
