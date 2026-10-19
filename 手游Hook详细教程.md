# 手游 (Android) il2cpp Hook 详细教程

> —— 从 dump.cs 到 hook 在手机上真正跑起来的完整流程
> 贯穿示例：示例手游 `ExampleClass.ExampleMethod`（暂停），RVA `0xDEADBEEF`
> 配套文件：`example_hook_pause_get_base.c`

---

## 〇、先看全局：整条链路长什么样

```
游戏 APK → Il2CppDumper 提取 → dump.cs (所有类/方法/RVA)
                │
                ▼
        dump工具 (中文翻译 / 搜索 / 查 RVA / 一键生成 hook 模板)
                │
                ▼
        选定目标方法, 拿到 RVA (相对模块基址的偏移)
                │
                ▼
        写 hook 代码: 运行时拿基址 + 基址+RVA 算真实地址 + DobbyHook
                │
                ▼
        NDK 编译成 libhook.so
                │
                ▼
        注入进游戏进程 (Zygisk / 注入器 / 重打包 / Frida)
                │
                ▼
        游戏里触发该方法 → adb logcat 看到基址和真实地址输出
```

为什么必须"运行时拿基址"？因为 **ASLR**（地址随机化）：每次启动游戏，
libil2cpp.so 都会被加载到不同的地址。dump.cs 里的 RVA 是**偏移量**，
不是绝对地址。真实地址 = 本次启动的基址 + RVA，所以 hook 代码必须
在游戏进程内部、加载时动态计算。这就是上一个例子里 `il2cpp_base()` 存在的意义。

---

## 一、5 分钟概念扫盲

| 名词 | 一句话解释 |
|---|---|
| **il2cpp** | Unity 把 C# 代码编译成 C++ 再编译成机器码的方案。游戏逻辑全在一个大 so 里 |
| **libil2cpp.so / GameAssembly.dll** | il2cpp 生成的大模块。安卓叫前者，Windows 叫后者。你要 hook 的所有函数都在里面 |
| **dump.cs** | 用 Il2CppDumper 从游戏的 so + global-metadata.dat 里还原出的"类/字段/方法清单"，带每个方法的 RVA |
| **RVA** | 相对虚拟地址。函数在模块内部的偏移。dump.cs 里给的就是它 |
| **基址 (base)** | 模块本次启动被加载到的起始地址。真实地址 = 基址 + RVA |
| **ASLR** | 每次启动基址都变，所以代码里必须动态拿基址，写死必崩 |
| **Dobby** | 开源的 inline hook 框架 (github.com/jmpews/Dobby)。把目标函数开头几条指令替换成跳转，跳到你的函数，同时保留"原函数"可调用 |
| **Zygisk** | Magisk 的组件，让自定义 so 在每个 App 进程孵化时自动加载。是目前最干净的注入方式 |
| **MethodInfo*** | il2cpp 每个方法最后一个隐藏参数。hook 签名照抄模板即可，一般用不到它 |

**il2cpp 方法的 C 签名规律**（重要）：

- 实例方法：`返回值 函数(void* this, 原参数..., void* method)`
- 静态方法：`返回值 函数(原参数..., void* method)`（没有 this）
- hook 时照 dump.cs 里的签名翻译成 C 类型即可（int→int/float→float/bool→bool/string→void* 等）

---

## 二、环境准备清单

**电脑端：**

| 工具 | 用途 | 获取 |
|---|---|---|
| Android NDK (r25 以上) | 编译 arm64 的 .so | developer.android.com/ndk/downloads |
| Dobby (源码或预编译) | hook 框架 | github.com/jmpews/Dobby |
| adb | 推文件、看日志 | Android platform-tools |
| (可选) Frida | 快速调试通道 | github.com/frida/frida/releases |
| (可选) apktool | 免 root 重打包方案 | apktool.org |
| dump工具.exe | 查 dump、生成模板 | 本目录 |

**手机端（三选一）：**

