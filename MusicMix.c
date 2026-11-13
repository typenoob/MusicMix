typedef void *Object;
typedef void *Selector;
typedef void *Class;
typedef void *Method;
typedef void (*Implementation)(void);
typedef unsigned long Options;
typedef _Bool Boolean;

extern Class objc_getClass(const char *name);
extern Selector sel_registerName(const char *name);
extern Method class_getInstanceMethod(Class cls, Selector selector);
extern Implementation method_getImplementation(Method method);
extern Implementation method_setImplementation(Method method, Implementation implementation);
extern void objc_msgSend(void);
extern int strcmp(const char *left, const char *right);
extern void syslog(int priority, const char *format, ...);

typedef Boolean (*CategoryOptionsFn)(Object, Selector, Object, Options, Object *);
typedef Boolean (*CategoryModeFn)(Object, Selector, Object, Object, Options, Object *);
typedef Boolean (*CategoryPolicyFn)(Object, Selector, Object, Object, long, Options, Object *);
typedef Boolean (*ActiveFn)(Object, Selector, Boolean, Object *);
typedef Boolean (*ActiveOptionsFn)(Object, Selector, Boolean, Options, Object *);

static CategoryOptionsFn originalCategoryOptions;
static CategoryModeFn originalCategoryMode;
static CategoryPolicyFn originalCategoryPolicy;
static ActiveFn originalActive;
static ActiveOptionsFn originalActiveOptions;
static Selector categoryOptionsSelector;
static Selector categorySelector;
static Selector optionsSelector;
static Selector utf8Selector;
static Selector stringSelector;

static const char *categoryName(Object category) {
    return category ? ((const char *(*)(Object, Selector))objc_msgSend)(category, utf8Selector) : (void *)0;
}

static Options mixedOptions(const char *category, Options options) {
    if (!category) return options;
    if (!strcmp(category, "AVAudioSessionCategoryPlayback") ||
        !strcmp(category, "AVAudioSessionCategoryPlayAndRecord") ||
        !strcmp(category, "AVAudioSessionCategoryMultiRoute") ||
        !strcmp(category, "AVAudioSessionCategorySoloAmbient")) {
        // InterruptSpokenAudioAndMixWithOthers is 0x11; keep its mixing bit, not its interruption bit.
        return (options & ~(Options)0x12) | (Options)1;
    }
    return options;
}

static Object mixedCategory(Object category) {
    const char *name = categoryName(category);
    if (name && !strcmp(name, "AVAudioSessionCategorySoloAmbient")) {
        return ((Object (*)(Class, Selector, const char *))objc_msgSend)(
            objc_getClass("NSString"), stringSelector, "AVAudioSessionCategoryPlayback");
    }
    return category;
}

static Boolean hookCategoryOptions(Object self, Selector cmd, Object category, Options options, Object *error) {
    options = mixedOptions(categoryName(category), options);
    return originalCategoryOptions(self, cmd, mixedCategory(category), options, error);
}

static Boolean hookCategory(Object self, Selector cmd, Object category, Object *error) {
    (void)cmd;
    // The optionless setter must not activate an exclusive category even briefly.
    return ((CategoryOptionsFn)objc_msgSend)(self, categoryOptionsSelector, mixedCategory(category),
                                           mixedOptions(categoryName(category), 0), error);
}

static Boolean hookCategoryMode(Object self, Selector cmd, Object category, Object mode, Options options, Object *error) {
    options = mixedOptions(categoryName(category), options);
    return originalCategoryMode(self, cmd, mixedCategory(category), mode, options, error);
}

static Boolean hookCategoryPolicy(Object self, Selector cmd, Object category, Object mode,
                                  long policy, Options options, Object *error) {
    options = mixedOptions(categoryName(category), options);
    return originalCategoryPolicy(self, cmd, mixedCategory(category), mode, policy, options, error);
}

static Boolean prepareActivation(Object self, Object *error) {
    Object category = ((Object (*)(Object, Selector))objc_msgSend)(self, categorySelector);
    Options options = ((Options (*)(Object, Selector))objc_msgSend)(self, optionsSelector);
    const char *name = categoryName(category);
    Options updated = mixedOptions(name, options);
    if (updated == options && (!name || strcmp(name, "AVAudioSessionCategorySoloAmbient"))) return 1;
    Boolean result = ((CategoryOptionsFn)objc_msgSend)(self, categoryOptionsSelector,
                                                      mixedCategory(category), updated, error);
    if (!result) syslog(4, "[MusicMix] Mixing rejected: category=%s options=0x%lx; activation blocked",
                        name ? name : "(nil)", updated);
    return result;
}

static Boolean hookActive(Object self, Selector cmd, Boolean active, Object *error) {
    if (active && !prepareActivation(self, error)) return 0;
    return originalActive(self, cmd, active, error);
}

static Boolean hookActiveOptions(Object self, Selector cmd, Boolean active, Options options, Object *error) {
    if (active && !prepareActivation(self, error)) return 0;
    return originalActiveOptions(self, cmd, active, options, error);
}

__attribute__((constructor)) static void installMixing(void) {
    Class cls = objc_getClass("AVAudioSession");
    if (!cls) {
        syslog(3, "[MusicMix] AVAudioSession is unavailable; hooks not installed");
        return;
    }
    categoryOptionsSelector = sel_registerName("setCategory:withOptions:error:");
    categorySelector = sel_registerName("category");
    optionsSelector = sel_registerName("categoryOptions");
    utf8Selector = sel_registerName("UTF8String");
    stringSelector = sel_registerName("stringWithUTF8String:");
    Selector simple = sel_registerName("setCategory:error:");
    Selector mode = sel_registerName("setCategory:mode:options:error:");
    Selector policy = sel_registerName("setCategory:mode:routeSharingPolicy:options:error:");
    Selector active = sel_registerName("setActive:error:");
    Selector activeOptions = sel_registerName("setActive:withOptions:error:");
    Method methods[] = {
        class_getInstanceMethod(cls, categoryOptionsSelector), class_getInstanceMethod(cls, simple),
        class_getInstanceMethod(cls, mode), class_getInstanceMethod(cls, policy),
        class_getInstanceMethod(cls, active), class_getInstanceMethod(cls, activeOptions)
    };
    for (unsigned int i = 0; i < sizeof(methods) / sizeof(methods[0]); ++i) {
        if (!methods[i]) {
            syslog(3, "[MusicMix] Required audio-session method %u is unavailable", i);
            return;
        }
    }
    originalCategoryOptions = (CategoryOptionsFn)method_getImplementation(methods[0]);
    originalCategoryMode = (CategoryModeFn)method_getImplementation(methods[2]);
    originalCategoryPolicy = (CategoryPolicyFn)method_getImplementation(methods[3]);
    originalActive = (ActiveFn)method_getImplementation(methods[4]);
    originalActiveOptions = (ActiveOptionsFn)method_getImplementation(methods[5]);
    method_setImplementation(methods[0], (Implementation)hookCategoryOptions);
    method_setImplementation(methods[1], (Implementation)hookCategory);
    method_setImplementation(methods[2], (Implementation)hookCategoryMode);
    method_setImplementation(methods[3], (Implementation)hookCategoryPolicy);
    method_setImplementation(methods[4], (Implementation)hookActive);
    method_setImplementation(methods[5], (Implementation)hookActiveOptions);
    syslog(5, "[MusicMix] Installed six audio-session hooks (1.0)");
}
