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
 * `ViewerClient.projects()` / `tree()` 集成测试（MockEngine，不依赖 daemon）（#179）。
 *
 * 背景：这两个方法此前没设 `expectSuccess`，daemon 返回 401/500 时**不会**抛
 * `ResponseException`，错误响应体走 `.body<>()` 因格式不符抛 JSON 异常，被
 * `ViewerException.from(e)` 误判为 NETWORK，使 AUTH/PROTOCOL 分类成为死路径
 * （与 #127 的设计意图相悖）。本测试验证加 `expectSuccess = true` 后错误响应被正确分类。
 *
 * 覆盖：
 * - projects() 200 + JSON → 返回 ListProjectsResponse（解析正确）
 * - tree(projectName) 200 + JSON → 返回 TreeResponse（解析正确），URL 含项目名
 * - 401/403 → ViewerException(AUTH)；500 → ViewerException(PROTOCOL)
 * - UnknownHostException → ViewerException(NETWORK)
 *
 * 通过 `httpClient` 测试缝注入 MockEngine（生产路径 fromConnection 等不传该参数，走默认 CIO/OkHttp）。
 */
class ViewerClientProjectsTreeTest {

    /** 构造一个带 ContentNegotiation(json) 的 MockEngine HttpClient（与 ViewerClient 生产配置一致）。 */
    private fun mockClient(handler: MockRequestHandler): HttpClient = HttpClient(MockEngine(handler)) {
        install(ContentNegotiation) {
            json(Json { ignoreUnknownKeys = true })
        }
    }

    private fun client(baseUrl: String, handler: MockRequestHandler): ViewerClient =
        ViewerClient(baseUrl = baseUrl, token = "tok-secret", httpClient = mockClient(handler))

    // ---------------- projects() ----------------

    @Test
    fun `200 projects 返回解析后的 ListProjectsResponse`() = runTest {
        val vc = client("http://192.168.1.2:7321") { request ->
            assertEquals(Url("http://192.168.1.2:7321/api/projects"), request.url)
            respond(
                """{"items":[{"name":"demo","path":"/srv/demo","default_agent":"main"}]}""",
                HttpStatusCode.OK,
                headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }

        val resp = vc.projects()

        assertEquals(1, resp.items.size)
        assertEquals("demo", resp.items[0].name)
        assertEquals("/srv/demo", resp.items[0].path)
        assertEquals("main", resp.items[0].defaultAgent)
    }

    @Test
    fun `projects 的 401 响应抛 ViewerException AUTH`() = runTest {
        val vc = client("http://192.168.1.2:7321") {
            respond("unauthorized", HttpStatusCode.Unauthorized,
                headersOf(HttpHeaders.ContentType, "text/plain"))
        }

        val ex = assertFailsWith<ViewerException> { vc.projects() }
        assertEquals(ViewerException.Kind.AUTH, ex.kind)
    }

    @Test
    fun `projects 的 500 响应抛 ViewerException PROTOCOL`() = runTest {
        val vc = client("http://192.168.1.2:7321") {
            respond("oops", HttpStatusCode.InternalServerError,
                headersOf(HttpHeaders.ContentType, "text/plain"))
        }

        val ex = assertFailsWith<ViewerException> { vc.projects() }
        assertEquals(ViewerException.Kind.PROTOCOL, ex.kind)
    }

    @Test
    fun `projects 的 UnknownHostException 抛 ViewerException NETWORK`() = runTest {
        val vc = client("http://192.168.1.2:7321") {
            throw UnknownHostException("192.168.1.2")
        }

        val ex = assertFailsWith<ViewerException> { vc.projects() }
        assertEquals(ViewerException.Kind.NETWORK, ex.kind)
        assertTrue(ex.cause is UnknownHostException)
    }

    // ---------------- tree() ----------------

    @Test
    fun `200 tree 返回解析后的 TreeResponse 且 URL 含项目名`() = runBlocking {
        val seenUrls = mutableListOf<Url>()
        val vc = client("http://10.0.0.5:7321") { request ->
            seenUrls += request.url
            respond(
                """{"entries":[{"path":"README.md","type":"blob","size":12},{"path":"src","type":"tree","size":0}]}""",
                HttpStatusCode.OK,
                headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }

        val resp = vc.tree("demo")

        // URL 拼成 <baseUrl>/api/projects/{name}/tree
        assertEquals(1, seenUrls.size)
        assertEquals("/api/projects/demo/tree", seenUrls[0].encodedPath)
        assertEquals("http://10.0.0.5:7321/api/projects/demo/tree", seenUrls[0].toString())

        // 解析正确
        assertEquals(2, resp.entries.size)
        assertEquals("README.md", resp.entries[0].path)
        assertEquals("blob", resp.entries[0].type)
        assertEquals(12L, resp.entries[0].size)
        assertEquals("src", resp.entries[1].path)
        assertEquals("tree", resp.entries[1].type)
    }

    @Test
    fun `tree 的 401 响应抛 ViewerException AUTH`() = runTest {
        val vc = client("http://192.168.1.2:7321") {
            respond("unauthorized", HttpStatusCode.Unauthorized,
                headersOf(HttpHeaders.ContentType, "text/plain"))
        }

        val ex = assertFailsWith<ViewerException> { vc.tree("demo") }
        assertEquals(ViewerException.Kind.AUTH, ex.kind)
    }

    @Test
    fun `tree 的 500 响应抛 ViewerException PROTOCOL`() = runTest {
        val vc = client("http://192.168.1.2:7321") {
            respond("oops", HttpStatusCode.InternalServerError,
                headersOf(HttpHeaders.ContentType, "text/plain"))
        }

        val ex = assertFailsWith<ViewerException> { vc.tree("demo") }
        assertEquals(ViewerException.Kind.PROTOCOL, ex.kind)
    }

    @Test
    fun `tree 的 UnknownHostException 抛 ViewerException NETWORK`() = runTest {
        val vc = client("http://192.168.1.2:7321") {
            throw UnknownHostException("192.168.1.2")
        }

        val ex = assertFailsWith<ViewerException> { vc.tree("demo") }
        assertEquals(ViewerException.Kind.NETWORK, ex.kind)
        assertTrue(ex.cause is UnknownHostException)
    }
}