1. **root 真机 + Magisk**（主线方案）：装 Magisk，开启 Zygisk —— 最稳、最适合长期用
2. **雷电/逍遥模拟器**：自带 root，调试方便（示例手游对模拟器本身兼容）
3. **无 root 真机**：只能走重打包 APK 方案（见 6.3，商业手游基本走不通，原因后述）

**确认包名与 ABI：**
示例手游手游包名 `com.example.game`。现代手机基本都是 **arm64-v8a**，
本教程所有命令按 arm64 写；32 位模拟器把 `arm64-v8a` 换成 `armeabi-v7a`。

---

## 三、第 1 步：从 dump.cs 定位目标（dump工具 里做）

打开 dump工具（或直接在对话里用 MCP 工具），两种查法：

- **按方法名找**：`find_method("set_value")` → 跨所有类搜方法，返回 类.方法 + RVA + 参数
- **按语义找类**：`search_class("跳舞")` → 按中文释义/分类搜类，再 `get_class_detail` 看类里有什么方法

本教程的例子：

```
[Assembly-CSharp.dll] ExampleClass.ExampleMethod   RVA 0xDEADBEEF   // 暂停
```

在详情页点「生成 Hook」→ 复制模板 → 模板长这样：

```c
#include <stdint.h>
#include "dobby.h"

// 目标: ExampleClass.ExampleMethod (暂停)  RVA: 0xDEADBEEF
static void (*orig_ExampleClass_ExampleMethod)(void* instance, void* method);

static int my_ExampleClass_ExampleMethod(void* instance, void* method)
{
    // TODO: 在这里写你的逻辑
    return orig_ExampleClass_ExampleMethod(instance, method);
}
```

模板里 `(uintptr_t)il2cpp_base` 是故意留空的——基址获取必须结合平台写，
这就是教程示例文件 `example_hook_pause_get_base.c` 补全的部分。

> **重要**：游戏每次更新，so 就变，dump.cs 里的 RVA 全部作废，必须重新 dump。
> RVA 对不上是"hook 后闪退"的第一大原因。

---

## 四、第 2 步：写 hook 代码（Android 版逐段讲解）

完整代码看 `example_hook_pause_get_base.c`，这里逐段讲清楚"为什么这么写"。

### 4.1 拿基址 —— 全教程最核心的一段

安卓没有 Windows 那样的 `GetModuleHandle`，标准做法是读 `/proc/self/maps`。
maps 每行长这样：

```
7a1b2c3000-7a1b2c5000 r--p 00000000 103:34 1234  /data/app/.../libil2cpp.so
7a1b2c5000-7a1b2c9000 r-xp 00002000 103:34 1234  /data/app/.../libil2cpp.so
```

格式：`起始地址-结束地址 权限 文件内偏移 ... 路径`

**基址 = 文件内偏移为 0 的那一行的起始地址**（ELF 头所在的第一条映射）：

```c
static uintptr_t il2cpp_base(void)
{
    static uintptr_t cached = 0;            // 拿过一次就缓存, 别反复读文件
    if (cached) return cached;

    FILE* fp = fopen("/proc/self/maps", "r");
    if (!fp) return 0;
    char line[512];
    while (fgets(line, sizeof(line), fp)) {
        if (!strstr(line, "libil2cpp.so")) continue;
        uintptr_t start, end, file_off;
        char perms[8];
        if (sscanf(line, "%lx-%lx %7s %lx", &start, &end, perms, &file_off) == 4
            && file_off == 0) {             // ← 偏移为 0 的那条才是基址
            cached = start;
            break;
        }
    }
    fclose(fp);
    return cached;
}
```

**新手最常踩的坑**：拿 `r-xp`（代码段）那条的起始当地址。错！
代码段比基址大一个 text 段偏移（上面例子里是 0x2000），直接加 RVA 会
得到错地址 → DobbyHook 装到错误位置 → 游戏闪退。必须认准 `offset == 0`。

