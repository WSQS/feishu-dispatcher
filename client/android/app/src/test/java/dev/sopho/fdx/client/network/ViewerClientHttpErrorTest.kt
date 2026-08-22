package dev.sopho.fdx.client.network

import io.ktor.client.HttpClient
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class ViewerClientHttpErrorTest {
    @Test
    fun `health_classifies_401_as_auth`() = runTest {
        assertHttpErrorKind(HttpStatusCode.Unauthorized, ViewerException.Kind.AUTH) { health() }
    }

    @Test
    fun `health_classifies_500_as_protocol`() = runTest {
        assertHttpErrorKind(HttpStatusCode.InternalServerError, ViewerException.Kind.PROTOCOL) { health() }
    }

    @Test
    fun `projects_classifies_401_as_auth`() = runTest {
        assertHttpErrorKind(HttpStatusCode.Unauthorized, ViewerException.Kind.AUTH) { projects() }
    }

    @Test
    fun `projects_classifies_500_as_protocol`() = runTest {
        assertHttpErrorKind(HttpStatusCode.InternalServerError, ViewerException.Kind.PROTOCOL) { projects() }
    }

    private suspend fun assertHttpErrorKind(
        status: HttpStatusCode,
        expectedKind: ViewerException.Kind,
        request: suspend ViewerClient.() -> Unit,
    ) {
        val client = ViewerClient(
            baseUrl = "http://test",
            token = "token",
            http = HttpClient(MockEngine { respond("error", status) }),
        )

        try {
            val error = assertFailsWith<ViewerException> {
                request(client)
            }
            assertEquals(expectedKind, error.kind)
        } finally {
            client.close()
        }
    }
}
