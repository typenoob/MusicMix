#!/usr/bin/env python3
import argparse
import hashlib
import math
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent
LOAD_PATH = b"@executable_path/Frameworks/DouyuMusicMix.dylib\0"


def commands(data):
    if struct.unpack_from('<II', data) != (0xFEEDFACF, 0x0100000C):
        raise ValueError('Expected a thin arm64 Mach-O')
    count, size = struct.unpack_from('<II', data, 16)
    position = 32
    for _ in range(count):
        command, length = struct.unpack_from('<II', data, position)
        if length < 8 or length % 8 or position + length > 32 + size:
            raise ValueError('Malformed Mach-O load command')
        yield position, command, length
        position += length
    if position != 32 + size:
        raise ValueError('Load command size mismatch')


def signature_location(data):
    matches = [p for p, c, _ in commands(data) if c == 0x1D]
    if len(matches) != 1:
        raise ValueError('Expected one LC_CODE_SIGNATURE')
    return struct.unpack_from('<II', data, matches[0] + 8)


def signature_blobs(data):
    offset, capacity = signature_location(data)
    signature = data[offset:offset + capacity]
    magic, length, count = struct.unpack_from('>III', signature)
    if magic != 0xFADE0CC0 or length > len(signature):
        raise ValueError('Invalid signature superblob')
    blobs = []
    for index in range(count):
        kind, start = struct.unpack_from('>II', signature, 12 + index * 8)
        blob_length = struct.unpack_from('>I', signature, start + 4)[0]
        if start + blob_length > length:
            raise ValueError('Signature blob exceeds bounds')
        blobs.append((kind, bytearray(signature[start:start + blob_length])))
    return blobs


def code_directory_pages(blob):
    version, flags, hash_offset, identifier, special, count, limit = struct.unpack_from('>7I', blob, 8)
    hash_size, hash_type, platform, page_exp = struct.unpack_from('>4B', blob, 36)
    if version >= 0x20100 and struct.unpack_from('>I', blob, 44)[0]:
        raise ValueError('Scatter signatures are not supported')
    if version >= 0x20300 and limit == 0xFFFFFFFF:
        limit = struct.unpack_from('>Q', blob, 56)[0]
    algorithm = {1: 'sha1', 2: 'sha256', 3: 'sha256', 4: 'sha384'}[hash_type]
    page_size = (1 << page_exp) if page_exp else limit
    if count != math.ceil(limit / page_size):
        raise ValueError('CodeDirectory page count mismatch')
    offsets = [hash_offset]
    if version >= 0x20500:
        pre_encrypt = struct.unpack_from('>I', blob, 92)[0]
        if pre_encrypt:
            offsets.append(pre_encrypt)
    for table in offsets:
        if table + count * hash_size > len(blob):
            raise ValueError('CodeDirectory hash table exceeds bounds')
    return algorithm, hash_size, page_size, count, limit, offsets


def resign_adhoc(data):
    offset, capacity = signature_location(data)
    blobs = [(kind, blob) for kind, blob in signature_blobs(data) if kind != 0x10000]
    for kind, blob in blobs:
        if struct.unpack_from('>I', blob)[0] != 0xFADE0C02:
            continue
        flags = struct.unpack_from('>I', blob, 12)[0]
        struct.pack_into('>I', blob, 12, flags | 2)
        algorithm, hash_size, page_size, count, limit, tables = code_directory_pages(blob)
        if limit > offset:
            raise ValueError('Signed range overlaps signature')
        for page in range(count):
            digest = hashlib.new(algorithm, data[page * page_size:min((page + 1) * page_size, limit)]).digest()[:hash_size]
            for table in tables:
                start = table + page * hash_size
                blob[start:start + hash_size] = digest
    start = 12 + len(blobs) * 8
    entries = bytearray()
    body = bytearray()
    for kind, blob in blobs:
        entries.extend(struct.pack('>II', kind, start + len(body)))
        body.extend(blob)
    signature = struct.pack('>III', 0xFADE0CC0, start + len(body), len(blobs)) + entries + body
    if len(signature) > capacity:
        raise ValueError('Rebuilt signature does not fit')
    data[offset:offset + capacity] = signature.ljust(capacity, b'\0')
    return data