> 进阶提示：很多老教程用 `strstr(line, "r-xp")` 找基址，在简单 so 上碰巧
> 能对（text 紧贴基址、偏差小到没影响），但现代大 so 必翻车。用 offset==0 永远是对的。

### 4.2 日志 —— 安卓用 logcat

```c
#include <android/log.h>
#define LOG(...)  __android_log_print(ANDROID_LOG_INFO, "DumpHook", __VA_ARGS__)
```

`DumpHook` 是日志标签，之后 `adb logcat -s DumpHook` 只看我们的输出，不受游戏刷屏干扰。

### 4.3 hook 函数本体

```c
static void (*orig_ExampleMethod)(void* instance, void* method);   // 原函数指针

static void my_ExampleMethod(void* instance, void* method)
{
    uintptr_t base = il2cpp_base();
    LOG("[DumpHook] il2cpp 模块基址   = 0x%llX\n", (unsigned long long)base);
    LOG("[DumpHook] ExampleMethod 真实地址     = 0x%llX\n",
        (unsigned long long)(base + PAUSE_RVA));
    LOG("[DumpHook] this  实例地址     = 0x%llX\n",
        (unsigned long long)(uintptr_t)instance);

    orig_ExampleMethod(instance, method);     // 调原函数 = 正常暂停
    // 想让"暂停"失效? 注释掉上面这行, 直接 return 即可
}
```

要点：
- **hook 函数签名必须和模板一致**（`void* instance, void* method`），返回值类型照 dump.cs 里抄（void/int/float/对象指针）
- `orig_ExampleMethod` 是 Dobby 填好的"原函数入口"，想放行就调它；不调 = 跳过原逻辑
- `instance` 就是这个对象的 this 指针，后面进阶用它打印类名

### 4.4 安装入口 —— .so 被加载时自动执行

```c
static void install_hooks(void)
{
    uintptr_t base = il2cpp_base();
    if (!base) { LOG("[DumpHook] 拿不到基址\n"); return; }

    void* target = (void*)(base + PAUSE_RVA);
    if (DobbyHook(target, (void*)my_ExampleMethod, (void**)&orig_ExampleMethod) == 0)
        LOG("[DumpHook] 安装成功\n");
}

__attribute__((constructor))          // so 一被 dlopen, 这个函数自动跑
static void install_on_load(void) { install_hooks(); }
```

**时序问题**：我们的 so 被加载时，libil2cpp.so 必须已经加载过了
（否则 maps 里找不到它）。Zygisk 方案在 App 进程 specialize 之后才加载我们，
此时 il2cpp 肯定已就绪，天然没问题；其他注入方式如果拿不到基址，先确认注入时机。

---

## 五、第 3 步：编译成 libhook.so

### 5.1 准备 Dobby 静态库

优先找 Dobby 的 GitHub Release 有没有预编译包（有就跳过本节）。
没有就源码编译（需要 NDK + CMake）：

```bash
git clone https://github.com/jmpews/Dobby --recursive
cd Dobby && mkdir build && cd build

# NDK 自带安卓工具链文件, CMAKE_TOOLCHAIN_FILE 指向它
# 把 %NDK% 换成你的 NDK 解压路径, 例如 D:\android-ndk-r26d
cmake .. -G Ninja ^
  -DCMAKE_TOOLCHAIN_FILE=%NDK%/build/cmake/android.toolchain.cmake ^
  -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-21 ^
  -DCMAKE_BUILD_TYPE=Release
ninja
```

产物在 `build/` 里：头文件 `include/dobby.h` + 静态库 `libdobby.a`
（不同版本目录略有差异，找到 .a 文件即可）。

### 5.2 一条命令编译我们的 hook

NDK 的 clang 在 `%NDK%\toolchains\llvm\prebuilt\windows-x86_64\bin\`，
把它加进 PATH，然后：

```bash
aarch64-linux-android21-clang -shared -O2 -fPIC ^
  example_hook_pause_get_base.c ^
  -I<Dobby的include目录> -L<Dobby的lib目录> -ldobby ^
  -llog ^
  -o libhook.so
