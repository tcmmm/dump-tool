# -*- coding: utf-8 -*-
"""
dump 翻译工作台 —— 桌面版
==========================
一款用于浏览 / 翻译 / 搜索 Il2CppDumper 生成的 dump.cs 的桌面应用。

功能:
  1. 加载任意 dump.cs 文件 (原生文件对话框 / 拖拽 / 命令行参数)
  2. 实时中文翻译 (复用 translate_dump.py 词典, 含 Unity 大白话)
  3. 树状浏览: 分组 → DLL → 用途分类 → 类
  4. 全局搜索 (类名 / 中文 / 分类名)
  5. 类详情: 字段 / 属性 / 方法 (含 RVA 地址)
  6. 一键生成 Dobby Hook C++ 代码
  7. 导出带中文注释的 dump.cs

技术: pywebview (原生窗口 + HTML5 前端) + Python 后端
"""

import os
import sys
import re
import io
import json
import time
import pickle
import hashlib
import zipfile
import threading

try:
    import adb_kit as AK
except Exception as _ak_err:  # 极端情况 (模块损坏) 降级: ADB 功能不可用但不影响主功能
    AK = None


def _resource_path(rel=''):
    """资源定位: 兼容源码运行与 PyInstaller 打包 (frozen) 两种场景"""
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    if rel:
        return os.path.join(base, rel)
    return base


# 复用 translate_dump.py 中的翻译词典与函数
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_THIS_DIR)
for _p in (_PARENT_DIR, _resource_path()):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import translate_dump as TD  # noqa: E402

from functools import lru_cache  # noqa: E402


# ============================================================
# 1.5 调试日志 (exe 旁的 _debug.log, 用于排查"点了没反应"类问题)
# ============================================================
def _exe_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return _THIS_DIR


def _dbg(msg):
    try:
        with open(os.path.join(_exe_dir(), '_debug.log'), 'a', encoding='utf-8') as f:
            f.write(f'{time.strftime("%m-%d %H:%M:%S")} {msg}\n')
    except Exception:
        pass


# ============================================================
# 1.6 Win32 原生文件对话框 (ctypes 直调 comdlg32)
# ============================================================
# 为什么不用 pywebview 的 create_file_dialog?
#   它内部走 WinForms OpenFileDialog, 从 js_bridge 的后台线程调用时
#   带跨线程 owner 参数, 在部分环境下抛异常被 pywebview 静默吞掉,
#   前端拿到 None —— 表现为"点按钮毫无反应"。
#   GetOpenFileNameW 是老牌 Win32 API, 不依赖 .NET/STA/owner, 任何线程可调。
import ctypes  # noqa: E402
from ctypes import wintypes  # noqa: E402


class _OFNW(ctypes.Structure):
    _fields_ = [
        ('lStructSize', wintypes.DWORD),
        ('hwndOwner', wintypes.HWND),
        ('hInstance', wintypes.HINSTANCE),
        ('lpstrFilter', wintypes.LPCWSTR),
        ('lpstrCustomFilter', wintypes.LPWSTR),
        ('nMaxCustFilter', wintypes.DWORD),
        ('nFilterIndex', wintypes.DWORD),
        ('lpstrFile', wintypes.LPWSTR),
        ('nMaxFile', wintypes.DWORD),
        ('lpstrFileTitle', wintypes.LPWSTR),
        ('nMaxFileTitle', wintypes.DWORD),
        ('lpstrInitialDir', wintypes.LPCWSTR),
        ('lpstrTitle', wintypes.LPCWSTR),
        ('Flags', wintypes.DWORD),
        ('nFileOffset', wintypes.WORD),
        ('nFileExtension', wintypes.WORD),
        ('lpstrDefExt', wintypes.LPCWSTR),
        ('lCustData', wintypes.LPARAM),
        ('lpfnHook', ctypes.c_void_p),
        ('lpTemplateName', wintypes.LPCWSTR),
        ('pvReserved', ctypes.c_void_p),
        ('dwReserved', wintypes.DWORD),
        ('FlagsEx', wintypes.DWORD),
    ]


_OFN_PATHMUSTEXIST = 0x00000800
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_HIDEREADONLY = 0x00000004
_OFN_NOCHANGEDIR = 0x00000008
_OFN_OVERWRITEPROMPT = 0x00000002
_OFN_EXPLORER = 0x00080000


def _win32_dialog(save_mode=False, title='', default_name='', file_filter=None):
    """弹出 Win32 文件选择/保存对话框。返回所选路径, 取消返回 None。
    file_filter: [(显示名, 通配符), ...] 例: [('adb 程序', 'adb.exe'), ('所有文件', '*.*')]"""
    try:
        if file_filter:
            # [(名, pat), ...] → '名\0pat\0名\0pat\0\0'
            parts = []
            for nm, pat in file_filter:
                parts.append(nm)
                parts.append(pat)
            filt = ctypes.create_unicode_buffer('\0'.join(parts) + '\0')
        elif save_mode:
            filt = ctypes.create_unicode_buffer('C# 源文件\0*.cs\0所有文件\0*.*\0')
        else:
            filt = ctypes.create_unicode_buffer('C# Dump 文件 (*.cs)\0*.cs\0所有文件\0*.*\0')
        buf = ctypes.create_unicode_buffer(default_name or '', 4096)
        title_buf = ctypes.create_unicode_buffer(title) if title else None
        ofn = _OFNW()
        ofn.lStructSize = ctypes.sizeof(_OFNW)
        ofn.hwndOwner = None
        ofn.lpstrFilter = ctypes.cast(filt, ctypes.c_wchar_p)
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buf, ctypes.c_wchar_p)
        ofn.nMaxFile = 4096
        ofn.lpstrTitle = ctypes.cast(title_buf, ctypes.c_wchar_p) if title_buf else None
        ofn.lpstrDefExt = 'cs'
        ofn.Flags = (_OFN_EXPLORER | _OFN_PATHMUSTEXIST | _OFN_NOCHANGEDIR |
                     (_OFN_OVERWRITEPROMPT if save_mode else _OFN_FILEMUSTEXIST | _OFN_HIDEREADONLY))
        fn = ctypes.windll.comdlg32.GetSaveFileNameW if save_mode else ctypes.windll.comdlg32.GetOpenFileNameW
        ok = fn(ctypes.byref(ofn))
        path = buf.value if ok else None
        _dbg(f'dialog(save={save_mode}) -> {path!r}')
        return path
    except Exception as e:
        _dbg(f'dialog EXCEPTION: {type(e).__name__}: {e}')
        return None


@lru_cache(maxsize=300000)
def _tr(identifier: str) -> str:
    """带缓存的标识符翻译 (避免重复计算)"""
    return TD.translate_identifier(identifier) or ''


def _method_cn(name: str) -> str:
    """方法名翻译 (含构造函数等特殊名)"""
    if name == '.ctor':
        return '构造函数'
    if name == '.cctor':
        return '静态构造函数'
    if name.startswith('get_') or name.startswith('set_'):
        return _tr(name[4:])
    if name.startswith('add_') or name.startswith('remove_'):
        return _tr(name)
    return _tr(name)


# ============================================================
# 1. 正则表达式 (dump.cs 结构解析)
# ============================================================
DLL_RE = re.compile(r'^// Dll :\s*(.+)$')
NS_RE = re.compile(r'^// Namespace:\s*(.*)$')
RVA_RE = re.compile(r'^\s*// RVA:\s*(0x[0-9a-fA-F]+)')

CLASS_RE = re.compile(
    r'^(?P<indent>\s*)(?P<attrs>(?:\[[^\]]*\]\s*)*)'
    r'(?P<mods>(?:public|internal|private|protected|static|abstract|sealed|partial|readonly|unsafe|new)\s+)*'
    r'(?P<kind>class|struct|interface|enum)\s+(?P<name>[A-Za-z_][\w`]*(?:<[^>]*>)?)'
    r'(?:\s*:\s*(?P<bases>[^\r\n{]+?))?\s*(?:\{|\r?$)'
)

METHOD_RE = re.compile(
    r'^(?P<indent>\s+)(?:\[[^\]]*\]\s*)*'
    r'(?P<mods>(?:(?:public|internal|private|protected|static|abstract|virtual|override|sealed|extern|unsafe|async|new|final|readonly|const)\s+)*)'
    r'(?P<ret>[A-Za-z_][\w<>\[\],\.\s\?\*&]*?)\s+'
    r'(?P<name>\.?[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)'
    r'(?P<tail>\s*(?:\{\s*\})?\s*(?://.*)?)$'
)

FIELD_RE = re.compile(
    r'^(?P<indent>\s+)(?P<mods>(?:public|internal|private|protected|static|readonly|const|volatile|unsafe|new)\s+)*'
    r'(?P<type>[A-Za-z_][\w<>\[\],\.\s\?\*]*?)\s+(?P<name>[A-Za-z_]\w*)\s*(?:=[^;]*)?;'
    r'(?P<tail>\s*(?://.*)?)$'
)

PROP_RE = re.compile(
    r'^(?P<indent>\s+)(?P<mods>(?:public|internal|private|protected|static|virtual|override|abstract|new)\s+)*'
    r'(?P<type>[A-Za-z_][\w<>\[\],\.\s\?\*]*?)\s+(?P<name>[A-Za-z_]\w*)\s*\{'
)

OFFSET_RE = re.compile(r'//\s*(0x[0-9a-fA-F]+)')

KIND_CN = {'class': '类', 'struct': '结构体', 'interface': '接口', 'enum': '枚举'}

# 游戏自有 DLL 白名单 (这些是重点逆向对象)
GAME_DLLS = {
    'BobPlugins.dll', 'BobTransformGizmo.dll', 'Assembly-CSharp.dll',
    'Debuger.dll', 'BobScript.dll', 'Ugc2Script.dll', 'BobMainScript.dll',
    'BobActivity2024.dll', 'HybridCLR.Runtime.dll',
}


# ============================================================
# 2. DLL 分组
# ============================================================
def classify_dll(name: str) -> str:
    """把 DLL 归入四大分组"""
    if name in GAME_DLLS:
        return '游戏自有代码'
    n = name.lower()
    if name == '__Generated':
        return '系统库'
    if n.startswith('unityengine') or n.startswith('unity.'):
        return 'Unity引擎库'
    if n.startswith('system') or n.startswith('mscorlib') or n.startswith('mono') \
       or n.startswith('netstandard') or n.startswith('microsoft'):
        return '.NET系统库'
    return '第三方库'


GROUP_ORDER = ['游戏自有代码', '第三方库', 'Unity引擎库', '.NET系统库']
GROUP_ICON = {'游戏自有代码': '🎮', '第三方库': '🧩', 'Unity引擎库': '🟦', '.NET系统库': '📚'}
GROUP_DESC = {
    '游戏自有代码': '游戏开发方自己写的代码——想改功能/找数据, 重点看这里',
    '第三方库': '游戏引用的第三方插件 (Spine/DOTween/UniTask 等)',
    'Unity引擎库': 'Unity 官方引擎模块 (GameObject/Transform 等基础能力)',
    '.NET系统库': '.NET 基础类库与 IL2CPP 运行时 (集合/字符串/IO 等)',
}

# 用途分类描述
CATEGORY_DESC = {cat: desc for cat, _, desc in TD.CATEGORY_RULES}


# ============================================================
# 3. IL2CPP 类型映射 (用于 Dobby Hook 代码生成)
# ============================================================
IL2CPP_TYPE_MAP = {
    'Void': 'void', 'Int32': 'int32_t', 'UInt32': 'uint32_t',
    'Int64': 'int64_t', 'UInt64': 'uint64_t',
    'Int16': 'int16_t', 'UInt16': 'uint16_t',
    'Byte': 'uint8_t', 'SByte': 'int8_t',
    'Single': 'float', 'Double': 'double',
    'Boolean': 'bool', 'Char': 'char',
    'String': 'Il2CppString*', 'Object': 'Il2CppObject*',
    'IntPtr': 'void*', 'UIntPtr': 'void*',
}


# C# 关键字小写形式 (Il2CppDumper 新版 dump.cs 方法签名用它)
CS_KEYWORD_TYPE_MAP = {
    'void': 'void', 'int': 'int32_t', 'uint': 'uint32_t',
    'long': 'int64_t', 'ulong': 'uint64_t', 'short': 'int16_t',
    'ushort': 'uint16_t', 'byte': 'uint8_t', 'sbyte': 'int8_t',
    'float': 'float', 'double': 'double', 'bool': 'bool',
    'char': 'char', 'string': 'Il2CppString*', 'object': 'Il2CppObject*',
}


def map_type(t: str) -> str:
    """IL2CPP 类型 → C++ 类型"""
    t = t.strip()
    if t in CS_KEYWORD_TYPE_MAP:
        return CS_KEYWORD_TYPE_MAP[t]
    if t in IL2CPP_TYPE_MAP:
        return IL2CPP_TYPE_MAP[t]
    # 枚举/自定义类型 → void* (引用类型); 简单类型尽量保留
    if t.endswith('[]'):
        return 'Il2CppArray*'
    if t.startswith(('List<', 'Dictionary<', 'HashSet<', 'Queue<', 'Stack<')):
        return 'void*'
    if t in ('Int32', 'Single', 'Boolean', 'Double', 'Int64', 'Byte', 'Int16'):
        return IL2CPP_TYPE_MAP[t]
    return 'void*'


def parse_params(params: str):
    """把方法参数串解析为 [(type, name), ...]"""
    if not params.strip():
        return []
    result = []
    depth = 0
    cur = ''
    for ch in params:
        if ch in '<([':
            depth += 1
            cur += ch
        elif ch in '>)]':
            depth -= 1
            cur += ch
        elif ch == ',' and depth == 0:
            result.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        result.append(cur.strip())
    out = []
    for p in result:
        p = p.strip()
        if not p:
            continue
        parts = p.rsplit(' ', 1)
        if len(parts) == 2:
            out.append((parts[0].strip(), parts[1].strip()))
        else:
            out.append((p, ''))
    return out


def gen_hook_code(class_name: str, method: dict, full: bool = False) -> str:
    """生成 Dobby Hook C++ 代码片段 (full=True 时生成双平台完整可编译示例)"""
    name = method['name']
    ret_cpp = map_type(method['ret'])
    is_static = method['static']
    params = parse_params(method.get('params', ''))
    safe = name.replace('.', '_').replace('`', '_').replace('<', '_').replace('>', '_')
    fn = f'{class_name}_{safe}'

    param_decls = []
    param_names = []
    for pt, pn in params:
        cpp = map_type(pt)
        pname = pn if pn else f'a{len(param_names)}'
        if not pn:
            pname = f'arg{len(param_names) + 1}'
        param_decls.append(f'{cpp} {pname}')
        param_names.append(pname)

    head = ', '.join(param_decls)
    call = ', '.join(param_names)

    if not is_static:
        if head:
            head = f'void* instance, {head}'
        else:
            head = 'void* instance'
        if call:
            call = f'instance, {call}'
        else:
            call = 'instance'

    head_full = f'{head}, MethodInfo* method' if head else 'MethodInfo* method'
    call_full = f'{call}, method' if call else 'method'
    rva = method['rva'] or ''
    cn = method['cn'] or ''

    if not full:
        lines = []
        lines.append(f'// ===== {class_name}.{name} ({cn})  RVA: {rva or "?"} =====')
        lines.append(f'static {ret_cpp} (*orig_{fn})({head_full});')
        lines.append(f'{ret_cpp} hook_{fn}({head_full}) {{')
        lines.append(f'    // TODO: 在这里写你的逻辑 (改参数 / 改返回值 / 直接 return 跳过)')
        lines.append(f'    {"return " if ret_cpp != "void" else ""}orig_{fn}({call_full});')
        lines.append('}')
        if rva:
            lines.append(f'// 安装:')
            lines.append(f'// DobbyHook((void*)((uintptr_t)il2cpp_base + {rva}), '
                        f'(void*)hook_{fn}, (void**)&orig_{fn});')
        return '\n'.join(lines)

    # ================= full 模式: 完整可编译示例 =================
    ret_kw = 'return ' if ret_cpp != 'void' else ''
    rva_def = rva if rva else '0x0'
    rva_warn = '' if rva else '  // ⚠ 该方法在 dump.cs 中无 RVA, 需自行确认地址!'
    L = []
    ap = L.append
    ap('// ============================================================================')
    ap(f'//  完整示例: hook {class_name}.{name} ({cn})')
    ap(f'//  RVA: {rva or "(无)"}   DLL: {method.get("dll", "?")}')
    ap('//  由 dump工具 一键生成 —— 精简模板在另一个标签页')
    ap('//')
    ap('//  演示完整链路:')
    ap('//    1. 运行时获取当前进程内 il2cpp 模块加载基址 (ASLR: 每次启动都变)')
    ap('//         Windows: GameAssembly.dll    Android: libil2cpp.so')
    ap('//    2. 真实地址 = 基址 + RVA, DobbyHook 安装 hook')
    ap(f'//    3. hook 触发时打印基址 / 真实地址 / this 实例地址')
    ap('//')
    ap('//  详细教程: dump工具/手游Hook详细教程.md')
    ap('// ============================================================================')
    ap('')
    ap('#include <stdint.h>')
    ap('#include <stdio.h>')
    ap('#include <stdlib.h>')
    ap('#include <stdarg.h>')
    ap('#include <string.h>')
    ap('')
    ap('#include "dobby.h"                    // https://github.com/jmpews/Dobby')
    ap('')
    ap('// ---------------------------------------------------------------------------')
    ap(f'// 目标方法{("  // " + cn) if cn else ""}')
    ap('// ---------------------------------------------------------------------------')
    ap(f'#define TARGET_RVA  {rva_def}ULL{rva_warn}')
    ap('')
    ap('// ---------------------------------------------------------------------------')
    ap('// 1) 日志: Windows 用 DebugView (勾选 Capture Win32) / VS 输出窗口;')
    ap('//    Android 用 adb logcat -s DumpHook')
    ap('// ---------------------------------------------------------------------------')
    ap('#ifdef _WIN32')
    ap('  #include <windows.h>')
    ap('')
    ap('  static void LOG(const char* fmt, ...)')
    ap('  {')
    ap('      char buf[512];')
    ap('      va_list ap;')
    ap('      va_start(ap, fmt);')
    ap('      vsnprintf(buf, sizeof(buf), fmt, ap);')
    ap('      va_end(ap);')
    ap('      OutputDebugStringA(buf);')
    ap('      printf("%s", buf);              // 若有控制台则同时打印')
    ap('  }')
    ap('#else')
    ap('  #include <android/log.h>')
    ap('  #include <dlfcn.h>')
    ap('  #define LOG(...)  __android_log_print(ANDROID_LOG_INFO, "DumpHook", __VA_ARGS__)')
    ap('#endif')
    ap('')
    ap('// ---------------------------------------------------------------------------')
    ap('// 2) 核心: 获取当前进程内 il2cpp 模块的加载基址')
    ap('//    真实地址 = 基址 + RVA (ASLR 使基址每次启动都变, 必须运行时动态取)')
    ap('// ---------------------------------------------------------------------------')
    ap('#ifdef _WIN32')
    ap('')
    ap('static uintptr_t il2cpp_base(void)')
    ap('{')
    ap('    // il2cpp 编译后所有游戏代码都在 GameAssembly.dll 里,')
    ap('    // HMODULE 的数值 = 该模块在本进程内的加载基址')
    ap('    HMODULE h = GetModuleHandleW(L"GameAssembly.dll");')
    ap('    return (uintptr_t)h;')
    ap('}')
    ap('')
    ap('#else  // ---- Android ----')
    ap('')
    ap('static uintptr_t il2cpp_base(void)')
    ap('{')
    ap('    // 读 /proc/self/maps, 每行格式:')
    ap('    //   "起始地址-结束地址 权限 文件内偏移 设备 inode 路径"')
    ap('    // 模块基址 = 文件内偏移为 0 的那条映射 (ELF 头所在段) 的起始地址。')
    ap('    // ⚠ 不能拿 r-xp 代码段当地址 —— 那比真基址大一个 text 段偏移,')
    ap('    //   直接加 RVA 会 hook 错位置导致闪退!')
    ap('    static uintptr_t cached = 0;')
    ap('    if (cached) return cached;')
    ap('')
    ap('    FILE* fp = fopen("/proc/self/maps", "r");')
    ap('    if (!fp) return 0;')
    ap('')
    ap('    char line[512];')
    ap('    while (fgets(line, sizeof(line), fp)) {')
    ap('        if (!strstr(line, "libil2cpp.so")) continue;')
    ap('        uintptr_t start, end, file_off;')
    ap('        char perms[8];')
    ap('        if (sscanf(line, "%lx-%lx %7s %lx", &start, &end, perms, &file_off) == 4')
    ap('            && file_off == 0) {')
    ap('            cached = start;')
    ap('            break;')
    ap('        }')
    ap('    }')
    ap('    fclose(fp);')
    ap('    return cached;')
    ap('}')
    ap('')
    ap('#endif')
    ap('')
    ap('// ---------------------------------------------------------------------------')
    ap(f'// 3) 原函数指针 + hook 函数  ({class_name}.{name}{("  // " + cn) if cn else ""})')
    ap('// ---------------------------------------------------------------------------')
    ap(f'static {ret_cpp} (*orig_{fn})({head_full});')
    ap('')
    ap(f'static {ret_cpp} hook_{fn}({head_full})')
    ap('{')
    ap('    uintptr_t base = il2cpp_base();')
    ap('')
    ap('    // ======== 演示: 得到当前进程地址 ========')
    ap('    LOG("[DumpHook] il2cpp 模块基址   = 0x%llX\\n", (unsigned long long)base);')
    ap('    LOG("[DumpHook] %s 真实地址 = 0x%llX  (base + RVA)\\n",')
    ap(f'        "{name}", (unsigned long long)(base + TARGET_RVA));')
    if not is_static:
        ap('    LOG("[DumpHook] this  实例地址     = 0x%llX\\n", (unsigned long long)(uintptr_t)instance);')
    ap('    // ==========================================')
    ap('')
    ap('    // TODO: 在这里写你的逻辑 (改参数 / 改返回值 / 直接 return 跳过原函数)')
    ap(f'    {ret_kw}orig_{fn}({call_full});')
    if ret_cpp != 'void':
        ap('    // 若想直接跳过原函数, 注释掉上面一行, 返回你想要的值:')
        ap('    // return ({ret_cpp})0;')
    ap('}')
    ap('')
    ap('// ---------------------------------------------------------------------------')
    ap('// 4) 安装 hook')
    ap('// ---------------------------------------------------------------------------')
    ap('static void install_hooks(void)')
    ap('{')
    ap('    uintptr_t base = il2cpp_base();')
    ap('    if (!base) {')
    ap('        LOG("[DumpHook] 拿不到基址(模块还没加载?), 放弃安装\\n");')
    ap('        return;')
    ap('    }')
    ap('')
    ap('    void* target = (void*)(base + TARGET_RVA);      // hook 点运行时真实地址')
    ap(f'    LOG("[DumpHook] 准备 hook 0x%llX  ({class_name}.{name})\\n",')
    ap('        (unsigned long long)(uintptr_t)target);')
    ap('')
    ap(f'    if (DobbyHook(target, (void*)hook_{fn}, (void**)&orig_{fn}) == 0)')
    ap('        LOG("[DumpHook] 安装成功! 触发该方法即可看到地址输出\\n");')
    ap('    else')
    ap('        LOG("[DumpHook] 安装失败\\n");')
    ap('}')
    ap('')
    ap('// ---------------------------------------------------------------------------')
    ap('// 5) 入口: Windows DLL 注入时执行 / Android .so 加载时执行')
    ap('// ---------------------------------------------------------------------------')
    ap('#ifdef _WIN32')
    ap('BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID reserved)')
    ap('{')
    ap('    if (reason == DLL_PROCESS_ATTACH) {')
    ap('        DisableThreadLibraryCalls(module);')
    ap('        install_hooks();')
    ap('        // 注: DllMain 里装 hook 有 loader lock 的理论风险, 演示够用;')
    ap('        //     正式项目建议 DllMain 里只 CreateThread, 在新线程里调 install_hooks')
    ap('    }')
    ap('    (void)reserved;')
    ap('    return TRUE;')
    ap('}')
    ap('#else')
    ap('__attribute__((constructor))          // .so 一被加载自动执行')
    ap('static void install_on_load(void) { install_hooks(); }')
    ap('#endif')
    ap('')
    ap('/* ---------------------------------------------------------------------------')
    ap('编译方法:')
    ap('')
    ap('【Windows / PC 版游戏】产物 hook.dll, 用注入器注入游戏进程')
    ap('    cl /LD this_file.c dobby.lib /Fe:hook.dll')
    ap('')
    ap('【Android 手游】产物 libhook.so, 通过 Zygisk 模块 / 注入器加载')
    ap('    aarch64-linux-android21-clang -shared -O2 -fPIC this_file.c \\')
    ap('        -I<dobby头文件目录> -L<dobby库目录> -ldobby -llog -o libhook.so')
    ap('')
    ap('验证:')
    ap('    adb logcat -s DumpHook        # 安卓看日志')
    ap('    DebugView (Capture Win32)     # Windows 看日志')
    ap('')
    ap('地址换算示例 (ASLR: 每次启动基址都会变):')
    try:
        rva_int = int(rva_def, 0)
    except ValueError:
        try:
            rva_int = int(rva_def, 16)
        except ValueError:
            rva_int = 0
    ap('    假设本次 libil2cpp.so 加载到 0x79a1b2c30000')
    ap(f'    则 {name} 真实地址 = 0x79a1b2c30000 + {rva_def} = 0x{0x79a1b2c30000 + rva_int:X}')
    ap('')
    ap('完整流程(Zygisk/Frida 注入/踩坑排查)见: 手游Hook详细教程.md')
    ap('--------------------------------------------------------------------------- */')
    return '\n'.join(L)


