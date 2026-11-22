# DSH 插件安装排障:三个静默失败点

> 针对 `@deepseek-ai/dsh` (DeepSeek Harness) 挂载 `dsh-tmem` 插件时排查出的安装层问题。
> 三个缺陷**全部静默失败**:没有异常、没有非零退出码,只有一行 `did not activate` 警告。
> 本文记录根因、修复位置与可复用的诊断手法。

---

## 0. 速查:一次判断插件是否真的装上了

```bash
# 1) 看合成后的 patch 树,确认 tmem 条目被解析成哪个绝对路径
dsh --profile headless --dump-config | grep -A2 tmem

# 2) 真启动一次,看未激活列表
dsh --profile headless "hello"
#    ✅ tmem 不在 "did not activate" 列表里
#    ❌ tmem (file:///...): failed to import

# 3) 确认工具真的进了 agent 的 tool schema
zstd -dc ~/.dsh/sessions/*/*/session.v4.jsonl.zstd | grep -c tmem_search
```

---

## 1. 缺陷一:patch 里的相对路径按 **patch 文件所在目录** 解析

### 现象

```
dsh: warning: 2 entries did not activate
tmem (file:///archive/tmem/.dsh/.dsh/plugins/tmem-dsh-plugin.mjs): failed to import
```

注意路径里的 **`.dsh/.dsh`** —— 目录被拼了两次。

### 根因

dsh 解析 patch 条目中相对 `name` 时,基准目录是**声明该条目的 patch 文件自身所在目录**,不是工作区根。

`.dsh/tmem.patch.yml` 位于 `<workspace>/.dsh/`,而生成器写入的是:

```yaml
name: ./.dsh/plugins/tmem-dsh-plugin.mjs      # ← 以工作区根为基准写的
```

于是解析结果变成 `<workspace>/.dsh/` + `./.dsh/plugins/...` = **`<workspace>/.dsh/.dsh/plugins/...`**,该文件不存在 → import 失败。

### 影响面

- `tmem init` 在**每一个**新工作区生成的 workspace patch 都是坏的(生成器与消费方对路径基准的理解不一致)。
- 仓库里已提交的那份 `.dsh/tmem.patch.yml` 同样是坏的。

### 修复

```yaml
# tmem/installer.mjs:161 与 .dsh/tmem.patch.yml
- insert:
    - id: tmem
      name: ./plugins/tmem-dsh-plugin.mjs    # 相对 <workspace>/.dsh/
```

并已在 `tmem/installer.mjs` 就地注释该解析规则,避免后续回归。

> **home 层不受此规则影响**:`~/.dsh/cordis.patch.yml` 由 installer 写入**绝对路径**,不受相对基准影响。

---

## 2. 缺陷二:CLI 入口判定在 symlink 下永久失配 → 静默退出

### 现象

```bash
$ tmem --version
$ tmem status
# 零输出、exit code 0 —— 命令像坏了一样
```

### 根因

`tmem/cli.mjs` 末尾用原始路径比较判断"自己是不是入口":

```js
const isEntrypoint =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
```

Node 启动主模块时会**解析 symlink**,于是:

| 取值 | 实际内容 |
| :--- | :--- |
| `process.argv[1]` | symlink 路径 `~/.nvm/.../bin/tmem` |
| `fileURLToPath(import.meta.url)` | 真实路径 `/archive/tmem/tmem/cli.mjs` |

两者**永不相等** → `main()` 从不执行 → 进程正常退出、无 stdout、无 stderr、exit 0。

### 影响面

任何以 symlink 方式分发的场景都会让 CLI 变成哑巴:`npm link`、`npm link -g`、手工软链 bin。由于退出码是 0,在 CI 或 shell 脚本里**完全看不出来**。

### 修复

两边都归一到真实路径再比较(`tmem/cli.mjs`):

```js
function resolveRealPath(target) {
  try { return realpathSync(target); } catch { return path.resolve(target); }
}
const isEntrypoint =
  process.argv[1] &&
  resolveRealPath(process.argv[1]) === resolveRealPath(fileURLToPath(import.meta.url));
```

`--preserve-symlinks-main` 下同样成立(两边都会被 realpath 归一)。

---

## 3. 缺陷三:home 级用户层缺失,插件对所有 profile 不可见

### 现象

