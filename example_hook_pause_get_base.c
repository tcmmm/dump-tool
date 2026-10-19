// ============================================================================
//  例子：hook ExampleClass.ExampleMethod (示例方法) + 获取当前进程地址
//  基于 dump工具 生成的 hook 模板改写, 补全了 il2cpp_base 的实现
//
//  演示三件事:
//    1. 怎么拿到「当前进程地址」—— 即 il2cpp 主模块在进程内的加载基址
//         Windows: GameAssembly.dll    Android: libil2cpp.so
//    2. 用 基址 + RVA 算出 hook 点在当前进程里的真实地址
//         ExampleClass.ExampleMethod 的 RVA = 0xDEADBEEF (dump.cs 里给的 RVA
//         都是相对基址的偏移, 必须加上基址才能变成可用的运行时地址)
//    3. hook 触发时拿到当前对象实例 this 的地址
//
//  编译方法见文件末尾注释。
//
//  说明: 这里的类名 / 方法名 / RVA 均为占位示例, 替换成你自己 dump.cs
//        里查到的目标即可。
// ============================================================================

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>

#include "dobby.h"                       // https://github.com/jmpews/Dobby

// ---------------------------------------------------------------------------
// dump工具 弹窗里给出的信息
// ---------------------------------------------------------------------------
#define EXAMPLE_RVA 0xDEADBEEFULL        // ExampleClass.ExampleMethod (示例)

// ---------------------------------------------------------------------------
// 1) 简单日志: Windows 用 OutputDebugString (用 DebugView / VS 输出窗口看),
//    Android 用 logcat
// ---------------------------------------------------------------------------
#ifdef _WIN32
  #include <windows.h>

  static void LOG(const char* fmt, ...)
  {
      char buf[512];
      va_list ap;
      va_start(ap, fmt);
      vsnprintf(buf, sizeof(buf), fmt, ap);
      va_end(ap);
      OutputDebugStringA(buf);           // DebugView 勾选 Capture Win32 可见
      printf("%s", buf);                 // 若有控制台则同时打印
  }
#else
  #include <android/log.h>
  #define LOG(...)  __android_log_print(ANDROID_LOG_INFO, "DumpHook", __VA_ARGS__)
#endif

// ---------------------------------------------------------------------------
// 2) 核心: 获取当前进程内 il2cpp 模块的加载基址
//    模板代码里的 `il2cpp_base` 就是这个值 —— 现在补全它!
// ---------------------------------------------------------------------------
#ifdef _WIN32

static uintptr_t il2cpp_base(void)
{
    // il2cpp 编译后所有游戏代码都在 GameAssembly.dll 里。
    // GetModuleHandleW 的返回值 (HMODULE) 在数值上 = 该模块在本进程内的
    // 加载基址, 所以:  模块内任意函数的真实地址 = HMODULE + RVA
    HMODULE h = GetModuleHandleW(L"GameAssembly.dll");
    return (uintptr_t)h;                 // 拿不到会返回 0, install 时有检查
}

#else  // ---- Android / ----

static uintptr_t il2cpp_base(void)
{
    // 安卓读 /proc/self/maps 找基址: 每行格式是
    //   "起始地址-结束地址 权限 文件内偏移 设备 inode 路径"
    // 模块基址 = 该 so 文件内偏移为 0 的那条映射(ELF 头所在段)的起始地址。
    // 注意不能拿 r-xp 代码段的起始当地址 —— 那比真正的基址大一个
    // text 段的 vaddr 偏移, 直接加 RVA 会算出错误地址导致 hook 失败/闪退
    static uintptr_t cached = 0;
    if (cached) return cached;

    FILE* fp = fopen("/proc/self/maps", "r");
    if (!fp) return 0;

    char line[512];
    while (fgets(line, sizeof(line), fp)) {
        if (!strstr(line, "libil2cpp.so")) continue;
        uintptr_t start, end, file_off;
        char perms[8];
        if (sscanf(line, "%lx-%lx %7s %lx", &start, &end, &perms, &file_off) == 4
            && file_off == 0) {
            cached = start;
            break;
        }
    }
    fclose(fp);
    return cached;
}

