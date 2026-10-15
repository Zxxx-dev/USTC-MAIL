"""只读验证 ICO 中的图像资源确实嵌入 Windows EXE，不执行该 EXE。"""

import argparse
from collections import Counter
import ctypes
from ctypes import wintypes
from pathlib import Path
import struct
import sys


RT_ICON = 3
RT_GROUP_ICON = 14
LOAD_LIBRARY_AS_DATAFILE = 0x00000002
LOAD_LIBRARY_AS_IMAGE_RESOURCE = 0x00000020


def read_ico(path):
    raw = path.read_bytes()
    if len(raw) < 6:
        raise ValueError(f"ICO 文件头不完整：{path}")
    reserved, kind, count = struct.unpack_from("<HHH", raw)
    if reserved != 0 or kind != 1 or count < 1 or len(raw) < 6 + count * 16:
        raise ValueError(f"ICO 目录格式无效：{path}")
    images = []
    for index in range(count):
        width, height, _colors, _reserved, _planes, _bits, size, offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + index * 16)
        end = offset + size
        if size == 0 or offset < 6 + count * 16 or end > len(raw):
            raise ValueError(f"ICO 图像数据越界或为空（条目 {index}）：{path}")
        images.append((width or 256, height or 256, raw[offset:end]))
    return images


def resource_api():
    if sys.platform != "win32":
        raise RuntimeError("PE 图标资源验证需要 Windows ctypes 资源 API")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel32.LoadLibraryExW.restype = wintypes.HMODULE
    kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]
    kernel32.FreeLibrary.restype = wintypes.BOOL
    kernel32.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    kernel32.FindResourceW.restype = ctypes.c_void_p
    kernel32.LoadResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    kernel32.LoadResource.restype = ctypes.c_void_p
    kernel32.LockResource.argtypes = [ctypes.c_void_p]
    kernel32.LockResource.restype = ctypes.c_void_p
    kernel32.SizeofResource.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    kernel32.SizeofResource.restype = wintypes.DWORD

    enum_callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t)
    kernel32.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p,
                                             enum_callback_type, ctypes.c_ssize_t]
    kernel32.EnumResourceNamesW.restype = wintypes.BOOL
    return kernel32, enum_callback_type


def make_resource_name(name):
    if isinstance(name, int):
        return ctypes.c_void_p(name)
    return ctypes.cast(ctypes.c_wchar_p(name), ctypes.c_void_p)


def load_resource_bytes(kernel32, module, type_id, name):
    handle = kernel32.FindResourceW(
        module, make_resource_name(name), ctypes.c_void_p(type_id))
    if not handle:
        return None
    size = kernel32.SizeofResource(module, handle)
    loaded = kernel32.LoadResource(module, handle)
    pointer = kernel32.LockResource(loaded) if loaded else None
    if not pointer or not size:
        return None
    return ctypes.string_at(pointer, size)


def resource_names(kernel32, callback_type, module, type_id):
    names = []

    def collect(_module, _type, name_pointer, _parameter):
        value = ctypes.cast(name_pointer, ctypes.c_void_p).value
        names.append(value if value <= 0xFFFF else ctypes.wstring_at(name_pointer))
        return True

    callback = callback_type(collect)
    ctypes.set_last_error(0)
    found = kernel32.EnumResourceNamesW(
        module, ctypes.c_void_p(type_id), callback, 0)
    if not found and not names:
        error = ctypes.get_last_error()
        raise RuntimeError(f"EXE 中找不到 RT_GROUP_ICON 资源（Windows 错误 {error}）")
    return names


def parse_group_icon(data):
    if len(data) < 6:
        raise ValueError("RT_GROUP_ICON 目录不完整")
    reserved, kind, count = struct.unpack_from("<HHH", data)
    if reserved != 0 or kind != 1 or count < 1 or len(data) < 6 + count * 14:
        raise ValueError("RT_GROUP_ICON 目录格式无效")
    entries = []
    for index in range(count):
        width, height, _colors, _reserved, planes, bits, size, icon_id = struct.unpack_from(
            "<BBBBHHIH", data, 6 + index * 14)
        entries.append((width or 256, height or 256, planes, bits, size, icon_id))
    return entries


def verify(executable, icon_file):
    images = read_ico(icon_file)
    expected = Counter((width, height, data) for width, height, data in images)
    kernel32, callback_type = resource_api()
    flags = LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE
    module = kernel32.LoadLibraryExW(str(executable.resolve()), None, flags)
    if not module:
        raise RuntimeError(f"无法将 EXE 作为数据文件读取（Windows 错误 {ctypes.get_last_error()}）")
    try:
        groups = resource_names(kernel32, callback_type, module, RT_GROUP_ICON)
        fewest_missing = len(images)
        for group_name in groups:
            group_data = load_resource_bytes(kernel32, module, RT_GROUP_ICON, group_name)
            if group_data is None:
                continue
            try:
                entries = parse_group_icon(group_data)
            except ValueError:
                continue
            embedded = Counter()
            for width, height, _planes, _bits, _size, icon_id in entries:
                image_data = load_resource_bytes(kernel32, module, RT_ICON, icon_id)
                if image_data is not None:
                    embedded[(width, height, image_data)] += 1
            missing = sum((expected - embedded).values())
            fewest_missing = min(fewest_missing, missing)
            if missing == 0:
                sizes = ", ".join(f"{width}x{height}" for width, height, _data in images)
                return sizes
        raise RuntimeError(
            f"EXE 的 RT_GROUP_ICON/RT_ICON 与 {icon_file} 不匹配；未找到全部 {len(images)} 个 ICO 图像（至少缺少 {fewest_missing}）")
    finally:
        kernel32.FreeLibrary(module)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, help="待检查的 Windows EXE")
    parser.add_argument("icon", type=Path, help="源 ICO 文件")
    args = parser.parse_args()
    for path in (args.executable, args.icon):
        if not path.is_file():
            parser.error(f"文件不存在：{path}")
    try:
        sizes = verify(args.executable, args.icon)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"EXE 图标验证失败：{error}\n")
    print(f"EXE 图标资源验证通过：{args.executable} 包含 {len(read_ico(args.icon))} 个 ICO 图像（{sizes}）。")


if __name__ == "__main__":
    main()
