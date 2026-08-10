@file:OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)

package dev.sopho.fdx.client.ui

import dev.sopho.fdx.client.network.ViewerClient
import io.ktor.client.HttpClient
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.json.Json

/** VM 单测公共脚手架：Main 换成测试调度器（viewModelScope 依赖 Main）。 */
fun runVmTest(block: suspend TestScope.() -> Unit) = runTest {
    Dispatchers.setMain(StandardTestDispatcher(testScheduler))
    try {
        block()
    } finally {
        Dispatchers.resetMain()
    }
}

/** 用 MockEngine 构造 ViewerClient（与生产共用 ContentNegotiation 配置）。 */
fun mockClient(engine: MockEngine): ViewerClient = ViewerClient(
    baseUrl = "http://test",
    token = "t",
    http = HttpClient(engine) {
        install(ContentNegotiation) {
            json(Json { ignoreUnknownKeys = true })
        }
    },
)

/**
 * 排空主队列并等真实 IO 回投：viewModelScope 加载会跳真实 Dispatchers.IO，
 * 虚拟时间的 advanceUntilIdle 等不到它，需用真实 sleep 给 IO 线程完成回投。
 */
fun TestScope.runUntilIdle() {
    repeat(200) {
        advanceUntilIdle()
        Thread.sleep(1)
    }
}
