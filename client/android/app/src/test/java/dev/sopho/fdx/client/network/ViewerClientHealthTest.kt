package dev.sopho.fdx.client.network

import io.ktor.client.HttpClient
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandler
import io.ktor.client.engine.mock.respond
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.Url
import io.ktor.http.headersOf
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import java.net.UnknownHostException
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

/**
 * `ViewerClient.health()` 集成测试（MockEngine，不依赖 daemon）。
 *
 * 覆盖：
 * - 200 + JSON → 返回 HealthResponse(ok=true, version=...)（解析正确）
 * - 请求带 `Authorization: Bearer <token>`（token 注入正确）
 * - URL 拼成 `<baseUrl>/api/health`（含尾斜杠 baseUrl 边界）
 * - 401/403 → ViewerException(AUTH)；500 → ViewerException(PROTOCOL)
 * - UnknownHostException → ViewerException(NETWORK)
 *
 * ViewerClient 默认自建 CIO engine；这里通过新增的 `httpClient` 测试缝注入 MockEngine
 * （注入不影响生产路径：fromConnection 等不传该参数，走默认 CIO/OkHttp）。
 */
class ViewerClientHealthTest {

    /** 构造一个带 ContentNegotiation(json) 的 MockEngine HttpClient（与 ViewerClient 生产配置一致）。 */
    private fun mockClient(handler: MockRequestHandler): HttpClient = HttpClient(MockEngine(handler)) {
        install(ContentNegotiation) {
            json(Json { ignoreUnknownKeys = true })
        }
    }

    private fun client(baseUrl: String, handler: MockRequestHandler): ViewerClient =
        ViewerClient(baseUrl = baseUrl, token = "tok-secret", httpClient = mockClient(handler))

    @Test
    fun `200 health 返回解析后的 HealthResponse`() = runTest {
        val vc = client("http://192.168.1.2:7321") { request ->
            assertEquals(Url("http://192.168.1.2:7321/api/health"), request.url)
            respond(
                """{"ok": true, "version": "0.0.1"}""",
                HttpStatusCode.OK,
                headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }

        val resp = vc.health()

        assertEquals(true, resp.ok)
        assertEquals("0.0.1", resp.version)
    }

    @Test
    fun `请求带 Authorization Bearer token`() = runBlocking {
        // 用一个数组捕获请求头（runBlocking 避免在 MockEngine handler 内直接断言失败时被吞）
        val seenAuth = mutableListOf<String>()
        val vc = client("http://192.168.1.2:7321") { request ->
            seenAuth += request.headers[HttpHeaders.Authorization].orEmpty()
            respond(
                """{"ok": true, "version": "0.0.1"}""",
                HttpStatusCode.OK,
                headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }

        vc.health()

        assertEquals(listOf("Bearer tok-secret"), seenAuth)
    }

    @Test
    fun `URL 拼成 baseUrl 路径下 api_health（无尾斜杠 baseUrl）`() = runBlocking {
        val seenUrls = mutableListOf<Url>()
        val vc = client("http://10.0.0.5:7321") { request ->
            seenUrls += request.url
            respond("""{"ok":true,"version":"1"}""", HttpStatusCode.OK,
                headersOf(HttpHeaders.ContentType, "application/json"))
        }

        vc.health()

        assertEquals(1, seenUrls.size)
        assertEquals("http", seenUrls[0].protocol.name)
        assertEquals("10.0.0.5", seenUrls[0].host)
        assertEquals(7321, seenUrls[0].port)
        assertEquals("/api/health", seenUrls[0].encodedPath)
    }

    @Test
    fun `尾斜杠 baseUrl 仍拼成 api_health（边界）`() = runBlocking {
        // baseUrl 末尾带斜杠：URLBuilder 解析后 pathSegments 重设为 [api, health]，
        // takeFrom 替换整个 URL，最终不应出现双斜杠或路径丢失。
        val seenUrls = mutableListOf<Url>()
        val vc = client("http://10.0.0.5:7321/") { request ->
            seenUrls += request.url
            respond("""{"ok":true,"version":"1"}""", HttpStatusCode.OK,
                headersOf(HttpHeaders.ContentType, "application/json"))
        }

        vc.health()

        assertEquals("/api/health", seenUrls[0].encodedPath)
        assertEquals("http://10.0.0.5:7321/api/health", seenUrls[0].toString())
    }

    @Test
    fun `401 响应抛 ViewerException AUTH`() = runTest {
        val vc = client("http://192.168.1.2:7321") {
            respond("unauthorized", HttpStatusCode.Unauthorized,
                headersOf(HttpHeaders.ContentType, "text/plain"))
        }

        val ex = assertFailsWith<ViewerException> { vc.health() }
        assertEquals(ViewerException.Kind.AUTH, ex.kind)
    }

    @Test
    fun `403 响应抛 ViewerException AUTH`() = runTest {
        val vc = client("http://192.168.1.2:7321") {
            respond("forbidden", HttpStatusCode.Forbidden,
                headersOf(HttpHeaders.ContentType, "text/plain"))
        }

        val ex = assertFailsWith<ViewerException> { vc.health() }
        assertEquals(ViewerException.Kind.AUTH, ex.kind)
    }

    @Test
    fun `500 响应抛 ViewerException PROTOCOL`() = runTest {
        val vc = client("http://192.168.1.2:7321") {
            respond("oops", HttpStatusCode.InternalServerError,
                headersOf(HttpHeaders.ContentType, "text/plain"))
        }

        val ex = assertFailsWith<ViewerException> { vc.health() }
        assertEquals(ViewerException.Kind.PROTOCOL, ex.kind)
    }

    @Test
    fun `UnknownHostException 抛 ViewerException NETWORK`() = runTest {
        val vc = client("http://192.168.1.2:7321") {
            throw UnknownHostException("192.168.1.2")
        }

        val ex = assertFailsWith<ViewerException> { vc.health() }
        assertEquals(ViewerException.Kind.NETWORK, ex.kind)
        assertTrue(ex.cause is UnknownHostException)
    }
}
