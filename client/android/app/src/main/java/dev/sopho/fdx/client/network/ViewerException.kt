package dev.sopho.fdx.client.network

import io.ktor.client.plugins.ResponseException
import java.net.UnknownHostException

/** viewer 调用失败，带分类，供 UI 给用户区分「网络问题」vs「鉴权问题」vs「协议错误」。 */
class ViewerException(
    val kind: Kind,
    message: String,
    cause: Throwable? = null,
) : Exception(message, cause) {
    enum class Kind {
        /** 连不上（DNS/超时/拒绝）—— 检查地址/网络/zerotier。 */
        NETWORK,

        /** 401/403 —— token 错。 */
        AUTH,

        /** 404 —— 资源不存在（如展开的目录已被删除）。 */
        NOT_FOUND,

        /** 其它 HTTP 错误状态 / 协议解析问题。 */
        PROTOCOL,
    }

    companion object {
        fun from(e: Throwable): ViewerException = when (e) {
            is ResponseException -> when (e.response.status.value) {
                401, 403 -> ViewerException(Kind.AUTH, "鉴权失败（token 错？）", e)
                404 -> ViewerException(Kind.NOT_FOUND, "资源不存在", e)
                else -> ViewerException(Kind.PROTOCOL, "HTTP ${e.response.status.value}", e)
            }
            is UnknownHostException ->
                ViewerException(Kind.NETWORK, "无法解析主机（地址错/不可达）", e)
            else -> {
                val msg = e.message ?: e.javaClass.simpleName
                ViewerException(Kind.NETWORK, msg, e)
            }
        }
    }
}
