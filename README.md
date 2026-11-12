# DouyuMusicMix

让 iPad 上的斗鱼 HD 直播与其他 App 的后台音乐同时有声，不因斗鱼的音频会话设置而暂停或压低音乐。

这是一个独立的 arm64 iOS dylib，通过 Objective-C Runtime 修改进程内的 `AVAudioSession` 行为，不依赖 Cydia Substrate、Substitute 或其他越狱 Hook 框架。TrollStore 用于安装修改后的 App，本身不负责注入。

**已收到用户实测成功反馈：iPadOS 15 + TrollStore + 斗鱼 HD 1.950（264）。** 这不代表其他系统版本、斗鱼版本或所有播放场景都已验证。

仓库和 Release 不提供斗鱼原版或修改版 IPA；请自行准备有权使用的未加密安装包。本项目与斗鱼、Apple 及 TrollStore 无隶属关系。

## 适用范围

| 项目 | 当前范围 |
| --- | --- |
| App | 斗鱼 HD 1.950，构建号 264 |
| Bundle ID | `tv.douyu.liveHD` |
| 主程序架构 | 单架构 arm64 Mach-O |
| 系统 | dylib 最低目标 iOS / iPadOS 15.0；用户实测 iPadOS 15 |
| 安装环境 | 已可正常使用 TrollStore 的设备 |
| 构建环境 | Debian 13 x86_64，Python 3、Clang 19、LLD 19 |

不适用于手机端斗鱼或其他版本的自动注入；构建脚本会校验 App 标识和版本。它不会解密 IPA，也不会安装 TrollStore。

## 使用方法

### 1. 准备构建工具

在 Debian 13 上安装：

```sh
sudo apt install python3 clang-19 lld-19
```

构建不下载 Apple SDK：脚本为实际引用的系统库符号生成最小 TAPI 链接描述，运行时仍调用 iPad 上的系统框架。

### 2. 使用自己的 IPA 构建并注入

在仓库目录运行：

```sh
python3 build.py "/path/to/your/DouyuHD-1.950.ipa"
```

输出位于仓库根目录：

- `DouyuMusicMix.dylib`：混播模块。
- `DouyuHD-1.950-MusicMix.ipa`：已注入模块的安装包。

脚本添加 `LC_LOAD_DYLIB` 加载命令，将模块放入 App 的 `Frameworks` 目录，并重建主程序的 ad-hoc 签名；保留原有权限声明和 Bundle ID，不修改主程序指令或其他 App 资源。输入文件保持不变，同名输出文件在再次构建时会被覆盖。

签名哈希校验不等于 App Store 签名或普通侧载支持；此工作流面向 TrollStore。

### 3. 在 TrollStore 中安装

将生成的 IPA 传到 iPad，使用 TrollStore 安装。

**安装包保留原包名，不是双开版，会与已安装的斗鱼 HD 冲突。** 安装前按自己的需求备份数据；是否需要替换现有安装由 TrollStore 的提示及当前安装状态决定，不能保证保留登录状态和数据。

先在音乐 App 中开始播放，再打开斗鱼 HD 进入直播间，确认两边都有声音。建议继续测试：

- 切换直播间、暂停后继续直播。
- 切换前后台、锁屏后返回。
- 先开直播，再启动音乐。
- 使用蓝牙耳机、AirPlay 等音频输出。

这些场景是验证建议，不表示全部经过实测。

## 只构建或使用 dylib

不持有 IPA 也可以构建模块：

```sh
python3 build.py --dylib-only
```

预编译模块通过 GitHub Releases 单独提供。它不是可以独立安装的 App，需要注入目标 App。

使用其他注入工具时，放置位置与加载路径应保持一致：

```text
Payload/liveHD.app/Frameworks/DouyuMusicMix.dylib
@executable_path/Frameworks/DouyuMusicMix.dylib
```

仅把 dylib 复制进 IPA 不会自动加载；还需要添加 Mach-O 加载命令并完成适合安装环境的签名处理。本仓库的 `build.py` 会完成整个流程，但会从源码重新编译模块。

## 原理

模块在加载时拦截六个 `AVAudioSession` 方法：

- `setCategory:error:`
- `setCategory:withOptions:error:`
- `setCategory:mode:options:error:`
- `setCategory:mode:routeSharingPolicy:options:error:`
- `setActive:error:`
- `setActive:withOptions:error:`

对 Playback、PlayAndRecord 和 MultiRoute 分类启用 `MixWithOthers`，移除 `DuckOthers` 以及 `InterruptSpokenAudioAndMixWithOthers` 中的打断位，并保留其他选项。SoloAmbient 转为可混播的 Playback；Ambient 和 Record 保持原样。

激活音频会话前再次检查混播选项，不主动恢复或控制其他 App，也不修改系统全局音频设置。

## 测试

```sh
python3 test_mix.py
```

不需要 IPA 即可运行：

- dylib 交叉编译和代码签名哈希校验。
- 4096 组可混播分类／选项组合测试。
- Ambient、Record 和空分类的保留行为。
- 六个 Hook 的注册、参数转发、激活／停用及失败行为。

如果根目录已有生成的 IPA，还会验证安装包中的签名、模块一致性、重复注入保护以及修改代码后能否检测到哈希错误。没有 IPA 时，该集成测试明确显示为跳过。

本地音频测试使用模拟的 Objective-C Runtime，只检查代码逻辑，不能替代 iPad 上的实际音频会话测试。

## 限制与排查

- 其他 App 若请求独占音频、系统来电或 Siri 介入，音乐仍可能中断。
- 无法自动恢复在模块加载前已经暂停的音乐；请手动重新播放。
- 不提供两个 App 的独立音量滑块；系统音量通常同时影响两者。
- 连麦、录音、语音处理和不同音频输出路由未完整验证。
- SoloAmbient 转为 Playback 后，原本受静音开关影响的行为可能改变。
- 若系统拒绝混播设置，模块会阻止本次会话激活，以免打断音乐；表现可能是直播无声。
- 模块应在 App 启动时加载，不面向运行中热注入。其他插件再次替换相同方法可能影响效果。
- 系统日志中的模块前缀为 `[DouyuMusicMix]`。反馈问题时请提供 iPadOS 版本、斗鱼版本、音乐 App、输出设备与复现步骤，勿上传账号凭据或个人数据。

要撤销修改，使用原版安装包替换修改版；仍需注意安装替换时的数据保留风险。