def gen_hook_byname_code(class_name: str, method: dict, ns: str = '') -> str:
    """生成「按名解析」版完整 Hook 工程: 运行时用 il2cpp 导出函数按名字查方法,
    不硬编码 RVA — 游戏更新后类名/方法名没变就不用改代码。"""
    name = method['name']
    ret_cpp = map_type(method['ret'])
    is_static = method['static']
    params = parse_params(method.get('params', ''))
    safe = name.replace('.', '_').replace('`', '_').replace('<', '_').replace('>', '_')
    fn = f'{class_name}_{safe}'
    ns_str = ns if ns and ns != '(全局)' else ''

    param_decls, param_names = [], []
    for pt, pn in params:
        cpp = map_type(pt)
        pname = pn if pn else f'arg{len(param_names) + 1}'
        param_decls.append(f'{cpp} {pname}')
        param_names.append(pname)
    head = ', '.join(param_decls)
    call = ', '.join(param_names)
    if not is_static:
        head = f'void* instance, {head}' if head else 'void* instance'
        call = f'instance, {call}' if call else 'instance'
    head_full = f'{head}, MethodInfo* method' if head else 'MethodInfo* method'
    call_full = f'{call}, method' if call else 'method'
    cn = method.get('cn') or ''
    argc = len(params)

    L = []
    ap = L.append
    ap('// =========================================================================')
    ap('// 🔄 按名解析 Hook — dump工具 生成')
    ap(f'// 目标: {ns_str + "." if ns_str else ""}{class_name}.{name}'
       + (f'  ({cn})' if cn else ''))
    ap('//')
    ap('// 与 RVA 版的区别: 不硬编码地址, 运行时通过 il2cpp 导出函数')
    ap('//   il2cpp_domain_get → assemblies → class_from_name → method_from_name')
    ap('//   按名字找到 MethodInfo, 取 methodPointer 再 DobbyHook。')
    ap('//   ✅ 游戏更新后只要 类名/方法名/参数个数 没变, 本文件一字不用改。')
    ap('//   ⚠ 同名同参数个数的重载无法区分; 泛型/嵌套类可能查不到。')
    ap('//   ⚠ 注入要等 il2cpp 初始化完 (内置重试等待, 构造函数直接跑没问题)。')
    ap('// =========================================================================')
    ap('#include <cstdio>')
    ap('#include <cstdint>')
    ap('#include <cstring>')
    ap('#if defined(_WIN32)')
    ap('#include <windows.h>')
    ap('#define usleep(us) Sleep((us) / 1000)')
    ap('#define LOGI(...) do { printf("[hook] " __VA_ARGS__); printf("\\n"); } while(0)')
    ap('#else')
    ap('#include <dlfcn.h>')
    ap('#include <unistd.h>')
    ap('#include <pthread.h>')
    ap('#include <android/log.h>')
    ap('#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, "hook", __VA_ARGS__)')
    ap('#endif')
    ap('')
    ap('// il2cpp 导出函数 (所有 il2cpp 游戏都导出; 结构体只取关心的第一个字段)')
    ap('struct MethodInfo { void* methodPointer; };')
    ap('')
    ap('static void* (*il2cpp_domain_get)();')
    ap('static void** (*il2cpp_domain_get_assemblies)(void* domain, size_t* size);')
    ap('static void* (*il2cpp_assembly_get_image)(void* assembly);')
    ap('static void* (*il2cpp_class_from_name)(void* image, const char* namespaze, const char* name);')
    ap('static void* (*il2cpp_class_get_method_from_name)(void* klass, const char* name, int argc);')
    ap('')
    ap('// ---- 平台适配: 拿模块句柄 ----')
    ap('static void* get_module_() {')
    ap('#if defined(_WIN32)')
    ap('    return (void*)GetModuleHandleW(L"GameAssembly.dll");')
    ap('#else')
    ap('    return dlopen("libil2cpp.so", RTLD_NOLOAD | RTLD_NOW) ?: dlopen("libil2cpp.so", RTLD_NOW);')
    ap('#endif')
    ap('}')
    ap('')
    ap('static void* resolve_(void* h, const char* sym) {')
    ap('#if defined(_WIN32)')
    ap('    return (void*)GetProcAddress((HMODULE)h, sym);')
    ap('#else')
    ap('    return dlsym(h, sym);')
    ap('#endif')
    ap('}')
    ap('')
    ap('static bool load_il2cpp_api_() {')
    ap('    void* h = get_module_();')
    ap('    if (!h) return false;')
    ap('    il2cpp_domain_get             = (void* (*)())resolve_(h, "il2cpp_domain_get");')
    ap('    il2cpp_domain_get_assemblies  = (void** (*)(void*, size_t*))resolve_(h, "il2cpp_domain_get_assemblies");')
    ap('    il2cpp_assembly_get_image     = (void* (*)(void*))resolve_(h, "il2cpp_assembly_get_image");')
    ap('    il2cpp_class_from_name        = (void* (*)(void*, const char*, const char*))resolve_(h, "il2cpp_class_from_name");')
    ap('    il2cpp_class_get_method_from_name = (void* (*)(void*, const char*, int))resolve_(h, "il2cpp_class_get_method_from_name");')
    ap('    return il2cpp_domain_get && il2cpp_domain_get_assemblies && il2cpp_assembly_get_image')
    ap('        && il2cpp_class_from_name && il2cpp_class_get_method_from_name;')
    ap('}')
    ap('')
    ap('// 跨全部程序集按 [命名空间.类.方法(参数个数)] 查找')
    ap(f'static void* find_method_(const char* namespaze, const char* cls, const char* m, int argc) {{')
    ap('    void* domain = il2cpp_domain_get();')
    ap('    if (!domain) return nullptr;')
    ap('    size_t n = 0;')
    ap('    void** assemblies = il2cpp_domain_get_assemblies(domain, &n);')
    ap('    if (!assemblies) return nullptr;')
    ap('    for (size_t i = 0; i < n; i++) {')
    ap('        void* image = il2cpp_assembly_get_image(assemblies[i]);')
    ap('        void* klass = il2cpp_class_from_name(image, namespaze, cls);')
    ap('        if (!klass) continue;')
    ap('        MethodInfo* mi = (MethodInfo*)il2cpp_class_get_method_from_name(klass, m, argc);')
    ap('        if (mi && mi->methodPointer) {')
    ap('            LOGI("[+] 找到 %s.%s (argc=%d) -> %p", cls, m, argc, mi->methodPointer);')
    ap('            return mi->methodPointer;')
    ap('        }')
    ap('    }')
    ap('    return nullptr;')
    ap('}')
    ap('')
    ap('// ---- Hook 目标 (和 RVA 版一致) ----')
    ap(f'static {ret_cpp} (*orig_{fn})({head_full});')
    ap(f'{ret_cpp} hook_{fn}({head_full}) {{')
    ap('    // TODO: 你的逻辑 (改参数 / 改返回值 / 直接 return 跳过)')
    ap(f'    {"return " if ret_cpp != "void" else ""}orig_{fn}({call_full});')
    ap('}')
    ap('')
    ap('// ---- 安装 (带等待: 构造函数时机 il2cpp 还没起来也没关系) ----')
    ap('static void install_() {')
    ap('    // 最多等 15 秒让 il2cpp 就绪 (模块加载 + domain 初始化)')
    ap('    for (int i = 0; i < 30; i++) {')
    ap('        if (load_il2cpp_api_() && il2cpp_domain_get()) break;')
    ap('        usleep(500 * 1000);')
    ap('    }')
    ap('    if (!il2cpp_domain_get()) { LOGI("[!] 15 秒内 il2cpp 未就绪"); return; }')
    ap(f'    void* target = find_method_("{ns_str}", "{class_name}", "{name}", {argc});')
    ap('    if (!target) {')
    ap(f'        LOGI("[!] 未找到 {ns_str}.{class_name}.{name} (改了名/参数个数?)");')
    ap('        return;')
    ap('    }')
    ap(f'    DobbyHook(target, (void*)hook_{fn}, (void**)&orig_{fn});')
    ap(f'    LOGI("[+] 按名解析 hook 安装成功: {class_name}.{name}");')
    ap('}')
    ap('')
    ap('#if defined(_WIN32)')
    ap('static DWORD WINAPI entry_(LPVOID) { install_(); return 0; }')
    ap('BOOL APIENTRY DllMain(HMODULE, DWORD reason, LPVOID) {')
    ap('    if (reason == DLL_PROCESS_ATTACH) {')
    ap('        DisableThreadLibraryCalls(GetModuleHandleW(nullptr));')
    ap('        CreateThread(nullptr, 0, entry_, nullptr, 0, nullptr);')
    ap('    }')
    ap('    return TRUE;')
    ap('}')
    ap('#else')
    ap('static void* entry_(void*) { install_(); return nullptr; }')
    ap('__attribute__((constructor)) static void on_load_() {')
    ap('    pthread_t t; pthread_create(&t, nullptr, entry_, nullptr);')
    ap('    pthread_detach(t);')
    ap('}')
    ap('#endif')
    return '\n'.join(L)


def gen_frida_code(class_name: str, method: dict, package: str = '') -> str:
    """生成 Frida JS 脚本 (Interceptor.attach 版, 调试期免编译)"""
    name = method['name']
    rva = method['rva'] or ''
    cn = method['cn'] or ''
    is_static = method['static']
    ret = method['ret'] or 'void'
    params = parse_params(method.get('params', ''))
    safe = name.replace('.', '_').replace('`', '_').replace('<', '_').replace('>', '_')
    tgt = f'{class_name}.{name}'
    spawn_hint = f'frida -U -f {package} -l this_file.js' if package else 'frida -U -f 目标包名 -l this_file.js'

    L = []
    ap = L.append
    ap('// =====================================================================')
    ap(f'// Frida 脚本: hook {tgt}{("  // " + cn) if cn else ""}')
    ap(f'// RVA: {rva or "(无)"}   由 dump工具 一键生成')
    ap('//')
    ap('// 用法 (二选一):')
    ap(f'//   冷启动(spawn, 推荐): {spawn_hint}')
    ap(f'//   附着已运行进程:      frida -U -n 进程名 -l this_file.js')
    ap('// 安卓模块是 libil2cpp.so, PC 版改成 GameAssembly.dll')
    ap('// =====================================================================')
    ap('')
    ap('const MODULE = "libil2cpp.so";')
    ap(f'const TARGET_RVA = 0x{rva[2:] if rva.startswith("0x") else (rva or "0")};'
       if rva else 'const TARGET_RVA = 0x0;  // ⚠ dump.cs 中该方法无 RVA, 需自行确认!')
    ap('')
    ap('function getBase(m) {')
    ap('    // frida 17 移除了 Module.findBaseAddress, 新旧版本兼容写法')
    ap('    if (typeof Module.findBaseAddress === "function") return Module.findBaseAddress(m);')
    ap('    var mod = Process.findModuleByName(m);')
    ap('    return mod ? mod.base : null;')
    ap('}')
    ap('')
    ap('function main() {')
    ap('    const base = getBase(MODULE);')
    ap('    if (!base) {')
    ap('        console.log(`[!] ${MODULE} 还没加载, 1秒后重试...`);')
    ap('        setTimeout(main, 1000);')
    ap('        return;')
    ap('    }')
    ap('    console.log(`[*] ${MODULE} 基址: ${base}`);')
    ap('    const target = base.add(TARGET_RVA);')
    ap('    console.log(`[*] ' + tgt + ' 真实地址: ${target}`);')
    ap('')
    ap('    Interceptor.attach(target, {')
    ap('        onEnter(args) {')
    if not is_static:
        ap('            // 实例方法: args[0] = this, 之后才是参数')
        ap('            console.log(`[*] ' + tgt + ' 被调用! this = ${args[0]}`);')
    else:
        ap('            // 静态方法: args[0] 起就是参数')
        ap('            console.log(`[*] ' + tgt + ' 被调用!`);')
    for i, (pt, pn) in enumerate(params):
        argi = i if is_static else i + 1
        ap(f'            console.log(`    args[{argi}] = ${{args[{argi}]}}`);  // {pn or ("arg" + str(argi))} ({pt})')
    ap('            // 改参数示例: args[1] = ptr(0x1234);')
    ap('            // 想直接跳过原函数: 把下一行取消注释 (阻止原方法执行)')
    ap('            // this.context.pc = this.returnAddress;')
    if ret not in ('void', 'Void'):
        ap('        },')   # ⚠ 有 onLeave 时 onEnter 闭包后必须逗号 — QuickJS 严格, 缺了直接 parse error
        ap('        onLeave(retval) {')
        ap('            console.log(`    返回值 = ${retval}`);')
        ap('            // 想改返回值: retval.replace(100);')
        ap('        }')
    else:
        ap('        }')
        ap('        // 返回 void, 无需 onLeave')
    ap('    });')
    ap('    console.log("[+] hook 安装成功, 等待游戏触发...");')
    ap('}')
    ap('')
    ap('main();')
    return '\n'.join(L)


# ---------------- ⚡ Frida 批量追踪 (整类/收藏方法 全量 Interceptor) ----------------

# C# 参数类型 → 追踪解码类型 ('' = 浮点类, 走 v 寄存器不占 x)
TRACE_ARG_KIND = {
    'int': 'i32', 'Int32': 'i32', 'uint': 'i32', 'UInt32': 'i32',
    'short': 'i32', 'Int16': 'i32', 'ushort': 'i32', 'UInt16': 'i32',
    'byte': 'i32', 'Byte': 'i32', 'sbyte': 'i32', 'SByte': 'i32', 'char': 'i32', 'Char': 'i32',
    'long': 'i64', 'Int64': 'i64', 'ulong': 'i64', 'UInt64': 'i64',
    'IntPtr': 'i64', 'UIntPtr': 'i64', 'nint': 'i64', 'nuint': 'i64',
    'bool': 'b1', 'Boolean': 'b1',
    'float': '', 'Single': '', 'double': '', 'Double': '',  # v 寄存器, 不占 x
    'String': 'str', 'string': 'str',
}


def gen_frida_trace_code(items, package='', enum_names=None):
    """生成批量追踪脚本: items = [(dll, class_name, method_tuple), ...]
    method_tuple = (name, rva, ret, is_static, params)。
    按 ARM64 ABI 预计算每个参数所在的 x 寄存器序号, 运行时解码打印。"""
    enum_names = enum_names or set()
    targets = []
    for dll, cls_name, m in items:
        name, rva, ret, is_static, params_str = m[0], m[1], m[2], m[3], m[4]
        if not rva:
            continue
        params = parse_params(params_str)
        spec = []      # [[xIndex, kind, pname], ...]
        xi = 0 if is_static else 1  # 实例方法 x0 = this
        for pt, pn in params:
            core = (pt or '').strip()
            core = re.sub(r'\[.*$', '', core).split('<')[0].strip().rsplit('.', 1)[-1]
            kind = TRACE_ARG_KIND.get(core, '')
            if kind == '' and core not in ('float', 'Single', 'double', 'Double'):
                if core in enum_names:
                    kind = 'i32'          # 枚举按 i32 解码
                elif pn == '' or core == '':
                    kind = 'p'
                else:
                    kind = 'p'            # 其余一律按指针/对象打印
            if kind == '':
                spec.append([-1, 'v', pn or core])  # v 寄存器: 不占 x
            else:
                spec.append([xi, kind, pn or (core + str(xi))])
                xi += 1
        ret_kind = 'void'
        if ret and ret.strip() not in ('void', 'Void'):
            rc = ret.strip().rsplit('.', 1)[-1]
            ret_kind = TRACE_ARG_KIND.get(rc, 'i32' if rc in enum_names else
                                          ('str' if rc in ('String', 'string') else 'p'))
        targets.append((dll, cls_name, name, rva, spec, ret_kind, is_static))

    pkg_hint = package or '目标包名'
    L = []
    ap = L.append
    ap('// ===========================================================================')
    ap('// Frida 批量追踪脚本 — dump工具 生成')
    ap(f'// 目标: {len(targets)} 个方法  (游戏里做操作, 控制台实时打印谁被调/参数/返回值)')
    ap('//')
    ap(f'// 用法:  frida -U -f {pkg_hint} -l trace.js     (冷启动, 推荐)')
    ap('//        frida -U -n 进程名 -l trace.js          (附着已运行进程)')
    ap('// 模块: 安卓 libil2cpp.so / PC 改 GameAssembly.dll')
    ap('//')
    ap('// 防刷屏: 每个函数默认最多打印 TRACE_LIMIT 次, 改 0 = 不限。')
    ap('// 追踪面太大游戏会卡: 不想要的函数直接删下方对应 attach 块。')
    ap('// 参数解码按 ARM64 AAPCS: x0=this(实例方法), 整数/指针依次占 x1..x7,')
    ap('// float/double 在 v 寄存器 (标 ≈vreg 不解码), String 自动读 il2cpp 字符串。')
    ap('// ===========================================================================')
    ap('')
    ap("const MODULE = 'libil2cpp.so';")
    ap('const TRACE_LIMIT = 50;   // 每个函数最多打印次数 (0=不限)')
    ap('const counts = {};')
    ap('')
    ap('// 读 il2cpp 字符串: [0x10]=长度, [0x14]=UTF-16 字符')
    ap('function readStr(p) {')
    ap('    if (p.isNull()) return "(null)";')
    ap('    try {')
    ap('        const len = p.add(0x10).readUInt();')
    ap('        if (len > 0 && len <= 2048) return p.add(0x14).readUtf16String(len);')
    ap('    } catch (e) {}')
    ap('    return p.toString();   // 不是合法字符串就打印指针')
    ap('}')
    ap('')
    ap('function decode(kind, p) {')
    ap('    switch (kind) {')
    ap("        case 'i32': return p.toInt32();")
    ap("        case 'i64': return p.toString();")
    ap("        case 'b1':  return (p.toInt32() !== 0);")
    ap("        case 'str': return readStr(p);")
    ap("        default:    return p.toString();   // 指针/对象")
    ap('    }')
    ap('}')
    ap('')
    ap('function logCall(tag, args, spec) {')
    ap('    const n = (counts[tag] = (counts[tag] || 0) + 1);')
    ap('    if (TRACE_LIMIT && n > TRACE_LIMIT) return;')
    ap('    const parts = [];')
    ap('    for (const s of spec) {')
    ap('        if (s[0] < 0) { parts.push(s[2] + "≈vreg"); continue; }   // float 参数')
    ap('        try { parts.push(s[2] + "=" + decode(s[1], args[s[0]])); } catch (e) {}')
    ap('    }')
    ap('    console.log(`[${tag}] #${n}` + (parts.length ? " " + parts.join(", ") : ""));')
    ap('}')
    ap('')
    ap('function logRet(tag, rv, kind) {')
    ap('    if (TRACE_LIMIT && (counts[tag] || 0) > TRACE_LIMIT) return;')
    ap("    if (kind === 'void') return;")
    ap('    try { console.log(`   ↳ ${tag} 返回 = ${decode(kind, rv)}`); } catch (e) {}')
    ap('}')
    ap('')
    ap('function getBase(m) {')
    ap('    // frida 17 移除了 Module.findBaseAddress, 新旧版本兼容写法')
    ap('    if (typeof Module.findBaseAddress === "function") return Module.findBaseAddress(m);')
    ap('    var mod = Process.findModuleByName(m);')
    ap('    return mod ? mod.base : null;')
    ap('}')
    ap('')
    ap('function main() {')
    ap('    const base = getBase(MODULE);')
    ap('    if (!base) {')
    ap('        console.log(`[!] ${MODULE} 还没加载, 1秒后重试...`);')
    ap('        setTimeout(main, 1000);')
    ap('        return;')
    ap('    }')
    ap('    console.log(`[*] ${MODULE} 基址: ${base} — 开始安装 ${TARGETS.length} 个追踪点`);')
    ap('    let okN = 0;')
    ap('    for (const t of TARGETS) {')
    ap('        try {')
    ap('            Interceptor.attach(base.add(t.rva), {')
    ap('                onEnter(a) { logCall(t.tag, a, t.spec); },')
    ap('                onLeave(r) { logRet(t.tag, r, t.ret); },')
    ap('            });')
    ap('            okN++;')
    ap("        } catch (e) { console.log('[!] attach 失败 ' + t.tag + ': ' + e); }")
    ap('    }')
    ap('    console.log(`[+] 追踪安装完成 ${okN}/${TARGETS.length}, 在游戏里做操作看日志吧`);')
    ap('}')
    ap('')
    ap('// ---- 追踪目标表 (rva/tag/参数解码规格 由 dump工具 预生成) ----')
    ap('const TARGETS = [')
    if targets:
        for dll, cls_name, name, rva, spec, ret_kind, is_static in targets:
            tag = f'{cls_name}.{name}'
            rv = rva if rva.lower().startswith('0x') else '0x' + rva
            spec_js = json.dumps(spec, ensure_ascii=False, separators=(',', ':'))
            st = 'static ' if is_static else ''
            ap(f"    {{ rva: {rv}, tag: {json.dumps(tag, ensure_ascii=False)}, "
               f"spec: {spec_js}, ret: '{ret_kind}' }},  // {st}({dll})")
    else:
        ap('    // (没有带 RVA 的方法)')
    ap('];')
    ap('')
    ap('main();')
    return '\n'.join(L)


def gen_ida_script_code(items: list, title: str = '') -> str:
    """生成 IDA Pro Python 命名脚本: 按 dump.cs 把函数重命名 + 加中文注释
    items: [(dll, class_name, method_name, rva, cn), ...]  rva 为 '' 时跳过"""
    L = []
    ap = L.append
    ap('# -*- coding: utf-8 -*-')
    ap('# =========================================================================')
    ap('#  IDA Pro 命名脚本 (由 dump工具 一键生成)')
    ap(f'#  {title or "Dump 函数重命名 + 中文注释"}')
    ap('#')
    ap('#  用法: 在 IDA 里打开 libil2cpp.so (或 GameAssembly.dll),')
    ap('#        File → Script file... → 选本文件, 自动重命名并加中文注释。')
    ap('#  提示: 游戏更新后重新生成 (RVA 会全变)。')
    ap('# =========================================================================')
    ap('import idaapi, idc')
    ap('')
    ap('BASE = idaapi.get_imagebase()')
    ap('print("[dump工具] imagebase = %s" % hex(BASE))')
    ap('')
    ap('RENAMES = [')
    used = {}
    rows = []
    for dll, cls, m, rva, cn in items:
        if not rva:
            continue
        base_name = f'{cls}__{m}'
        base_name = base_name.replace('.', '_').replace('`', '_').replace('<', '_').replace('>', '_')
        n = used.get(base_name, 0)
        fname = base_name if n == 0 else f'{base_name}_{n + 1}'
        used[base_name] = n + 1
        cmt = f'{cls}.{m}' + (f' // {cn}' if cn else '')
        rows.append(f'    ({rva}, "{fname}", "{cmt}"),')
    if not rows:
        rows.append('    # (本批次没有可重命名的方法 —— 需要 dump.cs 里有 RVA)')
    L.extend(rows)
    ap(']')
    ap('')
    ap('done = 0')
    ap('for rva, name, cmt in RENAMES:')
    ap('    ea = BASE + rva')
    ap('    idc.set_name(ea, name, idc.SN_NOWARN | idc.SN_NOCHECK)')
    ap('    idc.set_cmt(ea, cmt, 0)')
    ap('    done += 1')
    ap('')
    ap(f'print("[dump工具] 完成: 重命名/注释 {len(rows)} 个函数")')
    return '\n'.join(L)


# ---------------- 🚀 Hook 工程脚手架 (NDK/CMake 完整工程 zip) ----------------

def _c_ident(s):
    """C 标识符安全化"""
    s = re.sub(r'[^0-9A-Za-z_]', '_', s or '')
    if not s or s[0].isdigit():
        s = '_' + s
    return s


