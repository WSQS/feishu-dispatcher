package dev.sopho.fdx.client.network

import io.ktor.client.HttpClient
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.client.plugins.ResponseException
import io.ktor.client.plugins.expectSuccess
import io.ktor.client.request.get
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlinx.coroutines.runBlocking
import java.io.IOException
import java.net.UnknownHostException
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertSame
import kotlin.test.assertTrue

/**
 * `ViewerException.from(e)` 分类器的单测。
 *
 * 分类规则（见 ViewerException.kt）：
 * - ResponseException 且 status 401/403 → AUTH
 * - ResponseException 其它状态（如 500）→ PROTOCOL
 * - UnknownHostException → NETWORK
 * - 其它 Throwable（含普通 IOException）→ NETWORK
 *
 * ResponseException 由 Ktor 在收到非 2xx 响应时抛出，构造它需要一个真实的 HttpResponse；
 * 这里用 MockEngine 触发一次真实请求拿到异常实例，再喂给 `from(e)`，确保分类器在真实异常上工作。
 */
class ViewerExceptionTest {

    /** 用 MockEngine 做一次会失败的请求，把 Ktor 抛出的真实异常取出来。 */
    private fun exceptionFromFailingRequest(status: HttpStatusCode): ResponseException = runBlocking {
        val client = HttpClient(MockEngine {
            respond("", status, headersOf(HttpHeaders.ContentType, "text/plain"))
        })
        try {
            // expectSuccess=true 让非 2xx 响应抛 ClientRequestException / ServerResponseException
            //（都是 ResponseException 子类），无需依赖 DefaultResponseValidation 插件。
            client.get("https://example.test/api/health") { expectSuccess = true }
            error("期望抛 ResponseException，但请求成功了")
        } catch (e: ResponseException) {
            e
        } finally {
            client.close()
        }
    }

    @Test
    fun `401 响应分类为 AUTH`() {
        val e = exceptionFromFailingRequest(HttpStatusCode.Unauthorized)
        assertEquals(401, e.response.status.value)

        val mapped = ViewerException.from(e)

        assertEquals(ViewerException.Kind.AUTH, mapped.kind)
        assertSame(e, mapped.cause)
    }

    @Test
    fun `403 响应分类为 AUTH`() {
        val e = exceptionFromFailingRequest(HttpStatusCode.Forbidden)
        assertEquals(403, e.response.status.value)

        val mapped = ViewerException.from(e)

        assertEquals(ViewerException.Kind.AUTH, mapped.kind)
        assertSame(e, mapped.cause)
    }

    @Test
    fun `500 响应分类为 PROTOCOL`() {
        val e = exceptionFromFailingRequest(HttpStatusCode.InternalServerError)
        assertEquals(500, e.response.status.value)

        val mapped = ViewerException.from(e)

        assertEquals(ViewerException.Kind.PROTOCOL, mapped.kind)
        assertSame(e, mapped.cause)
    }

    @Test
    fun `UnknownHostException 分类为 NETWORK`() {
        val e = UnknownHostException("example.test")

        val mapped = ViewerException.from(e)

        assertEquals(ViewerException.Kind.NETWORK, mapped.kind)
        assertSame(e, mapped.cause)
    }

    @Test
    fun `普通 IOException 分类为 NETWORK`() {
        val e = IOException("broken pipe")

        val mapped = ViewerException.from(e)

        assertEquals(ViewerException.Kind.NETWORK, mapped.kind)
        assertSame(e, mapped.cause)
        // else 分支用原始异常 message 作为 ViewerException message
        assertEquals("broken pipe", mapped.message)
    }

    @Test
    fun `无 message 的异常回退用类名作为 NETWORK message`() {
        // message 为 null 的 Throwable：from() 走 else，msg = e.message ?: simpleName
        val e = MessagelessIOException()

        val mapped = ViewerException.from(e)

        assertEquals(ViewerException.Kind.NETWORK, mapped.kind)
        // 确认回退到异常类的 simpleName（匿名类的 simpleName 为空，故用具名子类）
        assertEquals(MessagelessIOException::class.java.simpleName, mapped.message)
    }
}

/** 具名 IOException 子类且无 message，用于验证 from() 的 message 回退（匿名类 simpleName 为空）。 */
private class MessagelessIOException : IOException()