```

逐参数解释：

| 参数 | 作用 |
|---|---|
| `-shared` | 编译成共享库 (.so)，不是可执行文件 |
| `-O2` | 优化级别，hook 代码别用 -O0（太慢）也别激进内联 |
| `-fPIC` | 位置无关代码，.so 必需 |
| `-I` / `-L` | 找 dobby.h 头文件 / libdobby.a 库的目录 |
| `-ldobby` | 链接 Dobby 静态库 |
| `-llog` | 链接安卓 log 库（`__android_log_print` 用） |
| `-o libhook.so` | 产物名。注意安卓只认 `lib` 开头的 so |

顺手减小体积：`llvm-strip libhook.so`

**自检产物**（养成习惯，防止编译出 x86 的 so 推到 arm 手机上白忙）：

```bash
llvm-readelf libhook.so -h | grep Machine
# 期望: Advanced Micro Devices X86-64 → 说明编错了(没带前缀命令)
# 期望: AArch64 → 正确
```

### 5.3 传到手机

```bash
# 模拟器: 先连接 (雷电默认 5555)
adb connect 127.0.0.1:5555
adb devices                          # 确认设备在线

adb push libhook.so /data/local/tmp/
```

---

## 六、第 4 步：注入进游戏进程（重头戏）

### 6.0 四种方案总览（先选路线）

| 方案 | 需要 root | 难度 | 适合场景 | 检测风险 |
|---|---|---|---|---|
| A. Zygisk 模块 | ✔ Magisk | ★★★ | 真机长期方案 | 中（可隐藏） |
| B. Frida | ✔ (server) | ★ | 调试期最快，免编译 | 高（部分商业游戏有 frida 检测） |
| C. ptrace 注入器 | ✔ | ★★★★ | 一次性验证 | 高 |
| D. 重打包 APK | ✘ | ★★★ | 只有无 root 设备时 | 签名变化，带校验的登录直接拒 |

**新手建议**：先用 **B (Frida)** 十分钟验证 RVA 和思路对不对，
跑通了再走 **A (Zygisk)** 做成稳定版。路线 C/D 了解即可。

### 6.1 方案 A：Zygisk 模块（真机主线）

原理：Magisk 的 Zygisk 会在每个 App 进程从 zygote 孵化出来时，
先加载你模块目录 `zygisk/` 里的 so，你在里面判断"这个进程是不是目标游戏"，
是就把真正的 `libhook.so` `dlopen` 进去。

**模块目录结构**（打成 zip 用 Magisk 安装）：

```
examplehook/
├── module.prop              ← 模块描述
├── zygisk/
│   └── arm64-v8a.so         ← Zygisk 入口 so (下面写的 zygisk.cpp 编译产物, 文件名必须=ABI名)
└── lib/
    └── arm64-v8a/
        └── libhook.so       ← 我们第五节编的 hook so
```

**module.prop：**

```
id=examplehook
name=示例手游 Hook 教学模块
version=v1.0
versionCode=1
author=me
description=lib2cpp hook 教学示例, 详见手游Hook详细教程.md
```

**zygisk.cpp：**

```cpp
#include "zygisk.h"          // 头文件从 Magisk 官方 zygisk-module-sample 拿
#include <jni.h>
#include <dlfcn.h>
#include <string.h>

using namespace zygisk;

class HookModule : public ModuleBase {
public:
    void onLoad(Api *api, JNIEnv *env) { this->api = api; this->env = env; }

    // App 进程 fork 出来、还没执行任何 App 代码之前
    void preAppSpecialize(AppSpecializeArgs *args) override {
        const char *proc = env->GetStringUTFChars(args->nice_name, nullptr);
        is_target = (strcmp(proc, "com.example.game") == 0);
        env->ReleaseStringUTFChars(args->nice_name, proc);
    }