def build_hook_project_zip(items, meta, sample=None):
    """生成完整 Android NDK Hook 工程 (zip bytes)。
    items: [(dll, class, method, rva, cn), ...]  (rva='' 跳过)
    meta: {'game':..., 'scope':..., 'count':N}
    sample: 示例 hook 用的方法 dict {'class','name','rva','ret','static','params','cn'}
    """
    sample = sample or {}
    safe_items = [it for it in items if it[3]]
    proj_name = 'hook_' + _c_ident(meta.get('scope') or 'targets')[:40]

    # ---- offsets.h ----
    L = []
    ap = L.append
    ap('// =========================================================================')
    ap('// 目标偏移表 — dump工具 自动生成')
    ap(f"// 游戏: {meta.get('game', '?')}   范围: {meta.get('scope', '?')}   方法数: {len(safe_items)}")
    ap('// 游戏更新后: 用工具「版本对比」加载新 dump → 「Hook 迁移」粘本文件重写 RVA,')
    ap('// 或右键用「🔄 按名解析」模板 (按名字找函数, 更新免改码)。')
    ap('// =========================================================================')
    ap('#pragma once')
    used = {}
    macro_rows = []
    for dll, cls, m, rva, cn in safe_items:
        base_name = f'RVA_{_c_ident(cls)}_{_c_ident(m)}'
        n = used.get(base_name, 0)
        macro = base_name if n == 0 else f'{base_name}_{n + 1}'
        used[base_name] = n + 1
        rv = rva if rva.lower().startswith('0x') else '0x' + rva
        cmt = f'  // {cls}.{m}'
        if cn:
            cmt += f' ({cn})'
        cmt += f' [{dll}]'
        macro_rows.append(f'#define {macro} {rv}{cmt}')
    if macro_rows:
        L.extend(macro_rows)
    else:
        ap('// (本范围没有带 RVA 的方法)')
    offsets_h = '\n'.join(L)

    # ---- il2cpp_api.h ----
    il2cpp_api_h = '''// il2cpp 运行时导出函数声明 (按名解析用, dump工具 生成)
#pragma once
#include <cstddef>

struct MethodInfo { void* methodPointer; };  // 只取关心的第一个字段

// 以下函数指针在 hook_main.cpp 的 load_il2cpp_api_() 里 dlsym/GetProcAddress 填充
extern void* (*il2cpp_domain_get)();
extern void** (*il2cpp_domain_get_assemblies)(void* domain, size_t* size);
extern void* (*il2cpp_assembly_get_image)(void* assembly);
extern void* (*il2cpp_class_from_name)(void* image, const char* namespaze, const char* name);
extern void* (*il2cpp_class_get_method_from_name)(void* klass, const char* name, int argc);

// 跨全部程序集按 [命名空间.类名.方法名(参数个数)] 查找, 返回函数地址
void* il2cpp_find_method(const char* namespaze, const char* cls, const char* m, int argc);
'''

    # ---- dobby.h ----
    dobby_h = '''// Dobby Inline Hook (https://github.com/jmpews/Dobby)
// 首次使用: 见 third_party/README.md 克隆到 third_party/Dobby
#pragma once
extern "C" int DobbyHook(void* address, void* replacement, void** origin);
'''

    # ---- hook_main.cpp ----
    s_cls = sample.get('class', '')
    s_m = sample.get('name', '')
    s_ret = map_type(sample.get('ret', 'void'))
    s_static = bool(sample.get('static'))
    s_params = parse_params(sample.get('params', '') or '')
    s_cn = sample.get('cn', '')
    s_rva = (sample.get('rva') or '')
    s_rva = s_rva if s_rva.lower().startswith('0x') else '0x' + s_rva
    s_macro = f'RVA_{_c_ident(s_cls)}_{_c_ident(s_m)}' if s_cls and s_m else 'RVA_SAMPLE'
    decls, names = [], []
    for pt, pn in s_params:
        pname = pn or f'arg{len(names) + 1}'
        decls.append(f'{map_type(pt)} {pname}')
        names.append(pname)
    head = ', '.join(decls)
    call = ', '.join(names)
    if not s_static:
        head = f'void* instance, {head}' if head else 'void* instance'
        call = f'instance, {call}' if call else 'instance'
    head_full = f'{head}, MethodInfo* method' if head else 'MethodInfo* method'
    call_full = f'{call}, method' if call else 'method'
    s_fn = _c_ident(f'{s_cls}_{s_m}') or 'Sample'
    ret_stmt = 'return ' if s_ret != 'void' else ''

    L = []
    ap = L.append
    ap('// =========================================================================')
    ap(f'// Hook 入口 — dump工具 生成  目标: {meta.get("game", "?")} / {meta.get("scope", "?")}')
    ap(f'// 示例 hook: {s_cls}.{s_m}' + (f' ({s_cn})' if s_cn else ''))
    ap('// 其余目标偏移见 offsets.h — 仿照示例补 hook 函数 + install 行即可。')
    ap('// =========================================================================')
    ap('#include "dobby.h"')
    ap('#include "offsets.h"')
    ap('#include "il2cpp_api.h"')
    ap('#include <cstdio>')
    ap('#include <cstdint>')
    ap('#if defined(_WIN32)')
    ap('#include <windows.h>')
    ap('#define LOGI(...) do { printf("[hook] " __VA_ARGS__); printf("\\n"); } while(0)')
    ap('#else')
    ap('#include <dlfcn.h>')
    ap('#include <unistd.h>')
    ap('#include <pthread.h>')
    ap('#include <android/log.h>')
    ap('#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, "hook", __VA_ARGS__)')
    ap('#endif')
    ap('')
    ap('// ---- 模块基址 (PC: GameAssembly.dll / 安卓: libil2cpp.so) ----')
    ap('static uintptr_t get_base_() {')
    ap('#if defined(_WIN32)')
    ap('    return (uintptr_t)GetModuleHandleW(L"GameAssembly.dll");')
    ap('#else')
    ap('    // 读 /proc/self/maps, 取 libil2cpp.so 文件偏移为 0 的映射段 = 真实基址')
    ap('    FILE* f = fopen("/proc/self/maps", "r");')
    ap('    if (!f) return 0;')
    ap('    char line[512];')
    ap('    uintptr_t base = 0;')
    ap('    while (fgets(line, sizeof(line), f)) {')
    ap('        unsigned long start, end, offset;')
    ap('        char perms[8], path[256] = {0};')
    ap('        if (sscanf(line, "%lx-%lx %7s %lx", &start, &end, perms, &offset) >= 4) {')
    ap('            char* p = strchr(line, \'/\');')
    ap('            if (p) sscanf(p, "%255s", path);')
    ap('            if (strstr(path, "libil2cpp.so") && offset == 0) {')
    ap('                base = (uintptr_t)start;')
    ap('                break;')
    ap('            }')
    ap('        }')
    ap('    }')
    ap('    fclose(f);')
    ap('    return base;')
    ap('#endif')
    ap('}')
    ap('')
    ap('// ---- il2cpp API (按名解析备用) ----')
    ap('void* (*il2cpp_domain_get)();')
    ap('void** (*il2cpp_domain_get_assemblies)(void*, size_t*);')
    ap('void* (*il2cpp_assembly_get_image)(void*);')
    ap('void* (*il2cpp_class_from_name)(void*, const char*, const char*);')
    ap('void* (*il2cpp_class_get_method_from_name)(void*, const char*, int);')
    ap('')
    ap('static void* resolve_(void* h, const char* sym) {')
    ap('#if defined(_WIN32)')
    ap('    return (void*)GetProcAddress((HMODULE)h, sym);')
    ap('#else')
    ap('    return dlsym(h, sym);')
    ap('#endif')
    ap('}')
    ap('')
    ap('void* il2cpp_find_method(const char* namespaze, const char* cls, const char* m, int argc) {')
    ap('    if (!il2cpp_domain_get) return nullptr;')
    ap('    void* domain = il2cpp_domain_get();')
    ap('    if (!domain) return nullptr;')
    ap('    size_t n = 0;')
    ap('    void** assemblies = il2cpp_domain_get_assemblies(domain, &n);')
    ap('    if (!assemblies) return nullptr;')
    ap('    for (size_t i = 0; i < n; i++) {')
    ap('        void* image = il2cpp_assembly_get_image(assemblies[i]);')
    ap('        void* klass = il2cpp_class_from_name(image, namespaze, cls);')
    ap('        if (!klass) continue;')
    ap('        MethodInfo* mi = (MethodInfo*)il2cpp_class_get_method_from_name(klass, m, argc);')
    ap('        if (mi && mi->methodPointer) return mi->methodPointer;')
    ap('    }')
    ap('    return nullptr;')
    ap('}')
    ap('')
    ap('// ---- 示例 Hook (改这里) ----')
    if s_cls and s_m:
        ap(f'// {s_cls}.{s_m}' + (f' — {s_cn}' if s_cn else ''))
        ap(f'static {s_ret} (*orig_{s_fn})({head_full});')
        ap(f'{s_ret} hook_{s_fn}({head_full}) {{')
        ap('    LOGI("%s 被调用!", "' + f'{s_cls}.{s_m}' + '");')
        ap('    // TODO: 你的逻辑 (改参数 / 改返回值 / 直接 return)')
        ap(f'    {ret_stmt}orig_{s_fn}({call_full});')
        ap('}')
    else:
        ap('// (没有可用示例 — 仿照 README 里的模板写)')
    ap('')
    ap('// ---- 安装 ----')
    ap('static void install_all_() {')
    ap('    uintptr_t base = 0;')
    ap('    for (int i = 0; i < 30 && !base; i++) {   // 最多等 15 秒让模块加载')
    ap('        base = get_base_();')
    ap('        if (!base) {')
    ap('#if defined(_WIN32)')
    ap('            Sleep(500);')
    ap('#else')
    ap('            usleep(500 * 1000);')
    ap('#endif')
    ap('        }')
    ap('    }')
    ap('    if (!base) { LOGI("[!] 模块未加载"); return; }')
    ap('    LOGI("[+] 基址 = 0x%llx", (unsigned long long)base);')
    if s_cls and s_m:
        ap(f'    DobbyHook((void*)(base + {s_macro}), (void*)hook_{s_fn}, (void**)&orig_{s_fn});')
        ap(f'    LOGI("[+] 已 hook {s_cls}.{s_m}");')
    ap('    // 其余目标: 仿照上面写好 hook 函数后取消注释 (偏移宏见 offsets.h):')
    count = 0
    for dll, cls, m, rva, cn in safe_items:
        if f'{cls}.{m}' == f'{s_cls}.{s_m}':
            continue  # 示例函数已在上面安装
        base_name = f'RVA_{_c_ident(cls)}_{_c_ident(m)}'
        if count < 5:
            ap(f'    // DobbyHook((void*)(base + {base_name}), (void*)hook_{_c_ident(cls)}_{_c_ident(m)}, (void**)&orig_{_c_ident(cls)}_{_c_ident(m)});  // {cls}.{m}')
            count += 1
    if len(safe_items) > count + 1:
        ap(f'    // … 共 {len(safe_items)} 个偏移, 全部见 offsets.h')
    ap('}')
    ap('')
    ap('#if defined(_WIN32)')
    ap('static DWORD WINAPI entry_(LPVOID) { install_all_(); return 0; }')
    ap('BOOL APIENTRY DllMain(HMODULE, DWORD reason, LPVOID) {')
    ap('    if (reason == DLL_PROCESS_ATTACH) {')
    ap('        DisableThreadLibraryCalls(GetModuleHandleW(nullptr));')
    ap('        CreateThread(nullptr, 0, entry_, nullptr, 0, nullptr);')
    ap('    }')
    ap('    return TRUE;')
    ap('}')
    ap('#else')
    ap('static void* entry_(void*) { install_all_(); return nullptr; }')
    ap('__attribute__((constructor)) static void on_load_() {')
    ap('    pthread_t t; pthread_create(&t, nullptr, entry_, nullptr);')
    ap('    pthread_detach(t);')
    ap('}')
    ap('#endif')
    hook_main_cpp = '\n'.join(L)

    # ---- CMakeLists.txt ----
    cmake = f'''# dump工具 生成的 Hook 工程 — {meta.get('game', '?')}
cmake_minimum_required(VERSION 3.10)
project({proj_name})

# 首次使用先克隆 Dobby (见 third_party/README.md), 之后这句直接生效
add_subdirectory(third_party/Dobby)

add_library(hook SHARED src/hook_main.cpp)
target_include_directories(hook PRIVATE src)
target_link_libraries(hook dobby log)
'''

    # ---- build.sh ----
    build_sh = f'''#!/usr/bin/env bash
# 一键编译推送 — dump工具 生成  (需要: Android NDK + cmake)
set -e
NDK="${{ANDROID_NDK:-$HOME/Android/Sdk/ndk/25.2.9519653}}"   # ← 改成你的 NDK 路径
[ -d "$NDK" ] || {{ echo "❌ NDK 不存在: $NDK (改脚本开头路径)"; exit 1; }}

cmake -B build \\
  -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \\
  -DANDROID_ABI=arm64-v8a \\
  -DANDROID_PLATFORM=android-24
cmake --build build -j"$(nproc)"

echo "✅ 产物: build/libhook.so"
adb push build/libhook.so /data/local/tmp/ && echo "✅ 已推到 /data/local/tmp/libhook.so"
echo "注入方式见 README.md (Zygisk / LSPatch / LD_PRELOAD)"
'''

    # ---- third_party/README.md ----
    third_party_md = '''# third_party

放第三方依赖。首次使用执行:

```bash
git clone --depth 1 https://github.com/jmpews/Dobby third_party/Dobby
```

或下载 zip 解压后把 Dobby 源码目录放到本目录, 保证存在 `third_party/Dobby/CMakeLists.txt`。
'''

    # ---- README.md ----
    n_items = len(safe_items)
    readme = f'''# {proj_name} — Android il2cpp Hook 工程

由 **dump工具** 自动生成 · 游戏: {meta.get('game', '?')} · 范围: {meta.get('scope', '?')} · 偏移 {n_items} 个

## 目录

```
├── CMakeLists.txt        # NDK 构建 (arm64-v8a)
├── build.sh              # 一键编译 + 推送
├── src/
│   ├── hook_main.cpp     # 入口: 基址获取 + 示例 hook + 安装
│   ├── offsets.h         # 全部目标 RVA (dump工具 生成)
│   ├── il2cpp_api.h      # il2cpp 导出函数 (按名解析备用)
│   └── dobby.h           # Dobby Hook 声明
└── third_party/          # 先克隆 Dobby 到这里 (见其 README)
```

## 使用步骤

1. **装环境** (一次性): Android NDK (r21+) + CMake + adb
2. **拉依赖**:
   ```bash
   git clone --depth 1 https://github.com/jmpews/Dobby third_party/Dobby
   ```
3. **编译推送**:
   ```bash
   bash build.sh
   ```
4. **注入** (三选一):
   - **Zygisk** (有 root + Magisk): 把 libhook.so 包成 Zygisk 模块, 对目标游戏生效
   - **LSPatch** (免 root): 用 LSPatch 把 so 塞进目标 APK 重打包后安装
   - **LD_PRELOAD** (调试): `adb shell setprop wrap.<包名> "LD_PRELOAD=/data/local/tmp/libhook.so"` 后冷启动游戏 (部分系统受限)
5. **看日志**: `adb logcat -s hook`

## 游戏更新了怎么办

- **方案 A (推荐)**: 用 dump工具 → 打开新版 dump.cs → 「版本对比」选新 dump → 「🛫 Hook 迁移」粘贴老代码/offsets.h → 一键重写 RVA
- **方案 B**: 向 dump工具 要「🔄 按名解析」模板 — 运行时按 类名.方法名 查函数, 更新免改码

## 写新 hook

offsets.h 里每个 `RVA_类_方法` 都是一个目标。仿照 hook_main.cpp 的示例:
声明 `orig_xxx` + 实现 `hook_xxx` + 在 install_all_() 里加一行 DobbyHook 即可。
'''

    # ---- 打包 zip ----
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        p = proj_name + '/'
        z.writestr(p + 'README.md', readme)
        z.writestr(p + 'CMakeLists.txt', cmake)
        z.writestr(p + 'build.sh', build_sh)
        z.writestr(p + 'src/hook_main.cpp', hook_main_cpp)
        z.writestr(p + 'src/offsets.h', offsets_h)
        z.writestr(p + 'src/il2cpp_api.h', il2cpp_api_h)
        z.writestr(p + 'src/dobby.h', dobby_h)
        z.writestr(p + 'third_party/README.md', third_party_md)
    return buf.getvalue(), proj_name


# ---------------- GG (GameGuardian) Lua 脚本生成 ----------------

GG_CS_SCALAR = {  # C# 标量类型 → GG flags key
    'int': 'i32', 'uint': 'u32', 'short': 'i16', 'ushort': 'u16',
    'long': 'i64', 'ulong': 'u64', 'byte': 'u8', 'sbyte': 'i8',
    'bool': 'b1', 'float': 'f32', 'double': 'f64', 'char': 'ch2',
    'nint': 'i64', 'nuint': 'u64', 'IntPtr': 'i64', 'UIntPtr': 'u64',
}

GG_CS_STRUCT = {  # Unity 常见值类型 struct → (子字段名, gg类型, 偏移增量)
    'Vector2':   (('x', 'f32', 0), ('y', 'f32', 4)),
    'Vector3':   (('x', 'f32', 0), ('y', 'f32', 4), ('z', 'f32', 8)),
    'Vector4':   (('x', 'f32', 0), ('y', 'f32', 4), ('z', 'f32', 8), ('w', 'f32', 12)),
    'Quaternion': (('x', 'f32', 0), ('y', 'f32', 4), ('z', 'f32', 8), ('w', 'f32', 12)),
    'Color':     (('r', 'f32', 0), ('g', 'f32', 4), ('b', 'f32', 8), ('a', 'f32', 12)),
    'Color32':   (('r', 'u8', 0), ('g', 'u8', 1), ('b', 'u8', 2), ('a', 'u8', 3)),
}


def gen_gg_script_code(dll_name: str, cls, dump_name: str = '',
                       enum_names=frozenset(), struct_names=frozenset()) -> str:
    """生成 GameGuardian Lua 读写骨架: 字段偏移表 + 反向定位 + 查看修改 + 方法地址表
    cls: 解析出的类对象 (fields 为 (name, type, offset, static) 四元组)"""
    cname = cls.name
    cls_cn = _tr(cname) or ''
    dump_name = dump_name or ''

    inst, statics, no_off = [], [], []
    for f in cls.fields:
        name, ftype, off, is_static = f[0], f[1].strip(), f[2], bool(f[3])
        if is_static:
            statics.append((name, ftype, off))
            continue
        if not off:
            no_off.append((name, ftype))
            continue
        inst.append((name, ftype, int(off, 16)))

    # 展开/映射为 GG 读写行: (显示名, gg类型key, 偏移, 中文注释)
    rows = []
    for name, ftype, off in inst:
        base_t = ftype.rstrip('?')
        cn = _tr(name) or ''
        if base_t in GG_CS_SCALAR:
            rows.append((name, GG_CS_SCALAR[base_t], off, cn))
        elif base_t in GG_CS_STRUCT:
            for sub, st, delta in GG_CS_STRUCT[base_t]:
                rows.append((f'{name}.{sub}', st, off + delta, (cn or name) + '.' + sub))
        elif base_t in enum_names:
            rows.append((name, 'i32', off, cn + ' [枚举]'))
        elif base_t in struct_names:
            rows.append((name, 'ptr', off, cn + ' [内联struct:此为前8字节,布局见dump]'))
        elif base_t.endswith('[]') or '[' in ftype or '<' in ftype:
            rows.append((name, 'ptr', off, cn + ' (数组/泛型指针)'))
        else:
            rows.append((name, 'ptr', off, cn + ' (引用指针)'))

    meth_rows = []
    for m in cls.methods:
        rva = m[1]
        if rva:
            meth_rows.append((m[0].replace("'", r"\'"), int(rva, 16), _method_cn(m[0]) or ''))

    L = []
    ap = L.append
    # ---------- 头注释 ----------
    ap('--[[')
    ap('  ==========================================================================')
    ap('  GG (GameGuardian) Lua 脚本 — 由 dump工具 一键生成')
    ap('  ==========================================================================')
    ap(f'  目标类  : {cname}  [{dll_name}]')
    if cls_cn:
        ap(f'  类说明  : {cls_cn} ({cls.cat})')
    ap(f'  字段    : {len(rows)} 个读写项 (实例) / {len(statics)} 个静态 / {len(no_off)} 个无偏移')
    ap(f'  方法    : {len(meth_rows)} 个含 RVA')
    if dump_name:
        ap(f'  来源    : {os.path.basename(dump_name)}')
    ap('')
    ap('  【使用流程】')
    ap('  1. 手机打开 GG, 勾选目标游戏进程')
    ap('  2. 运行本脚本 (GG → Scripts → 选本文件)')
    ap('  3. 手动搜索某个特征字段的当前值 (如 血量/分数), 在搜索结果里')
    ap('     长按任一条 → 复制地址 (如 0x12A45B0F8)')
    ap('  4. 菜单选「📍 定位对象」→ 选刚才搜的字段 → 粘贴地址')
    ap('     对象地址 = 粘贴的地址 - 该字段偏移 (脚本自动算)')
    ap('  5. 「📋 查看字段」读全部; 「✏️ 修改字段」改值 (立即生效)')
    ap('')
    ap('  【原理】')
    ap('  - dump.cs 字段偏移 = il2cpp 对象内偏移 (对象头 0x10 之后)')
    ap('  - 引用类型字段是 8 字节指针, 读到的是地址, 改它=换引用')
    ap('  - 静态字段不在对象里, 需运行时从 Class 对象取, 本骨架未自动化:')
    for name, ftype, off in statics[:20]:
        ap(f'      static {ftype} {name}; // {off or "(无偏移)"}')
    if len(statics) > 20:
        ap(f'      … 共 {len(statics)} 个')
    ap(']]')
    ap('')
    ap('-- ================== 数据表 (由 dump.cs 自动生成) =================')
    ap(f"local CLASS = '{cname}'")
    if cls_cn:
        ap(f"local CLASS_CN = '{cls_cn}'")
    else:
        ap("local CLASS_CN = ''")
    ap('')
    ap('local FIELDS = {')
    for name, tk, off, cn in rows:
        cn = cn.replace("'", r"\'")
        ap(f"  {{n='{name}', off={off:#x}, t='{tk}', cn='{cn}'}},")
    if not rows:
        ap('  -- (该类没有带偏移的实例字段: 见头部注释的静态字段段)')
    ap('}')
    ap('')
    ap('local METHODS = {  -- {n=方法名, rva=偏移, cn=中文}')
    for mname, rva, cn in meth_rows[:80]:
        cn = cn.replace("'", r"\'")
        ap(f"  {{n='{mname}', rva={rva:#x}, cn='{cn}'}},")
    if len(meth_rows) > 80:
        ap(f'  -- … 共 {len(meth_rows)} 个方法, 仅列前 80 个')
    if not meth_rows:
        ap('  -- (该类没有含 RVA 的方法)')
    ap('}')
    ap('')
    # ---------- 固定骨架 ----------
    ap(r'''-- ================== GG 类型映射 =================
local FLAGS = {
  i32 = gg.TYPE_DWORD, u32 = gg.TYPE_DWORD,
  i64 = gg.TYPE_QWORD, u64 = gg.TYPE_QWORD,
  i16 = gg.TYPE_WORD,  u16 = gg.TYPE_WORD,
  i8  = gg.TYPE_BYTE,  u8 = gg.TYPE_BYTE,  b1 = gg.TYPE_BYTE,
  ch2 = gg.TYPE_WORD,
  f32 = gg.TYPE_FLOAT, f64 = gg.TYPE_DOUBLE,
  ptr = gg.TYPE_QWORD,
}

local OBJ = nil    -- 对象实例地址 (定位后填入)
local BASE = nil   -- libil2cpp.so 加载基址

local function getBase()
  if BASE then return BASE end
  local ok, ranges = pcall(gg.getRangesList, 'libil2cpp.so')
  if ok and ranges and ranges[1] then
    BASE = ranges[1].start or ranges[1][1]
  end
  if not BASE then
    BASE = 0
    gg.toast('⚠ 未获取 libil2cpp.so 基址, 方法地址表不可用')
  end
  return BASE
end

-- ---------- 📍 定位对象: 用"已知字段地址"反推对象地址 ----------
local function locate()
  if #FIELDS == 0 then
    gg.alert('该类没有带偏移的实例字段, 无法定位', '知道了')
    return
  end
  local names = {}
  for i, f in ipairs(FIELDS) do
    names[i] = string.format('%s (+0x%X)', f.n, f.off)
  end
  local c = gg.choice(names, nil, '你在 GG 里搜到了哪个字段的值?')
  if not c then return end
  local f = FIELDS[c]
  local a = gg.prompt({'粘贴 GG 搜索结果里显示的地址\n(长按搜索结果可复制):'}, nil, {'text'})
  if not a or not a[1] or a[1] == '' then return end
  local addr = tonumber(a[1], 16) or tonumber(a[1])
  if not addr then gg.toast('地址解析失败'); return end
  OBJ = addr - f.off
  gg.alert(string.format('对象地址 = %s - 0x%X = 0x%X\n\n已记住! 可以直接查看/修改了',
      a[1], f.off, OBJ))
end

-- ---------- 📋 查看全部字段 ----------
local function readAll()
  if not OBJ then gg.toast('请先「📍 定位对象」'); return end
  local req = {}
  for i, f in ipairs(FIELDS) do
    req[i] = {address = OBJ + f.off, flags = FLAGS[f.t], name = f.n}
  end
  local res = gg.getValues(req)
  local out = {}
  for i, f in ipairs(FIELDS) do
    out[#out+1] = string.format('%-26s (+0x%03X) = %-22s -- %s',
        f.n, f.off, tostring(res[i].value), f.cn)
  end
  if #FIELDS <= 20 then
    gg.alert(table.concat(out, '\n'), '知道了')
  else
    print(table.concat(out, '\n'))
    gg.toast(#FIELDS .. ' 个字段已打印到日志')
  end
end

-- ---------- ✏️ 修改字段值 ----------
local function editOne()
  if not OBJ then gg.toast('请先「📍 定位对象」'); return end
  local names = {}
  for i, f in ipairs(FIELDS) do
    names[i] = string.format('%s (+0x%X %s) %s', f.n, f.off, f.t, f.cn)
  end
  local c = gg.choice(names, nil, '选择要修改的字段')
  if not c then return end
  local f = FIELDS[c]
  local cur = gg.getValues({{address = OBJ + f.off, flags = FLAGS[f.t]}})[1].value
  local wide = (f.t == 'ptr' or f.t == 'i64' or f.t == 'u64')
  local nv = gg.prompt({string.format('新值 (当前: %s)%s:',
      tostring(cur), wide and '\n支持 0x 十六进制' or '')},
      {tostring(cur)}, {wide and 'text' or 'number'})
  if not nv or nv[1] == nil then return end
  local v
  if wide then
    v = tonumber(nv[1], 16) or tonumber(nv[1])
  else
    v = tonumber(nv[1])
  end
  if v == nil then gg.toast('数值解析失败'); return end
  gg.setValues({{address = OBJ + f.off, flags = FLAGS[f.t], value = v}})
  gg.toast(f.n .. ' 已写入 ✓')
end

-- ---------- 🔢 方法地址表: 基址 + RVA = 运行时真实地址 ----------
local function methodAddrs()
  local b = getBase()
  if b == 0 then gg.alert('未拿到基址, 无法换算', '知道了'); return end
  local out = {string.format('[dump工具] %s 方法运行时地址 (基址 0x%X)', CLASS, b)}
  for _, m in ipairs(METHODS) do
    out[#out+1] = string.format('%-42s RVA 0x%08X -> 0x%X  -- %s',
        m.n, m.rva, b + m.rva, m.cn)
  end
  print(table.concat(out, '\n'))
  gg.toast(#METHODS .. ' 个方法地址已打印到日志\n(内存查看器可 goto address 对照)')
end

-- ---------- 🧰 底层读取工具 ----------
local function readPtr(addr)
  local ok, r = pcall(gg.getValues, {{address = addr, flags = gg.TYPE_QWORD}})
  if ok and r and r[1] and tonumber(r[1].value) then
    local v = tonumber(r[1].value)
    if v < 0 then v = v + 0x20000000000000000 end  -- 补码兜底
    return v
  end
  return nil
end

local function readStr(addr, maxLen)
  maxLen = maxLen or 64
  local items = {}
  for i = 0, maxLen - 1 do items[#items+1] = {address = addr + i, flags = gg.TYPE_BYTE} end
  local ok, vals = pcall(gg.getValues, items)
  if not ok or not vals then return nil end
  local t = {}
  for i = 1, maxLen do
    local b = vals[i] and tonumber(vals[i].value)
    if b == nil then return nil end
    if b < 0 then b = b + 256 end
    if b == 0 then break end
    if b < 33 or b > 126 then return nil end   -- 非可打印 ASCII → 不是标识符
    t[#t+1] = string.char(b)
  end
  if #t < 2 then return nil end
  return table.concat(t)
end

-- il2cpp 对象头: [obj+0x0] = Il2CppClass*, 类名指针在 klass+0x10
local function classNameOf(objAddr)
  local klass = readPtr(objAddr)
  if not klass or klass < 0x1000 or klass > 0x800000000000 then return nil end
  local name = readStr(klass + 0x10)
  if name and name:match('^[_%a][_%w]*$') then
    local ns = readStr(klass + 0x18)
    return name, ns
  end
  return nil
end

-- ---------- 📐 字段地址表: 定位后打印每个字段的绝对地址 ----------
local function dumpFieldAddrs()
  if not OBJ then gg.toast('请先「📍 定位对象」'); return end
  local out = {string.format('[dump工具] %s 字段绝对地址 (对象 0x%X)', CLASS, OBJ)}
  for _, f in ipairs(FIELDS) do
    out[#out+1] = string.format('%-26s 0x%-14X (+0x%03X)  -- %s',
        f.n, OBJ + f.off, f.off, f.cn)
  end
  print(table.concat(out, '\\n'))
  gg.toast(#FIELDS .. ' 个字段地址已打印到日志\\n(可直接复制到内存查看器 goto)')
end

-- ---------- 🔍 反查地址: 任意地址 → 方法 / 字段 / 类名 ----------
local function identifyAddr()
  local a = gg.prompt({'粘贴要反查的内存地址:\\n(字段地址 / 对象地址 / 方法地址都行)'}, nil, {'text'})
  if not a or not a[1] or a[1] == '' then return end
  local addr = tonumber(a[1], 16) or tonumber(a[1])
  if not addr then gg.toast('地址解析失败'); return end
  local lines = {string.format('🔍 反查 0x%X :', addr)}

  -- 1) 本类方法地址命中 (基址 + RVA)
  local b = getBase()
  if b > 0 and addr >= b then
    local rva = addr - b
    for _, m in ipairs(METHODS) do
      if m.rva == rva then
        lines[#lines+1] = string.format('✅ 方法: %s.%s  (RVA 0x%X)', CLASS, m.n, rva)
        if m.cn and m.cn ~= '' then lines[#lines+1] = '   ' .. m.cn end
        gg.alert(table.concat(lines, '\\n'), '知道了')
        return
      end
    end
  end

  -- 2) 已定位对象时的字段命中 (addr - OBJ = 偏移)
  if OBJ and addr >= OBJ and addr < OBJ + 0x1000 then
    local off = addr - OBJ
    for _, f in ipairs(FIELDS) do
      if f.off == off then
        lines[#lines+1] = string.format('✅ 字段: %s.%s  (+0x%X, %s)', CLASS, f.n, off, f.t)
        if f.cn and f.cn ~= '' then lines[#lines+1] = '   ' .. f.cn end
        gg.alert(table.concat(lines, '\\n'), '知道了')
        return
      end
    end
    lines[#lines+1] = string.format('距对象基址 +0x%X (无精确字段命中, 可能是数组/子对象内部)', off)
  end

  -- 3) il2cpp 对象头探测: 把地址当对象地址, 读 klass → 类名
  local name, ns = classNameOf(addr)
  if name then
    lines[#lines+1] = '✅ 是 il2cpp 对象, 类名: ' .. name
    if ns and #ns > 0 then lines[#lines+1] = '   命名空间: ' .. ns end
    if name == CLASS then
      lines[#lines+1] = '   ← 正是本脚本的目标类! 地址即对象地址'
    else
      lines[#lines+1] = '   (非本脚本目标类 ' .. CLASS .. ', 可在 dump工具 中对该类生成专属脚本)'
    end
    gg.alert(table.concat(lines, '\\n'), '知道了')
    return
  end

  lines[#lines+1] = '❌ 未识别, 排查建议:'
  lines[#lines+1] = '  · 地址处不是合法 il2cpp klass 指针'
  lines[#lines+1] = '  · 也不在已定位对象的字段范围内'
  lines[#lines+1] = '  · 若是别的类的地址: 在 dump工具 中生成该类的 GG 脚本再反查'
  gg.alert(table.concat(lines, '\\n'), '知道了')
end

-- ---------- ℹ 关于 ----------
local ABOUT_TEXT = [[
============================================================
 GG Lua 脚本骨架 - dump工具 生成
------------------------------------------------------------
 · FIELDS/METHODS 表全部来自 dump.cs 解析结果
 · 定位原理: 对象地址 = 已知字段地址 - 该字段偏移
 · 字段偏移是对象内偏移; 引用类型字段读出的是 8 字节指针
 · 方法地址 = libil2cpp.so 基址 + RVA (ASLR 每次启动变)
 · 想调函数逻辑请配合 Frida/Hook, GG 只做内存读写
============================================================
]]

local function about()
  print(ABOUT_TEXT)
  gg.toast('说明已打印到日志')
end

-- ================== 主循环 =================
while true do
  local c = gg.choice({
    '📍 定位对象 (首次使用先做这个)',
    '📋 查看全部字段',
    '✏️ 修改字段值',
    '📐 字段地址表 (定位后打印)',
    '🔢 方法地址表 (打印到日志)',
    '🔍 反查地址 (地址→字段/方法/类名)',
    'ℹ 关于本脚本',
    '🚪 退出',
  }, nil, CLASS .. '\\n' .. CLASS_CN .. ' · ' .. #FIELDS .. ' 字段 · dump工具生成')
  if c == 1 then locate()
  elseif c == 2 then readAll()
  elseif c == 3 then editOne()
  elseif c == 4 then dumpFieldAddrs()
  elseif c == 5 then methodAddrs()
  elseif c == 6 then identifyAddr()
  elseif c == 7 then about()
  elseif c == 8 or c == nil then break end
end
os.exit()''')
    return '\n'.join(L) + '\n'