`~/.dsh/cordis.patch.yml` **根本不存在**;`~/.dsh/profiles/{web,headless}/cordis.patch.yml` 里也没有 `tmem` 条目 → tmem 从未进入任何 profile。

### 根因

`~/.dsh/cordis.patch.yml` 是 dsh 的 **home 级用户层**,对**每个** profile 生效。dsh 的 patch 叠加顺序为:

```
bundle 层(按 package.json 的 dsh.profile.bundles 顺序)
  → profile 用户层   ~/.dsh/profiles/<profile>/cordis.patch.yml
  → home 用户层      ~/.dsh/cordis.patch.yml        ← 全局生效,优先级高于 profile 层
  → --patch 覆盖层   (命令行,按 argv 顺序)
  → telemetry 开关
```

installer 只会向 home 层写补丁(且是幂等的:已含 `id: tmem` 就跳过)。之前没有 `tmem init` 跑成功过,home 层就一直空着。

### 修复

新建 `~/.dsh/cordis.patch.yml`:

```yaml
# Home-level user patch layer for dsh (applies to every profile).
- insert:
    - id: tmem
      name: "/archive/tmem/tmem/dsh_plugin.mjs"
```

用绝对路径,避免 home 层与 workspace 层的路径基准混淆。

> **作用域说明**:插件的 **store 根** 仍按**会话工作目录**解析(`resolveSessionWorkspace` → workingDirectory 服务 / session cwd),
> 所以全局注册后,记忆仍然写到各自项目的 `.tmem/`,不会串到插件自身仓库。

---

## 4. 缺陷四:测试把 bug 固化成了契约

`tests/intent_graph.test.mjs` 原先断言:

```js
assert.ok(wsPatch.includes("./.dsh/plugins/tmem-dsh-plugin.mjs"));   // ← 绿,但产出是坏的
```

安装器产物测试若只断言**字符串字面量**,就会把 bug 锁进契约。

**修复后**:

```js
assert.ok(wsPatch.includes("./plugins/tmem-dsh-plugin.mjs"));
assert.ok(!wsPatch.includes("./.dsh/plugins/tmem-dsh-plugin.mjs"));
```

**教训**:installer 产物类测试应断言**语义**(解析后指向真实存在的文件),而非路径字面量。

---

## 5. 可复用的隔离诊断法

当"插件没加载"时,用以下顺序在 2 分钟内定位到是**路径问题**还是**插件代码问题**:

1. **看解析结果**:`dsh --profile <p> --dump-config | grep -A2 <plugin-id>` → 确认 `name:` 展开成的是哪个 `file://` 绝对路径。
2. **看真实加载结果**:启动一次,读 `did not activate` 警告里的 `failed to import` 具体路径。
3. **用覆盖层隔离**:另写一个 `--patch`,把同一插件指向**确认存在的绝对路径**。
   - 若未激活列表里该插件消失 → **路径问题**(本次三个缺陷均属此类);
   - 若依旧失败 → 才是插件代码/依赖问题。
4. **确认注册生效**:解压会话日志 `~/.dsh/sessions/<workspace>/session.v4.jsonl.zstd`(`zstd -dc`),
   搜插件工具名(如 `tmem_search`)。能搜到 schema → 工具确实进了 agent 的 tool list。

---

## 6. 遗留的环境性问题(与本次修复无关)

- **headless profile 缺凭据**:`MISSING_CREDENTIAL: llm-deepseek`,需 `DEEPSEEK_API_KEY`;
  web profile 走 `minimax-cn`(`MINIMAX_CN_API_KEY`,已存于 `~/.dsh/.credentials.yaml`)。
- **`npm test` 有 2 条改动前即存在的失败**(`Live @deepseek-ai/dsh compact + prune integration`、
  Milestone 5 压缩召回基准),已用 `git stash` 拉基线确认与本次改动无关。

---

## 7. 变更清单

| 文件 | 变更 |
| :--- | :--- |
| `tmem/installer.mjs` | 生成正确的相对路径 + 注释解析规则 |
| `.dsh/tmem.patch.yml` | 同步修正路径 |
| `tmem/cli.mjs` | 入口判定改为 realpath 归一比较 |
| `tests/intent_graph.test.mjs` | 断言改为语义化,并反向锁定旧写法 |
| `~/.dsh/cordis.patch.yml` | 新建:全局注册 tmem 插件 |
| 全局 npm | `npm link` 安装 `dsh-tmem`,使 `tmem` 命令进入 PATH |