    // App 代码即将开始跑, 此时 libil2cpp.so 已加载 —— 在这里注入
    void postAppSpecialize(const AppSpecializeArgs *) override {
        if (!is_target) return;
        void *h = dlopen("/data/adb/modules/examplehook/lib/arm64-v8a/libhook.so",
                         RTLD_NOW);
        // dlopen 成功 → constructor 自动跑 → install_hooks() 装好 hook
        // 失败时打日志排查:
        if (!h) __android_log_print(ANDROID_LOG_ERROR, "DumpHook",
                                    "dlopen 失败: %s", dlerror());
    }
private:
    Api *api = nullptr;
    JNIEnv *env = nullptr;
    bool is_target = false;
};

REGISTER_ZYGISK_MODULE(HookModule)
```

编译它同样用 NDK clang（把 zygisk.hpp 和它依赖的头文件放同目录）：

```bash
aarch64-linux-android21-clang -shared -O2 -fPIC -nostdlib++ \
  zygisk.cpp -I<zygisk头文件目录> -llog -o arm64-v8a.so
```

> 第一次做 Zygisk 模块，**强烈建议直接 git clone Magisk 官方的
> `zygisk-module-sample` 模板仓库**，把上面的逻辑填进去，目录结构、
> 打包脚本都是现成的，比从零搭省 90% 的坑。

**安装与启用：**

1. 把 `examplehook/` 目录压缩成 zip（注意是目录内容在 zip 根层，别多套一层）
2. Magisk App → 模块 → 从本地安装 → 选 zip → 重启
3. Magisk 首页确认 **Zygisk 开关已打开**
4. **隐藏**（可选但推荐）：Magisk → 配置 DenyList → 把 示例手游加进去

### 6.2 方案 B：Frida 快速通道（调试期首选）

不用编译 C 代码、不用做模块，10 分钟出结果验证思路。

**手机/模拟器（需 root）：**

```bash
# 下载对应架构的 frida-server (github.com/frida/frida/releases, 认准 android-arm64)
adb push frida-server-xx-android-arm64 /data/local/tmp/frida-server
adb shell "su -c 'chmod 755 /data/local/tmp/frida-server'"
adb shell "su -c '/data/local/tmp/frida-server &'"
```

**电脑写 hook.js**（JS 版，和 C 版逻辑完全对应）：

```js
const base = Module.findBaseAddress("libil2cpp.so");
console.log("[*] libil2cpp.so 基址: " + base);

