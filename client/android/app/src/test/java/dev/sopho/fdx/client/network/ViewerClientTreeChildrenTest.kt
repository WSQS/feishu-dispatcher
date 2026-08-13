package dev.sopho.fdx.client.network

import io.ktor.client.HttpClient
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class ViewerClientTreeChildrenTest {
    private fun client(engine: MockEngine): ViewerClient =
        ViewerClient(
            baseUrl = "http://test",
            token = "secret-token",
            http = HttpClient(engine) {
                install(ContentNegotiation) { json(Json { ignoreUnknownKeys = true }) }
            },
        )

    @Test
    fun `treeChildren sends path query and bearer token`() = runTest {
        val requestedPath = "src/a b+#?.kt"
        val responseJson =
            """{"path":"$requestedPath","entries":[{"name":"x.kt","path":"src/x.kt","type":"file"},{"name":"lib","path":"src/lib","type":"directory"}]}"""
        val engine = MockEngine { request ->
            assertEquals(HttpMethod.Get, request.method)
            assertEquals("/api/projects/proj%20name/tree/children", request.url.encodedPath)
            assertEquals(requestedPath, request.url.parameters["path"])
            assertEquals("Bearer secret-token", request.headers[HttpHeaders.Authorization])
            respond(
                responseJson,
                HttpStatusCode.OK,
                headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val c = client(engine)
        try {
            val r = c.treeChildren("proj name", requestedPath)
            assertEquals(requestedPath, r.path)
            assertEquals(2, r.entries.size)
            assertEquals("file", r.entries[0].type)
            assertEquals("directory", r.entries[1].type)
        } finally {
            c.close()
        }
    }

    @Test
    fun `treeChildren root sends empty path key`() = runTest {
        val engine = MockEngine { request ->
            assertEquals("/api/projects/proj/tree/children", request.url.encodedPath)
            assertEquals("", request.url.parameters["path"]) // 空串根目录，键仍存在
            respond(
                """{"path":"","entries":[]}""",
                HttpStatusCode.OK,
                headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val c = client(engine)
        try {
            val r = c.treeChildren("proj", "")
            assertEquals("", r.path)
            assertEquals(0, r.entries.size)
        } finally {
            c.close()
        }
    }

    @Test
    fun `treeChildren classifies 404 as not_found`() = runTest {
        val engine = MockEngine { respond("""{"error":"not found: x"}""", HttpStatusCode.NotFound) }
        val c = client(engine)
        try {
            val e = assertFailsWith<ViewerException> { c.treeChildren("proj", "x") }
            assertEquals(ViewerException.Kind.NOT_FOUND, e.kind)
        } finally {
            c.close()
        }
    }
}
