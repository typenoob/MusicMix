#!/usr/bin/env python3
from pathlib import Path
import plistlib
import subprocess
import unittest
import zipfile

import build

ROOT = Path(__file__).resolve().parent


def find_packaged_build():
    """Return (ipa path, app directory) for a root IPA already carrying the current module."""
    for path in sorted(ROOT.glob(f'*{build.SUFFIX}')):
        try:
            with zipfile.ZipFile(path) as archive:
                members = [name for name in archive.namelist()
                           if name.startswith('Payload/') and name.endswith(f'/Frameworks/{build.MODULE_NAME}.dylib')]
        except zipfile.BadZipFile:
            continue
        if members:
            return path, members[0].rsplit('/Frameworks/', 1)[0]
    return None, None


PACKAGED_IPA, PACKAGED_APP = find_packaged_build()

HARNESS = r'''
#include <assert.h>
#include <stdio.h>

const char *state_category = "AVAudioSessionCategoryPlayback";
Options state_options;
static Object seen_mode;
static long seen_policy;
static Options seen_activation_options;
static Boolean seen_active;
static int category_calls, activation_calls, reject_category;
static Object fake_error = (Object)0x1234;

static Boolean save_category(Object self, Selector cmd, Object category, Options options, Object *error) {
    (void)self; (void)cmd;
    category_calls++;
    if (reject_category) {
        if (error) *error = fake_error;
        return 0;
    }
    state_category = category;
    state_options = options;
    return 1;
}
static Boolean save_mode(Object self, Selector cmd, Object category, Object mode, Options options, Object *error) {
    seen_mode = mode;
    return save_category(self, cmd, category, options, error);
}
static Boolean save_policy(Object self, Selector cmd, Object category, Object mode, long policy, Options options, Object *error) {
    seen_policy = policy;
    return save_mode(self, cmd, category, mode, options, error);
}
static Boolean save_active(Object self, Selector cmd, Boolean active, Object *error) {
    (void)self; (void)cmd; (void)error;
    activation_calls++;
    seen_active = active;
    return 1;
}
static Boolean save_active_options(Object self, Selector cmd, Boolean active, Options options, Object *error) {
    seen_activation_options = options;
    return save_active(self, cmd, active, error);
}
unsigned long dispatch_category(Object self, Object category, Options options, Object *error) {
    return hookCategoryOptions(self, categoryOptionsSelector, category, options, error);
}
Class objc_getClass(const char *name) { return (Class)name; }
Selector sel_registerName(const char *name) { return (Selector)name; }
static Implementation fake_methods[6];
Method class_getInstanceMethod(Class cls, Selector selector) {
    (void)cls;
    const char *names[] = {"setCategory:withOptions:error:", "setCategory:error:",
        "setCategory:mode:options:error:", "setCategory:mode:routeSharingPolicy:options:error:",
        "setActive:error:", "setActive:withOptions:error:"};
    for (int i = 0; i < 6; i++) if (!strcmp(selector, names[i])) return &fake_methods[i];
    return 0;
}
Implementation method_getImplementation(Method method) { return *(Implementation *)method; }
Implementation method_setImplementation(Method method, Implementation value) {
    Implementation old = *(Implementation *)method;
    *(Implementation *)method = value;
    return old;
}
int main(void) {
    fake_methods[0] = (Implementation)save_category;
    fake_methods[1] = (Implementation)save_category;
    fake_methods[2] = (Implementation)save_mode;
    fake_methods[3] = (Implementation)save_policy;
    fake_methods[4] = (Implementation)save_active;
    fake_methods[5] = (Implementation)save_active_options;
    installMixing();
    assert(fake_methods[0] == (Implementation)hookCategoryOptions);
    assert(fake_methods[1] == (Implementation)hookCategory);
    assert(fake_methods[2] == (Implementation)hookCategoryMode);
    assert(fake_methods[3] == (Implementation)hookCategoryPolicy);
    assert(fake_methods[4] == (Implementation)hookActive);
    assert(fake_methods[5] == (Implementation)hookActiveOptions);
    const char *mixable[] = {"AVAudioSessionCategoryPlayback", "AVAudioSessionCategoryPlayAndRecord",
        "AVAudioSessionCategoryMultiRoute", "AVAudioSessionCategorySoloAmbient"};
    for (int i = 0; i < 4; ++i) {
        for (Options options = 0; options < 1024; ++options) {
            Options result = mixedOptions(mixable[i], options);
            assert(result & 1);
            assert(!(result & 0x12));
            assert((result & ~0x13UL) == (options & ~0x13UL));
            assert(mixedOptions(mixable[i], result) == result);
        }
    }
    for (Options options = 0; options < 1024; ++options) {
        assert(mixedOptions("AVAudioSessionCategoryAmbient", options) == options);
        assert(mixedOptions("AVAudioSessionCategoryRecord", options) == options);
        assert(mixedOptions(0, options) == options);
    }
    Object error = 0, session = (Object)0x100, mode = (Object)"AVAudioSessionModeDefault";
    assert(hookCategory(session, 0, (Object)mixable[3], &error));
    assert(!strcmp(state_category, mixable[0]) && state_options == 1);
    assert(hookCategoryOptions(session, 0, (Object)mixable[1], 0x77, &error));
    assert(!strcmp(state_category, mixable[1]) && state_options == 0x65);
    assert(hookCategoryMode(session, 0, (Object)mixable[0], mode, 0x12, &error));
    assert(seen_mode == mode && state_options == 1);
    assert(hookCategoryPolicy(session, 0, (Object)mixable[0], mode, 2, 0x40, &error));
    assert(seen_policy == 2 && seen_mode == mode && state_options == 0x41);
    state_options = 2;
    int previous = category_calls;
    assert(hookActive(session, 0, 1, &error));
    assert(state_options == 1 && category_calls == previous + 1 && seen_active);
    previous = category_calls;
    assert(hookActive(session, 0, 1, &error));
    assert(category_calls == previous);
    state_options = 2;
    assert(hookActiveOptions(session, 0, 0, 1, &error));
    assert(category_calls == previous && !seen_active && seen_activation_options == 1);
    assert(hookActiveOptions(session, 0, 1, 0, &error));
    assert(state_options == 1 && seen_active && seen_activation_options == 0);
    reject_category = 1;
    state_options = 0;
    previous = activation_calls;
    assert(!hookActive(session, 0, 1, &error));
    assert(error == fake_error && activation_calls == previous);
    assert(!hookActiveOptions(session, 0, 1, 0, 0));
    assert(activation_calls == previous);
    puts("PASS: 4096 mixable-option cases, unchanged Ambient/Record/nil, six installed hooks, argument forwarding, activation and failure behavior");
    return 0;
}
'''