const PAUSE_RVA = 0xDEADBEEF;
Interceptor.attach(base.add(PAUSE_RVA), {
    onEnter(args) {
        console.log("[*] ExampleMethod 被调用! this = " + args[0]);
        console.log("[*] 真实地址 = " + base.add(PAUSE_RVA));
        // 想跳过暂停逻辑: 取消下一行注释
        // return; 不行——Interceptor 里跳过用别的写法, 见 frida 文档 onEnter 里改 pc
    }
});
```

**跑起来：**

```bash
# PC 上装 frida: pip install frida-tools
frida -U -f com.example.game -l hook.js
# -U 连 USB/adb 设备, -f 冷启动目标包名, -l 加载脚本
```

验证完思路，把逻辑照搬到 C 版做 Zygisk 模块。
（也可以让 Frida 直接 `Module.load("/data/local/tmp/libhook.so")` 加载我们的 C 版 so，
两种方式混着用——C 版负责 hook，Frida 负责当注入器。）

### 6.3 方案 C：免 root 重打包 APK（商业手游基本走不通）

思路：把 libhook.so 塞进 APK，再在 smali 里加一句 `System.loadLibrary("hook")`，
让游戏自己加载我们的库。

步骤（MT 管理器手机上操作，或 PC apktool）：

1. 复制游戏 APK → apktool 反编译（或 MT 管理器直接打开）
2. `lib/arm64-v8a/` 里放入 libhook.so
3. 找到游戏入口（`AndroidManifest.xml` 里 application 标签 android:name 指向的类，
   没有就找启动 Activity），在其 `onCreate` 的 smali 里插入：

```smali
const-string v0, "hook"
invoke-static {v0}, Ljava/lang/System;->loadLibrary(Ljava/lang/String;)V
```

（注意该方法 `.locals` 至少为 1；smali 对齐细节看 MT 管理器报错提示）

4. 回编译 → **重新签名**（apksigner / MT 管理器签名）

**为什么商业手游走不通**：回编译后签名必然变化，商业手游登录接口/
TP 保护会校验签名，直接登录失败或闪退。此方案只对没有签名校验的
游戏（多数单机/小厂）有效。**也别尝试"签名校验绕过"，那是另一个深坑。**

### 6.4 方案 D：ptrace 注入器

root 下用现成开源注入器（搜 "android ptrace injector"，原理是
`process_attach` → 远程 `mmap` + 写入 so 路径 → 远程调用 `dlopen`），
游戏运行后把 libhook.so 注进目标 pid。一次性的调试手段，检测风险高，
不如 Zygisk 稳定，这里不展开。

### ⚠️ 关于带反作弊保护的游戏（TP / ACE 保护）

部分商业手游带 TP（ACE）等反作弊保护，会做注入器特征扫描、maps 扫描、
frida 检测。教学实验可能遇到"hook 装上了但游戏闪退/提示环境异常"。
**请把本教程用于安全研究与单机环境学习**（拿自己开发的游戏、开源
il2cpp 游戏或无保护单机游戏练手效果最好），对抗检测不在本教程范围，
也别拿去搞外挂——账号封禁是最轻的后果。

---

## 七、第 5 步：验证 hook 生效

### 7.1 看日志

```bash
adb logcat -s DumpHook
```

游戏里随便触发一次"暂停"（进设置、切后台等场景会调 `ExampleClass.ExampleMethod`），

预期输出：

```
[Dobby] 安装成功
[DumpHook] il2cpp 模块基址   = 0x79A1B2C3000
[DumpHook] ExampleMethod 真实地址     = 0x79A22118A9B4
[DumpHook] this  实例地址     = 0x79AC8B4C0A40
```

### 7.2 用 maps 交叉验证（严谨党的自检）

```bash
adb shell "su -c 'cat /proc/$(pidof com.example.game)/maps'" | grep libil2cpp.so | head -1
```

输出的起始地址应和 logcat 里打印的基址**完全一致**（同一次启动内比对；
游戏重启后两者一起变，属正常 ASLR）。

### 7.3 三层排查——日志分别说明什么问题

| 你看到的 | 结论 |
|---|---|
| 连 "安装成功" 都没有 | libhook.so 根本没被加载 → 查注入环节（6.1 dlopen 失败日志 / Zygisk 模块没生效 / 包名没匹配上） |
| 有"安装成功"，但触发功能时无输出 | hook 装上了但函数没被调用 → 可能这个方法不是你以为的入口，换 find_method 再找 |
| 有输出但游戏秒闪退 | RVA 大概率错了（游戏更新过/看串行了）→ 重新 dump，重新对 RVA |
| DobbyHook 返回非 0 | 目标地址不可写或不对齐 → 检查基址是不是拿的 offset==0 那条 |

---

## 八、进阶玩法

### 8.1 让"暂停"失效（跳过原函数）

hook 里**不调** orig 即可：

```c
static void my_ExampleMethod(void* instance, void* method)
{
    LOG("[DumpHook] 游戏想暂停? 我不放行\n");
    // orig_ExampleMethod(instance, method);   ← 注释掉 = 暂停逻辑被整个跳过
}
```

### 8.2 改参数（以坐标为例）

用 `find_method("set_value")` 找到 `ExampleClass.set_value(Vector3)` RVA `0xCAFEBABE`。
hook 签名把 Vector3 按三个 float 展开（64 位 ABI 下 12 字节的 Vector3 是 HFA，
在浮点寄存器里传）：

```c
static void (*orig_set_value)(void* instance, float x, float y, float z, void* method);