def verify_signature(data):
    verified = 0
    blobs = dict(signature_blobs(data))
    for kind, blob in blobs.items():
        if struct.unpack_from('>I', blob)[0] != 0xFADE0C02:
            continue
        algorithm, hash_size, page_size, count, limit, tables = code_directory_pages(blob)
        for table in tables:
            for page in range(count):
                digest = hashlib.new(algorithm, data[page * page_size:min((page + 1) * page_size, limit)]).digest()[:hash_size]
                start = table + page * hash_size
                if blob[start:start + hash_size] != digest:
                    raise ValueError(f'Code signature mismatch in page {page}')
        hash_offset, _, special = struct.unpack_from('>III', blob, 16)
        for slot in (2, 5, 7):
            if slot in blobs and slot <= special:
                digest = hashlib.new(algorithm, blobs[slot]).digest()[:hash_size]
                start = hash_offset - slot * hash_size
                if blob[start:start + hash_size] != digest:
                    raise ValueError(f'Special signature slot {slot} mismatch')
        if not struct.unpack_from('>I', blob, 12)[0] & 2:
            raise ValueError('Expected ad-hoc CodeDirectory')
        verified += 1
    if not verified:
        raise ValueError('No CodeDirectory found')
    return verified


def inject(data):
    data = bytearray(data)
    count, size = struct.unpack_from('<II', data, 16)
    first_section = len(data)
    for position, command, length in commands(data):
        if command in (0x21, 0x2C) and struct.unpack_from('<I', data, position + 16)[0]:
            raise ValueError('Executable is encrypted')
        if command == 0x19:
            sections = struct.unpack_from('<I', data, position + 64)[0]
            for index in range(sections):
                section = position + 72 + index * 80
                offset = struct.unpack_from('<I', data, section + 48)[0]
                if offset:
                    first_section = min(first_section, offset)
        if command == 0xC:
            name_offset = struct.unpack_from('<I', data, position + 8)[0]
            if data[position + name_offset:position + length].split(b'\0')[0] == LOAD_PATH.rstrip(b'\0'):
                raise ValueError('Module is already injected')
    length = (24 + len(LOAD_PATH) + 7) & ~7
    start = 32 + size
    if start + length > first_section or any(data[start:start + length]):
        raise ValueError('Not enough unused load-command padding')
    load_command = struct.pack('<6I', 0xC, length, 24, 0, 0x10000, 0x10000) + LOAD_PATH
    data[start:start + length] = load_command.ljust(length, b'\0')
    struct.pack_into('<II', data, 16, count + 1, size + length)
    return resign_adhoc(data)


def write_stub(path, install_name, symbols):
    exports = ', '.join(symbols)
    path.write_text(f'''--- !tapi-tbd-v3
archs: [ arm64 ]
platform: ios
install-name: '{install_name}'
exports:
  - archs: [ arm64 ]
    symbols: [ {exports} ]
...
''')


def build_library(output):
    compiler = shutil.which('clang-19')
    linker = shutil.which('ld64.lld-19')
    if not compiler or not linker:
        raise RuntimeError('clang-19 and ld64.lld-19 are required')
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    write_stub(build / 'libobjc.tbd', '/usr/lib/libobjc.A.dylib', [
        '_objc_getClass', '_sel_registerName', '_class_getInstanceMethod',
        '_method_getImplementation', '_method_setImplementation', '_objc_msgSend'])
    write_stub(build / 'libSystem.tbd', '/usr/lib/libSystem.B.dylib', ['_strcmp', '_syslog', 'dyld_stub_binder'])
    write_stub(build / 'AVFoundation.tbd', '/System/Library/Frameworks/AVFoundation.framework/AVFoundation', [])
    subprocess.run([compiler, '-target', 'arm64-apple-ios15.0', '-std=c11', '-Oz', '-fPIC',
                    '-ffreestanding', '-fno-stack-protector', '-fvisibility=hidden', '-Wall', '-Wextra', '-Werror',
                    '-c', str(ROOT / 'DouyuMusicMix.c'), '-o', str(build / 'DouyuMusicMix.o')], check=True)
    subprocess.run([linker, '-dylib', '-arch', 'arm64', '-platform_version', 'ios', '15.0', '15.0',
                    '-install_name', LOAD_PATH.rstrip(b'\0').decode(), '-current_version', '1.0',
                    '-compatibility_version', '1.0', '-adhoc_codesign', '-no_fixup_chains',
                    '-no_exported_symbols', '-o', str(output), str(build / 'DouyuMusicMix.o'),
                    str(build / 'libobjc.tbd'), str(build / 'libSystem.tbd'),
                    '-needed_library', str(build / 'AVFoundation.tbd')], check=True)
    verify_signature(output.read_bytes())


