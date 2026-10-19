# -*- coding: utf-8 -*-
"""
adb_kit.py — dump工具 的 ADB 调试模块
手机连接 / 应用管理 / il2cpp 材料提取 / 文件推送 / 日志抓取 / Frida 调试

依赖: 本机 adb.exe (PATH 或常见位置自动寻找, 也可手动指定)
技术路线 (真机验证过):
  - pm path 拿 APK 路径; toybox unzip -l 列 APK 内容
  - adb exec-out "unzip -p <apk> <entry>" 流式提取单文件 (不落地整个 APK, 二进制安全)
  - libil2cpp.so 提取后校验 ELF magic (\x7fELF), metadata 校验 0xFAB11BAF
  - su -c (Magisk) 跑 frida-server; 本机 frida CLI (frida.exe) 跑脚本
"""
import os
import random
import re
import shlex
import shutil
import subprocess
import threading
import time

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


class AdbError(Exception):
    """ADB 操作异常 (带用户可读信息)"""


# ============================================================
# 基础: 找 adb / 执行命令
# ============================================================

_ADB_PATH = None          # 进程内缓存
_USER_ADB = None          # 用户手动指定 (存 recent.json, 优先级最高)


def set_adb_path(path):
    """用户手动指定 adb.exe 路径 (None 清除)"""
    global _USER_ADB, _ADB_PATH
    _USER_ADB = path if (path and os.path.isfile(path)) else None
    _ADB_PATH = None  # 强制重找
    return _USER_ADB


def find_adb(refresh=False):
    """找 adb.exe: 用户指定 → PATH → 常见位置. 找不到返回 None"""
    global _ADB_PATH
    if _USER_ADB and os.path.isfile(_USER_ADB):
        return _USER_ADB
    if not refresh and _ADB_PATH and os.path.isfile(_ADB_PATH):
        return _ADB_PATH
    # 1) PATH
    p = shutil.which('adb')
    if p:
        _ADB_PATH = os.path.abspath(p)
        return _ADB_PATH
    # 2) 常见位置
    candidates = [
        r'D:\platform-tools\adb.exe',
        r'C:\platform-tools\adb.exe',
        r'C:\adb\adb.exe',
        r'C:\Android\platform-tools\adb.exe',
        os.path.expandvars(r'%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe'),
        os.path.expandvars(r'%PROGRAMFILES%\Android\platform-tools\adb.exe'),
        os.path.expandvars(r'%PROGRAMFILES(X86)%\Android\android-sdk\platform-tools\adb.exe'),
        os.path.expanduser(r'~\platform-tools\adb.exe'),
        os.path.expanduser(r'~\Downloads\platform-tools\adb.exe'),
        os.path.expanduser(r'~\Desktop\platform-tools\adb.exe'),
    ]
    for c in candidates:
        if os.path.isfile(c):
            _ADB_PATH = c
            return c
    return None