RUNTIME = r'''
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
extern const char *state_category;
extern unsigned long state_options;
extern unsigned long dispatch_category(void *, void *, unsigned long, void **);
uintptr_t objc_msgSend(void *self, const char *selector, ...) {
    va_list args;
    va_start(args, selector);
    uintptr_t result = 0;
    if (!strcmp(selector, "UTF8String")) result = (uintptr_t)self;
    else if (!strcmp(selector, "stringWithUTF8String:")) result = (uintptr_t)va_arg(args, const char *);
    else if (!strcmp(selector, "category")) result = (uintptr_t)state_category;
    else if (!strcmp(selector, "categoryOptions")) result = state_options;
    else if (!strcmp(selector, "setCategory:withOptions:error:")) {
        void *category = va_arg(args, void *);
        unsigned long options = va_arg(args, unsigned long);
        void **error = va_arg(args, void **);
        result = dispatch_category(self, category, options, error);
    } else {
        fprintf(stderr, "Unexpected mocked selector: %s\n", selector);
        abort();
    }
    va_end(args);
    return result;
}
'''


class MixingTests(unittest.TestCase):
    def test_native_hook_logic(self):
        directory = ROOT / 'build'
        directory.mkdir(exist_ok=True)
        source = (ROOT / f'{build.MODULE_NAME}.c').read_text().replace('__attribute__((constructor))', '')
        harness = directory / 'test_hooks.c'
        runtime = directory / 'mock_runtime.c'
        executable = directory / 'test_hooks'
        harness.write_text(source + HARNESS)
        runtime.write_text(RUNTIME)
        subprocess.run(['clang-19', '-std=c11', '-O0', '-Wall', '-Wextra', '-Werror',
                        str(harness), str(runtime), '-o', str(executable)], check=True)
        subprocess.run([str(executable)], check=True)

    def test_dylib_build(self):
        directory = ROOT / 'build'
        directory.mkdir(exist_ok=True)
        library = directory / f'test-{build.MODULE_NAME}.dylib'
        build.build_library(library)
        self.assertEqual(build.verify_signature(library.read_bytes()), 1)

    @unittest.skipUnless(PACKAGED_IPA and (ROOT / f'{build.MODULE_NAME}.dylib').exists(),
                         'Optional IPA integration test: first build using your own IPA')
    def test_packaged_signatures_and_injection(self):
        with zipfile.ZipFile(PACKAGED_IPA) as archive:
            info = plistlib.loads(archive.read(f'{PACKAGED_APP}/Info.plist'))
            app = archive.read(f"{PACKAGED_APP}/{info['CFBundleExecutable']}")
            library = archive.read(f'{PACKAGED_APP}/Frameworks/{build.MODULE_NAME}.dylib')
            self.assertEqual(build.verify_signature(app), 2)
            self.assertEqual(build.verify_signature(library), 1)
            self.assertEqual(library, (ROOT / f'{build.MODULE_NAME}.dylib').read_bytes())
            self.assertEqual(app.count(build.LOAD_PATH), 1)
            with self.assertRaisesRegex(ValueError, 'already injected'):
                build.inject(app)
            corrupt = bytearray(app)
            corrupt[16384] ^= 1
            with self.assertRaisesRegex(ValueError, 'signature mismatch'):
                build.verify_signature(corrupt)


if __name__ == '__main__':
    unittest.main(verbosity=2)
