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

class ViewerClientFileTest {
    @Test
    fun `file_sends_path_query_and_bearer_token`() = runTest {
        val requestedPath = "src/a b+#?.kt"
        val responseJson =
            """{"path":"$requestedPath","rev":"work","binary":false,"content":"ok"}"""
        val engine = MockEngine { request ->
            assertEquals(HttpMethod.Get, request.method)
            assertEquals("/api/projects/proj%20name/file", request.url.encodedPath)
            assertEquals(requestedPath, request.url.parameters["path"])
            assertEquals("work", request.url.parameters["rev"])
            assertEquals("Bearer secret-token", request.headers[HttpHeaders.Authorization])
            respond(
                responseJson,
                HttpStatusCode.OK,
                headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val client = ViewerClient(
            baseUrl = "http://test",
            token = "secret-token",
            http = HttpClient(engine) {
                install(ContentNegotiation) {
                    json(Json { ignoreUnknownKeys = true })
                }
            },
        )

        try {
            val response = client.file("proj name", requestedPath)

            assertEquals(requestedPath, response.path)
            assertEquals("ok", response.content)
        } finally {
            client.close()
        }
    }
}
