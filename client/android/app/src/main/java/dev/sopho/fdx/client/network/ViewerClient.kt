package dev.sopho.fdx.client.network

import android.util.Log
import dev.sopho.fdx.client.data.Connection
import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.engine.cio.CIO
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.plugins.expectSuccess
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.get
import io.ktor.http.URLBuilder
import io.ktor.http.takeFrom
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.net.URI

/** /api/health 的响应体（对应服务端 viewer.py 的 health()）。 */
@Serializable
data class HealthResponse(val ok: Boolean, val version: String)

/** /api/projects 返回的单个项目。 */
@Serializable
data class ProjectItem(
    val name: String,
    val path: String,
    @SerialName("default_agent") val defaultAgent: String,
)

/** /api/projects 的响应体。 */
@Serializable
data class ListProjectsResponse(val items: List<ProjectItem>)

/** /api/projects/{name}/tree 返回的单个文件条目。 */
@Serializable
data class TreeEntry(val path: String, val type: String, val size: Long)

/** /api/projects/{name}/tree 的响应体。 */
@Serializable
data class TreeResponse(val entries: List<TreeEntry>)

/** /api/projects/{name}/tree/children 返回的单个直接子项（目录或文件，无 size）。 */
@Serializable
data class TreeChildrenEntry(val name: String, val path: String, val type: String)

/** /api/projects/{name}/tree/children 的响应体。 */
@Serializable
data class TreeChildrenResponse(val path: String, val entries: List<TreeChildrenEntry>)

/** /api/projects/{name}/file 的响应体。 */
@Serializable
data class FileResponse(
    val path: String,
    val rev: String,
    val binary: Boolean,
    val content: String,
)

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
    // 测试缝：单测可注入 MockEngine 的 HttpClient；默认按 useZerotier 选引擎。
    private val http: HttpClient = defaultHttp(useZerotier, ztHost, ztPort),
) : java.io.Closeable {

    /** GET /api/health —— 存活探针 + 版本。失败抛 [ViewerException]（含分类）。 */
    suspend fun health(): HealthResponse = withContext(Dispatchers.IO) {
        val t = System.nanoTime()
        try {
            val r = http.get {
                url.takeFrom(URLBuilder(baseUrl).apply { pathSegments = listOf("api", "health") }.build())
                bearerAuth(token)
                expectSuccess = true
            }.body<HealthResponse>()
            Log.i(TAG, "health: ${(System.nanoTime() - t) / 1_000_000}ms")
            r
        } catch (e: Exception) {
            Log.w(TAG, "health: failed in ${(System.nanoTime() - t) / 1_000_000}ms", e)
            throw ViewerException.from(e)
        }
    }

    /** GET /api/projects —— 列出所有项目。失败抛 [ViewerException]（含分类）。 */
    suspend fun projects(): ListProjectsResponse = withContext(Dispatchers.IO) {
        val t = System.nanoTime()
        try {
            val r = http.get {
                url.takeFrom(URLBuilder(baseUrl).apply { pathSegments = listOf("api", "projects") }.build())
                bearerAuth(token)
                expectSuccess = true
            }.body<ListProjectsResponse>()
            Log.i(TAG, "projects: ${(System.nanoTime() - t) / 1_000_000}ms (${r.items.size} items)")
            r
        } catch (e: Exception) {
            Log.w(TAG, "projects: failed in ${(System.nanoTime() - t) / 1_000_000}ms", e)
            throw ViewerException.from(e)
        }
    }

    /** GET /api/projects/{name}/tree —— 列文件树。失败抛 [ViewerException]。 */
    suspend fun tree(projectName: String): TreeResponse = withContext(Dispatchers.IO) {
        val t = System.nanoTime()
        try {
            val r = http.get {
                url.takeFrom(
                    URLBuilder(baseUrl).apply { pathSegments = listOf("api", "projects", projectName, "tree") }.build(),
                )
                bearerAuth(token)
                expectSuccess = true
            }.body<TreeResponse>()
            Log.i(TAG, "tree: ${(System.nanoTime() - t) / 1_000_000}ms (${r.entries.size} files)")
            r
        } catch (e: Exception) {
            Log.w(TAG, "tree: failed in ${(System.nanoTime() - t) / 1_000_000}ms", e)
            throw ViewerException.from(e)
        }
    }

    /** GET /api/projects/{name}/tree/children?path= —— 列指定目录的直接子项（按目录加载）。
     *  [path] 是 workspace 相对路径，空串 = 根目录（必填，缺省仍带上 `path=`）。失败抛 [ViewerException]。 */
    suspend fun treeChildren(projectName: String, path: String): TreeChildrenResponse =
        withContext(Dispatchers.IO) {
            val t = System.nanoTime()
            try {
                val r = http.get {
                    url.takeFrom(
                        URLBuilder(baseUrl).apply {
                            pathSegments = listOf("api", "projects", projectName, "tree", "children")
                            parameters.append("path", path)
                        }.build(),
                    )
                    bearerAuth(token)
                    expectSuccess = true
                }.body<TreeChildrenResponse>()
                Log.i(TAG, "treeChildren: ${(System.nanoTime() - t) / 1_000_000}ms ($path ${r.entries.size} items)")
                r
            } catch (e: Exception) {
                Log.w(TAG, "treeChildren: failed in ${(System.nanoTime() - t) / 1_000_000}ms", e)
                throw ViewerException.from(e)
            }
        }

    /** GET /api/projects/{name}/file?path=&rev=work —— 读文件内容。失败抛 [ViewerException]。 */
    suspend fun file(projectName: String, path: String, rev: String = "work"): FileResponse =
        withContext(Dispatchers.IO) {
            val t = System.nanoTime()
            try {
                val r = http.get {
                    url.takeFrom(
                        URLBuilder(baseUrl).apply {
                            pathSegments = listOf("api", "projects", projectName, "file")
                            parameters.append("path", path)
                            parameters.append("rev", rev)
                        }.build(),
                    )
                    bearerAuth(token)
                    expectSuccess = true
                }.body<FileResponse>()
                Log.i(TAG, "file: ${(System.nanoTime() - t) / 1_000_000}ms ($path binary=${r.binary})")
                r
            } catch (e: Exception) {
                Log.w(TAG, "file: failed in ${(System.nanoTime() - t) / 1_000_000}ms", e)
                throw ViewerException.from(e)
            }
        }

    override fun close() = http.close()

    companion object {
        private const val TAG = "ViewerClient"

        /** 从 [Connection] 构造 ViewerClient（按 zerotier.enabled 选 engine + 解析 URL）。 */
        fun fromConnection(conn: Connection): ViewerClient {
            val url = conn.url.trim()
            val host = try { URI(url).host } catch (e: Exception) { "" }
            val port = try { URI(url).port } catch (e: Exception) { -1 }.let { if (it > 0) it else 7321 }
            return ViewerClient(
                baseUrl = url,
                token = conn.token.trim(),
                useZerotier = conn.zerotier.enabled,
                ztHost = host,
                ztPort = port,
            )
        }
    }
}

/** 默认引擎：普通 HTTP 用 CIO；ZeroTier 用 OkHttp + libzt SocketFactory。 */
private fun defaultHttp(useZerotier: Boolean, ztHost: String, ztPort: Int): HttpClient =
    if (useZerotier) {
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
