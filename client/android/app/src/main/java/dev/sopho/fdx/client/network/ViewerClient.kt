package dev.sopho.fdx.client.network

import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.engine.cio.CIO
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.get
import io.ktor.http.URLBuilder
import io.ktor.http.takeFrom
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** /api/health 的响应体（对应服务端 viewer.py 的 health()）。 */
@Serializable
data class HealthResponse(val ok: Boolean, val version: String)

/**
 * 连 daemon viewer 的客户端。封装 [baseUrl] + [token]，按 [useZerotier] 选 engine：
 *
 * - [useZerotier]=false（默认）→ CIO engine（普通 HTTP，局域网/Tailscale）。
 * - [useZerotier]=true → OkHttp engine + [ZeroTierSocketsSocketFactory]，HTTP 走 libzt socket。
 *   此时 [ztHost]/[ztPort] 是 daemon 的 ZeroTier 虚拟 IP + 端口（SocketFactory 连它）；
 *   [baseUrl] 仍用于 URL 拼接（host 应等于 ztHost，端口等于 ztPort）。
 *
 * - [baseUrl] 形如 `http://192.168.x.x:7321` 或 `http://<zerotier-ip>:7321`，**不带尾斜杠**。
 * - [token] viewer 的 bearer token（服务端自动生成、日志打印的那个）。
 *
 * 网络调用都在 [Dispatchers.IO] 上（让调用方更安心）。
 */
class ViewerClient(
    private val baseUrl: String,
    private val token: String,
    useZerotier: Boolean = false,
    ztHost: String = "",
    ztPort: Int = 7321,
) {
    private val http: HttpClient = if (useZerotier) {
        HttpClient(OkHttp) {
            install(ContentNegotiation) {
                json(Json { ignoreUnknownKeys = true })
            }
            engine {
                config {
                    socketFactory(ZeroTierSocketsSocketFactory(ztHost, ztPort))
                }
            }
        }
    } else {
        HttpClient(CIO) {
            install(ContentNegotiation) {
                json(Json { ignoreUnknownKeys = true })
            }
        }
    }

    /** GET /api/health —— 存活探针 + 版本。失败抛 [ViewerException]（含分类）。 */
    suspend fun health(): HealthResponse = withContext(Dispatchers.IO) {
        try {
            http.get {
                url.takeFrom(URLBuilder(baseUrl).apply { pathSegments = listOf("api", "health") }.build())
                bearerAuth(token)
            }.body()
        } catch (e: Exception) {
            throw ViewerException.from(e)
        }
    }

    fun close() = http.close()
}
