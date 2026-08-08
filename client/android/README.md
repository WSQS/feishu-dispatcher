# feishu-dispatcher viewer（Android 客户端）

移动端查看器 App：手机经私有网络（局域网 / Tailscale / ZeroTier）连 daemon 的 viewer
服务，查看 agent 在 workspace 干活时的文件树 / 文件内容 / 代码 diff。

> 这是 [feishu-dispatcher](../../..) 的安卓客户端部分，位于仓库 `client/android/`。
> 服务端 viewer 见 `feishu_dispatcher/viewer.py`。整体设计见 wayfinder map #103。

## 状态

骨架阶段（#121）：空 Compose Activity 能编译能装。后续里程碑：

- 网络层（Ktor + `/api/health`）— #122
- 配置页 + DataStore — #123
- 整合 + zerotier 真机验证 — #125

## 技术栈

- Kotlin + Jetpack Compose
- Gradle 8.9 + AGP 8.7.3
- minSdk 26 / targetSdk 36

## 构建

需要 Android SDK + JDK 17（注意：JDK 26 不被 Gradle 8.x 支持，要用 17）。

```bash
# 1. 指向你的 Android SDK（首次 clone 后，或让 Android Studio 自动生成）
echo "sdk.dir=$HOME/Android/Sdk" > local.properties

# 2. 编译 debug APK（JDK 17）
JAVA_HOME=/usr/lib/jvm/java-17-openjdk ./gradlew assembleDebug

# 产物：app/build/outputs/apk/debug/app-debug.apk
```

或直接用 Android Studio 打开本目录（`client/android/`），Gradle Sync 后 Build。

## 装机 / 运行

```bash
# 真机或模拟器连上 adb 后
~/Android/Sdk/platform-tools/adb install app/build/outputs/apk/debug/app-debug.apk
```

启动 App 显示 "feishu-dispatcher viewer" 文本（骨架阶段；UI 后续 child 补）。