def adb_version():
    adb = find_adb()
    if not adb:
        return ''
    try:
        r = subprocess.run([adb, 'version'], capture_output=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        m = re.search(r'Version ([\d.]+)', (r.stdout or b'').decode('utf-8', 'replace'))
        return m.group(1) if m else '?'
    except Exception:
        return '?'


def run_adb(args, device=None, timeout=60, binary=False):
    """执行 adb 命令.
    binary=True: exec-out 模式返回原始字节 (适合流式提取)
    普通模式返回 str; 命令失败(非零退出)抛 AdbError"""
    adb = find_adb()
    if not adb:
        raise AdbError('本机找不到 adb.exe — 请安装 Android platform-tools '
                       '(https://developer.android.com/tools/releases/platform-tools), '
                       '解压后在 ADB 面板里手动指定 adb.exe 路径')
    cmd = [adb]
    if device:
        cmd += ['-s', device]
    cmd += [str(a) for a in args]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise AdbError(f'命令超时 ({timeout}s): adb {" ".join(str(a) for a in args[:3])} …')
    except FileNotFoundError:
        raise AdbError(f'无法执行: {adb}')
    if binary:
        return r.stdout or b''
    out = (r.stdout or b'').decode('utf-8', 'replace').strip()
    err = (r.stderr or b'').decode('utf-8', 'replace').strip()
    # adb devices 列表等即使有 stderr 也有有效输出; 统一: 失败时带 stderr 抛错
    if r.returncode != 0:
        msg = (out + ' | ' + err).strip(' |')
        # 常见错误给友好提示
        if 'not found' in msg and 'device' in msg:
            raise AdbError('设备未连接或已离线 — 请重新插拔 USB / adb connect 后刷新')
        if 'more than one device' in msg or "more than one" in err:
            raise AdbError('检测到多台设备 — 请先在设备页选择要操作的设备')
        if 'offline' in msg:
            raise AdbError('设备离线 (offline) — 手机上重新授权 USB 调试或重连')
        raise AdbError(msg or f'adb 退出码 {r.returncode}')
    return out


def shell(cmd, device=None, timeout=30, root=False):
    """adb shell 执行; root=True 时用 su -c (Magisk).
    返回 stdout 文本. 命令失败不抛异常(壳命令普遍非零), 原样返回输出"""
    if root:
        full = f'su -c {shlex.quote(cmd)}'
    else:
        full = cmd
    adb = find_adb()
    if not adb:
        raise AdbError('本机找不到 adb.exe')
    c = [adb]
    if device:
        c += ['-s', device]
    c += ['shell', full]
    try:
        r = subprocess.run(c, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise AdbError(f'shell 超时 ({timeout}s): {cmd[:60]}')
    out = (r.stdout or b'').decode('utf-8', 'replace')
    err = (r.stderr or b'').decode('utf-8', 'replace')
    # su 不存在/未授权的典型输出
    if root and ('not found' in err or 'denied' in err or 'unknown' in out.lower()[:200]):
        pass  # 让调用方看输出判断
    return (out + (('\n' + err) if err and not out else '')).strip()


def has_root(serial):
    """检测设备是否有可用 root (su)"""
    try:
        out = shell('id', device=serial, timeout=10, root=True)
        return 'uid=0' in out
    except AdbError:
        return False


# ============================================================
# 设备管理
# ============================================================

def list_devices():
    """列出设备: [{serial, state, model, product, transport}]"""
    try:
        out = run_adb(['devices', '-l'], timeout=15)
    except AdbError as e:
        # daemon 可能没起, 尝试 start-server 后重试一次
        try:
            subprocess.run([find_adb(), 'start-server'], capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
            out = run_adb(['devices', '-l'], timeout=15)
        except Exception:
            raise e
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('*') or line.startswith('List of'):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        d = {'serial': parts[0], 'state': parts[1], 'model': '', 'product': ''}
        for p in parts[2:]:
            if p.startswith('model:'):
                d['model'] = p[6:]
            elif p.startswith('product:'):
                d['product'] = p[8:]
        devices.append(d)
    return devices


def connect_wifi(addr):
    """adb connect ip:port"""
    if ':' not in addr:
        addr = addr + ':5555'
    out = run_adb(['connect', addr], timeout=20)
    ok = ('connected' in out and 'cannot' not in out)
    return {'ok': ok, 'msg': out, 'addr': addr}


def device_info(serial):
    """设备详细信息: 型号/安卓/ABI/root 等"""
    def gp(prop):
        return shell(f'getprop {prop}', device=serial, timeout=10)
    info = {
        'serial': serial,
        'model': gp('ro.product.model') or '?',
        'brand': gp('ro.product.brand'),
        'android': gp('ro.build.version.release'),
        'sdk': gp('ro.build.version.sdk'),
        'abi': gp('ro.product.cpu.abi'),
        'security': gp('ro.build.version.security_patch'),
    }
    info['root'] = has_root(serial)
    # 存储剩余空间
    try:
        df = shell('df -h /data', device=serial, timeout=10)
        m = re.search(r'(\d+[GKM])\s+\d+[GKM]\s+(\d+[GKM])\s+\d+%', df)
        if m:
            info['disk_free'] = m.group(2)
    except Exception:
        pass
    return info


# ============================================================
# 应用管理
# ============================================================

def list_packages(serial, third_only=True):
    """已安装应用: [{pkg, apk}] (apk 为 base.apk 路径, split apk 另算)"""
    flag = '-3' if third_only else ''
    out = shell(f'pm list packages {flag} -f', device=serial, timeout=60)
    pkgs = []
    seen = set()
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r'package:(\S+)=(\S+)$', line)
        if not m:
            continue
        apk, pkg = m.group(1), m.group(2)
        if pkg in seen:
            continue
        seen.add(pkg)
        pkgs.append({'pkg': pkg, 'apk': apk})
    pkgs.sort(key=lambda x: x['pkg'])
    return pkgs


def pkg_apks(serial, pkg):
    """某包的全部 apk 路径 (base 在前)"""
    out = shell(f'pm path {pkg}', device=serial, timeout=20)
    apks = []
    for line in out.splitlines():
        m = re.match(r'package:(\S+)', line.strip())
        if m and m.group(1) not in apks:
            apks.append(m.group(1))
    return apks


def check_il2cpp(serial, pkg):
    """检测某应用是否 il2cpp (Unity) 游戏 → dict(il2cpp, arch, so_entry, metadata_entry, apks)"""
    apks = pkg_apks(serial, pkg)
    if not apks:
        return {'il2cpp': False, 'error': f'包 {pkg} 不存在或未安装'}
    # 1) APK 内找 libil2cpp.so (任意 abi)
    so_entry, so_apk, arch = None, None, ''
    for apk in apks:
        out = shell(f"unzip -l {shlex.quote(apk)} 2>/dev/null | grep libil2cpp.so",
                    device=serial, timeout=60)
        for line in out.splitlines():
            m = re.search(r'(lib/([\w-]+)/libil2cpp\.so)\s*$', line.strip())
            if m:
                so_entry, so_apk = m.group(1), apk
                arch = m.group(2)
                break
        if so_entry:
            break
    # 2) 找 metadata (只在 base.apk / 各 apk 的 assets 里)
    meta_entry, meta_apk = None, None
    for apk in apks:
        out = shell(
            f"unzip -l {shlex.quote(apk)} 2>/dev/null | "
            f"grep -E 'global[-_]metadata\\.dat'",
            device=serial, timeout=60)
        m = re.search(r'(assets/\S*global[-_]metadata\.dat)\s*$', out.strip())
        if m:
            meta_entry, meta_apk = m.group(1), apk
            break
    # 3) 兜底: 安装目录已解出的 so (extractNativeLibs=true 的游戏)
    installed_so = None
    if not so_entry:
        out = shell(f"pm path {pkg}", device=serial, timeout=20)
        base_dir = ''
        m = re.match(r'package:(.+?/base\.apk)', out.strip())
        if m:
            base_dir = os.path.dirname(m.group(1))  # /data/app/.../pkg-xxx
            for abi in ('arm64', 'arm'):
                p = f'{base_dir}/lib/{abi}/libil2cpp.so'
                r = shell(f'[ -f {p} ] && echo YES', device=serial, timeout=10)
                if 'YES' in r:
                    installed_so, arch = p, ('arm64-v8a' if abi == 'arm64' else 'armeabi-v7a')
                    break
    return {
        'il2cpp': bool(so_entry or installed_so),
        'arch': arch,
        'so_entry': so_entry,
        'so_apk': so_apk,
        'installed_so': installed_so,
        'metadata_entry': meta_entry,
        'metadata_apk': meta_apk,
        'apks': apks,
    }


def batch_check_il2cpp(serial, progress_cb=None, stop_flag=None):
    """批量检测所有第三方应用中的 il2cpp 游戏.
    在手机端一条 shell 循环跑 (170 包约 2 分钟), progress_cb(done, total, pkg) 回调"""
    # 先落包列表到手机临时文件, 再循环检测 (避免长管道断连)
    shell('pm list packages -3 -f > /data/local/tmp/_dumptool_pkgs.txt', device=serial, timeout=30)
    total = int((shell('wc -l < /data/local/tmp/_dumptool_pkgs.txt', device=serial, timeout=10) or '0').strip() or 0)
    if not total:
        return []
    # 分批拉取结果: 每 15 个包跑一段, 期间回报进度 (adb 单命令 timeout 限制)
    unity, done = [], 0
    BATCH = 15
    while done < total:
        if stop_flag and stop_flag.is_set():
            break
        start, end = done, min(done + BATCH, total)
        script = (
            f'sed -n "{start + 1},{end}p" /data/local/tmp/_dumptool_pkgs.txt | '
            'while IFS="=" read -r a p; do '
            'apk=${a#package:}; [ -n "$apk" ] || continue; '
            'unzip -l "$apk" 2>/dev/null | grep -q libil2cpp.so && echo "UNITY $p $apk"; '
            'done'
        )
        out = shell(script, device=serial, timeout=120)
        for line in out.splitlines():
            m = re.match(r'UNITY\s+(\S+)\s+(\S+)', line.strip())
            if m:
                unity.append({'pkg': m.group(1), 'apk': m.group(2)})
        done = end
        if progress_cb:
            try:
                progress_cb(done, total, '')
            except Exception:
                pass
    shell('rm -f /data/local/tmp/_dumptool_pkgs.txt', device=serial, timeout=10)
    return unity


# ============================================================
# il2cpp 材料提取 (libil2cpp.so + global-metadata.dat)
# ============================================================

ELF_MAGIC = b'\x7fELF'
IL2CPP_META_MAGIC = b'\xaf\x1b\xb1\xfa'   # 0xFAB11BAF little-endian
METADATA_KEYS = [
    'assets/bin/Data/il2cpp_data/Metadata/global_metadata.dat',  # Unity 2019+ / 新格式
    'assets/bin/Data/Managed/Metadata/global-metadata.dat',      # 旧格式
]


def _exec_out_unzip(serial, apk, entry, timeout=900):
    """流式提取 APK 内单文件 (exec-out 二进制安全). 返回 bytes, 失败 None"""
    cmd = f"unzip -p {shlex.quote(apk)} {shlex.quote(entry)}"
    try:
        data = run_adb(['exec-out', cmd], device=serial, timeout=timeout, binary=True)
    except AdbError:
        return None
    if not data or len(data) < 64:
        return None
    # toybox 错误会输出到 stderr(被合并), 二进制正确性靠 magic 由调用方校验
    return data


def _extract_apk_zipfile(serial, apk, entry, dest, progress_cb=None):
    """兜底策略: pull 整个 apk 到临时文件, Python zipfile 提取"""
    import zipfile
    tmp_apk = dest + '.apk.tmp'
    try:
        if progress_cb:
            progress_cb(f'pull', '正在拉取整个 APK (可能较慢)…')
        run_adb(['pull', apk, tmp_apk], device=serial, timeout=1800)
        with zipfile.ZipFile(tmp_apk) as zf:
            with zf.open(entry) as ef, open(dest, 'wb') as out:
                while True:
                    chunk = ef.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
        return True
    except Exception:
        return False
    finally:
        try:
            os.remove(tmp_apk)
        except OSError:
            pass


def pull_il2cpp(serial, pkg, dest_dir, progress_cb=None):
    """从手机已装游戏提取 il2cpp 逆向材料 → 本地 dest_dir.
    返回 {ok, so, metadata, arch, sizes, strategy} 或 {ok:False, error}"""
    def prog(msg):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass
    prog('正在检查应用…')
    ck = check_il2cpp(serial, pkg)
    if not ck['il2cpp']:
        return {'ok': False, 'error': f'{pkg} 不是 il2cpp/Unity 游戏 (APK 内无 libil2cpp.so)'}
    os.makedirs(dest_dir, exist_ok=True)
    result = {'ok': True, 'arch': ck['arch'], 'strategy': '', 'sizes': {}}

    # ---- 1. libil2cpp.so ----
    so_local = os.path.join(dest_dir, 'libil2cpp.so')
    got_so = False
    if ck['installed_so']:
        prog(f'正在拉取已解压的 {ck["installed_so"]} …')
        try:
            run_adb(['pull', ck['installed_so'], so_local], device=serial, timeout=900)
            with open(so_local, 'rb') as f:
                got_so = f.read(4) == ELF_MAGIC
            if got_so:
                result['strategy'] = 'install-dir'
        except AdbError:
            got_so = False
    if not got_so and ck['so_entry']:
        prog(f'正在流式提取 {ck["so_entry"]} (不落地整个 APK)…')
        data = _exec_out_unzip(serial, ck['so_apk'], ck['so_entry'])
        if data and data[:4] == ELF_MAGIC:
            with open(so_local, 'wb') as f:
                f.write(data)
            got_so = True
            result['strategy'] = 'stream'
    if not got_so and ck['so_entry']:
        prog('流式提取失败, 改为拉取整个 APK 解压…')
        if _extract_apk_zipfile(serial, ck['so_apk'], ck['so_entry'], so_local, progress_cb):
            with open(so_local, 'rb') as f:
                got_so = f.read(4) == ELF_MAGIC
            if got_so:
                result['strategy'] = 'pull-apk'
    if not got_so:
        return {'ok': False, 'error': 'libil2cpp.so 提取失败 (可尝试 root 模式重试)'}
    result['so'] = so_local
    result['sizes']['so'] = os.path.getsize(so_local)

    # ---- 2. global-metadata.dat ----
    meta_local = os.path.join(dest_dir, 'global-metadata.dat')
    got_meta = False
    if ck['metadata_entry']:
        prog(f'正在流式提取 {ck["metadata_entry"]} …')
        data = _exec_out_unzip(serial, ck['metadata_apk'], ck['metadata_entry'])
        if data and data[:4] == IL2CPP_META_MAGIC:
            with open(meta_local, 'wb') as f:
                f.write(data)
            got_meta = True
    else:
        # check_il2cpp 没找到命名规范的 metadata, 两个标准 key 逐个硬试
        base_apk = ck['apks'][0]
        for key in METADATA_KEYS:
            prog(f'正在尝试 {key} …')
            data = _exec_out_unzip(serial, base_apk, key)
            if data and data[:4] == IL2CPP_META_MAGIC:
                with open(meta_local, 'wb') as f:
                    f.write(data)
                got_meta = True
                break
    if not got_meta:
        # 兜底: pull base.apk 本地解压找 metadata
        base_apk = ck['apks'][0]
        prog('metadata 流式提取失败, 尝试拉取 base.apk 解压…')
        import zipfile
        tmp_apk = os.path.join(dest_dir, '_tmp_base.apk')
        try:
            run_adb(['pull', base_apk, tmp_apk], device=serial, timeout=1800)
            with zipfile.ZipFile(tmp_apk) as zf:
                names = zf.namelist()
                cand = [n for n in names if re.search(r'global[-_]metadata\.dat$', n)]
                if cand:
                    with zf.open(cand[0]) as ef, open(meta_local, 'wb') as out:
                        out.write(ef.read())
                    with open(meta_local, 'rb') as f:
                        got_meta = f.read(4) == IL2CPP_META_MAGIC
        except Exception:
            pass
        finally:
            try:
                if os.path.isfile(tmp_apk):
                    os.remove(tmp_apk)
            except OSError:
                pass
    # metadata 可选: 有些游戏 dump 只要 so + 旧 metadata; 拿不到只警告
    if got_meta:
        result['metadata'] = meta_local
        result['sizes']['metadata'] = os.path.getsize(meta_local)
    else:
        result['metadata'] = None
        result['warn'] = 'global-metadata.dat 未提取到 (APK 内未找到标准路径) — 只有 so 也够部分工具用'
    prog('完成')
    return result


# ============================================================
# 文件 / 截图 / 日志 / 进程
# ============================================================

SDCARD_DL = '/storage/emulated/0/Download'


def push_file(serial, local, remote=None):
    """推送文件到手机 (默认 /sdcard/Download/)"""
    if not os.path.isfile(local):
        raise AdbError(f'本地文件不存在: {local}')
    if not remote:
        remote = f'{SDCARD_DL}/{os.path.basename(local)}'
    run_adb(['push', local, remote], device=serial, timeout=900)
    return remote


def pull_file(serial, remote, local):
    """从手机拉文件到本地"""
    if not local:
        local = os.path.basename(remote)
    run_adb(['pull', remote, local], device=serial, timeout=1800)
    return local


def screenshot(serial, local_path):
    """手机截屏到本地 png"""
    data = run_adb(['exec-out', 'screencap -p'], device=serial, timeout=30, binary=True)
    if not data or data[:4] != b'\x89PNG':
        raise AdbError('截屏失败 (设备不支持 screencap?)')
    with open(local_path, 'wb') as f:
        f.write(data)
    return local_path


def logcat(serial, keyword=None, lines=300):
    """logcat 快照 (-d). keyword 过滤 (grep -i)"""
    n = max(10, min(int(lines or 300), 5000))
    cmd = f'logcat -d -t {n}'
    if keyword:
        cmd += f" | grep -i -F {shlex.quote(keyword)}"
    return shell(cmd, device=serial, timeout=60)


def list_processes(serial, keyword=None):
    """手机进程列表 (可按名过滤)"""
    out = shell('ps -A -o PID,NAME', device=serial, timeout=20)
    rows = []
    for line in out.splitlines():
        m = re.match(r'\s*(\d+)\s+(\S+)', line)
        if m:
            if keyword and keyword.lower() not in m.group(2).lower():
                continue
            rows.append({'pid': m.group(1), 'name': m.group(2)})
    return rows


def list_dir(serial, path='/sdcard/Download'):
    """列目录 (ls -la)"""
    return shell(f'ls -la {shlex.quote(path)}', device=serial, timeout=20)


# ============================================================
# Frida 调试
# ============================================================

def find_frida_cli():
    """找本机 frida CLI → {frida, frida_ps, version} 或 None"""
    frida = shutil.which('frida') or shutil.which('frida.exe')
    if not frida:
        return None
    frida_ps = shutil.which('frida-ps') or shutil.which('frida-ps.exe')
    ver = ''
    try:
        r = subprocess.run([frida, '--version'], capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        ver = (r.stdout or b'').decode().strip()
    except Exception:
        pass
    return {'frida': frida, 'frida_ps': frida_ps, 'version': ver}


def find_local_frida_server():
    """常见位置找 frida-server 二进制 (未下载返回 None)"""
    pats = []
    for d in (os.path.expanduser('~/Downloads'), os.path.expanduser('~/Desktop'),
              'D:\\', 'D:\\frida', 'C:\\frida', 'D:\\tool', 'D:\\Android'):
        if os.path.isdir(d):
            try:
                for name in os.listdir(d):
                    low = name.lower()
                    if low.startswith('frida-server') and ('android' in low or 'arm64' in low or low.endswith('.xz')):
                        p = os.path.join(d, name)
                        if os.path.isfile(p):
                            pats.append(p)
            except OSError:
                pass
    # 直接叫 frida-server 的文件 (无后缀)
    for d in (os.path.expanduser('~/Downloads'), os.path.expanduser('~/Desktop'), 'D:\\'):
        p = os.path.join(d, 'frida-server')
        if os.path.isfile(p) and p not in pats:
            pats.append(p)
    return pats[0] if pats else None


def frida_server_status(serial):
    """手机端 frida-server 运行状态"""
    out = shell("ps -A | grep -i frida | grep -v grep", device=serial, timeout=15)
    running = bool(out.strip())
    # 检查手机里是否已放好二进制
    has_bin = 'YES' in shell('[ -f /data/local/tmp/frida-server ] && echo YES',
                             device=serial, timeout=10)
    return {'running': running, 'detail': out.strip(), 'has_bin': has_bin}


def frida_push_start(serial, local_bin, name='frida-server'):
    """推送本地 frida-server 到手机 → chmod → su 启动 → 验证.
    local_bin 是 .xz 时先本地解压"""
    local = local_bin
    if local_bin.endswith('.xz'):
        import lzma
        try:
            out_bin = local[:-3]
            with lzma.open(local_bin) as fin, open(out_bin, 'wb') as fout:
                fout.write(fin.read())
            local = out_bin
        except Exception as e:
            raise AdbError(f'解压 .xz 失败: {e}')
    if not os.path.isfile(local):
        raise AdbError(f'本地文件不存在: {local}')
    remote = f'/data/local/tmp/{name}'
    run_adb(['push', local, remote], device=serial, timeout=300)
    shell(f'chmod 755 {remote}', device=serial, timeout=15)
    # 先杀旧的再启动
    shell(f'pkill -f {name} 2>/dev/null', device=serial, timeout=10, root=True)
    time.sleep(0.5)
    # nohup + & 后台启动; adb shell 退出不杀子进程
    shell(f'nohup {remote} >/dev/null 2>&1 &', device=serial, timeout=10, root=True)
    # 等待起来
    for _ in range(10):
        time.sleep(0.5)
        st = frida_server_status(serial)
        if st['running']:
            # 进一步验证: 本机 frida-ps 能列进程才算真通 (如果本机有 frida CLI)
            cli = find_frida_cli()
            ps_ok = None
            if cli and cli['frida_ps']:
                try:
                    r = subprocess.run([cli['frida_ps'], '-D', serial], capture_output=True,
                                       timeout=25, creationflags=CREATE_NO_WINDOW)
                    ps_ok = r.returncode == 0
                except Exception:
                    ps_ok = None
            return {'ok': True, 'running': True, 'frida_ps_ok': ps_ok}
    return {'ok': False, 'error': 'frida-server 推送并启动了, 但进程没有存活 — '
                                  '多半是本机 frida CLI 与 server 版本不匹配 (需要同大版本)'}


def frida_stop_server(serial, name='frida-server'):
    shell(f'pkill -f {name} 2>/dev/null', device=serial, timeout=10, root=True)
    time.sleep(0.4)
    return not frida_server_status(serial)['running']


def frida_ps(serial):
    """本机 frida-ps 列手机进程 → 文本"""
    cli = find_frida_cli()
    if not cli or not cli['frida_ps']:
        raise AdbError('本机没装 frida CLI — pip install frida-tools 后重试')
    r = subprocess.run([cli['frida_ps'], '-D', serial], capture_output=True, timeout=40,
                       creationflags=CREATE_NO_WINDOW)
    out = (r.stdout or b'').decode('utf-8', 'replace')
    err = (r.stderr or b'').decode('utf-8', 'replace')
    if r.returncode != 0:
        raise AdbError('frida-ps 失败: ' + (err or out)[:300] +
                       ' — 请确认 frida-server 已启动且版本与本机 frida 一致')
    return out


# ============================================================
# 🥷 Frida 隐身模式 (反检测)
# ============================================================

# 同长度随机替换的特征串 (单边安全的: 只影响特征名/线程名/注入名, 不动协议串如 frida:rpc)
# ⚠ 不可 patch 导出符号 (如 frida_agent_main) — ELF 动态符号查找走 GNU hash 表,
#    改字符串不改 hash 会导致 dlsym 失败 (真机踩过: Failed to attach: undefined symbol)
FRIDA_HIDE_STRINGS = [
    b'frida-server',        # 进程名 / 默认文件名
    b'frida-agent',         # 注入到目标进程的 agent 名 (maps 可见)
    b'frida-gadget',
    b'frida-agent-main',
    b'LIBFRIDA',            # gum 版本横幅
    b'gum-js-loop',         # agent 在目标进程创建的线程名
    b'frida-inject',
    b'pool-frida',          # gum 线程池名
]


def _rand_str(n, chars='abcdefghijklmnopqrstuvwxyz'):
    import random
    return ''.join(random.choice(chars) for _ in range(n))


def patch_frida_server(local_bin, out_path=None):
    """对 frida-server 二进制做特征串同长度随机替换 → (patched_path, 统计).
    协议字符串 (frida:rpc / AUTH 等) 不动, 与官方客户端保持兼容."""
    if local_bin.endswith('.xz'):
        import lzma
        try:
            plain = local_bin[:-3]
            with lzma.open(local_bin) as fin, open(plain, 'wb') as fout:
                fout.write(fin.read())
            local_bin = plain
        except Exception as e:
            raise AdbError(f'解压 .xz 失败: {e}')
    if not os.path.isfile(local_bin):
        raise AdbError(f'本地文件不存在: {local_bin}')
    with open(local_bin, 'rb') as f:
        data = f.read()
    if data[:4] != b'\x7fELF':
        raise AdbError('不是 ELF 二进制 — 请选择 frida-server-*-android-arm64 文件')
    stats = {}
    for s in FRIDA_HIDE_STRINGS:
        n = data.count(s)
        if n:
            rep = _rand_str(len(s)).encode()
            data = data.replace(s, rep)
            stats[s.decode()] = {'count': n, 'to': rep.decode()}
    out = out_path or (local_bin + '.hidden')
    with open(out, 'wb') as f:
        f.write(data)
    return out, stats


def frida_push_start_hidden(serial, local_bin, name=None, port=None):
    """🥷 隐身启动: patch 特征串 → 随机文件名推送 → 只监听 127.0.0.1:随机端口 → adb forward.
    同时清理普通模式残留 (旧 frida-server 进程+文件), 不留泄漏点.
    返回 {'ok':.., 'port':.., 'remote':.., 'name':.., 'stats':.., 'frida_ps_ok':..}"""
    patched, stats = patch_frida_server(local_bin)
    name = name or _rand_str(8)
    port = int(port or random.randint(20000, 40000))
    remote = f'/data/local/tmp/{name}'
    # 清普通模式残留: 在跑的 frida-server 进程 + 留在 tmp 的 frida 文件 (都是泄漏点)
    shell('pkill -f frida-server 2>/dev/null', device=serial, timeout=10, root=True)
    shell('rm -f /data/local/tmp/frida-server /data/local/tmp/frida-server-* '
          '/data/local/tmp/re.frida.server 2>/dev/null', device=serial, timeout=10, root=True)
    run_adb(['push', patched, remote], device=serial, timeout=300)
    shell(f'chmod 755 {remote}', device=serial, timeout=15)
    # 杀掉之前同路径的隐身实例 (若有)
    shell(f'pkill -f {remote} 2>/dev/null', device=serial, timeout=10, root=True)
    time.sleep(0.5)
    # 只监听 127.0.0.1: 不对外暴露端口; 通过 adb forward 隧道访问
    shell(f'nohup {remote} -l 127.0.0.1:{port} >/dev/null 2>&1 &',
          device=serial, timeout=10, root=True)
    # 本机端口转发
    run_adb(['forward', f'tcp:{port}', f'tcp:{port}'], device=serial, timeout=15)
    # 验证: 进程存活 + frida-ps 走 -H 隧道
    ps_ok = None
    for _ in range(12):
        time.sleep(0.5)
        alive = shell(f'pidof {name}', device=serial, timeout=10).strip()
        if alive.isdigit():
            break
    else:
        run_adb(['forward', '--remove', f'tcp:{port}'], device=serial, timeout=10)
        return {'ok': False, 'error': '隐身 server 启动后进程未存活 (检查 ABI/版本)'}
    cli = find_frida_cli()
    if cli and cli['frida_ps']:
        try:
            r = subprocess.run([cli['frida_ps'], '-H', f'127.0.0.1:{port}'],
                               capture_output=True, timeout=25, creationflags=CREATE_NO_WINDOW)
            ps_ok = r.returncode == 0
            if not ps_ok:
                # 收尾再试一次 (server 刚起 tunnel 可能慢半拍)
                time.sleep(1.5)
                r = subprocess.run([cli['frida_ps'], '-H', f'127.0.0.1:{port}'],
                                   capture_output=True, timeout=25, creationflags=CREATE_NO_WINDOW)
                ps_ok = r.returncode == 0
        except Exception:
            ps_ok = None
    return {'ok': True, 'port': port, 'remote': remote, 'name': name,
            'stats': stats, 'frida_ps_ok': ps_ok}


def frida_ps_host(port):
    """本机 frida-ps 走 -H 隧道列手机进程 (隐身模式)"""
    cli = find_frida_cli()
    if not cli or not cli['frida_ps']:
        raise AdbError('本机没装 frida CLI — pip install frida-tools 后重试')
    r = subprocess.run([cli['frida_ps'], '-H', f'127.0.0.1:{port}'],
                       capture_output=True, timeout=40, creationflags=CREATE_NO_WINDOW)
    out = (r.stdout or b'').decode('utf-8', 'replace')
    err = (r.stderr or b'').decode('utf-8', 'replace')
    if r.returncode != 0:
        raise AdbError('frida-ps (隐身隧道) 失败: ' + (err or out)[:300] +
                       ' — 确认隐身 server 在运行且端口转发未被清理')
    return out


def frida_stop_hidden(serial, name, port):
    """停隐身 server: 杀进程 + 移除转发 + 删远端文件"""
    if name:
        shell(f'pkill -f /data/local/tmp/{name} 2>/dev/null', device=serial, timeout=10, root=True)
        shell(f'rm -f /data/local/tmp/{name}', device=serial, timeout=10, root=True)
    if port:
        try:
            run_adb(['forward', '--remove', f'tcp:{port}'], device=serial, timeout=10)
        except Exception:
            pass
    return True


class FridaRunner:
    """后台跑一个 Frida 脚本, 流式捕获输出.
    用法: r = FridaRunner(); r.start(serial, pkg, script_path, spawn=True)
          r.read_new()  # 拿新输出行
          r.stop()"""
    def __init__(self):
        self.proc = None
        self._lines = []
        self._lock = threading.Lock()
        self._thread = None
        self.cmd_str = ''

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, serial, pkg, script_path, spawn=True, extra_args=None, host=None):
        if self.alive():
            raise AdbError('已有一个脚本在运行, 请先停止')
        cli = find_frida_cli()
        if not cli:
            raise AdbError('本机没装 frida CLI — pip install frida-tools 后重试')
        if not os.path.isfile(script_path):
            raise AdbError(f'脚本不存在: {script_path}')
        cmd = [cli['frida'], '-l', script_path]
        if host:
            cmd += ['-H', host]      # 🥷 隐身模式: 走 adb forward 隧道
        else:
            cmd += ['-D', serial]
        if spawn:
            cmd += ['-f', pkg]
        else:
            cmd += [pkg]
        if extra_args:
            cmd += extra_args
        self.cmd_str = ' '.join(cmd)
        # stdin=PIPE 保持打开 (frida 非交互模式依赖 stdin 不 EOF); stdout 捕获
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT,
                                         stdin=subprocess.PIPE,
                                         creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            raise AdbError(f'无法执行 frida CLI: {cli["frida"]}')
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        proc = self.proc   # 局部引用 — stop() 会把 self.proc 置 None, 直接用会竞争崩溃
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            with self._lock:
                self._lines.append(line)
                if len(self._lines) > 5000:
                    self._lines = self._lines[-4000:]
        try:
            code = proc.wait()
        except Exception:
            code = '?'
        with self._lock:
            self._lines.append(f'[frida 退出 code={code}]')

    def read_new(self, after=0):
        """读取 after 行之后的新行"""
        with self._lock:
            return list(self._lines[after:]), len(self._lines)

    def read_all(self):
        with self._lock:
            return '\n'.join(self._lines)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
