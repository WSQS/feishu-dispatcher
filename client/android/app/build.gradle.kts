plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "dev.sopho.fdx.client"
    compileSdk = 36
    // AGP 8.7.3 默认要 build-tools 34（本机无）；显式指定本机已有的 36.0.0，避免下 34。
    buildToolsVersion = "36.0.0"

    defaultConfig {
        applicationId = "dev.sopho.fdx.client"
        minSdk = 26          // 覆盖大多数在用安卓；DataStore/Ktor 都支持更低，26 稳妥
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    testOptions {
        unitTests {
            // JVM 单测里 android.util.Log 等框架类是空 stub，默认会抛 RuntimeException("Not mocked")；
            // 让它们返回默认值（Log.i/w 返回 0），不阻断业务逻辑测试。
            isReturnDefaultValues = true
        }
    }
}

dependencies {
    // Compose BOM 统一管理 Compose 库版本
    val composeBom = platform("androidx.compose:compose-bom:2024.10.01")
    implementation(composeBom)

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    // lifecycle-viewmodel-compose：ViewModel 的 Compose 集成
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")

    // Ktor Client：core + CIO engine（普通 HTTP）+ OkHttp engine（libzt socket）+
    // content-negotiation + json + kotlinx.serialization
    implementation("io.ktor:ktor-client-core:3.0.3")
    implementation("io.ktor:ktor-client-cio:3.0.3")
    implementation("io.ktor:ktor-client-okhttp:3.0.3")
    implementation("io.ktor:ktor-client-content-negotiation:3.0.3")
    implementation("io.ktor:ktor-serialization-kotlinx-json:3.0.3")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")

    // DataStore Preferences：存 viewer 地址 + token，协程友好
    implementation("androidx.datastore:datastore-preferences:1.1.1")

    // libzt（ZeroTier SDK）：App 自带 zerotier 组网，连 daemon viewer（来源 libzt-prebuild）
    implementation("com.github.WSQS:libzt-prebuild:main-a707ea6-20260808")

    debugImplementation("androidx.compose.ui:ui-tooling")

    // ---- JVM 单测依赖（testImplementation，跑在开发机 JVM，非 androidTest/设备）----
    // kotlin("test") 绑当前 Kotlin 版本的 kotlin.test + JUnit 运行器
    testImplementation(kotlin("test"))
    // MockEngine 与 Ktor 同版（3.0.3），避免引入冲突的 ktor-core
    testImplementation("io.ktor:ktor-client-mock:3.0.3")
    // 协程测试与 Ktor 3.0.3 依赖的 kotlinx-coroutines 1.9.0 对齐
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
}