#endif

// ---------------------------------------------------------------------------
// 3) 原函数指针 + hook 函数
//    il2cpp 的实例方法 C 签名固定为 (void* this, MethodInfo* method)
// ---------------------------------------------------------------------------
static void (*orig_ExampleMethod)(void* instance, void* method);

static void my_ExampleMethod(void* instance, void* method)
{
    uintptr_t base = il2cpp_base();

    // ======== 这里演示「得到当前进程地址」 ========
    LOG("[DumpHook] il2cpp 模块基址   = 0x%llX\n",
        (unsigned long long)base);
    LOG("[DumpHook] ExampleMethod 真实地址 = 0x%llX  (基址 + RVA 0xDEADBEEF)\n",
        (unsigned long long)(base + EXAMPLE_RVA));
    LOG("[DumpHook] this  实例地址     = 0x%llX\n",
        (unsigned long long)(uintptr_t)instance);
    LOG("[DumpHook] MethodInfo*        = 0x%llX\n",
        (unsigned long long)(uintptr_t)method);
    // ==================================================

    // TODO: 在这里写你的逻辑（改参数 / 改返回值 / 直接 return 跳过原方法）
    // 想直接跳过原逻辑, 就把下一行注释掉:
    orig_ExampleMethod(instance, method);
}

// ---------------------------------------------------------------------------
// 4) 安装 hook
// ---------------------------------------------------------------------------
static void install_hooks(void)
{
    uintptr_t base = il2cpp_base();
    if (!base) {
        LOG("[DumpHook] 拿不到基址(模块还没加载?), 放弃安装\n");
        return;
    }

    void* target = (void*)(base + EXAMPLE_RVA);   // hook 点的运行时真实地址
    LOG("[DumpHook] 准备 hook 地址: 0x%llX\n",
        (unsigned long long)(uintptr_t)target);

    // DobbyHook(目标地址, 替身函数, 保存原函数指针)
    if (DobbyHook(target, (void*)my_ExampleMethod, (void**)&orig_ExampleMethod) == 0)
        LOG("[DumpHook] 安装成功! 触发目标方法即可看到地址输出\n");
    else
        LOG("[DumpHook] 安装失败\n");
}

// ---------------------------------------------------------------------------
// 5) 入口: Windows DLL 被注入时自动执行 / Android .so 被加载时自动执行
// ---------------------------------------------------------------------------
#ifdef _WIN32
BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID reserved)
{
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(module);
        install_hooks();
        // 注: DllMain 里装 hook 有 loader lock 的理论风险, 演示够用;
        //     正式项目建议 DllMain 里只 CreateThread, 在新线程里调 install_hooks
    }
    return TRUE;
}
#else
__attribute__((constructor))
static void install_on_load(void) { install_hooks(); }
#endif

/* ---------------------------------------------------------------------------
编译方法:

【Windows / PC 版游戏】 产物 hook_example.dll, 用注入器注入到游戏进程
    cl /LD example_hook_pause_get_base.c dobby.lib /Fe:hook_example.dll
    (dobby.h / dobby.lib 取自 Dobby Release 的 windows 版)

【Android 手游】 产物 libhook.so, 通过 Zygisk 模块 / 注入器加载
    aarch64-linux-android21-clang -shared -O2 \
        example_hook_pause_get_base.c -o libhook.so -llog \
        path/to/libdobby.a

地址换算示例:
    假设本次运行 GameAssembly.dll 被加载到 0x7FF612340000
    则目标方法的真实地址 = 0x7FF612340000 + 0xDEADBEEF = 0x7FF61240DEEF
    (每次启动基址都会变 —— 这就是 ASLR, 所以 hook 代码必须动态拿基址)
--------------------------------------------------------------------------- */