def main():
    parser = argparse.ArgumentParser(description='Build DouyuMusicMix or inject your own unencrypted Douyu HD 1.950 IPA.')
    parser.add_argument('ipa', nargs='?', type=Path, help='Path to your own Douyu HD 1.950 (264) IPA')
    parser.add_argument('--dylib-only', action='store_true', help='Build the standalone dylib without an IPA')
    args = parser.parse_args()
    if (args.ipa is not None) == args.dylib_only:
        parser.error('Provide an IPA path or --dylib-only, but not both')
    output = ROOT / 'DouyuHD-1.950-MusicMix.ipa'
    library = ROOT / 'DouyuMusicMix.dylib'
    if args.dylib_only:
        build_library(library)
        print(f'{library.name}: {library.stat().st_size} bytes; SHA256 {hashlib.sha256(library.read_bytes()).hexdigest()}')
        return
    source = args.ipa.resolve()
    if source == output.resolve():
        raise ValueError('Input must not be the output IPA')
    build_library(library)
    with zipfile.ZipFile(source) as original:
        info_files = [name for name in original.namelist()
                      if name.startswith('Payload/') and name.count('/') == 2 and name.endswith('/Info.plist')]
        if len(info_files) != 1:
            raise ValueError('Expected one top-level application')
        info_path = info_files[0]
        info = plistlib.loads(original.read(info_path))
        if (info.get('CFBundleIdentifier'), info.get('CFBundleShortVersionString'), info.get('CFBundleVersion')) != ('tv.douyu.liveHD', '1.950', '264'):
            raise ValueError('This build targets Douyu HD 1.950 (264) only')
        app = info_path.rsplit('/', 1)[0]
        executable = app + '/' + info['CFBundleExecutable']
        before = original.read(executable)
        patched = inject(before)
        verify_signature(patched)
        old_entitlements = {k: bytes(v) for k, v in signature_blobs(before) if k in (5, 7)}
        new_entitlements = {k: bytes(v) for k, v in signature_blobs(patched) if k in (5, 7)}
        if old_entitlements != new_entitlements:
            raise ValueError('Entitlements unexpectedly changed')
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as result:
            for entry in original.infolist():
                data = patched if entry.filename == executable else original.read(entry)
                result.writestr(entry, data)
            entry = zipfile.ZipInfo(app + '/Frameworks/DouyuMusicMix.dylib')
            entry.create_system = 3
            entry.external_attr = 0o100755 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            result.writestr(entry, library.read_bytes())
    with zipfile.ZipFile(output) as result:
        if result.testzip() is not None:
            raise ValueError('Output ZIP CRC validation failed')
        verify_signature(result.read(executable))
        verify_signature(result.read(app + '/Frameworks/DouyuMusicMix.dylib'))
    for path in (library, output):
        print(f'{path.name}: {path.stat().st_size} bytes; SHA256 {hashlib.sha256(path.read_bytes()).hexdigest()}')
    print('Verified Mach-O code-page hashes, embedded entitlement hashes, unchanged entitlements and ZIP CRCs.')
    print('Installation and actual audio behavior still require TrollStore/iPad testing.')


if __name__ == '__main__':
    main()