static void my_set_value(void* instance, float x, float y, float z, void* method)
{
    LOG("[DumpHook] set_value(%.1f, %.1f, %.1f)\n", x, y, z);
    // 传参阶段把 y 抬高 50:
    orig_set_value(instance, x, y + 50.0f, z, method);
}
```

> **ABI 提醒**：struct 参数的展开规则因大小/平台而异。经验法则：
> 先照 dump.cs 的签名直译；不行就先只打印 instance 验证 hook 通了，
> 再调参数细节。参数拿错最坏结果是读到垃圾值，一般不崩。

### 8.3 打印 this 的类名，确认 hook 对没对象

用 il2cpp 的导出 API（libil2cpp.so 自带，dlsym 直接拿）：

```c
static void*  (*il2cpp_object_get_class)(void*);   // 对象 → 类
static const char* (*il2cpp_class_get_name)(void*); // 类 → 名字

static void init_il2cpp_api(void)
{
    void *h = dlopen("libil2cpp.so", RTLD_NOW);
    il2cpp_object_get_class = dlsym(h, "il2cpp_object_get_class");
    il2cpp_class_get_name   = dlsym(h, "il2cpp_class_get_name");
}

// hook 里:
LOG("[DumpHook] this 的类 = %s\n",
    il2cpp_class_get_name(il2cpp_object_get_class(instance)));
// 期望打印 ExampleClass —— 不是的话说明 hook 到别家函数上了
```

> dlopen 自己已加载的库只增引用计数，不会二次加载，放心用。

### 8.4 主动调用游戏函数

最朴素的方式：保存 orig 指针，在别的 hook 里用它主动调用。
更完整的主动调用（构造对象/调任意方法）需要 il2cpp 导出 API 全家桶
（`il2cpp_class_from_name`、`il2cpp_runtime_invoke` 等）——
思路同 8.3 dlsym 拿函数，参数装进 `void* args[]` 传给
`il2cpp_runtime_invoke(method, obj, args, &exc)`。需要时再展开，先挖到这。

### 8.5 多 hook 管理

一个 so 里装多个 hook 没有任何问题：每个目标重复"orig 指针 + my 函数 +
DobbyHook"三件套即可。建议 hook RVA 全部集中成 `#define` 列表放文件头，
游戏一更新只改这一处（配合 dump工具 重新查）。

---

## 九、常见问题速查表

| 症状 | 大概率原因 | 解法 |
|---|---|---|
| libhook.so 推上去加载失败 | 编译成了 x86 / 没带 `-llog` | readelf 查 Machine；链接参数补全 |
| dlopen 返回 null | 路径错 / SELinux 拒绝 | 打印 dlerror()；so 放模块目录而非 /sdcard |
| DobbyHook 后游戏闪退 | RVA 过期（游戏更新了）| 重新 dump + 重新查 RVA |
| 基址和 maps 对不上 | 拿了 r-xp 段当地址 | 改用 offset==0 判定（见 4.1） |
| Zygisk 模块不生效 | Zygisk 总开关没开 / zip 多套了一层目录 / ABI 文件名不对 | Magisk 逐项检查；解压 zip 确认根层结构 |
| 触发功能但没日志 | 该方法非真正入口 | find_method 换思路找真正的调用链 |
| 一切正常但突然闪退 | 触发了保护检测 | 见 6.4 的合规提醒 |
| %llX 打印怪值 | 格式串和参数类型不匹配 | 统一 `(unsigned long long)` 强转 |

---

## 十、最后的提醒（合规与边界）

- 本教程与配套工具用于 **il2cpp/Unity 运行机制学习、自有游戏调试、单机游戏安全研究**
- 修改**联网竞技游戏**可能违反用户协议：轻则封号，重则触及法律红线（破坏计算机信息系统）
- 商业手游的 TP/ACE 是商业级反外挂系统，本教程不涉及也不应被用于对抗它
- 传播外挂、代做外挂在国内有明确入罪案例，别碰

教程完 —— 出问题优先看第七节的排查表，90% 的坑都在那张表里。