def gen_gg_probe_code(star_classes=None, dump_name=''):
    """生成通用 il2cpp 探测器 GG Lua 脚本 (不绑定单个类):
    任意地址 → 类名探测 / 基址查询 / 对象头部 hexdump。
    star_classes: 收藏类名单 [(name, cn), ...], 命中时特别标注。
    """
    stars_lua = []
    for name, cn in (star_classes or []):
        stars_lua.append('  {n = "%s", cn = "%s"},' % (name, (cn or '').replace('"', '')))
    stars_block = '\n'.join(stars_lua) if stars_lua else ''

    L = []
    A = L.append
    A('-- ===========================================================')
    A('-- 🔍 il2cpp 探测器 (通用版) - dump工具 生成')
    A('-- 任意内存地址 → 属于哪个类 / 是什么对象 / 基址换算')
    if dump_name:
        A('-- 数据来源: %s' % dump_name)
    A('-- 适用: Unity il2cpp 游戏 (32/64 位均可)')
    A('-- 原理: il2cpp 对象头 [addr+0x0] 存 Il2CppClass*,')
    A('--       类名指针在 klass+0x10, 命名空间在 klass+0x18')
    A('-- ===========================================================')
    A('')
    A('local okT, TI = pcall(gg.getTargetInfo)')
    A('local is64bit = okT and TI and TI.x64 or false')
    A('')
    A('-- ⭐ 收藏类名单 (dump工具 中收藏的类, 探测命中会标注)')
    A('local STARS = {')
    A(stars_block)
    A('}')
    A('')
    A('local BASE = nil')
    A('')
    A('local function getBase()')
    A('  if BASE then return BASE end')
    A('  local ok, ranges = pcall(gg.getRangesList, \'libil2cpp.so\')')
    A('  if ok and ranges and ranges[1] then')
    A('    BASE = ranges[1].start or ranges[1][1]')
    A('  end')
    A('  if not BASE then BASE = 0 end')
    A('  return BASE')
    A('end')
    A('')
    A('local function readPtr(addr)')
    A('  local ok, r = pcall(gg.getValues, {{address = addr, flags = gg.TYPE_QWORD}})')
    A('  if ok and r and r[1] and tonumber(r[1].value) then')
    A('    local v = tonumber(r[1].value)')
    A('    if v < 0 then v = v + 0x20000000000000000 end')
    A('    return v')
    A('  end')
    A('  return nil')
    A('end')
    A('')
    A('local function readDword(addr)')
    A('  local ok, r = pcall(gg.getValues, {{address = addr, flags = gg.TYPE_DWORD}})')
    A('  if ok and r and r[1] and tonumber(r[1].value) then')
    A('    local v = tonumber(r[1].value)')
    A('    if v < 0 then v = v + 0x100000000 end')
    A('    return v')
    A('  end')
    A('  return nil')
    A('end')
    A('')
    A('local function readStr(addr, maxLen)')
    A('  maxLen = maxLen or 64')
    A('  local items = {}')
    A('  for i = 0, maxLen - 1 do items[#items+1] = {address = addr + i, flags = gg.TYPE_BYTE} end')
    A('  local ok, vals = pcall(gg.getValues, items)')
    A('  if not ok or not vals then return nil end')
    A('  local t = {}')
    A('  for i = 1, maxLen do')
    A('    local b = vals[i] and tonumber(vals[i].value)')
    A('    if b == nil then return nil end')
    A('    if b < 0 then b = b + 256 end')
    A('    if b == 0 then break end')
    A('    if b < 33 or b > 126 then return nil end')
    A('    t[#t+1] = string.char(b)')
    A('  end')
    A('  if #t < 2 then return nil end')
    A('  return table.concat(t)')
    A('end')
    A('')
    A('-- il2cpp 对象探测: 返回 类名, 命名空间 (32 位用 dword 指针)')
    A('local function classNameOf(objAddr, is64)')
    A('  local klass = is64 and readPtr(objAddr) or readDword(objAddr)')
    A('  if not klass or klass < 0x1000 then return nil end')
    A('  if is64 and klass > 0x800000000000 then return nil end')
    A('  local name = readStr(klass + 0x10)')
    A('  if name and name:match(\'^[_%a][_%w]*$\') then')
    A('    local ns = readStr(klass + 0x18)')
    A('    return name, ns')
    A('  end')
    A('  return nil')
    A('end')
    A('')
    A('-- ---------- 🔍 探测地址 ----------')
    A('local function probe()')
    A('  local a = gg.prompt({\'粘贴要探测的内存地址:\\n(对象地址 / 指针字段的值 / 任意疑似对象地址)\'},')
    A('      nil, {\'text\'})')
    A('  if not a or not a[1] or a[1] == \'\' then return end')
    A('  local addr = tonumber(a[1], 16) or tonumber(a[1])')
    A('  if not addr then gg.toast(\'地址解析失败\'); return end')
    A('  local lines = {string.format(\'🔍 探测 0x%X :\', addr)}')
    A('')
    A('  -- 1) 按当前进程位数尝试读对象头')
    A('  local name, ns = classNameOf(addr, is64bit)')
    A('  if name then')
    A('    lines[#lines+1] = \'✅ il2cpp 对象, 类名: \' .. name')
    A('    if ns and #ns > 0 then lines[#lines+1] = \'   命名空间: \' .. ns end')
    A('    for _, s in ipairs(STARS) do')
    A('      if s.n == name then')
    A('        lines[#lines+1] = \'   ⭐ 这是你收藏的类\' .. (s.cn ~= \'\' and (\' (\' .. s.cn .. \')\') or \'\')')
    A('        break')
    A('      end')
    A('    end')
    A('    lines[#lines+1] = \'   对象地址即此地址, 在 dump工具 中生成该类 GG 脚本可读写全部字段\'')
    A('    gg.alert(table.concat(lines, \'\\n\'), \'知道了\')')
    A('    return')
    A('  end')
    A('')
    A('  -- 2) 失败时: 地址可能指向对象内部, 尝试 0x0-0x100 对齐回扫')
    A('  for back = 8, 0x100, 8 do')
    A('    local n2, ns2 = classNameOf(addr - back, is64bit)')
    A('    if n2 then')
    A('      lines[#lines+1] = string.format(\'✅ 向前对齐 0x%X 后命中: 类名 %s\', back, n2)')
    A('      if ns2 and #ns2 > 0 then lines[#lines+1] = \'   命名空间: \' .. ns2 end')
    A('      lines[#lines+1] = string.format(\'   对象地址 ≈ 0x%X, 输入的地址在对象内 +0x%X 处\', addr - back, back)')
    A('      gg.alert(table.concat(lines, \'\\n\'), \'知道了\')')
    A('      return')
    A('    end')
    A('  end')
    A('')
    A('  -- 3) 都失败: 打印头部数据帮助判断')
    A('  lines[#lines+1] = \'❌ 未识别为 il2cpp 对象, 头部数据:\'')
    A('  local b = getBase()')
    A('  if b > 0 and addr >= b then')
    A('    lines[#lines+1] = string.format(\'提示: 基址 0x%X, 相对偏移(RVA) = 0x%X\', b, addr - b)')
    A('    lines[#lines+1] = \'     若是方法地址: 把 RVA 粘到 dump工具 搜索框可直接反查方法名\'')
    A('  end')
    A('  local vals = {}')
    A('  for i = 0, 3 do')
    A('    local v = is64bit and readPtr(addr + i * 8) or readDword(addr + i * 4)')
    A('    vals[#vals+1] = string.format(\'  [+0x%02X] = 0x%X\', i * (is64bit and 8 or 4), v or 0)')
    A('  end')
    A('  lines[#lines+1] = table.concat(vals, \'\\n\')')
    A('  print(table.concat(lines, \'\\n\'))')
    A('  gg.alert(table.concat(lines, \'\\n\'), \'知道了\')')
    A('end')
    A('')
    A('-- ---------- 📐 显示基址 ----------')
    A('local function showBase()')
    A('  local b = getBase()')
    A('  if b == 0 then gg.alert(\'未找到 libil2cpp.so (确认游戏是 il2cpp 且已运行)\', \'知道了\'); return end')
    A('  gg.alert(string.format(\'libil2cpp.so 基址 = 0x%X\\n\\n用 GG 搜到的绝对地址 - 基址 = RVA\\n把 RVA 粘到 dump工具 搜索框可反查方法\', b), \'知道了\')')
    A('  print(\'[探测器] libil2cpp.so 基址 = 0x\' .. string.format(\'%X\', b))')
    A('end')
    A('')
    A('-- ---------- 🧾 hexdump (对象头部 128 字节) ----------')
    A('local function dumpHead()')
    A('  local a = gg.prompt({\'粘贴对象/内存地址:\'}, nil, {\'text\'})')
    A('  if not a or not a[1] or a[1] == \'\' then return end')
    A('  local addr = tonumber(a[1], 16) or tonumber(a[1])')
    A('  if not addr then gg.toast(\'地址解析失败\'); return end')
    A('  local step = is64bit and 8 or 4')
    A('  local out = {string.format(\'🧾 0x%X 处 %d 字节:\', addr, 128)}')
    A('  for row = 0, 7 do')
    A('    local seg = {}')
    A('    for col = 0, 3 do')
    A('      local v = is64bit and readPtr(addr + (row * 4 + col) * 8) or readDword(addr + (row * 4 + col) * 4)')
    A('      seg[#seg+1] = string.format(\'%0\' .. (is64bit and 16 or 8) .. \'X\', v or 0)')
    A('    end')
    A('    out[#out+1] = string.format(\'  +%03X  %s\', row * 16, table.concat(seg, \' \'))')
    A('  end')
    A('  print(table.concat(out, \'\\n\'))')
    A('  gg.toast(\'已打印到日志\')')
    A('end')
    A('')
    A('-- ---------- 📋 收藏类名单 ----------')
    A('local function listStars()')
    A('  if #STARS == 0 then gg.alert(\'没有收藏类\\n\\n在 dump工具 的类详情页点 ⭐ 收藏后重新导出探测器\', \'知道了\'); return end')
    A('  local out = {\'⭐ 收藏类名单 (\' .. #STARS .. \' 个):\'}')
    A('  for i, s in ipairs(STARS) do')
    A('    out[#out+1] = string.format(\'  %d. %s%s\', i, s.n, s.cn ~= \'\' and (\' -- \' .. s.cn) or \'\')')
    A('  end')
    A('  print(table.concat(out, \'\\n\'))')
    A('  gg.toast(\'已打印到日志\')')
    A('end')
    A('')
    A('-- ================== 主循环 =================')
    A('while true do')
    A('  local c = gg.choice({')
    A('    \'🔍 探测地址 (→类名)\',')
    A('    \'📐 显示基址 (换算 RVA)\',')
    A('    \'🧾 对象头部 hexdump\',')
    A('    \'📋 收藏类名单\',')
    A('    \'🚪 退出\',')
    A('  }, nil, \'🔍 il2cpp 探测器 · dump工具\' .. (#STARS > 0 and (\' · ⭐\' .. #STARS) or \'\'))')
    A('  if c == 1 then probe()')
    A('  elseif c == 2 then showBase()')
    A('  elseif c == 3 then dumpHead()')
    A('  elseif c == 4 then listStars()')
    A('  elseif c == 5 or c == nil then break end')
    A('end')
    A('os.exit()')
    return '\n'.join(L) + '\n'


def diff_dumps(old_dlls: dict, new_dlls: dict) -> dict:
    """对比两份 dump 解析结果 (纯函数), 返回变化清单"""
    def build_index(dlls):
        idx = {}
        for dll_name, d in dlls.items():
            for cls in d['classes']:
                methods = {}
                for m in cls.methods:
                    mname, rva = m[0], m[1]
                    s = methods.setdefault(mname, set())
                    if rva:
                        s.add(rva)
                idx[(dll_name, cls.name)] = (cls.cn, methods)
        return idx

    old_idx = build_index(old_dlls)
    new_idx = build_index(new_dlls)

    classes_added, classes_removed = [], []
    methods_added, methods_removed = [], []
    rva_changed = []

    old_keys, new_keys = set(old_idx), set(new_idx)
    for k in sorted(new_keys - old_keys):
        cn, methods = new_idx[k]
        classes_added.append([k[0], k[1], cn, len(methods)])
    for k in sorted(old_keys - new_keys):
        cn, methods = old_idx[k]
        classes_removed.append([k[0], k[1], cn, len(methods)])

    for k in sorted(old_keys & new_keys):
        old_cn, old_m = old_idx[k]
        new_cn, new_m = new_idx[k]
        for mname in sorted(set(old_m) | set(new_m)):
            o_set = old_m.get(mname, set())
            n_set = new_m.get(mname, set())
            if o_set == n_set:
                continue
            removed_rva = o_set - n_set
            added_rva = n_set - o_set
            if removed_rva and added_rva:
                # 同名方法地址漂移 (可能含重载, 按顺序配对)
                for o_r, n_r in zip(sorted(removed_rva), sorted(added_rva)):
                    rva_changed.append([k[0], k[1], mname, o_r, n_r, new_cn])
            elif added_rva:
                for r in sorted(added_rva):
                    methods_added.append([k[0], k[1], mname, r, new_cn])
            elif removed_rva:
                for r in sorted(removed_rva):
                    methods_removed.append([k[0], k[1], mname, r, old_cn])

    return {
        'classes_added': classes_added,
        'classes_removed': classes_removed,
        'methods_added': methods_added,
        'methods_removed': methods_removed,
        'rva_changed': rva_changed,
        'stats': {
            'classes_added': len(classes_added),
            'classes_removed': len(classes_removed),
            'methods_added': len(methods_added),
            'methods_removed': len(methods_removed),
            'rva_changed': len(rva_changed),
        },
    }


# ============================================================
# 4. dump.cs 解析器
# ============================================================
class ClassInfo:
    __slots__ = ('name', 'kind', 'cn', 'purpose', 'base', 'role', 'cat', 'ns',
                 'methods', 'fields', 'props', 'start_depth')

    def __init__(self, name, kind, bases, ns):
        self.name = name.split('<')[0].split('`')[0]
        self.kind = KIND_CN.get(kind, kind)
        self.cn = _tr(self.name)
        self.purpose = TD.infer_class_purpose(self.name) or ''
        self.base = ''
        for b in re.split(r'[,\s]+', bases or ''):
            b = b.strip().split('<')[0].split('.')[-1]
            if b and not b.startswith('I'):
                self.base = b
                break
        self.role = TD.find_base_role(bases or '') or ''
        self.cat = TD.classify_class(self.name)
        self.ns = ns
        self.methods = []   # (name, rva, ret, is_static, params)
        self.fields = []    # (name, type, offset)
        self.props = []     # (name, type)
        self.start_depth = 0

    def summary(self):
        return {
            'n': self.name, 'k': self.kind, 'cn': self.cn,
            'cat': self.cat, 'b': self.base, 'r': self.role, 'p': self.purpose,
            'f': len(self.fields), 'm': len(self.methods), 'pr': len(self.props),
        }

    def detail(self):
        methods = [{
            'n': m[0], 'cn': _method_cn(m[0]), 'rva': m[1], 'ret': m[2], 'static': m[3], 'params': m[4],
        } for m in self.methods]
        fields = [{'n': f[0], 't': f[1], 'off': f[2], 'cn': _tr(f[0])} for f in self.fields]
        props = [{'n': p[0], 't': p[1], 'cn': _tr(p[0])} for p in self.props]
        return {
            'n': self.name, 'k': self.kind, 'cn': self.cn,
            'p': self.purpose, 'b': self.base, 'r': self.role,
            'cat': self.cat, 'ns': self.ns,
            'methods': methods, 'fields': fields, 'props': props,
        }


def parse_dump(path: str):
    """解析 dump.cs → {dll: {'group':.., 'classes':[ClassInfo], 'cats':{cat:[summary]}}}"""
    t0 = time.time()
    dlls = {}
    class_stack = []
    depth = 0
    cur_dll = '?'
    cur_ns = '(全局)'
    pending_rva = None
    section = None

    # 预分配按 DLL 组织
    def ensure_dll(name):
        if name not in dlls:
            dlls[name] = {'group': classify_dll(name), 'classes': [], 'cats': {}}
        return dlls[name]

    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            stripped = line.strip()

            # DLL / 命名空间标记
            m = DLL_RE.match(line)
            if m:
                cur_dll = m.group(1).strip()
                ensure_dll(cur_dll)
                continue
            m = NS_RE.match(line)
            if m:
                cur_ns = m.group(1).strip() or '(全局)'
                continue
            if line.startswith('// Image'):
                continue

            # 类声明
            cm = CLASS_RE.match(line)
            if cm and '//' not in line.split('{')[0][-40:]:
                cls = ClassInfo(cm.group('name'), cm.group('kind'), cm.group('bases') or '', cur_ns)
                cls.start_depth = depth
                dlls[cur_dll]['classes'].append(cls)
                class_stack.append(cls)
                if '{' in line:
                    depth += 1
                pending_rva = None
                section = None
                continue

            # 大括号 (仅类/结构体有独立行大括号; 方法体是行内 { })
            if stripped == '{':
                depth += 1
                continue
            if stripped == '}':
                depth -= 1
                while class_stack and class_stack[-1].start_depth >= depth:
                    class_stack.pop()
                pending_rva = None
                continue

            # 成员行 (属于栈顶类)
            if not class_stack:
                continue
            top = class_stack[-1]

            m = RVA_RE.match(line)
            if m:
                pending_rva = m.group(1)
                continue

            if '// Methods' in line:
                section = 'm'
                continue
            if '// Fields' in line:
                section = 'f'
                continue
            if '// Properties' in line:
                section = 'p'
                continue

            mm = METHOD_RE.match(line)
            if mm:
                name = mm.group('name')
                if name in ('get', 'set', 'add', 'remove'):
                    continue
                mods = mm.group('mods') or ''
                is_static = 'static' in mods
                rva = pending_rva or ''
                top.methods.append((name, rva, mm.group('ret').strip(), is_static, mm.group('params').strip()))
                pending_rva = None
                continue

            pm = PROP_RE.match(line)
            if pm:
                name = pm.group('name')
                top.props.append((name, pm.group('type').strip()))
                pending_rva = None
                continue

            fm = FIELD_RE.match(line)
            if fm:
                name = fm.group('name')
                om = OFFSET_RE.search(fm.group('tail') or '')
                mods = fm.group('mods') or ''
                top.fields.append((name, fm.group('type').strip(),
                                   om.group(1) if om else '', 'static' in mods))
                pending_rva = None
                continue

    # 建立每个 DLL 的分类索引 (cat -> [summary])
    for dll_name, dll in dlls.items():
        cats = {}
        for cls in dll['classes']:
            cats.setdefault(cls.cat, []).append(cls.summary())
        dll['cats'] = cats

    elapsed = time.time() - t0
    total_classes = sum(len(d['classes']) for d in dlls.values())
    total_methods = sum(len(c.methods) for d in dlls.values() for c in d['classes'])
    return dlls, total_classes, total_methods, elapsed


# ---------------- 解析缓存 (同一文件二次打开 10s → 1s 内) ----------------
CACHE_VER = 4  # 解析器/数据结构变更时 +1, 旧缓存自动失效


def _dict_fingerprint():
    """翻译词典指纹: 用户改了 WORD_DICT 后缓存自动失效"""
    try:
        raw = repr(sorted(TD.WORD_DICT.items())).encode('utf-8', 'replace')
        return hashlib.md5(raw).hexdigest()[:10]
    except Exception:
        return '?'


def load_parsed(path):
    """parse_dump 的缓存版: 返回 (dlls, 类数, 方法数, 耗时, 是否命中缓存)"""
    st = os.stat(path)
    cache_path = path + '.dcache'
    # 1) 尝试命中缓存
    if os.path.isfile(cache_path):
        try:
            t0 = time.time()
            with open(cache_path, 'rb') as f:
                head = pickle.load(f)
                if (head.get('ver') == CACHE_VER
                        and head.get('size') == st.st_size
                        and int(head.get('mtime', -1)) == int(st.st_mtime)
                        and head.get('dict') == _dict_fingerprint()):
                    dlls = pickle.load(f)
                    total_classes = sum(len(d['classes']) for d in dlls.values())
                    total_methods = sum(len(c.methods) for d in dlls.values() for c in d['classes'])
                    return dlls, total_classes, total_methods, time.time() - t0, True
        except Exception:
            pass  # 缓存损坏/版本不符 → 走正常解析
    # 2) 正常解析 + 写缓存
    dlls, total_classes, total_methods, elapsed = parse_dump(path)
    try:
        tmp = cache_path + '.tmp'
        with open(tmp, 'wb') as f:
            pickle.dump({'ver': CACHE_VER, 'size': st.st_size,
                         'mtime': int(st.st_mtime), 'dict': _dict_fingerprint()}, f, protocol=4)
            pickle.dump(dlls, f, protocol=4)
        os.replace(tmp, cache_path)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
    return dlls, total_classes, total_methods, elapsed, False


