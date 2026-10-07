# dsh-gui-handoff

**A DeepSeek Harness skill that hands a command-line-verified operation over to your GUI.**

**把命令行验证过的操作，交接给你的图形界面。**

[English](#english) · [中文](#中文)

---

## English

### The problem

There is a gap nobody names: an agent can do something perfectly in a terminal,
and the user still cannot reproduce it by hand — because nobody ever walked the
GUI path and nobody wrote down what to click.

Telling the user "just run this script" is not a fix. It leaves them with a black
box they cannot learn from, that breaks on another machine, and that does not
cover the GUI operations which have no config file at all.

### What this skill does

Two paths, **path B by default**:

| | Path B (default) | Path A (fallback) |
|---|---|---|
| Approach | The agent **actually drives the GUI once** | Freeze the steps into config files |
| Output | A tutorial with **real screenshots** | A config file + one keystroke |
| User learns it | **Yes** | No |
| Survives a new machine | **Yes** | No |

Path B runs a five-stage chain — **drive → observe → capture → reconstruct →
annotate** — and insists on two things that are easy to skip:

- **Ground truth, not vibes.** Whether a build succeeded is decided by the
  artifact's existence and timestamp. Whether the program *ran* is decided by its
  output or the debugger's exit code. Screenshots are for the human, never the
  verdict.
- **Verify the user can reproduce it.** "The machine ran it" is not "the user ran
  it". The tutorial must list its own preconditions, the user must walk it, and
  the scene must be reset afterwards so the user can tell their own success from
  pre-existing state.

### The part that is actually valuable

`skills/gui-handoff/SKILL.md` carries **30+ hard constraints**, nearly all of them
paid for with real debugging time:

- Chinese/UTF-8 path failures in linkers and debuggers — and the trap that
  **a command line can succeed while the GUI fails**, because the shell silently
  re-encodes arguments into the encoding the tool happened to want
- Chinese IME corrupting injected keystrokes (`by` → `不要`)
- Why **matching a window by title substring is dangerous** (three separate
  incidents), and what to do instead
- Why Chromium/Electron needs scancodes, and why focus can silently stay on
  another window
- Why reading fine text from a screenshot needs crop-and-upscale first
- How a hand-written PNG encoder produced **three horizontally-squashed copies**
  of every screenshot — and why that made a vision model look like a liar when it
  was telling the truth

Plus **seven working disciplines**, every one of them written down after being
violated. The most important:

> **Check the criterion itself before accepting a verification.**
> A tutorial once said "if you see the artifact, it worked". The user followed it,
> reported success, and the agent declared the acceptance test passed — but that
> criterion only covered *building*, not *running*. **When the criterion is wrong,
> passing is worthless.**

### Install

```sh
dsh plugin --profile <your-profile> add dsh-gui-handoff
```

Or from a checkout:

```sh
dsh plugin --profile <your-profile> add /path/to/dsh-gui-handoff
```

The plugin registers one skill (`gui-handoff`, rank 600 — lowest priority, so a
locally edited copy in your project or user skills directory always wins).

### Windows driver setup and checks

Python 3.11 or newer is required for the reference driver. Capture and input use
only the standard library; the optional read_region tool requires Pillow:

~~~sh
python -m pip install Pillow
python tools/gui-control/server.py
~~~

Configure your MCP host to launch that command with the checkout/package's
absolute path. It speaks newline-delimited JSON-RPC on stdio; diagnostics go to
stderr. Version 0.1.1 fixes pointer-sized Win32 handles, virtual-key Enter/Tab,
input failure reporting, and malformed-request handling. Input arguments are
validated before dispatch, and text to be typed is omitted from diagnostic logs.

Before taking control, run the countdown notice as a separate PowerShell process.
**Proceed only if its exit code is 0. Exit code 2 means the user cancelled.**
The notice is not automatically invoked by this reference server; the caller
must enforce this gate. The focus_window tool now fails if foreground focus
cannot be confirmed, so the caller must stop rather than typing into another window.

~~~powershell
powershell.exe -NoProfile -File tools/notify-takeover.ps1 -Task 'GUI walkthrough' -Seconds 3
if ($LASTEXITCODE -ne 0) { throw 'Takeover cancelled' }
~~~

Maintenance checks:

~~~sh
npm test
python -m unittest discover -s tests -v
~~~

The Windows tests mock keyboard injection. Their live checks only enumerate
windows and read cursor information; they do not click or type into your apps.

### What ships

```
lib/index.js                     skill provider (no runtime dependencies)
cordis.patch.yml                 bundle manifest
skills/gui-handoff/SKILL.md      the skill itself
tools/gui-control/server.py      reference GUI-control MCP server (Windows)
tools/notify-takeover.ps1        countdown notice shown before taking the mouse
```

The `tools/` directory is a **reference implementation for Windows**, not a
requirement. The portable part is the workflow and the constraints; Linux and
macOS need their own equivalents.

### Requirements

- DeepSeek Harness `>= 0.1.5-rc.1`
- Path B's driver stage needs *some* way to drive the target OS's GUI. This repo
  ships one for Windows; without one, the skill degrades to B2–B4
  (user drives while the agent observes) rather than failing.

### License

MIT.

---

## 中文

### 它解决什么

有一个没人命名的落差：**agent 能在终端里把事情做对，用户却无法用手复现** ——
因为没人走过图形界面那条路，也没人写下来该点哪里。

告诉用户"你跑一下这个脚本"不是解法。它留下的是一个学不会的黑盒、
换台机器就废，而且**根本覆盖不了那些没有配置文件的图形界面操作**。

### 这个 skill 做什么

两条路，**默认走 B**：

| | 路径 B（默认） | 路径 A（降级） |
|---|---|---|
| 做法 | agent **真的操作 GUI 走一遍** | 把步骤固化成配置文件 |
| 产出 | **带真截图**的教程 | 配置文件 + 一键操作 |
| 用户学到 | **是** | 否 |
| 换机器可用 | **是** | 否 |

路径 B 跑一条五环链 —— **驱动 → 观察 → 记录 → 还原 → 标注** —— 并坚持两件
最容易被跳过的事：

- **用地面真值，不用感觉。** 构建是否成功，看**产物是否存在 + 时间戳**；
  程序是否**真的运行了**，看程序输出或调试器退出码。截图是给人看的，
  **永远不作判据**。
- **验证用户能复现。**「机器跑通」不等于「用户跑通」。教程必须列出自己的前提，
  用户必须实走一遍，跑完还要**重置现场** —— 否则用户分不清产物是自己跑出来的
  还是早就有的。

### 真正值钱的部分

`skills/gui-handoff/SKILL.md` 里是 **30 多条硬约束**，几乎每一条都是拿真实的
调试时间换来的：

- 链接器与调试器的中文/UTF-8 路径失败 —— 以及那个陷阱：
  **命令行能过、图形界面却挂**，因为 shell 会悄悄把参数重新编码成工具恰好想要的编码
- 中文输入法污染注入的按键（`by` → `不要`）
- 为什么**按标题子串匹配窗口极其危险**（三次独立事故），以及该怎么做
- 为什么 Chromium/Electron 需要扫描码，为什么焦点可能悄悄留在另一个窗口
- 为什么从截图里读小字必须先裁剪放大
- 一个手写 PNG 编码器如何把每张截图变成**三份横向压扁的副本** ——
  以及它如何让一个视觉模型背了"胡说"的黑锅，而它说的全是真的

外加**七条工作纪律**，每一条都是**先违反、才写下的**。最重要的那条：

> **验收之前，先检查判据本身。**
> 有一份教程写着「看到产物就是成功」。用户照着做了、报告成功，agent 也据此
> 宣布验收通过 —— 但那个判据**只覆盖构建，不覆盖运行**。
> **判据错了，通过就是假的。**

### 安装

```sh
dsh plugin --profile <你的 profile> add dsh-gui-handoff
```

或从本地目录安装：

```sh
dsh plugin --profile <你的 profile> add /path/to/dsh-gui-handoff
```

插件注册一个 skill（`gui-handoff`，rank 600 —— 最低优先级，因此你在项目或用户
技能目录里本地改过的同名副本总是优先）。

### Windows 工具维护

参考驱动需要 Python 3.11+；截图和输入使用标准库，read_region 另需安装 Pillow。
控制工具和提示脚本现已包含在安装包中。

0.1.1 修复了 64 位窗口句柄、回车/Tab 注入、输入失败误报成功、异常请求导致
服务退出等问题。自动化调用者必须先运行接管提示，**只有退出码 0 才能继续；
退出码 2 表示用户取消**。参考 server 不会自动启动提示，需要调用者执行此检查。
聚焦目标窗口失败时工具会报错，调用者应停止输入。

运行 npm test 检查插件与安装包；Windows 上再运行
python -m unittest discover -s tests -v 检查协议、按键结构与 PNG。
测试不会向桌面注入按键或点击鼠标。

### 目录

```
lib/index.js                     skill provider（无运行时依赖）
cordis.patch.yml                 bundle 清单
skills/gui-handoff/SKILL.md      skill 本体
tools/gui-control/server.py      参考用的 GUI 控制 MCP server（Windows）
tools/notify-takeover.ps1        抢鼠标前的倒计时提示
```

`tools/` 是**面向 Windows 的参考实现，不是必需项**。可移植的是流程与约束；
Linux / macOS 需要各自的对等物。

### 依赖

- DeepSeek Harness `>= 0.1.5-rc.1`
- 路径 B 的驱动环节需要**某种**驱动目标系统图形界面的手段。本仓库提供一个
  Windows 实现；没有它时，skill 会降级到 B2–B4（用户操作、agent 观察），
  而不是直接失败。

### 许可

MIT。
