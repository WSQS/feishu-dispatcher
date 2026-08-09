package dev.sopho.fdx.client.data

import kotlinx.serialization.Serializable

/**
 * ZeroTier 配置（[Connection] 的子模型）：聚合 ZT 相关的开关与参数。
 *
 * [enabled] 是 ZT 模式开关：true 走 libzt（OkHttp engine + SocketFactory），false 走普通
 * HTTP（CIO engine，局域网/已装 ZT 客户端/Tailscale）。[networkId]（16 位 hex，ZeroTier
 * 网络 ID）和 [moonId]（10 位 hex，对应 libzt `zts_moon_orbit` 的 world ID；见
 * docs/research/libzt-moon-api.md）仅在 enabled=true 时有意义。
 */
@Serializable
data class ZerotierConfig(
    val enabled: Boolean = false,
    val networkId: String = "",
    val moonId: String = "",
) {
    /** enabled=true 时 networkId 必填（moonId 可选）；enabled=false 时视为有效（走普通 HTTP）。 */
    val isValid: Boolean get() = !enabled || networkId.isNotBlank()
}

/**
 * viewer 连接配置（领域模型）：定义「配置数据是什么」。
 *
 * 这是核心——[ConnectionStore]（怎么存）和 ConfigScreen（怎么显示）都是对它的
 * 解释/操作，彼此独立。加字段时改这里 + 给默认值，存储（整体序列化）自动跟上。
 */
@Serializable
data class Connection(
    val url: String = "",
    val token: String = "",
    val zerotier: ZerotierConfig = ZerotierConfig(),
) {
    /** 简单非空校验（地址、token 必填）。ZT 配置用 [zerotier.isValid]。 */
    val isValid: Boolean get() = url.isNotBlank() && token.isNotBlank()
}
