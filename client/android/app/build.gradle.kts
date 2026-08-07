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

    // Ktor Client：core + CIO engine（纯 Kotlin，无原生依赖）+
    // content-negotiation + json + kotlinx.serialization
    implementation("io.ktor:ktor-client-core:3.0.3")
    implementation("io.ktor:ktor-client-cio:3.0.3")
    implementation("io.ktor:ktor-client-content-negotiation:3.0.3")
    implementation("io.ktor:ktor-serialization-kotlinx-json:3.0.3")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")

    // DataStore Preferences：存 viewer 地址 + token，协程友好
    implementation("androidx.datastore:datastore-preferences:1.1.1")

    debugImplementation("androidx.compose.ui:ui-tooling")
}