# ============================================================
# 5. 应用 API (暴露给前端 JS 调用)
# ============================================================
class Api:
    def __init__(self):
        self.dump_path = None
        self.dlls = None
        self.total_classes = 0
        self.total_methods = 0
        self.load_elapsed = 0.0
        self.loaded_from_cache = False
        self._class_index = None  # 全局类索引 (用于搜索)
        self._rva_index = None    # RVA → (dll, cls, method) 索引 (用于地址反查)
        self._methods_flat = None # 方法名搜索用扁平索引 [(lname, dll, cls, m)]
        self._class_name_set = None  # 全部类名集合 (字段类型可点跳转判定)
        self.auto_load_path = None  # 命令行传入的待加载文件
        # ⭐ 收藏
        self._stars = self._load_stars()
        # 🔀 版本对比
        self.diff_state = 'none'      # none|loading|ready|error
        self.diff_msg = ''
        self.diff_path = None         # 第二份 dump.cs 路径
        self._diff_data = None        # diff_dumps 结果缓存
        self._diff_new_dlls = None
        self._diff_new_idx = None     # 🛫 Hook 迁移用: 新版 (类,方法)→[RVA] 索引
        # 🔤 字符串表 (stringliteral.json, 懒加载)
        self._strings_path = None
        self._strings = None
        # 📱 ADB 调试
        self._adb_serial = None       # 当前选中设备
        self._adb_apps = None         # 应用列表缓存
        self._adb_task = {'kind': '', 'state': 'idle', 'msg': '', 'result': None}
        self._adb_frida_runner = None  # FridaRunner 实例
        self._adb_frida_hidden = None  # 🥷 隐身模式 {'port','remote','name'} | None
        self._adb_load_cfg()

    # ---------- 文件加载 ----------
    def open_file(self):
        """原生文件选择对话框 (Win32), 返回所选路径 (取消返回 None)"""
        _dbg('api.open_file 被调用')
        return _win32_dialog(save_mode=False, title='选择 dump.cs 文件')

    def save_file(self, default_name='dump.zh-CN.cs'):
        _dbg('api.save_file 被调用')
        return _win32_dialog(save_mode=True, title='导出翻译后的 dump.cs',
                             default_name=default_name)

    def load_file(self, path):
        """解析 dump.cs, 返回摘要信息 (带缓存: 同文件二次打开秒级)"""
        _dbg(f'api.load_file({path!r})')
        if not path or not os.path.isfile(path):
            return {'ok': False, 'error': '文件不存在: ' + str(path)}
        try:
            (self.dlls, self.total_classes, self.total_methods,
             self.load_elapsed, cached) = load_parsed(path)
        except Exception as e:
            return {'ok': False, 'error': f'解析失败: {e}'}
        self.dump_path = path
        self.loaded_from_cache = cached
        self._kind_cache = None  # GG 脚本用: enum/struct 名集合缓存失效
        self._build_index()
        self._save_recent(path)
        # 自动探测同目录 stringliteral.json (Il2CppDumper 同时输出的字符串表)
        sp = os.path.join(os.path.dirname(os.path.abspath(path)), 'stringliteral.json')
        self._strings_path = sp if os.path.isfile(sp) else None
        self._strings = None
        return {
            'ok': True,
            'path': path,
            'name': os.path.basename(path),
            'size': os.path.getsize(path),
            'total_classes': self.total_classes,
            'total_methods': self.total_methods,
            'dll_count': len(self.dlls),
            'elapsed': round(self.load_elapsed, 2),
            'cached': cached,
            'strings_available': bool(self._strings_path),
        }

    def _build_index(self):
        idx = []
        rva_idx = {}
        mflat = []
        name_set = set()
        for dll_name, dll in self.dlls.items():
            for cls in dll['classes']:
                idx.append((cls.name.lower(), cls.cn, dll_name, cls))
                name_set.add(cls.name)
                for m in cls.methods:
                    if m[1]:
                        try:
                            rva_idx[int(m[1], 16)] = (dll_name, cls, m)
                        except (ValueError, TypeError):
                            pass
                    mflat.append((m[0].lower(), dll_name, cls, m))
        self._class_index = idx
        self._rva_index = rva_idx
        self._methods_flat = mflat      # 方法名搜索
        self._class_name_set = name_set  # 字段类型跳转判定

    # ---------- 结构查询 ----------
    def get_structure(self):
        """返回分组 → DLL → 分类 的树结构"""
        groups = {}
        for dll_name, dll in self.dlls.items():
            g = dll['group']
            groups.setdefault(g, []).append(dll_name)
        result = []
        for g in GROUP_ORDER:
            if g not in groups:
                continue
            dll_list = []
            for dll_name in sorted(groups[g]):
                dll = self.dlls[dll_name]
                cats = []
                for cat in sorted(dll['cats'], key=lambda c: -len(dll['cats'][c])):
                    cats.append({
                        'name': cat,
                        'desc': CATEGORY_DESC.get(cat, ''),
                        'count': len(dll['cats'][cat]),
                    })
                dll_list.append({
                    'name': dll_name,
                    'class_count': len(dll['classes']),
                    'cats': cats,
                })
            result.append({'name': g, 'icon': GROUP_ICON.get(g, '📦'), 'desc': GROUP_DESC.get(g, ''), 'dlls': dll_list})
        return result

    def get_classes(self, dll_name, cat, query='', offset=0, limit=500):
        """返回某个 DLL 下某分类的类列表 (支持过滤 + 分页)"""
        if dll_name not in self.dlls:
            return {'items': [], 'total': 0}
        cats = self.dlls[dll_name]['cats']
        lst = cats.get(cat, [])
        q = (query or '').lower()
        if q:
            lst = [c for c in lst if q in c['n'].lower() or q in c['cn']]
        total = len(lst)
        return {'items': lst[offset:offset + limit], 'total': total}

    _PRIM_TYPES = {
        'int', 'uint', 'short', 'ushort', 'long', 'ulong', 'byte', 'sbyte',
        'bool', 'float', 'double', 'char', 'string', 'String', 'object',
        'void', 'nint', 'nuint', 'IntPtr', 'UIntPtr', 'Int32', 'UInt32',
        'Int64', 'UInt64', 'Int16', 'UInt16', 'Single', 'Double', 'Boolean',
        'Byte', 'SByte', 'Object',
    }

    @classmethod
    def _clean_type(cls, t):
        """字段类型 → 可跳转的裸类名: 去掉数组/泛型/命名空间前缀"""
        t = (t or '').strip()
        t = re.sub(r'\[.*$', '', t)
        t = t.split('<')[0].strip()
        return t.rsplit('.', 1)[-1] if t else ''

    def get_class_detail(self, dll_name, class_name):
        """返回某个类的完整详情 (字段/属性/方法 + 继承链 + 类型跳转标记)"""
        if dll_name not in self.dlls:
            return None
        for cls in self.dlls[dll_name]['classes']:
            if cls.name == class_name:
                d = cls.detail()
                d['group'] = self.dlls[dll_name]['group']
                d['dll'] = dll_name
                d['chain'] = self._base_chain(dll_name, cls)
                # 字段/属性的自定义类型标记可跳转 (tl = 裸类名)
                if self._class_name_set:
                    for f in d['fields']:
                        cn = self._clean_type(f['t'])
                        if cn and cn not in self._PRIM_TYPES and cn in self._class_name_set:
                            f['tl'] = cn
                    for p in d['props']:
                        cn = self._clean_type(p['t'])
                        if cn and cn not in self._PRIM_TYPES and cn in self._class_name_set:
                            p['tl'] = cn
                return d
        return None

    # ---------- 搜索 ----------
    def search(self, query, limit=300):
        """全局搜索: 类名 / 中文 / 分类名 (只搜类, 兼容旧调用)"""
        if not self._class_index:
            return []
        q = (query or '').lower()
        if not q:
            return []
        results = []
        for lower_name, cn, dll_name, cls in self._class_index:
            if q in lower_name or q in cn or q in cls.cat.lower():
                results.append({
                    'n': cls.name, 'cn': cls.cn, 'cat': cls.cat,
                    'k': cls.kind, 'dll': dll_name,
                    'group': self.dlls[dll_name]['group'],
                    'f': len(cls.fields), 'm': len(cls.methods),
                })
                if len(results) >= limit:
                    break
        return results

    def search_all(self, query, class_limit=100, method_limit=200):
        """类 + 方法 双索引搜索: 类名/中文/分类 + 方法名 (前缀命中优先)"""
        q = (query or '').strip().lower()
        if not q or not self._class_index:
            return {'classes': [], 'methods': [], 'methods_total': 0}
        classes = self.search(query, class_limit)
        methods_pre, methods_inc = [], []
        total = 0
        for lname, dll_name, cls, m in self._methods_flat:
            if q in lname:
                total += 1
                row = {
                    'n': m[0], 'cn': _method_cn(m[0]),
                    'dll': dll_name, 'cls': cls.name, 'cls_cn': cls.cn,
                    'rva': m[1], 'static': m[3], 'ret': m[2], 'params': m[4] or '',
                }
                if lname.startswith(q):
                    if len(methods_pre) < method_limit:
                        methods_pre.append(row)
                elif len(methods_inc) < method_limit:
                    methods_inc.append(row)
        return {'classes': classes, 'methods': methods_pre + methods_inc,
                'methods_total': total}

    # ---------- 基类跳转 ----------
    def find_class(self, class_name):
        """按类名跨 DLL 精确查找 (前端点击基类/字段类型跳转用)"""
        if not self.dlls or not class_name:
            return {'ok': False, 'error': '未指定类名'}
        for dll_name, d in self.dlls.items():
            for cls in d['classes']:
                if cls.name == class_name:
                    return {'ok': True, 'dll': dll_name, 'name': cls.name, 'cn': cls.cn}
        return {'ok': False, 'error': f'未找到类: {class_name} (可能在外部依赖里, dump.cs 不含)'}

    def _base_chain(self, dll_name, cls):
        """向上追溯继承链: [{n, dll, cn, k}, ...] 排列为 祖先 → … → 本类 (末位=本类)"""
        chain = []
        seen = {cls.name}
        cur_dll, cur = dll_name, cls
        for _ in range(8):  # 防环 + 防超深
            chain.append({'n': cur.name, 'dll': cur_dll, 'cn': cur.cn, 'k': cur.kind})
            base = cur.base
            if not base or base in seen:
                break
            seen.add(base)
            hit = None
            for dn, d in self.dlls.items():
                for c in d['classes']:
                    if c.name == base:
                        hit = (dn, c)
                        break
                if hit:
                    break
            if not hit:
                # 基类不在 dump 里 (系统/外部库) — 只展示名字, 不可点
                chain.append({'n': base, 'dll': None, 'cn': '', 'k': ''})
                break
            cur_dll, cur = hit
        chain.reverse()   # 祖先在前, 本类在最后
        return chain

    # ---------- 翻译 ----------
    def translate(self, text):
        """翻译任意标识符 / 短语"""
        if not text:
            return ''
        return TD.translate_identifier(text) or '(未识别, 可在词典中补充)'

    # ---------- 🔤 字符串搜索 (stringliteral.json) ----------
    def get_strings_info(self):
        """加载后探测 dump.cs 同目录的 stringliteral.json"""
        return {'available': bool(self._strings_path), 'path': self._strings_path}

    def _ensure_strings(self):
        """懒加载字符串表 (首次调用时解析, 之后驻留内存)"""
        if self._strings is not None or not self._strings_path:
            return
        arr = []
        try:
            with open(self._strings_path, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
            for it in data if isinstance(data, list) else []:
                s = it.get('data', '') if isinstance(it, dict) else ''
                if s and 1 <= len(s) <= 4096:
                    arr.append((s, it.get('index', 0), it.get('offset', 0)))
        except Exception as e:
            _dbg(f'stringliteral.json 解析失败: {e}')
        self._strings = arr

    def search_strings(self, query, limit=100):
        """搜游戏内字符串: 复活/钻石 等文本 → metadata 偏移 (GG 文本搜索/IDA 字符串定位用)"""
        if not self._strings_path:
            return {'ok': False,
                    'error': ('dump.cs 同目录没有 stringliteral.json。\n'
                              '它是 Il2CppDumper 的输出文件之一, 把它和 dump.cs 放同一目录即可启用。')}
        self._ensure_strings()
        if not self._strings:
            return {'ok': False, 'error': 'stringliteral.json 解析失败或为空'}
        q = (query or '').strip().lower()
        if not q:
            return {'ok': True, 'total': 0, 'count': len(self._strings), 'items': []}
        pre, inc = [], []
        total = 0
        for s, idx, off in self._strings:
            if q in s.lower():
                total += 1
                row = {'data': s, 'index': idx, 'offset': off, 'len': len(s)}
                if s.lower().startswith(q):
                    if len(pre) < limit:
                        pre.append(row)
                elif len(inc) < limit:
                    inc.append(row)
        return {'ok': True, 'total': total, 'count': len(self._strings),
                'items': pre + inc}

    # ---------- 🔍 地址反查 ----------
    @staticmethod
    def _parse_hex(s):
        s = (s or '').strip().lower()
        if s.startswith('0x'):
            s = s[2:]
        return int(s, 16) if s else 0

    def find_by_rva(self, rva, base=None):
        """地址反查: RVA 或 绝对地址(配 base) → 类.方法"""
        if not self._rva_index:
            return {'ok': False, 'error': '尚未加载 dump'}
        try:
            val = self._parse_hex(rva)
        except ValueError:
            return {'ok': False, 'error': '无法解析的地址格式 (支持 0x94E89B4 / 94E89B4)'}
        if base:
            try:
                b = self._parse_hex(base)
            except ValueError:
                return {'ok': False, 'error': 'base 格式无效'}
            if b <= 0:
                return {'ok': False, 'error': 'base 不能为 0'}
            val = val - b
            if val < 0:
                return {'ok': False, 'error': '地址小于基址, 换算出负偏移'}
        if val not in self._rva_index and not base:
            # hex 未命中时尝试按十进制解释 (用户可能粘的是十进制数)
            try:
                dec = int((rva or '').strip(), 10)
                if dec in self._rva_index:
                    val = dec
            except ValueError:
                pass
        hit = self._rva_index.get(val)
        if not hit:
            return {'ok': False, 'error': f'RVA {hex(val)} 不在当前 dump 的方法地址表中'}
        dll_name, cls, m = hit
        return {
            'ok': True, 'rva': hex(val),
            'dll': dll_name, 'class': cls.name, 'class_cn': cls.cn, 'cat': cls.cat,
            'method': m[0], 'method_cn': _method_cn(m[0]),
            'static': m[3], 'ret': m[2], 'params': m[4] or '',
        }

    def find_field_offset(self, class_name, offset):
        """类内偏移反查: 类名 + 偏移 → 字段名 (GG 搜到地址后算偏移用)"""
        dll_name, cls = _find_class(self, class_name)
        if cls is None:
            return {'ok': False, 'error': f'未找到类: {class_name}'}
        try:
            off = self._parse_hex(offset)
        except ValueError:
            return {'ok': False, 'error': '偏移格式无效 (支持 0x38 / 56)'}
        hits = []
        for f in cls.fields:
            if not f[2]:
                continue
            try:
                if int(f[2], 16) == off:
                    hits.append({'name': f[0], 'type': f[1], 'offset': f[2]})
            except (ValueError, TypeError):
                pass
        if not hits:
            return {'ok': False, 'error': f'{cls.name} 中没有偏移为 {hex(off)} 的字段',
                    'class': cls.name, 'fields': len(cls.fields)}
        return {'ok': True, 'class': cls.name, 'class_cn': cls.cn, 'dll': dll_name,
                'offset': hex(off), 'fields': hits}

    def gen_hook(self, dll_name, class_name, method_name, full=False, mode=None):
        """生成指定方法的 Dobby Hook 代码。
        mode: 'mini'(默认精简) / 'full'(完整可编译示例) / 'byname'(按名解析, 更新免改码)"""
        if dll_name not in self.dlls:
            return ''
        for cls in self.dlls[dll_name]['classes']:
            if cls.name == class_name:
                for m in cls.methods:
                    if m[0] == method_name:
                        md = {'name': m[0], 'cn': _method_cn(m[0]), 'rva': m[1],
                              'ret': m[2], 'static': m[3], 'params': m[4],
                              'dll': dll_name}
                        if mode == 'byname':
                            return gen_hook_byname_code(class_name, md, ns=cls.ns)
                        return gen_hook_code(class_name, md, full=full)
        return ''

    # ---------- 导出 ----------
    def export_translated(self, out_path):
        """导出带中文注释的 dump.cs (复用 translate_dump 逻辑)"""
        if not self.dump_path:
            return {'ok': False, 'error': '尚未加载文件'}
        try:
            stats, elapsed = TD.process(self.dump_path, out_path, None)
            return {'ok': True, 'path': out_path, 'stats': dict(stats), 'elapsed': round(elapsed, 1)}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ---------- 最近打开 ----------
    def _recent_path(self):
        # 打包后写到 exe 所在目录(持久化), 源码运行时写到脚本目录
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = _THIS_DIR
        return os.path.join(base, 'recent.json')

    def _load_recent(self):
        try:
            with open(self._recent_path(), 'r', encoding='utf-8') as f:
                lst = json.load(f)
        except Exception:
            return []
        # 路径规范化去重 (统一分隔符与大小写, 避免同一文件记两条)
        seen = set()
        out = []
        for p in lst:
            if not isinstance(p, str):
                continue
            key = p.replace('/', '\\').lower()
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    def _save_recent(self, path):
        key = path.replace('/', '\\').lower()
        recent = [p for p in self._load_recent()
                  if p.replace('/', '\\').lower() != key]
        recent.insert(0, path)
        recent = recent[:10]
        try:
            with open(self._recent_path(), 'w', encoding='utf-8') as f:
                json.dump(recent, f, ensure_ascii=False)
        except Exception:
            pass

    def get_recent(self):
        _dbg('api.get_recent 被调用 (桥接健康信号)')
        return [p for p in self._load_recent() if os.path.isfile(p)]

    def get_auto_load_path(self):
        """前端 ready 后主动调用, 获取命令行传入的待加载文件路径"""
        _dbg(f'api.get_auto_load_path -> {self.auto_load_path!r} (桥接健康信号)')
        return self.auto_load_path

    # ==================== 📱 ADB 调试 ====================
    def _adb_cfg_path(self):
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = _THIS_DIR
        return os.path.join(base, '_adb_config.json')

    def _adb_load_cfg(self):
        try:
            with open(self._adb_cfg_path(), 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            self._adb_serial = cfg.get('serial')
            if cfg.get('adb_path'):
                AK.set_adb_path(cfg['adb_path'])
            # 🥷 隐身实例跨会话恢复 (status 会自动校验存活性, 死了就清理)
            fh = cfg.get('frida_hidden')
            if isinstance(fh, dict) and fh.get('name') and fh.get('port'):
                self._adb_frida_hidden = {'name': fh['name'], 'port': int(fh['port']),
                                          'remote': f"/data/local/tmp/{fh['name']}"}
            # 最近一次调试的 Hook (一键注入用: pkg/cls/m)
            lh = cfg.get('frida_last_hook')
            if isinstance(lh, dict):
                self._adb_frida_last_hook = {'pkg': str(lh.get('pkg') or ''),
                                             'cls': str(lh.get('cls') or ''),
                                             'm': str(lh.get('m') or '')}
        except Exception:
            pass

    def _adb_save_cfg(self, clear_hidden=False):
        try:
            # 先读旧配置合并 — 多进程(GUI/MCP/测试)共用一份 config,
            # 全量覆盖会互踩: 别的进程写的隐身实例记录被本进程 None 冲掉
            old = {}
            try:
                with open(self._adb_cfg_path(), 'r', encoding='utf-8') as f:
                    old = json.load(f)
            except Exception:
                old = {}
            if self._adb_frida_hidden:
                fh = {'name': self._adb_frida_hidden.get('name'),
                      'port': self._adb_frida_hidden.get('port')}
            elif clear_hidden:
                fh = None                    # 显式清除 (stop_server/status 发现死实例)
            else:
                fh = old.get('frida_hidden') # 本进程不知道 → 保留旧值
            lh = getattr(self, '_adb_frida_last_hook', None) or old.get('frida_last_hook')
            with open(self._adb_cfg_path(), 'w', encoding='utf-8') as f:
                json.dump({'serial': self._adb_serial,
                           'adb_path': AK._USER_ADB if AK else None,
                           'frida_hidden': fh,
                           'frida_last_hook': lh},
                          f, ensure_ascii=False)
        except Exception:
            pass

    def _adb_dev(self):
        """当前设备 serial: 选中的 → 唯一在线的 → None"""
        if not AK:
            raise AK.AdbError('ADB 模块不可用') if AK else Exception('ADB 模块不可用')
        if self._adb_serial:
            return self._adb_serial
        devs = AK.list_devices()
        online = [d for d in devs if d['state'] == 'device']
        if len(online) == 1:
            self._adb_serial = online[0]['serial']
            return self._adb_serial
        return None

    def _adb_need(self):
        """取当前设备, 没有则报错字典"""
        serial = self._adb_dev()
        if not serial:
            return None, {'ok': False, 'error': '没有可用设备 — 请先在「📱 ADB」面板连接并选择设备'}
        return serial, None

    def adb_state(self):
        """ADB 面板初始化: adb 可用性 + frida CLI + 设备列表 + 选中设备"""
        _dbg('api.adb_state 被调用')
        if not AK:
            return {'ok': False, 'error': 'adb_kit 模块加载失败 (安装损坏, 重启试试)'}
        adb_path = AK.find_adb()
        frida = AK.find_frida_cli()
        try:
            devs = AK.list_devices()
        except Exception as e:
            return {'ok': False, 'error': str(e), 'adb_path': adb_path, 'frida': frida}
        # 校正选中设备 (可能已离线)
        online = {d['serial'] for d in devs if d['state'] == 'device'}
        if self._adb_serial and self._adb_serial not in online:
            self._adb_serial = None
        if not self._adb_serial and len(online) == 1:
            self._adb_serial = next(iter(online))
        return {
            'ok': True,
            'adb_path': adb_path,
            'adb_ver': AK.adb_version() if adb_path else '',
            'frida': frida,
            'devices': devs,
            'serial': self._adb_serial,
        }

    def adb_devices(self):
        """刷新设备列表 (带详情: 型号/安卓/root)"""
        if not AK:
            return {'ok': False, 'error': 'ADB 模块不可用'}
        try:
            devs = AK.list_devices()
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        out = []
        for d in devs:
            info = {}
            if d['state'] == 'device':
                try:
                    info = AK.device_info(d['serial'])
                except Exception:
                    info = {'model': d.get('model', ''), 'root': False}
            out.append({'serial': d['serial'], 'state': d['state'],
                        'model': info.get('model') or d.get('model', ''),
                        'android': info.get('android', ''),
                        'abi': info.get('abi', ''),
                        'root': info.get('root', False),
                        'disk_free': info.get('disk_free', '')})
        return {'ok': True, 'devices': out, 'serial': self._adb_serial}

    def adb_connect(self, addr):
        """WiFi 连接 (ip 或 ip:port)"""
        if not AK:
            return {'ok': False, 'error': 'ADB 模块不可用'}
        try:
            r = AK.connect_wifi((addr or '').strip())
            return r
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def adb_select(self, serial):
        """选中当前操作设备"""
        self._adb_serial = serial or None
        self._adb_save_cfg()
        return {'ok': True, 'serial': self._adb_serial}

    def adb_set_path(self):
        """弹框手动指定 adb.exe"""
        if not AK:
            return {'ok': False, 'error': 'ADB 模块不可用'}
        path = _win32_dialog(save_mode=False, title='选择 adb.exe',
                             file_filter=[('adb 程序', 'adb.exe'), ('所有文件', '*.*')])
        if not path:
            return {'ok': False, 'error': '已取消'}
        AK.set_adb_path(path)
        self._adb_save_cfg()
        return {'ok': True, 'path': path}

    def adb_apps(self, refresh=False):
        """已安装应用列表 (缓存, refresh=True 强刷)"""
        serial, err = self._adb_need()
        if err:
            return err
        if not refresh and self._adb_apps and self._adb_apps.get('serial') == serial:
            return {'ok': True, 'serial': serial, 'apps': self._adb_apps['apps']}
        try:
            apps = AK.list_packages(serial, third_only=True)
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        self._adb_apps = {'serial': serial, 'apps': apps}
        return {'ok': True, 'serial': serial, 'apps': apps}

    def adb_check_il2cpp(self, pkg):
        """检测某应用是否 Unity/il2cpp 游戏"""
        serial, err = self._adb_need()
        if err:
            return err
        try:
            ck = AK.check_il2cpp(serial, pkg)
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        return {'ok': True, 'pkg': pkg, 'il2cpp': ck['il2cpp'], 'arch': ck['arch'],
                'so_entry': ck['so_entry'], 'metadata_entry': ck['metadata_entry']}

    def adb_scan_il2cpp(self):
        """后台批量扫描所有第三方应用中的 Unity 游戏 (任务模式, 前端轮询 adb_task_state)"""
        serial, err = self._adb_need()
        if err:
            return err
        if self._adb_task.get('state') == 'running':
            return {'ok': False, 'error': '已有任务在跑 (扫描/提取), 请等完成'}
        self._adb_task = {'kind': 'scan', 'state': 'running',
                          'msg': '正在扫描…', 'result': None}

        def _worker():
            try:
                def prog(done, total, _pkg):
                    self._adb_task['msg'] = f'正在扫描 {done}/{total} 个应用…'
                hits = AK.batch_check_il2cpp(serial, progress_cb=prog)
                self._adb_task = {'kind': 'scan', 'state': 'done',
                                  'msg': f'扫描完成: 发现 {len(hits)} 个 Unity/il2cpp 游戏',
                                  'result': hits}
            except Exception as e:
                self._adb_task = {'kind': 'scan', 'state': 'error',
                                  'msg': f'扫描失败: {e}', 'result': None}
        threading.Thread(target=_worker, daemon=True).start()
        return {'ok': True, 'started': True}

    def adb_pull_il2cpp(self, pkg, dest_dir=None):
        """提取某游戏的 il2cpp 材料 (libil2cpp.so + global-metadata.dat) 到本地.
        dest_dir=None 时弹保存框选位置; 后台任务模式, 前端轮询 adb_task_state"""
        serial, err = self._adb_need()
        if err:
            return err
        if self._adb_task.get('state') == 'running':
            return {'ok': False, 'error': '已有任务在跑, 请等完成'}
        if not dest_dir:
            p = _win32_dialog(save_mode=True, title='选择提取位置 (文件夹)',
                              default_name=f'{pkg}-il2cpp材料')
            if not p:
                return {'ok': False, 'error': '已取消'}
            dest_dir = os.path.dirname(p)
        self._adb_task = {'kind': 'pull', 'state': 'running',
                          'msg': '准备提取…', 'result': None}

        def _worker():
            try:
                r = AK.pull_il2cpp(serial, pkg, dest_dir,
                                   progress_cb=lambda m: self._adb_task.__setitem__('msg', m))
                if r.get('ok'):
                    self._adb_task = {'kind': 'pull', 'state': 'done',
                                      'msg': f'提取完成 → {dest_dir}', 'result': r}
                else:
                    self._adb_task = {'kind': 'pull', 'state': 'error',
                                      'msg': r.get('error', '提取失败'), 'result': None}
            except Exception as e:
                self._adb_task = {'kind': 'pull', 'state': 'error',
                                  'msg': f'提取失败: {e}', 'result': None}
        threading.Thread(target=_worker, daemon=True).start()
        return {'ok': True, 'started': True, 'dest_dir': dest_dir}

    def adb_task_state(self):
        """后台任务 (扫描/提取) 状态轮询"""
        return dict(self._adb_task)

    def adb_push(self, local_path=None, remote=None):
        """推送文件到手机 (默认 /sdcard/Download/). local_path=None 时弹选择框"""
        serial, err = self._adb_need()
        if err:
            return err
        if not local_path:
            local_path = _win32_dialog(save_mode=False, title='选择要推送的文件',
                                       file_filter=[('GG 脚本', '*.lua'), ('Frida 脚本', '*.js'),
                                                    ('所有文件', '*.*')])
            if not local_path:
                return {'ok': False, 'error': '已取消'}
        try:
            remote_path = AK.push_file(serial, local_path, remote)
            return {'ok': True, 'local': local_path, 'remote': remote_path}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def adb_screenshot(self):
        """手机截屏 → 弹保存框 → 存 png"""
        serial, err = self._adb_need()
        if err:
            return err
        path = _win32_dialog(save_mode=True, title='截屏保存为',
                             default_name='screenshot.png',
                             file_filter=[('PNG 图片', '*.png'), ('所有文件', '*.*')])
        if not path:
            return {'ok': False, 'error': '已取消'}
        try:
            AK.screenshot(serial, path)
            return {'ok': True, 'path': path}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def adb_logcat(self, keyword='', lines=300):
        """logcat 快照"""
        serial, err = self._adb_need()
        if err:
            return err
        try:
            out = AK.logcat(serial, keyword=(keyword or '').strip() or None,
                            lines=int(lines or 300))
            return {'ok': True, 'text': out}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def adb_shell(self, cmd, root=False):
        """执行一条手机 shell 命令"""
        serial, err = self._adb_need()
        if err:
            return err
        try:
            out = AK.shell(cmd or 'echo', device=serial, timeout=60, root=bool(root))
            return {'ok': True, 'text': out or '(无输出)'}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ---------- Frida ----------
    def adb_frida_status(self):
        """frida CLI + 手机端 frida-server 状态 (含隐身模式)"""
        serial, err = self._adb_need()
        if err:
            return err
        try:
            st = AK.frida_server_status(serial)
            # 隐身实例状态
            hid = None
            if self._adb_frida_hidden:
                h = self._adb_frida_hidden
                try:
                    alive = AK.shell(f"pidof {h['name']}", device=serial,
                                     timeout=10).strip().isdigit()
                except Exception:
                    alive = False
                if alive:
                    hid = {'port': h['port'], 'name': h['name']}
                else:
                    # 进程没了 (手机重启/被杀) — 删残留文件 + 清状态 + 清持久化
                    try:
                        AK.shell(f"rm -f {h.get('remote')} 2>/dev/null", device=serial,
                                 timeout=10, root=True)
                    except Exception:
                        pass
                    self._adb_frida_hidden = None
                    self._adb_save_cfg(clear_hidden=True)
            return {'ok': True, 'cli': AK.find_frida_cli(),
                    'server': st, 'serial': serial, 'hidden': hid,
                    'running_script': bool(self._adb_frida_runner and self._adb_frida_runner.alive())}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def adb_frida_start(self, local_bin=None, hidden=False):
        """推送并启动手机端 frida-server (local_bin=None: 自动找本地/弹框选择)
        hidden=True: 🥷 隐身模式 — 特征串 patch + 随机文件名 + 127.0.0.1:随机端口 + adb forward"""
        serial, err = self._adb_need()
        if err:
            return err
        if not local_bin:
            # 工具目录优先 (用户常把 frida-server 放 exe 旁边)
            try:
                base = os.path.dirname(self._adb_cfg_path())
                for name in sorted(os.listdir(base)):
                    if name.lower().startswith('frida-server') or name.lower().startswith('fs-'):
                        p = os.path.join(base, name)
                        if os.path.isfile(p):
                            local_bin = p
                            break
            except Exception:
                pass
        if not local_bin:
            local_bin = AK.find_local_frida_server()
            if not local_bin:
                local_bin = _win32_dialog(save_mode=False, title='选择 frida-server 文件 (arm64)',
                                          file_filter=[('frida-server', 'frida-server*'),
                                                       ('所有文件', '*.*')])
                if not local_bin:
                    return {'ok': False, 'error': '已取消'}
        try:
            if hidden:
                # 清掉上一个隐身实例 (换新的随机名/端口/指纹)
                if self._adb_frida_hidden:
                    h = self._adb_frida_hidden
                    try:
                        AK.frida_stop_hidden(serial, h.get('name'), h.get('port'))
                    except Exception:
                        pass
                    self._adb_frida_hidden = None
                r = AK.frida_push_start_hidden(serial, local_bin)
                if r.get('ok'):
                    self._adb_frida_hidden = {'port': r['port'], 'remote': r['remote'],
                                              'name': r['name'], 'stats': r.get('stats')}
                    self._adb_save_cfg()   # 持久化: 跨会话清理孤儿实例
                return r
            r = AK.frida_push_start(serial, local_bin)
            return r
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def adb_frida_stop_server(self):
        """停止手机端 frida-server (含隐身实例清理)"""
        serial, err = self._adb_need()
        if err:
            return err
        try:
            if self._adb_frida_hidden:
                h = self._adb_frida_hidden
                AK.frida_stop_hidden(serial, h.get('name'), h.get('port'))
                self._adb_frida_hidden = None
                self._adb_save_cfg(clear_hidden=True)
                return {'ok': True, 'hidden_stopped': True}
            return {'ok': AK.frida_stop_server(serial)}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _frida_hidden_host(self, serial):
        """隐身模式返回 '127.0.0.1:port' 并自愈 forward 隧道。
        adb server 重启 (kill-server/换数据线/系统休眠) 会清掉所有映射,
        每次走隧道前都重新 forward (幂等), 否则 frida 连不上报
        'Failed to spawn: unable to connect to remote frida-server'。"""
        if not self._adb_frida_hidden:
            return None
        port = self._adb_frida_hidden['port']
        try:
            AK.run_adb(['forward', f'tcp:{port}', f'tcp:{port}'], device=serial, timeout=10)
        except Exception:
            pass   # 自愈失败不阻塞 — 后续连接错误会正常上报
        return f'127.0.0.1:{port}'

    def _frida_pick_target(self, serial, pkg, spawn):
        """判定 attach/spawn 并解析注入目标。
        ⚡ attach 一律解析为 PID: 腾讯系游戏反作弊会把主进程从 frida 枚举里藏掉
        (pidof 看得到, frida-ps 找不到), 按名字 attach 报
        'unable to find process with name' — 按 PID 直注绕过枚举, 实测可注入。
        spawn 前预检包是否安装, 没装给友好报错 (否则 frida 报天书)。
        返回 (mode, spawn_v, target, pid, err)。"""
        if not re.match(r'^[A-Za-z0-9_.]+$', pkg or ''):
            return None, None, None, '', f'包名不合法: {pkg} (应为 com.xxx.yyy 格式)'
        pid = ''
        try:
            p = AK.shell('pidof ' + pkg, device=serial, timeout=10).strip()
            if p and p.split()[0].isdigit():
                pid = p.split()[0]
        except Exception:
            pid = ''
        if spawn is None:
            spawn_v = not pid          # 在跑 → attach, 没跑 → spawn 冷启动
        else:
            spawn_v = bool(spawn)
        if spawn_v:
            try:
                if not AK.shell(f'pm path {pkg} 2>/dev/null', device=serial, timeout=10).strip():
                    return None, None, None, '', (f'手机上没装 {pkg} — 包名可能填错了'
                                                  ' (「应用与材料」页可查所有包名)')
            except Exception:
                pass
            return 'spawn', True, pkg, pid, None
        if not pid:
            return None, None, None, '', (f'{pkg} 没在运行 — 先打开游戏再 attach,'
                                          ' 或勾「spawn 冷启动」')
        return 'attach', False, pid, pid, None

    def adb_frida_ps(self):
        """frida-ps 列手机进程 (隐身模式自动走 -H 隧道)"""
        serial, err = self._adb_need()
        if err:
            return err
        try:
            if self._adb_frida_hidden:
                self._frida_hidden_host(serial)   # 自愈 forward 隧道
                return {'ok': True, 'text': AK.frida_ps_host(self._adb_frida_hidden['port']),
                        'hidden': True}
            return {'ok': True, 'text': AK.frida_ps(serial)}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def adb_frida_run(self, pkg, script_path=None, spawn=True):
        """后台运行 Frida 脚本 (输出用 adb_frida_read 轮询)"""
        serial, err = self._adb_need()
        if err:
            return err
        if not pkg:
            return {'ok': False, 'error': '缺少包名'}
        if self._adb_frida_runner and self._adb_frida_runner.alive():
            return {'ok': False, 'error': '已有一个脚本在运行, 请先停止'}
        if not script_path:
            script_path = _win32_dialog(save_mode=False, title='选择 Frida JS 脚本',
                                        file_filter=[('Frida 脚本', '*.js'), ('所有文件', '*.*')])
            if not script_path:
                return {'ok': False, 'error': '已取消'}
        try:
            if self._adb_frida_runner is None:
                self._adb_frida_runner = AK.FridaRunner()
            mode, spawn_v, target, pid, terr = self._frida_pick_target(serial, pkg, spawn)
            if terr:
                return {'ok': False, 'error': terr}
            host = self._frida_hidden_host(serial)
            self._adb_frida_runner.start(serial, target, script_path, spawn=spawn_v, host=host)
            return {'ok': True, 'mode': mode, 'pkg': pkg, 'pid': pid or None,
                    'cmd': self._adb_frida_runner.cmd_str, 'hidden': bool(host)}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def adb_frida_run_code(self, pkg, code, spawn=None, cls=None, m=None):
        """直接运行一段 Frida JS 代码 (Hook 弹窗「ADB 调试」用, 免落文件免选文件)。
        spawn=None 自动判断: 手机上进程已在跑 → attach, 否则 spawn 冷启动。
        cls/m: 本次调试的类名/方法名 (记住供 ADB 面板「⚡ 注入」一键重跑)。
        已有脚本在跑会先停掉旧的 (调试场景直接替换)。"""
        serial, err = self._adb_need()
        if err:
            return err
        if not pkg:
            return {'ok': False, 'error': '缺少包名'}
        if not (code or '').strip():
            return {'ok': False, 'error': '脚本代码为空'}
        r = self._adb_frida_runner
        if r and r.alive():
            r.stop()   # 调试场景: 直接替换旧脚本
        # 代码落盘到 exe 同目录 (稳定路径, 每次覆盖)
        base = os.path.dirname(self._adb_cfg_path())
        script_path = os.path.join(base, '_hook_debug.js')
        try:
            with open(script_path, 'w', encoding='utf-8') as f:
                f.write(code)
        except Exception as e:
            return {'ok': False, 'error': f'写临时脚本失败: {e}'}
        # 判定 attach/spawn + 解析目标 (attach 用 PID 绕反作弊藏名; spawn 预检包安装)
        mode, spawn_v, target, pid, err = self._frida_pick_target(serial, pkg, spawn)
        if err:
            return {'ok': False, 'error': err}
        # server 状态预检: 普通模式下手机端没跑 frida-server → 友好报错 (否则 frida 报天书)
        if not self._adb_frida_hidden:
            try:
                st = AK.frida_server_status(serial)
                if not st.get('running'):
                    return {'ok': False, 'error': '手机端 frida-server 没在跑 — 先点「🥷 隐身启动」'
                                                  ' (或「🚀 推送并启动 server」)'}
            except Exception:
                pass
        try:
            if self._adb_frida_runner is None:
                self._adb_frida_runner = AK.FridaRunner()
            host = self._frida_hidden_host(serial)
            self._adb_frida_runner.start(serial, target, script_path, spawn=spawn_v, host=host)
            # 记住本次 Hook (⚡ 注入一键重跑用; cls/m 省略时保留上次记录)
            h = dict(getattr(self, '_adb_frida_last_hook', None) or {})
            h['pkg'] = pkg
            if cls:
                h['cls'] = str(cls)
            if m:
                h['m'] = str(m)
            self._adb_frida_last_hook = h
            self._adb_save_cfg()
            return {'ok': True, 'mode': mode, 'pkg': pkg, 'pid': pid or None,
                    'cmd': self._adb_frida_runner.cmd_str, 'hidden': bool(host)}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def adb_frida_last_hook(self):
        """最近一次调试的 Hook 信息 + 落盘脚本内容 (ADB 面板「⚡ 注入」用)。
        脚本即 run_code 覆盖写的 _hook_debug.js — 工具重启后依然可一键注入。"""
        h = dict(getattr(self, '_adb_frida_last_hook', None) or {})
        code = ''
        try:
            p = os.path.join(os.path.dirname(self._adb_cfg_path()), '_hook_debug.js')
            if os.path.isfile(p):
                with open(p, 'r', encoding='utf-8') as f:
                    code = f.read()
        except Exception:
            code = ''
        has = bool(h.get('pkg')) and bool(code.strip())
        return {'ok': True, 'has': has, 'pkg': h.get('pkg', ''),
                'cls': h.get('cls', ''), 'm': h.get('m', ''), 'code': code}

    def adb_frida_read(self, after=0):
        """读取 Frida 脚本运行的新输出 (after=上次 total)"""
        r = self._adb_frida_runner
        if not r:
            return {'ok': True, 'lines': [], 'total': 0, 'alive': False}
        lines, total = r.read_new(int(after or 0))
        return {'ok': True, 'lines': lines, 'total': total, 'alive': r.alive()}

    def adb_frida_stop_run(self):
        """停止 Frida 脚本运行"""
        if self._adb_frida_runner:
            self._adb_frida_runner.stop()
        return {'ok': True}

    def adb_get_serial(self):
        """当前选中设备 serial (供 GG 弹窗等处判断 ADB 可用)"""
        try:
            return {'ok': True, 'serial': self._adb_dev()}
        except Exception:
            return {'ok': True, 'serial': None}

    def pick_frida_script(self):
        """选择 Frida JS 脚本文件 (ADB 面板用)"""
        path = _win32_dialog(save_mode=False, title='选择 Frida JS 脚本',
                             file_filter=[('Frida 脚本', '*.js'), ('所有文件', '*.*')])
        if not path:
            return {'ok': False, 'error': '已取消'}
        return {'ok': True, 'path': path}

    # ==================== ⭐ 收藏 ====================
    def _stars_path(self):
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = _THIS_DIR
        return os.path.join(base, '_stars.json')

    def _load_stars(self):
        try:
            with open(self._stars_path(), 'r', encoding='utf-8') as f:
                lst = json.load(f)
            return [s for s in lst if isinstance(s, dict) and s.get('cls')]
        except Exception:
            return []

    def _save_stars(self):
        try:
            with open(self._stars_path(), 'w', encoding='utf-8') as f:
                json.dump(self._stars, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def toggle_star(self, dll, cls, m=None):
        """切换收藏 (m=None 收藏类, 否则收藏方法), 返回新状态"""
        m = m or ''
        for i, s in enumerate(self._stars):
            if s.get('dll') == dll and s.get('cls') == cls and s.get('m', '') == m:
                del self._stars[i]
                self._save_stars()
                return {'ok': True, 'starred': False, 'total': len(self._stars)}
        self._stars.append({'dll': dll, 'cls': cls, 'm': m, 'ts': int(time.time())})
        self._stars.sort(key=lambda s: (s['dll'], s['cls'], s.get('m', '')))
        self._save_stars()
        return {'ok': True, 'starred': True, 'total': len(self._stars)}

    def is_starred(self, dll, cls, m=None):
        m = m or ''
        return any(s.get('dll') == dll and s.get('cls') == cls and s.get('m', '') == m
                   for s in self._stars)

    def get_stars(self):
        return list(self._stars)

    # ==================== ⚡ Frida 脚本 ====================
    def _guess_package(self):
        """从 dump 文件名猜包名: com.example.game-dump.cs → com.example.game"""
        if not self.dump_path:
            return ''
        b = os.path.basename(self.dump_path)
        for suf in ('-dump.cs', '-dump.txt', '.cs', '.txt'):
            if b.endswith(suf):
                b = b[:-len(suf)]
                break
        return b if re.match(r'^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$', b or '') else ''

    def gen_frida(self, dll_name, class_name, method_name):
        """生成指定方法的 Frida JS 脚本"""
        if dll_name not in self.dlls:
            return ''
        for cls in self.dlls[dll_name]['classes']:
            if cls.name == class_name:
                for m in cls.methods:
                    if m[0] == method_name:
                        return gen_frida_code(class_name, {
                            'name': m[0], 'cn': _method_cn(m[0]), 'rva': m[1],
                            'ret': m[2], 'static': m[3], 'params': m[4],
                            'dll': dll_name,
                        }, package=self._guess_package())
        return ''

    # ---------- ⚡ Frida 批量追踪 ----------
    def _trace_items_for_class(self, dll_name, class_name):
        return [(dll_name, class_name, m)
                for cls in self.dlls.get(dll_name, {}).get('classes', [])
                if cls.name == class_name for m in cls.methods]

    def _trace_items_for_stars(self):
        items = []
        for s in self._stars:
            dll = s.get('dll')
            if dll not in self.dlls:
                continue
            if s.get('m'):
                for cls in self.dlls[dll]['classes']:
                    if cls.name == s['cls']:
                        for m in cls.methods:
                            if m[0] == s['m']:
                                items.append((dll, s['cls'], m))
                        break
            else:
                items.extend(self._trace_items_for_class(dll, s['cls']))
        return items

    def gen_frida_trace_class(self, dll_name, class_name):
        """整类全量方法追踪脚本"""
        items = self._trace_items_for_class(dll_name, class_name)
        if not items:
            return ''
        return gen_frida_trace_code(items, package=self._guess_package(),
                                    enum_names=self._gg_kind_names()[0])

    def gen_frida_trace_stars(self):
        """收藏项(方法精确定位 / 整类全量)追踪脚本"""
        items = self._trace_items_for_stars()
        if not items:
            return ''
        return gen_frida_trace_code(items, package=self._guess_package(),
                                    enum_names=self._gg_kind_names()[0])

    def export_frida_trace(self, dll_name=None, class_name=None, scope='class'):
        """弹保存框导出追踪脚本 .js"""
        if scope == 'stars':
            code = self.gen_frida_trace_stars()
            default = 'trace_starred.js'
        else:
            code = self.gen_frida_trace_class(dll_name, class_name)
            default = f'trace_{class_name or "x"}.js'
        if not code:
            return {'ok': False, 'error': '没有可追踪的方法 (需带 RVA)'}
        path = _win32_dialog(save_mode=True, title='导出 Frida 追踪脚本',
                             default_name=default)
        if not path:
            return {'ok': False, 'error': '已取消'}
        with open(path, 'w', encoding='utf-8') as f:
            f.write(code)
        return {'ok': True, 'path': path, 'lines': len(code.splitlines())}

    # ==================== 🧭 IDA 命名脚本 ====================
    def _ida_items_for_class(self, dll_name, class_name):
        items = []
        for cls in self.dlls.get(dll_name, {}).get('classes', []):
            if cls.name == class_name:
                for m in cls.methods:
                    items.append((dll_name, cls.name, m[0], m[1], _method_cn(m[0])))
        return items

    def _ida_items_for_stars(self):
        items = []
        for s in self._stars:
            dll = s.get('dll')
            if dll not in self.dlls:
                continue
            if s.get('m'):
                for cls in self.dlls[dll]['classes']:
                    if cls.name == s['cls']:
                        for m in cls.methods:
                            if m[0] == s['m']:
                                items.append((dll, s['cls'], m[0], m[1], _method_cn(m[0])))
                        break
            else:
                items.extend(self._ida_items_for_class(dll, s['cls']))
        return items

    def gen_ida_script(self, dll_name=None, class_name=None, scope='class'):
        """生成 IDA 命名脚本文本. scope='class' 单类 / 'stars' 收藏"""
        if scope == 'stars':
            items = self._ida_items_for_stars()
            title = '收藏项导出 (%d 项)' % len(self._stars)
        else:
            if not class_name or dll_name not in self.dlls:
                return ''
            items = self._ida_items_for_class(dll_name, class_name)
            title = f'{class_name} 类全部方法 ({len(items)})'
        if not items:
            return ''
        return gen_ida_script_code(items, title)

    def export_ida_script(self, dll_name, class_name):
        """弹保存框导出单类 IDA 脚本"""
        code = self.gen_ida_script(dll_name, class_name, scope='class')
        if not code:
            return {'ok': False, 'error': '该类没有可导出的方法 (需有 RVA)'}
        path = _win32_dialog(save_mode=True, title='导出 IDA 命名脚本',
                             default_name=f'ida_{class_name}.py')
        if not path:
            return {'ok': False, 'error': '已取消'}
        with open(path, 'w', encoding='utf-8') as f:
            f.write(code)
        return {'ok': True, 'path': path}

    def export_ida_script_stars(self):
        """弹保存框导出收藏项 IDA 脚本"""
        code = self.gen_ida_script(scope='stars')
        if not code:
            return {'ok': False, 'error': '收藏里没有可导出的方法'}
        path = _win32_dialog(save_mode=True, title='导出收藏 IDA 命名脚本',
                             default_name='ida_starred.py')
        if not path:
            return {'ok': False, 'error': '已取消'}
        with open(path, 'w', encoding='utf-8') as f:
            f.write(code)
        return {'ok': True, 'path': path}

    # ==================== 🎮 GG 脚本生成 ====================
    _kind_cache = None

    def _gg_kind_names(self):
        """收集 dump 里所有 enum / 自定义 struct 的类型名 (供 GG 类型映射用)
        注意: cls.kind 已是中文 (KIND_CN 翻译后), 必须比中文"""
        if self._kind_cache is None:
            enums, structs = set(), set()
            for dll in self.dlls.values():
                for cls in dll['classes']:
                    if cls.kind == '枚举':
                        enums.add(cls.name)
                    elif cls.kind == '结构体':
                        structs.add(cls.name)
            self._kind_cache = (frozenset(enums), frozenset(structs))
        return self._kind_cache

    def _find_cls(self, dll_name, class_name):
        for cls in self.dlls[dll_name]['classes']:
            if cls.name == class_name:
                return cls
        return None

    def gen_gg_script(self, dll_name, class_name):
        """生成该类的 GG Lua 读写骨架文本"""
        if dll_name not in self.dlls:
            return ''
        cls = self._find_cls(dll_name, class_name)
        if cls is None:
            return ''
        enums, structs = self._gg_kind_names()
        return gen_gg_script_code(dll_name, cls, self.dump_path or '',
                                  enum_names=enums, struct_names=structs)

    def export_gg_script(self, dll_name, class_name):
        """弹保存框导出 GG Lua 脚本"""
        code = self.gen_gg_script(dll_name, class_name)
        if not code:
            return {'ok': False, 'error': '生成失败 (类不存在?)'}
        path = _win32_dialog(save_mode=True, title='导出 GG Lua 脚本',
                             default_name=f'gg_{class_name}.lua')
        if not path:
            return {'ok': False, 'error': '已取消'}
        with open(path, 'w', encoding='utf-8') as f:
            f.write(code)
        return {'ok': True, 'path': path}

    # ==================== 🔍 通用探测器 ====================
    def gen_gg_probe(self):
        """生成通用 il2cpp 探测器脚本文本 (含收藏类名单)"""
        stars = [(s['class'], s.get('class_cn', '')) for s in self._stars if s.get('class')]
        dump_name = os.path.basename(self.dump_path) if self.dump_path else ''
        return gen_gg_probe_code(star_classes=stars, dump_name=dump_name)

    def export_gg_probe(self):
        """弹保存框导出通用探测器 GG Lua 脚本"""
        if not self.dlls:
            return {'ok': False, 'error': '请先加载 dump.cs (探测器会嵌入收藏类名单)'}
        code = self.gen_gg_probe()
        path = _win32_dialog(save_mode=True, title='导出 il2cpp 探测器 (通用)',
                             default_name='gg_probe.lua')
        if not path:
            return {'ok': False, 'error': '已取消'}
        with open(path, 'w', encoding='utf-8') as f:
            f.write(code)
        return {'ok': True, 'path': path}

    # ==================== 🚀 Hook 工程脚手架 ====================
    def gen_hook_project(self, dll_name=None, class_name=None, scope='class'):
        """生成 NDK/CMake 完整工程 zip (bytes, 项目名)。scope='class' 单类 / 'stars' 收藏"""
        if scope == 'stars':
            items = self._ida_items_for_stars()
            scope_name = '收藏项'
        else:
            items = self._ida_items_for_class(dll_name, class_name)
            scope_name = class_name or 'targets'
        if not items:
            return None, ''
        # 找示例方法 (第一个带 RVA 的)
        sample = None
        for dll, cls_name, m, rva, cn in items:
            if rva:
                for c in self.dlls.get(dll, {}).get('classes', []):
                    if c.name == cls_name:
                        for mm in c.methods:
                            if mm[0] == m:
                                sample = {'class': cls_name, 'name': m, 'rva': rva,
                                          'ret': mm[2], 'static': mm[3], 'params': mm[4],
                                          'cn': cn}
                                break
                    if sample:
                        break
            if sample:
                break
        meta = {
            'game': os.path.basename(self.dump_path) if self.dump_path else '?',
            'scope': scope_name,
        }
        data, proj_name = build_hook_project_zip(items, meta, sample=sample)
        return data, proj_name

    def export_hook_project(self, dll_name, class_name):
        """弹保存框导出单类 Hook 工程 zip"""
        data, proj = self.gen_hook_project(dll_name, class_name, scope='class')
        if not data:
            return {'ok': False, 'error': '该类没有带 RVA 的方法, 无法生成工程'}
        path = _win32_dialog(save_mode=True, title='导出 Hook 工程 (zip)',
                             default_name=proj + '.zip')
        if not path:
            return {'ok': False, 'error': '已取消'}
        with open(path, 'wb') as f:
            f.write(data)
        return {'ok': True, 'path': path, 'name': proj}

    def export_hook_project_stars(self):
        """弹保存框导出收藏项 Hook 工程 zip"""
        data, proj = self.gen_hook_project(scope='stars')
        if not data:
            return {'ok': False, 'error': '收藏里没有带 RVA 的方法'}
        path = _win32_dialog(save_mode=True, title='导出收藏 Hook 工程 (zip)',
                             default_name=proj + '.zip')
        if not path:
            return {'ok': False, 'error': '已取消'}
        with open(path, 'wb') as f:
            f.write(data)
        return {'ok': True, 'path': path, 'name': proj}

    # ==================== 🔀 版本对比 ====================
    def open_diff_file(self):
        """选第二份 dump.cs (新版), 选到后自动后台解析"""
        _dbg('api.open_diff_file 被调用')
        path = _win32_dialog(save_mode=False, title='选择新版 dump.cs (对比用)')
        if not path:
            return None
        self.load_diff_file(path)
        return path

    def load_diff_file(self, path):
        """后台线程解析第二份 dump.cs 用于版本对比"""
        if not path or not os.path.isfile(path):
            self.diff_state, self.diff_msg = 'error', '文件不存在: ' + str(path)
            return {'ok': False, 'error': self.diff_msg}
        if not self.dlls:
            self.diff_state, self.diff_msg = 'error', '请先加载当前版本 dump.cs'
            return {'ok': False, 'error': self.diff_msg}
        if os.path.abspath(path) == os.path.abspath(self.dump_path or ''):
            self.diff_state, self.diff_msg = 'error', '不能和自己对比'
            return {'ok': False, 'error': self.diff_msg}

        self.diff_state, self.diff_msg = 'loading', os.path.basename(path)
        self.diff_path = path
        self._diff_data = None
        self._diff_new_dlls = None
        self._diff_new_idx = None

        def _worker():
            try:
                new_dlls, _, _, _, _ = load_parsed(path)  # 对比文件同样走缓存
                self._diff_new_dlls = new_dlls
                self._diff_data = diff_dumps(self.dlls, new_dlls)
                self.diff_state = 'ready'
                _dbg(f'diff 解析完成: {self._diff_data["stats"]}')
            except Exception as e:
                self.diff_state, self.diff_msg = 'error', f'解析失败: {e}'

        threading.Thread(target=_worker, daemon=True).start()
        return {'ok': True, 'state': 'loading', 'name': os.path.basename(path)}

    def get_diff_state(self):
        return {'state': self.diff_state, 'msg': self.diff_msg}

    def _stars_diff(self):
        """收藏项在新版本里的状态: ok / moved / gone / class_gone / new_file"""
        out = []
        if not (self._diff_data and self._diff_new_dlls):
            return out
        idx = {}
        for dll_name, d in self._diff_new_dlls.items():
            for cls in d['classes']:
                methods = {}
                for m in cls.methods:
                    if m[1]:
                        methods.setdefault(m[0], set()).add(m[1])
                idx[(dll_name, cls.name)] = methods
        for s in self._stars:
            dll, cls, m = s.get('dll'), s.get('cls'), s.get('m', '')
            key = (dll, cls)
            label = f'{cls}.{m}' if m else cls
            old_rva = ''
            if self.dlls and dll in self.dlls:
                for c in self.dlls[dll]['classes']:
                    if c.name == cls:
                        if m:
                            for mm in c.methods:
                                if mm[0] == m and mm[1]:
                                    old_rva = mm[1]
                                    break
                        break
            if key not in idx:
                out.append({'label': label, 'status': 'class_gone', 'old': old_rva, 'new': ''})
            elif m and m not in idx[key]:
                out.append({'label': label, 'status': 'gone', 'old': old_rva, 'new': ''})
            elif m:
                rvas = idx[key][m]
                if old_rva and old_rva in rvas:
                    out.append({'label': label, 'status': 'ok', 'old': old_rva, 'new': old_rva})
                else:
                    new_rva = sorted(rvas)[0] if rvas else ''
                    out.append({'label': label, 'status': 'moved', 'old': old_rva, 'new': new_rva})
            else:
                out.append({'label': label, 'status': 'ok', 'old': '', 'new': f'{len(idx[key])} 方法'})
        return out

    def get_diff(self, max_items=500):
        """返回 diff 结果 (每节最多 max_items 条, 全量请用 export_diff_report)"""
        if self.diff_state != 'ready' or not self._diff_data:
            return None
        d = self._diff_data
        return {
            'old': os.path.basename(self.dump_path) if self.dump_path else '?',
            'new': os.path.basename(self.diff_path) if self.diff_path else '?',
            'classes_added': d['classes_added'][:max_items],
            'classes_removed': d['classes_removed'][:max_items],
            'methods_added': d['methods_added'][:max_items],
            'methods_removed': d['methods_removed'][:max_items],
            'rva_changed': d['rva_changed'][:max_items],
            'stars': self._stars_diff(),
            'stats': d['stats'],
            'truncated': any(len(d[k]) > max_items
                             for k in ('classes_added', 'classes_removed',
                                       'methods_added', 'methods_removed', 'rva_changed')),
        }

    def _diff_report_text(self):
        d = self._diff_data
        st = d['stats']
        L = [f'# dump 版本对比报告',
             '',
             f'- 旧版(当前加载): {os.path.basename(self.dump_path or "?")}',
             f'- 新版(对比文件): {os.path.basename(self.diff_path or "?")}',
             f'- 生成时间: {time.strftime("%Y-%m-%d %H:%M:%S")}',
             '',
             '## 总览',
             '',
             f'| 变化 | 数量 |',
             f'|---|---|',
             f'| 新增类 | {st["classes_added"]} |',
             f'| 删除类 | {st["classes_removed"]} |',
             f'| 新增方法(已有类) | {st["methods_added"]} |',
             f'| 删除方法(已有类) | {st["methods_removed"]} |',
             f'| RVA 变化 | {st["rva_changed"]} |',
             '']
        stars = self._stars_diff()
        if stars:
            L += ['## ⭐ 收藏项状态', '',
                  '| 收藏项 | 状态 | 旧 RVA | 新 RVA |', '|---|---|---|---|']
            smap = {'ok': '✅ 不变', 'moved': '🔁 RVA 变化', 'gone': '❌ 方法没了',
                    'class_gone': '❌ 类没了'}
            for s in stars:
                L.append(f'| {s["label"]} | {smap.get(s["status"], s["status"])} | {s["old"] or "-"} | {s["new"] or "-"} |')
            L.append('')

        def _sec(title, rows, fmt):
            L.extend([f'## {title} ({len(rows)})', ''])
            if not rows:
                L.append('(无)')
            for r in rows:
                L.append(fmt(r))
            L.append('')

        _sec('新增类', d['classes_added'],
             lambda r: f'- **{r[1]}** {("· " + r[2]) if r[2] else ""} [{r[0]}] ({r[3]} 方法)')
        _sec('删除类', d['classes_removed'],
             lambda r: f'- **{r[1]}** {("· " + r[2]) if r[2] else ""} [{r[0]}] ({r[3]} 方法)')
        _sec('RVA 变化', d['rva_changed'],
             lambda r: f'- `{r[1]}.{r[2]}`: {r[3]} → **{r[4]}** [{r[0]}]')
        _sec('新增方法(已有类)', d['methods_added'],
             lambda r: f'- `{r[1]}.{r[2]}` RVA {r[3]} [{r[0]}]')
        _sec('删除方法(已有类)', d['methods_removed'],
             lambda r: f'- `{r[1]}.{r[2]}` RVA {r[3]} [{r[0]}]')
        return '\n'.join(L)

    def export_diff_report(self):
        """弹保存框导出全量 diff 报告 (md)"""
        if self.diff_state != 'ready' or not self._diff_data:
            return {'ok': False, 'error': '还没有可用的对比结果'}
        path = _win32_dialog(save_mode=True, title='导出版本对比报告',
                             default_name='dump-diff报告.md')
        if not path:
            return {'ok': False, 'error': '已取消'}
        with open(path, 'w', encoding='utf-8') as f:
            f.write(self._diff_report_text())
        return {'ok': True, 'path': path}

    # ==================== 🛫 跨版本 Hook 迁移 ====================
    _MIGRATE_HEX_RE = re.compile(r'0[xX][0-9A-Fa-f]{5,12}')

    def _new_rva_index(self):
        """新版 dump 的 (类名, 方法名) → [RVA...] 索引 (懒构建缓存)"""
        if self._diff_new_idx is None and self._diff_new_dlls:
            idx = {}
            for d in self._diff_new_dlls.values():
                for cls in d['classes']:
                    for m in cls.methods:
                        if m[1]:
                            try:
                                idx.setdefault((cls.name, m[0]), []).append(int(m[1], 16))
                            except (ValueError, TypeError):
                                pass
            self._diff_new_idx = idx
        return self._diff_new_idx

    def migrate_hook(self, code):
        """老 hook 代码里的 RVA 一键改写成新版地址。
        需要: 当前加载旧版 dump + 「版本对比」已加载新版 dump。
        只有能对应到旧 dump 方法表的 0x 常量才会被改写, 其余常量原样保留。"""
        if self.diff_state != 'ready' or not self._diff_new_dlls:
            return {'ok': False, 'error': '请先点「版本对比」加载新版 dump.cs, 再做迁移'}
        if not code or not code.strip():
            return {'ok': False, 'error': '请粘贴老版本的 hook 代码'}
        if not self._rva_index:
            return {'ok': False, 'error': '尚未加载旧版 dump'}
        new_idx = self._new_rva_index()
        changed, same, missing = [], [], []
        seen = set()
        unknown = 0

        def _repl(mo):
            nonlocal unknown
            tok = mo.group(0)
            rva_int = int(tok, 16)
            if tok in seen:
                return tok
            seen.add(tok)
            hit = self._rva_index.get(rva_int)
            if not hit:
                unknown += 1  # 不是方法地址的常量 → 不动
                return tok
            dll, cls, m = hit
            key = (cls.name, m[0])
            news = new_idx.get(key)
            if not news:
                missing.append({'old': tok, 'method': f'{cls.name}.{m[0]}'})
                return tok
            new_rva = min(news)
            if new_rva == rva_int:
                same.append({'old': tok, 'new': tok, 'method': f'{cls.name}.{m[0]}'})
                return tok
            new_tok = '0x' + format(new_rva, 'X')
            changed.append({'old': tok, 'new': new_tok, 'method': f'{cls.name}.{m[0]}',
                            'dll': dll, 'ambiguous': len(news) > 1})
            return new_tok

        new_code = self._MIGRATE_HEX_RE.sub(_repl, code)
        return {'ok': True, 'code': new_code,
                'changed': changed, 'same': same, 'missing': missing,
                'unknown': unknown, 'tokens': len(seen)}


# ============================================================
# 5.5 MCP 服务器 (Model Context Protocol · stdio 传输 · 零第三方依赖)
#      用法: dump工具-mcp.exe  (AI 客户端通过 mcp.json 拉起)
# ============================================================
MCP_TOOLS = [
    {
        'name': 'get_status',
        'description': '查看当前加载的 dump 文件与统计 (DLL数/类型数/方法数)。服务器启动时自动加载最近打开的 dump。',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'load_dump',
        'description': '加载一个 dump.cs 文件 (Il2CppDumper 生成)。大文件约 10 秒。',
        'inputSchema': {'type': 'object',
                        'properties': {'path': {'type': 'string', 'description': 'dump.cs 绝对路径'}},
                        'required': ['path']},
    },
    {
        'name': 'get_structure',
        'description': '获取游戏结构树概览: 分组 → DLL(类型数) → 用途分类(类数)。',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'search_class',
        'description': '按类名/中文释义/分类搜索类, 返回 类名、中文翻译、DLL、分类、字段方法数。例: 查 player、坐标、背包。',
        'inputSchema': {'type': 'object',
                        'properties': {'query': {'type': 'string', 'description': '搜索词(大小写不敏感)'},
                                       'limit': {'type': 'number', 'description': '返回条数, 默认20'}},
                        'required': ['query']},
    },
    {
        'name': 'list_classes',
        'description': '列出某 DLL 某用途分类下的类 (如 BobMainScript.dll 的 角色与移动)。',
        'inputSchema': {'type': 'object',
                        'properties': {'dll': {'type': 'string', 'description': 'DLL 名, 如 BobMainScript.dll'},
                                       'category': {'type': 'string', 'description': '分类名, 如 界面UI/战斗与技能/角色与移动'},
                                       'query': {'type': 'string', 'description': '可选过滤'},
                                       'limit': {'type': 'number', 'description': '默认50'}},
                        'required': ['dll', 'category']},
    },
    {
        'name': 'get_class_detail',
        'description': '获取类完整详情: 中文释义/基类/用途 + 字段(含内存偏移) + 方法(含RVA)。大类建议用 section 分段。',
        'inputSchema': {'type': 'object',
                        'properties': {'class_name': {'type': 'string'},
                                       'dll': {'type': 'string', 'description': '所在DLL(同名类多DLL时用; 可省略自动找)'},
                                       'section': {'type': 'string',
                                                   'enum': ['all', 'summary', 'fields', 'methods', 'props'],
                                                   'description': 'all=全部(默认,超长截断); summary/fields/methods/props=单段全量'}},
                        'required': ['class_name']},
    },
    {
        'name': 'find_method',
        'description': '按方法名全局搜索(跨所有类), 返回 类.方法 + RVA + 参数 —— 逆向找 hook 地址最常用。例: find_method("set_position")。',
        'inputSchema': {'type': 'object',
                        'properties': {'method_name': {'type': 'string', 'description': '方法名(不区分大小写,部分匹配)'},
                                       'limit': {'type': 'number', 'description': '默认30'}},
                        'required': ['method_name']},
    },
    {
        'name': 'translate',
        'description': '英文标识符翻译成中文 (驼峰分词 + 游戏/Unity 词典)。例: translate("GetLocalPlayerPos")。',
        'inputSchema': {'type': 'object',
                        'properties': {'text': {'type': 'string'}},
                        'required': ['text']},
    },
    {
        'name': 'gen_hook',
        'description': ('生成方法的 Dobby Hook C++ 代码, 可直接编译。三种模式: '
                        'mode=mini 精简片段(默认) / mode=full 完整可编译示例(含基址获取/安装/编译命令) / '
                        'mode=byname 按名解析(运行时用 il2cpp 导出函数按名字查函数地址, 游戏更新免改码)。'),
        'inputSchema': {'type': 'object',
                        'properties': {'class_name': {'type': 'string'},
                                       'method_name': {'type': 'string'},
                                       'dll': {'type': 'string', 'description': '所在DLL(可省略自动找)'},
                                       'mode': {'type': 'string', 'enum': ['mini', 'full', 'byname'],
                                                'description': 'mini=精简片段(默认), full=完整示例, byname=按名解析(更新免改码)'},
                                       'full': {'type': 'boolean', 'description': '(兼容旧参数) true=完整示例, 等价 mode=full'}},
                        'required': ['class_name', 'method_name']},
    },
    {
        'name': 'gen_frida',
        'description': ('生成方法的 Frida JS 脚本 (Interceptor.attach 版, 调试期免编译免注入器, '
                        'frida -U -f 包名 -l xx.js 直接跑)。RVA/包名/参数注释自动填充。'),
        'inputSchema': {'type': 'object',
                        'properties': {'class_name': {'type': 'string'},
                                       'method_name': {'type': 'string'},
                                       'dll': {'type': 'string', 'description': '所在DLL(可省略自动找)'}},
                        'required': ['class_name', 'method_name']},
    },
    {
        'name': 'gen_ida_script',
        'description': ('生成 IDA Pro Python 命名脚本: 按 dump.cs 把函数重命名+加中文注释, '
                        '在 IDA 里打开 libil2cpp.so 后跑脚本即可。scope=stars 导出所有收藏项。'),
        'inputSchema': {'type': 'object',
                        'properties': {'class_name': {'type': 'string', 'description': '单类导出时的类名 (scope=stars 时省略)'},
                                       'dll': {'type': 'string', 'description': '所在DLL(可省略自动找)'},
                                       'scope': {'type': 'string', 'enum': ['class', 'stars'],
                                                 'description': 'class=单类全部方法(默认), stars=所有收藏项'}},
                        'required': []},
    },
    {
        'name': 'gen_gg_script',
        'description': ('生成 GameGuardian (GG) Lua 读写骨架: 该类字段偏移表 + 反向定位对象 + 查看修改字段 + '
                        '字段绝对地址表 + 方法运行时地址表 + 🔍反查地址(地址→字段/方法/类名)。'
                        'Vector3 等自动展开; 静态字段单独列出。给 save_path 则直接导出 .lua 文件。'),
        'inputSchema': {'type': 'object',
                        'properties': {'class_name': {'type': 'string'},
                                       'dll': {'type': 'string', 'description': '所在DLL(可省略自动找)'},
                                       'save_path': {'type': 'string',
                                                     'description': '导出路径, 如 D:/gg/CoreGameTimerBase.lua; 省略只返回文本'}},
                        'required': ['class_name']},
    },
    {
        'name': 'find_by_rva',
        'description': ('🔍 地址反查: 输入 RVA 或(绝对地址+base基址) 定位到对应的 类.方法。'
                        '适用场景: 从 GG/Frida/崩溃日志/他人给出的地址反查它是什么函数。'),
        'inputSchema': {'type': 'object',
                        'properties': {'rva': {'type': 'string',
                                               'description': '地址, 支持 0x94E89B4 / 94E89B4 / 十进制'},
                                       'base': {'type': 'string',
                                                'description': '可选: 进程内模块基址(libil2cpp.so/GameAssembly.dll), '
                                                               '传了则按 绝对地址-base=RVA 换算'}},
                        'required': ['rva']},
    },
    {
        'name': 'find_field_offset',
        'description': ('🔍 偏移反查: 类名 + 偏移量 → 该偏移对应哪个字段(含类型)。'
                        '适用场景: GG 定位对象后, 用 地址-对象地址 算出偏移, 反查字段名。'),
        'inputSchema': {'type': 'object',
                        'properties': {'class_name': {'type': 'string'},
                                       'offset': {'type': 'string',
                                                  'description': '对象内偏移, 支持 0x38 / 56'}},
                        'required': ['class_name', 'offset']},
    },
    {
        'name': 'gen_gg_probe',
        'description': ('生成通用 il2cpp 探测器 GG Lua 脚本(不绑定类): 任意内存地址→类名(il2cpp对象头探测), '
                        '基址查询, 对象头部 hexdump, 内嵌收藏类名单。给 save_path 则直接导出 .lua。'),
        'inputSchema': {'type': 'object',
                        'properties': {'save_path': {'type': 'string',
                                                     'description': '导出路径; 省略只返回文本'}},
                        'required': []},
    },
    {
        'name': 'manage_stars',
        'description': ('收藏(⭐)管理: 查询/添加/删除 收藏的类或方法。收藏项会在版本对比(diff)里优先标注 RVA 变化, '
                       '也可一键导出 IDA 命名脚本。'),
        'inputSchema': {'type': 'object',
                        'properties': {'action': {'type': 'string', 'enum': ['list', 'add', 'remove'],
                                                  'description': 'list=列出全部(默认), add=添加, remove=删除'},
                                       'class_name': {'type': 'string', 'description': 'add/remove 时必填'},
                                       'method_name': {'type': 'string', 'description': '收藏方法时填, 省略则收藏整个类'},
                                       'dll': {'type': 'string', 'description': '所在DLL(可省略自动找)'}},
                        'required': []},
    },
    {
        'name': 'compare_dumps',
        'description': ('双版本对比: 传一份新版 dump.cs 的路径, 与当前已加载的(旧版)对比, 返回新增/删除类与方法、'
                       'RVA 漂移、收藏项受影响情况。游戏更新后定位哪些 hook 要改就靠它。'
                       '注意: 解析大 dump 需 20-60 秒, 属正常等待 (同文件走缓存则秒级)。'),
        'inputSchema': {'type': 'object',
                        'properties': {'new_dump_path': {'type': 'string', 'description': '新版 dump.cs 的完整路径'}},
                        'required': ['new_dump_path']},
    },
    {
        'name': 'search_all',
        'description': ('全局搜索(类+方法双索引): 类名/中文/分类 + 方法名一次搜完。'
                       '搜 set_position 这类方法名直接给出 类.方法 + RVA, 比 search_class 只搜类更全。'),
        'inputSchema': {'type': 'object',
                        'properties': {'query': {'type': 'string'},
                                       'limit': {'type': 'number', 'description': '各类结果上限, 默认20'}},
                        'required': ['query']},
    },
    {
        'name': 'find_class',
        'description': ('按类名跨 DLL 精确定位: 返回所在 DLL、中文释义、完整继承链(基类逐级上溯)。'
                       '想知道某类的继承结构/在哪个程序集时用。'),
        'inputSchema': {'type': 'object',
                        'properties': {'class_name': {'type': 'string'}},
                        'required': ['class_name']},
    },
    {
        'name': 'search_strings',
        'description': ('🔤 字符串搜索: 在游戏全部字符串字面量(stringliteral.json)里搜文本, '
                       '返回命中字符串及其 metadata 偏移。搜「复活/钻石/体力」这类游戏文本定位相关功能。'
                       '需要 dump.cs 同目录存在 stringliteral.json (Il2CppDumper 的输出文件)。'),
        'inputSchema': {'type': 'object',
                        'properties': {'query': {'type': 'string', 'description': '搜索词'},
                                       'limit': {'type': 'number', 'description': '默认30'}},
                        'required': ['query']},
    },
    {
        'name': 'gen_frida_trace',
        'description': ('⚡ 生成 Frida 批量追踪脚本: 整类全部方法或所有收藏项一把梭, '
                       '游戏里做操作实时打印哪个函数被调/参数解码/返回值 (ARM64 寄存器映射, '
                       'il2cpp 字符串自动解码)。定位功能入口的神器。给 save_path 直接导出 .js。'),
        'inputSchema': {'type': 'object',
                        'properties': {'scope': {'type': 'string', 'enum': ['class', 'stars'],
                                                 'description': 'class=单类全部方法, stars=所有收藏项'},
                                       'class_name': {'type': 'string', 'description': 'scope=class 时的类名'},
                                       'dll': {'type': 'string', 'description': '所在DLL(可省略自动找)'},
                                       'save_path': {'type': 'string', 'description': '导出路径; 省略只返回文本'}},
                        'required': ['scope']},
    },
    {
        'name': 'migrate_hook',
        'description': ('🛫 跨版本 Hook 迁移: 粘贴老版本 hook 代码(含 0x RVA 常量), '
                       '自动把每个 RVA 换算成新版 dump 里的新地址。前置条件: '
                       '当前已加载旧版 dump + compare_dumps 已加载新版 dump。'
                       '只有能对应到旧 dump 方法表的常量才会被改写, 其余原样保留。'),
        'inputSchema': {'type': 'object',
                        'properties': {'hook_code': {'type': 'string', 'description': '老版本 hook C/C++ 代码全文'}},
                        'required': ['hook_code']},
    },
    {
        'name': 'gen_hook_project',
        'description': ('🚀 生成完整 Android NDK/CMake Hook 工程压缩包: hook_main.cpp(基址获取+示例hook+安装) '
                       '+ offsets.h(目标RVA表) + il2cpp_api.h(按名解析) + CMakeLists + build.sh(一键编译推送) + README。'
                       'scope=class 单类 / stars 收藏项。给 save_path 直接写 zip 文件。'),
        'inputSchema': {'type': 'object',
                        'properties': {'scope': {'type': 'string', 'enum': ['class', 'stars'],
                                                 'description': 'class=单类, stars=所有收藏项'},
                                       'class_name': {'type': 'string', 'description': 'scope=class 时的类名'},
                                       'dll': {'type': 'string', 'description': '所在DLL(可省略自动找)'},
                                       'save_path': {'type': 'string',
                                                     'description': 'zip 导出路径, 如 D:/proj/hook.zip; 省略返回工程说明+主要文件'}},
                        'required': ['scope']},
    },
    # ==================== 📱 ADB 调试 ====================
    {
        'name': 'adb_devices',
        'description': ('📱 列出本机 adb 连接的手机 (USB/WiFi), 含型号/安卓版本/ABI/root 状态。'
                       '多台设备时后续 ADB 工具需用 serial 参数指定; 单台自动选中。'),
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'adb_apps',
        'description': ('📱 列出手机上已安装的第三方应用 (包名+APK路径)。'
                       'check_pkg 传某个包名可顺便检测它是不是 Unity/il2cpp 游戏。'),
        'inputSchema': {'type': 'object',
                        'properties': {'serial': {'type': 'string', 'description': '设备序列号(多台时必须)'},
                                       'check_pkg': {'type': 'string', 'description': '顺便检测这个包是否 il2cpp 游戏'}},
                        },
    },
    {
        'name': 'adb_pull_il2cpp',
        'description': ('📱 从手机已安装的游戏直接提取 il2cpp 逆向材料: libil2cpp.so + global-metadata.dat '
                       '→ 本地目录 (流式提取, 不落地整个 APK)。'
                       '拿到后喂给 Il2CppDumper 即可产出 dump.cs。dest_dir 省略时存到工具目录「手机游戏材料/<包名>」。'),
        'inputSchema': {'type': 'object',
                        'properties': {'pkg': {'type': 'string', 'description': '游戏包名'},
                                       'serial': {'type': 'string', 'description': '设备序列号(多台时必须)'},
                                       'dest_dir': {'type': 'string', 'description': '本地保存目录(可省略)'}},
                        'required': ['pkg']},
    },
    {
        'name': 'adb_push_file',
        'description': ('📱 推送本地文件到手机 (GG 脚本 .lua / Frida 脚本 .js / hook so 等), '
                       '默认推到 /storage/emulated/0/Download/。'),
        'inputSchema': {'type': 'object',
                        'properties': {'local': {'type': 'string', 'description': '本地文件完整路径'},
                                       'serial': {'type': 'string', 'description': '设备序列号(多台时必须)'},
                                       'remote': {'type': 'string', 'description': '手机目标路径(默认 /sdcard/Download/)'}},
                        'required': ['local']},
    },
    {
        'name': 'adb_shell',
        'description': ('📱 在手机上执行 shell 命令 (root=true 时用 su -c, 需要 Magisk)。'
                       '例: 查进程 ps -A | grep xxx, 查文件 ls /data/local/tmp 等。'),
        'inputSchema': {'type': 'object',
                        'properties': {'cmd': {'type': 'string', 'description': 'shell 命令'},
                                       'serial': {'type': 'string', 'description': '设备序列号(多台时必须)'},
                                       'root': {'type': 'boolean', 'description': 'true=su 执行 (默认 false)'}},
                        'required': ['cmd']},
    },
    {
        'name': 'adb_logcat',
        'description': ('📱 抓手机 logcat 快照 (最近 N 行, 可按关键字过滤)。看崩溃/Unity 输出。'),
        'inputSchema': {'type': 'object',
                        'properties': {'keyword': {'type': 'string', 'description': '过滤关键字(可省略)'},
                                       'lines': {'type': 'integer', 'description': '行数 (默认 300, 最大 5000)'},
                                       'serial': {'type': 'string', 'description': '设备序列号(多台时必须)'}},
                        },
    },
    {
        'name': 'adb_frida',
        'description': ('📱 Frida 调试复合工具。action: '
                       'status=CLI+server状态 | start=推送并启动手机端 frida-server(需 local_bin, '
                       'hidden=true 走🥷隐身模式: 特征串patch+随机名+随机端口) | '
                       'stop_server=停 server | ps=frida-ps 列进程 | '
                       'run=后台跑脚本(pkg+script+spawn) | run_code=直接跑一段 JS 代码(pkg+code, 免落文件) | '
                       'read=读新输出(after) | stop_run=停脚本。'
                       '注意: 本机需已 pip install frida-tools, 手机端 server 版本需与本机 CLI 同大版本。'),
        'inputSchema': {'type': 'object',
                        'properties': {'action': {'type': 'string',
                                                  'enum': ['status', 'start', 'stop_server', 'ps',
                                                           'run', 'run_code', 'read', 'stop_run'],
                                                  'description': '操作类型'},
                                       'serial': {'type': 'string', 'description': '设备序列号(多台时必须)'},
                                       'local_bin': {'type': 'string', 'description': 'action=start: 本地 frida-server 文件路径'},
                                       'hidden': {'type': 'boolean', 'description': 'action=start: 🥷 隐身模式 (反检测: 特征串随机化+随机文件名+随机端口只听 127.0.0.1)'},
                                       'pkg': {'type': 'string', 'description': 'action=run/run_code: 游戏包名'},
                                       'script': {'type': 'string', 'description': 'action=run: 本地 Frida JS 脚本路径'},
                                       'code': {'type': 'string', 'description': 'action=run_code: 完整 Frida JS 代码内容'},
                                       'cls': {'type': 'string', 'description': 'action=run_code 可选: 本次 Hook 的类名 (记录后 GUI「⚡ 注入」可一键重跑)'},
                                       'm': {'type': 'string', 'description': 'action=run_code 可选: 本次 Hook 的方法名'},
                                       'spawn': {'type': 'boolean', 'description': 'action=run/run_code: 冷启动(run 默认 true; run_code 省略=自动: 进程在跑 attach, 没跑 spawn)'},
                                       'after': {'type': 'integer', 'description': 'action=read: 上次读到的行数'}},
                        'required': ['action']},
    },
]


def _find_class(api, class_name, dll=None):
    """按类名查找 ClassInfo (dll 省略时跨 DLL 找第一个)"""
    if not api.dlls:
        return None, None
    if dll:
        if dll not in api.dlls:
            return None, None
        for cls in api.dlls[dll]['classes']:
            if cls.name == class_name:
                return dll, cls
        return None, None
    for dll_name, d in api.dlls.items():
        for cls in d['classes']:
            if cls.name == class_name:
                return dll_name, cls
    return None, None


def _find_methods(api, method_name, limit=30):
    """全局按方法名搜索 (大小写不敏感, 部分匹配)"""
    q = method_name.lower()
    out = []
    for dll_name, d in api.dlls.items():
        for cls in d['classes']:
            for m in cls.methods:
                if q in m[0].lower():
                    out.append((dll_name, cls, m))
                    if len(out) >= limit:
                        return out
    return out


def _fmt_structure(api):
    lines = []
    for g in api.get_structure():
        total = sum(d['class_count'] for d in g['dlls'])
        lines.append(f"{g['icon']} {g['name']} — {len(g['dlls'])} 个 DLL · {total} 类型")
        for d in g['dlls']:
            cats = ', '.join(f"{c['name']}({c['count']})" for c in d['cats'])
            lines.append(f"  {d['name']} [{d['class_count']} 类型]: {cats}")
        lines.append('')
    return '\n'.join(lines)


def _fmt_class_summary(d):
    lines = [f"【{d['n']}】{d['k']} · {d.get('cn') or ''}"]
    if d.get('b'):
        lines.append(f"继承: {d['b']}" + (f"  (角色: {d['r']})" if d.get('r') else ''))
    if d.get('p'):
        lines.append(f"用途: {d['p']}")
    lines.append(f"所在: {d.get('dll','?')} · {d.get('cat','')} · 命名空间: {d.get('ns','')}")
    lines.append(f"规模: 字段 {len(d['fields'])} · 属性 {len(d['props'])} · 方法 {len(d['methods'])}")
    return '\n'.join(lines)


def _fmt_fields(fields, cap=1000):
    if not fields:
        return '(无字段)'
    lines = [f"{f['n']} : {f['t']}"
             + (f"  // 偏移 {f['off']}" if f.get('off') else '')
             + (f"  // {f['cn']}" if f.get('cn') else '')
             for f in fields[:cap]]
    if len(fields) > cap:
        lines.append(f"… 共 {len(fields)} 条, 仅显示前 {cap} 条")
    return '\n'.join(lines)


def _fmt_methods(methods, cap=1000):
    if not methods:
        return '(无方法)'
    lines = []
    for m in methods[:cap]:
        rva = m.get('rva') or '—'
        st = ' [static]' if m.get('static') else ''
        cn = f"  // {m['cn']}" if m.get('cn') else ''
        lines.append(f"{rva}  {m['n']}({m.get('params') or ''}){st}{cn}")
    if len(methods) > cap:
        lines.append(f"… 共 {len(methods)} 条, 仅显示前 {cap} 条")
    return '\n'.join(lines)


def _fmt_props(props, cap=1000):
    if not props:
        return '(无属性)'
    lines = [f"{p['n']} : {p['t']}" + (f"  // {p['cn']}" if p.get('cn') else '') for p in props[:cap]]
    if len(props) > cap:
        lines.append(f"… 共 {len(props)} 条, 仅显示前 {cap} 条")
    return '\n'.join(lines)


def _mcp_dispatch(api, name, a, not_ready):
    """执行 MCP 工具调用, 返回中文文本"""
    if name == 'get_status':
        if api.loading:
            return 'dump 正在后台加载中 (首次约 10 秒, 二次走缓存秒级), 稍后用 get_status 查看。'
        if not api.dlls:
            return '尚未加载 dump.cs。请用 load_dump(path) 加载。'
        src = ' (缓存秒开)' if api.loaded_from_cache else ''
        extra = []
        if api._strings_path:
            extra.append('字符串表: 已检测到 stringliteral.json (search_strings 可用)')
        if api.diff_state == 'ready':
            extra.append('版本对比: 新版已加载 (migrate_hook 可用)')
        elif api.diff_state == 'loading':
            extra.append('版本对比: 新版解析中…')
        tail = ('\n' + '\n'.join(extra)) if extra else ''
        return (f'已加载: {api.dump_path}\n'
                f'DLL {len(api.dlls)} 个 · 类型 {api.total_classes} · 方法 {api.total_methods}\n'
                f'解析耗时 {api.load_elapsed:.1f}s{src}{tail}')

    if name == 'load_dump':
        path = (a.get('path') or '').strip()
        if not path:
            return '缺少 path 参数。'
        r = api.load_file(path)
        if not r.get('ok'):
            return '加载失败: ' + str(r.get('error'))
        return (f"加载成功: {r['name']} ({r['dll_count']} DLL · {r['total_classes']} 类型 · "
                f"{r['total_methods']} 方法 · {r['elapsed']}s)")

    if name == 'translate':  # 纯词典查询, 不依赖 dump 加载状态
        text = (a.get('text') or '').strip()
        if not text:
            return '缺少 text 参数。'
        return api.translate(text)

    nr = not_ready()
    if nr:
        return nr

    if name == 'get_structure':
        return _fmt_structure(api)

    if name == 'search_class':
        q = (a.get('query') or '').strip()
        if not q:
            return '缺少 query 参数。'
        limit = int(a.get('limit') or 20)
        results = api.search(q, limit)
        if not results:
            return f'未找到匹配 "{q}" 的类。'
        lines = [f'找到 {len(results)} 个匹配:']
        for i, c in enumerate(results, 1):
            lines.append(f"{i}. {c['n']}  [{c['cn'] or '—'}] — {c['dll']} · {c['cat']} · "
                         f"字段{c['f']} 方法{c['m']}")
        return '\n'.join(lines)

    if name == 'list_classes':
        dll = (a.get('dll') or '').strip()
        cat = (a.get('category') or '').strip()
        if dll not in api.dlls:
            avail = ', '.join(sorted(api.dlls))
            return f'DLL 不存在: {dll}\n可用的 DLL: {avail}'
        cats = api.dlls[dll]['cats']
        if cat not in cats:
            avail = ', '.join(f"{k}({len(v)})" for k, v in sorted(cats.items(), key=lambda x: -len(x[1])))
            return f'分类不存在: {cat}\n{dll} 的分类: {avail}'
        limit = int(a.get('limit') or 50)
        r = api.get_classes(dll, cat, a.get('query') or '', 0, limit)
        items, total = r['items'], r['total']
        if not items:
            return '该条件下无类。'
        lines = [f'{dll} · {cat} — 共 {total} 个 (显示 {len(items)}):']
        for i, c in enumerate(items, 1):
            lines.append(f"{i}. {c['n']}  [{c['cn'] or '—'}]  字段{c['f']} 方法{c['m']}")
        return '\n'.join(lines)

    if name == 'get_class_detail':
        cname = (a.get('class_name') or '').strip()
        dll = (a.get('dll') or '').strip() or None
        dll_name, cls = _find_class(api, cname, dll)
        if cls is None:
            return f'未找到类: {cname}' + (f' (dll={dll})' if dll else '')
        d = cls.detail()
        d['dll'] = dll_name
        section = a.get('section') or 'all'
        parts = [_fmt_class_summary(d)]
        if section in ('all', 'fields'):
            cap = 300 if section == 'all' else 1000
            parts.append('── 字段 ──\n' + _fmt_fields(d['fields'], cap))
        if section in ('all', 'methods'):
            cap = 200 if section == 'all' else 1000
            parts.append('── 方法 (RVA 可直接用于 Dobby: 地址=libil2cpp.so基址+RVA) ──\n' + _fmt_methods(d['methods'], cap))
        if section in ('all', 'props'):
            cap = 100 if section == 'all' else 1000
            parts.append('── 属性 ──\n' + _fmt_props(d['props'], cap))
        return '\n\n'.join(parts)

    if name == 'find_method':
        mname = (a.get('method_name') or '').strip()
        if not mname:
            return '缺少 method_name 参数。'
        limit = int(a.get('limit') or 30)
        found = _find_methods(api, mname, limit)
        if not found:
            return f'未找到匹配 "{mname}" 的方法。'
        lines = [f'找到 {len(found)} 个匹配方法:']
        for dll_name, cls, m in found:
            cn = _method_cn(m[0])
            lines.append(f"[{dll_name}] {cls.name}.{m[0]}  RVA {m[1] or '—'}"
                         f"  ({m[4]}){' [static]' if m[3] else ''}" + (f"  // {cn}" if cn else ''))
        lines.append('提示: 用 gen_hook(class_name, method_name) 可生成完整 Dobby 代码。')
        return '\n'.join(lines)

    if name == 'gen_hook':
        cname = (a.get('class_name') or '').strip()
        mname = (a.get('method_name') or '').strip()
        dll = (a.get('dll') or '').strip() or None
        full = bool(a.get('full'))
        mode = (a.get('mode') or '').strip()
        if not mode:
            mode = 'full' if full else None
        if not cname or not mname:
            return '缺少 class_name / method_name 参数。'
        dll_name, cls = _find_class(api, cname, dll)
        if cls is None:
            # 尝试按方法名反查类
            found = _find_methods(api, mname, 5)
            hint = ''
            if found:
                hint = '\n相似方法:\n' + '\n'.join(
                    f"[{dn}] {c.name}.{m[0]} RVA {m[1]}" for dn, c, m in found)
            return f'未找到类: {cname}' + hint
        code = api.gen_hook(dll_name, cls.name, mname, full=full, mode=mode)
        if not code:
            # 类存在但方法不存在, 列出该类的相似方法
            similar = [m[0] for m in cls.methods if mname.lower() in m[0].lower()][:10]
            return (f'类 {cls.name} 中未找到方法 {mname}。'
                    + (f'\n相似方法: {", ".join(similar)}' if similar else
                       f'\n该类共 {len(cls.methods)} 个方法, 可用 get_class_detail 查看。'))
        return code

    if name == 'gen_frida':
        cname = (a.get('class_name') or '').strip()
        mname = (a.get('method_name') or '').strip()
        dll = (a.get('dll') or '').strip() or None
        if not cname or not mname:
            return '缺少 class_name / method_name 参数。'
        dll_name, cls = _find_class(api, cname, dll)
        if cls is None:
            return f'未找到类: {cname}'
        code = api.gen_frida(dll_name, cls.name, mname)
        if not code:
            similar = [m[0] for m in cls.methods if mname.lower() in m[0].lower()][:10]
            return (f'类 {cls.name} 中未找到方法 {mname}。'
                    + (f'\n相似方法: {", ".join(similar)}' if similar else ''))
        return code

    if name == 'gen_ida_script':
        scope = (a.get('scope') or 'class').strip()
        cname = (a.get('class_name') or '').strip()
        dll = (a.get('dll') or '').strip() or None
        if scope == 'stars':
            code = api.gen_ida_script(scope='stars')
            if not code:
                return '收藏为空或收藏项没有带 RVA 的方法, 先用 manage_stars 添加。'
            return code
        if not cname:
            return "scope=stars 导出收藏, 或提供 class_name 导出单类。"
        dll_name, cls = _find_class(api, cname, dll)
        if cls is None:
            return f'未找到类: {cname}'
        code = api.gen_ida_script(dll_name, cls.name, scope='class')
        if not code:
            return f'类 {cls.name} 中没有带 RVA 的方法。'
        return code

    if name == 'gen_gg_script':
        cname = (a.get('class_name') or '').strip()
        dll = (a.get('dll') or '').strip() or None
        save_path = (a.get('save_path') or '').strip()
        if not cname:
            return '缺少 class_name 参数。'
        dll_name, cls = _find_class(api, cname, dll)
        if cls is None:
            return f'未找到类: {cname}'
        code = api.gen_gg_script(dll_name, cls.name)
        if not code:
            return f'生成失败: 类 {cls.name}'
        if save_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(code)
                return (f'已导出 GG Lua 脚本: {save_path}\n'
                        f'({len(code.splitlines())} 行, 目标类 {cls.name})\n'
                        f'推到手机后 GG → Scripts 运行, 先「定位对象」再查看/修改字段。')
            except Exception as e:
                return f'写文件失败: {e}\n\n脚本内容如下:\n{code}'
        return code

    if name == 'find_by_rva':
        rva = (a.get('rva') or '').strip()
        base = (a.get('base') or '').strip() or None
        if not rva:
            return '缺少 rva 参数 (支持 0x94E89B4 / 94E89B4 / 十进制)。'
        r = api.find_by_rva(rva, base=base)
        if not r.get('ok'):
            return r.get('error', '反查失败')
        return (f"🔍 地址 {r['rva']} → [{r['dll']}] {r['class']}.{r['method']}\n"
                f"类: {r['class']}{' (' + r['class_cn'] + ')' if r.get('class_cn') else ''} · 分类: {r.get('cat', '')}\n"
                f"方法: {r['method']}{' (' + r['method_cn'] + ')' if r.get('method_cn') else ''}\n"
                f"签名: {'static ' if r.get('static') else ''}{r.get('ret', '')} {r['method']}({r.get('params', '')})\n"
                f"RVA: {r['rva']}\n"
                f"可直接 gen_hook / gen_frida 生成该方法的 hook 代码。")

    if name == 'find_field_offset':
        cname = (a.get('class_name') or '').strip()
        off = (a.get('offset') or '').strip()
        if not cname or not off:
            return '缺少 class_name / offset 参数。'
        r = api.find_field_offset(cname, off)
        if not r.get('ok'):
            hint = f"\n{r.get('class', cname)} 共 {r.get('fields', '?')} 个字段, 可 get_class_detail 查看全部偏移。" \
                if r.get('fields') else ''
            return r.get('error', '反查失败') + hint
        lines = [f"🔍 {r['class']}{' (' + r['class_cn'] + ')' if r.get('class_cn') else ''} "
                 f"偏移 {r['offset']} → {len(r['fields'])} 个字段命中:"]
        for f in r['fields']:
            lines.append(f"  {f['name']} : {f['type']}  (+{f['offset']})")
        lines.append('提示: gen_gg_script 生成该类脚本后, 定位对象即可直接读写这些字段。')
        return '\n'.join(lines)

    if name == 'gen_gg_probe':
        save_path = (a.get('save_path') or '').strip()
        if not api.dlls:
            return '尚未加载 dump, 无法生成 (load_dump 后重试)。'
        code = api.gen_gg_probe()
        if save_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(code)
                return (f'已导出 il2cpp 探测器: {save_path}\n'
                        f'({len(code.splitlines())} 行, 收藏类 {len(api._stars)} 个)\n'
                        f'任意地址 → 类名探测 / 基址换算 / 头部 hexdump。')
            except Exception as e:
                return f'写文件失败: {e}\n\n脚本内容如下:\n{code}'
        return code

    if name == 'manage_stars':
        action = (a.get('action') or 'list').strip()
        stars = api.get_stars()
        if action == 'list':
            if not stars:
                return ('收藏为空。用 action=add 添加, 例:\n'
                        '  {class_name: "CoreGameTimerBase", method_name: "Pause"}\n'
                        '收藏后: 版本对比会标注 RVA 变化, gen_ida_script 可一键导出收藏项命名脚本。')
            lines = [f'共 {len(stars)} 项收藏:']
            for s in stars:
                m = s.get('m', '')
                lines.append(f"  [{s.get('dll','?')}] {s['cls']}" + (f'.{m}' if m else ' (整类)'))
            return '\n'.join(lines)
        cname = (a.get('class_name') or '').strip()
        mname = (a.get('method_name') or '').strip()
        dll = (a.get('dll') or '').strip() or None
        if not cname:
            return 'add/remove 需要 class_name。'
        if action == 'add':
            dll_name, cls = _find_class(api, cname, dll)
            if cls is None:
                return f'未找到类: {cname}'
            if mname and not any(m[0] == mname for m in cls.methods):
                similar = [m[0] for m in cls.methods if mname.lower() in m[0].lower()][:8]
                return (f'类 {cls.name} 中未找到方法 {mname}。'
                        + (f'\n相似: {", ".join(similar)}' if similar else ''))
            r = api.toggle_star(dll_name, cls.name, mname or None)
            label = f'{cls.name}.{mname}' if mname else f'{cls.name} (整类)'
            return f"{'⭐ 已收藏' if r['starred'] else '已取消收藏'}: {label}  (当前共 {r['total']} 项)"
        if action == 'remove':
            for s in stars:
                if s.get('cls') == cname and s.get('m', '') == mname:
                    dll2 = dll or s.get('dll')
                    r = api.toggle_star(dll2, cname, mname or None)
                    return f"已删除收藏 {cname}" + (f'.{mname}' if mname else '') + f"  (剩 {r['total']} 项)"
            return f'收藏里没有: {cname}' + (f'.{mname}' if mname else '')
        return 'action 只支持 list / add / remove。'

    if name == 'compare_dumps':
        path = (a.get('new_dump_path') or '').strip()
        if not path:
            return '缺少 new_dump_path 参数 (新版 dump.cs 完整路径)。'
        if not os.path.isfile(path):
            return f'文件不存在: {path}'
        r = api.load_diff_file(path)
        if not r.get('ok'):
            return f'对比失败: {r.get("error")}'
        # 同步等待后台解析完成 (大文件 20-60 秒)
        t0 = time.time()
        while api.diff_state == 'loading' and time.time() - t0 < 300:
            time.sleep(0.5)
        if api.diff_state != 'ready':
            return f'对比未完成: {api.diff_msg}'
        d = api.get_diff(max_items=80)
        st = d['stats']
        lines = [f'版本对比: {d["old"]} → {d["new"]}', '',
                 f'新增类 {st["classes_added"]} · 删除类 {st["classes_removed"]} · '
                 f'新增方法 {st["methods_added"]} · 删除方法 {st["methods_removed"]} · '
                 f'RVA变化 {st["rva_changed"]}', '']
        if d['stars']:
            smap = {'ok': '✅不变', 'moved': '🔁RVA变化', 'gone': '❌方法没了', 'class_gone': '❌类没了'}
            lines.append('⭐ 收藏项状态:')
            for s in d['stars']:
                arrow = f' {s["old"]} → {s["new"]}' if s['status'] == 'moved' else (f' ({s["new"]})' if s['status'] == 'ok' else '')
                lines.append(f"  {smap.get(s['status'], s['status'])} {s['label']}{arrow}")
            lines.append('')
        if st['rva_changed']:
            lines.append('RVA 变化 (前 30):')
            for r in d['rva_changed'][:30]:
                lines.append(f'  {r[1]}.{r[2]}: {r[3]} → {r[4]}')
        if st['classes_added']:
            lines.append('新增类 (前 30):')
            for r in d['classes_added'][:30]:
                lines.append(f"  {r[1]} {('· ' + r[2]) if r[2] else ''} [{r[0]}] ({r[3]}方法)")
        if st['methods_added']:
            lines.append('新增方法 (前 30):')
            for r in d['methods_added'][:30]:
                lines.append(f'  {r[1]}.{r[2]} RVA {r[3]}')
        lines.append('\n(完整清单可用 GUI 的「版本对比→导出报告」, 或再问我要某类的细节)')
        return '\n'.join(lines)

    if name == 'search_all':
        q = (a.get('query') or '').strip()
        if not q:
            return '缺少 query 参数。'
        limit = int(a.get('limit') or 20)
        r = api.search_all(q, class_limit=limit, method_limit=limit)
        lines = []
        if r['classes']:
            lines.append(f"【类 {len(r['classes'])} 个】")
            for c in r['classes']:
                lines.append(f"  {c['n']}  [{c['cn'] or '—'}] — {c['dll']} · {c['cat']}")
        if r['methods']:
            lines.append(f"【方法 {len(r['methods'])} 个 / 共 {r['methods_total']} 命中】")
            for m in r['methods']:
                lines.append(f"  {m['cls']}.{m['n']}  RVA {m['rva'] or '—'}"
                             + (' [static]' if m['static'] else '')
                             + (f"  // {m['cn']}" if m['cn'] else ''))
        if not lines:
            return f'未找到匹配 "{q}" 的类或方法。'
        return '\n'.join(lines)

    if name == 'find_class':
        cname = (a.get('class_name') or '').strip()
        if not cname:
            return '缺少 class_name 参数。'
        r = api.find_class(cname)
        if not r.get('ok'):
            return r.get('error', '未找到')
        # 拿继承链
        d = api.get_class_detail(r['dll'], r['name'])
        chain = ' ▸ '.join(c['n'] for c in (d or {}).get('chain', [])) if d else r['name']
        return (f"找到: {r['name']}" + (f" ({r['cn']})" if r.get('cn') else '') +
                f"\n所在 DLL: {r['dll']}\n继承链: {chain}")

    if name == 'search_strings':
        q = (a.get('query') or '').strip()
        if not q:
            return '缺少 query 参数。'
        limit = int(a.get('limit') or 30)
        r = api.search_strings(q, limit)
        if not r.get('ok'):
            return r.get('error', '搜索失败')
        if not r['items']:
            return f'字符串表共 {r["count"]} 条, 没有匹配 "{q}" 的。'
        lines = [f'字符串表共 {r["count"]} 条, 命中 {r["total"]} 条 (显示 {len(r["items"])}):']
        for it in r['items']:
            text = it['data'][:80] + ('…' if len(it['data']) > 80 else '')
            lines.append(f"  [{it['index']}] {text}   (offset {it['offset']}, {it['len']}字符)")
        lines.append('\n用法: GG 里直接文本搜索该字符串 → 搜到的地址附近即相关逻辑;')
        lines.append('      或 IDA 搜字符串 xref 定位引用函数。')
        return '\n'.join(lines)

    if name == 'gen_frida_trace':
        scope = (a.get('scope') or 'class').strip()
        save_path = (a.get('save_path') or '').strip()
        cname = (a.get('class_name') or '').strip()
        dll = (a.get('dll') or '').strip() or None
        if scope == 'stars':
            code = api.gen_frida_trace_stars()
            label = '收藏项'
        else:
            if not cname:
                return 'scope=class 需要 class_name。'
            dll_name, cls = _find_class(api, cname, dll)
            if cls is None:
                return f'未找到类: {cname}'
            code = api.gen_frida_trace_class(dll_name, cls.name)
            label = f'{cls.name} 全部方法'
        if not code:
            return f'{label} 没有可追踪的方法 (需带 RVA)。'
        if save_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(code)
                return (f'已导出追踪脚本: {save_path}\n({len(code.splitlines())} 行, {label})\n'
                        f'用法: frida -U -f 包名 -l 该文件, 游戏里做操作看日志。')
            except Exception as e:
                return f'写文件失败: {e}\n\n脚本内容如下:\n{code}'
        return code

    if name == 'migrate_hook':
        code = (a.get('hook_code') or '').strip()
        if not code:
            return '缺少 hook_code 参数 (粘贴老版本 hook 代码全文)。'
        r = api.migrate_hook(code)
        if not r.get('ok'):
            return ('迁移失败: ' + str(r.get('error'))
                    + '\n前置条件: 当前加载旧版 dump + compare_dumps 已加载新版 dump。')
        lines = [f"🛫 迁移完成: 改写 {len(r['changed'])} 个 RVA · "
                 f"不变 {len(r['same'])} · 新版没有 {len(r['missing'])} · 无关常量 {r['unknown']}"]
        if r['changed']:
            lines.append('RVA 变更:')
            for c in r['changed'][:40]:
                lines.append(f"  {c['old']} → {c['new']}   ({c['method']}"
                             + (' ⚠同名重载' if c.get('ambiguous') else '') + ')')
        if r['missing']:
            lines.append('❌ 新版 dump 里找不到 (方法被删/改名), 代码中保持原样:')
            for c in r['missing'][:20]:
                lines.append(f"  {c['old']}   ({c['method']})")
        lines.append('\n=== 迁移后的代码 ===\n')
        lines.append(r['code'])
        return '\n'.join(lines)

    if name == 'gen_hook_project':
        scope = (a.get('scope') or 'class').strip()
        save_path = (a.get('save_path') or '').strip()
        cname = (a.get('class_name') or '').strip()
        dll = (a.get('dll') or '').strip() or None
        if scope == 'stars':
            data, proj = api.gen_hook_project(scope='stars')
            label = '收藏项'
        else:
            if not cname:
                return 'scope=class 需要 class_name。'
            dll_name, cls = _find_class(api, cname, dll)
            if cls is None:
                return f'未找到类: {cname}'
            data, proj = api.gen_hook_project(dll_name, cls.name, scope='class')
            label = f'{cls.name} 类'
        if not data:
            return f'{label} 没有带 RVA 的方法, 无法生成工程。'
        if save_path:
            try:
                if not save_path.lower().endswith('.zip'):
                    save_path += '.zip'
                os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
                with open(save_path, 'wb') as f:
                    f.write(data)
                return (f'已导出 Hook 工程压缩包: {save_path}\n'
                        f'({proj} · {label} · 解压后先克隆 Dobby 再 bash build.sh, 见工程内 README)')
            except Exception as e:
                return f'写文件失败: {e}'
        import zipfile as _zf
        try:
            names = _zf.ZipFile(io.BytesIO(data)).namelist()
        except Exception:
            names = ['(?)']
        return (f'Hook 工程 {proj} 已生成 ({len(data) // 1024} KB, {label})。\n'
                f'包含: ' + ', '.join(n.split('/', 1)[-1] for n in names) +
                f'\n给 save_path 参数可直接落盘 zip; 解压后步骤见 README (克隆 Dobby → build.sh)。')

    # ==================== 📱 ADB 调试 ====================
    def _pick_serial():
        s = (a.get('serial') or '').strip()
        if s:
            api.adb_select(s)
        serial = api._adb_dev()
        if not serial:
            raise RuntimeError('没有可用设备 — 先用 adb_devices 查看连接情况')
        return serial

    if name == 'adb_devices':
        try:
            r = api.adb_devices()
        except Exception as e:
            return f'adb 不可用: {e}'
        if not r.get('ok'):
            return '❌ ' + r.get('error', '')
        devs = r['devices']
        if not devs:
            return ('没有检测到设备。排查:\n'
                    '  1. USB 调试是否开启 (开发者选项)\n'
                    '  2. USB 连接时手机上是否点了授权弹窗\n'
                    '  3. WiFi 调试: 手机和电脑同一局域网, adb connect ip:5555')
        lines = [f'检测到 {len(devs)} 台设备:']
        for d in devs:
            mark = ' ←当前' if d['serial'] == r.get('serial') else ''
            lines.append(f"  {d['serial']}{mark}  [{d['state']}] {d['model']} "
                         f"Android {d['android']} {d['abi']} {'root✓' if d['root'] else '无root'}"
                         + (f" 剩余{d['disk_free']}" if d.get('disk_free') else ''))
        if len(devs) > 1:
            lines.append('\n多台设备 — 后续 ADB 调用请带 serial 参数指定。')
        return '\n'.join(lines)

    if name == 'adb_apps':
        try:
            serial = _pick_serial()
            r = api.adb_apps(refresh=True)
        except Exception as e:
            return f'❌ {e}'
        if not r.get('ok'):
            return '❌ ' + r['error']
        apps = r['apps']
        ck_msg = ''
        pkg_ck = (a.get('check_pkg') or '').strip()
        if pkg_ck:
            ck = api.adb_check_il2cpp(pkg_ck)
            if ck.get('ok'):
                ck_msg = (f"\n\n检测 {pkg_ck}: " +
                          ('✅ 是 Unity/il2cpp 游戏, 架构 ' + ck.get('arch', '?')
                           if ck['il2cpp'] else '❌ 不是 il2cpp 游戏 (APK 内无 libil2cpp.so)'))
            else:
                ck_msg = '\n\n检测失败: ' + ck.get('error', '')
        lines = [f'{serial} 上共 {len(apps)} 个第三方应用 (前 60 个):']
        for it in apps[:60]:
            lines.append(f"  {it['pkg']}")
        if len(apps) > 60:
            lines.append(f'  … (共 {len(apps)} 个)')
        return '\n'.join(lines) + ck_msg

    if name == 'adb_pull_il2cpp':
        pkg = (a.get('pkg') or '').strip()
        if not pkg:
            return '缺少 pkg 参数。'
        try:
            serial = _pick_serial()
        except Exception as e:
            return f'❌ {e}'
        dest = (a.get('dest_dir') or '').strip()
        if not dest:
            if getattr(sys, 'frozen', False):
                base = os.path.dirname(os.path.abspath(sys.executable))
            else:
                base = _THIS_DIR
            dest = os.path.join(base, '手机游戏材料', pkg)
        try:
            r = AK.pull_il2cpp(serial, pkg, dest)
        except Exception as e:
            return f'❌ 提取失败: {e}'
        if not r.get('ok'):
            return '❌ ' + r.get('error', '提取失败')
        lines = [f'✅ 提取完成 → {dest}  (架构 {r.get("arch")}, 方式 {r.get("strategy")})']
        if r.get('so'):
            lines.append(f"  libil2cpp.so: {r['sizes'].get('so', 0) // 1048576} MB")
        if r.get('metadata'):
            lines.append(f"  global-metadata.dat: {r['sizes'].get('metadata', 0) // 1048576} MB")
        if r.get('warn'):
            lines.append('  ⚠ ' + r['warn'])
        lines.append('\n下一步: 把这两个文件喂给 Il2CppDumper (或让用户在工具里打开产出的 dump.cs)。')
        return '\n'.join(lines)

    if name == 'adb_push_file':
        local = (a.get('local') or '').strip()
        if not local:
            return '缺少 local 参数 (本地文件完整路径)。'
        try:
            serial = _pick_serial()
            r = api.adb_push(local_path=local, remote=(a.get('remote') or '').strip() or None)
        except Exception as e:
            return f'❌ {e}'
        if not r.get('ok'):
            return '❌ ' + r['error']
        return f"✅ 已推送到手机 {serial}: {r['remote']}\n(GG 里文件选择器切到 Download 目录即可看到)"

    if name == 'adb_shell':
        cmd = (a.get('cmd') or '').strip()
        if not cmd:
            return '缺少 cmd 参数。'
        try:
            serial = _pick_serial()
            r = api.adb_shell(cmd, root=bool(a.get('root')))
        except Exception as e:
            return f'❌ {e}'
        if not r.get('ok'):
            return '❌ ' + r['error']
        return f'[{serial}]$ {cmd}\n{"-" * 40}\n' + r['text']

    if name == 'adb_logcat':
        try:
            serial = _pick_serial()
            r = api.adb_logcat(keyword=(a.get('keyword') or '').strip(),
                               lines=int(a.get('lines') or 300))
        except Exception as e:
            return f'❌ {e}'
        if not r.get('ok'):
            return '❌ ' + r['error']
        text = r['text']
        if len(text) > 12000:
            text = text[:12000] + f'\n… (截断, 共 {len(r["text"])} 字符, 调小 lines 或加 keyword)'
        return f'logcat 快照 ({serial}):\n{"-" * 40}\n' + text

    if name == 'adb_frida':
        action = (a.get('action') or '').strip()
        try:
            serial = _pick_serial()
        except Exception as e:
            return f'❌ {e}'
        if action == 'status':
            r = api.adb_frida_status()
            if not r.get('ok'):
                return '❌ ' + r['error']
            cli = r.get('cli') or {}
            srv = r.get('server') or {}
            lines = [
                f'设备: {serial}',
                f"本机 frida CLI: {cli.get('version', '未安装')}  ({cli.get('frida', '-')})"
                if cli else '本机 frida CLI: ❌ 未安装 (pip install frida-tools)',
                f"手机 frida-server: {'✅ 运行中' if srv.get('running') else '❌ 未运行'}"
                + (' (二进制已就位)' if srv.get('has_bin') else ' (未推送二进制)'),
            ]
            if r.get('running_script'):
                lines.append('有一个脚本正在运行 (action=read 看输出, stop_run 停止)')
            hid = r.get('hidden')
            if hid:
                lines.append(f"🥷 隐身模式运行中: {hid['name']} @ 127.0.0.1:{hid['port']} "
                             f"(特征串已 patch, ps 扫不到 frida)")
            lines.append('\n提示: CLI 与 server 需同大版本; 启动用 action=start (hidden=true 反检测)')
            return '\n'.join(lines)
        if action == 'start':
            lb = (a.get('local_bin') or '').strip() or None
            r = api.adb_frida_start(local_bin=lb, hidden=bool(a.get('hidden')))
            if not r.get('ok'):
                return '❌ ' + r.get('error', '启动失败')
            if r.get('port'):
                # 🥷 隐身启动结果
                st = r.get('stats') or {}
                patched = ', '.join(f"{k}×{v['count']}" for k, v in st.items()) or '无'
                return (f"✅ 🥷 隐身 server 已启动: {r['name']} @ 127.0.0.1:{r['port']}\n"
                        f"特征串已随机化: {patched}\n"
                        f"frida-ps 验证: {'✅ 通过' if r.get('frida_ps_ok') else '未验证'}\n"
                        f"{'⚠ ps 验证失败 — 检查本机 CLI 与 server 版本' if r.get('frida_ps_ok') is False else ''}")
            return (f'✅ frida-server 已在 {serial} 上启动'
                    + (' (frida-ps 验证通过)' if r.get('frida_ps_ok') else ' (未验证 — 本机 CLI 版本可能不匹配)'))
        if action == 'stop_server':
            r = api.adb_frida_stop_server()
            if not r.get('ok'):
                return '❌ 停止失败'
            return '✅ 已停止 (隐身实例已清理: 杀进程+移除转发+删远端文件)' if r.get('hidden_stopped') else '✅ 已停止'
        if action == 'ps':
            r = api.adb_frida_ps()
            if not r.get('ok'):
                return '❌ ' + r['error'] + '\n(先 action=start 启动 server)'
            text = r['text']
            return f'手机进程 (frida-ps, 前 60 行):\n{text[:6000]}'
        if action == 'run':
            r = api.adb_frida_run((a.get('pkg') or '').strip(),
                                  script_path=(a.get('script') or '').strip() or None,
                                  spawn=bool(a.get('spawn', True)))
            if not r.get('ok'):
                return '❌ ' + r['error']
            return '✅ 脚本已启动: ' + r.get('cmd', '') + '\n用 action=read (after=0) 读输出。'
        if action == 'run_code':
            r = api.adb_frida_run_code((a.get('pkg') or '').strip(),
                                       a.get('code') or '',
                                       spawn=a.get('spawn'),
                                       cls=a.get('cls'), m=a.get('m'))
            if not r.get('ok'):
                return '❌ ' + r['error']
            return (f"✅ 已在 {serial} 上以 {r.get('mode')} 模式启动: {r.get('pkg')}\n"
                    f'用 action=read (after=0) 读输出, stop_run 停止。')
        if action == 'read':
            r = api.adb_frida_read(int(a.get('after') or 0))
            lines = r.get('lines') or []
            head = f"[已收 {r.get('total')} 行, {'运行中' if r.get('alive') else '已退出'}]\n"
            return head + '\n'.join(lines[:300])
        if action == 'stop_run':
            api.adb_frida_stop_run()
            return '✅ 脚本已停止'
        return f'未知 action: {action} (status/start/stop_server/ps/run/run_code/read/stop_run)'

    return f'未知工具: {name}'


def run_mcp_server():
    """MCP 服务器主循环: JSON-RPC 2.0 over stdio"""
    import io
    import threading

    # Windows 控制台默认 GBK, 显式切 UTF-8 (MCP 协议要求)
    if hasattr(sys.stdin, 'buffer'):
        sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', newline='\n')

    api = Api()
    api.loading = False

    # 后台线程自动加载最近打开的 dump (不阻塞 initialize 握手)
    def _bg_load():
        api.loading = True
        try:
            recent = api.get_recent()
            if recent:
                api.load_file(recent[0])
        except Exception:
            pass
        finally:
            api.loading = False

    threading.Thread(target=_bg_load, daemon=True).start()

    def send(obj):
        try:
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
            sys.stdout.flush()
        except Exception:
            pass

    def not_ready():
        if getattr(api, 'loading', False):
            return 'dump 正在后台加载中 (大文件约 10 秒), 请稍后重试。'
        if not api.dlls:
            return ('尚未加载 dump.cs。请用 load_dump(path) 加载, '
                    '或在桌面版软件里打开过文件后再启动 MCP。')
        return None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get('method', '')
        rid = req.get('id')
        if rid is None:
            continue  # 通知类消息 (如 notifications/initialized) 无需响应
        if method == 'initialize':
            pv = (req.get('params') or {}).get('protocolVersion', '2024-11-05')
            send({'jsonrpc': '2.0', 'id': rid, 'result': {
                'protocolVersion': pv,
                'capabilities': {'tools': {}},
                'serverInfo': {'name': 'dump-tool', 'version': '1.0.0'},
            }})
        elif method == 'ping':
            send({'jsonrpc': '2.0', 'id': rid, 'result': {}})
        elif method == 'tools/list':
            send({'jsonrpc': '2.0', 'id': rid, 'result': {'tools': MCP_TOOLS}})
        elif method == 'tools/call':
            p = req.get('params') or {}
            tname = p.get('name', '')
            targs = p.get('arguments') or {}
            try:
                text = _mcp_dispatch(api, tname, targs, not_ready)
                send({'jsonrpc': '2.0', 'id': rid,
                      'result': {'content': [{'type': 'text', 'text': text}]}})
            except Exception as e:
                send({'jsonrpc': '2.0', 'id': rid,
                      'result': {'isError': True,
                                 'content': [{'type': 'text', 'text': f'工具执行出错: {e}'}]}})
        else:
            send({'jsonrpc': '2.0', 'id': rid,
                  'error': {'code': -32601, 'message': 'Method not found: ' + method}})


# ============================================================
# 6. 主入口
# ============================================================
def _webview2_installed():
    """检测 Windows 上是否安装了 Microsoft Edge WebView2 Runtime (pywebview 渲染依赖)"""
    if sys.platform != 'win32':
        return True
    # 1) 注册表检测 (最可靠)
    try:
        import winreg
        client_id = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
        subkeys = [
            'SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\' + client_id,
            'SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\' + client_id,
        ]
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sk in subkeys:
                try:
                    winreg.OpenKey(hive, sk).Close()
                    return True
                except OSError:
                    continue
    except ImportError:
        pass
    # 2) 目录检测 (兜底)
    for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles')):
        if base and os.path.isdir(os.path.join(base, 'Microsoft', 'EdgeWebView', 'Application')):
            return True
    return False


def _warn_webview2_missing():
    """WebView2 缺失时弹出中文提示框 (不依赖 WebView2 自身, 用原生 MessageBox)"""
    msg = (
        '检测到系统缺少「Microsoft Edge WebView2 运行时」，无法显示程序界面。\n\n'
        '解决方法（任选其一）：\n'
        '1. 安装或更新微软 Edge 浏览器；\n'
        '2. 到微软官网下载 WebView2 Runtime 安装：\n'
        '   https://developer.microsoft.com/microsoft-edge/webview2/\n'
        '   选择「Evergreen Standalone Installer」离线安装包。\n\n'
        '安装完成后重新启动本软件即可。'
    )
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, 'dump 翻译工作台 — 缺少运行组件', 0x10)
    except Exception:
        print(msg)


def main():
    _dbg('=== GUI 启动 ===')

    # 启动前检测渲染组件, 缺失则友好提示而非白屏/闪退
    if not _webview2_installed():
        _dbg('WebView2 缺失, 退出')
        _warn_webview2_missing()
        sys.exit(1)

    import webview

    api = Api()

    # 命令行传入文件路径则自动加载 (前端 ready 后通过 get_auto_load_path 主动拉取, 避免时序竞态)
    auto_load = None
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        auto_load = os.path.abspath(sys.argv[1])
    api.auto_load_path = auto_load

    frontend_dir = _resource_path('frontend')
    index_html = os.path.join(frontend_dir, 'index.html')

    # 把自动加载路径传给前端
    window = webview.create_window(
        'dump 翻译工作台',
        index_html,
        js_api=api,
        width=1360, height=860,
        min_size=(1024, 680),
        background_color='#0d1117',
    )
    api._window = window

    webview.start(debug=False)


if __name__ == '__main__':
    main()
