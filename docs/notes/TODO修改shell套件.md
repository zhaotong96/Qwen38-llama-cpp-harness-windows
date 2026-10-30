写在最前, 重复的报错留在上下文中没有意义. 如果报错了, 连续报错的情况下连续给到模型. 如果一旦通过了就不再保留报错信息了. 从上下文中把报错都剃出去, 损失一点命中没什么问题, 本来也不快, 上下文也不长, 还是别污染上下文给模型上压力了

“起始位置”= 工作目录（比如 `C:\Projects\MyApp`）。下面按 5 种方式分别给写法。每种都分两种情况：**在各自环境里直接写**，以及**从 cmd.exe 里调用这些前导时怎么写**。

---

## 1. `cmd /C`

`cmd /C` 本身没有工作目录参数，要在 `/C` 字符串里先 `cd /d`：

```cmd
cmd /C "cd /d "C:\Projects\MyApp" && cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build --config RelWithDebInfo -j"
```

嵌套双引号会冲突，路径有空格时更稳的写法是 `pushd`：

```cmd
pushd "C:\Projects\MyApp" && cmd /C "cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build --config RelWithDebInfo -j" & popd
```

或者用 `start /D`（不新开窗口、等待结束）：

```cmd
start /D "C:\Projects\MyApp" /B /WAIT cmd /C "cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build --config RelWithDebInfo -j"
```

---

## 2. pwsh 7.6

pwsh 7 有 `-WorkingDirectory` 参数，最直接：

```powershell
pwsh -NoProfile -WorkingDirectory "C:\Projects\MyApp" -Command 'cmd /C "cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build --config RelWithDebInfo -j"'
```

如果已经在 pwsh 里，也可以先切目录再调 `cmd`：

```powershell
Set-Location "C:\Projects\MyApp"
cmd /C "cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build --config RelWithDebInfo -j"
```

---

## 3. Windows PowerShell 5.1

5.1 **没有** `-WorkingDirectory` 参数。用 `-Command "Set-Location ...; cmd /C '...'"`：

```powershell
powershell -NoProfile -Command "Set-Location 'C:\Projects\MyApp'; cmd /C 'cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build --config RelWithDebInfo -j'"
```

或者用 `Start-Process -WorkingDirectory`：

```powershell
powershell -NoProfile -Command "Start-Process -FilePath cmd -ArgumentList '/C','cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build --config RelWithDebInfo -j' -WorkingDirectory 'C:\Projects\MyApp' -Wait -NoNewWindow"
```

---

## 4. Git Bash

用 `bash -c 'cd ... && cmd //C "..."'`。Git Bash 里路径可以写 `/c/Projects/MyApp` 或 `C:/Projects/MyApp`：

```bash
bash -c 'cd "/c/Projects/MyApp" && cmd //C "cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build --config RelWithDebInfo -j"'
```

或：

```bash
bash -c 'cd "C:/Projects/MyApp" && cmd //C "cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build --config RelWithDebInfo -j"'
```

也可以把 `cd` 放进 `cmd` 字符串里：

```bash
cmd //C "cd /d C:\Projects\MyApp && cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build build --config RelWithDebInfo -j"
```

---

## 5. Python

用 `subprocess.run(..., cwd=...)`，最干净：

```python
import subprocess, sys

cmd = (
    "cmake -B build -S . -DCMAKE_BUILD_TYPE=RelWithDebInfo "
    "&& cmake --build build --config RelWithDebInfo -j"
)

r = subprocess.run(["cmd", "/C", cmd], cwd=r"C:\Projects\MyApp")
sys.exit(r.returncode)
```

---

## 汇总表

| 方式 | 带起始位置的写法 |
|---|---|
| `cmd /C` | `cmd /C "cd /d "路径" && ..."`，或 `pushd "路径" && cmd /C "..." & popd`，或 `start /D "路径" /B /WAIT cmd /C "..."` |
| pwsh 7.6 | `pwsh -WorkingDirectory "路径" -Command 'cmd /C "..."'` |
| PowerShell 5.1 | `powershell -Command "Set-Location '路径'; cmd /C '...'"`，或 `Start-Process ... -WorkingDirectory '路径'` |
| Git Bash | `bash -c 'cd "/c/路径" && cmd //C "..."'` |
| Python | `subprocess.run(["cmd","/C",cmd], cwd=r"路径")` |

关键：`cmd /C` 没工作目录参数，所以要靠 `cd /d`、`pushd`、`start /D`、`-WorkingDirectory`、`cd &&`、`cwd=` 这些手段来指定起始位置。