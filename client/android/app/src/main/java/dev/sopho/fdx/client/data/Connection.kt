package dev.sopho.fdx.client.data

import kotlinx.serialization.Serializable

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
    val networkId: String = "", // ZeroTier network ID（16 位 hex）；v1 走 libzt 组网，必填
) {
    /**
     * 简单非空校验（地址、token、networkId 都非空才算有效；正式格式校验留后续）。
     *
     * v1 阶段 ZT 是必经路径，所以 networkId 也纳入必填。
     */
    val isValid: Boolean get() = url.isNotBlank() && token.isNotBlank() && networkId.isNotBlank()
}
