# feishu-dispatcher viewer（Android 客户端）

移动端查看器 App：手机经私有网络（局域网 / Tailscale / ZeroTier）连 daemon 的 viewer
服务，查看 agent 在 workspace 干活时的文件树和文件内容。

> 这是 [feishu-dispatcher](../../..) 的安卓客户端部分，位于仓库 `client/android/`。
> 服务端 viewer 见 `feishu_dispatcher/viewer.py`。整体设计见 wayfinder map #103。

## 功能

配置 viewer 地址与 token 后，经普通 HTTP / ZeroTier 连接 daemon 的 viewer 服务，
浏览项目列表、文件树与只读文件内容。文本文件以等宽字体展示；二进制文件显示不可
预览提示，超过 1 MB 的文件由服务端拒绝预览。

## 技术栈

- Kotlin + Jetpack Compose
- Gradle 8.13 + AGP 8.7.3
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

启动 App 后配置 viewer 地址与 token；连接成功后可依次进入项目列表、文件树和文件内容页。
